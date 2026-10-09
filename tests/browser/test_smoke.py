"""The pages in a real browser: they load without script errors, and the
controls people use most still do what they say."""

import re

import pytest
from playwright.sync_api import expect

from ..helpers import ADMIN_PASSWORD
from .conftest import DAY, SONGS

PAGES = [
    "/",
    "/archive",
    "/archive/themes",
    f"/archive/{DAY}",
    "/search?q=song",
    "/stats",
    "/member/alice",
    "/artist/Artist%20A",
    "/week",
    "/about",
]


@pytest.mark.parametrize("site", ["appliance", "hosted"])
@pytest.mark.parametrize("path", PAGES)
def test_page_loads_without_script_errors(request, page, site, path):
    server = request.getfixturevalue(site)
    response = page.goto(server.url + path)
    assert response is not None and response.status == 200
    page.wait_for_load_state("networkidle")


def wait_for_video(page, count: int) -> list[str]:
    """The video ids the stand-in YouTube player has loaded, once there are
    at least ``count`` of them. Polled with evaluate: wait_for_function
    evaluates a string, which the page's Content-Security-Policy refuses."""
    loads: list[str] = []
    for _ in range(100):
        loads = page.evaluate("window.__yt ? window.__yt.loads : []")
        if len(loads) >= count:
            return loads
        page.wait_for_timeout(50)
    raise AssertionError(f"expected {count} videos loaded, got {loads}")


def test_hosted_player_plays_skips_and_follows_a_finished_song(hosted, page):
    page.goto(hosted.url + "/")
    page.locator(".controls button").first.click()     # ▶, whichever form it takes
    first = wait_for_video(page, 1)[-1]
    assert first in {video_id for video_id, _, _ in SONGS}

    page.get_by_title("Next track").click()
    loads = wait_for_video(page, 2)
    assert loads[-1] != first

    # The song ends in the player: the page reports it and the next one loads.
    page.evaluate("window.__yt.opts.events.onStateChange({data: YT.PlayerState.ENDED})")
    loads = wait_for_video(page, 3)
    assert loads[-1] != loads[-2]


def test_appliance_equalizer_opens_and_switches(appliance, page):
    page.goto(appliance.url + "/")
    page.get_by_text("Equalizer").click()
    toggle = page.locator("#eq-on")
    expect(toggle).to_be_visible()
    on = "active" in (toggle.get_attribute("class") or "")
    toggle.click()
    if on:
        expect(toggle).not_to_have_class(re.compile(r"\bactive\b"))
    else:
        expect(toggle).to_have_class(re.compile(r"\bactive\b"))


def test_admin_signs_in_retitles_a_day_and_guards_a_removal(appliance, page):
    page.goto(appliance.url + "/admin")
    page.get_by_label("Password").fill(ADMIN_PASSWORD)
    page.get_by_role("button", name="Sign in").click()
    expect(page).to_have_url(re.compile(r"/admin/?$"))

    page.goto(f"{appliance.url}/admin/days/{DAY}")
    page.get_by_label("Title").fill("songs about boats")
    page.get_by_role("button", name="Save title").click()
    expect(page.get_by_label("Title")).to_have_value("songs about boats")

    page.goto(appliance.url + "/admin/log")
    expect(page.get_by_text("songs about boats").first).to_be_visible()

    page.goto(f"{appliance.url}/admin/days/{DAY}")
    page.get_by_role("link", name="Remove", exact=True).first.click()
    remove = page.get_by_role("button", name="Remove song")
    expect(remove).to_be_disabled()
    page.get_by_label(re.compile("to confirm")).fill(DAY)
    expect(remove).to_be_enabled()
