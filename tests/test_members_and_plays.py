"""Member pages and the listening record.

``plays`` is the only account of what actually came out of the speakers, as
opposed to what got posted; these cover the queries behind that and the
per-member pages the stats board now links to.
"""

import re
import time

from .test_archive_calendar import page_app
from .test_sessions import client_for


async def share(db, date, video_id, sender, title="Song", artist=None, theme=None,
                set_by=None, source="mesh"):
    """One song posted to a day (creating the day's theme on first use)."""
    row = await db.create_theme(date, theme or f"theme {date}", set_by=set_by)
    track = await db.add_track(
        video_id=video_id, url="u", channel="#music", sender=sender,
        mesh_ts=time.time(), source=source, theme_id=row["id"],
    )
    await db.update_track_metadata(track["id"], title=title, artist=artist, duration=60)
    return await db.track_by_id(track["id"])


async def test_recent_plays_are_newest_first_and_deduped(db):
    one = await share(db, "2026-08-01", "aaaaaaaaaaa", "ana", title="First")
    two = await share(db, "2026-08-01", "bbbbbbbbbbb", "bob", title="Second")
    await db.record_play(one["id"], "speaker")
    await db.record_play(two["id"], "speaker")
    await db.record_play(one["id"], "speaker")      # replayed: one row, not two
    recent = await db.recent_plays()
    assert [r["title"] for r in recent] == ["First", "Second"]
    assert recent[0]["date"] == "2026-08-01"        # carries its archive day
    assert recent[0]["sender"] == "ana"             # …and the row that played


async def test_most_played_counts_plays_not_shares(db):
    once = await share(db, "2026-08-01", "aaaaaaaaaaa", "ana", title="Rarely")
    often = await share(db, "2026-08-01", "bbbbbbbbbbb", "bob", title="Often")
    await db.record_play(once["id"], None)
    for _ in range(3):
        play_id = await db.record_play(often["id"], None)
        await db.mark_play_completed(play_id)
    chart = await db.most_played()
    # A song played once isn't a chart position — it's just the recently-played
    # list again, so the chart starts at two.
    assert [(c["title"], c["plays"], c["finished"]) for c in chart] == [("Often", 3, 3)]
    totals = await db.play_totals()
    assert totals == {"plays": 4, "tracks": 2, "finished": 3}


async def test_most_played_stays_empty_until_something_repeats(db):
    track = await share(db, "2026-08-01", "aaaaaaaaaaa", "ana")
    await db.record_play(track["id"], None)
    assert await db.most_played() == []


async def test_play_totals_survive_an_empty_history(db):
    assert await db.play_totals() == {"plays": 0, "tracks": 0, "finished": 0}
    assert await db.recent_plays() == []


async def test_stats_page_shows_the_listening_record(db, bus):
    track = await share(db, "2026-08-01", "aaaaaaaaaaa", "ana", title="Test tone")
    for _ in range(2):
        play_id = await db.record_play(track["id"], "speaker")
        await db.mark_play_completed(play_id)
    async with client_for(page_app(db, bus)) as client:
        body = (await client.get("/stats")).text
    assert "Recently played" in body and "Most played" in body
    assert "2 plays · 100% finished" in body
    assert 'href="/member/ana"' in body            # sharers are explorable now


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
