"""The ASGI middleware ``create_app`` stacks around the routes: the
cross-site guard, the browser hardening headers and the error counter
/healthz reports."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse

from ..runtime import record_error


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


# A Host header worth naming in the policy: a host name or address, with an
# optional port. Anything else gets no WebSocket entry rather than a header
# built from whatever a client sent.
_HOST_RE = re.compile(r"\A(?:[A-Za-z0-9.\-]+|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?\Z")


def content_security_policy(embed_mode: bool, host: str = "") -> str:
    """The sources our pages actually use, and nothing else.

    Scripts and styles are our own files (the templates carry no inline
    handlers — see eq.js/playbar.js for the delegated listeners that replaced
    them); images are ours plus YouTube stills; audio streams from ``/audio``;
    fetch and the WebSocket stay on this origin; the one frame is YouTube's
    player. Embed hosting adds the YouTube IFrame API, and nothing else: the
    donation button is a link of ours, not the donation site's script.

    ``host`` is the request's own, so the WebSocket can be spelled out as
    ``ws://host wss://host`` for the browsers that don't count a same-origin
    socket as ``'self'`` — this origin only, where it used to be any."""
    script = ["'self'"]
    if embed_mode:
        script.append("https://www.youtube.com")
    connect = ["'self'"]
    if host and _HOST_RE.match(host):
        connect += [f"ws://{host}", f"wss://{host}"]
    return "; ".join([
        "default-src 'self'",
        f"script-src {' '.join(script)}",
        "style-src 'self'",
        "img-src 'self' https://i.ytimg.com",
        "media-src 'self'",
        f"connect-src {' '.join(connect)}",
        "frame-src https://www.youtube.com",
        "font-src 'self'",
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
    site, the page's window being reachable from another origin's popup,
    and device permissions no page here asks for. ``hsts`` is for a site
    that is only ever https (``[web] public_url`` says so): a year of
    "never try http", which a browser applies only when it heard it over
    https anyway. A handler that already set one of these keeps its own
    value."""

    def __init__(
        self, app, embed_mode: bool, report_only: bool = False, hsts: bool = False
    ) -> None:
        self.app = app
        self.embed_mode = embed_mode
        self.csp_header = (
            b"content-security-policy-report-only" if report_only else b"content-security-policy"
        )
        self.headers = [
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"strict-origin-when-cross-origin"),
            (b"x-frame-options", b"SAMEORIGIN"),
            (b"cross-origin-opener-policy", b"same-origin"),
            (b"permissions-policy",
             b"camera=(), microphone=(), geolocation=(), payment=(), usb=(), midi=()"),
        ]
        if hsts:
            self.headers.append((b"strict-transport-security", b"max-age=31536000"))

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # The policy names this request's own host for its WebSocket.
        csp = content_security_policy(self.embed_mode, Headers(scope=scope).get("host", ""))
        headers = [(self.csp_header, csp.encode()), *self.headers]

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                raw = list(message.get("headers", []))
                present = {name.lower() for name, _ in raw}
                raw += [(name, value) for name, value in headers if name not in present]
                message["headers"] = raw
            await send(message)

        await self.app(scope, receive, send_with_headers)


class CountErrors:
    """Counts a request or socket that raised — a 500 to the visitor — for
    /healthz (see ``runtime.record_error``). The exception carries on to
    Starlette's own handler, which logs it and answers."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        try:
            await self.app(scope, receive, send)
        except Exception:
            if scope["type"] in ("http", "websocket"):
                record_error("request" if scope["type"] == "http" else "websocket")
            raise
