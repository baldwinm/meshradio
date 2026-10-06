"""Read-side queries behind the artist pages and the weekly recap.

Kept apart from ``archive.py`` (calendar, search, stats, members) because these
pages slice the archive along new lines — by who made a song, and by week —
rather than by the day it was posted.
"""

from __future__ import annotations

from typing import Any

from .core import Core

# YouTube Music share links resolve to an auto-generated "<Artist> - Topic"
# channel, while a plain YouTube link to the same artist's video names the
# artist's own channel. oEmbed's author_name is the channel either way, so
# without this the same artist would split into two pages.
TOPIC_SUFFIX = " - Topic"

# The same normalisation in SQL, over ``tr.artist``. LIKE is case-insensitive
# for ASCII, which is all the suffix is.
ARTIST_SQL = (
    "TRIM(CASE WHEN tr.artist LIKE '%" + TOPIC_SUFFIX + "' "
    f"THEN substr(tr.artist, 1, length(tr.artist) - {len(TOPIC_SUFFIX)}) "
    "ELSE tr.artist END)"
)


def artist_name(raw: str | None) -> str:
    """An artist as the pages show and link it: the Topic suffix dropped."""
    name = (raw or "").strip()
    if name.lower().endswith(TOPIC_SUFFIX.lower()):
        name = name[: -len(TOPIC_SUFFIX)].strip()
    return name


class BrowseQueries(Core):
    """Artists and weeks, as their pages read them. Radio filler is excluded
    everywhere: nobody on the channel posted it."""

    # -- artists --------------------------------------------------------------

    async def artist_lookup(self, name: str) -> str | None:
        """The channel's most-used spelling of an artist, or ``None`` if no
        channel song names them. Case-insensitive, Topic suffix or not."""
        key = artist_name(name)
        if not key:
            return None
        row = await self._fetchone(
            f"SELECT {ARTIST_SQL} AS artist, COUNT(*) AS n FROM tracks tr "
            f"WHERE tr.source != 'radio' AND {ARTIST_SQL} = ? COLLATE NOCASE "
            "GROUP BY 1 ORDER BY n DESC, artist LIMIT 1",
            (key,),
        )
        return row["artist"] if row else None

    async def artist_profile(self, name: str) -> dict[str, Any]:
        row = await self._fetchone(
            "SELECT COUNT(*) AS shares, COUNT(DISTINCT tr.video_id) AS songs, "
            " COUNT(DISTINCT lower(tr.sender)) AS sharers, COUNT(DISTINCT t.date) AS days, "
            " MIN(t.date) AS first_day, MAX(t.date) AS last_day "
            "FROM tracks tr JOIN themes t ON t.id=tr.theme_id "
            f"WHERE tr.source != 'radio' AND {ARTIST_SQL} = ? COLLATE NOCASE",
            (name,),
        )
        return row or {}

    async def artist_songs(self, name: str, limit: int = 50) -> list[dict[str, Any]]:
        """One row per song, most-shared first: how often, and the last day."""
        return await self._fetchall(
            "SELECT tr.video_id, COALESCE(MAX(tr.title), tr.video_id) AS title, "
            " COUNT(*) AS shares, MAX(t.date) AS last_day "
            "FROM tracks tr JOIN themes t ON t.id=tr.theme_id "
            f"WHERE tr.source != 'radio' AND {ARTIST_SQL} = ? COLLATE NOCASE "
            "GROUP BY tr.video_id ORDER BY shares DESC, last_day DESC LIMIT ?",
            (name, limit),
        )

    async def artist_sharers(self, name: str, limit: int = 10) -> list[dict[str, Any]]:
        """Who keeps posting this artist."""
        return await self._fetchall(
            "SELECT MAX(tr.sender) AS sender, COUNT(*) AS shares FROM tracks tr "
            f"WHERE tr.source != 'radio' AND {ARTIST_SQL} = ? COLLATE NOCASE "
            " AND tr.sender IS NOT NULL AND tr.sender != '' "
            "GROUP BY lower(tr.sender) ORDER BY shares DESC, sender LIMIT ?",
            (name, limit),
        )

    async def artist_themes(self, name: str, limit: int = 25) -> list[dict[str, Any]]:
        """The days the artist turned up, newest first, with that day's theme."""
        return await self._fetchall(
            "SELECT t.date, t.title, COUNT(*) AS tracks FROM tracks tr "
            "JOIN themes t ON t.id=tr.theme_id "
            f"WHERE tr.source != 'radio' AND {ARTIST_SQL} = ? COLLATE NOCASE "
            "GROUP BY t.id ORDER BY t.date DESC LIMIT ?",
            (name, limit),
        )

    async def top_artists(self, limit: int = 15) -> list[dict[str, Any]]:
        """The artists the channel posts most, for the Stats page."""
        return await self._fetchall(
            f"SELECT {ARTIST_SQL} AS artist, COUNT(*) AS shares, "
            " COUNT(DISTINCT lower(tr.sender)) AS sharers FROM tracks tr "
            "WHERE tr.source != 'radio' AND tr.artist IS NOT NULL "
            f" AND {ARTIST_SQL} != '' "
            "GROUP BY 1 COLLATE NOCASE ORDER BY shares DESC, sharers DESC, artist LIMIT ?",
            (limit,),
        )

    # -- weeks ----------------------------------------------------------------

    async def tracks_between(self, first: str, last: str) -> list[dict[str, Any]]:
        """Every channel song filed under a day from ``first`` to ``last``
        (inclusive, ``YYYY-MM-DD``), in posted order — the recap's raw rows."""
        return await self._fetchall(
            "SELECT t.date AS date, t.title AS theme_title, t.set_by, tr.video_id, "
            " tr.title, tr.artist, tr.sender, tr.mesh_ts "
            "FROM tracks tr JOIN themes t ON t.id=tr.theme_id "
            "WHERE tr.source != 'radio' AND t.date BETWEEN ? AND ? "
            "ORDER BY t.date, tr.mesh_ts, tr.id",
            (first, last),
        )

    async def sender_first_days(self) -> dict[str, str]:
        """Each member's first day on the channel, keyed by lowercased name
        (member pages match names case-insensitively, so a retyped name is not
        a newcomer)."""
        rows = await self._fetchall(
            "SELECT lower(tr.sender) AS who, MIN(t.date) AS first_day "
            "FROM tracks tr JOIN themes t ON t.id=tr.theme_id "
            "WHERE tr.source != 'radio' AND tr.sender IS NOT NULL AND tr.sender != '' "
            "GROUP BY lower(tr.sender)"
        )
        return {r["who"]: r["first_day"] for r in rows}

    async def song_first_days(self, video_ids: list[str]) -> dict[str, str]:
        """The first day each of ``video_ids`` was ever posted."""
        if not video_ids:
            return {}
        marks = ",".join("?" * len(video_ids))
        rows = await self._fetchall(
            "SELECT tr.video_id, MIN(t.date) AS first_day "
            "FROM tracks tr JOIN themes t ON t.id=tr.theme_id "
            f"WHERE tr.source != 'radio' AND tr.video_id IN ({marks}) "
            "GROUP BY tr.video_id",
            tuple(video_ids),
        )
        return {r["video_id"]: r["first_day"] for r in rows}
