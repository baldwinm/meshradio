"""The admin page: ``/admin``.

What the operator used to do over SSH with command-line flags, from a
browser: fix a day's theme, take a song off a day, correct a song's details,
merge artist spellings, check the feeds, and take or download backups.

Only mounted when an admin password hash is configured (``create_app`` gets
``admin=AdminSettings(...)``); without one every ``/admin`` URL is a 404.
Signing in is ``admin_auth``'s job. Every change goes in ``admin_log`` with
its before and after, and the ones that can be reversed carry what reversing
them needs, so the activity log can offer Undo.

One module per screen; ``common`` holds what they share.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import activity, artists, backups, config, days, device, feeds, overview, signin
from .common import admin_headers
from .filters import FILTERS

router = APIRouter()
for _screen in (signin, overview, days, artists, feeds, backups, activity, config, device):
    router.include_router(_screen.router)

__all__ = ["FILTERS", "admin_headers", "router"]
