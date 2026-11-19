"""HTTP API (FastAPI) over the kernel, knowledge base, providers and settings.

All routes live under /api and require the X-Agentic-OS: 1 header plus either a
paired session cookie (browser) or the Bearer token handed to the backend by the
desktop shell (Tauri). See security/local_auth.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__
from ..config import Settings
from ..kernel import KernelError
from ..providers.base import ProviderError
from ..providers.catalog import PROVIDER_KINDS
from ..providers.manager import ProviderConfigError
from ..rag import ANSWER_SYSTEM, EMBEDDER_DEFAULTS, EmbedderError, build_answer_prompt, build_embedder
from ..security.local_auth import SESSION_COOKIE, AuthError, LocalSecurityMiddleware, parse_cookies
from ..security.secrets import SecretStoreError
from ..tools.base import ToolError
from .state import AppState

log = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def _err(status: int, code: str, message: str, details: Optional[dict] = None) -> JSONResponse:
    body: Dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(body, status_code=status)


# ------------------------------------------------------------------- models
class PairIn(BaseModel):
    code: str = Field(min_length=4, max_length=32)


class TaskIn(BaseModel):
    request: str = Field(min_length=1, max_length=8000)
    title: Optional[str] = Field(None, max_length=120)
    plan: Optional[Dict[str, Any]] = None      # optional explicit plan (validated and policy-checked like any plan)


class ApproveIn(BaseModel):
    action_hash: str = Field(min_length=16, max_length=128)
    standing_minutes: Optional[float] = Field(None, gt=0, le=24 * 60)


class RejectIn(BaseModel):
    action_hash: str = Field(min_length=16, max_length=128)
    note: str = Field("", max_length=500)


class AnswerIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class SignalIn(BaseModel):
    signal: Literal["STOP", "CONT", "TERM", "KILL"]


class SettingsIn(BaseModel):
    workspace_root: Optional[str] = Field(None, max_length=1024)
    planner_mode: Optional[Literal["auto", "rules", "llm"]] = None
    require_approval_for_writes: Optional[bool] = None
    allow_workspace_writes: Optional[bool] = None
    allow_app_launch: Optional[bool] = None
    rag_auto_index: Optional[bool] = None
    rag_embedder: Optional[Dict[str, Any]] = None
    browser_headed: Optional[bool] = None
    browser_channel: Optional[Literal["msedge", "chrome", "auto"]] = None
    web_allow_private: Optional[bool] = None


class ProviderIn(BaseModel):
    kind: Optional[str] = None
    display_name: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    timeout_s: Optional[float] = None
    max_output_tokens: Optional[int] = None
    temperature: Optional[float] = None
    structured_mode: Optional[str] = None
    max_calls_per_task: Optional[int] = None
    enabled: Optional[bool] = None


class CredentialIn(BaseModel):
    api_key: str = Field(min_length=1, max_length=4096)


class IndexIn(BaseModel):
    path: str = Field(".", max_length=1024)


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    k: int = Field(8, ge=1, le=30)
    source: Optional[Literal["workspace", "note"]] = None
    path_prefix: Optional[str] = Field(None, max_length=1024)


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    k: int = Field(6, ge=1, le=12)


class NoteIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=200_000)


class AppIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    path: str = Field(min_length=1, max_length=1024)
    args_policy: Literal["none", "workspace_file"] = "none"
    requires_approval: bool = False


# ---------------------------------------------------------------------- app
def create_app(settings: Settings, state: Optional[AppState] = None, *, start_background: bool = True) -> FastAPI:
    st = state or AppState(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_background:
            st.start()
        ready = {"port": settings.port, "version": __version__, "auth": "bearer" if settings.bearer_token else "pairing"}
        print("AGENTIC_OS_READY " + json.dumps(ready), flush=True)
        try:
            yield
        finally:
            st.stop()

    app = FastAPI(title="Agentic OS", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url="/api/openapi.json" if settings.dev_mode else None)
    app.state.aos = st
    # Order matters: CORS is outermost so browser preflights from the desktop webview are answered
    # before the security middleware (which requires the custom header preflights cannot send).
    app.add_middleware(LocalSecurityMiddleware, auth=st.auth, settings=settings)
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.allowed_origins), allow_credentials=True,
                       allow_methods=["GET", "POST", "PUT", "DELETE"],
                       allow_headers=["content-type", "authorization", "x-agentic-os", "x-correlation-id"])

    @app.exception_handler(ApiError)
    async def _api_error(_req, exc: ApiError):
        return _err(exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(KernelError)
    async def _kernel_error(_req, exc: KernelError):
        return _err(404 if exc.code == "NO_SUCH_PROCESS" else 409, exc.code, exc.message)

    @app.exception_handler(ProviderConfigError)
    async def _provider_cfg(_req, exc: ProviderConfigError):
        return _err(400, "PROVIDER_CONFIG_INVALID", exc.message, {"field": exc.field})

    @app.exception_handler(ProviderError)
    async def _provider_err(_req, exc: ProviderError):
        return _err(502, exc.code, exc.message)

    @app.exception_handler(SecretStoreError)
    async def _secret_err(_req, exc: SecretStoreError):
        return _err(503, "CREDENTIAL_STORE_UNAVAILABLE", exc.safe_message)

    @app.exception_handler(ToolError)
    async def _tool_err(_req, exc: ToolError):
        return _err(400, exc.code, exc.message)

    @app.exception_handler(EmbedderError)
    async def _embed_err(_req, exc: EmbedderError):
        return _err(502, exc.code, exc.message)

    @app.exception_handler(AuthError)
    async def _auth_err(_req, exc: AuthError):
        return _err(exc.status, exc.code, exc.message)

    @app.get("/health")
    async def health():
        return {"ok": True, "version": __version__}

    api = APIRouter(prefix="/api")

    # ------------------------------------------------------------- auth
    @api.post("/auth/pair")
    async def pair(body: PairIn, request: Request):
        token = st.auth.pair(body.code, request.headers.get("user-agent"))
        resp = JSONResponse({"paired": True})
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", secure=False, path="/",
                        max_age=settings.session_ttl_days * 86400)
        return resp

    @api.get("/auth/session")
    async def session(request: Request):
        authz = request.headers.get("authorization", "")
        if authz[:7].lower() == "bearer " and st.auth.check_bearer(authz[7:].strip()):
            return {"paired": True, "mode": "bearer"}
        cookie = parse_cookies(request.headers.get("cookie", "")).get(SESSION_COOKIE)
        return {"paired": bool(cookie and st.auth.check_session(cookie)), "mode": "session"}

    @api.post("/auth/logout")
    async def logout(request: Request):
        st.auth.logout(parse_cookies(request.headers.get("cookie", "")).get(SESSION_COOKIE))
        resp = JSONResponse({"paired": False})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    # ----------------------------------------------------------- status
    @api.get("/status")
    async def status():
        k = st.kernel
        procs = list(k.procs.values()) if k else []
        return {
            "version": __version__,
            "platform": sys.platform,
            "workspace_root": st.workspace_root(),
            "planner": {"mode": st.user_settings().get("planner_mode"), "llm_provider": st.default_provider_id()},
            "kernel": {
                "processes": len([p for p in procs if not p.is_child]),
                "live": st.live_root_processes(),
                "awaiting_user": len([p for p in procs if p.state == "BLOCKED_USER"]),
            },
            "knowledge": {**st.kb.status(), "index_job": st.index_job.public()},
            "credential_store": st.secrets.status(),
            "automation": {"schedules": len([s for s in st.automation.list_schedules() if s["enabled"]]),
                           "listeners": len([x for x in st.automation.list_listeners() if x["enabled"]]),
                           "next_run": st.automation.min_next_run()},
            "mail": st.mail.status(),
            "home_root": st.home_root,
        }

    # -------------------------------------------------------- settings
    @api.get("/settings")
    async def get_settings():
        return {"settings": st.user_settings(), "embedder_kinds": EMBEDDER_DEFAULTS}

    @api.put("/settings")
    async def put_settings(body: SettingsIn):
        data = body.model_dump(exclude_none=True)
        if data.get("browser_channel") == "auto":
            data["browser_channel"] = None
            st.repos.settings.set("browser_channel", None)
        if "workspace_root" in data:
            try:
                await asyncio.to_thread(st.set_workspace, data.pop("workspace_root"))
            except (ValueError, ToolError) as exc:
                raise ApiError(400, "WORKSPACE_INVALID", getattr(exc, "message", None) or str(exc))
            except RuntimeError as exc:
                raise ApiError(409, "WORKSPACE_BUSY", str(exc))
        if "rag_embedder" in data:
            try:
                build_embedder(data["rag_embedder"], st.api_key_for)
            except EmbedderError as exc:
                raise ApiError(400, exc.code, exc.message)
        if data:
            st.repos.settings.set_many(data)
        if "rag_embedder" in data:
            st.reload_embedder()
        return {"settings": st.user_settings()}

    # ----------------------------------------------------------- tasks
    def _task_view(pub: dict) -> dict:
        task = st.repos.tasks.get(pub["task_id"]) or {}
        return {**pub, "created_at": task.get("created_at"), "status": task.get("status"),
                "summary": task.get("summary")}

    @api.post("/tasks")
    async def create_task(body: TaskIn):
        pid = st.kernel.spawn(body.request, title=body.title, plan=body.plan)
        return _task_view(st.kernel.proc(pid))

    @api.get("/tasks")
    async def list_tasks(limit: int = 100):
        roots = [p for p in st.kernel.ps() if not p.get("ppid")]
        live = {p["task_id"] for p in roots}
        rows = [_task_view(p) for p in roots]
        # include exited processes still in the DB (history)
        for raw in st.kernel.store.load_processes(include_exited=True):
            if raw.get("ppid") or raw["task_id"] in live:
                continue
            from ..kernel.process import ProcessControlBlock

            rows.append(_task_view(ProcessControlBlock.from_json(raw).public()))
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return {"tasks": rows[:limit]}

    @api.get("/tasks/{pid}")
    async def get_task(pid: str):
        view = _task_view(st.kernel.proc(pid))
        view["children"] = [c for c in st.kernel.ps() if c.get("ppid") == pid]
        return view

    @api.get("/tasks/{pid}/diff")
    async def task_diff(pid: str):
        return {"files": await asyncio.to_thread(st.kernel.staged_diff, pid)}

    @api.get("/tasks/{pid}/events")
    async def task_events(pid: str, after: int = 0):
        task_id = st.kernel.proc(pid)["task_id"]
        return {"events": st.repos.events.list(task_id, after_seq=after)}

    @api.get("/tasks/{pid}/outputs/{step_id}")
    async def task_output(pid: str, step_id: str):
        out = st.kernel.store.get_output(pid, step_id)
        if out is None:
            raise ApiError(404, "NO_OUTPUT", "That step has no stored output.")
        return {"output": out}

    @api.post("/tasks/{pid}/approve")
    async def approve(pid: str, body: ApproveIn):
        return st.kernel.approve(pid, body.action_hash, standing_minutes=body.standing_minutes)

    @api.post("/tasks/{pid}/reject")
    async def reject(pid: str, body: RejectIn):
        return st.kernel.reject(pid, body.action_hash, body.note)

    @api.post("/tasks/{pid}/answer")
    async def answer(pid: str, body: AnswerIn):
        return st.kernel.answer(pid, body.text)

    @api.post("/tasks/{pid}/commit")
    async def commit(pid: str):
        return st.kernel.commit(pid)

    @api.post("/tasks/{pid}/discard")
    async def discard(pid: str):
        return st.kernel.discard(pid)

    @api.post("/tasks/{pid}/undo")
    async def undo(pid: str):
        res = await asyncio.to_thread(st.kernel.undo, pid)
        if st.user_settings().get("rag_auto_index"):
            st.start_index(".")
        return res

    @api.post("/tasks/{pid}/signal")
    async def signal(pid: str, body: SignalIn):
        return st.kernel.signal(pid, body.signal)

    # ------------------------------------------------------------ events (SSE)
    @api.get("/events")
    async def events(request: Request, task_id: Optional[str] = None, after: int = 0, follow: bool = True):
        loop = asyncio.get_running_loop()
        sub = st.events.subscribe(task_id, loop)
        backlog = st.repos.events.list(task_id, after_seq=after) if task_id else []

        async def gen():
            try:
                yield "retry: 2000\n\n"
                for ev in backlog:
                    yield f"data: {json.dumps(ev, default=str)}\n\n"
                while follow:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(sub.queue.get(), timeout=15)
                        yield f"data: {json.dumps(ev, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                st.events.unsubscribe(sub)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------- knowledge
    @api.get("/kb/status")
    async def kb_status():
        return {**st.kb.status(), "index_job": st.index_job.public()}

    @api.get("/kb/documents")
    async def kb_documents():
        return {"documents": st.kb.documents()}

    @api.post("/kb/index")
    async def kb_index(body: IndexIn):
        return st.start_index(body.path)

    @api.post("/kb/index/cancel")
    async def kb_cancel():
        st.index_job.cancel.set()
        return st.index_job.public()

    @api.post("/kb/search")
    async def kb_search(body: SearchIn):
        guard = st.guard()

        def allow(hit: dict) -> bool:
            if hit["source"] != "workspace":
                return True
            try:
                return not guard.resolve(hit["path"]).sensitive
            except ToolError:
                return False

        hits = await asyncio.to_thread(st.kb.search, body.query, body.k, source=body.source,
                                       path_prefix=body.path_prefix, allow=allow)
        return {"hits": hits, "embedder": st.kb.embedder.name}

    @api.post("/kb/ask")
    async def kb_ask(body: AskIn):
        hits = await asyncio.to_thread(st.kb.search, body.question, body.k)
        sources = [{"n": i + 1, "path": h["path"], "source": h["source"], "start_line": h["start_line"],
                    "end_line": h["end_line"], "text": h["text"][:1200], "score": h["score"]} for i, h in enumerate(hits)]
        pid = st.default_provider_id()
        if not hits:
            return {"answer": "Nothing in the knowledge base matches that yet. Index a folder or add a note first.",
                    "mode": "empty", "sources": []}
        if pid is None:
            return {"answer": None, "mode": "retrieval_only", "sources": sources}
        provider = st.providers.build(pid)
        result = await asyncio.to_thread(provider.generate_text, ANSWER_SYSTEM, build_answer_prompt(body.question, hits))
        return {"answer": result.text, "mode": "generated", "provider": pid, "sources": sources}

    @api.post("/kb/notes")
    async def kb_note(body: NoteIn):
        return await asyncio.to_thread(st.kb.add_note, body.title, body.text)

    @api.delete("/kb/documents/{doc_id}")
    async def kb_delete(doc_id: str):
        if not st.kb.delete_document(doc_id):
            raise ApiError(404, "NO_SUCH_DOCUMENT", "Unknown document.")
        return {"deleted": True}

    @api.delete("/kb")
    async def kb_clear():
        return {"deleted": st.kb.clear()}

    # ------------------------------------------------------------- providers
    @api.get("/providers")
    async def providers():
        kinds = {k: {kk: vv for kk, vv in v.items() if kk != "hidden"} for k, v in PROVIDER_KINDS.items()
                 if k in st.providers.visible_kinds()}
        return {"providers": st.providers.list_public(), "kinds": kinds}

    @api.put("/providers/{provider_id}")
    async def save_provider(provider_id: str, body: ProviderIn):
        return st.providers.save(provider_id, body.model_dump(exclude_none=True))

    @api.post("/providers/{provider_id}/credential")
    async def set_credential(provider_id: str, body: CredentialIn):
        return st.providers.set_credential(provider_id, body.api_key)

    @api.delete("/providers/{provider_id}/credential")
    async def delete_credential(provider_id: str):
        return st.providers.delete_credential(provider_id)

    @api.post("/providers/{provider_id}/default")
    async def default_provider(provider_id: str):
        return st.providers.set_default(provider_id)

    @api.post("/providers/{provider_id}/test")
    async def test_provider(provider_id: str):
        return await asyncio.to_thread(st.providers.test, provider_id)

    @api.delete("/providers/{provider_id}")
    async def delete_provider(provider_id: str):
        if not st.providers.delete(provider_id):
            raise ApiError(404, "NO_SUCH_PROVIDER", "Unknown provider.")
        return {"deleted": True}

    # ------------------------------------------------------------ tools / apps
    @api.get("/tools")
    async def tools():
        return {"tools": st.registry.public()}

    @api.get("/apps")
    async def apps():
        return {"apps": st.repos.apps.list()}

    @api.put("/apps/{app_id}")
    async def put_app(app_id: str, body: AppIn):
        if not os.path.isabs(body.path):
            raise ApiError(400, "APP_PATH_INVALID", "Use the full path to the program.")
        return st.repos.apps.upsert(app_id, body.model_dump())

    @api.delete("/apps/{app_id}")
    async def delete_app(app_id: str):
        return {"deleted": st.repos.apps.delete(app_id)}

    from .routes_agentic import register as register_agentic
    register_agentic(api, app, st, ApiError)
    from .routes_connections import register as register_connections
    register_connections(api, app, st, ApiError)
    from .routes_chat import register as register_chat
    register_chat(api, app, st, ApiError)
    app.include_router(api)

    # --------------------------------------------------------- static UI (browser mode)
    dist = settings.ui_dist_dir
    if dist and os.path.isdir(dist) and os.path.isfile(os.path.join(dist, "index.html")):
        assets = os.path.join(dist, "assets")
        if os.path.isdir(assets):
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            candidate = os.path.realpath(os.path.join(dist, path))
            if path and candidate.startswith(os.path.realpath(dist)) and os.path.isfile(candidate):
                return FileResponse(candidate)
            return FileResponse(os.path.join(dist, "index.html"))

    return app
