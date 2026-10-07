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

The colon is what marks the title, and it has to be a real one — a smiley
(`theme is planes :-) or trains?`), a link, or a time (`theme at 8:30`) is
punctuation, not a delimiter, so those messages set no theme at all rather
than a garbled one. Say `theme … : <title>` and you'll be understood.

The first theme post of the day (America/Chicago) creates the day's theme
and locks it — later theme posts are ignored, so an accidental (or mischievous)
second "theme" can't reset it or split the day into two playlists. If songs
arrive before anyone sets a theme, they file under an `Untitled — <date>`
placeholder that the day's first real theme post then renames in place.

Because the lock also blocks corrections, the operator can retitle a day from
the command line — see [Fixing a theme](#fixing-a-theme).

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

Requirements: **Python 3.11+** (with SQLite 3.34 or newer, which every
2021+ OS ships — the search index uses its trigram tokenizer), **ffmpeg**, a
JavaScript runtime (**[deno](https://deno.com)**, which yt-dlp needs to solve
YouTube's challenge), and ~a few GB of disk for the audio cache. The `mpv` backend (Pi appliance
profiles) also needs `libmpv`. (Embed and demo modes need none of ffmpeg,
yt-dlp or deno — see below.)

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

[comchan]
enabled = true                     # backup analyzer feed; base_url already defaults

[cache]
ffmpeg_location = ""               # set to ffmpeg's folder if it's not on PATH
# ytdlp_extra_args = ["--js-runtimes", "deno:/path/to/deno"]   # if deno isn't on PATH
```

On first start MeshRadio backfills the channel's entire history from
CoreScope — themes, songs, senders — then polls every 3 minutes for new
posts. Audio downloads into `data/cache/` in the background, two tracks at a
time (`[cache] concurrency`), so a fresh backfill takes a few minutes. Run
`pytest` if you want to check the install (CI does the same from the lockfile:
`uv sync --locked --group dev && uv run pytest`).

Verify it's working: the log shows `corescope poll: N new tracks`, and the
Archive page fills with real days and themes. `meshradio --probe-feed` asks
each analyzer for its newest page from the command line, without starting
the radio or touching the archive, and prints what came back.

**The backup feed.** A single analyzer instance is a single point of failure
for ingestion, so a second CoreScope-compatible one
([analyzer.comchan.net](https://analyzer.comchan.net/#/channels)) is polled
alongside the primary under the `[comchan]` block. Both run continuously
rather than one failing over to the other: dedupe keys on
channel+sender+video+minute, not on which feed delivered the message, so the
overlap no-ops and an outage on either side costs nothing but the other's
poll interval. Its tracks are stamped `source = 'comchan'`, so it stays
visible which analyzer covered a given day. `base_url` defaults to a real
instance (unlike `[corescope]`, whose URL is set at provisioning) so existing
appliance configs get the backup without being edited; turn it off with
`enabled = false`.

Both hosts run CoreScope (analyzer.comchan.net runs the
[ComchanNet fork](https://github.com/ComchanNet/CoreScope), whose channel API
is upstream's unchanged) and go through one adapter: the analyzer pages
channel history 500 posts at a time, newest first, and the poller walks back
from the end until it reaches what the last poll handled, so a first boot or
a long outage recovers the whole channel rather than the newest hundred. To
see what a feed is answering from the machine that will poll it:

```
meshradio --probe-feed            # both feeds
meshradio --probe-feed comchan    # one of them; exits 1 if it fails
```

It reports whether the host answered, whether it lists the channel, which
fields its messages carry, and the newest few posts as the poller reads them.

Config precedence: `--config` flag → `$MESHRADIO_CONFIG` → `./meshradio.toml`
→ `/etc/meshradio/config.toml` → built-in defaults. Every key is optional;
see [meshradio.example.toml](meshradio.example.toml) for the full annotated
set. Secrets belong in the environment, not the committed file:
`MESHRADIO_INGEST_TOKEN` for the receiver's token (`[web] ingest_token`) and
`MESHRADIO_RELAY_TOKEN` for the pusher's (`[relay] token`), and
`MESHRADIO_ADMIN_PASSWORD_HASH` / `MESHRADIO_ADMIN_TOTP_SECRET` for the admin
page; each overrides the file. `MESHRADIO_PUBLIC_URL` likewise overrides
`[web] public_url`, so a host moved to its own domain names it from its
dashboard. Values are checked at startup — a number of the wrong type or out of
range, an unknown backend, audio format or time zone — and a bad one stops
the radio with a message naming every offending key, instead of a loop
crashing (or, for a negative interval, spinning) under the supervisor. A key
that isn't one the radio knows is logged and ignored.

**Command line.** Run `meshradio` with no flags to start the radio; the rest
are overrides and one-shot maintenance commands that act on the archive and
exit:

| Flag | What it does |
|---|---|
| `--config PATH` | use this config file (precedence above) |
| `--profile dev\|pi4\|lite` | override `hardware_profile` |
| `--port N` | override the web port (default 8080) |
| `--demo` | seed simulated channel traffic and playback — no yt-dlp/ffmpeg needed |
| `-v` | debug logging |
| `--list-backups`, `--restore-backup WHICH` | list / restore DB snapshots — see *Public hosting* |
| `--set-theme TITLE`, `--theme-date DATE` | retitle a day — see [Fixing a theme](#fixing-a-theme) |
| `--delete-track VIDEO`, `--track-date DATE` | drop a song from a day — see [Removing a song](#removing-a-song) |
| `--probe-feed [corescope\|comchan]` | poll an analyzer feed once and show what it answered — see *The backup feed* |
| `--hash-admin-password` | ask for an admin password and print the hash that turns on `/admin` — see [The admin page](#the-admin-page) |
| `--new-totp-secret` | print a secret for two-step admin sign-in |

---

## Using the web player

Open **http://localhost:8080** (or `http://<host-or-ip>:8080` from another
device on your LAN — e.g. `http://meshradio.local:8080` if the Pi's hostname is
`meshradio`).

- **Now Playing** — art, title, artist, and which mesh member shared it, for
  the latest day. It rolls forward to a new day on its own as that day's first
  songs arrive — an open tab updates with no reload. Controls: **▶/⏸
  play-pause**, **⏭ next track**, **🔀 shuffle** what's up next, **volume
  slider**, and (web mode) **📻 Start radio**. **⤴ Export** opens the whole
  day's songs as a YouTube playlist — every song for the day, whatever's
  playing.
- **Queue** — listed under the player. Select a track to reveal **⤒ Play next**
  and **✕ Remove** in the bar up top; **Clear queue** empties it (the current
  song keeps playing; radio mode switches off so it doesn't refill what you
  just cleared). A song already on the day's playlist won't be added twice, no
  matter how many people repost it, and a song that's already playing or
  queued isn't queued again however many times **+ queue** is pressed. A queue
  tops out at 200 songs (`[player] max_queue`); at that point only a fresh
  channel post still gets in, by displacing station filler.
- **Live jukebox** — when a new song lands on the channel it auto-plays if
  the radio is idle, or joins the queue if something's already playing. A new
  arrival never interrupts the current song.
- **Archive** — a month calendar; every day the channel played is a lit tile
  (theme + song count on hover), quiet days are blank. Tap a day for its
  theme → tracks: **▶ Play this day** replays a whole day in posted order,
  **+ queue** adds a single track. New visitors land with the newest day cued
  up so there's something to press play on.
  A day's page steps to the days either side of it, so you can read the archive
  straight through instead of going back to the calendar each time. Songs carry
  a small cover-art thumbnail on the day, search and member pages.
- **☰ All themes** — the calendar's companion view: every theme the channel has
  run, newest first, a year at a time, grouped by month, each linking to its
  day. A title used on more than one day carries an `N×` badge, so it's easy to
  see what's been done before picking tomorrow's. Days that never got a theme
  aren't listed.
- **Search** — find a song by title, artist, the member who shared it, or the
  theme it was shared under: any part of a word, in any case (`café` finds
  `CAFÉ`). The song whose title you actually typed comes first, then a title
  that starts with it, then one that contains it, then an artist, then the
  rows that matched only on a sharer or a theme; ties go to the most recent
  share. A row is a song, not a share, so a track posted on eight days is one
  line saying `shared 8× by 3 members` rather than eight lines burying
  everything else. Two dropdowns narrow it by **member** and **year**, and
  either works on its own — pick a member and a year with the box empty to see
  everything they shared that year. Both stay in the URL, so a narrowed
  search is a link you can paste. Results are cut off at 100 (the page says so
  when more match), and each links to its day with a **+ queue** button on any
  song that can be played. It's answered from an index, so it stays quick
  however large the archive grows.
- **Skins** — the header's dropdown re-dresses the player as **Winamp** (the
  default), **iTunes** or **Media Player**. The choice is kept in a cookie and
  applied on the server, so a page never flashes the wrong skin while it loads.
- **Keyboard** — <kbd>Space</kbd> (or <kbd>K</kbd>) play/pause, <kbd>N</kbd> next track,
  <kbd>←</kbd>/<kbd>→</kbd> jump back or forward 10s, <kbd>↑</kbd>/<kbd>↓</kbd>
  volume, <kbd>M</kbd> mute, <kbd>?</kbd> help. They drive the on-screen
  controls, so nothing gets out of step, and they keep out of the way while
  you're typing in the search box.
- **Lock-screen controls** — the tab that's playing shows the song (title,
  artist, cover, a scrub bar) on a phone's lock screen and in the notification
  shade, and headset buttons work too: play/pause, next, and scrubbing act like
  the on-screen controls, including while you're browsing the Archive. Only the
  speaker tab owns them, so a second tab doesn't fight for the lock screen.
  How much a given phone shows depends on its browser — in embed mode the audio
  plays inside YouTube's frame, which the page can't fully speak for.
- **Stats** — the channel's numbers: songs, shares, sharers, days, themes and
  plays, plus top sharers, top artists, most-shared songs, and the busiest themes.
- **Member pages** — every sharer's name is a link: what they've shared, the
  days they named, the artists they keep coming back to, and the span they've
  been on the channel.
- **Artist pages** — every artist name is a link too: their songs (most-shared
  first), who on the channel posts them most, and the themes they turned up in.
  A YouTube Music share names an auto-generated "Artist - Topic" channel; the
  suffix is folded away, so it lands on the same page as a plain video link.
- **Weekly recap** — `/week` sums up the newest week (Sunday to Saturday, like
  the calendar's rows): each day's theme, the busiest day, the top sharers and
  artists, who shared for the first time, and songs the channel had heard
  before. Step back through earlier weeks, or reach one from any day page.
  `/weekly.xml` is the same recap as an Atom feed, one entry per finished week,
  for a digest instead of a post a day.
- **Feed** — `/feed.xml` is an Atom feed of the last 30 days, one entry per
  day: the theme, the songs (each linked), and the day's first cover. Subscribe
  in any feed reader to hear the day's theme without opening the site; every
  page advertises it, so most readers find it from the site's address alone.
- **Shareable links** — a day pasted into a chat unfurls with its theme, song
  count, and cover art, so a link to `/archive/2026-08-11` says something
  before anyone clicks it. On an iPhone, **Add to Home Screen** names the app
  *MeshRadio* and uses the logo as its icon, not the day's theme.
- **🎲 Keep playing** — never run out: when the queue empties, this keeps the
  music going with random songs pulled from the archive. Unlike **Start radio**
  it needs no YouTube access, so it's the "don't stop at the end of the day"
  button that works on the public embed instance too. Filler songs show an
  `archive` badge in the queue; a freshly posted channel song still jumps ahead
  of them. Press **◼ Keep playing** to stop.
- **📻 Start radio** — when the queue runs dry, this seeds a "station" from
  the current (or last-played) track using its YouTube Mix: similar songs are
  fetched, cached, and queued, and the station keeps topping itself up until
  you press **◼ Radio on** to stop. Radio tracks show a `radio` badge in the
  queue and never pollute the channel archive.
- **10-band EQ + spectrum analyzer** — a real graphic equalizer with classic
  presets and an FFT spectrum display, Winamp-style. (Web-playback mode only;
  the embed player is the plain YouTube stream, so the panel isn't shown
  there.)
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

`push_url` has to be `https://`: the token rides on every push, so plain
`http://` is accepted only for `localhost` / `127.0.0.1` / `::1` (a dev receiver
on the same machine). Anything else is logged as `relay disabled: …` at startup
and the relay stays off — the radio itself keeps running.

The relay is self-healing: each push reports the receiver's track count, and
when it drops below the home node's (e.g. a fresh host with an empty disk) the
pusher resets its cursor and re-backfills the whole channel automatically. This
is now a safety net rather than the norm — the hosted instance keeps its
archive on a persistent disk (`disk:` in [render.yaml](render.yaml)), so
history survives deploys, restarts, and spin-downs on its own, and the host
also polls CoreScope directly. The relay going down
no longer costs the archive. `/healthz` exposes liveness plus ingest freshness
(Render's health check hits it; a stale `ingest_age_s` means every ingest
source stopped).

The DB is also snapshotted on a rotation (`[backup]` config): a copy is taken
before migrations on each boot and every few hours after, so a bad migration or
corruption has a clean rollback point. Each migration also runs as a single
transaction, so one that fails part-way leaves the archive exactly as it was
rather than half-converted. Snapshots default to `<data_dir>/backups`;
for whole-disk loss, pair them with host-level disk snapshots (Render takes
automatic daily ones on paid instances) or set `[backup].dir` to separate storage.
To restore, stop the service and run `meshradio --list-backups` then
`meshradio --restore-backup latest` (or a specific filename/path); it snapshots
the current DB first, so a restore is itself reversible.

The Pi runs under systemd — see [deploy/meshradio.service](deploy/meshradio.service)
for the unit and install/update commands. The unit sandboxes the service: it
runs yt-dlp, ffmpeg and deno against whatever the channel links to, so the
file system is read-only to it apart from `/var/lib/meshradio` (where
`data_dir` belongs), its cache directory and a private `/tmp`, with no
capabilities, no setuid, no kernel knobs and a memory ceiling. The tokens
go in `/etc/meshradio/env` (`MESHRADIO_RELAY_TOKEN`, and
`MESHRADIO_INGEST_TOKEN` if the node is itself a receiver), mode 0600, which
the unit reads as an `EnvironmentFile`.

### Hardening

The player has no login, so the web layer assumes no other site's page should be
able to drive it:

- **Cross-site requests are refused.** Every POST and WebSocket handshake has to
  come from a page this server served (its `Origin` must match `Host`); a
  different or `null` origin, or `Sec-Fetch-Site: cross-site`, gets a 403. Reads
  stay open, so an archive link pasted into a chat still works, and non-browser
  callers (curl, the relay) send no `Origin` and are unaffected.
- **Sessions open only for a cookie this server signed.** On the public embed
  host every browser gets its own session player, and a session costs a
  player, a task and a row on disk — so the cookie that names one carries a
  signature, and a press or a WebSocket that arrives without one (no cookie,
  a forged one, a bot spraying requests) acts on a throwaway preview instead
  of opening anything. A real browser always carries the cookie it got with
  the page. The signing key lives in the archive, so cookies outlive a
  redeploy the way the sessions they name do. Static files, audio, the health
  check, the feed, the sitemap and the relay endpoint carry no cookie at all.
- **Sockets are counted.** A session may hold 8 open WebSockets (a visitor's
  tabs), the communal appliance player 64, and the process 1,024 in all; past
  that a handshake is closed with "try again later" rather than accepted. A
  page may claim the speaker role once a second, and a claim from the page
  that already has it is ignored, since each claim re-sends state to every
  open socket.
- **Security headers and a Content-Security-Policy** go on every response: no
  inline script or style, YouTube's stills and player as the only third party,
  the WebSocket allowed to this host alone, `nosniff`, a strict referrer policy,
  `X-Frame-Options: SAMEORIGIN`, a same-origin opener policy, and a permissions
  policy that turns off the camera, microphone, location and payment APIs no
  page here uses. A site named with an `https://` `public_url` also sends
  `Strict-Transport-Security`. Set `[web] security_headers = false` if a proxy
  in front already sets them, or `csp_report_only = true` to try a policy change
  out — the browser console then reports what it would have blocked, without
  blocking it.
- **Presses and searches are rate-limited per client**: thirty presses then
  ten a second, fifteen searches then three a second; past that a 429 and the
  page stays as it was. Reads are never limited. Behind a reverse proxy, name
  it in `[web] trusted_proxies` (`["*"]` on a host like Render, where the proxy
  is the only way in) so the limiter sees visitors' real addresses and an
  `X-Forwarded-Proto` is believed; a header from any other peer is ignored.
- **Pin the host name** with `[web] allowed_hosts` (`["meshradio.local",
  "192.168.1.20"]`) to keep DNS-rebinding pages away from a LAN radio. Empty
  means any host, which an appliance reached by IP, `.local` name and
  port-forward all need.
- **Name the site** with `[web] public_url` on a public host, so canonical links,
  link previews and the sitemap come from config rather than the request's
  `Host` header.
- **Inputs are bounded.** `/api/ingest` takes at most 16 MiB / 5,000 messages per
  push, an analyzer response past 64 MiB is abandoned, and seek or duration
  values that are not finite (or run past a day) are rejected. Free text is
  bounded where rows are written, whatever fed it (mesh, analyzer, relay,
  yt-dlp): titles and artists are one line of at most 256 characters, sender
  names 64, with control characters dropped, and a track length that isn't a
  finite number of seconds is simply not stored — so a relay's metadata can't
  plant a value that breaks every page showing the day.

### The admin page

`/admin` does in a browser what the maintenance flags below do over SSH: name
or fix a day's theme, take a song off a day (and put it back), correct a
song's title or artist, merge artist spellings ("Beatles", "The Beatles - Topic")
into one, check and probe the feeds, and take or download backups. It's off
until you give it a password:

```
meshradio --hash-admin-password      # asks twice, prints scrypt$16384$8$1$…
```

Set what it prints as `MESHRADIO_ADMIN_PASSWORD_HASH` (Render: the service's
Environment settings; the Pi: `/etc/meshradio/env`) and restart. Only the hash
is stored, and a new one signs every browser out. For two-step sign-in, run
`meshradio --new-totp-secret`, add the secret to an authenticator app, and set
it as `MESHRADIO_ADMIN_TOTP_SECRET`. Without a hash every `/admin` URL is a 404.

What keeps it safe:

- **Sign-in.** The sign-in page asks only for the password, and looks the
  same whether or not two-step is on: the code is asked for on a page of its
  own that only a right password opens (for five minutes, three codes, from
  the same address). A wrong password is logged, and a wrong code is logged
  apart ("Right password, wrong code"), since it means the password is known.
  Five failures from one address pause sign-in from it for 15 minutes, and 50
  from all addresses together pause it for everyone, so guesses spread over
  many addresses still meet a limit. Each attempt counts before its answer is
  known, so parallel guesses can't slip past, and a right password doesn't
  reset the count until the code is right too. A used code stays used across
  a restart. Signing in sets a cookie scoped to
  `/admin` (HttpOnly, `SameSite=Strict`, Secure over https) that lasts at most
  12 hours and ends after 30 idle minutes; the archive keeps only its hash.
  Every form carries a CSRF token on top of the cross-site guard every POST
  already passes, and admin pages are `no-store` and `noindex`.
- **The activity log** (`/admin/log`) records every sign-in and every change,
  with the value before and after, for a year — including fixes made with
  `--set-theme` and `--delete-track`, marked CLI. Renames, removals, song edits
  and artist merges each carry an **Undo**, which adds an entry rather than
  erasing one.
- **Removing a song** shows what goes (the song, its plays) and needs the day's
  date typed to confirm. A backup is taken first (at most one every ten
  minutes, so a clean-up doesn't rotate the scheduled ones out). The removal
  keeps the song's details, so **Put back** under Removed songs restores it
  exactly, plays aside.
- **A hand-edited title or artist stays**: a late YouTube lookup or a relay
  re-push won't replace it. A merged artist spelling is remembered, so a song
  arriving later with it is respelled as it's stored.
- **Restoring a backup stays on the command line**, because it needs the
  service stopped; the Backups screen prints the command.
- **Settings** (`/admin/config`) has switches, sliders and time pickers for
  the ones that are safe to change from a browser: auto-play, quiet hours,
  starting volume, the live window, queue and top-up sizes, feed and relay
  intervals, the audio cache, download retries, and backups. Each says when
  it takes effect (right away, from the next check, or after a restart, with
  a banner listing what's waiting). A change is kept in the archive and laid
  over the config file at startup, so it survives restarts; **Use the
  file's** drops it again, and every change is in the activity log with an
  Undo. Secrets, addresses the server connects to, commands it runs, paths,
  and the site's own defences (security headers, rate limits, proxies,
  allowed hosts) stay read-only there, with secrets cut to their last four
  characters. On the public site the device settings (mesh node, audio cache,
  quiet hours, volume) aren't shown, and the **Device** screen (yt-dlp, cache
  use, failed downloads with Retry, the audio output) exists only on the Pi.

Each instance keeps its own archive and its own admin page: a fix made on the
public site stays there, and one made on the Pi stays on the Pi (a removal
isn't relayed, and a receiver ignores a relayed rename for a day it has
already locked).

### Fixing a theme

A day's theme locks on the channel's first `Theme:` post, so a bad title —
a typo, or a parse that split on the wrong colon (`theme is: planes :-) or
trains?`) — can't be corrected by posting again. Retitle it on the admin page's day screen, or directly:

```
meshradio --set-theme "water"                          # today
meshradio --set-theme "water" --theme-date 2026-07-06  # any day
```

It renames the day's existing theme in place, so the songs already filed
under it stay put, and leaves it locked. If the day has no theme yet, it
creates one, and links arriving later that day attach to it.

This edits whichever archive the config points at, and a rename does **not**
travel over the relay — the hosted receiver has its own locked theme for that
day and ignores the replayed message, exactly like a corrected repost on the
channel. Run it once per instance you want fixed (the Pi and the host).

### Removing a song

The queue's **✕ Remove** takes a song out of what's playing; it stays in the
day's playlist and comes back next time the day is played. To drop it from the
archive itself — a song posted before anyone set the theme, or posted to the
wrong day — remove it from the admin page's day screen, or by video id or link:

```
meshradio --delete-track "https://youtu.be/VIDEOID"           # today
meshradio --delete-track VIDEOID --track-date 2026-07-06      # any day
```

Get the id wrong and it prints that day's songs so you can pick one. The song
and its play history go, and so does its cached audio file if this node
downloaded one.

The removal sticks. The link is still on the channel, so ingest would keep
offering it back (a repost is a dedupe no-op, and the relay re-backfills a
receiver it thinks was wiped) — the archive records the day and video it
dropped and ignores the message from then on, the same way a locked theme
ignores a corrected repost. The tombstone is scoped to that day only: if
someone shares the song again next week it files normally.

If the song was the only thing on an `Untitled — <date>` placeholder, the
empty placeholder goes too, so the day doesn't sit lit but silent on the
calendar. A theme somebody actually named stays, empty or not.

Like `--set-theme`, this edits one archive — run it on each instance you want
fixed (the Pi and the host). A song already sitting in a running player's queue
plays out until the service restarts.

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
playing. Update it with `pip install -U yt-dlp` (or `uv pip install -U
yt-dlp`), or let the appliance do it nightly: install
[deploy/meshradio-ytdlp-update.timer](deploy/meshradio-ytdlp-update.service)
(the comments there say how), which runs `deploy/update-ytdlp.sh` against the
venv at 04:30. No restart is needed — yt-dlp is a subprocess, so the next
download uses the new version — and `/healthz` reports `ytdlp_version` so you
can see it took. Embed mode sidesteps this entirely (no downloads).

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
| Backup analyzer feed (analyzer.comchan.net) | ✅ working, tested — API shape confirmed from the analyzer's source, paged backfill, `--probe-feed` for a live check |
| Cache-first downloader (yt-dlp) + self-healing retries | ✅ working, tested |
| Player: live policy, queue, archive replay, quiet hours | ✅ working, tested |
| Web player (browser audio, radio-station mode, EQ/analyzer) | ✅ working, tested |
| **Embed mode + per-visitor sessions (public hosting)** | ✅ working, deployed on Render |
| **Relay (home node → hosted instance) + auto-backfill** | ✅ working, running on the Pi |
| **Supervised runtime, CI gate, `/healthz`** | ✅ working |
| Archive browsing: calendar, all-themes list, search, stats, member pages, Atom feed, link previews | ✅ working, tested |
| DB snapshots + restore, `--set-theme` / `--delete-track` operator fixes | ✅ working, tested |
| Admin page: sign-in (+ two-step), activity log with undo, theme/song fixes, artist merges, feeds, backups | ✅ working, tested |
| Hardening: cross-site guard, CSP + security headers, https-only relay | ✅ working, tested |
| Mesh serial ingestion (meshcore) | 🟡 built, needs validation on a Heltec V3 |
| OLED panel + encoder/buttons | 🟡 skeleton, needs hardware bring-up |
| PipeWire routing (pi4/lite backends) | 🟡 built, needs hardware bring-up |
| Bluetooth pairing (BlueZ) | ⬜ interface stubbed |
| UPS fuel gauge / safe shutdown | ⬜ stubbed |
| First-boot provisioning, pi-gen image, STLs, BOM docs | ⬜ not started (only the config-writing helper in `system/provision.py` exists) |

## Layout

```
meshradio/
├── app.py           # asyncio entrypoint (run): wires modules to the bus
├── cli.py           # the `meshradio` command: start the radio, or one maintenance task
├── bus.py           # tiny pub/sub EventBus + event vocabulary
├── config.py        # TOML config over dataclass defaults, checked at load (+ env secrets)
├── config_overrides.py  # settings the admin page may change, kept in the archive
├── db/              # aiosqlite layer behind one Database facade: core.py (connection,
│                    #   transactions, the migrations.py runner), fields.py (text bounds),
│                    #   and query mixins — themes, tracks, archive, browse, relay, web_sessions,
│                    #   admin
├── backup.py        # rotating DB snapshots + --list-backups / --restore-backup
├── net.py           # shared outbound HTTP client setup (User-Agent, timeouts)
├── runtime.py       # supervised task/Service runtime (restart-with-backoff)
├── ingest/          # parse.py (pure), service.py, mesh.py, corescope.py, relay.py
├── media/           # cacher.py (yt-dlp), backends.py (mpv | web | embed | null),
│                    #   player.py (queue + live policy), radio.py (YT Mix), metadata.py
├── audio/           # routing.py (PipeWire/wpctl, per-profile), bluetooth.py
├── ui/              # panel.py (OLED + controls; log panel on dev)
├── system/          # power.py (fuel gauge), provision.py (first boot)
└── web/             # FastAPI app split into:
    ├── server.py        # create_app: assembly, lifespan, sessions, origin guard, CSP headers
    ├── context.py       # WebContext shared state on app.state (+ short-TTL archive caches)
    ├── sessions.py      # per-visitor session players + speaker registry
    ├── routes_pages.py  # HTML pages (now playing, archive, search, stats, members, artists, weeks…) + htmx partials
    ├── routes_api.py    # player/queue control API
    ├── routes_ingest.py # /audio streaming, relay /api/ingest, /healthz
    ├── routes_admin.py  # /admin: overview, days, removals, artists, feeds, backups, log, config
    ├── admin_auth.py    # admin sign-in: scrypt password hash, two-step codes, lockout, CSRF
    ├── ws.py            # WebSocket: forwards bus events → htmx re-fetch
    ├── feed.py          # /feed.xml Atom builder (pure; safe against hostile text)
    ├── recap.py         # weekly recap summary + /weekly.xml builder (pure)
    ├── static/          # vendored htmx, style.css, admin.css, icons, and js/ — radio (socket +
    │                    #   audio), embed, eq, playbar, queue, keys, mediasession, nav, help,
    │                    #   skin, fx, admin
    └── templates/       # Jinja2 (base, index, archive*, search, stats, member, artist, week,
                         #   about, partials/, admin/)

deploy/meshradio.service   # systemd unit for the Pi relay
render.yaml + *.render.toml # public embed-mode deployment
.github/workflows/test.yml  # CI; Render deploys main only after it's green
```

Dev without media tooling: `pip install -e .` and run with `--demo` for
simulated traffic and playback (no yt-dlp/ffmpeg needed). On the appliance,
add hardware extras: `pip install -e ".[media,hw]"`.
