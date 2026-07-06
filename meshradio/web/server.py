"""LAN web UI: FastAPI + Jinja2 + htmx, WebSocket for live state.

No JS build chain, ever (architecture §9): htmx is a vendored single file,
templates are plain HTML. The WebSocket forwards bus events; the page reacts
by re-fetching htmx partials.
"""

from __future__ import annotations

import asyncio
import logging
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

_AUDIO_TYPES = {
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".webm": "audio/webm",
}


def create_app(bus: EventBus, db: Database, player: PlayerService, router) -> FastAPI:
    app = FastAPI(title="MeshRadio")
    templates = Jinja2Templates(directory=_HERE / "templates")
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")

    # -- pages -------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(request, "index.html", {"state": player.state()})

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
            request, "partials/now_playing.html", {"state": player.state()}
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

    @app.post("/api/queue/{track_id}")
    async def api_enqueue(track_id: int):
        await player.enqueue_track_id(track_id)
        return JSONResponse({"ok": True})

    @app.post("/api/play-day/{date}")
    async def api_play_day(date: str):
        await player.play_day(date)
        return RedirectResponse("/", status_code=303)

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

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        sub = bus.subscribe(PLAYER_STATE, OUTPUT_CHANGED, POWER_STATE)
        try:
            await websocket.send_json({"topic": PLAYER_STATE, "data": player.state()})
            async for topic, payload in sub:
                await websocket.send_json({"topic": topic, "data": payload})
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception:
            log.debug("websocket closed", exc_info=True)
        finally:
            sub.close()

    return app
