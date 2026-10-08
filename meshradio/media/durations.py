"""Song lengths for embed hosting.

The embed host never runs yt-dlp, and oEmbed has no length, so a song used
to get one only once a browser had played it — a queue of songs nobody had
heard yet had no total. This service reads lengths off YouTube's watch page
and stores them in the shared rows, so every queue (and archive page) has
them before the songs play.

Queued songs come first: a player publishes ``DURATION_WANTED`` for the
videos it holds without a length, and newly ready tracks are looked up as
they arrive. In between, the rest of the archive is filled newest first, one
request at a time. Each filled length is announced as ``TRACK_DURATION`` so
open players update their queues live.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque

import httpx

from ..bus import DURATION_WANTED, TRACK_DURATION, TRACK_READY, EventBus, Subscription
from ..db import Database
from ..net import http_client
from ..runtime import Service
from . import metadata

log = logging.getLogger(__name__)


class DurationService(Service):
    MAX_ATTEMPTS = 3           # per video, per process; then it waits for a browser report
    FAILURE_STREAK = 5         # consecutive failures that mean YouTube is refusing us...
    BACKOFF_S = 600.0          # ...so pause this long before trying again

    def __init__(
        self, db: Database, bus: EventBus, pace_s: float = 1.5, sweep_s: float = 300.0
    ):
        self.db = db
        self.bus = bus
        self.pace_s = pace_s       # pause between lookups: one request at a time, gently
        self.sweep_s = sweep_s     # how often to re-check the archive when idle
        self._urgent: deque[str] = deque()
        self._backlog: deque[str] = deque()
        self._attempts: dict[str, int] = {}
        self._streak = 0
        self._http: httpx.AsyncClient | None = None

    async def _run(self) -> None:
        sub = self.bus.subscribe(TRACK_READY, DURATION_WANTED)
        try:
            async with http_client(timeout=15) as self._http:
                while True:
                    self._drain(sub)
                    if not self._urgent and not self._backlog:
                        self._backlog.extend(
                            v for v in await self.db.videos_missing_duration()
                            if self._attempts.get(v, 0) < self.MAX_ATTEMPTS
                        )
                    video_id = self._next()
                    if video_id is None:
                        try:
                            topic, payload = await asyncio.wait_for(sub.get(), self.sweep_s)
                        except TimeoutError:
                            continue
                        self._take(topic, payload)
                        continue
                    if await self.resolve(video_id):
                        await asyncio.sleep(self.pace_s)
        finally:
            sub.close()
            self._http = None

    def _drain(self, sub: Subscription) -> None:
        while True:
            try:
                topic, payload = sub.queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._take(topic, payload)

    def _take(self, topic: str, payload: dict) -> None:
        if topic == TRACK_READY:
            track = payload.get("track") or {}
            ids = [] if track.get("duration") else [track.get("video_id")]
        else:
            ids = payload.get("video_ids") or []
        for video_id in ids:
            if video_id and video_id not in self._urgent:
                self._urgent.append(video_id)

    def _next(self) -> str | None:
        for pending in (self._urgent, self._backlog):
            while pending:
                video_id = pending.popleft()
                if self._attempts.get(video_id, 0) < self.MAX_ATTEMPTS:
                    return video_id
        return None

    async def resolve(self, video_id: str) -> bool:
        """Make sure ``video_id`` has a length, looking it up if no row holds
        one. Announces the length either way, so a player holding a stale
        copy catches up. Returns whether YouTube was asked."""
        known = await self.db.known_duration(video_id)
        if known:
            self.bus.publish(TRACK_DURATION, {"video_id": video_id, "duration": known})
            return False
        seconds = await metadata.fetch_duration(video_id, self._http)
        if seconds is None:
            self._attempts[video_id] = self._attempts.get(video_id, 0) + 1
            self._streak += 1
            if self._streak >= self.FAILURE_STREAK:
                log.warning(
                    "song length lookups failing (%d in a row); pausing %.0fs",
                    self._streak, self.BACKOFF_S,
                )
                self._streak = 0
                await asyncio.sleep(self.BACKOFF_S)
            return True
        self._streak = 0
        self._attempts.pop(video_id, None)
        await self.db.fill_video_duration(video_id, seconds)
        self.bus.publish(TRACK_DURATION, {"video_id": video_id, "duration": seconds})
        return True
