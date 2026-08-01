"""HTML pages and htmx partials."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .. import __version__
from .context import (
    WEEKDAY_HEADERS,
    archive_months,
    calendar_month,
    ctx_of,
    month_step,
    yt_export_url,
)

router = APIRouter()

GITHUB_URL = "https://github.com/baldwinm/meshradio"


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    ctx = ctx_of(request)
    p = await ctx.get_player(request)
    return ctx.templates.TemplateResponse(
        request, "index.html", {"state": p.state(), **await ctx.day_context(p)}
    )


@router.get("/archive", response_class=HTMLResponse)
async def archive(request: Request, m: str = ""):
    """One month at a time. ``m`` (``YYYY-MM``) picks it; anything unrecognised
    falls back to the newest month with songs in it."""
    ctx = ctx_of(request)
    days = await ctx.db.archive_days()
    months = archive_months(days)
    i = months.index(m) if m in months else len(months) - 1
    return ctx.templates.TemplateResponse(
        request,
        "archive.html",
        {
            "month": calendar_month(days, months[i]) if months else None,
            "prev_month": month_step(months[i - 1] if i > 0 else None),
            "next_month": month_step(months[i + 1] if 0 <= i < len(months) - 1 else None),
            "day_count": len(days),
            "weekday_headers": WEEKDAY_HEADERS,
        },
    )


@router.get("/archive/{date}", response_class=HTMLResponse)
async def archive_day(request: Request, date: str):
    ctx = ctx_of(request)
    themes = await ctx.db.themes_for_day(date)
    for theme in themes:
        theme["tracks"] = await ctx.db.tracks_for_theme(theme["id"])
    all_tracks = [track for theme in themes for track in theme["tracks"]]
    return ctx.templates.TemplateResponse(
        request,
        "archive_day.html",
        {"date": date, "themes": themes, "yt_export_url": yt_export_url(all_tracks)},
    )


@router.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = ""):
    ctx = ctx_of(request)
    q = q.strip()
    results = await ctx.db.search_tracks(q) if q else []
    return ctx.templates.TemplateResponse(
        request, "search.html", {"q": q, "results": results}
    )


@router.get("/stats", response_class=HTMLResponse)
async def stats(request: Request):
    ctx = ctx_of(request)
    return ctx.templates.TemplateResponse(
        request,
        "stats.html",
        {
            "totals": await ctx.db.overall_stats(),
            "top_songs": await ctx.db.top_songs(),
            "top_sharers": await ctx.db.top_sharers(),
            "busiest_themes": await ctx.db.busiest_themes(),
        },
    )


@router.get("/about", response_class=HTMLResponse)
async def about(request: Request):
    return ctx_of(request).templates.TemplateResponse(
        request, "about.html", {"version": __version__, "github_url": GITHUB_URL}
    )


@router.get("/partials/now-playing", response_class=HTMLResponse)
async def partial_now_playing(request: Request):
    return await ctx_of(request).render_now_playing(request)


@router.get("/partials/day-nav", response_class=HTMLResponse)
async def partial_day_nav(request: Request):
    return await ctx_of(request).render_day_nav(request)


@router.get("/partials/queue", response_class=HTMLResponse)
async def partial_queue(request: Request):
    return await ctx_of(request).render_queue(request)
