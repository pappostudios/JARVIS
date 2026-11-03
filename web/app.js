/* J.A.R.V.I.S. dashboard — desktop & Android (PWA). No build step, no framework. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const state = {
  settings: {}, overview: null, busy: false, convMode: false, lastInputWasVoice: false,
  coreMode: "idle", micLevel: 0, outLevel: 0, notified: new Set(), tab: "assistant",
};
try { state.notified = new Set(JSON.parse(localStorage.getItem("jarvis.notified") || "[]")); } catch {}
try { state.convMode = localStorage.getItem("jarvis.conv") === "1"; } catch {}

/* ───────────────────────── API ───────────────────────── */
async function api(path, opts = {}) {
  const init = { credentials: "same-origin", ...opts, headers: { ...(opts.headers || {}) } };
  if (opts.json !== undefined) {
    init.method = init.method || "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.json);
  }
  let r;
  try { r = await fetch(path, init); }
  catch (e) {
    if (init.signal?.aborted) throw e;
    goOffline(); throw new Error("Can't reach the JARVIS server. Make sure JARVIS is running on your computer.");
  }
  if (state.offline) goOnline();
  if (r.status === 401) { showLogin(); throw new Error("Not authenticated"); }
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r;
}
const getJSON = async (p) => (await api(p)).json();
const postJSON = async (p, body = {}) => (await api(p, { json: body })).json();

/* ───────────────────────── connection watchdog ───────────────────────── */
let offlineTimer = null;
function goOffline() {
  if (state.offline) return;
  state.offline = true;
  $("#offlineBar").classList.remove("hidden");
  $("#sysPill").classList.remove("ok"); $("#sysPill").classList.add("bad"); $("#sysText").textContent = "Offline";
  core.set("idle", "Server offline");
  clearInterval(offlineTimer);
  offlineTimer = setInterval(async () => {
    try { const r = await fetch("/api/health", { cache: "no-store" }); if (r.ok) goOnline(); } catch {}
  }, 4000);
}
function goOnline() {
  if (!state.offline) return;
  if (state.updating) { location.reload(); return; }
  state.offline = false;
  clearInterval(offlineTimer);
  $("#offlineBar").classList.add("hidden");
  core.set("idle");
  toast("Reconnected to JARVIS.");
  boot();
}
/* run an async UI action and report failures as a toast instead of failing silently */
const guard = (fn) => async (...args) => { try { await fn(...args); } catch (e) { if (e.message !== "Not authenticated") toast(e.message, "bad"); } };

/* ───────────────────────── helpers ───────────────────────── */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (d) => d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
const fmtDay = (iso) => { const d = new Date(iso + "T12:00"); return isNaN(d) ? iso : d.toLocaleDateString("en-GB", { day: "numeric", month: "short" }); };
const todayISO = () => { const d = new Date(); return new Date(d - d.getTimezoneOffset() * 6e4).toISOString().slice(0, 10); };

function renderMarkdown(text) {
  const lines = esc(text).split(/\n/);
  let html = "", inList = false, para = [];
  const inline = (s) => s
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*(?!\s)(.+?)\*/g, "$1<em>$2</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/(https?:\/\/[^\s<)]+)/g, '<a href="$1" target="_blank" rel="noopener">link</a>');
  const flush = () => { if (para.length) { html += `<p>${inline(para.join("<br>"))}</p>`; para = []; } };
  for (const line of lines) {
    const m = line.match(/^\s*(?:[-*•]|\d+\.)\s+(.*)/);
    if (m) { flush(); if (!inList) { html += "<ul>"; inList = true; } html += `<li>${inline(m[1])}</li>`; continue; }
    if (inList) { html += "</ul>"; inList = false; }
    if (!line.trim()) flush(); else para.push(line.replace(/^#+\s*/, ""));
  }
  flush(); if (inList) html += "</ul>";
  return html;
}

function toast(text, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = text;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 6000);
}

/* ───────────────────────── auth ───────────────────────── */
function showLogin() { $("#login").classList.remove("hidden"); setTimeout(() => $("#loginCode").focus(), 50); }
$("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#loginErr").textContent = "";
  try {
    let r;
    try { r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code: $("#loginCode").value }) }); }
    catch { throw new Error("Can't reach the JARVIS server. Is JARVIS running on your computer?"); }
    if (!r.ok) throw new Error((await r.json()).detail || "Denied");
    $("#login").classList.add("hidden");
    boot();
  } catch (err) { $("#loginErr").textContent = err.message; }
});

/* ───────────────────────── clock ───────────────────────── */
function tickClock() {
  const d = new Date();
  $("#clockTime").textContent = fmtTime(d);
  $("#clockDate").textContent = d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", year: "numeric" });
}
setInterval(tickClock, 1000); tickClock();

/* ───────────────────────── core visual ───────────────────────── */
const core = (() => {
  const cv = $("#core"), ctx = cv.getContext("2d");
  let w = 0, h = 0, t = 0, energy = 0, spin = 0;
  const bars = new Float32Array(72);
  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    w = cv.clientWidth; h = cv.clientHeight;
    cv.width = w * dpr; cv.height = h * dpr; ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  window.addEventListener("resize", resize); resize();
  const arc = (r, a0, a1, lw, color, blur = 0) => {
    ctx.beginPath(); ctx.arc(0, 0, r, a0, a1); ctx.lineWidth = lw; ctx.strokeStyle = color;
    ctx.shadowBlur = blur; ctx.shadowColor = color; ctx.stroke(); ctx.shadowBlur = 0;
  };
  function frame() {
    t += 1 / 60;
    const mode = state.coreMode;
    const level = mode === "listening" ? state.micLevel : mode === "speaking" ? state.outLevel : 0;
    energy += ((mode === "thinking" ? 0.35 + 0.15 * Math.sin(t * 6) : level) - energy) * 0.18;
    spin += mode === "thinking" ? 0.035 : 0.006 + energy * 0.03;
    if (w === 0) resize();
    ctx.clearRect(0, 0, w, h);
    const R = Math.min(w, h) * 0.42;
    ctx.save(); ctx.translate(w / 2, h / 2 - 8);
    const hue = mode === "listening" ? "255,120,140" : mode === "thinking" ? "255,190,90" : "70,217,255";
    const c = (a) => `rgba(${hue},${a})`;

    // outer tick ring
    ctx.save(); ctx.rotate(spin * 0.4);
    for (let i = 0; i < 120; i++) {
      const a = (i / 120) * Math.PI * 2, long = i % 10 === 0;
      ctx.beginPath();
      ctx.moveTo(Math.cos(a) * R, Math.sin(a) * R);
      ctx.lineTo(Math.cos(a) * (R - (long ? 10 : 4)), Math.sin(a) * (R - (long ? 10 : 4)));
      ctx.strokeStyle = c(long ? 0.55 : 0.18); ctx.lineWidth = 1; ctx.stroke();
    }
    ctx.restore();

    // audio bars
    for (let i = 0; i < bars.length; i++) {
      const target = (mode === "listening" || mode === "speaking")
        ? energy * (0.55 + 0.45 * Math.sin(i * 1.7 + t * 9) * Math.sin(i * 0.33 + t * 3)) : 0.02;
      bars[i] += (Math.max(0, target) - bars[i]) * 0.25;
      const a = (i / bars.length) * Math.PI * 2 - Math.PI / 2, r0 = R * 0.80, r1 = r0 + 4 + bars[i] * R * 0.32;
      ctx.beginPath(); ctx.moveTo(Math.cos(a) * r0, Math.sin(a) * r0); ctx.lineTo(Math.cos(a) * r1, Math.sin(a) * r1);
      ctx.strokeStyle = c(0.35 + bars[i]); ctx.lineWidth = 2; ctx.stroke();
    }

    // segmented arcs
    ctx.save(); ctx.rotate(spin);
    for (let i = 0; i < 3; i++) arc(R * 0.72, i * 2.094, i * 2.094 + 1.5, 2, c(0.85), 12);
    ctx.restore();
    ctx.save(); ctx.rotate(-spin * 1.6);
    for (let i = 0; i < 6; i++) arc(R * 0.64, i * 1.047, i * 1.047 + 0.6, 4, c(0.35));
    ctx.restore();
    arc(R * 0.56, 0, Math.PI * 2, 1, c(0.25));

    // core glow
    const cr = R * (0.30 + energy * 0.14 + 0.012 * Math.sin(t * 2));
    const g = ctx.createRadialGradient(0, 0, 0, 0, 0, cr * 1.9);
    g.addColorStop(0, "rgba(255,255,255,0.95)");
    g.addColorStop(0.25, c(0.9));
    g.addColorStop(0.6, c(0.18));
    g.addColorStop(1, c(0));
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(0, 0, cr * 1.9, 0, Math.PI * 2); ctx.fill();
    arc(cr, 0, Math.PI * 2, 2, c(0.9), 20);
    // inner triangle-ish lattice
    ctx.save(); ctx.rotate(-spin * 0.8);
    ctx.beginPath();
    for (let i = 0; i <= 6; i++) { const a = i * Math.PI / 3; const rr = cr * 0.62; i ? ctx.lineTo(Math.cos(a) * rr, Math.sin(a) * rr) : ctx.moveTo(Math.cos(a) * rr, Math.sin(a) * rr); }
    ctx.strokeStyle = "rgba(255,255,255,0.55)"; ctx.lineWidth = 1; ctx.stroke();
    ctx.restore();
    ctx.restore();
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
  return {
    set(mode, label) {
      state.coreMode = mode;
      $("#coreStatus").textContent = label || { idle: "Standing by", listening: "Listening", thinking: "Processing", speaking: "Speaking" }[mode];
    },
  };
})();

/* ───────────────────────── audio context ───────────────────────── */
let audioCtx = null, outAnalyser = null;
function ensureAudio() {
  if (!audioCtx) {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    outAnalyser = audioCtx.createAnalyser(); outAnalyser.fftSize = 512;
    outAnalyser.connect(audioCtx.destination);
    const buf = new Uint8Array(outAnalyser.fftSize);
    (function meter() {
      outAnalyser.getByteTimeDomainData(buf);
      let s = 0; for (const v of buf) { const x = (v - 128) / 128; s += x * x; }
      state.outLevel = Math.min(1, Math.sqrt(s / buf.length) * 4);
      requestAnimationFrame(meter);
    })();
  }
  if (audioCtx.state === "suspended") audioCtx.resume();
  return audioCtx;
}
document.addEventListener("pointerdown", ensureAudio, { once: true });

/* ───────────────────────── speech output ───────────────────────── */
let speakToken = 0, currentSource = null;

function stopSpeaking() {
  speakToken++;
  try { currentSource?.stop(); } catch {}
  currentSource = null;
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  if (state.coreMode === "speaking") core.set("idle");
}

function chunkForSpeech(text) {
  const clean = text.replace(/```[\s\S]*?```/g, " ").replace(/https?:\/\/\S+/g, "the link").replace(/[*_#`>|~]/g, "").replace(/\s+/g, " ").trim();
  const sentences = clean.match(/[^.!?]+[.!?]+["')\]]*\s*|[^.!?]+$/g) || [clean];
  const first = []; let i = 0, len = 0;
  while (i < sentences.length && (len < 90 || first.length === 0) && len + sentences[i].length < 240) { first.push(sentences[i]); len += sentences[i].length; i++; }
  if (!first.length) { first.push(sentences[0]); i = 1; }
  const rest = sentences.slice(i).join("").trim();
  return [first.join("").trim(), ...(rest ? [rest] : [])].filter(Boolean);
}

async function fetchTTS(text) {
  try {
    const r = await api("/api/tts", { json: { text } });
    if (r.status === 204) {
      const err = r.headers.get("X-TTS-Error");
      if (err) console.warn("TTS fallback:", err);
      return null;
    }
    return await r.arrayBuffer();
  } catch (e) { console.warn(e); return null; }
}

function pickBrowserVoice() {
  const voices = speechSynthesis.getVoices();
  const prefs = ["Microsoft Ryan", "Google UK English Male", "Daniel", "Arthur", "Microsoft George", "Oliver", "Microsoft Thomas"];
  for (const p of prefs) { const v = voices.find((v) => v.name.includes(p)); if (v) return v; }
  return voices.find((v) => v.lang === "en-GB") || voices.find((v) => v.lang.startsWith("en")) || null;
}

function browserSpeak(text, token) {
  return new Promise((resolve) => {
    if (!("speechSynthesis" in window) || token !== speakToken) return resolve();
    const u = new SpeechSynthesisUtterance(text);
    const v = pickBrowserVoice(); if (v) { u.voice = v; u.lang = v.lang; } else u.lang = "en-GB";
    u.rate = 1.0; u.pitch = 0.92;
    let fake = null;
    u.onstart = () => { core.set("speaking"); fake = setInterval(() => (state.outLevel = 0.25 + Math.random() * 0.35), 90); };
    u.onend = u.onerror = () => { clearInterval(fake); state.outLevel = 0; resolve(); };
    speechSynthesis.speak(u);
  });
}

async function playBuffer(arrayBuf, token) {
  const ctx = ensureAudio();
  const audio = await ctx.decodeAudioData(arrayBuf.slice(0));
  if (token !== speakToken) return;
  return new Promise((resolve) => {
    const src = ctx.createBufferSource();
    src.buffer = audio; src.connect(outAnalyser);
    src.onended = () => resolve();
    currentSource = src;
    core.set("speaking");
    src.start();
  });
}

async function speak(text) {
  stopSpeaking();
  const token = speakToken;
  const chunks = chunkForSpeech(text);
  if (!chunks.length) return;
  try {
    if (state.settings.voice_engine === "browser") {
      for (const c of chunks) await browserSpeak(c, token);
    } else {
      let next = fetchTTS(chunks[0]);
      for (let i = 0; i < chunks.length; i++) {
        const buf = await next;
        if (token !== speakToken) return;
        if (i + 1 < chunks.length) next = fetchTTS(chunks[i + 1]);
        if (!buf) { await browserSpeak(chunks.slice(i).join(" "), token); break; }
        await playBuffer(buf, token);
        if (token !== speakToken) return;
      }
    }
  } catch (e) { console.warn("speech failed", e); }
  if (token === speakToken) {
    core.set("idle");
    if (state.convMode && state.lastInputWasVoice) setTimeout(() => { if (!state.busy && !recorder) startListening(true); }, 350);
  }
}

/* ───────────────────────── speech input ───────────────────────── */
let recorder = null, micStream = null, vadTimer = null;

function encodeWAV(samples, rate) {
  const buf = new ArrayBuffer(44 + samples.length * 2), v = new DataView(buf);
  const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  w(0, "RIFF"); v.setUint32(4, 36 + samples.length * 2, true); w(8, "WAVE"); w(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, "data"); v.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) { const s = Math.max(-1, Math.min(1, samples[i])); v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true); }
  return new Blob([buf], { type: "audio/wav" });
}

async function blobToWav(blob) {
  const ctx = ensureAudio();
  const decoded = await ctx.decodeAudioData(await blob.arrayBuffer());
  const rate = 16000;
  const off = new OfflineAudioContext(1, Math.ceil(decoded.duration * rate), rate);
  const src = off.createBufferSource(); src.buffer = decoded; src.connect(off.destination); src.start();
  const rendered = await off.startRendering();
  return encodeWAV(rendered.getChannelData(0), rate);
}

async function startListening(auto = false) {
  if (recorder) return stopListening();
  stopSpeaking();
  if (!navigator.mediaDevices?.getUserMedia) {
    toast("Microphone needs HTTPS on phones. See README → 'Phone microphone'.", "warn");
    return;
  }
  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  } catch (e) { toast("Microphone unavailable: " + e.message, "bad"); return; }
  const ctx = ensureAudio();
  const srcNode = ctx.createMediaStreamSource(micStream);
  const an = ctx.createAnalyser(); an.fftSize = 1024; srcNode.connect(an);
  const data = new Float32Array(an.fftSize);
  const chunks = [];
  recorder = new MediaRecorder(micStream);
  recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  recorder.onstop = async () => {
    clearInterval(vadTimer); srcNode.disconnect();
    micStream.getTracks().forEach((t) => t.stop());
    const heard = recorder._heard; recorder = null; micStream = null;
    $("#micBtn").classList.remove("rec"); state.micLevel = 0;
    if (!heard) { core.set("idle"); return; }
    core.set("thinking", "Transcribing");
    try {
      const wav = await blobToWav(new Blob(chunks, { type: chunks[0]?.type || "audio/webm" }));
      const fd = new FormData(); fd.append("audio", wav, "speech.wav");
      const { text } = await (await api("/api/transcribe", { method: "POST", body: fd })).json();
      if (!text) { core.set("idle", "Didn't catch that"); setTimeout(() => state.coreMode === "idle" && core.set("idle"), 1800); return; }
      state.lastInputWasVoice = true;
      send(text);
    } catch (e) { core.set("idle"); toast("Transcription failed: " + e.message, "bad"); }
  };
  recorder._heard = false;
  recorder.start(250);
  $("#micBtn").classList.add("rec");
  core.set("listening");

  // voice-activity detection: auto-stop after a pause
  const started = performance.now();
  let floor = 0.01, samples = 0, lastVoice = 0;
  vadTimer = setInterval(() => {
    if (!recorder) return;
    an.getFloatTimeDomainData(data);
    let s = 0; for (const x of data) s += x * x;
    const rms = Math.sqrt(s / data.length);
    state.micLevel = Math.min(1, rms * 9);
    const now = performance.now();
    if (now - started < 350) { floor = (floor * samples + rms) / ++samples; return; }
    if (rms > Math.max(0.018, floor * 2.8)) { recorder._heard = true; lastVoice = now; }
    if (recorder._vadOff) return;
    if (recorder._heard && now - lastVoice > 1300) stopListening();
    else if (!recorder._heard && now - started > (auto ? 6000 : 8000)) stopListening();
    else if (now - started > 45000) stopListening();
  }, 60);
}

function stopListening() { if (recorder && recorder.state !== "inactive") recorder.stop(); }

$("#micBtn").addEventListener("click", () => startListening());
// Push-to-talk: hold Space when not typing
let spaceHeld = false;
document.addEventListener("keydown", (e) => {
  if (e.code !== "Space" || e.repeat || live.active || /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) return;
  e.preventDefault(); spaceHeld = true;
  startListening().then(() => {
    if (!recorder) return;
    recorder._vadOff = true; recorder._heard = true;
    if (!spaceHeld) stopListening();
  });
});
document.addEventListener("keyup", (e) => {
  if (e.code === "Space" && spaceHeld) { spaceHeld = false; stopListening(); }
});
$("#convBtn").addEventListener("click", () => {
  state.convMode = !state.convMode;
  $("#convBtn").setAttribute("aria-pressed", state.convMode);
  try { localStorage.setItem("jarvis.conv", state.convMode ? "1" : "0"); } catch {}
  toast(state.convMode ? "Conversation mode on — I'll keep listening after each reply." : "Conversation mode off.");
});
$("#convBtn").setAttribute("aria-pressed", state.convMode);

/* ───────────────────────── live voice call ─────────────────────────
   Phone-call mode: the mic streams to JARVIS (16 kHz PCM) over a WebSocket, JARVIS relays it to Gemini Live,
   and Gemini's voice streams back (24 kHz PCM) and is played as it arrives. */
const CAPTURE_WORKLET = `
class JarvisCapture extends AudioWorkletProcessor {
  constructor() { super(); this.ratio = sampleRate / 16000; this.pos = 0; this.acc = 0; this.n = 0; this.buf = []; }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      this.acc += ch[i]; this.n++; this.pos++;
      if (this.pos >= this.ratio) { this.pos -= this.ratio; this.buf.push(this.acc / this.n); this.acc = 0; this.n = 0; }
    }
    if (this.buf.length >= 640) {            // 40 ms chunks
      const out = new Int16Array(this.buf.length); let sum = 0;
      for (let i = 0; i < this.buf.length; i++) { const v = Math.max(-1, Math.min(1, this.buf[i])); out[i] = v < 0 ? v * 32768 : v * 32767; sum += v * v; }
      this.port.postMessage({ pcm: out.buffer, rms: Math.sqrt(sum / this.buf.length) }, [out.buffer]);
      this.buf = [];
    }
    return true;
  }
}
registerProcessor("jarvis-capture", JarvisCapture);`;

const live = { ws: null, stream: null, src: null, node: null, sink: null, playhead: 0, sources: [], userEl: null, jarvisEl: null,
  active: false, workletLoaded: false, dropAudio: false, quietUntil: 0, model: "" };

function liveSpeaking() { return live.sources.length > 0; }

function livePlay(buf) {
  if (live.dropAudio || !audioCtx) return;
  if (buf.byteLength % 2) buf = buf.slice(0, buf.byteLength - 1);
  const i16 = new Int16Array(buf), f = new Float32Array(i16.length);
  for (let i = 0; i < i16.length; i++) f[i] = i16[i] / 32768;
  const ab = audioCtx.createBuffer(1, f.length, 24000);
  ab.copyToChannel(f, 0);
  const node = audioCtx.createBufferSource();
  node.buffer = ab; node.connect(outAnalyser);
  const t = Math.max(audioCtx.currentTime + 0.03, live.playhead);
  node.start(t);
  live.playhead = t + ab.duration;
  live.sources.push(node);
  core.set("speaking", "Speaking");
  node.onended = () => {
    live.sources = live.sources.filter((x) => x !== node);
    if (!live.sources.length) { live.quietUntil = performance.now() + 350; if (live.active) core.set("listening", "Live — go ahead"); }
  };
}

function liveFlush() {
  live.sources.forEach((n) => { try { n.stop(); } catch {} });
  live.sources = []; live.playhead = 0;
}

function liveBubble(role, text) {
  const key = role === "user" ? "userEl" : "jarvisEl";
  if (!live[key]) {
    live[key] = addMessage(role, "");
    live[key].classList.add("live");
    live[key].dataset.raw = "";
  }
  const el = live[key];
  el.dataset.raw += text;
  el.innerHTML = renderMarkdown(el.dataset.raw.trim());
  chatEl.scrollTop = chatEl.scrollHeight;
}

function onLiveEvent(ev) {
  switch (ev.type) {
    case "ready":
      live.model = ev.model;
      $("#liveBtn").classList.remove("connecting");
      $("#liveText").textContent = `Live call · ${ev.model}`;
      core.set("listening", "Live — go ahead");
      if (!ev.resumed) toast(`Live call connected (${ev.model}). Just talk.`);
      break;
    case "user_text": liveBubble("user", ev.text); break;
    case "jarvis_text": liveBubble("jarvis", ev.text); break;
    case "interrupted": liveFlush(); break;
    case "turn_complete":
      live.userEl = live.jarvisEl = null; live.dropAudio = false;
      $("#liveText").textContent = `Live call · ${live.model}`;
      if (!liveSpeaking()) core.set("listening", "Live — go ahead");
      break;
    case "status": core.set("thinking", ev.text); $("#liveText").textContent = `Live call · ${ev.text}`; break;
    case "refresh": refresh(true); break;
    case "error": toast(ev.text, "bad"); addMessage("jarvis", "⚠ " + ev.text, { error: true }); break;
  }
}

async function startLive() {
  if (live.active) return stopLive();
  if (!navigator.mediaDevices?.getUserMedia) { toast("The microphone needs HTTPS on phones — see README → Phone microphone.", "warn"); return; }
  const ctx = ensureAudio();
  stopSpeaking(); if (recorder) stopListening();
  $("#liveBtn").classList.add("connecting");
  try {
    live.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
    if (!live.workletLoaded) {
      await ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([CAPTURE_WORKLET], { type: "application/javascript" })));
      live.workletLoaded = true;
    }
  } catch (e) { $("#liveBtn").classList.remove("connecting"); toast("Microphone unavailable: " + e.message, "bad"); return; }

  live.active = true; live.dropAudio = false; live.userEl = live.jarvisEl = null;
  document.body.classList.add("live-on");
  $("#liveBtn").classList.add("on"); $("#liveBtn").setAttribute("aria-label", "End live call");
  $("#liveText").textContent = "Live call · connecting";
  core.set("thinking", "Connecting");

  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/live`);
  ws.binaryType = "arraybuffer";
  live.ws = ws;
  ws.onmessage = (e) => (typeof e.data === "string" ? onLiveEvent(JSON.parse(e.data)) : livePlay(e.data));
  ws.onclose = (e) => {
    if (live.ws !== ws) return;
    if (e.code === 4401) { showLogin(); }
    if (live.active) { stopLive(true); }
  };
  ws.onerror = () => {};

  live.src = ctx.createMediaStreamSource(live.stream);
  live.node = new AudioWorkletNode(ctx, "jarvis-capture");
  live.sink = ctx.createGain(); live.sink.gain.value = 0;      // keeps the worklet running without playing the mic
  live.src.connect(live.node); live.node.connect(live.sink); live.sink.connect(ctx.destination);
  live.node.port.onmessage = ({ data }) => {
    if (!liveSpeaking()) state.micLevel = Math.min(1, data.rms * 9);
    if (!live.ws || live.ws.readyState !== 1) return;
    // Without headphones JARVIS would hear himself: hold the mic while he talks unless the user allowed interrupting.
    const mayInterrupt = state.settings.live_interrupt === "true";
    if (!mayInterrupt && (liveSpeaking() || performance.now() < live.quietUntil)) return;
    live.ws.send(data.pcm);
  };
}

function stopLive(remote = false) {
  const ws = live.ws; live.ws = null;
  if (ws && ws.readyState <= 1) { try { ws.send(JSON.stringify({ type: "hangup" })); } catch {} ws.close(); }
  live.active = false;
  liveFlush();
  try { live.src?.disconnect(); live.node?.disconnect(); live.sink?.disconnect(); } catch {}
  live.stream?.getTracks().forEach((t) => t.stop());
  live.stream = live.src = live.node = live.sink = null;
  document.body.classList.remove("live-on");
  $("#liveBtn").classList.remove("on", "connecting"); $("#liveBtn").setAttribute("aria-label", "Start live voice call");
  state.micLevel = 0;
  core.set("idle", remote ? "Call ended" : "Standing by");
  if (remote) setTimeout(() => state.coreMode === "idle" && core.set("idle"), 2500);
}

$("#liveBtn").addEventListener("click", () => startLive());
// Tap the core while JARVIS is talking in a call to cut him off.
$("#core").addEventListener("click", () => { if (live.active && liveSpeaking()) { liveFlush(); live.dropAudio = true; } });

/* ───────────────────────── chat ───────────────────────── */
const chatEl = $("#chat");
function addMessage(role, text, extra = {}) {
  const el = document.createElement("div");
  el.className = `msg ${role}${extra.error ? " error" : ""}`;
  el.dir = "auto";
  el.innerHTML = renderMarkdown(text);
  if (role === "jarvis" && !extra.error) {
    const rp = document.createElement("button");
    rp.className = "replay"; rp.title = "Speak again"; rp.textContent = "🔊";
    rp.onclick = () => speak(text);
    el.lastElementChild?.appendChild(rp);
  }
  const meta = [];
  if (extra.model) meta.push(`${extra.model}${extra.seconds != null ? ` · ${extra.seconds}s` : ""}`);
  if (extra.tools?.length) meta.push("⟶ " + [...new Set(extra.tools)].join(" · "));
  if (meta.length) {
    const tl = document.createElement("div"); tl.className = "tools";
    tl.textContent = meta.join("   ");
    el.appendChild(tl);
  }
  chatEl.appendChild(el);
  chatEl.scrollTop = chatEl.scrollHeight;
  return el;
}

async function send(text) {
  text = text.trim();
  if (!text || state.busy) return;
  ensureAudio(); stopSpeaking();
  state.busy = true; $("#sendBtn").disabled = true;
  addMessage("user", text);
  const typing = document.createElement("div"); typing.className = "typing"; typing.textContent = "Processing";
  chatEl.appendChild(typing); chatEl.scrollTop = chatEl.scrollHeight;
  core.set("thinking");
  let reply = null, statusText = "Thinking";
  const t0 = Date.now();
  const timer = setInterval(() => {
    const sec = Math.round((Date.now() - t0) / 1000);
    typing.textContent = sec >= 3 ? `${statusText} · ${sec}s` : statusText;
  }, 500);
  const abort = new AbortController();
  const killer = setTimeout(() => abort.abort(), 180000);
  try {
    const r = await api("/api/chat", { json: { text }, signal: abort.signal });
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const line = buf.slice(0, idx).trim(); buf = buf.slice(idx + 2);
        if (!line.startsWith("data:")) continue;
        const ev = JSON.parse(line.slice(5));
        if (ev.type === "status") { statusText = ev.text; core.set("thinking", ev.text); }
        else if (ev.type === "reply") { reply = ev; }
        else if (ev.type === "error") { reply = { error: true, text: ev.text }; }
      }
    }
  } catch (e) { reply = { error: true, text: abort.signal.aborted ? "That took far too long, so I stopped waiting. Please try again." : e.message }; }
  clearInterval(timer); clearTimeout(killer);
  typing.remove();
  state.busy = false; $("#sendBtn").disabled = false;
  if (!reply) reply = { error: true, text: "Connection lost." };
  if (reply.error) {
    addMessage("jarvis", "⚠ " + reply.text, { error: true }); core.set("idle");
  } else {
    addMessage("jarvis", reply.text, { tools: reply.tools, model: reply.model, seconds: reply.seconds });
    if (state.settings.auto_speak !== "false") speak(reply.text); else core.set("idle");
    if (reply.tools?.length) refresh(true);
  }
}

const input = $("#input");
$("#composer").addEventListener("submit", (e) => {
  e.preventDefault(); state.lastInputWasVoice = false; const t = input.value.trim(); input.value = ""; autoGrow();
  if (!t) return;
  if (live.active && live.ws?.readyState === 1) { liveBubble("user", t); live.ws.send(JSON.stringify({ type: "text", text: t })); return; }
  send(t);
});
input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); } });
const autoGrow = () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 140) + "px"; };
input.addEventListener("input", autoGrow);
$$("#chips button").forEach((b) => b.addEventListener("click", () => { state.lastInputWasVoice = false; send(b.dataset.q); setTab("assistant"); }));

/* ───────────────────────── dashboard panels ───────────────────────── */
function eventTimes(ev) {
  if (ev.all_day) return { s: null, e: null };
  return { s: new Date(ev.start), e: new Date(ev.end) };
}

function renderAgenda(list, el, compact = false) {
  if (!list?.length) { el.innerHTML = `<li class="empty">${compact ? "Nothing scheduled." : "Your day is clear."}</li>`; return; }
  const now = new Date();
  el.innerHTML = list.map((ev) => {
    const { s, e } = eventTimes(ev);
    const cls = s ? (e < now ? "past" : s <= now ? "now" : "") : "";
    const time = s ? `${fmtTime(s)}<small>${fmtTime(e)}</small>` : "ALL<small>DAY</small>";
    const meta = [ev.location, ev.attendees?.length ? `${ev.attendees.length} attendee${ev.attendees.length > 1 ? "s" : ""}` : ""].filter(Boolean).join(" · ");
    return `<li class="${cls}"><span class="t">${time}</span><div><div class="what" dir="auto">${esc(ev.title)}</div>
      ${meta ? `<div class="meta" dir="auto">${esc(meta)}</div>` : ""}${ev.meet_link && !compact ? `<a href="${esc(ev.meet_link)}" target="_blank" rel="noopener">Join Meet ↗</a>` : ""}</div></li>`;
  }).join("");
}

function renderNextUp(events) {
  const now = new Date();
  const next = (events || []).map((ev) => ({ ev, ...eventTimes(ev) })).filter((x) => x.s && x.e > now).sort((a, b) => a.s - b.s)[0];
  const el = $("#nextUp");
  if (!next) { el.classList.add("hidden"); return; }
  const mins = Math.round((next.s - now) / 6e4);
  const when = mins <= 0 ? "In progress" : mins < 60 ? `In ${mins} min` : `At ${fmtTime(next.s)}`;
  el.innerHTML = `<div class="lbl">Next · ${when}</div><div class="ttl" dir="auto">${esc(next.ev.title)}</div>`;
  el.classList.remove("hidden");
}

function googleMissing(el, msg) {
  el.innerHTML = `<li class="empty">${esc(msg || "Google not connected.")}<br><button class="mini" style="margin-top:8px" onclick="openSettings()">Connect Google</button></li>`;
}

function renderInbox(gd) {
  const el = $("#inbox");
  if (!gd.connected) return googleMissing(el, "Connect Google to see your inbox.");
  const n = gd.unread || 0;
  $("#unreadTag").textContent = `${n} unread`;
  $("#statUnread").textContent = n;
  const badge = $("#inboxBadge"); badge.textContent = n > 99 ? "99+" : n; badge.classList.toggle("hidden", !n);
  if (!gd.emails?.length) { el.innerHTML = `<li class="empty">Inbox zero. Splendid.</li>`; return; }
  el.innerHTML = gd.emails.map((m) => {
    const from = (m.from.match(/^"?([^"<]+)"?\s*</) || [, m.from])[1].trim();
    const d = new Date(m.date); const isToday = d.toDateString() === new Date().toDateString();
    const when = isNaN(d) ? "" : isToday ? fmtTime(d) : d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
    return `<li data-id="${esc(m.id)}" data-from="${esc(from)}" data-subject="${esc(m.subject)}">
      <div class="from"><span dir="auto">${esc(from)}</span><time>${when}</time></div>
      <div class="subj" dir="auto">${esc(m.subject)}</div><div class="snip" dir="auto">${esc(m.snippet)}</div></li>`;
  }).join("");
  $$("#inbox li[data-id]").forEach((li) => li.addEventListener("click", () => {
    setTab("assistant");
    send(`Summarise the email from ${li.dataset.from} titled "${li.dataset.subject}" (id ${li.dataset.id}) and suggest a reply if one is needed.`);
  }));
}

function renderApprovals(list) {
  $("#approvalsPanel").classList.toggle("hidden", !list.length);
  const bar = $("#approvalBar");
  bar.classList.toggle("hidden", !list.length);
  bar.textContent = `⚠ ${list.length} awaiting your approval — review ›`;
  bar.onclick = () => setTab("inbox");
  $("#approvals").innerHTML = list.map((a) => `<div class="approval"><pre>${esc(a.summary)}</pre>
    <div class="row"><button class="btn" data-reject="${a.id}">Discard</button><button class="btn primary" data-approve="${a.id}">Approve</button></div></div>`).join("");
  $$("[data-approve]").forEach((b) => b.onclick = async () => {
    b.disabled = true;
    try { await postJSON(`/api/actions/${b.dataset.approve}/approve`); toast("Done."); addMessage("jarvis", "Done — as you wish."); }
    catch (e) { toast(e.message, "bad"); }
    refresh(true);
  });
  $$("[data-reject]").forEach((b) => b.onclick = async () => { await postJSON(`/api/actions/${b.dataset.reject}/reject`); refresh(); });
}

function renderProjects(list) {
  const el = $("#projects");
  const sel = $("#taskProject"), cur = sel.value;
  sel.innerHTML = `<option value="">No project</option>` + list.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  sel.value = cur;
  if (!list.length) { el.innerHTML = `<li class="empty">No projects yet. Tell me about one — “Jarvis, new project: website relaunch, due 1 December.”</li>`; return; }
  el.innerHTML = list.map((p) => {
    const dl = p.deadline ? ` · due ${new Date(p.deadline).toLocaleDateString("en-GB", { day: "numeric", month: "short" })}` : "";
    return `<li data-id="${p.id}"><div class="proj-top"><span class="proj-name">${esc(p.name)} <span class="prio ${p.priority}">${p.priority}</span></span><span class="proj-pct">${p.progress_percent}%</span></div>
      <div class="bar"><i style="width:${p.progress_percent}%"></i></div>
      <div class="proj-meta">${p.tasks_done}/${p.tasks_total} tasks${dl}${p.status === "paused" ? " · paused" : ""}</div></li>`;
  }).join("");
  $$("#projects li[data-id]").forEach((li) => li.addEventListener("click", () => openProject(li.dataset.id)));
}

function renderTasks(list) {
  $("#taskTag").textContent = list.length;
  $("#statTasks").textContent = list.length;
  const el = $("#tasks"), today = todayISO();
  if (!list.length) { el.innerHTML = `<li class="empty" style="display:block">All clear.</li>`; return; }
  el.innerHTML = list.map((t) => {
    const overdue = t.due_date && t.due_date < today;
    const meta = [t.project && esc(t.project), t.due_date && `<span class="${overdue ? "overdue" : ""}">${overdue ? "overdue · " : ""}${t.due_date === today ? "today" : fmtDay(t.due_date)}</span>`,
      t.priority !== "medium" && `<span class="prio ${t.priority}">${t.priority}</span>`, t.status === "in_progress" && "in progress"].filter(Boolean).join(" ");
    return `<li class="${t.status === "done" ? "done" : ""}" data-id="${t.id}"><button class="check" aria-label="Toggle done"></button>
      <div><div class="tt" dir="auto">${esc(t.title)}</div>${meta ? `<div class="tm">${meta}</div>` : ""}</div><button class="del" aria-label="Delete">✕</button></li>`;
  }).join("");
  $$("#tasks li[data-id]").forEach((li) => {
    $(".check", li).onclick = async () => { li.classList.toggle("done"); await postJSON(`/api/tasks/${li.dataset.id}/toggle`); setTimeout(() => refresh(), 600); };
    $(".del", li).onclick = async () => { li.remove(); await api(`/api/tasks/${li.dataset.id}`, { method: "DELETE" }); refresh(); };
  });
}

function renderOverview(o) {
  state.overview = o;
  state.settings = o.settings;
  const gd = o.google || {};
  if (gd.connected) {
    renderAgenda(gd.events, $("#agenda"));
    renderAgenda(gd.tomorrow, $("#agendaTomorrow"), true);
    renderNextUp(gd.events);
    $("#eventCount").textContent = gd.events.length;
  } else {
    googleMissing($("#agenda"), "Connect Google to see your calendar.");
    $("#agendaTomorrow").innerHTML = ""; $("#nextUp").classList.add("hidden");
  }
  renderInbox(gd);
  renderApprovals(o.pending || []);
  renderProjects(o.projects || []);
  renderTasks(o.tasks || []);
  $("#statDone").textContent = o.done_today;
  const pill = $("#sysPill");
  pill.classList.remove("ok", "bad");
  if (gd.connected) { pill.classList.add("ok"); $("#sysText").textContent = "All systems online"; }
  else { $("#sysText").textContent = "Google offline"; }
  checkReminders();
}

let refreshing = false;
async function refresh(force = false) {
  if (refreshing) return; refreshing = true;
  try { renderOverview(await getJSON(`/api/overview${force ? "?refresh=1" : ""}`)); }
  catch (e) { if (e.message !== "Not authenticated" && !state.offline) { $("#sysPill").classList.add("bad"); $("#sysText").textContent = "Error"; console.warn(e); } }
  finally { refreshing = false; }
}
$("#refreshBtn").addEventListener("click", () => refresh(true));

/* tasks & projects quick add */
$("#taskForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const title = $("#taskInput").value.trim(); if (!title) return;
  $("#taskInput").value = "";
  await postJSON("/api/tasks", { title, project: $("#taskProject").value || null });
  refresh();
});

function openModal(title, html) {
  $("#projectModalTitle").textContent = title;
  $("#projectModalBody").innerHTML = html;
  $("#projectModal").classList.remove("hidden");
}
$$("#projectModal [data-close]").forEach((b) => b.onclick = () => $("#projectModal").classList.add("hidden"));
$("#projectModal").addEventListener("click", (e) => { if (e.target.id === "projectModal") $("#projectModal").classList.add("hidden"); });

$("#addProjectBtn").addEventListener("click", () => {
  openModal("New project", `
    <label>Name<input id="pName" required></label>
    <label>Goal / description<textarea id="pDesc" rows="3"></textarea></label>
    <div class="row"><label>Priority<select id="pPrio"><option>low</option><option selected>medium</option><option>high</option></select></label>
    <label>Deadline<input id="pDeadline" type="date"></label></div>
    <button class="btn primary wide" type="submit">Create project</button>`);
  $("#projectForm").onsubmit = async (e) => {
    e.preventDefault();
    await postJSON("/api/projects", { name: $("#pName").value, description: $("#pDesc").value, priority: $("#pPrio").value, deadline: $("#pDeadline").value || null });
    $("#projectModal").classList.add("hidden"); refresh();
  };
});

async function openProject(id) {
  const p = await getJSON(`/api/projects/${id}`);
  openModal(p.name, `
    <p class="muted" style="margin:0">${esc(p.description || "No description.")}</p>
    <div><div class="proj-top"><span class="muted">${p.tasks_done}/${p.tasks_total} tasks · ${p.status}${p.deadline ? " · due " + p.deadline : ""}</span><span class="proj-pct">${p.progress_percent}%</span></div>
    <div class="bar"><i style="width:${p.progress_percent}%"></i></div></div>
    <section><h3>Tasks</h3>${p.tasks.map((t) => `<div class="proj-detail-task">${t.status === "done" ? "✓" : "○"} ${esc(t.title)}${t.due_date ? ` <span class="muted">· ${t.due_date}</span>` : ""}</div>`).join("") || '<p class="muted">No tasks.</p>'}</section>
    <section><h3>Progress log</h3>${p.progress_log.map((l) => `<div class="proj-log">${l.created_at.slice(0, 10)} — ${esc(l.note)}</div>`).join("") || '<p class="muted">No updates yet.</p>'}</section>
    <div class="row"><button type="button" class="btn" id="pAsk">Ask JARVIS for next steps</button></div>`);
  $("#projectForm").onsubmit = (e) => e.preventDefault();
  $("#pAsk").onclick = () => { $("#projectModal").classList.add("hidden"); setTab("assistant"); send(`Review my project "${p.name}" and tell me the next steps and what to focus on this week.`); };
}

/* ───────────────────────── reminders & notifications ───────────────────────── */
function checkReminders() {
  const events = state.overview?.google?.events || [];
  const now = new Date();
  for (const ev of events) {
    const { s } = eventTimes(ev); if (!s) continue;
    const mins = (s - now) / 6e4, key = ev.id + "@" + ev.start;
    if (mins > 0 && mins <= 10 && !state.notified.has(key)) {
      state.notified.add(key);
      try { localStorage.setItem("jarvis.notified", JSON.stringify([...state.notified].slice(-200))); } catch {}
      const addr = state.settings.address_as || "sir";
      const msg = `Pardon the interruption, ${addr}: “${ev.title}” begins in ${Math.round(mins)} minutes.`;
      toast(msg, "warn");
      if (document.visibilityState === "visible" && !state.busy && !recorder) speak(msg);
      if ("Notification" in window && Notification.permission === "granted") {
        navigator.serviceWorker?.ready.then((reg) => reg.showNotification("J.A.R.V.I.S.", { body: msg, icon: "icons/icon-192.png", tag: key }))
          .catch(() => new Notification("J.A.R.V.I.S.", { body: msg }));
      }
    }
  }
}
setInterval(checkReminders, 30000);

/* ───────────────────────── settings ───────────────────────── */
const SETTING_FIELDS = ["live_interrupt", "model", "thinking", "user_name", "address_as", "about_me", "work_start", "work_end", "voice_engine", "gemini_voice", "edge_voice", "auto_speak", "require_confirmation"];

async function openSettings() {
  $("#settings").classList.remove("hidden");
  const data = await getJSON("/api/settings");
  $("#s_gemini_voice").innerHTML = data.gemini_voices.map((v) => `<option>${v}</option>`).join("");
  $("#s_edge_voice").innerHTML = data.edge_voices.map((v) => `<option>${v}</option>`).join("");
  for (const k of SETTING_FIELDS) {
    const el = $("#s_" + k); if (!el) continue;
    if (el.type === "checkbox") el.checked = data.settings[k] === "true"; else el.value = data.settings[k] ?? "";
  }
  syncVoiceRows();
  $("#modelInfo").textContent = "Checking which Gemini models your key can use…";
  fetch("/api/health").then((r) => r.json()).then((h) => { $("#versionInfo").textContent = `JARVIS version: ${h.version}`; }).catch(() => {});
  getJSON("/api/models").then((m) => {
    const sel = $("#s_model");
    sel.innerHTML = `<option value="auto">Automatic — fastest reliable model (recommended)</option>` +
      m.available.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
    if (m.setting !== "auto" && !m.available.includes(m.setting)) sel.insertAdjacentHTML("beforeend", `<option value="${esc(m.setting)}">${esc(m.setting)}</option>`);
    sel.value = m.setting;
    $("#modelInfo").innerHTML = `Last answer came from <strong>${esc(m.last_used || "—")}</strong>. Automatic order: ${esc(m.order.slice(0, 4).join(" → "))}`;
  }).catch((e) => { $("#modelInfo").textContent = e.message; });
  $("#memories").innerHTML = data.memories.map((m) => `<li><span>${esc(m.fact)}</span><button data-mem="${m.id}" aria-label="Forget">✕</button></li>`).join("") || '<li class="muted">Nothing yet — say “Jarvis, remember that…”.</li>';
  $$("[data-mem]").forEach((b) => b.onclick = async () => { await api(`/api/memories/${b.dataset.mem}`, { method: "DELETE" }); b.parentElement.remove(); });
  loadGoogleStatus();
}
window.openSettings = guard(openSettings);

function syncVoiceRows() {
  const e = $("#s_voice_engine").value;
  $("#geminiVoiceRow").classList.toggle("hidden", e !== "gemini");
  $("#edgeVoiceRow").classList.toggle("hidden", e !== "edge");
}
$("#s_voice_engine").addEventListener("change", syncVoiceRows);

function collectSettings() {
  const out = {};
  for (const k of SETTING_FIELDS) { const el = $("#s_" + k); if (el) out[k] = el.type === "checkbox" ? String(el.checked) : el.value; }
  return out;
}

$("#saveSettings").addEventListener("click", guard(async () => {
  state.settings = await postJSON("/api/settings", collectSettings());
  $("#settings").classList.add("hidden");
  toast("Settings saved.");
}));
$("#testVoice").addEventListener("click", guard(async () => {
  state.settings = await postJSON("/api/settings", collectSettings());
  const name = state.settings.user_name ? `, ${state.settings.user_name}` : "";
  speak(`Good ${new Date().getHours() < 12 ? "morning" : new Date().getHours() < 18 ? "afternoon" : "evening"}${name}. All systems are operational, and I'm at your service.`);
}));
$("#settingsBtn").addEventListener("click", guard(openSettings));
$("#closeSettings").addEventListener("click", () => $("#settings").classList.add("hidden"));
$("#settings").addEventListener("click", (e) => { if (e.target.id === "settings") $("#settings").classList.add("hidden"); });
$("#resetChat").addEventListener("click", guard(async () => { if (!confirm("Clear the conversation history?")) return; await postJSON("/api/reset"); chatEl.innerHTML = ""; toast("Conversation cleared."); }));
$("#shutdownBtn").addEventListener("click", guard(async () => {
  if (!confirm("Shut JARVIS down? Your phone will lose connection until you start him again from the desktop icon.")) return;
  await postJSON("/api/shutdown");
  $("#settings").classList.add("hidden");
  addMessage("jarvis", "Shutting down. Until next time.");
  setTimeout(goOffline, 1200);
}));
$("#logsBtn").addEventListener("click", guard(async () => {
  const { log, path } = await getJSON("/api/logs");
  openModal("Diagnostics", `
    <section><h3>Gemini connection test</h3>
      <p class="muted" style="margin:0">Checks your network, API key and each model, with timings. Takes up to 2 minutes.</p>
      <button type="button" class="btn primary" id="testGemini">Test Gemini</button>
      <div id="testResults"></div></section>
    <section><h3>Log</h3>
      <p class="muted" style="margin:0">File: <code>${esc(path)}</code></p>
      <pre class="logbox">${esc(log)}</pre>
      <button type="button" class="btn" id="copyLog">Copy log</button></section>`);
  $("#projectForm").onsubmit = (e) => e.preventDefault();
  $("#copyLog").onclick = () => navigator.clipboard?.writeText(log).then(() => toast("Log copied."));
  $("#testGemini").onclick = guard(async () => {
    const btn = $("#testGemini"); btn.disabled = true; btn.textContent = "Testing… (up to 2 minutes)";
    try {
      const r = await postJSON("/api/diagnose/gemini");
      const text = r.steps.map((x) => `${x.ok ? "✓" : "✗"} ${x.step} — ${x.seconds}s — ${x.detail}`).join("\n");
      $("#testResults").innerHTML = `<table class="diag">${r.steps.map((x) => `<tr class="${x.ok ? "ok" : "bad"}">
        <td>${x.ok ? "✓" : "✗"}</td><td>${esc(x.step)}<div class="muted">${esc(x.detail)}</div></td><td>${x.seconds}s</td></tr>`).join("")}</table>
        <button type="button" class="btn" id="copyTest">Copy results</button>`;
      $("#copyTest").onclick = () => navigator.clipboard?.writeText(text).then(() => toast("Results copied."));
    } finally { btn.disabled = false; btn.textContent = "Test Gemini"; }
  });
}));
$("#notifBtn").addEventListener("click", async () => {
  if (!("Notification" in window)) return toast("Notifications not supported here.", "warn");
  const p = await Notification.requestPermission(); toast(p === "granted" ? "Notifications enabled." : "Notifications blocked.");
});

let googlePoll = null;
const isDesktop = ["127.0.0.1", "localhost", "[::1]"].includes(location.hostname);
async function loadGoogleStatus() {
  const st = await getJSON("/api/google/status");
  const el = $("#googleStatus");
  $("#googleSetup").classList.toggle("hidden", st.connected || st.client_secret_present);
  $("#googleReupload").classList.toggle("hidden", st.connected || !st.client_secret_present);
  if (st.connected) el.innerHTML = `✓ Connected as <strong>${esc(st.email)}</strong>`;
  else if (st.auth_running) el.innerHTML = isDesktop
    ? "A Google sign-in page has opened in your web browser. Choose your account, press <strong>Continue</strong> on the “unverified app” notice, tick all boxes and allow."
    : "A Google sign-in page has opened <strong>on your computer's browser</strong> — finish signing in there.";
  else if (!st.client_secret_present) el.innerHTML = "One-time setup: Google needs you to create a private key for JARVIS (about 10 minutes). Follow the steps below.";
  else el.innerHTML = st.error ? `<span class="err">${esc(st.error)}</span>` : "Ready. Press <strong>Sign in with Google</strong>.";
  // the sign-in button is ALWAYS available (as "Try again" while a sign-in is pending)
  $("#googleConnect").classList.toggle("hidden", st.connected || !st.client_secret_present);
  $("#googleConnect").textContent = st.auth_running || st.error ? "Try again" : "Sign in with Google";
  $("#googleReopen").classList.toggle("hidden", !(st.auth_running && st.auth_url));
  $("#googleReopen").dataset.url = st.auth_url || "";
  $("#googleCancel").classList.toggle("hidden", !st.auth_running);
  $("#googleHelp").classList.toggle("hidden", st.connected || !st.client_secret_present || !(st.auth_running || st.error));
  $("#googleDisconnect").classList.toggle("hidden", !st.connected);
  clearTimeout(googlePoll);
  if (st.auth_running) googlePoll = setTimeout(async () => {
    await loadGoogleStatus().catch(() => {});
    if (!$("#googleConnect").classList.contains("hidden") || !$("#googleDisconnect").classList.contains("hidden")) refresh(true);
  }, 2500);
}
$("#googleConnect").addEventListener("click", guard(async () => {
  const r = await postJSON("/api/google/connect"); toast(r.message); await loadGoogleStatus();
}));
$("#googleCancel").addEventListener("click", guard(async () => { await postJSON("/api/google/cancel"); await loadGoogleStatus(); }));
$("#googleReopen").addEventListener("click", guard(async () => {
  const url = $("#googleReopen").dataset.url; if (!url) return;
  if (isDesktop) await postJSON("/api/open-url", { url }); else window.open(url, "_blank");
}));
$("#googleDisconnect").addEventListener("click", guard(async () => {
  if (!confirm("Disconnect your Google account from JARVIS?")) return;
  await postJSON("/api/google/disconnect"); await loadGoogleStatus(); refresh(true);
}));
async function uploadCredentials(file) {
  if (!file) return;
  const fd = new FormData(); fd.append("file", file, file.name);
  await api("/api/google/credentials", { method: "POST", body: fd });
  toast("Credentials saved. Now sign in with Google.");
  await loadGoogleStatus();
}
$("#credFile").addEventListener("change", guard(async (e) => { await uploadCredentials(e.target.files[0]); e.target.value = ""; }));
$("#credFile2").addEventListener("change", guard(async (e) => { await uploadCredentials(e.target.files[0]); e.target.value = ""; }));
// Links in the setup guide: on the desktop open them in your normal browser (where you're signed in to Google).
document.addEventListener("click", (e) => {
  const a = e.target.closest("a[data-ext]");
  if (!a || !isDesktop) return;
  e.preventDefault();
  api("/api/open-url", { json: { url: a.href } }).catch(() => window.open(a.href, "_blank"));
});

/* ───────────────────────── updates ───────────────────────── */
function showUpdate(info) {
  const box = $("#updateInfo");
  if (!info.available) {
    box.innerHTML = `✓ JARVIS is up to date <span class="muted">(${esc(info.current || "")})</span>`;
    $("#applyUpdateBtn").classList.add("hidden");
    return;
  }
  box.innerHTML = `<strong style="color:var(--cyan)">A new version is available.</strong><ul>${info.changes.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>`;
  $("#applyUpdateBtn").classList.remove("hidden");
}
$("#checkUpdateBtn").addEventListener("click", guard(async () => {
  $("#updateInfo").textContent = "Checking GitHub for updates…";
  try { showUpdate(await getJSON("/api/update/check")); }
  catch (e) { $("#updateInfo").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}));
$("#applyUpdateBtn").addEventListener("click", guard(async () => {
  $("#applyUpdateBtn").disabled = true;
  await postJSON("/api/update/apply");
  state.updating = true;
  const poll = async () => {
    try {
      const st = await (await fetch("/api/update/status", { cache: "no-store" })).json();
      if (st.error) {
        state.updating = false; $("#applyUpdateBtn").disabled = false;
        $("#updateInfo").innerHTML = `<span class="err">Update failed: ${esc(st.error)}</span>`;
        return;
      }
      $("#updateInfo").textContent = (st.step || "Updating") + "…";
    } catch { goOffline(); $("#updateInfo").textContent = "Restarting JARVIS…"; return; }  // server restarting
    setTimeout(poll, 1200);
  };
  poll();
}));
async function silentUpdateCheck() {
  if (!isDesktop || state.updateChecked) return;
  state.updateChecked = true;
  try {
    const info = await getJSON("/api/update/check");
    if (info.available) toast("A JARVIS update is available — Settings → Update & restart.");
  } catch {}
}

/* ───────────────────────── mobile tabs ───────────────────────── */
function setTab(tab) {
  state.tab = tab;
  $$("#tabbar button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $$(".layout [data-tab]").forEach((el) => el.classList.toggle("m-hide", !el.dataset.tab.split(" ").includes(tab)));
  if (tab === "assistant") chatEl.scrollTop = chatEl.scrollHeight;
}
$$("#tabbar button").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
setTab("assistant");

/* ───────────────────────── boot ───────────────────────── */
async function boot() {
  let s;
  try { s = await (await fetch("/api/session", { cache: "no-store" })).json(); }
  catch { goOffline(); return; }
  if (!s.authenticated) { showLogin(); return; }
  const health = await (await fetch("/api/health")).json();
  if (!health.gemini_key) { $("#sysPill").classList.add("bad"); toast("GEMINI_API_KEY missing in .env", "bad"); }
  try {
    const hist = await getJSON("/api/history");
    chatEl.innerHTML = "";
    hist.forEach((m) => addMessage(m.role === "user" ? "user" : "jarvis", m.text));
  } catch {}
  if (!chatEl.children.length) {
    const h = new Date().getHours();
    addMessage("jarvis", `Good ${h < 12 ? "morning" : h < 18 ? "afternoon" : "evening"}. JARVIS online — how may I help? Tap the microphone, hold Space, or type.`);
  }
  await refresh();
  setTimeout(silentUpdateCheck, 5000);
  if (!state.pollStarted) { state.pollStarted = true; setInterval(() => document.visibilityState === "visible" && !state.offline && refresh(), 60000); }
}
document.addEventListener("visibilitychange", () => document.visibilityState === "visible" && refresh());

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
boot();
