# MeshRadio

*A standalone internet radio that plays the Austin MeshCore `#music` channel.*

MeshRadio listens to the `#music` public channel on the Austin MeshCore mesh,
extracts the YouTube / YouTube Music links members post against the daily
theme, and plays them — live as they arrive, and from a browsable archive of
past days and themes. No hardware required: until the appliance kit exists,
**your browser is the radio** via the built-in web player.

There's a public instance at **[meshradio.onrender.com](https://meshradio.onrender.com)**
— open it and press play. Full hardware/software design:
[meshradio-architecture.md](meshradio-architecture.md).

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

The first theme post of the day (America/Chicago) creates the day's theme
and locks it — later theme posts are ignored, so an accidental (or mischievous)
second "theme" can't reset it or split the day into two playlists. If songs
arrive before anyone sets a theme, they file under an `Untitled — <date>`
placeholder that the day's first real theme post then renames in place.

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
junk like `&si=...` is ignored. The song attaches to that day's theme,
credited to your node name.

**What gets ignored** — chatter without links (`@mentions`, emoji reactions,
"great pick!") is skipped. Reposting a link someone already shared that day is
a no-op: a song appears only once per day's playlist, so it never shows up
twice no matter how many people (re)post it. The first post that day wins and
keeps the credit.

---

## Setup (no hardware — any PC, Mac dev box, or Linux/Pi server)

Requirements: **Python 3.11+**, **ffmpeg**, and ~a few GB of disk for the
audio cache. (Embed and demo modes need neither ffmpeg nor yt-dlp — see below.)

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

[letsmesh]
base_url = "https://analyzer.letsmesh.net" # backup feed; enabled = false to skip
channel = "#music"

[cache]
ffmpeg_location = ""               # set to ffmpeg's folder if it's not on PATH
```

On first start MeshRadio backfills the channel's entire history from
CoreScope — themes, songs, senders — then polls every 3 minutes for new
posts. It also polls the LetsMesh analyzer (`analyzer.letsmesh.net`) as a
**backup feed**: same channel, same API, so anything both feeds see dedupes to
one row, and the archive keeps filling if the Austin CoreScope instance goes
down. Audio downloads into `data/cache/` in the background (a fresh backfill
takes a few minutes). Run `pytest` if you want to check the install.

Verify it's working: the log shows `CoreScope poll: N new tracks`, and the
Archive page fills with real days and themes.

Config precedence: `--config` flag → `$MESHRADIO_CONFIG` → `./meshradio.toml`
→ `/etc/meshradio/config.toml` → built-in defaults. Every key is optional;
see [meshradio.example.toml](meshradio.example.toml) for the full annotated
set. Secrets (the relay/ingest token) belong in the environment
(`MESHRADIO_INGEST_TOKEN`), not the committed file.

---

## Using the web player

Open **http://localhost:8080** (or `http://<pi-address>:8080` /
`http://meshradio.local` from another device on your LAN).

- **Now Playing** — art, title, artist, and which mesh member shared it.
  Controls: **▶/⏸ play-pause**, **⏭ next track**, **volume slider**, and the
  **📻 Start radio** button (below). The queue is listed underneath — **⤒**
  bumps a track to play next, **✕** removes it, and **Clear queue** empties
  it (the current song keeps playing; radio mode switches off so it doesn't
  refill what you just cleared).
- **Live jukebox** — when a new song lands on the channel it auto-plays if
  the radio is idle, or joins the queue if something's already playing. A new
  arrival never interrupts the current song.
- **Archive** — browse by day → theme → tracks. **▶ Play this day** replays a
  whole day in posted order; **+ queue** adds a single track. New visitors
  land with the newest day cued up so there's something to press play on.
- **📻 Start radio** — when the queue runs dry, this seeds a "station" from
  the current (or last-played) track using its YouTube Mix: similar songs are
  fetched, cached, and queued, and the station keeps topping itself up until
  you press **◼ Radio on** to stop. Radio tracks show a `radio` badge in the
  queue and never pollute the channel archive.
- **10-band EQ + spectrum analyzer** — a real graphic equalizer with classic
  presets and an FFT spectrum display, Winamp-style. (Web-playback mode; the
  embed player is the plain YouTube stream.)
- **First click** — browsers block audio until you interact with the page
  once; if you see **🔊 Click to enable audio**, click it and you're set.
- **One speaker at a time** — on a communal player (LAN/appliance) open the
  page in as many tabs/devices as you like; controls stay in sync everywhere,
  but only one page plays audio (the most recently opened, so there's never
  an echo). Any other tab shows a **🔊 Play audio in this tab** button to take
  over as the speaker.
- **Live vs. backfill** — only songs posted within the last 30 minutes
  (`live_window_s`) auto-play or queue; older history downloads quietly into
  the archive. Channel songs always queue ahead of radio-station filler.

Where the sound comes out depends on `player.backend`:

| Backend | Speaker | Downloads audio? | Use |
|---|---|---|---|
| `web` (default off-hardware) | whatever browser has the page open | yes, via yt-dlp | LAN / dev box |
| `embed` | each visitor's own browser, via the YouTube IFrame player | **no** — metadata only | public hosting |
| `mpv` (`pi4`/`lite` profiles) | the Pi's speaker/jack/Bluetooth; the web page becomes a remote | yes | future appliance |

---

## Public hosting (embed mode + relay)

MeshRadio runs as a public web app in **embed mode**: instead of downloading
and serving audio, the server ships only metadata (video ids, titles, themes,
queue order) and each visitor's browser streams every song straight from
YouTube via the IFrame player. That keeps a public deployment clear of
redistributing copyrighted media, and each visitor gets **their own session
player** (queue, position, current day) so nobody can pause or hijack anyone
else's music. The [live instance](https://meshradio.onrender.com) is deployed
this way on Render — see [render.yaml](render.yaml) and
[meshradio.render.toml](meshradio.render.toml).

**The relay problem.** Cloudflare (and YouTube) challenge datacenter IPs, so
a hosted instance can't poll CoreScope or fetch YouTube itself. The fix is a
**relay**: a home node with residential internet (a Raspberry Pi under
systemd) polls the channel normally, then pushes new messages — plus the track
metadata it already resolved — to the hosted instance's authenticated
`POST /api/ingest` endpoint. Turn it on with a `[relay]` block:

```toml
[relay]
push_url = "https://meshradio.onrender.com"   # the hosted instance
token    = "…"                                 # must match its MESHRADIO_INGEST_TOKEN
interval_s = 120
```

The relay is self-healing: each push reports the receiver's track count, and
when it drops below the home node's (e.g. a fresh host with an empty disk) the
pusher resets its cursor and re-backfills the whole channel automatically. This
is now a safety net rather than the norm — the hosted instance keeps its
archive on a persistent disk (`disk:` in [render.yaml](render.yaml)), so
history survives deploys, restarts, and spin-downs on its own, and the host
also polls CoreScope and the LetsMesh analyzer directly. The relay going down
no longer costs the archive. `/healthz` exposes liveness plus ingest freshness
(Render's health check hits it; a stale `ingest_age_s` means every ingest
source stopped).

The Pi runs under systemd — see [deploy/meshradio.service](deploy/meshradio.service)
for the unit and install/update commands.

---

## FAQ

**How does this authenticate to Google / does YT Premium remove ads?**
It doesn't authenticate at all, and no Premium is needed. In web/mpv mode
MeshRadio never plays *from* youtube.com: `yt-dlp` downloads each track's raw
audio stream once into the local cache, and playback is always from that local
file. YouTube's ads are injected by their player app at watch time — they
aren't part of the media stream — so cached playback is inherently ad-free.
(If a video is age-restricted or region-locked, yt-dlp can't fetch it
anonymously; the track then shows in the archive as metadata-only with a
"couldn't fetch audio" badge.) In embed mode the browser uses YouTube's own
IFrame player, so normal YouTube ad rules apply there.

**What if yt-dlp breaks (YouTube changed something)?**
New tracks queue as "caching…" and retry; the already-cached archive keeps
playing. On the appliance a nightly job updates yt-dlp automatically — on a
dev box run `pip install -U yt-dlp`. Embed mode sidesteps this entirely (no
downloads).

**Does this need a mesh node plugged in?**
No. The CoreScope path covers everything with ~3 minutes of latency. A local
MeshCore companion node (Heltec V3 on USB) makes ingestion instant and
off-grid capable — enable `[mesh]` in config when you have one.

**Is the process resilient?**
Every long-lived loop (ingest, cacher, poller, relay) runs under a supervised
runtime: an unhandled exception is logged loudly and the loop restarts with
backoff instead of dying silently. systemd restarts the process itself on a
hard crash.

---

## Project status

**v0.1 — core software + web player + public hosting working, hardware
integration pending.**

| Area | State |
|---|---|
| Event bus, SQLite archive, migrations | ✅ working, tested |
| Link/theme parsing (matches real channel usage) | ✅ working, tested |
| Ingest pipeline + mesh/CoreScope dedupe | ✅ working, tested |
| CoreScope poller (Austin instance) | ✅ working, verified against live channel |
| LetsMesh analyzer backup feed (dedupes against CoreScope) | ✅ working, tested |
| Cache-first downloader (yt-dlp) + self-healing retries | ✅ working, tested |
| Player: live policy, queue, archive replay, quiet hours | ✅ working, tested |
| Web player (browser audio, radio-station mode, EQ/analyzer) | ✅ working, tested |
| **Embed mode + per-visitor sessions (public hosting)** | ✅ working, deployed on Render |
| **Relay (home node → hosted instance) + auto-backfill** | ✅ working, running on the Pi |
| **Supervised runtime, CI gate, `/healthz`** | ✅ working |
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
├── config.py        # TOML config over dataclass defaults (+ env secrets)
├── db.py            # aiosqlite layer + migrations (themes/tracks/plays/settings)
├── net.py           # shared outbound HTTP client setup (User-Agent, timeouts)
├── runtime.py       # supervised task/Service runtime (restart-with-backoff)
├── ingest/          # parse.py (pure), service.py, mesh.py, corescope.py, relay.py
├── media/           # cacher.py (yt-dlp), player.py, radio.py (YT Mix), metadata.py
├── audio/           # routing.py (PipeWire/wpctl, per-profile), bluetooth.py
├── ui/              # panel.py (OLED + controls; log panel on dev)
├── system/          # power.py (fuel gauge), provision.py (first boot)
└── web/             # FastAPI app split into:
    ├── server.py        # create_app: assembles everything, lifespan, sessions
    ├── context.py       # WebContext shared state on app.state
    ├── sessions.py      # per-visitor session players + speaker registry
    ├── routes_pages.py  # HTML pages + htmx partials
    ├── routes_api.py    # player/queue control API
    ├── routes_ingest.py # /audio streaming, relay /api/ingest, /healthz
    ├── ws.py            # WebSocket: forwards bus events → htmx re-fetch
    ├── static/         # vendored htmx + js/ (embed, eq, playbar, radio), style.css
    └── templates/      # Jinja2 (base, index, archive, partials/)

deploy/meshradio.service   # systemd unit for the Pi relay
render.yaml + *.render.toml # public embed-mode deployment
.github/workflows/test.yml  # CI; Render deploys main only after it's green
```

Dev without media tooling: `pip install -e .` and run with `--demo` for
simulated traffic and playback (no yt-dlp/ffmpeg needed). On the appliance,
add hardware extras: `pip install -e ".[media,hw]"`.
