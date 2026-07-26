"""Archive station: keep-playing filler drawn from channel history.

Unlike radio mode (YouTube Mix, needs a residential IP), the archive station is
pure local SQL, so it's the one that keeps the public embed host from dropping
into silence when a day's playlist ends.
"""

import asyncio
import time

from meshradio.config import PlayerConfig
from meshradio.db import Database
from meshradio.media.player import EmbedBackend, NullBackend, PlayerService

from .test_player import make_embed_player, make_pending_track, make_ready_track


async def _channel_day(db: Database, date: str, video_ids: list[str]) -> None:
    """Seed a day's playlist with ready channel tracks."""
    theme = await db.create_theme(date, f"theme {date}")
    for vid in video_ids:
        track = await db.add_track(
            video_id=vid,
            url=f"https://www.youtube.com/watch?v={vid}",
            channel="#music",
            sender="alice",
            mesh_ts=time.time(),
            source="mesh",
            theme_id=theme["id"],
        )
        await db.set_cache_status(track["id"], "ready", f"/cache/{vid}.opus")


async def test_random_channel_tracks_excludes_radio_and_themeless(db):
    await _channel_day(db, "2026-07-06", ["aaaaaaaaaaa", "bbbbbbbbbbb"])
    # A radio row (themeless) must never be offered as archive filler.
    await db.add_track(
        video_id="ccccccccccc", url="u", channel="radio", sender="radio",
        mesh_ts=time.time(), source="radio", theme_id=None,
    )
    got = await db.random_channel_tracks(limit=10)
    vids = {t["video_id"] for t in got}
    assert vids == {"aaaaaaaaaaa", "bbbbbbbbbbb"}


async def test_random_channel_tracks_honors_exclude(db):
    await _channel_day(db, "2026-07-06", ["aaaaaaaaaaa", "bbbbbbbbbbb"])
    got = await db.random_channel_tracks(limit=10, exclude_video_ids=["aaaaaaaaaaa"])
    assert [t["video_id"] for t in got] == ["bbbbbbbbbbb"]


async def test_ready_only_filters_unfetched(db):
    theme = await db.create_theme("2026-07-06", "t")
    pend = await db.add_track(
        video_id="aaaaaaaaaaa", url="u", channel="#music", sender="a",
        mesh_ts=time.time(), source="mesh", theme_id=theme["id"],
    )  # stays 'pending' — no file
    # ready_only (appliance rule) skips it; the embed rule (default) keeps it.
    assert await db.random_channel_tracks(ready_only=True) == []
    assert len(await db.random_channel_tracks(ready_only=False)) == 1
    assert pend is not None


async def test_archive_station_refills_at_end_of_queue(db, bus):
    """The last song of a day rolls straight into archive filler — no silence."""
    await _channel_day(db, "2026-07-06", ["aaaaaaaaaaa"])
    await _channel_day(db, "2026-07-05", ["bbbbbbbbbbb", "ccccccccccc"])
    player = PlayerService(PlayerConfig(), db, bus, backend=NullBackend())
    await player.play_day("2026-07-06")            # one-song day
    assert player.status == "playing"
    assert player.queue == []

    assert await player.start_station("archive") is True
    await player.skip()                            # finish the only real song
    # Still playing, now on filler pulled from the archive.
    assert player.status == "playing"
    assert player.current["video_id"] in {"bbbbbbbbbbb", "ccccccccccc"}
    assert player.state()["station"] == "archive"


async def test_archive_station_from_idle_plays_now(db, bus):
    await _channel_day(db, "2026-07-06", ["aaaaaaaaaaa"])
    player = PlayerService(PlayerConfig(), db, bus, backend=NullBackend())
    assert player.status == "idle"
    assert await player.start_station("archive") is True
    assert player.status == "playing"
    assert player.current["video_id"] == "aaaaaaaaaaa"


async def test_archive_station_never_repeats_current_or_queued(db, bus):
    await _channel_day(db, "2026-07-06", ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"])
    player = PlayerService(PlayerConfig(), db, bus, backend=NullBackend())
    rows = await db.random_channel_tracks(limit=1)
    player.current = rows[0]                       # some real archived song
    player.status = "playing"
    n = await player._extend_from_archive()
    filler_vids = {t["video_id"] for t in player.queue}
    assert player.current["video_id"] not in filler_vids   # not re-queued behind itself
    assert n == len(player.queue)


async def test_archive_station_works_in_embed_mode(db, bus):
    """The whole point: embed hosting (no cached files) can still keep playing."""
    theme = await db.create_theme("2026-07-06", "t")
    for vid in ["aaaaaaaaaaa", "bbbbbbbbbbb"]:
        await db.add_track(
            video_id=vid, url="u", channel="#music", sender="a",
            mesh_ts=time.time(), source="mesh", theme_id=theme["id"],
        )  # pending, no file — the normal embed state
    player = make_embed_player(db, bus)
    assert await player.start_station("archive") is True
    assert player.status == "playing"              # streamable by id despite no cache


async def test_archive_filler_yields_to_fresh_channel_post(db, bus):
    """A song posted live still jumps ahead of archive filler already queued."""
    await _channel_day(db, "2026-07-06", ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"])
    player = PlayerService(PlayerConfig(), db, bus, backend=NullBackend())
    player.current = await make_ready_track(db, "ddddddddddd", duration=60)
    player.status = "playing"
    player.station = "archive"
    await player._extend_from_archive()
    assert player.queue and all(t.get("filler") for t in player.queue)

    fresh = await make_ready_track(db, "eeeeeeeeeee", duration=60)
    await player.on_track_ready(fresh)             # a brand-new channel post
    assert player.queue[0]["id"] == fresh["id"]    # ahead of the filler
    assert not player.queue[0].get("filler")


async def test_station_switch_is_exclusive(db, bus):
    """Turning on the archive station clears a radio station and vice versa."""
    player = PlayerService(PlayerConfig(), db, bus, backend=NullBackend())
    await make_ready_track(db, "aaaaaaaaaaa")
    player.station = "radio"
    await player.start_station("archive")
    assert player.station == "archive"
