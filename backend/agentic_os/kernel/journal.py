"""Write-ahead journal: two-phase commit of an overlay, roll-forward recovery, undo.

Transaction states:
    PREPARING  -> blobs copied, journal rows written, nothing applied (recovery: abort)
    COMMITTING -> commit point passed (recovery: roll forward)
    COMMITTED  -> every row APPLIED
    UNDOING    -> undo in progress (recovery: roll the undo forward)
    UNDONE     -> every row COMPENSATED
Row states: PREPARED -> APPLIED -> COMPENSATED
"""

from __future__ import annotations

import os
import secrets
import shutil
from pathlib import Path
from typing import Callable, List, Optional

from ..tools.fileio import atomic_replace, sha256_file

FaultHook = Callable[[str], None]


class CommitConflict(Exception):
    def __init__(self, conflicts: List[dict], message: str = "files changed outside the task"):
        super().__init__(message)
        self.conflicts = conflicts


class JournalError(Exception):
    pass


def _sha_or_none(path: str) -> Optional[str]:
    return sha256_file(path) if os.path.isfile(path) else None


class Journal:
    def __init__(self, store, data_dir: str, fault: Optional[FaultHook] = None):
        self.store = store
        self.base_dir = os.path.join(str(data_dir), "journal")
        self.fault = fault or (lambda point: None)

    # --------------------------------------------------------------- commit
    def commit(self, overlay, pid: str, task_id: str) -> Optional[dict]:
        changes = overlay.changes()
        if not changes:
            return None
        conflicts = []
        for ch in changes:
            current = _sha_or_none(ch["lower_abs"])
            if ch["base_exists"] and current != ch["base_sha256"]:
                conflicts.append({"path": ch["rel_path"], "reason": "changed on disk since the task read it"})
            elif not ch["base_exists"] and os.path.lexists(ch["lower_abs"]):
                conflicts.append({"path": ch["rel_path"], "reason": "a file with this name appeared on disk meanwhile"})
        if conflicts:
            raise CommitConflict(conflicts)

        txn_id = "txn_" + secrets.token_hex(10)
        jdir = os.path.join(self.base_dir, txn_id)
        os.makedirs(jdir, exist_ok=True)
        entries = []
        for seq, ch in enumerate(changes, start=1):
            before_blob = None
            if ch["base_exists"]:
                before_blob = os.path.join(jdir, f"{seq}.before")
                shutil.copy2(ch["lower_abs"], before_blob)
            after_blob = os.path.join(jdir, f"{seq}.after")
            shutil.copy2(ch["upper_abs"], after_blob)
            entries.append({
                "seq": seq, "rel_path": ch["rel_path"], "op": ch["op"], "before_sha256": ch["base_sha256"],
                "after_sha256": ch["after_sha256"], "before_blob": before_blob, "after_blob": after_blob,
            })
        summary = {"root": overlay.root, "files": [{"path": e["rel_path"], "op": e["op"]} for e in entries]}
        self.store.create_txn(txn_id, pid, task_id, overlay.overlay_id, entries, summary)
        self.fault("prepared")
        self.store.set_txn_state(txn_id, "COMMITTING")          # commit point
        self.fault("commit_point")
        self._apply(txn_id)
        overlay.discard()
        return {"txn_id": txn_id, "files": summary["files"]}

    def _root(self, txn: dict) -> str:
        root = (txn.get("summary") or {}).get("root")
        if not root:
            raise JournalError(f"transaction {txn['txn_id']} has no workspace root")
        return root

    def _apply(self, txn_id: str) -> None:
        txn = self.store.get_txn(txn_id)
        root = self._root(txn)  # type: ignore[arg-type]
        for e in self.store.txn_entries(txn_id):
            if e["state"] == "APPLIED":
                continue
            lower = os.path.join(root, *e["rel_path"].split("/"))
            if _sha_or_none(lower) == e["after_sha256"]:
                self.store.set_entry(txn_id, e["seq"], "APPLIED")   # already applied before a crash
                continue
            created = []
            parent = Path(lower).parent
            missing = []
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for folder in reversed(missing):
                folder.mkdir(exist_ok=True)
                created.append(os.path.relpath(str(folder), root).replace("\\", "/"))
            with open(e["after_blob"], "rb") as fh:
                data = fh.read()
            atomic_replace(lower, data)
            self.fault(f"applied:{e['seq']}")
            self.store.set_entry(txn_id, e["seq"], "APPLIED", created)
        self.store.set_txn_state(txn_id, "COMMITTED")

    # ----------------------------------------------------------------- undo
    def undo(self, txn_id: str) -> dict:
        txn = self.store.get_txn(txn_id)
        if txn is None:
            raise JournalError("unknown transaction")
        if txn["state"] not in ("COMMITTED", "UNDOING"):
            raise JournalError(f"cannot undo a transaction in state {txn['state']}")
        root = self._root(txn)
        conflicts = []
        for e in self.store.txn_entries(txn_id):
            if e["state"] == "COMPENSATED":
                continue
            lower = os.path.join(root, *e["rel_path"].split("/"))
            current = _sha_or_none(lower)
            if current not in (e["after_sha256"], e["before_sha256"]):
                conflicts.append({"path": e["rel_path"], "reason": "edited after the commit; undo would lose those edits"})
        if conflicts:
            raise CommitConflict(conflicts, "files changed after the commit")
        self.store.set_txn_state(txn_id, "UNDOING")
        self._undo_apply(txn_id)
        return {"txn_id": txn_id, "files": [{"path": e["rel_path"], "op": e["op"]} for e in self.store.txn_entries(txn_id)]}

    def _undo_apply(self, txn_id: str) -> None:
        txn = self.store.get_txn(txn_id)
        root = self._root(txn)  # type: ignore[arg-type]
        for e in reversed(self.store.txn_entries(txn_id)):
            if e["state"] == "COMPENSATED":
                continue
            lower = os.path.join(root, *e["rel_path"].split("/"))
            if e["op"] == "modify":
                if _sha_or_none(lower) != e["before_sha256"]:
                    with open(e["before_blob"], "rb") as fh:
                        atomic_replace(lower, fh.read())
            else:  # create -> remove the created file
                if os.path.isfile(lower):
                    os.remove(lower)
            for rel_dir in reversed(e["created_dirs"]):
                d = os.path.join(root, *rel_dir.split("/"))
                try:
                    os.rmdir(d)         # only removes empty folders
                except OSError:
                    pass
            self.fault(f"undone:{e['seq']}")
            self.store.set_entry(txn_id, e["seq"], "COMPENSATED")
        self.store.set_txn_state(txn_id, "UNDONE")

    # -------------------------------------------------------------- recovery
    def recover(self) -> List[dict]:
        actions = []
        for txn in self.store.txns_in_state(["PREPARING", "COMMITTING", "UNDOING"]):
            if txn["state"] == "PREPARING":
                shutil.rmtree(os.path.join(self.base_dir, txn["txn_id"]), ignore_errors=True)
                self.store.delete_txn(txn["txn_id"])
                actions.append({"txn_id": txn["txn_id"], "action": "aborted", "pid": txn["pid"]})
            elif txn["state"] == "COMMITTING":
                self._apply(txn["txn_id"])
                actions.append({"txn_id": txn["txn_id"], "action": "rolled_forward", "pid": txn["pid"]})
            else:
                self._undo_apply(txn["txn_id"])
                actions.append({"txn_id": txn["txn_id"], "action": "undo_rolled_forward", "pid": txn["pid"]})
        return actions

    def resume_commit(self, txn_id: str) -> None:
        txn = self.store.get_txn(txn_id)
        if txn and txn["state"] == "COMMITTING":
            self._apply(txn_id)
