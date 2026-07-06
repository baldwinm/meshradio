# MeshRadio

*A standalone internet radio that plays the Austin MeshCore `#music` channel.*

MeshRadio listens to the `#music` public channel on the Austin MeshCore mesh,
extracts the YouTube / YouTube Music links members post against the daily
theme, and plays them — live as they arrive, and from a browsable archive of
past days and themes. No hardware required: until the appliance kit exists,
**your browser is the radio** via the built-in web player.

Full hardware/software design: [meshradio-architecture.md](meshradio-architecture.md).

---

## Posting songs on the mesh channel

Anything posted to `#music` flows into every MeshRadio automatically. The
rules the parser follows:

**Setting the day's theme** — post a message containing the word *theme*
followed by a colon and the title. All of these work (case-insensitive):

```
Theme: songs about rain
Happy Friday Music Meshers! Today's theme is: Friends and friendship.
theme for today: one hit wonders
```

The first theme post of the day (America/Chicago) creates the day's theme;
repeat posts of the same title are deduplicated. If nobody sets a theme,
songs file under `Untitled — <date>`.

**Sharing a song** — post any message containing a YouTube or YouTube Music
link. Supported forms:

```
https://music.youtube.com/watch?v=VIDEOID       (share link from YT Music app)
https://www.youtube.com/watch?v=VIDEOID
https://youtu.be/VIDEOID
https://youtube.com/shorts/VIDEOID
```

Extra text around the link is fine ("this one goes hard →
https://youtu.be/..."), multiple links in one message are fine, and tracking
junk like `&si=...` is ignored. The song attaches to the most recent theme of
that day, credited to your node name.

**What gets ignored** — chatter without links (`@mentions`, emoji reactions,
"great pick!") is skipped. Reposting a link someone already shared that day
creates a second entry credited to you (it's a jukebox, not a democracy);
the audio itself is only downloaded once.

---

## Setup (no hardware — any PC, Mac dev box, or Linux/Pi server)

Requirements: **Python 3.11+**, **ffmpeg**, and ~a few GB of disk for the
audio cache.

```sh
git clone https://github.com/baldwinm/meshradio && cd meshradio

# create a venv and install (any tool works; uv shown, plain pip works too)
uv venv && uv pip install -e ".[media]" --group dev
# or: python3 -m venv .venv && .venv/bin/pip install -e ".[media]"

cp meshradio.example.toml meshradio.toml    # then edit — see below
.venv/bin/meshradio                          # Windows: .venv\Scripts\meshradio
```

Minimal `meshradio.toml`:

```toml
hardware_profile = "dev"
data_dir = "./data"                # the archive + audio cache live here

[corescope]
base_url = "https://scope.digitaino.com"   # Austin CoreScope instance
channel = "#music"

[cache]
ffmpeg_location = ""               # set to ffmpeg's folder if it's not on PATH
```

On first start MeshRadio backfills the channel's entire history from
CoreScope — themes, songs, senders — then polls every 3 minutes for new
posts. Audio downloads into `data/cache/` in the background (a fresh backfill
takes a few minutes). Run `pytest` if you want to check the install (70 tests).

Verify it's working: the log shows `CoreScope poll: N new tracks`, and the
Archive page fills with real days and themes.

---

## Using the web player

Open **http://localhost:8080** (or `http://<pi-address>:8080` /
`http://meshradio.local` from another device on your LAN).

- **Now Playing** — art, title, artist, and which mesh member shared it.
  Controls: **▶/⏸ play-pause**, **⏭ next track**, **volume slider**, and the
  **📻 Start radio** button (below). The queue is listed underneath.
- **Live jukebox** — when a new song lands on the channel it auto-plays if
  the radio is idle, or joins the queue if something's already playing. A new
  arrival never interrupts the current song.
- **Archive** — browse by day → theme → tracks. **▶ Play this day** replays a
  whole day in posted order; **+ queue** adds a single track.
- **📻 Start radio** — when the queue runs dry, this seeds a "station" from
  the current (or last-played) track using its YouTube Mix: similar songs are
  fetched, cached, and queued, and the station keeps topping itself up until
  you press **◼ Radio on** to stop. Radio tracks show a `radio` badge in the
  queue and never pollute the channel archive.
- **First click** — browsers block audio until you interact with the page
  once; if you see **🔊 Click to enable audio**, click it and you're set.
- Multiple open tabs each play audio (mute the extras); controls stay in
  sync everywhere via WebSocket, and a track ending in one tab advances the
  queue exactly once.

Where the sound comes out: with `backend = "web"` (the default off-hardware),
whatever browser has the page open is the speaker. On the future appliance
build (`pi4`/`lite` profiles) mpv plays out the Pi's speaker/jack/Bluetooth
and the web page becomes a pure remote control.

---

## FAQ

**How does this authenticate to Google / does YT Premium remove ads?**
It doesn't authenticate at all, and no Premium is needed. MeshRadio never
plays from youtube.com: `yt-dlp` downloads each track's raw audio stream once
into the local cache, and playback is always from that local file. YouTube's
ads are injected by their player app at watch time — they aren't part of the
media stream — so cached playback is inherently ad-free for everyone. (If a
video is age-restricted or region-locked, yt-dlp can't fetch it anonymously;
the track then shows in the archive as metadata-only with a "couldn't fetch
audio" badge.)

**What if yt-dlp breaks (YouTube changed something)?**
New tracks queue as "caching…" and retry; the already-cached archive keeps
playing. On the appliance a nightly job updates yt-dlp automatically — on a
dev box run `pip install -U yt-dlp`.

**Does this need a mesh node plugged in?**
No. The CoreScope path covers everything with ~3 minutes of latency. A local
MeshCore companion node (Heltec V3 on USB) makes ingestion instant and
off-grid capable — enable `[mesh]` in config when you have one.

---

## Project status

**v0.1 — core software + web player working, hardware integration pending.**

| Area | State |
|---|---|
| Event bus, SQLite archive, migrations | ✅ working, tested |
| Link/theme parsing (matches real channel usage) | ✅ working, tested |
| Ingest pipeline + mesh/CoreScope dedupe | ✅ working, tested |
| CoreScope poller (Austin instance) | ✅ working, verified against live channel |
| Cache-first downloader (yt-dlp) + fallback ladder | ✅ working |
| Player: live policy, queue, archive replay, quiet hours | ✅ working, tested |
| **Web player (browser audio, radio-station mode)** | ✅ working, tested |
| Mesh serial ingestion (meshcore) | 🟡 built, needs validation on a Heltec V3 |
| OLED panel + encoder/buttons | 🟡 skeleton, needs hardware bring-up |
| PipeWire routing (pi4/lite backends) | 🟡 built, needs hardware bring-up |
| Bluetooth pairing (BlueZ) | ⬜ interface stubbed |
| UPS fuel gauge / safe shutdown | ⬜ stubbed |
| First-boot provisioning, pi-gen image, STLs, BOM docs | ⬜ not started |

## Layout

```
meshradio/
├── app.py           # asyncio entrypoint, wires modules to the bus
├── bus.py           # tiny pub/sub EventBus + event vocabulary
├── config.py        # TOML config over dataclass defaults
├── db.py            # aiosqlite layer + migrations (themes/tracks/plays/settings)
├── ingest/          # parse.py (pure), service.py, mesh.py, corescope.py
├── media/           # cacher.py (yt-dlp), player.py, radio.py (YT Mix), metadata.py
├── audio/           # routing.py (PipeWire/wpctl, per-profile), bluetooth.py
├── ui/              # panel.py (OLED + controls; log panel on dev)
├── system/          # power.py (fuel gauge), provision.py (first boot)
└── web/             # FastAPI + Jinja2 + vendored htmx, WebSocket live state
```

Dev without media tooling: `pip install -e .` and run with `--demo` for
simulated traffic and playback (no yt-dlp/ffmpeg needed). On the appliance,
add hardware extras: `pip install -e ".[media,hw]"`.
