"""API: Home notes, calendar, mail, schedules, listeners and the local webhook."""

import time

from tests.api.test_api import client, wait_for  # noqa: F401 - fixture


def test_status_reports_agentic_services(client):
    s = client.get("/api/status").json()
    assert s["mail"]["mode"] == "local" and s["automation"]["schedules"] == 0
    assert client.get("/api/home").json()["counts"]["~/Notes"] >= 1


def test_notes_crud_and_conflict(client):
    notes = client.get("/api/notes").json()["notes"]
    assert any(n["path"] == "~/Notes/Welcome.md" for n in notes)
    r = client.put("/api/notes/file", json={"title": "Shopping list", "content": "# Shopping list\n- milk\n"})
    assert r.status_code == 200, r.text
    path, sha = r.json()["path"], r.json()["sha256"]
    assert path == "~/Notes/shopping-list.md"
    got = client.get("/api/notes/file", params={"path": path}).json()
    assert got["content"].endswith("- milk\n") and got["title"] == "Shopping list"
    r = client.put("/api/notes/file", json={"path": path, "content": "x", "expected_sha256": "0" * 64})
    assert r.status_code == 409
    assert client.put("/api/notes/file", json={"path": path, "content": "# Shopping list\n- eggs\n",
                                               "expected_sha256": sha}).status_code == 200
    assert client.put("/api/notes/file", json={"path": "README.md", "content": "x"}).status_code == 400
    assert client.delete("/api/notes/file", params={"path": path}).json()["deleted"]
    assert client.get("/api/notes/file", params={"path": path}).status_code == 404


def test_calendar_add_list_delete(client):
    r = client.post("/api/calendar/events", json={"title": "Dentist", "start": "2030-01-02T09:00",
                                                  "duration_minutes": 30, "online_meeting": True})
    assert r.status_code == 200, r.text
    ev = r.json()
    assert ev["url"].startswith("https://meet.jit.si/")
    lst = client.get("/api/calendar", params={"start": "2030-01-01", "days": 7}).json()["events"]
    assert [e["title"] for e in lst] == ["Dentist"]
    assert client.delete(f"/api/calendar/events/{ev['uid']}").json()["deleted"]
    assert client.post("/api/calendar/events", json={"title": "x", "start": "nope"}).status_code == 400


def test_mail_local_inbox(client):
    msgs = client.get("/api/mail/messages").json()["messages"]
    assert len(msgs) == 3 and not msgs[0]["seen"]
    one = client.get("/api/mail/message", params={"id": msgs[0]["id"]}).json()
    assert one["body"]
    assert client.get("/api/mail/messages", params={"unread_only": True}).json()["messages"].__len__() == 2
    assert client.get("/api/mail/messages", params={"q": "planning"}).json()["messages"][0]["subject"].startswith("Notes")
    r = client.put("/api/mail/config", json={"mode": "imap"})
    assert r.status_code == 400
    r = client.post("/api/mail/password", json={"password": "pw"})
    assert r.json()["password_stored"]


def test_schedules_crud_preview_and_run_now(client):
    p = client.post("/api/schedules/preview", json={"kind": "cron", "spec": "30 9 * * 1-5"}).json()
    assert p["label"] == "Weekdays at 09:30" and len(p["next_runs"]) == 3
    assert client.post("/api/schedules/preview", json={"kind": "cron", "spec": "bad"}).status_code == 400
    s = client.post("/api/schedules", json={"name": "Inbox digest", "request": "list files", "kind": "cron",
                                            "spec": "0 8 * * *"}).json()
    assert s["enabled"] and s["next_run"]
    s = client.put(f"/api/schedules/{s['id']}", json={"enabled": False}).json()
    assert not s["enabled"]
    pid = client.post(f"/api/schedules/{s['id']}/run").json()["pid"]
    task = wait_for(client, pid, lambda t: t["exit"] or t["pending_user"])
    assert task["origin"]["kind"] == "schedule"
    assert client.get("/api/automation/runs", params={"source_id": s["id"]}).json()["runs"][0]["pid"] == pid
    assert client.delete(f"/api/schedules/{s['id']}").json()["deleted"]


def test_webhook_listener_starts_task(client):
    r = client.post("/api/listeners", json={"name": "CI hook", "kind": "webhook", "request": "Look at {repo}",
                                            "cooldown_s": 5})
    assert r.status_code == 200, r.text
    lst = r.json()
    secret = lst.pop("secret")
    assert secret and lst["has_secret"]
    assert "secret" not in client.get("/api/listeners").json()["listeners"][0]
    hdr = {"Host": "127.0.0.1:8765"}
    bad = client.post(f"/hooks/{lst['id']}", json={"repo": "x"}, headers={**hdr, "X-Hook-Secret": "wrong"})
    assert bad.status_code == 404
    ok = client.post(f"/hooks/{lst['id']}", json={"repo": "agentic-os"}, headers={**hdr, "X-Hook-Secret": secret})
    assert ok.status_code == 200 and ok.json()["accepted"], ok.text
    task = wait_for(client, ok.json()["pid"], lambda t: t["exit"] or t["pending_user"])
    assert task["origin"] == {"kind": "listener", "id": lst["id"], "name": "CI hook", "trigger": "webhook"}
    assert task["request"].startswith("Look at agentic-os")
    again = client.post(f"/hooks/{lst['id']}", json={}, headers={**hdr, "X-Hook-Secret": secret}).json()
    assert not again["accepted"]                                          # cooldown
    cross = client.post(f"/hooks/{lst['id']}", json={}, headers={**hdr, "X-Hook-Secret": secret,
                                                                  "Origin": "https://evil.example"})
    assert cross.status_code == 403


def test_file_listener_validation(client):
    r = client.post("/api/listeners", json={"kind": "file", "request": "x", "config": {"path": "nope/missing"}})
    assert r.status_code == 400
    r = client.post("/api/listeners", json={"kind": "file", "request": "x", "config": {"path": "~/Notes"}})
    assert r.status_code == 200


def test_task_with_explicit_plan_is_still_validated_and_policy_checked(client):
    plan = {"goal": "send", "steps": [{"id": "s", "tool": "email_send",
                                       "args": {"to": ["a@example.com"], "subject": "Hi", "body": "x"}}]}
    t = client.post("/api/tasks", json={"request": "send hi", "plan": plan}).json()
    t = wait_for(client, t["pid"], lambda x: x["pending_user"] or x["exit"])
    assert t["pending_user"]["kind"] == "approval" and t["pending_user"]["grant"]["kind"] == "mail"
    bad = {"goal": "x", "steps": [{"id": "s", "tool": "run_shell", "args": {"cmd": "rm -rf /"}}]}
    t = client.post("/api/tasks", json={"request": "bad", "plan": bad}).json()
    t = wait_for(client, t["pid"], lambda x: x["pending_user"] or x["exit"])
    assert not (t["exit"] or {}).get("code") == "completed"
