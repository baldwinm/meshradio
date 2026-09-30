"""Who may drive the radio: only pages this instance served.

The appliance is a communal player with no login, so the only thing between
a random web page and ``POST /api/output/bluetooth`` on the LAN is the
browser's Origin header — which the guard now checks for every state change
and every WebSocket handshake. Reads stay open; a link into the archive from
a chat is a cross-site GET and must keep working."""

import warnings

import pytest

with warnings.catch_warnings():
    # Starlette would rather we used httpx2; the WebSocket handshake tests
    # below only need the client, and httpx is what the project pins.
    warnings.simplefilter("ignore", DeprecationWarning)
    from starlette.testclient import TestClient, WebSocketDenialResponse

from meshradio.audio.routing import make_router
from meshradio.bus import EventBus
from meshradio.config import PlayerConfig
from meshradio.db import Database
from meshradio.media.player import NullBackend, PlayerService
from meshradio.web.server import create_app, same_site

from .test_archive_calendar import page_app
from .test_sessions import client_for

EVIL = {"origin": "https://evil.example"}


def test_same_site_rules():
    assert same_site(None, "radio.local:8080")                      # no browser involved
    assert same_site("http://radio.local:8080", "radio.local:8080")
    assert same_site("HTTP://Radio.Local:8080", "radio.local:8080")  # case-insensitive
    assert same_site("https://meshradio.onrender.com", "meshradio.onrender.com:443")
    assert same_site("http://radio.local:80", "radio.local")        # default ports
    assert not same_site("https://evil.example", "radio.local:8080")
    assert not same_site("null", "radio.local:8080")               # sandboxed / file://
    assert not same_site(None, "radio.local", fetch_site="cross-site")
    assert same_site(None, "radio.local", fetch_site="same-origin")


async def test_cross_site_state_changes_are_refused(db, bus):
    app = page_app(db, bus)
    player = app.state.ctx.player
    async with client_for(app) as client:
        for path in ("/api/volume/55", "/api/output/bluetooth", "/api/queue/clear", "/api/skip"):
            resp = await client.post(path, headers=EVIL)
            assert resp.status_code == 403, path
        assert player.volume == PlayerConfig().volume                # untouched
        assert app.state.ctx.audio_router.current() == "speaker"

        # A browser on our own page sends a matching Origin (client host is "test").
        assert (await client.post("/api/volume/55", headers={"origin": "http://test"})).status_code == 200
        assert player.volume == 55
        # No Origin at all: curl, the relay pusher, this test suite.
        assert (await client.post("/api/volume/56")).status_code == 200
        # Sec-Fetch-Site says cross-site even when Origin is missing.
        assert (await client.post("/api/volume/57", headers={"sec-fetch-site": "cross-site"})).status_code == 403
        assert player.volume == 56


async def test_cross_site_reads_still_work(db, bus):
    """Someone following a link from Slack arrives with a cross-site GET."""
    async with client_for(page_app(db, bus)) as client:
        headers = {**EVIL, "sec-fetch-site": "cross-site", "accept": "text/html"}
        assert (await client.get("/archive", headers=headers)).status_code == 200
        assert (await client.get("/api/state", headers=EVIL)).status_code == 200


def _communal_app(tmp_path):
    """An app whose WebSocket route needs no database: the guard decides
    before the handler runs, and the handler itself only reads player state."""
    bus = EventBus()
    db = Database(tmp_path / "unused.db")   # never connected, never touched
    player = PlayerService(PlayerConfig(), db, bus, backend=NullBackend())
    return create_app(bus, db, player, make_router("dev", bus))


def test_cross_site_websocket_is_refused(tmp_path):
    client = TestClient(_communal_app(tmp_path))
    with pytest.raises(WebSocketDenialResponse) as denied:
        with client.websocket_connect("/ws", headers=EVIL):
            pass
    assert denied.value.status_code == 403


def test_same_site_websocket_still_connects(tmp_path):
    client = TestClient(_communal_app(tmp_path))
    with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
        msg = ws.receive_json()
    assert msg["topic"] == "player.state"
    assert msg["data"]["speaker"] is True


async def test_allowed_hosts_pin_the_instance(db, bus):
    """With [web] allowed_hosts set, a request for any other name is refused —
    that is what stops a DNS-rebinding page reaching the appliance."""
    player = PlayerService(PlayerConfig(), db, bus, backend=NullBackend())
    app = create_app(bus, db, player, make_router("dev", bus), allowed_hosts=["radio.local"])
    async with client_for(app) as client:
        assert (await client.get("/api/state", headers={"host": "radio.local"})).status_code == 200
        assert (await client.get("/api/state", headers={"host": "evil.example"})).status_code == 400
    # Unset (the default): any host, as a LAN box reached by IP needs.
    async with client_for(page_app(db, bus)) as client:
        assert (await client.get("/api/state", headers={"host": "192.168.1.20:8080"})).status_code == 200


async def test_public_url_pins_canonical_links(db, bus):
    """A spoofed Host header must not be able to say where the site lives."""
    import re

    def canonical(html):
        return re.search(r'rel="canonical" href="([^"]+)"', html).group(1)

    html = {"accept": "text/html"}
    async with client_for(page_app(db, bus)) as client:
        # Without public_url the request's host is used, over the forwarded scheme…
        page = await client.get("/archive", headers={**html, "host": "radio.local:8080",
                                                     "x-forwarded-proto": "https"})
        assert canonical(page.text) == "https://radio.local:8080/archive"
        # …but only a real scheme is believed.
        page = await client.get("/archive", headers={**html, "x-forwarded-proto": "javascript"})
        assert canonical(page.text) == "http://test/archive"

    player = PlayerService(PlayerConfig(), db, bus, backend=NullBackend())
    app = create_app(bus, db, player, make_router("dev", bus),
                     public_url="https://meshradio.example.org/")
    async with client_for(app) as client:
        page = await client.get("/archive", headers={**html, "host": "evil.example"})
        assert canonical(page.text) == "https://meshradio.example.org/archive"
        sitemap = (await client.get("/sitemap.xml", headers={"host": "evil.example"})).text
        assert "evil.example" not in sitemap
        assert "<loc>https://meshradio.example.org/archive</loc>" in sitemap
