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
