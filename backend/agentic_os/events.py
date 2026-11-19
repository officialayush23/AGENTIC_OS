"""Structured event system.

Every meaningful thing that happens to a task (node transitions, policy
decisions, tool calls, approvals, verification, retries...) is recorded as an
event: persisted to SQLite (audit trail) and pushed to live SSE subscribers.

Events are redacted and size-bounded before they are stored. Graph nodes run
in worker threads, SSE handlers run on the asyncio loop, so fan-out uses
`loop.call_soon_threadsafe`.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
from typing import Any, Dict, Optional, Set

from .persistence.repositories import EventRepo
from .security.redaction import redactor
from .util import bounded_json, truncate_text

log = logging.getLogger(__name__)

correlation_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("correlation_id", default=None)

# Event types forwarded to the global (dashboard) stream as well.
GLOBAL_EVENT_TYPES = {
    "task.created", "task.status", "task.completed", "task.failed", "task.cancelled",
    "approval.requested", "approval.decided", "clarification.requested", "task.deleted",
    # kernel lifecycle (dashboard / task list refresh)
    "proc.spawn", "proc.exit", "plan.ready", "txn.review", "txn.committed", "txn.discarded", "txn.undone",
    "txn.conflict", "txn.incomplete", "signal",
}

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


class _Subscriber:
    __slots__ = ("queue", "loop", "task_id")

    def __init__(self, queue: "asyncio.Queue[dict]", loop: asyncio.AbstractEventLoop, task_id: Optional[str]):
        self.queue = queue
        self.loop = loop
        self.task_id = task_id


class EventBus:
    def __init__(self, repo: EventRepo):
        self.repo = repo
        self._subs: Dict[Optional[str], Set[_Subscriber]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------- publish
    def publish(
        self,
        task_id: str,
        type_: str,
        message: str,
        *,
        level: str = "info",
        node: Optional[str] = None,
        step_id: Optional[str] = None,
        data: Optional[dict] = None,
        correlation_id: Optional[str] = None,
    ) -> dict:
        safe_data = bounded_json(redactor.redact(data or {}), max_chars=24000, max_str=3000, max_items=100)
        if not isinstance(safe_data, dict):
            safe_data = {"value": safe_data}
        safe_message = truncate_text(redactor.redact_text(message or type_), 600)
        event = self.repo.append(
            task_id,
            type_,
            safe_message,
            level=level,
            node=node,
            step_id=step_id,
            data=safe_data,
            correlation_id=correlation_id or correlation_id_var.get(),
        )
        self._fan_out(event)
        return event

    def publish_global(self, event: dict) -> None:
        """Push a synthetic event to global subscribers only (not persisted)."""
        self._fan_out(event, global_only=True)

    def _fan_out(self, event: dict, global_only: bool = False) -> None:
        with self._lock:
            targets = []
            if not global_only:
                targets.extend(self._subs.get(event.get("task_id"), ()))
            if global_only or event.get("type") in GLOBAL_EVENT_TYPES:
                targets.extend(self._subs.get(None, ()))
        for sub in targets:
            try:
                sub.loop.call_soon_threadsafe(self._offer, sub, event)
            except RuntimeError:
                # Loop closed (shutdown); drop silently.
                pass

    @staticmethod
    def _offer(sub: _Subscriber, event: dict) -> None:
        try:
            sub.queue.put_nowait(event)
        except asyncio.QueueFull:
            # Slow consumer: tell it to resync from the database.
            try:
                while not sub.queue.empty():
                    sub.queue.get_nowait()
                sub.queue.put_nowait({"type": "stream.resync", "task_id": sub.task_id})
            except Exception:  # pragma: no cover
                pass

    # ----------------------------------------------------------- subscribe
    def subscribe(self, task_id: Optional[str], loop: asyncio.AbstractEventLoop, maxsize: int = 500) -> _Subscriber:
        sub = _Subscriber(asyncio.Queue(maxsize=maxsize), loop, task_id)
        with self._lock:
            self._subs.setdefault(task_id, set()).add(sub)
        return sub

    def unsubscribe(self, sub: _Subscriber) -> None:
        with self._lock:
            subs = self._subs.get(sub.task_id)
            if subs is not None:
                subs.discard(sub)
                if not subs:
                    self._subs.pop(sub.task_id, None)

    def subscriber_count(self) -> int:
        with self._lock:
            return sum(len(s) for s in self._subs.values())

    def close_all(self) -> None:
        """Wake every subscriber with a shutdown marker (used on app shutdown)."""
        with self._lock:
            subs = [s for group in self._subs.values() for s in group]
        for sub in subs:
            try:
                sub.loop.call_soon_threadsafe(self._offer, sub, {"type": "stream.shutdown", "task_id": sub.task_id})
            except RuntimeError:
                pass
