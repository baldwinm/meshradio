"""The overview: counts, what needs attention, recent changes, this
connection, and the Updates panel."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ... import __version__
from ...deployinfo import LAG_S, REPO_URL, deployed_commit, describe, read_status, running_commit
from ..admin_auth import ADMIN_PATH
from ..context import ctx_of
from .backups import snapshot_rows
from .common import ACTION_LABELS, client_ip, https, render, require_admin, settings_of
from .feeds import feed_rows
from .filters import fmt_ago

router = APIRouter(prefix=ADMIN_PATH)

# A backup older than this many intervals reads as overdue on the overview.
BACKUP_OVERDUE_FACTOR = 1.25


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def overview(request: Request):
    token = await require_admin(request)
    ctx = ctx_of(request)
    settings = settings_of(request)
    config = settings.config
    now = time.time()

    last_ingest = ctx.health.get("last_ingest")
    newest = await ctx.db.newest_channel_tracks(5)
    days = await ctx.archive_days()

    backup = None
    if config is not None and config.backup.enabled:
        snaps = snapshot_rows(config.backup_dir)
        newest_snap = snaps[0]["taken"] if snaps and snaps[0]["taken"] else None
        age = now - newest_snap if newest_snap else None
        backup = {
            "age": age,
            "interval": config.backup.interval_s,
            "overdue": age is None or age > config.backup.interval_s * BACKUP_OVERDUE_FACTOR,
        }

    feeds = feed_rows(request, now)
    attention = []
    untitled = await ctx.db.untitled_days()
    if untitled:
        attention.append({
            "text": f"{len(untitled)} day{'' if len(untitled) == 1 else 's'} still "
                    f"untitled: " + ", ".join(d["date"] for d in untitled[:4])
                    + ("…" if len(untitled) > 4 else ""),
            "href": f"{ADMIN_PATH}/days?untitled=1",
            "action": "Name them",
        })
    for feed in feeds:
        if feed["state"] in ("bad", "warn"):
            attention.append({
                "text": f"{feed['label']}: {feed['status']}",
                "href": f"{ADMIN_PATH}/feeds",
                "action": "Open feeds",
            })
    if backup is not None and backup["overdue"]:
        attention.append({
            "text": "The last backup is older than its schedule",
            "href": f"{ADMIN_PATH}/backups",
            "action": "Open backups",
        })
    updates = update_info(ctx, now)
    if updates["autoupdate"] and updates["autoupdate"]["state"] == "bad":
        attention.append({
            "text": f"Pi auto-update: {updates['autoupdate']['text']}",
            "href": "#updates",
            "action": "See why",
        })
    if updates["pi"] and updates["pi"]["lagging"]:
        attention.append({
            "text": "The Pi has been on a different commit from this site for "
                    f"{fmt_ago(updates['pi']['mismatch_age'])}",
            "href": "#updates",
            "action": "See why",
        })

    return render(
        request, "overview.html", token, section="overview",
        last_ingest_age=(now - last_ingest) if last_ingest else None,
        track_count=await ctx.db.channel_track_count(),
        day_count=len(days),
        visitors=ctx.sessions.count() if ctx.sessions is not None else None,
        backup=backup,
        attention=attention,
        newest=newest,
        changes=await ctx.db.admin_log_entries("changes", limit=5),
        labels=ACTION_LABELS,
        all_ok=not any(f["state"] == "bad" for f in feeds),
        connection=connection_info(request),
        updates=updates,
    )


def update_info(ctx: Any, now: float) -> dict[str, Any]:
    """The overview's Updates panel: what this server runs, and — on the Pi
    from its own report, on the hosted site from what the Pi relays — what
    the Pi runs and whether its auto-updater is keeping it current."""
    receiver = bool(ctx.ingest_token)
    status = read_status()
    pi = None
    if receiver:
        node = ctx.health.get("node")
        if node is not None:
            since = node.get("mismatch_since")
            mismatch_age = now - since if since is not None else None
            pi = {"version": node["version"], "commit": deployed_commit(node, now),
                  "age": now - node["at"], "mismatch_age": mismatch_age,
                  # Only a Pi that updates itself is expected to keep up.
                  "self_updating": node["autoupdate"] is not None,
                  "lagging": node["autoupdate"] is not None
                  and mismatch_age is not None and mismatch_age > LAG_S}
            status = status or node["autoupdate"]
    autoupdate = None
    # A receiver with no word from the Pi yet has nothing to say about it;
    # everywhere else a missing report is worth showing.
    if status is not None or not receiver or pi is not None:
        autoupdate = describe(status, now)
        if status is not None:
            autoupdate.update(commit=status["commit"], target=status["target"],
                              updated_age=now - status["updated_at"]
                              if status["updated_at"] else None)
    return {
        "version": __version__,
        "commit": running_commit(),
        "receiver": receiver,
        "pi": pi,
        "autoupdate": autoupdate,
        "repo": REPO_URL,
    }


# Headers a proxy may use to say who the visitor is, shown as they arrived
# so [web] proxy_hops can be checked against a real request.
_CLIENT_HEADERS = ("x-forwarded-for", "x-forwarded-proto", "true-client-ip",
                   "cf-connecting-ip", "x-real-ip")


def connection_info(request: Request) -> dict[str, Any]:
    """How this request reached the app: the address the rate limits and the
    sign-in throttle use, whether it came over https, and what the proxy
    said. Your own address is the check — it should be the one shown."""
    peer = getattr(request.state, "peer", None)
    return {
        "address": client_ip(request),
        "peer": peer if peer is not None else client_ip(request),
        "https": https(request),
        "headers": [(name, request.headers[name]) for name in _CLIENT_HEADERS
                    if name in request.headers],
    }
