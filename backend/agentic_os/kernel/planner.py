"""Planners and deterministic plan validation.

A planner turns a request into a structured plan of registered tool calls. It
runs on the I/O driver (it may call an LLM). Validation runs in the kernel and
is deterministic: the model can propose a plan, never force one.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

PLAN_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "needs_clarification": {"type": "boolean"},
        "clarification_question": {"type": ["string", "null"]},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "tool": {"type": "string"},
                    "args": {"type": "object"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "expected_outcome": {"type": "string"},
                    "risk": {"type": ["string", "null"]},
                    "requires_approval": {"type": ["boolean", "null"]},
                },
                "required": ["id", "tool", "args"],
            },
        },
        "completion_criteria": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["goal", "steps"],
}

SYSTEM_PROMPT = (
    "You are the planner of Agentic OS, a desktop agent kernel. Turn the user's request into a short plan of "
    "tool calls using ONLY the tools listed. Every write is staged in a transaction the user reviews before "
    "anything changes on disk, so do not ask for confirmation steps. Never invent tools. Use workspace-relative "
    "paths. If the request is ambiguous, set needs_clarification=true and ask one question. Respond with JSON only."
)

_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,48}$")


class Planner:
    kind = "base"

    def plan(self, request: str, catalog: List[dict], *, feedback: Optional[str] = None,
             completed: Optional[List[dict]] = None, clarification: Optional[dict] = None) -> dict:
        raise NotImplementedError


class StaticPlanner(Planner):
    """Returns a fixed plan, or calls `fn(request, feedback, completed)`. Used for tests and demos."""

    kind = "static"

    def __init__(self, plan: Optional[dict] = None, fn: Optional[Callable[..., dict]] = None):
        self._plan = plan
        self._fn = fn

    def plan(self, request, catalog, *, feedback=None, completed=None, clarification=None) -> dict:
        data = self._fn(request, feedback, completed, clarification) if self._fn else self._plan
        return {"plan": data, "usage": {"llm_calls": 0, "tokens": 0}}


class LLMPlanner(Planner):
    kind = "llm"

    def __init__(self, provider_factory: Callable[[], Any]):
        self.provider_factory = provider_factory

    def plan(self, request, catalog, *, feedback=None, completed=None, clarification=None) -> dict:
        import json

        provider = self.provider_factory()
        parts = [f"User request:\n{request}", "Available tools:\n" + json.dumps(catalog, ensure_ascii=False)]
        if clarification:
            parts.append(f"Clarification. Question: {clarification.get('question')} Answer: {clarification.get('answer')}")
        if completed:
            parts.append("Steps already completed (do not repeat them):\n" + json.dumps(completed, ensure_ascii=False)[:8000])
        if feedback:
            parts.append(f"The previous plan failed. Fix this problem:\n{feedback}")
        result = provider.generate_structured(SYSTEM_PROMPT, "\n\n".join(parts), PLAN_SCHEMA, "plan")
        usage = getattr(result, "usage", None)
        tokens = int(getattr(usage, "total_tokens", 0) or 0) if usage is not None else 0
        return {"plan": result.data, "usage": {"llm_calls": 1, "tokens": tokens}}


class RefError(Exception):
    pass


def iter_refs(value: Any):
    """Yield every `{"$ref": "<step>.output[.field...]"}` string inside an args structure."""
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str) and len(value) == 1:
            yield ref
            return
        for v in value.values():
            yield from iter_refs(v)
    elif isinstance(value, list):
        for v in value:
            yield from iter_refs(v)


def ref_step(ref: str) -> str:
    return ref.split(".", 1)[0]


def resolve_refs(value: Any, lookup: Callable[[str], Optional[dict]]) -> Any:
    """Replace `$ref`s with outputs of completed steps. `lookup(step_id)` returns that step's output."""
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str) and len(value) == 1:
            parts = ref.split(".")
            if len(parts) < 2 or parts[1] != "output":
                raise RefError(f"bad reference {ref!r}; use '<step_id>.output' or '<step_id>.output.<field>'")
            node: Any = lookup(parts[0])
            if node is None:
                raise RefError(f"reference {ref!r} points to a step without output")
            for key in parts[2:]:
                if isinstance(node, dict) and key in node:
                    node = node[key]
                elif isinstance(node, list) and key.isdigit() and int(key) < len(node):
                    node = node[int(key)]
                else:
                    raise RefError(f"reference {ref!r}: no field {key!r}")
            return node
        return {k: resolve_refs(v, lookup) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_refs(v, lookup) for v in value]
    return value


def validate_plan(plan: Any, registry, *, max_steps: int, completed_ids: Optional[set] = None) -> List[str]:
    """Deterministic structural validation. Returns a list of problems (empty = valid)."""
    errors: List[str] = []
    if not isinstance(plan, dict):
        return ["plan must be a JSON object"]
    if plan.get("needs_clarification"):
        if not str(plan.get("clarification_question") or "").strip():
            errors.append("needs_clarification is set but no question was given")
        return errors
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        return ["plan has no steps"]
    if len(steps) > max_steps:
        errors.append(f"plan has {len(steps)} steps; the limit is {max_steps}")
    seen = set(completed_ids or ())
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"step {i + 1} is not an object")
            continue
        sid = str(step.get("id") or "")
        if not _ID_RE.match(sid):
            errors.append(f"step {i + 1} has an invalid id {sid!r}")
        elif sid in seen and sid not in (completed_ids or ()):
            errors.append(f"duplicate step id {sid!r}")
        tool = str(step.get("tool") or "")
        spec = registry.get(tool)
        if spec is None:
            hint = registry.unsupported_hint(tool)
            errors.append(f"step {sid}: unknown tool {tool!r}" + (f" ({hint})" if hint else ""))
        else:
            args = step.get("args") or {}
            refs = list(iter_refs(args))
            for ref in refs:
                if ref_step(ref) not in seen:
                    errors.append(f"step {sid}: reference {ref!r} must point to an earlier step")
            if not refs:   # args with references are validated after they are resolved
                try:
                    registry.validate_args(tool, args)
                except Exception as exc:  # ArgsValidationError
                    errors.append(f"step {sid}: {exc}")
        for dep in step.get("depends_on") or []:
            if dep not in seen:
                errors.append(f"step {sid} depends on {dep!r}, which does not come earlier")
        seen.add(sid)
    return errors


# ---------------------------------------------------------------------------- rules
_PATH = r"([\w./\\-]+\.[A-Za-z0-9]{1,8}|[\w./\\-]+/)"
_Q = r"[\"'“‘`](.+?)[\"'”’`]"


def _clean_path(p: str) -> str:
    return p.replace("\\", "/").strip().strip("/").rstrip(".,;:") or "."


class RulesPlanner(Planner):
    """Deterministic planner for common requests. Works with no API key; never calls a model."""

    kind = "rules"

    HELP = ("I can do these without an AI provider: 'write a project report', 'search for \"text\"', "
            "'find files named *.md', 'read docs/notes.md', 'list files in src', 'what does the code say about X?' "
            "(searches the knowledge base), 'create notes/todo.md with \"...\"', 'append \"...\" to notes.md', "
            "'replace \"a\" with \"b\" in file.txt'. Add an AI provider in Settings for free-form requests.")

    def match(self, request: str) -> Optional[dict]:
        r = request.strip()
        low = r.lower()
        m = re.search(rf"replace\s+{_Q}\s+with\s+{_Q}\s+in\s+{_PATH}", r, re.I)
        if m:
            return self._plan("Replace text", [{"id": "edit", "title": f"Edit {_clean_path(m.group(3))}",
                                                "tool": "edit_workspace_file",
                                                "args": {"path": _clean_path(m.group(3)), "operation": "replace_text",
                                                         "find": m.group(1), "replace": m.group(2)}}])
        m = re.search(rf"(append|prepend|add)\s+{_Q}\s+(?:to|at the (?:end|start) of)\s+{_PATH}", r, re.I)
        if m:
            op = "prepend" if m.group(1).lower() == "prepend" else "append"
            text = m.group(2) if m.group(2).endswith("\n") else m.group(2) + "\n"
            return self._plan(f"{op.title()} text", [{"id": "edit", "title": f"{op.title()} to {m.group(3)}",
                                                     "tool": "edit_workspace_file",
                                                     "args": {"path": _clean_path(m.group(3)), "operation": op,
                                                              "text": text}}])
        m = re.search(rf"(?:create|write|make|new)\s+(?:a\s+)?(?:file\s+)?{_PATH}\s+(?:with|containing|saying)\s+{_Q}",
                      r, re.I | re.S)
        if m:
            content = m.group(2) if m.group(2).endswith("\n") else m.group(2) + "\n"
            return self._plan("Create a file", [{"id": "create", "title": f"Create {m.group(1)}",
                                                 "tool": "create_workspace_file",
                                                 "args": {"path": _clean_path(m.group(1)), "content": content}}])
        if re.search(r"\b(report|inventory|overview|summari[sz]e (the )?(project|workspace|repo))\b", low):
            m = re.search(r"\b(?:to|as|into|in)\s+([\w./\\-]+\.md)\b", r)
            target = _clean_path(m.group(1)) if m else "reports/project_report.md"
            return self._plan("Write a project report", [
                {"id": "list", "title": "List files", "tool": "list_workspace_files", "args": {"path": ".", "max_depth": 4}},
                {"id": "config", "title": "Identify config files", "tool": "identify_project_config_files",
                 "args": {"path": "."}},
                {"id": "report", "title": "Compose report", "tool": "build_workspace_report",
                 "args": {"title": "Project Report", "listing": {"$ref": "list.output"},
                          "config": {"$ref": "config.output"}}},
                {"id": "save", "title": f"Save {target}", "tool": "create_workspace_file",
                 "args": {"path": target, "content": {"$ref": "report.output.markdown"}, "overwrite": True}},
            ])
        m = re.search(r"(?:find|list|show)\s+(?:all\s+)?files\s+(?:named|matching|like)\s+([\w.*?\-]+)", r, re.I)
        if m:
            return self._plan("Find files", [{"id": "find", "title": f"Find {m.group(1)}", "tool": "search_workspace_files",
                                              "args": {"name_pattern": m.group(1), "path": "."}}])
        m = re.search(rf"(?:search|grep|look)\s+(?:for\s+)?{_Q}", r, re.I) or \
            re.search(r"(?:search|grep)\s+(?:for\s+)?(\S.{0,80})$", r, re.I)
        if m:
            q = m.group(1).strip()
            return self._plan("Search the workspace", [
                {"id": "grep", "title": f"Search files for '{q}'", "tool": "search_workspace_files",
                 "args": {"query": q[:200], "path": "."}},
                {"id": "kb", "title": "Search the knowledge base", "tool": "search_knowledge", "args": {"query": q[:500]}},
            ])
        m = re.search(r"(?:list|show)\s+(?:the\s+)?(?:files|folders|contents)(?:\s+(?:in|of|under)\s+([\w./\\-]+))?", r, re.I)
        if m:
            return self._plan("List files", [{"id": "list", "title": "List files", "tool": "list_workspace_files",
                                              "args": {"path": _clean_path(m.group(1) or "."), "max_depth": 3}}])
        m = re.search(rf"(?:read|show|open|cat|print|display)\s+(?:the\s+)?(?:file\s+)?{_PATH}", r, re.I)
        if m and "/" not in m.group(1)[-1:]:
            return self._plan("Read a file", [{"id": "read", "title": f"Read {m.group(1)}", "tool": "read_workspace_file",
                                               "args": {"path": _clean_path(m.group(1))}}])
        if re.search(r"\b(system|machine|computer)\s+info", low):
            return self._plan("System information", [{"id": "sys", "tool": "get_system_info", "args": {}}])
        if r.endswith("?") or re.match(r"(what|how|why|where|which|who|when|explain|tell me)\b", low):
            return self._plan("Answer from the knowledge base", [
                {"id": "kb", "title": "Search the knowledge base", "tool": "search_knowledge", "args": {"query": r[:500]}}])
        return None

    @staticmethod
    def _plan(goal: str, steps: List[dict]) -> dict:
        return {"goal": goal, "steps": steps, "needs_clarification": False}

    def plan(self, request, catalog, *, feedback=None, completed=None, clarification=None) -> dict:
        text = request if not clarification else f"{clarification.get('answer')}"
        plan = self.match(text) or self.match(request)
        if plan is None or feedback:
            q = self.HELP if plan is None else f"That didn't work ({feedback[:200]}). {self.HELP}"
            plan = {"goal": request, "steps": [], "needs_clarification": True, "clarification_question": q}
        return {"plan": plan, "usage": {"llm_calls": 0, "tokens": 0}}


class HybridPlanner(Planner):
    """Deterministic rules first; an LLM for everything else (when a provider is configured)."""

    kind = "hybrid"

    def __init__(self, llm_factory: Optional[Callable[[], Optional[Planner]]] = None):
        self.rules = RulesPlanner()
        self.llm_factory = llm_factory or (lambda: None)

    def plan(self, request, catalog, *, feedback=None, completed=None, clarification=None) -> dict:
        if not feedback and not clarification:
            hit = self.rules.match(request)
            if hit is not None:
                return {"plan": hit, "usage": {"llm_calls": 0, "tokens": 0}, "planner": "rules"}
        llm = self.llm_factory()
        if llm is None:
            out = self.rules.plan(request, catalog, feedback=feedback, completed=completed, clarification=clarification)
            out["planner"] = "rules"
            return out
        out = llm.plan(request, catalog, feedback=feedback, completed=completed, clarification=clarification)
        out["planner"] = "llm"
        return out
