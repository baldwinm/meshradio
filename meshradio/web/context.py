"""Shared request context for the web routers.

One WebContext rides on ``app.state.ctx``; route handlers pull it from the
request instead of closing over create_app locals. ``get_player`` is the
session/communal fork: per-visitor players in embed mode, the appliance's
single player otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

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
        days = sorted(d["date"] for d in await self.db.archive_days() if d["tracks"])
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
