# MeshRadio — Architecture Document

*A standalone internet radio that plays the Austin MeshCore `#music` channel.*
*Status: v0.1 draft — architecture locked, implementation not started. Working name only; rename at will.*

---

## 1. Concept and locked requirements

MeshRadio is a shelf/portable appliance that listens to the `#music` public channel on the Austin MeshCore mesh, extracts the YouTube Music links that members post against the daily theme, and plays them — live as they arrive, and from a browsable archive of past days and themes.

Decisions locked during design:

| Decision | Choice |
|---|---|
| Link ingestion | Onboard MeshCore node (primary) + AUS CoreScope polling (fallback/backfill) |
| Playback source | yt-dlp primary, graceful metadata-only fallback |
| Playback model | Live jukebox + browsable archive by day/theme |
| Interaction | OLED + physical knobs **and** LAN web UI |
| Power | Battery with dock/charging (wall-capable) |
| Audio outputs | Built-in speaker, 3.5mm jack, Bluetooth (radio → user's BT speaker) |
| Distribution | Fully documented kit: BOM, STLs, flashable image, assembly guide |
| Enclosure | 3D printed, STLs in repo |
| Display | Small OLED (theme + track text) |
| Language | Python |

Kit design constraints that follow from "anyone can build it": every part orderable from Adafruit/Amazon/Mouser, **no SMD soldering** (header pins and screw terminals only), and first-boot setup that requires zero Linux knowledge.

---

## 2. Hardware architecture

### Compute: Raspberry Pi 4 (2GB)

The Pi 4 wins over the Zero 2 W for the kit build, despite worse power draw, for three reasons:

1. **Bluetooth/WiFi coexistence.** The Zero 2 W's combo chip shares one radio path; streaming over WiFi while sourcing A2DP audio to a BT speaker produces stutter. The fix is a dedicated USB BT dongle — but the Zero 2 W has a single micro-USB OTG port, which the MeshCore node also wants. The Pi 4's four USB-A ports make this a non-problem.
2. **yt-dlp extraction speed.** 2–4s on a Pi 4 vs 10–20s on a Zero 2 W. Matters for perceived responsiveness when someone posts a link.
3. **Kit assembly.** Full-size headers, full-size USB, no OTG adapters.

A **"Lite" variant** on the Zero 2 W (UART-wired node, no BT-out, smaller battery) can be documented later for cost-sensitive builders; the software should not assume Pi 4-only.

### MeshCore node: Heltec V3 over USB serial

Runs stock MeshCore **companion radio firmware**; the Pi talks to it over `/dev/ttyUSB*` using the `meshcore` Python library. The node carries the `#music` channel key. Antenna passes through the enclosure via an SMA bulkhead so the radio is also a functioning mesh client wherever it sits.

### Audio chain

Three outputs, one policy: **PipeWire owns routing; the app just selects the default sink.**

| Output | Hardware | Notes |
|---|---|---|
| Built-in speaker | MAX98357A I2S amp → 3" 4Ω full-range driver | ~3W mono; ported chamber designed into the STL for usable bass |
| 3.5mm jack | Pi 4 onboard A/V jack | Quality is "fine for a kitchen radio"; a USB DAC is a documented upgrade path |
| Bluetooth | USB BT 5.x dongle (onboard BT disabled) | A2DP **source** role via BlueZ/PipeWire; pairing initiated from OLED menu or web UI |

Routing behavior: manual selection from the encoder menu or web UI; auto-switch to BT when a paired speaker connects (configurable).

### Front panel

- **2.42" SSD1309 OLED** (128×64, I2C) — bigger sibling of the ubiquitous 0.96", far more readable, still ~$12.
- **Rotary encoder with push** — volume; push = play/pause; long-press = output select.
- **Two buttons** — Next/Skip and Mode (Live ↔ Archive browse).

### Power

UPS HAT with I2C fuel gauge and pass-through charging (Waveshare UPS HAT (B) or Geekworm X728 class) + 2–4× 18650 cells. At the Pi 4's ~3–4W average with the amp, 4 cells (~48Wh) yields roughly **8–12 hours** portable. The "dock" is simply the charge input on a 3D-printed stand — no pogo pins, no custom PCB, keeps the kit honest. Fuel gauge drives the OLED battery icon and a safe-shutdown at ~5%.

### Bill of materials (ballpark)

| Part | Est. |
|---|---|
| Raspberry Pi 4 (2GB) + SD card | $55 |
| Heltec V3 + SMA pigtail/bulkhead + antenna | $30 |
| USB Bluetooth 5.x dongle | $8 |
| MAX98357A breakout | $6 |
| 3" full-range driver | $10 |
| 2.42" SSD1309 OLED | $12 |
| Rotary encoder, buttons, wiring | $8 |
| UPS HAT + 4× 18650 | $50 |
| Filament, fasteners, misc | $10 |
| **Total** | **~$190** |

---

## 3. System diagram

```
                      ┌────────────────────────────────────────────┐
  Austin mesh         │  Raspberry Pi 4 — meshradio (one asyncio   │
  ~~~~~~~~~~~         │  Python app, systemd-managed)              │
 #music channel       │                                            │
      │               │  ┌──────────┐    ┌────────────────────┐    │
┌─────▼─────┐  USB    │  │ ingest   │───►│  SQLite            │    │
│ Heltec V3 ├────────►│  │ · mesh   │    │  themes / tracks / │    │
│ companion │ serial  │  │ · scope  │◄──►│  plays / settings  │    │
└───────────┘         │  └────┬─────┘    └─────────┬──────────┘    │
                      │       │ events             │               │
  AUS CoreScope ──────┼──►────┘              ┌─────▼─────┐         │
  (WiFi, poll)        │                      │  player   │──mpv──┐ │
                      │  ┌───────────┐       │  · queue  │       │ │
  YouTube ◄───────────┼──┤ cacher    │──────►│  · cache  │       │ │
  (yt-dlp)            │  │ (opus)    │       └───────────┘       │ │
                      │  └───────────┘                           │ │
                      │  ┌───────────┐    ┌───────────┐    ┌─────▼──────┐
                      │  │ panel     │    │ web       │    │ PipeWire   │
                      │  │ OLED+knob │    │ FastAPI + │    │ sink select│
                      │  └───────────┘    │ htmx + WS │    └─┬───┬───┬──┘
                      └───────────────────┴───────────┴──────┼───┼───┼──┘
                                                          spkr  3.5  BT
```

---

## 4. Software architecture

### One process, not five

A **single asyncio application** with clearly separated modules communicating over an in-process event bus, backed by SQLite. Not microservices, not MQTT-between-daemons. Rationale: this is an appliance, and the failure domain is the whole box anyway; one process means one systemd unit, one log stream, no IPC serialization bugs, and a codebase a future contributor (or an AI pair) can hold in their head. This is the single biggest maintainability decision in the project.

```
meshradio/
├── app.py              # asyncio entrypoint, wires modules to the bus
├── bus.py              # tiny pub/sub EventBus (asyncio queues, ~50 lines)
├── db.py               # aiosqlite layer + migrations
├── ingest/
│   ├── mesh.py         # meshcore serial client, #music subscription
│   ├── corescope.py    # CoreScope poller (fallback + backfill)
│   └── parse.py        # link extraction, theme detection  ← pure functions, unit-tested
├── media/
│   ├── cacher.py       # yt-dlp download-to-cache worker
│   ├── player.py       # mpv control via python-mpv (JSON IPC)
│   └── metadata.py     # oEmbed / fallback metadata resolution
├── audio/
│   ├── routing.py      # PipeWire sink selection (wpctl)
│   └── bluetooth.py    # BlueZ pairing/connection state machine
├── ui/
│   ├── panel.py        # OLED screens (luma.oled) + encoder/buttons (gpiozero)
│   └── screens/        # NowPlaying, Queue, Archive, Outputs, Status
├── web/
│   ├── server.py       # FastAPI, WebSocket for live state
│   ├── templates/      # Jinja2 + htmx — no JS build chain, ever
│   └── static/
├── system/
│   ├── power.py        # fuel gauge polling, safe shutdown
│   └── provision.py    # first-boot AP-mode WiFi setup (nmcli)
└── tests/
```

**Key dependency choices** (all boring on purpose): `meshcore`, `yt-dlp`, `python-mpv`, `FastAPI`+`uvicorn`, `htmx` (vendored single JS file), `luma.oled`, `gpiozero`, `aiosqlite`. No Redis, no Docker, no Node.

### Event flow

`ingest` publishes `track.discovered` → `cacher` downloads audio in the background and publishes `track.ready` → `player` enqueues per live-mode policy → `panel` and `web` subscribe to `player.state` and render. Every module is a subscriber/publisher on the bus and touches the DB through `db.py` only.

---

## 5. Data model

```sql
themes(  id, date, title, set_by, raw_message, created_at )
tracks(  id, video_id, url, title, artist, duration,
         theme_id → themes, sender, mesh_ts, ingested_at,
         source TEXT CHECK(source IN ('mesh','corescope')),
         cache_path, cache_status,          -- pending|ready|failed
         dedupe_hash UNIQUE )
plays(   id, track_id → tracks, played_at, output, completed )
settings(key, value)
```

`dedupe_hash = sha256(channel + sender + normalized_video_id + mesh_ts_bucketed_to_60s)` — this is what lets mesh and CoreScope ingestion coexist without double-entry: whichever path delivers the message first wins, the other no-ops on the UNIQUE constraint.

---

## 6. Ingestion

### Mesh path (primary)

`meshcore` client on the Heltec serial port, subscribed to `#music` (channel key in config). On each message: run `parse.extract_links()` (matches `music.youtube.com`, `youtube.com/watch`, `youtu.be`), normalize to a canonical video ID, attach sender + timestamp, insert.

### CoreScope path (fallback + backfill)

Poll the AUS CoreScope instance every 2–5 min for `#music` channel packets; same parser, same dedupe. Serves two jobs: catching messages the local node missed (RF is RF), and **backfilling history on first boot** so a freshly built kit radio arrives with the channel's archive already populated. *(Exact endpoint/auth to be confirmed against the AUS instance's API — isolate in `corescope.py` so it's a one-file adaptation if the API shifts.)*

### Theme detection

Proposal: adopt a lightweight channel convention — the daily theme post starts with `Theme:` (case-insensitive), e.g. `Theme: songs about rain`. Parser rule: first `Theme:` message of the day (America/Chicago) creates the theme row; every link message attaches to the most recent theme. Fallback when no theme is posted: auto-create `Untitled — <date>`. This costs the channel nothing (it matches how a human would post anyway) and makes parsing deterministic instead of vibes-based.

---

## 7. Playback pipeline

**Cache-first.** On `track.discovered`, the cacher runs `yt-dlp -f bestaudio -x --audio-format opus` into `/var/lib/meshradio/cache/<video_id>.opus` (~3–5MB/track). The player only ever plays local files. Benefits: archive replay never re-hits YouTube, playback survives net hiccups, and a yt-dlp breakage delays *new* tracks without touching the archive. Cache is LRU-pruned at a configurable cap (default 8GB ≈ 1,600+ tracks — realistically, never prunes).

**Fallback ladder** when a track can't be fetched:

1. Cached file (normal path)
2. Fresh yt-dlp extract retry (with backoff; auto-`pip install -U yt-dlp` as a nightly job, since upstream fixes breakages within days)
3. **Metadata-only mode**: resolve title/artist via YouTube's oEmbed endpoint (no API key needed), display the track on OLED/web with a "couldn't fetch audio" badge — the channel history stays intact and browsable even when playback can't happen
4. *(Optional, config-off by default)*: play the 30s preview from the iTunes Search API as an audible placeholder

**Live mode policy:** a new track never interrupts the current one. If the radio is idle in Live mode, a new arrival auto-plays (with a brief OLED toast: sender + title). If something's playing, it enqueues. Configurable quiet hours suppress auto-play.

**mpv** via `python-mpv` handles decode/output — battle-tested, gapless, and it outputs to whatever PipeWire sink is current, so output switching requires zero player logic.

---

## 8. Audio routing & Bluetooth

PipeWire (Bookworm default) with three sinks: I2S amp, headphone jack, BT device. `audio/routing.py` is a thin `wpctl` wrapper exposing `set_output(sink)` + current-state events on the bus. Bluetooth pairing is a small state machine over BlueZ D-Bus: OLED menu → "Pair speaker" → scan → select → PipeWire picks up the A2DP sink → auto-route. Paired devices persist; reconnection auto-routes if enabled.

---

## 9. Interfaces

### OLED + controls (panel)

Five screens, encoder-navigated: **Now Playing** (theme / title–artist marquee / sender / battery / output icon), **Queue**, **Archive** (scroll days → themes → tracks, push to replay a whole day), **Outputs**, **Status** (mesh RSSI, WiFi, CoreScope last-poll, cache stats, IP).

### Web UI

FastAPI serving Jinja2 + htmx at `http://meshradio.local` (avahi mDNS). WebSocket pushes player state. Pages: Now Playing (with album art fetched via oEmbed thumbnail — the one place the web UI beats the OLED), Archive browser, queue management, output/volume, settings (WiFi, channel key, quiet hours, CoreScope URL), and a log viewer. htmx keeps the frontend a set of HTML templates — no npm, no build step, which is a kit-maintainability feature, not a limitation.

---

## 10. Kit provisioning & first-boot UX

- **Flashable image** built with `pi-gen` (or `sdm`) in CI: OS + dependencies + app preinstalled. Builder flashes, boots, done.
- **First boot:** no known WiFi → `provision.py` brings up a `MeshRadio-Setup` AP via NetworkManager (`nmcli`) with a captive-portal page: pick WiFi, paste `#music` channel key, optionally set CoreScope URL. Reboot into service.
- **Updates:** OLED/web "Update" button = `git pull` + `pip install -e .` + restart. Nightly `yt-dlp` self-update as a systemd timer.
- **Repo deliverables:** source, STLs (`/hardware/stl`), wiring diagram (`/hardware/wiring.svg` — everything is header/screw-terminal), BOM with live links, assembly guide with photos, channel-convention doc, image-build workflow.

---

## 11. Why these choices (maintainability ledger)

- **Python**: meshcore client, yt-dlp, mpv bindings, luma.oled, gpiozero — every hardware and media dependency is Python-first. Any other language means writing at least one of these yourself.
- **Monolith + event bus**: one unit to deploy, debug, and reason about; modules stay decoupled through the bus, so ripping out CoreScope or adding a Spotify resolver later is additive.
- **SQLite**: the archive *is* the product; a single file that survives reflashes (kept on a separate data partition) and can be copied off as a channel history export.
- **htmx over React**: the web UI must still build in five years on a Pi with no internet toolchain.
- **Cache-first playback**: converts yt-dlp's known fragility from "radio is broken" into "newest song is delayed."
- **Everything through PipeWire**: output switching, BT, and volume are OS problems, not app problems.

## 12. Assumptions to confirm

1. Theme convention (`Theme:` prefix) is acceptable to propose to the channel.
2. Live mode = auto-play when idle, enqueue when busy, never interrupt.
3. AUS CoreScope exposes a pollable API for channel messages (adapter isolated regardless).
4. Mono 3W built-in speaker is an acceptable "decent"; stereo would double amp/driver cost and complicate the enclosure for marginal gain at this size.
5. Data partition separate from OS partition so reflashing the image preserves the archive/cache.
