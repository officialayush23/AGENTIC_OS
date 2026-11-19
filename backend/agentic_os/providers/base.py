"""Provider interface, normalized errors and results (standard library only)."""

from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from ..security.redaction import redactor
from ..util import truncate_text


class ErrorCode:
    AUTH_FAILED = "PROVIDER_AUTH_FAILED"
    PERMISSION_DENIED = "PROVIDER_PERMISSION_DENIED"
    RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    QUOTA_EXCEEDED = "PROVIDER_QUOTA_EXCEEDED"
    TIMEOUT = "PROVIDER_TIMEOUT"
    CONNECTION_ERROR = "PROVIDER_CONNECTION_ERROR"
    MODEL_NOT_FOUND = "PROVIDER_MODEL_NOT_FOUND"
    BAD_REQUEST = "PROVIDER_BAD_REQUEST"
    UNSUPPORTED_FEATURE = "PROVIDER_UNSUPPORTED_FEATURE"
    INVALID_OUTPUT = "PROVIDER_INVALID_OUTPUT"
    OUTPUT_TRUNCATED = "PROVIDER_OUTPUT_TRUNCATED"
    CONTENT_FILTERED = "PROVIDER_CONTENT_FILTERED"
    SERVER_ERROR = "PROVIDER_SERVER_ERROR"
    NOT_CONFIGURED = "PROVIDER_NOT_CONFIGURED"
    CREDENTIAL_MISSING = "PROVIDER_CREDENTIAL_MISSING"
    SDK_MISSING = "PROVIDER_SDK_MISSING"
    BUDGET_EXCEEDED = "LLM_BUDGET_EXCEEDED"
    UNKNOWN = "PROVIDER_ERROR"


RETRYABLE_CODES = {ErrorCode.RATE_LIMITED, ErrorCode.TIMEOUT, ErrorCode.CONNECTION_ERROR, ErrorCode.SERVER_ERROR}

USER_HINTS = {
    ErrorCode.AUTH_FAILED: "The API key was rejected. Replace the credential in Settings → Providers.",
    ErrorCode.PERMISSION_DENIED: "The key is valid but not allowed to use this model or endpoint.",
    ErrorCode.RATE_LIMITED: "The provider is rate limiting requests. Wait and retry.",
    ErrorCode.QUOTA_EXCEEDED: "The account has no remaining quota or credit. Check billing with the provider.",
    ErrorCode.TIMEOUT: "The provider did not answer in time. Retry, or raise the timeout in provider settings.",
    ErrorCode.CONNECTION_ERROR: "Could not reach the provider. Check your internet connection and base URL.",
    ErrorCode.MODEL_NOT_FOUND: "The model ID was not found. Check the model ID in provider settings.",
    ErrorCode.UNSUPPORTED_FEATURE: "This model rejected a request option. Try another structured-output mode in provider settings.",
    ErrorCode.INVALID_OUTPUT: "The model did not return valid structured output.",
    ErrorCode.OUTPUT_TRUNCATED: "The model ran out of output tokens. Raise 'max output tokens' in provider settings.",
    ErrorCode.CREDENTIAL_MISSING: "No API key is stored for this provider. Add one in Settings → Providers.",
    ErrorCode.SDK_MISSING: "The provider SDK is not installed. Re-run setup.",
}


class ProviderError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: Optional[bool] = None,
        status_code: Optional[int] = None,
        provider_kind: Optional[str] = None,
        retry_after_s: Optional[float] = None,
    ):
        safe = truncate_text(redactor.redact_text(str(message or code)), 400)
        super().__init__(safe)
        self.code = code
        self.message = safe
        self.retryable = (code in RETRYABLE_CODES) if retryable is None else retryable
        self.status_code = status_code
        self.provider_kind = provider_kind
        self.retry_after_s = retry_after_s

    @property
    def hint(self) -> Optional[str]:
        return USER_HINTS.get(self.code)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "status_code": self.status_code,
            "provider_kind": self.provider_kind,
            "hint": self.hint,
        }


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return int(self.input_tokens or 0) + int(self.output_tokens or 0)

    def as_dict(self) -> dict:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens, "total_tokens": self.total_tokens}


@dataclass
class LLMResult:
    text: str
    data: Optional[Any] = None
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    model: str = ""
    provider_kind: str = ""
    finish_reason: Optional[str] = None
    mode: str = "text"

    def meta(self) -> dict:
        return {
            "model": self.model,
            "provider_kind": self.provider_kind,
            "latency_ms": self.latency_ms,
            "finish_reason": self.finish_reason,
            "mode": self.mode,
            **self.usage.as_dict(),
        }


@dataclass
class ProviderCapabilities:
    structured_modes: List[str]
    default_structured_mode: str
    supports_streaming: bool = True
    supports_tool_calling: bool = False
    supports_json_schema: bool = False
    notes: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProviderConfig:
    id: str  # noqa: A003
    kind: str
    display_name: str
    model: str
    base_url: Optional[str] = None
    timeout_s: float = 60.0
    max_output_tokens: int = 4096
    temperature: Optional[float] = None
    structured_mode: str = "auto"
    max_calls_per_task: int = 8
    extra: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_record(cls, rec: dict) -> "ProviderConfig":
        return cls(
            id=rec["id"],
            kind=rec["kind"],
            display_name=rec.get("display_name") or rec["id"],
            model=rec["model"],
            base_url=rec.get("base_url"),
            timeout_s=float(rec.get("timeout_s") or 60.0),
            max_output_tokens=int(rec.get("max_output_tokens") or 4096),
            temperature=rec.get("temperature"),
            structured_mode=rec.get("structured_mode") or "auto",
            max_calls_per_task=int(rec.get("max_calls_per_task") or 8),
            extra=dict(rec.get("extra") or {}),
        )


_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*\n?(.*?)\n?\s*```\s*$", re.S)


def extract_json(text: str) -> Any:
    """Parse JSON from a model response that may be wrapped in prose or code fences."""
    if text is None:
        raise ProviderError(ErrorCode.INVALID_OUTPUT, "empty response")
    candidate = text.strip()
    m = _FENCE_RE.match(candidate)
    if m:
        candidate = m.group(1).strip()
    try:
        return json.loads(candidate)
    except ValueError:
        pass
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(candidate[start:end + 1])
        except ValueError:
            pass
    raise ProviderError(ErrorCode.INVALID_OUTPUT, "the model response was not valid JSON")


def retry_after_from(exc: Exception) -> Optional[float]:
    """Read a Retry-After header from an SDK exception, if any."""
    try:
        value = exc.response.headers.get("retry-after")  # type: ignore[attr-defined]
        return float(value) if value else None
    except Exception:  # noqa: BLE001
        return None


def status_to_error(status: Optional[int], msg: str, kind: str) -> ProviderError:
    """Map a bare HTTP status to a normalized provider error."""
    if status == 401:
        return ProviderError(ErrorCode.AUTH_FAILED, f"authentication failed: {msg}", status_code=status, provider_kind=kind)
    if status == 403:
        return ProviderError(ErrorCode.PERMISSION_DENIED, f"permission denied: {msg}", status_code=status, provider_kind=kind)
    if status == 404:
        return ProviderError(ErrorCode.MODEL_NOT_FOUND, f"not found: {msg}", status_code=status, provider_kind=kind)
    if status == 408:
        return ProviderError(ErrorCode.TIMEOUT, f"timeout: {msg}", status_code=status, provider_kind=kind)
    if status == 429:
        return ProviderError(ErrorCode.RATE_LIMITED, f"rate limited: {msg}", status_code=status, provider_kind=kind)
    if status is not None and status >= 500:
        return ProviderError(ErrorCode.SERVER_ERROR, f"provider server error ({status}): {msg}", status_code=status, provider_kind=kind)
    return ProviderError(ErrorCode.BAD_REQUEST, f"request failed ({status}): {msg}", status_code=status, provider_kind=kind)


def schema_instructions(schema: dict) -> str:
    """Prompt text describing the required JSON output (used when the API cannot enforce a schema)."""
    return (
        "Respond with exactly one JSON object and nothing else (no prose, no code fences). "
        "The object must conform to this JSON Schema:\n"
        + json.dumps(schema, separators=(",", ":"), ensure_ascii=False)
    )


_NAME_RE = re.compile(r"[^A-Za-z0-9_\-]")


def safe_schema_name(name: str) -> str:
    cleaned = _NAME_RE.sub("_", name or "result")[:64]
    return cleaned or "result"


def inline_schema_refs(schema: dict) -> dict:
    """Resolve local `$ref`s (`#/$defs/...`) so every provider receives a self-contained schema."""
    defs = schema.get("$defs") or schema.get("definitions") or {}

    def resolve(node: Any, depth: int = 0) -> Any:
        if depth > 30:
            return {}
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and (ref.startswith("#/$defs/") or ref.startswith("#/definitions/")):
                target = defs.get(ref.split("/")[-1], {})
                merged = {k: v for k, v in node.items() if k != "$ref"}
                resolved = resolve(target, depth + 1)
                if isinstance(resolved, dict):
                    resolved = {**resolved, **{k: resolve(v, depth + 1) for k, v in merged.items()}}
                return resolved
            return {k: resolve(v, depth + 1) for k, v in node.items() if k not in ("$defs", "definitions")}
        if isinstance(node, list):
            return [resolve(x, depth + 1) for x in node]
        return node

    return resolve(schema)


class LLMProvider(ABC):
    """Base class. Subclasses implement `_call` (and optionally `_stream`)."""

    kind = "abstract"

    def __init__(self, config: ProviderConfig, api_key: Optional[str]):
        self.config = config
        self._api_key = api_key
        if api_key:
            redactor.register_secret(api_key)

    def __repr__(self) -> str:  # never leak the key through repr()
        return f"<{type(self).__name__} id={self.config.id!r} model={self.config.model!r}>"

    # ------------------------------------------------------------ interface
    @abstractmethod
    def capabilities(self) -> ProviderCapabilities:
        ...

    @abstractmethod
    def _call(
        self,
        system: str,
        user: str,
        *,
        schema: Optional[dict],
        schema_name: str,
        mode: str,
        max_output_tokens: int,
    ) -> LLMResult:
        """Perform one request. Must raise ProviderError for every failure."""

    def _stream(self, system: str, user: str, *, max_output_tokens: int) -> Iterator[str]:
        result = self._call(system, user, schema=None, schema_name="text", mode="text", max_output_tokens=max_output_tokens)
        yield result.text

    # -------------------------------------------------------------- helpers
    def resolve_mode(self, requested: Optional[str] = None) -> str:
        caps = self.capabilities()
        mode = requested or self.config.structured_mode or "auto"
        if mode == "auto" or mode not in caps.structured_modes:
            return caps.default_structured_mode
        return mode

    def generate_text(self, system: str, user: str, *, max_output_tokens: Optional[int] = None) -> LLMResult:
        started = time.monotonic()
        result = self._call(
            system, user, schema=None, schema_name="text", mode="text",
            max_output_tokens=int(max_output_tokens or self.config.max_output_tokens),
        )
        result.latency_ms = result.latency_ms or int((time.monotonic() - started) * 1000)
        result.provider_kind = self.kind
        return result

    def generate_structured(
        self,
        system: str,
        user: str,
        schema: dict,
        schema_name: str,
        *,
        max_output_tokens: Optional[int] = None,
    ) -> LLMResult:
        mode = self.resolve_mode()
        clean_schema = inline_schema_refs(schema)
        started = time.monotonic()
        result = self._call(
            system, user, schema=clean_schema, schema_name=schema_name, mode=mode,
            max_output_tokens=int(max_output_tokens or self.config.max_output_tokens),
        )
        result.latency_ms = result.latency_ms or int((time.monotonic() - started) * 1000)
        result.provider_kind = self.kind
        result.mode = mode
        if result.data is None:
            if result.finish_reason in ("length", "max_tokens", "MAX_TOKENS"):
                raise ProviderError(ErrorCode.OUTPUT_TRUNCATED, "structured output was cut off by the token limit", provider_kind=self.kind)
            result.data = extract_json(result.text)
        if not isinstance(result.data, dict):
            raise ProviderError(ErrorCode.INVALID_OUTPUT, "structured output must be a JSON object", provider_kind=self.kind)
        return result

    def stream_text(self, system: str, user: str, *, max_output_tokens: Optional[int] = None) -> Iterator[str]:
        return self._stream(system, user, max_output_tokens=int(max_output_tokens or self.config.max_output_tokens))

    def test_connection(self) -> dict:
        """Small real request that also probes structured output support."""
        schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}, "echo": {"type": "string"}},
            "required": ["ok", "echo"],
        }
        started = time.monotonic()
        try:
            result = self.generate_structured(
                "You are a connectivity check. Reply only with a JSON object.",
                'Return JSON exactly like {"ok": true, "echo": "agentic-os"}.',
                schema,
                "connectivity_check",
                max_output_tokens=min(1024, max(256, self.config.max_output_tokens)),
            )
            data = result.data or {}
            structured_ok = data.get("ok") is True
            return {
                "ok": True,
                "structured_output_ok": structured_ok,
                "mode": result.mode,
                "model": result.model or self.config.model,
                "latency_ms": result.latency_ms,
                "usage": result.usage.as_dict(),
                "error": None if structured_ok else {
                    "code": ErrorCode.INVALID_OUTPUT,
                    "message": "Connected, but the model did not follow the JSON instruction.",
                },
            }
        except ProviderError as exc:
            return {
                "ok": False,
                "structured_output_ok": False,
                "mode": self.resolve_mode(),
                "model": self.config.model,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "usage": None,
                "error": exc.to_dict(),
            }
