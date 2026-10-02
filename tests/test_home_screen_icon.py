"""iOS "Add to Home Screen": the logo as the icon, and a name that isn't the
day's theme.

Without a touch icon iOS draws a letter tile from the page <title>, which on
Now Playing is the theme — it showed "S" for "States & cities"."""

import struct
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import meshradio.web as web

from .helpers import client_for, page_app

STATIC = Path(web.__file__).parent / "static"


async def seed_themed_day(db, date, title):
    theme = await db.create_theme(date, title, locked=True)
    await db.add_track(
        video_id="aaaaaaaaaaa", url="u", channel="#music", sender="alice",
        mesh_ts=1_785_000_000.0, source="mesh", theme_id=theme["id"],
    )


async def test_the_fixed_name_does_not_follow_the_theme(db, bus):
    """The page <title> is what iOS would otherwise name the app. Now Playing
    shows today (channel time) until it's cued elsewhere, so seed today."""
    today = datetime.now(ZoneInfo("America/Chicago")).date().isoformat()
    await seed_themed_day(db, today, "States & cities")
    async with client_for(page_app(db, bus)) as client:
        body = (await client.get("/")).text
    assert "<title>States &amp; cities" in body            # the title does carry the theme…
    assert 'apple-mobile-web-app-title" content="MeshRadio"' in body   # …the app name doesn't


async def test_the_icon_is_served_as_a_png(db, bus):
    async with client_for(page_app(db, bus)) as client:
        resp = await client.get("/static/apple-touch-icon.png?v=1")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert "immutable" in resp.headers["cache-control"]


def test_the_icon_is_the_180px_opaque_png_ios_wants():
    """iOS ignores SVG touch icons, wants 180×180 for iPhone, and fills any
    transparency with black — so: a PNG, that size, no alpha channel."""
    data = (STATIC / "apple-touch-icon.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height, _depth, color_type = struct.unpack(">IIBB", data[16:26])
    assert (width, height) == (180, 180)
    assert color_type in (0, 2, 3)                 # grey, RGB or palette — not 4/6 (alpha)
