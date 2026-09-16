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
        self.buf = bytearray()
        self.committed_ms = 0

    def _ms(self, nbytes: int) -> float:
        return nbytes / 2 / self.rate * 1000

    def _bytes(self, ms: float) -> int:
        return int(ms * self.rate / 1000) * 2

    def add(self, pcm: bytes) -> None:
        self.buf += pcm

    def duration_ms(self) -> float:
        return self._ms(len(self.buf))

    def has_window(self) -> bool:
        return self.duration_ms() >= self.committed_ms + self.step_ms

    def take_window(self) -> tuple[bytes, int, int]:
        """Return (pcm, window_start_ms, boundary_ms) for the next full window."""
        start_ms = max(0, self.committed_ms - self.context_ms)
        end_ms = self.committed_ms + self.step_ms
        return bytes(self.buf[self._bytes(start_ms):self._bytes(end_ms)]), start_ms, self.committed_ms

    def advance(self) -> None:
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
        session_offset = p.get("offsetMs", 0) + window_start_ms
        if session_offset >= boundary_ms - OVERLAP_TOLERANCE_MS:
            shifted = dict(p)
            shifted["offsetMs"] = session_offset
            out.append(shifted)
    return out


def parse_config(raw: dict) -> dict:
    """Validate the initial client config message. Raises ValueError on misuse."""
    if raw.get("type") != "config":
        raise ValueError("first message must be a config frame")
    mode = raw.get("mode")
    if mode not in ("mai", "standard", "hybrid"):
        raise ValueError("mode must be one of mai, standard, hybrid")
    biasing = raw.get("biasing", True)
    raw_phrases = raw.get("phrases") or []
    if isinstance(raw_phrases, str):
        raw_phrases = raw_phrases.replace("\n", ",").split(",")
    phrases = [p.strip() for p in raw_phrases if str(p).strip()] if biasing else []
    profanity = (raw.get("profanity") or "").strip()
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
    while True:
        ev = await out_q.get()
        if ev is None:
            break
        try:
            await ws.send_json(ev)
        except Exception:
            break


async def _read_config(ws: WebSocket) -> dict:
    msg = await ws.receive()
    if msg["type"] == "websocket.disconnect":
        raise ValueError("client disconnected before config")
    text = msg.get("text")
    if text is None:
        raise ValueError("expected a text config frame")
    return parse_config(json.loads(text))


@router.websocket("/ws/realtime")
async def realtime_ws(ws: WebSocket) -> None:
    await ws.accept()
    loop = asyncio.get_running_loop()
    out_q: asyncio.Queue = asyncio.Queue()
    sender = asyncio.create_task(_sender(ws, out_q))

    async def emit(ev: dict) -> None:
        await out_q.put(ev)

    def emit_ts(ev: dict) -> None:
        """Thread-safe emit for Speech SDK callback threads."""
        try:
            loop.call_soon_threadsafe(out_q.put_nowait, ev)
        except RuntimeError:
            pass

    try:
        cfg = await _read_config(ws)
    except Exception as exc:
        await emit({"type": "error", "recoverable": False, "message": str(exc)})
        await emit({"type": "done"})
        await out_q.put(None)
        await sender
        await _safe_close(ws)
        return

    await emit({"type": "ready", "sessionId": uuid4().hex, "mode": cfg["mode"]})

    try:
        if cfg["mode"] == "mai":
            await _run_mai(ws, emit, cfg)
        else:
            await _run_standard(ws, emit, emit_ts, cfg, hybrid=(cfg["mode"] == "hybrid"))
    except Exception as exc:
        await emit({"type": "error", "recoverable": False, "message": f"session error: {exc}"})
        await emit({"type": "done"})

    await out_q.put(None)
    await sender
    await _safe_close(ws)


async def _safe_close(ws: WebSocket) -> None:
    try:
        await ws.close()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# MAI near-real-time worker
# --------------------------------------------------------------------------- #
async def _run_mai(ws: WebSocket, emit, cfg: dict) -> None:
    rb = MaiRollingBuffer()
    definition = mai_client.build_definition(cfg["phrases"], cfg["profanity"], diarization=False)
    total = 0
    aborted = False

    while True:
        msg = await ws.receive()
        if msg["type"] == "websocket.disconnect":
            aborted = True
            break
        audio = msg.get("bytes")
        if audio is not None:
            total += len(audio)
            if total > MAX_SESSION_BYTES:
                await emit({"type": "error", "recoverable": False,
                            "message": "Reached the 5-minute live limit; stopping."})
                aborted = True
                break
            rb.add(audio)
            while rb.has_window():
                await _mai_emit_window(emit, rb.take_window(), definition)
                rb.advance()
        else:
            text = msg.get("text")
            if text and _is_stop(text):
                break

    if not aborted:
        flushed = rb.flush_window()
        if flushed:
            await _mai_emit_window(emit, flushed, definition)
    await emit({"type": "done"})


async def _mai_emit_window(emit, window: tuple[bytes, int, int], definition: dict) -> None:
    pcm, start_ms, boundary_ms = window
    if not pcm:
        return
    await emit({"type": "status", "state": "processing", "windowStartMs": start_ms})
    try:
        raw = await mai_client.transcribe_wav(mai_client.pcm16_to_wav(pcm), definition, timeout=60)
    except mai_client.MaiError as exc:
        await emit({"type": "error", "recoverable": True,
                    "message": f"MAI window failed ({exc.status_code})."})
        return
    except Exception:
        await emit({"type": "error", "recoverable": True, "message": "MAI window error."})
        return
    norm = mai_client.normalize(raw)
    kept = filter_new_phrases(norm["phrases"], start_ms, boundary_ms)
    await emit({"type": "chunk", "phrases": kept, "windowStartMs": start_ms})


# --------------------------------------------------------------------------- #
# Standard / Hybrid worker (Azure Speech SDK continuous recognition)
# --------------------------------------------------------------------------- #
async def _run_standard(ws: WebSocket, emit, emit_ts, cfg: dict, hybrid: bool) -> None:
    try:
        import azure.cognitiveservices.speech as speechsdk
    except Exception:
        await emit({"type": "error", "recoverable": False,
                    "message": "Speech SDK is not available on the server."})
        await emit({"type": "done"})
        return

    recognizer, push = _build_standard_recognizer(speechsdk, cfg)
    _wire_standard_callbacks(speechsdk, recognizer, emit_ts)

    try:
        await asyncio.to_thread(recognizer.start_continuous_recognition)
    except Exception as exc:
        await emit({"type": "error", "recoverable": False,
                    "message": f"Could not start recognition: {exc}"})
        await emit({"type": "done"})
        return

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
        try:
            push.close()
        except Exception:
            pass
        try:
            await asyncio.to_thread(recognizer.stop_continuous_recognition)
        except Exception:
            pass

    if hybrid and full and not aborted:
        await _hybrid_finalize(emit, cfg, bytes(full))

    await emit({"type": "done"})


def _build_standard_recognizer(speechsdk, cfg: dict):
    speech_config = speechsdk.SpeechConfig(endpoint=ENDPOINT, token_credential=_credential)
    prof = cfg["profanity"].lower()
    if prof == "masked":
        speech_config.set_profanity(speechsdk.ProfanityOption.Masked)
    elif prof == "removed":
        speech_config.set_profanity(speechsdk.ProfanityOption.Removed)
    elif prof in ("raw", "none"):
        speech_config.set_profanity(speechsdk.ProfanityOption.Raw)

    fmt = speechsdk.audio.AudioStreamFormat(samples_per_second=SAMPLE_RATE, bits_per_sample=16, channels=1)
    push = speechsdk.audio.PushAudioInputStream(stream_format=fmt)
    audio_config = speechsdk.audio.AudioConfig(stream=push)

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

    if cfg["phrases"]:
        grammar = speechsdk.PhraseListGrammar.from_recognizer(recognizer)
        for phrase in cfg["phrases"]:
            grammar.addPhrase(phrase)
    return recognizer, push


def _wire_standard_callbacks(speechsdk, recognizer, emit_ts) -> None:
    def on_recognizing(evt):
        if evt.result.text:
            emit_ts({"type": "partial", "text": evt.result.text})

    def on_recognized(evt):
        if evt.result.reason == speechsdk.ResultReason.RecognizedSpeech and evt.result.text:
            locale = evt.result.properties.get(
                speechsdk.PropertyId.SpeechServiceConnection_AutoDetectSourceLanguageResult, ""
            )
            emit_ts({
                "type": "final",
                "text": evt.result.text,
                "offsetMs": int(evt.result.offset / 10000),
                "durationMs": int(evt.result.duration / 10000),
                "locale": locale,
            })

    def on_canceled(evt):
        emit_ts({"type": "error", "recoverable": False,
                 "message": f"Recognition canceled: {evt.reason}"})

    recognizer.recognizing.connect(on_recognizing)
    recognizer.recognized.connect(on_recognized)
    recognizer.canceled.connect(on_canceled)


async def _hybrid_finalize(emit, cfg: dict, pcm: bytes) -> None:
    await emit({"type": "status", "state": "finalizing"})
    definition = mai_client.build_definition(cfg["phrases"], cfg["profanity"],
                                             diarization=cfg["diarization"])
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
    try:
        return json.loads(text).get("type") == "stop"
    except Exception:
        return False
