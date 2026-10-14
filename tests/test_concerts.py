"""Upcoming shows for the channel's artists, nearest the listener first.

Ticketmaster is faked (helpers.fake_ticketmaster) and the IP database is a
real file in DB-IP's format built per test (helpers.write_geo_db)."""

import asyncio
import gzip
from datetime import date, timedelta

import httpx

from meshradio.concerts import Concerts, Lineup, Show, ShowFinder, name_key, nearest
from meshradio.config import load_config
from meshradio.geoip import GeoDbUpdater, GeoLocator, Place

from .helpers import (
    client_for,
    embed_app,
    fake_ticketmaster,
    geo_record,
    make_ready_on,
    page_app,
    peer,
    share,
    tm_attraction,
    tm_event,
    write_geo_db,
)

AUSTIN = Place("Austin, Texas", 30.27, -97.74, "US", home=True)
SOON = (date.today() + timedelta(days=20)).isoformat()
LATER = (date.today() + timedelta(days=40)).isoformat()
DALLAS = (32.78, -96.80)
LONDON = (51.51, -0.13)


def finder_with(attractions=(), events=None, status=200):
    client, seen = fake_ticketmaster(attractions, events, status)
    finder = ShowFinder("test-key", client=client)
    finder.SPACING_S = 0
    return finder, seen


# -- matching an artist -------------------------------------------------------


def test_names_match_across_case_spacing_ampersands_and_a_leading_the():
    assert name_key("The  Black Keys") == name_key("black keys")
    assert name_key("Simon & Garfunkel") == name_key("simon and garfunkel")
    assert name_key("Phoenix") != name_key("Phoenix Suns")


async def test_only_an_exact_name_counts_and_the_busiest_namesake_wins():
    finder, seen = finder_with(
        [tm_attraction("x", "Phoenix Tribute Band", 9), tm_attraction("a", "Phoenix", 1),
         tm_attraction("b", "PHOENIX", 4)],
        {"b": [tm_event(SOON, "Dallas", "TX", *DALLAS)]},
    )
    lineup = await finder.lineup("Phoenix")
    assert [s.city for s in lineup.shows] == ["Dallas, TX"]
    assert lineup.url.endswith("/b")
    assert seen[-1].url.params["attractionId"] == "b"
    assert all(r.url.params["apikey"] == "test-key" for r in seen)


async def test_no_namesake_means_no_shows_and_no_event_search():
    finder, seen = finder_with([tm_attraction("x", "Someone Else")])
    assert await finder.lineup("Phoenix") == Lineup()
    assert len(seen) == 1


async def test_stand_in_and_missing_artists_are_never_looked_up():
    finder, seen = finder_with([tm_attraction("a", "Release")])
    for artist in (None, "", "Release - Topic"):
        assert await finder.lineup(artist) == Lineup()
    assert seen == []


async def test_cancelled_and_postponed_shows_are_dropped():
    finder, _ = finder_with([tm_attraction("a", "Band")], {"a": [
        tm_event(SOON, "Dallas", "TX", *DALLAS, status="cancelled"),
        tm_event(SOON, "Houston", "TX", 29.76, -95.37, status="postponed"),
        tm_event(LATER, "Austin", "TX", 30.27, -97.74),
    ]})
    assert [s.city for s in (await finder.lineup("Band")).shows] == ["Austin, TX"]


# -- staying inside Ticketmaster's quota --------------------------------------


async def test_a_lineup_is_fetched_once_and_shared_until_it_expires():
    now = [0.0]
    client, seen = fake_ticketmaster([tm_attraction("a", "Band")],
                                     {"a": [tm_event(SOON, "Dallas", "TX", *DALLAS)]})
    finder = ShowFinder("k", ttl_s=100, client=client, clock=lambda: now[0])
    finder.SPACING_S = 0
    await asyncio.gather(*(finder.lineup("Band") for _ in range(5)))
    await finder.lineup("band")
    assert len(seen) == 2                    # one search, one event list
    now[0] = 101
    assert finder.cached("Band") is None
    await finder.lineup("Band")
    assert len(seen) == 4


async def test_a_rate_limit_pauses_every_lookup():
    finder, seen = finder_with(status=429)
    assert await finder.lineup("Band") == Lineup()
    assert await finder.lineup("Other Band") == Lineup()
    assert len(seen) == 1


async def test_the_daily_budget_stops_calls():
    finder, seen = finder_with([tm_attraction("x", "Nobody")])
    finder.DAILY_CAP = 2
    for artist in ("One", "Two", "Three"):
        await finder.lineup(artist)
    assert len(seen) == 2


# -- nearest first ------------------------------------------------------------


def shows():
    return (
        Show(SOON, "A", "Club", "London, England", "u1", *LONDON),
        Show(LATER, "B", "Club", "Dallas, TX", "u2", *DALLAS),
        Show(SOON, "C", "Club", "Somewhere", "u3"),
    )


def test_shows_sort_by_distance_in_the_listeners_unit():
    rows = nearest(shows(), AUSTIN, 3)
    assert [r["show"].name for r in rows] == ["B", "A", "C"]   # no location goes last
    assert 175 < rows[0]["away"] < 200                          # Austin to Dallas, miles
    paris = Place("Paris, France", 48.86, 2.35, "FR")
    assert 330 < nearest(shows(), paris, 1)[0]["away"] < 360    # Paris to London, km


def test_without_a_location_shows_go_soonest_first_and_past_ones_drop():
    rows = nearest(shows(), None, 3)
    assert [r["show"].name for r in rows] == ["A", "B", "C"]
    assert all(r["away"] is None for r in rows)
    assert [r["show"].name for r in nearest(shows(), None, 3, since=LATER)] == ["B"]


# -- placing a listener -------------------------------------------------------


def test_listeners_are_placed_from_the_ip_database(tmp_path):
    path = write_geo_db(tmp_path / "geo.mmdb", {
        "8.8.8.0/24": geo_record("Dallas", "Texas", "US", *DALLAS),
        "81.2.69.0/24": geo_record("London", "England", "GB", *LONDON),
    })
    locator = GeoLocator(path, home=AUSTIN)
    assert locator.locate("8.8.8.8") == Place("Dallas, Texas", *DALLAS, "US")
    assert locator.locate("81.2.69.1").label == "London, GB"
    assert locator.locate("1.1.1.1") is None                # not in the file
    assert locator.locate("not an address") is None
    # The radio's own network is wherever the radio is.
    for local in ("192.168.1.20", "127.0.0.1", "100.101.102.103", "fd7a::1"):
        assert locator.locate(local) == AUSTIN


def test_no_database_means_public_addresses_are_unknown(tmp_path):
    locator = GeoLocator(tmp_path / "missing.mmdb", home=AUSTIN)
    assert locator.locate("8.8.8.8") is None
    (tmp_path / "bad.mmdb").write_bytes(b"not a database")
    assert GeoLocator(tmp_path / "bad.mmdb").locate("8.8.8.8") is None


async def test_the_updater_installs_last_months_file_when_this_months_isnt_out(tmp_path):
    source = write_geo_db(tmp_path / "src.mmdb",
                          {"8.8.8.0/24": geo_record("Dallas", "Texas", "US", *DALLAS)})
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        if "2026-01" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(200, content=gzip.compress(source.read_bytes()))

    target = tmp_path / "geoip" / "dbip-city-lite.mmdb"
    locator = GeoLocator(target)
    assert locator.locate("8.8.8.8") is None
    updater = GeoDbUpdater(
        locator, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        today=lambda: date(2026, 1, 3),
    )
    assert updater.stale()
    assert await updater.update()
    assert asked == ["/free/dbip-city-lite-2026-01.mmdb.gz",
                     "/free/dbip-city-lite-2025-12.mmdb.gz"]
    assert locator.locate("8.8.8.8").label == "Dallas, Texas"
    assert not updater.stale()
    assert sorted(p.name for p in target.parent.iterdir()) == [target.name]


# -- on the site --------------------------------------------------------------


def concerts_for(tmp_path, attractions, events):
    client, seen = fake_ticketmaster(attractions, events)
    finder = ShowFinder("k", client=client)
    finder.SPACING_S = 0
    path = write_geo_db(tmp_path / "geo.mmdb",
                        {"81.2.69.0/24": geo_record("London", "England", "GB", *LONDON)})
    return Concerts(finder, GeoLocator(path, home=AUSTIN)), seen


def band_shows():
    return {"a": [tm_event(SOON, "London", "", *LONDON, venue="Roundhouse"),
                  tm_event(LATER, "Dallas", "TX", *DALLAS, venue="Granada")]}


async def test_the_artist_page_lists_the_shows_nearest_the_visitor(db, bus, tmp_path):
    await share(db, "2026-08-01", "aaaaaaaaaaa", "alice", artist="The Band")
    concerts, _ = concerts_for(tmp_path, [tm_attraction("a", "Band")], band_shows())
    app = embed_app(db, bus, concerts=concerts)
    async with client_for(app) as client:
        page = (await client.get("/artist/The%20Band")).text
    assert 'hx-get="/partials/shows?artist=The%20Band"' in page

    async with peer(app, "81.2.69.10") as visitor:
        response = await visitor.get("/partials/shows?artist=the%20band")
    body = response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert "Nearest to London, GB" in body and "db-ip.com" in body
    assert body.index("Roundhouse") < body.index("Granada")

    async with peer(app, "192.168.1.5") as neighbour:      # on the radio's own network
        body = (await neighbour.get("/partials/shows?artist=The%20Band")).text
    assert "Nearest to Austin, Texas." in body and "db-ip.com" not in body
    assert body.index("Granada") < body.index("Roundhouse")

    async with peer(app, "1.1.1.1") as stranger:            # an address the file lacks
        body = (await stranger.get("/partials/shows?artist=The%20Band")).text
    assert "Soonest first" in body


async def test_only_the_channels_artists_can_be_looked_up(db, bus, tmp_path):
    concerts, seen = concerts_for(tmp_path, [tm_attraction("a", "Band")], band_shows())
    async with client_for(embed_app(db, bus, concerts=concerts)) as client:
        assert (await client.get("/partials/shows?artist=Anyone")).status_code == 404
    assert seen == []


async def test_without_a_key_there_are_no_listings_anywhere(db, bus):
    await share(db, "2026-08-01", "aaaaaaaaaaa", "alice", artist="Band")
    async with client_for(embed_app(db, bus)) as client:
        assert "/partials/shows" not in (await client.get("/artist/Band")).text
        assert (await client.get("/partials/shows?artist=Band")).status_code == 404
        assert "Concert listings" not in (await client.get("/privacy")).text


async def test_the_now_playing_card_asks_once_then_shows_the_nearest_show(db, bus, tmp_path):
    track = await make_ready_on(db, "aaaaaaaaaaa", "2026-08-01")
    await db.update_track_metadata(track["id"], title="Song", artist="Band")
    concerts, seen = concerts_for(tmp_path, [tm_attraction("a", "Band")], band_shows())
    app = page_app(db, bus, concerts=concerts)
    async with client_for(app) as client:
        await client.post("/api/play-day/2026-08-01")
        first = (await client.get("/partials/live")).text
        assert 'hx-get="/partials/shows?compact=1&artist=Band"' in first
        assert 'href="/artist/Band"' in first
        line = (await client.get("/partials/shows?compact=1&artist=Band")).text
        assert "Dallas, TX" in line and "Roundhouse" not in line
        again = (await client.get("/partials/live")).text
    assert "hx-get=\"/partials/shows" not in again          # cached now: drawn in place
    assert "🎟 Dallas, TX" in again
    assert len(seen) == 2


async def test_the_privacy_page_explains_the_lookup_when_listings_are_on(db, bus, tmp_path):
    concerts, _ = concerts_for(tmp_path, [], {})
    async with client_for(embed_app(db, bus, concerts=concerts)) as client:
        page = (await client.get("/privacy")).text
    assert "Concert listings" in page and "DB-IP" in page and "Ticketmaster" in page
    assert "find concerts near you" in page


def test_the_key_comes_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("MESHRADIO_TICKETMASTER_KEY", " abc ")
    config = load_config(tmp_path / "none.toml")
    assert config.concerts.ticketmaster_key == "abc"
    assert config.geoip_db.name == "dbip-city-lite.mmdb"
