"""Upcoming shows for the channel's artists, nearest to the listener first.

Shows come from Ticketmaster's Discovery API (free key, 5,000 calls a day,
5 a second). They are fetched by artist only, never with anything about the
listener: one attraction search to find the artist and one event search for
their upcoming dates, kept for ``ttl_s`` and shared by every listener. The
distance sort happens here, against the city geoip.py found for the request,
so a listener's location never leaves the server.

Matching is deliberately strict, because the channel's artist names are
YouTube channel names: an attraction counts only if its name equals the
artist's (case, spacing, "&"/"and" and a leading "The" aside). Several equal
names go to the one with the most upcoming events. No match means no shows,
which reads better than somebody else's tour.

Ticketmaster's terms want data cached only "for reasonable periods" and calls
tied to what users do, so nothing is fetched until someone looks at an
artist, and the cache lives in memory only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from .db.browse import artist_name
from .db.fields import is_placeholder_artist
from .geoip import Place
from .net import http_client

log = logging.getLogger(__name__)

API = "https://app.ticketmaster.com/discovery/v2"
EARTH_KM = 6371.0
KM_PER_MILE = 1.609344


@dataclass(frozen=True)
class Show:
    day: str                   # local date, YYYY-MM-DD
    name: str
    venue: str
    city: str                  # "Dallas, TX"
    url: str
    lat: float | None = None
    lon: float | None = None


@dataclass(frozen=True)
class Lineup:
    """An artist's upcoming shows as Ticketmaster has them."""
    shows: tuple[Show, ...] = ()
    url: str = ""              # the artist's Ticketmaster page


def show_date(day: str) -> str:
    """``2026-11-03`` -> ``Tue, Nov 3``."""
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return day
    return f"{d:%a}, {d:%b} {d.day}"


def name_key(name: str) -> str:
    """An artist name reduced to what two spellings of it share."""
    key = " ".join(name.casefold().replace("&", " and ").split())
    key = re.sub(r"^the ", "", key)
    return re.sub(r"[^\w ]", "", key).strip()


def distance_km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    """Great-circle distance."""
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_KM * math.asin(math.sqrt(h))


def nearest(
    shows: tuple[Show, ...],
    place: Place | None,
    limit: int,
    since: str = "",
    within_km: float | None = None,
) -> list[dict[str, Any]]:
    """The ``limit`` shows on or after ``since`` nearest ``place`` (soonest
    first when there's no place), each with its distance in the listener's
    unit. With ``within_km``, only shows that close to ``place`` count, so
    an unknown place, or a show with no location, has none."""
    if within_km is not None and place is None:
        return []
    rows: list[dict[str, Any]] = []
    for show in shows:
        if show.day < since:
            continue           # a cached lineup outlives its first dates
        away = None
        if place is not None and show.lat is not None and show.lon is not None:
            km = distance_km(place.lat, place.lon, show.lat, show.lon)
            if within_km is not None and km > within_km:
                continue
            away = km / KM_PER_MILE if place.miles else km
        elif within_km is not None:
            continue
        rows.append({"show": show, "away": away})
    if place is not None:
        rows.sort(key=lambda r: (r["away"] is None, r["away"] or 0.0, r["show"].day))
    return rows[:limit]


def _attraction_match(artist: str, attractions: list[dict[str, Any]]) -> dict[str, Any] | None:
    want = name_key(artist)
    same = [a for a in attractions if name_key(str(a.get("name", ""))) == want]
    if not same:
        return None
    return max(same, key=lambda a: (a.get("upcomingEvents") or {}).get("_total", 0))


def _show_from_event(event: dict[str, Any]) -> Show | None:
    dates = event.get("dates") or {}
    if (dates.get("status") or {}).get("code") in ("cancelled", "canceled", "postponed"):
        return None
    day = (dates.get("start") or {}).get("localDate")
    url = event.get("url")
    if not day or not url:
        return None
    venues = (event.get("_embedded") or {}).get("venues") or [{}]
    venue = venues[0]
    city = (venue.get("city") or {}).get("name", "")
    region = (venue.get("state") or {}).get("stateCode") or (
        (venue.get("country") or {}).get("name", "")
    )
    lat = lon = None
    location = venue.get("location") or {}
    with contextlib.suppress(KeyError, TypeError, ValueError):
        lat, lon = float(location["latitude"]), float(location["longitude"])
    return Show(
        day=day,
        name=str(event.get("name", "")),
        venue=str(venue.get("name", "")),
        city=", ".join(p for p in (city, region) if p),
        url=str(url),
        lat=lat,
        lon=lon,
    )


class ShowFinder:
    """Upcoming shows per artist, from Ticketmaster, cached in memory.

    One lookup per artist at a time (concurrent asks share it), one request
    at a time to Ticketmaster and no faster than its 5 a second, and a daily
    ceiling under the free quota. A 429 or a failure pauses or parks that
    artist rather than retrying on every page view."""

    DAILY_CAP = 4000           # of Ticketmaster's 5,000
    SPACING_S = 0.25           # under 5 a second
    FAIL_TTL_S = 3600.0
    RATE_LIMIT_PAUSE_S = 600.0

    def __init__(
        self,
        api_key: str,
        ttl_s: float = 12 * 3600,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], date] = date.today,
    ) -> None:
        self.api_key = api_key
        self.ttl_s = ttl_s
        self._client = client
        self._clock = clock
        self._today = today
        self._cache: dict[str, tuple[float, Lineup]] = {}
        self._inflight: dict[str, asyncio.Task[Lineup]] = {}
        self._gate = asyncio.Lock()
        self._last_call = 0.0
        self._paused_until = 0.0
        self._calls_day = ""
        self.calls_today = 0

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def wants(artist: str | None) -> bool:
        """Whether ``artist`` is a name worth looking up at all."""
        name = artist_name(artist)
        return bool(name) and not is_placeholder_artist(artist) and bool(name_key(name))

    def cached(self, artist: str) -> Lineup | None:
        """The artist's shows if known and fresh; never calls out."""
        hit = self._cache.get(name_key(artist_name(artist)))
        if hit is not None and self._clock() < hit[0]:
            return hit[1]
        return None

    async def lineup(self, artist: str) -> Lineup:
        """The artist's upcoming shows, fetching them if they aren't cached."""
        if not self.wants(artist):
            return Lineup()
        hit = self.cached(artist)
        if hit is not None:
            return hit
        key = name_key(artist_name(artist))
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(self._fetch(key, artist_name(artist)))
            self._inflight[key] = task
            task.add_done_callback(lambda _t: self._inflight.pop(key, None))
        return await asyncio.shield(task)

    async def _fetch(self, key: str, artist: str) -> Lineup:
        try:
            found = await self._get(
                "/attractions.json",
                {"keyword": artist, "classificationName": "music", "size": "20"},
            )
            match = _attraction_match(
                artist, ((found.get("_embedded") or {}).get("attractions")) or []
            )
            lineup = Lineup()
            if match is not None:
                events = await self._get(
                    "/events.json",
                    {"attractionId": str(match["id"]), "size": "100", "sort": "date,asc"},
                )
                shows = [
                    show
                    for event in ((events.get("_embedded") or {}).get("events")) or []
                    if (show := _show_from_event(event)) is not None
                ]
                lineup = Lineup(shows=tuple(shows), url=str(match.get("url", "")))
            self._cache[key] = (self._clock() + self.ttl_s, lineup)
            return lineup
        except _Unavailable as exc:
            # Keep serving a stale answer if there is one; otherwise park the
            # artist for a while rather than ask again on every page view.
            log.info("concerts: %s unavailable: %s", artist, exc)
            stale = self._cache.get(key)
            lineup = stale[1] if stale else Lineup()
            self._cache[key] = (self._clock() + self.FAIL_TTL_S, lineup)
            return lineup

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        async with self._gate:
            now = self._clock()
            if now < self._paused_until:
                raise _Unavailable("paused after a rate limit")
            day = self._today().isoformat()
            if day != self._calls_day:
                self._calls_day, self.calls_today = day, 0
            if self.calls_today >= self.DAILY_CAP:
                raise _Unavailable("daily call budget spent")
            wait = self._last_call + self.SPACING_S - now
            if wait > 0:
                await asyncio.sleep(wait)
            self.calls_today += 1
            self._last_call = self._clock()
            if self._client is None:
                self._client = http_client(timeout=10)
            try:
                response = await self._client.get(
                    API + path, params={**params, "apikey": self.api_key}
                )
            except httpx.HTTPError as exc:
                raise _Unavailable(type(exc).__name__) from exc
            if response.status_code == 429:
                self._paused_until = self._clock() + self.RATE_LIMIT_PAUSE_S
                raise _Unavailable("rate limited")
            if response.status_code != 200:
                raise _Unavailable(f"HTTP {response.status_code}")
            try:
                body = response.json()
            except ValueError as exc:
                raise _Unavailable("not JSON") from exc
            return body if isinstance(body, dict) else {}


class _Unavailable(Exception):
    """Ticketmaster can't answer right now."""


@dataclass
class Concerts:
    """What the web app needs: the shows, where a listener is, and how far
    from them a show may be (``[concerts] radius_miles``)."""
    finder: ShowFinder
    locator: Any               # geoip.GeoLocator
    radius_miles: int = 100

    @property
    def radius_km(self) -> float:
        return self.radius_miles * KM_PER_MILE
