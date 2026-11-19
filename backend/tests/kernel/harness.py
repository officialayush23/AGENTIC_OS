"""Test harness: builds a kernel over a temp workspace, with a real or simulated I/O driver."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from agentic_os.kernel import Kernel, SimClock, SimIO, StaticPlanner, ThreadPoolIO
from agentic_os.persistence.db import Database
from agentic_os.policy.engine import PolicyEngine
from agentic_os.tools.registry import build_default_registry

SEED_FILES: Dict[str, str] = {
    "README.md": "# Demo project\n\nA tiny project used by the kernel tests.\n",
    "package.json": '{\n  "name": "demo",\n  "version": "1.0.0"\n}\n',
    "src/app.py": "def main():\n    return 'hello'\n",
    "docs/notes.md": "Line one\nTODO: write docs\n",
    ".env": "SECRET=hunter2\n",
}


def make_workspace(root: str, files: Optional[Dict[str, str]] = None) -> None:
    for rel, text in (files or SEED_FILES).items():
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)


def snapshot(root: str) -> Dict[str, str]:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            p = os.path.join(dirpath, name)
            rel = os.path.relpath(p, root).replace("\\", "/")
            with open(p, "rb") as fh:
                out[rel] = hashlib.sha256(fh.read()).hexdigest()
    return out


class FakeOS:
    """OS adapter that records launches instead of starting real programs."""

    def __init__(self):
        self.launches = []

    def validate_executable(self, path):
        return path

    def launch(self, exe, argv, cwd=None):
        self.launches.append({"exe": exe, "argv": list(argv), "cwd": cwd})
        return {"pid": 4242 + len(self.launches), "launched_at": "2026-11-15T00:00:00Z", "still_running": True}

    def system_info(self, root=None):
        return {"os": "test", "version": "1", "machine": "x", "python": "3", "cpu_count": 1}

    def list_processes(self, limit=20, sort_by="memory"):
        return {"processes": [], "total": 0}


def allow_notepad(kernel, requires_approval=False):
    kernel.apps.upsert("notepad", {"name": "Notepad", "path": "/usr/bin/notepad", "args_policy": "workspace_file",
                                   "requires_approval": requires_approval})


@dataclass
class Env:
    tmp: str
    workspace: str
    data_dir: str
    db_path: str


def make_env(tmp_path) -> Env:
    ws = os.path.join(str(tmp_path), "workspace")
    data = os.path.join(str(tmp_path), "data")
    os.makedirs(ws)
    os.makedirs(data)
    make_workspace(ws)
    return Env(str(tmp_path), ws, data, os.path.join(data, "agentic_os.db"))


def build_kernel(env: Env, plan=None, *, plan_fn: Optional[Callable] = None, sim: bool = True, seed: int = 0,
                 effect_at_issue: float = 0.0, settings: Optional[dict] = None, journal_fault=None,
                 fork_enabled: bool = True, planner=None, budget=None, clock: Optional[SimClock] = None,
                 os_adapter=None):
    db = Database(env.db_path)
    db.migrate()
    registry = build_default_registry()
    policy = PolicyEngine(registry)
    if sim:
        clock = clock or SimClock()
        io = SimIO(clock, seed=seed, effect_at_issue=effect_at_issue)
    else:
        clock = None
        io = ThreadPoolIO(workers=4)
    kwargs = {}
    if clock is not None:
        kwargs["clock"] = clock
    kernel = Kernel(
        db=db, registry=registry, policy=policy,
        planner=planner or StaticPlanner(plan=plan, fn=plan_fn), io=io,
        workspace_root=env.workspace, data_dir=env.data_dir, settings_fn=lambda: dict(settings or {}),
        journal_fault=journal_fault, fork_enabled=fork_enabled, budget=budget, os_adapter=os_adapter or FakeOS(), **kwargs,
    )
    kernel.boot()
    return kernel


REPORT_PLAN = {
    "goal": "Inventory the project and write a report",
    "steps": [
        {"id": "list", "title": "List files", "tool": "list_workspace_files", "args": {"path": ".", "max_depth": 3}},
        {"id": "config", "title": "Find config files", "tool": "identify_project_config_files", "args": {"path": "."}},
        {"id": "readme", "title": "Read README", "tool": "read_workspace_file", "args": {"path": "README.md"}},
        {"id": "write", "title": "Write report", "tool": "create_workspace_file", "depends_on": ["list", "readme"],
         "args": {"path": "reports/project_report.md", "content": "# Project report\n\nFiles inventoried.\n"}},
        {"id": "edit", "title": "Resolve TODO", "tool": "edit_workspace_file", "depends_on": ["write"],
         "args": {"path": "docs/notes.md", "operation": "replace_text", "find": "TODO: write docs",
                  "replace": "Docs written by the agent"}},
    ],
}


REF_REPORT_PLAN = {
    "goal": "Write a project report from real tool outputs",
    "steps": [
        {"id": "list", "tool": "list_workspace_files", "args": {"path": ".", "max_depth": 3}},
        {"id": "config", "tool": "identify_project_config_files", "args": {"path": "."}},
        {"id": "report", "tool": "build_workspace_report",
         "args": {"title": "Project Report", "listing": {"$ref": "list.output"}, "config": {"$ref": "config.output"}}},
        {"id": "save", "tool": "create_workspace_file",
         "args": {"path": "project_report.md", "content": {"$ref": "report.output.markdown"}}},
    ],
}
