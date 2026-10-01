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
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import Headers
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import PlainTextResponse

from ..bus import (
    EventBus,
    INGEST_STATUS,
    THEME_CREATED,
    TRACK_DISCOVERED,
    TRACK_FAILED,
    TRACK_READY,
)
from ..db import Database
from ..media.player import PlayerService
from ..runtime import supervise
from . import routes_api, routes_ingest, routes_pages, ws
from .context import WebContext, absolute_url
from .sessions import (
    MAX_SOCKETS_COMMUNAL,
    SESSION_COOKIE,
    SessionManager,
    SpeakerRegistry,
    issue_cookie,
    verify_cookie,
)

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
# documents, but documents nobody presses anything from.
_NO_SESSION_PREFIXES = ("/static/", "/audio/")
_NO_SESSION_PATHS = frozenset(
    {"/healthz", "/robots.txt", "/sitemap.xml", "/feed.xml", "/api/ingest"}
)


def _sessionless(path: str) -> bool:
    return path in _NO_SESSION_PATHS or path.startswith(_NO_SESSION_PREFIXES)


def _mmss(value) -> str:
    """Seconds → 'm:ss' (or 'h:mm:ss'); empty string for unknown durations.

    Unknown includes anything that isn't a finite non-negative number: the
    archive refuses those now, but a filter that can raise mid-render turns
    one bad row into a 500 on every page that shows it."""
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        return ""
    if value < 0:
        return ""
    if value >= 3600:
        return f"{value // 3600}:{value % 3600 // 60:02d}:{value % 60:02d}"
    return f"{value // 60}:{value % 60:02d}"


def _asset_version() -> int:
    """Newest mtime under static/ — cache-busts CSS/JS across app updates."""
    static = _HERE / "static"
    return int(max(f.stat().st_mtime for f in static.rglob("*") if f.is_file()))


def _authority(netloc: str) -> str:
    """``host[:port]`` normalised for comparison: lower-cased, default ports
    dropped (a browser's Origin omits ``:443``; a Host header may carry it)."""
    netloc = netloc.strip().lower()
    for default in (":80", ":443"):
        if netloc.endswith(default):
            return netloc[: -len(default)]
    return netloc


def same_site(origin: str | None, host: str, fetch_site: str | None = None) -> bool:
    """Did this request come from a page we served?

    Browsers attach ``Origin`` to every POST and WebSocket handshake, so a
    mismatch with ``Host`` is another site driving the radio. No ``Origin`` at
    all means no browser was involved (curl, the relay pusher, tests) and is
    allowed. ``Sec-Fetch-Site`` is the belt to that brace where present."""
    if fetch_site is not None and fetch_site.strip().lower() == "cross-site":
        return False
    if origin is None:
        return True
    if origin.strip().lower() == "null":   # sandboxed frame, file://, redirects
        return False
    return _authority(urlsplit(origin).netloc) == _authority(host)


class OriginGuard:
    """Refuse cross-site state changes and WebSocket hijacks.

    The appliance is a communal player with no login: any page a LAN user has
    open could otherwise POST ``/api/skip`` or ``/api/output/bluetooth`` at
    it, or open ``/ws`` to read state and claim the speaker role (browsers
    don't enforce same-origin on WebSockets). Reads stay open — a cross-site
    page can't see their bodies without CORS headers, and a link into the
    archive from a chat is a cross-site GET that must keep working."""

    SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        kind = scope["type"]
        if kind == "websocket" or (kind == "http" and scope["method"] not in self.SAFE_METHODS):
            headers = Headers(scope=scope)
            if not same_site(headers.get("origin"), headers.get("host", ""),
                             headers.get("sec-fetch-site")):
                if kind == "websocket":
                    await self._deny_websocket(scope, send)
                else:
                    response = PlainTextResponse("cross-site request refused", status_code=403)
                    await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

    @staticmethod
    async def _deny_websocket(scope, send) -> None:
        """Close before accepting: uvicorn answers that handshake with a
        plain 403. (Its ASGI denial-response extension would let us write
        the 403 ourselves, but the websockets implementation it ships then
        logs "returned without completing handshake" and tries a 500 on top;
        the close is the path it handles cleanly.)"""
        await send({"type": "websocket.close", "code": 1008})


def content_security_policy(embed_mode: bool) -> str:
    """The sources our pages actually use, and nothing else.

    Scripts and styles are our own files (the templates carry no inline
    handlers — see eq.js/playbar.js for the delegated listeners that replaced
    them); images are ours plus YouTube stills; audio streams from ``/audio``;
    fetch and the WebSocket stay on this origin; the one frame is YouTube's
    player. Embed hosting adds the YouTube IFrame API and the coffee button,
    which styles itself inline and pulls its own font."""
    script = ["'self'"]
    style = ["'self'"]
    img = ["'self'", "https://i.ytimg.com"]
    font = ["'self'"]
    if embed_mode:
        script += ["https://www.youtube.com", "https://cdnjs.buymeacoffee.com"]
        style += ["'unsafe-inline'", "https://cdnjs.buymeacoffee.com", "https://fonts.googleapis.com"]
        img += ["https://cdn.buymeacoffee.com", "https://cdnjs.buymeacoffee.com",
                "https://www.buymeacoffee.com"]
        font += ["https://fonts.gstatic.com"]
    return "; ".join([
        "default-src 'self'",
        f"script-src {' '.join(script)}",
        f"style-src {' '.join(style)}",
        f"img-src {' '.join(img)}",
        "media-src 'self'",
        # ws:/wss: spelled out: older browsers don't count a same-origin
        # WebSocket as 'self'.
        "connect-src 'self' ws: wss:",
        "frame-src https://www.youtube.com",
        f"font-src {' '.join(font)}",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'self'",
    ])


class SecurityHeaders:
    """Browser hardening headers on every HTTP response.

    The policy is the one thing here with teeth: with no inline script
    allowed, markup that reaches a page through a mesh name or a title can't
    run anything even if escaping ever slipped. The rest closes the usual
    small doors — content sniffing, referrer leakage, framing by another
    site. A handler that already set one of these keeps its own value."""

    def __init__(self, app, csp: str, report_only: bool = False) -> None:
        self.app = app
        csp_header = "content-security-policy-report-only" if report_only else "content-security-policy"
        self.headers = [
            (csp_header.encode(), csp.encode()),
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"strict-origin-when-cross-origin"),
            (b"x-frame-options", b"SAMEORIGIN"),
        ]

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                raw = list(message.get("headers", []))
                present = {name.lower() for name, _ in raw}
                raw += [(name, value) for name, value in self.headers if name not in present]
                message["headers"] = raw
            await send(message)

        await self.app(scope, receive, send_with_headers)


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
    allowed_hosts: Sequence[str] = (),
    public_url: str = "",
    security_headers: bool = True,
    csp_report_only: bool = False,
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
                    request.url.scheme == "https"
                    or request.headers.get("x-forwarded-proto", "")
                    .split(",")[0].strip() == "https"
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

    # Outermost, so a refused request never reaches the session or skin
    # middleware either (each add_middleware wraps everything before it).
    app.add_middleware(OriginGuard)
    if allowed_hosts:
        app.add_middleware(
            TrustedHostMiddleware, allowed_hosts=list(allowed_hosts), www_redirect=False
        )
    if security_headers:
        # Outermost of all: the guards' own 403/400 answers get them too.
        app.add_middleware(
            SecurityHeaders,
            csp=content_security_policy(embed_mode=player_factory is not None),
            report_only=csp_report_only,
        )
    return app
