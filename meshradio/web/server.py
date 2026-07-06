"""LAN web UI: FastAPI + Jinja2 + htmx, WebSocket for live state.

No JS build chain, ever (architecture §9): htmx is a vendored single file,
templates are plain HTML. The WebSocket forwards bus events; the page reacts
by re-fetching htmx partials.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..bus import EventBus, OUTPUT_CHANGED, PLAYER_STATE, POWER_STATE
from ..db import Database
from ..media.player import PlayerService

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


def create_app(bus: EventBus, db: Database, player: PlayerService, router) -> FastAPI:
    app = FastAPI(title="MeshRadio")
    templates = Jinja2Templates(directory=_HERE / "templates")
    templates.env.filters["mmss"] = _mmss
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")

    def _today() -> str:
        return datetime.now(player.tz).date().isoformat()

    async def day_context() -> dict:
        """The day being played (or today), its theme(s), and the adjacent
        archive days for the prev/next navigation."""
        today = _today()
        day = player.day or today
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
        return templates.TemplateResponse(
            request, "index.html", {"state": player.state(), **await day_context()}
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
        return templates.TemplateResponse(
            request, "partials/now_playing.html", {"state": player.state(), **await day_context()}
        )

    @app.get("/partials/day-nav", response_class=HTMLResponse)
    async def partial_day_nav(request: Request):
        return templates.TemplateResponse(
            request, "partials/day_nav.html", {"state": player.state(), **await day_context()}
        )

    @app.get("/partials/queue", response_class=HTMLResponse)
    async def partial_queue(request: Request):
        return templates.TemplateResponse(
            request, "partials/queue.html", {"state": player.state()}
        )

    # -- JSON API ------------------------------------------------------------

    @app.get("/api/state")
    async def api_state():
        return JSONResponse(player.state())

    @app.post("/api/skip")
    async def api_skip(request: Request):
        await player.skip()
        return await partial_now_playing(request)

    @app.post("/api/pause")
    async def api_pause(request: Request):
        await player.toggle_pause()
        return await partial_now_playing(request)

    @app.post("/api/volume/{level}")
    async def api_volume(request: Request, level: int):
        await player.set_volume(level)
        return await partial_now_playing(request)

    @app.post("/api/seek/{seconds}")
    async def api_seek(seconds: float):
        await player.seek(seconds)
        return JSONResponse({"ok": True, "position": player.position()})

    # NOTE: literal /api/queue/* routes must be registered before the
    # /api/queue/{track_id} catch-all or "clear" gets parsed as a track id.
    @app.post("/api/queue/clear")
    async def api_queue_clear(request: Request):
        await player.clear_queue()
        return await partial_queue(request)

    @app.post("/api/queue/remove/{index}/{track_id}")
    async def api_queue_remove(request: Request, index: int, track_id: int):
        await player.remove_from_queue(index, track_id)
        return await partial_queue(request)

    @app.post("/api/queue/top/{index}/{track_id}")
    async def api_queue_top(request: Request, index: int, track_id: int):
        await player.move_to_front(index, track_id)
        return await partial_queue(request)

    @app.post("/api/queue/{track_id}")
    async def api_enqueue(track_id: int):
        await player.enqueue_track_id(track_id)
        return JSONResponse({"ok": True})

    @app.post("/api/play-day/{date}")
    async def api_play_day(date: str):
        await player.play_day(date)
        return RedirectResponse("/", status_code=303)

    @app.post("/api/play-today")
    async def api_play_today(request: Request):
        """Default play action: today's songs, else the newest archived day."""
        today = _today()
        await player.play_day(today)
        if player.status != "playing":
            for d in await db.archive_days():  # newest first
                if d["date"] < today and d["tracks"]:
                    await player.play_day(d["date"])
                    if player.status == "playing":
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
    async def api_ended(track_id: int):
        """Browser reports its <audio> element finished the track."""
        advanced = await player.notify_ended(track_id)
        return JSONResponse({"advanced": advanced})

    @app.post("/api/radio/start")
    async def api_radio_start(request: Request):
        ok = await player.start_radio()
        return await partial_now_playing(request)

    @app.post("/api/radio/stop")
    async def api_radio_stop(request: Request):
        await player.stop_radio()
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

    async def broadcast_state() -> None:
        """Push fresh state to every page (speaker role may have moved)."""
        for conn in speakers.clients():
            try:
                await conn.send_json({
                    "topic": PLAYER_STATE,
                    "data": {**player.state(), "speaker": speakers.is_speaker(conn)},
                })
            except Exception:
                pass

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        speakers.join(websocket)
        sub = bus.subscribe(PLAYER_STATE, OUTPUT_CHANGED, POWER_STATE)

        async def recv_loop():
            while True:
                msg = await websocket.receive_text()
                if msg == "claim":
                    speakers.claim(websocket)
                    await broadcast_state()

        async def send_loop():
            async for topic, payload in sub:
                if topic == PLAYER_STATE:
                    payload = {**payload, "speaker": speakers.is_speaker(websocket)}
                await websocket.send_json({"topic": topic, "data": payload})

        tasks = [asyncio.create_task(recv_loop()), asyncio.create_task(send_loop())]
        try:
            await broadcast_state()  # joining may reassign the speaker
            await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception:
            log.debug("websocket closed", exc_info=True)
        finally:
            for task in tasks:
                task.cancel()
            sub.close()
            speakers.leave(websocket)
            await broadcast_state()  # promote the next speaker

    return app
