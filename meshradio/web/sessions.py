"""Visitor sessions and speaker election for the web UI.

The appliance modes (web/mpv) run one communal radio; public embed hosting
gives every browser its own session — otherwise any visitor could pause
everyone's music and each new connection would steal the speaker role
mid-song.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..bus import EventBus
from ..db import Database
from ..media.player import PlayerService
from ..runtime import supervise

log = logging.getLogger(__name__)

SESSION_COOKIE = "mr_sid"


class SpeakerRegistry:
    """Exactly one connected page is the 'speaker' — the tab that actually
    plays audio. Everyone else is a silent remote. Newest connection wins;
    any tab can claim the role explicitly."""

    def __init__(self) -> None:
        self._conns: list = []

    def join(self, conn) -> None:
        self._conns.append(conn)

    def leave(self, conn) -> None:
        if conn in self._conns:
            self._conns.remove(conn)

    def claim(self, conn) -> None:
        if conn in self._conns:
            self._conns.remove(conn)
            self._conns.append(conn)

    def is_speaker(self, conn) -> bool:
        return bool(self._conns) and self._conns[-1] is conn

    def clients(self) -> list:
        return list(self._conns)


@dataclass
class Session:
    """One visitor's private radio: their own player (queue, position, day)
    and their own speaker election among their tabs."""
    player: PlayerService
    bus: EventBus
    speakers: SpeakerRegistry = field(default_factory=SpeakerRegistry)
    last_seen: float = field(default_factory=time.monotonic)


class SessionManager:
    """Per-visitor sessions for public embed hosting.

    Sessions persist: state snapshots flush to the web_sessions table a few
    seconds after changes, and a returning cookie (or the whole process,
    after a deploy) restores from there — reaping only evicts from memory."""

    def __init__(self, factory: Callable[[EventBus], PlayerService], db: Database):
        self._factory = factory
        self._db = db
        self._sessions: dict[str, Session] = {}
        self._dirty: set[str] = set()
        self._maintenance: asyncio.Task | None = None

    def count(self) -> int:
        return len(self._sessions)

    async def get(self, sid: str) -> Session:
        session = self._sessions.get(sid)
        if session is None:
            out_bus = EventBus()
            player = self._factory(out_bus)
            session = Session(player=player, bus=out_bus)
            self._sessions[sid] = session
            saved = await self._db.load_web_session(sid)
            if saved:
                try:
                    await player.restore(json.loads(saved))
                except Exception:
                    log.exception("session %s… restore failed; starting fresh", sid[:8])
            if player.status == "idle" and player.current is None:
                # Brand-new visitor (or a stale snapshot that restored to
                # nothing): land with the newest day cued, not an empty player.
                await self._cue_latest(player)
            player.on_state = lambda: self._dirty.add(sid)
            log.info("session %s… started (%d live)", sid[:8], len(self._sessions))
            if self._maintenance is None:
                self._maintenance = supervise("session-maintenance", self._maintenance_loop)
        session.last_seen = time.monotonic()
        return session

    async def _cue_latest(self, player: PlayerService) -> None:
        for day in await self._db.archive_days():  # newest first
            if day["tracks"] and await player.cue_day(day["date"]):
                return

    async def _maintenance_loop(self) -> None:
        ticks = 0
        while True:
            await asyncio.sleep(5)
            await self.flush()
            ticks += 1
            if ticks % 60 == 0:  # every ~5 minutes
                await self.reap()

    async def flush(self) -> None:
        """Persist snapshots for sessions whose state changed."""
        while self._dirty:
            sid = self._dirty.pop()
            session = self._sessions.get(sid)
            if session is not None:
                await self._db.save_web_session(
                    sid, json.dumps(session.player.snapshot())
                )

    async def reap(self, max_idle_s: float = 1800) -> None:
        """Evict idle sessions from memory (their snapshots stay on disk for
        a returning visitor) and forget snapshots older than a week."""
        await self.flush()
        now = time.monotonic()
        for sid, session in list(self._sessions.items()):
            if not session.speakers.clients() and now - session.last_seen > max_idle_s:
                del self._sessions[sid]
                await session.player.stop()
                log.info("session %s… reaped (%d live)", sid[:8], len(self._sessions))
        stale = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        await self._db.delete_web_sessions(older_than=stale)
