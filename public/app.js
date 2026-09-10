const $ = (id) => document.getElementById(id);

const state = {
  blob: null,
  blobUrl: null,
  engine: "mai",
  recorder: null,
  chunks: [],
  timer: null,
  startedAt: 0,
  phrases: [],
};

/* ---------------- Recording ---------------- */
async function toggleRecord() {
  if (state.recorder && state.recorder.state === "recording") {
    state.recorder.stop();
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    showError("Microphone permission denied. Allow mic access or upload a file instead.");
    return;
  }
  state.chunks = [];
  const mime = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "";
  state.recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
  state.recorder.ondataavailable = (e) => e.data.size && state.chunks.push(e.data);
  state.recorder.onstop = () => {
    stream.getTracks().forEach((t) => t.stop());
    const blob = new Blob(state.chunks, { type: state.recorder.mimeType || "audio/webm" });
    setAudio(blob, "recording.webm");
    stopTimer();
    $("recordBtn").classList.remove("recording");
    $("recordState").textContent = "Recorded \u2713  ready to transcribe";
  };
  state.recorder.start();
  $("recordBtn").classList.add("recording");
  $("recordState").textContent = "Recording\u2026 tap to stop";
  startTimer();
}

function startTimer() {
  state.startedAt = Date.now();
  state.timer = setInterval(() => {
    const s = Math.floor((Date.now() - state.startedAt) / 1000);
    $("recordTimer").textContent =
      String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
  }, 250);
}
function stopTimer() { clearInterval(state.timer); }

/* ---------------- Audio source ---------------- */
function setAudio(blob, name) {
  if (state.blobUrl) URL.revokeObjectURL(state.blobUrl);
  state.blob = blob;
  state.blobUrl = URL.createObjectURL(blob);
  const player = $("player");
  player.src = state.blobUrl;
  player.hidden = false;
  $("transcribeBtn").disabled = false;
  if (name) {
    $("fileName").textContent = name;
    $("fileName").hidden = false;
  }
}

/* ---------------- Transcribe ---------------- */
async function transcribe() {
  if (!state.blob) return;
  setBusy(true);
  hide("errorBox");
  hide("biasNote");

  // The MAI-transcribe backend decodes with libsndfile, which rejects WebM/Opus.
  // Convert whatever we captured to 16 kHz mono WAV in the browser first.
  let uploadBlob = state.blob;
  try {
    uploadBlob = await toWav16kMono(state.blob);
  } catch (e) {
    uploadBlob = state.blob;
  }

  // ---- BUILD THE REQUEST: each option below becomes a form field the backend
  // turns into the JSON `definition` that selects the model + features ----------
  const fd = new FormData();
  fd.append("audio", uploadBlob, "audio.wav");        // the audio to transcribe
  fd.append("engine", state.engine);                  // "mai" -> MAI-Transcribe-2 model
  fd.append("biasing", $("biasingToggle").checked ? "true" : "false"); // keyword biasing on/off
  fd.append("phrases", $("phrases").value);           // the domain terms to boost
  fd.append("diarization", $("diarizationToggle").checked ? "true" : "false"); // speaker labels on/off

  try {
    const res = await fetch("/api/transcribe", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) {
      showError(data.error || data.detail || `Request failed (HTTP ${res.status}).`);
      return;
    }
    render(data);
  } catch (e) {
    showError("Could not reach the backend. Is the server running?");
  } finally {
    setBusy(false);
  }
}

function setBusy(busy) {
  $("transcribeBtn").disabled = busy || !state.blob;
  document.querySelector(".btn-spinner").hidden = !busy;
  document.querySelector(".btn-label").textContent = busy ? "Transcribing\u2026" : "Transcribe";
}

/* ---------------- Render ---------------- */
const LANG_NAMES = { en: "English", hi: "Hindi", mr: "Marathi", ta: "Tamil", te: "Telugu", bn: "Bengali", gu: "Gujarati", kn: "Kannada", pa: "Punjabi", ur: "Urdu", de: "German", fr: "French", es: "Spanish", ja: "Japanese", zh: "Chinese" };

function langClass(locale) {
  const base = (locale || "").toLowerCase().split("-")[0];
  if (base === "en") return "lang-en";
  if (base === "hi") return "lang-hi";
  return "lang-other";
}
function langLabel(locale) {
  const base = (locale || "").toLowerCase().split("-")[0];
  return (base || "?").toUpperCase();
}
function langName(locale) {
  const base = (locale || "").toLowerCase().split("-")[0];
  return LANG_NAMES[base] || (base ? base.toUpperCase() : "unknown");
}

function render(data) {
  hide("empty");
  hide("speakerNote");
  $("statBar").hidden = false;

  $("statEngine").textContent = "MAI-Transcribe-2";

  const names = [...new Set(data.detectedLocales.map(langName))];
  $("statLangs").textContent = names.length ? names.join(" + ") : "\u2014";
  $("statAudio").textContent = (data.audioMs / 1000).toFixed(1) + "s";
  $("statLatency").textContent = (data.latencyMs / 1000).toFixed(2) + "s";
  const ratio = data.audioMs && data.latencyMs ? (data.audioMs / data.latencyMs) : 0;
  $("statSpeed").textContent = ratio ? `${ratio.toFixed(1)}\u00d7 real-time` : "\u2014";

  const box = $("transcript");
  box.hidden = false;
  box.innerHTML = "";

  // ---- SUMMARY: detected languages + confidence (confidence shown only if the
  // model returns one; MAI-Transcribe-2 returns 0, so it stays hidden) --------
  const summary = document.createElement("div");
  summary.className = "transcript-summary";
  const langNames = data.detectedLocales.map(langName);
  const langsText = langNames.length ? langNames.join(", ") : "\u2014";
  let confItem = "";
  const scores = data.phrases.map((p) => p.confidence).filter((c) => typeof c === "number" && c > 0);
  if (scores.length) {
    const avg = Math.round((scores.reduce((a, b) => a + b, 0) / scores.length) * 100);
    confItem = `<span class="summary-item"><span class="summary-k">Confidence</span><span class="summary-v">${avg}%</span></span>`;
  }
  summary.innerHTML =
    `<span class="summary-item"><span class="summary-k">Languages</span><span class="summary-v">${escapeHtml(langsText)}</span></span>` +
    confItem;
  box.appendChild(summary);

  const phrases = data.phrases.length
    ? data.phrases
    : [{ text: data.combinedText, offsetMs: 0, durationMs: data.audioMs, locale: data.detectedLocales[0] || "" }];

  const detectedSpeakers = new Set();
  phrases.forEach((p) => {
    // Each phrase becomes its own conversation line: "Speaker N :- text".
    const line = document.createElement("div");
    line.className = "phrase turn";
    line.dataset.start = p.offsetMs;
    line.dataset.end = p.offsetMs + p.durationMs;

    // ---- SPEAKER LABELS: shown on EVERY line when diarization is on ---------
    // Each phrase carries a numeric `speaker` id (0,1,...); we show it 1-based.
    const hasSpeaker = p.speaker !== null && p.speaker !== undefined;
    let speakerLabel = "";
    if (hasSpeaker) {
      const speakerNumber = Number(p.speaker) + 1;
      detectedSpeakers.add(speakerNumber);
      speakerLabel = `<span class="turn-speaker">Speaker ${speakerNumber} :-</span>`;
    }
    // Detected language per phrase (small EN/HI chip right before the text).
    const lang = p.locale
      ? `<span class="lang-chip ${langClass(p.locale)}" title="${langName(p.locale)}">${langLabel(p.locale)}</span>`
      : "";
    line.innerHTML = speakerLabel + `<span class="turn-body">${lang}${escapeHtml(p.text)}</span>`;
    line.onclick = () => { $("player").currentTime = p.offsetMs / 1000; $("player").play(); };
    box.appendChild(line);
  });

  // Small footer note summarizing how many speakers diarization found.
  if (data.diarizationEnabled) {
    $("speakerNote").hidden = false;
    $("speakerNote").innerHTML = detectedSpeakers.size
      ? `<b>Speaker labels on.</b> Detected ${detectedSpeakers.size} speaker${detectedSpeakers.size === 1 ? "" : "s"}.`
      : "<b>Speaker labels on.</b> The service did not detect distinct speakers in this audio.";
  }

  // Small footer note confirming which biasing terms were sent to the model.
  if (data.biasedPhrases && data.biasedPhrases.length && $("biasingToggle").checked) {
    $("biasNote").hidden = false;
    $("biasNote").innerHTML =
      `<b>Keyword biasing on.</b> Boosted ${data.biasedPhrases.length} domain terms: ` +
      data.biasedPhrases.map(escapeHtml).join(", ") + ".";
  }
}

/* ---------------- Karaoke highlight ---------------- */
function syncHighlight() {
  const t = $("player").currentTime * 1000;
  document.querySelectorAll(".phrase").forEach((el) => {
    const on = t >= +el.dataset.start && t < +el.dataset.end;
    el.classList.toggle("active", on);
  });
}

/* ---------------- Helpers ---------------- */
async function toWav16kMono(blob) {
  const arrayBuf = await blob.arrayBuffer();
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  const decodeCtx = new AudioCtx();
  let decoded;
  try {
    decoded = await decodeCtx.decodeAudioData(arrayBuf);
  } finally {
    decodeCtx.close();
  }
  const rate = 16000;
  const frames = Math.max(1, Math.ceil(decoded.duration * rate));
  const offline = new OfflineAudioContext(1, frames, rate);
  const src = offline.createBufferSource();
  src.buffer = decoded;
  src.connect(offline.destination);
  src.start(0);
  const rendered = await offline.startRendering();
  return encodeWavPCM16(rendered.getChannelData(0), rate);
}

function encodeWavPCM16(samples, sampleRate) {
  const dataSize = samples.length * 2;
  const view = new DataView(new ArrayBuffer(44 + dataSize));
  let o = 0;
  const str = (s) => { for (let i = 0; i < s.length; i++) view.setUint8(o++, s.charCodeAt(i)); };
  const u32 = (v) => { view.setUint32(o, v, true); o += 4; };
  const u16 = (v) => { view.setUint16(o, v, true); o += 2; };
  str("RIFF"); u32(36 + dataSize); str("WAVE");
  str("fmt "); u32(16); u16(1); u16(1); u32(sampleRate); u32(sampleRate * 2); u16(2); u16(16);
  str("data"); u32(dataSize);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    o += 2;
  }
  return new Blob([view], { type: "audio/wav" });
}

function showError(msg) { const b = $("errorBox"); b.hidden = false; b.textContent = msg; }
function hide(id) { $(id).hidden = true; }
function escapeHtml(s) { return (s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

/* ---------------- Wire up ---------------- */
$("recordBtn").addEventListener("click", toggleRecord);
$("transcribeBtn").addEventListener("click", transcribe);
$("player").addEventListener("timeupdate", syncHighlight);

$("fileInput").addEventListener("change", (e) => {
  const f = e.target.files[0];
  if (f) setAudio(f, f.name);
});

const dz = $("dropZone");
["dragover", "dragenter"].forEach((ev) =>
  dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); })
);
["dragleave", "drop"].forEach((ev) =>
  dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); })
);
dz.addEventListener("drop", (e) => {
  const f = e.dataTransfer.files[0];
  if (f) setAudio(f, f.name);
});

