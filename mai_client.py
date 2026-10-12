"""Self-contained MAI-Transcribe-2 fast-transcription client for the Real time tab.

This module is intentionally independent of app.py so the existing Batch
implementation stays untouched. It reuses the same keyless (Entra ID) auth and
the same fast-transcription REST endpoint, and it exposes small pure helpers
(WAV framing, definition building, normalization) that the realtime workers and
the unit tests can use without any Azure credentials.
"""
import asyncio
import io
import json
import os
import time
import wave

import httpx
from azure.identity import DefaultAzureCredential

# Speech resource endpoint and REST route for fast transcription (set via env, never hard-coded).
ENDPOINT = os.environ.get("SPEECH_ENDPOINT", "").rstrip("/")
API_VERSION = os.environ.get("SPEECH_API_VERSION", "2025-10-15")
TRANSCRIBE_URL = f"{ENDPOINT}/speechtotext/transcriptions:transcribe?api-version={API_VERSION}"
# Audience for the Entra token used instead of API keys.
TOKEN_RESOURCE = "https://cognitiveservices.azure.com"

# Browser audio is resampled to 16 kHz mono PCM16 before it reaches the server.
SAMPLE_RATE = 16000

# Managed identity in Azure, `az login` locally; plus a small in-memory token cache.
_credential = DefaultAzureCredential()
_token_cache = {"value": "", "exp": 0.0}


class MaiError(Exception):
    """Raised when the fast-transcription endpoint returns a non-200 response."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(f"MAI transcription failed ({status_code})")
        self.status_code = status_code
        self.detail = detail


def _get_token_sync() -> str:
    """Return a cached Entra token, refreshing it when <2 minutes remain."""
    now = time.time()
    # Reuse the cached token while it is still comfortably valid.
    if _token_cache["value"] and now < _token_cache["exp"] - 120:
        return _token_cache["value"]
    # Otherwise fetch a fresh token and remember its expiry.
    token = _credential.get_token(f"{TOKEN_RESOURCE}/.default")
    _token_cache["value"] = token.token
    _token_cache["exp"] = float(token.expires_on)
    return _token_cache["value"]


async def get_token() -> str:
    """Acquire a cached Entra token off the event loop thread."""
    return await asyncio.to_thread(_get_token_sync)


def pcm16_to_wav(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Wrap raw 16-bit mono PCM in a WAV container libsndfile can decode."""
    buf = io.BytesIO()
    # Write a standard WAV header (mono, 16-bit) followed by the raw samples.
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def build_definition(
    phrases: list[str] | None = None,
    profanity: str = "",
    diarization: bool = False,
) -> dict:
    """Build a MAI-Transcribe-2 fast-transcription `definition` payload.

    Mirrors the proven Batch selector: enhancedMode + model. MAI auto-detects
    language and picks its own speaker count, so no locales/maxSpeakers are sent.
    """
    # Selecting the MAI model: enhancedMode + model name is what routes to MAI-Transcribe-2.
    definition: dict = {
        "enhancedMode": {"enabled": True, "task": "transcribe", "model": "MAI-Transcribe-2"}
    }
    # Optional keyword biasing: boost domain terms (NEFT, KYC, ...).
    if phrases:
        definition["phraseList"] = {"phrases": phrases}
    # Optional profanity handling (Masked / Removed / Tags).
    if profanity:
        definition["profanityFilterMode"] = profanity
    # Optional speaker labels (MAI chooses the speaker count itself).
    if diarization:
        definition["diarization"] = {"enabled": True}
    return definition


def normalize(data: dict) -> dict:
    """Flatten a raw fast-transcription response into the UI-facing shape."""
    phrases = []
    # Map each recognized phrase (and its words) to short UI-friendly keys.
    for p in data.get("phrases", []):
        phrases.append(
            {
                "text": p.get("text", ""),
                "offsetMs": p.get("offsetMilliseconds", 0),
                "durationMs": p.get("durationMilliseconds", 0),
                "locale": p.get("locale", ""),
                "confidence": p.get("confidence", 0),
                "speaker": p.get("speaker"),
                "words": [
                    {
                        "text": w.get("text", ""),
                        "offsetMs": w.get("offsetMilliseconds", 0),
                        "durationMs": w.get("durationMilliseconds", 0),
                    }
                    for w in p.get("words", [])
                ],
            }
        )
    # Whole-audio text plus the set of languages detected across phrases.
    combined = " ".join(cp.get("text", "") for cp in data.get("combinedPhrases", [])).strip()
    detected = sorted({p["locale"] for p in phrases if p["locale"]})
    return {
        "combinedText": combined,
        "phrases": phrases,
        "detectedLocales": detected,
        "audioMs": data.get("durationMilliseconds", 0),
    }


async def transcribe_wav(wav_bytes: bytes, definition: dict, timeout: float = 120.0) -> dict:
    """POST a WAV clip to the fast-transcription endpoint and return raw JSON."""
    token = await get_token()
    # Multipart body: the audio file plus the JSON `definition` that selects model/features.
    files = {"audio": ("audio.wav", wav_bytes, "audio/wav")}
    data = {"definition": json.dumps(definition)}
    # Call the Speech endpoint with the keyless bearer token.
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            TRANSCRIBE_URL,
            headers={"Authorization": f"Bearer {token}"},
            files=files,
            data=data,
        )
    # Surface service rejections (bad audio, unsupported option) as a typed error.
    if resp.status_code != 200:
        raise MaiError(resp.status_code, resp.text)
    return resp.json()
