"""HTML pages and htmx partials."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from .. import __version__
from .context import ctx_of

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
async def archive(request: Request):
    ctx = ctx_of(request)
    days = await ctx.db.archive_days()
    return ctx.templates.TemplateResponse(request, "archive.html", {"days": days})


@router.get("/archive/{date}", response_class=HTMLResponse)
async def archive_day(request: Request, date: str):
    ctx = ctx_of(request)
    themes = await ctx.db.themes_for_day(date)
    for theme in themes:
        theme["tracks"] = await ctx.db.tracks_for_theme(theme["id"])
    return ctx.templates.TemplateResponse(
        request, "archive_day.html", {"date": date, "themes": themes}
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
