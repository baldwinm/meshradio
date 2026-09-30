"""Atom feed of the channel's days — one entry per day, newest first.

The pages people share are days, so a day is the entry: its theme, how many
songs, and the songs themselves, with the day's first cover as the picture. A
subscriber learns the day's theme the moment the channel sets it, without
opening the site.

Pure functions over the rows ``Database.recent_days_tracks`` returns, so the
markup is testable without a server. Everything a member typed on the mesh —
sender names, theme titles — lands in the XML, and so does whatever oEmbed said
a title was. One control character in any of them would make the whole document
unparseable, and a feed reader does not skip the bad entry, it drops the feed
for as long as that day stays in the window — hence ``_clean``.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from html import escape as html_escape
from typing import Any, Callable
from xml.sax.saxutils import escape as xml_escape, quoteattr

from ..ingest.parse import untitled_theme

# How many days the feed carries. A reader that has been away for a month has
# missed nothing it could still act on, and the document stays a fixed size
# however long the channel runs.
FEED_DAYS = 30

# A busy day can carry a hundred songs; past this the entry says how many more
# and leaves the rest to the day's page.
SONGS_PER_ENTRY = 50

FEED_TITLE = "MeshRadio — Austin #music"
FEED_SUBTITLE = (
    "Songs shared each day on the Austin MeshCore #music channel, played against "
    "that day's theme."
)

# XML 1.0 forbids these outright (everything below U+0020 but tab, newline and
# carriage return, plus the two non-characters a UTF-8 encoder will still emit).
_XML_INVALID = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def _clean(value: Any) -> str:
    return _XML_INVALID.sub("", str(value))


def _text(value: Any) -> str:
    """Character data for an element body."""
    return xml_escape(_clean(value))


def _attr(value: Any) -> str:
    """A quoted attribute value, quotes included."""
    return quoteattr(_clean(value))


def _stamp(ts: float | None, fallback_date: str) -> str:
    """RFC 3339 UTC. A track with no usable time falls back to its day's
    midnight rather than taking the feed down: mesh clocks are not trusted."""
    try:
        if ts is not None:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
    except (OverflowError, OSError, ValueError):
        pass
    return f"{fallback_date}T00:00:00Z"


def _days(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows grouped by day, in the order they arrive (newest day first)."""
    days: dict[str, dict[str, Any]] = {}
    for row in rows:
        day = days.setdefault(
            row["date"], {"date": row["date"], "themes": [], "songs": [], "last": None}
        )
        if row["theme_title"] not in day["themes"]:
            day["themes"].append(row["theme_title"])
        day["songs"].append(row)
        ts = row["mesh_ts"]
        if ts is not None and (day["last"] is None or ts > day["last"]):
            day["last"] = ts
    return list(days.values())


def _song_item(song: dict[str, Any]) -> str:
    """One song as a list item: linked title, artist, who shared it."""
    title = song["title"] or song["video_id"]
    line = f'<a href="https://youtu.be/{html_escape(song["video_id"])}">{html_escape(title)}</a>'
    if song["artist"]:
        line += f" — {html_escape(song['artist'])}"
    if song["sender"]:
        line += f" ({html_escape(song['sender'])})"
    return f"<li>{line}</li>"


def _entry_html(day: dict[str, Any], link: str) -> str:
    songs = day["songs"]
    first = songs[0]["video_id"]
    items = "".join(_song_item(s) for s in songs[:SONGS_PER_ENTRY])
    more = len(songs) - SONGS_PER_ENTRY
    tail = (
        f'<p><a href="{html_escape(link)}">…and {more} more</a></p>' if more > 0 else ""
    )
    return (
        f'<p><img src="https://i.ytimg.com/vi/{html_escape(first)}/mqdefault.jpg" alt=""></p>'
        f"<ol>{items}</ol>{tail}"
    )


def build_feed(
    rows: list[dict[str, Any]],
    *,
    feed_url: str,
    site_url: str,
    day_url: Callable[[str], str],
    now: datetime | None = None,
) -> str:
    """The Atom document for ``rows`` (``Database.recent_days_tracks``).

    ``day_url`` maps a date to that day's page. It doubles as the entry's id:
    a permalink is stable for as long as the page exists, which is exactly what
    a reader needs to recognise an entry it has already shown."""
    entries: list[str] = []
    newest = ""
    for day in _days(rows):
        date = day["date"]
        link = day_url(date)
        # A placeholder was nobody's choice; the date alone says as much.
        themes = [t for t in day["themes"] if t != untitled_theme(date)]
        titles = " · ".join(themes)
        n = len(day["songs"])
        updated = _stamp(day["last"], date)
        newest = max(newest, updated)
        summary = (
            f"{n} {'song' if n == 1 else 'songs'} shared on {date}"
            + (f" for the theme “{titles}”" if titles else "")
            + " on the Austin mesh #music channel."
        )
        entries.append(
            "<entry>"
            f"<id>{_text(link)}</id>"
            f"<title>{_text(f'{date} — {titles}' if titles else date)}</title>"
            f"<link rel=\"alternate\" type=\"text/html\" href={_attr(link)}/>"
            f"<updated>{updated}</updated>"
            f"<summary>{_text(summary)}</summary>"
            f"<content type=\"html\">{_text(_entry_html(day, link))}</content>"
            "</entry>"
        )
    if not newest:
        newest = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        f"<id>{_text(feed_url)}</id>"
        f"<title>{_text(FEED_TITLE)}</title>"
        f"<subtitle>{_text(FEED_SUBTITLE)}</subtitle>"
        f"<updated>{newest}</updated>"
        f"<link rel=\"self\" type=\"application/atom+xml\" href={_attr(feed_url)}/>"
        f"<link rel=\"alternate\" type=\"text/html\" href={_attr(site_url)}/>"
        "<author><name>MeshRadio</name></author>"
        + "".join(entries)
        + "</feed>"
    )
