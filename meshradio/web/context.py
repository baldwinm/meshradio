"""Shared request context for the web routers.

One WebContext rides on ``app.state.ctx``; route handlers pull it from the
request instead of closing over create_app locals. ``get_player`` is the
session/communal fork: per-visitor players in embed mode, the appliance's
single player otherwise.
"""

from __future__ import annotations

from calendar import Calendar, month_name
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from starlette.datastructures import URL

from ..bus import EventBus
from ..db import Database
from ..media.player import PlayerService
from .feed import FEED_DAYS
from .recap import finished_weeks, summarize_week, week_end, week_start, week_starts
from .sessions import SessionManager, SpeakerRegistry

# Sunday-first weeks (US convention; the channel is Austin-local).
WEEKDAY_HEADERS = ["S", "M", "T", "W", "T", "F", "S"]
_CAL = Calendar(firstweekday=6)

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


def proxy_trusted(request: Request) -> bool:
    """Did this request come through a proxy whose forwarding headers we
    believe (``[web] trusted_proxies``)? With uvicorn's own proxy handling on
    the same list, a trusted proxy has already been replaced by the visitor
    in ``request.client``; what's left to check here is the direct peer."""
    trusted = getattr(request.app.state, "trusted_proxies", frozenset({"127.0.0.1"}))
    peer = request.client.host if request.client else ""
    return "*" in trusted or peer in trusted


def forwarded_scheme(request: Request) -> str | None:
    """The scheme a trusted proxy says the visitor used — ``http`` or
    ``https`` — else None. A header from anyone else is just a header: a
    LAN client claiming https must not get a Secure cookie it can't send
    back, or an https canonical link for a plain-http radio."""
    if not proxy_trusted(request):
        return None
    value = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return value if value in ("http", "https") else None


def absolute_url(request: Request, path: str | None = None) -> str:
    """A full ``https://host/path`` URL for this request (or for ``path`` on the
    same host) — what link previews, canonical links and the sitemap need.

    With ``[web] public_url`` configured that is the authority, whatever Host
    header the request carried: a canonical link is a statement about where the
    page lives, and a spoofed Host must not be able to make one. Without it,
    the request's own host is used. Hosted deployments sit behind a
    TLS-terminating proxy, so the scheme the app sees is plain http;
    ``x-forwarded-proto`` is the one that matches the URL a visitor would
    paste, and only ``http``/``https`` are believed. Query strings are dropped:
    a preview for ``/archive?m=2026-08`` is a preview of the archive."""
    url = request.url.replace(query="", fragment="") if path is None else (
        request.base_url.replace(path=path, query="", fragment="")
    )
    public = getattr(request.app.state, "public_url", "")
    if public:
        base = URL(public)
        return str(url.replace(scheme=base.scheme, netloc=base.netloc))
    forwarded = forwarded_scheme(request)
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
    open_sockets: int = 0          # WebSockets live right now, across every session
    _cache: dict[str, tuple[float, Any]] = field(default_factory=dict)

    # Whole-archive aggregates, kept until the archive changes. Every
    # player-state push makes each open page re-fetch the now-playing and
    # day-nav partials, and both rebuild day_context — so archive_days(),
    # which aggregates the whole themes×tracks join, ran twice per event per
    # tab; the stats and theme pages (both in the sitemap) recomputed five
    # and one full scans per hit. None of it changes until a song or a theme
    # lands, and those arrive as bus events, so the cache is dropped on the
    # event (``invalidate``, wired up in create_app) and otherwise kept: no
    # staleness, and nothing recomputed between songs however hard a crawler
    # or a feed reader polls. The TTL is only the safety net for a change
    # that sends no event — the operator CLI editing the archive from another
    # process, a play landing in the stats — and bounds how long such a one
    # can go unseen.
    CACHE_TTL_S = 60.0

    def invalidate(self, _topic: str | None = None, _payload: Any = None) -> None:
        """Forget every cached aggregate; shaped as a bus listener."""
        self._cache.clear()

    async def _cached(self, key: str, load: Callable[[], Awaitable[Any]]) -> Any:
        now = monotonic()
        hit = self._cache.get(key)
        if hit is not None and now - hit[0] < self.CACHE_TTL_S:
            return hit[1]
        value = await load()
        self._cache[key] = (now, value)
        return value

    async def archive_days(self) -> list[dict[str, Any]]:
        """``Database.archive_days`` behind the TTL."""
        return await self._cached("days", self.db.archive_days)

    async def all_themes(self) -> list[dict[str, Any]]:
        """``Database.all_themes`` behind the TTL."""
        return await self._cached("themes", self.db.all_themes)

    async def recent_days_tracks(self) -> list[dict[str, Any]]:
        """The feed's rows behind the TTL. /feed.xml is public and polled, and
        a reader or crawler hammering it should cost one query per few seconds,
        not one per request."""
        return await self._cached(
            "feed", lambda: self.db.recent_days_tracks(FEED_DAYS)
        )

    async def sender_first_days(self) -> dict[str, str]:
        """``Database.sender_first_days`` behind the TTL — every recap view
        and the weekly feed ask it, and it aggregates the whole history."""
        return await self._cached("firsts", self.db.sender_first_days)

    async def finished_weeks(self) -> list[dict[str, Any]]:
        """The weekly feed's recaps behind the TTL, newest first: one query
        for the whole window, split into weeks here."""
        async def load() -> list[dict[str, Any]]:
            starts = finished_weeks(self.today(), week_starts(await self.archive_days()))
            if not starts:
                return []
            rows = await self.db.tracks_between(min(starts), week_end(max(starts)))
            by_week: dict[str, list[dict[str, Any]]] = {s: [] for s in starts}
            for row in rows:
                by_week.setdefault(week_start(row["date"]), []).append(row)
            firsts = await self.sender_first_days()
            return [summarize_week(s, by_week[s], firsts) for s in starts if by_week[s]]
        return await self._cached("weekly", load)

    async def stats(self) -> dict[str, Any]:
        """Everything the Stats page shows, behind the TTL."""
        async def load() -> dict[str, Any]:
            return {
                "totals": await self.db.overall_stats(),
                "plays": await self.db.play_totals(),
                "top_songs": await self.db.top_songs(),
                "top_sharers": await self.db.top_sharers(),
                "busiest_themes": await self.db.busiest_themes(),
                "top_artists": await self.db.top_artists(),
            }
        return await self._cached("stats", load)

    async def get_player(self, request: Request) -> PlayerService:
        """The player this request acts on.

        Appliance: the one communal player. Embed hosting: the visitor's own
        session player — opened by a POST (they pressed something) or found
        by a GET (their page's WebSocket, or a returning cookie's snapshot,
        opened it). A GET from a visitor with no session gets a throwaway
        preview instead: the page still renders cued, but a crawler or a
        cookie-spraying bot leaves no session behind. So does a POST that
        presented no cookie of ours (the middleware minted one on this very
        request): a browser always carries the cookie it got with the page,
        so that press is a script's, and it used to open — and persist — a
        session per request."""
        if self.sessions is None:
            return self.player
        sid = getattr(request.state, "sid", None)
        if sid is None or getattr(request.state, "fresh_sid", True):
            return await self.sessions.preview()         # nothing of ours presented
        if request.method in ("GET", "HEAD"):
            session = await self.sessions.lookup(sid)
            return session.player if session else await self.sessions.preview()
        return (await self.sessions.get(sid)).player

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

    async def render_live(self, request: Request):
        """Everything on Now Playing that a state push changes — the bar, the
        queue, the day nav — in one response, as out-of-band swaps into the
        page's three containers. One request per event instead of three, and
        the day context (two queries) built once instead of twice."""
        p = await self.get_player(request)
        return self.templates.TemplateResponse(
            request, "partials/live.html",
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
