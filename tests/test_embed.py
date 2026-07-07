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
    c = Cacher(CacheConfig(max_retries=1), Path(tmp_path), db, bus, embed=True)
    track = await make_pending_track(db, "aaaaaaaaaaa")
    sub = bus.subscribe(TRACK_FAILED)
    await c.process_track(track)
    _, payload = await asyncio.wait_for(sub.get(), 1)
    assert payload["track"]["cache_status"] == "failed"


async def test_embed_oembed_retries_transient_failures(db, bus, monkeypatch, tmp_path):
    """A throttled oEmbed call leaves the track pending; the periodic sweep
    retries and it goes ready once YouTube answers again."""
    calls = {"n": 0}

    async def flaky_oembed(video_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return {"title": "Song A", "artist": "Artist A", "thumbnail": ""}

    monkeypatch.setattr(cacher_mod.metadata, "fetch_oembed", flaky_oembed)
    c = Cacher(CacheConfig(), Path(tmp_path), db, bus, embed=True)
    track = await make_pending_track(db, "aaaaaaaaaaa")
    await c.process_track(track)
    assert (await db.track_by_id(track["id"]))["cache_status"] == "pending"
    await c.process_track(track)   # what the sweep does a minute later
    assert (await db.track_by_id(track["id"]))["cache_status"] == "ready"


async def test_embed_ready_without_oembed_when_meta_relayed(db, bus, monkeypatch, tmp_path):
    """Relayed messages carry title/artist/duration; the embed cacher must
    go straight to ready without asking YouTube anything."""
    from meshradio.ingest.service import IngestService

    async def must_not_run(video_id):
        raise AssertionError("oEmbed called despite relayed metadata")

    monkeypatch.setattr(cacher_mod.metadata, "fetch_oembed", must_not_run)
    c = Cacher(CacheConfig(), Path(tmp_path), db, bus, embed=True)
    ingest = IngestService(db, bus, channel="#music")
    await ingest.handle_message(
        sender="bob",
        text="https://youtu.be/aaaaaaaaaaa",
        ts=1_783_400_000.0,
        source="corescope",
        meta={"title": "Song A", "artist": "Artist A", "duration": 213.0},
    )
    track = (await db.tracks_since(""))[0]
    assert track["title"] == "Song A"
    assert track["duration"] == 213.0
    await c.process_track(track)
    assert (await db.track_by_id(track["id"]))["cache_status"] == "ready"


async def test_meta_backfills_deduped_track(db, bus):
    """A re-pushed message for a known bare track fills in its metadata."""
    from meshradio.ingest.service import IngestService

    ingest = IngestService(db, bus, channel="#music")
    await ingest.handle_message(
        sender="bob", text="https://youtu.be/aaaaaaaaaaa", ts=1_783_400_000.0,
        source="corescope",
    )
    assert (await db.tracks_since(""))[0]["title"] is None
    inserted = await ingest.handle_message(
        sender="bob", text="https://youtu.be/aaaaaaaaaaa", ts=1_783_400_000.0,
        source="corescope", meta={"title": "Song A", "duration": 213.0},
    )
    assert inserted == 0                      # deduped, but…
    row = (await db.tracks_since(""))[0]
    assert row["title"] == "Song A"           # …metadata landed anyway
    assert row["duration"] == 213.0


async def test_process_track_skips_already_ready(db, bus, monkeypatch, tmp_path):
    """Sweep + event stream can deliver the same track twice; the second
    delivery must not refetch or re-announce it."""
    async def must_not_run(video_id):
        raise AssertionError("oEmbed called for an already-ready track")

    monkeypatch.setattr(cacher_mod.metadata, "fetch_oembed", must_not_run)
    c = Cacher(CacheConfig(), Path(tmp_path), db, bus, embed=True)
    track = await make_pending_track(db, "aaaaaaaaaaa")
    await db.set_cache_status(track["id"], "ready")
    stale = dict(track, cache_status="pending")   # snapshot from before
    await c.process_track(stale)                  # no exception, no calls


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
