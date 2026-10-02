"""Every player-state push makes the index page refresh its live regions.
That used to be three requests (now-playing, queue, day-nav), two of which
rebuilt the day context; it is one request that swaps all three out-of-band."""

import re

from .helpers import client_for, counting, make_ready_on, page_app


async def test_index_fetches_one_live_partial_per_state_event(db, bus):
    await make_ready_on(db, "aaaaaaaaaaa", "2026-08-01")
    async with client_for(page_app(db, bus)) as client:
        page = (await client.get("/")).text
    sync = re.search(r'<div id="live-sync"[^>]*>', page).group(0)
    assert 'hx-get="/partials/live"' in sync
    assert 'hx-trigger="meshradio:state from:body"' in sync
    assert 'hx-swap="none"' in sync                            # only the oob swaps land
    # The containers exist for the swaps but fetch nothing themselves.
    for target in ("now-playing", "queue", "day-nav"):
        assert f'<div id="{target}">' in page
    assert "/partials/now-playing" not in page
    assert "/partials/day-nav" not in page
    assert 'hx-get="/partials/queue"' not in page


async def test_live_partial_swaps_all_three_regions_from_one_day_context(db, bus):
    await make_ready_on(db, "aaaaaaaaaaa", "2026-08-01")
    await make_ready_on(db, "bbbbbbbbbbb", "2026-08-01")
    app = page_app(db, bus)
    async with client_for(app) as client:
        await client.post("/api/play-day/2026-08-01")
        day_lookups = counting(db, "themes_for_day")
        body = (await client.get("/partials/live")).text
    for target in ("now-playing", "queue", "day-nav"):
        assert f'<div id="{target}" hx-swap-oob="innerHTML">' in body
    assert "bbbbbbbbbbb" in body                               # the queue is in there
    assert 'id="pb-scrub"' in body                             # and the play bar
    assert day_lookups["n"] == 1                               # built once, not per region
