"""Theme rows: a day's playlist and its lock."""

from __future__ import annotations

import logging
from typing import Any

from .core import Core, _next_second, utcnow
from .fields import (
    MAX_SENDER,
    MAX_TITLE,
    clean_text,
)

log = logging.getLogger(__name__)


class ThemeQueries(Core):
    """Themes: one per day, locked once a real one is set."""

    async def create_theme(
        self,
        date: str,
        title: str,
        set_by: str | None = None,
        raw_message: str | None = None,
        locked: bool = False,
    ) -> dict[str, Any]:
        """Insert a theme; on (date, title) conflict return the existing row.

        ``locked`` marks the day's theme as final: the ingest pipeline refuses
        to reset a locked theme, so a later "Theme: …" message can't spawn a
        rival playlist. Auto-created "Untitled —" placeholders stay unlocked so
        the real theme can still adopt them (see ``adopt_theme``)."""
        cleaned = clean_text(title, MAX_TITLE)
        if cleaned is None:
            raise ValueError("a theme needs a title")
        title = cleaned
        set_by = clean_text(set_by, MAX_SENDER)
        # RETURNING (not lastrowid, which is unreliable after DO NOTHING)
        # distinguishes a fresh insert from a conflict no-op.
        async with self.transaction():
            cur = await self.db.execute(
                "INSERT INTO themes(date,title,set_by,raw_message,created_at,locked) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(date,title) DO NOTHING RETURNING id",
                (date, title, set_by, raw_message, utcnow(), int(locked)),
            )
            inserted = await cur.fetchone()
            if inserted:
                row = await self._fetchone("SELECT * FROM themes WHERE id=?", (inserted["id"],))
            else:
                row = await self._fetchone(
                    "SELECT * FROM themes WHERE date=? AND title=?", (date, title)
                )
        assert row is not None
        return row

    async def adopt_theme(
        self, theme_id: int, title: str, set_by: str | None = None, raw_message: str | None = None
    ) -> dict[str, Any]:
        """Give an existing (unlocked) theme a real title and lock it.

        Used when links arrived before the theme was announced: the day's
        "Untitled —" placeholder is renamed in place so every early track stays
        in the one playlist instead of being stranded on a separate theme.

        Stamps ``updated_at`` so the relay re-pushes the adoption: created_at
        stays put (it still says when the day's playlist opened), and
        ``themes_since`` orders on updated_at when present. It is forced past
        created_at because the relay's cursor is usually sitting on exactly
        this row — the push that skipped the placeholder — and an equal
        second would leave the adoption invisible to it."""
        cleaned = clean_text(title, MAX_TITLE)
        if cleaned is None:
            raise ValueError("a theme needs a title")
        title = cleaned
        set_by = clean_text(set_by, MAX_SENDER)
        async with self.transaction():
            row = await self._fetchone("SELECT created_at FROM themes WHERE id=?", (theme_id,))
            assert row is not None
            updated_at = max(utcnow(), _next_second(row["created_at"]))
            await self.db.execute(
                "UPDATE themes SET title=?, set_by=?, raw_message=?, locked=1, updated_at=? "
                "WHERE id=?",
                (title, set_by, raw_message, updated_at, theme_id),
            )
            row = await self._fetchone("SELECT * FROM themes WHERE id=?", (theme_id,))
        assert row is not None
        return row

    async def rename_theme(self, theme_id: int, title: str) -> dict[str, Any]:
        """Retitle a theme by hand, keeping it locked and keeping its playlist.

        The escape hatch for a title the channel got wrong: a mangled parse (a
        ``:-)`` in the post splits before the real title), a typo, a theme
        announced under the wrong words. Ingest can't fix these — the day's
        theme locks on the first ``Theme:`` post and every later one is
        ignored — and the row can't just be replaced either, because the day's
        tracks hang off this ``id``.

        ``set_by`` and ``created_at`` are left alone: they still record who
        opened the day and when. ``raw_message`` is rewritten to a canonical
        ``Theme: <title>``, because the relay replays it verbatim as the
        channel message — leaving the original there would re-broadcast the
        bad title to any receiver rebuilding its archive from the relay.

        Raises ``sqlite3.IntegrityError`` if the date already has a theme with
        this title (UNIQUE(date, title)); callers report that as a no-op."""
        cleaned = clean_text(title, MAX_TITLE)
        if cleaned is None:
            raise ValueError("a theme needs a title")
        title = cleaned
        async with self.transaction():
            row = await self._fetchone("SELECT created_at FROM themes WHERE id=?", (theme_id,))
            assert row is not None
            # Same nudge as adopt_theme: strictly past created_at, so a relay
            # cursor parked on this row still sees the change.
            updated_at = max(utcnow(), _next_second(row["created_at"]))
            await self.db.execute(
                "UPDATE themes SET title=?, raw_message=?, locked=1, updated_at=? WHERE id=?",
                (title, f"Theme: {title}", updated_at, theme_id),
            )
            row = await self._fetchone("SELECT * FROM themes WHERE id=?", (theme_id,))
        assert row is not None
        return row

    async def latest_theme_for_date(self, date: str) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM themes WHERE date=? ORDER BY created_at DESC, id DESC LIMIT 1",
            (date,),
        )

    async def theme_by_id(self, theme_id: int) -> dict[str, Any] | None:
        return await self._fetchone("SELECT * FROM themes WHERE id=?", (theme_id,))
