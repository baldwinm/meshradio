"""Settings: the ones a browser may change (``config_overrides.SETTINGS``),
saved in the database and laid over the config file, and the rest of the
running config shown read-only."""

from __future__ import annotations

import dataclasses
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ... import config_overrides as overrides_mod
from ...config import ConfigError
from ..admin_auth import ADMIN_PATH
from ..context import ctx_of
from .common import checked_form, client_ip, field, go, is_embed, render, require_admin, settings_of

router = APIRouter(prefix=ADMIN_PATH)

# Secrets in the config view show only their last four characters.
SECRET_KEYS = {
    ("web", "ingest_token"), ("relay", "token"), ("mesh", "channel_key"),
    ("web", "admin_password_hash"), ("web", "admin_totp_secret"),
    ("concerts", "ticketmaster_key"),
}
# Where each setting the environment can override comes from.
ENV_KEYS = {
    ("web", "ingest_token"): "MESHRADIO_INGEST_TOKEN",
    ("relay", "token"): "MESHRADIO_RELAY_TOKEN",
    ("web", "admin_password_hash"): "MESHRADIO_ADMIN_PASSWORD_HASH",
    ("web", "admin_totp_secret"): "MESHRADIO_ADMIN_TOTP_SECRET",
    ("concerts", "ticketmaster_key"): "MESHRADIO_TICKETMASTER_KEY",
}
# Device-only settings, left off the public site's config view: the public
# instance has no hardware, so they say nothing true about it.
DEVICE_SECTIONS = {"mesh", "cache"}
DEVICE_KEYS = {("player", "quiet_hours"), ("player", "volume")}


def config_rows(
    config: Any, embed: bool, skip: frozenset[str] | set[str] = frozenset()
) -> list[dict[str, Any]]:
    """The running config, one row per setting, secrets masked. ``skip``
    leaves out keys shown elsewhere (the editable ones)."""
    rows = []
    defaults = type(config)()
    for section in dataclasses.fields(config):
        value = getattr(config, section.name)
        if dataclasses.is_dataclass(value):
            if embed and section.name in DEVICE_SECTIONS:
                continue
            default_section = getattr(defaults, section.name)
            for item in dataclasses.fields(value):
                if embed and (section.name, item.name) in DEVICE_KEYS:
                    continue
                if f"{section.name}.{item.name}" in skip:
                    continue
                rows.append(_config_row(
                    section.name, item.name, getattr(value, item.name),
                    getattr(default_section, item.name),
                ))
        else:
            rows.append(_config_row("", section.name, value, getattr(defaults, section.name)))
    return rows


def _config_row(section: str, name: str, value: Any, default: Any) -> dict[str, Any]:
    env = ENV_KEYS.get((section, name))
    if env and os.environ.get(env):
        source = "environment"
    elif value == default:
        source = "default"
    else:
        source = "config file"
    if (section, name) in SECRET_KEYS:
        shown = f"•••• {str(value)[-4:]}" if value else "not set"
    elif isinstance(value, list):
        shown = ", ".join(str(v) for v in value) or "(none)"
    elif isinstance(value, bool):
        shown = "true" if value else "false"
    else:
        shown = str(value) if value != "" else "(empty)"
    return {"key": f"{section}.{name}" if section else name, "value": shown, "source": source}


def _config_groups(request: Request) -> list[dict[str, Any]]:
    """The editable settings, grouped, each with what its control needs."""
    admin = settings_of(request)
    config = admin.config
    groups = []
    for group_key, title in overrides_mod.GROUPS:
        items = []
        for setting in overrides_mod.visible(config, is_embed(request)):
            if setting.group != group_key:
                continue
            value = overrides_mod.current(config, setting)
            file_value = admin.file_values.get(setting.key, value)
            start, end = "22:00", "08:00"
            if setting.control == "quiet_hours" and value:
                start, end = value.split("-", 1)
            items.append({
                "s": setting,
                "value": value,
                "shown": overrides_mod.display(setting, value),
                "range": overrides_mod.bounds(setting, overrides_mod.display(setting, value)),
                "described": overrides_mod.describe(setting, value),
                "overridden": setting.key in admin.overrides,
                "file": overrides_mod.describe(setting, file_value),
                "waiting": setting.applies == "restart"
                and admin.started.get(setting.key) != value,
                "start": start.strip(),
                "end": end.strip(),
            })
        if items:
            groups.append({"key": group_key, "title": title, "items": items})
    return groups


@router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request, saved: str = "", error: str = "", group: str = ""):
    token = await require_admin(request)
    admin = settings_of(request)
    config = admin.config
    if config is None:
        return render(request, "config.html", token, section="config", rows=[], groups=[],
                      two_step=bool(admin.totp_secret))
    groups = _config_groups(request)
    editable = {i["s"].key for g in groups for i in g["items"]}
    waiting = [i["s"].label for g in groups for i in g["items"] if i["waiting"]]
    return render(
        request, "config.html", token, section="config",
        groups=groups, rows=config_rows(config, is_embed(request), editable),
        two_step=bool(admin.totp_secret), waiting=waiting,
        saved=saved[:200], error=error[:300],
        group=group,
    )


async def set_override(
    request: Request, setting: overrides_mod.Setting, value: Any, overridden: bool
) -> str | None:
    """Put ``setting`` at ``value`` (kept as an admin-page change when
    ``overridden``, else back to the file's), check the result is a config
    the radio runs on, apply it to the running config and save. Returns an
    error for the operator, or None."""
    admin = settings_of(request)
    if not overridden:
        value = admin.file_values.get(setting.key, overrides_mod.current(admin.config, setting))
    try:
        overrides_mod.check(admin.config, setting, value)
    except ConfigError as exc:
        return f"{setting.label}: {str(exc).splitlines()[-1].strip()}"
    db = ctx_of(request).db
    saved = await overrides_mod.load(db)
    if overridden:
        saved[setting.key] = value
    else:
        saved.pop(setting.key, None)
    await overrides_mod.save(db, saved)
    admin.overrides = saved
    overrides_mod.apply(admin.config, setting, value)
    return None


async def _record_change(
    request: Request, setting: overrides_mod.Setting, before: Any, had: bool
) -> None:
    admin = settings_of(request)
    after = overrides_mod.current(admin.config, setting)
    await ctx_of(request).db.log_admin(
        "change_setting", ip=client_ip(request), target=setting.label,
        before=overrides_mod.describe(setting, before),
        after=overrides_mod.describe(setting, after)
        + ("" if setting.key in admin.overrides else " (the file's)"),
        undo={"key": setting.key, "override": before, "had": had},
    )


@router.post("/config")
async def save_config(request: Request):
    _token, form = await checked_form(request)
    admin = settings_of(request)
    if admin.config is None:
        raise HTTPException(404)
    group = field(form, "group")
    settings = [s for s in overrides_mod.visible(admin.config, is_embed(request))
                if s.group == group]
    if not settings:
        raise HTTPException(400, "unknown settings group")
    # Every value first, so one bad field changes nothing.
    wanted = []
    for setting in settings:
        before = overrides_mod.current(admin.config, setting)
        try:
            value = overrides_mod.parse(setting, form, overrides_mod.display(setting, before))
        except ValueError as exc:
            return go(f"{ADMIN_PATH}/config", error=str(exc), group=group)
        # Compared as the control shows it, so a value the file gives more
        # finely than the slider steps isn't "changed" by an untouched slider.
        if overrides_mod.display(setting, value) != overrides_mod.display(setting, before):
            wanted.append((setting, value, before))
    for setting, value, _before in wanted:
        try:
            overrides_mod.check(admin.config, setting, value)
        except ConfigError as exc:
            return go(f"{ADMIN_PATH}/config", group=group,
                      error=f"{setting.label}: {str(exc).splitlines()[-1].strip()}")
    for setting, value, before in wanted:
        had = setting.key in admin.overrides
        error = await set_override(request, setting, value, True)
        if error:
            return go(f"{ADMIN_PATH}/config", error=error, group=group)
        await _record_change(request, setting, before, had)
    names = ", ".join(s.label for s, _v, _b in wanted)
    return go(f"{ADMIN_PATH}/config", group=group,
              saved=f"Saved: {names}." if wanted else "Nothing changed.")


@router.post("/config/reset")
async def reset_setting(request: Request):
    _token, form = await checked_form(request)
    admin = settings_of(request)
    if admin.config is None:
        raise HTTPException(404)
    setting = overrides_mod.BY_KEY.get(field(form, "reset"))
    if setting is None or setting not in overrides_mod.visible(admin.config, is_embed(request)):
        raise HTTPException(400, "not a setting this page changes")
    if setting.key not in admin.overrides:
        return go(f"{ADMIN_PATH}/config", group=setting.group,
                  saved=f"{setting.label} already uses the file's value.")
    before = overrides_mod.current(admin.config, setting)
    error = await set_override(request, setting, None, False)
    if error:
        return go(f"{ADMIN_PATH}/config", error=error, group=setting.group)
    await _record_change(request, setting, before, True)
    return go(f"{ADMIN_PATH}/config", group=setting.group,
              saved=f"{setting.label} is back to the file's value.")
