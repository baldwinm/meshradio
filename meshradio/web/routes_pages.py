"""HTML pages and htmx partials."""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

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
from .feed import build_feed
from .recap import (
    build_weekly_feed,
    summarize_week,
    week_end,
    week_label,
    week_start,
    week_starts,
    week_summary_line,
)

router = APIRouter()

GITHUB_URL = "https://github.com/baldwinm/meshradio"

# The archive's day pages are the one place a URL segment is free text. Anything
# that isn't a date is a 404, not a page titled with whatever was typed.
ISO_DATE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")

# How many search hits a page shows. One more is fetched to detect the cut-off.
SEARCH_LIMIT = 100
# The longest query searched; nobody types more, and the page echoes it.
SEARCH_MAX_CHARS = 200
# A member filter is a mesh name, which the archive stores at 64 characters.
SEARCH_NAME_MAX_CHARS = 64
# A year filter is four digits or it is nothing. Anything else is dropped
# rather than 404'd: a stale link should still return the search it names.
SEARCH_YEAR = re.compile(r"\A\d{4}\Z")


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
    themes = await ctx.all_themes()
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
            "week": week_start(date),
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
async def search(request: Request, q: str = "", member: str = "", year: str = ""):
    """Results are capped; ask for one more than we show so the page can say
    the list is cut off instead of reporting the cap as the total.

    A filter is a search in its own right — "everything Ana shared in 2026"
    names no song — so the query runs whenever any of the three is set, and
    the filters stay in the URL so a narrowed search is a link."""
    ctx = ctx_of(request)
    q = q.strip()[:SEARCH_MAX_CHARS].strip()
    member = member.strip()[:SEARCH_NAME_MAX_CHARS]
    year = year if SEARCH_YEAR.match(year) else ""
    rows = (
        await ctx.db.search_tracks(
            q, limit=SEARCH_LIMIT + 1, sender=member or None, year=year or None
        )
        if (q or member or year) else []
    )
    return ctx.templates.TemplateResponse(
        request,
        "search.html",
        {
            "q": q,
            "member": member,
            "year": year,
            "filters": await ctx.search_filters(),
            "results": rows[:SEARCH_LIMIT],
            "more": len(rows) > SEARCH_LIMIT,
        },
    )


@router.get("/stats", response_class=HTMLResponse)
async def stats(request: Request):
    ctx = ctx_of(request)
    return ctx.templates.TemplateResponse(request, "stats.html", await ctx.stats())


@router.get("/member/{name:path}", response_class=HTMLResponse)
async def member(request: Request, name: str):
    """One member's record on the channel: what they've shared, the days they
    named, who they keep coming back to.

    Names are as typed on the mesh, so the lookup is case-insensitive and the
    page titles itself with the channel's own spelling. ``:path`` because a
    mesh name may contain a slash: the link encodes it, but uvicorn decodes
    the path before routing, and a plain segment then matched nothing."""
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


@router.get("/artist/{name:path}", response_class=HTMLResponse)
async def artist(request: Request, name: str):
    """Everything the channel has posted by one artist, and who posts it.

    The artist is whatever YouTube's oEmbed named as the video's channel, with
    YouTube Music's "- Topic" suffix folded away so a Music share and a plain
    video link land on the same page. ``:path`` for the same reason as the
    member route: a name may carry a slash."""
    ctx = ctx_of(request)
    canonical = await ctx.db.artist_lookup(name)
    if canonical is None:
        raise HTTPException(404, "no songs by that artist")
    profile = await ctx.db.artist_profile(canonical)
    songs = await ctx.db.artist_songs(canonical)
    shares = profile.get("shares") or 0
    sharers = profile.get("sharers") or 0
    return ctx.templates.TemplateResponse(
        request,
        "artist.html",
        {
            "name": canonical,
            "profile": profile,
            "songs": songs,
            "sharers": await ctx.db.artist_sharers(canonical),
            "themes": await ctx.db.artist_themes(canonical),
            "yt_export_url": yt_export_url(songs),
            "meta_description": (
                f"{canonical} has come up {shares} {'time' if shares == 1 else 'times'}, "
                f"shared by {sharers} {'member' if sharers == 1 else 'members'} "
                "of the Austin mesh #music channel."
            ),
            "meta_image": yt_thumbnail(songs[0]["video_id"] if songs else None),
        },
    )


async def _week_page(request: Request, start: str | None):
    ctx = ctx_of(request)
    starts = week_starts(await ctx.archive_days())
    if start is None:
        if not starts:
            return ctx.templates.TemplateResponse(request, "week.html", {"week": None})
        start = starts[-1]
    rows = await ctx.db.tracks_between(start, week_end(start))
    if not rows:
        raise HTTPException(404, "no songs that week")
    song_firsts = await ctx.db.song_first_days(sorted({r["video_id"] for r in rows}))
    week = summarize_week(start, rows, await ctx.sender_first_days(), song_firsts)
    return ctx.templates.TemplateResponse(
        request,
        "week.html",
        {
            "week": week,
            "prev_week": max((s for s in starts if s < start), default=None),
            "next_week": min((s for s in starts if s > start), default=None),
            "week_label": week_label,
            "meta_description": week_summary_line(week),
            "meta_image": yt_thumbnail(rows[0]["video_id"]),
            # /week moves every Sunday; the dated page is the one to keep.
            "meta_url": absolute_url(request, f"/week/{start}"),
        },
    )


@router.get("/week", response_class=HTMLResponse)
async def week_latest(request: Request):
    """The newest week with songs in it."""
    return await _week_page(request, None)


@router.get("/week/{date}", response_class=HTMLResponse)
async def week(request: Request, date: str):
    """One Sunday-to-Saturday week. Any date in it finds it: a day page links
    its own date's week without working out the Sunday, and the redirect
    keeps one URL per week."""
    if not ISO_DATE.fullmatch(date):
        raise HTTPException(404, "no such week")
    try:
        start = week_start(date)
    except ValueError:
        raise HTTPException(404, "no such week") from None
    if start != date:
        return RedirectResponse(f"/week/{start}", status_code=301)
    return await _week_page(request, start)


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
    """Every archived day is a page worth finding — that's the whole archive —
    and so is every week's recap."""
    ctx = ctx_of(request)
    days = await ctx.archive_days()
    paths = ["/", "/archive", "/archive/themes", "/week", "/stats", "/about"]
    paths += [f"/archive/{d['date']}" for d in days]
    paths += [f"/week/{s}" for s in week_starts(days)]
    urls = "".join(f"<url><loc>{absolute_url(request, p)}</loc></url>" for p in paths)
    return Response(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{urls}</urlset>",
        media_type="application/xml",
    )


@router.get("/feed.xml")
async def feed(request: Request):
    """The channel's last month as an Atom feed, one entry per day — so the
    day's theme reaches a subscriber without their opening the site."""
    ctx = ctx_of(request)
    return Response(
        build_feed(
            await ctx.recent_days_tracks(),
            feed_url=absolute_url(request, "/feed.xml"),
            site_url=absolute_url(request, "/"),
            day_url=lambda date: absolute_url(request, f"/archive/{date}"),
        ),
        media_type="application/atom+xml",
    )


@router.get("/weekly.xml")
async def weekly_feed(request: Request):
    """The recap as an Atom feed: one entry per finished week, so a
    subscriber gets a digest instead of a post a day."""
    ctx = ctx_of(request)
    return Response(
        build_weekly_feed(
            await ctx.finished_weeks(),
            feed_url=absolute_url(request, "/weekly.xml"),
            site_url=absolute_url(request, "/"),
            week_url=lambda start: absolute_url(request, f"/week/{start}"),
            day_url=lambda date: absolute_url(request, f"/archive/{date}"),
        ),
        media_type="application/atom+xml",
    )


@router.get("/partials/live", response_class=HTMLResponse)
async def partial_live(request: Request):
    """What the index page fetches on every state push (see index.html)."""
    return await ctx_of(request).render_live(request)


@router.get("/partials/now-playing", response_class=HTMLResponse)
async def partial_now_playing(request: Request):
    return await ctx_of(request).render_now_playing(request)


@router.get("/partials/day-nav", response_class=HTMLResponse)
async def partial_day_nav(request: Request):
    return await ctx_of(request).render_day_nav(request)


@router.get("/partials/queue", response_class=HTMLResponse)
async def partial_queue(request: Request):
    return await ctx_of(request).render_queue(request)
