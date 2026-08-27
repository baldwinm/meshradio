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
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..bus import EventBus, INGEST_STATUS
from ..db import Database
from ..media.player import PlayerService
from ..runtime import supervise
from . import routes_api, routes_ingest, routes_pages, ws
from .context import WebContext, absolute_url
from .sessions import SESSION_COOKIE, SessionManager, SpeakerRegistry, valid_sid

log = logging.getLogger(__name__)

_HERE = Path(__file__).parent

# Selectable UI skins (see static/style.css). The chosen one rides in a cookie
# and is rendered onto <html data-skin> server-side, so there's no flash of the
# default skin on load. The alt theme is allowed here so its cookie survives a
# reload once a client opts into it.
ALLOWED_SKINS = {"winamp", "itunes", "wmp", "aurora"}
DEFAULT_SKIN = "winamp"


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


class VersionedStatic(StaticFiles):
    """Static files that tell the browser how long to keep them.

    Templates request assets as ``/static/x.js?v=<asset_v>`` (see
    ``_asset_version``), so a URL carrying ``v`` names one immutable build and
    can be cached forever — the next deploy changes the query string. Without
    the versioned URL we can't make that promise (someone linked the bare
    path), so those get a short window and keep revalidating.

    This is what stops every page navigation from re-checking eleven assets:
    on the hosted embed that's eleven round trips a visitor doesn't need."""

    IMMUTABLE = "public, max-age=31536000, immutable"
    SHORT = "public, max-age=300"

    # *args/**kwargs so a future Starlette can add parameters without breaking
    # the override; the three we name are the long-standing ones.
    def file_response(self, full_path, stat_result, scope, *args, **kwargs):
        response = super().file_response(full_path, stat_result, scope, *args, **kwargs)
        versioned = b"v=" in scope.get("query_string", b"")
        response.headers["cache-control"] = self.IMMUTABLE if versioned else self.SHORT
        return response


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
    # via the lifespan watcher below, by any successful analyzer poll —
    # primary or backup feed, so a primary outage the backup covers doesn't
    # read as "every ingest source stopped".
    health: dict = {"last_ingest": None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def watch_ingest():
            sub = bus.subscribe(INGEST_STATUS)
            try:
                async for _topic, payload in sub:
                    # "ok" is the pollers' success marker; mesh reports link
                    # state ("connected"/"disconnected"), which isn't ingest.
                    if "ok" in payload.values():
                        health["last_ingest"] = time.time()
            finally:
                sub.close()

        task = supervise("ingest-health-watch", watch_ingest)
        yield
        task.cancel()

    app = FastAPI(title="MeshRadio", lifespan=lifespan)
    # HTML, CSS and JS are mostly repeated markup — the archive pages compress
    # better than 10:1. Audio is already compressed, and gzipping it would just
    # burn CPU on the Pi, but it's over the 500-byte floor either way, so the
    # /audio route sets its own no-op encoding (see routes_ingest).
    app.add_middleware(GZipMiddleware, minimum_size=500)
    templates = Jinja2Templates(directory=_HERE / "templates")
    templates.env.filters["mmss"] = _mmss
    # base.html builds every page's link-preview tags from the live request.
    templates.env.globals["absolute_url"] = absolute_url
    templates.env.globals["asset_v"] = _asset_version()
    # Public embed hosting only: the Buy-Me-a-Coffee button pulls an external
    # CDN script, so keep it off the offline LAN/appliance skin. player_factory
    # is set exactly when we're in embed mode (see app.py).
    templates.env.globals["embed_mode"] = player_factory is not None
    app.mount("/static", VersionedStatic(directory=_HERE / "static"), name="static")

    @app.middleware("http")
    async def skin_from_cookie(request: Request, call_next):
        skin = request.cookies.get("skin", DEFAULT_SKIN)
        request.state.skin = skin if skin in ALLOWED_SKINS else DEFAULT_SKIN
        return await call_next(request)

    # Per-visitor sessions (public embed hosting) vs one communal player
    # (the appliance). A cookie names the session; each browser gets its own.
    sessions = SessionManager(player_factory, db, bus, player.tz) if player_factory else None

    if sessions is not None:
        @app.middleware("http")
        async def ensure_session_cookie(request: Request, call_next):
            sid = request.cookies.get(SESSION_COOKIE)
            # A forged/garbage sid never becomes a session key — reissue.
            fresh = not valid_sid(sid)
            if fresh:
                sid = secrets.token_hex(16)
            request.state.sid = sid
            response = await call_next(request)
            if fresh:
                # Secure when the visitor reached us over HTTPS (hosted embed
                # deployments sit behind a TLS-terminating proxy); plain-HTTP
                # LAN/appliance use keeps working without it.
                https = (
                    request.url.scheme == "https"
                    or request.headers.get("x-forwarded-proto", "")
                    .split(",")[0].strip() == "https"
                )
                response.set_cookie(
                    SESSION_COOKIE, sid,
                    max_age=365 * 24 * 3600, httponly=True, samesite="lax",
                    secure=https,
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

    @app.exception_handler(404)
    async def not_found(request: Request, exc):
        """A browser gets the site's own 404 page; anything else keeps the JSON.

        The archive's day URLs are the one place a typo (or a crawler) lands on
        a path built from free text, and the old behaviour was a 200 titled
        with whatever was typed."""
        wants_html = "text/html" in request.headers.get("accept", "")
        detail = getattr(exc, "detail", None)
        if not wants_html:
            return JSONResponse({"detail": detail or "Not Found"}, status_code=404)
        return templates.TemplateResponse(
            request,
            "404.html",
            # Not a document: no indexing, and no canonical/og:url built from
            # the path that missed — that path is whatever a stranger typed.
            {"detail": detail, "meta_url": absolute_url(request, "/"), "meta_noindex": True},
            status_code=404,
        )

    app.include_router(routes_pages.router)
    app.include_router(routes_api.router)
    app.include_router(routes_ingest.router)
    app.include_router(ws.router)
    return app
