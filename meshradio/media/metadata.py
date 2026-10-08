"""Metadata fallback via YouTube's oEmbed endpoint (no API key needed).

Used when yt-dlp can't fetch audio (fallback ladder step 3, architecture §7):
the track still gets a title/artist so the archive stays browsable. Embed
hosting never runs yt-dlp, so it also reads lengths here (``fetch_duration``).
"""

from __future__ import annotations

import logging
import re

import httpx

from ..net import http_client

log = logging.getLogger(__name__)

OEMBED_URL = "https://www.youtube.com/oembed"
WATCH_URL = "https://www.youtube.com/watch"
# oEmbed has no length, so the watch page is read for one: the player
# response embedded in it carries "lengthSeconds", and the page's schema.org
# markup an ISO 8601 duration. A browser's User-Agent gets the regular page;
# the consent cookie skips the EU cookie wall that would stand in for it.
WATCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cookie": "CONSENT=YES+1; SOCS=CAI",
}
_LENGTH_SECONDS = re.compile(r'"lengthSeconds"\s*:\s*"(\d+)"')
_ISO_DURATION = re.compile(
    r'itemprop="duration"\s+content="PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?"'
)


async def fetch_oembed(
    video_id: str, client: httpx.AsyncClient | None = None
) -> dict[str, str] | None:
    """Return {"title", "artist", "thumbnail"} or None if unresolvable.

    Pass the caller's ``client`` to reuse its connection pool: the cacher
    resolves a whole backfill this way, and a fresh TLS handshake per video
    was most of each lookup's time. Without one, a throwaway client is used."""
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    params = {"url": watch_url, "format": "json"}
    try:
        if client is None:
            async with http_client(timeout=15) as own:
                resp = await own.get(OEMBED_URL, params=params)
        else:
            resp = await client.get(OEMBED_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        log.warning("oEmbed lookup failed for %s", video_id, exc_info=True)
        return None
    return {
        "title": data.get("title", ""),
        "artist": data.get("author_name", ""),
        "thumbnail": data.get("thumbnail_url", ""),
    }


def parse_watch_duration(html: str) -> float | None:
    """A video's length in seconds from its watch page, or None."""
    match = _LENGTH_SECONDS.search(html)
    if match and int(match.group(1)) > 0:
        return float(match.group(1))
    match = _ISO_DURATION.search(html)
    if match:
        h, m, s = (int(g or 0) for g in match.groups())
        seconds = h * 3600 + m * 60 + s
        if seconds > 0:
            return float(seconds)
    return None


async def fetch_duration(
    video_id: str, client: httpx.AsyncClient | None = None
) -> float | None:
    """A video's length in seconds from YouTube, or None if it can't be read."""
    params = {"v": video_id}
    try:
        if client is None:
            async with http_client(timeout=15) as own:
                resp = await own.get(WATCH_URL, params=params, headers=WATCH_HEADERS)
        else:
            resp = await client.get(WATCH_URL, params=params, headers=WATCH_HEADERS)
        resp.raise_for_status()
    except Exception:
        log.warning("length lookup failed for %s", video_id, exc_info=True)
        return None
    seconds = parse_watch_duration(resp.text)
    if seconds is None:
        log.info("no length on the watch page for %s", video_id)
    return seconds
