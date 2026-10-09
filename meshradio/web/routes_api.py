"""Playback / queue / day JSON+partial API."""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Path, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..bus import TRACK_FAILED
from ..media import metadata
from .context import ctx_of

log = logging.getLogger(__name__)

router = APIRouter()

# YouTube player errors that are about the video itself rather than this
# browser: 2 = bad id, 100 = removed or private, 101/150 = the owner turned
# embedding off. 5 (an HTML5 player hiccup) is left out.
DEAD_VIDEO_CODES = frozenset({2, 100, 101, 150})
# How long one video's report stays answered: a song is checked with YouTube
# at most this often, however many tabs (or scripts) report it.
UNPLAYABLE_RECHECK_S = 3600.0

# A position or a length in seconds. Anything past a day is a mistake, and
# ``inf``/``nan`` parse as floats too: a seek to infinity leaves the player's
# clock unserialisable (every state read 500s), and a reported duration lands
# in the shared tracks table, where it breaks every session that queues the
# song. Reject at the edge so nothing downstream has to think about it.
_SECONDS: dict[str, Any] = dict(allow_inf_nan=False, le=24 * 3600)


@router.get("/api/state")
async def api_state(request: Request):
    ctx = ctx_of(request)
    return JSONResponse((await ctx.get_player(request)).state())


@router.post("/api/skip")
async def api_skip(request: Request):
    ctx = ctx_of(request)
    await (await ctx.get_player(request)).skip()
    return await ctx.render_now_playing(request)


@router.post("/api/pause")
async def api_pause(request: Request):
    ctx = ctx_of(request)
    await (await ctx.get_player(request)).toggle_pause()
    return await ctx.render_now_playing(request)


@router.post("/api/volume/{level}")
async def api_volume(request: Request, level: int):
    ctx = ctx_of(request)
    await (await ctx.get_player(request)).set_volume(level)
    return await ctx.render_now_playing(request)


@router.post("/api/seek/{seconds}")
async def api_seek(request: Request, seconds: float = Path(ge=0, **_SECONDS)):
    p = await ctx_of(request).get_player(request)
    await p.seek(seconds)
    return JSONResponse({"ok": True, "position": p.position()})


# NOTE: literal /api/queue/* routes must be registered before the
# /api/queue/{track_id} catch-all or "clear" gets parsed as a track id.
@router.post("/api/queue/clear")
async def api_queue_clear(request: Request):
    ctx = ctx_of(request)
    await (await ctx.get_player(request)).clear_queue()
    return await ctx.render_queue(request)


@router.post("/api/queue/shuffle")
async def api_queue_shuffle(request: Request):
    ctx = ctx_of(request)
    await (await ctx.get_player(request)).shuffle_queue()
    return await ctx.render_queue(request)


@router.post("/api/queue/remove/{index}/{track_id}")
async def api_queue_remove(request: Request, index: int, track_id: int):
    ctx = ctx_of(request)
    await (await ctx.get_player(request)).remove_from_queue(index, track_id)
    return await ctx.render_queue(request)


@router.post("/api/queue/top/{index}/{track_id}")
async def api_queue_top(request: Request, index: int, track_id: int):
    ctx = ctx_of(request)
    await (await ctx.get_player(request)).move_to_front(index, track_id)
    return await ctx.render_queue(request)


@router.post("/api/queue/{track_id}")
async def api_enqueue(request: Request, track_id: int):
    """``queued`` is False when the press changed nothing: the song is
    already playing or queued, the queue is at its ceiling, or there's no
    such playable track."""
    ctx = ctx_of(request)
    queued = await (await ctx.get_player(request)).enqueue_track_id(track_id)
    return JSONResponse({"ok": True, "queued": bool(queued)})


@router.post("/api/play-day/{date}")
async def api_play_day(request: Request, date: str):
    ctx = ctx_of(request)
    await (await ctx.get_player(request)).play_day(date)
    return RedirectResponse("/", status_code=303)


@router.post("/api/play-today")
async def api_play_today(request: Request):
    """Default play action: today's songs, else the newest archived day."""
    ctx = ctx_of(request)
    p = await ctx.get_player(request)
    today = ctx.today()
    await p.play_day(today)
    if p.status != "playing":
        for d in await ctx.db.archive_days():  # newest first
            if d["date"] < today and d["tracks"]:
                await p.play_day(d["date"])
                if p.status == "playing":
                    break
    return await ctx.render_now_playing(request)


@router.post("/api/ended/{track_id}")
async def api_ended(request: Request, track_id: int):
    """Browser reports its <audio>/embed player finished the track."""
    ctx = ctx_of(request)
    advanced = await (await ctx.get_player(request)).notify_ended(track_id)
    return JSONResponse({"advanced": advanced})


@router.post("/api/unplayable/{track_id}/{code}")
async def api_unplayable(
    request: Request, track_id: int, code: int, background: BackgroundTasks
):
    """The embed speaker tab's YouTube player refused the current song.

    The report is unauthenticated and the tracks table is shared, so it is
    only a hint: it counts for the song this visitor is playing right now,
    and the song is marked unplayable only when YouTube's own oEmbed lookup
    agrees it can't be embedded (a removed, private or embed-disabled video
    gets no answer there). A region or age block, which oEmbed still
    describes, leaves the song alone. The check runs after the response, so
    the tab's skip to the next song isn't held up by it."""
    ctx = ctx_of(request)
    p = await ctx.get_player(request)
    cur = p.current
    if not p.embed or code not in DEAD_VIDEO_CODES or not cur or cur["id"] != track_id:
        return JSONResponse({"checking": False})
    checked = unplayable_checks(request)
    now = time.monotonic()
    video_id = cur["video_id"]
    last = checked.get(video_id)
    if last is not None and now - last < UNPLAYABLE_RECHECK_S:
        return JSONResponse({"checking": False})
    checked[video_id] = now
    background.add_task(_confirm_unplayable, ctx, video_id, code)
    return JSONResponse({"checking": True})


def unplayable_checks(request: Request) -> dict[str, float]:
    """Video id -> when it was last checked, per app (admin's Try again
    clears an entry so a fresh failure is looked at again)."""
    state = request.app.state
    if not hasattr(state, "unplayable_checks"):
        state.unplayable_checks = {}
    return state.unplayable_checks


async def _confirm_unplayable(ctx: Any, video_id: str, code: int) -> None:
    if await metadata.fetch_oembed(video_id) is not None:
        return
    changed = await ctx.db.mark_video_failed(video_id)
    if changed:
        log.warning(
            "YouTube won't play %s (player error %d); marked %d share(s) unplayable",
            video_id, code, len(changed),
        )
    for row in changed:
        ctx.bus.publish(TRACK_FAILED, {"track": row})


@router.post("/api/duration/{track_id}/{seconds}")
async def api_duration(
    request: Request, track_id: int, seconds: float = Path(gt=0, **_SECONDS)
):
    """The embed speaker tab reports a track's real duration (embed tracks
    start without one — oEmbed metadata has no length). Only a missing
    duration is ever filled (see ``PlayerService.report_duration``)."""
    ctx = ctx_of(request)
    await (await ctx.get_player(request)).report_duration(track_id, seconds)
    return JSONResponse({"ok": True})


@router.post("/api/station/{kind}")
async def api_station(request: Request, kind: Literal["radio", "archive", "off"]):
    """Pick what plays once the queue runs dry: a YouTube Mix ('radio'), random
    channel history ('archive'), or nothing ('off')."""
    ctx = ctx_of(request)
    p = await ctx.get_player(request)
    if kind == "off":
        await p.stop_station()
    else:
        await p.start_station(kind)
    return await ctx.render_now_playing(request)


# Output selection — speaker, jack, Bluetooth — is the appliance's. Public
# embed hosting has nothing to select (each visitor's browser is their own
# output), so server.py mounts this router only off-embed instead of leaving
# routes up that answer for a no-op.
output_router = APIRouter()


@output_router.post("/api/output/{name}")
async def api_output(request: Request, name: str):
    ctx = ctx_of(request)
    ok = await ctx.audio_router.set_output(name)
    return JSONResponse({"ok": ok, "output": ctx.audio_router.current()})


@output_router.get("/api/outputs")
async def api_outputs(request: Request):
    ctx = ctx_of(request)
    return JSONResponse({
        "outputs": ctx.audio_router.outputs(),
        "current": ctx.audio_router.current(),
    })
