# MeshRadio — Architecture Document

*A standalone internet radio that plays the Austin MeshCore `#music` channel.*
*Status: v0.1 — the core software is built, tested (453 tests), and running:
ingest, cache-first player, browser web player, YouTube-Mix radio mode, a
browsable archive site (calendar, themes, search, stats, member and artist pages, weekly recap, feeds),
a signed-in admin page (§9), and a public embed-mode deployment fed by a home-node relay (§14). The hardware
kit (§2) remains design-locked and not yet built; module status is tracked in
the [README](README.md). This document is the full design; sections marked below
note where the implementation has diverged from or gone beyond the original plan
— and where a described piece is still only a plan (§7 iTunes preview, §9
settings editing and log viewer, §10 first-boot flow).*

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

*As-built layout (the design above held; `backup.py`, `net.py`, `runtime.py`,
`ingest/relay.py`, `media/radio.py` were added, `web/` grew a router split —
see §14 — and the three largest modules were later split by concern: `db.py`
into a package behind the same `Database` facade, the command line out of
`app.py` into `cli.py`, the playback engines out of `player.py`):*

```
meshradio/
├── app.py              # asyncio entrypoint (run), wires modules to the bus
├── cli.py              # the `meshradio` command: start, or one maintenance task and exit
├── bus.py              # tiny pub/sub EventBus (asyncio queues, plus synchronous listeners)
├── config.py           # TOML over dataclass defaults, checked at load; secrets from env
├── db/                 # aiosqlite layer behind one Database facade
│   ├── core.py         #   connection, the write transaction, migration runner, settings
│   ├── migrations.py   #   the versioned schema scripts
│   ├── fields.py       #   bounds on free text and lengths, applied where rows are written
│   ├── themes.py       #   query mixins, one per concern: themes, tracks (+ plays),
│   ├── tracks.py       #   the archive's read side (calendar, search, stats, members),
│   ├── archive.py      #   the relay's cursors, visitor session snapshots,
│   ├── browse.py       #   artists and weeks
│   ├── relay.py
│   └── web_sessions.py
├── backup.py           # rotating DB snapshots; --list-backups / --restore-backup (§14)
├── net.py              # shared outbound HTTP client (User-Agent, timeouts)
├── runtime.py          # supervised task/Service runtime — restart-with-backoff
├── ingest/
│   ├── parse.py        # link extraction, theme detection  ← pure functions, unit-tested
│   ├── service.py      # message → theme/track rows, dedupe (the ingest core)
│   ├── mesh.py         # meshcore serial client, #music subscription
│   ├── corescope.py    # CoreScope poller (fallback + backfill; also the backup feed)
│   └── relay.py        # push local channel history to a hosted instance (§14)
├── media/
│   ├── cacher.py       # yt-dlp download-to-cache worker (self-healing retries)
│   ├── backends.py     # engines behind one protocol: mpv | web | embed | null
│   ├── player.py       # queue + live policy over a backend
│   ├── radio.py        # YouTube-Mix "station" continuations (radio mode)
│   └── metadata.py     # oEmbed / fallback metadata resolution
├── audio/
│   ├── routing.py      # PipeWire sink selection (wpctl), per-profile
│   └── bluetooth.py    # BlueZ pairing/connection state machine
├── ui/
│   └── panel.py        # OLED screens (luma.oled) + encoder/buttons (gpiozero); log panel on dev
├── web/                # FastAPI + Jinja2 + htmx + WebSocket (split into routers)
│   ├── server.py       # create_app: assembly, lifespan, session middleware, origin guard, security headers
│   ├── context.py      # WebContext — shared state on app.state; short-TTL whole-archive caches
│   ├── sessions.py     # per-visitor session players + speaker registry (embed)
│   ├── routes_pages.py # HTML pages (now playing, archive, search, stats, member, artist, week, about, feeds) + htmx partials
│   ├── routes_api.py   # player/queue control API
│   ├── routes_ingest.py# /audio streaming, relay /api/ingest, /healthz
│   ├── ws.py           # WebSocket: forwards bus events → htmx re-fetch
│   ├── feed.py         # /feed.xml Atom builder — pure functions over db rows
│   ├── recap.py        # weekly recap summary + /weekly.xml builder — pure, like feed.py
│   ├── templates/      # Jinja2 + htmx — no JS build chain, ever
│   └── static/         # vendored htmx, style.css, icons, js/ (radio, embed, eq, playbar, queue,
│                       #   keys, mediasession, nav, help, skin, fx)
├── system/
│   ├── power.py        # fuel gauge polling, safe shutdown
│   └── provision.py    # first-boot AP-mode WiFi setup (nmcli)
tests/                  # top-level; pytest-asyncio, shared builders in tests/helpers.py
```

**Key dependency choices** (all boring on purpose): `meshcore`, `yt-dlp`, `python-mpv`, `FastAPI`+`uvicorn`, `httpx`, `htmx` (vendored single JS file), `luma.oled`, `gpiozero`, `aiosqlite`. No Redis, no Docker, no Node. Only the web/ingest core is a hard dependency: yt-dlp and python-mpv sit behind the `media` extra and the Pi hardware libraries (`meshcore`, `luma.oled`, `gpiozero`) behind `hw`, so a public embed host or a dev box installs neither. yt-dlp's YouTube extractor also needs a JavaScript runtime (deno) on the machine to solve YouTube's challenge.

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
the DB through the `db` package only. The web WebSocket forwards bus payloads verbatim
(they're plain dicts), and the page reacts by re-fetching htmx partials — on Now
Playing, a single `/partials/live` request that swaps the player bar, queue and
day nav together as out-of-band regions, rather than one request per region.

---

## 5. Data model

```sql
themes(  id, date, title, set_by, raw_message, created_at, locked,
         updated_at )                        -- set when a placeholder is adopted
tracks(  id, video_id, url, title, artist, duration,
         theme_id → themes, sender, mesh_ts, ingested_at,
         source TEXT CHECK(source IN ('mesh','corescope','radio',
                                      'letsmesh','comchan')),
         cache_path, cache_status,          -- pending|ready|failed
         dedupe_hash UNIQUE,
         meta_edited_at )                   -- title/artist pinned by hand (admin)
plays(   id, track_id → tracks, played_at, output, completed )
settings(    key, value )
web_sessions(sid, updated_at, state )       -- per-visitor snapshot, JSON
deleted_tracks(id, date, video_id, title, sender, deleted_at,
               track_json,                   -- the removed row, so it can be put back
               UNIQUE(date, video_id))       -- tombstones for operator removals
admin_log(   id, at, actor, ip, action, target, before, after,
             undo, undone_by )               -- every admin sign-in and change
admin_sessions(token_hash, created_at, seen_at, ip, key_fp)
artist_aliases(alias COLLATE NOCASE PRIMARY KEY, canonical, created_at)
```

The whole archive sits behind **one aiosqlite connection** in WAL mode, shared by every coroutine. Each `await` inside a write is a point where another coroutine's statements run on that connection, so the driver's implicit transactions let one task's rollback discard another's uncommitted update (an `add_track` dedupe rollback once un-readied a track the cacher had just finished). The connection therefore runs in autocommit mode and every write goes through `Database.transaction()`: writers are serialised behind a lock, `BEGIN IMMEDIATE`/`COMMIT` are explicit, a nested call from the same task joins the open transaction, and reads never wait. Batch writers (a CoreScope poll, a relay push) wrap 500 messages per transaction instead of paying one commit per row.

Three dedupe rules apply. `dedupe_hash = sha256("channel|sender|video_id|mesh_ts_bucketed_to_60s")` lets mesh and CoreScope ingestion coexist without double-entry: whichever path delivers the *same message* first wins, the other no-ops on the UNIQUE constraint (an `ON CONFLICT … DO NOTHING` insert). Separately, a **partial unique index on `(theme_id, video_id)`** enforces *one song per day's playlist* — a repost of a video already under a theme is refused, so a song never shows up twice no matter who reposts it. Radio filler (`theme_id` NULL) is exempt; the index's partial `WHERE theme_id IS NOT NULL` lets a Mix echo the same video across days. Third, a **tombstone** in `deleted_tracks` keyed on `(date, video_id)`: when the operator removes a song by hand (`--delete-track`) the link is still on the channel, so any re-backfill — the relay's self-healing one, a restore, a cursor reset — would insert it straight back. `add_track` consults the tombstone and ignores the replay, the same way a locked theme ignores a corrected repost. It is scoped to that one day, so the song can be shared again on another.

Schema is applied through a **versioned migration list** in `db/migrations.py`, run in order at connect and recorded via `PRAGMA user_version`. Migrations that landed after the initial design:

- **`radio` / `letsmesh` / `comchan` track sources** — Mix continuations (§7), a since-retired backup analyzer feed, and the backup feed that replaced it (both §6). SQLite can't `ALTER` a `CHECK`, so each rebuilds the `tracks` table; `'letsmesh'` stays in the constraint because existing rows may carry it, even though nothing writes it now. A rebuild drops the table's indexes with it, so by the `comchan` migration all five `idx_tracks_*` indexes have to be recreated, not just the two the original schema had — and any future rebuild must also recreate the `sender` index added below.
- **`web_sessions`** — per-visitor player snapshots (queue, position, day) for embed hosting (§14), so a visitor's session survives a redeploy (which restarts the process). Keyed by the session cookie.
- **Lockable themes** — a `locked` flag plus a backfill that merges same-day rival themes into one playlist, so a stray second "Theme:" post can't split a day.
- **One-song-per-playlist** — collapses any duplicate `(theme_id, video_id)` rows that predate the rule (keeping the earliest, repointing plays) and adds the partial unique index above.
- **Query indexes** — `tracks(video_id)` (the cacher's reuse-an-existing-file check ran a full scan per download), `tracks(ingested_at)` (the relay's cursor predicate, hit every push interval), and `tracks(sender COLLATE NOCASE)` (the member pages look a name up case-insensitively four times a visit). The last is declared with the query's own collation, or the planner wouldn't use it.
- **`themes.updated_at`** — stamped when a placeholder is adopted, so the rename reaches the relay receiver (§14).
- **`deleted_tracks`** — the tombstone table above.
- **The admin page** (v14) — `admin_log`, `admin_sessions` and `artist_aliases`, plus `tracks.meta_edited_at` and `deleted_tracks.track_json` (§9, *The admin page*). Both columns are `ALTER TABLE … ADD COLUMN`, so no rebuild.

Rotating snapshots of the whole DB (`backup.py`, §14) provide a rollback point for a bad migration or corruption, separate from disk durability.

---

## 6. Ingestion

### Mesh path (primary)

`meshcore` client on the Heltec serial port, subscribed to `#music` (channel key in config). On each message: run `parse.extract_links()` (matches `music.youtube.com`, `youtube.com/watch`, `youtu.be`), normalize to a canonical video ID, attach sender + timestamp, insert.

### CoreScope path (fallback + backfill)

Poll the AUS CoreScope instance every 3 min (`poll_interval_s`) for `#music` channel packets; same parser, same dedupe. Serves two jobs: catching messages the local node missed (RF is RF), and **backfilling history on first boot** so a freshly built kit radio arrives with the channel's archive already populated.

*As built:* the adapter targets CoreScope's real API (verified against a live instance in July 2026): `GET /api/channels/{hash}/messages`, with the URL-encoded channel name (`#music` → `%23music`) as the hash, returning each message's `sender`, `text`, `sender_timestamp` (the mesh-side send time, the same value the local node sees — which is what makes cross-source dedupe line up) and `first_seen`. No auth is needed. There is **no `since` parameter**, but there is paging: `limit` (default 100, clamped to the instance's `channelMessagesMax`, 500 unless the operator changed it) and `offset`, counted back from the newest post, each page emitted oldest-first. A request that sends neither gets only the newest hundred — enough between polls, but no backfill and no recovery from a long outage — so the poller asks for full pages from the end of the channel and walks back until a page holds nothing newer than its cursor (`first_seen`, kept in `settings`) or it runs off the start. The first boot has no cursor and walks the whole channel: that is the backfill. The walk advances by what each page actually held (an instance clamped lower just takes more pages), stops on a build that ignores `offset` (the same page twice), and spends one 64 MiB byte budget across all its pages, so an analyzer that misbehaves can't grow the process's memory. Cursor ties, late RF duplicates and the one-post overlap an arrival mid-walk shifts in all fall through to the dedupe hash, which makes reprocessing a no-op. Pages are sorted by mesh time before ingest so a theme lands before the links posted after it, and committed 500 per transaction. A post whose radio sent no clock (`sender_timestamp` is omitted) is dated by the analyzer's first sighting rather than dropped. Everything API-specific stays in `corescope.py`, so it remains a one-file adaptation if the API shifts; `meshradio --probe-feed` fetches one page from each configured feed and prints the fields and newest posts it got, the check to run from the machine that will poll when a host is new or ingestion goes quiet.

**Retired backup feed (LetsMesh analyzer).** A second poll instance once ran against the LetsMesh MeshCore analyzer (`analyzer.letsmesh.net`) — a CoreScope-family API — as a fallback for an AUS CoreScope outage. That host retired the endpoint and moved its API behind a Cloudflare challenge a headless poller can't clear, so the feed was dropped rather than repointed. `CoreScopePoller` keeps its generic `name`/`source` parameters, so adding another CoreScope-compatible feed later is still a one-liner in `app.py`; dedupe on channel+sender+video+minute (not source) makes any two overlapping feeds no-op each other.

**Current backup feed (`analyzer.comchan.net`).** A Digitaino CoreScope outage took ingestion down with it, so that generic mechanism is now in use: the `[comchan]` block wires a second `CoreScopePoller` under `name`/`source` `comchan`. It is **not** a failover — both feeds poll continuously and dedupe swallows the overlap, which is why neither needs health tracking to decide who is in charge, and why an outage on either side costs only the other's poll interval. Two details differ from the primary: `base_url` carries a real default in `ComchanConfig` (an appliance config written by `system/provision.py` has no `[comchan]` section, and a backup nobody configured is no backup), and its tracks carry their own `source`, so it stays queryable which analyzer covered a day — and a repeat of the LetsMesh retirement is one query to find. `/healthz` counts a successful poll from *any* feed as ingest freshness; scoping it to the primary would have reported "every ingest source stopped" during exactly the outage the backup exists for. The host runs the ComchanNet fork of CoreScope, whose channel endpoint is upstream's unchanged (confirmed from both sources in 2026-10, since the host isn't reachable from every network); the poller's paging and timestamp fallback above were written against that code, and `--probe-feed comchan` is the live check.

### Theme detection

Original proposal: adopt a lightweight channel convention — the daily theme post starts with `Theme:` (case-insensitive), e.g. `Theme: songs about rain`. *As built, the parser is looser than the proposal, because it matches how the channel actually posts:* the word "theme", up to ~40 characters of filler (`Today's theme is: …`, `theme for today: …`), then a colon and the title, on one line. The colon has to be a real delimiter — an emoticon (`:-)`), a URL scheme, or a clock time (`8:30`) is punctuation, so a message with no genuine colon declares nothing and the day keeps its placeholder rather than locking on a garbled title (the day of 2026-07-30 once locked as `-) or trains? …`, and a locked theme can only be fixed out of band with `--set-theme`). Parser rule: the first `Theme:` message of the day (America/Chicago) creates the theme row and **locks** it; every link message attaches to that day's theme. Once locked, a later `Theme:` message is ignored — it can't reset the theme or split the day into a second playlist. Fallback when no theme is posted first: auto-create an unlocked `Untitled — <date>` placeholder; the day's first real `Theme:` message then adopts that placeholder in place (renaming it and locking it) so early links stay in the one playlist. This costs the channel nothing (it matches how a human would post anyway) and makes parsing deterministic instead of vibes-based.

---

## 7. Playback pipeline

**Cache-first.** On `track.discovered`, the cacher runs `yt-dlp -f bestaudio -x --audio-format opus` into `/var/lib/meshradio/cache/<video_id>.opus` (~3–5MB/track). The player only ever plays local files. Benefits: archive replay never re-hits YouTube, playback survives net hiccups, and a yt-dlp breakage delays *new* tracks without touching the archive. Cache is LRU-pruned at a configurable cap (default 8GB ≈ 1,600+ tracks — realistically, never prunes); the order comes from `tracks.last_played_at`, stamped on every play (migration v13), so the pruner walks an index a batch at a time rather than aggregating `plays` — which returned every cached row, 416 ms at 50k tracks, each time a download finished over the cap. The cacher works **a few tracks at once** (`[cache] concurrency`, default 2), each in its own task behind a semaphore, because one download yt-dlp sat on for five minutes used to hold up every track behind it. A track is never worked twice at once even though a sweep of `pending` rows (at least once a minute) and the `track.discovered` stream can both hand it over; the sweep is also what recovers events dropped under a backfill burst. A single shared HTTP client serves every oEmbed lookup while the cacher runs, since a fresh TLS handshake per video was most of each lookup's time.

**Fallback ladder** when a track can't be fetched:

1. Cached file (normal path)
2. Fresh yt-dlp extract retry (`max_retries` attempts, backoff growing with each, and a 5-minute timeout after which the subprocess is killed). The nightly yt-dlp upgrade the design calls for — upstream fixes breakages within days — is a systemd timer ([deploy/meshradio-ytdlp-update.timer](deploy/meshradio-ytdlp-update.timer)) running `deploy/update-ytdlp.sh` against the venv; no restart is needed, since yt-dlp is a subprocess, and `/healthz` reports the version in play so the timer's work is visible.
3. **Metadata-only mode**: resolve title/artist via YouTube's oEmbed endpoint (no API key needed), display the track on OLED/web with a "couldn't fetch audio" badge — the channel history stays intact and browsable even when playback can't happen
4. *(Optional, config-off by default; **not built**)*: play the 30s preview from the iTunes Search API as an audible placeholder

**Live mode policy:** a new track never interrupts the current one. If the radio is idle in Live mode, a new arrival auto-plays (with a brief OLED toast: sender + title). If something's playing, it enqueues. Only tracks posted within `live_window_s` (default 30 min) count as live; older ones are backfill and stay archive-only, so a first-boot history download doesn't stampede the queue. Configurable quiet hours suppress auto-play.

**Player backends** (selected by `player.backend`; `auto` picks by hardware profile):

| Backend | Speaker | Source | Use |
|---|---|---|---|
| `mpv` | Pi sinks (I2S / jack / BT) via `python-mpv` | local cache file | appliance (`pi4`/`lite`) |
| `web` | the browser that has the page open | server streams the cache file (`GET /audio/{id}`) | LAN / dev box |
| `embed` | each visitor's own browser, via the YouTube IFrame player | YouTube directly — **no download** | public hosting (§14) |
| `null` | — | — | `--demo` / tests |

**mpv** via `python-mpv` handles decode/output — battle-tested, gapless, and it outputs to whatever PipeWire sink is current, so output switching requires zero player logic. The `web` and `embed` backends emit the same `player.state` events; the browser is the output device instead of mpv.

**Stations — filling the queue when the day runs out.** Two of them, mutually exclusive (`PlayerService.station` is `None | "radio" | "archive"`):

- **Radio (`media/radio.py`).** Seeds a "station" from the current (or last-played) track using its YouTube Mix: `radio.py` fetches similar tracks in batches (`radio_batch`), the cacher caches them, and they queue with `source='radio'` (theme_id NULL, so they never pollute the archive). Needs yt-dlp and a residential IP, so it's an appliance/LAN feature — omitted in embed mode.
- **Archive.** Replays random channel history (`Database.random_channel_tracks`, `ORDER BY RANDOM()`) as filler, tagged with a per-queue-entry `filler` flag rather than a distinct source (the rows are ordinary channel tracks). It's pure local SQL — no fetch, no cacher round trip, refilled synchronously right inside `_advance` — so it's the station that works on the **public embed host**, where a finished day otherwise dropped every visitor into silence. This is the default "Keep playing" on that deployment.

Either way the station keeps topping itself up until stopped, and channel posts always queue ahead of station filler (`_is_filler`).

---

## 8. Audio routing & Bluetooth

PipeWire (Bookworm default) with three sinks: I2S amp, headphone jack, BT device. `audio/routing.py` is a thin `wpctl` wrapper exposing `set_output(sink)` + current-state events on the bus. Bluetooth pairing is a small state machine over BlueZ D-Bus: OLED menu → "Pair speaker" → scan → select → PipeWire picks up the A2DP sink → auto-route. Paired devices persist; reconnection auto-routes if enabled.

---

## 9. Interfaces

### OLED + controls (panel)

Five screens, encoder-navigated: **Now Playing** (theme / title–artist marquee / sender / battery / output icon), **Queue**, **Archive** (scroll days → themes → tracks, push to replay a whole day), **Outputs**, **Status** (mesh RSSI, WiFi, CoreScope last-poll, cache stats, IP).

### Web UI

FastAPI serving Jinja2 + htmx at `http://meshradio.local` (avahi mDNS). WebSocket pushes player state. Pages: Now Playing (with album art fetched via oEmbed thumbnail — the one place the web UI beats the OLED), Archive browser, queue management, output/volume, settings (WiFi, channel key, quiet hours, CoreScope URL), and a log viewer. htmx keeps the frontend a set of HTML templates — no npm, no build step, which is a kit-maintainability feature, not a limitation.

*As built:* the pages are **Now Playing** (cover art is YouTube's still for the video, not an oEmbed lookup), the **Archive** (calendar and all-themes list, plus a page per day), **Search**, **Stats**, **Member** pages and **About**, with queue management, shuffle, volume and the stations on Now Playing. The **admin page** (below) covers the operator's fixes and shows the running config read-only; *editing* settings (WiFi, channel key, quiet hours, CoreScope URL) and the **log viewer** are *not built* — configuration is the TOML file, logs are `journalctl`. Output selection exists as an API (`/api/output/{name}`, `/api/outputs`) with no control on the page yet. The app listens on port 8080 (`[web] port`); a `meshradio.local` name comes from the OS's mDNS, not from anything the app sets up.

The **Archive** browser is a month calendar (`context.archive_months` and `calendar_month`): every day the channel played is a lit, tappable tile carrying its theme title and song count; quiet days render blank. It replaced a flat reverse-chronological list so the channel's rhythm — which days were busy, which went quiet — is visible at a glance.

The calendar answers "what happened on this day"; **`/archive/themes`** answers the other question members ask — "what have we done already?" It lists every theme the channel has run (`Database.all_themes`), newest first, a year at a time (the calendar's month paging applied to a list, so the page is a fixed size however long the channel runs), grouped into months, each linking to its day. A day page steps to the days either side of it and back to its month, so the archive reads straight through instead of via the calendar every time; a URL that isn't a date, or is a day the channel was quiet, is a 404 rather than a page titled with whatever was typed. `Untitled — <date>` placeholders are filtered out: nobody chose them, and their days are still on the calendar. `context.theme_history` counts how many days share a title (matched case- and space-insensitively) so a repeat carries an `N×` badge — the cheapest way to see whether "rain songs" has been done before. The route is declared *before* `/archive/{date}` so the path isn't taken for a date.

**Keyboard shortcuts** (`keys.js`) drive the *existing* controls rather than talking to the server themselves: play/pause and next click the htmx buttons, volume and mute call playbar.js, and seeking goes through the scrub bar, which already knows this tab's real position. A shortcut therefore can't disagree with what's on screen, and the handler binds nothing up front — the controls live in a swapped partial, so each press looks them up fresh. Keys are ignored while a text field has focus, and Space is left to the browser when a button, link or `<summary>` is focused, so it can't both activate the control and toggle playback. `K` is an alias for play/pause that isn't an activation key, so it works even with a button focused.

**Lock-screen controls** (`mediasession.js`). The speaker tab tells the OS what's playing — title, artist, the video's still and, once known, the duration — through the Media Session API, and the OS hands back play, pause, skip and scrub presses. The handlers POST to the same endpoints the buttons do instead of clicking them the way `keys.js` does: the buttons exist only on Now Playing, while the hx-boost nav keeps the music going across every page, so a lock-screen "pause" that went looking for a button would silently do nothing on the Archive. The server stays the source of truth and pushes the result to every tab, so the OS and the page can't disagree. The script listens for `meshradio:state` rather than being called from `applyState`, so a fault in a cosmetic feature can't break playback, and it dedupes on the state *object*: that event also fires for power and output pushes, where re-sending the stale position would yank the lock-screen scrubber backwards. Only the speaker tab owns the session (a silent remote claiming "playing" would fight it for the lock screen), and the mpv backend has no browser audio to describe. OS "play" while the server already says playing resumes this tab's own audio instead of toggling — that is the autoplay-blocked case, and a toggle would pause everyone. Seeking shares `seekTo()` with the scrub bar. *Checked in desktop Chromium against the embed and web backends; how a real lock screen renders varies by OS and browser, and in embed mode the audio lives in YouTube's cross-origin iframe.*

**The listening record.** `plays` is written on every track start and stays largely internal — the cache pruner's LRU ordering comes from it. `/stats` shows only the total (`play_totals`); per-song listening lists (recently played, most played, completion rates) were built and then deliberately pulled, so the stats page stays a picture of what the *channel* did rather than what one deployment's speakers did. The table still carries `completed`, so that view can come back if it's ever wanted.

**Search.** `/search?q=` is a case-insensitive substring match over a track's title, artist, sharer and theme title. It was four LIKE scans over the join; since migration v13 a query of three characters or more is answered by FTS5 external-content indexes over `tracks` and `themes` with the trigram tokenizer, which keeps exactly LIKE's semantics — any substring, any case — adds the Unicode case folding LIKE never had (`café` finds `CAFÉ`), and makes a search with no hits cost well under a millisecond at 50k tracks (a common word is bounded by the join over its thousands of hits). Triggers keep the indexes in step with inserts, metadata updates, renames and deletions, and the query is passed as one quoted phrase so every character in it is literal. A trigram index cannot see a query shorter than three characters, so those still take the LIKE path, with its wildcards escaped so `100%` searches for the literal text either way. **A row is a song, not a share.** The same video posted on eight days was eight rows, which spent the page's 100-row budget burying seven other songs; the query now groups by `video_id` and carries `shares` and `sharers` counts, with the track columns taken from the newest share — SQLite hands a bare column the row its single min/max aggregate picked, so the id a result offers to **+ queue** is the most recent copy and the day it links to is the day it last charted. **Ordering is by match, then recency.** Newest-first alone put an incidental hit on a theme title above the song the visitor named, so a `CASE` in the select list ranks an exact title above a title that starts with the query, above one that contains it, above an artist, above a row that matched only through its sharer or theme; ties go to the newest share. The tiers are LIKE, so they fold ASCII case only — `CAFÉ` still finds `café` through the index, it just doesn't win the exact-match tier. **`member` and `year` are filters, and a search in their own right:** "everything Ana shared in 2026" names no song, so the query runs whenever any of the three parameters is set, and with none of them set the answer is empty rather than the whole archive. The member filter is `sender = ? COLLATE NOCASE` (the `idx_tracks_sender` index a member page already uses) and the year is `substr(t.date,1,4)`; both stay in the URL, so a narrowed search is a link. The two dropdowns come from `search_filters()` — every member who has posted, grouped case-insensitively the way a member page resolves a name, and every year the archive covers — read through the aggregate cache, since every visit to the page draws them and they change only when a song or a theme lands, which drops that cache anyway. A `year` that isn't four digits is dropped rather than 404'd, so a stale link still returns the search it names. It asks for one more than the 100 it shows so the page can say the list is cut off instead of reporting the cap as the total; radio filler is excluded; a result offers **+ queue** only for a song that is playable. The path is `Disallow`ed in `robots.txt` — a query string is an unbounded space.

**Skins.** The header dropdown re-themes the whole UI as Winamp (default), iTunes or Media Player. The choice rides in a `skin` cookie that the server reads into `<html data-skin>`, so the first paint is already the right skin — a client-only switch would flash the default on every load. An unrecognised cookie value falls back to the default rather than being echoed into the page.

**Member pages.** `/member/<name>` gathers what the channel already knows about a sharer — their songs, the days they named (`themes.set_by`), the artists they repeat, the span they've been around. Names arrive as typed on the mesh, so lookups are `COLLATE NOCASE` and the page titles itself with the spelling that member uses most; the URL's spelling is never echoed. Radio filler carries the seed track's sender but nobody posted it, so `source != 'radio'` runs through every member query.

**Artist pages.** `/artist/<name>` is the member page turned around: an artist's songs (one row per video, most-shared first), who posts them, and the days they came up. The artist is oEmbed's `author_name`, which is the YouTube *channel*, and a YouTube Music share link resolves to an auto-generated `<Artist> - Topic` channel while a plain video link names the artist's own — so the suffix is folded away in one SQL expression (`db/browse.py`, `ARTIST_SQL`) and its Python twin (`artist_name`), and lookups are `COLLATE NOCASE`, so both spellings and any case reach one page. Other channel-name variants (`…VEVO`) are left alone: guessing at them would merge artists that aren't the same. Day, member, search and stats pages link artist names here. The queries live in their own mixin (`BrowseQueries`) rather than `archive.py`, and scan rather than use an index — the expression can't use one, and the tracks table is thousands of rows, not millions.

**Weekly recap.** `/week/<sunday>` sums up one Sunday-to-Saturday week (the calendar's rows): each day's theme and who set it, the busiest day, top sharers and artists, members whose first-ever share fell that week, and songs first posted before it. `/week` is the newest week with songs, with its canonical link pointing at the dated page; any other date in a week 301s to its Sunday, so a day page links its week without working out the date and each week keeps one URL. Prev/next step only between weeks with songs, and the sitemap lists every one. The summary is a pure function (`web/recap.py`, `summarize_week`) over one `tracks_between` query, plus each member's first day (`sender_first_days`, behind the TTL — it aggregates all of history) and the week's songs' first days. Names are grouped lowercased, as member pages match them, so a retyped name is neither a second sharer nor a newcomer. `/weekly.xml` carries the newest `WEEKLY_FEED_WEEKS` (12) *finished* weeks, newest first: a week still running would change under a reader that had already shown it, so an entry appears once, stamped at the week's close, and keeps its page as its id. It reuses `feed.py`'s escaping, and `base.html` advertises it beside `/feed.xml`.

**Link previews.** A day of the channel gets pasted into a chat far more often than it gets browsed, and an unfurled card was blank. `base.html` builds Open Graph and Twitter tags for every page, and the day route fills them with the theme, the song count, and the day's first track as `og:image`. `absolute_url` honours `x-forwarded-proto`, because the hosted deployment terminates TLS upstream and would otherwise advertise `http://` URLs. `robots.txt` keeps crawlers off the API, partials, audio and search; `sitemap.xml` lists every archived day. The 404 is `noindex` and points its canonical at the site root rather than reflecting the path that missed. With `[web] public_url` set, `absolute_url` takes the authority from config instead of the request's `Host` header — a canonical link is a statement about where the page lives, and a spoofed `Host` must not be able to make one. For iOS "Add to Home Screen", `base.html` pins `apple-mobile-web-app-title` and links a real 180×180 opaque PNG (`apple-touch-icon.png`): iOS ignores SVG icons and fills transparency black, and without these it names the app after the page `<title>` — on Now Playing, the day's theme — and draws a letter tile.

**Cover art in lists.** Day, search and member pages put a 56×32 still beside each song (`partials/thumb.html` — one macro, so the three can't drift). It is `mqdefault` rather than the smaller `default.jpg`, which has letterbox bars baked in, and it is the still Now Playing already uses, so the song that's playing is already cached; `i.ytimg.com` was already in the image CSP. Images are lazy and sized in both markup and CSS, so a 100-row search neither fetches every still up front nor shifts as they arrive. The queue deliberately has none: it re-renders on every state push, and re-inserting images on each swap would flicker for no gain.

**Feed.** `/feed.xml` is an Atom feed of the newest `FEED_DAYS` (30) days that have songs, one entry per day — the unit people already share. `web/feed.py` is pure functions over `Database.recent_days_tracks`, so the markup is tested without a server. Choices that were deliberate: an entry's `id` and link are the day's permalink, so a reader recognises an entry it has already shown; `updated` is the day's newest song time, not the request time, so entries don't look changed on every poll; free text from the mesh (sender names, theme titles, oEmbed titles) has XML-forbidden control characters stripped, because one of them makes the whole document unparseable and a reader drops the *feed*, not the entry, for as long as that day stays in the window; a huge day is cut at `SONGS_PER_ENTRY` with "…and N more" while the summary keeps the real count; a nonsense mesh clock falls back to the day's midnight instead of taking the feed down; a placeholder `Untitled —` theme is just the date. The query picks days with a per-theme `EXISTS` walking the date index (as `newest_day_with_tracks` does) rather than a `DISTINCT` over the themes×tracks join, which read every track; the rows sit behind the same 5 s TTL as the other whole-archive reads, so a polling stampede costs one query. `base.html` advertises the feed with `<link rel="alternate">` on every page, built with `absolute_url` so it is https behind the proxy.

**Delivery.** The app gzips its own responses (`GZipMiddleware`, at level 6 rather than the default 9: the same size within a percent at about half the CPU, and every partial re-render goes through it) — the archive pages are repeated markup and compress better than 10:1, which is what the hosted embed's visitors are actually waiting on. Every first-party asset URL carries `?v=<newest static mtime>`, so those responses are `immutable` for a year and a navigation costs zero asset requests; a bare, unversioned path can't make that promise and gets five minutes instead (`VersionedStatic`) — which is what the vendored `htmx.min.js`, referenced without a version, gets. `/audio` opts out of gzip with `Content-Encoding: identity`: opus is already compressed, and gzipping a ranged 206 would break seeking. htmx is deferred — nothing calls its API at parse time, so there's no reason to block first paint on it.

The nav is **hx-boosted**, swapping `<main>` (`hx-select`) rather than reloading the document. That keeps the `<audio>` element, the WebSocket and the speaker role alive across a click, so moving between Now Playing and the Archive no longer interrupts the song — the reason `hx-select` matters is that a default boost would replace the whole body and re-run every script, opening a second socket. The header sits outside the swap, so `nav.js` recomputes `aria-current` from the URL on settle and on back/forward.

**Hot-path reads.** Every player-state push makes each open page re-fetch, and the whole-archive aggregates behind those pages (`archive_days`, `all_themes`, the stats queries, the feed's rows) each scan the themes×tracks join. `WebContext` keeps them until the archive changes: nothing in them moves except when a song or a theme lands, and those arrive as bus events, so `create_app` registers `WebContext.invalidate` as a synchronous bus listener (`EventBus.listen`, which runs on the publisher's own stack with no task and no queue) for `track.discovered`, `track.ready`, `track.failed` and `theme.created`. The caches are dropped exactly then and otherwise kept however hard a crawler or a feed reader polls, and the day arrows never lag. A 60-second TTL (`CACHE_TTL_S`) remains as the safety net for a change that sends no event — the operator CLI editing the archive from another process, a play landing in the stats total. The connection itself carries a 32 MiB page cache, memory-mapped reads and in-memory temp tables, and runs `PRAGMA optimize` before it closes. Broadcasting a state push is parallel (`asyncio.gather`) with a per-socket send timeout (`SEND_TIMEOUT_S`, 5 s), so a page that stopped reading — a laptop lid closing mid-send — can't delay every page after it.

**Hardening.** The player has no login, so `server.py` wraps the app in three layers, outermost first. *Security headers* (`SecurityHeaders`) put a Content-Security-Policy on every response — scripts and styles are our own files (the templates carry no inline handlers; `eq.js` and `playbar.js` use delegated listeners instead), images are ours plus YouTube stills, audio streams from `/audio`, fetch and the WebSocket stay on the origin, the only frame is YouTube's player, and embed hosting additionally allows the YouTube IFrame API and the donation button — together with `nosniff`, a strict referrer policy and `X-Frame-Options: SAMEORIGIN`. The policy is the part with teeth: with no inline script allowed, markup that reached a page through a mesh name or an oEmbed title couldn't run even if escaping slipped. A handler that already set one of these keeps its own value, and the policy is switchable (`security_headers`) or report-only (`csp_report_only`) for trying a change out. *Host pinning* (`TrustedHostMiddleware`, from `[web] allowed_hosts`) is off by default, because an appliance reached by IP, `.local` name and port-forward needs any host; turning it on keeps DNS-rebinding pages from reaching a LAN radio. *The origin guard* (`OriginGuard`) refuses cross-site state changes: browsers attach `Origin` to every POST and WebSocket handshake, so one that doesn't match `Host` (or is `null`, or `Sec-Fetch-Site: cross-site`) is another site driving the radio — otherwise any page a LAN user has open could `POST /api/skip`, or open `/ws` to claim the speaker role, since browsers don't enforce same-origin on WebSockets. A request with no `Origin` is not a browser (curl, the relay pusher, tests) and passes; reads stay open so an archive link pasted into a chat keeps working. The WebSocket is refused by closing before `accept`, which uvicorn turns into a plain 403 — the ASGI denial-response extension would let us write the 403 ourselves, but the websockets implementation then logs a failed handshake and tries a 500 on top. Inputs are bounded at the edge too: seek and duration values must be finite and at most a day, and the relay endpoint caps a push at 16 MiB / 5,000 messages (§14). Free text is bounded where rows are written rather than per source (`Database.clean_text`, `clean_duration`): titles, artists and theme titles are one line of at most 256 characters, sender names 64, control characters dropped, and a track length that isn't a finite number of seconds within a day is not stored — the relay's `meta` used to go straight into the shared row, and one `"duration": "inf"` broke the home page and `/api/state` for every visitor cued onto that day. The player reads lengths through one guard (`_duration`) and the `mmss` filter formats nothing it can't, so a row from before the bound can't take a page down either. A queue holds at most `[player] max_queue` (200) songs and never the same video twice; at the ceiling a fresh channel post displaces station filler and nothing displaces a post. Open WebSockets are counted — 8 per session, 64 on the communal registry, 1,024 per process, a refused handshake closed before `accept` with 1013 — and a page may claim the speaker role once a second, never when it already has it, since each claim fans state out to every socket. The config file is checked before any of this runs (`validate_config`): every numeric key's type and range, the flags, the enumerations (profile, backend, audio format), the quiet-hours shape and the time zone, with every problem reported at once and startup refused, because a negative interval made `asyncio.sleep` return at once and a quoted number crashed a loop the supervisor then restarted forever; unknown keys are logged and ignored. Presses and searches are rate-limited per client (`web/ratelimit.py`: a token bucket of thirty presses then ten a second, fifteen searches then three a second, 429 with `Retry-After` past that, reads never counted, the relay's authenticated push exempt), sitting inside the origin guard so a refused cross-site request costs nobody their budget. The client's address is the real one only behind a proxy named in `[web] trusted_proxies` — one list that feeds uvicorn's `forwarded_allow_ips` and gates whether an `X-Forwarded-Proto` is believed for the canonical link and the cookie's Secure flag; a header from any other peer is just a header. The policy itself names only this origin and YouTube now (the donation button is a link of ours, which was the only reason for inline styles and two more CDNs), spells the WebSocket out as this request's own host rather than any, and every response adds `Cross-Origin-Opener-Policy: same-origin` and a `Permissions-Policy` that turns off the device APIs no page asks for; a site named with an `https` `public_url` sends `Strict-Transport-Security`.

**The admin page.** `/admin` (`web/routes_admin.py`, `web/admin_auth.py`, `db/admin.py`, templates under `admin/`) is the browser form of the operator's maintenance flags: retitle a day (`rename_theme`, as `--set-theme`), take a song off a day (`delete_track`, as `--delete-track`), correct a song's title or artist, merge artist spellings, see each ingest path's last report and probe a feed (`corescope.probe`, as `--probe-feed`), and list, take and download backups. It is mounted only when `[web] admin_password_hash` (or `MESHRADIO_ADMIN_PASSWORD_HASH`) holds a hash from `meshradio --hash-admin-password` — `create_app(admin=AdminSettings(...))` — so a deployment that never sets one has no admin surface at all, and `validate_config` refuses a hash that could never match rather than start with an admin page nobody can enter. The design follows the usual rules for an admin surface on a public site, each for a reason specific to this one. *Authentication*: scrypt (N=2¹⁴, r=8 — OWASP's floor for scrypt) from the standard library, so the appliance carries no new dependency, checked off the event loop because 50 ms of CPU on the one loop would stall every visitor; optional RFC 6238 codes (`MESHRADIO_ADMIN_TOTP_SECRET`) with a one-step window and the last used step remembered (in `settings`, so across a restart too), so a code read over a shoulder can't be replayed in its half-minute. The code is a second page, `/admin/login/code`, reached only by a right password: the password page and its wrong-password answer are identical with or without two-step, so a visitor can't tell it's on. A right password issues a step cookie (`mr_admin_step`, path `/admin/login`, five minutes, three codes, bound to the address and the key fingerprint, held in memory in `PendingSignIns`), and a wrong code is logged as `sign_in_code_failed`, since it means the password is known. Five failures per address in 15 minutes pause sign-in from it, and 50 across all addresses pause it for everyone (`LoginThrottle`, in memory: a restart forgets, and the rate limiter and scrypt's cost still bound a guesser) — the global ceiling is what holds against guesses spread over many addresses, or a forged `X-Forwarded-For` where every proxy is trusted. An attempt counts as it starts, not when scrypt answers, so a parallel burst can't all pass the check; a right password takes back only its own attempt, so it buys no fresh tries at the code. *Sessions*: a random token in a cookie scoped to `/admin`, `HttpOnly`, `SameSite=Strict`, Secure over https, 12 hours at most and 30 minutes idle; the table holds only its hash, stamped with a fingerprint of the password hash and TOTP secret so changing either signs every browser out. The visitor session middleware skips `/admin` entirely, so an admin page never mints a player. *CSRF*: every form carries an HMAC of the session token — derived, not stored — on top of the origin guard every POST already passes. Forms are parsed with `urllib.parse` (64 KiB cap) rather than adding python-multipart for them. *Headers*: `Cache-Control: no-store` and `X-Robots-Tag: noindex` on every admin response, `robots.txt` disallows `/admin`, and the pages live under the same CSP (their one script, `admin.js`, is a file, and the templates carry no inline style). *Audit*: `admin_log` records each sign-in, failure and change with its before and after values, kept a year (pruned as entries are written); the CLI's `--set-theme` and `--delete-track` write to it too (actor `cli`), so no fix bypasses the record. *Safe destructive actions*: a removal states its real effects (the row, its plays, a tombstone), needs the day's date typed (the server checks it; `admin.js` only keeps the button disabled until it matches), and takes a `preremove` snapshot first — at most one per ten minutes, so a clean-up of many songs can't rotate every scheduled snapshot out of `[backup] keep`. The tombstone now carries the whole removed row (`track_json`), so `restore_deleted_track` puts a song back exactly, plays aside; a tombstone from before v14 can only be lifted, and that song returns when the channel's history is next read in full. Undo exists for renames, removals, song edits and merges, writes its own log entry and marks the original `undone_by`, so the log reads straight through. Restoring a whole backup stays CLI-only: it replaces the database under every running session, and the service has to be stopped. *Edits that stick*: a hand-corrected title or artist sets `meta_edited_at`, and `update_track_metadata` then leaves both alone, because a late oEmbed answer or a relay re-push would otherwise put the wrong text back. A merge writes `artist_aliases` and respells existing rows; `canonical_artist` maps a song's artist as it is stored (in `add_track` and `update_track_metadata`), so later arrivals join the merged name, and the log entry keeps each respelled row's old value for undo. Look-alike spellings are grouped by case, a leading "The", YouTube's " - Topic" suffix, "&"/"and" and punctuation (`routes_admin.artist_key`). *Device-only behavior stays off the public site*: the config view drops the mesh, cache, quiet-hours and volume settings there, and the Device screen (yt-dlp version, cache use, failed downloads with Retry, the output) is a 404. Each instance keeps its own archive and admin page — a removal isn't relayed, and a receiver ignores a relayed rename for a day it has already locked.

Now Playing always tracks the latest day: the server re-cues an idle session onto the newest day both on each visit and live (a bus watcher rolls open tabs forward the moment a new day's first song lands), while never interrupting one that's actively playing. Playback controls include **🔀 shuffle** (reorders the upcoming queue) and a persistent **⤴ Export** that opens the whole day's songs as an anonymous YouTube `watch_videos` playlist regardless of what's playing. The queue uses a selection model: click a track, then **⤒ Play next** / **✕ Remove** act on it from the bar beside **Clear queue** — one tap-target set instead of per-row buttons, which reads better on touch. The spectrum-analyzer canvas renders only where it can be driven (web-playback mode); embed hosting streams inside a cross-origin YouTube iframe, so it's omitted there rather than sitting blank.

---

## 10. Kit provisioning & first-boot UX

- **Flashable image** built with `pi-gen` (or `sdm`) in CI: OS + dependencies + app preinstalled. Builder flashes, boots, done.
- **First boot:** no known WiFi → `provision.py` brings up a `MeshRadio-Setup` AP via NetworkManager (`nmcli`) with a captive-portal page: pick WiFi, paste `#music` channel key, optionally set CoreScope URL. Reboot into service.
- **Updates:** OLED/web "Update" button = `git pull` + `pip install -e .` + restart. Nightly `yt-dlp` self-update as a systemd timer.
- **Repo deliverables:** source, STLs (`/hardware/stl`), wiring diagram (`/hardware/wiring.svg` — everything is header/screw-terminal), BOM with live links, assembly guide with photos, channel-convention doc, image-build workflow.

*As built, little of the above exists yet.* `system/provision.py` holds only `write_config_toml()`, which renders a minimal `config.toml` (profile, `#music` channel key, CoreScope URL) for a provisioned radio; the AP/captive-portal flow, the `pi-gen` image, the update button, `/hardware/` and the guides are all still to do; the nightly yt-dlp timer is in `deploy/` (§7). Today an appliance is set up by hand: clone, install, write `/etc/meshradio/config.toml`, and install [deploy/meshradio.service](deploy/meshradio.service) (§14).

---

## 11. Why these choices (maintainability ledger)

- **Python**: meshcore client, yt-dlp, mpv bindings, luma.oled, gpiozero — every hardware and media dependency is Python-first. Any other language means writing at least one of these yourself.
- **Monolith + event bus**: one unit to deploy, debug, and reason about; modules stay decoupled through the bus, so ripping out CoreScope or adding a Spotify resolver later is additive.
- **SQLite**: the archive *is* the product; a single file that survives reflashes (kept on a separate data partition) and can be copied off as a channel history export.
- **htmx over React**: the web UI must still build in five years on a Pi with no internet toolchain.
- **Cache-first playback**: converts yt-dlp's known fragility from "radio is broken" into "newest song is delayed."
- **Everything through PipeWire**: output switching, BT, and volume are OS problems, not app problems.

## 12. Assumptions to confirm

1. Theme convention (`Theme:` prefix) is acceptable to propose to the channel. *(Moot in practice: the channel already posts themes in its own phrasings, and the parser reads those — see §6.)*
2. Live mode = auto-play when idle, enqueue when busy, never interrupt.
3. AUS CoreScope exposes a pollable API for channel messages (adapter isolated regardless). *(Confirmed — §6.)*
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
- **Dependency:** the Lite relies on the CoreScope poll rather than RF, so it's only as live as its feeds. The backup analyzer feed (§6, `[comchan]`, on by default even for configs written at provisioning) covers an outage of the AUS instance, so the Lite goes dark only if both are down. The full build additionally keeps working off RF.
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
`[relay].token` on the Pi), compared with `secrets.compare_digest` on bytes, so a
non-ASCII garbage header is a 401 rather than a 500. The token rides on every
push, so the pusher refuses a `push_url` that isn't `https://` — plain `http://`
is allowed only to `localhost`, `127.0.0.1` or `::1` — and does so at startup: a
misconfigured relay logs `relay disabled: …` and stays off instead of taking the
radio down (or looping the systemd unit), and instead of leaking the token every
two minutes. The receiver bounds what it will parse: a push is refused with a 413
past 16 MiB (counted as the body arrives, since a chunked request carries no
`Content-Length`) and only its first 5,000 messages are ingested. Relayed messages
are stamped `source = 'corescope'` — they are the community channel's history,
whichever feed first saw them.

Auto-created "Untitled —" placeholder themes are *not* relayed — the receiver
makes its own when the day's first link lands. So when the real theme message
turns up later and adopts the placeholder (§6), that rename has to be pushed as
its own message, or the host stays on "Untitled" for the day. `adopt_theme`
therefore stamps `themes.updated_at`, and the relay's themes cursor keys on
`COALESCE(updated_at, created_at)` — an in-place rename moves the row back in
front of the cursor instead of vanishing behind it.

**Self-healing against a wiped receiver.** Each push reports the receiver's track
count; when it drops below the home node's (a fresh host with an empty disk), the
pusher resets its cursor and re-backfills the whole channel automatically. An
empty push still goes out as a heartbeat so the wipe is detected promptly. This is
now a safety net rather than the norm: the host keeps its archive on a **persistent
disk** and polls CoreScope itself, so it depends on the
relay for neither ingestion nor durability — the relay going down no longer costs
the archive.

### Per-visitor sessions

The appliance is one communal radio; a public host is not — nobody should be able
to pause or skip a stranger's music. In embed mode each browser gets its **own
session player** (`web/sessions.py`): a session cookie names it, `app.py` supplies
a `player_factory`, and each session player has its own queue/position/day while
still hearing the shared `track.ready` stream. Snapshots persist to the
`web_sessions` table (§5) so a session survives a redeploy. On the appliance/LAN
path there's no factory — it stays the single shared player with the
"one-speaker-at-a-time" speaker registry.

A session is opened by the page's WebSocket connecting, by a POST (the visitor
pressed something), or by a returning cookie that has a snapshot on disk — never
by a bare GET, and only ever for a cookie this server signed. The cookie is
`<sid>.<hmac>` (`sessions.issue_cookie` / `verify_cookie`), keyed by a secret
created on first use and kept in the `settings` table, so cookies outlive a
redeploy along with the snapshots they name. A page view from a visitor with no
session renders from a throwaway cued player, and so does a POST that presented
no cookie of ours (the middleware minted one on that very request): a browser
always carries the cookie it got with the page, so that press is a script's.
Each such request used to mint a player, a task and a row on disk and churn the
session cap — a cheap POST flood could fill the hosted disk — and a well-formed
random sid did the same, so checking the shape was no defence. A WebSocket
handshake without a cookie we signed is refused rather than given a session
nothing could present again. Static files, audio, the health check, the feed,
the sitemap and the relay endpoint are outside the session middleware entirely
and carry no cookie. On shutdown the app's lifespan flushes every pending
snapshot and stops every player (`SessionManager.stop`), so a deploy or restart
loses nothing a visitor did in the last flush interval.

The manager keeps every session's landing view current. It cues the newest
day-with-songs when a session is opened or restored, re-checks on each visit for
an idle/paused session parked on an older day, and — via a `track.ready` watcher —
rolls idle sessions forward the moment a newer day's first song lands, pushing
state to open tabs so they update with no reload. A session that's *actually*
playing is always left alone; a restored "playing" flag is treated as stale
(a page load has no audio going yet in embed mode), so it advances too.

A session with no live player hears no `track.ready`, so a restored one would
miss every song posted while it was away — one saved with a single song up would
come back showing that song while the archive held the rest of the day. Each
player therefore keeps `seen_track_id`, the highest track id of its parked day it
has accounted for, in the snapshot; `restore()` queues the day's playable songs
above it behind what the session already holds. Songs the visitor played or
removed sit below the mark and stay gone. Snapshots saved before the mark existed
are caught up from the furthest song they still hold.

### Deployment & operations

- **Render blueprint** ([render.yaml](render.yaml)) — embed mode via
  [meshradio.render.toml](meshradio.render.toml); Python 3.11 to match CI;
  `MESHRADIO_INGEST_TOKEN` generated as a secret. A **persistent disk** mounts at
  the data dir so the archive survives deploys/restarts (disks need a paid
  instance, hence the Starter plan). The build is `uv sync --locked --no-dev`
  (Render adds uv when `uv.lock` is in the repo root), so production runs the
  pinned set the suite gated rather than a fresh resolution from the index.
- **CI gate** ([.github/workflows/test.yml](.github/workflows/test.yml)) — Render
  deploys `main` only after the test suite is green (`autoDeployTrigger:
  checksPass`), so a red suite never reaches production. CI installs from
  `uv.lock` (`uv sync --locked`, which fails if the lock is stale), so what it
  tests is the pinned set the project resolved, not whatever the index serves
  that day. A second workflow ([audit.yml](.github/workflows/audit.yml)) runs
  `pip-audit` over the exported pins — every extra, no dev tooling — on pull
  requests, weekly and on demand, and Dependabot opens a weekly grouped PR for
  the lock and the pinned actions. The audit deliberately doesn't run on push:
  an advisory against a library on a path the radio never touches shouldn't
  hold a deploy. The test job runs the suite on Python 3.11, 3.12 and 3.13,
  and a lint job runs `ruff check` and `mypy` (configured in `pyproject.toml`),
  so a style or type regression holds a deploy the way a red suite does.
- **`/healthz`** — liveness plus ingest freshness (`ingest_age_s`, track count,
  session count); Render's health check hits it, and a stale age means *every*
  ingest source (relay, CoreScope) went quiet.
- **DB backups** (`backup.py`) — rotating whole-DB snapshots (before migrations on
  each boot, then on an interval) for rollback from a bad migration or corruption,
  independent of host disk snapshots. Restore with `meshradio --list-backups` /
  `--restore-backup`, which snapshots the current DB first so it's reversible.
- **The Pi relay** runs under systemd ([deploy/meshradio.service](deploy/meshradio.service)):
  `Restart=on-failure` rides out transient network/CoreScope hiccups. The unit
  sandboxes the service — it runs yt-dlp, ffmpeg and deno against whatever the
  channel links to — so the file system is read-only to it apart from
  `/var/lib/meshradio` (the `StateDirectory`, also the working directory, where
  `data_dir` belongs), its cache directory and a private `/tmp`; no new
  privileges, an empty capability set, the `@system-service` syscall set, Unix
  and IP sockets only, and a memory ceiling. Device isolation is left out so the
  appliance profiles keep the OLED, GPIO and the node's serial port. The tokens
  come from `/etc/meshradio/env` (`MESHRADIO_RELAY_TOKEN`, and
  `MESHRADIO_INGEST_TOKEN` for a node that is itself a receiver), read as an
  `EnvironmentFile`, instead of sitting in `config.toml`.
- **Hardening config** — a public host should also set `[web] public_url` (canonical
  links and previews from config, not the `Host` header); a LAN appliance may set
  `[web] allowed_hosts`. The origin guard and security headers are on by default
  everywhere (§9). [meshradio.render.toml](meshradio.render.toml) names the
  hosted site's `public_url` (so it builds its links from config and sends
  `Strict-Transport-Security`) and trusts every peer as the proxy
  (`trusted_proxies = ["*"]`), since nothing reaches that app except through
  Render's; it leaves `allowed_hosts` empty.

### What this validates about the original design

The monolith-plus-event-bus decision (§4) paid off here: embed mode is one new
`PlayerBackend`, the relay is one new bus-agnostic `Service`, per-visitor players
are the *same* `PlayerService` instantiated per session, and the receiver reuses
the *same* ingest pipeline. A public multi-tenant deployment the design never
anticipated slotted in without touching the appliance code paths.
