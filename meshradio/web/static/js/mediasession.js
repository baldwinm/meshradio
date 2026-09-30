// Lock-screen, notification-shade and headset controls (Media Session API).
//
// Whatever tab is making the sound tells the OS what's playing — title,
// artist, the still, how long it runs — and the OS hands back play, pause,
// skip and scrub presses. Everything the OS needs is already in the state
// radio.js receives, so this only translates it.
//
// Two decisions worth knowing:
//
// * The handlers POST to the same endpoints the on-screen buttons do instead
//   of clicking those buttons (which is what keys.js does). The buttons exist
//   only on Now Playing, but hx-boost keeps the music going while you browse
//   the Archive — a lock-screen "pause" that looked for a button on the page
//   would silently do nothing there. The server is the source of truth and
//   pushes the result back to every tab, so nothing can disagree.
//
// * It listens for radio.js's ``meshradio:state`` event rather than being
//   called from applyState: a cosmetic feature must not be able to break
//   playback, and an exception in a listener never reaches the dispatcher.
//
// Only the speaker tab owns the session. A silent remote tab that claimed
// "playing" would fight it for the lock screen, so those clear theirs. The
// appliance's mpv backend has no browser audio at all, so there is nothing
// for a page to describe.
(function () {
  if (!("mediaSession" in navigator)) return;
  var ms = navigator.mediaSession;

  var shown = "";                       // what the OS is currently showing, to skip no-op updates
  var synced = null;                    // the state object last pushed to the OS

  function post(url) {
    fetch(url, { method: "POST" }).catch(function () {});
  }

  // The OS "play" also arrives when the server already says playing but this
  // tab's audio is stopped — the browser blocked autoplay. A media-key press
  // counts as a gesture, so resume locally instead of toggling the server
  // (which would pause everyone).
  function resumeHere() {
    try { if (audio.src && audio.paused) audio.play().catch(function () {}); } catch (e) {}
    try { if (typeof ytPlayer !== "undefined" && ytPlayer) ytPlayer.playVideo(); } catch (e) {}
  }

  function handler(name, fn) {
    // Not every browser knows every action, and an unknown one throws.
    try { ms.setActionHandler(name, fn); } catch (e) {}
  }

  handler("play", function () {
    if (!lastState) return;
    if (lastState.status === "paused") post("/api/pause");   // toggle: server resumes, pushes state
    else resumeHere();
  });
  handler("pause", function () {
    if (lastState && lastState.status === "playing") post("/api/pause");
  });
  handler("nexttrack", function () { post("/api/skip"); });
  handler("seekto", function (details) {
    if (details && isFinite(details.seekTime)) seekTo(Math.max(0, details.seekTime));   // playbar.js
  });

  function clear() {
    shown = "";
    ms.metadata = null;
    ms.playbackState = "none";
    try { ms.setPositionState(); } catch (e) {}
  }

  function sync() {
    var s = lastState;                  // radio.js: the newest player.state
    // meshradio:state also fires for power and output pushes, where lastState
    // is unchanged and its position is stale — re-sending it would yank the
    // lock-screen scrubber backwards. A new player.state is a new object.
    if (!s || s === synced) return;
    synced = s;
    var cur = s.current;
    if (!(s.web_audio || s.embed) || !s.speaker || !cur || s.status === "idle") {
      if (shown) clear();
      return;
    }

    // Title and artist arrive late for a fresh embed track (oEmbed), so the key
    // covers them: a same-id update still refreshes the lock screen.
    var artist = cur.artist || (cur.sender ? "shared by " + cur.sender : "");
    var key = [cur.id, cur.title, artist].join("\u0001");
    if (key !== shown) {
      shown = key;
      ms.metadata = new MediaMetadata({
        title: cur.title || cur.video_id,
        artist: artist,
        album: "MeshRadio",
        artwork: [{
          src: "https://i.ytimg.com/vi/" + encodeURIComponent(cur.video_id) + "/mqdefault.jpg",
          sizes: "320x180",
          type: "image/jpeg",
        }],
      });
    }
    ms.playbackState = s.status === "playing" ? "playing" : "paused";

    // A scrub bar on the lock screen needs a duration; embed tracks start
    // without one until the player reports it. setPositionState throws on an
    // impossible pair, so only hand it a sane one.
    try {
      var d = cur.duration;
      if (d > 0 && isFinite(d)) {
        ms.setPositionState({
          duration: d,
          playbackRate: 1,
          position: Math.min(Math.max(s.position || 0, 0), d),
        });
      } else {
        ms.setPositionState();
      }
    } catch (e) {}
  }

  document.body.addEventListener("meshradio:state", sync);
})();
