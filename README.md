# MeshRadio

*A standalone internet radio that plays the Austin MeshCore `#music` channel.*

MeshRadio is a shelf/portable appliance that listens to the `#music` public
channel on the Austin MeshCore mesh, extracts the YouTube Music links members
post against the daily theme, and plays them — live as they arrive, and from a
browsable archive of past days and themes.

Full design: [meshradio-architecture.md](meshradio-architecture.md).

## Status

**v0.1 — core software working, hardware integration pending.**

| Area | State |
|---|---|
| Event bus, SQLite archive, migrations | ✅ working, tested |
| Link/theme parsing (channel convention) | ✅ working, tested |
| Ingest pipeline + mesh/CoreScope dedupe | ✅ working, tested |
| Cache-first downloader (yt-dlp) + fallback ladder | ✅ working (oEmbed metadata-only fallback) |
| Player: live-mode policy, queue, archive replay, quiet hours | ✅ working, tested |
| Web UI (FastAPI + htmx + WebSocket) | ✅ working |
| CoreScope poller | ✅ built against the real CoreScope API, tested; needs the AUS instance URL in config |
| Mesh serial ingestion (meshcore) | 🟡 built, needs validation on a Heltec V3 |
| OLED panel + encoder/buttons | 🟡 skeleton, needs hardware bring-up |
| PipeWire routing (pi4/lite backends) | 🟡 built, needs hardware bring-up |
| Bluetooth pairing (BlueZ) | ⬜ interface stubbed |
| UPS fuel gauge / safe shutdown | ⬜ stubbed |
| First-boot provisioning, pi-gen image, STLs, BOM docs | ⬜ not started |

## Dev quickstart (any OS, no hardware)

```sh
uv venv && uv pip install -e . --group dev   # or: pip install -e .
pytest                                        # 52 tests
python -m meshradio.app --demo -v             # simulated channel traffic
```

Then open http://localhost:8080 — the `--demo` flag pushes a fake day of
channel messages through the real pipeline (theme post, three links from
three senders) with simulated playback, so you can watch the live jukebox,
queue, and archive work without a mesh node, yt-dlp, or mpv installed.

Configuration is TOML (`./meshradio.toml`, `$MESHRADIO_CONFIG`, or
`/etc/meshradio/config.toml`) — see [meshradio.example.toml](meshradio.example.toml).
`hardware_profile` selects backends: `dev` (null hardware), `pi4` (full kit),
`lite` (Zero 2 W variant).

## Layout

```
meshradio/
├── app.py           # asyncio entrypoint, wires modules to the bus
├── bus.py           # tiny pub/sub EventBus + event vocabulary
├── config.py        # TOML config over dataclass defaults
├── db.py            # aiosqlite layer + migrations (themes/tracks/plays/settings)
├── ingest/          # parse.py (pure), service.py, mesh.py, corescope.py
├── media/           # cacher.py (yt-dlp), player.py (mpv/null), metadata.py (oEmbed)
├── audio/           # routing.py (PipeWire/wpctl, per-profile), bluetooth.py
├── ui/              # panel.py (OLED + controls; log panel on dev)
├── system/          # power.py (fuel gauge), provision.py (first boot)
└── web/             # FastAPI + Jinja2 + vendored htmx, WebSocket live state
```

On the appliance, install with media + hardware extras:
`pip install -e ".[media,hw]"`.
