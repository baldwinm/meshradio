"""Shared request context for the web routers.

One WebContext rides on ``app.state.ctx``; route handlers pull it from the
request instead of closing over create_app locals. ``get_player`` is the
session/communal fork: per-visitor players in embed mode, the appliance's
single player otherwise.
"""

from __future__ import annotations

from calendar import Calendar, month_name
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from time import monotonic
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

# Sunday-first weeks (US convention; the channel is Austin-local).
WEEKDAY_HEADERS = ["S", "M", "T", "W", "T", "F", "S"]
_CAL = Calendar(firstweekday=6)

from ..bus import EventBus
from ..db import Database
from ..media.player import PlayerService
from .sessions import SESSION_COOKIE, SessionManager, SpeakerRegistry

# YouTube's anonymous "make a playlist from these ids" endpoint. Undocumented
# but long-standing; gets unreliable past ~50 ids, so we cap.
YT_WATCH_VIDEOS = "https://www.youtube.com/watch_videos?video_ids="
YT_EXPORT_CAP = 50


def yt_export_url(tracks: list[dict[str, Any]]) -> str:
    """A YouTube playlist link for a day's tracks, in posted order, deduped.
    Empty string when the day has no songs."""
    ids: list[str] = []
    for track in tracks:
        vid = track.get("video_id")
        if vid and vid not in ids:
            ids.append(vid)
    return YT_WATCH_VIDEOS + ",".join(ids[:YT_EXPORT_CAP]) if ids else ""


def archive_months(days: list[dict[str, Any]]) -> list[str]:
    """The ``YYYY-MM`` months the channel was alive in, oldest first. The Archive
    page steps through these, so an empty month is never a destination."""
    return sorted({d["date"][:7] for d in days})


def month_label(key: str) -> str:
    """``2026-07`` -> ``July 2026``."""
    year, month = (int(part) for part in key.split("-"))
    return f"{month_name[month]} {year}"


def month_step(key: str | None) -> dict[str, str] | None:
    """A prev/next target for the Archive page's month nav — ``None`` at the
    ends of history, where the button isn't drawn."""
    return {"key": key, "label": month_label(key)} if key else None


def calendar_month(days: list[dict[str, Any]], key: str) -> dict[str, Any]:
    """One month (``key`` is ``YYYY-MM``) as a calendar grid for the Archive page.

    ``{label, key, weeks}`` where ``weeks`` is a list of 7-cell rows. A cell is
    ``None`` where the week spills into an adjacent month (rendered blank),
    otherwise ``{day, iso, info}`` — ``info`` being that day's archive row
    (title, track count) or ``None`` for a day the channel was quiet."""
    by_date = {d["date"]: d for d in days}
    year, month = (int(part) for part in key.split("-"))
    weeks: list[list[dict[str, Any] | None]] = []
    for week in _CAL.monthdatescalendar(year, month):
        row: list[dict[str, Any] | None] = []
        for dt in week:
            if dt.month != month:
                row.append(None)  # spillover day owned by the neighbor month
                continue
            iso = dt.isoformat()
            row.append({"day": dt.day, "iso": iso, "info": by_date.get(iso)})
        weeks.append(row)
    return {"label": month_label(key), "key": key, "weeks": weeks}


def absolute_url(request: Request, path: str | None = None) -> str:
    """A full ``https://host/path`` URL for this request (or for ``path`` on the
    same host) — what link previews, canonical links and the sitemap need.

    Hosted deployments sit behind a TLS-terminating proxy, so the scheme the app
    sees is plain http; ``x-forwarded-proto`` is the one that matches the URL a
    visitor would paste. Query strings are dropped: a preview for
    ``/archive?m=2026-08`` is a preview of the archive."""
    url = request.url.replace(query="", fragment="") if path is None else (
        request.base_url.replace(path=path, query="", fragment="")
    )
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    if forwarded:
        url = url.replace(scheme=forwarded)
    return str(url)


def yt_thumbnail(video_id: str | None) -> str:
    """The 480×360 still for a video — the image a shared link previews with.
    ``hqdefault`` because every video has one; ``maxresdefault`` 404s on plenty."""
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else ""


def archive_years(themes: list[dict[str, Any]]) -> list[str]:
    """The years the channel ran themes in, oldest first — the theme list pages
    through these the way the calendar pages through months."""
    return sorted({t["date"][:4] for t in themes})


def year_step(key: str | None) -> dict[str, str] | None:
    """A prev/next target for the theme list's year nav, or ``None`` at the ends
    of history (where the arrow is drawn dead)."""
    return {"key": key, "label": key} if key else None


def theme_key(title: str) -> str:
    """A theme title as its identity across days: case- and spacing-insensitive,
    so ``Rain songs`` and ``rain  songs`` are one theme run twice."""
    return " ".join(title.split()).casefold()


def theme_history(
    themes: list[dict[str, Any]], all_themes: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Themes grouped into month sections for the Archive's theme list.

    ``themes`` comes from ``Database.all_themes`` (newest first), so the months
    fall out by walking it in order. Each theme carries ``runs`` — how many days
    used that title — so a theme the channel has come back to says so wherever
    it appears. ``all_themes`` is the whole history when ``themes`` is only the
    year on screen: the count has to span history, or a repeat looks like a
    first outing whenever the pair straddles a year boundary."""
    runs: Counter[str] = Counter(
        theme_key(t["title"]) for t in (all_themes if all_themes is not None else themes)
    )
    months: list[dict[str, Any]] = []
    for theme in themes:
        key = theme["date"][:7]
        if not months or months[-1]["key"] != key:
            months.append({"key": key, "label": month_label(key), "themes": []})
        months[-1]["themes"].append({**theme, "runs": runs[theme_key(theme["title"])]})
    return months


@dataclass
class WebContext:
    bus: EventBus
    db: Database
    player: PlayerService          # the communal (appliance) player
    audio_router: Any              # output routing (speaker/bluetooth/...)
    ingest: Any
    ingest_token: str
    sessions: SessionManager | None
    templates: Jinja2Templates
    speakers: SpeakerRegistry      # communal speaker election
    health: dict
    _days: tuple[float, list[dict[str, Any]]] | None = None

    # Every player-state push makes each open page re-fetch the now-playing and
    # day-nav partials, and both rebuild day_context — so archive_days(), which
    # aggregates the whole themes×tracks join, ran twice per event per tab. It
    # only changes when a song lands, so a few seconds of staleness costs
    # nothing (at worst the day arrows lag one event) and takes the query off
    # the hot path entirely.
    DAYS_TTL_S = 5.0

    async def archive_days(self) -> list[dict[str, Any]]:
        """``Database.archive_days`` behind a short TTL (see DAYS_TTL_S)."""
        now = monotonic()
        if self._days is not None and now - self._days[0] < self.DAYS_TTL_S:
            return self._days[1]
        days = await self.db.archive_days()
        self._days = (now, days)
        return days

    async def get_player(self, request: Request) -> PlayerService:
        if self.sessions is None:
            return self.player
        sid = getattr(request.state, "sid", None) or request.cookies.get(SESSION_COOKIE)
        return (await self.sessions.get(sid or "anonymous")).player

    def today(self) -> str:
        return datetime.now(self.player.tz).date().isoformat()

    async def day_context(self, p: PlayerService) -> dict:
        """The day being played (or today), its theme(s), and the adjacent
        archive days for the prev/next navigation."""
        today = self.today()
        day = p.day or today
        themes = await self.db.themes_for_day(day)
        days = sorted(d["date"] for d in await self.archive_days() if d["tracks"])
        return {
            "day": day,
            "today": today,
            "theme_titles": [t["title"] for t in themes],
            "prev_day": max((d for d in days if d < day), default=None),
            "next_day": min((d for d in days if day < d <= today), default=None),
            # Whole-day export, independent of playback — every song for the day,
            # not just what's still queued.
            "yt_export_url": yt_export_url(await self.db.tracks_for_day(day)),
        }

    # -- partial renderers (shared by page and API routes) -------------------

    async def render_now_playing(self, request: Request):
        p = await self.get_player(request)
        return self.templates.TemplateResponse(
            request, "partials/now_playing.html",
            {"state": p.state(), **await self.day_context(p)},
        )

    async def render_queue(self, request: Request):
        return self.templates.TemplateResponse(
            request, "partials/queue.html",
            {"state": (await self.get_player(request)).state()},
        )

    async def render_day_nav(self, request: Request):
        p = await self.get_player(request)
        return self.templates.TemplateResponse(
            request, "partials/day_nav.html",
            {"state": p.state(), **await self.day_context(p)},
        )


def ctx_of(request_or_ws) -> WebContext:
    return request_or_ws.app.state.ctx
