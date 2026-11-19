"""Provider configuration, credentials and construction.

Non-secret settings live in the `providers` table. API keys live only in the
OS credential store (`SecretStore`). Public representations never include a
key - only whether one is stored.
"""

from __future__ import annotations

import logging
import re
import threading
import urllib.parse
from typing import Dict, Optional

from ..config import Settings
from ..persistence.repositories import ProviderRepo
from ..security.redaction import redactor
from ..security.secrets import SecretStore, SecretStoreError, provider_secret_name
from ..util import utc_now_iso
from .base import ErrorCode, LLMProvider, ProviderConfig, ProviderError
from .catalog import PROVIDER_KINDS

log = logging.getLogger(__name__)

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{1,39}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/@\-]{1,120}$")
LOOPBACK = {"localhost", "127.0.0.1", "::1"}
EXTRA_RULES = {
    "reasoning_effort": lambda v: v in (None, "minimal", "low", "medium", "high"),
    "token_param": lambda v: v in (None, "max_tokens", "max_completion_tokens"),
    "thinking_budget": lambda v: v is None or (isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 65536),
}


class ProviderConfigError(ValueError):
    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field
        self.message = message


def validate_base_url(url: Optional[str], kind: str) -> Optional[str]:
    meta = PROVIDER_KINDS[kind]
    if url is None or str(url).strip() == "":
        if meta.get("requires_base_url"):
            raise ProviderConfigError("base_url", "A base URL is required for this provider type (e.g. https://host/v1).")
        return None
    raw = str(url).strip()
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in ("http", "https"):
        raise ProviderConfigError("base_url", "Base URL must start with https:// (or http:// for a loopback address).")
    if parsed.username or parsed.password:
        raise ProviderConfigError("base_url", "Do not put credentials in the URL; store the key as the provider credential.")
    if parsed.query or parsed.fragment:
        raise ProviderConfigError("base_url", "Base URL must not contain a query string or fragment.")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ProviderConfigError("base_url", "Base URL has no host.")
    if parsed.scheme == "http" and host not in LOOPBACK:
        raise ProviderConfigError("base_url", "Plain http is only allowed for loopback addresses. Use https.")
    return raw.rstrip("/")


def validate_api_key(value: str) -> str:
    if not isinstance(value, str):
        raise ProviderConfigError("api_key", "API key must be text.")
    key = value.strip()
    if len(key) < 8:
        raise ProviderConfigError("api_key", "API key looks too short.")
    if len(key) > 4096:
        raise ProviderConfigError("api_key", "API key is too long.")
    if any(c.isspace() for c in key) or not key.isprintable():
        raise ProviderConfigError("api_key", "API key must not contain spaces or control characters.")
    return key


class ProviderManager:
    def __init__(self, repo: ProviderRepo, secrets: SecretStore, settings: Settings):
        self.repo = repo
        self.secrets = secrets
        self.settings = settings
        self._overrides: Dict[str, LLMProvider] = {}
        self._cache: Dict[str, tuple] = {}
        self._lock = threading.Lock()
        self.allow_fake = bool(settings.enable_fake_provider)

    # ------------------------------------------------------------ helpers
    def register_override(self, provider_id: str, provider: LLMProvider) -> None:
        """Tests and development: route a provider id to a prebuilt instance."""
        with self._lock:
            self._overrides[provider_id] = provider

    def _invalidate(self, provider_id: str) -> None:
        with self._lock:
            self._cache.pop(provider_id, None)

    def visible_kinds(self) -> list:
        return [k for k, m in PROVIDER_KINDS.items() if not m.get("hidden") or self.allow_fake]

    def credential_status(self, provider_id: str, kind: str) -> dict:
        store = self.secrets.status()
        if kind == "fake":
            return {"stored": True, "required": False, "store_available": store.get("available", False),
                    "backend": store.get("backend"), "error": None}
        stored, error = False, None
        if store.get("available"):
            try:
                stored = self.secrets.get(provider_secret_name(provider_id)) is not None
            except SecretStoreError as exc:
                error = exc.safe_message
        else:
            error = store.get("reason")
        return {"stored": stored, "required": True, "store_available": bool(store.get("available")),
                "backend": store.get("backend"), "error": error}

    def public(self, rec: dict) -> dict:
        meta = PROVIDER_KINDS.get(rec["kind"], {})
        return {
            "id": rec["id"],
            "kind": rec["kind"],
            "kind_label": meta.get("label", rec["kind"]),
            "display_name": rec["display_name"],
            "model": rec["model"],
            "base_url": rec.get("base_url"),
            "timeout_s": rec.get("timeout_s"),
            "max_output_tokens": rec.get("max_output_tokens"),
            "temperature": rec.get("temperature"),
            "structured_mode": rec.get("structured_mode"),
            "structured_modes": ["auto", *meta.get("structured_modes", [])],
            "max_calls_per_task": rec.get("max_calls_per_task"),
            "extra": rec.get("extra") or {},
            "enabled": rec.get("enabled", True),
            "is_default": rec.get("is_default", False),
            "credential": self.credential_status(rec["id"], rec["kind"]),
            "last_test": rec.get("last_test"),
            "notes": meta.get("notes", ""),
            "created_at": rec.get("created_at"),
            "updated_at": rec.get("updated_at"),
        }

    def list_public(self) -> list:
        return [self.public(r) for r in self.repo.list()]

    def get_public(self, provider_id: str) -> Optional[dict]:
        rec = self.repo.get(provider_id)
        return self.public(rec) if rec else None

    # ---------------------------------------------------------- mutations
    def validate(self, provider_id: str, payload: dict, existing: Optional[dict]) -> dict:
        if not ID_RE.match(provider_id or ""):
            raise ProviderConfigError("id", "Provider id must be 2-40 chars: lowercase letters, digits, '-' or '_'.")
        kind = payload.get("kind") or (existing or {}).get("kind")
        if kind not in PROVIDER_KINDS:
            raise ProviderConfigError("kind", f"Unknown provider type {kind!r}.")
        if kind == "fake" and not self.allow_fake:
            raise ProviderConfigError("kind", "The fake provider is only available in tests or with AGENTIC_OS_ENABLE_FAKE_PROVIDER=1.")
        meta = PROVIDER_KINDS[kind]
        merged = {**(existing or {}), **{k: v for k, v in payload.items() if v is not None or k in ("temperature", "base_url")}}
        out: dict = {"kind": kind}

        display_name = str(merged.get("display_name") or meta["label"]).strip()
        if not (1 <= len(display_name) <= 60):
            raise ProviderConfigError("display_name", "Display name must be 1-60 characters.")
        out["display_name"] = display_name

        model = str(merged.get("model") or meta.get("default_model") or "").strip()
        if not MODEL_RE.match(model):
            raise ProviderConfigError("model", "Model ID is required and may contain letters, digits and . _ : / @ -")
        out["model"] = model

        out["base_url"] = validate_base_url(merged.get("base_url"), kind)

        try:
            timeout_s = float(merged.get("timeout_s") if merged.get("timeout_s") is not None else 60)
        except (TypeError, ValueError):
            raise ProviderConfigError("timeout_s", "Timeout must be a number of seconds.")
        if not (5 <= timeout_s <= 600):
            raise ProviderConfigError("timeout_s", "Timeout must be between 5 and 600 seconds.")
        out["timeout_s"] = timeout_s

        try:
            mot = int(merged.get("max_output_tokens") or meta.get("default_max_output_tokens", 4096))
        except (TypeError, ValueError):
            raise ProviderConfigError("max_output_tokens", "Max output tokens must be an integer.")
        if not (64 <= mot <= 128000):
            raise ProviderConfigError("max_output_tokens", "Max output tokens must be between 64 and 128000.")
        out["max_output_tokens"] = mot

        temp = merged.get("temperature")
        if temp in ("", None):
            out["temperature"] = None
        else:
            try:
                temp = float(temp)
            except (TypeError, ValueError):
                raise ProviderConfigError("temperature", "Temperature must be a number or empty.")
            if not (0.0 <= temp <= 2.0):
                raise ProviderConfigError("temperature", "Temperature must be between 0 and 2.")
            out["temperature"] = temp

        mode = merged.get("structured_mode") or "auto"
        if mode != "auto" and mode not in meta["structured_modes"]:
            raise ProviderConfigError("structured_mode", f"Structured mode must be one of: auto, {', '.join(meta['structured_modes'])}.")
        out["structured_mode"] = mode

        try:
            calls = int(merged.get("max_calls_per_task") or 8)
        except (TypeError, ValueError):
            raise ProviderConfigError("max_calls_per_task", "Request budget must be an integer.")
        if not (1 <= calls <= 30):
            raise ProviderConfigError("max_calls_per_task", "Request budget must be between 1 and 30 calls per task.")
        out["max_calls_per_task"] = calls

        extra = merged.get("extra") or {}
        if not isinstance(extra, dict):
            raise ProviderConfigError("extra", "Extra options must be an object.")
        clean_extra = {}
        for key, value in extra.items():
            rule = EXTRA_RULES.get(key)
            if rule is None:
                raise ProviderConfigError("extra", f"Unknown extra option {key!r}.")
            if not rule(value):
                raise ProviderConfigError("extra", f"Invalid value for {key!r}.")
            if value is not None:
                clean_extra[key] = value
        out["extra"] = clean_extra
        out["enabled"] = bool(merged.get("enabled", True))
        return out

    def save(self, provider_id: str, payload: dict) -> dict:
        existing = self.repo.get(provider_id)
        values = self.validate(provider_id, payload, existing)
        rec = self.repo.upsert(provider_id, values)
        self._invalidate(provider_id)
        log.info("provider %s saved (kind=%s model=%s)", provider_id, values["kind"], values["model"])
        return self.public(rec)

    def set_credential(self, provider_id: str, api_key: str) -> dict:
        rec = self.repo.get(provider_id)
        if rec is None:
            raise ProviderConfigError("id", "Save the provider settings before adding a credential.")
        if rec["kind"] == "fake":
            raise ProviderConfigError("api_key", "The fake provider does not use credentials.")
        key = validate_api_key(api_key)
        self.secrets.set(provider_secret_name(provider_id), key)  # raises SecretStoreError if unavailable
        self._invalidate(provider_id)
        return self.public(self.repo.get(provider_id))  # type: ignore[arg-type]

    def delete_credential(self, provider_id: str) -> dict:
        rec = self.repo.get(provider_id)
        if rec is None:
            raise ProviderConfigError("id", "Unknown provider.")
        old = None
        try:
            old = self.secrets.get(provider_secret_name(provider_id))
        except SecretStoreError:
            pass
        self.secrets.delete(provider_secret_name(provider_id))
        if old:
            # Keep redacting the old value for the rest of this process lifetime.
            redactor.register_secret(old)
        self._invalidate(provider_id)
        return self.public(self.repo.get(provider_id))  # type: ignore[arg-type]

    def delete(self, provider_id: str) -> bool:
        rec = self.repo.get(provider_id)
        if rec is None:
            return False
        if rec["kind"] != "fake":
            try:
                self.secrets.delete(provider_secret_name(provider_id))
            except SecretStoreError as exc:
                log.warning("could not delete credential for %s: %s", provider_id, exc.safe_message)
        self._invalidate(provider_id)
        with self._lock:
            self._overrides.pop(provider_id, None)
        return self.repo.delete(provider_id)

    def set_default(self, provider_id: str) -> dict:
        rec = self.repo.get(provider_id)
        if rec is None:
            raise ProviderConfigError("id", "Unknown provider.")
        self.repo.set_default(provider_id)
        return self.public(self.repo.get(provider_id))  # type: ignore[arg-type]

    # -------------------------------------------------------------- usage
    def resolve_record(self, provider_id: Optional[str]) -> Optional[dict]:
        if provider_id:
            return self.repo.get(provider_id)
        return self.repo.get_default()

    def build(self, provider_id: str) -> LLMProvider:
        rec = self.repo.get(provider_id)
        if rec is None:
            raise ProviderError(ErrorCode.NOT_CONFIGURED, f"provider '{provider_id}' is not configured")
        if not rec.get("enabled", True):
            raise ProviderError(ErrorCode.NOT_CONFIGURED, f"provider '{provider_id}' is disabled")
        with self._lock:
            override = self._overrides.get(provider_id)
            cached = self._cache.get(provider_id)
        if override is not None:
            return override
        if cached is not None and cached[0] == rec.get("updated_at"):
            return cached[1]
        cfg = ProviderConfig.from_record(rec)
        if cfg.kind == "fake":
            if not self.allow_fake:
                raise ProviderError(ErrorCode.NOT_CONFIGURED, "the fake provider is disabled")
            from .fake_provider import FakeProvider
            provider: LLMProvider = FakeProvider(cfg)
        else:
            try:
                key = self.secrets.get(provider_secret_name(provider_id))
            except SecretStoreError as exc:
                raise ProviderError(ErrorCode.CREDENTIAL_MISSING, exc.safe_message, provider_kind=cfg.kind)
            if not key:
                raise ProviderError(
                    ErrorCode.CREDENTIAL_MISSING,
                    f"no API key is stored for provider '{provider_id}'", provider_kind=cfg.kind,
                )
            if cfg.kind in ("openai", "openai_compatible"):
                from .openai_provider import OpenAIProvider
                provider = OpenAIProvider(cfg, key)
            elif cfg.kind == "anthropic":
                from .anthropic_provider import AnthropicProvider
                provider = AnthropicProvider(cfg, key)
            elif cfg.kind == "gemini":
                from .gemini_provider import GeminiProvider
                provider = GeminiProvider(cfg, key)
            else:  # pragma: no cover - validate() prevents this
                raise ProviderError(ErrorCode.NOT_CONFIGURED, f"unsupported provider kind {cfg.kind!r}")
        with self._lock:
            self._cache[provider_id] = (rec.get("updated_at"), provider)
        return provider

    def test(self, provider_id: str) -> dict:
        try:
            provider = self.build(provider_id)
        except ProviderError as exc:
            result = {"ok": False, "structured_output_ok": False, "mode": None, "model": None,
                      "latency_ms": 0, "usage": None, "error": exc.to_dict()}
        else:
            result = provider.test_connection()
        result["tested_at"] = utc_now_iso()
        if self.repo.get(provider_id) is not None:
            self.repo.set_last_test(provider_id, result)
        return result
