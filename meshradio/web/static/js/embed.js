// Embed mode: the speaker tab streams via the YouTube IFrame player.
// No audio touches the server; the tab reports 'ended' and real durations
// back, and follows seeks/pauses from the shared state.
let ytPlayer = null, ytApiRequested = false, ytCurrentVid = null, embedTrackId = null;

function loadYtApi() {
  if (ytApiRequested) return;
  ytApiRequested = true;
  const s = document.createElement("script");
  s.src = "https://www.youtube.com/iframe_api";
  document.head.appendChild(s);
}
window.onYouTubeIframeAPIReady = () => { if (lastState) applyEmbed(lastState); };

function ytNudgePlay() {
  try { ytPlayer.playVideo(); } catch (e) {}
  setTimeout(() => {                 // autoplay refused without a gesture?
    try {
      const st = ytPlayer.getPlayerState();
      if (lastState && lastState.status === "playing" &&
          st !== YT.PlayerState.PLAYING && st !== YT.PlayerState.BUFFERING)
        showCtl("embed", "▶ Click to start playback");
    } catch (e) {}
  }, 800);
}

function onYtError(e) {
  // 101/150 = embedding disabled, 100 = removed, 2/5 = bad id/HTML5 error.
  // Whatever it is, this video won't play here — skip instead of stalling
  // the session until someone presses next.
  if (embedTrackId !== null) {
    fetch("/api/ended/" + embedTrackId, { method: "POST" });
  }
}

function onYtState(e) {
  if (e.data === YT.PlayerState.ENDED && embedTrackId !== null) {
    fetch("/api/ended/" + embedTrackId, { method: "POST" });
  } else if (e.data === YT.PlayerState.PLAYING) {
    audioCtl.hidden = true;
    const cur = lastState && lastState.current;
    const d = ytPlayer.getDuration();
    if (cur && embedTrackId === cur.id && !cur.duration && d > 0)
      fetch("/api/duration/" + embedTrackId + "/" + d, { method: "POST" });
  }
}

function applyEmbed(s) {
  const win = document.getElementById("yt-window");
  const eqWin = document.getElementById("eq-window");
  if (eqWin) eqWin.style.display = "none";   // EQ can't reach iframe audio
  if (!win) return;
  if (!s.speaker) {
    win.hidden = true;
    embedTrackId = null;
    if (ytPlayer) {
      try { ytPlayer.destroy(); } catch (e) {}
      ytPlayer = null;
      ytCurrentVid = null;
      win.innerHTML = '<div id="yt-player"></div>';
    }
    if (s.status === "playing") showCtl("claim", "🔊 Play here in this tab");
    else audioCtl.hidden = true;
    return;
  }
  const hasTrack = s.current && (s.status === "playing" || s.status === "paused");
  win.hidden = !hasTrack;
  if (!hasTrack) {
    embedTrackId = null;
    ytCurrentVid = null;
    if (ytPlayer) { try { ytPlayer.stopVideo(); } catch (e) {} }
    audioCtl.hidden = true;
    return;
  }
  if (!window.YT || !window.YT.Player) { loadYtApi(); return; }  // resumes via onYouTubeIframeAPIReady
  if (!ytPlayer) {
    ytCurrentVid = s.current.video_id;
    embedTrackId = s.current.id;
    ytPlayer = new YT.Player("yt-player", {
      videoId: ytCurrentVid,
      playerVars: { autoplay: 1, rel: 0, start: Math.floor(s.position || 0) },
      events: {
        onReady: () => {
          try { ytPlayer.setVolume(s.volume); } catch (e) {}
          if (s.status === "playing") ytNudgePlay();
        },
        onStateChange: onYtState,
        onError: onYtError,
      },
    });
    return;
  }
  try {
    ytPlayer.setVolume(s.volume);
    if (ytCurrentVid !== s.current.video_id) {
      ytCurrentVid = s.current.video_id;
      embedTrackId = s.current.id;
      ytPlayer.loadVideoById(ytCurrentVid, s.position || 0);
      if (s.status === "playing") ytNudgePlay();
    } else if (s.status === "paused") {
      embedTrackId = s.current.id;
      ytPlayer.pauseVideo();
    } else {
      embedTrackId = s.current.id;
      if (Math.abs((ytPlayer.getCurrentTime() || 0) - s.position) > 3)
        ytPlayer.seekTo(s.position, true);       // a remote tab scrubbed
      ytNudgePlay();
    }
  } catch (e) {}
}
