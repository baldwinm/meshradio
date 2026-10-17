"""What the listener pages put first and how they say it: the day's theme as
Now Playing's headline, the YouTube player as the card's art on the public
site, a queue that is just songs until ✎ Edit, plain words when nothing is playing, and
stat tiles that never show a zero or a "1 shares"."""

import re

from meshradio.web.templating import day_label

from .helpers import client_for, embed_app, make_ready_on, page_app, share


def test_day_label_says_a_day_the_way_people_do():
    assert day_label("2026-10-16") == "Fri, Oct 16"
    assert day_label("2026-10-16", long=True) == "Friday, October 16, 2026"
    assert day_label("not a day") == "not a day"          # shown as it came, never a 500
    assert day_label(None) == ""


async def test_the_days_theme_is_now_playings_headline(db, bus):
    track = await share(db, "2026-08-01", "aaaaaaaaaaa", "ana", title="Mystery Train",
                        theme="Songs about trains")
    await db.set_cache_status(track["id"], "ready")
    async with client_for(page_app(db, bus)) as client:
        await client.post("/api/play-day/2026-08-01")
        body = (await client.get("/")).text
    assert '<h2 class="day-theme">Songs about trains</h2>' in body
    assert '<span class="day-date">Sat, Aug 1</span>' in body


async def test_on_the_public_site_the_player_is_the_cards_art(db, bus):
    await make_ready_on(db, "aaaaaaaaaaa", "2026-08-01")
    async with client_for(embed_app(db, bus)) as client:
        await client.post("/api/play-day/2026-08-01")
        page = (await client.get("/")).text
        stats = (await client.get("/stats")).text
    assert '<main class="page-now">' in page
    assert 'id="yt-slot"' in page                          # where dock.js lays the player
    assert "/static/js/dock.js" in page
    assert '<main class="page-other">' in stats            # docked everywhere else


async def test_the_pi_keeps_its_cover_art_and_no_player_window_script(db, bus):
    await make_ready_on(db, "aaaaaaaaaaa", "2026-08-01")
    async with client_for(page_app(db, bus)) as client:
        await client.post("/api/play-day/2026-08-01")
        body = (await client.get("/")).text
    assert 'id="yt-slot"' not in body and 'class="art"' in body
    assert "/static/js/dock.js" not in body


async def test_nothing_playing_says_what_to_do_not_the_mode(db, bus):
    async with client_for(page_app(db, bus)) as client:
        body = (await client.get("/")).text
    assert "Nothing playing" in body
    assert "mode: idle" not in body and "waiting for the mesh" not in body
    assert "Press ▶ to play" in body and 'href="/archive"' in body


async def test_queue_rows_carry_no_buttons_and_one_edit_switch_does(db, bus):
    """Listening, the queue is just songs: no per-row buttons. Edit mode's
    boxes, handles and action bar are in the markup, shown by queue.js."""
    for video_id in ("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"):
        await make_ready_on(db, video_id, "2026-08-01")
    async with client_for(page_app(db, bus)) as client:
        await client.post("/api/play-day/2026-08-01")
        body = (await client.get("/partials/live")).text
    rows = re.findall(r"<li data-track-id=.*?</li>", body, re.S)
    assert len(rows) == 2                                  # the playing song isn't queued
    assert not any("<button" in row for row in rows)
    assert all('class="q-check"' in row and 'class="q-handle"' in row for row in rows)
    assert body.count('class="subtle q-edit"') == 1
    bar = re.search(r'<div class="q-editbar".*?</div>', body, re.S).group(0)
    for action in ("q-next", "q-remove", "q-all", "q-clear", "q-done"):
        assert action in bar


async def test_keep_playing_is_a_switch_that_shows_its_state(db, bus):
    await make_ready_on(db, "aaaaaaaaaaa", "2026-08-01")
    async with client_for(page_app(db, bus)) as client:
        await client.post("/api/play-day/2026-08-01")
        off = (await client.get("/partials/live")).text
        await client.post("/api/station/archive")
        on = (await client.get("/partials/live")).text

    def keep_playing(body):
        return re.search(r'<button class="toggle"[^>]*>(?:(?!</button>).)*Keep playing</button>',
                         body, re.S).group(0)

    assert 'aria-pressed="false"' in keep_playing(off)
    assert 'aria-pressed="true"' in keep_playing(on)
    assert "◼" not in on                                   # no "stop" glyph on a switch


async def test_stat_tiles_skip_zeros_and_say_one_in_the_singular(db, bus):
    await share(db, "2026-08-01", "aaaaaaaaaaa", "ana", title="Only", artist="Nilsson")
    async with client_for(page_app(db, bus)) as client:
        body = (await client.get("/stats")).text
    tiles = dict(
        (label, int(n))
        for n, label in re.findall(r'stat-n">(\d+)</span><span class="stat-l">([\w ]+)<', body)
    )
    assert tiles == {"song": 1, "share": 1, "sharer": 1, "day": 1, "theme": 1}   # no "0 plays"
    assert "1 shares" not in body and "1 share ·" in body


async def test_an_archive_day_with_one_theme_headlines_it(db, bus):
    await share(db, "2026-08-01", "aaaaaaaaaaa", "ana", title="One", theme="Rain songs",
                set_by="ana")
    async with client_for(page_app(db, bus)) as client:
        body = (await client.get("/archive/2026-08-01")).text
    assert '<h2 class="day-title">Rain songs</h2>' in body
    assert "Saturday, August 1, 2026" in body
    assert "theme set by" in body
    assert 'class="theme-title"' not in body               # not said twice


async def test_queue_buttons_say_what_they_did(db, bus):
    await make_ready_on(db, "aaaaaaaaaaa", "2026-08-01", title="Mystery Train")
    async with client_for(page_app(db, bus)) as client:
        body = (await client.get("/archive/2026-08-01")).text
    button = re.search(r'<button class="add-btn".*?</button>', body, re.S).group(0)
    assert 'data-toast="Queued: Mystery Train"' in button
    assert 'data-done="✓"' in button
    assert 'aria-label="Add Mystery Train to the queue"' in button
