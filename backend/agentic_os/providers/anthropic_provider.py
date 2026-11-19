"""Anthropic Claude via the official `anthropic` SDK (Messages API)."""

from __future__ import annotations

import json
import time
from typing import Iterator, Optional

from .base import (
    ErrorCode, LLMProvider, LLMResult, ProviderCapabilities, ProviderConfig, ProviderError, Usage,
    extract_json, retry_after_from, safe_schema_name, schema_instructions, status_to_error,
)
from .catalog import PROVIDER_KINDS

_UNSUPPORTED_MARKERS = ("tool_choice", "temperature", "thinking", "not supported", "does not support", "input_schema")


class AnthropicProvider(LLMProvider):
    kind = "anthropic"

    def __init__(self, config: ProviderConfig, api_key: Optional[str]):
        super().__init__(config, api_key)
        self._client = None
        self._sdk = None

    def capabilities(self) -> ProviderCapabilities:
        meta = PROVIDER_KINDS["anthropic"]
        return ProviderCapabilities(
            structured_modes=list(meta["structured_modes"]),
            default_structured_mode=meta["default_mode"],
            supports_streaming=True,
            supports_tool_calling=True,
            supports_json_schema=False,
            notes=meta["notes"],
        )

    def _get_client(self):
        if self._client is not None:
            return self._sdk, self._client
        try:
            import anthropic  # type: ignore
        except ImportError:
            raise ProviderError(ErrorCode.SDK_MISSING, "the 'anthropic' package is not installed", provider_kind=self.kind)
        kwargs = {"api_key": self._api_key or "missing-key", "timeout": float(self.config.timeout_s), "max_retries": 0}
        if self.config.base_url:
            kwargs["base_url"] = self.config.base_url
        self._sdk = anthropic
        self._client = anthropic.Anthropic(**kwargs)
        return self._sdk, self._client

    def _base_params(self, system: str, user: str, max_output_tokens: int) -> dict:
        params = {
            "model": self.config.model,
            "max_tokens": int(max_output_tokens),
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if self.config.temperature is not None:
            params["temperature"] = float(self.config.temperature)
        return params

    def _call(self, system, user, *, schema, schema_name, mode, max_output_tokens) -> LLMResult:
        sdk, client = self._get_client()
        params = self._base_params(system, user, max_output_tokens)
        tool_mode = schema is not None and mode == "tool_call"
        tool_name = safe_schema_name(schema_name)
        if tool_mode:
            params["tools"] = [{
                "name": tool_name,
                "description": "Submit the structured result. Call this tool exactly once with the complete result.",
                "input_schema": schema,
            }]
            params["tool_choice"] = {"type": "tool", "name": tool_name}
        elif schema is not None:
            params["system"] = system + "\n\n" + schema_instructions(schema)

        started = time.monotonic()
        try:
            resp = client.messages.create(**params)
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(sdk, exc)
        latency = int((time.monotonic() - started) * 1000)

        stop = getattr(resp, "stop_reason", None)
        if stop == "refusal":
            raise ProviderError(ErrorCode.CONTENT_FILTERED, "the model declined to answer", provider_kind=self.kind)
        blocks = getattr(resp, "content", None) or []
        text = "".join(getattr(b, "text", "") or "" for b in blocks if getattr(b, "type", "") == "text")
        data = None
        if tool_mode:
            for block in blocks:
                if getattr(block, "type", "") == "tool_use":
                    raw = getattr(block, "input", None)
                    if isinstance(raw, dict):
                        data = raw
                    break
            if data is None:
                if stop == "max_tokens":
                    raise ProviderError(ErrorCode.OUTPUT_TRUNCATED, "structured output was cut off by the token limit", provider_kind=self.kind)
                data = extract_json(text)
            text = json.dumps(data, ensure_ascii=False)
        elif schema is not None:
            if stop == "max_tokens":
                raise ProviderError(ErrorCode.OUTPUT_TRUNCATED, "structured output was cut off by the token limit", provider_kind=self.kind)
            data = extract_json(text)
        u = getattr(resp, "usage", None)
        usage = Usage(
            input_tokens=int(getattr(u, "input_tokens", 0) or 0) if u else 0,
            output_tokens=int(getattr(u, "output_tokens", 0) or 0) if u else 0,
        )
        return LLMResult(
            text=text, data=data, usage=usage, latency_ms=latency,
            model=getattr(resp, "model", None) or self.config.model, provider_kind=self.kind,
            finish_reason=stop, mode=mode,
        )

    def _stream(self, system: str, user: str, *, max_output_tokens: int) -> Iterator[str]:
        sdk, client = self._get_client()
        params = self._base_params(system, user, max_output_tokens)
        try:
            with client.messages.stream(**params) as stream:
                for piece in stream.text_stream:
                    if piece:
                        yield piece
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(sdk, exc)

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
                ErrorCode.MODEL_NOT_FOUND, f"model '{self.config.model}' not found: {msg}", status_code=status, provider_kind=kind,
            )
        if isinstance(exc, sdk.RateLimitError):
            return ProviderError(
                ErrorCode.RATE_LIMITED, f"rate limited: {msg}", status_code=status, provider_kind=kind,
                retry_after_s=retry_after_from(exc),
            )
        if isinstance(exc, sdk.BadRequestError):
            low = msg.lower()
            if "credit balance" in low or "billing" in low:
                return ProviderError(ErrorCode.QUOTA_EXCEEDED, f"billing problem: {msg}", status_code=status, provider_kind=kind, retryable=False)
            if any(m in low for m in _UNSUPPORTED_MARKERS):
                return ProviderError(ErrorCode.UNSUPPORTED_FEATURE, f"the model rejected a request option: {msg}", status_code=status, provider_kind=kind)
            return ProviderError(ErrorCode.BAD_REQUEST, f"bad request: {msg}", status_code=status, provider_kind=kind)
        if isinstance(exc, sdk.InternalServerError):
            return ProviderError(ErrorCode.SERVER_ERROR, f"provider server error: {msg}", status_code=status, provider_kind=kind)
        if isinstance(exc, sdk.APIStatusError):
            return status_to_error(status, msg, kind)
        return ProviderError(ErrorCode.UNKNOWN, f"{type(exc).__name__}: {msg}", provider_kind=kind)
