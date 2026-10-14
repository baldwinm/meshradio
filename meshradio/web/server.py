"""Web app assembly: FastAPI + Jinja2 + htmx, WebSocket for live state.

No JS build chain, ever (architecture §9): htmx is a vendored single file,
templates are plain HTML. The WebSocket forwards bus events; the page reacts
by re-fetching htmx partials.

Routes live in routes_pages / routes_api / routes_ingest / ws / admin; shared
state rides on app.state.ctx (see context.WebContext); per-visitor sessions
(embed hosting) in sessions.SessionManager. The middleware is in middleware,
the templates' filters and globals in templating.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..bus import (
    INGEST_STATUS,
    THEME_CREATED,
    TRACK_DISCOVERED,
    TRACK_FAILED,
    TRACK_READY,
    EventBus,
)
from ..concerts import Concerts
from ..db import Database
from ..media.player import PlayerService
from ..runtime import supervise
from . import admin as admin_pages
from . import routes_api, routes_ingest, routes_pages, ws
from .admin_auth import ADMIN_PATH, AdminSettings
from .context import WebContext, absolute_url, forwarded_scheme
from .middleware import CountErrors, OriginGuard, SecurityHeaders
from .proxy import ForwardedClient
from .ratelimit import RateLimiter
from .sessions import (
    MAX_SOCKETS_COMMUNAL,
    SESSION_COOKIE,
    SessionManager,
    SpeakerRegistry,
    issue_cookie,
    verify_cookie,
)
from .templating import VersionedStatic, make_templates

log = logging.getLogger(__name__)

_HERE = Path(__file__).parent

# Selectable UI skins (see static/style.css). The chosen one rides in a cookie
# and is rendered onto <html data-skin> server-side, so there's no flash of the
# default skin on load. The alt theme is allowed here so its cookie survives a
# reload once a client opts into it.
ALLOWED_SKINS = {"winamp", "itunes", "wmp", "aurora"}
DEFAULT_SKIN = "winamp"

# Responses that carry no page get no session cookie. Minting one is a token
# and a header per request for nothing, and it hands a crawler fetching the
# stylesheet, the relay pushing to /api/ingest and the host's health checker
# a cookie they will present straight back. The sitemap and the feed are
# documents, but documents nobody presses anything from. The admin page has
# its own sign-in cookie and no player.
_NO_SESSION_PREFIXES = ("/static/", "/audio/", f"{ADMIN_PATH}/")
_NO_SESSION_PATHS = frozenset(
    {"/healthz", "/robots.txt", "/sitemap.xml", "/feed.xml", "/api/ingest"}
)


# Always allowed when [web] allowed_hosts is pinned (see create_app).
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost")


def _sessionless(path: str) -> bool:
    return (
        path in _NO_SESSION_PATHS or path == ADMIN_PATH or path.startswith(_NO_SESSION_PREFIXES)
    )


def create_app(
    bus: EventBus,
    db: Database,
    player: PlayerService,
    router,
    ingest=None,
    ingest_token: str = "",
    player_factory: Callable[[EventBus], PlayerService] | None = None,
    allowed_hosts: Sequence[str] = (),
    public_url: str = "",
    security_headers: bool = True,
    csp_report_only: bool = False,
    trusted_proxies: Sequence[str] = ("127.0.0.1",),
    proxy_hops: int = 0,
    rate_limit: bool = True,
    admin: AdminSettings | None = None,
    concerts: Concerts | None = None,
) -> FastAPI:
    # Ingest freshness for /healthz: updated by successful relay pushes and,
    # via the lifespan watcher below, by any successful analyzer poll —
    # primary or backup feed, so a primary outage the backup covers doesn't
    # read as "every ingest source stopped". ``feeds`` keeps each source's
    # last report for the admin page's Feeds screen.
    health: dict = {"last_ingest": None, "feeds": {}}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def watch_ingest():
            sub = bus.subscribe(INGEST_STATUS)
            try:
                async for _topic, payload in sub:
                    now = time.time()
                    for source, status in payload.items():
                        seen = health["feeds"].setdefault(source, {})
                        seen.update(status=status, at=now)
                        if status == "ok":
                            seen["last_ok"] = now
                    # "ok" is the pollers' success marker; mesh reports link
                    # state ("connected"/"disconnected"), which isn't ingest.
                    if "ok" in payload.values():
                        health["last_ingest"] = now
            finally:
                sub.close()

        task = supervise("ingest-health-watch", watch_ingest)
        yield
        task.cancel()
        # Visitors' snapshots flush every few seconds; on shutdown, write
        # what is pending now rather than lose it with the process.
        if sessions is not None:
            await sessions.stop()

    # No /docs, /redoc or /openapi.json: nothing outside this app calls its
    # API except the relay, and FastAPI's defaults would hand every visitor
    # a map of the routes.
    app = FastAPI(
        title="MeshRadio", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    # HTML, CSS and JS are mostly repeated markup — the archive pages compress
    # better than 10:1. Audio is already compressed, and gzipping it would just
    # burn CPU on the Pi, but it's over the 500-byte floor either way, so the
    # /audio route sets its own no-op encoding (see routes_ingest). Level 6
    # rather than Starlette's default 9: the same size within a percent at
    # about half the CPU, and every partial re-render goes through here.
    app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)
    # player_factory is set exactly when we're in embed mode (see app.py).
    templates = make_templates(
        embed_mode=player_factory is not None, concerts_on=concerts is not None
    )
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
            if _sessionless(request.url.path):
                return await call_next(request)
            # Only a cookie this server signed names a session; anything else
            # (none, garbage, forged, an earlier key's) is reissued, and the
            # request is marked as having presented nothing — a session may
            # not be opened on it (see context.get_player).
            secret = await sessions.secret()
            sid = verify_cookie(request.cookies.get(SESSION_COOKIE), secret)
            fresh = sid is None
            if fresh:
                sid, cookie = issue_cookie(secret)
            request.state.sid = sid
            request.state.fresh_sid = fresh
            response = await call_next(request)
            if fresh:
                # Secure when the visitor reached us over HTTPS (hosted embed
                # deployments sit behind a TLS-terminating proxy); plain-HTTP
                # LAN/appliance use keeps working without it.
                https = (
                    request.url.scheme == "https" or forwarded_scheme(request) == "https"
                )
                response.set_cookie(
                    SESSION_COOKIE, cookie,
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
        speakers=SpeakerRegistry(MAX_SOCKETS_COMMUNAL),
        health=health,
        concerts=concerts,
    )
    app.state.ctx = ctx
    app.state.sessions = sessions
    app.state.speakers = ctx.speakers
    # The whole-archive aggregates change exactly when a song or a theme
    # lands; drop them then, on the publisher's own stack, rather than on a
    # clock (see WebContext.CACHE_TTL_S).
    bus.listen(ctx.invalidate, TRACK_DISCOVERED, TRACK_READY, TRACK_FAILED, THEME_CREATED)
    # See context.absolute_url: the one place the site names itself.
    app.state.public_url = public_url.rstrip("/")
    # See context.forwarded_scheme: whose X-Forwarded-* headers to believe.
    app.state.trusted_proxies = frozenset(trusted_proxies)

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
    if player_factory is None:
        app.include_router(routes_api.output_router)   # the appliance's outputs
    app.include_router(routes_ingest.router)
    app.include_router(ws.router)

    # The admin page exists only with a password hash configured; without one
    # every /admin URL is the ordinary 404.
    app.state.admin = admin
    if admin is not None:
        app.include_router(admin_pages.router)
        templates.env.filters.update(admin_pages.FILTERS)

        @app.middleware("http")
        async def admin_headers(request: Request, call_next):
            response = await call_next(request)
            path = request.url.path
            if path == ADMIN_PATH or path.startswith(f"{ADMIN_PATH}/"):
                admin_pages.admin_headers(response)
            return response

    # Inside the origin guard, so a refused cross-site request never costs a
    # client its budget; outside the session middleware, so a refused press
    # mints no cookie and opens nothing.
    if rate_limit:
        app.add_middleware(RateLimiter)
    # Outermost, so a refused request never reaches the session or skin
    # middleware either (each add_middleware wraps everything before it).
    app.add_middleware(OriginGuard)
    if allowed_hosts:
        # Plus the machine's own names: the Pi's auto-updater checks
        # http://127.0.0.1:8080/healthz after each update, and a pinned list
        # without them answered it 400 and rolled every update back. A
        # DNS-rebinding page arrives under its own host name, never these.
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=[*allowed_hosts, *_LOOPBACK_HOSTS],
            www_redirect=False,
        )
    if security_headers:
        # Outermost of all: the guards' own 403/400 answers get them too.
        app.add_middleware(
            SecurityHeaders,
            embed_mode=player_factory is not None,
            report_only=csp_report_only,
            hsts=public_url.startswith("https://"),
        )
    if proxy_hops:
        # Before anything reads the client's address or scheme: the rate
        # limits, the admin sign-in throttle, the Secure cookie flag.
        app.add_middleware(ForwardedClient, hops=proxy_hops, trusted=list(trusted_proxies))
    # Outermost, so a failure anywhere inside — a route or a middleware — counts.
    app.add_middleware(CountErrors)
    return app
