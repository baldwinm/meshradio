"""Read-side queries behind the archive pages: calendar, search, stats, members."""

from __future__ import annotations

import logging
from typing import Any

from .core import Core

log = logging.getLogger(__name__)


# Below this many characters a search goes to LIKE: the trigram index can't
# see a shorter query at all.
FTS_MIN_CHARS = 3


def _like_escape(text: str) -> str:
    """Wildcards a visitor typed are literal characters, not a pattern: a
    search for "100%" must not degenerate into a match-everything LIKE."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class ArchiveQueries(Core):
    """The archive as the pages read it."""

    async def archive_days(self) -> list[dict[str, Any]]:
        """One row per archived day, newest first: theme count, song count, and
        the theme title(s) — enough for the Archive calendar to label a cell
        without a query per day. ``titles`` is comma-joined on the rare day that
        predates locked themes and still carries more than one."""
        return await self._fetchall(
            "SELECT t.date AS date, COUNT(DISTINCT t.id) AS themes, COUNT(tr.id) AS tracks, "
            "GROUP_CONCAT(DISTINCT t.title) AS titles "
            "FROM themes t LEFT JOIN tracks tr ON tr.theme_id=t.id "
            "GROUP BY t.date ORDER BY t.date DESC"
        )

    async def newest_day_with_tracks(self) -> str | None:
        """The most recent archive day holding at least one song — where a
        new visitor lands. ``archive_days`` answers the same question by
        aggregating the whole history; this is one lookup, and it runs on
        every request from an idle session until today's first song lands."""
        row = await self._fetchone(
            "SELECT MAX(t.date) AS date FROM themes t "
            "WHERE EXISTS (SELECT 1 FROM tracks tr WHERE tr.theme_id=t.id)"
        )
        return row["date"] if row and row["date"] else None

    async def recent_days_tracks(self, days: int = 30) -> list[dict[str, Any]]:
        """Every channel song on the newest ``days`` days that have any — the
        feed's source. Newest day first, and within a day in posted order.

        The days are picked from ``themes`` with a per-theme EXISTS (the shape
        ``newest_day_with_tracks`` uses) rather than by DISTINCT over a
        themes×tracks join: this walks one row per day down the date index and
        stops at the limit, where the join read every track to find the dates.
        A day with a theme but no songs isn't an entry worth a subscriber's
        attention, so it's left out."""
        return await self._fetchall(
            "SELECT t.date AS date, t.title AS theme_title, tr.video_id, tr.title, "
            " tr.artist, tr.sender, tr.mesh_ts "
            "FROM tracks tr JOIN themes t ON t.id=tr.theme_id "
            "WHERE tr.source != 'radio' AND t.date IN ("
            " SELECT t2.date FROM themes t2 WHERE EXISTS ("
            "  SELECT 1 FROM tracks tr2 WHERE tr2.theme_id=t2.id AND tr2.source != 'radio') "
            " GROUP BY t2.date ORDER BY t2.date DESC LIMIT ?) "
            "ORDER BY t.date DESC, tr.mesh_ts, tr.id",
            (days,),
        )

    async def all_themes(self) -> list[dict[str, Any]]:
        """Every theme the channel actually used, newest first, with its song
        count — the Archive's theme list.

        ``Untitled — <date>`` placeholders are left out: nobody chose them, and
        the day they hold is still on the calendar. Ordering matches the
        calendar's (newest first), with a day's rare second theme — only
        possible on days predating locked themes — in creation order."""
        return await self._fetchall(
            "SELECT t.id, t.date, t.title, t.set_by, COUNT(tr.id) AS tracks "
            "FROM themes t LEFT JOIN tracks tr ON tr.theme_id=t.id "
            "WHERE t.title NOT LIKE 'Untitled — %' "
            "GROUP BY t.id ORDER BY t.date DESC, t.created_at, t.id"
        )

    async def random_channel_tracks(
        self,
        limit: int = 10,
        exclude_video_ids: list[str] | tuple[str, ...] = (),
        ready_only: bool = False,
    ) -> list[dict[str, Any]]:
        """A random sample of songs the channel actually shared.

        Feeds the player's archive station (§7): unlike radio mode this needs no
        network, so it works on the public embed host where YouTube is
        unreachable. Radio filler and themeless rows are excluded — this replays
        the channel, not a Mix. ``ready_only`` demands a downloaded file (the
        appliance's rule); embed hosting streams by video id, so there only
        'failed' rows are skipped. Both mirror ``PlayerService._is_playable``,
        pushed into SQL so a random sample can't come back all-unplayable."""
        clauses = ["source != 'radio'", "theme_id IS NOT NULL"]
        if ready_only:
            clauses += ["cache_status = 'ready'", "cache_path IS NOT NULL"]
        else:
            clauses.append("cache_status != 'failed'")
        params: list[Any] = []
        if exclude_video_ids:
            clauses.append(f"video_id NOT IN ({','.join('?' * len(exclude_video_ids))})")
            params.extend(exclude_video_ids)
        params.append(limit)
        return await self._fetchall(
            f"SELECT * FROM tracks WHERE {' AND '.join(clauses)} ORDER BY RANDOM() LIMIT ?",
            tuple(params),
        )

    async def themes_for_day(self, date: str) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT t.*, COUNT(tr.id) AS track_count FROM themes t "
            "LEFT JOIN tracks tr ON tr.theme_id=t.id "
            "WHERE t.date=? GROUP BY t.id ORDER BY t.created_at",
            (date,),
        )

    async def tracks_for_theme(self, theme_id: int) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT * FROM tracks WHERE theme_id=? ORDER BY mesh_ts", (theme_id,)
        )

    async def tracks_for_day(self, date: str) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT tr.* FROM tracks tr JOIN themes t ON tr.theme_id=t.id "
            "WHERE t.date=? ORDER BY tr.mesh_ts",
            (date,),
        )


    # -- search & stats -------------------------------------------------------

    async def search_tracks(
        self,
        query: str,
        limit: int = 100,
        sender: str | None = None,
        year: str | None = None,
    ) -> list[dict[str, Any]]:
        """Channel songs whose title, artist, sharer, or theme matches
        ``query`` (case-insensitive substring), best match first.

        Three characters and up are answered by the trigram FTS index
        (migration v13): the same substring match LIKE made, with the
        Unicode case folding LIKE never had, from an index instead of four
        scans of the join. The index can't see a shorter query, so one or
        two characters still take the LIKE path, with its wildcards escaped
        so "100%" searches for the literal text either way.

        ``sender`` and ``year`` narrow the result without naming a song, and
        are a search on their own: "everything Ana shared in 2026" is a
        question with no query text in it. With none of the three set there
        is nothing to search for, and the answer is empty rather than the
        whole archive.

        A row is a *song*, not a share. The same video posted on eight days
        was eight rows, which buried seven other songs; it is now one row
        carrying ``shares`` and ``sharers`` counts, with the track columns
        taken from the newest share — SQLite hands a bare column the row its
        single min/max aggregate picked, so the id a result offers to queue
        is the most recent copy. Ordering puts a title that matches exactly
        above one that merely starts with the query, then a title hit above
        an artist hit, above a match that was only on the sharer or the
        theme; ties go to the newest share. The tiers are LIKE, so they fold
        ASCII case only: ``CAFÉ`` still finds ``café`` through the index, it
        just doesn't win the exact-match tier."""
        query = query.strip()
        if not (query or sender or year):
            return []

        where = ["tr.source != 'radio'"]
        params: list[Any] = []
        rank, rank_params = "0", []

        if query:
            if len(query) >= FTS_MIN_CHARS:
                # One quoted phrase: every character in it is literal to FTS5.
                match = '"' + query.replace('"', '""') + '"'
                where.append(
                    "(tr.id IN (SELECT rowid FROM tracks_fts WHERE tracks_fts MATCH ?)"
                    " OR tr.theme_id IN (SELECT rowid FROM themes_fts WHERE themes_fts MATCH ?))"
                )
                params += [match, match]
            else:
                like = f"%{_like_escape(query)}%"
                where.append(
                    "(tr.title LIKE ? ESCAPE '\\' OR tr.artist LIKE ? ESCAPE '\\'"
                    " OR tr.sender LIKE ? ESCAPE '\\' OR t.title LIKE ? ESCAPE '\\')"
                )
                params += [like] * 4
            escaped = _like_escape(query)
            rank = (
                "CASE WHEN tr.title LIKE ? ESCAPE '\\' THEN 0"
                " WHEN tr.title LIKE ? ESCAPE '\\' THEN 1"
                " WHEN tr.title LIKE ? ESCAPE '\\' THEN 2"
                " WHEN tr.artist LIKE ? ESCAPE '\\' THEN 3 ELSE 4 END"
            )
            rank_params = [escaped, f"{escaped}%", f"%{escaped}%", f"%{escaped}%"]

        if sender:
            where.append("tr.sender = ? COLLATE NOCASE")
            params.append(sender)
        if year:
            where.append("substr(t.date, 1, 4) = ?")
            params.append(year)

        return await self._fetchall(
            "SELECT tr.*, t.date AS date, t.title AS theme_title, "
            " MAX(tr.mesh_ts) AS newest_ts, COUNT(*) AS shares, "
            f" COUNT(DISTINCT tr.sender) AS sharers, {rank} AS match_rank "
            "FROM tracks tr JOIN themes t ON tr.theme_id=t.id "
            f"WHERE {' AND '.join(where)} "
            "GROUP BY tr.video_id "
            "ORDER BY match_rank, newest_ts DESC LIMIT ?",
            tuple(rank_params + params + [limit]),
        )

    async def search_filters(self) -> dict[str, list[str]]:
        """What the search page offers to filter by: every member who has
        posted, and every year the archive covers. Both are short lists that
        change only when a song or a theme lands, so the page reads them
        from the aggregate cache rather than querying them per visit. Names
        are grouped case-insensitively, the way a member page resolves one,
        so a member who once typed their name differently is one option."""
        senders = await self._fetchall(
            "SELECT sender FROM tracks "
            "WHERE source != 'radio' AND sender IS NOT NULL AND sender != '' "
            "GROUP BY sender COLLATE NOCASE ORDER BY sender COLLATE NOCASE"
        )
        years = await self._fetchall(
            "SELECT DISTINCT substr(date, 1, 4) AS year FROM themes ORDER BY year DESC"
        )
        return {
            "senders": [r["sender"] for r in senders],
            "years": [r["year"] for r in years],
        }

    async def overall_stats(self) -> dict[str, Any]:
        row = await self._fetchone(
            "SELECT "
            " (SELECT COUNT(*) FROM tracks WHERE source!='radio') AS shares,"
            " (SELECT COUNT(DISTINCT video_id) FROM tracks WHERE source!='radio') AS songs,"
            " (SELECT COUNT(DISTINCT sender) FROM tracks WHERE source!='radio') AS sharers,"
            " (SELECT COUNT(*) FROM themes) AS themes,"
            " (SELECT COUNT(DISTINCT date) FROM themes) AS days"
        )
        return row or {}

    async def top_songs(self, limit: int = 15) -> list[dict[str, Any]]:
        """Most-shared songs. A song is one row per day it was posted (same-day
        reposts collapse into one), so this counts distinct days it charted."""
        return await self._fetchall(
            "SELECT video_id, COALESCE(MAX(title), video_id) AS title, MAX(artist) AS artist,"
            " COUNT(*) AS shares, COUNT(DISTINCT sender) AS sharers "
            "FROM tracks WHERE source!='radio' "
            "GROUP BY video_id ORDER BY shares DESC, sharers DESC, title LIMIT ?",
            (limit,),
        )

    async def top_sharers(self, limit: int = 15) -> list[dict[str, Any]]:
        """Most active members by tracks posted."""
        return await self._fetchall(
            "SELECT sender, COUNT(*) AS shares, COUNT(DISTINCT video_id) AS songs "
            "FROM tracks WHERE source!='radio' AND sender IS NOT NULL AND sender!='' "
            "GROUP BY sender ORDER BY shares DESC, songs DESC LIMIT ?",
            (limit,),
        )

    async def play_totals(self) -> dict[str, Any]:
        """Plays, distinct songs played, and how many ran to the end."""
        row = await self._fetchone(
            "SELECT COUNT(*) AS plays, COUNT(DISTINCT track_id) AS tracks, "
            " COALESCE(SUM(completed), 0) AS finished FROM plays"
        )
        return row or {}


    # -- members --------------------------------------------------------------

    async def member_name(self, name: str) -> str | None:
        """The channel's own spelling of a member's name, or ``None`` if nobody
        by that name ever posted. Names arrive as typed on the mesh, so lookups
        are case-insensitive and the most-used spelling wins the page title."""
        row = await self._fetchone(
            "SELECT sender, COUNT(*) AS n FROM tracks "
            "WHERE sender = ? COLLATE NOCASE AND sender IS NOT NULL AND sender != '' "
            "GROUP BY sender ORDER BY n DESC LIMIT 1",
            (name,),
        )
        return row["sender"] if row else None

    async def member_profile(self, name: str) -> dict[str, Any]:
        """One member's channel record: how much they've shared and over what
        span. Radio filler is excluded — it was never theirs."""
        row = await self._fetchone(
            "SELECT COUNT(*) AS shares, COUNT(DISTINCT tr.video_id) AS songs, "
            " COUNT(DISTINCT t.date) AS days, MIN(t.date) AS first_day, "
            " MAX(t.date) AS last_day "
            "FROM tracks tr JOIN themes t ON t.id=tr.theme_id "
            "WHERE tr.sender = ? COLLATE NOCASE AND tr.source != 'radio'",
            (name,),
        )
        return row or {}

    async def member_tracks(self, name: str, limit: int = 50) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT tr.*, t.date AS date, t.title AS theme_title "
            "FROM tracks tr JOIN themes t ON t.id=tr.theme_id "
            "WHERE tr.sender = ? COLLATE NOCASE AND tr.source != 'radio' "
            "ORDER BY tr.mesh_ts DESC LIMIT ?",
            (name, limit),
        )

    async def member_themes(self, name: str, limit: int = 25) -> list[dict[str, Any]]:
        """Days this member named. Placeholders aren't anyone's doing."""
        return await self._fetchall(
            "SELECT t.date, t.title, COUNT(tr.id) AS tracks FROM themes t "
            "LEFT JOIN tracks tr ON tr.theme_id=t.id "
            "WHERE t.set_by = ? COLLATE NOCASE AND t.title NOT LIKE 'Untitled — %' "
            "GROUP BY t.id ORDER BY t.date DESC LIMIT ?",
            (name, limit),
        )

    async def member_artists(self, name: str, limit: int = 5) -> list[dict[str, Any]]:
        """The artists a member keeps coming back to."""
        return await self._fetchall(
            "SELECT artist, COUNT(*) AS shares FROM tracks "
            "WHERE sender = ? COLLATE NOCASE AND source != 'radio' "
            " AND artist IS NOT NULL AND artist != '' "
            "GROUP BY artist COLLATE NOCASE ORDER BY shares DESC, artist LIMIT ?",
            (name, limit),
        )

    async def busiest_themes(self, limit: int = 10) -> list[dict[str, Any]]:
        """Themes that drew the most songs."""
        return await self._fetchall(
            "SELECT t.date, t.title, COUNT(tr.id) AS tracks FROM themes t "
            "JOIN tracks tr ON tr.theme_id=t.id AND tr.source!='radio' "
            "GROUP BY t.id ORDER BY tracks DESC, t.date DESC LIMIT ?",
            (limit,),
        )
