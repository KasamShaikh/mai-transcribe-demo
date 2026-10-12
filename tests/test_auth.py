"""Tests for the login gate (auth.py)."""
import os
import unittest

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app as appmod
import auth


class TestAuthGate(unittest.TestCase):
    def setUp(self):
        # Test-only credentials; real ones live in the deployment environment.
        os.environ["APP_USERNAME"] = "demo-user"
        os.environ["APP_PASSWORD"] = "demo-pass"
        auth._failures.clear()
        self.client = TestClient(appmod.app, base_url="https://testserver", follow_redirects=False)

    def tearDown(self):
        os.environ.pop("APP_USERNAME", None)
        os.environ.pop("APP_PASSWORD", None)

    def test_pages_redirect_to_login_when_signed_out(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "/login.html")

    def test_api_returns_401_when_signed_out(self):
        self.assertEqual(self.client.post("/api/english", json={"phrases": []}).status_code, 401)

    def test_login_page_is_public(self):
        self.assertEqual(self.client.get("/login.html").status_code, 200)

    def test_wrong_password_rejected(self):
        r = self.client.post("/api/login", json={"username": "demo-user", "password": "nope"})
        self.assertEqual(r.status_code, 401)
        self.assertNotIn("set-cookie", r.headers)

    def test_login_grants_access_and_logout_revokes(self):
        r = self.client.post("/api/login", json={"username": "demo-user", "password": "demo-pass"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.client.post("/api/logout")
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_websocket_requires_session(self):
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/ws/realtime"):
                pass

    def test_lockout_after_repeated_failures(self):
        for _ in range(auth.MAX_FAILURES):
            self.client.post("/api/login", json={"username": "demo-user", "password": "bad"})
        r = self.client.post("/api/login", json={"username": "demo-user", "password": "demo-pass"})
        self.assertEqual(r.status_code, 429)

    def test_tampered_or_expired_token_invalid(self):
        token = auth.make_token()
        self.assertTrue(auth.valid_token(token))
        self.assertFalse(auth.valid_token(token[:-2] + "00"))
        self.assertFalse(auth.valid_token(token, now=9_999_999_999))


if __name__ == "__main__":
    unittest.main()
