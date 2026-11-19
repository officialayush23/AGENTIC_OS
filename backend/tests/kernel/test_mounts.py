import os

import pytest

from agentic_os.kernel import Budget

from .harness import build_kernel, make_env, snapshot


class Crash(BaseException):
    pass


@pytest.fixture()
def env(tmp_path):
    e = make_env(tmp_path)
    e.home = os.path.join(str(tmp_path), "home")
    os.makedirs(os.path.join(e.home, "Notes"))
    with open(os.path.join(e.home, "Notes", "ideas.md"), "w") as fh:
        fh.write("# Ideas\n- first\n")
    return e


PLAN = {"goal": "both mounts", "steps": [
    {"id": "note", "tool": "create_workspace_file", "args": {"path": "~/Notes/meeting.md", "content": "# Meeting\n"}},
    {"id": "edit", "tool": "edit_workspace_file",
     "args": {"path": "~/Notes/ideas.md", "operation": "append", "text": "- second\n"}},
    {"id": "ws", "tool": "create_workspace_file", "args": {"path": "summary.md", "content": "done\n"}},
    {"id": "list", "tool": "list_workspace_files", "args": {"path": "~/Notes"}},
]}


def test_one_transaction_spans_both_mounts(env):
    ws0, home0 = snapshot(env.workspace), snapshot(env.home)
    k = build_kernel(env, PLAN, home_root=env.home)
    pid = k.spawn("x")
    k.run_until_idle()
    p = k.proc(pid)
    assert p["pending_user"]["kind"] == "review", p["exit"]
    assert {f["path"] for f in p["pending_user"]["files"]} == {"~/Notes/meeting.md", "~/Notes/ideas.md", "summary.md"}
    listing = {e["path"] for e in p["steps_state"]["list"]["summary"]["entries"]}
    assert {"~/Notes/meeting.md", "~/Notes/ideas.md"} <= listing          # merged view inside the home mount
    assert snapshot(env.workspace) == ws0 and snapshot(env.home) == home0   # nothing written yet
    k.commit(pid)
    k.run_until_idle()
    assert open(os.path.join(env.home, "Notes", "ideas.md")).read().endswith("- second\n")
    assert os.path.exists(os.path.join(env.workspace, "summary.md"))
    k.undo(pid)
    assert snapshot(env.workspace) == ws0 and snapshot(env.home) == home0


def test_home_needs_capability(env):
    plan = {"goal": "read home", "steps": [{"id": "r", "tool": "read_workspace_file", "args": {"path": "~/Notes/ideas.md"}}]}
    k = build_kernel(env, plan, home_root=env.home, budget=Budget(max_replans=0))
    pid = k.spawn("x")
    for cap in k.caps.held(pid):
        if cap.kind == "fs":
            k.caps.revoke(cap.cap_id)
    k.caps.grant(pid, "fs", "docs", ["read"])
    k.run_until_idle()
    assert k.proc(pid)["exit"]["code"] == "failed"


def test_home_scoped_capability_allows_home_only(env):
    plan = {"goal": "read", "steps": [{"id": "r", "tool": "read_workspace_file", "args": {"path": "~/Notes/ideas.md"}},
                                     {"id": "w", "tool": "read_workspace_file", "args": {"path": "README.md"}}]}
    k = build_kernel(env, plan, home_root=env.home, budget=Budget(max_replans=0), fork_enabled=False)
    pid = k.spawn("x")
    for cap in k.caps.held(pid):
        if cap.kind == "fs":
            k.caps.revoke(cap.cap_id)
    k.caps.grant(pid, "fs", "~/Notes", ["read"])
    k.run_until_idle()
    p = k.proc(pid)
    assert p["steps_state"]["r"]["status"] == "done"
    assert p["exit"]["code"] == "failed" and "README.md" not in str(p["steps_state"]["r"])


@pytest.mark.parametrize("point", ["prepared", "commit_point", "applied:1", "applied:2"])
def test_crash_during_cross_mount_commit(env, point):
    def fault(p):
        if p == point:
            raise Crash(p)

    k = build_kernel(env, PLAN, home_root=env.home, journal_fault=fault)
    pid = k.spawn("x")
    k.run_until_idle()
    k.commit(pid)
    with pytest.raises(Crash):
        k.run_until_idle()
    k.db.close()
    k = build_kernel(env, PLAN, home_root=env.home)
    for _ in range(5):
        k.run_until_idle()
        p = k.proc(pid)
        if p["state"] == "EXITED":
            break
        if (p["pending_user"] or {}).get("kind") == "review":
            k.commit(pid)
    assert p["exit"]["code"] == "completed"
    assert open(os.path.join(env.home, "Notes", "ideas.md")).read().endswith("- second\n")
    assert os.path.exists(os.path.join(env.home, "Notes", "meeting.md"))
    assert os.path.exists(os.path.join(env.workspace, "summary.md"))
