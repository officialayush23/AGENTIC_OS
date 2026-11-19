import os
import time

import pytest

from agentic_os.kernel import Budget, KernelError, Signal, StaticPlanner
from agentic_os.kernel.kernel import Kernel
from agentic_os.policy.engine import PolicyEngine
from agentic_os.tools.base import ToolContext
from agentic_os.tools.pathguard import WorkspaceGuard

from .harness import REPORT_PLAN, allow_notepad, build_kernel, make_env, snapshot


@pytest.fixture()
def env(tmp_path):
    return make_env(tmp_path)


def events(kernel, pid):
    task_id = kernel.proc(pid)["task_id"]
    return [e["type"] for e in kernel.events.repo.list(task_id)]


def step(kernel):
    """One unit of progress: a tick, or (if idle) the next I/O completion."""
    if not kernel.tick() and kernel.io.pending():
        for c in kernel.io.wait(timeout=0.5):
            kernel._deliver(c)


def run_to_review(kernel, request="do it"):
    pid = kernel.spawn(request)
    kernel.run_until_idle()
    return pid


# ---------------------------------------------------------------- happy path
def test_transaction_end_to_end(env):
    before = snapshot(env.workspace)
    k = build_kernel(env, REPORT_PLAN, seed=1)
    pid = run_to_review(k)
    p = k.proc(pid)
    assert p["state"] == "BLOCKED_USER" and p["pending_user"]["kind"] == "review"
    assert {f["path"] for f in p["pending_user"]["files"]} == {"docs/notes.md", "reports/project_report.md"}
    assert snapshot(env.workspace) == before, "nothing may touch the real workspace before commit"
    k.commit(pid)
    k.run_until_idle()
    p = k.proc(pid)
    assert p["state"] == "EXITED" and p["exit"]["code"] == "completed" and p["txn_id"]
    assert os.path.exists(os.path.join(env.workspace, "reports", "project_report.md"))
    assert k.tasks.get(p["task_id"])["status"] == "completed"
    k.undo(pid)
    assert snapshot(env.workspace) == before


def test_read_only_steps_are_forked_and_reaped(env):
    k = build_kernel(env, REPORT_PLAN, seed=2)
    pid = run_to_review(k)
    types = events(k, pid)
    assert types.count("proc.fork") == 3 and types.count("proc.reaped") == 3
    p = k.proc(pid)
    assert all(p["steps_state"][s]["status"] == "done" for s in ("list", "config", "readme"))
    assert p["steps_state"]["list"]["via"].startswith("proc_")
    assert p["usage"]["executions"] == 5


def test_children_hold_only_read_capabilities(env):
    k = build_kernel(env, REPORT_PLAN, seed=2)
    pid = k.spawn("x")
    for _ in range(50):              # run until the children exist but before they finish
        step(k)
        kids = [p for p in k.procs.values() if p.ppid == pid]
        if kids:
            break
    assert kids
    for child in kids:
        held = k.caps.held(child.pid)
        assert held and all("write" not in c.rights for c in held)
        assert not any(c.kind == "app" for c in held)


def test_discard_leaves_workspace_untouched(env):
    before = snapshot(env.workspace)
    k = build_kernel(env, REPORT_PLAN)
    pid = run_to_review(k)
    k.discard(pid)
    p = k.proc(pid)
    assert p["exit"]["code"] == "discarded"
    assert snapshot(env.workspace) == before
    with pytest.raises(KernelError):
        k.undo(pid)


def test_no_changes_means_no_review(env):
    plan = {"goal": "look", "steps": [{"id": "r", "tool": "read_workspace_file", "args": {"path": "README.md"}}]}
    k = build_kernel(env, plan)
    pid = run_to_review(k)
    p = k.proc(pid)
    assert p["state"] == "EXITED" and p["exit"]["code"] == "completed" and p["txn_id"] is None


# ------------------------------------------------------------------- signals
def test_stop_freezes_and_cont_resumes(env):
    k = build_kernel(env, REPORT_PLAN, fork_enabled=False)
    pid = k.spawn("x")
    for _ in range(4):
        step(k)
    k.signal(pid, Signal.STOP.value)
    slices = k.proc(pid)["slices"]
    k.run_until_idle()
    assert k.proc(pid)["state"] == "STOPPED"
    assert k.proc(pid)["slices"] == slices, "a stopped process gets no time slices"
    k.signal(pid, Signal.CONT.value)
    k.run_until_idle()
    assert k.proc(pid)["pending_user"]["kind"] == "review"


def test_term_cancels_and_discards(env):
    before = snapshot(env.workspace)
    k = build_kernel(env, REPORT_PLAN, fork_enabled=False)
    pid = k.spawn("x")
    for _ in range(40):
        step(k)
        if k.proc(pid)["steps_state"].get("write", {}).get("status") == "done":
            break
    k.signal(pid, Signal.TERM.value)
    k.run_until_idle()
    p = k.proc(pid)
    assert p["exit"]["code"] == "cancelled"
    assert snapshot(env.workspace) == before
    assert k.staged_diff(pid) == []
    assert k.caps.held(pid) == []
    with pytest.raises(KernelError):
        k.signal(pid, Signal.CONT.value)


def test_kill_with_io_in_flight_drops_the_late_write(env):
    before = snapshot(env.workspace)
    plan = {"goal": "w", "steps": [{"id": "w", "tool": "create_workspace_file",
                                     "args": {"path": "late.txt", "content": "late\n"}}]}
    k = build_kernel(env, plan)
    pid = k.spawn("x")
    while not (k.procs[pid].pending_io and k.procs[pid].pending_io["kind"] == "tool"):
        step(k)
    k.signal(pid, Signal.KILL.value)
    k.run_until_idle()               # the in-flight write completes after the kill
    assert k.proc(pid)["exit"]["code"] == "cancelled"
    assert k.staged_diff(pid) == []
    assert snapshot(env.workspace) == before


# ---------------------------------------------- capabilities, approvals, outbox
def test_irreversible_action_needs_a_granted_capability(env):
    plan = {"goal": "open", "steps": [{"id": "open", "tool": "launch_allowlisted_application",
                                        "args": {"app_id": "notepad", "file": "README.md"}}]}
    k = build_kernel(env, plan)
    allow_notepad(k)
    pid = run_to_review(k)
    p = k.proc(pid)
    pend = p["pending_user"]
    assert pend["kind"] == "approval" and pend["irreversible"] and pend["grant"]["kind"] == "app"
    assert k.os_adapter.launches == []
    k.approve(pid, pend["action_hash"])
    k.run_until_idle()
    assert len(k.os_adapter.launches) == 1
    assert k.proc(pid)["exit"]["code"] == "completed"
    assert k.caps.find(pid, "app", "launch", "notepad") is None   # one-shot grant used up / revoked at exit


def test_approval_bound_to_action_hash(env):
    plan = {"goal": "open", "steps": [{"id": "open", "tool": "launch_allowlisted_application",
                                        "args": {"app_id": "notepad"}}]}
    k = build_kernel(env, plan)
    allow_notepad(k)
    pid = run_to_review(k)
    with pytest.raises(KernelError) as exc:
        k.approve(pid, "0" * 64)
    assert exc.value.code == "APPROVAL_MISMATCH"


def test_rejection_replans_then_fails_cleanly(env):
    plan = {"goal": "open", "steps": [{"id": "open", "tool": "launch_allowlisted_application",
                                        "args": {"app_id": "notepad"}}]}
    k = build_kernel(env, plan, budget=Budget(max_replans=0))
    allow_notepad(k)
    pid = run_to_review(k)
    k.reject(pid, k.proc(pid)["pending_user"]["action_hash"], "not now")
    k.run_until_idle()
    p = k.proc(pid)
    assert p["exit"]["code"] == "failed" and "declined" in p["exit"]["reason"]
    assert k.os_adapter.launches == []


def test_outbox_defers_launch_until_after_commit(env):
    plan = {"goal": "report then open", "steps": [
        {"id": "write", "tool": "create_workspace_file", "args": {"path": "report.md", "content": "# R\n"}},
        {"id": "open", "tool": "launch_allowlisted_application", "depends_on": ["write"],
         "args": {"app_id": "notepad", "file": "report.md"}},
    ]}
    k = build_kernel(env, plan)
    allow_notepad(k)
    pid = run_to_review(k)
    p = k.proc(pid)
    assert p["pending_user"]["kind"] == "review" and p["outbox"] == ["open"]
    assert p["steps_state"]["open"]["status"] == "deferred"
    assert k.os_adapter.launches == []
    k.commit(pid)
    k.run_until_idle()
    pend = k.proc(pid)["pending_user"]
    assert pend["kind"] == "approval"                    # launch still needs its grant, now against real files
    k.approve(pid, pend["action_hash"], standing_minutes=10)
    k.run_until_idle()
    p = k.proc(pid)
    assert p["exit"]["code"] == "completed"
    assert k.os_adapter.launches[0]["argv"][0] == os.path.join(k.workspace_root, "report.md")


def test_discard_drops_outbox(env):
    plan = {"goal": "report then open", "steps": [
        {"id": "write", "tool": "create_workspace_file", "args": {"path": "report.md", "content": "# R\n"}},
        {"id": "open", "tool": "launch_allowlisted_application", "args": {"app_id": "notepad", "file": "report.md"}},
    ]}
    k = build_kernel(env, plan)
    allow_notepad(k)
    pid = run_to_review(k)
    k.discard(pid)
    k.run_until_idle()
    assert k.os_adapter.launches == []


def test_sensitive_files_denied_inside_the_kernel(env):
    plan = {"goal": "leak", "steps": [{"id": "r", "tool": "read_workspace_file", "args": {"path": ".env"}}]}
    k = build_kernel(env, plan, budget=Budget(max_replans=0))
    pid = run_to_review(k)
    p = k.proc(pid)
    assert p["exit"]["code"] == "failed"
    assert "policy.decision" in events(k, pid)


# ------------------------------------------------------------ failures, replans
def test_invalid_plan_is_replanned(env):
    calls = []

    def fn(request, feedback, completed, clarification):
        calls.append(feedback)
        if len(calls) == 1:
            return {"goal": "bad", "steps": [{"id": "x", "tool": "run_shell", "args": {"cmd": "rm -rf /"}}]}
        return {"goal": "ok", "steps": [{"id": "r", "tool": "read_workspace_file", "args": {"path": "README.md"}}]}

    k = build_kernel(env, plan_fn=fn)
    pid = run_to_review(k)
    assert calls[0] is None and "run_shell" in calls[1]
    assert k.proc(pid)["exit"]["code"] == "completed"
    assert "plan.invalid" in events(k, pid)


def test_failure_after_partial_work_offers_review_not_silent_loss(env):
    plan = {"goal": "partial", "steps": [
        {"id": "w", "tool": "create_workspace_file", "args": {"path": "keep.md", "content": "useful\n"}},
        {"id": "e", "tool": "edit_workspace_file", "depends_on": ["w"],
         "args": {"path": "docs/notes.md", "operation": "replace_text", "find": "NOT THERE", "replace": "x"}},
    ]}
    before = snapshot(env.workspace)
    k = build_kernel(env, plan, budget=Budget(max_replans=0))
    pid = run_to_review(k)
    p = k.proc(pid)
    assert p["pending_user"]["kind"] == "review" and p["pending_user"]["outcome"] == "failed"
    assert snapshot(env.workspace) == before
    k.commit(pid)
    k.run_until_idle()
    p = k.proc(pid)
    assert p["exit"]["code"] == "failed" and p["txn_id"]          # user chose to keep the partial result
    assert os.path.exists(os.path.join(env.workspace, "keep.md"))


def test_clarification_round_trip(env):
    def fn(request, feedback, completed, clarification):
        if not clarification:
            return {"goal": "?", "needs_clarification": True, "clarification_question": "Which file?", "steps": []}
        return {"goal": "read", "steps": [{"id": "r", "tool": "read_workspace_file",
                                           "args": {"path": clarification["answer"]}}]}

    k = build_kernel(env, plan_fn=fn)
    pid = run_to_review(k)
    assert k.proc(pid)["pending_user"] == {"kind": "clarify", "question": "Which file?"}
    k.answer(pid, "README.md")
    k.run_until_idle()
    assert k.proc(pid)["exit"]["code"] == "completed"


def test_execution_budget(env):
    steps = [{"id": f"r{i}", "tool": "read_workspace_file", "args": {"path": "README.md"}} for i in range(6)]
    k = build_kernel(env, {"goal": "many", "steps": steps}, budget=Budget(max_executions=3), fork_enabled=False)
    pid = run_to_review(k)
    p = k.proc(pid)
    assert p["exit"]["code"] == "budget_exceeded"
    assert p["usage"]["executions"] == 3


def test_wall_clock_deadline(env):
    steps = [{"id": f"r{i}", "tool": "read_workspace_file", "args": {"path": "README.md"}} for i in range(10)]
    k = build_kernel(env, {"goal": "slow", "steps": steps}, budget=Budget(wall_clock_s=2.0), fork_enabled=False)
    pid = run_to_review(k)
    assert k.proc(pid)["exit"]["code"] == "budget_exceeded"


def test_capability_scope_limits_a_process(env):
    plan = {"goal": "read outside scope", "steps": [{"id": "r", "tool": "read_workspace_file",
                                                     "args": {"path": "src/app.py"}}]}
    k = build_kernel(env, plan, budget=Budget(max_replans=0))
    pid = k.spawn("x")
    # narrow the process's fs authority to docs/ before it runs
    for cap in k.caps.held(pid):
        if cap.kind == "fs":
            k.caps.revoke(cap.cap_id)
    k.caps.grant(pid, "fs", "docs", ["read", "write"])
    k.run_until_idle()
    p = k.proc(pid)
    assert p["exit"]["code"] == "failed"
    assert "capability" in p["exit"]["reason"].lower()


# ------------------------------------------------------------- restart safety
def test_pending_review_survives_restart(env):
    k = build_kernel(env, REPORT_PLAN)
    pid = run_to_review(k)
    k.db.close()
    k2 = build_kernel(env, REPORT_PLAN)
    assert k2.proc(pid)["pending_user"]["kind"] == "review"
    k2.commit(pid)
    k2.run_until_idle()
    assert k2.proc(pid)["exit"]["code"] == "completed"


def test_threaded_loop_with_real_io(env):
    k = build_kernel(env, REPORT_PLAN, sim=False)
    k.start()
    try:
        pid = k.spawn("report")
        deadline = time.time() + 20
        while time.time() < deadline and (k.proc(pid)["pending_user"] or {}).get("kind") != "review":
            time.sleep(0.02)
        assert k.proc(pid)["pending_user"]["kind"] == "review"
        k.commit(pid)
        while time.time() < deadline and k.proc(pid)["state"] != "EXITED":
            time.sleep(0.02)
        assert k.proc(pid)["exit"]["code"] == "completed"
    finally:
        k.stop()


# ------------------------------------------------- policy outside the kernel
def test_edit_still_requires_approval_without_a_transaction(env):
    from agentic_os.tools.registry import build_default_registry

    reg = build_default_registry()
    pe = PolicyEngine(reg)
    ctx = ToolContext(task_id="t", step_id="s", workspace=WorkspaceGuard(env.workspace), os=None, apps=None,
                      user_settings={})
    step = {"id": "e", "tool": "edit_workspace_file"}
    args = {"path": "docs/notes.md", "operation": "append", "text": "more"}
    plain = pe.evaluate(task_id="t", plan_version=1, step=step, args=args, ctx=ctx, user_settings={})
    assert plain.decision == "require_approval"
    staged = pe.evaluate(task_id="t", plan_version=1, step=step, args=args, ctx=ctx, user_settings={},
                         deferred_capabilities={"workspace.write"})
    assert staged.decision == "allow" and "P016_deferred_to_commit" in staged.rules
    strict = pe.evaluate(task_id="t", plan_version=1, step=step, args=args, ctx=ctx,
                         user_settings={"require_approval_for_writes": True}, deferred_capabilities={"workspace.write"})
    assert strict.decision == "require_approval"


# ------------------------------------------------------------------ $ref data flow
def test_step_outputs_flow_through_refs(env):
    from .harness import REF_REPORT_PLAN

    k = build_kernel(env, REF_REPORT_PLAN, seed=4)
    pid = run_to_review(k)
    p = k.proc(pid)
    assert p["pending_user"]["kind"] == "review", p
    files = {f["path"]: f for f in p["pending_user"]["files"]}
    assert "project_report.md" in files
    assert "README.md" in files["project_report.md"]["diff"]          # the listing really reached the report
    k.commit(pid)
    k.run_until_idle()
    with open(os.path.join(env.workspace, "project_report.md"), encoding="utf-8") as fh:
        assert fh.read().startswith("# Project Report")


def test_bad_reference_is_rejected_by_validation(env):
    plan = {"goal": "bad", "steps": [{"id": "save", "tool": "create_workspace_file",
                                       "args": {"path": "x.md", "content": {"$ref": "later.output.markdown"}}}]}
    k = build_kernel(env, plan, budget=Budget(max_replans=0))
    pid = run_to_review(k)
    assert k.proc(pid)["exit"]["code"] == "failed"
    assert "plan.invalid" in events(k, pid)


# ---------------------------------------------------------------- RAG inside the kernel
def test_search_knowledge_tool_respects_capabilities(env):
    from agentic_os.rag import KnowledgeBase, LocalHashEmbedder
    from agentic_os.tools.pathguard import WorkspaceGuard

    plan = {"goal": "kb", "steps": [{"id": "kb", "tool": "search_knowledge", "args": {"query": "hello main"}}]}
    k = build_kernel(env, plan)
    kb = KnowledgeBase(k.db, LocalHashEmbedder())
    kb.index_workspace(WorkspaceGuard(env.workspace))
    k.services["rag"] = kb
    pid = k.spawn("x")
    for cap in k.caps.held(pid):          # restrict this process to docs/
        if cap.kind == "fs":
            k.caps.revoke(cap.cap_id)
    k.caps.grant(pid, "fs", "docs", ["read"])
    k.run_until_idle()
    p = k.proc(pid)
    assert p["exit"]["code"] == "completed"
    paths = {h["path"] for h in p["steps_state"]["kb"]["summary"]["hits"]}
    assert paths and all(path.startswith("docs/") for path in paths)
