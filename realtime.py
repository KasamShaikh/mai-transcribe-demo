"""Real time transcription backend (isolated from the Batch implementation).

Exposes a single WebSocket, ``/ws/realtime``, that drives three pipelines:

* ``mai``      - near-real-time: rolling ~8 s windows sent to MAI-Transcribe-2.
* ``standard`` - true realtime interim results via the Azure Speech SDK.
* ``hybrid``   - standard interim captions while recording, then one full
                 MAI-Transcribe-2 pass that atomically replaces the transcript.

The windowing/offset helpers are pure and unit-tested. Azure access uses the
same keyless credential as the Batch tab and never leaves the server.
"""
import asyncio
import json
from uuid import uuid4

from fastapi import APIRouter, WebSocket

import english_summary
import mai_client
from mai_client import ENDPOINT, SAMPLE_RATE, _credential

router = APIRouter()

BYTES_PER_MS = SAMPLE_RATE * 2 // 1000          # 32 bytes/ms (16 kHz, mono, 16-bit)
MAI_STEP_MS = 7000                              # new audio committed per window
MAI_CONTEXT_MS = 1000                           # acoustic lead-in re-sent each window
MAX_SESSION_MS = 5 * 60 * 1000                  # hard cap to bound memory
MAX_SESSION_BYTES = MAX_SESSION_MS * BYTES_PER_MS
OVERLAP_TOLERANCE_MS = 400                       # slack when dropping already-committed context


# --------------------------------------------------------------------------- #
# Pure helpers (no Azure, no I/O) — covered by tests/test_realtime.py
# --------------------------------------------------------------------------- #
class MaiRollingBuffer:
    """Accumulates PCM and yields overlapping windows on a fixed cadence.

    Each window re-sends ``context_ms`` of already-committed audio so MAI has
    acoustic lead-in, then ``step_ms`` of new audio. Offsets are reconciled onto
    the session timeline by :func:`filter_new_phrases`.
    """

    def __init__(self, step_ms: int = MAI_STEP_MS, context_ms: int = MAI_CONTEXT_MS,
                 sample_rate: int = SAMPLE_RATE):
        self.step_ms = step_ms
        self.context_ms = context_ms
        self.rate = sample_rate
        self.buf = bytearray()          # all PCM received so far
        self.committed_ms = 0           # audio already sent and turned into text

    # Convert between byte counts and milliseconds for 16-bit mono PCM.
    def _ms(self, nbytes: int) -> float:
        return nbytes / 2 / self.rate * 1000

    def _bytes(self, ms: float) -> int:
        return int(ms * self.rate / 1000) * 2

    def add(self, pcm: bytes) -> None:
        """Append newly received audio."""
        self.buf += pcm

    def duration_ms(self) -> float:
        """Total audio buffered so far."""
        return self._ms(len(self.buf))

    def has_window(self) -> bool:
        """True once a full step of new audio is available."""
        return self.duration_ms() >= self.committed_ms + self.step_ms

    def take_window(self) -> tuple[bytes, int, int]:
        """Return (pcm, window_start_ms, boundary_ms) for the next full window."""
        # Start one context-length earlier so MAI hears the lead-in.
        start_ms = max(0, self.committed_ms - self.context_ms)
        end_ms = self.committed_ms + self.step_ms
        return bytes(self.buf[self._bytes(start_ms):self._bytes(end_ms)]), start_ms, self.committed_ms

    def advance(self) -> None:
        """Mark the current step as committed."""
        self.committed_ms += self.step_ms

    def flush_window(self) -> tuple[bytes, int, int] | None:
        """Return the trailing partial window (audio past the commit point)."""
        if self.duration_ms() <= self.committed_ms:
            return None
        start_ms = max(0, self.committed_ms - self.context_ms)
        return bytes(self.buf[self._bytes(start_ms):]), start_ms, self.committed_ms


def filter_new_phrases(phrases: list[dict], window_start_ms: int, boundary_ms: int) -> list[dict]:
    """Shift window-relative offsets onto the session timeline and drop phrases
    that start inside the already-committed context region (overlap de-dup)."""
    out: list[dict] = []
    for p in phrases:
        # Convert the window-relative offset to the session timeline.
        session_offset = p.get("offsetMs", 0) + window_start_ms
        # Keep only phrases that begin after the already-committed boundary.
        if session_offset >= boundary_ms - OVERLAP_TOLERANCE_MS:
            shifted = dict(p)
            shifted["offsetMs"] = session_offset
            out.append(shifted)
    return out


def parse_config(raw: dict) -> dict:
    """Validate the initial client config message. Raises ValueError on misuse."""
    # The first frame must be a config message naming a known pipeline.
    if raw.get("type") != "config":
        raise ValueError("first message must be a config frame")
    mode = raw.get("mode")
    if mode not in ("mai", "standard", "hybrid"):
        raise ValueError("mode must be one of mai, standard, hybrid")
    # Keyword biasing: accept a list or a comma/newline string; ignore if biasing is off.
    biasing = raw.get("biasing", True)
    raw_phrases = raw.get("phrases") or []
    if isinstance(raw_phrases, str):
        raw_phrases = raw_phrases.replace("\n", ",").split(",")
    phrases = [p.strip() for p in raw_phrases if str(p).strip()] if biasing else []
    profanity = (raw.get("profanity") or "").strip()
    # Return a clean config with safe defaults.
    return {
        "mode": mode,
        "phrases": phrases,
        "profanity": profanity,
        "diarization": bool(raw.get("diarization", False)),
        "language": (raw.get("language") or "en-US").strip(),
    }


# --------------------------------------------------------------------------- #
# WebSocket plumbing
# --------------------------------------------------------------------------- #
async def _sender(ws: WebSocket, out_q: asyncio.Queue) -> None:
    """Drain the outgoing event queue to the browser until a None sentinel arrives."""
    while True:
        ev = await out_q.get()
        if ev is None:
            break
        try:
            await ws.send_json(ev)
        except Exception:
            break


async def _read_config(ws: WebSocket) -> dict:
    """Read and validate the first (config) frame from the client."""
    msg = await ws.receive()
    if msg["type"] == "websocket.disconnect":
        raise ValueError("client disconnected before config")
    text = msg.get("text")
    if text is None:
        raise ValueError("expected a text config frame")
    return parse_config(json.loads(text))


@router.websocket("/ws/realtime")
async def realtime_ws(ws: WebSocket) -> None:
    """One live session: read config, run the chosen pipeline, stream events back."""
    await ws.accept()
    loop = asyncio.get_running_loop()
    out_q: asyncio.Queue = asyncio.Queue()
    sender = asyncio.create_task(_sender(ws, out_q))
    # Remembers every final phrase so the English transcript can be built at the end.
    collector = english_summary.TranscriptCollector()

    def put(ev: dict) -> None:
        """Record the event for the collector, then queue it for the browser."""
        collector.add(ev)
        out_q.put_nowait(ev)

    async def emit(ev: dict) -> None:
        # Just before the final `done`, add the English transcript + summary.
        if ev.get("type") == "done":
            await _emit_english(put, collector)
        put(ev)

    def emit_ts(ev: dict) -> None:
        """Thread-safe emit for Speech SDK callback threads."""
        try:
            loop.call_soon_threadsafe(put, ev)
        except RuntimeError:
            pass

    # Reject bad configs early with a clear, non-recoverable error.
    try:
        cfg = await _read_config(ws)
    except Exception as exc:
        await emit({"type": "error", "recoverable": False, "message": str(exc)})
        await emit({"type": "done"})
        await out_q.put(None)
        await sender
        await _safe_close(ws)
        return

    # Tell the browser it can start streaming audio.
    await emit({"type": "ready", "sessionId": uuid4().hex, "mode": cfg["mode"]})

    # Run the selected pipeline; any unexpected failure ends the session cleanly.
    try:
        if cfg["mode"] == "mai":
            await _run_mai(ws, emit, cfg)
        else:
            await _run_standard(ws, emit, emit_ts, cfg, hybrid=(cfg["mode"] == "hybrid"))
    except Exception as exc:
        await emit({"type": "error", "recoverable": False, "message": f"session error: {exc}"})
        await emit({"type": "done"})

    # Flush remaining events, then close the socket.
    await out_q.put(None)
    await sender
    await _safe_close(ws)


async def _emit_english(put, collector) -> None:
    """Translate + summarize the final transcript once, just before `done`."""
    text = collector.text()
    # Nothing to translate, or the feature is not configured: skip silently.
    if not text or not english_summary.is_configured():
        return
    # Let the UI show a "generating..." status while the model runs.
    put({"type": "status", "state": "summarizing"})
    try:
        result = await english_summary.summarize_to_english(text)
    except Exception:
        # Non-fatal: the original transcript is already on screen.
        put({"type": "error", "recoverable": True,
             "message": "Could not generate the English transcript and summary."})
        return
    put({"type": "english", "transcript": result["englishTranscript"],
         "summary": result["summary"]})


async def _safe_close(ws: WebSocket) -> None:
    """Close the socket, ignoring errors if the client is already gone."""
    try:
        await ws.close()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# MAI near-real-time worker
# --------------------------------------------------------------------------- #
async def _run_mai(ws: WebSocket, emit, cfg: dict) -> None:
    """MAI pipeline: buffer mic audio and transcribe it in rolling windows."""
    rb = MaiRollingBuffer()
    definition = mai_client.build_definition(cfg["phrases"], cfg["profanity"], diarization=False)
    total = 0
    aborted = False

    # Receive audio frames until the client stops, disconnects or hits the cap.
    while True:
        msg = await ws.receive()
        if msg["type"] == "websocket.disconnect":
            aborted = True
            break
        audio = msg.get("bytes")
        if audio is not None:
            # Enforce the session length cap to bound memory.
            total += len(audio)
            if total > MAX_SESSION_BYTES:
                await emit({"type": "error", "recoverable": False,
                            "message": "Reached the 5-minute live limit; stopping."})
                aborted = True
                break
            # Transcribe every full window of new audio.
            rb.add(audio)
            while rb.has_window():
                await _mai_emit_window(emit, rb.take_window(), definition)
                rb.advance()
        else:
            text = msg.get("text")
            if text and _is_stop(text):
                break

    # On a normal stop, transcribe the trailing partial window too.
    if not aborted:
        flushed = rb.flush_window()
        if flushed:
            await _mai_emit_window(emit, flushed, definition)
    await emit({"type": "done"})


async def _mai_emit_window(emit, window: tuple[bytes, int, int], definition: dict) -> None:
    """Send one audio window to MAI and emit only its newly spoken phrases."""
    pcm, start_ms, boundary_ms = window
    if not pcm:
        return
    await emit({"type": "status", "state": "processing", "windowStartMs": start_ms})
    # A failed window is reported but does not end the session.
    try:
        raw = await mai_client.transcribe_wav(mai_client.pcm16_to_wav(pcm), definition, timeout=60)
    except mai_client.MaiError as exc:
        await emit({"type": "error", "recoverable": True,
                    "message": f"MAI window failed ({exc.status_code})."})
        return
    except Exception:
        await emit({"type": "error", "recoverable": True, "message": "MAI window error."})
        return
    # Drop phrases already committed from the overlapping context, then emit the rest.
    norm = mai_client.normalize(raw)
    kept = filter_new_phrases(norm["phrases"], start_ms, boundary_ms)
    await emit({"type": "chunk", "phrases": kept, "windowStartMs": start_ms})


# --------------------------------------------------------------------------- #
# Standard / Hybrid worker (Azure Speech SDK continuous recognition)
# --------------------------------------------------------------------------- #
async def _run_standard(ws: WebSocket, emit, emit_ts, cfg: dict, hybrid: bool) -> None:
    """Standard pipeline (Speech SDK streaming); in Hybrid also keeps the full audio."""
    # The Speech SDK is optional at import time; fail clearly if it is missing.
    try:
        import azure.cognitiveservices.speech as speechsdk
    except Exception:
        await emit({"type": "error", "recoverable": False,
                    "message": "Speech SDK is not available on the server."})
        await emit({"type": "done"})
        return

    # Create the recognizer and route its events to the WebSocket.
    recognizer, push = _build_standard_recognizer(speechsdk, cfg)
    _wire_standard_callbacks(speechsdk, recognizer, emit_ts)

    # Start streaming recognition (blocking SDK call, so run it in a thread).
    try:
        await asyncio.to_thread(recognizer.start_continuous_recognition)
    except Exception as exc:
        await emit({"type": "error", "recoverable": False,
                    "message": f"Could not start recognition: {exc}"})
        await emit({"type": "done"})
        return

    # Hybrid keeps every PCM byte for the final MAI pass.
    full = bytearray() if hybrid else None
    aborted = False
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                aborted = True
                break
            audio = msg.get("bytes")
            if audio is not None:
                # Feed audio to the SDK as it arrives.
                push.write(audio)
                if hybrid:
                    full += audio
                    if len(full) > MAX_SESSION_BYTES:
                        await emit({"type": "error", "recoverable": False,
                                    "message": "Reached the 5-minute live limit; stopping."})
                        aborted = True
                        break
            else:
                text = msg.get("text")
                if text and _is_stop(text):
                    break
    finally:
        # Always close the audio stream and stop recognition, even on errors.
        try:
            push.close()
        except Exception:
            pass
        try:
            await asyncio.to_thread(recognizer.stop_continuous_recognition)
        except Exception:
            pass

    # Hybrid only: replace the live text with one clean MAI pass over the whole clip.
    if hybrid and full and not aborted:
        await _hybrid_finalize(emit, cfg, bytes(full))

    await emit({"type": "done"})


def _build_standard_recognizer(speechsdk, cfg: dict):
    """Create a keyless Speech SDK recognizer fed by a push stream."""
    speech_config = speechsdk.SpeechConfig(endpoint=ENDPOINT, token_credential=_credential)
    # Apply the requested profanity handling.
    prof = cfg["profanity"].lower()
    if prof == "masked":
        speech_config.set_profanity(speechsdk.ProfanityOption.Masked)
    elif prof == "removed":
        speech_config.set_profanity(speechsdk.ProfanityOption.Removed)
    elif prof in ("raw", "none"):
        speech_config.set_profanity(speechsdk.ProfanityOption.Raw)

    # Audio arrives as 16 kHz mono PCM16 through a push stream.
    fmt = speechsdk.audio.AudioStreamFormat(samples_per_second=SAMPLE_RATE, bits_per_sample=16, channels=1)
    push = speechsdk.audio.PushAudioInputStream(stream_format=fmt)
    audio_config = speechsdk.audio.AudioConfig(stream=push)

    # Prefer continuous English/Hindi auto-detect; fall back to a single fixed language.
    try:
        auto_detect = speechsdk.AutoDetectSourceLanguageConfig(languages=["en-US", "hi-IN"])
        speech_config.set_property(
            speechsdk.PropertyId.SpeechServiceConnection_LanguageIdMode, "Continuous"
        )
        recognizer = speechsdk.SpeechRecognizer(
            speech_config=speech_config,
            auto_detect_source_language_config=auto_detect,
            audio_config=audio_config,
        )
    except Exception:
        speech_config.speech_recognition_language = cfg["language"]
        recognizer = speechsdk.SpeechRecognizer(speech_config=speech_config, audio_config=audio_config)

    # Keyword biasing via the phrase list.
    if cfg["phrases"]:
        grammar = speechsdk.PhraseListGrammar.from_recognizer(recognizer)
        for phrase in cfg["phrases"]:
            grammar.addPhrase(phrase)
    return recognizer, push


def _wire_standard_callbacks(speechsdk, recognizer, emit_ts) -> None:
    """Bridge Speech SDK events (worker threads) to WebSocket events."""
    # Interim words while the person is still speaking.
    def on_recognizing(evt):
        if evt.result.text:
            emit_ts({"type": "partial", "text": evt.result.text})

    # A finished sentence, with its timing and detected language.
    def on_recognized(evt):
        if evt.result.reason == speechsdk.ResultReason.RecognizedSpeech and evt.result.text:
            locale = evt.result.properties.get(
                speechsdk.PropertyId.SpeechServiceConnection_AutoDetectSourceLanguageResult, ""
            )
            emit_ts({
                "type": "final",
                "text": evt.result.text,
                "offsetMs": int(evt.result.offset / 10000),      # SDK ticks are 100 ns
                "durationMs": int(evt.result.duration / 10000),
                "locale": locale,
            })

    # Service-side cancellation (auth, quota, network).
    def on_canceled(evt):
        emit_ts({"type": "error", "recoverable": False,
                 "message": f"Recognition canceled: {evt.reason}"})

    recognizer.recognizing.connect(on_recognizing)
    recognizer.recognized.connect(on_recognized)
    recognizer.canceled.connect(on_canceled)


async def _hybrid_finalize(emit, cfg: dict, pcm: bytes) -> None:
    """Hybrid: run MAI once over the full recording and replace the live transcript."""
    await emit({"type": "status", "state": "finalizing"})
    definition = mai_client.build_definition(cfg["phrases"], cfg["profanity"],
                                             diarization=cfg["diarization"])
    # If the MAI pass fails, the live transcript is kept.
    try:
        raw = await mai_client.transcribe_wav(mai_client.pcm16_to_wav(pcm), definition, timeout=120)
    except Exception:
        await emit({"type": "error", "recoverable": True,
                    "message": "Final MAI pass failed; keeping the live transcript."})
        return
    result = mai_client.normalize(raw)
    result["diarizationEnabled"] = cfg["diarization"]
    result["biasedPhrases"] = cfg["phrases"]
    await emit({"type": "replacement", "result": result})


def _is_stop(text: str) -> bool:
    """True if a text frame is the client's `stop` message."""
    try:
        return json.loads(text).get("type") == "stop"
    except Exception:
        return False
