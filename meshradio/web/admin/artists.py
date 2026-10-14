"""Artist names: merge look-alike spellings, keep ones that only look
alike apart, and give songs their artist by hand (Fix artists)."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ...db.fields import VIDEO_ID_RE
from ..admin_auth import ADMIN_PATH
from ..context import ctx_of
from .common import ACTION_LABELS, checked_form, client_ip, field, go, render, require_admin

router = APIRouter(prefix=ADMIN_PATH)

IGNORED_ARTISTS_KEY = "admin.artists_ignored"

_ARTIST_PUNCT = re.compile(r"[\W_]+")


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
