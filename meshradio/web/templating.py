"""Page rendering: the Jinja2 environment with its filters and globals, and
the static files with their cache headers."""

from __future__ import annotations

import datetime
import hashlib
import math
from pathlib import Path

from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..concerts import show_date
from .context import absolute_url
from .routes_pages import GITHUB_URL

_HERE = Path(__file__).parent


def mmss(value) -> str:
    """Seconds → 'm:ss' (or 'h:mm:ss'); empty string for unknown durations.

    Unknown includes anything that isn't a finite non-negative number: the
    archive refuses those now, but a filter that can raise mid-render turns
    one bad row into a 500 on every page that shows it."""
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        return ""
    if value < 0:
        return ""
    if value >= 3600:
        return f"{value // 3600}:{value % 3600 // 60:02d}:{value % 60:02d}"
    return f"{value // 60}:{value % 60:02d}"


def total_length(tracks) -> tuple[str, bool]:
    """A list of tracks' summed length as 'h:mm:ss', and whether any track's
    length is still unknown (so the total is a lower bound)."""
    total, unknown = 0, False
    for track in tracks or ():
        duration = (track or {}).get("duration")
        try:
            seconds = math.nan if duration is None else float(duration)
        except (TypeError, ValueError, OverflowError):
            seconds = math.nan
        if math.isfinite(seconds) and seconds > 0:
            total += int(seconds)
        else:
            unknown = True
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}", unknown


def day_label(value, long: bool = False) -> str:
    """An ISO day as people say it: 'Thu, Oct 16', or with ``long``
    'Thursday, October 16, 2026'. Anything that isn't a date comes back as
    it was, so a bad row shows its raw text rather than breaking the page."""
    try:
        day = datetime.date.fromisoformat(str(value))
    except ValueError:
        return str(value or "")
    if long:
        return f"{day:%A}, {day:%B} {day.day}, {day.year}"
    return f"{day:%a}, {day:%b} {day.day}"


def asset_version(static: Path) -> str:
    """A hash of everything under static/ — cache-busts CSS/JS across updates.

    Content rather than the newest mtime: a fresh clone (which is what every
    hosted build is) stamps every file with the build's time, so a deploy
    used to invalidate every asset whether or not one had changed."""
    digest = hashlib.sha256()
    for path in sorted(p for p in static.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(static)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


class VersionedStatic(StaticFiles):
    """Static files that tell the browser how long to keep them.

    Templates request assets as ``/static/x.js?v=<asset_v>`` (see
    ``asset_version``), so a URL carrying ``v`` names one immutable build and
    can be cached forever — the next deploy changes the query string. Without
    the versioned URL we can't make that promise (someone linked the bare
    path), so those get a short window and keep revalidating.

    This is what stops every page navigation from re-checking eleven assets:
    on the hosted embed that's eleven round trips a visitor doesn't need."""

    IMMUTABLE = "public, max-age=31536000, immutable"
    SHORT = "public, max-age=300"

    # *args/**kwargs so a future Starlette can add parameters without breaking
    # the override; the three we name are the long-standing ones.
    def file_response(self, full_path, stat_result, scope, *args, **kwargs):
        response = super().file_response(full_path, stat_result, scope, *args, **kwargs)
        versioned = b"v=" in scope.get("query_string", b"")
        response.headers["cache-control"] = self.IMMUTABLE if versioned else self.SHORT
        return response


def make_templates(embed_mode: bool, concerts_on: bool) -> Jinja2Templates:
    """The page templates, with the filters and globals every page uses.
    ``embed_mode`` is public embed hosting (per-visitor players)."""
    templates = Jinja2Templates(directory=_HERE / "templates")
    templates.env.filters["mmss"] = mmss
    templates.env.filters["total_length"] = total_length
    templates.env.filters["show_date"] = show_date
    templates.env.filters["day_label"] = day_label
    # base.html builds every page's link-preview tags from the live request.
    templates.env.globals["absolute_url"] = absolute_url
    templates.env.globals["asset_v"] = asset_version(_HERE / "static")
    # The site footer (base.html) shows both on every page.
    templates.env.globals["version"] = __version__
    templates.env.globals["github_url"] = GITHUB_URL
    # Public embed hosting only: the Buy-Me-a-Coffee button pulls an external
    # CDN script, so keep it off the offline LAN/appliance skin.
    templates.env.globals["embed_mode"] = embed_mode
    # Upcoming shows on artist pages and the now-playing card (concerts.py).
    templates.env.globals["concerts_on"] = concerts_on
    return templates
