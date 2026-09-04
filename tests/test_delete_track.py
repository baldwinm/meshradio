"""Manual song removal: db.delete_track and the --delete-track CLI.

A song posted before the day's theme was set (or posted to the wrong day)
can't be taken back on the channel — the link is still there, and a repost is
a dedupe no-op — so the archive needs an out-of-band way to drop it and to
remember that it did.
"""

from argparse import Namespace
from datetime import datetime
from zoneinfo import ZoneInfo

from meshradio.app import _run_delete_track
from meshradio.bus import EventBus
from meshradio.config import Config
from meshradio.db import Database
from meshradio.ingest.parse import untitled_theme
from meshradio.ingest.service import IngestService

VID = "dQw4w9WgXcQ"
OTHER = "aaaaaaaaaaa"
DAY = "2026-07-06"


def _args(video, date=None):
    return Namespace(delete_track=video, track_date=date)


def _config(tmp_path, tz="America/Chicago"):
    config = Config(data_dir=tmp_path)
    config.player.timezone = tz
    return config


async def _add_track(db, theme_id, video_id=VID, sender="alice", ts=1_751_800_000.0):
    return await db.add_track(
        video_id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        channel="#music",
        sender=sender,
        mesh_ts=ts,
        source="mesh",
        theme_id=theme_id,
    )


# -- db layer ----------------------------------------------------------------


async def test_delete_removes_the_song_and_its_plays(db: Database):
    theme = await db.create_theme(DAY, "water", locked=True)
    track = await _add_track(db, theme["id"])
    await db.record_play(track["id"], output="web")

    deleted = await db.delete_track(track["id"])

    assert deleted["video_id"] == VID
    assert await db.track_by_id(track["id"]) is None
    assert await db.tracks_for_theme(theme["id"]) == []
    assert (await db.play_totals())["plays"] == 0


async def test_deleted_song_does_not_come_back_on_re_ingest(db: Database, bus: EventBus):
    """The tombstone is the point: the channel message is still out there, and
    any re-backfill replays it."""
    ingest = IngestService(db, bus, channel="#music")
    ts = datetime(2026, 7, 6, 15, 0, tzinfo=ZoneInfo("America/Chicago")).timestamp()
    await ingest.handle_message(
        sender="alice", text=f"https://youtu.be/{VID}", ts=ts, source="corescope"
    )
    track = (await db.tracks_for_day(DAY))[0]
    await db.delete_track(track["id"])

    inserted = await ingest.handle_message(
        sender="alice", text=f"https://youtu.be/{VID}", ts=ts + 3600, source="corescope"
    )

    assert inserted == 0
    assert await db.tracks_for_day(DAY) == []


async def test_tombstone_outlives_the_theme_row_it_was_filed_under(db: Database, bus: EventBus):
    """The placeholder holding the song is cleaned up with it, so the day's
    next link builds a brand-new theme row — the tombstone is keyed on the day
    for exactly this reason."""
    theme = await db.create_theme(DAY, untitled_theme(DAY))
    track = await _add_track(db, theme["id"])
    await db.delete_track(track["id"])
    assert await db.delete_empty_placeholder(theme["id"]) is True

    ingest = IngestService(db, bus, channel="#music")
    ts = datetime(2026, 7, 6, 18, 0, tzinfo=ZoneInfo("America/Chicago")).timestamp()
    inserted = await ingest.handle_message(
        sender="bob", text=f"look at this {OTHER} https://youtu.be/{VID}",
        ts=ts, source="corescope",
    )

    assert inserted == 0
    assert await db.tracks_for_day(DAY) == []


async def test_tombstone_is_scoped_to_the_day(db: Database, bus: EventBus):
    """Removing a song from one day says nothing about the next: the same song
    is fair game when somebody shares it under another theme."""
    theme = await db.create_theme(DAY, "water", locked=True)
    track = await _add_track(db, theme["id"])
    await db.delete_track(track["id"])

    ingest = IngestService(db, bus, channel="#music")
    ts = datetime(2026, 7, 7, 15, 0, tzinfo=ZoneInfo("America/Chicago")).timestamp()
    inserted = await ingest.handle_message(
        sender="bob", text=f"https://youtu.be/{VID}", ts=ts, source="corescope"
    )

    assert inserted == 1
    assert len(await db.tracks_for_day("2026-07-07")) == 1


async def test_relay_total_survives_a_deletion(db: Database):
    """A receiver one song lighter because of --delete-track is not a wiped
    receiver; counting the tombstone keeps the pusher from re-backfilling the
    whole channel every interval."""
    theme = await db.create_theme(DAY, "water", locked=True)
    track = await _add_track(db, theme["id"])
    await _add_track(db, theme["id"], video_id=OTHER, sender="bob")
    before = await db.relay_track_total()

    await db.delete_track(track["id"])

    assert await db.channel_track_count() == 1     # it really is gone
    assert await db.relay_track_total() == before  # but it's still accounted for


async def test_radio_filler_leaves_no_tombstone(db: Database):
    """Station padding isn't a channel post, so there's nothing to come back."""
    track = await db.add_track(
        video_id=OTHER, url=f"https://www.youtube.com/watch?v={OTHER}",
        channel="radio", sender="radio", mesh_ts=1_751_800_000.0,
        source="radio", theme_id=None,
    )
    before = await db.relay_track_total()

    await db.delete_track(track["id"])

    assert await db.track_by_id(track["id"]) is None
    assert await db.relay_track_total() == before
    cur = await db.db.execute("SELECT COUNT(*) FROM deleted_tracks")
    assert (await cur.fetchone())[0] == 0


async def test_deleting_a_missing_track_is_a_no_op(db: Database):
    assert await db.delete_track(999) is None


async def test_empty_untitled_placeholder_is_cleaned_up(db: Database):
    """The day the song arrived before the theme: nobody named that playlist,
    and with the song gone it would light up a calendar tile for nothing."""
    theme = await db.create_theme(DAY, untitled_theme(DAY))
    track = await _add_track(db, theme["id"])
    await db.delete_track(track["id"])

    assert await db.delete_empty_placeholder(theme["id"]) is True
    assert await db.theme_by_id(theme["id"]) is None
    assert await db.archive_days() == []


async def test_a_real_theme_survives_losing_its_last_song(db: Database):
    theme = await db.create_theme(DAY, "water", set_by="alice", locked=True)
    track = await _add_track(db, theme["id"])
    await db.delete_track(track["id"])

    assert await db.delete_empty_placeholder(theme["id"]) is False
    assert (await db.theme_by_id(theme["id"]))["title"] == "water"


async def test_placeholder_with_songs_left_is_kept(db: Database):
    theme = await db.create_theme(DAY, untitled_theme(DAY))
    track = await _add_track(db, theme["id"])
    await _add_track(db, theme["id"], video_id=OTHER, sender="bob")
    await db.delete_track(track["id"])

    assert await db.delete_empty_placeholder(theme["id"]) is False
    assert len(await db.tracks_for_theme(theme["id"])) == 1


# -- CLI ---------------------------------------------------------------------


async def test_cli_deletes_from_today_by_default(tmp_path, capsys):
    config = _config(tmp_path)
    today = datetime.now(ZoneInfo(config.player.timezone)).strftime("%Y-%m-%d")

    db = Database(config.db_path)
    await db.connect()
    theme = await db.create_theme(today, untitled_theme(today))
    track = await _add_track(db, theme["id"])
    await db.update_track_metadata(track["id"], title="Never Gonna Give You Up")
    await db.close()

    assert await _run_delete_track(config, _args(VID)) == 0

    db = Database(config.db_path)
    await db.connect()
    try:
        assert await db.tracks_for_day(today) == []
        assert await db.latest_theme_for_date(today) is None   # placeholder went too
    finally:
        await db.close()
    out = capsys.readouterr().out
    assert "Never Gonna Give You Up" in out and "shared by alice" in out
    assert "placeholder" in out


async def test_cli_accepts_a_url_and_a_date(tmp_path, capsys):
    config = _config(tmp_path)
    db = Database(config.db_path)
    await db.connect()
    theme = await db.create_theme(DAY, "water", locked=True)
    await _add_track(db, theme["id"])
    await db.close()

    code = await _run_delete_track(
        config, _args(f"https://music.youtube.com/watch?v={VID}&si=xyz", DAY)
    )

    assert code == 0
    db = Database(config.db_path)
    await db.connect()
    try:
        assert await db.tracks_for_day(DAY) == []
        assert (await db.latest_theme_for_date(DAY))["title"] == "water"  # theme stays
    finally:
        await db.close()
    assert VID in capsys.readouterr().out


async def test_cli_deletes_the_cached_audio(tmp_path, capsys):
    config = _config(tmp_path)
    cached = config.cache_dir
    cached.mkdir(parents=True, exist_ok=True)
    audio = cached / f"{VID}.m4a"
    audio.write_bytes(b"not really audio")

    db = Database(config.db_path)
    await db.connect()
    theme = await db.create_theme(DAY, "water", locked=True)
    track = await _add_track(db, theme["id"])
    await db.set_cache_status(track["id"], "ready", cache_path=str(audio))
    await db.close()

    assert await _run_delete_track(config, _args(VID, DAY)) == 0
    assert not audio.exists()
    assert "cached audio removed" in capsys.readouterr().out


async def test_cli_leaves_files_outside_the_cache_alone(tmp_path, capsys):
    """A hand-edited or stale cache_path must not turn a track deletion into
    an arbitrary unlink."""
    config = _config(tmp_path)
    outsider = tmp_path / "important.txt"
    outsider.write_text("keep me")

    db = Database(config.db_path)
    await db.connect()
    theme = await db.create_theme(DAY, "water", locked=True)
    track = await _add_track(db, theme["id"])
    await db.set_cache_status(track["id"], "ready", cache_path=str(outsider))
    await db.close()

    assert await _run_delete_track(config, _args(VID, DAY)) == 0
    assert outsider.exists()


async def test_cli_lists_the_day_when_the_song_is_not_found(tmp_path, capsys):
    config = _config(tmp_path)
    db = Database(config.db_path)
    await db.connect()
    theme = await db.create_theme(DAY, "water", locked=True)
    track = await _add_track(db, theme["id"], video_id=OTHER)
    await db.update_track_metadata(track["id"], title="Song A")
    await db.close()

    assert await _run_delete_track(config, _args(VID, DAY)) == 1

    err = capsys.readouterr().err
    assert "no song with video id" in err
    assert OTHER in err and "Song A" in err          # pick one from the list
    db = Database(config.db_path)
    await db.connect()
    try:
        assert len(await db.tracks_for_day(DAY)) == 1
    finally:
        await db.close()


async def test_cli_rejects_a_bad_video_and_a_bad_date(tmp_path):
    config = _config(tmp_path)
    assert await _run_delete_track(config, _args("not a link")) == 1
    assert await _run_delete_track(config, _args(VID, "July 6th")) == 1
