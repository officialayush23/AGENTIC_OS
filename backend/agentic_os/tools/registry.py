"""Central tool registry."""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ValidationError

from .base import RISK_DESCRIPTIONS, ToolSpec

# Friendly explanations for capabilities a model may ask for that v0.1 does not provide.
UNSUPPORTED_TOOL_HINTS = {
    "run_shell": "Shell/command execution is not available in Agentic OS v0.1.",
    "run_command": "Shell/command execution is not available in Agentic OS v0.1.",
    "execute_command": "Shell/command execution is not available in Agentic OS v0.1.",
    "shell": "Shell/command execution is not available in Agentic OS v0.1.",
    "powershell": "Shell/command execution is not available in Agentic OS v0.1.",
    "cmd": "Shell/command execution is not available in Agentic OS v0.1.",
    "delete_file": "Deleting files is not supported in v0.1.",
    "delete_workspace_file": "Deleting files is not supported in v0.1.",
    "remove_file": "Deleting files is not supported in v0.1.",
    "move_file": "Moving or renaming files is not supported in v0.1.",
    "rename_file": "Moving or renaming files is not supported in v0.1.",
    "send_email": "Email is a planned integration, not available in v0.1.",
    "http_request": "Network requests are not available in v0.1.",
    "web_search": "Web search is a planned integration, not available in v0.1.",
    "browse": "Browser automation is planned, not available in v0.1.",
    "open_url": "Browser automation is planned, not available in v0.1.",
    "download_file": "Downloads are not available in v0.1.",
    "install_package": "Installing software is not allowed.",
    "elevate": "Administrator elevation is never allowed.",
    "run_as_admin": "Administrator elevation is never allowed.",
}


class ArgsValidationError(Exception):
    def __init__(self, tool: str, errors: List[dict]):
        self.tool = tool
        self.errors = errors
        summary = "; ".join(f"{e['field']}: {e['message']}" for e in errors[:5])
        super().__init__(f"invalid arguments for {tool}: {summary}")


def format_validation_errors(exc: ValidationError) -> List[dict]:
    out = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "__root__") or "(arguments)"
        msg = str(err.get("msg", "invalid value"))
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        out.append({"field": loc, "message": msg[:300], "type": str(err.get("type", ""))})
    return out


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        spec.validate_spec()
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} is already registered")
        self._tools[spec.name] = spec

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return sorted(self._tools)

    def all(self) -> List[ToolSpec]:  # noqa: A003
        return [self._tools[n] for n in self.names()]

    def is_available(self, spec: ToolSpec, platform: str = sys.platform) -> bool:
        plat = "win32" if platform.startswith("win") else ("darwin" if platform == "darwin" else "linux")
        return plat in spec.platforms

    def validate_args(self, name: str, args: Any) -> BaseModel:
        spec = self.get(name)
        if spec is None:
            raise ArgsValidationError(name, [{"field": "tool", "message": "unknown tool", "type": "unknown_tool"}])
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ArgsValidationError(name, [{"field": "(arguments)", "message": "arguments must be an object", "type": "type"}])
        try:
            return spec.input_model.model_validate(args)
        except ValidationError as exc:
            raise ArgsValidationError(name, format_validation_errors(exc)) from None

    @staticmethod
    def unsupported_hint(name: str) -> Optional[str]:
        key = (name or "").strip().lower()
        if key in UNSUPPORTED_TOOL_HINTS:
            return UNSUPPORTED_TOOL_HINTS[key]
        for prefix in ("browser_", "gmail_", "calendar_", "notion_", "email_", "web_"):
            if key.startswith(prefix):
                return "That integration is planned but not available in v0.1."
        return None

    def llm_catalog(self) -> List[dict]:
        """Compact tool descriptions for planning prompts (no handlers, no internals)."""
        catalog = []
        for spec in self.all():
            if not self.is_available(spec):
                continue
            schema = spec.input_schema()
            props = {}
            for key, prop in (schema.get("properties") or {}).items():
                entry = {k: v for k, v in prop.items() if k in ("type", "description", "default", "enum", "minimum",
                                                                    "maximum", "maxLength", "anyOf", "items", "pattern")}
                props[key] = entry
            catalog.append({
                "name": spec.name,
                "description": spec.description,
                "risk": spec.risk.value,
                "mutates": spec.mutates,
                "approval": spec.approval,
                "args": props,
                "required": schema.get("required", []),
                "examples": list(spec.examples),
            })
        return catalog

    def public(self) -> List[dict]:
        out = []
        for spec in self.all():
            item = spec.public()
            item["available"] = self.is_available(spec)
            out.append(item)
        return out

    @staticmethod
    def risk_levels() -> List[Tuple[str, str]]:
        return [(level.value, desc) for level, desc in RISK_DESCRIPTIONS.items()]


def build_default_registry() -> ToolRegistry:
    from .apps import APP_TOOLS
    from .knowledge import KNOWLEDGE_TOOLS
    from .system import SYSTEM_TOOLS
    from .workspace import WORKSPACE_TOOLS

    registry = ToolRegistry()
    for spec in [*WORKSPACE_TOOLS, *APP_TOOLS, *SYSTEM_TOOLS, *KNOWLEDGE_TOOLS]:
        registry.register(spec)
    return registry
