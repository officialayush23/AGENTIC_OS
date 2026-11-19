"""Local request authentication and request-origin validation.

Binding to 127.0.0.1 is not enough: any web page the user visits can try to
send requests to localhost ports (CSRF, DNS rebinding), and other local
programs can connect too. Every request therefore passes these checks:

1. Host header must be this backend's loopback host:port (stops DNS rebinding).
2. Origin header, when present, must be an allowed origin (this backend, plus
   the Vite dev server in dev mode and Tauri origins in desktop mode).
3. API requests must carry the custom header `X-Agentic-OS: 1`. Browsers
   cannot add custom headers cross-origin without a CORS preflight, and the
   CORS policy only allows the origins above.
4. API requests (except the pairing endpoints) must be authenticated by a
   session cookie obtained by pairing, or by a Bearer token handed to the
   backend over stdin by a trusted parent process (the Tauri shell).

Pairing: at startup the backend prints a one-time code (and opens the browser
on a link containing it). Presenting the code creates an HttpOnly,
SameSite=Strict session cookie. Codes are single-use, expire, and failed
attempts are rate limited.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
import uuid
from collections import deque
from datetime import timedelta
from typing import Callable, Deque, Dict, Optional, Tuple

from ..config import Settings
from ..events import correlation_id_var
from ..persistence.repositories import SessionRepo
from ..util import iso, parse_iso, utc_now, utc_now_iso

log = logging.getLogger(__name__)

SESSION_COOKIE = "aos_session"
CLIENT_HEADER = "x-agentic-os"
PAIRING_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
MAX_FAILURES_PER_WINDOW = 10
FAILURE_WINDOW_S = 60.0
LOCKOUT_S = 60.0
_CID_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")

PUBLIC_ENDPOINTS = {
    ("GET", "/health"),
    ("HEAD", "/health"),
    ("GET", "/auth/pair"),
    ("POST", "/api/auth/pair"),
    ("GET", "/api/auth/session"),
}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
)


class AuthError(Exception):
    def __init__(self, code: str, message: str, status: int = 401):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def generate_pairing_code() -> str:
    raw = "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_code(code: str) -> str:
    return re.sub(r"[\s\-]", "", (code or "")).upper()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class LocalAuth:
    def __init__(self, settings: Settings, sessions: SessionRepo, on_code_rotated: Optional[Callable[[str], None]] = None):
        self.settings = settings
        self.sessions = sessions
        self._lock = threading.Lock()
        self._code = ""
        self._code_expires = 0.0
        self._failures: Deque[float] = deque()
        self._locked_until = 0.0
        self._on_code_rotated = on_code_rotated
        self._touch_cache: Dict[str, float] = {}
        self.rotate_code(notify=False)

    # ------------------------------------------------------------ pairing
    def set_rotation_callback(self, callback: Optional[Callable[[str], None]]) -> None:
        self._on_code_rotated = callback

    def rotate_code(self, notify: bool = True) -> str:
        with self._lock:
            self._code = generate_pairing_code()
            self._code_expires = time.monotonic() + self.settings.pairing_code_ttl_s
            code = self._code
        if notify and self._on_code_rotated:
            try:
                self._on_code_rotated(code)
            except Exception:  # pragma: no cover - console printing failures are non-fatal
                pass
        return code

    def current_code(self) -> str:
        with self._lock:
            expired = time.monotonic() >= self._code_expires
            code = self._code
        if expired:
            code = self.rotate_code()
        return code

    def code_seconds_left(self) -> int:
        with self._lock:
            return max(0, int(self._code_expires - time.monotonic()))

    def pair(self, code: str, user_agent: Optional[str] = None) -> str:
        now = time.monotonic()
        with self._lock:
            if now < self._locked_until:
                raise AuthError("PAIRING_LOCKED", "Too many failed pairing attempts. Wait a minute and try again.", 429)
            while self._failures and now - self._failures[0] > FAILURE_WINDOW_S:
                self._failures.popleft()
            expected = normalize_code(self._code)
            expired = now >= self._code_expires
            given = normalize_code(code)
            ok = (not expired) and len(given) == len(expected) and hmac.compare_digest(given, expected)
            if not ok:
                self._failures.append(now)
                if len(self._failures) >= MAX_FAILURES_PER_WINDOW:
                    self._locked_until = now + LOCKOUT_S
                    self._failures.clear()
        if not ok:
            if expired:
                self.rotate_code()
                raise AuthError(
                    "PAIRING_CODE_EXPIRED",
                    "That pairing code has expired. A new code was printed in the Agentic OS terminal window.",
                )
            raise AuthError("PAIRING_CODE_INVALID", "Pairing code is not valid. Check the Agentic OS terminal window.")
        token = secrets.token_urlsafe(32)
        expires = iso(utc_now() + timedelta(days=self.settings.session_ttl_days))
        self.sessions.create(hash_token(token), expires, user_agent)  # type: ignore[arg-type]
        self.sessions.purge_expired(utc_now_iso())
        log.info("browser paired; new session created")
        self.rotate_code()  # single use
        return token

    # ----------------------------------------------------------- sessions
    def check_session(self, token: Optional[str]) -> bool:
        if not token or len(token) > 256:
            return False
        h = hash_token(token)
        row = self.sessions.get(h)
        if not row:
            return False
        expires = parse_iso(row.get("expires_at"))
        if expires is None or expires <= utc_now():
            self.sessions.delete(h)
            return False
        last = self._touch_cache.get(h, 0.0)
        if time.monotonic() - last > 300:
            self._touch_cache[h] = time.monotonic()
            try:
                self.sessions.touch(h)
            except Exception:  # pragma: no cover
                pass
        return True

    def check_bearer(self, token: Optional[str]) -> bool:
        expected = self.settings.bearer_token
        if not expected or not token:
            return False
        return hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))

    def logout(self, token: Optional[str]) -> None:
        if token:
            self.sessions.delete(hash_token(token))

    def revoke_all_sessions(self) -> int:
        return self.sessions.delete_all()


# ---------------------------------------------------------------------------
def parse_cookies(header: str) -> Dict[str, str]:
    cookies: Dict[str, str] = {}
    for part in (header or "").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            cookies[name.strip()] = value.strip().strip('"')
    return cookies


def _headers(scope) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw_k, raw_v in scope.get("headers", []) or []:
        k = raw_k.decode("latin-1").lower()
        v = raw_v.decode("latin-1")
        if k == "cookie" and k in out:
            out[k] = out[k] + "; " + v
        else:
            out[k] = v
    return out


def is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


class LocalSecurityMiddleware:
    """Pure ASGI middleware (safe for streaming/SSE responses)."""

    def __init__(self, app, auth: LocalAuth, settings: Settings):
        self.app = app
        self.auth = auth
        self.settings = settings
        self.allowed_hosts = {h.lower() for h in settings.allowed_hosts}
        self.allowed_origins = {o.lower() for o in settings.allowed_origins}

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = _headers(scope)
        cid_in = headers.get("x-correlation-id", "")
        cid = cid_in if _CID_RE.match(cid_in or "") else uuid.uuid4().hex
        ctx_token = correlation_id_var.set(cid)
        try:
            method = str(scope.get("method", "GET")).upper()
            path = scope.get("path", "/") or "/"
            rejection = self._check(scope, headers, method, path)
            if rejection is not None:
                status, code, message = rejection
                await self._reject(send, status, code, message, cid)
                return
            await self.app(scope, receive, self._wrap_send(send, cid, path))
        finally:
            correlation_id_var.reset(ctx_token)

    def _check(self, scope, headers: Dict[str, str], method: str, path: str) -> Optional[Tuple[int, str, str]]:
        host = headers.get("host", "").strip().lower()
        if host not in self.allowed_hosts:
            return 403, "HOST_NOT_ALLOWED", "Requests must be addressed to the local Agentic OS host."

        origin = headers.get("origin")
        if origin is not None:
            if origin.strip().rstrip("/").lower() not in self.allowed_origins:
                return 403, "ORIGIN_NOT_ALLOWED", "This origin is not allowed to call the Agentic OS API."
        elif method not in SAFE_METHODS:
            site = headers.get("sec-fetch-site")
            if site is not None and site not in ("same-origin", "none"):
                return 403, "CROSS_SITE_REQUEST", "Cross-site requests are not allowed."

        if is_api_path(path):
            if headers.get(CLIENT_HEADER) != "1":
                return 403, "CLIENT_HEADER_REQUIRED", "Missing X-Agentic-OS request header."
        if (method, path) in PUBLIC_ENDPOINTS or not (is_api_path(path) or path.startswith("/auth/")):
            return None

        auth_kind = None
        authz = headers.get("authorization", "")
        if authz[:7].lower() == "bearer " and self.auth.check_bearer(authz[7:].strip()):
            auth_kind = "bearer"
        else:
            cookie = parse_cookies(headers.get("cookie", "")).get(SESSION_COOKIE)
            if cookie and self.auth.check_session(cookie):
                auth_kind = "session"
        if auth_kind is None:
            return 401, "AUTH_REQUIRED", "This browser is not paired with Agentic OS. Enter the pairing code shown in the terminal."
        scope.setdefault("state", {})["auth"] = auth_kind
        return None

    async def _reject(self, send, status: int, code: str, message: str, cid: str) -> None:
        body = json.dumps({"error": {"code": code, "message": message, "correlation_id": cid}}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"x-correlation-id", cid.encode()),
                    (b"x-content-type-options", b"nosniff"),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _wrap_send(self, send, cid: str, path: str):
        api = is_api_path(path)

        async def wrapped(message):
            if message.get("type") == "http.response.start":
                existing = {k.lower() for k, _ in message.get("headers", [])}
                extra = [
                    (b"x-correlation-id", cid.encode()),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                ]
                if b"content-security-policy" not in existing:
                    extra.append((b"content-security-policy", CSP.encode()))
                if api and b"cache-control" not in existing:
                    extra.append((b"cache-control", b"no-store"))
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [h for h in extra if h[0] not in existing]
            await send(message)

        return wrapped
