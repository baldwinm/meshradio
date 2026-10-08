// Song lengths for the queue's total (embed hosting).
//
// The server can't always learn a song's length: it never downloads, oEmbed
// has no length, and YouTube may refuse its own lookups from a datacenter.
// This browser can ask YouTube directly, so it does: a hidden, silent IFrame
// player cues each queued song that has no length, reads it, and reports it
// through the same /api/duration route the speaker tab uses once a song
// plays. The server fills the shared row (blanks only) and every open queue
// updates.
//
// Cueing usually yields the length at once. When it doesn't, the hidden
// player starts the video muted, reads the length the moment it plays, and
// stops it. A reading is only taken while the hidden player holds the video
// asked about, so a late event from the previous song can't be misreported.
//
// Listens for radio.js's ``meshradio:state`` (like mediasession.js), so a
// fault here can't touch playback. Needs embed.js's loadYtApi.
(function () {
  var asked = {};            // video ids this page has already tried
  var todo = [];             // tracks waiting for a probe
  var probe = null;          // the hidden YT.Player
  var probing = null;        // the track being measured
  var giveUp = null;         // timer that moves on from a song that won't answer
  var PLAYING = 1, CUED = 5;

  function sync() {
    var s = lastState;
    if (!s || !s.embed) return;
    [s.current].concat(s.queue || []).forEach(function (t) {
      if (t && !t.duration && !asked[t.video_id]) {
        asked[t.video_id] = true;
        todo.push({ id: t.id, video_id: t.video_id });
      }
    });
    next();
  }

  function next() {
    if (probing || !todo.length) return;
    if (!window.YT || !window.YT.Player) {   // embed.js loads the API on demand
      loadYtApi();
      setTimeout(next, 500);
      return;
    }
    probing = todo.shift();
    giveUp = setTimeout(function () { finish(probing, 0); }, 15000);
    if (probe) {
      probe.cueVideoById(probing.video_id);
      return;
    }
    var box = document.createElement("div");
    box.id = "yt-probe";
    box.setAttribute("aria-hidden", "true");
    var slot = document.createElement("div");
    box.appendChild(slot);
    document.body.appendChild(box);      // outside <main>: survives hx-boost swaps
    probe = new YT.Player(slot, {
      width: 200,
      height: 200,
      videoId: probing.video_id,
      playerVars: { autoplay: 0, controls: 0, playsinline: 1, rel: 0 },
      events: {
        onReady: function () { poll(probing, 8); },
        onStateChange: onState,
        onError: function () { finish(probing, 0); },
      },
    });
  }

  // The length, if the hidden player holds `track` and knows it; else 0.
  function read(track) {
    try {
      if (!track || probing !== track) return 0;
      if (probe.getVideoData().video_id !== track.video_id) return 0;
      return probe.getDuration() || 0;
    } catch (e) {
      return 0;
    }
  }

  function poll(track, tries) {
    if (probing !== track) return;
    var d = read(track);
    if (d > 0) return finish(track, d);
    if (tries > 0) {
      setTimeout(function () { poll(track, tries - 1); }, 250);
      return;
    }
    // Cued but still no length: some players only learn it once the video
    // starts. Start it silently and read it on PLAYING.
    try { probe.mute(); probe.playVideo(); } catch (e) { finish(track, 0); }
  }

  function onState(e) {
    var track = probing;
    if (e.data === CUED) poll(track, 8);
    else if (e.data === PLAYING) {
      var d = read(track);
      if (d > 0) finish(track, d);
    }
  }

  function finish(track, d) {
    if (!track || probing !== track) return;
    clearTimeout(giveUp);
    probing = null;
    try { probe.stopVideo(); } catch (e) {}
    if (d > 0) {
      fetch("/api/duration/" + track.id + "/" + d, { method: "POST" }).catch(function () {});
    }
    setTimeout(next, 300);
  }

  document.body.addEventListener("meshradio:state", sync);
  sync();
})();
