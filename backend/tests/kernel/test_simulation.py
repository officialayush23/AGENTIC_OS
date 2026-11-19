"""Deterministic simulation tests.

Every run is reproducible from its seed. The simulated I/O driver completes
requests in a seeded order and, for a fraction of requests, performs the side
effect immediately but delivers the completion later, so a crash can land
*between* "the file was written" and "the kernel heard about it".

A crash is modelled by abandoning the kernel object (and its in-flight
completions) at a random tick, then booting a fresh kernel on the same
database, data folder and workspace.
"""

import os
import random

import pytest

from agentic_os.kernel import SimClock

from .harness import REPORT_PLAN, build_kernel, make_env, snapshot

SEEDS = int(os.environ.get("AOS_SIM_SEEDS", "60"))


class Crash(BaseException):  # a power cut cannot be caught by the kernel
    pass


def drive(kernel, ticks):
    """Advance the kernel by up to `ticks` units of progress."""
    for _ in range(ticks):
        if not kernel.tick():
            if not kernel.io.pending():
                return
            for c in kernel.io.wait(timeout=0.5):
                kernel._deliver(c)


def finish(kernel, pid):
    """Run to completion, committing when asked to review."""
    for _ in range(10):
        kernel.run_until_idle()
        p = kernel.proc(pid)
        if p["state"] == "EXITED":
            return p
        if (p["pending_user"] or {}).get("kind") == "review":
            kernel.commit(pid)
    raise AssertionError(f"process did not finish: {p['state']} {p['phase']}")


def reference_result(tmp_path):
    env = make_env(tmp_path / "ref")
    k = build_kernel(env, REPORT_PLAN, seed=0)
    pid = k.spawn("report")
    finish(k, pid)
    return snapshot(env.workspace)


@pytest.fixture(scope="module")
def expected(tmp_path_factory):
    return reference_result(tmp_path_factory.mktemp("reference"))


@pytest.mark.parametrize("seed", range(SEEDS))
def test_crash_anywhere_converges(tmp_path, expected, seed):
    rng = random.Random(seed)
    env = make_env(tmp_path)
    original = snapshot(env.workspace)
    clock = SimClock()
    fork = rng.random() < 0.7
    k = build_kernel(env, REPORT_PLAN, seed=seed, effect_at_issue=0.5, clock=clock, fork_enabled=fork)
    pid = k.spawn("report")
    crashes = rng.randint(1, 3)
    for _ in range(crashes):
        drive(k, rng.randint(1, 40))
        p = k.proc(pid)
        if p["state"] != "EXITED" and (p["pending_user"] or {}).get("kind") != "review":
            assert snapshot(env.workspace) == original, f"seed {seed}: workspace changed before commit"
        k.io.crash()
        k.db.close()
        k = build_kernel(env, REPORT_PLAN, seed=seed + 1000, effect_at_issue=0.5, clock=clock, fork_enabled=fork)
    p = finish(k, pid)
    assert p["exit"]["code"] == "completed", f"seed {seed}: {p['exit']}"
    assert snapshot(env.workspace) == expected, f"seed {seed}: diverged from the crash-free run"
    k.undo(pid)
    assert snapshot(env.workspace) == original, f"seed {seed}: undo did not restore the original"


@pytest.mark.parametrize("seed", range(min(SEEDS, 30)))
def test_crash_during_commit_converges(tmp_path, expected, seed):
    rng = random.Random(seed)
    env = make_env(tmp_path)
    clock = SimClock()
    points = ["prepared", "commit_point", "applied:1", "applied:2"]
    target = rng.choice(points)

    def fault(point):
        if point == target:
            raise Crash(point)

    k = build_kernel(env, REPORT_PLAN, seed=seed, clock=clock, journal_fault=fault)
    pid = k.spawn("report")
    k.run_until_idle()
    k.commit(pid)
    with pytest.raises(Crash):
        k.run_until_idle()
    k.db.close()
    k = build_kernel(env, REPORT_PLAN, seed=seed, clock=clock)    # no fault this time
    p = finish(k, pid)
    assert p["exit"]["code"] == "completed", f"seed {seed} @ {target}: {p['exit']}"
    assert snapshot(env.workspace) == expected, f"seed {seed} @ {target}"


@pytest.mark.parametrize("seed", range(min(SEEDS, 20)))
def test_concurrent_tasks_are_isolated(tmp_path, seed):
    """Two tasks staging different edits never see each other's uncommitted writes."""
    env = make_env(tmp_path)
    plan_a = {"goal": "a", "steps": [{"id": "w", "tool": "create_workspace_file",
                                       "args": {"path": "a.txt", "content": "A\n"}}]}
    plan_b = {"goal": "b", "steps": [
        {"id": "w", "tool": "create_workspace_file", "args": {"path": "b.txt", "content": "B\n"}},
        {"id": "r", "tool": "list_workspace_files", "args": {"path": "."}},
    ]}

    def fn(request, feedback, completed, clar):
        return plan_a if request == "a" else plan_b

    k = build_kernel(env, plan_fn=fn, seed=seed)
    pa, pb = k.spawn("a"), k.spawn("b")
    k.run_until_idle()
    listing = k.proc(pb)["steps_state"]["r"]["summary"]
    names = {e["path"] for e in listing["entries"]}
    assert "b.txt" in names and "a.txt" not in names
    assert {f["path"] for f in k.proc(pa)["pending_user"]["files"]} == {"a.txt"}
    k.commit(pa)
    k.commit(pb)
    k.run_until_idle()
    assert os.path.exists(os.path.join(env.workspace, "a.txt")) and os.path.exists(os.path.join(env.workspace, "b.txt"))
