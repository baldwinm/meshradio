"""Configuration: a TOML file layered over dataclass defaults.

Search order: --config CLI arg, $MESHRADIO_CONFIG, ./meshradio.toml,
/etc/meshradio/config.toml. Missing file = pure defaults (dev profile).

``hardware_profile`` selects backends everywhere: "pi4" (full kit),
"lite" (Zero 2 W, shared-I2S), "dev" (no hardware, null backends).
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

VALID_PROFILES = ("dev", "pi4", "lite")
VALID_BACKENDS = ("auto", "mpv", "web", "embed", "null")
# What yt-dlp's --audio-format accepts. The value is also the cache file's
# extension, so this doubles as the path-safe set.
VALID_AUDIO_FORMATS = ("best", "aac", "alac", "flac", "m4a", "mp3", "opus", "vorbis", "wav")


@dataclass
class MeshConfig:
    enabled: bool = False
    serial_port: str = ""          # empty = autodetect /dev/ttyUSB*
    channel: str = "#music"
    channel_key: str = ""          # MeshCore channel key, set at provisioning


@dataclass
class CoreScopeConfig:
    enabled: bool = True
    base_url: str = ""             # AUS CoreScope instance, set at provisioning
    channel: str = "#music"
    poll_interval_s: int = 180


@dataclass
class ComchanConfig(CoreScopeConfig):
    """Backup analyzer feed — a second CoreScope-compatible instance polled
    alongside the primary, so an outage there doesn't stop ingestion.

    Unlike the primary, ``base_url`` carries a default: a backup nobody
    remembered to configure is no backup, and appliance configs written at
    provisioning (system/provision.py) have no section for it. Both feeds
    run continuously — dedupe keys on channel+sender+video+minute, not
    source, so their overlap no-ops rather than needing failover logic.
    """
    base_url: str = "https://analyzer.comchan.net"


@dataclass
class PlayerConfig:
    backend: str = "auto"          # auto | mpv | web | embed | null; auto = mpv on pi4/lite,
                                   # web on dev. embed = YouTube IFrame in the browser, no
                                   # downloads (the mode for public hosting)
    live_autoplay: bool = True     # auto-play new arrivals when idle in Live mode
    quiet_hours: str = ""          # "22:00-08:00" suppresses autoplay; empty = off
    timezone: str = "America/Chicago"
    volume: int = 70
    radio_batch: int = 10          # tracks pulled per YouTube Mix fetch in radio mode
    station_batch: int = 10        # archived songs queued per archive-station top-up
    live_window_s: int = 1800      # only tracks posted within this window auto-play;
                                   # older ones are backfill and stay archive-only
    max_queue: int = 200           # ceiling on queued tracks per player; a song already
                                   # playing or queued is never added twice


@dataclass
class CacheConfig:
    max_bytes: int = 8 * 1024**3   # LRU prune cap (default 8 GB)
    ytdlp_bin: str = "yt-dlp"
    audio_format: str = "opus"
    max_retries: int = 3
    retry_backoff_s: int = 30
    ffmpeg_location: str = ""      # dir/exe passed to yt-dlp when ffmpeg isn't on PATH
    concurrency: int = 2           # downloads (yt-dlp processes) or oEmbed lookups in flight
                                   # at once; one stuck fetch no longer stalls the backlog
    ytdlp_extra_args: list = field(default_factory=list)  # e.g. ["--js-runtimes", "deno:C:/path/deno.exe"]


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    ingest_token: str = ""         # enables POST /api/ingest (relay receiver); empty = off.
                                   # Prefer the MESHRADIO_INGEST_TOKEN env var on hosts.
    # Host names this instance answers to (Starlette TrustedHost syntax;
    # "*.example.org" wildcards allowed). Empty = any, which is what a LAN
    # appliance reached by IP, .local name and port-forward all need. Set it
    # to pin the appliance against DNS rebinding, or a host to its domain.
    allowed_hosts: list = field(default_factory=list)
    # The URL visitors use ("https://meshradio.example.org"): canonical
    # links, link previews and the sitemap are built from it instead of from
    # whatever Host header a request carried. Empty = derive from the request.
    public_url: str = ""
    # Content-Security-Policy and friends on every response (see
    # web/server.py: SecurityHeaders). Off only if a proxy in front already
    # sets them. csp_report_only keeps the policy advisory — the browser
    # console reports what it would have blocked — for trying a change out.
    security_headers: bool = True
    csp_report_only: bool = False
    # Reverse proxies whose X-Forwarded-* headers are believed — for the
    # visitor's real address (what the rate limiter keys on) and whether they
    # arrived over https (the cookie's Secure flag, canonical links). The
    # default is uvicorn's own: the loopback address. "*" trusts every peer,
    # which is right for a host like Render where nothing reaches the app
    # except through its proxy, and wrong for a LAN appliance anyone can
    # connect to directly.
    trusted_proxies: list = field(default_factory=lambda: ["127.0.0.1"])
    # Per-client ceilings on presses (POSTs) and searches — see
    # web/ratelimit.py. Off only behind a proxy that already enforces its own.
    rate_limit: bool = True


@dataclass
class RelayConfig:
    """Push this node's channel history to a hosted instance whose datacenter
    IP Cloudflare won't let poll CoreScope directly."""
    push_url: str = ""             # hosted instance base URL, e.g. https://meshradio.example.org
    token: str = ""                # must match the receiver's ingest token
    interval_s: int = 120


@dataclass
class BackupConfig:
    """Rotating on-disk snapshots of the archive DB — a rollback point for a
    bad migration or corruption, independent of any host-level disk snapshot."""
    enabled: bool = True
    interval_s: int = 21600        # 6h between periodic snapshots
    keep: int = 8                  # how many snapshots to retain (older pruned)
    dir: str = ""                  # snapshot dir; empty = <data_dir>/backups


@dataclass
class Config:
    hardware_profile: str = "dev"
    data_dir: Path = field(default_factory=lambda: Path("./data"))
    mesh: MeshConfig = field(default_factory=MeshConfig)
    corescope: CoreScopeConfig = field(default_factory=CoreScopeConfig)
    comchan: ComchanConfig = field(default_factory=ComchanConfig)
    player: PlayerConfig = field(default_factory=PlayerConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    web: WebConfig = field(default_factory=WebConfig)
    relay: RelayConfig = field(default_factory=RelayConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "meshradio.db"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def backup_dir(self) -> Path:
        return Path(self.backup.dir) if self.backup.dir else self.data_dir / "backups"


class ConfigError(ValueError):
    """The config says something the radio can't run on. The message names
    every offending key, so a file is fixed in one pass."""


def _apply(section_obj, data: dict, label: str) -> None:
    for key, value in data.items():
        if not hasattr(section_obj, key):
            # A misspelt key silently leaving the default in force is the
            # kind of mistake that only shows up as "why isn't it polling".
            log.warning("config: unknown key %r in %s ignored", key, label)
            continue
        current = getattr(section_obj, key)
        if isinstance(current, Path):
            value = Path(value)
        setattr(section_obj, key, value)


# Every numeric key, with its floor and ceiling. TOML parses what it's given,
# and a value that parses isn't a value the radio can run on: a negative
# interval makes asyncio.sleep return at once, so one typo turned a poller
# into a hot loop against the analyzer; a quoted number crashes the loop
# that uses it, which the supervisor then restarts forever.
_INTS: list[tuple[str, str, int | None, int | None]] = [
    ("corescope", "poll_interval_s", 1, None),
    ("comchan", "poll_interval_s", 1, None),
    ("player", "volume", 0, 100),
    ("player", "radio_batch", 1, None),
    ("player", "station_batch", 1, None),
    ("player", "live_window_s", 0, None),
    ("player", "max_queue", 1, None),
    ("cache", "max_bytes", 0, None),
    ("cache", "max_retries", 1, None),
    ("cache", "retry_backoff_s", 0, None),
    ("cache", "concurrency", 1, None),
    ("web", "port", 1, 65535),
    ("relay", "interval_s", 1, None),
    ("backup", "interval_s", 1, None),
    ("backup", "keep", 0, None),
]
_BOOLS = [
    ("mesh", "enabled"), ("corescope", "enabled"), ("comchan", "enabled"),
    ("player", "live_autoplay"), ("web", "security_headers"),
    ("web", "csp_report_only"), ("web", "rate_limit"), ("backup", "enabled"),
]
_STRINGS = [
    ("mesh", "serial_port"), ("mesh", "channel"), ("mesh", "channel_key"),
    ("corescope", "base_url"), ("corescope", "channel"),
    ("comchan", "base_url"), ("comchan", "channel"),
    ("player", "backend"), ("player", "quiet_hours"), ("player", "timezone"),
    ("cache", "ytdlp_bin"), ("cache", "audio_format"), ("cache", "ffmpeg_location"),
    ("web", "host"), ("web", "ingest_token"), ("web", "public_url"),
    ("relay", "push_url"), ("relay", "token"), ("backup", "dir"),
]
_STRING_LISTS = [
    ("web", "allowed_hosts"), ("web", "trusted_proxies"), ("cache", "ytdlp_extra_args"),
]


def _quiet_hours_ok(spec: str) -> bool:
    """Empty, or ``HH:MM-HH:MM`` (the shape PlayerService.in_quiet_hours reads)."""
    if not spec:
        return True
    parts = spec.split("-")
    if len(parts) != 2:
        return False
    try:
        for part in parts:
            datetime.strptime(part.strip(), "%H:%M")
    except ValueError:
        return False
    return True


def validate_config(cfg: Config) -> None:
    """Raise ConfigError naming every key whose value the radio can't run on."""
    problems: list[str] = []

    def key(section: str, name: str) -> str:
        return f"[{section}] {name}"

    for section, name, lo, hi in _INTS:
        value = getattr(getattr(cfg, section), name)
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append(f"{key(section, name)} must be an integer, got {value!r}")
        elif (lo is not None and value < lo) or (hi is not None and value > hi):
            bounds = f"at least {lo}" if hi is None else f"between {lo} and {hi}"
            problems.append(f"{key(section, name)} must be {bounds}, got {value!r}")
    for section, name in _BOOLS:
        value = getattr(getattr(cfg, section), name)
        if not isinstance(value, bool):
            problems.append(f"{key(section, name)} must be true or false, got {value!r}")
    for section, name in _STRINGS:
        value = getattr(getattr(cfg, section), name)
        if not isinstance(value, str):
            problems.append(f"{key(section, name)} must be a string, got {value!r}")
    for section, name in _STRING_LISTS:
        value = getattr(getattr(cfg, section), name)
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            problems.append(f"{key(section, name)} must be a list of strings, got {value!r}")

    if cfg.hardware_profile not in VALID_PROFILES:
        problems.append(
            f"hardware_profile must be one of {VALID_PROFILES}, got {cfg.hardware_profile!r}"
        )
    if isinstance(cfg.player.backend, str) and cfg.player.backend not in VALID_BACKENDS:
        problems.append(
            f"{key('player', 'backend')} must be one of {VALID_BACKENDS}, "
            f"got {cfg.player.backend!r}"
        )
    if isinstance(cfg.cache.audio_format, str) and cfg.cache.audio_format not in VALID_AUDIO_FORMATS:
        problems.append(
            f"{key('cache', 'audio_format')} must be one of {VALID_AUDIO_FORMATS}, "
            f"got {cfg.cache.audio_format!r}"
        )
    if isinstance(cfg.player.quiet_hours, str) and not _quiet_hours_ok(cfg.player.quiet_hours):
        problems.append(
            f"{key('player', 'quiet_hours')} must be empty or \"HH:MM-HH:MM\", "
            f"got {cfg.player.quiet_hours!r}"
        )
    if isinstance(cfg.player.timezone, str):
        try:
            ZoneInfo(cfg.player.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            problems.append(
                f"{key('player', 'timezone')} is not a known time zone: {cfg.player.timezone!r}"
            )
    if problems:
        raise ConfigError("config:\n  " + "\n  ".join(problems))


def load_config(path: str | Path | None = None) -> Config:
    cfg = Config()
    candidates = [
        path,
        os.environ.get("MESHRADIO_CONFIG"),
        "meshradio.toml",
        "/etc/meshradio/config.toml",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            with open(candidate, "rb") as f:
                raw = tomllib.load(f)
            sections = ("mesh", "corescope", "comchan", "player", "cache",
                        "web", "relay", "backup")
            for section in sections:
                if section in raw:
                    _apply(getattr(cfg, section), raw[section], f"[{section}]")
            top = {k: v for k, v in raw.items() if not isinstance(v, dict)}
            _apply(cfg, top, "the top level")
            for section in raw:
                if isinstance(raw[section], dict) and section not in sections:
                    log.warning("config: unknown section [%s] ignored", section)
            break

    # Secrets belong in the environment, not in a committed config file: the
    # receiver's token on a host, the pusher's on the home node.
    env_token = os.environ.get("MESHRADIO_INGEST_TOKEN")
    if env_token:
        cfg.web.ingest_token = env_token
    env_relay = os.environ.get("MESHRADIO_RELAY_TOKEN")
    if env_relay:
        cfg.relay.token = env_relay

    validate_config(cfg)
    return cfg
