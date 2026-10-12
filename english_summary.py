"""End-of-call English transcript + summary.

Sends the final (code-mixed) transcript once to an Azure OpenAI chat deployment
using the same keyless Entra credential as the Speech calls. The original
transcript is never modified; failures are non-fatal for callers.
"""
import asyncio
import json
import os

import httpx

from mai_client import TOKEN_RESOURCE, _credential

AOAI_ENDPOINT = os.environ.get("AOAI_ENDPOINT", "").rstrip("/")
AOAI_DEPLOYMENT = os.environ.get("AOAI_DEPLOYMENT", "")
AOAI_API_VERSION = os.environ.get("AOAI_API_VERSION", "2024-10-21")
MAX_INPUT_CHARS = 30000

SYSTEM_PROMPT = (
    "You process call transcripts that mix English, Hindi, Marathi and other Indian languages "
    "(often romanized, e.g. 'das hazar ka stock kal'). Return JSON with exactly two keys:\n"
    "\"englishTranscript\": the whole conversation translated into natural, fluent English, one "
    "line per input line, keeping any 'Speaker N:' prefixes. Translate by meaning, convert "
    "numbers/amounts (e.g. 'das hazar' -> 'ten thousand') and relative time words (kal -> "
    "tomorrow or yesterday, from context). Keep names, acronyms (NEFT, KYC, UPI) and product "
    "terms unchanged. Do not add facts.\n"
    "\"summary\": 2-4 sentences in English covering what was discussed, decisions and the "
    "intended actions (who will do what, and when).\n"
    "Output JSON only."
)


class EnglishSummaryError(Exception):
    pass


def is_configured() -> bool:
    return bool(AOAI_ENDPOINT and AOAI_DEPLOYMENT)


def phrases_to_text(phrases: list[dict]) -> str:
    """One line per phrase, prefixed with the speaker label when present."""
    lines = []
    for p in phrases:
        text = (p.get("text") or "").strip()
        if not text:
            continue
        speaker = p.get("speaker")
        prefix = f"Speaker {int(speaker) + 1}: " if isinstance(speaker, int) else ""
        lines.append(prefix + text)
    return "\n".join(lines)


class TranscriptCollector:
    """Accumulates final phrases from realtime events (any pipeline)."""

    def __init__(self) -> None:
        self.phrases: list[dict] = []

    def add(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "final":
            self.phrases.append(ev)
        elif kind == "chunk":
            self.phrases.extend(ev.get("phrases") or [])
        elif kind == "replacement":
            result = ev.get("result") or {}
            self.phrases = list(result.get("phrases") or []) or (
                [{"text": result["combinedText"]}] if result.get("combinedText") else []
            )

    def text(self) -> str:
        return phrases_to_text(self.phrases)


def _parse(content: str) -> dict:
    try:
        data = json.loads(content)
    except Exception as exc:
        raise EnglishSummaryError("model returned invalid JSON") from exc
    transcript, summary = data.get("englishTranscript"), data.get("summary")
    if isinstance(transcript, list):
        transcript = "\n".join(str(x) for x in transcript)
    if not isinstance(transcript, str) or not isinstance(summary, str):
        raise EnglishSummaryError("model response missing fields")
    return {"englishTranscript": transcript.strip(), "summary": summary.strip()}


async def summarize_to_english(text: str, timeout: float = 60.0) -> dict:
    """Return {"englishTranscript", "summary"} for a code-mixed transcript."""
    if not is_configured():
        raise EnglishSummaryError("English summary is not configured on the server.")
    text = text.strip()
    if not text:
        raise EnglishSummaryError("empty transcript")
    token = (await asyncio.to_thread(_credential.get_token, f"{TOKEN_RESOURCE}/.default")).token
    url = (f"{AOAI_ENDPOINT}/openai/deployments/{AOAI_DEPLOYMENT}"
           f"/chat/completions?api-version={AOAI_API_VERSION}")
    body = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text[:MAX_INPUT_CHARS]},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise EnglishSummaryError(f"request failed: {exc}") from exc
    if resp.status_code != 200:
        raise EnglishSummaryError(f"Azure OpenAI returned {resp.status_code}")
    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        raise EnglishSummaryError("unexpected response shape") from exc
    return _parse(content)
