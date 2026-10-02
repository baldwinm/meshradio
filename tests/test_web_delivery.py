"""How pages reach the browser: compression, asset caching, 404s, and the
per-page identity (title, current nav item) that history and tabs rely on."""

import re
import time

import pytest

from .helpers import client_for, make_ready_on, page_app, seed_day

GZIP = {"accept-encoding": "gzip"}
HTML = {"accept": "text/html,application/xhtml+xml"}


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


async def test_pages_link_versioned_assets(db, bus):
    """The immutable promise is only safe because every asset URL is versioned."""
    async with client_for(page_app(db, bus)) as client:
        body = (await client.get("/")).text
    assert "/static/style.css?v=" in body
    assert "/static/htmx.min.js" in body and "defer" in body


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


async def test_bad_archive_date_is_a_404_page(db, bus):
    await seed_day(db, "2026-08-01", "aaaaaaaaaaa")
    async with client_for(page_app(db, bus)) as client:
        resp = await client.get("/archive/not-a-date", headers=HTML)
    assert resp.status_code == 404
    assert "not-a-date" not in resp.text        # never echo the path back
    assert "Not found" in resp.text
    assert "/archive/themes" in resp.text        # offers a way onwards


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


async def test_archive_days_are_cached_between_calls(db, bus):
    """Two partial renders in the same instant must not re-run the aggregate."""
    from meshradio.web.context import ctx_of  # noqa: F401  (documents the path)

    app = page_app(db, bus)
    ctx = app.state.ctx
    await seed_day(db, "2026-08-01", "aaaaaaaaaaa")
    calls = 0
    original = db.archive_days

    async def counted():
        nonlocal calls
        calls += 1
        return await original()

    db.archive_days = counted
    try:
        first = await ctx.archive_days()
        again = await ctx.archive_days()
        assert calls == 1 and first == again
        at, value = ctx._cache["days"]
        ctx._cache["days"] = (at - ctx.CACHE_TTL_S - 1, value)   # age it out
        await ctx.archive_days()
        assert calls == 2
    finally:
        db.archive_days = original


def meta_of(body: str) -> dict[str, str]:
    """The page's link-preview tags, by property/name."""
    return dict(
        re.findall(r'<meta (?:property|name)="([^"]+)" content="([^"]*)"', body)
    )


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


async def test_the_404_declares_nothing_canonical(db, bus):
    async with client_for(page_app(db, bus)) as client:
        body = (await client.get("/archive/nope", headers=HTML)).text
    meta = meta_of(body)
    assert meta["robots"] == "noindex"
    assert meta["og:url"].endswith("/")             # the site, not the bad path
    assert "nope" not in body


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
    assert "first 100 matches" in body
    assert body.count("+ queue") <= 100


async def test_a_landing_song_drops_the_cached_aggregates(db, bus):
    """The caches go on the event that changes the archive, not on a clock:
    the render after a song lands sees it, and nothing is recomputed between
    songs however hard a crawler or a feed reader polls."""
    from meshradio.bus import TRACK_DISCOVERED

    app = page_app(db, bus)
    ctx = app.state.ctx
    await seed_day(db, "2026-08-01", "aaaaaaaaaaa")
    calls = 0
    original = db.archive_days

    async def counted():
        nonlocal calls
        calls += 1
        return await original()

    db.archive_days = counted
    try:
        for _ in range(3):
            await ctx.archive_days()
        assert calls == 1
        await seed_day(db, "2026-08-02", "bbbbbbbbbbb")        # no event: a direct write
        assert len(await ctx.archive_days()) == 1 and calls == 1   # still served from memory
        bus.publish(TRACK_DISCOVERED, {"track": {"id": 2}})
        assert len(await ctx.archive_days()) == 2 and calls == 2
    finally:
        db.archive_days = original


async def test_search_queries_are_cut_to_a_sane_length(db, bus):
    """Nobody types more than the cap; what arrives past it is a script's,
    and it never reaches the index."""
    from meshradio.web.routes_pages import SEARCH_MAX_CHARS

    app = page_app(db, bus)
    seen = []
    original = db.search_tracks

    async def spy(query, limit=100):
        seen.append(query)
        return await original(query, limit=limit)

    db.search_tracks = spy
    try:
        async with client_for(app) as client:
            assert (await client.get("/search", params={"q": "x" * 5000})).status_code == 200
        assert seen == ["x" * SEARCH_MAX_CHARS]
    finally:
        db.search_tracks = original


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
