"""Kernel monitor and the scripted OS demos."""

import time

from tests.api.test_api import client, wait_for  # noqa: F401 - fixture


def _demo(client, name):
    r = client.post(f"/api/kernel/demo/{name}")
    assert r.status_code == 200, r.text
    return r.json()["pids"]


def test_monitor_shape(client):
    k = client.get("/api/kernel").json()
    assert set(k) >= {"processes", "runq", "txns", "log", "counters", "states"}
    assert set(k["runq"]) == {"boosted", "normal", "background"}


def test_transaction_demo_commit_and_undo(client):
    pid = _demo(client, "transaction")[0]
    t = wait_for(client, pid, lambda t: t["state"] == "BLOCKED_USER")
    diff = client.get(f"/api/tasks/{pid}/diff").json()
    paths = {d["path"] for d in (diff.get("files") or diff.get("changes") or diff if isinstance(diff, list) else [])}
    assert "os-demo/transaction.md" in str(diff)
    assert client.post(f"/api/tasks/{pid}/commit").status_code == 200
    wait_for(client, pid, lambda t: t["state"] == "EXITED")
    k = client.get("/api/kernel").json()
    assert k["counters"]["commits"] >= 1 and k["txns"][0]["files"] >= 2
    assert client.post(f"/api/tasks/{pid}/undo").status_code == 200
    assert client.get("/api/kernel").json()["counters"]["undos"] >= 1


def test_fork_demo_spawns_children(client):
    pid = _demo(client, "fork")[0]
    seen_children = False
    end = time.time() + 20
    while time.time() < end:
        k = client.get("/api/kernel").json()
        if any(p["ppid"] == pid for p in k["processes"]):
            seen_children = True
        t = client.get(f"/api/tasks/{pid}").json()
        if t["state"] == "EXITED":
            break
        time.sleep(0.1)
    assert seen_children
    assert client.get("/api/kernel").json()["counters"]["forks"] >= 4


def test_signals_demo_stop_cont_term(client):
    pid = _demo(client, "signals")[0]
    wait_for(client, pid, lambda t: t["state"] in ("BLOCKED_IO", "READY", "RUNNING"))
    assert client.post(f"/api/tasks/{pid}/signal", json={"signal": "STOP"}).status_code == 200
    assert client.get(f"/api/tasks/{pid}").json()["state"] == "STOPPED"
    assert client.post(f"/api/tasks/{pid}/signal", json={"signal": "CONT"}).status_code == 200
    assert client.post(f"/api/tasks/{pid}/signal", json={"signal": "TERM"}).status_code == 200
    wait_for(client, pid, lambda t: t["state"] == "EXITED")


def test_rlimit_demo_is_terminated(client):
    pid = _demo(client, "rlimit")[0]
    t = wait_for(client, pid, lambda t: t["state"] == "EXITED", timeout=30)
    assert "budget" in str(t.get("exit")).lower()


def test_capability_demo_needs_an_app(client):
    r = client.post("/api/kernel/demo/capability")
    assert r.status_code in (200, 400)
    assert client.post("/api/kernel/demo/nope").status_code == 404


def test_capability_demo_blocks_for_a_grant(client):
    import sys
    assert client.put("/api/apps/notepad", json={"name": "Notepad", "path": sys.executable}).status_code == 200
    r = client.post("/api/kernel/demo/capability")
    assert r.status_code == 200, r.text
    pid = r.json()["pids"][0]
    t = wait_for(client, pid, lambda t: t["state"] in ("BLOCKED_USER", "EXITED"))
    assert t["state"] == "BLOCKED_USER", t.get("exit")
    h = t["pending_user"]["action_hash"]
    r = client.post(f"/api/tasks/{pid}/reject", json={"action_hash": h, "note": "demo"})
    assert r.status_code == 200, r.text
    # A rejected action goes back to the planner (recover phase); end the demo with TERM.
    time.sleep(0.3)
    if client.get(f"/api/tasks/{pid}").json()["state"] != "EXITED":
        client.post(f"/api/tasks/{pid}/signal", json={"signal": "TERM"})
    wait_for(client, pid, lambda t: t["state"] == "EXITED")
