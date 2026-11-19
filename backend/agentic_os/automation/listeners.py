"""Listener service: turns outside events into tasks (like interrupt handlers).

* file    - polls a folder in the workspace or Home for created / changed / deleted files
* email   - polls a mail folder for new messages (optionally filtered by sender or subject)
* webhook - an HTTP POST to /api/hooks/<id> with the listener's secret (handled by the API)

Each listener has a cooldown: events arriving inside it are batched into the next task.
Changes committed by a task that the same listener started are ignored, so a listener never
re-triggers itself.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .store import AutomationStore, render_request

log = logging.getLogger(__name__)

MAX_FILES = 5000


def snapshot(root: str, pattern: str = "*", ignored=lambda name: False) -> Dict[str, List[float]]:
    out: Dict[str, List[float]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and not ignored(d)]
        for fn in filenames:
            if fn.startswith(".") or not fnmatch.fnmatch(fn.lower(), (pattern or "*").lower()):
                continue
            p = os.path.join(dirpath, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out[os.path.relpath(p, root).replace(os.sep, "/")] = [round(st.st_mtime, 3), st.st_size]
            if len(out) >= MAX_FILES:
                return out
    return out


def diff_snapshots(old: Dict[str, list], new: Dict[str, list]) -> List[dict]:
    changes = []
    for k, v in new.items():
        if k not in old:
            changes.append({"path": k, "change": "created"})
        elif list(old[k]) != list(v):
            changes.append({"path": k, "change": "modified"})
    for k in old:
        if k not in new:
            changes.append({"path": k, "change": "deleted"})
    return changes


class ListenerService:
    def __init__(self, store: AutomationStore, *, spawn: Callable[..., str], resolve_root: Callable[[str], str],
                 mail=None, clock: Callable[[], float] = time.time, publish: Optional[Callable[[dict], None]] = None):
        self.store = store
        self.spawn = spawn
        self.resolve_root = resolve_root     # listener path ("docs", "~/Notes") -> (absolute folder, display prefix)
        self.mail = mail
        self.clock = clock
        self.publish = publish
        self._last_poll: Dict[str, float] = {}
        self._own_commits: Dict[str, Dict[str, float]] = {}    # listener id -> {display path: time}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="aos-listeners", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(2.0):
            try:
                self.poll_all()
            except Exception:  # noqa: BLE001
                log.exception("listener poll failed")

    # ------------------------------------------------------------- commits
    def note_commit(self, origin: Optional[dict], paths: List[str]) -> None:
        if origin and origin.get("kind") == "listener":
            mine = self._own_commits.setdefault(origin["id"], {})
            now = self.clock()
            for p in paths:
                mine[p] = now

    # ---------------------------------------------------------------- poll
    def poll_all(self, force: bool = False) -> List[str]:
        fired = []
        for lst in self.store.list_listeners():
            if not lst["enabled"] or lst["kind"] == "webhook":
                continue
            every = float(lst["config"].get("poll_s") or (5 if lst["kind"] == "file" else 60))
            now = self.clock()
            if not force and now - self._last_poll.get(lst["id"], 0) < every:
                continue
            self._last_poll[lst["id"]] = now
            try:
                pid = self._poll_file(lst) if lst["kind"] == "file" else self._poll_email(lst)
                if pid:
                    fired.append(pid)
            except Exception as exc:  # noqa: BLE001
                self.store.save_state(lst["id"], lst["state"], error=str(getattr(exc, "message", exc))[:300])
        return fired

    def _poll_file(self, lst: dict) -> Optional[str]:
        cfg = lst["config"]
        root, prefix = self.resolve_root(cfg.get("path") or ".")
        snap = snapshot(root, cfg.get("pattern") or "*")
        state = dict(lst["state"])
        if "snapshot" not in state:                       # first poll: take a baseline, fire nothing
            self.store.save_state(lst["id"], {"snapshot": snap, "pending": []})
            return None
        wanted = set(cfg.get("events") or ["created", "modified", "deleted"])
        changes = [c for c in diff_snapshots(state["snapshot"], snap) if c["change"] in wanted]
        mine = self._own_commits.get(lst["id"], {})
        now = self.clock()
        for c in changes:
            c["path"] = f"{prefix}{c['path']}" if prefix else c["path"]
        changes = [c for c in changes if now - mine.get(c["path"], -1e9) > 120]
        pending = (state.get("pending") or []) + changes
        if pending and now - float(lst.get("last_fired") or 0) >= float(lst["cooldown_s"]):
            pid = self._fire(lst, {"trigger": "file", "changes": len(pending), "path": pending[0]["path"],
                                   "files": ", ".join(f"{c['path']} ({c['change']})" for c in pending[:30])}, pending)
            self.store.save_state(lst["id"], {"snapshot": snap, "pending": []})
            return pid
        self.store.save_state(lst["id"], {"snapshot": snap, "pending": pending[-200:]})
        return None

    def _poll_email(self, lst: dict) -> Optional[str]:
        if self.mail is None:
            return None
        cfg = lst["config"]
        folder = cfg.get("folder") or "INBOX"
        t = self.mail.transport()
        uids = t.uids(folder)
        state = dict(lst["state"])
        if "seen" not in state:
            self.store.save_state(lst["id"], {"seen": uids[:1000]})
            return None
        seen = set(state["seen"])
        new = [u for u in uids if u not in seen][:5]
        if not new:
            return None
        pid = None
        f_from = (cfg.get("from_contains") or "").lower()
        f_subj = (cfg.get("subject_contains") or "").lower()
        for uid in reversed(new):
            meta, body = t.fetch(folder, uid)
            if f_from and f_from not in meta["from"].lower():
                continue
            if f_subj and f_subj not in meta["subject"].lower():
                continue
            pid = self._fire(lst, {"trigger": "email", "id": meta["id"], "from": meta["from"],
                                   "subject": meta["subject"], "date": meta["date"], "body": body}, [meta["id"]])
        self.store.save_state(lst["id"], {"seen": (new + list(state["seen"]))[:1000]})
        return pid

    # ---------------------------------------------------------------- fire
    def fire_webhook(self, lst: dict, payload: Dict[str, Any]) -> Optional[str]:
        now = self.clock()
        if now - float(lst.get("last_fired") or 0) < float(lst["cooldown_s"]):
            return None
        flat = {k: v for k, v in payload.items() if isinstance(v, (str, int, float, bool))}
        body = payload.get("body") or payload.get("text") or ""
        return self._fire(lst, {"trigger": "webhook", **flat, "body": body}, [])

    def _fire(self, lst: dict, event: Dict[str, Any], items: list) -> Optional[str]:
        request = render_request(lst["request"], event)
        pid = self.spawn(request, title=f"{lst['name']}", auto_commit=lst["auto_commit"],
                         origin={"kind": "listener", "id": lst["id"], "name": lst["name"], "trigger": event["trigger"]})
        detail = {k: (str(v)[:300]) for k, v in event.items() if k != "body"}
        self.store.mark_listener_fired(lst["id"], pid, self.clock(), detail)
        if self.publish:
            self.publish({"type": "automation.fired", "data": {"listener": lst["id"], "pid": pid, **detail}})
        return pid
