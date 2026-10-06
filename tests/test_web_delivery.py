"""How pages reach the browser: compression, asset caching, 404s, and the
per-page identity (title, current nav item) that history and tabs rely on."""

import re
import time

import pytest

from .helpers import (
    client_for,
    counting,
    embed_app,
    make_ready_on,
    page_app,
    seed_day,
    seed_shares,
)

GZIP = {"accept-encoding": "gzip"}
HTML = {"accept": "text/html,application/xhtml+xml"}


def meta_of(body: str) -> dict[str, str]:
    """The page's link-preview tags, by property/name."""
    return dict(
        re.findall(r'<meta (?:property|name)="([^"]+)" content="([^"]*)"', body)
    )


async def seed_month(db, month="2026-08", days=20):
    """Enough of an archive that pages are worth compressing."""
    for day in range(1, days + 1):
        await seed_day(db, f"{month}-{day:02d}", f"{day:011d}")


async def test_pages_are_compressed(db, bus):
    await seed_month(db)
    async with client_for(page_app(db, bus)) as client:
        resp = await client.get("/archive", headers=GZIP)
    assert resp.headers.get("content-encoding") == "gzip"
    assert "Vary" in resp.headers or "vary" in resp.headers


async def test_small_partials_skip_compression(db, bus):
    """Below the middleware floor, gzip would cost more than it saves."""
    async with client_for(page_app(db, bus)) as client:
        resp = await client.get("/partials/queue", headers=GZIP)
    assert resp.headers.get("content-encoding") is None


async def test_versioned_assets_are_immutable(db, bus):
    async with client_for(page_app(db, bus)) as client:
        versioned = await client.get("/static/js/radio.js?v=12345")
        bare = await client.get("/static/js/radio.js")
    assert versioned.headers["cache-control"] == "public, max-age=31536000, immutable"
    # No version in the URL: we can't promise the bytes never change.
    assert bare.headers["cache-control"] == "public, max-age=300"


async def test_every_page_carries_the_shared_head(db, bus):
    """hx-boost swaps <main> and never re-runs scripts, so whatever page a
    visitor lands on directly has to carry the whole head: versioned assets
    (the immutable promise is only safe because every asset URL is
    versioned), the lock-screen script, the feed for readers and browsers
    to autodiscover, and the touch icon with a fixed app name."""
    await seed_day(db, "2026-08-01", "aaaaaaaaaaa")
    async with client_for(page_app(db, bus)) as client:
        for path in ("/", "/archive", "/archive/2026-08-01", "/search", "/stats", "/about"):
            body = (await client.get(path)).text
            assert "/static/style.css?v=" in body, path
            assert "/static/htmx.min.js" in body and "defer" in body, path
            assert "/static/js/mediasession.js?v=" in body, path
            assert ('<link rel="alternate" type="application/atom+xml"' in body
                    and 'href="http://test/feed.xml"' in body), path
            assert '<link rel="apple-touch-icon" href="/static/apple-touch-icon.png?v=' in body
            assert '<meta name="apple-mobile-web-app-title" content="MeshRadio">' in body, path


async def test_audio_opts_out_of_gzip(db, bus, tmp_path):
    """Re-compressing opus wastes CPU and would break ranged requests."""
    track = await make_ready_on(db, "aaaaaaaaaaa", "2026-08-01")
    path = tmp_path / "song.opus"
    path.write_bytes(b"OggS" + b"\0" * 4096)
    await db.set_cache_status(track["id"], "ready", str(path))
    async with client_for(page_app(db, bus)) as client:
        resp = await client.get(f"/audio/{track['id']}", headers=GZIP)
    assert resp.status_code == 200
    assert resp.headers["content-encoding"] == "identity"


@pytest.mark.parametrize(
    "path,expect",
    [
        ("/", "Now Playing"),
        ("/archive", "Archive"),
        ("/archive/themes", "Archive"),   # the theme list is part of the archive
        ("/search", "Search"),
        ("/stats", "Stats"),
        ("/about", "About"),
    ],
)
async def test_nav_marks_the_current_section(db, bus, path, expect):
    await seed_day(db, "2026-08-01", "aaaaaaaaaaa")
    async with client_for(page_app(db, bus)) as client:
        body = (await client.get(path)).text
    marked = [
        line for line in body.splitlines() if 'aria-current="page"' in line
    ]
    assert len(marked) == 1, f"{path}: expected exactly one current nav item"
    assert expect in body[body.index('aria-current="page"'):][:80]


async def test_pages_have_their_own_titles(db, bus):
    await seed_day(db, "2026-08-01", "aaaaaaaaaaa")
    async with client_for(page_app(db, bus)) as client:
        titles = {}
        for path in ("/", "/archive", "/archive/themes", "/archive/2026-08-01",
                     "/search?q=x", "/stats", "/about"):
            body = (await client.get(path)).text
            titles[path] = body[body.index("<title>") + 7:body.index("</title>")]
    assert len(set(titles.values())) == len(titles), titles
    assert titles["/archive"].startswith("Archive")
    assert "2026-08-01" in titles["/archive/2026-08-01"]
    assert all(t.endswith("MeshRadio") for t in titles.values())


async def test_bad_archive_date_is_a_404_page_that_claims_nothing(db, bus):
    await seed_day(db, "2026-08-01", "aaaaaaaaaaa")
    async with client_for(page_app(db, bus)) as client:
        resp = await client.get("/archive/not-a-date", headers=HTML)
    assert resp.status_code == 404
    assert "not-a-date" not in resp.text        # never echo the path back
    assert "Not found" in resp.text
    assert "/archive/themes" in resp.text        # offers a way onwards
    meta = meta_of(resp.text)
    assert meta["robots"] == "noindex"
    assert meta["og:url"].endswith("/")          # the site, not the bad path


async def test_quiet_day_is_a_404_not_an_empty_page(db, bus):
    await seed_day(db, "2026-08-01", "aaaaaaaaaaa")
    async with client_for(page_app(db, bus)) as client:
        assert (await client.get("/archive/2026-08-02", headers=HTML)).status_code == 404
        assert (await client.get("/archive/2026-08-01", headers=HTML)).status_code == 200


async def test_api_404s_stay_json(db, bus):
    async with client_for(page_app(db, bus)) as client:
        resp = await client.get("/audio/9999", headers={"accept": "*/*"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "track not cached"


async def test_a_day_previews_with_its_theme_and_art(db, bus):
    """A day is what gets pasted into a chat; the card has to say something."""
    theme = await db.create_theme("2026-08-01", "Rain songs")
    await db.add_track(
        video_id="aaaaaaaaaaa", url="u", channel="#music", sender="ana",
        mesh_ts=time.time(), source="mesh", theme_id=theme["id"],
    )
    async with client_for(page_app(db, bus)) as client:
        meta = meta_of((await client.get("/archive/2026-08-01")).text)
    assert meta["og:title"] == "2026-08-01 — Rain songs"
    assert "1 song" in meta["og:description"] and "Rain songs" in meta["og:description"]
    assert meta["og:image"] == "https://i.ytimg.com/vi/aaaaaaaaaaa/hqdefault.jpg"
    assert meta["twitter:card"] == "summary_large_image"
    assert meta["og:url"].endswith("/archive/2026-08-01")


async def test_pages_without_art_still_carry_a_card(db, bus):
    async with client_for(page_app(db, bus)) as client:
        meta = meta_of((await client.get("/about")).text)
    assert meta["og:title"] == "About"
    assert meta["twitter:card"] == "summary"        # no image to show
    assert "og:image" not in meta
    assert meta["description"].startswith("Songs shared each day")


async def test_crawlers_get_the_pages_and_not_the_machinery(db, bus):
    await seed_day(db, "2026-08-01", "aaaaaaaaaaa")
    async with client_for(page_app(db, bus)) as client:
        robots = (await client.get("/robots.txt")).text
        sitemap = (await client.get("/sitemap.xml")).text
    for blocked in ("/api/", "/partials/", "/audio/", "/search"):
        assert f"Disallow: {blocked}" in robots
    assert "Sitemap: http" in robots and "/sitemap.xml" in robots
    assert "/archive/2026-08-01</loc>" in sitemap   # every archived day
    assert "/archive/themes</loc>" in sitemap
    assert sitemap.startswith("<?xml")


async def test_forwarded_https_survives_the_proxy(db, bus):
    """Hosted deployments terminate TLS upstream; the app sees plain http."""
    async with client_for(page_app(db, bus)) as client:
        body = (await client.get("/about", headers={"x-forwarded-proto": "https"})).text
    assert meta_of(body)["og:url"].startswith("https://")


async def test_help_documents_the_shortcuts(db, bus):
    async with client_for(page_app(db, bus)) as client:
        body = (await client.get("/")).text
    assert "/static/js/keys.js" in body
    help_text = body[body.index("<dialog id=\"help\""):body.index("</dialog>")]
    assert "<strong>Keyboard</strong>" in help_text
    for key in ("Space", "N", "M", "?"):
        assert f'class="k">{key}<' in help_text


async def test_search_says_when_the_list_is_cut_off(db, bus):
    theme = await db.create_theme("2026-08-01", "many songs")
    for i in range(105):
        await db.add_track(
            video_id=f"{i:011d}", url="u", channel="#music", sender="alice",
            mesh_ts=time.time() + i, source="mesh", theme_id=theme["id"],
            title=f"Song {i}",
        )
    async with client_for(page_app(db, bus)) as client:
        body = (await client.get("/search", params={"q": "Song"})).text
    assert "first 100 songs" in body
    assert body.count("+ queue") <= 100


async def test_archive_days_are_cached_until_a_song_lands_or_the_ttl_passes(db, bus):
    """The caches go on the event that changes the archive, not on a clock:
    the render after a song lands sees it, and nothing is recomputed between
    songs however hard a crawler or a feed reader polls. The TTL is the
    safety net for a write that raised no event."""
    from meshradio.bus import TRACK_DISCOVERED

    app = page_app(db, bus)
    ctx = app.state.ctx
    await seed_day(db, "2026-08-01", "aaaaaaaaaaa")
    calls = counting(db, "archive_days")
    first = await ctx.archive_days()
    for _ in range(3):
        assert await ctx.archive_days() == first
    assert calls["n"] == 1
    await seed_day(db, "2026-08-02", "bbbbbbbbbbb")        # no event: a direct write
    assert len(await ctx.archive_days()) == 1 and calls["n"] == 1   # still served from memory
    bus.publish(TRACK_DISCOVERED, {"track": {"id": 2}})
    assert len(await ctx.archive_days()) == 2 and calls["n"] == 2
    at, value = ctx._cache["days"]
    ctx._cache["days"] = (at - ctx.CACHE_TTL_S - 1, value)   # age it out
    await ctx.archive_days()
    assert calls["n"] == 3


async def test_search_queries_are_cut_to_a_sane_length(db, bus):
    """Nobody types more than the cap; what arrives past it is a script's,
    and it never reaches the index."""
    from meshradio.web.routes_pages import SEARCH_MAX_CHARS

    app = page_app(db, bus)
    seen = []
    original = db.search_tracks

    async def spy(query, **kwargs):
        seen.append(query)
        return await original(query, **kwargs)

    db.search_tracks = spy
    try:
        async with client_for(app) as client:
            assert (await client.get("/search", params={"q": "x" * 5000})).status_code == 200
        assert seen == ["x" * SEARCH_MAX_CHARS]
    finally:
        db.search_tracks = original


async def test_search_filters_narrow_the_page_and_stay_in_the_url(db, bus):
    """A member and a year are a search on their own, and they survive in the
    form so a narrowed search is a link somebody can paste."""
    await seed_shares(db, "aaaaaaaaaaa", ["2025-07-06"], title="Old One", senders=("Ana",))
    await seed_shares(db, "bbbbbbbbbbb", ["2026-07-06"], title="New One", senders=("Ana",))
    await seed_shares(db, "ccccccccccc", ["2026-07-07"], title="Theirs", senders=("bob",))

    async with client_for(page_app(db, bus)) as client:
        both = (await client.get("/search", params={"member": "Ana", "year": "2026"})).text
        opened = (await client.get("/search")).text
        junk = (await client.get("/search", params={"year": "sometime"})).text

    assert "New One" in both and "Old One" not in both and "Theirs" not in both
    assert "shared by Ana" in both and "in 2026" in both
    assert '<option value="Ana" selected>' in both and '<option value="2026" selected>' in both
    # Both dropdowns are offered before anything is searched, and nothing is
    # listed until one of the three is set.
    assert '<option value="bob"' in opened and '<option value="2025"' in opened
    assert "New One" not in opened and "Old One" not in opened
    # A year that isn't a year is dropped, not a 404: the page still answers.
    assert "New One" not in junk and "Old One" not in junk


async def test_search_lists_a_repeat_share_once(db, bus):
    """The same song on three days is one row saying so, not three rows
    pushing other songs off the page."""
    await seed_shares(
        db, "aaaaaaaaaaa", ["2026-07-06", "2026-07-07", "2026-07-08"],
        title="Purple Rain", senders=("alice", "bob", "alice"))

    async with client_for(page_app(db, bus)) as client:
        body = (await client.get("/search", params={"q": "rain"})).text

    assert body.count("Purple Rain") == 1
    assert "shared 3&times; by 2 members" in body
    assert "1 song for “rain”" in body


def test_asset_version_follows_content_not_mtime(tmp_path, monkeypatch):
    """A fresh clone stamps every file with the build's time, so a version
    from mtimes invalidated every asset on every deploy. The hash moves only
    when a file's bytes do."""
    from meshradio.web import server as server_mod

    static = tmp_path / "static"
    static.mkdir()
    (static / "style.css").write_text("body{}")
    (static / "js").mkdir()
    (static / "js" / "radio.js").write_text("// radio")
    monkeypatch.setattr(server_mod, "_HERE", tmp_path)
    first = server_mod._asset_version()
    assert re.fullmatch(r"[0-9a-f]{12}", first)
    (static / "style.css").touch()                                  # a new mtime, same bytes
    assert server_mod._asset_version() == first
    (static / "style.css").write_text("body{margin:0}")
    assert server_mod._asset_version() != first


@pytest.mark.parametrize("headers", [{}, {"HX-Request": "true", "HX-Boosted": "true"}])
async def test_equalizer_is_device_only(db, bus, headers):
    """The EQ shapes this tab's Web Audio graph, which embed hosting's YouTube
    iframe never feeds — so the public site must not render it, whether Now
    Playing is a first load or a boosted hop back from the Archive."""
    async with client_for(embed_app(db, bus)) as client:
        await client.get("/archive", headers=headers)
        hosted = (await client.get("/", headers=headers)).text
    async with client_for(page_app(db, bus)) as client:
        appliance = (await client.get("/", headers=headers)).text
    assert 'id="eq-window"' not in hosted
    assert 'id="eq-window"' in appliance
