"""Member pages and the listening record.

``plays`` is the only account of what actually came out of the speakers, as
opposed to what got posted; these cover the queries behind that and the
per-member pages the stats board now links to.
"""

import re
import time

from .helpers import client_for, page_app, share


async def test_play_totals_count_what_was_played(db):
    assert await db.play_totals() == {"plays": 0, "tracks": 0, "finished": 0}
    once = await share(db, "2026-08-01", "aaaaaaaaaaa", "ana", title="Rarely")
    often = await share(db, "2026-08-01", "bbbbbbbbbbb", "bob", title="Often")
    await db.record_play(once["id"], None)
    for _ in range(3):
        play_id = await db.record_play(often["id"], None)
        await db.mark_play_completed(play_id)
    assert await db.play_totals() == {"plays": 4, "tracks": 2, "finished": 3}


async def test_stats_page_counts_plays_and_links_members(db, bus):
    track = await share(db, "2026-08-01", "aaaaaaaaaaa", "ana", title="Test tone")
    await db.record_play(track["id"], "speaker")
    async with client_for(page_app(db, bus)) as client:
        body = (await client.get("/stats")).text
    assert 'class="stat-l">plays' in body
    assert 'href="/member/ana"' in body            # sharers are explorable
    # The per-song listening lists were pulled; the tile is all that remains.
    assert "Recently played" not in body and "Most played" not in body


async def test_member_page_gathers_a_members_record(db, bus):
    await share(db, "2026-08-01", "aaaaaaaaaaa", "ana", title="One",
                artist="Nilsson", theme="Rain songs", set_by="ana")
    await share(db, "2026-08-02", "bbbbbbbbbbb", "ana", title="Two", artist="Nilsson")
    await share(db, "2026-08-02", "ccccccccccc", "bob", title="Three")
    async with client_for(page_app(db, bus)) as client:
        body = (await client.get("/member/ana")).text
    assert "<h2>ana</h2>" in body
    assert "One" in body and "Two" in body and "Three" not in body   # theirs only
    assert "Rain songs" in body                    # the day they named
    assert "Nilsson" in body                       # on repeat
    assert 'href="/archive/2026-08-01"' in body
    tiles = re.findall(r'stat-n">(\d+)</span><span class="stat-l">(\w+)', body)
    assert dict((label, int(n)) for n, label in tiles) == {
        "shares": 2, "songs": 2, "days": 2, "themes": 1,
    }


async def test_member_lookup_is_case_insensitive_and_keeps_their_spelling(db, bus):
    await share(db, "2026-08-01", "aaaaaaaaaaa", "AnaB")
    await share(db, "2026-08-02", "bbbbbbbbbbb", "AnaB")
    await share(db, "2026-08-03", "ccccccccccc", "anab")   # same person, retyped
    async with client_for(page_app(db, bus)) as client:
        resp = await client.get("/member/ANAB")
    assert resp.status_code == 200
    assert "<h2>AnaB</h2>" in resp.text             # the spelling they use most
    assert "<h2>ANAB</h2>" not in resp.text         # not the one in the URL


async def test_unknown_member_is_a_404(db, bus):
    await share(db, "2026-08-01", "aaaaaaaaaaa", "ana")
    async with client_for(page_app(db, bus)) as client:
        resp = await client.get("/member/ghost42", headers={"accept": "text/html"})
    assert resp.status_code == 404
    assert "ghost42" not in resp.text               # never echo the path back
    assert "Not found" in resp.text


async def test_radio_filler_is_not_a_members_share(db, bus):
    """Mix continuations carry the seed's name but nobody posted them."""
    await share(db, "2026-08-01", "aaaaaaaaaaa", "ana", title="Theirs")
    filler = await db.add_track(
        video_id="ddddddddddd", url="u", channel="#music", sender="ana",
        mesh_ts=time.time(), source="radio", theme_id=None,
    )
    await db.update_track_metadata(filler["id"], title="Filler")
    async with client_for(page_app(db, bus)) as client:
        body = (await client.get("/member/ana")).text
    assert "Theirs" in body and "Filler" not in body


async def test_a_member_name_with_a_slash_has_a_page(db, bus):
    """Mesh names can carry a slash. The day page links it encoded; uvicorn
    decodes the path before routing, so the route has to take a path."""
    await share(db, "2026-08-01", "aaaaaaaaaaa", "Node/K5ABC")
    async with client_for(page_app(db, bus)) as client:
        for path in ("/member/Node%2FK5ABC", "/member/Node/K5ABC"):
            page = await client.get(path)
            assert page.status_code == 200 and "Node/K5ABC" in page.text, path
        href = re.search(r'href="(/member/[^"]+)"', (await client.get("/archive/2026-08-01")).text)
        assert href and (await client.get(href.group(1))).status_code == 200
        assert (await client.get("/member/nobody/here")).status_code == 404
