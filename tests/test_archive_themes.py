"""Archive theme list: every theme the channel ran, newest first, grouped by
month, with repeats counted."""

import time

from meshradio.ingest.parse import untitled_theme
from meshradio.web.context import archive_years, theme_history, theme_key, year_step

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


def test_archive_years_are_deduped_oldest_first():
    themes = [{"date": "2026-08-02"}, {"date": "2025-07-30"}, {"date": "2026-01-04"}]
    assert archive_years(themes) == ["2025", "2026"]
    assert year_step("2025") == {"key": "2025", "label": "2025"}
    assert year_step(None) is None


def test_repeat_counts_span_years_not_just_the_page():
    """A theme run in December and again in January is a repeat in both."""
    every = [
        {"date": "2026-01-04", "title": "Rain songs", "tracks": 1},
        {"date": "2025-12-30", "title": "rain songs", "tracks": 2},
    ]
    shown = [every[0]]                       # the 2026 page
    months = theme_history(shown, all_themes=every)
    assert months[0]["themes"][0]["runs"] == 2


async def test_themes_page_shows_one_year_at_a_time(db, bus):
    await seed_theme(db, "2025-12-30", "Deep cuts", ["aaaaaaaaaaa"])
    await seed_theme(db, "2026-01-04", "Rain songs", ["bbbbbbbbbbb"])
    async with client_for(page_app(db, bus)) as client:
        newest = (await client.get("/archive/themes")).text
        older = (await client.get("/archive/themes", params={"y": "2025"})).text
    assert "Rain songs" in newest and "Deep cuts" not in newest      # 2026 only
    assert "/archive/themes?y=2025" in newest                        # step back
    assert "Deep cuts" in older and "Rain songs" not in older
    assert "/archive/themes?y=2026" in older                         # step forward


async def test_unknown_year_falls_back_to_the_newest(db, bus):
    await seed_theme(db, "2026-01-04", "Rain songs", ["aaaaaaaaaaa"])
    async with client_for(page_app(db, bus)) as client:
        for bad in ("1999", "bogus", "../etc"):
            resp = await client.get("/archive/themes", params={"y": bad})
            assert resp.status_code == 200
            assert "Rain songs" in resp.text


async def test_theme_page_size_stays_bounded(db, bus):
    """The whole point of paging: three years of history is not one page."""
    for year in ("2024", "2025", "2026"):
        for month in range(1, 13):
            await seed_theme(db, f"{year}-{month:02d}-05", f"{year} theme {month}")
    async with client_for(page_app(db, bus)) as client:
        body = (await client.get("/archive/themes")).text
    assert body.count("board-name") == 12          # one year's worth, not 36
    assert "2026 theme 12" in body and "2024 theme 12" not in body


async def test_empty_theme_list_still_renders(db, bus):
    async with client_for(page_app(db, bus)) as client:
        resp = await client.get("/archive/themes")
    assert resp.status_code == 200
    assert "No themes yet" in resp.text
