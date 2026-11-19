"""OpenAI and OpenAI-compatible endpoints via the official `openai` SDK (Chat Completions)."""

from __future__ import annotations

import time
from typing import Iterator, Optional

from .base import (
    ErrorCode, LLMProvider, LLMResult, ProviderCapabilities, ProviderConfig, ProviderError, Usage,
    extract_json, retry_after_from, safe_schema_name, schema_instructions, status_to_error,
)
from .catalog import PROVIDER_KINDS

_UNSUPPORTED_MARKERS = (
    "response_format", "json_schema", "json_object", "unsupported parameter", "unsupported value",
    "not supported", "max_tokens", "max_completion_tokens", "temperature", "reasoning_effort",
)


class OpenAIProvider(LLMProvider):
    kind = "openai"

    def __init__(self, config: ProviderConfig, api_key: Optional[str]):
        super().__init__(config, api_key)
        self.kind = config.kind if config.kind in ("openai", "openai_compatible") else "openai"
        self._client = None
        self._sdk = None

    def capabilities(self) -> ProviderCapabilities:
        meta = PROVIDER_KINDS[self.kind]
        return ProviderCapabilities(
            structured_modes=list(meta["structured_modes"]),
            default_structured_mode=meta["default_mode"],
            supports_streaming=True,
            supports_tool_calling=True,
            supports_json_schema=True,
            notes=meta["notes"],
        )

    # ----------------------------------------------------------------- SDK
    def _get_client(self):
        if self._client is not None:
            return self._sdk, self._client
        try:
            import openai  # type: ignore
        except ImportError:
            raise ProviderError(ErrorCode.SDK_MISSING, "the 'openai' package is not installed", provider_kind=self.kind)
        kwargs = {
            "api_key": self._api_key or "missing-key",
            "timeout": float(self.config.timeout_s),
            "max_retries": 0,  # retries are decided by the orchestrator, not hidden in the SDK
        }
        if self.config.base_url:
            kwargs["base_url"] = self.config.base_url
        self._sdk = openai
        self._client = openai.OpenAI(**kwargs)
        return self._sdk, self._client

    def _base_params(self, system: str, user: str, max_output_tokens: int) -> dict:
        cfg = self.config
        params = {
            "model": cfg.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        token_param = cfg.extra.get("token_param") or ("max_completion_tokens" if self.kind == "openai" else "max_tokens")
        if token_param not in ("max_tokens", "max_completion_tokens"):
            token_param = "max_tokens"
        params[token_param] = int(max_output_tokens)
        if cfg.temperature is not None:
            params["temperature"] = float(cfg.temperature)
        effort = cfg.extra.get("reasoning_effort")
        if effort in ("minimal", "low", "medium", "high"):
            params["reasoning_effort"] = effort
        return params

    # ---------------------------------------------------------------- call
    def _call(self, system, user, *, schema, schema_name, mode, max_output_tokens) -> LLMResult:
        sdk, client = self._get_client()
        params = self._base_params(system, user, max_output_tokens)
        if schema is not None:
            if mode == "json_schema":
                params["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": safe_schema_name(schema_name), "schema": schema, "strict": False},
                }
            else:
                if mode == "json_object":
                    params["response_format"] = {"type": "json_object"}
                params["messages"][0]["content"] = system + "\n\n" + schema_instructions(schema)
        started = time.monotonic()
        try:
            resp = client.chat.completions.create(**params)
        except Exception as exc:  # noqa: BLE001 - normalized below
            raise self._map_error(sdk, exc)
        latency = int((time.monotonic() - started) * 1000)

        choices = getattr(resp, "choices", None) or []
        if not choices:
            raise ProviderError(ErrorCode.INVALID_OUTPUT, "the response contained no choices", provider_kind=self.kind)
        choice = choices[0]
        message = choice.message
        refusal = getattr(message, "refusal", None)
        if refusal:
            raise ProviderError(ErrorCode.CONTENT_FILTERED, f"the model refused: {refusal}", provider_kind=self.kind)
        finish = getattr(choice, "finish_reason", None)
        if finish == "content_filter":
            raise ProviderError(ErrorCode.CONTENT_FILTERED, "the response was blocked by the provider's content filter", provider_kind=self.kind)
        text = message.content or ""
        u = getattr(resp, "usage", None)
        usage = Usage(
            input_tokens=int(getattr(u, "prompt_tokens", 0) or 0) if u else 0,
            output_tokens=int(getattr(u, "completion_tokens", 0) or 0) if u else 0,
        )
        data = None
        if schema is not None:
            if finish == "length":
                raise ProviderError(ErrorCode.OUTPUT_TRUNCATED, "structured output was cut off by the token limit", provider_kind=self.kind)
            data = extract_json(text)
        return LLMResult(
            text=text, data=data, usage=usage, latency_ms=latency,
            model=getattr(resp, "model", None) or self.config.model, provider_kind=self.kind,
            finish_reason=finish, mode=mode,
        )

    def _stream(self, system: str, user: str, *, max_output_tokens: int) -> Iterator[str]:
        sdk, client = self._get_client()
        params = self._base_params(system, user, max_output_tokens)
        params["stream"] = True
        try:
            stream = client.chat.completions.create(**params)
            for chunk in stream:
                choices = getattr(chunk, "choices", None) or []
                if choices:
                    delta = getattr(choices[0], "delta", None)
                    piece = getattr(delta, "content", None) if delta is not None else None
                    if piece:
                        yield piece
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(sdk, exc)

    # -------------------------------------------------------------- errors
    def transcribe(self, audio: bytes, mime: str) -> str:
        sdk, client = self._get_client()
        ext = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/wav": "wav", "audio/mp4": "mp4", "audio/mpeg": "mp3"}.get(
            mime.split(";")[0], "webm")
        try:
            res = client.audio.transcriptions.create(model=self.config.extra.get("transcribe_model") or "gpt-4o-mini-transcribe",
                                                     file=(f"speech.{ext}", audio, mime.split(";")[0]))
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(sdk, exc)
        return (getattr(res, "text", "") or "").strip()

    def _map_error(self, sdk, exc: Exception) -> ProviderError:
        kind = self.kind
        status = getattr(exc, "status_code", None)
        msg = str(getattr(exc, "message", None) or exc)
        if sdk is None:
            return ProviderError(ErrorCode.UNKNOWN, f"{type(exc).__name__}: {msg}", provider_kind=kind)
        if isinstance(exc, sdk.APITimeoutError):
            return ProviderError(ErrorCode.TIMEOUT, f"request timed out after {self.config.timeout_s:.0f}s", provider_kind=kind)
        if isinstance(exc, sdk.APIConnectionError):
            return ProviderError(ErrorCode.CONNECTION_ERROR, f"could not connect to the provider: {msg}", provider_kind=kind)
        if isinstance(exc, sdk.AuthenticationError):
            return ProviderError(ErrorCode.AUTH_FAILED, f"authentication failed: {msg}", status_code=status, provider_kind=kind)
        if isinstance(exc, sdk.PermissionDeniedError):
            return ProviderError(ErrorCode.PERMISSION_DENIED, f"permission denied: {msg}", status_code=status, provider_kind=kind)
        if isinstance(exc, sdk.NotFoundError):
            return ProviderError(
                ErrorCode.MODEL_NOT_FOUND,
                f"model '{self.config.model}' or endpoint not found: {msg}", status_code=status, provider_kind=kind,
            )
        if isinstance(exc, sdk.RateLimitError):
            code = getattr(exc, "code", None)
            if code == "insufficient_quota" or "quota" in msg.lower():
                return ProviderError(ErrorCode.QUOTA_EXCEEDED, f"quota exceeded: {msg}", status_code=status, provider_kind=kind, retryable=False)
            return ProviderError(
                ErrorCode.RATE_LIMITED, f"rate limited: {msg}", status_code=status, provider_kind=kind,
                retry_after_s=retry_after_from(exc),
            )
        if isinstance(exc, sdk.BadRequestError):
            low = msg.lower()
            if any(marker in low for marker in _UNSUPPORTED_MARKERS):
                return ProviderError(ErrorCode.UNSUPPORTED_FEATURE, f"the model rejected a request option: {msg}", status_code=status, provider_kind=kind)
            return ProviderError(ErrorCode.BAD_REQUEST, f"bad request: {msg}", status_code=status, provider_kind=kind)
        if isinstance(exc, sdk.InternalServerError):
            return ProviderError(ErrorCode.SERVER_ERROR, f"provider server error: {msg}", status_code=status, provider_kind=kind)
        if isinstance(exc, sdk.APIStatusError):
            return status_to_error(status, msg, kind)
        return ProviderError(ErrorCode.UNKNOWN, f"{type(exc).__name__}: {msg}", provider_kind=kind)
