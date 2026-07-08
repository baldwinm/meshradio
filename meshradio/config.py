"""Configuration: a TOML file layered over dataclass defaults.

Search order: --config CLI arg, $MESHRADIO_CONFIG, ./meshradio.toml,
/etc/meshradio/config.toml. Missing file = pure defaults (dev profile).

``hardware_profile`` selects backends everywhere: "pi4" (full kit),
"lite" (Zero 2 W, shared-I2S), "dev" (no hardware, null backends).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

VALID_PROFILES = ("dev", "pi4", "lite")


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
class LetsMeshConfig(CoreScopeConfig):
    """Backup channel feed — the LetsMesh MeshCore analyzer
    (analyzer.letsmesh.net), a CoreScope-compatible API polled exactly like the
    primary. Keeps the archive filling when the Austin CoreScope instance is
    down; messages both feeds (and the local mesh node) see dedupe against each
    other, so running it alongside a healthy CoreScope costs nothing."""
    enabled: bool = True
    base_url: str = "https://analyzer.letsmesh.net"


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
    live_window_s: int = 1800      # only tracks posted within this window auto-play;
                                   # older ones are backfill and stay archive-only


@dataclass
class CacheConfig:
    max_bytes: int = 8 * 1024**3   # LRU prune cap (default 8 GB)
    ytdlp_bin: str = "yt-dlp"
    audio_format: str = "opus"
    max_retries: int = 3
    retry_backoff_s: int = 30
    ffmpeg_location: str = ""      # dir/exe passed to yt-dlp when ffmpeg isn't on PATH
    ytdlp_extra_args: list = field(default_factory=list)  # e.g. ["--js-runtimes", "deno:C:/path/deno.exe"]


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    ingest_token: str = ""         # enables POST /api/ingest (relay receiver); empty = off.
                                   # Prefer the MESHRADIO_INGEST_TOKEN env var on hosts.


@dataclass
class RelayConfig:
    """Push this node's channel history to a hosted instance whose datacenter
    IP Cloudflare won't let poll CoreScope directly."""
    push_url: str = ""             # hosted instance base URL, e.g. https://meshradio.example.org
    token: str = ""                # must match the receiver's ingest token
    interval_s: int = 120


@dataclass
class Config:
    hardware_profile: str = "dev"
    data_dir: Path = field(default_factory=lambda: Path("./data"))
    mesh: MeshConfig = field(default_factory=MeshConfig)
    corescope: CoreScopeConfig = field(default_factory=CoreScopeConfig)
    letsmesh: LetsMeshConfig = field(default_factory=LetsMeshConfig)
    player: PlayerConfig = field(default_factory=PlayerConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    web: WebConfig = field(default_factory=WebConfig)
    relay: RelayConfig = field(default_factory=RelayConfig)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "meshradio.db"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"


def _apply(section_obj, data: dict) -> None:
    for key, value in data.items():
        if hasattr(section_obj, key):
            current = getattr(section_obj, key)
            if isinstance(current, Path):
                value = Path(value)
            setattr(section_obj, key, value)


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
            for section in ("mesh", "corescope", "letsmesh", "player", "cache", "web", "relay"):
                if section in raw:
                    _apply(getattr(cfg, section), raw[section])
            _apply(cfg, {k: v for k, v in raw.items() if not isinstance(v, dict)})
            break

    # Secrets belong in the environment, not in a committed config file.
    env_token = os.environ.get("MESHRADIO_INGEST_TOKEN")
    if env_token:
        cfg.web.ingest_token = env_token

    if cfg.hardware_profile not in VALID_PROFILES:
        raise ValueError(
            f"hardware_profile must be one of {VALID_PROFILES}, got {cfg.hardware_profile!r}"
        )
    return cfg
