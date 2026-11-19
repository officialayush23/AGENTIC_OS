"""Local model provider: ONNX Runtime GenAI on this PC (GPU via DirectML, or CPU).

Structured output uses ONNX Runtime GenAI's guidance (constrained decoding with a JSON schema), so even a
small model returns valid JSON. Generation is serialised per model (one GPU), and loaded models are cached
for the life of the process.
"""

from __future__ import annotations

import json
import os
import threading
import time
import logging
from typing import Dict, Optional, Tuple

from .base import ErrorCode, LLMProvider, LLMResult, ProviderCapabilities, ProviderError, Usage, schema_instructions
from .catalog import PROVIDER_KINDS

log = logging.getLogger(__name__)
_MODELS: Dict[Tuple[str, str], tuple] = {}
_MODELS_LOCK = threading.Lock()
MAX_PROMPT_TOKENS = 12000


def _device_of(path: str, default: str) -> str:
    try:
        with open(os.path.join(path, ".agentic-os-ready"), encoding="utf-8") as fh:
            return json.load(fh).get("device") or default
    except (OSError, ValueError):
        return default


def load_model(path: str, device: str):
    """(model, tokenizer, lock, context_length) for a model folder, loaded once per process."""
    key = (os.path.abspath(path), device)
    with _MODELS_LOCK:
        if key in _MODELS:
            return _MODELS[key]
        try:
            import onnxruntime_genai as og
        except ImportError:
            raise ProviderError(ErrorCode.SDK_MISSING, "the local AI runtime is not installed: open Settings → Local AI "
                                "model and click Install", provider_kind="local")
        if not os.path.exists(os.path.join(path, "genai_config.json")):
            raise ProviderError(ErrorCode.MODEL_NOT_FOUND, f"no local model at {path}: install one in Settings → Local AI model",
                                provider_kind="local")
        def build(dev: str):
            cfg = _config(og, path)
            cfg.clear_providers()
            if dev in ("dml", "cuda"):
                cfg.append_provider(dev)
            model = og.Model(cfg)
            return model, og.Tokenizer(model)

        try:
            model, tok = build(device)
        except Exception as exc:  # noqa: BLE001
            if device == "cpu":
                raise ProviderError(ErrorCode.NOT_CONFIGURED, f"the local model could not be loaded: {exc}", provider_kind="local")
            # No usable GPU path (driver, runtime build): the same int4 build also runs on the CPU, just slower.
            log.warning("local model: %s unavailable (%s); running on the CPU", device, exc)
            try:
                model, tok = build("cpu")
            except Exception as exc2:  # noqa: BLE001
                raise ProviderError(ErrorCode.NOT_CONFIGURED, f"the local model could not be loaded on {device} ({exc}) "
                                    f"or on the CPU ({exc2})", provider_kind="local")
        try:
            with open(os.path.join(path, "genai_config.json"), encoding="utf-8") as fh:
                ctx = int(json.load(fh)["model"].get("context_length") or 4096)
        except (OSError, ValueError, KeyError):
            ctx = 4096
        _MODELS[key] = (model, tok, threading.Lock(), ctx)
        return _MODELS[key]


def _config(og, path: str):
    """og.Config, dropping config keys this runtime version does not know (newer model builds add some)."""
    import re
    for _ in range(8):
        try:
            return og.Config(path)
        except Exception as exc:  # noqa: BLE001
            m = re.search(r'Unknown value "([A-Za-z0-9_]+)"', str(exc))
            if not m:
                raise
            cfg_path = os.path.join(path, "genai_config.json")
            with open(cfg_path, encoding="utf-8") as fh:
                data = json.load(fh)

            def strip(node) -> bool:
                if isinstance(node, dict):
                    if m.group(1) in node:
                        node.pop(m.group(1))
                        return True
                    return any(strip(v) for v in node.values())
                return False

            if not strip(data):
                raise
            backup = cfg_path + ".orig"
            if not os.path.exists(backup):
                os.replace(cfg_path, backup)
            with open(cfg_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
    return og.Config(path)


def chatml(system: str, user: str) -> str:
    """Qwen2.5 chat format."""
    return (f"<|im_start|>system\n{system}<|im_end|>\n<|im_start|>user\n{user}<|im_end|>\n"
            f"<|im_start|>assistant\n")


class LocalOnnxProvider(LLMProvider):
    kind = "local"
    RETRY_DELAYS_S = ()        # a local failure will not fix itself by retrying
    FALLBACK_MODELS = ()

    def __init__(self, config, models_dir: str):
        super().__init__(config, None)
        self.path = config.extra.get("path") or os.path.join(models_dir, config.model)
        default = "dml" if "gpu" in config.model else "cpu"
        self.device = config.extra.get("device") or _device_of(self.path, default)

    def capabilities(self) -> ProviderCapabilities:
        meta = PROVIDER_KINDS["local"]
        return ProviderCapabilities(structured_modes=list(meta["structured_modes"]), default_structured_mode=meta["default_mode"],
                                    supports_streaming=False, supports_json_schema=True, notes=meta["notes"])

    def _call(self, system, user, *, schema, schema_name, mode, max_output_tokens) -> LLMResult:
        import onnxruntime_genai as og  # noqa: F401 - load_model reports a missing runtime clearly
        model, tok, lock, ctx = load_model(self.path, self.device)
        sys_prompt = system + ("\n\n" + schema_instructions(schema) if schema is not None else "")
        max_out = max(64, min(int(max_output_tokens or 1024), 2048))
        ids = tok.encode(chatml(sys_prompt, user))
        budget = min(MAX_PROMPT_TOKENS, ctx - max_out - 8)
        if len(ids) > budget:  # keep the start and the end of the user text
            keep = max(200, int(len(user) * budget / len(ids)) - 200)
            user = user[: keep // 2] + "\n…\n" + user[-keep // 2:]
            ids = tok.encode(chatml(sys_prompt, user))
        started = time.monotonic()
        deadline = started + float(self.config.timeout_s or 180)
        with lock:
            try:
                params = og.GeneratorParams(model)
                opts = {"max_length": len(ids) + max_out, "do_sample": False}
                params.set_search_options(**opts)
                if schema is not None and mode != "prompt":
                    try:  # compact JSON: a small model cannot then pad with endless whitespace
                        params.set_guidance("json_schema", json.dumps({**schema, "x-guidance": {"whitespace_flexible": False}}))
                    except Exception:  # noqa: BLE001 - older runtimes: prompt-only JSON
                        pass
                gen = og.Generator(model, params)
                gen.append_tokens(ids)
                n = 0
                while not gen.is_done():
                    if time.monotonic() > deadline:
                        raise ProviderError(ErrorCode.TIMEOUT, f"the local model did not finish within "
                                            f"{int(self.config.timeout_s or 180)} s", provider_kind=self.kind)
                    gen.generate_next_token()
                    n += 1
                out = tok.decode(gen.get_sequence(0)[len(ids):])
            except ProviderError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise ProviderError(ErrorCode.SERVER_ERROR, f"local generation failed: {exc}", provider_kind=self.kind)
        finish = "length" if n >= max_out else "stop"
        if schema is not None and finish == "length":
            raise ProviderError(ErrorCode.OUTPUT_TRUNCATED, f"the local model did not finish its JSON within {max_out} tokens "
                                f"(it began: {out.strip()[:60]!r})", provider_kind=self.kind)
        return LLMResult(text=out.strip(), usage=Usage(input_tokens=len(ids), output_tokens=n),
                         latency_ms=int((time.monotonic() - started) * 1000), model=self.config.model,
                         provider_kind=self.kind, finish_reason=finish)
