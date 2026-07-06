"""Tiny in-process pub/sub event bus.

Every module in meshradio communicates through this bus: publishers call
``publish(topic, payload)``, subscribers get an async iterator of
``(topic, payload)`` tuples. Payloads are plain dicts so events stay
serializable (the web WebSocket forwards them verbatim).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

log = logging.getLogger(__name__)

# Topic constants — the full event vocabulary lives here so it is greppable.
TRACK_DISCOVERED = "track.discovered"  # ingest -> cacher: new link seen on the channel
TRACK_READY = "track.ready"            # cacher -> player: audio cached, playable
TRACK_FAILED = "track.failed"          # cacher: audio could not be fetched (metadata-only)
THEME_CREATED = "theme.created"        # ingest: a new daily theme row exists
PLAYER_STATE = "player.state"          # player -> panel/web: full player state snapshot
OUTPUT_CHANGED = "output.changed"      # audio routing: default sink switched
POWER_STATE = "power.state"            # fuel gauge: battery percent / charging
INGEST_STATUS = "ingest.status"        # mesh/corescope health for the Status screen


class Subscription:
    """A single subscriber's view of the bus. Async-iterable; call close() when done."""

    def __init__(self, bus: "EventBus", topics: tuple[str, ...], maxsize: int = 256):
        self._bus = bus
        self.topics = topics
        self.queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=maxsize)
        self._closed = False

    def __aiter__(self) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        return self

    async def __anext__(self) -> tuple[str, dict[str, Any]]:
        if self._closed:
            raise StopAsyncIteration
        return await self.queue.get()

    async def get(self) -> tuple[str, dict[str, Any]]:
        return await self.queue.get()

    def close(self) -> None:
        self._closed = True
        self._bus._unsubscribe(self)


class EventBus:
    def __init__(self) -> None:
        self._subs: list[Subscription] = []

    def subscribe(self, *topics: str) -> Subscription:
        """Subscribe to one or more topics. No topics = all topics."""
        sub = Subscription(self, topics)
        self._subs.append(sub)
        return sub

    def _unsubscribe(self, sub: Subscription) -> None:
        if sub in self._subs:
            self._subs.remove(sub)

    def publish(self, topic: str, payload: dict[str, Any] | None = None) -> None:
        """Fan a message out to every matching subscriber. Never blocks the publisher:
        if a subscriber's queue is full its oldest event is dropped (slow consumers
        lose history, they don't stall the radio)."""
        payload = payload or {}
        for sub in self._subs:
            if sub.topics and topic not in sub.topics:
                continue
            try:
                sub.queue.put_nowait((topic, payload))
            except asyncio.QueueFull:
                try:
                    sub.queue.get_nowait()
                    sub.queue.put_nowait((topic, payload))
                except asyncio.QueueEmpty:  # pragma: no cover - race guard
                    pass
                log.warning("slow subscriber on %s dropped an event", topic)
