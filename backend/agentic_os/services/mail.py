"""Email service: a small transport interface with two implementations.

* ``LocalMailbox`` - .eml files under the data folder. Used for the demo, tests and
  "offline" mode. Sending writes to the local Sent folder and is clearly marked as not delivered.
* ``ImapSmtpTransport`` - a real account over IMAP (read) and SMTP (send) with TLS. The password
  lives in the OS credential store, never in the database.

Every outgoing message carries a deterministic Message-ID derived from (task, step), so after a crash
the kernel can reconcile: if a message with that id is already in Sent, it is not sent again.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import imaplib
import mimetypes
import os
import re
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, make_msgid, parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

MAIL_SECRET = "mail:password"
ADDR_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


class MailError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def message_id_for(task_id: str, step_id: str, domain: str = "agentic-os.local") -> str:
    h = hashlib.sha256(f"{task_id}:{step_id}".encode()).hexdigest()[:24]
    return f"<aos-{h}@{domain}>"


def valid_address(addr: str) -> bool:
    return bool(ADDR_RE.match(addr.strip()))


def split_addresses(value: Any) -> List[str]:
    if not value:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    out: List[str] = []
    for _, addr in getaddresses([str(i) for i in items]):
        if addr:
            out.append(addr.strip())
    return out


def _text_of(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                try:
                    return part.get_content()
                except Exception:  # noqa: BLE001
                    return (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                html = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
                return re.sub(r"<[^>]+>", " ", html)
        return ""
    try:
        return msg.get_content()
    except Exception:  # noqa: BLE001
        return (msg.get_payload(decode=True) or b"").decode("utf-8", "replace")


def summarize(uid: str, msg: email.message.Message, folder: str, seen: bool = True) -> dict:
    try:
        date = parsedate_to_datetime(msg.get("Date")).isoformat() if msg.get("Date") else None
    except (TypeError, ValueError):
        date = None
    body = _text_of(msg)
    return {
        "id": f"{folder}:{uid}", "uid": uid, "folder": folder, "subject": str(msg.get("Subject") or "(no subject)"),
        "from": str(msg.get("From") or ""), "to": str(msg.get("To") or ""), "cc": str(msg.get("Cc") or ""),
        "date": date, "message_id": str(msg.get("Message-ID") or ""), "seen": seen,
        "snippet": re.sub(r"\s+", " ", body).strip()[:200],
        "attachments": [p.get_filename() for p in msg.walk() if p.get_filename()] if msg.is_multipart() else [],
    }


def build_message(*, sender: str, to: Sequence[str], subject: str, body: str, cc: Sequence[str] = (),
                  attachments: Sequence[Tuple[str, bytes]] = (), message_id: Optional[str] = None,
                  in_reply_to: Optional[str] = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = message_id or make_msgid(domain="agentic-os.local")
    msg["X-Mailer"] = "Agentic OS"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    for name, data in attachments:
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if name.lower().endswith(".ics"):
            ctype = "text/calendar"
        main, sub = ctype.split("/", 1)
        if main == "text":
            params = {"method": "REQUEST"} if sub == "calendar" else {}
            msg.add_attachment(data.decode("utf-8", "replace"), subtype=sub, filename=os.path.basename(name), params=params)
        else:
            msg.add_attachment(data, maintype=main, subtype=sub, filename=os.path.basename(name))
    return msg


# ---------------------------------------------------------------------------
class LocalMailbox:
    """Folders of .eml files. INBOX is seeded with a few demo messages on first use."""

    kind = "local"

    def __init__(self, root: str, address: str = "you@agentic-os.local"):
        self.root = Path(root)
        self.address = address
        self._lock = threading.Lock()
        for f in ("INBOX", "Sent"):
            (self.root / f).mkdir(parents=True, exist_ok=True)

    def seed_demo(self) -> None:
        inbox = self.root / "INBOX"
        if any(inbox.iterdir()):
            return
        demo = [
            ("Priya Nair <priya@example.com>", "Design review on Friday",
             "Hi,\n\nCan we do the design review on Friday at 11:00? I'd like to walk through the new onboarding "
             "screens and the empty states.\n\nThanks,\nPriya"),
            ("Build bot <ci@example.com>", "Nightly build failed: tests/test_api.py",
             "The nightly build failed.\n\n1 failed, 183 passed\nFAILED tests/test_api.py::test_events_replay\n"),
            ("Sam Okafor <sam@example.com>", "Notes from the planning call",
             "Action items:\n- Sam: draft the Q1 roadmap\n- You: send the kernel write-up by Monday\n- Priya: book a room"),
        ]
        for i, (frm, subject, body) in enumerate(demo):
            msg = build_message(sender=frm, to=[self.address], subject=subject, body=body)
            self.deliver(msg, folder="INBOX", uid=f"{int(time.time())}{i:02d}", seen=False)

    def deliver(self, msg: EmailMessage, folder: str = "INBOX", uid: Optional[str] = None, seen: bool = False) -> str:
        with self._lock:
            d = self.root / folder
            d.mkdir(parents=True, exist_ok=True)
            uid = uid or f"{int(time.time() * 1000)}"
            name = f"{uid}{'' if seen else '.new'}.eml"
            tmp = d / (name + ".tmp")
            tmp.write_bytes(msg.as_bytes(policy=email.policy.SMTP))
            os.replace(tmp, d / name)
            return uid

    def _files(self, folder: str) -> List[Path]:
        d = self.root / folder
        if not d.is_dir():
            raise MailError("NO_SUCH_FOLDER", f"There is no mail folder called {folder!r}.")
        return sorted((p for p in d.iterdir() if p.suffix == ".eml"), key=lambda p: p.name, reverse=True)

    @staticmethod
    def _uid(p: Path) -> str:
        return p.name.split(".")[0]

    def _load(self, p: Path) -> email.message.Message:
        return email.message_from_bytes(p.read_bytes(), policy=email.policy.default)

    def folders(self) -> List[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())

    def list(self, folder: str = "INBOX", limit: int = 20, unread_only: bool = False) -> List[dict]:
        out = []
        for p in self._files(folder):
            seen = ".new" not in p.name
            if unread_only and seen:
                continue
            out.append(summarize(self._uid(p), self._load(p), folder, seen))
            if len(out) >= limit:
                break
        return out

    def uids(self, folder: str = "INBOX") -> List[str]:
        return [self._uid(p) for p in self._files(folder)]

    def fetch(self, folder: str, uid: str) -> Tuple[dict, str]:
        for p in self._files(folder):
            if self._uid(p) == uid:
                msg = self._load(p)
                return summarize(uid, msg, folder, ".new" not in p.name), _text_of(msg)
        raise MailError("NO_SUCH_MESSAGE", "That message was not found.")

    def mark_seen(self, folder: str, uid: str) -> None:
        for p in self._files(folder):
            if self._uid(p) == uid and ".new" in p.name:
                os.replace(p, p.with_name(p.name.replace(".new", "")))

    def search(self, query: str, folder: str = "INBOX", limit: int = 20) -> List[dict]:
        q = query.lower()
        out = []
        for p in self._files(folder):
            msg = self._load(p)
            hay = " ".join([str(msg.get("Subject") or ""), str(msg.get("From") or ""), _text_of(msg)]).lower()
            if all(term in hay for term in q.split()):
                out.append(summarize(self._uid(p), msg, folder, ".new" not in p.name))
                if len(out) >= limit:
                    break
        return out

    def send(self, msg: EmailMessage) -> dict:
        uid = self.deliver(msg, folder="Sent", seen=True)
        return {"delivered": False, "transport": "local", "uid": uid,
                "note": "Saved to the local Sent folder. Connect a real account in Settings to deliver email."}

    def sent_contains(self, message_id: str) -> bool:
        for p in self._files("Sent"):
            if message_id in p.read_text("utf-8", "replace")[:4000]:
                return True
        return False


# ---------------------------------------------------------------------------
class ImapSmtpTransport:
    kind = "imap"

    def __init__(self, cfg: Dict[str, Any], password: Optional[str], timeout: float = 20.0):
        self.cfg = cfg
        self.password = password
        self.timeout = timeout
        self.address = cfg.get("address") or cfg.get("username") or ""
        if not (cfg.get("imap_host") and cfg.get("username")):
            raise MailError("MAIL_NOT_CONFIGURED", "Add the IMAP server and username in Settings → Email.")
        if not password:
            raise MailError("MAIL_NOT_CONFIGURED", "Save the email password in Settings → Email.")

    def _imap(self) -> imaplib.IMAP4:
        host, port = self.cfg["imap_host"], int(self.cfg.get("imap_port") or 993)
        ctx = ssl.create_default_context()
        try:
            if port == 993:
                conn = imaplib.IMAP4_SSL(host, port, ssl_context=ctx, timeout=self.timeout)
            else:
                conn = imaplib.IMAP4(host, port, timeout=self.timeout)
                conn.starttls(ssl_context=ctx)
            conn.login(self.cfg["username"], self.password)
            return conn
        except (OSError, imaplib.IMAP4.error) as exc:
            raise MailError("MAIL_CONNECT_FAILED", f"Could not sign in to {host}: {exc}", retryable=True) from None

    @staticmethod
    def _q(folder: str) -> str:
        return '"' + folder.replace('"', '') + '"'

    def folders(self) -> List[str]:
        conn = self._imap()
        try:
            _, data = conn.list()
            out = []
            for line in data or []:
                text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
                out.append(text.rsplit(' "/" ', 1)[-1].strip('"'))
            return out
        finally:
            conn.logout()

    def _fetch_many(self, conn, folder: str, uids: List[bytes]) -> List[dict]:
        out = []
        for uid in uids:
            _, data = conn.uid("fetch", uid, "(FLAGS BODY.PEEK[])")
            if not data or not isinstance(data[0], tuple):
                continue
            flags = data[0][0].decode("utf-8", "replace")
            msg = email.message_from_bytes(data[0][1], policy=email.policy.default)
            out.append(summarize(uid.decode(), msg, folder, "\\Seen" in flags))
        return out

    def list(self, folder: str = "INBOX", limit: int = 20, unread_only: bool = False) -> List[dict]:
        conn = self._imap()
        try:
            conn.select(self._q(folder), readonly=True)
            _, data = conn.uid("search", None, "UNSEEN" if unread_only else "ALL")
            uids = (data[0] or b"").split()[-limit:][::-1]
            return self._fetch_many(conn, folder, uids)
        finally:
            conn.logout()

    def uids(self, folder: str = "INBOX") -> List[str]:
        conn = self._imap()
        try:
            conn.select(self._q(folder), readonly=True)
            _, data = conn.uid("search", None, "ALL")
            return [u.decode() for u in (data[0] or b"").split()][::-1]
        finally:
            conn.logout()

    def fetch(self, folder: str, uid: str) -> Tuple[dict, str]:
        conn = self._imap()
        try:
            conn.select(self._q(folder), readonly=True)
            _, data = conn.uid("fetch", uid.encode(), "(FLAGS BODY.PEEK[])")
            if not data or not isinstance(data[0], tuple):
                raise MailError("NO_SUCH_MESSAGE", "That message was not found.")
            msg = email.message_from_bytes(data[0][1], policy=email.policy.default)
            return summarize(uid, msg, folder, "\\Seen" in data[0][0].decode()), _text_of(msg)
        finally:
            conn.logout()

    def mark_seen(self, folder: str, uid: str) -> None:
        conn = self._imap()
        try:
            conn.select(self._q(folder))
            conn.uid("store", uid.encode(), "+FLAGS", "(\\Seen)")
        finally:
            conn.logout()

    def search(self, query: str, folder: str = "INBOX", limit: int = 20) -> List[dict]:
        conn = self._imap()
        try:
            conn.select(self._q(folder), readonly=True)
            safe = query.replace('"', "")
            _, data = conn.uid("search", "CHARSET", "UTF-8", "TEXT", f'"{safe}"')
            uids = (data[0] or b"").split()[-limit:][::-1]
            return self._fetch_many(conn, folder, uids)
        finally:
            conn.logout()

    def send(self, msg: EmailMessage) -> dict:
        host, port = self.cfg.get("smtp_host") or self.cfg["imap_host"], int(self.cfg.get("smtp_port") or 587)
        ctx = ssl.create_default_context()
        try:
            if port == 465:
                server = smtplib.SMTP_SSL(host, port, context=ctx, timeout=self.timeout)
            else:
                server = smtplib.SMTP(host, port, timeout=self.timeout)
                server.starttls(context=ctx)
            with server:
                server.login(self.cfg.get("smtp_username") or self.cfg["username"], self.password)
                refused = server.send_message(msg)
        except (OSError, smtplib.SMTPException) as exc:
            raise MailError("MAIL_SEND_FAILED", f"The mail server did not accept the message: {exc}") from None
        sent_folder = self.cfg.get("sent_folder") or "Sent"
        try:  # keep a copy in Sent (many providers do this automatically; duplicates are harmless)
            conn = self._imap()
            try:
                conn.append(self._q(sent_folder), "(\\Seen)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
            finally:
                conn.logout()
        except Exception:  # noqa: BLE001
            pass
        return {"delivered": True, "transport": "smtp", "refused": list(refused.keys())}

    def sent_contains(self, message_id: str) -> bool:
        conn = self._imap()
        try:
            conn.select(self._q(self.cfg.get("sent_folder") or "Sent"), readonly=True)
            _, data = conn.uid("search", None, "HEADER", "Message-ID", message_id.strip("<>"))
            return bool((data[0] or b"").split())
        finally:
            conn.logout()


# ---------------------------------------------------------------------------
class MailService:
    """Resolves the configured transport on each call (settings can change at runtime)."""

    def __init__(self, data_dir: str, settings_fn, secrets, google=None):
        self.local = LocalMailbox(os.path.join(data_dir, "mail"))
        self.settings_fn = settings_fn
        self.secrets = secrets
        self.google = google          # GoogleAuth, for mode "gmail" (Gmail API)

    def config(self) -> dict:
        return dict((self.settings_fn() or {}).get("mail") or {})

    def transport(self):
        cfg = self.config()
        mode = cfg.get("mode") or "local"
        if mode == "off":
            raise MailError("MAIL_OFF", "Email is turned off in Settings → Email.")
        if mode == "gmail":
            from .google import GmailTransport
            if self.google is None or not self.google.has("gmail"):
                raise MailError("MAIL_NOT_CONFIGURED", "Sign in to Google with Gmail allowed (Connections → Google).")
            return GmailTransport(self.google)
        if mode == "imap":
            try:
                password = self.secrets.get(MAIL_SECRET)
            except Exception:  # noqa: BLE001
                password = None
            return ImapSmtpTransport(cfg, password)
        self.local.address = cfg.get("address") or self.local.address
        return self.local

    def sender(self) -> str:
        cfg = self.config()
        t = self.transport()
        addr = cfg.get("address") or getattr(t, "address", "") or "you@agentic-os.local"
        return formataddr((cfg.get("display_name") or "", addr)) if cfg.get("display_name") else addr

    def status(self) -> dict:
        cfg = self.config()
        mode = cfg.get("mode") or "local"
        try:
            stored = bool(self.secrets.get(MAIL_SECRET))
        except Exception:  # noqa: BLE001
            stored = False
        if mode == "gmail" and self.google is not None:
            g = self.google.status()
            return {"mode": mode, "address": g.get("email") or "", "imap_host": None, "smtp_host": None,
                    "password_stored": g["connected"], "delivers": g["connected"] and "gmail" in g["services"]}
        return {"mode": mode, "address": cfg.get("address") or (self.local.address if mode == "local" else ""),
                "imap_host": cfg.get("imap_host"), "smtp_host": cfg.get("smtp_host"), "password_stored": stored,
                "delivers": mode == "imap"}
