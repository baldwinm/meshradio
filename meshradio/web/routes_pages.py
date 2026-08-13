"""HTML pages and htmx partials."""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from .. import __version__
from .context import (
    WEEKDAY_HEADERS,
    absolute_url,
    archive_months,
    archive_years,
    calendar_month,
    ctx_of,
    month_step,
    theme_history,
    theme_key,
    year_step,
    yt_export_url,
    yt_thumbnail,
)

router = APIRouter()

GITHUB_URL = "https://github.com/baldwinm/meshradio"

# The archive's day pages are the one place a URL segment is free text. Anything
# that isn't a date is a 404, not a page titled with whatever was typed.
ISO_DATE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")

# How many search hits a page shows. One more is fetched to detect the cut-off.
SEARCH_LIMIT = 100


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    ctx = ctx_of(request)
    p = await ctx.get_player(request)
    day = await ctx.day_context(p)
    state = p.state()
    titles = " · ".join(day["theme_titles"])
    return ctx.templates.TemplateResponse(
        request,
        "index.html",
        {
            "state": state,
            **day,
            "meta_description": (
                f"Today's theme on the Austin mesh #music channel: “{titles}”."
                if titles else
                "Songs shared each day on the Austin MeshCore #music channel, "
                "played against that day's theme."
            ),
            "meta_image": yt_thumbnail((state.get("current") or {}).get("video_id")),
        },
    )


@router.get("/archive", response_class=HTMLResponse)
async def archive(request: Request, m: str = ""):
    """One month at a time. ``m`` (``YYYY-MM``) picks it; anything unrecognised
    falls back to the newest month with songs in it."""
    ctx = ctx_of(request)
    days = await ctx.archive_days()
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


@router.get("/archive/themes", response_class=HTMLResponse)
async def archive_themes(request: Request, y: str = ""):
    """Every theme the channel has run, a year at a time — the calendar's month
    paging applied to the list, so the page stays a fixed size however long the
    channel runs. ``y`` picks the year; anything unrecognised falls back to the
    newest. Declared *before* ``/archive/{date}`` so the path isn't taken for a
    date.

    The repeat counts come from the whole history, not the year on screen: "run
    3×" has to mean 3 times ever."""
    ctx = ctx_of(request)
    themes = await ctx.db.all_themes()
    years = archive_years(themes)
    i = years.index(y) if y in years else len(years) - 1
    year = years[i] if years else None
    shown = [t for t in themes if t["date"][:4] == year] if year else []
    return ctx.templates.TemplateResponse(
        request,
        "archive_themes.html",
        {
            "year": year,
            "months": theme_history(shown, all_themes=themes),
            # Older year to the left, newer to the right — same as the calendar.
            "prev_year": year_step(years[i - 1] if i > 0 else None),
            "next_year": year_step(years[i + 1] if 0 <= i < len(years) - 1 else None),
            "shown_count": len(shown),
            "theme_count": len(themes),
            "title_count": len({theme_key(t["title"]) for t in themes}),
        },
    )


@router.get("/archive/{date}", response_class=HTMLResponse)
async def archive_day(request: Request, date: str):
    """One day's playlist, with steps to the days either side so the archive can
    be read straight through instead of via the calendar every time."""
    ctx = ctx_of(request)
    if not ISO_DATE.fullmatch(date):
        raise HTTPException(404, "no such archived day")
    themes = await ctx.db.themes_for_day(date)
    if not themes:
        raise HTTPException(404, "no such archived day")
    for theme in themes:
        theme["tracks"] = await ctx.db.tracks_for_theme(theme["id"])
    all_tracks = [track for theme in themes for track in theme["tracks"]]
    days = sorted(d["date"] for d in await ctx.archive_days())
    titles = " · ".join(t["title"] for t in themes)
    n = len(all_tracks)
    return ctx.templates.TemplateResponse(
        request,
        "archive_day.html",
        {
            "date": date,
            "themes": themes,
            "yt_export_url": yt_export_url(all_tracks),
            "prev_day": max((d for d in days if d < date), default=None),
            "next_day": min((d for d in days if d > date), default=None),
            "month": date[:7],
            # A day is the unit people paste into a chat, so give the unfurl
            # something to say: the theme, the count, and the day's first song
            # as the picture.
            "meta_description": (
                f"{n} {'song' if n == 1 else 'songs'} shared on {date} for the "
                f"theme “{titles}” on the Austin mesh #music channel."
            ),
            "meta_image": yt_thumbnail(all_tracks[0]["video_id"] if all_tracks else None),
        },
    )


@router.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = ""):
    """Results are capped; ask for one more than we show so the page can say
    the list is cut off instead of reporting the cap as the total."""
    ctx = ctx_of(request)
    q = q.strip()
    rows = await ctx.db.search_tracks(q, limit=SEARCH_LIMIT + 1) if q else []
    return ctx.templates.TemplateResponse(
        request,
        "search.html",
        {"q": q, "results": rows[:SEARCH_LIMIT], "more": len(rows) > SEARCH_LIMIT},
    )


@router.get("/stats", response_class=HTMLResponse)
async def stats(request: Request):
    ctx = ctx_of(request)
    return ctx.templates.TemplateResponse(
        request,
        "stats.html",
        {
            "totals": await ctx.db.overall_stats(),
            "plays": await ctx.db.play_totals(),
            "top_songs": await ctx.db.top_songs(),
            "top_sharers": await ctx.db.top_sharers(),
            "busiest_themes": await ctx.db.busiest_themes(),
        },
    )


@router.get("/member/{name}", response_class=HTMLResponse)
async def member(request: Request, name: str):
    """One member's record on the channel: what they've shared, the days they
    named, who they keep coming back to.

    Names are as typed on the mesh, so the lookup is case-insensitive and the
    page titles itself with the channel's own spelling."""
    ctx = ctx_of(request)
    canonical = await ctx.db.member_name(name)
    if canonical is None:
        raise HTTPException(404, "nobody by that name has posted")
    profile = await ctx.db.member_profile(canonical)
    tracks = await ctx.db.member_tracks(canonical)
    return ctx.templates.TemplateResponse(
        request,
        "member.html",
        {
            "name": canonical,
            "profile": profile,
            "tracks": tracks,
            "themes": await ctx.db.member_themes(canonical),
            "artists": await ctx.db.member_artists(canonical),
            "yt_export_url": yt_export_url(tracks),
            "meta_description": (
                f"{canonical} has shared {profile.get('shares') or 0} songs "
                f"on {profile.get('days') or 0} days of the Austin mesh "
                "#music channel."
            ),
        },
    )


@router.get("/about", response_class=HTMLResponse)
async def about(request: Request):
    return ctx_of(request).templates.TemplateResponse(
        request, "about.html", {"version": __version__, "github_url": GITHUB_URL}
    )


@router.get("/robots.txt", response_class=PlainTextResponse)
async def robots(request: Request):
    """Crawlers: pages yes, machinery no. The API, partials and audio are not
    documents, and /search with a query is an infinite space."""
    sitemap_url = absolute_url(request, "/sitemap.xml")
    return PlainTextResponse(
        "User-agent: *\n"
        "Disallow: /api/\n"
        "Disallow: /partials/\n"
        "Disallow: /audio/\n"
        "Disallow: /search\n"
        f"Sitemap: {sitemap_url}\n"
    )


@router.get("/sitemap.xml")
async def sitemap(request: Request):
    """Every archived day is a page worth finding — that's the whole archive."""
    ctx = ctx_of(request)
    paths = ["/", "/archive", "/archive/themes", "/stats", "/about"]
    paths += [f"/archive/{d['date']}" for d in await ctx.archive_days()]
    urls = "".join(f"<url><loc>{absolute_url(request, p)}</loc></url>" for p in paths)
    return Response(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{urls}</urlset>",
        media_type="application/xml",
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
