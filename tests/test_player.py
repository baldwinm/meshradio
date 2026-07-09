import asyncio
import random
import time
from datetime import datetime

from meshradio.bus import EventBus, PLAYER_STATE
from meshradio.config import PlayerConfig
from meshradio.db import Database
from meshradio.media.player import EmbedBackend, NullBackend, PlayerService


async def make_ready_track(db: Database, video_id: str, duration: float = 0.05):
    theme = await db.create_theme("2026-07-06", "test theme")
    track = await db.add_track(
        video_id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        channel="#music",
        sender="alice",
        # "posted just now": a fixed timestamp here aged past the player's
        # 30-minute live window mid-session once and failed half the suite.
        mesh_ts=time.time(),
        source="mesh",
        theme_id=theme["id"],
    )
    await db.update_track_metadata(track["id"], title=video_id, duration=duration)
    await db.set_cache_status(track["id"], "ready", f"/cache/{video_id}.opus")
    return await db.track_by_id(track["id"])


def make_player(db, bus, **config_overrides) -> PlayerService:
    config = PlayerConfig(**config_overrides)
    return PlayerService(config, db, bus, backend=NullBackend())


def make_embed_player(db, bus, **config_overrides) -> PlayerService:
    """A player whose backend streams in the browser — the public-hosting mode
    where tracks are playable by video id without a downloaded file."""
    config = PlayerConfig(**config_overrides)
    return PlayerService(config, db, bus, backend=EmbedBackend())


async def make_pending_track(db: Database, video_id: str):
    """A channel track that never got a cached file or metadata — the state
    most tracks sit in on the datacenter-hosted embed instance (oEmbed
    throttled). Streamable by id all the same."""
    theme = await db.create_theme("2026-07-06", "test theme")
    track = await db.add_track(
        video_id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        channel="#music",
        sender="alice",
        mesh_ts=time.time(),
        source="mesh",
        theme_id=theme["id"],
    )
    return await db.track_by_id(track["id"])


async def test_idle_live_autoplays(db, bus):
    player = make_player(db, bus)
    track = await make_ready_track(db, "aaaaaaaaaaa")
    await player.on_track_ready(track)
    assert player.status == "playing"
    assert player.current["id"] == track["id"]


async def test_busy_enqueues_never_interrupts(db, bus):
    player = make_player(db, bus)
    first = await make_ready_track(db, "aaaaaaaaaaa", duration=60)
    second = await make_ready_track(db, "bbbbbbbbbbb")
    await player.on_track_ready(first)
    await player.on_track_ready(second)
    assert player.current["id"] == first["id"]  # not interrupted
    assert [t["id"] for t in player.queue] == [second["id"]]


async def test_autoplay_disabled_enqueues(db, bus):
    player = make_player(db, bus, live_autoplay=False)
    track = await make_ready_track(db, "aaaaaaaaaaa")
    await player.on_track_ready(track)
    assert player.status == "idle"
    assert len(player.queue) == 1


async def test_position_advances_and_pauses(db, bus):
    player = make_player(db, bus)
    track = await make_ready_track(db, "aaaaaaaaaaa", duration=60)
    await player.on_track_ready(track)
    await asyncio.sleep(0.05)
    assert 0 < player.position() < 1
    await player.toggle_pause()
    frozen = player.position()
    await asyncio.sleep(0.05)
    assert player.position() == frozen  # clock stops while paused
    assert player.state()["position"] == round(frozen, 1)


async def test_seek_moves_and_clamps(db, bus):
    player = make_player(db, bus)
    track = await make_ready_track(db, "aaaaaaaaaaa", duration=60)
    await player.on_track_ready(track)
    await player.seek(30)
    assert 30 <= player.position() < 31
    await player.seek(9999)
    assert player.position() == 60  # clamped to the track duration
    await player.seek(-5)
    assert player.position() < 1


async def test_play_day_tracks_the_day(db, bus):
    player = make_player(db, bus)
    await make_ready_track(db, "aaaaaaaaaaa", duration=0.02)
    await player.play_day("2026-07-06")
    assert player.mode == "archive"
    assert player.state()["day"] == "2026-07-06"
    await asyncio.sleep(0.1)  # replay ends → back to live, day cleared
    assert player.status == "idle"
    assert player.state()["day"] is None


async def test_cue_day_embed_includes_uncached_tracks(db, bus):
    """Embed hosting streams by video id, so a fresh visitor's cued day is the
    whole playlist — not just the few tracks that got oEmbed metadata."""
    ready = await make_ready_track(db, "aaaaaaaaaaa")
    pending = await make_pending_track(db, "bbbbbbbbbbb")
    player = make_embed_player(db, bus)
    assert await player.cue_day("2026-07-06") is True
    seen = [player.current["id"]] + [t["id"] for t in player.queue]
    assert set(seen) == {ready["id"], pending["id"]}  # all songs, cached or not


async def test_cue_day_appliance_needs_cached_audio(db, bus):
    """The appliance plays local files, so an uncached track can't be cued —
    the readiness gate stays for non-embed players."""
    await make_ready_track(db, "aaaaaaaaaaa")
    await make_pending_track(db, "bbbbbbbbbbb")
    player = make_player(db, bus)  # NullBackend -> not embed
    assert await player.cue_day("2026-07-06") is True
    seen = [player.current["id"]] + [t["id"] for t in player.queue]
    assert len(seen) == 1  # only the ready one


async def test_cue_day_embed_skips_failed(db, bus):
    """A row marked 'failed' (deleted/unavailable video) is still dropped in
    embed mode — the IFrame can't play it."""
    ready = await make_ready_track(db, "aaaaaaaaaaa")
    failed = await make_pending_track(db, "bbbbbbbbbbb")
    await db.set_cache_status(failed["id"], "failed")
    player = make_embed_player(db, bus)
    assert await player.cue_day("2026-07-06") is True
    seen = [player.current["id"]] + [t["id"] for t in player.queue]
    assert set(seen) == {ready["id"]}


async def test_shuffle_queue_reorders_but_keeps_tracks(db, bus):
    player = make_player(db, bus)
    tracks = [await make_ready_track(db, "vid%08d" % i) for i in range(8)]
    player.current = tracks[0]
    player.queue = tracks[1:]
    original = [t["id"] for t in player.queue]
    random.seed(1234)
    await player.shuffle_queue()
    shuffled = [t["id"] for t in player.queue]
    assert sorted(shuffled) == sorted(original)   # same songs, nothing lost
    assert shuffled != original                   # actually reordered
    assert player.current["id"] == tracks[0]["id"]  # now-playing untouched


async def test_shuffle_queue_noop_when_too_small(db, bus):
    player = make_player(db, bus)
    t = await make_ready_track(db, "aaaaaaaaaaa")
    player.queue = [t]
    await player.shuffle_queue()
    assert [x["id"] for x in player.queue] == [t["id"]]


async def test_seek_without_track_is_noop(db, bus):
    player = make_player(db, bus)
    await player.seek(30)
    assert player.position() == 0


async def test_track_end_advances_queue(db, bus):
    player = make_player(db, bus)
    first = await make_ready_track(db, "aaaaaaaaaaa", duration=0.02)
    second = await make_ready_track(db, "bbbbbbbbbbb", duration=60)
    await player.on_track_ready(first)
    await player.on_track_ready(second)
    await asyncio.sleep(0.1)  # let the NullBackend timer fire and advance
    assert player.current["id"] == second["id"]
    assert player.queue == []


async def test_track_end_to_idle_records_completion(db, bus):
    player = make_player(db, bus)
    track = await make_ready_track(db, "aaaaaaaaaaa", duration=0.02)
    await player.on_track_ready(track)
    await asyncio.sleep(0.1)
    assert player.status == "idle"
    plays = await db._fetchall("SELECT * FROM plays")
    assert len(plays) == 1
    assert plays[0]["completed"] == 1


async def test_skip(db, bus):
    player = make_player(db, bus)
    first = await make_ready_track(db, "aaaaaaaaaaa", duration=60)
    second = await make_ready_track(db, "bbbbbbbbbbb", duration=60)
    await player.on_track_ready(first)
    await player.on_track_ready(second)
    await player.skip()
    assert player.current["id"] == second["id"]
    plays = await db._fetchall("SELECT * FROM plays WHERE track_id=?", (first["id"],))
    assert plays[0]["completed"] == 0  # skipped, not completed


async def test_quiet_hours_suppresses_autoplay(db, bus):
    player = make_player(db, bus, quiet_hours="00:00-23:59")
    track = await make_ready_track(db, "aaaaaaaaaaa")
    await player.on_track_ready(track)
    assert player.status == "idle"
    assert len(player.queue) == 1


def test_quiet_hours_overnight_span(db, bus):
    player = make_player(db, bus, quiet_hours="22:00-08:00")
    assert player.in_quiet_hours(datetime(2026, 7, 6, 23, 0))
    assert player.in_quiet_hours(datetime(2026, 7, 6, 7, 0))
    assert not player.in_quiet_hours(datetime(2026, 7, 6, 12, 0))


def test_quiet_hours_bad_spec_is_off(db, bus):
    player = make_player(db, bus, quiet_hours="whenever")
    assert not player.in_quiet_hours()


async def test_play_day_archive_mode(db, bus):
    player = make_player(db, bus)
    await make_ready_track(db, "aaaaaaaaaaa", duration=60)
    await make_ready_track(db, "bbbbbbbbbbb", duration=60)
    await player.play_day("2026-07-06")
    assert player.mode == "archive"
    assert player.status == "playing"
    assert len(player.queue) == 1


async def test_state_published(db, bus):
    sub = bus.subscribe(PLAYER_STATE)
    player = make_player(db, bus)
    track = await make_ready_track(db, "aaaaaaaaaaa")
    await player.on_track_ready(track)
    _, state = await asyncio.wait_for(sub.get(), 1)
    assert state["status"] == "playing"
    assert state["current"]["video_id"] == "aaaaaaaaaaa"


async def test_volume_clamped(db, bus):
    player = make_player(db, bus)
    await player.set_volume(150)
    assert player.volume == 100
    await player.set_volume(-5)
    assert player.volume == 0
