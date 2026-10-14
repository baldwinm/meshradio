"""The feeds: each ingest path's last report, a probe of a poller's feed,
and asking the Pi to send its whole channel again."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ...ingest.corescope import format_probe, probe
from ...ingest.relay import CURSOR_KEY
from ...net import http_client
from ..admin_auth import ADMIN_PATH
from ..context import client_host, ctx_of
from .common import checked_form, go, render, require_admin, settings_of

router = APIRouter(prefix=ADMIN_PATH)

# A feed whose last good answer is older than this many poll intervals is
# flagged even if its last attempt wasn't an outright error.
FEED_STALE_FACTOR = 3

# The Pi pushes every two minutes even with nothing new ([relay] interval_s),
# so a receiver that hasn't heard from it in three pushes reads as quiet, and
# past the uptime check's limit (MAX_RELAY_AGE_S in uptime.yml) as stopped.
RELAY_QUIET_S = 3 * 120
RELAY_STOPPED_S = 1800


def feed_rows(request: Request, now: float) -> list[dict[str, Any]]:
    """One row per ingest path this server runs, with a plain-words status
    and a state for its chip: ok, warn, bad, or off."""
    ctx = ctx_of(request)
    settings = settings_of(request)
    config = settings.config
    seen = ctx.health.get("feeds", {})
    rows: list[dict[str, Any]] = []

    def poller(name: str, label: str, cfg: Any) -> None:
        if cfg is None or not cfg.enabled or not cfg.base_url:
            rows.append({"name": name, "label": label, "detail": "", "state": "off",
                         "status": "Off", "at": None, "last_ok": None, "probe": False})
            return
        info = seen.get(name, {})
        last_ok = info.get("last_ok")
        stale_after = cfg.poll_interval_s * FEED_STALE_FACTOR
        if info.get("status") == "error":
            state, status = "bad", "No answer"
        elif info.get("status") == "ok" and last_ok and now - last_ok > stale_after:
            state, status = "warn", "Quiet"
        elif info.get("status") == "ok":
            state, status = "ok", "OK"
        else:
            state, status = "dim", "Waiting for its first poll"
        rows.append({"name": name, "label": label, "detail": cfg.base_url, "state": state,
                     "status": status, "at": info.get("at"), "last_ok": last_ok,
                     "probe": True})

    poller("corescope", "CoreScope", config.corescope if config else None)
    poller("comchan", "comchan (backup)", config.comchan if config else None)

    if ctx.ingest_token:
        info = seen.get("relay", {})
        at = info.get("at")
        if not at:
            state, status = "dim", "No push received since start"
        elif now - at > RELAY_STOPPED_S:
            state, status = "bad", f"No push for {int((now - at) // 60)} min"
        elif now - at > RELAY_QUIET_S:
            state, status = "warn", "Quiet"
        else:
            state, status = "ok", "OK"
        rows.append({"name": "relay", "label": "Relay from the Pi", "detail": "received here",
                     "state": state, "status": status,
                     "at": at, "last_ok": at, "probe": False})
    if settings.relay is not None:
        status = settings.relay.status
        if not status:
            state, text = "dim", "Not pushed yet"
        elif status.get("ok"):
            state, text = "ok", f"OK · public site has {status.get('remote_total', '?')} songs"
        else:
            state, text = "bad", f"Failed ({status.get('error')})"
        rows.append({"name": "relay-push", "label": "Relay to the public site",
                     "detail": config.relay.push_url if config else "", "state": state,
                     "status": text, "at": status.get("at"),
                     "last_ok": status.get("at") if status.get("ok") else None,
                     "probe": False, "resend": True})
    if config is not None and config.mesh.enabled:
        info = seen.get("mesh", {})
        link = info.get("status")
        rows.append({"name": "mesh", "label": "Mesh node", "detail": config.mesh.serial_port
                     or "auto-detected", "state": "ok" if link == "connected" else "bad",
                     "status": "Connected" if link == "connected" else "Not connected",
                     "at": info.get("at"), "last_ok": None, "probe": False})
    return rows


@router.get("/feeds", response_class=HTMLResponse)
async def feeds_page(request: Request):
    token = await require_admin(request)
    return render(request, "feeds.html", token, section="feeds",
                  feeds=feed_rows(request, time.time()), result=None)


@router.post("/feeds/{name}/probe", response_class=HTMLResponse)
async def probe_feed(request: Request, name: str):
    token, _form = await checked_form(request)
    config = settings_of(request).config
    feeds = {"corescope": config.corescope, "comchan": config.comchan} if config else {}
    feed = feeds.get(name)
    if feed is None or not feed.base_url:
        raise HTTPException(404, "no such feed")
    async with http_client(base_url=feed.base_url) as client:
        report = await probe(client, feed.channel, name=name)
    await ctx_of(request).db.log_admin(
        "probe_feed", ip=client_host(request), target=name, after="ok" if report.ok else "failed"
    )
    return render(request, "feeds.html", token, section="feeds",
                  feeds=feed_rows(request, time.time()),
                  result={"name": name, "ok": report.ok, "lines": format_probe(report)})


@router.post("/feeds/relay/resend")
async def relay_resend(request: Request):
    """Reset the home node's push cursor, so its next push sends the whole
    channel again — for a public site whose archive was lost or rolled back
    in a way the automatic count check can't see."""
    _token, _form = await checked_form(request)
    if settings_of(request).relay is None:
        raise HTTPException(404)
    db = ctx_of(request).db
    await db.set_setting(CURSOR_KEY, "")
    await db.log_admin("relay_resend", ip=client_host(request))
    return go(f"{ADMIN_PATH}/feeds")
