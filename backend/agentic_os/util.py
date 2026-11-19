"""Small shared helpers (standard library only)."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_now_iso() -> str:
    return iso(utc_now())  # type: ignore[return-value]


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso_in(seconds: float) -> str:
    return iso(utc_now() + timedelta(seconds=seconds))  # type: ignore[return-value]


def new_id(prefix: str = "") -> str:
    """Opaque, URL-safe identifier. Prefix makes logs easier to read."""
    core = uuid.uuid4().hex[:20]
    return f"{prefix}_{core}" if prefix else core


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def canonical_json(value: Any) -> str:
    """Deterministic JSON used for hashing (sorted keys, no whitespace)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_hex(data: Any) -> str:
    if isinstance(data, bytes):
        raw = data
    elif isinstance(data, str):
        raw = data.encode("utf-8")
    else:
        raw = canonical_json(data).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def json_loads(value: Optional[str], default: Any = None) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def truncate_text(text: str, limit: int) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 20)] + f"... [truncated {len(text) - limit + 20} chars]"


def bounded_json(value: Any, max_chars: int = 20000, max_str: int = 4000, max_items: int = 200) -> Any:
    """Return a copy of `value` with long strings / lists shortened.

    Used before storing tool output in graph state or events so checkpoints and
    the SSE stream stay small. The full (redacted) result lives in SQLite.
    """

    def _walk(v: Any, depth: int, s_limit: int, n_limit: int) -> Any:
        if depth > 12:
            return "[max depth]"
        if isinstance(v, str):
            return truncate_text(v, s_limit)
        if isinstance(v, dict):
            out = {}
            for i, (k, item) in enumerate(v.items()):
                if i >= n_limit:
                    out["__truncated_keys__"] = len(v) - n_limit
                    break
                out[str(k)] = _walk(item, depth + 1, s_limit, n_limit)
            return out
        if isinstance(v, (list, tuple)):
            items = [_walk(x, depth + 1, s_limit, n_limit) for x in list(v)[:n_limit]]
            if len(v) > n_limit:
                items.append(f"[... {len(v) - n_limit} more items]")
            return items
        if isinstance(v, (int, float, bool)) or v is None:
            return v
        return truncate_text(str(v), s_limit)

    s, n = max_str, max_items
    text = ""
    for _ in range(4):
        result = _walk(value, 0, s, n)
        text = json_dumps(result)
        if len(text) <= max_chars:
            return result
        s, n = max(100, s // 4), max(5, n // 4)
    return {"__truncated__": True, "preview": truncate_text(text, max_chars)}
