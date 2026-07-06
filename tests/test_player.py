import asyncio
from datetime import datetime

from meshradio.bus import EventBus, PLAYER_STATE
from meshradio.config import PlayerConfig
from meshradio.db import Database
from meshradio.media.player import NullBackend, PlayerService


async def make_ready_track(db: Database, video_id: str, duration: float = 0.05):
    theme = await db.create_theme("2026-07-06", "test theme")
    track = await db.add_track(
        video_id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        channel="#music",
        sender="alice",
        mesh_ts=1_783_443_600.0,
        source="mesh",
        theme_id=theme["id"],
    )
    await db.update_track_metadata(track["id"], title=video_id, duration=duration)
    await db.set_cache_status(track["id"], "ready", f"/cache/{video_id}.opus")
    return await db.track_by_id(track["id"])


def make_player(db, bus, **config_overrides) -> PlayerService:
    config = PlayerConfig(**config_overrides)
    return PlayerService(config, db, bus, backend=NullBackend())


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
