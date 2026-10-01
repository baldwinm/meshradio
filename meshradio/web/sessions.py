"""Visitor sessions and speaker election for the web UI.

The appliance modes (web/mpv) run one communal radio; public embed hosting
gives every browser its own session — otherwise any visitor could pause
everyone's music and each new connection would steal the speaker role
mid-song.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ..bus import TRACK_READY, EventBus
from ..db import Database
from ..media.player import PlayerService
from ..runtime import supervise

log = logging.getLogger(__name__)

SESSION_COOKIE = "mr_sid"

# The settings row holding the key cookies are signed with. In the archive
# rather than in memory so a cookie outlives a redeploy the way the snapshot
# it names does.
SECRET_KEY = "web.session_secret"

# A cookie is ``<sid>.<signature>``: the session id (secrets.token_hex(16),
# the key in memory and on disk) and the first 32 hex digits of an HMAC over
# it. A session costs a player, a task and a row on disk, and a cookie is the
# only thing that opens one — so a value this server didn't sign (forged,
# mangled, sprayed by a bot, or minted under an earlier key) is worth
# nothing, not a session nothing could ever present again.
_COOKIE_RE = re.compile(r"\A([0-9a-f]{32})\.([0-9a-f]{32})\Z")


def _sign(sid: str, secret: bytes) -> str:
    return hmac.new(secret, sid.encode(), hashlib.sha256).hexdigest()[:32]


def issue_cookie(secret: bytes) -> tuple[str, str]:
    """A fresh session id and the cookie value that proves this server set it."""
    sid = secrets.token_hex(16)
    return sid, f"{sid}.{_sign(sid, secret)}"


def verify_cookie(value: str | None, secret: bytes) -> str | None:
    """The session id a cookie names, if this server issued it; else None."""
    match = _COOKIE_RE.match(value or "")
    if match is None:
        return None
    sid, signature = match.groups()
    return sid if hmac.compare_digest(signature, _sign(sid, secret)) else None


# Ceilings on open WebSockets. A visitor's own tabs are a handful; a bot with
# one cookie could otherwise open sockets without end, each a pair of tasks
# and one more target for every state fan-out. The communal (appliance)
# registry is shared by everyone on the LAN, so it gets more room; the
# process-wide cap (web/ws.py) is the backstop across all sessions.
MAX_SOCKETS_PER_SESSION = 8
MAX_SOCKETS_COMMUNAL = 64


class SpeakerRegistry:
    """Exactly one connected page is the 'speaker' — the tab that actually
    plays audio. Everyone else is a silent remote. Newest connection wins;
    any tab can claim the role explicitly."""

    def __init__(self, max_clients: int = MAX_SOCKETS_COMMUNAL) -> None:
        self._conns: list = []
        self.max_clients = max_clients

    def full(self) -> bool:
        return len(self._conns) >= self.max_clients

    def join(self, conn) -> bool:
        """Add a page; False (and nothing changes) once the registry is full."""
        if self.full():
            return False
        self._conns.append(conn)
        return True

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
    speakers: SpeakerRegistry = field(
        default_factory=lambda: SpeakerRegistry(MAX_SOCKETS_PER_SESSION)
    )
    last_seen: float = field(default_factory=time.monotonic)


class SessionManager:
    """Per-visitor sessions for public embed hosting.

    Sessions persist: state snapshots flush to the web_sessions table a few
    seconds after changes, and a returning cookie (or the whole process,
    after a deploy) restores from there — reaping only evicts from memory.

    A session is opened by the page's WebSocket connecting, by a POST, or
    by a returning cookie that has a snapshot on disk — never by a bare GET,
    and only ever for a cookie this server signed (``verify_cookie``). A
    crawler walking the sitemap, or a bot spraying requests with no cookie
    or a forged one, used to mint a player, a task and a row on disk per
    request and churn the cap; now a visitor with no session gets
    ``preview()``, a cued player that is thrown away with the response."""

    # Hard ceiling on live in-memory sessions. Every session carries a
    # PlayerService plus a supervised task, so without a cap a bot spraying
    # fresh cookies grows the process without bound. Legit idle visitors are
    # reaped long before this matters; hitting it evicts the stalest session.
    MAX_SESSIONS = 512

    def __init__(self, factory: Callable[[EventBus], PlayerService], db: Database,
                 bus: EventBus | None = None, tz=UTC):
        self._factory = factory
        self._db = db
        self._bus = bus                # shared bus: TRACK_READY announces new songs
        self._tz = tz                  # channel-local day boundary
        self._sessions: dict[str, Session] = {}
        self._dirty: set[str] = set()
        self._maintenance: asyncio.Task | None = None
        self._day_watch: asyncio.Task | None = None
        self._newest_day: str | None = None   # newest-day query cache
        self._rolled_day: str | None = None   # last day the watcher rolled tabs to
        self._secret: bytes | None = None     # cookie signing key, see secret()
        self._secret_lock = asyncio.Lock()

    def count(self) -> int:
        return len(self._sessions)

    async def secret(self) -> bytes:
        """The key session cookies are signed with: created on first use and
        kept in the settings table, so the cookies visitors already hold keep
        naming their sessions across a redeploy."""
        if self._secret is None:
            async with self._secret_lock:
                if self._secret is None:
                    stored = await self._db.get_setting(SECRET_KEY)
                    if not stored:
                        stored = secrets.token_hex(32)
                        await self._db.set_setting(SECRET_KEY, stored)
                    self._secret = bytes.fromhex(stored)
        return self._secret

    async def get(self, sid: str) -> Session:
        """The visitor's session, opened (fresh or from its snapshot) if it
        isn't live. What a POST and the WebSocket use: both mean a real
        browser is driving a player of its own."""
        session = self._sessions.get(sid)
        if session is None:
            session = await self._open(sid, await self._db.load_web_session(sid))
        else:
            await self._refresh(session)
        session.last_seen = time.monotonic()
        return session

    async def lookup(self, sid: str) -> Session | None:
        """The visitor's session if one exists — live, or on disk from an
        earlier visit — else ``None``. What a GET uses: a page view never
        opens a session on its own, so an unknown cookie (or none) leaves
        nothing behind, and the caller shows ``preview()`` instead."""
        session = self._sessions.get(sid)
        if session is None:
            saved = await self._db.load_web_session(sid)
            if saved is None:
                return None
            session = await self._open(sid, saved)
        else:
            await self._refresh(session)
        session.last_seen = time.monotonic()
        return session

    async def preview(self) -> PlayerService:
        """A cued player for a visitor with no session: what the landing page
        shows until its WebSocket (or a first press) opens one. Not registered,
        not started, not saved — it lives for one response."""
        player = self._factory(EventBus())
        await self._cue_latest(player)
        return player

    async def _open(self, sid: str, saved: str | None) -> Session:
        if len(self._sessions) >= self.MAX_SESSIONS:
            await self._evict_one()
        out_bus = EventBus()
        player = self._factory(out_bus)
        session = Session(player=player, bus=out_bus)
        self._sessions[sid] = session
        if saved:
            try:
                await player.restore(json.loads(saved))
            except Exception:
                log.exception("session %s… restore failed; starting fresh", sid[:8])
        # A brand-new or freshly-restored session lands on the newest day.
        # A restored "playing" flag is stale — a page load never has audio
        # going yet in embed mode — so we advance it too; only a warm,
        # actually-playing session (see _refresh) is spared.
        await self._cue_latest(player)
        player.on_state = lambda: self._dirty.add(sid)
        # The manager owns the player's lifetime: started here, stopped by
        # reap/evict. (A preview() player is never started.)
        player.start()
        log.info("session %s… started (%d live)", sid[:8], len(self._sessions))
        if self._maintenance is None:
            self._maintenance = supervise("session-maintenance", self._maintenance_loop)
            if self._bus is not None:
                self._day_watch = supervise("session-day-watch", self._watch_new_days)
        return session

    async def stop(self) -> None:
        """Shut down cleanly: write every changed snapshot and stop every
        player. The app's lifespan calls this on shutdown; without it a
        deploy or restart dropped whatever the last flush interval (5 s)
        hadn't written, for every visitor at once."""
        for task in (self._maintenance, self._day_watch):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._maintenance = self._day_watch = None
        await self.flush()
        sessions, self._sessions = self._sessions, {}
        for session in sessions.values():
            await session.player.stop()
        if sessions:
            log.info("flushed and stopped %d session(s)", len(sessions))

    async def _refresh(self, session: Session) -> None:
        """A warm session that isn't mid-playback and is parked on an older
        day rolls forward if a newer day has appeared since (e.g. overnight),
        so "Now Playing" always shows the latest day. A session that's
        genuinely playing is left alone — never yank a listener."""
        player = session.player
        if player.status != "playing" and player.day != self._local_today(player):
            await self._cue_latest(player)

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
        newest = await self._db.newest_day_with_tracks()
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
        assert self._bus is not None   # only started with a shared bus (see _open)
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
            day = datetime.fromtimestamp(float(mesh_ts), UTC).astimezone(
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
        stale = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        await self._db.delete_web_sessions(older_than=stale)
