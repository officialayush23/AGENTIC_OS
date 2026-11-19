"""Secret redaction for logs, events, diagnostics and error messages.

Two layers:
1. Known secrets: every credential loaded from the secret store is registered
   here at runtime and masked by exact match, whatever its format.
2. Pattern + key-name heuristics for well-known token formats.

Only strings are redacted; numbers/booleans pass through (so metrics such as
`input_tokens` are not destroyed by the key-name heuristic).
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Iterable

REDACTED = "[REDACTED]"

_SENSITIVE_EXACT = {
    "apikey", "api_key", "authorization", "proxy_authorization", "password", "passwd",
    "secret", "client_secret", "token", "access_token", "refresh_token", "id_token",
    "bearer", "cookie", "set_cookie", "private_key", "access_key", "secret_key",
    "session_token", "x_api_key", "x_goog_api_key", "credential", "credentials",
    "pairing_code", "aos_session",
}
_SENSITIVE_SUFFIXES = (
    "_secret", "_password", "_api_key", "_apikey", "_token", "_private_key",
    "_access_key", "_credential", "_credentials",
)

_VALUE_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"), "[REDACTED_KEY]"),
    (re.compile(r"\bsk-(?:proj-|live-|svcacct-|or-v1-)?[A-Za-z0-9_\-]{16,}"), "[REDACTED_KEY]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), "[REDACTED_KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED_KEY]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[REDACTED_KEY]"),
    (re.compile(r"\bgsk_[A-Za-z0-9]{20,}"), "[REDACTED_KEY]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "[REDACTED_JWT]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|token|secret|password|code)=([^&\s\"']+)"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)(x-api-key|x-goog-api-key|authorization)\s*[:=]\s*[^\s,;\"']+"), r"\1: [REDACTED]"),
]


def is_sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    norm = key.strip().lower().replace("-", "_")
    return norm in _SENSITIVE_EXACT or norm.endswith(_SENSITIVE_SUFFIXES)


class Redactor:
    def __init__(self) -> None:
        self._known: set[str] = set()
        self._lock = threading.Lock()

    def register_secret(self, value: str | None) -> None:
        """Mask this exact value everywhere from now on (values < 6 chars are ignored)."""
        if not value or not isinstance(value, str) or len(value) < 6:
            return
        with self._lock:
            self._known.add(value)

    def forget_secret(self, value: str | None) -> None:
        if not value:
            return
        with self._lock:
            self._known.discard(value)

    def redact_text(self, text: Any) -> Any:
        if not isinstance(text, str) or not text:
            return text
        out = text
        with self._lock:
            known = sorted(self._known, key=len, reverse=True)
        for secret in known:
            if secret in out:
                out = out.replace(secret, REDACTED)
        for pattern, repl in _VALUE_PATTERNS:
            out = pattern.sub(repl, out)
        return out

    def redact(self, value: Any, _depth: int = 0) -> Any:
        if _depth > 25:
            return "[max depth]"
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                if is_sensitive_key(k) and isinstance(v, str) and v:
                    out[k] = REDACTED
                elif is_sensitive_key(k) and isinstance(v, (list, dict)) and v:
                    out[k] = REDACTED
                else:
                    out[k] = self.redact(v, _depth + 1)
            return out
        if isinstance(value, (list, tuple)):
            return [self.redact(v, _depth + 1) for v in value]
        return value

    def redact_headers(self, headers: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
        return [(k, REDACTED if is_sensitive_key(k) else self.redact_text(v)) for k, v in headers]


redactor = Redactor()


class RedactingFilter(logging.Filter):
    """Logging filter that redacts the fully formatted message."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - logging API
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed log call
            message = str(record.msg)
        record.msg = redactor.redact_text(message)
        record.args = ()
        if record.exc_text:
            record.exc_text = redactor.redact_text(record.exc_text)
        return True
