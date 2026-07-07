// Core client state: the <audio> element, speaker role, WebSocket link.
//
// These files are plain classic scripts sharing top-level bindings — no
// build chain (architecture §9). Load order (radio → embed → eq → playbar)
// matters only for top-level code; cross-file calls all happen after load.
//
// Web playback: exactly ONE connected page is the "speaker" (the server
// elects the newest connection; the button below claims the role). The
// speaker's <audio> element streams the cached file; on 'ended' it tells
// the server to advance, guarded server-side by track id so duplicate
// signals can't double-skip.
const audio = new Audio();
const audioCtl = document.getElementById("audio-ctl");
let currentTrackId = null;
let socket = null;
let lastState = null;

audioCtl.addEventListener("click", () => {
  if (audioCtl.dataset.action === "claim" && socket) {
    socket.send("claim");           // server re-elects and pushes new state
  } else if (audioCtl.dataset.action === "embed" && ytPlayer) {
    try { ytPlayer.playVideo(); } catch (e) {}
  } else {
    audio.play().catch(() => {});   // autoplay was blocked; this click unblocks
    if (audioCtx) audioCtx.resume().catch(() => {});
  }
  audioCtl.hidden = true;
});

function showCtl(action, label) {
  audioCtl.dataset.action = action;
  audioCtl.textContent = label;
  audioCtl.hidden = false;
}

function applyState(s) {
  lastState = s;
  if (s.embed) { applyEmbed(s); return; }
  if (!s.web_audio) return;
  if (!s.speaker) {
    currentTrackId = null;
    audio.pause();
    audio.removeAttribute("src");
    if (s.status === "playing") showCtl("claim", "🔊 Play audio in this tab");
    else audioCtl.hidden = true;
    return;
  }
  audio.volume = s.volume / 100;
  if (s.status === "playing" && s.current) {
    if (currentTrackId !== s.current.id) {
      currentTrackId = s.current.id;
      audio.src = "/audio/" + s.current.id;
      audio.onended = () => fetch("/api/ended/" + currentTrackId, { method: "POST" });
    } else if (audio.readyState > 0 && Math.abs(audio.currentTime - s.position) > 3) {
      audio.currentTime = s.position;  // a remote tab scrubbed; follow it
    }
    // Browsers block autoplay before the first user gesture; offer a button.
    audio.play().then(() => { audioCtl.hidden = true; activateGraph(); })
                .catch(() => { showCtl("enable", "🔊 Click to enable audio"); });
  } else if (s.status === "paused") {
    audio.pause();
  } else {
    currentTrackId = null;
    audio.pause();
    audio.removeAttribute("src");
    audioCtl.hidden = true;
  }
}

// Live state: drive the audio element and nudge htmx containers to re-fetch.
(function connect() {
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  socket = ws;
  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.topic === "player.state") { trackPos(msg.data); applyState(msg.data); }
    document.body.dispatchEvent(new Event("meshradio:state"));
  };
  ws.onclose = () => { socket = null; setTimeout(connect, 3000); };
})();
