"""Realtime backend tests. Pure windowing/offset logic plus the MAI WebSocket
path with the Azure call mocked (no credentials or network required)."""
import json
import unittest
import wave
import io

from fastapi.testclient import TestClient

import app as appmod
import english_summary
import mai_client
import realtime
from realtime import MaiRollingBuffer, filter_new_phrases, parse_config


def ms_to_bytes(ms):
    return int(ms * realtime.SAMPLE_RATE / 1000) * 2


class TestRollingBuffer(unittest.TestCase):
    def test_window_boundaries_and_flush(self):
        rb = MaiRollingBuffer(step_ms=7000, context_ms=1000)
        rb.add(b"\x00\x00" * (8000 * realtime.SAMPLE_RATE // 1000))  # 8000 ms
        self.assertTrue(rb.has_window())
        pcm, start, boundary = rb.take_window()
        self.assertEqual(start, 0)
        self.assertEqual(boundary, 0)
        self.assertEqual(len(pcm), ms_to_bytes(7000))
        rb.advance()
        self.assertFalse(rb.has_window())  # 8000 < 7000 + 7000
        flushed = rb.flush_window()
        self.assertIsNotNone(flushed)
        fpcm, fstart, fboundary = flushed
        self.assertEqual(fstart, 6000)  # committed(7000) - context(1000)
        self.assertEqual(fboundary, 7000)

    def test_no_flush_when_nothing_new(self):
        rb = MaiRollingBuffer()
        rb.add(b"\x00\x00" * (3000 * realtime.SAMPLE_RATE // 1000))  # below one step
        self.assertFalse(rb.has_window())
        self.assertIsNotNone(rb.flush_window())  # 3s of uncommitted audio


class TestFilterNewPhrases(unittest.TestCase):
    def test_shifts_offsets_and_drops_context(self):
        phrases = [
            {"text": "old", "offsetMs": 200, "durationMs": 0},    # session 6200 < 7000 -> drop
            {"text": "new", "offsetMs": 1200, "durationMs": 400},  # session 7200 -> keep
        ]
        kept = filter_new_phrases(phrases, window_start_ms=6000, boundary_ms=7000)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["text"], "new")
        self.assertEqual(kept[0]["offsetMs"], 7200)

    def test_first_window_keeps_all(self):
        phrases = [{"text": "a", "offsetMs": 0, "durationMs": 100}]
        kept = filter_new_phrases(phrases, window_start_ms=0, boundary_ms=0)
        self.assertEqual(len(kept), 1)


class TestParseConfig(unittest.TestCase):
    def test_valid_mai(self):
        cfg = parse_config({"type": "config", "mode": "mai", "phrases": "NEFT, KYC", "biasing": True})
        self.assertEqual(cfg["mode"], "mai")
        self.assertEqual(cfg["phrases"], ["NEFT", "KYC"])

    def test_biasing_off_clears_phrases(self):
        cfg = parse_config({"type": "config", "mode": "standard", "phrases": ["A"], "biasing": False})
        self.assertEqual(cfg["phrases"], [])

    def test_bad_mode(self):
        with self.assertRaises(ValueError):
            parse_config({"type": "config", "mode": "nope"})

    def test_missing_type(self):
        with self.assertRaises(ValueError):
            parse_config({"mode": "mai"})


class TestWavFraming(unittest.TestCase):
    def test_pcm16_to_wav_roundtrip(self):
        pcm = b"\x01\x00" * 16000  # 1 s
        wav = mai_client.pcm16_to_wav(pcm)
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertIn(b"WAVE", wav[:16])
        with wave.open(io.BytesIO(wav), "rb") as w:
            self.assertEqual(w.getnchannels(), 1)
            self.assertEqual(w.getframerate(), 16000)
            self.assertEqual(w.getsampwidth(), 2)


class TestMaiWebSocket(unittest.TestCase):
    def test_mai_pipeline_emits_chunks(self):
        canned = {
            "durationMilliseconds": 7000,
            "combinedPhrases": [{"text": "hello world"}],
            "phrases": [{
                "text": "hello world", "offsetMilliseconds": 100,
                "durationMilliseconds": 900, "locale": "en-US", "confidence": 0,
            }],
        }

        async def fake_transcribe(wav_bytes, definition, timeout=120):
            return canned

        original = mai_client.transcribe_wav
        mai_client.transcribe_wav = fake_transcribe
        try:
            with TestClient(appmod.app) as client:
                with client.websocket_connect("/ws/realtime") as ws:
                    ws.send_text(json.dumps({"type": "config", "mode": "mai", "biasing": False}))
                    ready = ws.receive_json()
                    self.assertEqual(ready["type"], "ready")
                    ws.send_bytes(b"\x00\x00" * (8000 * realtime.SAMPLE_RATE // 1000))
                    ws.send_text(json.dumps({"type": "stop"}))
                    types = []
                    while True:
                        ev = ws.receive_json()
                        types.append(ev["type"])
                        if ev["type"] == "done":
                            break
                    self.assertIn("chunk", types)
        finally:
            mai_client.transcribe_wav = original

    def test_english_event_precedes_done(self):
        canned = {"phrases": [{"text": "das hazar ka stock kal", "locale": "hi-IN"}]}

        async def fake_transcribe(wav_bytes, definition, timeout=120):
            return canned

        async def fake_summarize(text, timeout=60.0):
            self.assertIn("das hazar", text)
            return {"englishTranscript": "ten thousand stock tomorrow", "summary": "Will buy stock tomorrow."}

        orig_t, orig_s, orig_c = (mai_client.transcribe_wav, english_summary.summarize_to_english,
                                  english_summary.is_configured)
        mai_client.transcribe_wav = fake_transcribe
        english_summary.summarize_to_english = fake_summarize
        english_summary.is_configured = lambda: True
        try:
            with TestClient(appmod.app) as client:
                with client.websocket_connect("/ws/realtime") as ws:
                    ws.send_text(json.dumps({"type": "config", "mode": "mai", "biasing": False}))
                    ws.receive_json()
                    ws.send_bytes(b"\x00\x00" * (8000 * realtime.SAMPLE_RATE // 1000))
                    ws.send_text(json.dumps({"type": "stop"}))
                    events = []
                    while True:
                        ev = ws.receive_json()
                        events.append(ev)
                        if ev["type"] == "done":
                            break
            types = [e["type"] for e in events]
            self.assertLess(types.index("english"), types.index("done"))
            eng = next(e for e in events if e["type"] == "english")
            self.assertEqual(eng["summary"], "Will buy stock tomorrow.")
        finally:
            mai_client.transcribe_wav, english_summary.summarize_to_english, english_summary.is_configured = (
                orig_t, orig_s, orig_c)


class TestEnglishSummaryHelpers(unittest.TestCase):
    def test_collector_replacement_overrides_live(self):
        c = english_summary.TranscriptCollector()
        c.add({"type": "final", "text": "live text"})
        c.add({"type": "replacement", "result": {"phrases": [{"text": "clean", "speaker": 0}]}})
        self.assertEqual(c.text(), "Speaker 1: clean")

    def test_parse_requires_fields(self):
        with self.assertRaises(english_summary.EnglishSummaryError):
            english_summary._parse('{"summary": "x"}')
        out = english_summary._parse('{"englishTranscript": "a", "summary": "b"}')
        self.assertEqual(out["summary"], "b")


if __name__ == "__main__":
    unittest.main()
