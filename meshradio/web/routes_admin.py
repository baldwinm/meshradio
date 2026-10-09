"""The admin page: ``/admin``.

What the operator used to do over SSH with command-line flags, from a
browser: fix a day's theme, take a song off a day, correct a song's details,
merge artist spellings, check the feeds, and take or download backups.

Only mounted when an admin password hash is configured (``create_app`` gets
``admin=AdminSettings(...)``); without one every ``/admin`` URL is a 404.
Signing in is ``admin_auth``'s job. Every change goes in ``admin_log`` with
its before and after, and the ones that can be reversed carry what reversing
them needs, so the activity log can offer Undo.

Forms post plain ``application/x-www-form-urlencoded`` bodies, parsed here
with the standard library rather than adding python-multipart for them.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import sqlite3
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from .. import backup as backup_mod
from .. import config_overrides as overrides_mod
from ..bus import TRACK_DISCOVERED
from ..config import ConfigError
from ..db.fields import VIDEO_ID_RE
from ..ingest.corescope import format_probe, probe
from ..ingest.parse import untitled_theme
from ..ingest.relay import CURSOR_KEY
from ..media.cacher import drop_cache_file
from ..net import http_client
from .admin_auth import (
    ADMIN_COOKIE,
    ADMIN_PATH,
    SESSION_IDLE_S,
    SESSION_MAX_S,
    STEP_COOKIE,
    STEP_MAX_TRIES,
    STEP_PATH,
    STEP_TTL_S,
    AdminSettings,
    check_totp,
    csrf_token,
    new_session_token,
    token_hash,
    verify_password,
)
from .context import ctx_of, forwarded_scheme
from .routes_api import unplayable_checks

log = logging.getLogger(__name__)

router = APIRouter(prefix=ADMIN_PATH)

ISO_DATE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
ISO_MONTH = re.compile(r"\A\d{4}-\d{2}\Z")

# The biggest form body read. The largest real one is an artist merge.
MAX_FORM_BYTES = 64 * 1024

# One "before removing a song" snapshot covers a burst of removals: a clean-up
# of ten songs must not rotate every scheduled snapshot out of the keep count.
PREREMOVE_SNAPSHOT_EVERY_S = 10 * 60

# A backup older than this many intervals reads as overdue on the overview.
BACKUP_OVERDUE_FACTOR = 1.25

# A feed whose last good answer is older than this many poll intervals is
# flagged even if its last attempt wasn't an outright error.
FEED_STALE_FACTOR = 3

LOG_PAGE = 100

# What each backup's label means, for the Backups screen.
BACKUP_REASONS = {
    "auto": "Scheduled",
    "premigrate": "Before an upgrade (on boot)",
    "prerestore": "Before a restore",
    "preremove": "Before removing a song",
    "manual": "Taken from this page",
}

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

# Secrets in the config view show only their last four characters.
SECRET_KEYS = {
    ("web", "ingest_token"), ("relay", "token"), ("mesh", "channel_key"),
    ("web", "admin_password_hash"), ("web", "admin_totp_secret"),
}
# Where each setting the environment can override comes from.
ENV_KEYS = {
    ("web", "ingest_token"): "MESHRADIO_INGEST_TOKEN",
    ("relay", "token"): "MESHRADIO_RELAY_TOKEN",
    ("web", "admin_password_hash"): "MESHRADIO_ADMIN_PASSWORD_HASH",
    ("web", "admin_totp_secret"): "MESHRADIO_ADMIN_TOTP_SECRET",
}
# Device-only settings, left off the public site's config view: the public
# instance has no hardware, so they say nothing true about it.
DEVICE_SECTIONS = {"mesh", "cache"}
DEVICE_KEYS = {("player", "quiet_hours"), ("player", "volume")}

IGNORED_ARTISTS_KEY = "admin.artists_ignored"
TOTP_STEP_KEY = "admin.totp_last_step"

_ARTIST_PUNCT = re.compile(r"[\W_]+")


# -- small helpers ------------------------------------------------------------


def settings_of(request: Request) -> AdminSettings:
    return request.app.state.admin


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


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


def artist_key(name: str) -> str:
    """What two spellings of one artist have in common: case, a leading
    "The", YouTube's " - Topic" channel suffix, "&" against "and", and
    punctuation and spacing all set aside."""
    key = name.strip().lower()
    if key.endswith(" - topic"):
        key = key[: -len(" - topic")]
    if key.startswith("the "):
        key = key[4:]
    key = key.replace("&", " and ")
    return _ARTIST_PUNCT.sub("", key)


def lookalike_groups(
    spellings: list[dict[str, Any]], ignored: set[str]
) -> list[dict[str, Any]]:
    """Spellings that reduce to the same ``artist_key``, biggest group first,
    each with a suggested name: the most-used spelling that isn't a Topic
    channel's."""
    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in spellings:
        key = artist_key(row["artist"])
        if key:
            by_key[key].append(row)
    groups = []
    for key, rows in by_key.items():
        if len(rows) < 2 or key in ignored:
            continue
        rows = sorted(rows, key=lambda r: (-r["songs"], r["artist"]))
        plain = [r for r in rows if not r["artist"].lower().endswith(" - topic")]
        groups.append({
            "key": key,
            "spellings": rows,
            "songs": sum(r["songs"] for r in rows),
            "suggested": (plain or rows)[0]["artist"],
        })
    return sorted(groups, key=lambda g: (-g["songs"], g["key"]))


def snapshot_rows(backup_dir: Path) -> list[dict[str, Any]]:
    """The backup directory's snapshots, newest first, with what took them."""
    rows = []
    for path in reversed(backup_mod.list_snapshots(backup_dir)):
        match = re.match(r"meshradio-(\d{8}T\d{6}Z)-(.+)\.db\Z", path.name)
        taken = None
        label = ""
        if match:
            taken = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            label = match.group(2)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        rows.append({
            "name": path.name,
            "taken": taken.timestamp() if taken else None,
            "reason": BACKUP_REASONS.get(label, label or "—"),
            "size": size,
        })
    return rows


def config_rows(
    config: Any, embed: bool, skip: frozenset[str] | set[str] = frozenset()
) -> list[dict[str, Any]]:
    """The running config, one row per setting, secrets masked. ``skip``
    leaves out keys shown elsewhere (the editable ones)."""
    rows = []
    defaults = type(config)()
    for section in dataclasses.fields(config):
        value = getattr(config, section.name)
        if dataclasses.is_dataclass(value):
            if embed and section.name in DEVICE_SECTIONS:
                continue
            default_section = getattr(defaults, section.name)
            for item in dataclasses.fields(value):
                if embed and (section.name, item.name) in DEVICE_KEYS:
                    continue
                if f"{section.name}.{item.name}" in skip:
                    continue
                rows.append(_config_row(
                    section.name, item.name, getattr(value, item.name),
                    getattr(default_section, item.name),
                ))
        else:
            rows.append(_config_row("", section.name, value, getattr(defaults, section.name)))
    return rows


def _config_row(section: str, name: str, value: Any, default: Any) -> dict[str, Any]:
    env = ENV_KEYS.get((section, name))
    if env and os.environ.get(env):
        source = "environment"
    elif value == default:
        source = "default"
    else:
        source = "config file"
    if (section, name) in SECRET_KEYS:
        shown = f"•••• {str(value)[-4:]}" if value else "not set"
    elif isinstance(value, list):
        shown = ", ".join(str(v) for v in value) or "(none)"
    elif isinstance(value, bool):
        shown = "true" if value else "false"
    else:
        shown = str(value) if value != "" else "(empty)"
    return {"key": f"{section}.{name}" if section else name, "value": shown, "source": source}


# -- signing in ---------------------------------------------------------------


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
            "plain_http": not _https(request),
            **context,
        },
        status_code=status_code,
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = ""):
    if await current_admin(request):
        return go(safe_next(next))
    # The same page whether or not a code is configured: nothing here tells
    # a visitor there's a second step.
    return render(request, "login.html", None, next=next)


def _paused(throttle_wait: float) -> str:
    minutes = max(1, round(throttle_wait / 60))
    return (f"Too many tries. Sign-in is paused for {minutes} more minute"
            f"{'' if minutes == 1 else 's'}.")


def _https(request: Request) -> bool:
    return request.url.scheme == "https" or forwarded_scheme(request) == "https"


@router.post("/login")
async def login(request: Request):
    settings = settings_of(request)
    db = ctx_of(request).db
    ip = client_ip(request)
    form = await read_form(request)
    nxt = field(form, "next")
    now = time.time()

    wait = settings.throttle.locked_for(ip, now)
    if wait:
        return render(request, "login.html", None, next=nxt, error=_paused(wait),
                      status_code=429)
    settings.throttle.begin(ip, now)

    password = form.get("password", [""])[0]
    # scrypt is ~50 ms of CPU: off the event loop, so a sign-in can't stall
    # every visitor's page while it runs.
    ok = bool(password) and await asyncio.to_thread(
        verify_password, password, settings.password_hash
    )
    if not ok:
        await db.log_admin("sign_in_failed", ip=ip)
        return render(request, "login.html", None, next=nxt,
                      error="That password didn't match.", status_code=401)

    if not settings.totp_secret:
        settings.throttle.succeed(ip, now)
        return await _signed_in(request, nxt)

    # Right password, code still to come. The attempt isn't a failure, but
    # the address's earlier ones stand until the code is right too.
    settings.throttle.passed(ip, now)
    step = settings.pending.open(ip, nxt, settings.fingerprint, now)
    response = go(f"{STEP_PATH}/code")
    response.set_cookie(
        STEP_COOKIE, step, max_age=STEP_TTL_S, path=STEP_PATH,
        httponly=True, samesite="strict", secure=_https(request),
    )
    return response


def _drop_step(response: Response) -> Response:
    response.delete_cookie(STEP_COOKIE, path=STEP_PATH)
    return response


@router.get("/login/code", response_class=HTMLResponse)
async def code_page(request: Request):
    settings = settings_of(request)
    step = request.cookies.get(STEP_COOKIE)
    if not settings.totp_secret or settings.pending.get(
        step, client_ip(request), settings.fingerprint, time.time()
    ) is None:
        return _drop_step(go(f"{ADMIN_PATH}/login"))
    return render(request, "login_code.html", step)


@router.post("/login/code")
async def code(request: Request):
    settings = settings_of(request)
    db = ctx_of(request).db
    ip = client_ip(request)
    now = time.time()
    step = request.cookies.get(STEP_COOKIE)
    form = await read_form(request)

    pending = None
    if settings.totp_secret:
        pending = settings.pending.get(step, ip, settings.fingerprint, now)
    if pending is None or step is None:
        return _drop_step(go(f"{ADMIN_PATH}/login"))
    if field(form, "csrf") != csrf_token(step):
        raise HTTPException(403, "this form is out of date; reload the page and try again")

    wait = settings.throttle.locked_for(ip, now)
    if wait:
        settings.pending.close(step)
        return _drop_step(render(request, "login.html", None, next=pending.landing,
                                 error=_paused(wait), status_code=429))
    settings.throttle.begin(ip, now)

    # The last step used survives a restart, so a code seen once can't be
    # replayed after one.
    stored = int(await db.get_setting(TOTP_STEP_KEY, "-1") or -1)
    last = max(settings.last_totp_counter, stored)
    counter = check_totp(settings.totp_secret, field(form, "code"), now, last)
    if counter is None:
        pending.tries += 1
        # Logged apart from a wrong password: it means the password is known.
        await db.log_admin("sign_in_code_failed", ip=ip)
        if pending.tries >= STEP_MAX_TRIES:
            settings.pending.close(step)
            return _drop_step(render(request, "login.html", None, next=pending.landing,
                                     error="That code didn't match. Start again.",
                                     status_code=401))
        return render(request, "login_code.html", step,
                      error="That code didn't match.", status_code=401)

    settings.throttle.succeed(ip, now)
    settings.last_totp_counter = counter
    await db.set_setting(TOTP_STEP_KEY, str(counter))
    settings.pending.close(step)
    return _drop_step(await _signed_in(request, pending.landing))


async def _signed_in(request: Request, landing: str) -> Response:
    """Start a session: a fresh token (never one the browser brought), its
    hash stored, the cookie scoped to /admin."""
    settings = settings_of(request)
    db = ctx_of(request).db
    ip = client_ip(request)
    now = time.time()
    token = new_session_token()
    await db.prune_admin_sessions(now - SESSION_MAX_S, now - SESSION_IDLE_S)
    await db.create_admin_session(token_hash(token), ip, settings.fingerprint)
    await db.log_admin("sign_in", ip=ip)
    response = go(safe_next(landing))
    response.set_cookie(
        ADMIN_COOKIE, token, max_age=SESSION_MAX_S, path=ADMIN_PATH,
        httponly=True, samesite="strict", secure=_https(request),
    )
    return response


@router.post("/logout")
async def logout(request: Request):
    token, _form = await checked_form(request)
    db = ctx_of(request).db
    await db.end_admin_session(token_hash(token))
    await db.log_admin("sign_out", ip=client_ip(request))
    response = go(f"{ADMIN_PATH}/login")
    response.delete_cookie(ADMIN_COOKIE, path=ADMIN_PATH)
    return response


# -- overview -----------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def overview(request: Request):
    token = await require_admin(request)
    ctx = ctx_of(request)
    settings = settings_of(request)
    config = settings.config
    now = time.time()

    last_ingest = ctx.health.get("last_ingest")
    newest = await ctx.db.newest_channel_tracks(5)
    days = await ctx.archive_days()

    backup = None
    if config is not None and config.backup.enabled:
        snaps = snapshot_rows(config.backup_dir)
        newest_snap = snaps[0]["taken"] if snaps and snaps[0]["taken"] else None
        age = now - newest_snap if newest_snap else None
        backup = {
            "age": age,
            "interval": config.backup.interval_s,
            "overdue": age is None or age > config.backup.interval_s * BACKUP_OVERDUE_FACTOR,
        }

    feeds = feed_rows(request, now)
    attention = []
    untitled = await ctx.db.untitled_days()
    if untitled:
        attention.append({
            "text": f"{len(untitled)} day{'' if len(untitled) == 1 else 's'} still "
                    f"untitled: " + ", ".join(d["date"] for d in untitled[:4])
                    + ("…" if len(untitled) > 4 else ""),
            "href": f"{ADMIN_PATH}/days?untitled=1",
            "action": "Name them",
        })
    for feed in feeds:
        if feed["state"] in ("bad", "warn"):
            attention.append({
                "text": f"{feed['label']}: {feed['status']}",
                "href": f"{ADMIN_PATH}/feeds",
                "action": "Open feeds",
            })
    if backup is not None and backup["overdue"]:
        attention.append({
            "text": "The last backup is older than its schedule",
            "href": f"{ADMIN_PATH}/backups",
            "action": "Open backups",
        })

    return render(
        request, "overview.html", token, section="overview",
        last_ingest_age=(now - last_ingest) if last_ingest else None,
        track_count=await ctx.db.channel_track_count(),
        day_count=len(days),
        visitors=ctx.sessions.count() if ctx.sessions is not None else None,
        backup=backup,
        attention=attention,
        newest=newest,
        changes=await ctx.db.admin_log_entries("changes", limit=5),
        labels=ACTION_LABELS,
        all_ok=not any(f["state"] == "bad" for f in feeds),
        connection=connection_info(request),
    )


# Headers a proxy may use to say who the visitor is, shown as they arrived
# so [web] proxy_hops can be checked against a real request.
_CLIENT_HEADERS = ("x-forwarded-for", "x-forwarded-proto", "true-client-ip",
                   "cf-connecting-ip", "x-real-ip")


def connection_info(request: Request) -> dict[str, Any]:
    """How this request reached the app: the address the rate limits and the
    sign-in throttle use, whether it came over https, and what the proxy
    said. Your own address is the check — it should be the one shown."""
    peer = getattr(request.state, "peer", None)
    return {
        "address": client_ip(request),
        "peer": peer if peer is not None else client_ip(request),
        "https": _https(request),
        "headers": [(name, request.headers[name]) for name in _CLIENT_HEADERS
                    if name in request.headers],
    }


# -- days ---------------------------------------------------------------------


@router.get("/days", response_class=HTMLResponse)
async def days_list(request: Request, month: str = "", untitled: str = ""):
    token = await require_admin(request)
    ctx = ctx_of(request)
    if untitled:
        rows = [
            {"date": d["date"], "titles": d["title"], "tracks": d["tracks"], "themes": 1}
            for d in await ctx.db.untitled_days()
        ]
        return render(request, "days.html", token, section="days", days=rows,
                      untitled=True, months=[], month="")
    all_days = await ctx.archive_days()
    months = sorted({d["date"][:7] for d in all_days}, reverse=True)
    if not ISO_MONTH.match(month) or month not in months:
        month = months[0] if months else ""
    rows = [d for d in all_days if d["date"].startswith(month)]
    return render(request, "days.html", token, section="days", days=rows,
                  untitled=False, months=months, month=month)


async def _day_or_404(request: Request, date: str) -> dict[str, Any]:
    if not ISO_DATE.match(date):
        raise HTTPException(404, "no such day")
    theme = await ctx_of(request).db.latest_theme_for_date(date)
    if theme is None:
        raise HTTPException(404, "no such day")
    return theme


@router.get("/days/{date}", response_class=HTMLResponse)
async def day_page(request: Request, date: str, done: int | None = None, error: str = ""):
    token = await require_admin(request)
    ctx = ctx_of(request)
    theme = await _day_or_404(request, date)
    known = [d["date"] for d in reversed(await ctx.archive_days())]
    index = known.index(date) if date in known else -1
    prev_day = known[index - 1] if index > 0 else None
    next_day = known[index + 1] if 0 <= index < len(known) - 1 else None
    banner = await ctx.db.admin_log_entry(done) if done else None
    return render(
        request, "day.html", token, section="days",
        date=date, theme=theme, tracks=await ctx.db.tracks_for_day(date),
        untitled=theme["title"] == untitled_theme(date),
        prev_day=prev_day, next_day=next_day,
        banner=banner, labels=ACTION_LABELS, error=error,
    )


@router.post("/days/{date}/theme")
async def rename_day(request: Request, date: str):
    _token, form = await checked_form(request)
    ctx = ctx_of(request)
    theme = await _day_or_404(request, date)
    title = field(form, "title")
    if not title:
        return go(f"{ADMIN_PATH}/days/{date}", error="A theme needs a title.")
    if title == theme["title"]:
        return go(f"{ADMIN_PATH}/days/{date}")
    try:
        renamed = await ctx.db.rename_theme(theme["id"], title)
    except sqlite3.IntegrityError:
        return go(f"{ADMIN_PATH}/days/{date}",
                  error="That day already has another theme with that title.")
    except ValueError:
        return go(f"{ADMIN_PATH}/days/{date}", error="A theme needs a title.")
    entry = await ctx.db.log_admin(
        "rename_theme", ip=client_ip(request), target=date,
        before=theme["title"], after=renamed["title"],
        undo={"theme_id": theme["id"], "title": theme["title"]},
    )
    ctx.invalidate()
    return go(f"{ADMIN_PATH}/days/{date}", done=entry)


# -- one song -----------------------------------------------------------------


async def _track_or_404(request: Request, track_id: int) -> tuple[dict[str, Any], str | None]:
    db = ctx_of(request).db
    track = await db.track_by_id(track_id)
    if track is None:
        raise HTTPException(404, "no such song")
    theme = await db.theme_by_id(track["theme_id"]) if track["theme_id"] else None
    return track, theme["date"] if theme else None


@router.get("/tracks/{track_id}/edit", response_class=HTMLResponse)
async def edit_track_page(request: Request, track_id: int):
    token = await require_admin(request)
    track, date = await _track_or_404(request, track_id)
    return render(request, "track_edit.html", token, section="days", track=track, date=date)


@router.post("/tracks/{track_id}/edit")
async def edit_track(request: Request, track_id: int):
    token, form = await checked_form(request)
    ctx = ctx_of(request)
    track, date = await _track_or_404(request, track_id)
    try:
        before, after = await ctx.db.edit_track(
            track_id, field(form, "title"), field(form, "artist") or None
        )
    except ValueError as exc:
        return render(request, "track_edit.html", token, section="days", track=track,
                      date=date, error=f"{str(exc).capitalize()}.", status_code=400)
    entry = await ctx.db.log_admin(
        "edit_track", ip=client_ip(request), target=date,
        before=_song_label(before), after=_song_label(after),
        undo={"track_id": track_id, "title": before["title"], "artist": before["artist"],
              "edited_at": before.get("meta_edited_at")},
    )
    ctx.invalidate()
    return go(f"{ADMIN_PATH}/days/{date}" if date else f"{ADMIN_PATH}/log", done=entry)


def _song_label(track: dict[str, Any]) -> str:
    title = track.get("title") or track.get("video_id") or "?"
    return f"{title} · {track['artist']}" if track.get("artist") else title


@router.get("/tracks/{track_id}/remove", response_class=HTMLResponse)
async def remove_track_page(request: Request, track_id: int):
    token = await require_admin(request)
    track, date = await _track_or_404(request, track_id)
    if date is None:
        raise HTTPException(404, "that song isn't on a day")
    return render(request, "track_remove.html", token, section="days", track=track,
                  date=date, plays=await ctx_of(request).db.play_count(track_id))


@router.post("/tracks/{track_id}/remove")
async def remove_track(request: Request, track_id: int):
    token, form = await checked_form(request)
    ctx = ctx_of(request)
    settings = settings_of(request)
    track, date = await _track_or_404(request, track_id)
    if date is None:
        raise HTTPException(404, "that song isn't on a day")
    if field(form, "confirm") != date:
        return render(request, "track_remove.html", token, section="days", track=track,
                      date=date, plays=await ctx.db.play_count(track_id), status_code=400,
                      error=f"Type {date} to confirm.")
    await _snapshot_before_removal(settings)
    deleted = await ctx.db.delete_track(track_id)
    if deleted is None:
        return go(f"{ADMIN_PATH}/days/{date}")
    config = settings.config
    if config is not None and not is_embed(request):
        await asyncio.to_thread(drop_cache_file, config.cache_dir, deleted)
    if deleted["theme_id"] is not None:
        await ctx.db.delete_empty_placeholder(deleted["theme_id"])
    entry = await ctx.db.log_admin(
        "remove_track", ip=client_ip(request), target=date,
        before=_song_label(deleted), after="removed",
        undo={"date": date, "video_id": deleted["video_id"]},
    )
    ctx.invalidate()
    if await ctx.db.latest_theme_for_date(date) is None:
        return go(f"{ADMIN_PATH}/removed", done=entry)
    return go(f"{ADMIN_PATH}/days/{date}", done=entry)


async def _snapshot_before_removal(settings: AdminSettings) -> None:
    config = settings.config
    if config is None or not config.backup.enabled:
        return
    snaps = snapshot_rows(config.backup_dir)
    if snaps and snaps[0]["taken"] and (
        time.time() - snaps[0]["taken"] < PREREMOVE_SNAPSHOT_EVERY_S
    ):
        return
    try:
        await asyncio.to_thread(backup_mod.snapshot, config.db_path, config.backup_dir,
                                "preremove")
        await asyncio.to_thread(backup_mod.rotate, config.backup_dir, config.backup.keep)
    except Exception:
        log.exception("backup before removal failed (removing anyway)")


@router.post("/tracks/{track_id}/retry")
async def retry_track(request: Request, track_id: int):
    """Send a failed song back through the cacher: a failed download on the
    device, or (public site) a video the player reported YouTube won't play,
    with every share of it, so the next failure is checked afresh."""
    _token, _form = await checked_form(request)
    ctx = ctx_of(request)
    track, date = await _track_or_404(request, track_id)
    if is_embed(request):
        rows = await ctx.db.retry_video(track["video_id"])
        unplayable_checks(request).pop(track["video_id"], None)
    else:
        await ctx.db.set_cache_status(track_id, "pending")
        rows = [{**track, "cache_status": "pending", "cache_path": None}]
    for row in rows:
        ctx.bus.publish(TRACK_DISCOVERED, {"track": row})
    await ctx.db.log_admin("retry_track", ip=client_ip(request), target=date,
                           before=_song_label(track))
    return go(f"{ADMIN_PATH}/removed" if is_embed(request) else f"{ADMIN_PATH}/device")


# -- removed songs ------------------------------------------------------------


@router.get("/removed", response_class=HTMLResponse)
async def removed_page(request: Request, done: int | None = None):
    token = await require_admin(request)
    ctx = ctx_of(request)
    banner = await ctx.db.admin_log_entry(done) if done else None
    # On the public site a "failed" song is one YouTube won't play (the
    # device lists its failed downloads on its own page instead).
    unplayable = await ctx.db.failed_tracks() if is_embed(request) else []
    return render(request, "removed.html", token, section="removed",
                  removed=await ctx.db.removed_tracks(), unplayable=unplayable,
                  banner=banner, labels=ACTION_LABELS)


async def _put_back(request: Request, date: str, video_id: str) -> tuple[str, str]:
    """Lift a removal; returns (what happened, the song's label)."""
    db = ctx_of(request).db
    tomb = await db.removed_track(date, video_id)
    if tomb is None:
        return "gone", video_id
    label = tomb["title"] or video_id
    restored = await db.restore_deleted_track(date, video_id)
    if restored is not None:
        ctx_of(request).bus.publish(TRACK_DISCOVERED, {"track": restored})
        ctx_of(request).invalidate()
        return "restored", label
    return "unblocked", label


@router.post("/removed/{date}/{video_id}/restore")
async def restore_removed(request: Request, date: str, video_id: str):
    _token, _form = await checked_form(request)
    if not ISO_DATE.match(date):
        raise HTTPException(404)
    outcome, label = await _put_back(request, date, video_id)
    if outcome == "gone":
        return go(f"{ADMIN_PATH}/removed")
    entry = await ctx_of(request).db.log_admin(
        "restore_track", ip=client_ip(request), target=date, before="removed",
        after=label if outcome == "restored" else f"{label} (allowed back)",
    )
    return go(f"{ADMIN_PATH}/removed", done=entry)


# -- artists ------------------------------------------------------------------


async def _ignored_artists(request: Request) -> set[str]:
    raw = await ctx_of(request).db.get_setting(IGNORED_ARTISTS_KEY, "[]")
    try:
        return set(json.loads(raw or "[]"))
    except ValueError:
        return set()


@router.get("/artists", response_class=HTMLResponse)
async def artists_page(request: Request, done: int | None = None, error: str = ""):
    token = await require_admin(request)
    db = ctx_of(request).db
    spellings = await db.artist_spellings()
    banner = await db.admin_log_entry(done) if done else None
    return render(
        request, "artists.html", token, section="artists",
        groups=lookalike_groups(spellings, await _ignored_artists(request)),
        spellings=spellings, aliases=await db.artist_aliases(),
        unfiled=len(await db.songs_to_fix()),
        banner=banner, labels=ACTION_LABELS, error=error,
    )


@router.post("/artists/merge")
async def merge_artists(request: Request):
    _token, form = await checked_form(request)
    ctx = ctx_of(request)
    known = {r["artist"] for r in await ctx.db.artist_spellings()}
    spellings = [s for s in form.get("spelling", []) if s in known]
    canonical = field(form, "canonical")
    if not canonical:
        return go(f"{ADMIN_PATH}/artists", error="Give the name to show.")
    folded = [s for s in spellings if s != canonical]
    if not folded:
        error = (
            "Pick at least one spelling that differs from the name to show."
            if spellings or not form.get("spelling")
            else "No song carries that name; pick one from the list."
        )
        return go(f"{ADMIN_PATH}/artists", error=error)
    try:
        changed = await ctx.db.merge_artists(folded, canonical)
    except ValueError:
        return go(f"{ADMIN_PATH}/artists", error="Give the name to show.")
    entry = await ctx.db.log_admin(
        "merge_artists", ip=client_ip(request), target=canonical,
        before=" · ".join(folded), after=f"{canonical} ({len(changed)} songs)",
        undo={"spellings": folded, "canonical": canonical, "changed": changed},
    )
    ctx.invalidate()
    return go(f"{ADMIN_PATH}/artists", done=entry)


@router.post("/artists/ignore")
async def ignore_artists(request: Request):
    _token, form = await checked_form(request)
    db = ctx_of(request).db
    key = field(form, "key")
    if key:
        ignored = await _ignored_artists(request)
        ignored.add(key)
        await db.set_setting(IGNORED_ARTISTS_KEY, json.dumps(sorted(ignored)))
        await db.log_admin("ignore_artists", ip=client_ip(request), target=key)
    return go(f"{ADMIN_PATH}/artists")


# Each song's artist box on the Fix artists screen is named this plus its
# video id, so one form can carry a whole list.
FIX_FIELD = "artist."


def _fix_path(name: str = "") -> str:
    return f"{ADMIN_PATH}/artists/fix" + ("?" + urlencode({"name": name}) if name else "")


@router.get("/artists/fix", response_class=HTMLResponse)
async def fix_artists_page(request: Request, name: str = "", done: int | None = None,
                           error: str = ""):
    """Songs to give an artist by hand, many at once: the ones with no artist
    (or a stand-in channel name like "Release - Topic" for one), or every
    song filed under one artist name."""
    token = await require_admin(request)
    db = ctx_of(request).db
    name = name.strip()[:256]
    banner = await db.admin_log_entry(done) if done else None
    return render(
        request, "artists_fix.html", token, section="artists",
        artist=name, songs=await db.songs_to_fix(name or None),
        spellings=await db.artist_spellings(), field_prefix=FIX_FIELD,
        banner=banner, labels=ACTION_LABELS, error=error,
    )


@router.post("/artists/fix")
async def fix_artists(request: Request):
    _token, form = await checked_form(request)
    ctx = ctx_of(request)
    name = field(form, "name")[:256]
    wanted: dict[str, str] = {}
    for key, values in form.items():
        if not key.startswith(FIX_FIELD):
            continue
        video_id = key[len(FIX_FIELD):]
        artist = values[0].strip() if values else ""
        if artist and VIDEO_ID_RE.match(video_id):
            wanted[video_id] = artist
    if not wanted:
        return go(_fix_path(name), error="Type an artist next to at least one song.")
    changed = await ctx.db.set_song_artists(wanted)
    if not changed:
        return go(_fix_path(name))
    names = sorted(set(wanted.values()), key=str.lower)
    shown = " · ".join(names[:5]) + (f" and {len(names) - 5} more" if len(names) > 5 else "")
    entry = await ctx.db.log_admin(
        "fix_artists", ip=client_ip(request), target=name or None,
        before=f"{len(changed)} song{'' if len(changed) == 1 else 's'}", after=shown,
        undo={"rows": changed},
    )
    ctx.invalidate()
    return go(_fix_path(name), done=entry)


# -- feeds --------------------------------------------------------------------


def feed_rows(request: Request, now: float) -> list[dict[str, Any]]:
    """One row per ingest path this server runs, with a plain-words status
    and a state for its chip: ok, warn, bad, or off."""
    ctx = ctx_of(request)
    settings = settings_of(request)
    config = settings.config
    seen = ctx.health.get("feeds", {})
    rows: list[dict[str, Any]] = []

    def poller(name: str, label: str, cfg: Any) -> None:
        if cfg is None or not cfg.enabled or not cfg.base_url:
            rows.append({"name": name, "label": label, "detail": "", "state": "off",
                         "status": "Off", "at": None, "last_ok": None, "probe": False})
            return
        info = seen.get(name, {})
        last_ok = info.get("last_ok")
        stale_after = cfg.poll_interval_s * FEED_STALE_FACTOR
        if info.get("status") == "error":
            state, status = "bad", "No answer"
        elif info.get("status") == "ok" and last_ok and now - last_ok > stale_after:
            state, status = "warn", "Quiet"
        elif info.get("status") == "ok":
            state, status = "ok", "OK"
        else:
            state, status = "dim", "Waiting for its first poll"
        rows.append({"name": name, "label": label, "detail": cfg.base_url, "state": state,
                     "status": status, "at": info.get("at"), "last_ok": last_ok,
                     "probe": True})

    poller("corescope", "CoreScope", config.corescope if config else None)
    poller("comchan", "comchan (backup)", config.comchan if config else None)

    if ctx.ingest_token:
        info = seen.get("relay", {})
        rows.append({"name": "relay", "label": "Relay from the Pi", "detail": "received here",
                     "state": "ok" if info else "dim",
                     "status": "OK" if info else "No push received since start",
                     "at": info.get("at"), "last_ok": info.get("at"), "probe": False})
    if settings.relay is not None:
        status = settings.relay.status
        if not status:
            state, text = "dim", "Not pushed yet"
        elif status.get("ok"):
            state, text = "ok", f"OK · public site has {status.get('remote_total', '?')} songs"
        else:
            state, text = "bad", f"Failed ({status.get('error')})"
        rows.append({"name": "relay-push", "label": "Relay to the public site",
                     "detail": config.relay.push_url if config else "", "state": state,
                     "status": text, "at": status.get("at"),
                     "last_ok": status.get("at") if status.get("ok") else None,
                     "probe": False, "resend": True})
    if config is not None and config.mesh.enabled:
        info = seen.get("mesh", {})
        link = info.get("status")
        rows.append({"name": "mesh", "label": "Mesh node", "detail": config.mesh.serial_port
                     or "auto-detected", "state": "ok" if link == "connected" else "bad",
                     "status": "Connected" if link == "connected" else "Not connected",
                     "at": info.get("at"), "last_ok": None, "probe": False})
    return rows


@router.get("/feeds", response_class=HTMLResponse)
async def feeds_page(request: Request):
    token = await require_admin(request)
    return render(request, "feeds.html", token, section="feeds",
                  feeds=feed_rows(request, time.time()), result=None)


@router.post("/feeds/{name}/probe", response_class=HTMLResponse)
async def probe_feed(request: Request, name: str):
    token, _form = await checked_form(request)
    config = settings_of(request).config
    feeds = {"corescope": config.corescope, "comchan": config.comchan} if config else {}
    feed = feeds.get(name)
    if feed is None or not feed.base_url:
        raise HTTPException(404, "no such feed")
    async with http_client(base_url=feed.base_url) as client:
        report = await probe(client, feed.channel, name=name)
    await ctx_of(request).db.log_admin(
        "probe_feed", ip=client_ip(request), target=name, after="ok" if report.ok else "failed"
    )
    return render(request, "feeds.html", token, section="feeds",
                  feeds=feed_rows(request, time.time()),
                  result={"name": name, "ok": report.ok, "lines": format_probe(report)})


@router.post("/feeds/relay/resend")
async def relay_resend(request: Request):
    """Reset the home node's push cursor, so its next push sends the whole
    channel again — for a public site whose archive was lost or rolled back
    in a way the automatic count check can't see."""
    _token, _form = await checked_form(request)
    if settings_of(request).relay is None:
        raise HTTPException(404)
    db = ctx_of(request).db
    await db.set_setting(CURSOR_KEY, "")
    await db.log_admin("relay_resend", ip=client_ip(request))
    return go(f"{ADMIN_PATH}/feeds")


# -- backups ------------------------------------------------------------------


def _backup_config(request: Request) -> Any:
    config = settings_of(request).config
    if config is None:
        raise HTTPException(404, "backups aren't available here")
    return config


@router.get("/backups", response_class=HTMLResponse)
async def backups_page(request: Request):
    token = await require_admin(request)
    config = _backup_config(request)
    return render(request, "backups.html", token, section="backups",
                  snapshots=snapshot_rows(config.backup_dir), backup=config.backup,
                  same_disk=not config.backup.dir)


@router.post("/backups")
async def take_backup(request: Request):
    _token, _form = await checked_form(request)
    config = _backup_config(request)
    dest = await asyncio.to_thread(backup_mod.snapshot, config.db_path, config.backup_dir,
                                   "manual")
    await asyncio.to_thread(backup_mod.rotate, config.backup_dir, config.backup.keep)
    await ctx_of(request).db.log_admin(
        "backup", ip=client_ip(request), after=dest.name if dest else None
    )
    return go(f"{ADMIN_PATH}/backups")


@router.get("/backups/{name}")
async def download_backup(request: Request, name: str):
    await require_admin(request)
    config = _backup_config(request)
    # Only a name the listing produced: never a path built from the URL.
    match = next(
        (p for p in backup_mod.list_snapshots(config.backup_dir) if p.name == name), None
    )
    if match is None:
        raise HTTPException(404, "no such backup")
    await ctx_of(request).db.log_admin("download_backup", ip=client_ip(request), target=name)
    return FileResponse(
        match, media_type="application/vnd.sqlite3", filename=name,
        headers={"content-encoding": "identity"},
    )


# -- activity log -------------------------------------------------------------


@router.get("/log", response_class=HTMLResponse)
async def log_page(request: Request, kind: str = "all", before: int | None = None,
                   error: str = ""):
    token = await require_admin(request)
    if kind not in ("all", "changes", "sign-ins"):
        kind = "all"
    entries = await ctx_of(request).db.admin_log_entries(kind, LOG_PAGE + 1, before)
    more = len(entries) > LOG_PAGE
    entries = entries[:LOG_PAGE]
    return render(request, "log.html", token, section="log", entries=entries, kind=kind,
                  labels=ACTION_LABELS, older=entries[-1]["id"] if more else None,
                  error=error)


UNDOABLE = {
    "rename_theme", "remove_track", "edit_track", "merge_artists", "fix_artists",
    "change_setting",
}


@router.post("/log/{entry_id}/undo")
async def undo(request: Request, entry_id: int):
    _token, form = await checked_form(request)
    ctx = ctx_of(request)
    db = ctx.db
    entry = await db.admin_log_entry(entry_id)
    back = field(form, "back")
    back = back if back.startswith(f"{ADMIN_PATH}/") or back == ADMIN_PATH else \
        f"{ADMIN_PATH}/log"
    if entry is None or entry["action"] not in UNDOABLE or not entry["undo"]:
        return go(f"{ADMIN_PATH}/log", error="That change can't be undone.")
    if entry["undone_by"]:
        return go(f"{ADMIN_PATH}/log", error="That change was already undone.")
    data = json.loads(entry["undo"])
    action = entry["action"]
    after = None
    if action == "rename_theme":
        theme = await db.theme_by_id(data["theme_id"])
        if theme is None:
            return go(f"{ADMIN_PATH}/log", error="That day's theme no longer exists.")
        try:
            await db.rename_theme(theme["id"], data["title"])
        except sqlite3.IntegrityError:
            return go(f"{ADMIN_PATH}/log", error="That day already has a theme with that title.")
        after = data["title"]
    elif action == "remove_track":
        outcome, label = await _put_back(request, data["date"], data["video_id"])
        if outcome == "gone":
            return go(f"{ADMIN_PATH}/log", error="That removal was already lifted.")
        after = label if outcome == "restored" else f"{label} (allowed back)"
    elif action == "edit_track":
        if not await db.revert_track_edit(
            data["track_id"], data["title"], data["artist"], data["edited_at"]
        ):
            return go(f"{ADMIN_PATH}/log", error="That song is no longer in the archive.")
        after = entry["before"]
    elif action == "fix_artists":
        for row in data["rows"]:
            await db.revert_track_edit(
                row["id"], row["title"], row["artist"], row["meta_edited_at"]
            )
        after = entry["before"]
    elif action == "merge_artists":
        await db.unmerge_artists(
            data["spellings"], data["canonical"], [tuple(c) for c in data["changed"]]
        )
        after = entry["before"]
    elif action == "change_setting":
        setting = overrides_mod.BY_KEY.get(data["key"])
        if setting is None or settings_of(request).config is None:
            return go(f"{ADMIN_PATH}/log", error="That setting can't be changed here any more.")
        error = await _set_override(request, setting, data["override"], data.get("had", True))
        if error:
            return go(f"{ADMIN_PATH}/log", error=error)
        after = entry["before"]
    new_id = await db.log_admin(
        "undo", ip=client_ip(request), target=entry["target"],
        before=f"{ACTION_LABELS.get(action, action)}: {entry['after'] or ''}", after=after,
    )
    await db.mark_undone(entry_id, new_id)
    ctx.invalidate()
    return go(back)


# -- config and device --------------------------------------------------------


def _config_groups(request: Request) -> list[dict[str, Any]]:
    """The editable settings, grouped, each with what its control needs."""
    admin = settings_of(request)
    config = admin.config
    groups = []
    for group_key, title in overrides_mod.GROUPS:
        items = []
        for setting in overrides_mod.visible(config, is_embed(request)):
            if setting.group != group_key:
                continue
            value = overrides_mod.current(config, setting)
            file_value = admin.file_values.get(setting.key, value)
            start, end = "22:00", "08:00"
            if setting.control == "quiet_hours" and value:
                start, end = value.split("-", 1)
            items.append({
                "s": setting,
                "value": value,
                "shown": overrides_mod.display(setting, value),
                "range": overrides_mod.bounds(setting, overrides_mod.display(setting, value)),
                "described": overrides_mod.describe(setting, value),
                "overridden": setting.key in admin.overrides,
                "file": overrides_mod.describe(setting, file_value),
                "waiting": setting.applies == "restart"
                and admin.started.get(setting.key) != value,
                "start": start.strip(),
                "end": end.strip(),
            })
        if items:
            groups.append({"key": group_key, "title": title, "items": items})
    return groups


@router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request, saved: str = "", error: str = "", group: str = ""):
    token = await require_admin(request)
    admin = settings_of(request)
    config = admin.config
    if config is None:
        return render(request, "config.html", token, section="config", rows=[], groups=[],
                      two_step=bool(admin.totp_secret))
    groups = _config_groups(request)
    editable = {i["s"].key for g in groups for i in g["items"]}
    waiting = [i["s"].label for g in groups for i in g["items"] if i["waiting"]]
    return render(
        request, "config.html", token, section="config",
        groups=groups, rows=config_rows(config, is_embed(request), editable),
        two_step=bool(admin.totp_secret), waiting=waiting,
        saved=saved[:200], error=error[:300],
        group=group,
    )


async def _set_override(
    request: Request, setting: overrides_mod.Setting, value: Any, overridden: bool
) -> str | None:
    """Put ``setting`` at ``value`` (kept as an admin-page change when
    ``overridden``, else back to the file's), check the result is a config
    the radio runs on, apply it to the running config and save. Returns an
    error for the operator, or None."""
    admin = settings_of(request)
    if not overridden:
        value = admin.file_values.get(setting.key, overrides_mod.current(admin.config, setting))
    try:
        overrides_mod.check(admin.config, setting, value)
    except ConfigError as exc:
        return f"{setting.label}: {str(exc).splitlines()[-1].strip()}"
    db = ctx_of(request).db
    saved = await overrides_mod.load(db)
    if overridden:
        saved[setting.key] = value
    else:
        saved.pop(setting.key, None)
    await overrides_mod.save(db, saved)
    admin.overrides = saved
    overrides_mod.apply(admin.config, setting, value)
    return None


async def _record_change(
    request: Request, setting: overrides_mod.Setting, before: Any, had: bool
) -> None:
    admin = settings_of(request)
    after = overrides_mod.current(admin.config, setting)
    await ctx_of(request).db.log_admin(
        "change_setting", ip=client_ip(request), target=setting.label,
        before=overrides_mod.describe(setting, before),
        after=overrides_mod.describe(setting, after)
        + ("" if setting.key in admin.overrides else " (the file's)"),
        undo={"key": setting.key, "override": before, "had": had},
    )


@router.post("/config")
async def save_config(request: Request):
    _token, form = await checked_form(request)
    admin = settings_of(request)
    if admin.config is None:
        raise HTTPException(404)
    group = field(form, "group")
    settings = [s for s in overrides_mod.visible(admin.config, is_embed(request))
                if s.group == group]
    if not settings:
        raise HTTPException(400, "unknown settings group")
    # Every value first, so one bad field changes nothing.
    wanted = []
    for setting in settings:
        before = overrides_mod.current(admin.config, setting)
        try:
            value = overrides_mod.parse(setting, form, overrides_mod.display(setting, before))
        except ValueError as exc:
            return go(f"{ADMIN_PATH}/config", error=str(exc), group=group)
        # Compared as the control shows it, so a value the file gives more
        # finely than the slider steps isn't "changed" by an untouched slider.
        if overrides_mod.display(setting, value) != overrides_mod.display(setting, before):
            wanted.append((setting, value, before))
    for setting, value, _before in wanted:
        try:
            overrides_mod.check(admin.config, setting, value)
        except ConfigError as exc:
            return go(f"{ADMIN_PATH}/config", group=group,
                      error=f"{setting.label}: {str(exc).splitlines()[-1].strip()}")
    for setting, value, before in wanted:
        had = setting.key in admin.overrides
        error = await _set_override(request, setting, value, True)
        if error:
            return go(f"{ADMIN_PATH}/config", error=error, group=group)
        await _record_change(request, setting, before, had)
    names = ", ".join(s.label for s, _v, _b in wanted)
    return go(f"{ADMIN_PATH}/config", group=group,
              saved=f"Saved: {names}." if wanted else "Nothing changed.")


@router.post("/config/reset")
async def reset_setting(request: Request):
    _token, form = await checked_form(request)
    admin = settings_of(request)
    if admin.config is None:
        raise HTTPException(404)
    setting = overrides_mod.BY_KEY.get(field(form, "reset"))
    if setting is None or setting not in overrides_mod.visible(admin.config, is_embed(request)):
        raise HTTPException(400, "not a setting this page changes")
    if setting.key not in admin.overrides:
        return go(f"{ADMIN_PATH}/config", group=setting.group,
                  saved=f"{setting.label} already uses the file's value.")
    before = overrides_mod.current(admin.config, setting)
    error = await _set_override(request, setting, None, False)
    if error:
        return go(f"{ADMIN_PATH}/config", error=error, group=setting.group)
    await _record_change(request, setting, before, True)
    return go(f"{ADMIN_PATH}/config", group=setting.group,
              saved=f"{setting.label} is back to the file's value.")


@router.get("/device", response_class=HTMLResponse)
async def device_page(request: Request):
    token = await require_admin(request)
    if is_embed(request):
        raise HTTPException(404)
    ctx = ctx_of(request)
    config = settings_of(request).config
    cache_bytes = None
    if config is not None:
        cache_bytes = await asyncio.to_thread(_dir_bytes, config.cache_dir)
    router_ = ctx.audio_router
    return render(
        request, "device.html", token, section="device",
        ytdlp=ctx.health.get("ytdlp_version"),
        cache_bytes=cache_bytes,
        cache_max=config.cache.max_bytes if config is not None else None,
        counts=await ctx.db.cache_status_counts(),
        failed=await ctx.db.failed_tracks(),
        output=router_.current() if router_ is not None else None,
        outputs=router_.outputs() if router_ is not None else [],
    )


def _dir_bytes(path: Path) -> int:
    total = 0
    try:
        for entry in Path(path).iterdir():
            if entry.is_file():
                total += entry.stat().st_size
    except OSError:
        return total
    return total


# -- template filters ---------------------------------------------------------


def fmt_ago(seconds: float | None) -> str:
    """``125`` -> ``2 min``: how long ago, to the unit that matters."""
    if seconds is None:
        return "never"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} days"


def fmt_when(value: Any, tz: Any) -> str:
    """An epoch or a stored ``…Z`` timestamp as ``6 Oct 12:40`` in the
    channel's time zone."""
    if value in (None, ""):
        return "—"
    try:
        if isinstance(value, int | float):
            moment = datetime.fromtimestamp(value, UTC)
        else:
            moment = datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (ValueError, OverflowError, OSError):
        return str(value)
    local = moment.astimezone(tz)
    return f"{local.day} {local:%b %H:%M}"


def fmt_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def short_ip(ip: str | None) -> str:
    """Enough of an address to tell yours from a stranger's on screen; the
    log keeps it whole."""
    if not ip:
        return "—"
    if ":" in ip:
        return ":".join(ip.split(":")[:2]) + ":…"
    parts = ip.split(".")
    return ".".join(parts[:2]) + ".x.x" if len(parts) == 4 else ip


FILTERS = {"ago": fmt_ago, "when": fmt_when, "bytes": fmt_bytes, "short_ip": short_ip}


def admin_headers(response: Response) -> None:
    """Admin pages are nobody's cache entry and nobody's search result."""
    response.headers["cache-control"] = "no-store"
    response.headers["x-robots-tag"] = "noindex, nofollow"
