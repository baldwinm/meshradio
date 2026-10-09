"""Supervised asyncio runtime for meshradio's background work.

Every long-lived loop runs under ``supervise()``: an unhandled exception is
logged loudly and the loop restarts with backoff instead of dying silently.
(A silently-dead cacher task shipped to production once; this makes that
class of failure impossible by construction.) One-shot background work goes
through ``spawn()``, which guarantees a logged traceback at minimum.

A log line alone still reaches no one, so each such failure is also counted
(``record_error``) and ``/healthz`` reports the last hour's count, which the
uptime check alerts on.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Callable, Coroutine
from typing import Any

log = logging.getLogger(__name__)

_BACKOFF_S = (1, 5, 30)

# Unexpected failures, newest last: (when, what). Bounded, so a crash loop
# costs a fixed amount of memory; a thousand in the window is "a lot" either way.
_errors: deque[tuple[float, str]] = deque(maxlen=1000)


def record_error(source: str) -> None:
    """Count one unexpected failure — a crashed loop, a failed background
    task, a request that raised. ``source`` names where (a service or task
    name, ``request``), never what was in it: /healthz is public."""
    _errors.append((time.time(), source))


def recent_errors(window_s: float = 3600) -> dict[str, int]:
    """Failures in the last ``window_s`` seconds, counted by source."""
    cutoff = time.time() - window_s
    counts: dict[str, int] = {}
    for at, source in _errors:
        if at >= cutoff:
            counts[source] = counts.get(source, 0) + 1
    return counts


def clear_errors() -> None:
    _errors.clear()


def supervise(name: str, factory: Callable[[], Coroutine[Any, Any, None]]) -> asyncio.Task:
    """Run ``factory()`` under supervision: a raised exception restarts the
    loop with backoff (1s → 5s → 30s); a clean return ends it deliberately;
    cancellation propagates."""

    async def runner() -> None:
        failures = 0
        while True:
            try:
                await factory()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                delay = _BACKOFF_S[min(failures, len(_BACKOFF_S) - 1)]
                failures += 1
                record_error(name)
                log.exception("service %r crashed (restart #%d in %ds)", name, failures, delay)
                await asyncio.sleep(delay)

    return asyncio.create_task(runner(), name=name)


def spawn(name: str, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """Fire-and-forget with a logged traceback instead of silent death."""
    task = asyncio.create_task(coro, name=name)

    def _report(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception() is not None:
            record_error(name)
            log.error("background task %r failed", name, exc_info=t.exception())

    task.add_done_callback(_report)
    return task


class Service:
    """Base for long-lived services: ``start()`` supervises ``self._run()``,
    ``stop()`` cancels and awaits it. Subclasses implement ``_run``."""

    _task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = supervise(type(self).__name__, self._run)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:  # pragma: no cover — subclasses implement
        raise NotImplementedError
