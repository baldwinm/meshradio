"""Embed mode: browser streams from YouTube, server never touches audio."""

import asyncio
from pathlib import Path

from meshradio.bus import TRACK_FAILED, TRACK_READY
from meshradio.config import CacheConfig, PlayerConfig
from meshradio.media import cacher as cacher_mod
from meshradio.media.cacher import Cacher
from meshradio.media.player import EmbedBackend, PlayerService

from .test_player import make_ready_track


async def make_pending_track(db, video_id):
    theme = await db.create_theme("2026-07-06", "test theme")
    return await db.add_track(
        video_id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        channel="#music",
        sender="alice",
        mesh_ts=1_783_443_600.0,
        source="mesh",
        theme_id=theme["id"],
    )


async def test_embed_plays_without_cache_path(db, bus):
    player = PlayerService(PlayerConfig(), db, bus, backend=EmbedBackend())
    track = await make_pending_track(db, "aaaaaaaaaaa")
    await db.set_cache_status(track["id"], "ready")  # ready, no file
    await player.play_track(await db.track_by_id(track["id"]))
    assert player.status == "playing"
    assert player.state()["embed"] is True


async def test_embed_cacher_skips_download(db, bus, monkeypatch, tmp_path):
    async def fake_oembed(video_id):
        return {"title": "Song A", "artist": "Artist A", "thumbnail": ""}

    monkeypatch.setattr(cacher_mod.metadata, "fetch_oembed", fake_oembed)
    cache_dir = Path(tmp_path) / "cache"
    cache_dir.mkdir()
    c = Cacher(CacheConfig(), cache_dir, db, bus, embed=True)
    track = await make_pending_track(db, "aaaaaaaaaaa")
    sub = bus.subscribe(TRACK_READY)
    await c.process_track(track)
    _, payload = await asyncio.wait_for(sub.get(), 1)
    ready = payload["track"]
    assert ready["cache_status"] == "ready"
    assert ready["cache_path"] is None          # nothing downloaded
    assert ready["title"] == "Song A"
    assert list(cache_dir.iterdir()) == []       # cache dir untouched


async def test_embed_cacher_fails_unresolvable(db, bus, monkeypatch, tmp_path):
    async def fake_oembed(video_id):
        return None  # deleted/private video

    monkeypatch.setattr(cacher_mod.metadata, "fetch_oembed", fake_oembed)
    c = Cacher(CacheConfig(), Path(tmp_path), db, bus, embed=True)
    track = await make_pending_track(db, "aaaaaaaaaaa")
    sub = bus.subscribe(TRACK_FAILED)
    await c.process_track(track)
    _, payload = await asyncio.wait_for(sub.get(), 1)
    assert payload["track"]["cache_status"] == "failed"


async def test_report_duration_fills_missing(db, bus):
    player = PlayerService(PlayerConfig(), db, bus, backend=EmbedBackend())
    track = await make_pending_track(db, "aaaaaaaaaaa")
    await db.set_cache_status(track["id"], "ready")
    await player.play_track(await db.track_by_id(track["id"]))
    assert player.current["duration"] is None
    await player.report_duration(track["id"], 212.5)
    assert player.current["duration"] == 212.5
    row = await db.track_by_id(track["id"])
    assert row["duration"] == 212.5
    await player.report_duration(track["id"], -1)  # bogus values ignored
    assert player.current["duration"] == 212.5


async def test_web_backend_state_not_embed(db, bus):
    from meshradio.media.player import WebBackend

    player = PlayerService(PlayerConfig(), db, bus, backend=WebBackend())
    await make_ready_track(db, "aaaaaaaaaaa", duration=60)
    assert player.state()["embed"] is False
