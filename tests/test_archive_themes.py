"""Archive theme list: every theme the channel ran, newest first, grouped by
month, with repeats counted."""

import time

from meshradio.ingest.parse import untitled_theme
from meshradio.web.context import theme_history, theme_key

from .test_archive_calendar import page_app
from .test_sessions import client_for


async def seed_theme(db, date, title, video_ids=(), set_by=None):
    theme = await db.create_theme(date, title, set_by=set_by)
    for video_id in video_ids:
        await db.add_track(
            video_id=video_id, url="u", channel="#music", sender="alice",
            mesh_ts=time.time(), source="mesh", theme_id=theme["id"],
        )
    return theme


def test_theme_key_ignores_case_and_spacing():
    assert theme_key("Rain  songs") == theme_key("rain songs")


def test_theme_history_groups_months_and_counts_repeats():
    months = theme_history([
        {"date": "2026-08-02", "title": "Rain songs", "tracks": 2},
        {"date": "2026-07-30", "title": "One hit wonders", "tracks": 5},
        {"date": "2026-07-04", "title": "rain  songs", "tracks": 1},
    ])
    assert [m["label"] for m in months] == ["August 2026", "July 2026"]
    assert [t["title"] for t in months[1]["themes"]] == ["One hit wonders", "rain  songs"]
    assert [t["runs"] for t in months[0]["themes"]] == [2]      # ran again in July
    assert [t["runs"] for t in months[1]["themes"]] == [1, 2]


async def test_all_themes_newest_first_with_song_counts(db):
    await seed_theme(db, "2026-07-04", "One hit wonders", ["aaaaaaaaaaa", "bbbbbbbbbbb"])
    await seed_theme(db, "2026-08-02", "Rain songs", ["ccccccccccc"])
    themes = await db.all_themes()
    assert [(t["title"], t["tracks"]) for t in themes] == [
        ("Rain songs", 1),
        ("One hit wonders", 2),
    ]


async def test_all_themes_skips_untitled_placeholders(db):
    await seed_theme(db, "2026-07-05", untitled_theme("2026-07-05"), ["aaaaaaaaaaa"])
    await seed_theme(db, "2026-07-06", "Rain songs")
    assert [t["title"] for t in await db.all_themes()] == ["Rain songs"]


async def test_themes_page_lists_every_theme(db, bus):
    await seed_theme(db, "2026-07-04", "One hit wonders", ["aaaaaaaaaaa"], set_by="bob")
    await seed_theme(db, "2026-08-02", "Rain songs", ["bbbbbbbbbbb"])
    async with client_for(page_app(db, bus)) as client:
        body = (await client.get("/archive/themes")).text
    assert "One hit wonders" in body and "Rain songs" in body
    assert "August 2026" in body and "July 2026" in body
    assert "/archive/2026-07-04" in body                  # tap a theme for its day
    assert "set by bob" in body
    assert body.index("Rain songs") < body.index("One hit wonders")   # newest first


async def test_themes_page_is_not_read_as_a_date(db, bus):
    """``/archive/themes`` must not fall through to the day page."""
    await seed_theme(db, "2026-08-02", "Rain songs", ["aaaaaaaaaaa"])
    async with client_for(page_app(db, bus)) as client:
        resp = await client.get("/archive/themes")
        calendar = (await client.get("/archive")).text
    assert resp.status_code == 200
    assert "<h2>Themes</h2>" in resp.text
    assert "/api/play-day/themes" not in resp.text       # not the day page
    assert "/archive/themes" in calendar                  # reachable from the calendar


async def test_empty_theme_list_still_renders(db, bus):
    async with client_for(page_app(db, bus)) as client:
        resp = await client.get("/archive/themes")
    assert resp.status_code == 200
    assert "No themes yet" in resp.text
