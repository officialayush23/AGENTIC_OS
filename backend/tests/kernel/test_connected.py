"""Google (OAuth, Calendar + Meet, Gmail API) and Notion, against a fake HTTP server; app discovery."""

import base64
import json
import os
import urllib.parse

import pytest

from agentic_os.security.secrets import MemorySecretStore
from agentic_os.services.google import GmailTransport, GoogleAuth, GoogleCalendar, GoogleError, event_id_for
from agentic_os.services.http import HttpClient
from agentic_os.services.mail import build_message
from agentic_os.services.notion import NotionClient, NotionError

from .harness import build_kernel, make_env


class FakeGoogle(HttpClient):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.events = {}
        self.sent = []
        self.refresh_ok = True

    def send(self, method, url, headers, body):
        self.calls.append((method, url))
        u = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        js = lambda o, s=200: (s, json.dumps(o).encode())  # noqa: E731
        if u.path == "/token":
            form = dict(urllib.parse.parse_qsl(body.decode()))
            if form["grant_type"] == "authorization_code":
                assert form["code_verifier"] and form["code"] == "good-code"
                return js({"access_token": "at1", "refresh_token": "rt1", "expires_in": 3600,
                           "scope": "openid email https://www.googleapis.com/auth/calendar.events "
                                    "https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/gmail.send"})
            return js({"access_token": "at2", "expires_in": 3600}) if self.refresh_ok else js({"error": "invalid_grant"}, 400)
        if u.path == "/revoke":
            return js({})
        assert headers.get("Authorization", "").startswith("Bearer at")
        if u.path == "/v1/userinfo":
            return js({"email": "me@gmail.com"})
        if u.path.endswith("/calendars/primary/events") and method == "POST":
            ev = json.loads(body)
            if ev["id"] in self.events:
                return js({"error": {"message": "The requested identifier already exists."}}, 409)
            ev["hangoutLink"] = "https://meet.google.com/abc-defg-hij" if "conferenceData" in ev else None
            ev["htmlLink"] = "https://calendar.google.com/x"
            ev["sendUpdates"] = q.get("sendUpdates")
            self.events[ev["id"]] = ev
            return js(ev)
        if "/calendars/primary/events/" in u.path:
            eid = u.path.rsplit("/", 1)[-1]
            return js(self.events[eid]) if eid in self.events else js({"error": {"message": "Not Found"}}, 404)
        if u.path.endswith("/calendars/primary/events"):
            return js({"items": list(self.events.values())})
        if u.path.endswith("/messages/send"):
            self.sent.append(json.loads(body)["raw"])
            return js({"id": f"g{len(self.sent)}"})
        if u.path.endswith("/messages") and method == "GET":
            if "rfc822msgid" in q.get("q", ""):
                hit = any(q["q"].split(":", 1)[1] in base64.urlsafe_b64decode(r + "==").decode() for r in self.sent)
                return js({"messages": [{"id": "g1"}]} if hit else {})
            return js({"messages": [{"id": "m1"}]})
        if "/messages/m1" in u.path:
            raw = build_message(sender="a@b.com", to=["me@gmail.com"], subject="Hi from Gmail", body="hello").as_bytes()
            return js({"raw": base64.urlsafe_b64encode(raw).decode(), "labelIds": ["INBOX", "UNREAD"]})
        return js({"error": {"message": f"unexpected {method} {u.path}"}}, 500)


@pytest.fixture()
def google():
    store = {}
    http = FakeGoogle()
    auth = GoogleAuth(lambda: {"google": store.get("g", {})}, lambda c: store.__setitem__("g", c), MemorySecretStore(), http=http)
    return auth, http


def test_google_oauth_pkce_flow_and_refresh(google):
    auth, http = google
    with pytest.raises(GoogleError):
        auth.start("http://127.0.0.1:8765/oauth/google/callback", ["calendar"])
    with pytest.raises(GoogleError):
        auth.configure("not-a-client-id", "s")
    auth.configure("123-abc.apps.googleusercontent.com", "GOCSPX-secret")
    url = auth.start("http://127.0.0.1:8765/oauth/google/callback", ["calendar", "gmail"])
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert q["code_challenge_method"] == "S256" and q["access_type"] == "offline"
    assert "calendar.events" in q["scope"] and "gmail.send" in q["scope"]
    with pytest.raises(GoogleError):
        auth.finish("wrong-state", "good-code")
    st = auth.finish(q["state"], "good-code")
    assert st["connected"] and st["email"] == "me@gmail.com" and set(st["services"]) == {"calendar", "gmail"}
    with pytest.raises(GoogleError):
        auth.finish(q["state"], "good-code")                      # one-time state
    auth._token = None
    assert auth.access_token() == "at2"                           # refreshed
    http.refresh_ok = False
    auth._token = None
    with pytest.raises(GoogleError) as e:
        auth.access_token()
    assert e.value.code == "GOOGLE_RECONNECT"


def _connect(auth):
    auth.configure("123-abc.apps.googleusercontent.com", "GOCSPX-secret")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(auth.start("http://x/cb", ["calendar", "gmail"])).query))
    auth.finish(q["state"], "good-code")


def test_gmail_transport(google):
    auth, http = google
    _connect(auth)
    t = GmailTransport(auth)
    msgs = t.list("INBOX", 5)
    assert msgs[0]["subject"] == "Hi from Gmail" and not msgs[0]["seen"]
    msg = build_message(sender="me@gmail.com", to=["x@y.com"], subject="s", body="b", message_id="<aos-1@agentic-os.local>")
    assert not t.sent_contains("<aos-1@agentic-os.local>")
    assert t.send(msg)["delivered"]
    assert t.sent_contains("<aos-1@agentic-os.local>")


class _G:
    def __init__(self, auth):
        self.auth = auth
        self.calendar = GoogleCalendar(auth)


def test_gcal_create_with_meet_needs_one_shot_approval_and_is_idempotent(tmp_path, google):
    auth, http = google
    _connect(auth)
    env = make_env(tmp_path)
    plan = {"goal": "meet", "steps": [{"id": "ev", "tool": "gcal_create_event", "args": {
        "title": "Design review", "start": "2026-11-20T15:00", "duration_minutes": 45,
        "attendees": ["priya@example.com"], "google_meet": True}}]}
    k = build_kernel(env, plan, services={"google": _G(auth)})
    pid = k.spawn("meet priya")
    k.run_until_idle()
    p = k.proc(pid)
    pend = p["pending_user"]
    assert pend["kind"] == "approval" and pend["grant"]["kind"] == "google" and pend["irreversible"]
    assert pend["preview"]["type"] == "event" and pend["preview"]["meet"] is True
    assert not http.events                                          # nothing created before approval
    k.approve(pid, pend["action_hash"])
    k.run_until_idle()
    p = k.proc(pid)
    assert p["exit"]["code"] == "completed", p["exit"]
    out = k.store.get_output(pid, "ev")
    assert out["url"].startswith("https://meet.google.com/")
    ev = http.events[event_id_for(f"{p['task_id']}:ev")]
    assert ev["sendUpdates"] == "all" and ev["conferenceData"]["createRequest"]["conferenceSolutionKey"]["type"] == "hangoutsMeet"
    # a retried create (e.g. after a crash) finds the same event instead of duplicating it
    again = GoogleCalendar(auth).create_event(event_id=ev["id"], title="x", start="2026-11-20T15:00", end="2026-11-20T15:45",
                                              attendees=[], description=None, location=None, meet=False, notify=False)
    assert again["already_existed"] and len(http.events) == 1


class FakeNotion(HttpClient):
    def __init__(self):
        super().__init__()
        self.pages = {"1a2b3c4d-5e6f-7081-92a3-b4c5d6e7f809": {"title": "Roadmap", "blocks": [
            {"type": "heading_1", "heading_1": {"rich_text": [{"plain_text": "Q1"}]}},
            {"type": "to_do", "to_do": {"rich_text": [{"plain_text": "Ship kernel"}], "checked": True}}]}}
        self.created = []

    def send(self, method, url, headers, body):
        assert headers["Notion-Version"]
        if headers.get("Authorization") != "Bearer ntn_goodtoken":
            return 401, b'{"message": "API token is invalid."}'
        u = urllib.parse.urlsplit(url)
        js = lambda o, s=200: (s, json.dumps(o).encode())  # noqa: E731
        if u.path == "/v1/users/me":
            return js({"name": "Agentic OS", "bot": {"workspace_name": "Ayush's workspace"}})
        if u.path == "/v1/search":
            return js({"results": [{"id": pid, "url": f"https://notion.so/{pid}",
                                    "properties": {"title": {"type": "title", "title": [{"plain_text": p["title"]}]}}}
                                   for pid, p in self.pages.items()]})
        if u.path.startswith("/v1/pages/") and method == "GET":
            pid = u.path.rsplit("/", 1)[-1]
            if pid not in self.pages:
                return js({"message": "not found"}, 404)
            return js({"id": pid, "properties": {"title": {"type": "title", "title": [{"plain_text": self.pages[pid]["title"]}]}}})
        if u.path.endswith("/children") and method == "GET":
            pid = u.path.split("/")[3]
            return js({"results": self.pages[pid]["blocks"], "has_more": False})
        if u.path == "/v1/pages" and method == "POST":
            b = json.loads(body)
            self.created.append(b)
            return js({"id": "99999999-9999-9999-9999-999999999999", "url": "https://notion.so/new"})
        if u.path.endswith("/children") and method == "PATCH":
            return js({"results": []})
        return js({"message": "unexpected"}, 500)


def test_notion_client_and_tools(tmp_path):
    secrets = MemorySecretStore()
    cfg = {"notion": {"default_parent": "1a2b3c4d-5e6f-7081-92a3-b4c5d6e7f809", "default_parent_title": "Roadmap"}}
    http = FakeNotion()
    n = NotionClient(secrets, lambda: cfg, http=http)
    with pytest.raises(NotionError):
        n.search("x")
    secrets.set("notion:token", "ntn_bad")
    with pytest.raises(NotionError) as e:
        n.me()
    assert e.value.code == "NOTION_AUTH"
    secrets.set("notion:token", "ntn_goodtoken")
    assert n.search("road")[0]["title"] == "Roadmap"
    page = n.read_page("https://www.notion.so/Roadmap-1a2b3c4d5e6f708192a3b4c5d6e7f809")
    assert "# Q1" in page["text"] and "- [x] Ship kernel" in page["text"]
    env = make_env(tmp_path)
    plan = {"goal": "notion", "steps": [
        {"id": "find", "tool": "notion_search", "args": {"query": "roadmap"}},
        {"id": "make", "tool": "notion_create_page", "args": {"title": "Standup", "content": "# Today\n- shipped connectors"}}]}
    k = build_kernel(env, plan, services={"notion": n})
    pid = k.spawn("save standup to notion")
    k.run_until_idle()
    pend = k.proc(pid)["pending_user"]
    assert pend["kind"] == "approval" and pend["grant"]["kind"] == "notion" and "Roadmap" in pend["effect"]
    assert not http.created
    k.approve(pid, pend["action_hash"])
    k.run_until_idle()
    assert k.proc(pid)["exit"]["code"] == "completed"
    made = http.created[0]
    assert made["parent"]["page_id"] == "1a2b3c4d-5e6f-7081-92a3-b4c5d6e7f809"
    assert [b["type"] for b in made["children"]] == ["heading_1", "bulleted_list_item"]


def test_app_discovery_returns_executables():
    from agentic_os.services.app_discovery import discover
    apps = discover(force=True)
    assert all(os.path.isabs(a["path"]) and a["id"] for a in apps)
    assert len({a["path"] for a in apps}) == len(apps)
