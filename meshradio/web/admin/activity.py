"""The activity log, and Undo for the changes that carry what reversing
them needs."""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ... import config_overrides as overrides_mod
from ..admin_auth import ADMIN_PATH
from ..context import client_host, ctx_of
from .common import ACTION_LABELS, checked_form, field, go, render, require_admin, settings_of
from .config import set_override
from .days import put_back

router = APIRouter(prefix=ADMIN_PATH)

LOG_PAGE = 100


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


class _UndoFailed(Exception):
    """An undo that can't go ahead; the message is shown on the log page."""


async def _undo_rename_theme(request: Request, entry, data) -> str | None:
    db = ctx_of(request).db
    theme = await db.theme_by_id(data["theme_id"])
    if theme is None:
        raise _UndoFailed("That day's theme no longer exists.")
    try:
        await db.rename_theme(theme["id"], data["title"])
    except sqlite3.IntegrityError:
        raise _UndoFailed("That day already has a theme with that title.") from None
    return data["title"]


async def _undo_remove_track(request: Request, entry, data) -> str | None:
    outcome, label = await put_back(request, data["date"], data["video_id"])
    if outcome == "gone":
        raise _UndoFailed("That removal was already lifted.")
    return label if outcome == "restored" else f"{label} (allowed back)"


async def _undo_edit_track(request: Request, entry, data) -> str | None:
    if not await ctx_of(request).db.revert_track_edit(
        data["track_id"], data["title"], data["artist"], data["edited_at"]
    ):
        raise _UndoFailed("That song is no longer in the archive.")
    return entry["before"]


async def _undo_fix_artists(request: Request, entry, data) -> str | None:
    db = ctx_of(request).db
    for row in data["rows"]:
        await db.revert_track_edit(
            row["id"], row["title"], row["artist"], row["meta_edited_at"]
        )
    return entry["before"]


async def _undo_merge_artists(request: Request, entry, data) -> str | None:
    await ctx_of(request).db.unmerge_artists(
        data["spellings"], data["canonical"], [tuple(c) for c in data["changed"]]
    )
    return entry["before"]


async def _undo_change_setting(request: Request, entry, data) -> str | None:
    setting = overrides_mod.BY_KEY.get(data["key"])
    if setting is None or settings_of(request).config is None:
        raise _UndoFailed("That setting can't be changed here any more.")
    error = await set_override(request, setting, data["override"], data.get("had", True))
    if error:
        raise _UndoFailed(error)
    return entry["before"]


# How to reverse each undoable action; each returns the value the undo leaves.
_UNDOERS = {
    "rename_theme": _undo_rename_theme,
    "remove_track": _undo_remove_track,
    "edit_track": _undo_edit_track,
    "fix_artists": _undo_fix_artists,
    "merge_artists": _undo_merge_artists,
    "change_setting": _undo_change_setting,
}
UNDOABLE = set(_UNDOERS)


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
    action = entry["action"]
    try:
        after = await _UNDOERS[action](request, entry, json.loads(entry["undo"]))
    except _UndoFailed as exc:
        return go(f"{ADMIN_PATH}/log", error=str(exc))
    new_id = await db.log_admin(
        "undo", ip=client_host(request), target=entry["target"],
        before=f"{ACTION_LABELS.get(action, action)}: {entry['after'] or ''}", after=after,
    )
    await db.mark_undone(entry_id, new_id)
    ctx.invalidate()
    return go(back)
