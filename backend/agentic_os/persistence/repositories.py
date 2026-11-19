"""Repositories: the only code that writes SQL for application records."""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..util import json_dumps, json_loads, new_id, utc_now_iso
from .db import Database


def _bool(v: Any) -> bool:
    return bool(int(v)) if v is not None else False


class SettingsRepo:
    def __init__(self, db: Database):
        self.db = db

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.query_one("SELECT value_json FROM settings WHERE key = ?", (key,))
        return json_loads(row["value_json"], default) if row else default

    def get_all(self) -> Dict[str, Any]:
        return {r["key"]: json_loads(r["value_json"]) for r in self.db.query("SELECT key, value_json FROM settings")}

    def set(self, key: str, value: Any) -> None:  # noqa: A003
        self.db.execute(
            "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
            (key, json_dumps(value), utc_now_iso()),
        )

    def set_many(self, values: Dict[str, Any]) -> None:
        with self.db.transaction() as conn:
            now = utc_now_iso()
            for key, value in values.items():
                conn.execute(
                    "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
                    (key, json_dumps(value), now),
                )

    def delete(self, key: str) -> None:
        self.db.execute("DELETE FROM settings WHERE key = ?", (key,))


# --------------------------------------------------------------------------
PROVIDER_FIELDS = (
    "kind", "display_name", "model", "base_url", "timeout_s", "max_output_tokens",
    "temperature", "structured_mode", "max_calls_per_task", "enabled",
)


class ProviderRepo:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _row(r: Optional[sqlite3.Row]) -> Optional[dict]:
        if r is None:
            return None
        d = dict(r)
        d["extra"] = json_loads(d.pop("extra_json", "{}"), {})
        d["last_test"] = json_loads(d.pop("last_test_json", None), None)
        d["is_default"] = _bool(d.get("is_default"))
        d["enabled"] = _bool(d.get("enabled"))
        return d

    def list(self) -> List[dict]:  # noqa: A003
        return [self._row(r) for r in self.db.query("SELECT * FROM providers ORDER BY created_at")]  # type: ignore[misc]

    def get(self, provider_id: str) -> Optional[dict]:
        return self._row(self.db.query_one("SELECT * FROM providers WHERE id = ?", (provider_id,)))

    def get_default(self) -> Optional[dict]:
        return self._row(self.db.query_one("SELECT * FROM providers WHERE is_default = 1 AND enabled = 1 LIMIT 1"))

    def upsert(self, provider_id: str, values: dict) -> dict:
        now = utc_now_iso()
        existing = self.get(provider_id)
        extra = values.get("extra")
        with self.db.transaction() as conn:
            if existing is None:
                conn.execute(
                    "INSERT INTO providers(id, kind, display_name, model, base_url, timeout_s, max_output_tokens,"
                    " temperature, structured_mode, max_calls_per_task, extra_json, is_default, enabled, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
                    (
                        provider_id, values["kind"], values["display_name"], values["model"], values.get("base_url"),
                        float(values.get("timeout_s", 60)), int(values.get("max_output_tokens", 4096)),
                        values.get("temperature"), values.get("structured_mode", "auto"),
                        int(values.get("max_calls_per_task", 8)), json_dumps(extra or {}),
                        1 if values.get("enabled", True) else 0, now, now,
                    ),
                )
                has_default = conn.execute("SELECT COUNT(*) FROM providers WHERE is_default = 1").fetchone()[0]
                if not has_default:
                    conn.execute("UPDATE providers SET is_default = 1 WHERE id = ?", (provider_id,))
            else:
                sets, params = [], []
                for f in PROVIDER_FIELDS:
                    if f in values:
                        v = values[f]
                        if f == "enabled":
                            v = 1 if v else 0
                        sets.append(f"{f} = ?")
                        params.append(v)
                if extra is not None:
                    sets.append("extra_json = ?")
                    params.append(json_dumps(extra))
                sets.append("updated_at = ?")
                params.append(now)
                params.append(provider_id)
                conn.execute(f"UPDATE providers SET {', '.join(sets)} WHERE id = ?", params)
        return self.get(provider_id)  # type: ignore[return-value]

    def set_default(self, provider_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE providers SET is_default = 0")
            conn.execute("UPDATE providers SET is_default = 1 WHERE id = ?", (provider_id,))

    def set_last_test(self, provider_id: str, result: dict) -> None:
        self.db.execute(
            "UPDATE providers SET last_test_json = ?, updated_at = ? WHERE id = ?",
            (json_dumps(result), utc_now_iso(), provider_id),
        )

    def delete(self, provider_id: str) -> bool:
        with self.db.transaction() as conn:
            was_default = conn.execute(
                "SELECT is_default FROM providers WHERE id = ?", (provider_id,)
            ).fetchone()
            cur = conn.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
            if was_default and was_default[0]:
                nxt = conn.execute("SELECT id FROM providers WHERE enabled = 1 ORDER BY created_at LIMIT 1").fetchone()
                if nxt:
                    conn.execute("UPDATE providers SET is_default = 1 WHERE id = ?", (nxt[0],))
            return cur.rowcount > 0


# --------------------------------------------------------------------------
TASK_UPDATABLE = {
    "title", "status", "provider_id", "provider_kind", "model", "options", "attempt", "thread_id",
    "plan_version", "current_node", "current_step_id", "cancel_requested", "error_code",
    "error_message", "summary", "metrics", "started_at", "finished_at",
}
_TASK_JSON = {"options": "options_json", "summary": "summary_json", "metrics": "metrics_json"}

STEP_UPDATABLE = {
    "status", "attempts", "policy", "result", "verification_result", "error_code", "error_message",
    "started_at", "finished_at", "risk", "requires_approval",
}
_STEP_JSON = {"policy": "policy_json", "result": "result_json", "verification_result": "verification_result_json"}


class TaskRepo:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _row(r: Optional[sqlite3.Row]) -> Optional[dict]:
        if r is None:
            return None
        d = dict(r)
        d["options"] = json_loads(d.pop("options_json", "{}"), {})
        d["summary"] = json_loads(d.pop("summary_json", None), None)
        d["metrics"] = json_loads(d.pop("metrics_json", "{}"), {})
        d["cancel_requested"] = _bool(d.get("cancel_requested"))
        return d

    def create(self, values: dict) -> dict:
        now = utc_now_iso()
        self.db.execute(
            "INSERT INTO tasks(id, title, request, status, planner_mode, provider_id, provider_kind, model,"
            " options_json, client_request_id, attempt, thread_id, plan_version, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (
                values["id"], values["title"], values["request"], values.get("status", "queued"),
                values.get("planner_mode", "auto"), values.get("provider_id"), values.get("provider_kind"),
                values.get("model"), json_dumps(values.get("options", {})), values.get("client_request_id"),
                int(values.get("attempt", 1)), values["thread_id"], now, now,
            ),
        )
        return self.get(values["id"])  # type: ignore[return-value]

    def get(self, task_id: str) -> Optional[dict]:
        return self._row(self.db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,)))

    def get_by_client_request_id(self, client_request_id: str) -> Optional[dict]:
        return self._row(self.db.query_one("SELECT * FROM tasks WHERE client_request_id = ?", (client_request_id,)))

    def list(  # noqa: A003
        self, query: Optional[str] = None, status: Optional[Sequence[str]] = None, limit: int = 50, offset: int = 0
    ) -> Tuple[List[dict], int]:
        where, params = [], []
        if query:
            for term in query.split()[:6]:
                where.append("(title LIKE ? ESCAPE '\\' OR request LIKE ? ESCAPE '\\')")
                esc = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                params.extend([f"%{esc}%", f"%{esc}%"])
        if status:
            where.append(f"status IN ({', '.join('?' for _ in status)})")
            params.extend(status)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = self.db.query_one(f"SELECT COUNT(*) AS n FROM tasks{clause}", params)["n"]  # type: ignore[index]
        rows = self.db.query(
            f"SELECT * FROM tasks{clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*params, int(limit), int(offset)],
        )
        return [self._row(r) for r in rows], int(total)  # type: ignore[misc]

    def list_by_status(self, statuses: Iterable[str]) -> List[dict]:
        st = list(statuses)
        rows = self.db.query(
            f"SELECT * FROM tasks WHERE status IN ({', '.join('?' for _ in st)}) ORDER BY created_at", st
        )
        return [self._row(r) for r in rows]  # type: ignore[misc]

    def count_by_status(self) -> Dict[str, int]:
        return {r["status"]: int(r["n"]) for r in self.db.query("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")}

    def update(self, task_id: str, **fields: Any) -> Optional[dict]:
        sets, params = [], []
        for key, value in fields.items():
            if key not in TASK_UPDATABLE:
                raise ValueError(f"task field {key!r} is not updatable")
            col = _TASK_JSON.get(key, key)
            if key in _TASK_JSON:
                value = json_dumps(value) if value is not None else None
            if key == "cancel_requested":
                value = 1 if value else 0
            sets.append(f"{col} = ?")
            params.append(value)
        if not sets:
            return self.get(task_id)
        sets.append("updated_at = ?")
        params.append(utc_now_iso())
        params.append(task_id)
        self.db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", params)
        return self.get(task_id)

    def delete(self, task_id: str) -> bool:
        return self.db.execute("DELETE FROM tasks WHERE id = ?", (task_id,)).rowcount > 0

    def delete_all(self) -> int:
        return self.db.execute("DELETE FROM tasks").rowcount

    # ----------------------------------------------------------------- steps
    @staticmethod
    def _step_row(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["args"] = json_loads(d.pop("args_json", "{}"), {})
        d["depends_on"] = json_loads(d.pop("depends_on_json", "[]"), [])
        d["verification"] = json_loads(d.pop("verification_json", None), None)
        d["policy"] = json_loads(d.pop("policy_json", None), None)
        d["result"] = json_loads(d.pop("result_json", None), None)
        d["verification_result"] = json_loads(d.pop("verification_result_json", None), None)
        d["requires_approval"] = _bool(d.get("requires_approval"))
        return d

    def replace_plan_steps(self, task_id: str, plan_version: int, steps: List[dict]) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM task_steps WHERE task_id = ? AND plan_version = ?", (task_id, plan_version))
            for pos, s in enumerate(steps):
                conn.execute(
                    "INSERT INTO task_steps(task_id, plan_version, step_id, position, title, tool, args_json,"
                    " depends_on_json, expected_outcome, risk, requires_approval, verification_json, status,"
                    " attempts, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                    (
                        task_id, plan_version, s["id"], pos, s.get("title") or s["id"], s["tool"],
                        json_dumps(s.get("args", {})), json_dumps(s.get("depends_on", [])),
                        s.get("expected_outcome"), s.get("risk"), 1 if s.get("requires_approval") else 0,
                        json_dumps(s.get("verification")) if s.get("verification") else None,
                        s.get("status", "planned"), now,
                    ),
                )

    def update_step(self, task_id: str, plan_version: int, step_id: str, **fields: Any) -> None:
        sets, params = [], []
        for key, value in fields.items():
            if key not in STEP_UPDATABLE:
                raise ValueError(f"step field {key!r} is not updatable")
            col = _STEP_JSON.get(key, key)
            if key in _STEP_JSON:
                value = json_dumps(value) if value is not None else None
            if key == "requires_approval":
                value = 1 if value else 0
            sets.append(f"{col} = ?")
            params.append(value)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.append(utc_now_iso())
        params.extend([task_id, plan_version, step_id])
        self.db.execute(
            f"UPDATE task_steps SET {', '.join(sets)} WHERE task_id = ? AND plan_version = ? AND step_id = ?", params
        )

    def list_steps(self, task_id: str, plan_version: Optional[int] = None) -> List[dict]:
        if plan_version is None:
            row = self.db.query_one("SELECT MAX(plan_version) AS v FROM task_steps WHERE task_id = ?", (task_id,))
            if not row or row["v"] is None:
                return []
            plan_version = int(row["v"])
        rows = self.db.query(
            "SELECT * FROM task_steps WHERE task_id = ? AND plan_version = ? ORDER BY position", (task_id, plan_version)
        )
        return [self._step_row(r) for r in rows]

    def plan_versions(self, task_id: str) -> List[int]:
        rows = self.db.query(
            "SELECT DISTINCT plan_version FROM task_steps WHERE task_id = ? ORDER BY plan_version", (task_id,)
        )
        return [int(r["plan_version"]) for r in rows]


# --------------------------------------------------------------------------
class EventRepo:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["data"] = json_loads(d.pop("data_json", "{}"), {})
        return d

    def append(
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
        ts = utc_now_iso()
        with self.db.transaction() as conn:
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM task_events WHERE task_id = ?", (task_id,)
            ).fetchone()[0]
            cur = conn.execute(
                "INSERT INTO task_events(task_id, seq, ts, type, level, node, step_id, message, data_json, correlation_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, seq, ts, type_, level, node, step_id, message, json_dumps(data or {}), correlation_id),
            )
            event_id = cur.lastrowid
        return {
            "id": event_id, "task_id": task_id, "seq": int(seq), "ts": ts, "type": type_, "level": level,
            "node": node, "step_id": step_id, "message": message, "data": data or {}, "correlation_id": correlation_id,
        }

    def list(self, task_id: str, after_seq: int = 0, limit: int = 2000) -> List[dict]:  # noqa: A003
        rows = self.db.query(
            "SELECT * FROM task_events WHERE task_id = ? AND seq > ? ORDER BY seq LIMIT ?",
            (task_id, int(after_seq), int(limit)),
        )
        return [self._row(r) for r in rows]

    def last_seq(self, task_id: str) -> int:
        row = self.db.query_one("SELECT COALESCE(MAX(seq), 0) AS s FROM task_events WHERE task_id = ?", (task_id,))
        return int(row["s"]) if row else 0


# --------------------------------------------------------------------------
class ApprovalRepo:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _row(r: Optional[sqlite3.Row]) -> Optional[dict]:
        if r is None:
            return None
        d = dict(r)
        d["args_preview"] = json_loads(d.pop("args_preview_json", "{}"), {})
        d["reasons"] = json_loads(d.pop("reasons_json", "[]"), [])
        d["preview"] = json_loads(d.pop("preview_json", None), None)
        return d

    def create(self, values: dict) -> dict:
        approval_id = values.get("id") or new_id("apr")
        self.db.execute(
            "INSERT INTO approvals(id, task_id, kind, step_id, plan_version, tool, action_hash, args_preview_json,"
            " risk, reasons_json, effect, preview_json, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (
                approval_id, values["task_id"], values["kind"], values.get("step_id"), int(values["plan_version"]),
                values.get("tool"), values["action_hash"], json_dumps(values.get("args_preview", {})),
                values.get("risk"), json_dumps(values.get("reasons", [])), values.get("effect"),
                json_dumps(values.get("preview")) if values.get("preview") is not None else None, utc_now_iso(),
            ),
        )
        return self.get(approval_id)  # type: ignore[return-value]

    def get(self, approval_id: str) -> Optional[dict]:
        return self._row(self.db.query_one("SELECT * FROM approvals WHERE id = ?", (approval_id,)))

    def find_latest_for_hash(self, task_id: str, action_hash: str) -> Optional[dict]:
        return self._row(
            self.db.query_one(
                "SELECT * FROM approvals WHERE task_id = ? AND action_hash = ? ORDER BY created_at DESC LIMIT 1",
                (task_id, action_hash),
            )
        )

    def list(self, task_id: Optional[str] = None, status: Optional[str] = None, limit: int = 200) -> List[dict]:  # noqa: A003
        where, params = [], []
        if task_id:
            where.append("task_id = ?")
            params.append(task_id)
        if status:
            where.append("status = ?")
            params.append(status)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self.db.query(f"SELECT * FROM approvals{clause} ORDER BY created_at DESC LIMIT ?", [*params, int(limit)])
        return [self._row(r) for r in rows]  # type: ignore[misc]

    def decide(self, approval_id: str, status: str, note: Optional[str] = None) -> Optional[dict]:
        """Atomically move a pending approval to approved/rejected. Returns None if it was not pending."""
        if status not in ("approved", "rejected", "superseded", "expired"):
            raise ValueError("invalid approval status")
        cur = self.db.execute(
            "UPDATE approvals SET status = ?, decision_note = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
            (status, note, utc_now_iso(), approval_id),
        )
        if cur.rowcount == 0:
            return None
        return self.get(approval_id)

    def expire_pending(self, task_id: str, status: str = "expired") -> int:
        return self.db.execute(
            "UPDATE approvals SET status = ?, decided_at = ? WHERE task_id = ? AND status = 'pending'",
            (status, utc_now_iso(), task_id),
        ).rowcount

    def count_pending(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM approvals WHERE status = 'pending'")
        return int(row["n"]) if row else 0


# --------------------------------------------------------------------------
class ExecutionRepo:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _row(r: Optional[sqlite3.Row]) -> Optional[dict]:
        if r is None:
            return None
        d = dict(r)
        d["result"] = json_loads(d.pop("result_json", None), None)
        return d

    def get(self, key: str) -> Optional[dict]:
        return self._row(self.db.query_one("SELECT * FROM tool_executions WHERE idempotency_key = ?", (key,)))

    def start(self, key: str, task_id: str, step_id: str, tool: str, tool_version: str, args_hash: str) -> dict:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT attempt FROM tool_executions WHERE idempotency_key = ?", (key,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO tool_executions(idempotency_key, task_id, step_id, tool, tool_version, status,"
                    " args_hash, attempt, started_at) VALUES (?, ?, ?, ?, ?, 'started', ?, 1, ?)",
                    (key, task_id, step_id, tool, tool_version, args_hash, now),
                )
            else:
                conn.execute(
                    "UPDATE tool_executions SET status = 'started', step_id = ?, attempt = attempt + 1,"
                    " started_at = ?, finished_at = NULL, result_json = NULL WHERE idempotency_key = ?",
                    (step_id, now, key),
                )
        return self.get(key)  # type: ignore[return-value]

    def finish(self, key: str, status: str, result: dict) -> None:
        self.db.execute(
            "UPDATE tool_executions SET status = ?, result_json = ?, finished_at = ? WHERE idempotency_key = ?",
            (status, json_dumps(result), utc_now_iso(), key),
        )

    def list(self, task_id: str) -> List[dict]:  # noqa: A003
        rows = self.db.query("SELECT * FROM tool_executions WHERE task_id = ? ORDER BY started_at", (task_id,))
        return [self._row(r) for r in rows]  # type: ignore[misc]


# --------------------------------------------------------------------------
class SessionRepo:
    def __init__(self, db: Database):
        self.db = db

    def create(self, token_hash: str, expires_at: str, user_agent: Optional[str]) -> None:
        now = utc_now_iso()
        self.db.execute(
            "INSERT INTO sessions(token_hash, created_at, expires_at, last_seen_at, user_agent) VALUES (?, ?, ?, ?, ?)",
            (token_hash, now, expires_at, now, (user_agent or "")[:200]),
        )

    def get(self, token_hash: str) -> Optional[dict]:
        row = self.db.query_one("SELECT * FROM sessions WHERE token_hash = ?", (token_hash,))
        return dict(row) if row else None

    def touch(self, token_hash: str) -> None:
        self.db.execute("UPDATE sessions SET last_seen_at = ? WHERE token_hash = ?", (utc_now_iso(), token_hash))

    def delete(self, token_hash: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def purge_expired(self, now_iso: str) -> int:
        return self.db.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso,)).rowcount

    def delete_all(self) -> int:
        return self.db.execute("DELETE FROM sessions").rowcount


# --------------------------------------------------------------------------
class AppRepo:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _row(r: Optional[sqlite3.Row]) -> Optional[dict]:
        if r is None:
            return None
        d = dict(r)
        d["requires_approval"] = _bool(d.get("requires_approval"))
        d["enabled"] = _bool(d.get("enabled"))
        d["builtin"] = _bool(d.get("builtin"))
        return d

    def list(self) -> List[dict]:  # noqa: A003
        return [self._row(r) for r in self.db.query("SELECT * FROM allowed_apps ORDER BY name")]  # type: ignore[misc]

    def get(self, app_id: str) -> Optional[dict]:
        return self._row(self.db.query_one("SELECT * FROM allowed_apps WHERE id = ?", (app_id,)))

    def upsert(self, app_id: str, values: dict) -> dict:
        now = utc_now_iso()
        self.db.execute(
            "INSERT INTO allowed_apps(id, name, path, args_policy, requires_approval, enabled, builtin, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET name = excluded.name, path = excluded.path,"
            " args_policy = excluded.args_policy, requires_approval = excluded.requires_approval,"
            " enabled = excluded.enabled, updated_at = excluded.updated_at",
            (
                app_id, values["name"], values["path"], values.get("args_policy", "none"),
                1 if values.get("requires_approval") else 0, 1 if values.get("enabled", True) else 0,
                1 if values.get("builtin") else 0, now, now,
            ),
        )
        return self.get(app_id)  # type: ignore[return-value]

    def delete(self, app_id: str) -> bool:
        return self.db.execute("DELETE FROM allowed_apps WHERE id = ?", (app_id,)).rowcount > 0


# --------------------------------------------------------------------------
class MemoryRepo:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["approved_by_user"] = _bool(d.get("approved_by_user"))
        return d

    def add(
        self,
        kind: str,
        content: str,
        *,
        key: Optional[str] = None,
        source_task_id: Optional[str] = None,
        approved_by_user: bool = False,
    ) -> dict:
        item_id = new_id("mem")
        now = utc_now_iso()
        self.db.execute(
            "INSERT INTO memory_items(id, kind, key, content, source_task_id, approved_by_user, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, kind, key, content, source_task_id, 1 if approved_by_user else 0, now, now),
        )
        row = self.db.query_one("SELECT * FROM memory_items WHERE id = ?", (item_id,))
        return self._row(row)  # type: ignore[arg-type]

    def upsert_preference(self, key: str, content: str) -> dict:
        existing = self.db.query_one("SELECT id FROM memory_items WHERE kind = 'preference' AND key = ?", (key,))
        if existing:
            self.db.execute(
                "UPDATE memory_items SET content = ?, approved_by_user = 1, updated_at = ? WHERE id = ?",
                (content, utc_now_iso(), existing["id"]),
            )
            row = self.db.query_one("SELECT * FROM memory_items WHERE id = ?", (existing["id"],))
            return self._row(row)  # type: ignore[arg-type]
        return self.add("preference", content, key=key, approved_by_user=True)

    def list(self, kind: Optional[str] = None, limit: int = 100) -> List[dict]:  # noqa: A003
        if kind:
            rows = self.db.query(
                "SELECT * FROM memory_items WHERE kind = ? ORDER BY created_at DESC LIMIT ?", (kind, int(limit))
            )
        else:
            rows = self.db.query("SELECT * FROM memory_items ORDER BY created_at DESC LIMIT ?", (int(limit),))
        return [self._row(r) for r in rows]

    def search(self, text: str, kind: str = "task_summary", limit: int = 3) -> List[dict]:
        terms = [t for t in (text or "").lower().split() if len(t) >= 4][:5]
        if not terms:
            return []
        where = " OR ".join("LOWER(content) LIKE ?" for _ in terms)
        rows = self.db.query(
            f"SELECT * FROM memory_items WHERE kind = ? AND ({where}) ORDER BY created_at DESC LIMIT ?",
            [kind, *[f"%{t}%" for t in terms], int(limit)],
        )
        return [self._row(r) for r in rows]

    def delete(self, item_id: str) -> bool:
        return self.db.execute("DELETE FROM memory_items WHERE id = ?", (item_id,)).rowcount > 0

    def delete_all(self, kind: Optional[str] = None) -> int:
        if kind:
            return self.db.execute("DELETE FROM memory_items WHERE kind = ?", (kind,)).rowcount
        return self.db.execute("DELETE FROM memory_items").rowcount


class Repositories:
    """Convenience container."""

    def __init__(self, db: Database):
        self.db = db
        self.settings = SettingsRepo(db)
        self.providers = ProviderRepo(db)
        self.tasks = TaskRepo(db)
        self.events = EventRepo(db)
        self.approvals = ApprovalRepo(db)
        self.executions = ExecutionRepo(db)
        self.sessions = SessionRepo(db)
        self.apps = AppRepo(db)
        self.memory = MemoryRepo(db)
