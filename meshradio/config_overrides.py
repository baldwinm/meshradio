"""Settings the admin page may change, and how its changes are kept.

The config file stays the operator's: the service can't write it (it's
root's, under /etc, and the unit's file system is read-only), and the
public site's comes from the repository. So a change made on the admin
page is kept in the archive (one JSON object under ``OVERRIDES_KEY`` in the
settings table) and laid over the file's values at startup. Clearing one
puts the file's value back.

Only settings that are safe to hand a browser are here. Not secrets, not
URLs (an analyzer or relay address is somewhere the server would then
connect, with a token), not commands (yt-dlp's path and arguments run as a
process), not paths, and nothing that loosens the site's own defences
(security headers, rate limits, trusted proxies, allowed hosts): those
stay in the file, where changing them takes shell access.

Most of these are read where they're used, so changing the running
config's value is the change. The few that set something up once at
startup say so (``applies="restart"``).
"""

from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from .config import Config, ConfigError, validate_config

log = logging.getLogger(__name__)

OVERRIDES_KEY = "admin.config_overrides"

GIB = 1024**3


@dataclass(frozen=True)
class Setting:
    section: str
    name: str
    label: str
    help: str
    control: str             # toggle | slider | number | quiet_hours
    group: str
    lo: int = 0              # bounds and step in display units
    hi: int = 0
    step: int = 1
    unit: str = ""
    scale: int = 1           # stored value = display value * scale
    applies: str = "live"    # live | wait (after the current pause) | restart
    device_only: bool = False
    needs_relay: bool = False

    @property
    def key(self) -> str:
        return f"{self.section}.{self.name}"


GROUPS = [
    ("playback", "Playback"),
    ("feeds", "Feeds"),
    ("downloads", "Downloads"),
    ("backups", "Backups"),
]

SETTINGS: list[Setting] = [
    Setting("player", "live_autoplay", "Auto-play new songs",
            "When nothing is playing, a song posted to the channel starts on its own.",
            "toggle", "playback"),
    Setting("player", "quiet_hours", "Quiet hours",
            "Auto-play stays off between these times (the channel's time zone).",
            "quiet_hours", "playback", device_only=True),
    Setting("player", "volume", "Starting volume",
            "The volume the radio starts at. The player's own control changes it while it plays.",
            "slider", "playback", 0, 100, 1, "%", applies="restart", device_only=True),
    Setting("player", "live_window_s", "Live window",
            "Only songs posted this recently auto-play or join the queue; older ones stay "
            "in the archive.",
            "slider", "playback", 0, 360, 5, "min", scale=60),
    Setting("player", "max_queue", "Queue limit",
            "The most songs one queue holds.",
            "number", "playback", 10, 1000, 10, "songs"),
    Setting("player", "station_batch", "Archive station top-up",
            "Songs added each time Keep playing runs low.",
            "slider", "playback", 1, 50, 1, "songs"),
    Setting("player", "radio_batch", "Radio mode top-up",
            "Songs pulled from a YouTube Mix each time radio mode runs low.",
            "slider", "playback", 1, 50, 1, "songs", device_only=True),
    Setting("corescope", "enabled", "Poll CoreScope",
            "The main analyzer feed.", "toggle", "feeds", applies="restart"),
    Setting("corescope", "poll_interval_s", "CoreScope poll interval",
            "How long between checks of the main feed.",
            "slider", "feeds", 30, 1800, 30, "s", applies="wait"),
    Setting("comchan", "enabled", "Poll comchan",
            "The backup analyzer feed.", "toggle", "feeds", applies="restart"),
    Setting("comchan", "poll_interval_s", "comchan poll interval",
            "How long between checks of the backup feed.",
            "slider", "feeds", 30, 1800, 30, "s", applies="wait"),
    Setting("relay", "interval_s", "Relay push interval",
            "How long between pushes of new channel history to the public site.",
            "slider", "feeds", 30, 1800, 30, "s", applies="wait", needs_relay=True),
    Setting("cache", "max_bytes", "Audio cache size",
            "Downloaded songs past this are pruned, least recently played first.",
            "slider", "downloads", 1, 128, 1, "GB", scale=GIB, device_only=True),
    Setting("cache", "concurrency", "Downloads at once",
            "A Pi copes with 2.", "slider", "downloads", 1, 4, 1, "",
            applies="restart", device_only=True),
    Setting("cache", "max_retries", "Download attempts",
            "Tries per song before it's marked failed.",
            "slider", "downloads", 1, 10, 1, "", device_only=True),
    Setting("cache", "retry_backoff_s", "Retry pause",
            "Wait before a retry, longer each time.",
            "slider", "downloads", 0, 600, 10, "s", device_only=True),
    Setting("backup", "enabled", "Scheduled backups",
            "Snapshots of the archive on a timer.", "toggle", "backups", applies="restart"),
    Setting("backup", "interval_s", "Backup interval",
            "How long between scheduled snapshots.",
            "slider", "backups", 1, 48, 1, "h", scale=3600, applies="wait"),
    Setting("backup", "keep", "Snapshots kept",
            "Older ones are deleted.", "slider", "backups", 1, 50, 1, ""),
]

BY_KEY = {s.key: s for s in SETTINGS}

_TIME = re.compile(r"\A([01]\d|2[0-3]):[0-5]\d\Z")


def visible(config: Config, embed: bool) -> list[Setting]:
    """The settings this instance offers: the public site has no device, and
    a relay interval means nothing without a relay."""
    relay_on = bool(config.relay.push_url and config.relay.token)
    return [
        s for s in SETTINGS
        if not (embed and s.device_only) and not (s.needs_relay and not relay_on)
    ]


def current(config: Config, setting: Setting) -> Any:
    return getattr(getattr(config, setting.section), setting.name)


def display(setting: Setting, value: Any) -> Any:
    """A stored value in the units the control shows."""
    if setting.control in ("slider", "number") and setting.scale != 1:
        return round(value / setting.scale)
    return value


def describe(setting: Setting, value: Any) -> str:
    """A value as the activity log and the page say it."""
    if setting.control == "toggle":
        return "on" if value else "off"
    if setting.control == "quiet_hours":
        return value.replace("-", " to ") if value else "off"
    shown = display(setting, value)
    if setting.unit == "%":
        return f"{shown}%"
    return f"{shown} {setting.unit}".strip()


def bounds(setting: Setting, shown: Any) -> tuple[int, int]:
    """The control's range, stretched to take in a value the file set
    outside it, so the page shows (and leaves alone) what's really in force."""
    lo, hi = setting.lo, setting.hi
    if isinstance(shown, int) and not isinstance(shown, bool):
        lo, hi = min(lo, shown), max(hi, shown)
    return lo, hi


def parse(setting: Setting, form: dict[str, list[str]], shown: Any = None) -> Any:
    """The stored value a submitted form asks for. Raises ValueError with a
    message for the operator when it isn't one this setting takes. ``shown``
    is the value the page showed, accepted even outside the usual range."""
    def one(name: str) -> str:
        values = form.get(name)
        return values[0].strip() if values else ""

    key = setting.key
    if setting.control == "toggle":
        return one(key) in ("on", "true", "1")
    if setting.control == "quiet_hours":
        if one(f"{key}.on") not in ("on", "true", "1"):
            return ""
        start, end = one(f"{key}.start"), one(f"{key}.end")
        if not (_TIME.match(start) and _TIME.match(end)):
            raise ValueError(f"{setting.label}: give both times as HH:MM.")
        if start == end:
            raise ValueError(f"{setting.label}: the start and end can't be the same time.")
        return f"{start}-{end}"
    raw = one(key)
    try:
        number = int(raw)
    except ValueError:
        raise ValueError(f"{setting.label}: {raw!r} isn't a whole number.") from None
    if number != shown and not setting.lo <= number <= setting.hi:
        raise ValueError(
            f"{setting.label}: must be between {setting.lo} and {setting.hi}"
            f"{' ' + setting.unit if setting.unit else ''}."
        )
    return number * setting.scale


def check(config: Config, setting: Setting, value: Any) -> None:
    """Raise ConfigError if ``value`` would leave a config the radio can't run."""
    trial = copy.deepcopy(config)
    setattr(getattr(trial, setting.section), setting.name, value)
    validate_config(trial)


def apply(config: Config, setting: Setting, value: Any) -> None:
    setattr(getattr(config, setting.section), setting.name, value)


async def load(db) -> dict[str, Any]:
    raw = await db.get_setting(OVERRIDES_KEY, "{}")
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        log.error("admin config overrides unreadable; ignoring them")
        return {}
    return data if isinstance(data, dict) else {}


async def save(db, overrides: dict[str, Any]) -> None:
    await db.set_setting(OVERRIDES_KEY, json.dumps(overrides, sort_keys=True))


def apply_saved(config: Config, overrides: dict[str, Any]) -> dict[str, Any]:
    """Lay saved admin-page values over the file's at startup. One that's no
    longer allowed or no longer valid (a key removed from SETTINGS, a bound
    tightened) is skipped with a warning rather than stopping the radio.
    Returns the ones applied."""
    applied = {}
    for key, value in overrides.items():
        setting = BY_KEY.get(key)
        if setting is None:
            log.warning("admin config: %s can't be set from the admin page; ignored", key)
            continue
        try:
            check(config, setting, value)
        except ConfigError as exc:
            log.warning("admin config: %s ignored: %s", key, exc)
            continue
        apply(config, setting, value)
        applied[key] = value
    if applied:
        log.info("admin page settings in force: %s", ", ".join(sorted(applied)))
    return applied
