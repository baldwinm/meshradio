# MeshRadio — Architecture Document

*A standalone internet radio that plays the Austin MeshCore `#music` channel.*
*Status: v0.1 — the core software is built, tested (119 tests), and running:
ingest, cache-first player, browser web player, YouTube-Mix radio mode, and a
public embed-mode deployment fed by a home-node relay (§14). The hardware kit
(§2) remains design-locked and not yet built; module status is tracked in the
[README](README.md). This document is the full design; sections marked below
note where the implementation has diverged from or gone beyond the original plan.*

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

A **Lite variant** on the Zero 2 W — no mesh node, CoreScope-only ingestion — is fully specified in §13; the software must not assume Pi 4-only.

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

*As-built layout (the design above held; `net.py`, `runtime.py`,
`ingest/relay.py`, `media/radio.py` were added, and `web/` grew a router split —
see §14):*

```
meshradio/
├── app.py              # asyncio entrypoint, wires modules to the bus
├── bus.py              # tiny pub/sub EventBus (asyncio queues)
├── config.py           # TOML over dataclass defaults; secrets from env
├── db.py               # aiosqlite layer + migrations
├── net.py              # shared outbound HTTP client (User-Agent, timeouts)
├── runtime.py          # supervised task/Service runtime — restart-with-backoff
├── ingest/
│   ├── parse.py        # link extraction, theme detection  ← pure functions, unit-tested
│   ├── service.py      # message → theme/track rows, dedupe (the ingest core)
│   ├── mesh.py         # meshcore serial client, #music subscription
│   ├── corescope.py    # CoreScope poller (fallback + backfill)
│   └── relay.py        # push local channel history to a hosted instance (§14)
├── media/
│   ├── cacher.py       # yt-dlp download-to-cache worker (self-healing retries)
│   ├── player.py       # backends: mpv | web | embed | null; queue + live policy
│   ├── radio.py        # YouTube-Mix "station" continuations (radio mode)
│   └── metadata.py     # oEmbed / fallback metadata resolution
├── audio/
│   ├── routing.py      # PipeWire sink selection (wpctl), per-profile
│   └── bluetooth.py    # BlueZ pairing/connection state machine
├── ui/
│   └── panel.py        # OLED screens (luma.oled) + encoder/buttons (gpiozero); log panel on dev
├── web/                # FastAPI + Jinja2 + htmx + WebSocket (split into routers)
│   ├── server.py       # create_app: assembly, lifespan, session middleware
│   ├── context.py      # WebContext — shared state on app.state
│   ├── sessions.py     # per-visitor session players + speaker registry (embed)
│   ├── routes_pages.py # HTML pages + htmx partials
│   ├── routes_api.py   # player/queue control API
│   ├── routes_ingest.py# /audio streaming, relay /api/ingest, /healthz
│   ├── ws.py           # WebSocket: forwards bus events → htmx re-fetch
│   ├── templates/      # Jinja2 + htmx — no JS build chain, ever
│   └── static/         # vendored htmx + js/ (embed, eq, playbar, radio), style.css
├── system/
│   ├── power.py        # fuel gauge polling, safe shutdown
│   └── provision.py    # first-boot AP-mode WiFi setup (nmcli)
tests/                  # top-level; 119 tests, pytest-asyncio
```

**Key dependency choices** (all boring on purpose): `meshcore`, `yt-dlp`, `python-mpv`, `FastAPI`+`uvicorn`, `httpx`, `htmx` (vendored single JS file), `luma.oled`, `gpiozero`, `aiosqlite`. No Redis, no Docker, no Node.

**Supervised runtime.** Every long-lived loop runs under `runtime.supervise()` (or the `Service` base class): an unhandled exception is logged loudly and the loop restarts with backoff (1s → 5s → 30s) instead of dying silently. One-shot background work goes through `spawn()`, which guarantees a logged traceback. This exists because a silently-dead cacher task once shipped to production; the runtime makes that failure class impossible by construction. systemd still restarts the whole process on a hard crash (§4 "one process").

### Event flow

The full topic vocabulary lives in `bus.py` as constants. Core flow:
`ingest` (via `ingest/service.py`) publishes **`track.discovered`** → `cacher`
downloads audio in the background and publishes **`track.ready`** (or
**`track.failed`** for metadata-only tracks) → `player` enqueues per live-mode
policy → `panel` and `web` subscribe to **`player.state`** and render. Themes
announce on **`theme.created`**; routing, power, and ingest health emit
**`output.changed`**, **`power.state`**, and **`ingest.status`** (the last also
feeds `/healthz`). Every module is a subscriber/publisher on the bus and touches
the DB through `db.py` only. The web WebSocket forwards bus payloads verbatim
(they're plain dicts), and the page reacts by re-fetching htmx partials.

---

## 5. Data model

```sql
themes(  id, date, title, set_by, raw_message, created_at )
tracks(  id, video_id, url, title, artist, duration,
         theme_id → themes, sender, mesh_ts, ingested_at,
         source TEXT CHECK(source IN ('mesh','corescope','radio')),
         cache_path, cache_status,          -- pending|ready|failed
         dedupe_hash UNIQUE )
plays(   id, track_id → tracks, played_at, output, completed )
settings(    key, value )
web_sessions(sid, updated_at, state )       -- per-visitor snapshot, JSON
```

`dedupe_hash = sha256("channel|sender|video_id|mesh_ts_bucketed_to_60s")` — this is what lets mesh and CoreScope ingestion coexist without double-entry: whichever path delivers the message first wins, the other no-ops on the UNIQUE constraint (an `ON CONFLICT … DO NOTHING` insert).

Schema is applied through a **versioned migration list** in `db.py`, run in order at connect and recorded via `PRAGMA user_version`. Two migrations landed after the initial design:

- **`radio` track source** — YouTube-Mix continuations queued by radio mode (§7). They keep `theme_id` NULL so they never appear in the channel archive. SQLite can't `ALTER` a `CHECK`, so the migration rebuilds the `tracks` table.
- **`web_sessions`** — per-visitor player snapshots (queue, position, day) for embed hosting (§14), so a visitor's session survives an ephemeral-host redeploy. Keyed by the session cookie.

---

## 6. Ingestion

### Mesh path (primary)

`meshcore` client on the Heltec serial port, subscribed to `#music` (channel key in config). On each message: run `parse.extract_links()` (matches `music.youtube.com`, `youtube.com/watch`, `youtu.be`), normalize to a canonical video ID, attach sender + timestamp, insert.

### CoreScope path (fallback + backfill)

Poll the AUS CoreScope instance every 2–5 min for `#music` channel packets; same parser, same dedupe. Serves two jobs: catching messages the local node missed (RF is RF), and **backfilling history on first boot** so a freshly built kit radio arrives with the channel's archive already populated. *(Exact endpoint/auth to be confirmed against the AUS instance's API — isolate in `corescope.py` so it's a one-file adaptation if the API shifts.)*

### Theme detection

Proposal: adopt a lightweight channel convention — the daily theme post starts with `Theme:` (case-insensitive), e.g. `Theme: songs about rain`. Parser rule: the first `Theme:` message of the day (America/Chicago) creates the theme row and **locks** it; every link message attaches to that day's theme. Once locked, a later `Theme:` message is ignored — it can't reset the theme or split the day into a second playlist. Fallback when no theme is posted first: auto-create an unlocked `Untitled — <date>` placeholder; the day's first real `Theme:` message then adopts that placeholder in place (renaming it and locking it) so early links stay in the one playlist. This costs the channel nothing (it matches how a human would post anyway) and makes parsing deterministic instead of vibes-based.

---

## 7. Playback pipeline

**Cache-first.** On `track.discovered`, the cacher runs `yt-dlp -f bestaudio -x --audio-format opus` into `/var/lib/meshradio/cache/<video_id>.opus` (~3–5MB/track). The player only ever plays local files. Benefits: archive replay never re-hits YouTube, playback survives net hiccups, and a yt-dlp breakage delays *new* tracks without touching the archive. Cache is LRU-pruned at a configurable cap (default 8GB ≈ 1,600+ tracks — realistically, never prunes).

**Fallback ladder** when a track can't be fetched:

1. Cached file (normal path)
2. Fresh yt-dlp extract retry (with backoff; auto-`pip install -U yt-dlp` as a nightly job, since upstream fixes breakages within days)
3. **Metadata-only mode**: resolve title/artist via YouTube's oEmbed endpoint (no API key needed), display the track on OLED/web with a "couldn't fetch audio" badge — the channel history stays intact and browsable even when playback can't happen
4. *(Optional, config-off by default)*: play the 30s preview from the iTunes Search API as an audible placeholder

**Live mode policy:** a new track never interrupts the current one. If the radio is idle in Live mode, a new arrival auto-plays (with a brief OLED toast: sender + title). If something's playing, it enqueues. Only tracks posted within `live_window_s` (default 30 min) count as live; older ones are backfill and stay archive-only, so a first-boot history download doesn't stampede the queue. Configurable quiet hours suppress auto-play.

**Player backends** (selected by `player.backend`; `auto` picks by hardware profile):

| Backend | Speaker | Source | Use |
|---|---|---|---|
| `mpv` | Pi sinks (I2S / jack / BT) via `python-mpv` | local cache file | appliance (`pi4`/`lite`) |
| `web` | the browser that has the page open | server streams the cache file (`GET /audio/{id}`) | LAN / dev box |
| `embed` | each visitor's own browser, via the YouTube IFrame player | YouTube directly — **no download** | public hosting (§14) |
| `null` | — | — | `--demo` / tests |

**mpv** via `python-mpv` handles decode/output — battle-tested, gapless, and it outputs to whatever PipeWire sink is current, so output switching requires zero player logic. The `web` and `embed` backends emit the same `player.state` events; the browser is the output device instead of mpv.

**Radio mode (`media/radio.py`).** When the queue runs dry, the player can seed a "station" from the current (or last-played) track using its YouTube Mix: `radio.py` fetches similar tracks in batches (`radio_batch`), the cacher caches them, and they queue with `source='radio'` (theme_id NULL, so they never pollute the archive). The station keeps topping itself up until stopped. Channel posts always queue ahead of radio filler.

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

---

## 13. MeshRadio Lite — budget variant (no mesh node)

Same product, same codebase, roughly **40% of the cost**. The Lite drops the onboard Heltec V3 and ingests exclusively from the AUS CoreScope instance over WiFi. That single deletion unlocks the rest of the cost reduction: with no node competing for USB, the **Pi Zero 2 W's lone OTG port goes to the Bluetooth dongle**, which resolves the WiFi/BT coexistence problem that forced the Pi 4 in the full build (onboard BT stays disabled; the dongle handles A2DP).

### Hardware deltas

| Subsystem | Full kit | Lite |
|---|---|---|
| Compute | Pi 4 (2GB) | Pi Zero 2 W |
| Mesh ingestion | Heltec V3, USB serial | — (CoreScope poll only) |
| Bluetooth out | USB dongle on USB-A | USB dongle on OTG (adapter) |
| Built-in speaker | MAX98357A → 3" driver | MAX98357A → 2.5–3" driver (unchanged, 3W) |
| 3.5mm out | Pi 4 onboard jack | **PCM5102A DAC board** (Zero has no analog jack) |
| Display | 2.42" SSD1309 | 0.96" SSD1306 |
| Controls | Encoder + 2 buttons | Encoder + 1 button (Mode folds into long-press) |
| Power | UPS HAT + 4× 18650 | Wall-powered base; UPS HAT (C) + cell as optional add-on |

### The shared-I2S trick

The Zero 2 W has one I2S peripheral but the bus fans out fine: the MAX98357A (speaker) and PCM5102A (3.5mm) hang on the **same BCLK/LRCLK/DIN lines** and both receive the audio stream. Output "switching" between them is a GPIO on the MAX98357A's SD (shutdown) pin — speaker muted when Line Out is selected, unmuted otherwise. Both boards are through-hole header breakouts, keeping the no-SMD kit rule intact.

Software impact is confined to `audio/routing.py`: on the full build, speaker/jack/BT are three PipeWire sinks; on Lite, speaker and jack are one ALSA/PipeWire sink plus an amp-enable GPIO, and BT remains a separate sink. The routing module exposes the same `set_output()` interface either way — a `hardware_profile` key in settings selects the backend. Nothing above the routing layer knows the difference.

### Ingestion & behavior tradeoffs (be honest in the docs)

- **Latency:** live tracks arrive on the CoreScope poll cadence (2–5 min) instead of at RF speed. For a radio, this is nearly invisible — but it's not "watch the message land."
- **Dependency:** the Lite is down if the AUS CoreScope instance is down. The full build keeps working off RF.
- **Not a mesh client:** the Lite doesn't strengthen the mesh or work off-grid; it's a listener to the channel's reflection, not the channel. Worth a plain-language note in the kit docs so builders pick with eyes open.
- **Upgrade path:** add a Heltec V3 later via a powered micro-USB hub (or migrate the SD card to a Pi 4) — the `hardware_profile` setting and the disabled `ingest/mesh.py` module make this a config change, not a rebuild.

### Lite BOM (ballpark)

| Part | Est. |
|---|---|
| Pi Zero 2 W + SD card | $23 |
| USB BT 5.x dongle + OTG adapter | $10 |
| MAX98357A breakout | $6 |
| PCM5102A DAC board | $6 |
| 2.5–3" full-range driver | $8 |
| 0.96" SSD1306 OLED | $5 |
| Rotary encoder, button, wiring | $6 |
| 5V/2.5A wall supply | $8 |
| Filament, fasteners, misc | $5 |
| **Total (wall-powered)** | **~$77** |
| *Optional:* UPS HAT (C) + cell | *+$20 → ~$97, ~4–5h runtime* |

### What deliberately does *not* change

Everything above the hardware line: same image (both device trees baked in, `hardware_profile` chosen at first-boot setup), same web UI, same archive, same cache-first player, same STL design language (smaller shell, shared speaker-chamber geometry). One codebase, two SKUs.

---

## 14. Public hosting — embed mode & the relay *(added post-design)*

The original design assumed the radio *is* the appliance. In practice a third
deployment target emerged before the hardware: **a public web app anyone can
open in a browser** (live at [meshradio.onrender.com](https://meshradio.onrender.com)).
It runs the same codebase with two new pieces — an embed player backend and a
relay — plus per-visitor sessions. None of this touches the appliance path.

### The two problems a public host has

1. **It can't legally serve the audio.** A public server downloading and
   redistributing YouTube audio is a different thing from a private appliance
   caching for personal playback.
2. **It can't reach the sources.** Cloudflare (fronting CoreScope) and YouTube
   both challenge datacenter IPs, so a hosted instance can neither poll the
   channel nor run yt-dlp successfully.

### Embed mode solves (1)

With `player.backend = "embed"` the server ships **only metadata** — video ids,
titles, artists, themes, queue order — and each visitor's browser streams every
song straight from YouTube via the **IFrame player** (`static/js/embed.js`). The
server never downloads or serves audio; the cacher runs in metadata-only mode.
This keeps a public deployment clear of redistributing copyrighted media, and it
means normal YouTube ad rules apply in the browser (unlike cache-first playback).

### The relay solves (2)

A node with residential internet — the Pi at home, under systemd — polls the
channel normally, then **pushes** new messages to the hosted instance instead of
the host pulling:

```
  Home node (Pi, residential IP)                 Hosted instance (datacenter IP)
  ┌──────────────────────────────┐               ┌────────────────────────────┐
  │ corescope poll → ingest → DB  │               │  POST /api/ingest (token)  │
  │ relay.py: DB → messages ──────┼──HTTPS POST──►│  → same ingest pipeline    │
  │   (+ resolved track metadata) │   /api/ingest │  → SQLite → embed players  │
  └──────────────────────────────┘               └────────────────────────────┘
        residential internet                        can't poll CoreScope itself
```

`ingest/relay.py` reconstructs channel messages from the local DB (themes +
tracks, sorted by mesh time) and POSTs them — carrying the **track metadata it
already resolved**, since the datacenter-side host can't ask YouTube itself — to
the receiver's authenticated `POST /api/ingest` (`routes_ingest.py`). The
receiver funnels them through the *same* ingest pipeline, so its dedupe makes
re-pushes no-ops; the relay's cursor is an optimization, not a correctness
requirement. Auth is a shared bearer token (`MESHRADIO_INGEST_TOKEN` on the host,
`[relay].token` on the Pi), compared with `secrets.compare_digest`.

**Self-healing against ephemeral disks.** Free-tier hosting wipes its disk on
every deploy and spin-down. Each push reports the receiver's track count; when it
drops below the home node's, the pusher resets its cursor and re-backfills the
whole channel automatically. An empty push still goes out as a heartbeat so a
wiped receiver is detected promptly.

### Per-visitor sessions

The appliance is one communal radio; a public host is not — nobody should be able
to pause or skip a stranger's music. In embed mode each browser gets its **own
session player** (`web/sessions.py`): a session cookie names it, `app.py` supplies
a `player_factory`, and each session player has its own queue/position/day while
still hearing the shared `track.ready` stream. Snapshots persist to the
`web_sessions` table (§5) so a session survives a redeploy. On the appliance/LAN
path there's no factory — it stays the single shared player with the
"one-speaker-at-a-time" speaker registry.

### Deployment & operations

- **Render blueprint** ([render.yaml](render.yaml)) — embed mode via
  [meshradio.render.toml](meshradio.render.toml); Python 3.11 to match CI;
  `MESHRADIO_INGEST_TOKEN` generated as a secret.
- **CI gate** ([.github/workflows/test.yml](.github/workflows/test.yml)) — Render
  deploys `main` only after the test suite is green (`autoDeployTrigger:
  checksPass`), so a red suite never reaches production.
- **`/healthz`** — liveness plus ingest freshness (`ingest_age_s`, track count,
  session count); Render's health check hits it, and a stale age means the relay
  stopped pushing.
- **The Pi relay** runs under systemd ([deploy/meshradio.service](deploy/meshradio.service)):
  `Restart=on-failure` rides out transient network/CoreScope hiccups.

### What this validates about the original design

The monolith-plus-event-bus decision (§4) paid off here: embed mode is one new
`PlayerBackend`, the relay is one new bus-agnostic `Service`, per-visitor players
are the *same* `PlayerService` instantiated per session, and the receiver reuses
the *same* ingest pipeline. A public multi-tenant deployment the design never
anticipated slotted in without touching the appliance code paths.
