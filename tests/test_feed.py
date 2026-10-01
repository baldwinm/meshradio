"""/feed.xml: the channel's days as an Atom feed, one entry per day."""

import xml.etree.ElementTree as ET
from datetime import UTC, datetime

from meshradio.ingest.parse import untitled_theme
from meshradio.web.feed import FEED_DAYS, SONGS_PER_ENTRY, _stamp, build_feed

from .test_archive_calendar import page_app
from .test_sessions import client_for

NS = {"a": "http://www.w3.org/2005/Atom"}
T0 = 1_785_000_000.0      # a fixed mesh time, so `updated` is checkable


async def add_day(db, date, title, songs, *, sender="alice", t0=T0):
    """A day with a theme and ``songs`` — (video_id, title, artist) triples."""
    theme = await db.create_theme(date, title, locked=True)
    for i, (vid, name, artist) in enumerate(songs):
        await db.add_track(
            video_id=vid, url="u", channel="#music", sender=sender, mesh_ts=t0 + i * 90,
            source="mesh", theme_id=theme["id"], title=name, artist=artist,
        )
    return theme


def parse(text):
    return ET.fromstring(text)


async def fetch(db, bus, **kw):
    async with client_for(page_app(db, bus)) as client:
        return await client.get("/feed.xml", **kw)


async def test_feed_is_well_formed_atom_newest_day_first(db, bus):
    await add_day(db, "2026-08-01", "rain songs", [("aaaaaaaaaaa", "Rain", "Ann")])
    await add_day(db, "2026-08-02", "one hit wonders", [("bbbbbbbbbbb", "Hit", "Bo")])
    resp = await fetch(db, bus)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/atom+xml")
    root = parse(resp.text)
    assert root.tag == "{http://www.w3.org/2005/Atom}feed"
    entries = root.findall("a:entry", NS)
    assert [e.find("a:title", NS).text for e in entries] == [
        "2026-08-02 — one hit wonders", "2026-08-01 — rain songs",
    ]
    # An entry's id and link are the day's page, so a reader recognises it again.
    first = entries[0]
    assert first.find("a:id", NS).text == "http://test/archive/2026-08-02"
    assert first.find("a:link", NS).get("href") == "http://test/archive/2026-08-02"
    assert root.find("a:id", NS).text == "http://test/feed.xml"
    rels = {link.get("rel"): link.get("href") for link in root.findall("a:link", NS)}
    assert rels == {"self": "http://test/feed.xml", "alternate": "http://test/"}
    assert root.find("a:author/a:name", NS).text == "MeshRadio"


async def test_entry_lists_the_songs_with_the_days_first_cover(db, bus):
    await add_day(db, "2026-08-01", "rain songs", [
        ("aaaaaaaaaaa", "Rain", "Ann"), ("bbbbbbbbbbb", "Storm", ""),
    ], sender="bob")
    (entry,) = parse((await fetch(db, bus)).text).findall("a:entry", NS)
    assert entry.find("a:summary", NS).text == (
        "2 songs shared on 2026-08-01 for the theme “rain songs” on the Austin mesh #music channel."
    )
    html = entry.find("a:content", NS).text            # the HTML, unescaped by the parse
    assert entry.find("a:content", NS).get("type") == "html"
    assert '<a href="https://youtu.be/aaaaaaaaaaa">Rain</a> — Ann (bob)' in html
    assert '<a href="https://youtu.be/bbbbbbbbbbb">Storm</a> (bob)' in html   # no artist, no dash
    assert html.index("Rain") < html.index("Storm")                          # posted order
    assert "https://i.ytimg.com/vi/aaaaaaaaaaa/mqdefault.jpg" in html        # first song's still


async def test_one_song_is_singular(db, bus):
    await add_day(db, "2026-08-01", "rain songs", [("aaaaaaaaaaa", "Rain", "")])
    (entry,) = parse((await fetch(db, bus)).text).findall("a:entry", NS)
    assert entry.find("a:summary", NS).text.startswith("1 song shared")


async def test_hostile_text_cannot_break_the_document_or_inject_markup(db, bus):
    """Sender names, theme titles and oEmbed titles are all free text. A control
    character would make the feed unparseable — and a reader drops the whole
    feed, not the entry — so it has to be stripped, and markup has to stay text."""
    await add_day(
        db, "2026-08-01", "rain \x08 <b>songs</b> ￾",
        [("aaaaaaaaaaa", "x\x1b<script>alert(1)</script>", "Ann\x0b & \"Co\"")],
        sender="mal\x0cory <i>",
    )
    resp = await fetch(db, bus)
    root = parse(resp.text)                                # raises if not well-formed
    (entry,) = root.findall("a:entry", NS)
    # The row itself now drops the controls (Database.clean_text); the feed's
    # own stripping stays as the backstop for rows written before it did.
    assert entry.find("a:title", NS).text == "2026-08-01 — rain <b>songs</b>"
    html = entry.find("a:content", NS).text
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<i>" not in html and "&lt;i&gt;" in html
    assert "\x08" not in resp.text and "\x1b" not in resp.text


async def test_feed_carries_the_newest_days_only(db, bus):
    for day in range(1, FEED_DAYS + 3):
        await add_day(db, f"2026-07-{day:02d}" if day < 32 else "2026-08-01", f"theme {day}",
                      [(f"{day:011d}", f"Song {day}", "")])
    entries = parse((await fetch(db, bus)).text).findall("a:entry", NS)
    assert len(entries) == FEED_DAYS
    assert entries[0].find("a:id", NS).text.endswith("/archive/2026-08-01")      # newest kept
    assert not any(e.find("a:id", NS).text.endswith("/archive/2026-07-01") for e in entries)


async def test_days_without_songs_and_radio_filler_are_left_out(db, bus):
    await add_day(db, "2026-08-01", "rain songs", [("aaaaaaaaaaa", "Rain", "")])
    await db.create_theme("2026-08-02", "named but silent")          # a day, no songs
    await db.add_track(                                              # station filler, no theme
        video_id="rrrrrrrrrrr", url="u", channel="#music", sender="alice",
        mesh_ts=T0, source="radio", theme_id=None,
    )
    entries = parse((await fetch(db, bus)).text).findall("a:entry", NS)
    assert [e.find("a:title", NS).text for e in entries] == ["2026-08-01 — rain songs"]


async def test_a_placeholder_theme_is_just_the_date(db, bus):
    await add_day(db, "2026-08-01", untitled_theme("2026-08-01"), [("aaaaaaaaaaa", "Rain", "")])
    (entry,) = parse((await fetch(db, bus)).text).findall("a:entry", NS)
    assert entry.find("a:title", NS).text == "2026-08-01"
    assert "Untitled" not in entry.find("a:summary", NS).text


async def test_updated_is_the_days_newest_song_and_does_not_churn(db, bus):
    """A reader shows an entry as changed when `updated` moves. It must move only
    when a song lands that day — not on every poll."""
    await add_day(db, "2026-08-01", "rain songs",
                  [("aaaaaaaaaaa", "Rain", ""), ("bbbbbbbbbbb", "Storm", "")])
    newest = datetime.fromtimestamp(T0 + 90, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    first = await fetch(db, bus)
    second = await fetch(db, bus)
    assert first.text == second.text
    root = parse(first.text)
    assert root.find("a:entry/a:updated", NS).text == newest
    assert root.find("a:updated", NS).text == newest


async def test_an_empty_archive_is_still_a_valid_feed(db, bus):
    root = parse((await fetch(db, bus)).text)
    assert root.findall("a:entry", NS) == []
    assert root.find("a:updated", NS).text.endswith("Z")            # Atom requires one


async def test_a_huge_day_is_cut_off_and_says_so(db, bus):
    count = SONGS_PER_ENTRY + 5
    await add_day(db, "2026-08-01", "many songs",
                  [(f"{i:011d}", f"Song {i}", "") for i in range(count)])
    (entry,) = parse((await fetch(db, bus)).text).findall("a:entry", NS)
    html = entry.find("a:content", NS).text
    assert html.count("<li>") == SONGS_PER_ENTRY
    assert "…and 5 more" in html and "/archive/2026-08-01" in html
    assert f"{count} songs shared" in entry.find("a:summary", NS).text   # the count is the real one


def test_a_nonsense_mesh_clock_falls_back_to_the_day():
    """Mesh nodes have bad clocks; one must not take the feed down."""
    assert _stamp(None, "2026-08-01") == "2026-08-01T00:00:00Z"
    assert _stamp(1e300, "2026-08-01") == "2026-08-01T00:00:00Z"
    assert _stamp(float("nan"), "2026-08-01") == "2026-08-01T00:00:00Z"
    assert _stamp(0, "2026-08-01") == "1970-01-01T00:00:00Z"


def test_build_feed_needs_no_server():
    rows = [{"date": "2026-08-01", "theme_title": "rain", "video_id": "aaaaaaaaaaa",
             "title": "Rain", "artist": None, "sender": None, "mesh_ts": T0}]
    root = parse(build_feed(rows, feed_url="https://x/feed.xml", site_url="https://x/",
                            day_url=lambda d: f"https://x/archive/{d}"))
    assert root.find("a:entry/a:id", NS).text == "https://x/archive/2026-08-01"


async def test_feed_urls_follow_the_proxys_scheme(db, bus):
    """Hosted deployments terminate TLS upstream; the links a reader follows
    have to be https."""
    await add_day(db, "2026-08-01", "rain songs", [("aaaaaaaaaaa", "Rain", "")])
    root = parse((await fetch(db, bus, headers={"x-forwarded-proto": "https"})).text)
    assert root.find("a:id", NS).text == "https://test/feed.xml"
    assert root.find("a:entry/a:id", NS).text == "https://test/archive/2026-08-01"


async def test_feed_reads_go_through_the_ttl_cache(db, bus, monkeypatch):
    """/feed.xml is public and polled; a stampede should cost one query."""
    await add_day(db, "2026-08-01", "rain songs", [("aaaaaaaaaaa", "Rain", "")])
    calls = {"n": 0}
    real = db.recent_days_tracks

    async def counting(days=30):
        calls["n"] += 1
        return await real(days)

    monkeypatch.setattr(db, "recent_days_tracks", counting)
    async with client_for(page_app(db, bus)) as client:
        for _ in range(3):
            assert (await client.get("/feed.xml")).status_code == 200
    assert calls["n"] == 1


async def test_every_page_advertises_the_feed(db, bus):
    """Readers and browsers autodiscover from whatever page the visitor is on."""
    await add_day(db, "2026-08-01", "rain songs", [("aaaaaaaaaaa", "Rain", "")])
    async with client_for(page_app(db, bus)) as client:
        for path in ("/", "/archive", "/archive/2026-08-01", "/stats", "/about"):
            body = (await client.get(path)).text
            assert ('<link rel="alternate" type="application/atom+xml"' in body
                    and 'href="http://test/feed.xml"' in body), path


# -- the query ---------------------------------------------------------------

async def test_recent_days_tracks_limits_days_and_keeps_posted_order(db):
    await add_day(db, "2026-08-01", "a", [("aaaaaaaaaaa", "A1", ""), ("bbbbbbbbbbb", "A2", "")])
    await add_day(db, "2026-08-02", "b", [("ccccccccccc", "B1", "")])
    await add_day(db, "2026-08-03", "c", [("ddddddddddd", "C1", "")])
    rows = await db.recent_days_tracks(days=2)
    assert [(r["date"], r["title"]) for r in rows] == [
        ("2026-08-03", "C1"), ("2026-08-02", "B1"),
    ]
    rows = await db.recent_days_tracks(days=5)
    # Newest day first, posted order within a day.
    assert [r["title"] for r in rows] == ["C1", "B1", "A1", "A2"]


async def test_recent_days_counts_days_with_songs_not_empty_themes(db):
    """A run of silent themed days must not crowd real days out of the window."""
    await add_day(db, "2026-08-01", "a", [("aaaaaaaaaaa", "A1", "")])
    for day in (2, 3, 4):
        await db.create_theme(f"2026-08-{day:02d}", f"silent {day}")
    rows = await db.recent_days_tracks(days=1)
    assert [r["title"] for r in rows] == ["A1"]
