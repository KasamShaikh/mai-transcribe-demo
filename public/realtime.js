/* Real time tab client — fully isolated from app.js (the Batch tab).
   Streams 16 kHz PCM over a WebSocket and renders one of three pipelines:
   MAI near-real-time, Standard realtime, or Hybrid. */
const $ = (sel) => document.querySelector(sel);

const LANG_NAMES = {
  en: "English", hi: "Hindi", mr: "Marathi", ta: "Tamil", te: "Telugu",
  bn: "Bengali", gu: "Gujarati", kn: "Kannada", pa: "Punjabi", ur: "Urdu",
  de: "German", fr: "French", es: "Spanish", ja: "Japanese", zh: "Chinese",
};

const state = {
  mode: "mai",
  ws: null,
  ctx: null,
  node: null,
  stream: null,
  streaming: false,
  running: false,
  startedAt: 0,
  finalizeAt: 0,
  timer: null,
  committed: 0,
};

const MODE_HINTS = {
  mai: "Near real time — audio is sent in rolling ~8-second windows to MAI-Transcribe-2. Text appears every few seconds. Speaker labels are off in this mode.",
  standard: "True real time — the Azure Speech SDK streams interim words as you speak and commits each sentence when you pause.",
  hybrid: "Best of both — you see Standard interim captions live, then the whole recording is replaced by one clean MAI-Transcribe-2 pass when you stop.",
};

/* ---------------- Mode selection ---------------- */
function selectMode(mode) {
  state.mode = mode;
  document.querySelectorAll("#rtModeSeg .seg-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.mode === mode)
  );
  $("#rtHint").textContent = MODE_HINTS[mode];
  const diarField = $("#rtDiarField");
  const diarOn = mode === "hybrid";
  $("#rtDiar").disabled = !diarOn;
  diarField.classList.toggle("disabled", !diarOn);
  $("#rtDiarHint").textContent = diarOn
    ? "Labels each speaker in the final MAI transcript."
    : "Available in Hybrid mode only (needs the full-recording MAI pass).";
}

/* ---------------- Start / Stop ---------------- */
async function start() {
  resetOutput();
  hide("#rtError");
  setButtons(true);
  setStatus("connecting", "Connecting…");
  try {
    await setupCapture();
  } catch (e) {
    showError("Microphone permission denied. Allow mic access to use live transcription.");
    cleanup();
    return;
  }
  openSocket();
}

async function stop() {
  if (!state.running && !state.ws) return;
  state.streaming = false;
  state.finalizeAt = Date.now();
  stopCapture();
  setStatus("finishing", state.mode === "hybrid" ? "Finalizing with MAI…" : "Finishing…");
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify({ type: "stop" }));
  }
}

/* ---------------- Capture ---------------- */
async function setupCapture() {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
  });
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  await ctx.audioWorklet.addModule("pcm-capture-worklet.js?v=1");
  if (ctx.state === "suspended") await ctx.resume();

  const source = ctx.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(ctx, "pcm-capture", {
    processorOptions: { targetRate: 16000 },
  });
  node.port.onmessage = (e) => {
    if (state.streaming && state.ws && state.ws.readyState === WebSocket.OPEN) {
      state.ws.send(e.data);
    }
  };
  // Keep the graph pulling audio without producing any output.
  const sink = ctx.createGain();
  sink.gain.value = 0;
  source.connect(node);
  node.connect(sink);
  sink.connect(ctx.destination);

  state.stream = stream;
  state.ctx = ctx;
  state.node = node;
}

function stopCapture() {
  try { if (state.node) state.node.port.onmessage = null; } catch (e) {}
  try { if (state.node) state.node.disconnect(); } catch (e) {}
  try { if (state.stream) state.stream.getTracks().forEach((t) => t.stop()); } catch (e) {}
  try { if (state.ctx) state.ctx.close(); } catch (e) {}
  state.node = null;
  state.stream = null;
  state.ctx = null;
}

/* ---------------- WebSocket ---------------- */
function wsUrl() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws/realtime`;
}

function openSocket() {
  const ws = new WebSocket(wsUrl());
  ws.binaryType = "arraybuffer";
  state.ws = ws;
  ws.onopen = () => {
    ws.send(JSON.stringify({
      type: "config",
      mode: state.mode,
      phrases: $("#rtPhrases").value,
      biasing: $("#rtBiasing").checked,
      diarization: state.mode === "hybrid" && $("#rtDiar").checked,
      profanity: "",
    }));
  };
  ws.onmessage = (e) => {
    try { handleEvent(JSON.parse(e.data)); } catch (err) {}
  };
  ws.onerror = () => showError("Connection error. Is the server running?");
  ws.onclose = () => { if (state.running) finish(); };
}

/* ---------------- Event handling ---------------- */
function handleEvent(ev) {
  switch (ev.type) {
    case "ready": onReady(); break;
    case "status": onStatus(ev); break;
    case "partial": setInterim(ev.text); break;
    case "final": commitLine(ev); setInterim(""); break;
    case "chunk": (ev.phrases || []).forEach(commitLine); break;
    case "replacement": renderFinal(ev.result); break;
    case "error": onError(ev); break;
    case "done": finish(); break;
  }
}

function onReady() {
  state.streaming = true;
  state.running = true;
  state.startedAt = Date.now();
  startTimer();
  const label = state.mode === "standard" ? "Listening — speak now" : "Listening…";
  setStatus("live", label);
  $("#rtStateChip").hidden = false;
  $("#rtStateChip").textContent = modeLabel(state.mode);
}

function onStatus(ev) {
  if (ev.state === "processing") setStatus("live", "Listening… (transcribing window)");
  else if (ev.state === "finalizing") setStatus("finishing", "Finalizing with MAI-Transcribe-2…");
}

function onError(ev) {
  if (ev.recoverable) {
    flash(ev.message);
  } else {
    showError(ev.message || "Live transcription error.");
  }
}

function finish() {
  if (state.finalizeAt && state.mode === "hybrid") {
    const secs = ((Date.now() - state.finalizeAt) / 1000).toFixed(1);
    $("#rtFinalNote").textContent = `Finalized with MAI-Transcribe-2 in ${secs}s.`;
    $("#rtFinalNote").hidden = false;
  }
  state.running = false;
  state.streaming = false;
  stopCapture();
  stopTimer();
  try { if (state.ws) state.ws.close(); } catch (e) {}
  state.ws = null;
  setButtons(false);
  if (state.committed > 0 || $("#rtSummary").childElementCount) {
    setStatus("done", "Done");
  } else {
    setStatus("idle", "No speech captured");
    show("#rtEmpty");
  }
}

function cleanup() {
  state.running = false;
  state.streaming = false;
  stopCapture();
  stopTimer();
  try { if (state.ws) state.ws.close(); } catch (e) {}
  state.ws = null;
  setButtons(false);
}

/* ---------------- Rendering ---------------- */
function resetOutput() {
  state.committed = 0;
  $("#rtOutput").innerHTML = "";
  $("#rtSummary").innerHTML = "";
  $("#rtSummary").hidden = true;
  setInterim("");
  $("#rtFinalNote").hidden = true;
  $("#rtStateChip").hidden = true;
  hide("#rtEmpty");
}

function commitLine(p) {
  hide("#rtEmpty");
  state.committed += 1;
  const line = document.createElement("div");
  line.className = "phrase turn";
  let speaker = "";
  if (p.speaker !== null && p.speaker !== undefined) {
    speaker = `<span class="turn-speaker">Speaker ${Number(p.speaker) + 1} :-</span>`;
  }
  const lang = p.locale
    ? `<span class="lang-chip ${langClass(p.locale)}" title="${langName(p.locale)}">${langLabel(p.locale)}</span>`
    : "";
  line.innerHTML = speaker + `<span class="turn-body">${lang}${escapeHtml(p.text)}</span>`;
  $("#rtOutput").appendChild(line);
  scrollOutput();
}

function setInterim(text) {
  const el = $("#rtInterim");
  if (!text) { el.hidden = true; el.textContent = ""; return; }
  el.hidden = false;
  el.textContent = text;
  scrollOutput();
}

function renderFinal(result) {
  $("#rtOutput").innerHTML = "";
  setInterim("");
  state.committed = 0;

  const summary = $("#rtSummary");
  summary.hidden = false;
  const langs = (result.detectedLocales || []).map(langName);
  const langsText = langs.length ? langs.join(", ") : "—";
  const audio = result.audioMs ? (result.audioMs / 1000).toFixed(1) + "s" : "—";
  summary.innerHTML =
    `<span class="summary-item"><span class="summary-k">Final engine</span><span class="summary-v">MAI-Transcribe-2</span></span>` +
    `<span class="summary-item"><span class="summary-k">Languages</span><span class="summary-v">${escapeHtml(langsText)}</span></span>` +
    `<span class="summary-item"><span class="summary-k">Audio</span><span class="summary-v">${audio}</span></span>`;

  const phrases = result.phrases && result.phrases.length
    ? result.phrases
    : [{ text: result.combinedText, locale: (result.detectedLocales || [])[0] || "" }];
  phrases.forEach(commitLine);
}

/* ---------------- Status / timer / helpers ---------------- */
function modeLabel(mode) {
  return { mai: "MAI Near-real-time", standard: "Standard Realtime", hybrid: "Hybrid" }[mode];
}
function setStatus(kind, text) {
  const dot = $("#rtDot");
  dot.className = "live-dot " + kind;
  $("#rtStatusText").textContent = text;
}
function setButtons(running) {
  $("#rtStart").disabled = running;
  $("#rtStop").disabled = !running;
  document.querySelectorAll("#rtModeSeg .seg-btn").forEach((b) => (b.disabled = running));
  $("#rtPhrases").disabled = running;
  $("#rtBiasing").disabled = running;
  $("#rtDiar").disabled = running || state.mode !== "hybrid";
}
function startTimer() {
  state.startedAt = Date.now();
  state.timer = setInterval(() => {
    const s = Math.floor((Date.now() - state.startedAt) / 1000);
    $("#rtTimer").textContent =
      String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
  }, 250);
}
function stopTimer() { clearInterval(state.timer); }

function langClass(locale) {
  const b = (locale || "").toLowerCase().split("-")[0];
  if (b === "en") return "lang-en";
  if (b === "hi") return "lang-hi";
  return "lang-other";
}
function langLabel(locale) { return ((locale || "").toLowerCase().split("-")[0] || "?").toUpperCase(); }
function langName(locale) {
  const b = (locale || "").toLowerCase().split("-")[0];
  return LANG_NAMES[b] || (b ? b.toUpperCase() : "unknown");
}
function scrollOutput() {
  const box = $("#rtScroll");
  box.scrollTop = box.scrollHeight;
}
function flash(msg) {
  const n = $("#rtFlash");
  n.textContent = msg;
  n.hidden = false;
  clearTimeout(n._t);
  n._t = setTimeout(() => { n.hidden = true; }, 4000);
}
function showError(msg) { const b = $("#rtError"); b.hidden = false; b.textContent = msg; }
function show(sel) { $(sel).hidden = false; }
function hide(sel) { $(sel).hidden = true; }
function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------------- Wire up ---------------- */
document.querySelectorAll("#rtModeSeg .seg-btn").forEach((b) =>
  b.addEventListener("click", () => { if (!state.running) selectMode(b.dataset.mode); })
);
$("#rtStart").addEventListener("click", start);
$("#rtStop").addEventListener("click", stop);
window.addEventListener("pagehide", cleanup);
window.addEventListener("beforeunload", cleanup);
selectMode("mai");
