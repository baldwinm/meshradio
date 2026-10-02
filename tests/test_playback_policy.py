"""Queue priority, backfill freshness, and speaker election."""

import time

from meshradio.media.player import _is_filler
from meshradio.web.server import SpeakerRegistry

from .helpers import make_player, make_ready_track


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


async def test_remove_from_queue(db, bus):
    player = make_player(db, bus)
    player.queue = [
        {"id": 1, "video_id": "a", "source": "corescope"},
        {"id": 2, "video_id": "b", "source": "corescope"},
    ]
    assert await player.remove_from_queue(0, 1) is True
    assert [t["id"] for t in player.queue] == [2]


async def test_remove_with_stale_index_noops(db, bus):
    player = make_player(db, bus)
    player.queue = [{"id": 1, "video_id": "a", "source": "corescope"}]
    # Client rendered an older queue: index 0 now holds a different track.
    assert await player.remove_from_queue(0, 999) is False
    assert await player.remove_from_queue(5, 1) is False
    assert len(player.queue) == 1


async def test_move_to_front(db, bus):
    player = make_player(db, bus)
    player.queue = [
        {"id": 1, "video_id": "a", "source": "corescope"},
        {"id": 2, "video_id": "b", "source": "corescope"},
        {"id": 3, "video_id": "c", "source": "radio"},
    ]
    assert await player.move_to_front(2, 3) is True
    assert [t["id"] for t in player.queue] == [3, 1, 2]


async def test_clear_queue_also_stops_station(db, bus):
    player = make_player(db, bus)
    player.queue = [{"id": 1, "video_id": "a", "source": "radio"}]
    player.station = "radio"
    await player.clear_queue()
    assert player.queue == []
    assert player.station is None


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


# -- queue bounds ---------------------------------------------------------------

async def test_queue_refuses_a_song_already_queued_or_playing(db, bus):
    """A repost, or a double press on "+ queue", must not list a song twice."""
    a = await make_ready_track(db, "aaaaaaaaaaa", duration=60)
    b = await make_ready_track(db, "bbbbbbbbbbb", duration=60)
    player = make_player(db, bus)
    await player.play_track(a)
    assert await player.enqueue_track_id(b["id"]) is True
    for _ in range(3):
        assert await player.enqueue_track_id(b["id"]) is False
    assert [t["id"] for t in player.queue] == [b["id"]]
    assert await player.enqueue_track_id(a["id"]) is False      # it's playing
    assert len(player.queue) == 1


async def test_queue_has_a_ceiling_that_a_channel_post_still_crosses(db, bus):
    """Pressing "+ queue" a thousand times used to make a thousand entries,
    and every state push and snapshot grew with them. Filler stops at the
    ceiling; a fresh channel post displaces the last piece of filler."""
    player = make_player(db, bus, max_queue=3)
    tracks = [await make_ready_track(db, f"{i:011d}", duration=60) for i in range(6)]
    await player.play_track(tracks[0])
    for t in tracks[1:4]:
        assert player._enqueue(dict(t, filler=True))
    assert not player._enqueue(dict(tracks[4], filler=True))     # full: filler is dropped
    assert player._enqueue(tracks[5])                             # a post displaces filler
    assert [t["id"] for t in player.queue] == [tracks[5]["id"], tracks[1]["id"], tracks[2]["id"]]
    assert player._enqueue(tracks[4])                             # the last filler, again
    assert [t["id"] for t in player.queue] == [tracks[5]["id"], tracks[4]["id"], tracks[1]["id"]]
    assert player._enqueue(tracks[3])                             # and the one left
    assert not any(_is_filler(t) for t in player.queue)
    # Nothing displaces a channel post: the queue is all posts now, so the
    # next one is refused rather than pushing somebody's pick out.
    extra = await make_ready_track(db, "eeeeeeeeeee", duration=60)
    assert not player._enqueue(extra)
    assert [t["id"] for t in player.queue] == [tracks[5]["id"], tracks[4]["id"], tracks[3]["id"]]


async def test_restore_bounds_and_deduplicates_the_queue(db, bus):
    """A snapshot from before the ceiling (or a tampered row) can't bring an
    unbounded or duplicated queue back."""
    a = await make_ready_track(db, "aaaaaaaaaaa", duration=60)
    b = await make_ready_track(db, "bbbbbbbbbbb", duration=60)
    player = make_player(db, bus, max_queue=5)
    await player.restore({
        "status": "paused", "current_track_id": a["id"], "position": 0,
        "queue_track_ids": [a["id"], b["id"]] * 50,
    })
    assert player.current["id"] == a["id"] and player.status == "paused"
    assert [t["id"] for t in player.queue] == [b["id"]]
