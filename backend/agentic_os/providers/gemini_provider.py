"""Google Gemini via the official `google-genai` SDK."""

from __future__ import annotations

import time
from typing import Iterator, Optional

from .base import (
    ErrorCode, LLMProvider, LLMResult, ProviderCapabilities, ProviderConfig, ProviderError, Usage,
    extract_json, schema_instructions,
)
from .catalog import PROVIDER_KINDS

_BLOCKED = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION", "IMAGE_SAFETY"}


class GeminiProvider(LLMProvider):
    kind = "gemini"

    def __init__(self, config: ProviderConfig, api_key: Optional[str]):
        super().__init__(config, api_key)
        self._client = None
        self._types = None

    def capabilities(self) -> ProviderCapabilities:
        meta = PROVIDER_KINDS["gemini"]
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
            return self._types, self._client
        try:
            from google import genai  # type: ignore
            from google.genai import types  # type: ignore
        except ImportError:
            raise ProviderError(ErrorCode.SDK_MISSING, "the 'google-genai' package is not installed", provider_kind=self.kind)
        http_kwargs = {"timeout": int(float(self.config.timeout_s) * 1000)}  # milliseconds
        if self.config.base_url:
            http_kwargs["base_url"] = self.config.base_url
        self._types = types
        self._client = genai.Client(api_key=self._api_key or "missing-key", http_options=types.HttpOptions(**http_kwargs))
        return self._types, self._client

    def _config(self, types, system: str, max_output_tokens: int, json_mode: bool):
        kwargs = {"system_instruction": system, "max_output_tokens": int(max_output_tokens)}
        if self.config.temperature is not None:
            kwargs["temperature"] = float(self.config.temperature)
        if json_mode:
            kwargs["response_mime_type"] = "application/json"
        budget = self.config.extra.get("thinking_budget")
        if isinstance(budget, int) and not isinstance(budget, bool):
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=budget)
        return types.GenerateContentConfig(**kwargs)

    def _call(self, system, user, *, schema, schema_name, mode, max_output_tokens) -> LLMResult:
        types, client = self._get_client()
        json_mode = schema is not None and mode == "json_mode"
        sys_prompt = system + ("\n\n" + schema_instructions(schema) if schema is not None else "")
        started = time.monotonic()
        try:
            resp = client.models.generate_content(
                model=self.config.model, contents=user, config=self._config(types, sys_prompt, max_output_tokens, json_mode)
            )
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc)
        latency = int((time.monotonic() - started) * 1000)

        candidates = getattr(resp, "candidates", None) or []
        finish = None
        if candidates:
            fr = getattr(candidates[0], "finish_reason", None)
            finish = getattr(fr, "name", None) or (str(fr).split(".")[-1] if fr is not None else None)
        else:
            feedback = getattr(resp, "prompt_feedback", None)
            if feedback is not None and getattr(feedback, "block_reason", None):
                raise ProviderError(ErrorCode.CONTENT_FILTERED, "the prompt was blocked by the provider's safety filter", provider_kind=self.kind)
        if finish in _BLOCKED:
            raise ProviderError(ErrorCode.CONTENT_FILTERED, f"the response was blocked ({finish})", provider_kind=self.kind)
        try:
            text = resp.text or ""
        except Exception:  # noqa: BLE001 - some SDK versions raise when there are no text parts
            text = ""
        um = getattr(resp, "usage_metadata", None)
        usage = Usage(
            input_tokens=int(getattr(um, "prompt_token_count", 0) or 0) if um else 0,
            output_tokens=(int(getattr(um, "candidates_token_count", 0) or 0) + int(getattr(um, "thoughts_token_count", 0) or 0)) if um else 0,
        )
        data = None
        if schema is not None:
            if not text.strip() and finish == "MAX_TOKENS":
                raise ProviderError(ErrorCode.OUTPUT_TRUNCATED, "structured output was cut off by the token limit", provider_kind=self.kind)
            try:
                data = extract_json(text)
            except ProviderError:
                if finish == "MAX_TOKENS":
                    raise ProviderError(ErrorCode.OUTPUT_TRUNCATED, "structured output was cut off by the token limit", provider_kind=self.kind)
                raise
        return LLMResult(
            text=text, data=data, usage=usage, latency_ms=latency,
            model=getattr(resp, "model_version", None) or self.config.model, provider_kind=self.kind,
            finish_reason=finish, mode=mode,
        )

    def _stream(self, system: str, user: str, *, max_output_tokens: int) -> Iterator[str]:
        types, client = self._get_client()
        try:
            for chunk in client.models.generate_content_stream(
                model=self.config.model, contents=user, config=self._config(types, system, max_output_tokens, False)
            ):
                try:
                    piece = chunk.text
                except Exception:  # noqa: BLE001
                    piece = None
                if piece:
                    yield piece
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc)

    def transcribe(self, audio: bytes, mime: str) -> str:
        types, client = self._get_client()
        try:
            resp = client.models.generate_content(
                model=self.config.model,
                contents=[types.Part.from_bytes(data=audio, mime_type=mime.split(";")[0] or "audio/webm"),
                          "Transcribe this speech exactly, in the language spoken. Reply with the words only."])
        except Exception as exc:  # noqa: BLE001
            raise self._map_error(exc)
        try:
            return (resp.text or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _map_error(self, exc: Exception) -> ProviderError:
        kind = self.kind
        try:
            from google.genai import errors as gerrors  # type: ignore
        except ImportError:  # pragma: no cover
            gerrors = None
        try:
            import httpx  # type: ignore
        except ImportError:  # pragma: no cover
            httpx = None
        if gerrors is not None and isinstance(exc, gerrors.APIError):
            code = getattr(exc, "code", None)
            msg = str(getattr(exc, "message", None) or exc)
            status_text = str(getattr(exc, "status", "") or "")
            low = f"{msg} {status_text}".lower()
            if "api key not valid" in low or "api_key_invalid" in low or "invalid api key" in low:
                return ProviderError(ErrorCode.AUTH_FAILED, f"authentication failed: {msg}", status_code=code, provider_kind=kind)
            if code == 401:
                return ProviderError(ErrorCode.AUTH_FAILED, f"authentication failed: {msg}", status_code=code, provider_kind=kind)
            if code == 403:
                return ProviderError(ErrorCode.PERMISSION_DENIED, f"permission denied: {msg}", status_code=code, provider_kind=kind)
            if code == 404:
                return ProviderError(ErrorCode.MODEL_NOT_FOUND, f"model '{self.config.model}' not found: {msg}", status_code=code, provider_kind=kind)
            if code == 429:
                if "billing" in low or "plan" in low and "quota" in low:
                    return ProviderError(ErrorCode.QUOTA_EXCEEDED, f"quota exceeded: {msg}", status_code=code, provider_kind=kind, retryable=False)
                return ProviderError(ErrorCode.RATE_LIMITED, f"rate limited: {msg}", status_code=code, provider_kind=kind)
            if code == 400:
                if "not supported" in low or "response_mime_type" in low or "thinking" in low or "unsupported" in low:
                    return ProviderError(ErrorCode.UNSUPPORTED_FEATURE, f"the model rejected a request option: {msg}", status_code=code, provider_kind=kind)
                return ProviderError(ErrorCode.BAD_REQUEST, f"bad request: {msg}", status_code=code, provider_kind=kind)
            if code is not None and code >= 500:
                return ProviderError(ErrorCode.SERVER_ERROR, f"provider server error ({code}): {msg}", status_code=code, provider_kind=kind)
            return ProviderError(ErrorCode.BAD_REQUEST, f"request failed ({code}): {msg}", status_code=code, provider_kind=kind)
        if httpx is not None:
            if isinstance(exc, httpx.TimeoutException):
                return ProviderError(ErrorCode.TIMEOUT, f"request timed out after {self.config.timeout_s:.0f}s", provider_kind=kind)
            if isinstance(exc, httpx.TransportError):
                return ProviderError(ErrorCode.CONNECTION_ERROR, f"could not connect to the provider: {exc}", provider_kind=kind)
        if isinstance(exc, TimeoutError):
            return ProviderError(ErrorCode.TIMEOUT, "request timed out", provider_kind=kind)
        return ProviderError(ErrorCode.UNKNOWN, f"{type(exc).__name__}: {exc}", provider_kind=kind)
