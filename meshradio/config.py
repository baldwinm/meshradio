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
class PlayerConfig:
    live_autoplay: bool = True     # auto-play new arrivals when idle in Live mode
    quiet_hours: str = ""          # "22:00-08:00" suppresses autoplay; empty = off
    timezone: str = "America/Chicago"
    volume: int = 70


@dataclass
class CacheConfig:
    max_bytes: int = 8 * 1024**3   # LRU prune cap (default 8 GB)
    ytdlp_bin: str = "yt-dlp"
    audio_format: str = "opus"
    max_retries: int = 3
    retry_backoff_s: int = 30


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class Config:
    hardware_profile: str = "dev"
    data_dir: Path = field(default_factory=lambda: Path("./data"))
    mesh: MeshConfig = field(default_factory=MeshConfig)
    corescope: CoreScopeConfig = field(default_factory=CoreScopeConfig)
    player: PlayerConfig = field(default_factory=PlayerConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    web: WebConfig = field(default_factory=WebConfig)

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
            for section in ("mesh", "corescope", "player", "cache", "web"):
                if section in raw:
                    _apply(getattr(cfg, section), raw[section])
            _apply(cfg, {k: v for k, v in raw.items() if not isinstance(v, dict)})
            break

    if cfg.hardware_profile not in VALID_PROFILES:
        raise ValueError(
            f"hardware_profile must be one of {VALID_PROFILES}, got {cfg.hardware_profile!r}"
        )
    return cfg
