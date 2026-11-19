"""Tiny JSON-over-HTTPS client (stdlib only). Tests replace `HttpClient.send` with a fake."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple


class HttpError(Exception):
    def __init__(self, status: int, message: str, body: Any = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.body = body


class HttpClient:
    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

    def send(self, method: str, url: str, headers: Dict[str, str], body: Optional[bytes]) -> Tuple[int, bytes]:
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 - fixed https endpoints
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read() or b""
        except (urllib.error.URLError, OSError) as exc:
            raise HttpError(0, f"could not reach {urllib.parse.urlsplit(url).hostname}: {getattr(exc, 'reason', exc)}") from None

    def request(self, method: str, url: str, *, token: Optional[str] = None, json_body: Any = None,
                form: Optional[Dict[str, str]] = None, params: Optional[Dict[str, Any]] = None,
                headers: Optional[Dict[str, str]] = None, raw: Optional[bytes] = None,
                content_type: Optional[str] = None) -> Any:
        if params:
            q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
            url = f"{url}{'&' if '?' in url else '?'}{q}"
        h = {"Accept": "application/json", "User-Agent": "AgenticOS/0.2"}
        h.update(headers or {})
        body = None
        if token:
            h["Authorization"] = f"Bearer {token}"
        if json_body is not None:
            body = json.dumps(json_body).encode()
            h["Content-Type"] = "application/json"
        elif form is not None:
            body = urllib.parse.urlencode(form).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        elif raw is not None:
            body = raw
            h["Content-Type"] = content_type or "application/octet-stream"
        status, data = self.send(method, url, h, body)
        try:
            parsed = json.loads(data.decode("utf-8")) if data else {}
        except ValueError:
            parsed = {"raw": data[:500].decode("utf-8", "replace")}
        if status >= 400:
            msg = ""
            if isinstance(parsed, dict):
                err = parsed.get("error")
                msg = (err.get("message") if isinstance(err, dict) else err) or parsed.get("message") or \
                    parsed.get("error_description") or ""
            raise HttpError(status, f"HTTP {status}: {msg or 'request failed'}", parsed)
        return parsed
