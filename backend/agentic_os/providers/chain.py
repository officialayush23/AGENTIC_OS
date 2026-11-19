"""Use the next provider when one is unavailable (busy, out of quota, offline, misconfigured).

Typical chain: [Gemini, local Qwen]. The online model is tried first (with its own retries); if it
cannot answer, the local model on this PC does, so tasks keep working without internet or quota.
Errors that another model would not fix (bad request, output filtered) are raised straight away.
"""

from __future__ import annotations

import logging
from typing import Any, List

from .base import ErrorCode, ProviderError

log = logging.getLogger(__name__)

FALL_THROUGH = {ErrorCode.RATE_LIMITED, ErrorCode.QUOTA_EXCEEDED, ErrorCode.TIMEOUT, ErrorCode.CONNECTION_ERROR,
                ErrorCode.SERVER_ERROR, ErrorCode.AUTH_FAILED, ErrorCode.PERMISSION_DENIED, ErrorCode.MODEL_NOT_FOUND,
                ErrorCode.NOT_CONFIGURED, ErrorCode.CREDENTIAL_MISSING, ErrorCode.SDK_MISSING, ErrorCode.UNKNOWN}


class ChainProvider:
    def __init__(self, providers: List[Any]):
        if not providers:
            raise ValueError("ChainProvider needs at least one provider")
        self.providers = providers
        self.last_used = providers[0]

    @property
    def config(self):
        return self.providers[0].config

    @property
    def kind(self) -> str:
        return self.providers[0].kind

    def _run(self, method: str, *args, local_variant=None, **kwargs):
        last: ProviderError | None = None
        for i, p in enumerate(self.providers):
            try:
                call_args = args
                if local_variant is not None and getattr(p, "kind", "") == "local":
                    # (user, schema) tuned for a small local model: compact prompt, tighter schema
                    call_args = (args[0], local_variant[0], local_variant[1], *args[3:])
                out = getattr(p, method)(*call_args, **kwargs)
                self.last_used = p
                if i:
                    log.info("answered by fallback provider %s after %s", p.kind, last.code if last else "?")
                return out
            except ProviderError as exc:
                if exc.code not in FALL_THROUGH or i == len(self.providers) - 1:
                    raise
                log.warning("%s unavailable (%s); trying %s", p.kind, exc.code, self.providers[i + 1].kind)
                last = exc
        raise last  # pragma: no cover

    def generate_text(self, *args, **kwargs):
        return self._run("generate_text", *args, **kwargs)

    def generate_structured(self, *args, **kwargs):
        return self._run("generate_structured", *args, **kwargs)

    def transcribe(self, *args, **kwargs):
        last = None
        for p in self.providers:  # only providers that can hear
            try:
                return p.transcribe(*args, **kwargs)
            except ProviderError as exc:
                last = exc
        raise last

    def test_connection(self) -> dict:
        return self.providers[0].test_connection()
