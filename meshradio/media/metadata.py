"""Metadata fallback via YouTube's oEmbed endpoint (no API key needed).

Used when yt-dlp can't fetch audio (fallback ladder step 3, architecture §7):
the track still gets a title/artist so the archive stays browsable.
"""

from __future__ import annotations

import logging

from ..net import http_client

log = logging.getLogger(__name__)

OEMBED_URL = "https://www.youtube.com/oembed"


async def fetch_oembed(video_id: str) -> dict[str, str] | None:
    """Return {"title", "artist", "thumbnail"} or None if unresolvable."""
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        async with http_client(timeout=15) as client:
            resp = await client.get(
                OEMBED_URL, params={"url": watch_url, "format": "json"}
            )
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
