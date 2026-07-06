import asyncio

from meshradio.bus import TRACK_DISCOVERED
from meshradio.config import CacheConfig, PlayerConfig
from meshradio.db import Database
from meshradio.media.player import NullBackend, PlayerService, WebBackend
from meshradio.media.radio import RadioService

from .test_player import make_ready_track

SEED = "aaaaaaaaaaa"


class FakeRadio(RadioService):
    """RadioService with the yt-dlp subprocess swapped for canned entries."""

    def __init__(self, db, bus, entries):
        super().__init__(CacheConfig(), db, bus)
        self.entries = entries
        self.fetched_seeds = []

    async def _fetch_mix(self, video_id):
        self.fetched_seeds.append(video_id)
        return self.entries


async def test_radio_extend_inserts_and_publishes(db, bus):
    seed = await make_ready_track(db, SEED)
    radio = FakeRadio(db, bus, [
        {"id": SEED, "title": "the seed itself"},          # skipped
        {"id": "bbbbbbbbbbb", "title": "Song B", "uploader": "Artist B"},
        {"id": "ccccccccccc", "title": "Song C", "uploader": "Artist C"},
    ])
    sub = bus.subscribe(TRACK_DISCOVERED)
    inserted = await radio.extend(seed, limit=10)
    assert inserted == 2
    _, payload = await asyncio.wait_for(sub.get(), 1)
    track = payload["track"]
    assert track["source"] == "radio"
    assert track["theme_id"] is None
    assert track["title"] == "Song B"


async def test_radio_tracks_hidden_from_archive(db, bus):
    seed = await make_ready_track(db, SEED)
    radio = FakeRadio(db, bus, [{"id": "bbbbbbbbbbb", "title": "Song B"}])
    await radio.extend(seed)
    days = await db.archive_days()
    # only the seed's theme day, and it counts only the channel track
    assert len(days) == 1
    assert days[0]["tracks"] == 1


async def test_radio_same_day_restart_dedupes(db, bus):
    seed = await make_ready_track(db, SEED)
    entries = [{"id": "bbbbbbbbbbb", "title": "Song B"}]
    radio = FakeRadio(db, bus, entries)
    assert await radio.extend(seed) == 1
    assert await radio.extend(seed) == 0  # same seed, same day -> no dupes


async def test_start_radio_seeds_from_current(db, bus):
    player = PlayerService(PlayerConfig(), db, bus, backend=NullBackend())
    seed = await make_ready_track(db, SEED, duration=60)
    player.radio = FakeRadio(db, bus, [{"id": "bbbbbbbbbbb", "title": "Song B"}])
    await player.on_track_ready(seed)
    assert await player.start_radio() is True
    assert player.radio_active
    assert player.radio.fetched_seeds == [SEED]


async def test_start_radio_seeds_from_last_played_when_idle(db, bus):
    player = PlayerService(PlayerConfig(), db, bus, backend=NullBackend())
    seed = await make_ready_track(db, SEED, duration=0.01)
    player.radio = FakeRadio(db, bus, [])
    await player.on_track_ready(seed)
    await asyncio.sleep(0.1)  # track finishes; player idle
    assert player.status == "idle"
    assert await player.start_radio() is True
    assert player.radio.fetched_seeds == [SEED]


async def test_start_radio_without_history_fails(db, bus):
    player = PlayerService(PlayerConfig(), db, bus, backend=NullBackend())
    player.radio = FakeRadio(db, bus, [])
    assert await player.start_radio() is False


async def test_stop_radio_keeps_queued_tracks(db, bus):
    """Radio off only stops NEW mix fetches; already-queued radio tracks stay."""
    player = PlayerService(PlayerConfig(), db, bus, backend=NullBackend())
    seed = await make_ready_track(db, SEED, duration=60)
    await player.on_track_ready(seed)
    player.queue.append({"id": 99, "video_id": "x", "source": "radio"})
    channel_track = await make_ready_track(db, "ddddddddddd", duration=60)
    player.queue.append(channel_track)
    player.radio_active = True
    await player.stop_radio()
    assert not player.radio_active
    assert [t["id"] for t in player.queue] == [99, channel_track["id"]]


async def test_late_radio_track_still_enqueued_after_stop(db, bus):
    """A radio track still downloading when the user hits stop was already
    requested — it joins the queue when it finishes; only NEW fetches stop."""
    player = PlayerService(PlayerConfig(), db, bus, backend=NullBackend())
    seed = await make_ready_track(db, SEED, duration=60)
    await player.on_track_ready(seed)

    player.radio_active = True
    await player.stop_radio()
    radio_track = dict(await make_ready_track(db, "bbbbbbbbbbb", duration=60))
    radio_track["source"] = "radio"
    await player.on_track_ready(radio_track)  # late arrival from the cacher
    assert [t["id"] for t in player.queue] == [radio_track["id"]]


async def test_stopped_radio_never_extends(db, bus):
    """With radio off, draining the queue must not trigger new mix fetches."""
    player = PlayerService(PlayerConfig(), db, bus, backend=NullBackend())
    player.radio = FakeRadio(db, bus, [{"id": "bbbbbbbbbbb", "title": "Song B"}])
    seed = await make_ready_track(db, SEED, duration=0.02)
    await player.on_track_ready(seed)
    await asyncio.sleep(0.1)  # track ends with an empty queue
    assert player.status == "idle"
    assert player.radio.fetched_seeds == []


async def test_web_backend_waits_for_browser_signal(db, bus):
    player = PlayerService(PlayerConfig(), db, bus, backend=WebBackend())
    track = await make_ready_track(db, SEED, duration=0.01)
    await player.on_track_ready(track)
    await asyncio.sleep(0.05)
    assert player.status == "playing"  # no internal timer; waits for the browser

    # Wrong track id: no advance (stale tab / duplicate signal).
    assert await player.notify_ended(track["id"] + 999) is False
    assert player.status == "playing"

    assert await player.notify_ended(track["id"]) is True
    assert player.status == "idle"
    # Second signal for the same track no-ops.
    assert await player.notify_ended(track["id"]) is False


async def test_state_flags(db, bus):
    player = PlayerService(PlayerConfig(), db, bus, backend=WebBackend())
    state = player.state()
    assert state["web_audio"] is True
    assert state["radio"] is False
