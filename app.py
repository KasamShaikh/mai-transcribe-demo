"""MAI-Transcribe-2 demo backend: a thin keyless proxy to the Azure Speech
fast-transcription endpoint, plus static file hosting for the demo UI."""
import json
import os
import time
from pathlib import Path

import httpx
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

# ---- CONFIG: which Speech resource + API to call -------------------------------
# SPEECH_ENDPOINT points at a plain Azure Speech resource (kind: SpeechServices) —
# no Foundry needed. The fast-transcription REST path below is where MAI-Transcribe-2
# runs; the model itself is selected later in _build_definition().
ENDPOINT = os.environ["SPEECH_ENDPOINT"].rstrip("/")
API_VERSION = os.environ.get("SPEECH_API_VERSION", "2025-10-15")
# The single endpoint every transcription request is POSTed to.
TRANSCRIBE_URL = f"{ENDPOINT}/speechtotext/transcriptions:transcribe?api-version={API_VERSION}"
# Entra token audience for keyless auth (no API keys).
TOKEN_RESOURCE = "https://cognitiveservices.azure.com"

PUBLIC_DIR = Path(__file__).parent / "public"

app = FastAPI(title="MAI-Transcribe-2 Demo")

# ---- AUTH: keyless Entra token (instead of an API key) -------------------------
# DefaultAzureCredential uses the container's user-assigned managed identity in
# Azure (via AZURE_CLIENT_ID) and your `az login` locally. The identity holds the
# "Cognitive Services User" role on the Speech resource. Token cached until expiry.
_credential = DefaultAzureCredential()
_token_cache = {"value": "", "exp": 0.0}


def _get_token() -> str:
    now = time.time()
    if _token_cache["value"] and now < _token_cache["exp"] - 120:
        return _token_cache["value"]
    token = _credential.get_token(f"{TOKEN_RESOURCE}/.default")
    _token_cache["value"] = token.token
    _token_cache["exp"] = float(token.expires_on)
    return _token_cache["value"]


def _build_definition(
    engine: str,
    phrases: list[str],
    profanity: str,
    diarization: bool = False,
    max_speakers: int = 2,
) -> dict:
    """Map a UI engine choice + options to a fast-transcription `definition`.

    The returned dict is sent as the `definition` form field of the request. Every
    feature (model, diarization, biasing, profanity) is turned on by adding a key here.
    """
    # ---- MODEL SELECTION: this is where MAI-Transcribe-2 is chosen ----------
    # enhancedMode.enabled=True + model="MAI-Transcribe-2" is the ONLY thing that
    # makes the request run on the MAI model. Remove `model` -> it falls back to
    # LLM Speech; drop enhancedMode entirely -> standard fast transcription.
    if engine == "mai":
        definition: dict = {
            "enhancedMode": {"enabled": True, "task": "transcribe", "model": "MAI-Transcribe-2"}
        }
    elif engine == "llm":  # LLM Speech: enhancedMode WITHOUT a model name
        definition = {"enhancedMode": {"enabled": True, "task": "transcribe"}}
    else:  # "standard" fast transcription — multilingual auto-detect, returns confidence + words
        definition = {"locales": []}

    # ---- KEYWORD BIASING: boost domain terms (NEFT, KYC, ...) ---------------
    # Adding phraseList makes the model favor these words; they are hints, not forced.
    if phrases:
        definition["phraseList"] = {"phrases": phrases}
    # ---- PROFANITY FILTER: e.g. "Masked" / "Removed" / "Tags" ---------------
    if profanity:
        definition["profanityFilterMode"] = profanity
    # ---- SPEAKER DIARIZATION: label Speaker 1 / Speaker 2 ------------------
    # diarization.enabled=True asks the service to split speakers. MAI-Transcribe-2
    # picks the speaker count itself, so we ONLY send maxSpeakers for the other
    # engines (LLM Speech / Standard), which accept that hint.
    if diarization:
        definition["diarization"] = {"enabled": True}
        if engine != "mai":
            definition["diarization"]["maxSpeakers"] = min(max(max_speakers, 2), 35)
    return definition


def _normalize(data: dict) -> dict:
    phrases = []
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
    combined = " ".join(cp.get("text", "") for cp in data.get("combinedPhrases", [])).strip()
    detected = sorted({p["locale"] for p in phrases if p["locale"]})
    return {
        "combinedText": combined,
        "phrases": phrases,
        "detectedLocales": detected,
        "audioMs": data.get("durationMilliseconds", 0),
    }


@app.post("/api/transcribe")
async def transcribe(
    audio: UploadFile,
    engine: str = Form("mai"),
    phrases: str = Form(""),
    biasing: str = Form("true"),
    diarization: str = Form("false"),
    max_speakers: int = Form(2),
    profanity: str = Form(""),
):
    # Split the comma/newline textbox into a clean list only when biasing is toggled on.
    phrase_list: list[str] = []
    if biasing.lower() == "true" and phrases.strip():
        phrase_list = [p.strip() for p in phrases.replace("\n", ",").split(",") if p.strip()]

    # Build the JSON `definition` that selects the model and enables each feature.
    definition = _build_definition(
        engine,
        phrase_list,
        profanity.strip(),
        diarization.lower() == "true",
        max_speakers,
    )

    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="No audio received.")

    # ---- THE ACTUAL CALL TO AZURE SPEECH / MAI-Transcribe-2 -----------------
    # multipart form: `audio` = the file, `definition` = the JSON built above.
    # Auth is the keyless bearer token. This POST is where MAI-Transcribe-2 runs.
    token = _get_token()
    files = {"audio": (audio.filename or "audio.webm", audio_bytes, audio.content_type or "application/octet-stream")}
    payload = {"definition": json.dumps(definition)}

    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            TRANSCRIBE_URL,
            headers={"Authorization": f"Bearer {token}"},
            files=files,
            data=payload,
        )
    latency_ms = round((time.perf_counter() - started) * 1000)

    # Non-200 = the service rejected the request (bad audio, unsupported option, etc.).
    if resp.status_code != 200:
        return JSONResponse(status_code=resp.status_code, content={"error": resp.text})

    # Flatten the raw Azure response into the small shape the UI expects.
    result = _normalize(resp.json())
    result["engine"] = engine
    result["biasedPhrases"] = phrase_list
    result["diarizationEnabled"] = diarization.lower() == "true"
    result["latencyMs"] = latency_ms
    return result


@app.get("/api/health")
async def health():
    return {"ok": True, "endpoint": ENDPOINT, "apiVersion": API_VERSION}


app.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="static")
