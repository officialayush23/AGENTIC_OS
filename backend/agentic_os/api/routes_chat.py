"""Conversations, voice, knowledge graph and the dashboard."""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, FastAPI, Request
from pydantic import BaseModel, Field

from ..providers.base import ProviderError
from ..util import json_loads


class ChatIn(BaseModel):
    title: Optional[str] = Field(None, max_length=120)


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    voice: bool = False


class GraphBuildIn(BaseModel):
    deep: bool = False


def register(api: APIRouter, app: FastAPI, st, ApiError) -> None:
    # ----------------------------------------------------------- conversations
    @api.get("/chats")
    async def chats():
        return {"chats": st.chat.list()}

    @api.post("/chats")
    async def new_chat(body: ChatIn):
        return st.chat.create(body.title or "New conversation")

    @api.get("/chats/{cid}")
    async def chat(cid: str):
        c = st.chat.get(cid)
        if c is None:
            raise ApiError(404, "NO_SUCH_CHAT", "Unknown conversation.")
        return c

    @api.put("/chats/{cid}")
    async def rename_chat(cid: str, body: ChatIn):
        st.chat.rename(cid, body.title or "Conversation")
        return st.chat.get(cid)

    @api.delete("/chats/{cid}")
    async def delete_chat(cid: str):
        return {"deleted": st.chat.delete(cid)}

    @api.post("/chats/{cid}/messages")
    async def post_message(cid: str, body: MessageIn):
        try:
            return await asyncio.to_thread(st.chat.post, cid, body.text, voice=body.voice)
        except KeyError:
            raise ApiError(404, "NO_SUCH_CHAT", "Unknown conversation.")

    # ------------------------------------------------------------------- voice
    @api.post("/voice/transcribe")
    async def transcribe(request: Request):
        audio = await request.body()
        if not audio:
            raise ApiError(400, "NO_AUDIO", "No audio was sent.")
        if len(audio) > 20_000_000:
            raise ApiError(413, "AUDIO_TOO_LARGE", "Keep voice messages under about 5 minutes.")
        provider = st.llm_provider()
        if provider is None:
            raise ApiError(409, "NO_PROVIDER", "Speech recognition needs an AI provider (Gemini or OpenAI) in Settings.")
        try:
            text = await asyncio.to_thread(provider.transcribe, audio, request.headers.get("content-type", "audio/webm"))
        except ProviderError as exc:
            raise ApiError(502, exc.code, exc.message)
        return {"text": text, "provider": provider.kind}

    # ---------------------------------------------------------- knowledge graph
    @api.get("/kg")
    async def kg(limit: int = 250, types: Optional[str] = None, focus: Optional[str] = None):
        g = st.kg.graph(limit=max(10, min(limit, 1500)), types=types.split(",") if types else None, focus=focus)
        return {**g, "stats": st.kg.stats(), "job": st.kg_job.public()}

    @api.get("/kg/node")
    async def kg_node(id: str):  # noqa: A002
        n = st.kg.node(id)
        if n is None:
            raise ApiError(404, "NO_SUCH_NODE", "Not in the graph.")
        return n

    @api.get("/kg/search")
    async def kg_search(q: str):
        return {"matches": st.kg.search(q, 20)}

    @api.post("/kg/build")
    async def kg_build(body: GraphBuildIn):
        if body.deep and st.llm_provider() is None:
            raise ApiError(409, "NO_PROVIDER", "Deep extraction needs an AI provider (Settings → AI providers).")
        return st.start_graph(deep=body.deep)

    # --------------------------------------------------------------- dashboard
    @api.get("/dashboard")
    async def dashboard(days: int = 14):
        days = max(7, min(days, 60))
        rows = st.db.query("SELECT pid, ppid, state, pcb_json, created_at FROM kernel_processes WHERE ppid IS NULL "
                           "ORDER BY created_at DESC LIMIT 2000")
        today = datetime.now(timezone.utc).date()
        by_day = {(today - timedelta(days=i)).isoformat(): Counter() for i in range(days - 1, -1, -1)}
        tools: Counter = Counter()
        origins: Counter = Counter()
        outcomes: Counter = Counter()
        approvals = 0
        commits = 0
        recent: List[dict] = []
        for r in rows:
            pcb = json_loads(r["pcb_json"], {})
            code = (pcb.get("exit") or {}).get("code") or ("waiting" if pcb.get("pending_user") else "running")
            outcomes[code] += 1
            day = (r["created_at"] or "")[:10]
            if day in by_day:
                by_day[day][{"completed": "done", "failed": "failed", "budget_exceeded": "failed"}.get(code, "other")] += 1
            for s in (pcb.get("plan") or {}).get("steps") or []:
                tools[s.get("tool")] += 1
            origins[(pcb.get("origin") or {}).get("kind", "user")] += 1
            if pcb.get("txn_id"):
                commits += 1
            if (pcb.get("pending_user") or {}).get("kind") in ("approval", "review", "clarify"):
                approvals += 1
            if len(recent) < 8:
                recent.append({"pid": r["pid"], "title": pcb.get("title"), "code": code, "created_at": r["created_at"],
                               "origin": (pcb.get("origin") or {}).get("kind", "user")})
        total = sum(outcomes.values())
        done = outcomes.get("completed", 0)
        finished = sum(v for k, v in outcomes.items() if k not in ("running", "waiting"))
        runs = st.automation.runs(None, 500)
        auto_days = Counter(datetime.fromtimestamp(x["fired_at"]).date().isoformat() for x in runs)
        return {
            "kpis": {"tasks": total, "success_rate": round(100 * done / finished) if finished else None,
                     "waiting": approvals, "commits": commits,
                     "automations": len([s for s in st.automation.list_schedules() if s["enabled"]]) +
                                    len([l for l in st.automation.list_listeners() if l["enabled"]]),
                     "automation_runs": len(runs)},
            "per_day": [{"day": d, "done": c["done"], "failed": c["failed"], "other": c["other"],
                         "automations": auto_days.get(d, 0)} for d, c in by_day.items()],
            "tools": [{"tool": t, "count": n} for t, n in tools.most_common(10) if t],
            "origins": dict(origins), "outcomes": dict(outcomes), "recent": recent,
            "knowledge": {**st.kb.status(), "graph": st.kg.stats()},
            "connections": {"google": st.google.auth.status()["connected"], "notion": st.notion.status()["connected"],
                            "mail": st.mail.status()["mode"]},
        }
