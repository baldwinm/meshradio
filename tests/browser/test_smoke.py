"""The pages in a real browser: they load without script errors, and the
controls people use most still do what they say."""

import re

import pytest
from playwright.sync_api import expect

from ..helpers import ADMIN_PASSWORD
from .conftest import DAY, EARLIER, SONGS

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
    "/privacy",
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


def test_pausing_a_day_played_from_the_archive_stays_on_it(hosted, page):
    """Archive → an earlier day → ▶ Play this day, then pause: the player
    pauses that day's song instead of swapping in the newest day's first
    song and playing it."""
    page.goto(hosted.url + f"/archive/{EARLIER}")
    page.get_by_role("button", name="Play this day").click()
    page.wait_for_url(hosted.url + "/")
    assert wait_for_video(page, 1)[-1] == "ddddddddddd"

    page.get_by_title("Play/Pause").click()
    expect(page.get_by_title("Play/Pause")).to_have_text("▶")
    page.wait_for_load_state("networkidle")
    assert page.evaluate("window.__yt.loads") == ["ddddddddddd"]
    assert page.evaluate("window.__yt.getPlayerState()") == 2          # paused
    expect(page.locator("#now-playing")).to_contain_text("Song D")


def test_now_playing_text_scrolls_only_when_it_does_not_fit(hosted, page):
    page.goto(hosted.url + "/")
    page.locator(".controls button").first.click()
    wait_for_video(page, 1)
    page.wait_for_load_state("networkidle")       # the card's re-renders after play
    title = page.locator(".title.marquee")
    expect(title).not_to_have_class(re.compile(r"\bscrolling\b"))   # "Song A" fits

    long = "A very long song title that no phone screen could ever show in one line"
    page.evaluate(f"""() => {{
        document.querySelector('.title.marquee span').textContent = {long!r};
        window.dispatchEvent(new Event('resize'));
    }}""")
    expect(title).to_have_class(re.compile(r"\bscrolling\b"))

    page.evaluate("""() => {
        document.querySelector('.title.marquee span').textContent = 'Short';
        window.dispatchEvent(new Event('resize'));
    }""")
    expect(title).not_to_have_class(re.compile(r"\bscrolling\b"))


PHONE = {"width": 390, "height": 844}


@pytest.mark.parametrize("site", ["appliance", "hosted"])
@pytest.mark.parametrize("path", ["/", "/archive", f"/archive/{DAY}", "/search?q=song", "/stats"])
def test_phone_pages_never_scroll_sideways(request, page, site, path):
    """At a phone's width nothing runs off the side: the header's tabs, the
    song rows and the search form all fit."""
    server = request.getfixturevalue(site)
    page.set_viewport_size(PHONE)
    page.goto(server.url + path)
    page.wait_for_load_state("networkidle")
    assert page.evaluate("document.documentElement.scrollWidth") <= PHONE["width"]


def queue_titles(page):
    return page.locator("#queue .queue > li .title").all_inner_texts()


def open_queue_editor(server, page):
    """Play DAY (Song A plays; B and C queue), then press ✎ Edit."""
    page.goto(server.url + f"/archive/{DAY}")
    page.get_by_role("button", name="Play this day").click()
    page.wait_for_url(server.url + "/")
    page.wait_for_load_state("networkidle")
    expect(page.locator("#queue .queue > li")).to_have_count(2)
    page.get_by_role("button", name="✎ Edit").click()
    expect(page.locator("#queue")).to_have_class(re.compile(r"\bediting\b"))


def test_queue_rows_have_no_buttons_until_edit(hosted, page):
    page.goto(hosted.url + f"/archive/{DAY}")
    page.get_by_role("button", name="Play this day").click()
    page.wait_for_url(hosted.url + "/")
    rows = page.locator("#queue .queue > li")
    expect(rows).to_have_count(2)
    expect(rows.first.locator(".q-check")).to_be_hidden()
    expect(rows.first.locator(".q-handle")).to_be_hidden()
    expect(page.locator(".q-editbar")).to_be_hidden()


def test_edit_mode_removes_the_ticked_songs_and_undoes_it(hosted, page):
    open_queue_editor(hosted, page)
    before = queue_titles(page)
    page.get_by_label(f"Select {before[0]}").check()
    expect(page.locator(".q-count")).to_have_text("1 selected")
    page.locator(".q-remove").click()
    expect(page.locator("#queue .queue > li")).to_have_count(1)
    toast = page.locator(".mr-toast.has-action")
    expect(toast).to_contain_text("Removed 1 song")
    toast.get_by_role("button", name="Undo").click()
    expect(page.locator("#queue .queue > li")).to_have_count(2)
    assert queue_titles(page) == before


def test_edit_mode_moves_songs_by_drag_and_by_keyboard(hosted, page):
    open_queue_editor(hosted, page)
    first, second = queue_titles(page)
    handle = page.locator("#queue .queue > li").nth(1).locator(".q-handle")
    target = page.locator("#queue .queue > li").first.bounding_box()
    box = handle.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2, target["y"] + 2, steps=8)
    page.mouse.up()
    expect(page.locator("#queue .queue > li .title").first).to_have_text(second)

    page.get_by_label(f"Select {second}").focus()
    page.keyboard.press("Alt+ArrowDown")
    expect(page.locator("#queue .queue > li .title").first).to_have_text(first)

    page.keyboard.press("Escape")
    expect(page.locator("#queue")).not_to_have_class(re.compile(r"\bediting\b"))


def test_phone_edit_bar_sits_at_the_bottom_of_the_screen(hosted, page):
    page.set_viewport_size(PHONE)
    open_queue_editor(hosted, page)
    bar = page.locator(".q-editbar").bounding_box()
    assert bar and abs(bar["y"] + bar["height"] - PHONE["height"]) < 2
    assert page.evaluate("document.documentElement.scrollWidth") <= PHONE["width"]


def test_queueing_from_the_archive_says_it_worked(hosted, page):
    page.goto(hosted.url + f"/archive/{EARLIER}")
    button = page.get_by_role("button", name="Add Song D to the queue")
    button.click()
    expect(button).to_have_text("✓")
    expect(button).to_be_disabled()
    expect(page.locator(".mr-toast")).to_contain_text("Queued: Song D")


def test_the_player_sits_in_the_card_and_docks_elsewhere(hosted, page):
    page.goto(hosted.url + "/")
    page.locator(".controls button").first.click()
    wait_for_video(page, 1)
    page.wait_for_load_state("networkidle")
    win = page.locator("#yt-window")
    expect(win).to_have_class(re.compile(r"\bin-slot\b"))
    slot = page.locator("#yt-slot").bounding_box()
    box = win.bounding_box()
    assert slot and box
    assert abs(slot["x"] - box["x"]) < 2 and abs(slot["y"] - box["y"]) < 2
    assert box["height"] >= 199                    # YouTube's smallest player

    page.get_by_role("link", name="Archive").first.click()
    page.wait_for_url(hosted.url + "/archive")
    expect(win).not_to_have_class(re.compile(r"\bin-slot\b"))
    assert win.evaluate("el => getComputedStyle(el).position") == "fixed"
    assert page.evaluate("window.__yt.loads.length") == 1    # the song never reloaded
