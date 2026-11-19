"""Provider-independent LLM layer.

Providers are *transports*: they turn (system, user, optional JSON schema)
into text or parsed JSON, with normalized errors, usage and latency. Prompts
and schemas for planning/evaluation live in `agentic_os.orchestration.llm_ops`.
"""
