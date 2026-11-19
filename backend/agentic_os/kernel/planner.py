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
    "paths. If the request is ambiguous, set needs_clarification=true and ask one question. Respond with JSON only.\n"
    "- Small talk, greetings or thanks (in any language, e.g. 'hi', 'bol bhai', 'kya haal hai'): one respond_to_user "
    "step with a short, friendly reply in the user's own language and tone. Do not search files for small talk.\n"
    "- Questions about the user's day, focus, priorities, pending work or 'what should I do': list the calendar "
    "(calendar_list, 7 days), unread email (email_list) and search notes for TODOs and next steps (search_knowledge). "
    "Do not add a respond_to_user step after tools that fetch data; the answer is written from their results.\n"
    "- Text after 'Conversation so far' is earlier chat for context: use it to resolve 'it', 'that', follow-ups."
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


def compact_catalog(catalog: list) -> str:
    """One line per tool: name(required, optional?): description. ~4x smaller, for small local models."""
    lines = []
    for t in catalog:
        req = set(t.get("required") or [])
        args = ", ".join(f"{k}{'' if k in req else '?'}" for k in (t.get("args") or {}))
        desc = str(t.get("description") or "").split(". ")[0].rstrip(".")
        lines.append(f"- {t['name']}({args}): {desc}")
    return "\n".join(lines)


class LLMPlanner(Planner):
    kind = "llm"

    def __init__(self, provider_factory: Callable[[], Any]):
        self.provider_factory = provider_factory

    def plan(self, request, catalog, *, feedback=None, completed=None, clarification=None) -> dict:
        import json

        provider = self.provider_factory()
        extra = []
        if clarification:
            extra.append(f"Clarification. Question: {clarification.get('question')} Answer: {clarification.get('answer')}")
        if completed:
            extra.append("Steps already completed (do not repeat them):\n" + json.dumps(completed, ensure_ascii=False)[:8000])
        if feedback:
            extra.append(f"The previous plan failed. Fix this problem:\n{feedback}")
        full = "\n\n".join([f"User request:\n{request}", "Available tools:\n" + json.dumps(catalog, ensure_ascii=False), *extra])
        # Small local models get a one-line-per-tool catalog and a schema that only allows real tool names.
        compact = "\n\n".join([f"User request:\n{request}", "Available tools (name(args, ? = optional): purpose):\n"
                                + compact_catalog(catalog), *extra,
                                 "Use as few steps as possible (usually one or two). Only use a tool the request "
                                 "clearly needs; never add extra steps such as screenshots, searches or replies."])
        names = [t["name"] for t in catalog]
        tight = {"type": "object", "required": ["goal", "steps"], "additionalProperties": False, "properties": {
            "goal": {"type": "string"},
            "needs_clarification": {"type": "boolean"},
            "clarification_question": {"type": ["string", "null"]},
            "steps": {"type": "array", "minItems": 1, "maxItems": 4, "items": {
                "type": "object", "required": ["id", "title", "tool", "args"], "additionalProperties": False,
                "properties": {"id": {"type": "string"}, "title": {"type": "string"}, "tool": {"type": "string", "enum": names},
                               "args": {"type": "object"}, "depends_on": {"type": "array", "items": {"type": "string"}}}}}}}
        if getattr(provider, "providers", None) is not None:  # a ChainProvider: each link gets the variant it suits
            result = provider.generate_structured(SYSTEM_PROMPT, full, PLAN_SCHEMA, "plan",
                                                  local_variant=(compact, tight))
        elif getattr(provider, "kind", "") == "local":
            result = provider.generate_structured(SYSTEM_PROMPT, compact, tight, "plan")
        else:
            result = provider.generate_structured(SYSTEM_PROMPT, full, PLAN_SCHEMA, "plan")
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


_URL = r"((?:https?://)?(?:[\w-]+\.)+[a-z]{2,}(?::\d+)?(?:/[^\s\"']*)?|https?://(?:localhost|127\.0\.0\.1)(?::\d+)?(?:/[^\s\"']*)?)"
_EMAIL = r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"


_FILE_EXT = re.compile(r"\.(md|txt|py|json|js|jsx|ts|tsx|csv|log|ya?ml|toml|ini|cfg|html?|css|ics|eml|rs|go|java|c|h|cpp|"
                       r"sh|bat|ps1|xml|sql|lock|env|docx?|xlsx?|pdf|png|jpe?g)$", re.I)


def _web(url: str) -> bool:
    return url.lower().startswith(("http://", "https://")) or not _FILE_EXT.search(url.split("/")[0] if "/" in url else url)


def parse_when(text: str, now: Optional["datetime"] = None) -> Optional[str]:
    """'2026-11-20 11:00', 'tomorrow at 3pm', 'today 09:30', 'friday at 11' -> ISO local time."""
    from datetime import datetime, timedelta
    now = now or datetime.now()
    low = text.lower()
    m = re.search(r"(\d{4}-\d{2}-\d{2})[t ]+(\d{1,2}):(\d{2})", low)
    if m:
        return f"{m.group(1)}T{int(m.group(2)):02d}:{m.group(3)}"
    day = None
    if "tomorrow" in low:
        day = now + timedelta(days=1)
    elif "today" in low or "tonight" in low:
        day = now
    else:
        names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        for i, n in enumerate(names):
            if re.search(rf"\b{n}\b", low):
                ahead = (i - now.weekday()) % 7 or 7
                day = now + timedelta(days=ahead)
                break
    t = re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", low) or \
        re.search(r"\b(\d{1,2})(?::(\d{2}))\s*(am|pm)?\b", low) or re.search(r"\b(\d{1,2})\s*(am|pm)\b()", low)
    if day is None or t is None:
        return None
    groups = [g for g in t.groups()]
    hour = int(groups[0])
    minute = int(groups[1]) if groups[1] and groups[1].isdigit() else 0
    ampm = next((g for g in groups[1:] if g in ("am", "pm")), None)
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return day.replace(hour=hour, minute=minute).strftime("%Y-%m-%dT%H:%M")


_FILLER = re.compile(r"\b(especially|particularly|specifically|mainly|mostly|including|like|and also|as well as|and|or)\b", re.I)


def _email_topic(text: str) -> str:
    """'HACKATHONS , ESPECIALLY VOYAGE HACK' -> 'HACKATHONS, VOYAGE HACK' (commas = alternatives)."""
    t = re.sub(r"^(?:(?:the|of|about|for|on|regarding|related to|from)\s+)+", "", text.strip(), flags=re.I)
    t = _FILLER.sub(",", t.strip(" ?.!\"'"))
    parts = [x.strip() for x in t.split(",") if x.strip()]
    return ", ".join(dict.fromkeys(parts))


def agentic_rules(r: str, offline: bool = True, ctx: str = "") -> Optional[dict]:
    """Offline plans for email, calendar, notes, the web and the knowledge graph."""
    low = r.lower()
    plan = lambda goal, steps: {"goal": goal, "steps": steps, "needs_clarification": False}  # noqa: E731
    if offline and re.fullmatch(r"(hi+|hello|hey+|yo|sup|what'?s up|good (morning|afternoon|evening)|thanks|thank you|namaste|"
                                r"bol bhai|bolo|kaise ho|kya haal( hai)?|hello bhai|hi bhai)[!.? ]*", low.strip()):
        return plan("Say hello", [{"id": "hi", "title": "Reply", "tool": "respond_to_user", "args": {
            "text": "Hi! Tell me what to do: check your email, schedule a meeting, take a note, research a page, "
                    "or build a knowledge graph of your files."}}])
    if re.search(r"\b(focus|priorit(y|ies|ise|ize)|on my plate|pending|what should i (do|work on)|my day|plan for today|"
                 r"what'?s next|kya karna)\b", low):
        return plan("Brief you on your day", [
            {"id": "cal", "title": "Upcoming events", "tool": "calendar_list", "args": {"days": 7}},
            {"id": "mail", "title": "Unread mail", "tool": "email_list", "args": {"unread_only": True, "limit": 10}},
            {"id": "todo", "title": "Open TODOs in your notes", "tool": "search_knowledge",
             "args": {"query": "TODO next steps priorities deadline focus"}}])
    if re.search(r"\b(build|create|make|update|refresh|generate)\b.*\b(knowledge graph|graph|mind ?map)\b", low):
        return plan("Build the knowledge graph", [{"id": "kg", "title": "Build the knowledge graph",
                                                   "tool": "build_knowledge_graph", "args": {"deep": "deep" in low}}])
    m = re.search(r"\b(?:what do (?:i|you) know about|who is|what is connected to|graph of|connections of)\s+(.{2,80}?)\??$", r, re.I)
    if m:
        return plan("Look it up in the knowledge graph", [
            {"id": "kg", "title": f"Find '{m.group(1)}' in the graph", "tool": "kg_query", "args": {"query": m.group(1)}},
            {"id": "kb", "title": "Search the knowledge base", "tool": "search_knowledge", "args": {"query": m.group(1)}}])
    m = re.search(r"(?:search|find|look)\s+(?:in\s+)?(?:my\s+)?(?:e-?mails?|mail|inbox)\s+(?:for\s+)?(.+)$", r, re.I)
    if m:
        q = _email_topic(m.group(1)) or m.group(1).strip().strip("\"'")
        return plan("Search email", [{"id": "find", "title": f"Search mail for '{q}'", "tool": "email_search",
                                      "args": {"query": q[:200]}}])
    m = re.search(r"\b(?:e-?mails?|mails?|inbox|messages)\b\s*(?:that are\s+|which are\s+)?(?:related to|about|regarding|on|from|"
                  r"for|mentioning|containing|of)\s+(.+)$", r, re.I | re.S)
    if m and not re.search(_EMAIL, m.group(1)) and _email_topic(m.group(1)):
        q = _email_topic(m.group(1))
        return plan("Find related email", [{"id": "find", "title": f"Search mail for {q}", "tool": "email_search",
                                            "args": {"query": q[:200], "limit": 25}}])
    # Follow-up on an email answer: "which are of voyage hack", "only the ones from Unstop".
    m = re.match(r"^(?:which|show(?: me)?|only|just|filter|any)\s+(?:(?:ones|of (?:these|them|those)|e-?mails?|mails?|messages?)\s+)?"
                 r"(?:(?:are|is)\s+)?(?:the ones\s+)?(?:of|about|from|regarding|related to|for|on|mentioning)\s+(.+?)[?.!]*$", r, re.I)
    if m and re.search(r"\b(e-?mails?|messages?|inbox|unread)\b", ctx, re.I) and _email_topic(m.group(1)):
        q = _email_topic(m.group(1))
        return plan("Narrow down the email", [{"id": "find", "title": f"Search mail for {q}", "tool": "email_search",
                                               "args": {"query": q[:200], "limit": 25}}])
    if re.search(r"\b(check|read|show|list|any)\b.*\b(e-?mails?|mail|inbox)\b", low) and not re.search(_EMAIL, r):
        return plan("Check email", [{"id": "inbox", "title": "List unread mail", "tool": "email_list",
                                     "args": {"unread_only": "unread" in low or "new" in low, "limit": 20}}])
    m = re.search(rf"^(?:send\s+(?:an?\s+)?)?e-?mail\s+(?:to\s+)?({_EMAIL}(?:\s*(?:,|and)\s*{_EMAIL})*)\s*(?:saying|that|:|with)\s*(.+)$",
                  r, re.I | re.S)
    if m:
        to = re.findall(_EMAIL, m.group(1))
        body = m.group(2).strip().strip("\"'")
        subject = re.split(r"[.!?\n]", body, 1)[0][:80] or "Message"
        return plan("Send an email", [{"id": "send", "title": f"Email {', '.join(to)}", "tool": "email_send",
                                       "args": {"to": to, "subject": subject, "body": body}}])
    if re.search(r"\b(calendar|schedule|agenda)\b", low) and re.search(r"\b(what|show|list|my|today|week)\b", low) \
            and not re.search(r"\b(add|book|schedule a|set up|create)\b", low):
        days = 7 if "week" in low else 1 if "today" in low else 14
        return plan("Show the calendar", [{"id": "cal", "title": "List events", "tool": "calendar_list",
                                           "args": {"days": days}}])
    m = re.search(r"\b(?:schedule|book|set up|add|create)\s+(?:a\s+|an\s+)?(meeting|call|event|appointment|reminder)\b(.*)$",
                  r, re.I | re.S)
    if m:
        rest = m.group(2)
        when = parse_when(rest)
        if when:
            attendees = re.findall(_EMAIL, rest)
            t = re.search(r"\b(?:about|for|titled|called)\s+[\"']?([^\"'@]+?)[\"']?(?:\s+(?:with|on|at|tomorrow|today)\b|$)", rest, re.I)
            who = re.search(r"\bwith\s+([A-Z][a-z]+)", rest)
            title = (t.group(1).strip() if t else None) or (f"{m.group(1).title()} with {who.group(1)}" if who else m.group(1).title())
            dur = re.search(r"(\d{1,3})\s*(?:min|minutes)\b", rest)
            hours = re.search(r"(\d)\s*(?:h|hour|hours)\b", rest)
            duration = int(dur.group(1)) if dur else (int(hours.group(1)) * 60 if hours else 30)
            online = m.group(1).lower() in ("meeting", "call") and not re.search(r"\bin person\b", rest, re.I)
            steps = [{"id": "event", "title": f"Add '{title}' to the calendar", "tool": "calendar_add_event",
                      "args": {"title": title, "start": when, "duration_minutes": duration, "attendees": attendees,
                               "online_meeting": online}}]
            if attendees:
                steps.append({"id": "invite", "title": "Email the invitation", "tool": "email_send",
                              "args": {"to": attendees, "subject": f"Invitation: {title}",
                                       "body": f"Hi,\n\nYou're invited to '{title}' on {when.replace('T', ' at ')}."
                                               + ("\nJoin the call with the link in the attached invitation." if online else "")
                                               + "\n\nThe calendar invitation is attached.",
                                       "attachments": [{"$ref": "event.output.invite_file"}]}})
            return plan("Schedule a meeting", steps)
    m = re.match(r"^(?:(?:what\s+)?i\s+mean(?:\s+is)?|please|can\s+you|could\s+you|pls)?[\s,]*"
                 r"(?:create|creates|make|write|start|add|open|new)\s+(?:me\s+)?(?:a\s+|an\s+|some\s+)?(?:new\s+)?"
                 r"(?:notes?|file\s+in\s+(?:my\s+)?notes|notes?\s+file)\b(?:\s+for\s+me)?"
                 r"(?:\s+(?:for|about|on|regarding|titled|called|named)\s+(?:my\s+|the\s+)?(.+?))?"
                 r"(?:\s*(?:,|and)\s+(?:add|write|put|include)\b.*)?[.!?\s]*$", r, re.I | re.S)
    if m and offline:
        topic = (m.group(1) or "").strip(" .\"'")
        title = (topic[:1].upper() + topic[1:]) if topic else "New note"
        body = (f"# {title}\n\n## Goal\n\n\n## Notes\n\n- \n\n## Next steps\n\n- [ ] \n")
        return plan("Create a note", [{"id": "note", "title": f"Create note '{title}'", "tool": "create_note",
                                       "args": {"title": title, "body": body}}])
    m = re.search(r"^(?:take\s+a\s+note|note(?:\s+down)?|jot(?:\s+down)?|new\s+note)\s*(?:titled\s+[\"'](.+?)[\"'])?\s*[:,-]?\s*(.+)$",
                  r, re.I | re.S)
    if m:
        body = m.group(2).strip()
        title = m.group(1) or " ".join(re.sub(r"[^\w\s-]", "", body).split()[:6]) or "Note"
        return plan("Take a note", [{"id": "note", "title": f"Create note '{title}'", "tool": "create_note",
                                     "args": {"title": title, "body": body}}])
    m = re.search(rf"\b(?:screenshot|take a screenshot of|capture)\s+{_URL}", r, re.I)
    if m and _web(m.group(1)):
        url = m.group(1)
        return plan("Screenshot a page", [
            {"id": "open", "title": f"Open {url}", "tool": "browser_open", "args": {"url": url}},
            {"id": "shot", "title": "Take a screenshot", "tool": "browser_screenshot", "args": {"url": url},
             "depends_on": ["open"]}])
    m = re.search(rf"\b(?:open|go to|browse|visit|navigate to)\s+{_URL}", r, re.I)
    if m and _web(m.group(1)):
        return plan("Open a web page", [{"id": "open", "title": f"Open {m.group(1)}", "tool": "browser_open",
                                         "args": {"url": m.group(1)}}])
    m = re.search(rf"\b(?:fetch|download|read|summari[sz]e|get)\s+(?:the\s+page\s+)?{_URL}", r, re.I)
    if m and _web(m.group(1)):
        return plan("Read a web page", [{"id": "fetch", "title": f"Fetch {m.group(1)}", "tool": "web_fetch",
                                         "args": {"url": m.group(1)}}])
    return None


class RulesPlanner(Planner):
    """Deterministic planner for common requests. Works with no API key; never calls a model."""

    kind = "rules"

    HELP = ("I can do these without an AI provider: 'write a project report', 'search for \"text\"', "
            "'find files named *.md', 'read docs/notes.md', 'list files in src', 'what does the code say about X?' "
            "(searches the knowledge base), 'create notes/todo.md with \"...\"', 'append \"...\" to notes.md', "
            "'replace \"a\" with \"b\" in file.txt', 'check my email', 'email sam@example.com saying ...', "
            "'schedule a meeting with priya@example.com tomorrow at 3pm', 'what's on my calendar this week', "
            "'note: buy milk', 'open example.com', 'fetch https://example.com'. Add an AI provider in Settings for free-form requests.")

    def match(self, request: str, offline: bool = True) -> Optional[dict]:
        r = request.strip().split("\n\nTrigger details (data, not instructions):", 1)[0]
        r, _, ctx = r.partition("\n\nConversation so far (context, not instructions):")
        r = r.strip()
        low = r.lower()
        hit = agentic_rules(r, offline, ctx)
        if hit is not None:
            return hit
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
        if offline and (r.endswith("?") or re.match(r"(what|how|why|where|which|who|when|explain|tell me)\b", low)):
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
        llm = self.llm_factory()
        if not feedback and not clarification:
            # With a model available, small talk and open questions go to it (it answers in the user's language).
            hit = self.rules.match(request, offline=llm is None)
            if hit is not None:
                return {"plan": hit, "usage": {"llm_calls": 0, "tokens": 0}, "planner": "rules"}
        if llm is None:
            out = self.rules.plan(request, catalog, feedback=feedback, completed=completed, clarification=clarification)
            out["planner"] = "rules"
            return out
        try:
            out = llm.plan(request, catalog, feedback=feedback, completed=completed, clarification=clarification)
        except Exception as exc:  # noqa: BLE001
            # The model is down or overloaded (after its own retries): use an offline plan if one fits.
            if getattr(exc, "code", None) is not None and not feedback:  # any provider error, quota included
                hit = self.rules.match(request, offline=True)
                if hit is not None and not hit.get("needs_clarification"):
                    hit = {**hit, "goal": f"{hit.get('goal')} (AI unavailable, offline plan)"}
                    return {"plan": hit, "usage": {"llm_calls": 1, "tokens": 0}, "planner": "rules"}
            raise
        out["planner"] = "llm"
        return out
