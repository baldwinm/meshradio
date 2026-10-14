"""What every admin screen shares: who is signed in, reading a form, the
redirect after a POST, and rendering a page under ``admin/``.

Forms post plain ``application/x-www-form-urlencoded`` bodies, parsed here
with the standard library rather than adding python-multipart for them.
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import parse_qs, urlencode

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..admin_auth import (
    ADMIN_COOKIE,
    ADMIN_PATH,
    SESSION_IDLE_S,
    SESSION_MAX_S,
    AdminSettings,
    csrf_token,
    token_hash,
)
from ..context import ctx_of, is_https

ISO_DATE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
ISO_MONTH = re.compile(r"\A\d{4}-\d{2}\Z")

# The biggest form body read. The largest real one is an artist merge.
MAX_FORM_BYTES = 64 * 1024


# The activity log's wording for each action.
ACTION_LABELS = {
    "sign_in": "Signed in",
    "sign_in_failed": "Failed sign-in",
    "sign_in_code_failed": "Right password, wrong code",
    "sign_out": "Signed out",
    "rename_theme": "Renamed theme",
    "create_theme": "Named a day",
    "remove_track": "Removed song",
    "restore_track": "Put a song back",
    "edit_track": "Edited song",
    "merge_artists": "Merged artist names",
    "ignore_artists": "Kept artist names apart",
    "fix_artists": "Set song artists",
    "backup": "Took a backup",
    "download_backup": "Downloaded a backup",
    "probe_feed": "Probed a feed",
    "relay_resend": "Re-sent everything to the public site",
    "retry_track": "Retried a song",
    "change_setting": "Changed a setting",
    "undo": "Undid",
}


def settings_of(request: Request) -> AdminSettings:
    return request.app.state.admin


def is_embed(request: Request) -> bool:
    """The public site: per-visitor players, no hardware."""
    return ctx_of(request).sessions is not None


def go(path: str, **params: Any) -> RedirectResponse:
    """A redirect after a POST (303, so the browser follows with a GET)."""
    query = {k: v for k, v in params.items() if v not in (None, "")}
    url = path + ("?" + urlencode(query) if query else "")
    return RedirectResponse(url, status_code=303)


async def read_form(request: Request) -> dict[str, list[str]]:
    """A urlencoded body as ``{name: [values]}``, refused past the cap."""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            raise HTTPException(413, "form too large")
    try:
        return parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=500)
    except (UnicodeDecodeError, ValueError):
        raise HTTPException(400, "unreadable form") from None


def field(form: dict[str, list[str]], name: str) -> str:
    values = form.get(name)
    return values[0].strip() if values else ""


async def current_admin(request: Request) -> str | None:
    """The session token of a signed-in admin, or None. Expired sign-ins and
    ones from before a password change are ended on sight."""
    token = request.cookies.get(ADMIN_COOKIE)
    if not token:
        return None
    db = ctx_of(request).db
    hashed = token_hash(token)
    row = await db.admin_session(hashed)
    if row is None:
        return None
    now = time.time()
    if (
        row["key_fp"] != settings_of(request).fingerprint
        or now - row["created_at"] > SESSION_MAX_S
        or now - row["seen_at"] > SESSION_IDLE_S
    ):
        await db.end_admin_session(hashed)
        return None
    await db.touch_admin_session(hashed)
    return token


async def require_admin(request: Request) -> str:
    token = await current_admin(request)
    if token is None:
        target = request.url.path
        if request.url.query:
            target += "?" + request.url.query
        location = f"{ADMIN_PATH}/login"
        if request.method == "GET" and target not in (ADMIN_PATH, f"{ADMIN_PATH}/"):
            location += "?" + urlencode({"next": target})
        raise HTTPException(303, headers={"Location": location})
    return token


async def checked_form(request: Request) -> tuple[str, dict[str, list[str]]]:
    """A signed-in admin's form, with its CSRF token verified."""
    token = await require_admin(request)
    form = await read_form(request)
    if field(form, "csrf") != csrf_token(token):
        raise HTTPException(403, "this form is out of date; reload the page and try again")
    return token, form


def safe_next(target: str) -> str:
    """Where to land after signing in: only ever a page under /admin."""
    if target.startswith(f"{ADMIN_PATH}/") and not target.startswith("//") and "\\" not in target:
        return target
    return ADMIN_PATH


def render(request: Request, name: str, token: str | None, **context: Any) -> HTMLResponse:
    ctx = ctx_of(request)
    status_code = context.pop("status_code", 200)
    return ctx.templates.TemplateResponse(
        request,
        f"admin/{name}",
        {
            "csrf": csrf_token(token) if token else "",
            "embed": is_embed(request),
            "tz": ctx.player.tz,
            "section": "",
            "plain_http": not is_https(request),
            **context,
        },
        status_code=status_code,
    )


def admin_headers(response: Response) -> None:
    """Admin pages are nobody's cache entry and nobody's search result."""
    response.headers["cache-control"] = "no-store"
    response.headers["x-robots-tag"] = "noindex, nofollow"
