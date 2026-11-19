"""Application state: wires database, security, providers, knowledge base and kernel together."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ..config import Settings
from ..events import EventBus
from ..kernel import HybridPlanner, Kernel, LLMPlanner, ThreadPoolIO
from ..automation.listeners import ListenerService
from ..automation.store import AutomationStore
from ..kernel.mounts import MountedWorkspace, split_home
from ..kernel.process import DONE_STATES, ProcState
from ..services.browser import BrowserService
from ..services.mail import MailService
from ..persistence.db import Database
from ..persistence.repositories import Repositories
from ..policy.engine import PolicyEngine
from ..providers.manager import ProviderManager
from ..rag import KnowledgeBase, build_embedder
from ..security.local_auth import LocalAuth
from ..security.secrets import SecretStore, build_secret_store, provider_secret_name
from ..tools.pathguard import WorkspaceGuard
from ..tools.registry import build_default_registry

log = logging.getLogger(__name__)

DEFAULT_USER_SETTINGS: Dict[str, Any] = {
    "workspace_root": None,
    "planner_mode": "auto",                 # auto (rules, then LLM if configured) | rules | llm
    "require_approval_for_writes": False,   # True: also approve every staged write step
    "allow_workspace_writes": True,
    "allow_app_launch": True,
    "rag_embedder": {"kind": "local"},
    "rag_auto_index": True,
    "mail": {"mode": "local", "address": "you@agentic-os.local"},
    "browser_headed": False,              # show the browser window while the agent uses it
    "browser_channel": None,              # msedge | chrome | None (auto)
    "web_allow_private": False,           # allow opening pages on the local network / localhost
}

HOME_SEED = {
    "Notes/Welcome.md": "# Welcome to your Home folder\n\nNotes live here as plain markdown files. The agent can "
                        "create and edit them, and every change is staged for your review like any other file.\n\n"
                        "- `~/Notes` - notes\n- `~/Calendar/calendar.ics` - your local calendar (any calendar app "
                        "can import it)\n- `~/Mail/Outbox` - email drafts\n",
    "Calendar/calendar.ics": "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Agentic OS//Calendar 1.0//EN\r\n"
                             "CALSCALE:GREGORIAN\r\nEND:VCALENDAR\r\n",
    "Mail/Outbox/.keep": "",
}

DEMO_FILES = {
    "README.md": "# Demo workspace\n\nThis folder is a safe sandbox for trying Agentic OS.\n"
                 "Point Settings → Workspace at a real project when you are ready.\n",
    "notes/ideas.md": "# Ideas\n\n- Ship the kernel UI\n- TODO: write the onboarding guide\n",
    "src/app.py": "def main():\n    \"\"\"Entry point.\"\"\"\n    return 'hello from the demo workspace'\n",
    "package.json": '{\n  "name": "demo-workspace",\n  "version": "0.1.0"\n}\n',
}


class IndexJob:
    def __init__(self) -> None:
        self.running = False
        self.progress: dict = {}
        self.result: Optional[dict] = None
        self.error: Optional[str] = None
        self.cancel = threading.Event()

    def public(self) -> dict:
        return {"running": self.running, "progress": self.progress, "result": self.result, "error": self.error}


class AppState:
    def __init__(self, settings: Settings, *, secrets: Optional[SecretStore] = None,
                 io_factory: Callable[[], Any] = lambda: ThreadPoolIO(workers=4), os_adapter: Any = None):
        settings.ensure_dirs()
        self.settings = settings
        self.db = Database(str(settings.db_path))
        self.db.migrate()
        self.repos = Repositories(self.db)
        self.events = EventBus(self.repos.events)
        self.secrets = secrets or build_secret_store(settings.secret_store_backend, settings.keyring_service,
                                                     settings.allow_insecure_keyring)
        self.providers = ProviderManager(self.repos.providers, self.secrets, settings)
        self.auth = LocalAuth(settings, self.repos.sessions)
        self.registry = build_default_registry()
        self.policy = PolicyEngine(self.registry)
        self.io_factory = io_factory
        self.os_adapter = os_adapter
        self.index_job = IndexJob()
        self._lock = threading.RLock()
        self.kb = KnowledgeBase(self.db, self._make_embedder())
        self.home_root = self._home()
        self.mail = MailService(str(settings.data_dir), self.user_settings, self.secrets)
        try:
            self.mail.local.seed_demo()
        except Exception:  # noqa: BLE001
            log.exception("could not seed the demo mailbox")
        self.browser = BrowserService(str(settings.data_dir), self.user_settings)
        self.automation = AutomationStore(self.db)
        self.kernel: Optional[Kernel] = None
        self.listeners = ListenerService(
            self.automation, spawn=lambda *a, **k: self.kernel.spawn(*a, **k), resolve_root=self.listener_root,
            mail=self.mail, publish=self.events.publish_global)
        self._build_kernel()

    # ------------------------------------------------------------- settings
    def user_settings(self) -> Dict[str, Any]:
        stored = self.repos.settings.get_all() or {}
        merged = {**DEFAULT_USER_SETTINGS, **{k: v for k, v in stored.items() if v is not None}}
        if not merged.get("workspace_root"):
            merged["workspace_root"] = str(self._demo_workspace())
        return merged

    def _demo_workspace(self) -> Path:
        root = self.settings.default_demo_workspace
        if not root.exists():
            for rel, text in DEMO_FILES.items():
                p = root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding="utf-8")
        return root

    def workspace_root(self) -> str:
        return str(self.user_settings()["workspace_root"])

    def _home(self) -> str:
        root = Path(self.settings.data_dir) / "home"
        for rel, text in HOME_SEED.items():
            p = root / rel
            if not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding="utf-8", newline="")
        return str(root)

    def guard(self) -> WorkspaceGuard:
        s = self.user_settings()
        return WorkspaceGuard(self.workspace_root(), sensitive_patterns=s.get("sensitive_patterns"),
                              ignored_dirs=s.get("ignored_dirs"))

    def home_guard(self) -> WorkspaceGuard:
        s = self.user_settings()
        return WorkspaceGuard(self.home_root, sensitive_patterns=s.get("sensitive_patterns"),
                              ignored_dirs=s.get("ignored_dirs"))

    def mounted(self) -> MountedWorkspace:
        """Read view over both mounts: project paths and `~/` Home paths."""
        return MountedWorkspace(self.guard(), self.home_guard())

    def listener_root(self, path: str):
        home, rel = split_home(path)
        target = self.mounted().resolve(path)
        if not target.is_dir:
            raise ValueError(f"{path} is not a folder")
        prefix = ("~/" if home else "") + ("" if rel in (".", "") else rel.rstrip("/") + "/")
        return target.abs_path, prefix

    # ------------------------------------------------------------ providers
    def api_key_for(self, provider_id: str) -> Optional[str]:
        try:
            return self.secrets.get(provider_secret_name(provider_id))
        except Exception:  # noqa: BLE001
            return None

    def default_provider_id(self) -> Optional[str]:
        rec = self.providers.resolve_record(None)
        if not rec or not rec.get("enabled", True):
            return None
        cred = self.providers.credential_status(rec["id"], rec["kind"])
        if cred.get("required") and not cred.get("stored"):
            return None
        return rec["id"]

    def _llm_planner(self) -> Optional[LLMPlanner]:
        mode = self.user_settings().get("planner_mode", "auto")
        if mode == "rules":
            return None
        pid = self.default_provider_id()
        if pid is None:
            return None
        return LLMPlanner(lambda: self.providers.build(pid))

    # ------------------------------------------------------------------ RAG
    def _make_embedder(self):
        cfg = self.user_settings().get("rag_embedder") or {"kind": "local"}
        try:
            return build_embedder(cfg, self.api_key_for)
        except Exception as exc:  # noqa: BLE001 - never block startup on a bad embedder config
            log.warning("embedder config invalid (%s); using the local embedder", exc)
            return build_embedder({"kind": "local"})

    def reload_embedder(self) -> None:
        self.kb.set_embedder(build_embedder(self.user_settings().get("rag_embedder") or {"kind": "local"},
                                            self.api_key_for))

    def start_index(self, path: str = ".") -> dict:
        with self._lock:
            if self.index_job.running:
                return self.index_job.public()
            job = IndexJob()
            job.running = True
            self.index_job = job
        guard = self.mounted()

        def progress(p: dict) -> None:
            job.progress = p
            self.events.publish_global({"type": "kb.progress", "data": p})

        def run() -> None:
            try:
                job.result = self.kb.index_workspace(guard, path, progress=progress, cancel=job.cancel.is_set)
                if path == "." and not job.cancel.is_set():
                    home = self.kb.index_workspace(guard, "~", progress=progress, cancel=job.cancel.is_set)
                    job.result = {k: (v + home.get(k, 0) if isinstance(v, int) and not isinstance(v, bool) else v)
                                  for k, v in job.result.items()}
            except Exception as exc:  # noqa: BLE001
                job.error = getattr(exc, "message", None) or str(exc)
                log.exception("indexing failed")
            finally:
                job.running = False
                self.events.publish_global({"type": "kb.done", "data": {"result": job.result, "error": job.error}})

        threading.Thread(target=run, name="aos-index", daemon=True).start()
        return job.public()

    # --------------------------------------------------------------- kernel
    def _build_kernel(self) -> None:
        kernel = Kernel(
            db=self.db, registry=self.registry, policy=self.policy,
            planner=HybridPlanner(self._llm_planner), io=self.io_factory(),
            workspace_root=self.workspace_root(), data_dir=str(self.settings.data_dir),
            settings_fn=self.user_settings, os_adapter=self.os_adapter, events=self.events,
            services={"rag": self.kb, "on_commit": self._on_commit, "on_exit": self._on_exit, "mail": self.mail,
                      "browser": self.browser},
            home_root=self.home_root, automation=self.automation,
        )
        kernel.boot()
        self.kernel = kernel

    def _on_exit(self, pid: str, task_id: str, code: str) -> None:
        self.browser.close(task_id)

    def _on_commit(self, pid: str, files) -> None:
        pcb = self.kernel.procs.get(pid) if self.kernel else None
        self.listeners.note_commit(getattr(pcb, "origin", None), [f.get("path") for f in files or []])
        if self.user_settings().get("rag_auto_index"):
            self.start_index(".")

    def live_root_processes(self) -> int:
        assert self.kernel is not None
        return sum(1 for p in self.kernel.procs.values()
                   if not p.is_child and ProcState(p.state) not in DONE_STATES)

    def set_workspace(self, path: str) -> str:
        real = os.path.realpath(os.path.expanduser(path))
        if not os.path.isdir(real):
            raise ValueError("That folder does not exist.")
        WorkspaceGuard(real)                       # validates (not a drive root, etc.)
        with self._lock:
            if self.live_root_processes():
                raise RuntimeError("Finish, commit or discard running tasks before changing the workspace.")
            started = self.kernel is not None and self.kernel._loop_thread is not None
            if self.kernel is not None:
                self.kernel.stop()
            self.repos.settings.set("workspace_root", real)
            self._build_kernel()
            if started:
                self.kernel.start()
        return real

    def start(self) -> None:
        assert self.kernel is not None
        self.kernel.start()
        self.listeners.start()
        if self.user_settings().get("rag_auto_index"):
            self.start_index(".")

    def stop(self) -> None:
        self.index_job.cancel.set()
        self.listeners.stop()
        if self.kernel is not None:
            self.kernel.stop()
        self.browser.stop()
        self.events.close_all()
        self.db.close()
