"""The weekly recap: one channel week summed up, as a page and as a feed.

Weeks run Sunday to Saturday, the same as the Archive calendar's rows, and a
week is named by its Sunday (``/week/2026-10-04``). Everything here is pure
functions over the rows ``Database.tracks_between`` returns, so the summary
and the feed markup are testable without a server.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from html import escape as html_escape
from typing import Any

from ..db.browse import artist_name
from ..ingest.parse import untitled_theme
from .feed import _attr, _stamp, _text

# How many finished weeks the weekly feed carries — about a quarter.
WEEKLY_FEED_WEEKS = 12

# How long each board on the page (and in a feed entry) runs.
BOARD_LEN = 5

WEEKLY_FEED_TITLE = "MeshRadio — Austin #music, weekly"
WEEKLY_FEED_SUBTITLE = (
    "One entry per week: the themes, who shared, and who was new on the "
    "Austin MeshCore #music channel."
)


def week_start(iso: str) -> str:
    """The Sunday that opens the week holding ``iso``."""
    d = date.fromisoformat(iso)
    return (d - timedelta(days=(d.weekday() + 1) % 7)).isoformat()


def week_end(start: str) -> str:
    """The Saturday that closes the week opened by ``start``."""
    return (date.fromisoformat(start) + timedelta(days=6)).isoformat()


def week_label(start: str) -> str:
    """``2026-10-04`` -> ``Oct 4 – 10, 2026``; a week across a month or year
    boundary names both ends in full."""
    a = date.fromisoformat(start)
    b = a + timedelta(days=6)
    if a.year != b.year:
        return f"{a:%b} {a.day}, {a.year} – {b:%b} {b.day}, {b.year}"
    if a.month != b.month:
        return f"{a:%b} {a.day} – {b:%b} {b.day}, {b.year}"
    return f"{a:%b} {a.day} – {b.day}, {b.year}"


def week_starts(days: list[dict[str, Any]]) -> list[str]:
    """The Sundays of every week with a song in it, oldest first — the recap's
    prev/next steps, so a quiet week is never a destination."""
    return sorted({week_start(d["date"]) for d in days if d.get("tracks")})


def summarize_week(
    start: str,
    rows: list[dict[str, Any]],
    sender_firsts: dict[str, str],
    song_firsts: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Everything the recap shows for the week opened by ``start``.

    ``rows`` are that week's songs (``Database.tracks_between``),
    ``sender_firsts`` each member's first day (lowercased name ->
    ``YYYY-MM-DD``), and ``song_firsts`` the first day each of the week's songs
    was ever posted, for the "heard before" board; leave it out to skip it."""
    end = week_end(start)
    days: dict[str, dict[str, Any]] = {}
    sharers: Counter[str] = Counter()
    spelling: dict[str, str] = {}
    artists: Counter[str] = Counter()
    artist_spelling: dict[str, str] = {}
    for row in rows:
        day = days.setdefault(row["date"], {
            "date": row["date"],
            "weekday": f"{date.fromisoformat(row['date']):%a}",
            "themes": [], "set_by": None, "tracks": 0,
        })
        title = row["theme_title"]
        if title != untitled_theme(row["date"]) and title not in day["themes"]:
            day["themes"].append(title)
            day["set_by"] = day["set_by"] or row.get("set_by")
        day["tracks"] += 1
        if row["sender"]:
            who = row["sender"].lower()
            sharers[who] += 1
            spelling.setdefault(who, row["sender"])
        name = artist_name(row["artist"])
        if name:
            artists[name.lower()] += 1
            artist_spelling.setdefault(name.lower(), name)

    by_day = sorted(days.values(), key=lambda d: d["date"])
    busiest = max(by_day, key=lambda d: d["tracks"], default=None)
    new_faces = sorted(
        (spelling[who] for who in sharers if start <= sender_firsts.get(who, "") <= end),
        key=str.lower,
    )
    heard_before = []
    if song_firsts:
        seen: set[str] = set()
        for row in rows:
            first = song_firsts.get(row["video_id"])
            if first and first < start and row["video_id"] not in seen:
                seen.add(row["video_id"])
                heard_before.append({**row, "first_day": first})

    return {
        "start": start,
        "end": end,
        "label": week_label(start),
        "shares": len(rows),
        "songs": len({r["video_id"] for r in rows}),
        "sharers": len(sharers),
        "days": by_day,
        # Only worth saying when there was more than one day to pick from.
        "busiest": busiest if len(by_day) > 1 else None,
        "top_sharers": [
            {"sender": spelling[who], "shares": n}
            for who, n in sorted(sharers.items(), key=lambda kv: (-kv[1], kv[0]))[:BOARD_LEN]
        ],
        "top_artists": [
            {"artist": artist_spelling[key], "shares": n}
            for key, n in sorted(artists.items(), key=lambda kv: (-kv[1], kv[0]))[:BOARD_LEN]
        ],
        "new_faces": new_faces,
        "heard_before": heard_before[:BOARD_LEN],
    }


def finished_weeks(today: str, starts: list[str], limit: int = WEEKLY_FEED_WEEKS) -> list[str]:
    """The newest ``limit`` week starts whose Saturday is already behind
    ``today``, newest first. A week still running isn't a digest yet: its
    entry would change under a reader that had already shown it."""
    done = [s for s in starts if week_end(s) < today]
    return sorted(done, reverse=True)[:limit]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def week_summary_line(week: dict[str, Any]) -> str:
    """One sentence for a link preview or a feed summary."""
    themes = [t for d in week["days"] for t in d["themes"]]
    line = (
        f"{_plural(week['shares'], 'song')} from {_plural(week['sharers'], 'member')} "
        f"over {_plural(len(week['days']), 'day')} on the Austin mesh #music channel"
    )
    return line + (f": {' · '.join(themes)}." if themes else ".")


def _entry_html(week: dict[str, Any], day_url: Callable[[str], str]) -> str:
    items = []
    for d in week["days"]:
        label = " · ".join(d["themes"]) or "No theme"
        items.append(
            f'<li><a href="{html_escape(day_url(d["date"]))}">{html_escape(d["weekday"])} '
            f'{html_escape(d["date"])}</a>: {html_escape(label)} '
            f'({_plural(d["tracks"], "song")})</li>'
        )
    parts = [f"<ol>{''.join(items)}</ol>"]
    if week["top_sharers"]:
        who = ", ".join(
            f"{html_escape(s['sender'])} ({s['shares']})" for s in week["top_sharers"]
        )
        parts.append(f"<p>Most shares: {who}</p>")
    if week["new_faces"]:
        parts.append(
            "<p>New this week: " + ", ".join(html_escape(n) for n in week["new_faces"]) + "</p>"
        )
    return "".join(parts)


def build_weekly_feed(
    weeks: list[dict[str, Any]],
    *,
    feed_url: str,
    site_url: str,
    week_url: Callable[[str], str],
    day_url: Callable[[str], str],
    now: datetime | None = None,
) -> str:
    """The Atom document for ``weeks`` (``summarize_week`` results, newest
    first). A week's page is its entry's id, as a day's is in /feed.xml."""
    entries: list[str] = []
    newest = ""
    for week in weeks:
        link = week_url(week["start"])
        # Stamped at the week's close: the entry appears once, when it's done.
        updated = _stamp(None, (date.fromisoformat(week["end"]) + timedelta(days=1)).isoformat())
        newest = max(newest, updated)
        entries.append(
            "<entry>"
            f"<id>{_text(link)}</id>"
            f"<title>{_text('Week of ' + week['label'])}</title>"
            f"<link rel=\"alternate\" type=\"text/html\" href={_attr(link)}/>"
            f"<updated>{updated}</updated>"
            f"<summary>{_text(week_summary_line(week))}</summary>"
            f"<content type=\"html\">{_text(_entry_html(week, day_url))}</content>"
            "</entry>"
        )
    if not newest:
        newest = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        f"<id>{_text(feed_url)}</id>"
        f"<title>{_text(WEEKLY_FEED_TITLE)}</title>"
        f"<subtitle>{_text(WEEKLY_FEED_SUBTITLE)}</subtitle>"
        f"<updated>{newest}</updated>"
        f"<link rel=\"self\" type=\"application/atom+xml\" href={_attr(feed_url)}/>"
        f"<link rel=\"alternate\" type=\"text/html\" href={_attr(site_url)}/>"
        "<author><name>MeshRadio</name></author>"
        + "".join(entries)
        + "</feed>"
    )
