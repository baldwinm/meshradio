"""Playback / queue / day JSON+partial API."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .context import ctx_of

router = APIRouter()


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
async def api_seek(request: Request, seconds: float):
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
    ctx = ctx_of(request)
    await (await ctx.get_player(request)).enqueue_track_id(track_id)
    return JSONResponse({"ok": True})


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


@router.post("/api/duration/{track_id}/{seconds}")
async def api_duration(request: Request, track_id: int, seconds: float):
    """The embed speaker tab reports a track's real duration (embed tracks
    start without one — oEmbed metadata has no length)."""
    ctx = ctx_of(request)
    await (await ctx.get_player(request)).report_duration(track_id, seconds)
    return JSONResponse({"ok": True})


@router.post("/api/radio/start")
async def api_radio_start(request: Request):
    ctx = ctx_of(request)
    await (await ctx.get_player(request)).start_radio()
    return await ctx.render_now_playing(request)


@router.post("/api/radio/stop")
async def api_radio_stop(request: Request):
    ctx = ctx_of(request)
    await (await ctx.get_player(request)).stop_radio()
    return await ctx.render_now_playing(request)


@router.post("/api/output/{name}")
async def api_output(request: Request, name: str):
    ctx = ctx_of(request)
    ok = await ctx.audio_router.set_output(name)
    return JSONResponse({"ok": ok, "output": ctx.audio_router.current()})


@router.get("/api/outputs")
async def api_outputs(request: Request):
    ctx = ctx_of(request)
    return JSONResponse({
        "outputs": ctx.audio_router.outputs(),
        "current": ctx.audio_router.current(),
    })
