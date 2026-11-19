"""Kernel monitor: a live `ps` / `top` view of the kernel, plus scripted demos of each OS mechanism.

GET  /api/kernel             process table, run queue, capability table, journal, kernel log, counters
POST /api/kernel/demo/{name} spawn a scripted process that exercises one mechanism (see DEMOS)
"""

from __future__ import annotations

from typing import Callable, Dict

from fastapi import APIRouter, FastAPI

from ..util import json_loads

KERNEL_EVENTS = ("proc.spawn", "proc.fork", "proc.wait", "proc.reaped", "proc.exit", "proc.recovered", "signal",
                 "syscall.enter", "syscall.exit", "cap.grant", "cap.denied", "policy.decision", "approval.requested",
                 "approval.decided", "budget.exceeded", "txn.review", "txn.committed", "txn.undone", "txn.discarded",
                 "txn.conflict", "txn.recovered", "txn.incomplete", "outbox.deferred", "plan.ready", "plan.replan",
                 "step.retry", "verify.failed", "kernel.error")
COUNTED = {"syscalls": "syscall.enter", "interrupts": "syscall.exit", "forks": "proc.fork", "signals": "signal",
           "grants": "cap.grant", "denials": "cap.denied", "commits": "txn.committed", "undos": "txn.undone",
           "recoveries": "proc.recovered", "rlimit_kills": "budget.exceeded"}


def _demo_plans(st) -> Dict[str, Callable[[], dict]]:
    def transaction() -> dict:
        return {"title": "OS demo: transaction (copy-on-write, review, commit, undo)", "plan": {
            "goal": "Write two files in a private overlay, then wait for review", "needs_clarification": False, "steps": [
                {"id": "w1", "title": "Create os-demo/transaction.md", "tool": "create_workspace_file",
                 "args": {"path": "os-demo/transaction.md", "overwrite": True,
                          "content": "# Transaction demo\n\nWritten into this task's copy-on-write overlay.\n"}},
                {"id": "w2", "title": "Append a line", "tool": "edit_workspace_file", "depends_on": ["w1"],
                 "args": {"path": "os-demo/transaction.md", "operation": "append",
                          "text": "\nNothing touches the real file until you commit. Undo restores it from the journal.\n"}},
                {"id": "w3", "title": "Create os-demo/second.md", "tool": "create_workspace_file", "depends_on": ["w2"],
                 "args": {"path": "os-demo/second.md", "overwrite": True, "content": "Second file in the same transaction.\n"}},
            ]}}

    def fork() -> dict:
        return {"title": "OS demo: fork / wait / reap (parallel children)", "plan": {
            "goal": "Run four read-only steps as child processes", "needs_clarification": False, "steps": [
                {"id": "a", "title": "Child A: search files for TODO", "tool": "search_workspace_files", "args": {"query": "TODO"}},
                {"id": "b", "title": "Child B: list files", "tool": "list_workspace_files", "args": {"path": ".", "max_depth": 2}},
                {"id": "c", "title": "Child C: sleep 4 s", "tool": "wait", "args": {"seconds": 4}},
                {"id": "d", "title": "Child D: sleep 6 s", "tool": "wait", "args": {"seconds": 6}},
                {"id": "e", "title": "Parent continues after reaping", "tool": "respond_to_user", "depends_on": ["a", "b", "c", "d"],
                 "args": {"text": "All four children exited and were reaped; the parent resumed."}},
            ]}}

    def signals() -> dict:
        steps, prev = [], None
        for i in range(1, 7):
            s = {"id": f"s{i}", "title": f"Slow step {i} of 6 (5 s)", "tool": "wait", "args": {"seconds": 5}}
            if prev:
                s["depends_on"] = [prev]
            steps.append(s)
            prev = s["id"]
        return {"title": "OS demo: signals (STOP / CONT / TERM) and crash recovery", "plan": {
            "goal": "A 30-second sequential job to pause, resume, kill, or survive a restart", "needs_clarification": False,
            "steps": steps}}

    def capability() -> dict:
        apps = [a for a in st.repos.apps.list() if a.get("enabled", True)]
        app = next((a for a in apps if "notepad" in (a.get("id", "") + a.get("name", "")).lower()), apps[0] if apps else None)
        if app is None:
            raise LookupError("Add an app first (Settings → Apps), then run this demo.")
        return {"title": "OS demo: capabilities (approval is a grant)", "plan": {
            "goal": f"List files (allowed), then launch {app.get('name') or app['id']} (needs a capability)",
            "needs_clarification": False, "steps": [
                {"id": "info", "title": "List files (fs:read is granted at spawn)", "tool": "list_workspace_files",
                 "args": {"path": ".", "max_depth": 1}},
                {"id": "launch", "title": f"Launch {app.get('name') or app['id']} (no app:launch capability yet)",
                 "tool": "launch_allowlisted_application", "depends_on": ["info"], "args": {"app_id": app["id"]}},
            ]}}

    def rlimit() -> dict:
        steps = [{"id": f"r{i}", "title": f"Step {i}", "tool": "wait", "args": {"seconds": 1},
                  **({"depends_on": [f"r{i - 1}"]} if i > 1 else {})} for i in range(1, 6)]
        return {"title": "OS demo: rlimits (step budget of 2)", "budget": {"max_executions": 2}, "plan": {
            "goal": "Five steps with a budget of two executions: the kernel terminates it", "needs_clarification": False,
            "steps": steps}}

    def scheduler() -> dict:
        return {"title": "OS demo: scheduler (three processes share the CPU)", "many": 3, "plan": {
            "goal": "Several short sleeps", "needs_clarification": False, "steps": [
                {"id": "x1", "title": "Sleep 3 s", "tool": "wait", "args": {"seconds": 3}},
                {"id": "x2", "title": "Sleep 3 s", "tool": "wait", "args": {"seconds": 3}, "depends_on": ["x1"]},
                {"id": "x3", "title": "Sleep 3 s", "tool": "wait", "args": {"seconds": 3}, "depends_on": ["x2"]},
            ]}}

    return {"transaction": transaction, "fork": fork, "signals": signals, "capability": capability,
            "rlimit": rlimit, "scheduler": scheduler}


def register(api: APIRouter, app: FastAPI, st, ApiError) -> None:
    @api.get("/kernel")
    async def kernel_view():
        k = st.kernel
        procs = k.ps()
        rows = []
        for p in procs:
            steps = (p.get("plan") or {}).get("steps") or []
            done = sum(1 for s in (p.get("steps_state") or {}).values() if (s or {}).get("status") == "done")
            rows.append({k_: p.get(k_) for k_ in ("pid", "ppid", "title", "state", "phase", "priority", "slices", "usage",
                                                   "budget", "children", "exit", "txn_id", "origin", "capabilities",
                                                   "pending_user", "auto_commit")}
                        | {"steps_total": len(steps), "steps_done": done})
        rows.sort(key=lambda r: (r["state"] in ("EXITED", "ZOMBIE"), str(r["pid"])), reverse=False)
        runq = k._command(k.runq.snapshot)
        txns = [dict(r) for r in st.db.query(
            "SELECT t.txn_id, t.pid, t.task_id, t.state, t.created_at, t.updated_at, "
            "(SELECT COUNT(*) FROM kernel_journal j WHERE j.txn_id = t.txn_id) AS files "
            "FROM kernel_txns t ORDER BY t.created_at DESC LIMIT 15")]
        marks = ",".join("?" * len(KERNEL_EVENTS))
        log = []
        for r in st.db.query(f"SELECT e.id, e.ts, e.type, e.level, e.node, e.step_id, e.message, e.data_json, t.title "
                             f"FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id WHERE e.type IN ({marks}) "
                             f"ORDER BY e.id DESC LIMIT 80", KERNEL_EVENTS):
            d = dict(r)
            data = json_loads(d.pop("data_json", "{}"), {})
            d["pid"], d["ppid"] = data.get("pid"), data.get("ppid")
            d["tool"] = data.get("tool")
            log.append(d)
        counts = {name: 0 for name in COUNTED}
        by_type = {r["type"]: r["n"] for r in st.db.query(
            f"SELECT type, COUNT(*) AS n FROM task_events WHERE type IN ({','.join('?' * len(COUNTED))}) GROUP BY type",
            tuple(COUNTED.values()))}
        for name, t in COUNTED.items():
            counts[name] = by_type.get(t, 0)
        states: Dict[str, int] = {}
        for r in rows:
            states[r["state"]] = states.get(r["state"], 0) + 1
        return {"processes": rows, "runq": runq, "txns": txns, "log": log, "counters": counts, "states": states,
                "fork_enabled": getattr(k, "fork_enabled", True)}

    @api.post("/kernel/demo/{name}")
    async def kernel_demo(name: str):
        demos = _demo_plans(st)
        if name not in demos:
            raise ApiError(404, "DEMO_UNKNOWN", f"Unknown demo '{name}'. Try: {', '.join(demos)}.")
        try:
            spec = demos[name]()
        except LookupError as exc:
            raise ApiError(400, "DEMO_NEEDS_SETUP", str(exc))
        pids = [st.kernel.spawn(spec["title"], title=spec["title"] + (f" #{i + 1}" if spec.get("many") else ""),
                                plan=spec["plan"], budget=spec.get("budget"), origin={"kind": "demo", "id": name})
                for i in range(int(spec.get("many") or 1))]
        return {"pids": pids, "demo": name}
