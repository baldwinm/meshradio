"""Days and songs: retitle a day, correct or remove a song, retry one that
failed, and put removed songs back."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ... import backup as backup_mod
from ...bus import TRACK_DISCOVERED
from ...ingest.parse import untitled_theme
from ...media.cacher import drop_cache_file
from ..admin_auth import ADMIN_PATH, AdminSettings
from ..context import ctx_of
from ..routes_api import unplayable_checks
from .backups import snapshot_rows
from .common import (
    ACTION_LABELS,
    ISO_DATE,
    ISO_MONTH,
    checked_form,
    client_ip,
    field,
    go,
    is_embed,
    render,
    require_admin,
    settings_of,
)

log = logging.getLogger(__name__)


router = APIRouter(prefix=ADMIN_PATH)

# One "before removing a song" snapshot covers a burst of removals: a clean-up
# of ten songs must not rotate every scheduled snapshot out of the keep count.
PREREMOVE_SNAPSHOT_EVERY_S = 10 * 60


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


async def put_back(request: Request, date: str, video_id: str) -> tuple[str, str]:
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
    outcome, label = await put_back(request, date, video_id)
    if outcome == "gone":
        return go(f"{ADMIN_PATH}/removed")
    entry = await ctx_of(request).db.log_admin(
        "restore_track", ip=client_ip(request), target=date, before="removed",
        after=label if outcome == "restored" else f"{label} (allowed back)",
    )
    return go(f"{ADMIN_PATH}/removed", done=entry)
