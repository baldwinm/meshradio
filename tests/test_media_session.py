"""Lock-screen controls (mediasession.js).

The behavior — metadata, handlers, the speaker-tab rule — is browser-only and
was checked in real Chromium against both the embed and the web backends. This
pins what a Python test can: the pieces it leans on are still where it
expects them. (That it ships on every page is in test_web_delivery.)"""

from pathlib import Path

import meshradio.web as web

JS = Path(web.__file__).parent / "static" / "js"


def test_seeking_from_the_os_shares_the_scrub_bars_path():
    """One function moves the local player *and* tells the server; a second
    copy in the media handlers would be the one that drifts."""
    playbar = (JS / "playbar.js").read_text()
    assert "function seekTo(pos)" in playbar
    assert "seekTo(+el.value)" in playbar          # the scrub bar goes through it
    assert "seekTo(" in (JS / "mediasession.js").read_text()


def test_handlers_post_to_the_same_endpoints_as_the_buttons():
    """Not click the buttons: they exist only on Now Playing, while hx-boost
    keeps the music going across every other page."""
    src = (JS / "mediasession.js").read_text()
    assert "/api/pause" in src and "/api/skip" in src
    assert "querySelector" not in src              # no dependence on what's on screen
