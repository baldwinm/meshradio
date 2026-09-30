"""Browser hardening headers, and the Content-Security-Policy they carry.

The policy allows no inline script, which is only honest if the templates
carry none — so this also checks every page and partial for ``on*=``
handlers, and that the scripts that replaced them at least parse."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from meshradio.audio.routing import make_router
from meshradio.config import PlayerConfig
from meshradio.media.player import NullBackend, PlayerService
from meshradio.web.server import content_security_policy, create_app

from .test_archive_calendar import page_app, seed_day
from .test_sessions import client_for, embed_app, make_ready_on

PAGES = ["/", "/archive", "/archive/themes", "/archive/2026-08-01", "/search?q=a",
         "/stats", "/about", "/member/alice", "/nope",
         "/partials/now-playing", "/partials/queue", "/partials/day-nav"]


async def test_every_response_carries_the_headers(db, bus):
    await seed_day(db, "2026-08-01", "aaaaaaaaaaa")
    async with client_for(page_app(db, bus)) as client:
        for path in PAGES + ["/api/state", "/healthz", "/static/style.css", "/robots.txt"]:
            resp = await client.get(path, headers={"accept": "text/html"})
            h = resp.headers
            assert h["x-content-type-options"] == "nosniff", path
            assert h["referrer-policy"] == "strict-origin-when-cross-origin", path
            assert h["x-frame-options"] == "SAMEORIGIN", path
            assert "frame-ancestors 'self'" in h["content-security-policy"], path
            assert "content-security-policy-report-only" not in h


async def test_policy_names_only_what_the_pages_use(db, bus):
    appliance = content_security_policy(embed_mode=False)
    assert "script-src 'self';" in appliance                     # our files, nothing inline
    assert "'unsafe-inline'" not in appliance and "'unsafe-eval'" not in appliance
    assert "img-src 'self' https://i.ytimg.com" in appliance     # video stills
    assert "frame-src https://www.youtube.com" in appliance
    assert "connect-src 'self' ws: wss:" in appliance
    assert "object-src 'none'" in appliance and "base-uri 'self'" in appliance

    hosted = content_security_policy(embed_mode=True)
    assert "script-src 'self' https://www.youtube.com https://cdnjs.buymeacoffee.com" in hosted
    assert "https://fonts.gstatic.com" in hosted

    async with client_for(page_app(db, bus)) as client:
        assert (await client.get("/")).headers["content-security-policy"] == appliance
    async with client_for(embed_app(db, bus)) as client:
        assert (await client.get("/")).headers["content-security-policy"] == hosted


async def test_report_only_and_off_switches(db, bus):
    player = PlayerService(PlayerConfig(), db, bus, backend=NullBackend())
    advisory = create_app(bus, db, player, make_router("dev", bus), csp_report_only=True)
    async with client_for(advisory) as client:
        h = (await client.get("/")).headers
        assert "content-security-policy" not in h
        assert "default-src 'self'" in h["content-security-policy-report-only"]
        assert h["x-content-type-options"] == "nosniff"          # the rest stay on
    off = create_app(bus, db, player, make_router("dev", bus), security_headers=False)
    async with client_for(off) as client:
        h = (await client.get("/")).headers
        assert "content-security-policy" not in h and "x-content-type-options" not in h


async def test_pages_carry_no_inline_script_or_style(db, bus):
    """What makes the policy honest: nothing a page needs is inline."""
    await seed_day(db, "2026-08-01", "aaaaaaaaaaa")
    await make_ready_on(db, "bbbbbbbbbbb", "2026-08-02")
    inline = re.compile(r"""\son[a-z]+\s*=|javascript:|<style\b|\sstyle\s*=""", re.I)
    async with client_for(page_app(db, bus)) as client:
        await client.post("/api/play-day/2026-08-02")             # a full now-playing bar
        for path in PAGES:
            body = (await client.get(path, headers={"accept": "text/html"})).text
            assert not inline.search(body), (path, inline.search(body).group(0))
    # The meta config that keeps htmx from injecting its indicator <style>.
    async with client_for(page_app(db, bus)) as client:
        assert '"includeIndicatorStyles": false' in (await client.get("/")).text


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_scripts_parse():
    """No build chain means no other syntax check for the client code."""
    js = sorted((Path(__file__).parent.parent / "meshradio/web/static/js").glob("*.js"))
    assert js
    for path in js:
        subprocess.run(["node", "--check", str(path)], check=True)
