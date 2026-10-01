"""Per-client ceilings on the requests that cost something.

Nothing bounded how fast one client could press buttons or search. A
press is a session write and a state fan-out; a search is an index lookup
and a page render; neither is expensive, but a script issuing them in a
loop was the one way left to make the radio do work without end. Each
client gets a token bucket per kind: a burst that covers any human, then a
sustained rate well below what a loop produces. Past it, 429 with
``Retry-After``, and htmx leaves the page as it was.

Keyed by the client's address. Behind a trusted proxy (``[web]
trusted_proxies``) uvicorn has already put the visitor's address in the
scope; otherwise it is the peer's, which on a LAN is the visitor anyway.
GETs other than ``/search`` are never limited — pages, partials, the feed
and static files are cheap and cached — and the relay's authenticated
push has its own caps.
"""

from __future__ import annotations

import time

from starlette.responses import PlainTextResponse

# (burst, sustained per second) per client. Thirty presses cover a frantic
# minute at the queue; ten a second sustained is still a hundredth of a loop.
PRESSES = (30, 10.0)
SEARCHES = (15, 3.0)

# Clients tracked before idle ones are swept. A bucket refills to full after
# capacity/rate seconds of quiet, so a client that old is indistinguishable
# from a new one and can be forgotten.
MAX_TRACKED = 10_000

_SAFE = frozenset({"GET", "HEAD", "OPTIONS"})


class Buckets:
    """Token buckets, one per key, created full on first sight."""

    def __init__(self, capacity: int, per_second: float) -> None:
        self.capacity = capacity
        self.rate = per_second
        self._state: dict[str, tuple[float, float]] = {}   # key -> (tokens, at)

    def take(self, key: str, now: float) -> bool:
        """Spend one token for ``key`` if it has one; False means refused."""
        tokens, at = self._state.get(key, (float(self.capacity), now))
        tokens = min(float(self.capacity), tokens + (now - at) * self.rate)
        if tokens < 1.0:
            self._state[key] = (tokens, now)
            return False
        self._state[key] = (tokens - 1.0, now)
        if len(self._state) > MAX_TRACKED:
            self._sweep(now)
        return True

    def _sweep(self, now: float) -> None:
        idle = self.capacity / self.rate if self.rate else float("inf")
        self._state = {k: v for k, v in self._state.items() if now - v[1] < idle}

    def tracked(self) -> int:
        return len(self._state)


class RateLimiter:
    def __init__(
        self,
        app,
        presses: tuple[int, float] | None = None,
        searches: tuple[int, float] | None = None,
    ) -> None:
        self.app = app
        # Read at construction, not at import, so a test can set tiny ones.
        self.presses = Buckets(*(presses or PRESSES))
        self.searches = Buckets(*(searches or SEARCHES))

    def pick(self, method: str, path: str) -> Buckets | None:
        """Which ceiling a request counts against, if any."""
        if method in _SAFE:
            return self.searches if path == "/search" else None
        if path == "/api/ingest":
            return None
        return self.presses

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            buckets = self.pick(scope["method"], scope["path"])
            if buckets is not None:
                client = scope.get("client") or ("", 0)
                if not buckets.take(client[0], time.monotonic()):
                    response = PlainTextResponse(
                        "too many requests; slow down",
                        status_code=429,
                        headers={"retry-after": "1"},
                    )
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)
