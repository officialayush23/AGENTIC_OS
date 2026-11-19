"""Google account: OAuth 2.0 for installed apps (loopback + PKCE), Gmail API and Calendar API (with Meet).

The user creates a "Desktop app" OAuth client in their own Google Cloud project once (the Connections
screen walks through it) and pastes its client ID and secret. Signing in opens Google's consent page in
the browser; Google redirects back to this computer (`http://127.0.0.1:<port>/oauth/google/callback`).
The refresh token and client secret are kept in the OS credential store, never in the database.
"""

from __future__ import annotations

import base64
import email
import email.policy
import hashlib
import secrets
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from .http import HttpClient, HttpError
from .mail import MailError, summarize, _text_of

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
CAL = "https://www.googleapis.com/calendar/v3"

SCOPES = {
    "base": ["openid", "email"],
    "calendar": ["https://www.googleapis.com/auth/calendar.events"],
    "gmail": ["https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/gmail.send"],
}
SECRET_CLIENT = "google:client_secret"
SECRET_REFRESH = "google:refresh_token"


class GoogleError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class GoogleAuth:
    def __init__(self, settings_fn: Callable[[], dict], save_settings: Callable[[dict], None], secrets_store,
                 http: Optional[HttpClient] = None, clock: Callable[[], float] = time.time):
        self.settings_fn = settings_fn
        self.save_settings = save_settings
        self.secrets = secrets_store
        self.http = http or HttpClient()
        self.clock = clock
        self._pending: Dict[str, Tuple[str, str, float, List[str]]] = {}
        self._token: Optional[Tuple[str, float]] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ settings
    def cfg(self) -> dict:
        return dict((self.settings_fn() or {}).get("google") or {})

    def _secret(self, name: str) -> Optional[str]:
        try:
            return self.secrets.get(name)
        except Exception:  # noqa: BLE001
            return None

    def configure(self, client_id: str, client_secret: str) -> dict:
        client_id = client_id.strip()
        if not client_id.endswith(".apps.googleusercontent.com"):
            raise GoogleError("GOOGLE_CLIENT_INVALID", "The client ID should end with .apps.googleusercontent.com")
        self.secrets.set(SECRET_CLIENT, client_secret.strip())
        self.save_settings({**self.cfg(), "client_id": client_id})
        return self.status()

    def status(self) -> dict:
        c = self.cfg()
        connected = bool(self._secret(SECRET_REFRESH))
        return {"configured": bool(c.get("client_id") and self._secret(SECRET_CLIENT)), "connected": connected,
                "email": c.get("email") if connected else None, "services": c.get("services") or [],
                "client_id": c.get("client_id")}

    def has(self, service: str) -> bool:
        st = self.status()
        return st["connected"] and service in st["services"]

    # ------------------------------------------------------------- sign in
    def start(self, redirect_uri: str, services: List[str]) -> str:
        c = self.cfg()
        if not (c.get("client_id") and self._secret(SECRET_CLIENT)):
            raise GoogleError("GOOGLE_NOT_CONFIGURED", "Add your OAuth client ID and secret first.")
        services = [s for s in services if s in ("calendar", "gmail")] or ["calendar"]
        verifier = _b64url(secrets.token_bytes(48))
        state = secrets.token_urlsafe(24)
        scopes = SCOPES["base"] + [x for s in services for x in SCOPES[s]]
        with self._lock:
            now = self.clock()
            self._pending = {k: v for k, v in self._pending.items() if now - v[2] < 600}
            self._pending[state] = (verifier, redirect_uri, now, services)
        q = {"client_id": c["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
             "scope": " ".join(scopes), "state": state, "access_type": "offline", "prompt": "consent",
             "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256",
             "include_granted_scopes": "true"}
        return f"{AUTH_URL}?{urllib.parse.urlencode(q)}"

    def finish(self, state: str, code: str) -> dict:
        with self._lock:
            pend = self._pending.pop(state, None)
        if pend is None:
            raise GoogleError("GOOGLE_STATE_INVALID", "This sign-in link expired or was already used. Start again.")
        verifier, redirect_uri, _, services = pend
        c = self.cfg()
        try:
            tok = self.http.request("POST", TOKEN_URL, form={
                "client_id": c["client_id"], "client_secret": self._secret(SECRET_CLIENT) or "", "code": code,
                "code_verifier": verifier, "grant_type": "authorization_code", "redirect_uri": redirect_uri})
        except HttpError as exc:
            raise GoogleError("GOOGLE_TOKEN_FAILED", f"Google did not accept the sign-in: {exc.message}") from None
        if not tok.get("refresh_token"):
            raise GoogleError("GOOGLE_NO_REFRESH", "Google did not return a refresh token. Remove Agentic OS from your "
                              "Google account's third-party access and connect again.")
        self.secrets.set(SECRET_REFRESH, tok["refresh_token"])
        self._token = (tok["access_token"], self.clock() + float(tok.get("expires_in", 3600)) - 60)
        granted = tok.get("scope", "")
        got = [s for s in services if all(x in granted for x in SCOPES[s])]
        mail = None
        try:
            mail = self.http.request("GET", USERINFO_URL, token=tok["access_token"]).get("email")
        except HttpError:
            pass
        self.save_settings({**c, "email": mail, "services": got, "connected_at": datetime.now().isoformat(timespec="seconds")})
        return self.status()

    def access_token(self) -> str:
        with self._lock:
            if self._token and self._token[1] > self.clock():
                return self._token[0]
            refresh = self._secret(SECRET_REFRESH)
            c = self.cfg()
            if not refresh:
                raise GoogleError("GOOGLE_NOT_CONNECTED", "Connect your Google account in Connections first.")
            try:
                tok = self.http.request("POST", TOKEN_URL, form={
                    "client_id": c.get("client_id", ""), "client_secret": self._secret(SECRET_CLIENT) or "",
                    "refresh_token": refresh, "grant_type": "refresh_token"})
            except HttpError as exc:
                if exc.status in (400, 401):
                    raise GoogleError("GOOGLE_RECONNECT", "Google access expired or was revoked. Connect again.") from None
                raise GoogleError("GOOGLE_UNREACHABLE", exc.message, retryable=True) from None
            self._token = (tok["access_token"], self.clock() + float(tok.get("expires_in", 3600)) - 60)
            return self._token[0]

    def disconnect(self) -> dict:
        refresh = self._secret(SECRET_REFRESH)
        if refresh:
            try:
                self.http.request("POST", REVOKE_URL, form={"token": refresh})
            except HttpError:
                pass
            try:
                self.secrets.delete(SECRET_REFRESH)
            except Exception:  # noqa: BLE001
                pass
        self._token = None
        c = self.cfg()
        c.pop("email", None)
        c["services"] = []
        self.save_settings(c)
        return self.status()

    def call(self, method: str, url: str, **kw) -> Any:
        try:
            return self.http.request(method, url, token=self.access_token(), **kw)
        except HttpError as exc:
            if exc.status == 401:
                self._token = None
                return self.http.request(method, url, token=self.access_token(), **kw)
            if exc.status == 403:
                raise GoogleError("GOOGLE_FORBIDDEN", f"Google refused: {exc.message}. Was this service allowed when you "
                                  "connected? Is the API enabled in your Cloud project?") from None
            if exc.status == 404:
                raise GoogleError("GOOGLE_NOT_FOUND", "Not found in your Google account.") from None
            raise GoogleError("GOOGLE_API_ERROR", exc.message, retryable=exc.status >= 500 or exc.status == 0) from None


# ===================================================================== Gmail
class GmailTransport:
    """Mail transport over the Gmail API (same interface as LocalMailbox / ImapSmtpTransport)."""

    kind = "gmail"
    LABELS = {"INBOX": "INBOX", "Sent": "SENT", "SENT": "SENT"}

    def __init__(self, auth: GoogleAuth):
        self.auth = auth
        self.address = auth.cfg().get("email") or ""

    def _wrap(self, fn):
        try:
            return fn()
        except GoogleError as exc:
            raise MailError(exc.code, exc.message, exc.retryable) from None

    def _ids(self, folder: str, limit: int, q: Optional[str] = None) -> List[str]:
        params = {"maxResults": limit, "labelIds": self.LABELS.get(folder, folder)}
        if q:
            params["q"] = q
        res = self._wrap(lambda: self.auth.call("GET", f"{GMAIL}/messages", params=params))
        return [m["id"] for m in res.get("messages") or []]

    def _raw(self, mid: str) -> Tuple[email.message.Message, bool]:
        res = self._wrap(lambda: self.auth.call("GET", f"{GMAIL}/messages/{mid}", params={"format": "raw"}))
        raw = base64.urlsafe_b64decode(res["raw"] + "=" * (-len(res["raw"]) % 4))
        return email.message_from_bytes(raw, policy=email.policy.default), "UNREAD" not in (res.get("labelIds") or [])

    def folders(self) -> List[str]:
        res = self._wrap(lambda: self.auth.call("GET", f"{GMAIL}/labels"))
        return sorted(l["name"] for l in res.get("labels") or [])

    def list(self, folder: str = "INBOX", limit: int = 20, unread_only: bool = False) -> List[dict]:
        out = []
        for mid in self._ids(folder, limit, "is:unread" if unread_only else None):
            msg, seen = self._raw(mid)
            out.append(summarize(mid, msg, folder, seen))
        return out

    def uids(self, folder: str = "INBOX") -> List[str]:
        return self._ids(folder, 100)

    def fetch(self, folder: str, uid: str) -> Tuple[dict, str]:
        msg, seen = self._raw(uid)
        return summarize(uid, msg, folder, seen), _text_of(msg)

    def mark_seen(self, folder: str, uid: str) -> None:
        self._wrap(lambda: self.auth.call("POST", f"{GMAIL}/messages/{uid}/modify", json_body={"removeLabelIds": ["UNREAD"]}))

    def search(self, query: str, folder: str = "INBOX", limit: int = 20) -> List[dict]:
        out = []
        from .mail import gmail_query, search_alternatives
        alts = search_alternatives(query)
        for mid in self._ids(folder, limit, gmail_query(alts) if alts else query):
            msg, seen = self._raw(mid)
            out.append(summarize(mid, msg, folder, seen))
        return out

    def send(self, msg) -> dict:
        raw = _b64url(msg.as_bytes(policy=email.policy.SMTP))
        res = self._wrap(lambda: self.auth.call("POST", f"{GMAIL}/messages/send", json_body={"raw": raw}))
        return {"delivered": True, "transport": "gmail", "gmail_id": res.get("id")}

    def sent_contains(self, message_id: str) -> bool:
        res = self._wrap(lambda: self.auth.call("GET", f"{GMAIL}/messages",
                                                params={"q": f"rfc822msgid:{message_id.strip('<>')}", "maxResults": 1}))
        return bool(res.get("messages"))


# ================================================================== Calendar
def event_id_for(seed: str) -> str:
    """Deterministic Google event id (base32hex alphabet: 0-9 a-v), so a retried create is detected."""
    return "aos" + hashlib.sha256(seed.encode()).hexdigest()[:40]


def _rfc3339(local_iso: str) -> str:
    return datetime.fromisoformat(local_iso).astimezone().isoformat()


def _simplify(ev: dict) -> dict:
    s = ev.get("start") or {}
    e = ev.get("end") or {}
    meet = ev.get("hangoutLink") or next((p.get("uri") for p in ((ev.get("conferenceData") or {}).get("entryPoints") or [])
                                         if p.get("entryPointType") == "video"), None)
    return {"id": ev.get("id"), "title": ev.get("summary") or "(no title)",
            "start": (s.get("dateTime") or s.get("date") or "")[:16], "end": (e.get("dateTime") or e.get("date") or "")[:16],
            "location": ev.get("location"), "description": (ev.get("description") or "")[:2000], "url": meet,
            "html_link": ev.get("htmlLink"), "attendees": [a.get("email") for a in ev.get("attendees") or [] if a.get("email")],
            "status": ev.get("status"), "source": "google"}


class GoogleCalendar:
    def __init__(self, auth: GoogleAuth):
        self.auth = auth

    def list_events(self, start: datetime, days: int) -> List[dict]:
        res = self.auth.call("GET", f"{CAL}/calendars/primary/events", params={
            "timeMin": start.astimezone().isoformat(), "timeMax": (start + timedelta(days=days)).astimezone().isoformat(),
            "singleEvents": "true", "orderBy": "startTime", "maxResults": 100})
        return [_simplify(e) for e in res.get("items") or []]

    def get(self, event_id: str) -> Optional[dict]:
        try:
            return _simplify(self.auth.call("GET", f"{CAL}/calendars/primary/events/{event_id}"))
        except GoogleError as exc:
            if exc.code == "GOOGLE_NOT_FOUND":
                return None
            raise

    def create_event(self, *, event_id: str, title: str, start: str, end: str, attendees: List[str],
                     description: Optional[str], location: Optional[str], meet: bool, notify: bool) -> dict:
        body: Dict[str, Any] = {"id": event_id, "summary": title, "start": {"dateTime": _rfc3339(start)},
                                "end": {"dateTime": _rfc3339(end)}, "attendees": [{"email": a} for a in attendees]}
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if meet:
            body["conferenceData"] = {"createRequest": {"requestId": event_id,
                                                        "conferenceSolutionKey": {"type": "hangoutsMeet"}}}
        try:
            res = self.auth.call("POST", f"{CAL}/calendars/primary/events", json_body=body, params={
                "conferenceDataVersion": 1 if meet else 0, "sendUpdates": "all" if notify and attendees else "none"})
        except GoogleError as exc:
            if "409" in exc.message or "already exists" in exc.message.lower():
                existing = self.get(event_id)
                if existing:
                    return {**existing, "already_existed": True}
            raise
        return _simplify(res)
