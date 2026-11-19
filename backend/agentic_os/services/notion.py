"""Notion through an internal integration token (Settings → Connections → Notion).

The user creates an integration at notion.so/my-integrations, pastes its secret, and shares the pages the
agent may use with that integration (page menu → Connections). The token lives in the credential store.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

from .http import HttpClient, HttpError

API = "https://api.notion.com/v1"
VERSION = "2026-03-11"
SECRET = "notion:token"
ID_RE = re.compile(r"([0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)


class NotionError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def page_id(value: str) -> str:
    """Accept a page id or a notion.so URL."""
    v = (value or "").strip()
    if "notion.so" in v or "notion.site" in v:
        from urllib.parse import urlsplit
        last = urlsplit(v).path.rstrip("/").split("/")[-1]
        v = last.split("-")[-1]
    m = ID_RE.findall(v)
    if not m:
        raise NotionError("NOTION_BAD_ID", "Give a Notion page link or id.")
    raw = m[-1].replace("-", "")
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def _rt(text: str) -> List[dict]:
    return [{"type": "text", "text": {"content": text[i:i + 1900]}} for i in range(0, max(len(text), 1), 1900)] if text else []


def markdown_blocks(md: str) -> List[dict]:
    blocks: List[dict] = []
    code: Optional[List[str]] = None
    for line in (md or "").replace("\r\n", "\n").split("\n"):
        if line.strip().startswith("```"):
            if code is None:
                code = []
            else:
                blocks.append({"type": "code", "code": {"rich_text": _rt("\n".join(code)), "language": "plain text"}})
                code = None
            continue
        if code is not None:
            code.append(line)
            continue
        s = line.rstrip()
        if not s.strip():
            continue
        for prefix, kind in (("### ", "heading_3"), ("## ", "heading_2"), ("# ", "heading_1")):
            if s.startswith(prefix):
                blocks.append({"type": kind, kind: {"rich_text": _rt(s[len(prefix):])}})
                break
        else:
            m = re.match(r"^\s*[-*] \[( |x|X)\] (.*)$", s)
            if m:
                blocks.append({"type": "to_do", "to_do": {"rich_text": _rt(m.group(2)), "checked": m.group(1) != " "}})
            elif re.match(r"^\s*[-*] ", s):
                blocks.append({"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _rt(re.sub(r"^\s*[-*] ", "", s))}})
            elif re.match(r"^\s*\d+[.)] ", s):
                blocks.append({"type": "numbered_list_item", "numbered_list_item": {"rich_text": _rt(re.sub(r"^\s*\d+[.)] ", "", s))}})
            elif s.startswith("> "):
                blocks.append({"type": "quote", "quote": {"rich_text": _rt(s[2:])}})
            else:
                blocks.append({"type": "paragraph", "paragraph": {"rich_text": _rt(s)}})
    if code is not None:
        blocks.append({"type": "code", "code": {"rich_text": _rt("\n".join(code)), "language": "plain text"}})
    return blocks


def _plain(rich: List[dict]) -> str:
    return "".join(r.get("plain_text") or (r.get("text") or {}).get("content", "") for r in rich or [])


def block_text(b: dict) -> str:
    t = b.get("type")
    body = b.get(t) or {}
    text = _plain(body.get("rich_text") or [])
    return {"heading_1": f"# {text}", "heading_2": f"## {text}", "heading_3": f"### {text}",
            "bulleted_list_item": f"- {text}", "numbered_list_item": f"1. {text}",
            "to_do": f"- [{'x' if body.get('checked') else ' '}] {text}", "quote": f"> {text}",
            "code": f"```\n{text}\n```", "child_page": f"[page] {body.get('title', '')}",
            "divider": "---"}.get(t, text)


def page_title(page: dict) -> str:
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return _plain(prop.get("title") or []) or "Untitled"
    return "Untitled"


class NotionClient:
    def __init__(self, secrets_store, settings_fn: Callable[[], dict], http: Optional[HttpClient] = None):
        self.secrets = secrets_store
        self.settings_fn = settings_fn
        self.http = http or HttpClient()

    def token(self) -> Optional[str]:
        try:
            return self.secrets.get(SECRET)
        except Exception:  # noqa: BLE001
            return None

    def cfg(self) -> dict:
        return dict((self.settings_fn() or {}).get("notion") or {})

    def status(self) -> dict:
        c = self.cfg()
        return {"connected": bool(self.token()), "workspace": c.get("workspace"), "bot": c.get("bot"),
                "default_parent": c.get("default_parent"), "default_parent_title": c.get("default_parent_title")}

    def _call(self, method: str, path: str, **kw) -> Any:
        tok = self.token()
        if not tok:
            raise NotionError("NOTION_NOT_CONNECTED", "Connect Notion in Connections first.")
        try:
            return self.http.request(method, f"{API}{path}", token=tok, headers={"Notion-Version": VERSION}, **kw)
        except HttpError as exc:
            if exc.status == 401:
                raise NotionError("NOTION_AUTH", "Notion rejected the integration token. Paste it again.") from None
            if exc.status == 404:
                raise NotionError("NOTION_NOT_SHARED", "Notion can't find that page. Share it with your integration "
                                  "(page ··· menu → Connections).") from None
            if exc.status == 429:
                raise NotionError("NOTION_RATE_LIMITED", "Notion is rate limiting; try again shortly.", retryable=True) from None
            raise NotionError("NOTION_API_ERROR", exc.message, retryable=exc.status >= 500 or exc.status == 0) from None

    def me(self) -> dict:
        return self._call("GET", "/users/me")

    def search(self, query: str, limit: int = 10) -> List[dict]:
        res = self._call("POST", "/search", json_body={"query": query, "page_size": min(limit, 50),
                                                       "filter": {"property": "object", "value": "page"}})
        return [{"id": p["id"], "title": page_title(p), "url": p.get("url"),
                 "last_edited": p.get("last_edited_time")} for p in res.get("results") or []]

    def read_page(self, pid: str, max_blocks: int = 400) -> dict:
        pid = page_id(pid)
        page = self._call("GET", f"/pages/{pid}")
        lines, cursor, n = [], None, 0
        while n < max_blocks:
            res = self._call("GET", f"/blocks/{pid}/children", params={"page_size": 100, "start_cursor": cursor})
            for b in res.get("results") or []:
                lines.append(block_text(b))
                n += 1
            if not res.get("has_more"):
                break
            cursor = res.get("next_cursor")
        return {"id": pid, "title": page_title(page), "url": page.get("url"), "text": "\n".join(l for l in lines if l),
                "truncated": n >= max_blocks}

    def create_page(self, parent: str, title: str, markdown: str) -> dict:
        blocks = markdown_blocks(markdown)
        res = self._call("POST", "/pages", json_body={
            "parent": {"page_id": page_id(parent)},
            "properties": {"title": {"title": [{"type": "text", "text": {"content": title[:1900]}}]}},
            "children": blocks[:100]})
        for i in range(100, len(blocks), 100):
            self._call("PATCH", f"/blocks/{res['id']}/children", json_body={"children": blocks[i:i + 100]})
        return {"id": res["id"], "url": res.get("url"), "title": title, "blocks": len(blocks)}

    def append(self, pid: str, markdown: str) -> dict:
        pid = page_id(pid)
        blocks = markdown_blocks(markdown)
        for i in range(0, len(blocks), 100):
            self._call("PATCH", f"/blocks/{pid}/children", json_body={"children": blocks[i:i + 100]})
        return {"id": pid, "blocks": len(blocks)}
