"""Connections: Google (Gmail, Calendar, Meet), Notion, email presets, and installed-app discovery."""

from __future__ import annotations

import asyncio
import html
import re
import webbrowser
from typing import List, Optional

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..services.app_discovery import discover
from ..services.google import GoogleError
from ..services.mail import MAIL_SECRET, MailError, valid_address
from ..services.notion import SECRET as NOTION_SECRET, NotionError, page_id


class GoogleClientIn(BaseModel):
    client_id: str = Field(min_length=10, max_length=300)
    client_secret: str = Field(min_length=4, max_length=300)


class GoogleStartIn(BaseModel):
    services: List[str] = Field(default_factory=lambda: ["calendar", "gmail"])


class AppPasswordIn(BaseModel):
    address: str = Field(min_length=3, max_length=300)
    password: str = Field(min_length=4, max_length=200)
    display_name: Optional[str] = Field(None, max_length=200)


class NotionTokenIn(BaseModel):
    token: str = Field(min_length=10, max_length=300)


class NotionDefaultIn(BaseModel):
    page: str = Field(min_length=8, max_length=500)
    title: Optional[str] = Field(None, max_length=300)


class AppAddIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    path: str = Field(min_length=1, max_length=1024)
    id: Optional[str] = Field(None, max_length=60)


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Agentic OS</title>
<style>body{{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;background:#f0f0f0;
font:15px/1.5 "Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;color:#111}}
.c{{background:#fff;border-radius:26px;padding:32px 36px;max-width:440px}}h1{{font-weight:400;font-size:28px;margin:0 0 8px}}
p{{color:#4d4d4d;margin:0}}.chip{{display:inline-block;padding:4px 12px;border-radius:999px;font-size:12px;font-weight:500;
margin-bottom:14px;background:{bg};color:{fg}}}</style></head><body><div class="c"><span class="chip">{word}</span>
<h1>{title}</h1><p>{text}</p></div></body></html>"""


def register(api: APIRouter, app: FastAPI, st, ApiError) -> None:
    def gerr(exc: GoogleError):
        return ApiError(502 if exc.retryable else 400, exc.code, exc.message)

    def nerr(exc: NotionError):
        return ApiError(502 if exc.retryable else 400, exc.code, exc.message)

    @api.get("/connections")
    async def connections():
        return {"google": st.google.auth.status(), "notion": st.notion.status(), "mail": st.mail.status(),
                "apps": len(st.repos.apps.list())}

    # ----------------------------------------------------------------- Google
    @api.put("/connections/google/client")
    async def google_client(body: GoogleClientIn):
        try:
            return st.google.auth.configure(body.client_id, body.client_secret)
        except GoogleError as exc:
            raise gerr(exc)

    @api.post("/connections/google/start")
    async def google_start(body: GoogleStartIn, request: Request):
        port = request.url.port or st.settings.port
        redirect = f"http://127.0.0.1:{port}/oauth/google/callback"
        try:
            url = st.google.auth.start(redirect, body.services)
        except GoogleError as exc:
            raise gerr(exc)
        opened = False
        try:
            opened = await asyncio.to_thread(webbrowser.open, url)
        except Exception:  # noqa: BLE001
            opened = False
        return {"url": url, "opened": bool(opened), "redirect_uri": redirect}

    @app.get("/oauth/google/callback", include_in_schema=False)
    async def google_callback(state: str = "", code: str = "", error: str = ""):
        def page(ok: bool, title: str, text: str):
            return HTMLResponse(PAGE.format(word="Connected" if ok else "Not connected", bg="#e6f4ec" if ok else "#fdecec",
                                            fg="#1f7a4d" if ok else "#c62828", title=html.escape(title),
                                            text=html.escape(text)), status_code=200 if ok else 400)
        if error:
            return page(False, "Google sign-in was cancelled", f"Google said: {error}. You can close this tab.")
        try:
            status = await asyncio.to_thread(st.google.auth.finish, state, code)
        except GoogleError as exc:
            return page(False, "Couldn't connect Google", exc.message)
        if "gmail" in status["services"]:
            cfg = dict(st.user_settings().get("mail") or {})
            if cfg.get("mode") in (None, "local", "off"):
                st.repos.settings.set("mail", {**cfg, "mode": "gmail", "address": status.get("email")})
        st.events.publish_global({"type": "connections.changed", "data": {"google": True}})
        allowed = " and ".join({"calendar": "Calendar + Meet", "gmail": "Gmail"}[s] for s in status["services"]) or "your account"
        return page(True, "Google is connected", f"Agentic OS can now use {allowed} for {status.get('email') or 'your account'}. "
                    "You can close this tab and go back to the app.")

    @api.post("/connections/google/disconnect")
    async def google_disconnect():
        res = await asyncio.to_thread(st.google.auth.disconnect)
        cfg = dict(st.user_settings().get("mail") or {})
        if cfg.get("mode") == "gmail":
            st.repos.settings.set("mail", {**cfg, "mode": "local"})
        return res

    @api.post("/connections/google/test")
    async def google_test():
        from datetime import datetime
        out = {}
        try:
            if st.google.auth.has("calendar"):
                ev = await asyncio.to_thread(st.google.calendar.list_events, datetime.now(), 7)
                out["calendar"] = f"{len(ev)} event(s) in the next 7 days"
            if st.google.auth.has("gmail"):
                from ..services.google import GmailTransport
                ids = await asyncio.to_thread(GmailTransport(st.google.auth).uids, "INBOX")
                out["gmail"] = f"inbox readable ({len(ids)} recent messages)"
        except GoogleError as exc:
            raise gerr(exc)
        except MailError as exc:
            raise ApiError(400, exc.code, exc.message)
        return {"ok": True, **out}

    @api.get("/connections/google/events")
    async def google_events(start: Optional[str] = None, days: int = 7):
        from datetime import datetime
        if not st.google.auth.has("calendar"):
            return {"events": [], "connected": False}
        try:
            s = datetime.fromisoformat(start) if start else datetime.now()
            ev = await asyncio.to_thread(st.google.calendar.list_events, s.replace(hour=0, minute=0, second=0, microsecond=0),
                                         max(1, min(days, 60)))
        except GoogleError as exc:
            raise gerr(exc)
        except ValueError:
            raise ApiError(400, "BAD_TIME", "Use a date like 2026-11-20.")
        return {"events": ev, "connected": True}

    # ------------------------------------------------------------ mail presets
    @api.put("/connections/gmail/app-password")
    async def gmail_app_password(body: AppPasswordIn):
        addr = body.address.strip()
        if not valid_address(addr):
            raise ApiError(400, "BAD_ADDRESS", "That is not an email address.")
        pw = re.sub(r"\s+", "", body.password)          # Google shows app passwords in groups of four
        st.secrets.set(MAIL_SECRET, pw)
        st.repos.settings.set("mail", {"mode": "imap", "address": addr, "display_name": body.display_name,
                                       "imap_host": "imap.gmail.com", "imap_port": 993, "smtp_host": "smtp.gmail.com",
                                       "smtp_port": 465, "username": addr, "sent_folder": "[Gmail]/Sent Mail"})
        try:
            t = st.mail.transport()
            await asyncio.to_thread(t.list, "INBOX", 1, False)
        except MailError as exc:
            raise ApiError(400, exc.code, exc.message + " Check the address and the 16-letter app password.")
        return st.mail.status()

    @api.post("/connections/mail/mode")
    async def mail_mode(body: dict):
        mode = str(body.get("mode") or "")
        if mode not in ("local", "gmail", "imap", "off"):
            raise ApiError(400, "BAD_MODE", "Unknown mail mode.")
        if mode == "gmail" and not st.google.auth.has("gmail"):
            raise ApiError(400, "GOOGLE_NOT_CONNECTED", "Sign in with Google and allow Gmail first.")
        cfg = dict(st.user_settings().get("mail") or {})
        st.repos.settings.set("mail", {**cfg, "mode": mode})
        return st.mail.status()

    # ------------------------------------------------------------------ Notion
    @api.put("/connections/notion")
    async def notion_connect(body: NotionTokenIn):
        token = body.token.strip()
        if not token.startswith(("ntn_", "secret_")):
            raise ApiError(400, "NOTION_TOKEN_INVALID", "Notion integration secrets start with ntn_ (or secret_).")
        st.secrets.set(NOTION_SECRET, token)
        try:
            me = await asyncio.to_thread(st.notion.me)
        except NotionError as exc:
            st.secrets.delete(NOTION_SECRET)
            raise nerr(exc)
        bot = me.get("bot") or {}
        cfg = dict(st.user_settings().get("notion") or {})
        st.repos.settings.set("notion", {**cfg, "bot": me.get("name"), "workspace": bot.get("workspace_name")})
        return st.notion.status()

    @api.delete("/connections/notion")
    async def notion_disconnect():
        try:
            st.secrets.delete(NOTION_SECRET)
        except Exception:  # noqa: BLE001
            pass
        st.repos.settings.set("notion", {})
        return st.notion.status()

    @api.get("/connections/notion/pages")
    async def notion_pages(q: str = ""):
        try:
            return {"pages": await asyncio.to_thread(st.notion.search, q, 20)}
        except NotionError as exc:
            raise nerr(exc)

    @api.put("/connections/notion/default")
    async def notion_default(body: NotionDefaultIn):
        try:
            pid = page_id(body.page)
        except NotionError as exc:
            raise nerr(exc)
        cfg = dict(st.user_settings().get("notion") or {})
        st.repos.settings.set("notion", {**cfg, "default_parent": pid, "default_parent_title": body.title or pid})
        return st.notion.status()

    # -------------------------------------------------------------------- apps
    @api.get("/apps/discover")
    async def apps_discover(refresh: bool = False):
        found = await asyncio.to_thread(discover, refresh)
        added = {a["path"].lower() for a in st.repos.apps.list()}
        return {"apps": [{**a, "added": a["path"].lower() in added} for a in found]}

    @api.post("/apps/add")
    async def apps_add(body: AppAddIn):
        import os
        if not os.path.isabs(body.path) or not os.path.isfile(body.path):
            raise ApiError(400, "APP_PATH_INVALID", "That program was not found.")
        base = body.id or re.sub(r"[^a-z0-9]+", "-", body.name.lower()).strip("-")[:40] or "app"
        ids = {a["id"] for a in st.repos.apps.list()}
        app_id, n = base, 2
        while app_id in ids:
            app_id, n = f"{base}-{n}", n + 1
        st.repos.apps.upsert(app_id, {"name": body.name, "path": body.path, "args_policy": "workspace_file",
                                      "requires_approval": False})
        return {"id": app_id, "apps": st.repos.apps.list()}
