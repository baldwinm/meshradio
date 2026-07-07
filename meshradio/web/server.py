"""Web app assembly: FastAPI + Jinja2 + htmx, WebSocket for live state.

No JS build chain, ever (architecture §9): htmx is a vendored single file,
templates are plain HTML. The WebSocket forwards bus events; the page reacts
by re-fetching htmx partials.

Routes live in routes_pages / routes_api / routes_ingest / ws; shared state
rides on app.state.ctx (see context.WebContext); per-visitor sessions (embed
hosting) in sessions.SessionManager.
"""

from __future__ import annotations

import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..bus import EventBus, INGEST_STATUS
from ..db import Database
from ..media.player import PlayerService
from ..runtime import supervise
from . import routes_api, routes_ingest, routes_pages, ws
from .context import WebContext
from .sessions import SESSION_COOKIE, SessionManager, SpeakerRegistry

log = logging.getLogger(__name__)

_HERE = Path(__file__).parent


def _mmss(value) -> str:
    """Seconds → 'm:ss' (or 'h:mm:ss'); empty string for unknown durations."""
    if value is None:
        return ""
    value = int(value)
    if value >= 3600:
        return f"{value // 3600}:{value % 3600 // 60:02d}:{value % 60:02d}"
    return f"{value // 60}:{value % 60:02d}"


def _asset_version() -> int:
    """Newest mtime under static/ — cache-busts CSS/JS across app updates."""
    static = _HERE / "static"
    return int(max(f.stat().st_mtime for f in static.rglob("*") if f.is_file()))


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
    templates.env.globals["asset_v"] = _asset_version()
    # Public embed hosting only: the Buy-Me-a-Coffee button pulls an external
    # CDN script, so keep it off the offline LAN/appliance skin. player_factory
    # is set exactly when we're in embed mode (see app.py).
    templates.env.globals["embed_mode"] = player_factory is not None
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")

    # Per-visitor sessions (public embed hosting) vs one communal player
    # (the appliance). A cookie names the session; each browser gets its own.
    sessions = SessionManager(player_factory, db) if player_factory else None

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

    ctx = WebContext(
        bus=bus,
        db=db,
        player=player,
        audio_router=router,
        ingest=ingest,
        ingest_token=ingest_token,
        sessions=sessions,
        templates=templates,
        speakers=SpeakerRegistry(),
        health=health,
    )
    app.state.ctx = ctx
    app.state.sessions = sessions
    app.state.speakers = ctx.speakers

    app.include_router(routes_pages.router)
    app.include_router(routes_api.router)
    app.include_router(routes_ingest.router)
    app.include_router(ws.router)
    return app
