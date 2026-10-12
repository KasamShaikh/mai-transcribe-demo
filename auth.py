"""Simple username/password gate for the whole demo (pages, APIs and WebSocket).

Credentials come from the environment (APP_USERNAME / APP_PASSWORD) so they are never
stored in the repository. A successful login sets a signed, HttpOnly session cookie.
If APP_PASSWORD is not set, the gate is disabled (handy for local development).
"""
import base64
import hashlib
import hmac
import json
import os
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

COOKIE_NAME = "mai_session"
SESSION_SECONDS = 12 * 60 * 60
MAX_FAILURES = 5                 # failed attempts allowed per client...
LOCKOUT_SECONDS = 60             # ...before it is locked out for this long

# Paths reachable without a session: the login page itself and its assets.
PUBLIC_PATHS = {"/login", "/login.html", "/login.css", "/styles.css", "/favicon.ico",
                "/api/login", "/api/health"}

router = APIRouter()
_failures: dict[str, list[float]] = {}   # client id -> timestamps of recent failures


def _username() -> str:
    return os.environ.get("APP_USERNAME", "")


def _password() -> str:
    return os.environ.get("APP_PASSWORD", "")


def enabled() -> bool:
    """The gate is active only when a password is configured."""
    return bool(_password())


def _secret() -> bytes:
    """Signing key: SESSION_SECRET if set, otherwise derived from the password."""
    base = os.environ.get("SESSION_SECRET") or _password()
    return hashlib.sha256(("mai-session:" + base).encode()).digest()


def make_token(now: float | None = None) -> str:
    """Create a signed token holding only its expiry time."""
    exp = int((now or time.time()) + SESSION_SECONDS)
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def valid_token(token: str | None, now: float | None = None) -> bool:
    """True if the token's signature is correct and it has not expired."""
    if not token or "." not in token:
        return False
    body, sig = token.rsplit(".", 1)
    expected = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        # Restore the base64 padding that was stripped to keep the cookie value plain.
        padded = body + "=" * (-len(body) % 4)
        exp = json.loads(base64.urlsafe_b64decode(padded.encode()))["exp"]
    except Exception:
        return False
    return (now or time.time()) < exp


def check_credentials(username: str, password: str) -> bool:
    """Constant-time comparison of the submitted credentials."""
    ok_user = hmac.compare_digest(username.encode(), _username().encode())
    ok_pass = hmac.compare_digest(password.encode(), _password().encode())
    return ok_user and ok_pass


def _client_id(request: Request) -> str:
    """Best-effort client IP (first X-Forwarded-For hop behind the Container Apps proxy)."""
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def _locked_out(client: str, now: float) -> bool:
    recent = [t for t in _failures.get(client, []) if now - t < LOCKOUT_SECONDS]
    _failures[client] = recent
    return len(recent) >= MAX_FAILURES


@router.post("/api/login")
async def login(request: Request):
    """Validate credentials and start a session."""
    client = _client_id(request)
    now = time.time()
    # Throttle repeated wrong guesses.
    if _locked_out(client, now):
        return JSONResponse(status_code=429, content={"error": "Too many attempts. Try again in a minute."})
    try:
        data = await request.json()
    except Exception:
        data = {}
    username = str(data.get("username", ""))[:64]
    password = str(data.get("password", ""))[:128]
    if not check_credentials(username, password):
        _failures.setdefault(client, []).append(now)
        return JSONResponse(status_code=401, content={"error": "Invalid username or password."})
    # Success: set a signed, HttpOnly cookie.
    _failures.pop(client, None)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE_NAME, make_token(now), max_age=SESSION_SECONDS, httponly=True,
                    secure=request.url.scheme == "https" or
                    request.headers.get("x-forwarded-proto") == "https",
                    samesite="lax", path="/")
    return resp


@router.post("/api/logout")
async def logout():
    """End the session by clearing the cookie."""
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


def _cookie_from_scope(scope) -> str | None:
    """Read the session cookie from raw ASGI headers."""
    for name, value in scope.get("headers", []):
        if name == b"cookie":
            for part in value.decode("latin-1").split(";"):
                k, _, v = part.strip().partition("=")
                if k == COOKIE_NAME:
                    return v.strip('"')
    return None


class AuthMiddleware:
    """Pure ASGI middleware so it protects HTTP routes and the WebSocket alike."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or not enabled():
            return await self.app(scope, receive, send)
        path = scope["path"]
        # Login page, its assets and the login API are always reachable.
        if path in PUBLIC_PATHS or valid_token(_cookie_from_scope(scope)):
            return await self.app(scope, receive, send)

        # Not signed in: reject the WebSocket, 401 the APIs, redirect pages to the login form.
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 4401})
        if path.startswith("/api/"):
            resp = JSONResponse(status_code=401, content={"error": "Not signed in."})
        else:
            resp = RedirectResponse("/login.html", status_code=302)
        await resp(scope, receive, send)
