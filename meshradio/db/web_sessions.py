"""Per-visitor session snapshots (public embed hosting)."""

from __future__ import annotations

import logging

from .core import Core, utcnow

log = logging.getLogger(__name__)


class WebSessionQueries(Core):
    """Session snapshots: upsert, load, reap."""

    async def save_web_session(self, sid: str, state: str) -> None:
        await self.save_web_sessions([(sid, state)])

    async def save_web_sessions(self, items: list[tuple[str, str]]) -> None:
        """Upsert several session snapshots in one commit (periodic flush)."""
        if not items:
            return
        now = utcnow()
        async with self.transaction():
            await self.db.executemany(
                "INSERT INTO web_sessions(sid, updated_at, state) VALUES(?,?,?) "
                "ON CONFLICT(sid) DO UPDATE SET updated_at=excluded.updated_at, "
                "state=excluded.state",
                [(sid, now, state) for sid, state in items],
            )

    async def load_web_session(self, sid: str) -> str | None:
        row = await self._fetchone(
            "SELECT state FROM web_sessions WHERE sid=?", (sid,)
        )
        return row["state"] if row else None

    async def delete_web_sessions(
        self, sids: list[str] | None = None, older_than: str | None = None
    ) -> None:
        async with self.transaction():
            if sids:
                await self.db.executemany(
                    "DELETE FROM web_sessions WHERE sid=?", [(s,) for s in sids]
                )
            if older_than:
                await self.db.execute(
                    "DELETE FROM web_sessions WHERE updated_at < ?", (older_than,)
                )
