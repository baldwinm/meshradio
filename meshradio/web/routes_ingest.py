"""Relay receiver, cached-audio streaming, and health."""

from __future__ import annotations

import secrets
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from .context import ctx_of

router = APIRouter()

_AUDIO_TYPES = {
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".webm": "audio/webm",
}


@router.get("/audio/{track_id}")
async def audio(request: Request, track_id: int):
    """Stream a cached track to the browser (web playback)."""
    track = await ctx_of(request).db.track_by_id(track_id)
    if not track or track["cache_status"] != "ready" or not track["cache_path"]:
        raise HTTPException(404, "track not cached")
    path = Path(track["cache_path"])
    if not path.exists():
        raise HTTPException(404, "cache file missing")
    media_type = _AUDIO_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media_type)


@router.post("/api/ingest")
async def api_ingest(request: Request):
    """Relay receiver: a home node pushes channel messages here when this
    instance can't poll CoreScope itself (Cloudflare challenges datacenter
    IPs). Shared-token auth; the ingest pipeline's dedupe makes replays
    harmless."""
    ctx = ctx_of(request)
    if not ctx.ingest_token or ctx.ingest is None:
        raise HTTPException(404)
    supplied = request.headers.get("authorization", "")
    if not secrets.compare_digest(supplied, f"Bearer {ctx.ingest_token}"):
        raise HTTPException(401, "bad token")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON")
    inserted = 0
    for msg in (payload.get("messages") or [])[:5000]:
        try:
            meta = msg.get("meta")
            inserted += await ctx.ingest.handle_message(
                sender=str(msg["sender"]),
                text=str(msg["text"]),
                ts=float(msg["ts"]),
                source="corescope",  # relayed community-channel history
                meta=meta if isinstance(meta, dict) else None,
            )
        except (KeyError, TypeError, ValueError):
            continue  # skip malformed entries, keep the batch going
    ctx.health["last_ingest"] = time.time()
    # Total lets the pusher detect a wiped DB (ephemeral hosting) and reset
    # its cursor for a full re-backfill.
    return JSONResponse({
        "ok": True,
        "inserted": inserted,
        "tracks": await ctx.db.channel_track_count(),
    })


@router.get("/healthz")
async def healthz(request: Request):
    """Liveness + basic freshness; Render's health check hits this."""
    ctx = ctx_of(request)
    last = ctx.health["last_ingest"]
    return JSONResponse({
        "ok": True,
        "tracks": await ctx.db.channel_track_count(),
        "sessions": ctx.sessions.count() if ctx.sessions else None,
        "ingest_age_s": round(time.time() - last, 1) if last else None,
    })
