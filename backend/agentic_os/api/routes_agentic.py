"""Routes for the agentic services: Home (notes, calendar), email, schedules, listeners, browser, webhooks."""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..automation.store import AutomationError, next_run, schedule_label
from ..kernel.mounts import is_home
from ..services import calendar as cal
from ..services.mail import MAIL_SECRET, MailError, build_message, split_addresses, valid_address
from ..tools.base import ToolError
from ..tools.fileio import atomic_replace, read_text_file, sha256_file, sha256_text
from ..tools.personal import CALENDAR_FILE, NOTES_DIR, slugify


class NoteWriteIn(BaseModel):
    path: Optional[str] = Field(None, max_length=1024)
    title: Optional[str] = Field(None, max_length=200)
    content: str = Field(max_length=2_000_000)
    expected_sha256: Optional[str] = Field(None, max_length=64)


class EventIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    start: str
    end: Optional[str] = None
    duration_minutes: int = Field(30, ge=5, le=1440)
    location: Optional[str] = Field(None, max_length=300)
    description: Optional[str] = Field(None, max_length=10_000)
    online_meeting: bool = False


class MailConfigIn(BaseModel):
    mode: Literal["off", "local", "imap"] = "local"
    address: Optional[str] = Field(None, max_length=300)
    display_name: Optional[str] = Field(None, max_length=200)
    imap_host: Optional[str] = Field(None, max_length=300)
    imap_port: Optional[int] = Field(None, ge=1, le=65535)
    smtp_host: Optional[str] = Field(None, max_length=300)
    smtp_port: Optional[int] = Field(None, ge=1, le=65535)
    username: Optional[str] = Field(None, max_length=300)
    sent_folder: Optional[str] = Field(None, max_length=200)


class SecretIn(BaseModel):
    password: str = Field(min_length=1, max_length=4096)


class ScheduleIn(BaseModel):
    name: str = Field("", max_length=120)
    request: str = Field(min_length=1, max_length=8000)
    kind: Literal["cron", "interval", "once"]
    spec: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    auto_commit: bool = False


class ScheduleUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=120)
    request: Optional[str] = Field(None, max_length=8000)
    kind: Optional[Literal["cron", "interval", "once"]] = None
    spec: Optional[str] = Field(None, max_length=200)
    enabled: Optional[bool] = None
    auto_commit: Optional[bool] = None


class PreviewIn(BaseModel):
    kind: Literal["cron", "interval", "once"]
    spec: str = Field(min_length=1, max_length=200)


class ListenerIn(BaseModel):
    name: str = Field("", max_length=120)
    kind: Literal["file", "email", "webhook"]
    request: str = Field(min_length=1, max_length=8000)
    config: Dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    auto_commit: bool = False
    cooldown_s: float = Field(60, ge=5, le=86400)


class ListenerUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=120)
    request: Optional[str] = Field(None, max_length=8000)
    config: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None
    auto_commit: Optional[bool] = None
    cooldown_s: Optional[float] = Field(None, ge=5, le=86400)


def register(api: APIRouter, app: FastAPI, st, ApiError) -> None:  # noqa: C901 - a flat list of routes
    def auto_err(exc: AutomationError):
        return ApiError(404 if exc.code.startswith("NO_SUCH") else 400, exc.code, exc.message)

    def mail_err(exc: MailError):
        return ApiError(502 if exc.retryable or exc.code.endswith("FAILED") else 400, exc.code, exc.message)

    # ------------------------------------------------------------- home / notes
    def home_path(path: str) -> str:
        p = (path or "").replace("\\", "/").strip()
        if not is_home(p):
            raise ApiError(400, "NOT_HOME", "Only files in your Home folder (~/...) can be edited here.")
        return p

    def note_info(m, rel: str) -> dict:
        t = m.resolve(rel)
        title = os.path.splitext(os.path.basename(rel))[0].replace("-", " ")
        try:
            with open(t.abs_path, encoding="utf-8", errors="replace") as fh:
                head = fh.read(4000)
            for line in head.splitlines():
                if line.startswith("# "):
                    title = line[2:].strip()
                    break
            snippet = " ".join(x for x in head.splitlines() if x and not x.startswith("#"))[:160]
        except OSError:
            snippet = ""
        stt = os.stat(t.abs_path)
        return {"path": rel, "title": title, "snippet": snippet, "size": stt.st_size, "modified": stt.st_mtime}

    @api.get("/home")
    async def home_info():
        m = st.mounted()
        out = {}
        for d in ("~/Notes", "~/Calendar", "~/Mail/Outbox"):
            t = m.resolve(d)
            files = list(m.iter_files(t, max_depth=6, max_files=5000)) if t.is_dir else []
            out[d] = len([f for f in files if not f.endswith("/.keep")])
        return {"root": st.home_root, "counts": out}

    @api.get("/notes")
    async def notes():
        m = st.mounted()
        t = m.resolve(NOTES_DIR)
        files = [f for f in m.iter_files(t, max_depth=6, max_files=5000) if f.lower().endswith((".md", ".txt"))]
        items = [note_info(m, f) for f in files]
        items.sort(key=lambda n: -n["modified"])
        return {"notes": items}

    @api.get("/notes/file")
    async def note_file(path: str):
        m = st.mounted()
        t = m.resolve(home_path(path))
        if t.sensitive or not t.is_file:
            raise ApiError(404, "NO_SUCH_NOTE", "That note does not exist.")
        text, enc, _ = read_text_file(t.abs_path, 4_000_000)
        return {**note_info(m, t.rel), "content": text, "sha256": sha256_file(t.abs_path)}

    def reindex(rel: str, text: Optional[str]) -> None:
        if text is None:
            for d in st.kb.documents(5000):
                if d["source"] == "workspace" and d["path"] == rel:
                    st.kb.delete_document(d["id"])
        else:
            st.kb.upsert_document(source="workspace", path=rel, title=rel, text=text, mtime=time.time())

    @api.put("/notes/file")
    async def save_note(body: NoteWriteIn):
        rel = home_path(body.path) if body.path else f"{NOTES_DIR}/{slugify(body.title or 'untitled')}.md"
        if not rel.lower().endswith((".md", ".txt")):
            raise ApiError(400, "NOT_A_NOTE", "Notes are .md or .txt files.")
        m = st.mounted()
        t = m.resolve(rel, for_write=True)
        if t.sensitive:
            raise ApiError(403, "SENSITIVE_PATH", "That file is protected.")
        if t.exists and body.expected_sha256 and sha256_file(t.abs_path) != body.expected_sha256:
            raise ApiError(409, "NOTE_CHANGED", "This note changed since you opened it (perhaps a task committed "
                           "an edit). Reload it first.")
        if not t.exists and not body.path and os.path.exists(t.abs_path):
            raise ApiError(409, "NOTE_EXISTS", "A note with that name exists.")
        os.makedirs(os.path.dirname(t.abs_path), exist_ok=True)
        atomic_replace(t.abs_path, body.content.encode("utf-8"))
        await asyncio.to_thread(reindex, t.rel, body.content)
        st.events.publish_global({"type": "home.changed", "data": {"path": t.rel, "by": "user"}})
        return {**note_info(m, t.rel), "sha256": sha256_text(body.content)}

    @api.delete("/notes/file")
    async def delete_note(path: str):
        m = st.mounted()
        t = m.resolve(home_path(path))
        if not t.is_file or t.sensitive:
            raise ApiError(404, "NO_SUCH_NOTE", "That note does not exist.")
        trash = os.path.join(str(st.settings.data_dir), "trash", datetime.now().strftime("%Y%m%d-%H%M%S"))
        os.makedirs(trash, exist_ok=True)
        shutil.move(t.abs_path, os.path.join(trash, os.path.basename(t.abs_path)))
        await asyncio.to_thread(reindex, t.rel, None)
        st.events.publish_global({"type": "home.changed", "data": {"path": t.rel, "by": "user"}})
        return {"deleted": True, "kept_in": trash}

    # ---------------------------------------------------------------- calendar
    def cal_abs() -> str:
        return st.mounted().resolve(CALENDAR_FILE, for_write=True).abs_path

    def cal_events() -> List[dict]:
        p = cal_abs()
        return cal.parse_ics(open(p, encoding="utf-8").read()) if os.path.exists(p) else []

    @api.get("/calendar")
    async def calendar(start: Optional[str] = None, days: int = 14):
        try:
            s = cal.parse_user_time(start) if start else datetime.now()
        except ValueError as exc:
            raise ApiError(400, "BAD_TIME", str(exc))
        s = s.replace(hour=0, minute=0)
        evs = cal.in_range(cal_events(), s, max(1, min(days, 366)))
        return {"start": s.date().isoformat(), "days": days, "events": evs, "file": CALENDAR_FILE}

    @api.post("/calendar/events")
    async def add_event(body: EventIn):
        try:
            s = cal.parse_user_time(body.start)
            e = cal.parse_user_time(body.end) if body.end else s + timedelta(minutes=body.duration_minutes)
        except ValueError as exc:
            raise ApiError(400, "BAD_TIME", str(exc))
        if e <= s:
            raise ApiError(400, "BAD_TIME", "The event must end after it starts.")
        ev = {"uid": cal.new_uid(f"user:{time.time()}:{body.title}"), "title": body.title, "start": s.isoformat(timespec="minutes"),
              "end": e.isoformat(timespec="minutes"), "location": body.location, "description": body.description,
              "attendees": []}
        if body.online_meeting:
            ev["url"] = cal.jitsi_link(body.title)
            ev["location"] = body.location or ev["url"]
        atomic_replace(cal_abs(), cal.render_ics(cal_events() + [ev]).encode("utf-8"))
        st.events.publish_global({"type": "home.changed", "data": {"path": CALENDAR_FILE, "by": "user"}})
        return ev

    @api.delete("/calendar/events/{uid}")
    async def delete_event(uid: str):
        evs = cal_events()
        keep = [e for e in evs if e["uid"] != uid]
        if len(keep) == len(evs):
            raise ApiError(404, "NO_SUCH_EVENT", "That event is not in the calendar.")
        atomic_replace(cal_abs(), cal.render_ics(keep).encode("utf-8"))
        st.events.publish_global({"type": "home.changed", "data": {"path": CALENDAR_FILE, "by": "user"}})
        return {"deleted": True}

    @api.get("/calendar.ics")
    async def calendar_file():
        return FileResponse(cal_abs(), media_type="text/calendar", filename="calendar.ics")

    # -------------------------------------------------------------------- mail
    @api.get("/mail/status")
    async def mail_status():
        return st.mail.status()

    @api.put("/mail/config")
    async def mail_config(body: MailConfigIn):
        cfg = {k: v for k, v in body.model_dump().items() if v not in (None, "")}
        if cfg.get("address") and not valid_address(cfg["address"]):
            raise ApiError(400, "BAD_ADDRESS", "That is not an email address.")
        if cfg["mode"] == "imap" and not (cfg.get("imap_host") and cfg.get("username")):
            raise ApiError(400, "MAIL_CONFIG_INCOMPLETE", "Enter the IMAP server and user name.")
        st.repos.settings.set("mail", cfg)
        return st.mail.status()

    @api.post("/mail/password")
    async def mail_password(body: SecretIn):
        st.secrets.set(MAIL_SECRET, body.password)
        return st.mail.status()

    @api.delete("/mail/password")
    async def mail_password_delete():
        st.secrets.delete(MAIL_SECRET)
        return st.mail.status()

    @api.post("/mail/test")
    async def mail_test():
        try:
            t = await asyncio.to_thread(st.mail.transport)
            msgs = await asyncio.to_thread(t.list, "INBOX", 1, False)
        except MailError as exc:
            raise mail_err(exc)
        return {"ok": True, "transport": t.kind, "sample": msgs[:1]}

    @api.get("/mail/messages")
    async def mail_messages(folder: str = "INBOX", limit: int = 30, unread_only: bool = False, q: Optional[str] = None):
        try:
            t = st.mail.transport()
            if q:
                msgs = await asyncio.to_thread(t.search, q, folder, min(limit, 100))
            else:
                msgs = await asyncio.to_thread(t.list, folder, min(limit, 100), unread_only)
        except MailError as exc:
            raise mail_err(exc)
        return {"folder": folder, "messages": msgs, "transport": t.kind}

    @api.get("/mail/folders")
    async def mail_folders():
        try:
            t = st.mail.transport()
            return {"folders": await asyncio.to_thread(t.folders)}
        except MailError as exc:
            raise mail_err(exc)

    @api.get("/mail/message")
    async def mail_message(id: str, mark_read: bool = True):  # noqa: A002
        folder, _, uid = id.rpartition(":")
        try:
            t = st.mail.transport()
            meta, body = await asyncio.to_thread(t.fetch, folder or "INBOX", uid)
            if mark_read and not meta.get("seen"):
                await asyncio.to_thread(t.mark_seen, folder or "INBOX", uid)
        except MailError as exc:
            raise mail_err(exc)
        return {**meta, "body": body}

    @api.post("/mail/demo")
    async def mail_demo(body: Dict[str, Any]):
        """Drop a message into the local demo inbox (lets you try email listeners without an account)."""
        if st.mail.status()["mode"] != "local":
            raise ApiError(409, "NOT_LOCAL", "Demo messages only work with the local mailbox.")
        frm = str(body.get("from") or "Demo Sender <demo@example.com>")
        msg = build_message(sender=frm, to=[st.mail.local.address], subject=str(body.get("subject") or "Hello"),
                            body=str(body.get("body") or "This is a demo message."))
        uid = st.mail.local.deliver(msg, "INBOX")
        return {"id": f"INBOX:{uid}"}

    # --------------------------------------------------------------- schedules
    @api.get("/schedules")
    async def schedules():
        return {"schedules": st.automation.list_schedules()}

    @api.post("/schedules/preview")
    async def schedule_preview(body: PreviewIn):
        try:
            t, runs = time.time(), []
            for _ in range(3):
                n = next_run(body.kind, body.spec, t)
                if n is None:
                    break
                runs.append(datetime.fromtimestamp(n).isoformat(timespec="minutes"))
                t = n
            return {"label": schedule_label(body.kind, body.spec), "next_runs": runs}
        except AutomationError as exc:
            raise auto_err(exc)

    @api.post("/schedules")
    async def create_schedule(body: ScheduleIn):
        try:
            return st.automation.create_schedule(**body.model_dump())
        except AutomationError as exc:
            raise auto_err(exc)

    @api.put("/schedules/{sid}")
    async def update_schedule(sid: str, body: ScheduleUpdate):
        try:
            return st.automation.update_schedule(sid, **body.model_dump())
        except AutomationError as exc:
            raise auto_err(exc)

    @api.delete("/schedules/{sid}")
    async def delete_schedule(sid: str):
        if not st.automation.delete_schedule(sid):
            raise ApiError(404, "NO_SUCH_SCHEDULE", "Unknown schedule.")
        return {"deleted": True}

    @api.post("/schedules/{sid}/run")
    async def run_schedule(sid: str):
        s = st.automation.get_schedule(sid)
        if s is None:
            raise ApiError(404, "NO_SUCH_SCHEDULE", "Unknown schedule.")
        pid = st.kernel.spawn(s["request"], title=s["name"], auto_commit=s["auto_commit"],
                              origin={"kind": "schedule", "id": sid, "name": s["name"], "manual": True})
        st.automation.record_run("schedule", sid, pid, time.time(), {"name": s["name"], "manual": True})
        return {"pid": pid}

    # --------------------------------------------------------------- listeners
    @api.get("/listeners")
    async def listeners():
        return {"listeners": st.automation.list_listeners()}

    def check_listener_config(kind: str, config: Dict[str, Any]) -> None:
        if kind == "file":
            try:
                st.listener_root(str(config.get("path") or "."))
            except (ValueError, ToolError) as exc:
                raise ApiError(400, "LISTENER_INVALID", getattr(exc, "message", None) or str(exc))

    @api.post("/listeners")
    async def create_listener(body: ListenerIn):
        check_listener_config(body.kind, body.config)
        try:
            return st.automation.create_listener(**body.model_dump())
        except AutomationError as exc:
            raise auto_err(exc)

    @api.put("/listeners/{lid}")
    async def update_listener(lid: str, body: ListenerUpdate):
        cur = st.automation.get_listener(lid)
        if cur is None:
            raise ApiError(404, "NO_SUCH_LISTENER", "Unknown listener.")
        if body.config is not None:
            check_listener_config(cur["kind"], body.config)
        return st.automation.update_listener(lid, **body.model_dump())

    @api.delete("/listeners/{lid}")
    async def delete_listener(lid: str):
        if not st.automation.delete_listener(lid):
            raise ApiError(404, "NO_SUCH_LISTENER", "Unknown listener.")
        return {"deleted": True}

    @api.post("/listeners/{lid}/poll")
    async def poll_listener(lid: str):
        if st.automation.get_listener(lid) is None:
            raise ApiError(404, "NO_SUCH_LISTENER", "Unknown listener.")
        fired = await asyncio.to_thread(st.listeners.poll_all, True)
        return {"fired": fired, "listener": st.automation.get_listener(lid)}

    @api.get("/automation/runs")
    async def automation_runs(source_id: Optional[str] = None, limit: int = 50):
        return {"runs": st.automation.runs(source_id, min(limit, 200))}

    # ----------------------------------------------------------------- browser
    @api.get("/browser/status")
    async def browser_status():
        s = st.user_settings()
        return {**st.browser.status(), "headed": bool(s.get("browser_headed")), "channel": s.get("browser_channel"),
                "allow_private": bool(s.get("web_allow_private"))}

    @api.get("/screens/{task_id}/{name}")
    async def screen(task_id: str, name: str):
        if not (task_id.replace("_", "").isalnum() and name.endswith(".png") and "/" not in name and ".." not in name):
            raise ApiError(404, "NOT_FOUND", "No such screenshot.")
        p = os.path.join(str(st.settings.data_dir), "screens", task_id, name)
        if not os.path.isfile(p):
            raise ApiError(404, "NOT_FOUND", "No such screenshot.")
        return FileResponse(p, media_type="image/png")

    # ---------------------------------------------------------------- webhooks
    @app.post("/hooks/{lid}", include_in_schema=False)
    async def webhook(lid: str, request: Request):
        """Local webhook: POST with header X-Hook-Secret (or ?key=). Loopback only, like the whole API."""
        lst = st.automation.get_listener(lid)
        secret = request.headers.get("x-hook-secret") or request.query_params.get("key") or ""
        if lst is None or lst["kind"] != "webhook" or not st.automation.check_secret(lid, secret):
            raise ApiError(404, "NO_SUCH_HOOK", "Unknown hook or wrong secret.")
        if not lst["enabled"]:
            raise ApiError(409, "HOOK_DISABLED", "This listener is turned off.")
        raw = await request.body()
        if len(raw) > 256_000:
            raise ApiError(413, "TOO_LARGE", "Webhook payloads are limited to 256 KB.")
        try:
            payload = await request.json() if raw else {}
            if not isinstance(payload, dict):
                payload = {"body": str(payload)}
        except ValueError:
            payload = {"body": raw.decode("utf-8", "replace")}
        pid = st.listeners.fire_webhook(lst, payload)
        return {"accepted": pid is not None, "pid": pid,
                **({} if pid else {"reason": "cooling down; try again later"})}
