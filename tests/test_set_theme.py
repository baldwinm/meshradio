"""Manual theme correction: db.rename_theme and the --set-theme CLI.

The channel can only ever set a day's theme once (it locks on the first
"Theme:" post), so a title that lands wrong — mangled parse, typo — has no
in-band fix. These cover the out-of-band one.
"""

from argparse import Namespace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from meshradio.app import _run_set_theme
from meshradio.config import Config
from meshradio.db import Database

VID = "dQw4w9WgXcQ"


def _args(title, date=None):
    return Namespace(set_theme=title, theme_date=date)


def _config(tmp_path, tz="America/Chicago"):
    config = Config(data_dir=tmp_path)
    config.player.timezone = tz
    return config


async def _add_track(db, theme_id, video_id=VID, sender="alice", ts=1_751_800_000.0):
    return await db.add_track(
        video_id=video_id,
        url=f"https://y/{video_id}",
        channel="#music",
        sender=sender,
        mesh_ts=ts,
        source="mesh",
        theme_id=theme_id,
    )


async def test_rename_keeps_the_playlist_and_the_lock(db: Database):
    theme = await db.create_theme(
        "2026-07-06", "-) or trains?", set_by="alice",
        raw_message="Today's theme is: planes :-) or trains?", locked=True,
    )
    await _add_track(db, theme["id"])

    renamed = await db.rename_theme(theme["id"], "water")

    assert renamed["id"] == theme["id"]        # same row: tracks stay attached
    assert renamed["title"] == "water"
    assert renamed["locked"] == 1
    assert renamed["set_by"] == "alice"        # who opened the day is preserved
    assert renamed["created_at"] == theme["created_at"]
    assert len(await db.tracks_for_theme(theme["id"])) == 1


async def test_rename_rewrites_raw_message_for_the_relay(db: Database):
    """raw_message is replayed verbatim as the channel message, so a receiver
    rebuilding from the relay must not be handed the old title again."""
    theme = await db.create_theme(
        "2026-07-06", "-) or trains?", set_by="alice",
        raw_message="Today's theme is: planes :-) or trains?", locked=True,
    )
    renamed = await db.rename_theme(theme["id"], "water")
    assert renamed["raw_message"] == "Theme: water"


async def test_rename_stamps_updated_at_past_created_at(db: Database):
    """Same cursor nudge as adopt_theme: a relay parked on this row's second
    still sees the change."""
    theme = await db.create_theme("2026-07-06", "old", set_by="alice", locked=True)
    renamed = await db.rename_theme(theme["id"], "water")
    assert renamed["updated_at"] > theme["created_at"]
    assert (await db.themes_since(theme["created_at"], theme["id"]))[0]["title"] == "water"


async def test_rename_collides_with_another_theme_that_day(db: Database):
    import sqlite3

    first = await db.create_theme("2026-07-06", "water", set_by="alice")
    second = await db.create_theme("2026-07-06", "wrong", set_by="bob")
    assert first["id"] != second["id"]
    with pytest.raises(sqlite3.IntegrityError):
        await db.rename_theme(second["id"], "water")


async def test_cli_renames_today_by_default(tmp_path, capsys):
    config = _config(tmp_path)
    today = datetime.now(ZoneInfo(config.player.timezone)).strftime("%Y-%m-%d")

    db = Database(config.db_path)
    await db.connect()
    theme = await db.create_theme(today, "-) or trains?", set_by="alice", locked=True)
    await _add_track(db, theme["id"])
    await db.close()

    assert await _run_set_theme(config, _args("water")) == 0

    db = Database(config.db_path)
    await db.connect()
    try:
        current = await db.latest_theme_for_date(today)
        assert current["title"] == "water"
        assert len(await db.tracks_for_theme(current["id"])) == 1  # songs survive
    finally:
        await db.close()
    assert "1 song kept" in capsys.readouterr().out


async def test_cli_creates_a_locked_theme_when_the_day_is_empty(tmp_path):
    config = _config(tmp_path)
    assert await _run_set_theme(config, _args("water", "2026-07-06")) == 0

    db = Database(config.db_path)
    await db.connect()
    try:
        theme = await db.latest_theme_for_date("2026-07-06")
    finally:
        await db.close()
    assert theme["title"] == "water"
    # Locked, so the next "Theme:" post on the channel can't override it, and
    # carrying a raw_message means the relay ships it instead of skipping it
    # as an auto-created placeholder.
    assert theme["locked"] == 1
    assert theme["raw_message"] == "Theme: water"


async def test_cli_is_a_noop_when_the_title_already_matches(tmp_path, capsys):
    config = _config(tmp_path)
    db = Database(config.db_path)
    await db.connect()
    before = await db.create_theme("2026-07-06", "water", set_by="alice", locked=True)
    await db.close()

    assert await _run_set_theme(config, _args("water", "2026-07-06")) == 0

    db = Database(config.db_path)
    await db.connect()
    try:
        after = await db.latest_theme_for_date("2026-07-06")
    finally:
        await db.close()
    assert after["updated_at"] is None       # untouched, so it doesn't re-relay
    assert after["created_at"] == before["created_at"]
    assert "already" in capsys.readouterr().out


async def test_cli_adopts_an_untitled_placeholder(tmp_path):
    """Links arrived before anyone named the day; the placeholder is renamed in
    place so the early songs stay in the one playlist."""
    config = _config(tmp_path)
    db = Database(config.db_path)
    await db.connect()
    placeholder = await db.create_theme("2026-07-06", "Untitled — 2026-07-06")
    await _add_track(db, placeholder["id"])
    await db.close()

    assert await _run_set_theme(config, _args("water", "2026-07-06")) == 0

    db = Database(config.db_path)
    await db.connect()
    try:
        theme = await db.latest_theme_for_date("2026-07-06")
        assert theme["id"] == placeholder["id"]
        assert theme["title"] == "water"
        assert len(await db.tracks_for_theme(theme["id"])) == 1
    finally:
        await db.close()


@pytest.mark.parametrize("bad", ["", "   ", "\n"])
async def test_cli_rejects_an_empty_title(tmp_path, bad):
    assert await _run_set_theme(_config(tmp_path), _args(bad, "2026-07-06")) == 1


@pytest.mark.parametrize("bad", ["07-06-2026", "2026-7-6-", "yesterday", "2026-13-01"])
async def test_cli_rejects_a_malformed_date(tmp_path, bad):
    assert await _run_set_theme(_config(tmp_path), _args("water", bad)) == 1


async def test_cli_reports_a_collision_instead_of_failing(tmp_path, capsys):
    config = _config(tmp_path)
    db = Database(config.db_path)
    await db.connect()
    await db.create_theme("2026-07-06", "water", set_by="alice")
    await db.create_theme("2026-07-06", "wrong", set_by="bob")
    await db.close()

    assert await _run_set_theme(config, _args("water", "2026-07-06")) == 1
    assert "already has a different theme row" in capsys.readouterr().err


async def test_cli_title_is_stripped(tmp_path):
    config = _config(tmp_path)
    assert await _run_set_theme(config, _args("  water  ", "2026-07-06")) == 0

    db = Database(config.db_path)
    await db.connect()
    try:
        assert (await db.latest_theme_for_date("2026-07-06"))["title"] == "water"
    finally:
        await db.close()
