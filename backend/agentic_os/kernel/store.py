"""SQLite persistence for kernel state (migration 2 tables)."""

from __future__ import annotations

from typing import Dict, List, Optional

from ..persistence.db import Database
from ..util import json_dumps, json_loads, utc_now_iso


class KernelStore:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------ processes
    def save_process(self, pcb: dict) -> None:
        now = utc_now_iso()
        self.db.execute(
            "INSERT INTO kernel_processes(pid, ppid, task_id, state, phase, pcb_json, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(pid) DO UPDATE SET state=excluded.state, phase=excluded.phase,"
            " pcb_json=excluded.pcb_json, updated_at=excluded.updated_at",
            (pcb["pid"], pcb.get("ppid"), pcb["task_id"], pcb["state"], pcb["phase"], json_dumps(pcb), now, now),
        )

    def load_processes(self, include_exited: bool = False) -> List[dict]:
        sql = "SELECT pcb_json FROM kernel_processes"
        if not include_exited:
            sql += " WHERE state != 'EXITED'"
        sql += " ORDER BY created_at, pid"
        return [json_loads(r["pcb_json"], {}) for r in self.db.query(sql)]

    def get_process(self, pid: str) -> Optional[dict]:
        row = self.db.query_one("SELECT pcb_json FROM kernel_processes WHERE pid = ?", (pid,))
        return json_loads(row["pcb_json"], None) if row else None

    # --------------------------------------------------------- step outputs
    def save_output(self, pid: str, step_id: str, output: dict) -> None:
        self.db.execute(
            "INSERT INTO kernel_step_outputs(pid, step_id, output_json, created_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(pid, step_id) DO UPDATE SET output_json=excluded.output_json, created_at=excluded.created_at",
            (pid, step_id, json_dumps(output), utc_now_iso()),
        )

    def get_output(self, pid: str, step_id: str) -> Optional[dict]:
        row = self.db.query_one("SELECT output_json FROM kernel_step_outputs WHERE pid = ? AND step_id = ?",
                                (pid, step_id))
        return json_loads(row["output_json"], None) if row else None

    # --------------------------------------------------------- capabilities
    def save_capability(self, cap: dict, now: str) -> None:
        self.db.execute(
            "INSERT INTO kernel_capabilities(cap_id, pid, kind, scope, rights, expires_at, uses_left, parent_cap,"
            " origin, revoked, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(cap_id) DO UPDATE SET uses_left=excluded.uses_left, revoked=excluded.revoked",
            (cap["cap_id"], cap["pid"], cap["kind"], cap["scope"], ",".join(cap["rights"]), cap.get("expires_at"),
             cap.get("uses_left"), cap.get("parent_cap"), cap.get("origin", "spawn"), 1 if cap.get("revoked") else 0, now),
        )

    def load_capabilities(self) -> List[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM kernel_capabilities")]

    # -------------------------------------------------------------- overlay
    def record_base(self, overlay_id: str, rel: str, base_exists: bool, base_sha: Optional[str]) -> bool:
        """Record the base state of a path the first time it is written. Returns True if newly recorded."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO kernel_overlay_files(overlay_id, rel_path, base_exists, base_sha256, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (overlay_id, rel, 1 if base_exists else 0, base_sha, utc_now_iso()),
        )
        return cur.rowcount > 0

    def overlay_bases(self, overlay_id: str) -> Dict[str, dict]:
        rows = self.db.query("SELECT * FROM kernel_overlay_files WHERE overlay_id = ?", (overlay_id,))
        return {r["rel_path"]: {"exists": bool(r["base_exists"]), "sha256": r["base_sha256"]} for r in rows}

    def clear_overlay(self, overlay_id: str) -> None:
        self.db.execute("DELETE FROM kernel_overlay_files WHERE overlay_id = ?", (overlay_id,))

    # ------------------------------------------------------------- journal
    def create_txn(self, txn_id: str, pid: str, task_id: str, overlay_id: str, entries: List[dict],
                   summary: dict) -> None:
        now = utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO kernel_txns(txn_id, pid, task_id, overlay_id, state, summary_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'PREPARING', ?, ?, ?)",
                (txn_id, pid, task_id, overlay_id, json_dumps(summary), now, now),
            )
            for e in entries:
                conn.execute(
                    "INSERT INTO kernel_journal(txn_id, seq, rel_path, op, before_sha256, after_sha256, before_blob,"
                    " after_blob, created_dirs_json, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', 'PREPARED')",
                    (txn_id, e["seq"], e["rel_path"], e["op"], e.get("before_sha256"), e["after_sha256"],
                     e.get("before_blob"), e["after_blob"]),
                )

    def set_txn_state(self, txn_id: str, state: str) -> None:
        self.db.execute("UPDATE kernel_txns SET state = ?, updated_at = ? WHERE txn_id = ?", (state, utc_now_iso(), txn_id))

    def set_entry(self, txn_id: str, seq: int, state: str, created_dirs: Optional[List[str]] = None) -> None:
        if created_dirs is None:
            self.db.execute("UPDATE kernel_journal SET state = ? WHERE txn_id = ? AND seq = ?", (state, txn_id, seq))
        else:
            self.db.execute("UPDATE kernel_journal SET state = ?, created_dirs_json = ? WHERE txn_id = ? AND seq = ?",
                            (state, json_dumps(created_dirs), txn_id, seq))

    def get_txn(self, txn_id: str) -> Optional[dict]:
        row = self.db.query_one("SELECT * FROM kernel_txns WHERE txn_id = ?", (txn_id,))
        if not row:
            return None
        d = dict(row)
        d["summary"] = json_loads(d.pop("summary_json", "{}"), {})
        return d

    def txn_entries(self, txn_id: str) -> List[dict]:
        rows = self.db.query("SELECT * FROM kernel_journal WHERE txn_id = ? ORDER BY seq", (txn_id,))
        out = []
        for r in rows:
            d = dict(r)
            d["created_dirs"] = json_loads(d.pop("created_dirs_json", "[]"), [])
            out.append(d)
        return out

    def txns_in_state(self, states: List[str]) -> List[dict]:
        rows = self.db.query(
            f"SELECT txn_id FROM kernel_txns WHERE state IN ({', '.join('?' for _ in states)}) ORDER BY created_at",
            states,
        )
        return [self.get_txn(r["txn_id"]) for r in rows]  # type: ignore[misc]

    def delete_txn(self, txn_id: str) -> None:
        self.db.execute("DELETE FROM kernel_txns WHERE txn_id = ?", (txn_id,))
