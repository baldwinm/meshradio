"""Backups: list the snapshots, take one, download one."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from ... import backup as backup_mod
from ..admin_auth import ADMIN_PATH
from ..context import ctx_of
from .common import checked_form, client_ip, go, render, require_admin, settings_of

router = APIRouter(prefix=ADMIN_PATH)

# What each backup's label means, for the Backups screen.
BACKUP_REASONS = {
    "auto": "Scheduled",
    "premigrate": "Before an upgrade (on boot)",
    "prerestore": "Before a restore",
    "preremove": "Before removing a song",
    "manual": "Taken from this page",
}


def snapshot_rows(backup_dir: Path) -> list[dict[str, Any]]:
    """The backup directory's snapshots, newest first, with what took them."""
    rows = []
    for path in reversed(backup_mod.list_snapshots(backup_dir)):
        match = re.match(r"meshradio-(\d{8}T\d{6}Z)-(.+)\.db\Z", path.name)
        taken = None
        label = ""
        if match:
            taken = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            label = match.group(2)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        rows.append({
            "name": path.name,
            "taken": taken.timestamp() if taken else None,
            "reason": BACKUP_REASONS.get(label, label or "—"),
            "size": size,
        })
    return rows


def _backup_config(request: Request) -> Any:
    config = settings_of(request).config
    if config is None:
        raise HTTPException(404, "backups aren't available here")
    return config


@router.get("/backups", response_class=HTMLResponse)
async def backups_page(request: Request):
    token = await require_admin(request)
    config = _backup_config(request)
    return render(request, "backups.html", token, section="backups",
                  snapshots=snapshot_rows(config.backup_dir), backup=config.backup,
                  same_disk=not config.backup.dir)


@router.post("/backups")
async def take_backup(request: Request):
    _token, _form = await checked_form(request)
    config = _backup_config(request)
    dest = await asyncio.to_thread(backup_mod.snapshot, config.db_path, config.backup_dir,
                                   "manual")
    await asyncio.to_thread(backup_mod.rotate, config.backup_dir, config.backup.keep)
    await ctx_of(request).db.log_admin(
        "backup", ip=client_ip(request), after=dest.name if dest else None
    )
    return go(f"{ADMIN_PATH}/backups")


@router.get("/backups/{name}")
async def download_backup(request: Request, name: str):
    await require_admin(request)
    config = _backup_config(request)
    # Only a name the listing produced: never a path built from the URL.
    match = next(
        (p for p in backup_mod.list_snapshots(config.backup_dir) if p.name == name), None
    )
    if match is None:
        raise HTTPException(404, "no such backup")
    await ctx_of(request).db.log_admin("download_backup", ip=client_ip(request), target=name)
    return FileResponse(
        match, media_type="application/vnd.sqlite3", filename=name,
        headers={"content-encoding": "identity"},
    )
