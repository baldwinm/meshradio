"""The device screen (the Pi only): yt-dlp, the audio cache, failed
downloads and the audio output."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..admin_auth import ADMIN_PATH
from ..context import ctx_of
from .common import is_embed, render, require_admin, settings_of

router = APIRouter(prefix=ADMIN_PATH)


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
