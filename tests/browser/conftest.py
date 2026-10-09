"""Browser tests: the real pages in headless Chromium against a live server.

They catch what the HTTP tests can't see: a script that throws, a control
that no longer does anything, a page the Content-Security-Policy breaks.
Nothing leaves the machine. YouTube's IFrame API is replaced by a stand-in
that records what the page asks of it, and every other outside request
(thumbnails) gets an empty answer.

Run: MESHRADIO_BROWSER_TESTS=1 uv run --group browser pytest tests/browser
(after `uv run --group browser playwright install chromium` once).
"""

import pytest
from playwright.sync_api import sync_playwright

from meshradio.ingest.service import IngestService
from meshradio.runtime import recent_errors

from ..helpers import (
    LiveServer,
    admin_settings,
    embed_app,
    page_app,
    share,
)

DAY = "2026-07-06"
EARLIER = "2026-07-05"
SONGS = [
    ("aaaaaaaaaaa", "Song A", "Artist A"),
    ("bbbbbbbbbbb", "Song B", "Artist B"),
    ("ccccccccccc", "Song C", "Artist A"),
]

# Enough of YT.Player for embed.js: it builds the player, loads, plays and
# pauses videos, and listens for state changes. ``window.__yt`` keeps every
# video id the page loaded, in order, for the tests to read.
FAKE_IFRAME_API = """
window.YT = {
  PlayerState: {UNSTARTED: -1, ENDED: 0, PLAYING: 1, PAUSED: 2, BUFFERING: 3, CUED: 5},
  Player: class {
    constructor(el, opts) {
      this.opts = opts; this.loads = [opts.videoId]; this.state = -1;
      window.__yt = this;
      setTimeout(() => opts.events.onReady({target: this}), 0);
    }
    playVideo() { this.state = 1; this.opts.events.onStateChange({data: 1}); }
    pauseVideo() { this.state = 2; }
    stopVideo() { this.state = 5; }
    loadVideoById(id) { this.loads.push(id); }
    getPlayerState() { return this.state; }
    getDuration() { return 60; }
    getCurrentTime() { return 0; }
    seekTo() {}
    setVolume() {}
    destroy() {}
  },
};
window.onYouTubeIframeAPIReady();
"""


async def seed(db):
    tracks = []
    for video_id, title, artist in SONGS:
        tracks.append(await share(db, DAY, video_id, "alice", title=title, artist=artist,
                                  theme="songs about trains"))
    await share(db, EARLIER, "ddddddddddd", "bob", title="Song D", artist="Artist B",
                theme="rainy days")
    return tracks


@pytest.fixture(scope="session")
def appliance(tmp_path_factory):
    """The Pi: one communal player, with the admin page on."""
    tmp = tmp_path_factory.mktemp("appliance")

    async def build(db, bus):
        await seed(db)
        return page_app(db, bus, admin=admin_settings(tmp))

    with LiveServer(build, tmp / "meshradio.db") as server:
        yield server


@pytest.fixture(scope="session")
def hosted(tmp_path_factory):
    """meshradio.co: embed mode, a player per visitor."""
    tmp = tmp_path_factory.mktemp("hosted")

    async def build(db, bus):
        await seed(db)
        return embed_app(db, bus, ingest=IngestService(db, bus, channel="#music"))

    with LiveServer(build, tmp / "meshradio.db") as server:
        yield server


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def page(browser):
    """A fresh browser profile. A script error or console error on any page
    it opens fails the test, and so does a server-side failure (a request
    that raised) while it ran."""
    context = browser.new_context()

    def route(route):
        url = route.request.url
        if url.startswith("http://127.0.0.1:"):
            route.continue_()
        elif url.startswith("https://www.youtube.com/iframe_api"):
            route.fulfill(content_type="text/javascript", body=FAKE_IFRAME_API)
        else:
            route.fulfill(status=200, body="")

    context.route("**/*", route)
    page = context.new_page()
    page.set_default_timeout(5000)
    problems: list[str] = []
    page.on("pageerror", lambda exc: problems.append(f"script error: {exc}"))
    page.on(
        "console",
        lambda msg: problems.append(f"console: {msg.text}") if msg.type == "error" else None,
    )
    yield page
    context.close()
    assert not problems, "\n".join(problems)
    assert not recent_errors(), f"the server failed: {recent_errors()}"
