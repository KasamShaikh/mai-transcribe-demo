"""Regression tests that pin the existing Batch behavior. These must pass
identically before and after the Real time tab was added."""
import unittest

from fastapi.testclient import TestClient

import app as appmod


class TestBatchContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(appmod.app)

    def test_health_unchanged(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_build_definition_mai(self):
        d = appmod._build_definition("mai", [], "", False, 2)
        self.assertTrue(d["enhancedMode"]["enabled"])
        self.assertEqual(d["enhancedMode"]["model"], "MAI-Transcribe-2")

    def test_build_definition_standard(self):
        d = appmod._build_definition("standard", [], "", False, 2)
        self.assertIn("locales", d)
        self.assertNotIn("enhancedMode", d)

    def test_build_definition_llm_has_no_model(self):
        d = appmod._build_definition("llm", [], "", False, 2)
        self.assertIn("enhancedMode", d)
        self.assertNotIn("model", d["enhancedMode"])

    def test_build_definition_extras(self):
        d = appmod._build_definition("mai", ["NEFT"], "Masked", True, 3)
        self.assertEqual(d["phraseList"]["phrases"], ["NEFT"])
        self.assertEqual(d["profanityFilterMode"], "Masked")
        self.assertTrue(d["diarization"]["enabled"])
        # MAI picks its own speaker count — no maxSpeakers hint.
        self.assertNotIn("maxSpeakers", d["diarization"])

    def test_normalize_shape(self):
        raw = {
            "durationMilliseconds": 5000,
            "combinedPhrases": [{"text": "hi there"}],
            "phrases": [
                {"text": "hi there", "offsetMilliseconds": 0,
                 "durationMilliseconds": 500, "locale": "en-US", "confidence": 0}
            ],
        }
        n = appmod._normalize(raw)
        self.assertEqual(n["combinedText"], "hi there")
        self.assertEqual(n["audioMs"], 5000)
        self.assertEqual(n["phrases"][0]["offsetMs"], 0)
        self.assertEqual(n["detectedLocales"], ["en-US"])

    def test_transcribe_rejects_empty_audio(self):
        r = self.client.post(
            "/api/transcribe",
            data={"engine": "mai"},
            files={"audio": ("a.wav", b"", "audio/wav")},
        )
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
