"""The visitor's real address behind a proxy that appends to X-Forwarded-For.

uvicorn's own proxy handling, told to trust every peer (``"*"``, right for
Render where the proxy is the only way in), takes the *leftmost*
X-Forwarded-For entry. Render's proxy appends to whatever header the
visitor sent rather than replacing it, so the leftmost entry is whatever the
visitor typed: one ``curl -H 'X-Forwarded-For: 1.2.3.4'`` per request and
every per-address limit (button presses, searches, admin sign-in) sees a
fresh client each time.

Entries a trusted proxy adds are at the right-hand end, so counting from
there is the only reading a visitor can't steer. ``[web] proxy_hops`` says
how many proxies stand in front: the visitor is that many entries from the
right. With it set, app.py turns uvicorn's handling off and this middleware
does the job instead.
"""

from __future__ import annotations

from collections.abc import Sequence


def forwarded_for(headers: Sequence[tuple[bytes, bytes]]) -> list[str]:
    """Every X-Forwarded-For entry, in order, across repeated header lines
    (which HTTP says to read as one comma-joined list)."""
    entries: list[str] = []
    for name, value in headers:
        if name.lower() == b"x-forwarded-for":
            entries += [e.strip() for e in value.decode("latin-1").split(",") if e.strip()]
    return entries


def visitor_address(entries: list[str], hops: int) -> str | None:
    """The address ``hops`` entries from the right, or the leftmost when the
    chain is shorter than that (a request no outside party has padded, so
    its leftmost entry is the one the first proxy added). None if empty."""
    if not entries:
        return None
    return entries[-hops] if len(entries) >= hops else entries[0]


class ForwardedClient:
    """Rewrite the ASGI client to the visitor's address counted ``hops``
    from the right of X-Forwarded-For, and the scheme to X-Forwarded-Proto's,
    for requests whose direct peer is a trusted proxy. The peer is kept as
    ``request.state.peer`` for the admin page's connection check."""

    def __init__(self, app, hops: int, trusted: Sequence[str]) -> None:
        self.app = app
        self.hops = hops
        self.trusted = frozenset(trusted)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket"):
            peer = scope.get("client")
            scope.setdefault("state", {})["peer"] = peer[0] if peer else ""
            if "*" in self.trusted or (peer and peer[0] in self.trusted):
                address = visitor_address(forwarded_for(scope["headers"]), self.hops)
                if address:
                    scope["client"] = (address, 0)
                # As uvicorn would: the scheme the visitor used, so
                # request.url (and a websocket's) says https behind TLS.
                proto = dict(scope["headers"]).get(b"x-forwarded-proto", b"")
                proto = proto.decode("latin-1").split(",")[0].strip().lower()
                if proto in ("http", "https"):
                    secure = proto == "https"
                    if scope["type"] == "websocket":
                        scope["scheme"] = "wss" if secure else "ws"
                    else:
                        scope["scheme"] = proto
        await self.app(scope, receive, send)
