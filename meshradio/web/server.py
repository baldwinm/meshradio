"""LAN web UI: FastAPI + Jinja2 + htmx, WebSocket for live state.

No JS build chain, ever (architecture §9): htmx is a vendored single file,
templates are plain HTML. The WebSocket forwards bus events; the page reacts
by re-fetching htmx partials.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..bus import EventBus, INGEST_STATUS, OUTPUT_CHANGED, PLAYER_STATE, POWER_STATE
from ..db import Database
from ..media.player import PlayerService
from ..runtime import supervise

log = logging.getLogger(__name__)

_HERE = Path(__file__).parent


class SpeakerRegistry:
    """Exactly one connected page is the 'speaker' — the tab that actually
    plays audio in web-playback mode. Everyone else is a silent remote.
    Newest connection wins; any tab can claim the role explicitly."""

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

SESSION_COOKIE = "mr_sid"


@dataclass
class Session:
    """One visitor's private radio: their own player (queue, position, day)
    and their own speaker election among their tabs."""
    player: PlayerService
    bus: EventBus
    speakers: SpeakerRegistry = field(default_factory=SpeakerRegistry)
    last_seen: float = field(default_factory=time.monotonic)


class SessionManager:
    """Per-visitor sessions for public embed hosting. The appliance modes
    (web/mpv) are one communal radio and never use this; in embed mode a
    shared player would let any visitor pause everyone's music and every
    new connection would steal the speaker role mid-song.

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
            player.on_state = lambda: self._dirty.add(sid)
            log.info("session %s… started (%d live)", sid[:8], len(self._sessions))
            if self._maintenance is None:
                self._maintenance = supervise("session-maintenance", self._maintenance_loop)
        session.last_seen = time.monotonic()
        return session

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


_AUDIO_TYPES = {
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".webm": "audio/webm",
}


def _mmss(value) -> str:
    """Seconds → 'm:ss' (or 'h:mm:ss'); empty string for unknown durations."""
    if value is None:
        return ""
    value = int(value)
    if value >= 3600:
        return f"{value // 3600}:{value % 3600 // 60:02d}:{value % 60:02d}"
    return f"{value // 60}:{value % 60:02d}"


def create_app(
    bus: EventBus,
    db: Database,
    player: PlayerService,
    router,
    ingest=None,
    ingest_token: str = "",
    player_factory: Callable[[EventBus], PlayerService] | None = None,
) -> FastAPI:
    # Ingest freshness for /healthz: updated by successful relay pushes and,
    # via the lifespan watcher below, by successful CoreScope polls.
    health: dict = {"last_ingest": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def watch_ingest():
            sub = bus.subscribe(INGEST_STATUS)
            try:
                async for _topic, payload in sub:
                    if payload.get("corescope") == "ok":
                        health["last_ingest"] = time.time()
            finally:
                sub.close()

        task = supervise("ingest-health-watch", watch_ingest)
        yield
        task.cancel()

    app = FastAPI(title="MeshRadio", lifespan=lifespan)
    templates = Jinja2Templates(directory=_HERE / "templates")
    templates.env.filters["mmss"] = _mmss
    # Cache-buster: browsers can hold stale CSS across app updates otherwise.
    templates.env.globals["asset_v"] = int((_HERE / "static" / "style.css").stat().st_mtime)
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")

    # Per-visitor sessions (public embed hosting) vs one communal player
    # (the appliance). A cookie names the session; each browser gets its own.
    sessions = SessionManager(player_factory, db) if player_factory else None
    app.state.sessions = sessions

    if sessions is not None:
        @app.middleware("http")
        async def ensure_session_cookie(request: Request, call_next):
            sid = request.cookies.get(SESSION_COOKIE)
            fresh = sid is None
            if fresh:
                sid = secrets.token_hex(16)
            request.state.sid = sid
            response = await call_next(request)
            if fresh:
                response.set_cookie(
                    SESSION_COOKIE, sid,
                    max_age=365 * 24 * 3600, httponly=True, samesite="lax",
                )
            return response

    async def get_player(request: Request) -> PlayerService:
        if sessions is None:
            return player
        sid = getattr(request.state, "sid", None) or request.cookies.get(SESSION_COOKIE)
        return (await sessions.get(sid or "anonymous")).player

    def _today() -> str:
        return datetime.now(player.tz).date().isoformat()

    async def day_context(p: PlayerService) -> dict:
        """The day being played (or today), its theme(s), and the adjacent
        archive days for the prev/next navigation."""
        today = _today()
        day = p.day or today
        themes = await db.themes_for_day(day)
        days = sorted(d["date"] for d in await db.archive_days() if d["tracks"])
        return {
            "day": day,
            "today": today,
            "theme_titles": [t["title"] for t in themes],
            "prev_day": max((d for d in days if d < day), default=None),
            "next_day": min((d for d in days if day < d <= today), default=None),
        }

    # -- pages -------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        p = await get_player(request)
        return templates.TemplateResponse(
            request, "index.html", {"state": p.state(), **await day_context(p)}
        )

    @app.get("/archive", response_class=HTMLResponse)
    async def archive(request: Request):
        days = await db.archive_days()
        return templates.TemplateResponse(request, "archive.html", {"days": days})

    @app.get("/archive/{date}", response_class=HTMLResponse)
    async def archive_day(request: Request, date: str):
        themes = await db.themes_for_day(date)
        for theme in themes:
            theme["tracks"] = await db.tracks_for_theme(theme["id"])
        return templates.TemplateResponse(
            request, "archive_day.html", {"date": date, "themes": themes}
        )

    # -- htmx partials -----------------------------------------------------

    @app.get("/partials/now-playing", response_class=HTMLResponse)
    async def partial_now_playing(request: Request):
        p = await get_player(request)
        return templates.TemplateResponse(
            request, "partials/now_playing.html", {"state": p.state(), **await day_context(p)}
        )

    @app.get("/partials/day-nav", response_class=HTMLResponse)
    async def partial_day_nav(request: Request):
        p = await get_player(request)
        return templates.TemplateResponse(
            request, "partials/day_nav.html", {"state": p.state(), **await day_context(p)}
        )

    @app.get("/partials/queue", response_class=HTMLResponse)
    async def partial_queue(request: Request):
        return templates.TemplateResponse(
            request, "partials/queue.html", {"state": (await get_player(request)).state()}
        )

    # -- JSON API ------------------------------------------------------------

    @app.get("/api/state")
    async def api_state(request: Request):
        return JSONResponse((await get_player(request)).state())

    @app.post("/api/skip")
    async def api_skip(request: Request):
        await (await get_player(request)).skip()
        return await partial_now_playing(request)

    @app.post("/api/pause")
    async def api_pause(request: Request):
        await (await get_player(request)).toggle_pause()
        return await partial_now_playing(request)

    @app.post("/api/volume/{level}")
    async def api_volume(request: Request, level: int):
        await (await get_player(request)).set_volume(level)
        return await partial_now_playing(request)

    @app.post("/api/seek/{seconds}")
    async def api_seek(request: Request, seconds: float):
        p = await get_player(request)
        await p.seek(seconds)
        return JSONResponse({"ok": True, "position": p.position()})

    # NOTE: literal /api/queue/* routes must be registered before the
    # /api/queue/{track_id} catch-all or "clear" gets parsed as a track id.
    @app.post("/api/queue/clear")
    async def api_queue_clear(request: Request):
        await (await get_player(request)).clear_queue()
        return await partial_queue(request)

    @app.post("/api/queue/remove/{index}/{track_id}")
    async def api_queue_remove(request: Request, index: int, track_id: int):
        await (await get_player(request)).remove_from_queue(index, track_id)
        return await partial_queue(request)

    @app.post("/api/queue/top/{index}/{track_id}")
    async def api_queue_top(request: Request, index: int, track_id: int):
        await (await get_player(request)).move_to_front(index, track_id)
        return await partial_queue(request)

    @app.post("/api/queue/{track_id}")
    async def api_enqueue(request: Request, track_id: int):
        await (await get_player(request)).enqueue_track_id(track_id)
        return JSONResponse({"ok": True})

    @app.post("/api/play-day/{date}")
    async def api_play_day(request: Request, date: str):
        await (await get_player(request)).play_day(date)
        return RedirectResponse("/", status_code=303)

    @app.post("/api/play-today")
    async def api_play_today(request: Request):
        """Default play action: today's songs, else the newest archived day."""
        p = await get_player(request)
        today = _today()
        await p.play_day(today)
        if p.status != "playing":
            for d in await db.archive_days():  # newest first
                if d["date"] < today and d["tracks"]:
                    await p.play_day(d["date"])
                    if p.status == "playing":
                        break
        return await partial_now_playing(request)

    @app.get("/audio/{track_id}")
    async def audio(track_id: int):
        """Stream a cached track to the browser (web playback)."""
        track = await db.track_by_id(track_id)
        if not track or track["cache_status"] != "ready" or not track["cache_path"]:
            raise HTTPException(404, "track not cached")
        path = Path(track["cache_path"])
        if not path.exists():
            raise HTTPException(404, "cache file missing")
        media_type = _AUDIO_TYPES.get(path.suffix.lower(), "application/octet-stream")
        return FileResponse(path, media_type=media_type)

    @app.post("/api/ended/{track_id}")
    async def api_ended(request: Request, track_id: int):
        """Browser reports its <audio>/embed player finished the track."""
        advanced = await (await get_player(request)).notify_ended(track_id)
        return JSONResponse({"advanced": advanced})

    @app.post("/api/duration/{track_id}/{seconds}")
    async def api_duration(request: Request, track_id: int, seconds: float):
        """The embed speaker tab reports a track's real duration (embed
        tracks start without one — oEmbed metadata has no length)."""
        await (await get_player(request)).report_duration(track_id, seconds)
        return JSONResponse({"ok": True})

    @app.post("/api/ingest")
    async def api_ingest(request: Request):
        """Relay receiver: a home node pushes channel messages here when this
        instance can't poll CoreScope itself (Cloudflare challenges
        datacenter IPs). Shared-token auth; the normal ingest pipeline's
        dedupe makes replays harmless."""
        if not ingest_token or ingest is None:
            raise HTTPException(404)
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied, f"Bearer {ingest_token}"):
            raise HTTPException(401, "bad token")
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(400, "invalid JSON")
        inserted = 0
        for msg in (payload.get("messages") or [])[:5000]:
            try:
                meta = msg.get("meta")
                inserted += await ingest.handle_message(
                    sender=str(msg["sender"]),
                    text=str(msg["text"]),
                    ts=float(msg["ts"]),
                    source="corescope",  # relayed community-channel history
                    meta=meta if isinstance(meta, dict) else None,
                )
            except (KeyError, TypeError, ValueError):
                continue  # skip malformed entries, keep the batch going
        health["last_ingest"] = time.time()
        # Total lets the pusher detect a wiped DB (ephemeral hosting) and
        # reset its cursor for a full re-backfill.
        return JSONResponse({
            "ok": True,
            "inserted": inserted,
            "tracks": await db.channel_track_count(),
        })

    @app.get("/healthz")
    async def healthz():
        """Liveness + basic freshness; Render's health check hits this."""
        last = health["last_ingest"]
        return JSONResponse({
            "ok": True,
            "tracks": await db.channel_track_count(),
            "sessions": sessions.count() if sessions else None,
            "ingest_age_s": round(time.time() - last, 1) if last else None,
        })

    @app.post("/api/radio/start")
    async def api_radio_start(request: Request):
        await (await get_player(request)).start_radio()
        return await partial_now_playing(request)

    @app.post("/api/radio/stop")
    async def api_radio_stop(request: Request):
        await (await get_player(request)).stop_radio()
        return await partial_now_playing(request)

    @app.post("/api/output/{name}")
    async def api_output(name: str):
        ok = await router.set_output(name)
        return JSONResponse({"ok": ok, "output": router.current()})

    @app.get("/api/outputs")
    async def api_outputs():
        return JSONResponse({"outputs": router.outputs(), "current": router.current()})

    # -- WebSocket -----------------------------------------------------------

    speakers = SpeakerRegistry()
    app.state.speakers = speakers

    async def broadcast_state(reg: SpeakerRegistry, p: PlayerService) -> None:
        """Push fresh state to a session's pages (speaker role may have moved)."""
        for conn in reg.clients():
            try:
                await conn.send_json({
                    "topic": PLAYER_STATE,
                    "data": {**p.state(), "speaker": reg.is_speaker(conn)},
                })
            except Exception:
                pass

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        if sessions is not None:
            # Per-visitor session: this browser's own player/bus/speakers.
            sid = websocket.cookies.get(SESSION_COOKIE) or secrets.token_hex(16)
            session = await sessions.get(sid)
            reg, p = session.speakers, session.player
            sub = session.bus.subscribe(PLAYER_STATE)
        else:
            reg, p = speakers, player
            sub = bus.subscribe(PLAYER_STATE, OUTPUT_CHANGED, POWER_STATE)
        reg.join(websocket)

        async def recv_loop():
            while True:
                msg = await websocket.receive_text()
                if msg == "claim":
                    reg.claim(websocket)
                    await broadcast_state(reg, p)

        async def send_loop():
            async for topic, payload in sub:
                if topic == PLAYER_STATE:
                    payload = {**payload, "speaker": reg.is_speaker(websocket)}
                await websocket.send_json({"topic": topic, "data": payload})

        tasks = [asyncio.create_task(recv_loop()), asyncio.create_task(send_loop())]
        try:
            await broadcast_state(reg, p)  # joining may reassign the speaker
            await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception:
            log.debug("websocket closed", exc_info=True)
        finally:
            for task in tasks:
                task.cancel()
            sub.close()
            reg.leave(websocket)
            await broadcast_state(reg, p)  # promote the next speaker

    return app
