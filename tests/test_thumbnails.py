"""Cover-art stamps beside songs on the archive's list pages (day, search,
member) — one macro, so the three can't drift apart."""

import re
import time

from .helpers import client_for, page_app, seed_day

STILL = "https://i.ytimg.com/vi/{}/mqdefault.jpg"


def stamps(body):
    """Every cover-art <img> on the page, as its raw tag."""
    return re.findall(r'<img class="thumb"[^>]*>', body, flags=re.S)


async def test_day_page_shows_a_stamp_per_song(db, bus):
    theme = await db.create_theme("2026-07-06", "rain songs")
    for vid in ("aaaaaaaaaaa", "bbbbbbbbbbb"):
        await db.add_track(
            video_id=vid, url="u", channel="#music", sender="alice",
            mesh_ts=time.time(), source="mesh", theme_id=theme["id"],
        )
    async with client_for(page_app(db, bus)) as client:
        body = (await client.get("/archive/2026-07-06")).text
    assert len(stamps(body)) == 2
    assert STILL.format("aaaaaaaaaaa") in body and STILL.format("bbbbbbbbbbb") in body


async def test_search_and_member_pages_show_stamps(db, bus):
    await seed_day(db, "2026-07-06", "aaaaaaaaaaa")
    async with client_for(page_app(db, bus)) as client:
        search = (await client.get("/search?q=alice")).text
        member = (await client.get("/member/alice")).text
    for body in (search, member):
        assert len(stamps(body)) == 1
        assert STILL.format("aaaaaaaaaaa") in body


async def test_stamps_are_lazy_sized_and_decorative(db, bus):
    """Lazy so a 100-row search doesn't fetch every still up front; sized in
    markup so rows don't shift as they arrive; empty alt because the title
    beside it is the text."""
    await seed_day(db, "2026-07-06", "aaaaaaaaaaa")
    async with client_for(page_app(db, bus)) as client:
        (tag,) = stamps((await client.get("/archive/2026-07-06")).text)
    assert 'loading="lazy"' in tag
    assert 'width="56"' in tag and 'height="32"' in tag
    assert 'alt=""' in tag


async def test_no_stamps_where_there_are_no_songs(db, bus):
    await seed_day(db, "2026-07-06", "aaaaaaaaaaa")
    async with client_for(page_app(db, bus)) as client:
        empty = (await client.get("/search?q=no-such-thing")).text
        blank = (await client.get("/search")).text
    assert stamps(empty) == [] and stamps(blank) == []


async def test_stamp_images_are_allowed_by_the_csp(db, bus):
    """The stills come from i.ytimg.com; if the policy ever stops allowing it
    every stamp is a broken image, silently."""
    await seed_day(db, "2026-07-06", "aaaaaaaaaaa")
    async with client_for(page_app(db, bus)) as client:
        csp = (await client.get("/archive/2026-07-06")).headers["content-security-policy"]
    img_src = next(d for d in csp.split("; ") if d.startswith("img-src"))
    assert "https://i.ytimg.com" in img_src
