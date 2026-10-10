"""The site footer on every public page, and the privacy page it links to.

The privacy page makes promises about the code (which cookies, what the
server keeps), so the parts that can drift are pinned here."""

from meshradio import __version__
from meshradio.web.sessions import SESSION_COOKIE

from .helpers import client_for, embed_app, page_app, seed_day

HTML = {"accept": "text/html"}
PAGES = ["/", "/archive", "/archive/themes", "/archive/2026-08-01", "/week",
         "/search?q=a", "/stats", "/about", "/privacy", "/nope"]


def footer_of(body: str) -> str:
    return body[body.index('<footer id="site-footer">'):body.index("</footer>")]


async def test_every_page_has_the_footer_in_both_modes(db, bus):
    await seed_day(db, "2026-08-01", "aaaaaaaaaaa")
    for app in (page_app(db, bus), embed_app(db, bus)):
        async with client_for(app) as client:
            for path in PAGES:
                footer = footer_of((await client.get(path, headers=HTML)).text)
                assert f"v{__version__}" in footer, path
                for href in ('href="/archive"', 'href="/privacy"', 'href="/feed.xml"',
                             'href="https://github.com/baldwinm/meshradio/issues/new"'):
                    assert href in footer, (path, href)


async def test_youtube_note_and_coffee_only_on_the_public_site(db, bus):
    async with client_for(embed_app(db, bus)) as client:
        hosted = footer_of((await client.get("/")).text)
    async with client_for(page_app(db, bus)) as client:
        appliance = footer_of((await client.get("/")).text)
    assert "YouTube" in hosted and 'class="coffee"' in hosted
    assert "YouTube" not in appliance and 'class="coffee"' not in appliance


async def test_footer_links_keep_the_music_playing(db, bus):
    """Internal links are boosted like the header's, so following one swaps
    <main> instead of reloading the page and stopping the song."""
    async with client_for(page_app(db, bus)) as client:
        footer = footer_of((await client.get("/")).text)
    listen = footer[footer.index("<h2>Listen</h2>") - 120:footer.index("<h2>Community</h2>")]
    assert 'hx-boost="true"' in listen and 'hx-select="main"' in listen


async def test_privacy_page_names_every_cookie_the_site_sets(db, bus):
    """A first visit to the public site gets exactly the cookies the page
    describes; the Pi sets none, and its page doesn't claim a session one."""
    async with client_for(embed_app(db, bus), visited=False) as client:
        first = await client.get("/privacy")
        page = first.text
    set_names = {c.split("=", 1)[0] for c in first.headers.get_list("set-cookie")}
    assert set_names == {SESSION_COOKIE}
    assert SESSION_COOKIE in page and ">skin<" in page
    assert "Last updated" in page

    async with client_for(page_app(db, bus), visited=False) as client:
        first = await client.get("/privacy")
    assert not first.headers.get_list("set-cookie")
    assert SESSION_COOKIE not in first.text
    assert "Render" not in first.text   # the Pi isn't hosted there


async def test_privacy_page_is_in_the_sitemap(db, bus):
    async with client_for(page_app(db, bus)) as client:
        assert "<loc>http://test/privacy</loc>" in (await client.get("/sitemap.xml")).text
