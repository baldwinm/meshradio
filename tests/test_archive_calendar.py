"""Archive page calendar: one month at a time, arrows stepping through the
months the channel was actually alive in."""

import time

from meshradio.audio.routing import make_router
from meshradio.config import PlayerConfig
from meshradio.media.player import NullBackend, PlayerService
from meshradio.web.context import archive_months, calendar_month, month_step
from meshradio.web.server import create_app

from .test_sessions import client_for


async def seed_day(db, date, video_id):
    theme = await db.create_theme(date, f"theme {date}")
    await db.add_track(
        video_id=video_id, url="u", channel="#music", sender="alice",
        mesh_ts=time.time(), source="mesh", theme_id=theme["id"],
    )


def page_app(db, bus):
    player = PlayerService(PlayerConfig(), db, bus, backend=NullBackend())
    return create_app(bus, db, player, make_router("dev", bus))


def test_archive_months_are_deduped_oldest_first():
    days = [{"date": "2026-08-01"}, {"date": "2026-07-09"}, {"date": "2026-07-03"}]
    assert archive_months(days) == ["2026-07", "2026-08"]


def test_calendar_month_grid_blanks_the_neighbors():
    month = calendar_month([{"date": "2026-07-04", "tracks": 3}], "2026-07")
    assert month["label"] == "July 2026"
    assert all(len(week) == 7 for week in month["weeks"])
    cells = [c for week in month["weeks"] for c in week if c]
    assert [c["day"] for c in cells] == list(range(1, 32))  # July only
    assert [c["iso"] for c in cells if c["info"]] == ["2026-07-04"]


def test_month_step_labels_the_target():
    assert month_step("2026-07") == {"key": "2026-07", "label": "July 2026"}
    assert month_step(None) is None


async def test_archive_opens_on_the_newest_month(db, bus):
    await seed_day(db, "2026-07-03", "aaaaaaaaaaa")
    await seed_day(db, "2026-08-01", "bbbbbbbbbbb")
    async with client_for(page_app(db, bus)) as client:
        body = (await client.get("/archive")).text
    assert "August 2026" in body and "July 2026" in body  # label + prev-arrow title
    assert "/archive?m=2026-07" in body                   # backwards
    assert "/archive?m=2026-09" not in body               # no forwards past the end
    assert "/archive/2026-08-01" in body                  # August's day is on the grid
    assert "/archive/2026-07-03" not in body              # July's is not


async def test_archive_steps_backwards_and_offers_the_way_forward(db, bus):
    await seed_day(db, "2026-07-03", "aaaaaaaaaaa")
    await seed_day(db, "2026-08-01", "bbbbbbbbbbb")
    async with client_for(page_app(db, bus)) as client:
        body = (await client.get("/archive?m=2026-07")).text
    assert "<h3>July 2026</h3>" in body
    assert "/archive?m=2026-08" in body                   # forwards again
    assert "/archive/2026-07-03" in body


async def test_unknown_month_falls_back_to_the_newest(db, bus):
    await seed_day(db, "2026-07-03", "aaaaaaaaaaa")
    async with client_for(page_app(db, bus)) as client:
        for bad in ("1999-01", "bogus", "../etc"):
            resp = await client.get("/archive", params={"m": bad})
            assert resp.status_code == 200
            assert "<h3>July 2026</h3>" in resp.text


async def test_empty_archive_still_renders(db, bus):
    async with client_for(page_app(db, bus)) as client:
        resp = await client.get("/archive")
    assert resp.status_code == 200
    assert "No history yet" in resp.text
