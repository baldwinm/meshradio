"""Song lengths on the embed host: looked up from YouTube so a queue of songs
nobody has played yet still adds up to a total."""

import asyncio

from meshradio.bus import DURATION_WANTED, TRACK_DURATION
from meshradio.media import metadata
from meshradio.media.durations import DurationService

from .helpers import (
    client_for,
    embed_app,
    make_embed_player,
    make_player,
    make_ready_on,
    page_app,
    share,
)


def test_watch_page_lengths_are_read_from_either_marking():
    assert metadata.parse_watch_duration('..."lengthSeconds":"213",...') == 213.0
    page = '<meta itemprop="duration" content="PT1H2M5S">'
    assert metadata.parse_watch_duration(page) == 3725.0
    assert metadata.parse_watch_duration('<meta itemprop="duration" content="PT4M">') == 240.0
    assert metadata.parse_watch_duration('"lengthSeconds":"0"') is None
    assert metadata.parse_watch_duration("<html>consent wall</html>") is None


async def test_a_looked_up_length_fills_every_share_and_is_announced(db, bus, monkeypatch):
    calls = []

    async def fake_fetch(video_id, client=None):
        calls.append(video_id)
        return 187.0

    monkeypatch.setattr(metadata, "fetch_duration", fake_fetch)
    first = await make_ready_on(db, "aaaaaaaaaaa", "2026-08-01", duration=None)
    repost = await share(db, "2026-08-02", "aaaaaaaaaaa", "bob")
    await db.db.execute(
        "UPDATE tracks SET duration=NULL, cache_status='ready' WHERE id=?", (repost["id"],)
    )
    sub = bus.subscribe(TRACK_DURATION)
    service = DurationService(db, bus)

    assert await db.videos_missing_duration() == ["aaaaaaaaaaa"]
    assert await service.resolve("aaaaaaaaaaa") is True
    rows = await db.tracks_for_video("aaaaaaaaaaa")
    assert len(rows) == 2 and all(r["duration"] == 187.0 for r in rows)
    assert (await db.track_by_id(first["id"]))["duration"] == 187.0
    announced = {"video_id": "aaaaaaaaaaa", "duration": 187.0}
    assert sub.queue.get_nowait() == (TRACK_DURATION, announced)
    assert await db.videos_missing_duration() == []

    # Already known: no second request, but still announced for stale players.
    assert await service.resolve("aaaaaaaaaaa") is False
    assert calls == ["aaaaaaaaaaa"]
    assert sub.queue.get_nowait()[1]["duration"] == 187.0


async def test_a_video_youtube_will_not_describe_is_given_up_on(db, bus, monkeypatch):
    async def no_length(video_id, client=None):
        return None

    monkeypatch.setattr(metadata, "fetch_duration", no_length)
    await make_ready_on(db, "aaaaaaaaaaa", "2026-08-01", duration=None)
    service = DurationService(db, bus)
    for _ in range(DurationService.MAX_ATTEMPTS):
        service._urgent.append("aaaaaaaaaaa")
        assert service._next() == "aaaaaaaaaaa"
        await service.resolve("aaaaaaaaaaa")
    service._urgent.append("aaaaaaaaaaa")
    assert service._next() is None
    assert (await db.tracks_for_video("aaaaaaaaaaa"))[0]["duration"] is None


async def test_an_embed_player_asks_for_its_missing_lengths_once(db, bus):
    await make_ready_on(db, "aaaaaaaaaaa", "2026-08-01", duration=None)
    await make_ready_on(db, "bbbbbbbbbbb", "2026-08-01", duration=None)
    await make_ready_on(db, "ccccccccccc", "2026-08-01", duration=200)
    sub = bus.subscribe(DURATION_WANTED)
    player = make_embed_player(db, bus)
    await player.play_day("2026-08-01")
    topic, payload = sub.queue.get_nowait()
    assert payload == {"video_ids": ["aaaaaaaaaaa", "bbbbbbbbbbb"]}
    player.publish_state()
    assert sub.queue.empty()                                  # not asked again

    # The appliance downloads its songs and learns lengths that way.
    appliance = make_player(db, bus)
    await appliance.play_day("2026-08-01")
    assert sub.queue.empty()


async def test_queues_fill_in_live_as_lengths_arrive(db, bus, monkeypatch):
    lengths = {"aaaaaaaaaaa": 200.0, "bbbbbbbbbbb": 3000.0, "ccccccccccc": 725.0}

    async def fake_fetch(video_id, client=None):
        return lengths[video_id]

    monkeypatch.setattr(metadata, "fetch_duration", fake_fetch)
    for video_id in lengths:
        await make_ready_on(db, video_id, "2026-08-01", duration=None)
    player = make_embed_player(db, bus)
    service = DurationService(db, bus, pace_s=0)
    player.start()
    service.start()
    try:
        await asyncio.sleep(0)
        await player.play_day("2026-08-01")
        for _ in range(200):
            if all(t["duration"] for t in player.state()["queue"]):
                break
            await asyncio.sleep(0.01)
        assert [t["duration"] for t in player.state()["queue"]] == [3000.0, 725.0]
        assert player.state()["current"]["duration"] == 200.0
    finally:
        await service.stop()
        await player.stop()


async def test_a_browser_report_reaches_every_session_holding_the_song(db, bus):
    """One visitor's tab measures a song; another visitor with the same song
    queued gets the length too, and so do its reposts."""
    await make_ready_on(db, "aaaaaaaaaaa", "2026-08-01", duration=None)
    await make_ready_on(db, "bbbbbbbbbbb", "2026-08-01", duration=None)
    mine, theirs = make_embed_player(db, bus), make_embed_player(db, bus)
    await mine.play_day("2026-08-01")
    await theirs.play_day("2026-08-01")
    theirs.start()
    try:
        await asyncio.sleep(0)
        queued = mine.queue[0]
        await mine.report_duration(queued["id"], 241)
        for _ in range(100):
            if theirs.queue[0]["duration"]:
                break
            await asyncio.sleep(0.01)
        assert mine.queue[0]["duration"] == 241
        assert theirs.queue[0]["duration"] == 241
    finally:
        await theirs.stop()


async def test_only_the_hosted_site_measures_lengths_in_the_browser(db, bus):
    async with client_for(embed_app(db, bus)) as client:
        assert "/static/js/lengths.js" in (await client.get("/")).text
    async with client_for(page_app(db, bus)) as client:
        assert "/static/js/lengths.js" not in (await client.get("/")).text
