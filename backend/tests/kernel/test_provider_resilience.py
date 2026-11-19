"""Transient provider errors are retried, then fallback models are tried; the planner degrades to rules."""

from agentic_os.kernel.planner import HybridPlanner
from agentic_os.providers.base import ErrorCode, LLMResult, ProviderError
from agentic_os.providers.gemini_provider import GeminiProvider
from agentic_os.providers.base import ProviderConfig


def _provider(script):
    cfg = ProviderConfig(id="g", kind="gemini", display_name="Gemini", model="gemini-3.5-flash")
    p = GeminiProvider(cfg, "k")
    p.RETRY_DELAYS_S = (0.0, 0.0)
    calls = []

    def fake_call(self, system, user, **kw):
        calls.append(self.config.model)
        out = script(self.config.model, len(calls))
        if isinstance(out, Exception):
            raise out
        return LLMResult(text=out, model=self.config.model)

    GeminiProvider._call = fake_call
    return p, calls


def test_retry_then_success(monkeypatch):
    orig = GeminiProvider._call
    try:
        p, calls = _provider(lambda m, n: ProviderError(ErrorCode.SERVER_ERROR, "503") if n < 3 else "ok")
        assert p.generate_text("s", "u").text == "ok"
        assert calls == ["gemini-3.5-flash"] * 3
    finally:
        GeminiProvider._call = orig


def test_falls_back_to_another_model(monkeypatch):
    orig = GeminiProvider._call
    try:
        p, calls = _provider(lambda m, n: "ok" if m == "gemini-flash-latest" else ProviderError(ErrorCode.SERVER_ERROR, "503"))
        assert p.generate_text("s", "u").text == "ok"
        assert calls[:3] == ["gemini-3.5-flash"] * 3 and calls[-1] == "gemini-flash-latest"
        assert p.config.model == "gemini-3.5-flash"
    finally:
        GeminiProvider._call = orig


def test_auth_errors_are_not_retried():
    orig = GeminiProvider._call
    try:
        p, calls = _provider(lambda m, n: ProviderError(ErrorCode.AUTH_FAILED, "bad key"))
        try:
            p.generate_text("s", "u")
        except ProviderError as exc:
            assert exc.code == ErrorCode.AUTH_FAILED
        assert len(calls) == 1
    finally:
        GeminiProvider._call = orig


def test_planner_uses_offline_plan_when_model_is_down():
    class Down:
        def plan(self, *a, **k):
            raise ProviderError(ErrorCode.SERVER_ERROR, "503")

    out = HybridPlanner(llm_factory=lambda: Down()).plan("hello there, what does the README say?", [])
    assert out["planner"] == "rules" and "offline" in out["plan"]["goal"]
