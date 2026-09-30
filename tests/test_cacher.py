from meshradio.config import CacheConfig
from meshradio.db import Database
from meshradio.media.cacher import Cacher


def _make_cacher(tmp_path, db, bus, max_bytes):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    return Cacher(CacheConfig(max_bytes=max_bytes), cache_dir, db, bus), cache_dir


async def _ready_track(db, video_id, path):
    track = await db.add_track(
        video_id=video_id,
        url=f"https://youtu.be/{video_id}",
        channel="#music",
        sender="alice",
        mesh_ts=1_751_800_000.0,
        source="mesh",
        theme_id=None,
    )
    await db.set_cache_status(track["id"], "ready", str(path))
    return track


async def test_prune_under_cap_is_noop(tmp_path, db: Database, bus):
    cacher, cache_dir = _make_cacher(tmp_path, db, bus, max_bytes=1000)
    f = cache_dir / "dQw4w9WgXcQ.opus"
    f.write_bytes(b"x" * 100)
    track = await _ready_track(db, "dQw4w9WgXcQ", f)

    await cacher.prune(added_bytes=100)

    assert f.exists()
    assert (await db.track_by_id(track["id"]))["cache_status"] == "ready"
    assert cacher._cache_bytes == 100  # seeded from disk, still under cap


async def test_prune_evicts_lru_over_cap(tmp_path, db: Database, bus):
    cacher, cache_dir = _make_cacher(tmp_path, db, bus, max_bytes=150)
    old = cache_dir / "dQw4w9WgXcQ.opus"
    new = cache_dir / "9bZkp7q19f0.opus"
    old.write_bytes(b"x" * 100)
    new.write_bytes(b"x" * 100)
    old_track = await _ready_track(db, "dQw4w9WgXcQ", old)
    new_track = await _ready_track(db, "9bZkp7q19f0", new)
    # old_track played once so it's the more-recently-used; new_track (never
    # played) is the LRU victim... but LRU order also weights ingest time, so
    # play the OLD one to make it clearly the keeper.
    await db.record_play(old_track["id"], "speaker")

    await cacher.prune(added_bytes=100)  # 200 > 150 cap

    assert not new.exists()
    assert old.exists()
    assert (await db.track_by_id(new_track["id"]))["cache_status"] == "pending"
    assert (await db.track_by_id(old_track["id"]))["cache_status"] == "ready"
    assert cacher._cache_bytes <= cacher.config.max_bytes


async def test_prune_seeds_estimate_from_disk_once(tmp_path, db: Database, bus):
    cacher, cache_dir = _make_cacher(tmp_path, db, bus, max_bytes=10_000)
    (cache_dir / "dQw4w9WgXcQ.opus").write_bytes(b"x" * 500)

    # First prune seeds the estimate from disk (500) even with added_bytes=0.
    await cacher.prune()
    assert cacher._cache_bytes == 500


async def _pending(db, video_id):
    return await db.add_track(
        video_id=video_id, url=f"https://youtu.be/{video_id}", channel="#music",
        sender="alice", mesh_ts=1_751_800_000.0, source="mesh", theme_id=None,
    )


async def _settle(predicate, tries=200):
    import asyncio
    for _ in range(tries):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def test_workers_run_a_few_downloads_at_once_and_each_track_once(tmp_path, db, bus, monkeypatch):
    """One download yt-dlp sits on must not hold up the backlog; and a track
    the sweep and the event stream both hand over is worked exactly once."""
    import asyncio
    from meshradio.bus import TRACK_DISCOVERED

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cacher = Cacher(CacheConfig(concurrency=2), cache_dir, db, bus)
    tracks = [await _pending(db, f"{i:011d}") for i in range(5)]
    running = {"now": 0, "peak": 0, "downloads": []}

    async def fake_download(url, video_id):
        running["now"] += 1
        running["peak"] = max(running["peak"], running["now"])
        running["downloads"].append(video_id)
        await asyncio.sleep(0.05)
        running["now"] -= 1
        path = cache_dir / f"{video_id}.opus"
        path.write_bytes(b"x")
        return {"title": video_id, "_filepath": path, "_filesize": 1}

    monkeypatch.setattr(cacher, "_download", fake_download)
    cacher.start()
    for track in tracks:                       # the event stream duplicates the sweep
        bus.publish(TRACK_DISCOVERED, {"track": track})
    try:
        async def all_ready():
            statuses = [(await db.track_by_id(t["id"]))["cache_status"] for t in tracks]
            return all(s == "ready" for s in statuses)
        for _ in range(300):
            if await all_ready():
                break
            await asyncio.sleep(0.01)
        assert await all_ready()
    finally:
        await cacher.stop()
    assert running["peak"] == 2                # capped at concurrency
    assert sorted(running["downloads"]) == sorted(t["video_id"] for t in tracks)   # once each
    assert cacher._inflight == {}


async def test_embed_lookups_share_one_http_client(tmp_path, db, bus, monkeypatch):
    import asyncio
    import httpx
    from meshradio.media import cacher as cacher_mod

    seen = []

    async def fake_oembed(video_id, client=None):
        seen.append(client)
        return {"title": video_id, "artist": "", "thumbnail": ""}

    monkeypatch.setattr(cacher_mod.metadata, "fetch_oembed", fake_oembed)
    cacher = Cacher(CacheConfig(), tmp_path, db, bus, embed=True)
    tracks = [await _pending(db, f"{i:011d}") for i in range(3)]
    cacher.start()
    try:
        assert await _settle(lambda: len(seen) == 3)
    finally:
        await cacher.stop()
    assert all(isinstance(c, httpx.AsyncClient) for c in seen)
    assert len({id(c) for c in seen}) == 1                     # the same one every time
    assert cacher._http is None                                # closed with the service
    # Outside the running service (a direct call) the lookup still works.
    await cacher.process_track(await _pending(db, "zzzzzzzzzzz"))
    assert seen[-1] is None
