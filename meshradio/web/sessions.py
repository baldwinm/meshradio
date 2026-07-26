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
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..bus import EventBus, TRACK_READY
from ..db import Database
from ..media.player import PlayerService
from ..runtime import supervise

log = logging.getLogger(__name__)

SESSION_COOKIE = "mr_sid"

# Session ids are always server-issued secrets.token_hex(16). Anything else in
# the cookie is forged — reject it before it becomes a dict key and a DB row.
_SID_RE = re.compile(r"\A[0-9a-f]{32}\Z")


def valid_sid(sid: str | None) -> bool:
    return bool(sid) and _SID_RE.match(sid) is not None


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

    # Hard ceiling on live in-memory sessions. Every session carries a
    # PlayerService plus a supervised task, so without a cap a bot spraying
    # fresh cookies grows the process without bound. Legit idle visitors are
    # reaped long before this matters; hitting it evicts the stalest session.
    MAX_SESSIONS = 512

    def __init__(self, factory: Callable[[EventBus], PlayerService], db: Database,
                 bus: EventBus | None = None, tz=timezone.utc):
        self._factory = factory
        self._db = db
        self._bus = bus                # shared bus: TRACK_READY announces new songs
        self._tz = tz                  # channel-local day boundary
        self._sessions: dict[str, Session] = {}
        self._dirty: set[str] = set()
        self._maintenance: asyncio.Task | None = None
        self._newest_day: str | None = None   # newest-day query cache
        self._rolled_day: str | None = None   # last day the watcher rolled tabs to

    def count(self) -> int:
        return len(self._sessions)

    async def get(self, sid: str) -> Session:
        session = self._sessions.get(sid)
        if session is None:
            if len(self._sessions) >= self.MAX_SESSIONS:
                await self._evict_one()
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
            # A brand-new or freshly-restored session lands on the newest day.
            # A restored "playing" flag is stale — a page load never has audio
            # going yet in embed mode — so we advance it too; only a warm,
            # actually-playing session (handled below) is spared.
            await self._cue_latest(player)
            player.on_state = lambda: self._dirty.add(sid)
            log.info("session %s… started (%d live)", sid[:8], len(self._sessions))
            if self._maintenance is None:
                self._maintenance = supervise("session-maintenance", self._maintenance_loop)
                if self._bus is not None:
                    supervise("session-day-watch", self._watch_new_days)
        elif session.player.status != "playing" and session.player.day != self._local_today(session.player):
            # Existing (warm) session that isn't mid-playback and is parked on an
            # older day: roll it forward if a newer day has appeared since (e.g.
            # overnight), so "Now Playing" always shows the latest day. A session
            # that's genuinely playing is left alone — never yank a listener.
            await self._cue_latest(session.player)
        session.last_seen = time.monotonic()
        return session

    def _local_today(self, player: PlayerService) -> str:
        return datetime.now(player.tz).date().isoformat()

    async def _evict_one(self) -> None:
        """Drop the stalest session to make room (cap reached). Prefer one with
        no connected sockets; fall back to oldest last_seen. State is flushed
        first, so a legit visitor caught in an eviction just restores."""
        candidates = sorted(self._sessions.items(), key=lambda kv: kv[1].last_seen)
        sid, session = next(
            ((s, sess) for s, sess in candidates if not sess.speakers.clients()),
            candidates[0],
        )
        await self._db.save_web_session(sid, json.dumps(session.player.snapshot()))
        self._dirty.discard(sid)
        del self._sessions[sid]
        await session.player.stop()
        log.warning("session cap %d hit: evicted %s…", self.MAX_SESSIONS, sid[:8])

    async def _newest_day_with_tracks(self, refresh: bool = False) -> str | None:
        """Newest archive day that has songs. This backs every request from an
        idle session, so it's cached — but only while the known newest day IS
        the local today: nothing newer can exist until midnight, and before
        that (a session parked on yesterday) it must re-query so a page load
        still rolls forward the moment today's first song lands."""
        if (
            not refresh
            and self._newest_day is not None
            and self._newest_day == datetime.now(self._tz).date().isoformat()
        ):
            return self._newest_day
        newest = next((d["date"] for d in await self._db.archive_days() if d["tracks"]), None)
        if newest is not None:
            self._newest_day = newest
        return newest

    async def _cue_latest(self, player: PlayerService) -> None:
        """Cue the newest day that has songs. A no-op when the player is already
        parked on that newest day, so a listener keeps their spot; otherwise it
        moves them forward. Callers decide whether to spare active playback."""
        newest = await self._newest_day_with_tracks()
        if newest is None:
            return
        if player.day == newest and player.current is not None:
            return  # already on the newest day — keep their position
        await player.cue_day(newest)

    async def _watch_new_days(self) -> None:
        """When the first song of a newer day lands, roll idle open tabs onto it
        with no reload — the re-cue publishes state to each session's sockets."""
        sub = self._bus.subscribe(TRACK_READY)
        try:
            async for _topic, payload in sub:
                await self._advance_idle_to_newest(payload.get("track"))
        finally:
            sub.close()

    async def _advance_idle_to_newest(self, track: dict | None) -> None:
        if not track or track.get("source") == "radio" or not self._sessions:
            return
        # Cheap pre-filter: only a track on a day newer than the last one we
        # rolled to can change anything, so backfill of old days never hits
        # the DB. Dedup state is ``_rolled_day``, deliberately separate from
        # the ``_newest_day`` query cache that requests also refresh —
        # otherwise a request racing this event could make the watcher skip
        # rolling the other idle tabs.
        mesh_ts = track.get("mesh_ts")
        if mesh_ts and self._rolled_day is not None:
            day = datetime.fromtimestamp(float(mesh_ts), timezone.utc).astimezone(
                self._tz).date().isoformat()
            if day <= self._rolled_day:
                return
        newest = await self._newest_day_with_tracks(refresh=True)
        if newest is None or newest == self._rolled_day:
            return
        self._rolled_day = newest
        moved = 0
        for session in list(self._sessions.values()):
            player = session.player
            if player.status != "playing" and player.day != newest:
                await self._cue_latest(player)   # publishes state → tab updates live
                moved += 1
        if moved:
            log.info("new day %s: rolled %d idle session(s) forward", newest, moved)

    async def _maintenance_loop(self) -> None:
        ticks = 0
        while True:
            await asyncio.sleep(5)
            await self.flush()
            ticks += 1
            if ticks % 60 == 0:  # every ~5 minutes
                await self.reap()

    async def flush(self) -> None:
        """Persist snapshots for sessions whose state changed — one commit for
        the whole batch, not one per session."""
        batch: list[tuple[str, str]] = []
        while self._dirty:
            sid = self._dirty.pop()
            session = self._sessions.get(sid)
            if session is not None:
                batch.append((sid, json.dumps(session.player.snapshot())))
        await self._db.save_web_sessions(batch)

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
