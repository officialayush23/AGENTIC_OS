"""API: connections (Google OAuth callback, Notion, Gmail preset) and one-click app allowlisting."""

import urllib.parse

from tests.api.test_api import client  # noqa: F401 - fixture
from tests.kernel.test_connected import FakeGoogle, FakeNotion


def test_google_connect_flow_through_callback(client, monkeypatch):
    st = client.app.state.aos
    st.google.auth.http = FakeGoogle()
    monkeypatch.setattr("webbrowser.open", lambda url: True)
    assert client.get("/api/connections").json()["google"]["configured"] is False
    r = client.put("/api/connections/google/client", json={"client_id": "1-x.apps.googleusercontent.com",
                                                             "client_secret": "GOCSPX-s"})
    assert r.json()["configured"]
    r = client.post("/api/connections/google/start", json={"services": ["calendar", "gmail"]}).json()
    assert r["opened"] and r["redirect_uri"].endswith("/oauth/google/callback")
    state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(r["url"]).query))["state"]
    page = client.get(f"/oauth/google/callback?state={state}&code=good-code", headers={"Host": "127.0.0.1:8765"})
    assert page.status_code == 200 and "Google is connected" in page.text
    c = client.get("/api/connections").json()
    assert c["google"]["connected"] and c["google"]["email"] == "me@gmail.com"
    assert c["mail"]["mode"] == "gmail" and c["mail"]["delivers"]
    bad = client.get("/oauth/google/callback?state=nope&code=x", headers={"Host": "127.0.0.1:8765"})
    assert bad.status_code == 400
    assert client.post("/api/connections/google/disconnect").json()["connected"] is False
    assert client.get("/api/mail/status").json()["mode"] == "local"


def test_notion_connect_and_default_page(client):
    st = client.app.state.aos
    st.notion.http = FakeNotion()
    assert client.put("/api/connections/notion", json={"token": "nope-not-a-token"}).status_code == 400
    assert client.put("/api/connections/notion", json={"token": "ntn_bad_token_x"}).status_code == 400
    r = client.put("/api/connections/notion", json={"token": "ntn_goodtoken"})
    assert r.status_code == 200 and r.json()["workspace"] == "Ayush's workspace"
    pages = client.get("/api/connections/notion/pages", params={"q": "road"}).json()["pages"]
    r = client.put("/api/connections/notion/default", json={"page": pages[0]["id"], "title": pages[0]["title"]})
    assert r.json()["default_parent_title"] == "Roadmap"
    assert client.delete("/api/connections/notion").json()["connected"] is False


def test_apps_discover_and_one_click_add(client):
    apps = client.get("/api/apps/discover").json()["apps"]
    if not apps:
        return
    a = apps[0]
    r = client.post("/api/apps/add", json={"name": a["name"], "path": a["path"]})
    assert r.status_code == 200
    again = client.get("/api/apps/discover").json()["apps"]
    assert next(x for x in again if x["path"] == a["path"])["added"]
    assert client.post("/api/apps/add", json={"name": "x", "path": "/no/such/app"}).status_code == 400
