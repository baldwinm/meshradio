"""Where a listener roughly is, from their IP address, without asking anyone.

The concert listings (concerts.py) sort an artist's shows by distance from
the listener. Their address is looked up in DB-IP's free "IP to City Lite"
database, a file kept on this server, so the address never leaves the
process and nothing about the lookup is stored: the answer is a city, used
for one response and dropped.

An address that isn't on the public internet (the Pi's LAN, Tailscale) has
no entry in any such database; those listeners are wherever the radio is,
so they get ``[concerts] home_*`` instead.

DB-IP publishes the file monthly under CC BY 4.0, which asks for a link to
db-ip.com wherever its results are shown (partials/shows.html does that).
``GeoDbUpdater`` fetches it on hosts that turn ``geoip_download`` on.
"""

from __future__ import annotations

import asyncio
import gzip
import ipaddress
import logging
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from .net import http_client
from .runtime import Service

log = logging.getLogger(__name__)

DBIP_URL = "https://download.db-ip.com/free/dbip-city-lite-{month}.mmdb.gz"
DBIP_CREDIT_URL = "https://db-ip.com"

# Countries whose listeners read distances in miles.
MILES_COUNTRIES = frozenset({"US", "GB", "LR", "MM"})


@dataclass(frozen=True)
class Place:
    """A listener's approximate whereabouts: a city, not an address."""
    label: str                 # "Dallas, Texas" / "Lyon, France"
    lat: float
    lon: float
    country: str = ""          # ISO code, for the distance unit
    home: bool = False         # the radio's own location, not a lookup

    @property
    def miles(self) -> bool:
        return self.country in MILES_COUNTRIES


def _open_reader(path: Path) -> Any:
    import maxminddb

    return maxminddb.open_database(str(path))


def place_from_record(record: Any) -> Place | None:
    """A GeoIP2-City-shaped record (what DB-IP's file holds) as a Place."""
    if not isinstance(record, dict):
        return None
    location = record.get("location") or {}
    lat, lon = location.get("latitude"), location.get("longitude")
    if not isinstance(lat, int | float) or not isinstance(lon, int | float):
        return None
    city = ((record.get("city") or {}).get("names") or {}).get("en", "")
    subdivisions = record.get("subdivisions") or [{}]
    region = (subdivisions[0].get("names") or {}).get("en", "")
    country = record.get("country") or {}
    code = country.get("iso_code", "") or ""
    country_name = (country.get("names") or {}).get("en", "") or code
    # A US listener knows their state; anyone else, their country.
    second = region if code == "US" else country_name
    label = ", ".join(p for p in (city, second) if p) or country_name
    if not label:
        return None
    return Place(label=label, lat=float(lat), lon=float(lon), country=code)


class GeoLocator:
    """Address → Place, from the database file at ``path``.

    The file is opened on first use and reopened when ``reload`` is called
    after an update; a missing or unreadable file means every public address
    is simply unknown (the listings then go by date)."""

    def __init__(
        self,
        path: Path,
        home: Place | None = None,
        opener: Callable[[Path], Any] = _open_reader,
    ) -> None:
        self.path = path
        self.home = home
        self._opener = opener
        self._reader: Any = None
        self._tried = False

    def reload(self) -> None:
        old, self._reader, self._tried = self._reader, None, False
        if old is not None:
            close = getattr(old, "close", None)
            if close:
                close()

    @property
    def ready(self) -> bool:
        return self._get_reader() is not None

    def _get_reader(self) -> Any:
        if self._reader is None and not self._tried:
            self._tried = True
            if self.path.is_file():
                try:
                    self._reader = self._opener(self.path)
                except Exception as exc:  # a corrupt or half-written file
                    log.warning("geoip: can't open %s: %s", self.path, exc)
        return self._reader

    def locate(self, address: str | None) -> Place | None:
        try:
            ip = ipaddress.ip_address((address or "").strip())
        except ValueError:
            return None
        if not ip.is_global:
            return self.home
        reader = self._get_reader()
        if reader is None:
            return None
        try:
            return place_from_record(reader.get(str(ip)))
        except Exception as exc:
            log.debug("geoip: lookup failed: %s", exc)
            return None


def _months_back(today: date, n: int) -> str:
    month = today.month - n
    year = today.year + (month - 1) // 12
    return f"{year:04d}-{(month - 1) % 12 + 1:02d}"


class GeoDbUpdater(Service):
    """Keep DB-IP's monthly city file current at ``locator.path``.

    Checked daily; fetched when the file is missing or older than
    ``max_age_s``. This month's file appears some days into the month, so
    last month's is tried after it. The download lands beside the target and
    is renamed over it only once fully unpacked, so the reader never sees a
    partial file."""

    CHECK_S = 24 * 3600.0
    RETRY_S = 3600.0

    def __init__(
        self,
        locator: GeoLocator,
        max_age_s: float = 32 * 24 * 3600,
        client: httpx.AsyncClient | None = None,
        today: Callable[[], date] = date.today,
    ) -> None:
        self.locator = locator
        self.max_age_s = max_age_s
        self._client = client
        self._today = today
        self.updated_at: float | None = None

    def stale(self) -> bool:
        try:
            return time.time() - self.locator.path.stat().st_mtime > self.max_age_s
        except FileNotFoundError:
            return True

    async def _run(self) -> None:
        while True:
            wait = self.CHECK_S
            if self.stale():
                try:
                    if not await self.update():
                        wait = self.RETRY_S
                except (httpx.HTTPError, OSError) as exc:
                    log.warning("geoip: update failed: %s", exc)
                    wait = self.RETRY_S
            await asyncio.sleep(wait)

    async def update(self) -> bool:
        """Fetch the newest published file. Returns whether one was installed."""
        target = self.locator.path
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".gz.part")
        unpacked = target.with_name(target.name + ".part")
        client = self._client or http_client(timeout=120, follow_redirects=True)
        try:
            for back in (0, 1):
                url = DBIP_URL.format(month=_months_back(self._today(), back))
                async with client.stream("GET", url) as response:
                    if response.status_code == 404:
                        continue
                    response.raise_for_status()
                    with open(part, "wb") as out:
                        async for chunk in response.aiter_bytes(1 << 20):
                            out.write(chunk)
                await asyncio.to_thread(_gunzip, part, unpacked)
                os.replace(unpacked, target)
                self.locator.reload()
                self.updated_at = time.time()
                log.info("geoip: installed %s", url.rsplit("/", 1)[-1])
                return True
            log.warning("geoip: no DB-IP file published for this month or last")
            return False
        finally:
            if self._client is None:
                await client.aclose()
            for leftover in (part, unpacked):
                leftover.unlink(missing_ok=True)


def _gunzip(src: Path, dst: Path) -> None:
    with gzip.open(src, "rb") as zipped, open(dst, "wb") as out:
        shutil.copyfileobj(zipped, out, 1 << 20)
