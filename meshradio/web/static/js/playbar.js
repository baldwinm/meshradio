// Play bar. The speaker tab reads its own <audio> element (exact); the
// embed speaker reads its iframe player; remote tabs extrapolate from the
// last server state + wall clock. The htmx swap replaces the bar's DOM on
// every state event, so the ticker re-queries elements each time and
// handlers live inline in the template.
let lastPos = { value: 0, at: Date.now(), playing: false, duration: 0 };

function trackPos(s) {
  lastPos = {
    value: s.position || 0,
    at: Date.now(),
    playing: s.status === "playing",
    duration: (s.current && s.current.duration) || 0,
  };
}

function fmtTime(s) {
  s = Math.max(0, Math.round(s));
  const m = Math.floor(s / 60) % 60, h = Math.floor(s / 3600), sec = String(s % 60).padStart(2, "0");
  return h ? h + ":" + String(m).padStart(2, "0") + ":" + sec : m + ":" + sec;
}

function paintBar(scrub, pos, dur) {
  const elapsed = document.getElementById("pb-elapsed");
  const remaining = document.getElementById("pb-remaining");
  if (elapsed) elapsed.textContent = fmtTime(pos);
  if (remaining) remaining.textContent = dur ? "-" + fmtTime(dur - pos) : "";
}

setInterval(() => {
  const scrub = document.getElementById("pb-scrub");
  if (!scrub || scrub.dataset.seeking) return;
  let pos = null, dur = null;
  if (currentTrackId !== null && audio.src) {          // this tab is the speaker
    pos = audio.currentTime;
    dur = audio.duration || lastPos.duration;
  } else if (ytPlayer && embedTrackId !== null) {      // embed-mode speaker
    try {
      pos = ytPlayer.getCurrentTime() || 0;
      dur = ytPlayer.getDuration() || lastPos.duration;
    } catch (e) { pos = null; }
  }
  if (pos === null) {                                  // remote: extrapolate
    pos = lastPos.value + (lastPos.playing ? (Date.now() - lastPos.at) / 1000 : 0);
    dur = lastPos.duration;
  }
  if (dur) pos = Math.min(pos, dur);
  if (dur && +scrub.max !== Math.round(dur)) scrub.max = Math.round(dur);
  scrub.value = Math.round(pos);
  paintBar(scrub, pos, dur);
}, 500);

function scrubPreview(el) {                            // dragging: preview only
  el.dataset.seeking = "1";
  paintBar(el, +el.value, +el.max);
}

function scrubCommit(el) {                             // released: actually seek
  delete el.dataset.seeking;
  const pos = +el.value;
  if (currentTrackId !== null && audio.src) audio.currentTime = pos;
  if (ytPlayer && embedTrackId !== null) { try { ytPlayer.seekTo(pos, true); } catch (e) {} }
  fetch("/api/seek/" + pos, { method: "POST" });       // keep server clock + remotes in sync
}
