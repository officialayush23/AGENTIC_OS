import os
import time

import pytest
from fastapi.testclient import TestClient

from agentic_os.api.app import create_app
from agentic_os.api.state import AppState
from agentic_os.config import Settings
from agentic_os.kernel import ThreadPoolIO
from agentic_os.security.secrets import MemorySecretStore

from tests.kernel.harness import FakeOS, make_workspace

TOKEN = "t" * 48
HDR = {"X-Agentic-OS": "1", "Authorization": f"Bearer {TOKEN}", "Host": "127.0.0.1:8765"}


@pytest.fixture()
def client(tmp_path):
    data = tmp_path / "data"
    ws = tmp_path / "ws"
    ws.mkdir()
    make_workspace(str(ws))
    settings = Settings(data_dir=data, bearer_token=TOKEN, ui_dist_dir=None, secret_store_backend="memory",
                        enable_fake_provider=True)
    state = AppState(settings, secrets=MemorySecretStore(), io_factory=lambda: ThreadPoolIO(2), os_adapter=FakeOS())
    state.repos.settings.set("workspace_root", str(ws))
    state.repos.settings.set("rag_auto_index", False)
    state.kernel.stop()
    state._build_kernel()
    app = create_app(settings, state)
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        c.headers.update(HDR)
        c.ws = ws
        yield c


def wait_for(client, pid, pred, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        t = client.get(f"/api/tasks/{pid}").json()
        if pred(t):
            return t
        time.sleep(0.05)
    raise AssertionError(f"timed out: {t['state']} {t['phase']}")


def test_security_requires_header_token_and_host(client):
    assert client.get("/health").status_code == 200
    assert client.get("/api/status", headers={"X-Agentic-OS": ""}).status_code == 403
    assert client.get("/api/status", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/status", headers={"Host": "evil.example:8765"}).status_code == 403
    assert client.get("/api/status", headers={"Origin": "https://evil.example"}).status_code == 403
    r = client.get("/api/status", headers={"Origin": "http://tauri.localhost"})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "http://tauri.localhost"


def test_cors_preflight_for_desktop_webview(client):
    r = client.options("/api/tasks", headers={"Origin": "http://tauri.localhost", "Access-Control-Request-Method": "POST",
                                              "Access-Control-Request-Headers": "x-agentic-os,authorization,content-type"})
    assert r.status_code == 200


def test_task_lifecycle_over_http(client):
    r = client.post("/api/tasks", json={"request": "Write a project report"})
    assert r.status_code == 200, r.text
    pid = r.json()["pid"]
    t = wait_for(client, pid, lambda t: (t.get("pending_user") or {}).get("kind") == "review")
    assert {f["path"] for f in t["pending_user"]["files"]} == {"reports/project_report.md"}
    assert not (client.ws / "reports").exists()
    diff = client.get(f"/api/tasks/{pid}/diff").json()["files"]
    assert diff and diff[0]["op"] == "create"
    assert client.post(f"/api/tasks/{pid}/commit").status_code == 200
    t = wait_for(client, pid, lambda t: t["state"] == "EXITED")
    assert t["exit"]["code"] == "completed" and (client.ws / "reports" / "project_report.md").exists()
    events = client.get(f"/api/tasks/{pid}/events").json()["events"]
    assert any(e["type"] == "txn.committed" for e in events)
    assert client.post(f"/api/tasks/{pid}/undo").status_code == 200
    assert not (client.ws / "reports").exists()
    tasks = client.get("/api/tasks").json()["tasks"]
    assert tasks[0]["pid"] == pid


def test_clarification_and_errors(client):
    pid = client.post("/api/tasks", json={"request": "make me a sandwich"}).json()["pid"]
    t = wait_for(client, pid, lambda t: (t.get("pending_user") or {}).get("kind") == "clarify")
    assert "AI provider" in t["pending_user"]["question"]
    r = client.post(f"/api/tasks/{pid}/commit")
    assert r.status_code == 409 and r.json()["error"]["code"] == "NO_PENDING_REVIEW"
    assert client.get("/api/tasks/proc_nope").status_code == 404
    client.post(f"/api/tasks/{pid}/signal", json={"signal": "TERM"})
    assert wait_for(client, pid, lambda t: t["state"] == "EXITED")["exit"]["code"] == "cancelled"


def test_knowledge_base_endpoints(client):
    r = client.post("/api/kb/index", json={"path": "."})
    assert r.status_code == 200
    end = time.time() + 20
    while client.get("/api/kb/status").json()["index_job"]["running"] and time.time() < end:
        time.sleep(0.05)
    st = client.get("/api/kb/status").json()
    assert st["documents"] >= 4 and st["index_job"]["result"]["indexed"] >= 4
    paths = {d["path"] for d in client.get("/api/kb/documents").json()["documents"]}
    assert ".env" not in paths
    hits = client.post("/api/kb/search", json={"query": "TODO write docs"}).json()["hits"]
    assert hits[0]["path"] == "docs/notes.md"
    client.post("/api/kb/notes", json={"title": "Decision", "text": "We picked SQLite for local storage."})
    ans = client.post("/api/kb/ask", json={"question": "what storage did we pick?"}).json()
    assert ans["mode"] == "retrieval_only" and ans["sources"][0]["source"] == "note"


def test_settings_and_workspace_switch(client, tmp_path):
    s = client.get("/api/settings").json()
    assert s["settings"]["workspace_root"] == str(client.ws)
    other = tmp_path / "other"
    other.mkdir()
    (other / "a.txt").write_text("x")
    r = client.put("/api/settings", json={"workspace_root": str(other), "planner_mode": "rules"})
    assert r.status_code == 200 and r.json()["settings"]["workspace_root"] == os.path.realpath(other)
    assert client.put("/api/settings", json={"workspace_root": str(tmp_path / "missing")}).status_code == 400
    assert client.put("/api/settings", json={"rag_embedder": {"kind": "bogus"}}).status_code == 400
    r = client.put("/api/settings", json={"rag_embedder": {"kind": "local", "dim": 256}})
    assert r.status_code == 200 and client.get("/api/kb/status").json()["embedder"] == "local:hash-256-v1"


def test_workspace_switch_blocked_while_task_waits(client, tmp_path):
    pid = client.post("/api/tasks", json={"request": "Write a project report"}).json()["pid"]
    wait_for(client, pid, lambda t: (t.get("pending_user") or {}).get("kind") == "review")
    other = tmp_path / "o2"
    other.mkdir()
    r = client.put("/api/settings", json={"workspace_root": str(other)})
    assert r.status_code == 409


def test_providers_and_llm_planner(client):
    kinds = client.get("/api/providers").json()["kinds"]
    assert "openai" in kinds
    r = client.put("/api/providers/fake1", json={"kind": "fake", "display_name": "Fake", "model": "fake-1"})
    assert r.status_code == 200, r.text
    client.post("/api/providers/fake1/default")
    st = client.get("/api/status").json()
    assert st["planner"]["llm_provider"] == "fake1"
    bad = client.put("/api/providers/x1", json={"kind": "openai", "model": "gpt", "base_url": "http://evil.example"})
    assert bad.status_code == 400 and bad.json()["error"]["details"]["field"] == "base_url"


def test_sse_stream_delivers_task_events(client):
    pid = client.post("/api/tasks", json={"request": "read docs/notes.md"}).json()["pid"]
    t = wait_for(client, pid, lambda t: t["state"] == "EXITED")
    with client.stream("GET", f"/api/events?task_id={t['task_id']}&follow=false") as resp:
        assert resp.status_code == 200
        seen = []
        for line in resp.iter_lines():
            if line.startswith("data: "):
                seen.append(line)
            if len(seen) >= 3:
                break
    assert any("proc.spawn" in s for s in seen)
