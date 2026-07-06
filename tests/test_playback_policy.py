"""Queue priority, backfill freshness, and speaker election."""

import time

from meshradio.config import PlayerConfig
from meshradio.media.player import NullBackend, PlayerService
from meshradio.web.server import SpeakerRegistry

from .test_player import make_ready_track


def make_player(db, bus, **overrides) -> PlayerService:
    return PlayerService(PlayerConfig(**overrides), db, bus, backend=NullBackend())


async def test_stale_backfill_track_does_not_autoplay(db, bus):
    player = make_player(db, bus, live_window_s=1800)
    track = await make_ready_track(db, "aaaaaaaaaaa")
    track = dict(track, mesh_ts=time.time() - 86400)  # posted yesterday: backfill
    await player.on_track_ready(track)
    assert player.status == "idle"
    assert player.queue == []  # archive-only; not queued either


async def test_fresh_track_autoplays(db, bus):
    player = make_player(db, bus, live_window_s=1800)
    track = await make_ready_track(db, "aaaaaaaaaaa")
    track = dict(track, mesh_ts=time.time() - 60)  # posted a minute ago
    await player.on_track_ready(track)
    assert player.status == "playing"


async def test_channel_tracks_jump_ahead_of_radio_filler(db, bus):
    player = make_player(db, bus)
    current = await make_ready_track(db, "aaaaaaaaaaa", duration=60)
    await player.on_track_ready(dict(current, mesh_ts=time.time()))
    player.queue = [
        {"id": 91, "video_id": "r1", "source": "radio"},
        {"id": 92, "video_id": "r2", "source": "radio"},
    ]
    fresh = await make_ready_track(db, "bbbbbbbbbbb", duration=60)
    await player.on_track_ready(dict(fresh, mesh_ts=time.time()))
    assert [t["video_id"] for t in player.queue] == ["bbbbbbbbbbb", "r1", "r2"]


async def test_radio_tracks_append_at_end(db, bus):
    player = make_player(db, bus)
    current = await make_ready_track(db, "aaaaaaaaaaa", duration=60)
    await player.on_track_ready(dict(current, mesh_ts=time.time()))
    radio_track = await make_ready_track(db, "ccccccccccc", duration=60)
    await player.on_track_ready(dict(radio_track, source="radio"))
    channel_track = await make_ready_track(db, "bbbbbbbbbbb", duration=60)
    await player.on_track_ready(dict(channel_track, mesh_ts=time.time()))
    assert [t["video_id"] for t in player.queue] == ["bbbbbbbbbbb", "ccccccccccc"]


def test_speaker_registry_newest_wins():
    reg = SpeakerRegistry()
    reg.join("a")
    assert reg.is_speaker("a")
    reg.join("b")
    assert reg.is_speaker("b")
    assert not reg.is_speaker("a")


def test_speaker_registry_claim():
    reg = SpeakerRegistry()
    reg.join("a")
    reg.join("b")
    reg.claim("a")
    assert reg.is_speaker("a")
    assert not reg.is_speaker("b")


def test_speaker_registry_leave_promotes_previous():
    reg = SpeakerRegistry()
    reg.join("a")
    reg.join("b")
    reg.leave("b")
    assert reg.is_speaker("a")
    reg.leave("a")
    assert not reg.is_speaker("a")
    assert reg.clients() == []


def test_speaker_registry_leave_unknown_is_noop():
    reg = SpeakerRegistry()
    reg.join("a")
    reg.leave("ghost")
    assert reg.is_speaker("a")
