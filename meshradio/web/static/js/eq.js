// EQ + visualizer (Web Audio; lives in the speaker tab, which IS the sound
// card in web-playback mode). Once the <audio> element is wired into the
// graph its output only reaches the speakers through it, so the graph is
// built lazily on first playback and resumed on user gesture.
const EQ_FREQS = [60, 170, 310, 600, 1000, 3000, 6000, 12000, 14000, 16000];
let audioCtx = null, analyser = null, preampNode = null;
const eqFilters = [];
let eq = { on: true, preamp: 0, bands: EQ_FREQS.map(() => 0) };
try { eq = Object.assign(eq, JSON.parse(localStorage.getItem("meshradio-eq") || "{}")); } catch (e) {}

function dbGain(db) { return Math.pow(10, db / 20); }

function ensureGraph() {
  if (audioCtx) return;
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  let node = audioCtx.createMediaElementSource(audio);
  preampNode = audioCtx.createGain();
  node.connect(preampNode);
  node = preampNode;
  EQ_FREQS.forEach((freq, i) => {
    const f = audioCtx.createBiquadFilter();
    f.type = i === 0 ? "lowshelf" : i === EQ_FREQS.length - 1 ? "highshelf" : "peaking";
    f.frequency.value = freq;
    if (f.type === "peaking") f.Q.value = 1.1;
    node.connect(f);
    node = f;
    eqFilters.push(f);
  });
  analyser = audioCtx.createAnalyser();
  analyser.fftSize = 512;
  analyser.smoothingTimeConstant = 0.72;
  node.connect(analyser);
  analyser.connect(audioCtx.destination);
  applyEq();
}

function activateGraph() {
  ensureGraph();
  if (audioCtx.state !== "running") {
    audioCtx.resume().catch(() => {});
    // If the browser refuses without a gesture the audio is silent, so
    // surface the existing unblock button; its click resumes the context.
    setTimeout(() => {
      if (audioCtx.state !== "running") showCtl("enable", "🔊 Click to enable audio");
    }, 300);
  }
}

function applyEq() {
  const btn = document.getElementById("eq-on");
  if (btn) btn.classList.toggle("active", eq.on);
  if (!audioCtx) return;
  preampNode.gain.value = eq.on ? dbGain(eq.preamp) : 1;
  eqFilters.forEach((f, i) => { f.gain.value = eq.on ? (eq.bands[i] || 0) : 0; });
}

// The classic Winamp preset bank (dB per band, clamped to this EQ's ±12).
const EQ_PRESETS = {
  "Flat":               [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
  "Classical":          [0, 0, 0, 0, 0, 0, -7.2, -7.2, -7.2, -9.6],
  "Club":               [0, 0, 8, 5.6, 5.6, 5.6, 3.2, 0, 0, 0],
  "Dance":              [9.6, 7.2, 2.4, 0, 0, -5.6, -7.2, -7.2, 0, 0],
  "Full Bass":          [-8, 9.6, 9.6, 5.6, 1.6, -4, -8, -10.4, -11.2, -11.2],
  "Full Bass & Treble": [7.2, 5.6, 0, -7.2, -4.8, 1.6, 8, 11.2, 12, 12],
  "Full Treble":        [-9.6, -9.6, -9.6, -4, 2.4, 11.2, 12, 12, 12, 12],
  "Headphones":         [4.8, 11.2, 5.6, -3.2, -2.4, 1.6, 4.8, 9.6, 12, 12],
  "Large Hall":         [10.4, 10.4, 5.6, 5.6, 0, -4.8, -4.8, -4.8, 0, 0],
  "Live":               [-4.8, 0, 4, 5.6, 5.6, 5.6, 4, 2.4, 2.4, 2.4],
  "Party":              [7.2, 7.2, 0, 0, 0, 0, 0, 0, 7.2, 7.2],
  "Pop":                [-1.6, 4.8, 7.2, 8, 5.6, 0, -2.4, -2.4, -1.6, -1.6],
  "Reggae":             [0, 0, 0, -5.6, 0, 6.4, 6.4, 0, 0, 0],
  "Rock":               [8, 4.8, -5.6, -8, -3.2, 4, 8.8, 11.2, 11.2, 11.2],
  "Ska":                [-2.4, -4.8, -4, 0, 4, 5.6, 8.8, 9.6, 11.2, 9.6],
  "Soft":               [4.8, 1.6, 0, -2.4, 0, 4, 8, 9.6, 11.2, 12],
  "Soft Rock":          [4, 4, 2.4, 0, -4, -5.6, -3.2, 0, 2.4, 8.8],
  "Techno":             [8, 5.6, 0, -5.6, -4.8, 0, 8, 9.6, 9.6, 8.8],
};

function saveEq() { localStorage.setItem("meshradio-eq", JSON.stringify(eq)); applyEq(); }
function setPresetLabel(name) {
  const sel = document.getElementById("eq-preset");
  if (sel) sel.value = name;
}
function eqBand(i, v) { eq.bands[i] = +v; setPresetLabel(""); saveEq(); }
function eqPreamp(v) { eq.preamp = +v; saveEq(); }
function eqToggle() { eq.on = !eq.on; saveEq(); }
function eqReset() { eq.preamp = 0; eq.bands = EQ_FREQS.map(() => 0); setPresetLabel("Flat"); saveEq(); syncEqSliders(); }
function eqPreset(name) {
  const bands = EQ_PRESETS[name];
  if (!bands) return;
  eq.bands = bands.slice();
  eq.on = true;                     // picking a preset means "I want EQ"
  saveEq();
  syncEqSliders();
}

function syncEqSliders() {
  const pre = document.getElementById("eq-preamp");
  if (pre) pre.value = eq.preamp;
  document.querySelectorAll(".band-input").forEach((el, i) => { el.value = eq.bands[i] || 0; });
  applyEq();
}

document.addEventListener("DOMContentLoaded", () => {
  const sel = document.getElementById("eq-preset");
  if (sel) {
    for (const name of Object.keys(EQ_PRESETS)) {
      const opt = document.createElement("option");
      opt.value = opt.textContent = name;
      sel.appendChild(opt);
    }
  }
  syncEqSliders();
  const win = document.getElementById("eq-window");
  if (win) {
    win.open = localStorage.getItem("meshradio-eq-open") === "1";
    win.addEventListener("toggle", () =>
      localStorage.setItem("meshradio-eq-open", win.open ? "1" : "0"));
  }
});

// Spectrum analyzer: 19 log-spaced bars with slowly falling peak caps,
// drawn from real FFT data when this tab is playing, flat otherwise.
// htmx replaces the canvas on every state push, so look it up per frame.
const VIS_BARS = 19;
const visLevels = new Array(VIS_BARS).fill(0);
const visPeaks = new Array(VIS_BARS).fill(0);
let visData = null;

(function drawVis() {
  requestAnimationFrame(drawVis);
  const c = document.getElementById("vis-canvas");
  if (!c) return;
  const g = c.getContext("2d");
  const W = c.width, H = c.height;
  g.fillStyle = "#000";
  g.fillRect(0, 0, W, H);
  const live = analyser && audio.src && !audio.paused;
  if (live) {
    if (!visData || visData.length !== analyser.frequencyBinCount)
      visData = new Uint8Array(analyser.frequencyBinCount);
    analyser.getByteFrequencyData(visData);
  }
  const grad = g.createLinearGradient(0, H, 0, 0);
  grad.addColorStop(0, "#00e800");
  grad.addColorStop(0.6, "#ffe000");
  grad.addColorStop(1, "#ff4030");
  const bw = W / VIS_BARS;
  for (let i = 0; i < VIS_BARS; i++) {
    let level = 0;
    if (live) {
      // log-spaced bin ranges so the bass doesn't hog every bar
      const lo = Math.floor(Math.pow(visData.length, i / VIS_BARS));
      const hi = Math.max(lo + 1, Math.floor(Math.pow(visData.length, (i + 1) / VIS_BARS)));
      let sum = 0;
      for (let j = lo; j < hi; j++) sum += visData[j];
      level = sum / (hi - lo) / 255;
    }
    visLevels[i] = Math.max(level, visLevels[i] - 0.07);        // smooth fall
    visPeaks[i] = Math.max(visLevels[i], visPeaks[i] - 0.008);  // caps fall slower
    const h = Math.round(visLevels[i] * (H - 2));
    if (h > 0) {
      g.fillStyle = grad;
      g.fillRect(i * bw + 1, H - h, bw - 2, h);
    }
    const ph = Math.round(visPeaks[i] * (H - 2));
    if (ph > 1) {
      g.fillStyle = "#b0e8c0";
      g.fillRect(i * bw + 1, H - ph - 1, bw - 2, 1);
    }
  }
  // dark scanlines for the segmented Winamp look
  g.fillStyle = "rgba(0,0,0,0.45)";
  for (let y = H - 3; y > 0; y -= 3) g.fillRect(0, y, W, 1);
})();
