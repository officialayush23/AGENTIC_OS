"""Static metadata about supported provider kinds.

Model IDs change often; these are *editable defaults* checked against provider
documentation on 2026-10-16. The connection test reports the real error if a
model ID is wrong or retired.
"""

from __future__ import annotations

PROVIDER_KINDS = {
    "openai": {
        "label": "OpenAI",
        "default_model": "gpt-5.4-mini",
        "example_models": ["gpt-5.4-mini", "gpt-5.2", "gpt-4.1-mini"],
        "requires_base_url": False,
        "structured_modes": ["json_object", "json_schema", "prompt"],
        "default_mode": "json_object",
        "default_max_output_tokens": 8000,
        "key_hint": "Starts with sk-",
        "docs_url": "https://platform.openai.com/docs/models",
        "notes": "Uses Chat Completions with max_completion_tokens. GPT-5-family reasoning models only accept the "
                 "default temperature, so leave temperature empty unless your model supports it.",
    },
    "anthropic": {
        "label": "Anthropic",
        "default_model": "claude-sonnet-4-6",
        "example_models": ["claude-sonnet-4-6", "claude-sonnet-5", "claude-haiku-4-5"],
        "requires_base_url": False,
        "structured_modes": ["tool_call", "prompt"],
        "default_mode": "tool_call",
        "default_max_output_tokens": 8000,
        "key_hint": "Starts with sk-ant-",
        "docs_url": "https://docs.anthropic.com/en/docs/about-claude/models",
        "notes": "Structured output uses a forced tool call (tool_choice) with the plan schema as the tool input schema.",
    },
    "gemini": {
        "label": "Google Gemini",
        "default_model": "gemini-flash-latest",
        "example_models": ["gemini-flash-latest", "gemini-3.8-flash", "gemini-3.6-flash", "gemini-flash-lite-latest"],
        "requires_base_url": False,
        "structured_modes": ["json_mode", "prompt"],
        "default_mode": "json_mode",
        "default_max_output_tokens": 8192,
        "key_hint": "Starts with AIza",
        "docs_url": "https://ai.google.dev/gemini-api/docs/models",
        "notes": "Uses the google-genai SDK with response_mime_type=application/json; the schema is described in the "
                 "prompt and validated locally.",
    },
    "local": {
        "label": "Local model (this PC)",
        "default_model": "qwen2.5-1.5b-gpu-dml",
        "example_models": ["qwen2.5-1.5b-gpu-dml", "qwen2.5-1.5b-cpu", "qwen2.5-3b-cpu"],
        "requires_base_url": False,
        "requires_key": False,
        "structured_modes": ["json_schema", "prompt"],
        "default_mode": "json_schema",
        "default_max_output_tokens": 1536,
        "key_hint": "No key needed",
        "docs_url": "https://onnxruntime.ai/docs/genai/",
        "notes": "Runs a quantized Qwen2.5 with ONNX Runtime GenAI on your GPU (DirectML) or CPU. No internet, no quota. "
                 "JSON output is constrained to the schema. Install it from Settings → Local AI model.",
    },
    "openai_compatible": {
        "label": "OpenAI-compatible endpoint",
        "default_model": "",
        "example_models": [],
        "requires_base_url": True,
        "structured_modes": ["json_object", "json_schema", "prompt"],
        "default_mode": "json_object",
        "default_max_output_tokens": 4096,
        "key_hint": "Whatever key the endpoint expects",
        "docs_url": "",
        "notes": "For gateways exposing /v1/chat/completions. Support for response_format varies: if the connection "
                 "test fails with an unsupported-feature error, switch structured mode to 'prompt'. Sends max_tokens "
                 "unless extra.token_param is set to max_completion_tokens.",
    },
    "fake": {
        "label": "Fake provider (tests and development only)",
        "default_model": "fake-model",
        "example_models": ["fake-model"],
        "requires_base_url": False,
        "structured_modes": ["json_object"],
        "default_mode": "json_object",
        "default_max_output_tokens": 4096,
        "key_hint": "No key required",
        "docs_url": "",
        "notes": "Deterministic scripted responses. Hidden unless AGENTIC_OS_ENABLE_FAKE_PROVIDER=1.",
        "hidden": True,
    },
}


def public_catalog(include_hidden: bool = False) -> list:
    out = []
    for kind, meta in PROVIDER_KINDS.items():
        if meta.get("hidden") and not include_hidden:
            continue
        out.append({"kind": kind, **{k: v for k, v in meta.items() if k != "hidden"}})
    return out
