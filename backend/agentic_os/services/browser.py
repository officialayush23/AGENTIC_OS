"""Browser control.

* ``fetch_page`` - plain HTTP GET (no JavaScript) with size and time limits, HTML turned into text.
* ``BrowserService`` - a real browser (Microsoft Edge or Google Chrome already installed on the computer,
  driven by Playwright). Playwright's sync API is bound to one thread, so every call is marshalled onto
  a dedicated browser thread (like a device driver with its own queue). Each task gets its own browser
  context (separate cookies, separate tab), closed when the task ends.

Pages are summarised as text plus a numbered list of interactive elements (`e1`, `e2`, ...) that the
click/type tools refer to.
"""

from __future__ import annotations

import ipaddress
import os
import queue
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import Future
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

UA = "Mozilla/5.0 (compatible; AgenticOS/0.2; +https://github.com/officialayush23)"
MAX_BYTES = 3_000_000


class BrowserError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


# ---------------------------------------------------------------------- text
class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header",
             "footer", "pre", "blockquote", "table", "ul", "ol"}

    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.out: List[str] = []
        self.links: List[dict] = []
        self.title = ""
        self._skip = 0
        self._in_title = False
        self._href: Optional[str] = None
        self._link_text: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag in ("h1", "h2", "h3"):
            self.out.append("#" * int(tag[1]) + " ")
        if tag == "li":
            self.out.append("- ")
        if tag == "a":
            href = dict(attrs).get("href")
            if href and not href.startswith(("javascript:", "#")):
                self._href = urljoin(self.base, href)
                self._link_text = []

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._href:
            text = " ".join("".join(self._link_text).split())
            if text and len(self.links) < 200:
                self.links.append({"text": text[:120], "url": self._href})
            self._href = None
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._skip:
            return
        self.out.append(data)
        if self._href is not None:
            self._link_text.append(data)

    def text(self) -> str:
        raw = "".join(self.out)
        raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
        raw = re.sub(r" *\n *", "\n", raw)
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def html_to_text(html: str, base: str = "") -> dict:
    p = _Text(base)
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001 - malformed HTML still yields partial text
        pass
    return {"title": " ".join(p.title.split()), "text": p.text(), "links": p.links}


def check_url(url: str, *, allow_private: bool = False) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise BrowserError("URL_SCHEME", "Only http:// and https:// addresses can be opened.")
    host = parts.hostname or ""
    if not host:
        raise BrowserError("URL_INVALID", "That address has no host name.")
    if not allow_private:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            raise BrowserError("DNS_FAILED", f"Could not find {host}.", retryable=True) from None
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise BrowserError("URL_PRIVATE", f"{host} is on your local network; local addresses are blocked "
                                   "(turn on 'Allow local network pages' in Settings to open them).")
    return url.strip()


def fetch_page(url: str, *, timeout: float = 20.0, allow_private: bool = False, max_bytes: int = MAX_BYTES) -> dict:
    url = check_url(url, allow_private=allow_private)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,text/plain,*/*;q=0.5"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - scheme checked above
            final = resp.geturl()
            ctype = resp.headers.get("Content-Type", "")
            data = resp.read(max_bytes + 1)
            status = resp.status
    except urllib.error.HTTPError as exc:
        raise BrowserError("HTTP_ERROR", f"The site answered {exc.code} {exc.reason}.",
                           retryable=exc.code >= 500) from None
    except (urllib.error.URLError, OSError) as exc:
        raise BrowserError("FETCH_FAILED", f"Could not load the page: {getattr(exc, 'reason', exc)}",
                           retryable=True) from None
    truncated = len(data) > max_bytes
    data = data[:max_bytes]
    charset = "utf-8"
    m = re.search(r"charset=([\w-]+)", ctype)
    if m:
        charset = m.group(1)
    body = data.decode(charset, "replace")
    if "html" in ctype or body.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        page = html_to_text(body, final)
    else:
        page = {"title": "", "text": body, "links": []}
    return {"url": final, "status": status, "content_type": ctype, "truncated": truncated, **page}


# ------------------------------------------------------------------- browser
SNAPSHOT_JS = r"""
() => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const els = Array.from(document.querySelectorAll(
    'a[href], button, input:not([type=hidden]), textarea, select, [role=button], [role=link], [contenteditable=true]'));
  let n = 0; const out = [];
  for (const el of els) {
    if (!vis(el) || n >= 150) continue;
    n += 1; const ref = 'e' + n; el.setAttribute('data-aos-ref', ref);
    const label = (el.getAttribute('aria-label') || el.innerText || el.value || el.placeholder ||
                   el.getAttribute('title') || el.name || '').trim().replace(/\s+/g, ' ').slice(0, 100);
    out.push({ref, tag: el.tagName.toLowerCase(), type: el.type || el.getAttribute('role') || '', label,
              href: el.href || null});
  }
  return {title: document.title, url: location.href, text: (document.body ? document.body.innerText : '').slice(0, 60000),
          elements: out};
}
"""


class BrowserService:
    """One browser process, one thread, one context per task."""

    def __init__(self, data_dir: str, settings_fn: Callable[[], dict]):
        self.data_dir = data_dir
        self.settings_fn = settings_fn
        self._q: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._pw = None
        self._browser = None
        self._contexts: Dict[str, Any] = {}
        self._last_used: Dict[str, float] = {}
        self._lock = threading.Lock()
        self.available: Optional[bool] = None
        self.last_error: Optional[str] = None

    # thread plumbing --------------------------------------------------------
    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="aos-browser", daemon=True)
                self._thread.start()

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            fn, fut = item
            if fut.set_running_or_notify_cancel():
                try:
                    fut.set_result(fn())
                except BaseException as exc:  # noqa: BLE001
                    fut.set_exception(exc)
        self._shutdown()

    def call(self, fn: Callable[[], Any], timeout: float = 60.0) -> Any:
        self._ensure_thread()
        fut: Future = Future()
        self._q.put((fn, fut))
        return fut.result(timeout=timeout)

    # browser lifecycle (browser thread only) ----------------------------------
    def _launch(self):
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.available = False
            raise BrowserError("BROWSER_MISSING", "Browser control needs the 'playwright' package "
                               "(pip install playwright).") from None
        s = self.settings_fn() or {}
        headed = bool(s.get("browser_headed"))
        channels = [s.get("browser_channel")] if s.get("browser_channel") else (
            ["msedge", "chrome"] if sys.platform == "win32" else ["chrome", "msedge"])
        self._pw = self._pw or sync_playwright().start()
        errors = []
        for ch in [*channels, None]:
            try:
                kw = {"headless": not headed}
                if ch:
                    kw["channel"] = ch
                self._browser = self._pw.chromium.launch(**kw)
                self.available, self.last_error = True, None
                return self._browser
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{ch or 'bundled chromium'}: {str(exc).splitlines()[0][:160]}")
        self.available = False
        self.last_error = "; ".join(errors)
        raise BrowserError("BROWSER_UNAVAILABLE", "Could not start Microsoft Edge or Google Chrome. " + self.last_error)

    def _page(self, key: str):
        ctx = self._contexts.get(key)
        if ctx is None:
            ctx = self._launch().new_context(user_agent=None, viewport={"width": 1280, "height": 860},
                                             accept_downloads=False)
            ctx.set_default_timeout(20_000)
            self._contexts[key] = ctx
        self._last_used[key] = time.time()
        pages = ctx.pages
        return pages[-1] if pages else ctx.new_page()

    def _snapshot(self, page) -> dict:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:  # noqa: BLE001
            pass
        snap = page.evaluate(SNAPSHOT_JS)
        snap["text"] = re.sub(r"\n{3,}", "\n\n", snap.get("text") or "").strip()
        return snap

    def _shutdown(self) -> None:
        for ctx in list(self._contexts.values()):
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
        self._contexts.clear()
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._browser = self._pw = None

    # public API (any thread) -------------------------------------------------
    def open(self, key: str, url: str) -> dict:
        def do():
            page = self._page(key)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as exc:  # noqa: BLE001
                raise BrowserError("NAVIGATION_FAILED", f"Could not open {url}: {str(exc).splitlines()[0][:200]}",
                                   retryable=True) from None
            return self._snapshot(page)
        return self.call(do, timeout=60)

    def current(self, key: str) -> dict:
        def do():
            if key not in self._contexts:
                raise BrowserError("NO_PAGE", "No page is open in this task. Open one first.")
            return self._snapshot(self._page(key))
        return self.call(do)

    def screenshot(self, key: str, path: str) -> dict:
        def do():
            if key not in self._contexts:
                raise BrowserError("NO_PAGE", "No page is open in this task. Open one first.")
            page = self._page(key)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            page.screenshot(path=path, full_page=False)
            return {"url": page.url, "title": page.title(), "path": path}
        return self.call(do)

    def _locate(self, page, ref: str):
        if not re.fullmatch(r"e\d{1,4}", ref or ""):
            raise BrowserError("BAD_REF", "Use an element reference like e12 from the page snapshot.")
        loc = page.locator(f'[data-aos-ref="{ref}"]')
        if loc.count() == 0:
            raise BrowserError("REF_STALE", f"Element {ref} is no longer on the page; read the page again.")
        return loc.first

    def describe(self, key: str, ref: str) -> dict:
        def do():
            if key not in self._contexts:
                raise BrowserError("NO_PAGE", "No page is open in this task.")
            page = self._page(key)
            el = self._locate(page, ref)
            info = el.evaluate("e => ({tag: e.tagName.toLowerCase(), type: e.type || '', "
                               "label: (e.getAttribute('aria-label') || e.innerText || e.value || e.placeholder || "
                               "e.name || '').trim().slice(0, 100), href: e.href || null, "
                               "form: e.form ? (e.form.action || '') : null})")
            return {"url": page.url, **info}
        return self.call(do)

    def click(self, key: str, ref: str) -> dict:
        def do():
            page = self._page(key)
            self._locate(page, ref).click(timeout=10_000)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except Exception:  # noqa: BLE001
                pass
            return self._snapshot(page)
        return self.call(do)

    def type(self, key: str, ref: str, text: str, submit: bool) -> dict:  # noqa: A003
        def do():
            page = self._page(key)
            el = self._locate(page, ref)
            el.fill(text, timeout=10_000)
            if submit:
                el.press("Enter")
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=10_000)
                except Exception:  # noqa: BLE001
                    pass
            return self._snapshot(page)
        return self.call(do)

    def close(self, key: str) -> None:
        def do():
            ctx = self._contexts.pop(key, None)
            if ctx is not None:
                ctx.close()
        if self._thread is not None and self._thread.is_alive():
            try:
                self.call(do, timeout=10)
            except Exception:  # noqa: BLE001
                pass

    def status(self) -> dict:
        return {"available": self.available, "open_sessions": len(self._contexts), "last_error": self.last_error}

    def stop(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._q.put(None)
            self._thread.join(timeout=10)
