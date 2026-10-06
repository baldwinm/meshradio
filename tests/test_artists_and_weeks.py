"""Artist pages and the weekly recap — the archive sliced by who made a song
and by Sunday-to-Saturday week, rather than by the day it was posted."""

import re
import time
import xml.etree.ElementTree as ET

import pytest

from meshradio.db.browse import artist_name
from meshradio.web import recap
from meshradio.web.recap import finished_weeks, summarize_week, week_label, week_start

from .helpers import client_for, page_app, share

NS = {"a": "http://www.w3.org/2005/Atom"}


def tiles(body):
    found = re.findall(r'stat-n">(\d+)</span><span class="stat-l">(\w+)', body)
    return {label: int(n) for n, label in found}


# -- artists ------------------------------------------------------------------


@pytest.mark.parametrize("raw, shown", [
    ("Nilsson - Topic", "Nilsson"),
    ("nilsson - topic", "nilsson"),
    ("  Nilsson  ", "Nilsson"),
    ("NilssonVEVO", "NilssonVEVO"),
    (None, ""),
])
def test_artist_name_folds_the_topic_suffix(raw, shown):
    assert artist_name(raw) == shown


async def test_artist_page_gathers_songs_sharers_and_themes(db, bus):
    await share(db, "2026-08-01", "aaaaaaaaaaa", "ana", title="One",
                artist="Nilsson - Topic", theme="Rain songs")
    await share(db, "2026-08-02", "aaaaaaaaaaa", "bob", title="One", artist="Nilsson")
    await share(db, "2026-08-02", "bbbbbbbbbbb", "ana", title="Two", artist="Nilsson")
    await share(db, "2026-08-02", "ccccccccccc", "ana", title="Other", artist="Someone")
    async with client_for(page_app(db, bus)) as client:
        body = (await client.get("/artist/Nilsson")).text
    assert "<h2>Nilsson</h2>" in body              # Topic and plain channels merge
    assert tiles(body) == {"shares": 3, "songs": 2, "sharers": 2, "days": 2}
    assert "One" in body and "Two" in body and "Other" not in body
    assert 'href="/member/ana"' in body and 'href="/member/bob"' in body
    assert "Rain songs" in body
    assert "watch_videos?video_ids=aaaaaaaaaaa,bbbbbbbbbbb" in body


async def test_artist_lookup_is_case_insensitive_and_takes_the_topic_form(db, bus):
    await share(db, "2026-08-01", "aaaaaaaaaaa", "ana", artist="Nilsson")
    async with client_for(page_app(db, bus)) as client:
        for path in ("/artist/nilsson", "/artist/Nilsson%20-%20Topic"):
            resp = await client.get(path)
            assert resp.status_code == 200 and "<h2>Nilsson</h2>" in resp.text, path


async def test_unknown_artist_and_radio_filler_are_404s(db, bus):
    await share(db, "2026-08-01", "aaaaaaaaaaa", "ana", artist="Nilsson")
    filler = await db.add_track(
        video_id="ddddddddddd", url="u", channel="#music", sender="ana",
        mesh_ts=time.time(), source="radio", theme_id=None,
    )
    await db.update_track_metadata(filler["id"], title="Filler", artist="Mix Only")
    async with client_for(page_app(db, bus)) as client:
        for path in ("/artist/ghost42", "/artist/Mix%20Only"):
            resp = await client.get(path, headers={"accept": "text/html"})
            assert resp.status_code == 404, path
            assert "ghost42" not in resp.text


async def test_artists_link_from_days_members_and_stats(db, bus):
    await share(db, "2026-08-01", "aaaaaaaaaaa", "ana", artist="Nilsson - Topic")
    async with client_for(page_app(db, bus)) as client:
        for path in ("/archive/2026-08-01", "/member/ana", "/stats"):
            body = (await client.get(path)).text
            href = re.search(r'href="(/artist/[^"]+)"', body)
            assert href, path
            assert (await client.get(href.group(1))).status_code == 200, path
    assert (await db.top_artists())[0]["artist"] == "Nilsson"


# -- weeks: the pure parts ----------------------------------------------------


@pytest.mark.parametrize("day, sunday", [
    ("2026-10-04", "2026-10-04"),      # a Sunday is its own week
    ("2026-10-10", "2026-10-04"),      # Saturday closes it
    ("2026-10-11", "2026-10-11"),
    ("2026-01-01", "2025-12-28"),      # across a year
])
def test_week_start_is_the_sunday(day, sunday):
    assert week_start(day) == sunday


@pytest.mark.parametrize("start, label", [
    ("2026-10-04", "Oct 4 – 10, 2026"),
    ("2026-09-27", "Sep 27 – Oct 3, 2026"),
    ("2025-12-28", "Dec 28, 2025 – Jan 3, 2026"),
])
def test_week_label(start, label):
    assert week_label(start) == label


def test_only_weeks_already_over_go_in_the_feed():
    starts = ["2026-09-20", "2026-09-27", "2026-10-04"]
    assert finished_weeks("2026-10-06", starts) == ["2026-09-27", "2026-09-20"]
    assert finished_weeks("2026-10-11", starts, limit=1) == ["2026-10-04"]


def row(date, vid, sender, theme="t", artist=None, set_by=None):
    return {"date": date, "theme_title": theme, "set_by": set_by, "video_id": vid,
            "title": vid, "artist": artist, "sender": sender, "mesh_ts": 0.0}


def test_summary_counts_the_week_and_spots_newcomers():
    rows = [
        row("2026-10-04", "a", "Ana", "Rain", set_by="Ana", artist="X - Topic"),
        row("2026-10-04", "b", "bob", "Rain", artist="X"),
        row("2026-10-06", "c", "ana", "Untitled — 2026-10-06"),
        row("2026-10-06", "a", "cy", "Untitled — 2026-10-06"),
    ]
    firsts = {"ana": "2026-08-01", "bob": "2026-10-04", "cy": "2026-10-06"}
    week = summarize_week("2026-10-04", rows, firsts, {"a": "2026-08-01", "b": "2026-10-04"})
    assert (week["shares"], week["songs"], week["sharers"]) == (4, 3, 3)
    assert [(d["weekday"], d["themes"], d["tracks"]) for d in week["days"]] == [
        ("Sun", ["Rain"], 2), ("Tue", [], 2),          # a placeholder is no theme
    ]
    assert week["days"][0]["set_by"] == "Ana"
    assert week["top_sharers"][0] == {"sender": "Ana", "shares": 2}   # retyped name, one member
    assert week["new_faces"] == ["bob", "cy"]
    assert week["top_artists"] == [{"artist": "X", "shares": 2}]
    assert [s["video_id"] for s in week["heard_before"]] == ["a"]
    assert week["busiest"]["date"] == "2026-10-04"     # tie goes to the earlier day


# -- weeks: the pages ---------------------------------------------------------


async def test_week_page_sums_up_the_week(db, bus):
    await share(db, "2026-09-30", "zzzzzzzzzzz", "ana")                       # the week before
    await share(db, "2026-10-04", "aaaaaaaaaaa", "ana", theme="Rain", set_by="ana",
                artist="Nilsson")
    await share(db, "2026-10-05", "bbbbbbbbbbb", "newbie", theme="Sun")
    await share(db, "2026-10-05", "zzzzzzzzzzz", "bob", theme="Sun", title="Again")
    async with client_for(page_app(db, bus)) as client:
        body = (await client.get("/week/2026-10-04")).text
    assert "Week of Oct 4 – 10, 2026" in body
    assert tiles(body) == {"shares": 3, "songs": 3, "sharers": 3, "days": 2}
    assert "Rain" in body and "Sun" in body
    assert "New on the channel" in body and 'href="/member/newbie"' in body
    assert 'href="/artist/Nilsson"' in body
    assert "Heard before" in body and "first on 2026-09-30" in body
    assert 'href="/week/2026-09-27" rel="prev"' in body
    assert 'rel="next"' not in body


async def test_any_day_of_a_week_redirects_to_its_sunday(db, bus):
    await share(db, "2026-10-07", "aaaaaaaaaaa", "ana")
    async with client_for(page_app(db, bus)) as client:
        resp = await client.get("/week/2026-10-07")
        assert resp.status_code == 301 and resp.headers["location"] == "/week/2026-10-04"
        for bad in ("/week/2026-10-18", "/week/nonsense", "/week/2026-13-45"):
            assert (await client.get(bad)).status_code == 404, bad


async def test_week_without_a_date_is_the_newest_and_points_at_it(db, bus):
    await share(db, "2026-09-30", "aaaaaaaaaaa", "ana")
    await share(db, "2026-10-07", "bbbbbbbbbbb", "ana")
    async with client_for(page_app(db, bus)) as client:
        body = (await client.get("/week")).text
        assert "Week of Oct 4 – 10, 2026" in body
        assert '<link rel="canonical" href="http://test/week/2026-10-04">' in body
        day = (await client.get("/archive/2026-10-07")).text
        assert 'href="/week/2026-10-04"' in day        # a day links its week
        assert "http://test/week/2026-09-27" in (await client.get("/sitemap.xml")).text


async def test_weekly_feed_carries_finished_weeks_only(db, bus, monkeypatch):
    await share(db, "2026-09-22", "aaaaaaaaaaa", "ana", theme="Rain")
    await share(db, "2026-09-29", "bbbbbbbbbbb", "bob", theme="Sun")
    await share(db, "2026-10-05", "ccccccccccc", "ana", theme="Not yet")
    app = page_app(db, bus)
    monkeypatch.setattr(app.state.ctx, "today", lambda: "2026-10-06")
    async with client_for(app) as client:
        resp = await client.get("/weekly.xml")
    assert resp.headers["content-type"].startswith("application/atom+xml")
    root = ET.fromstring(resp.text)
    entries = root.findall("a:entry", NS)
    assert [e.find("a:title", NS).text for e in entries] == [
        "Week of Sep 27 – Oct 3, 2026", "Week of Sep 20 – 26, 2026",
    ]
    first = entries[0]
    assert first.find("a:id", NS).text == "http://test/week/2026-09-27"
    assert first.find("a:updated", NS).text == "2026-10-04T00:00:00Z"
    assert "Sun" in first.find("a:summary", NS).text
    assert "New this week: bob" in first.find("a:content", NS).text


def test_weekly_feed_is_well_formed_when_empty_and_with_control_chars():
    week = summarize_week("2026-10-04", [row("2026-10-04", "a", "an\x07a", "Ra\x01in")], {})
    text = recap.build_weekly_feed(
        [week], feed_url="f", site_url="s", week_url=str, day_url=str,
    )
    assert ET.fromstring(text).find("a:entry", NS) is not None
    empty = recap.build_weekly_feed([], feed_url="f", site_url="s", week_url=str, day_url=str)
    assert ET.fromstring(empty).find("a:entry", NS) is None
