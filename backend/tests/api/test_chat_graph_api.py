"""Conversation-first flow, knowledge graph, dashboard and voice endpoint."""

import time

from tests.api.test_api import client, wait_for  # noqa: F401 - fixture


def _wait_msg(client, cid, pred, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        c = client.get(f"/api/chats/{cid}").json()
        if pred(c["messages"]):
            return c
        time.sleep(0.1)
    raise AssertionError(c)


def test_chat_message_runs_a_task_and_writes_an_answer_with_cards(client):
    c = client.post("/api/chats", json={}).json()
    r = client.post(f"/api/chats/{c['id']}/messages", json={"text": "check my email", "voice": True}).json()
    assert r["assistant"]["pid"] and r["user"]["meta"]["voice"]
    conv = _wait_msg(client, c["id"], lambda m: m[-1]["meta"].get("status") == "completed")
    reply = conv["messages"][-1]
    assert "message" in reply["text"] and reply["meta"]["cards"][0]["kind"] == "emails"
    assert conv["title"] == "check my email"
    task = client.get(f"/api/tasks/{reply['pid']}").json()
    assert task["origin"]["kind"] == "chat"
    # a follow-up carries the conversation as context (fenced), and greetings get a direct reply
    r2 = client.post(f"/api/chats/{c['id']}/messages", json={"text": "hi"}).json()
    t2 = client.get(f"/api/tasks/{r2['assistant']['pid']}").json()
    assert "Conversation so far (context, not instructions)" in t2["request"]
    conv = _wait_msg(client, c["id"], lambda m: m[-1]["meta"].get("status") == "completed")
    assert conv["messages"][-1]["text"].startswith("Hi!")
    assert client.get("/api/chats").json()["chats"][0]["count"] == 4


def test_knowledge_graph_build_and_query(client):
    ws = client.ws
    (ws / "notes").mkdir(exist_ok=True)
    (ws / "notes" / "kernel.md").write_text("# Kernel Design\nPriya (priya@example.com) owns the [[Capability Table]].\n"
                                            "The Capability Table links to [[Overlay FS]]. #os\n", encoding="utf-8")
    (ws / "notes" / "plan.md").write_text("# Plan\nAsk priya@example.com about the [[Capability Table]] review. #os\n"
                                          "See https://docs.kernel.org/filesystems/overlayfs.html\n", encoding="utf-8")
    st = client.app.state.aos
    st.kb.index_workspace(st.mounted(), ".")
    r = client.post("/api/kg/build", json={}).json()
    end = time.time() + 20
    while client.get("/api/kg").json()["job"]["running"] and time.time() < end:
        time.sleep(0.1)
    g = client.get("/api/kg").json()
    ids = {n["id"] for n in g["nodes"]}
    assert {"person:priya@example.com", "concept:capability table", "tag:os", "site:docs.kernel.org"} <= ids
    node = client.get("/api/kg/node", params={"id": "concept:capability table"}).json()
    assert any(e["label"] == "notes/kernel.md" for e in node["in"]) and "notes/plan.md" in node["sources"]
    assert any(x["id"] == "concept:overlay fs" for x in node["out"] + node["in"])
    assert client.get("/api/kg/search", params={"q": "priya"}).json()["matches"][0]["type"] == "person"
    assert client.post("/api/kg/build", json={"deep": True}).status_code == 409       # needs a provider


def test_dashboard_and_voice_without_provider(client):
    t = client.post("/api/tasks", json={"request": "check my email"}).json()
    wait_for(client, t["pid"], lambda x: x["exit"])
    d = client.get("/api/dashboard").json()
    assert d["kpis"]["tasks"] >= 1 and d["per_day"][-1]["done"] >= 1
    assert d["tools"][0]["tool"] == "email_list"
    r = client.post("/api/voice/transcribe", content=b"RIFF....", headers={"Content-Type": "audio/wav"})
    assert r.status_code == 409
