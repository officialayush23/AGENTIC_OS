"""Copy-on-write overlay workspace (the process's "address space").

`OverlayWorkspace` is a drop-in `WorkspaceGuard`. Every path a tool resolves
goes through it, so it is the single place where the kernel:

1. runs all `WorkspaceGuard` safety checks against the *real* (lower) path,
2. checks the process's `fs` capabilities for that path and right,
3. maps the path into the overlay: reads see the upper copy if one exists,
   writes are redirected to the upper dir (copying the lower file up first
   and recording its base hash so commit can detect conflicts).

Existing tools need no changes: they only ever use `ctx.workspace.resolve()`,
`walk()`, `iter_files()` and `rel_of()`.
"""

from __future__ import annotations

import os
import shutil
from pathlib import PurePosixPath
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..tools.base import ToolError
from ..tools.fileio import read_text_file, sha256_file, unified_diff
from ..tools.pathguard import PathError, ResolvedPath, WorkspaceGuard, _within

TEMP_PREFIX = ".aos-"
DIFF_READ_LIMIT = 2 * 1024 * 1024

CapCheck = Callable[[str, str], None]   # (rel_path, right) -> raises on denial


def _is_temp(name: str) -> bool:
    return name.startswith(TEMP_PREFIX) and name.endswith(".tmp")


class OverlayWorkspace(WorkspaceGuard):
    def __init__(
        self,
        lower_root: str,
        upper_root: str,
        overlay_id: str,
        store,
        *,
        cap_check: Optional[CapCheck] = None,
        sensitive_patterns: Optional[Sequence[str]] = None,
        ignored_dirs: Optional[Sequence[str]] = None,
    ):
        super().__init__(lower_root, sensitive_patterns=sensitive_patterns, ignored_dirs=ignored_dirs)
        os.makedirs(upper_root, exist_ok=True)
        self.upper = os.path.realpath(upper_root)
        self.overlay_id = overlay_id
        self.store = store
        self.cap_check = cap_check
        self.transactional = True

    # -------------------------------------------------------------- helpers
    def _upper_abs(self, rel: str) -> str:
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        return os.path.join(self.upper, *parts) if parts else self.upper

    def _lower_abs(self, rel: str) -> str:
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        return os.path.join(self.root, *parts) if parts else self.root

    @staticmethod
    def _rp(abs_path: str, rel: str, sensitive: Optional[str]) -> ResolvedPath:
        return ResolvedPath(
            abs_path=abs_path, rel=rel, exists=os.path.exists(abs_path), is_dir=os.path.isdir(abs_path),
            is_file=os.path.isfile(abs_path), is_symlink=False, sensitive=sensitive,
        )

    def _mirror_parents(self, rel: str) -> None:
        """Create upper copies of parent folders that already exist in lower."""
        parts = rel.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            sub = "/".join(parts[:i])
            if os.path.isdir(self._lower_abs(sub)):
                os.makedirs(self._upper_abs(sub), exist_ok=True)
            else:
                break

    # -------------------------------------------------------------- resolve
    def resolve(self, user_path: Optional[str], *, for_write: bool = False) -> ResolvedPath:
        rp = super().resolve(user_path, for_write=for_write)      # all lower-path safety checks
        if self.cap_check is not None:
            self.cap_check(rp.rel, "write" if for_write else "read")
        if rp.rel == ".":
            return rp
        # rp.rel was validated component-by-component (no '..', ':', reserved names), so the
        # upper path is lexically inside the upper dir. Symlinks inside upper are refused below.
        upper_abs = self._upper_abs(rp.rel)
        if os.path.islink(os.path.dirname(upper_abs)):
            raise PathError("PATH_SYMLINK_WRITE", "Symbolic links are not allowed in the overlay.")

        if for_write:
            if rp.sensitive:
                # Never copy sensitive files into the overlay; the tool's own check will deny the write.
                return self._rp(upper_abs, rp.rel, rp.sensitive)
            if rp.is_dir:
                os.makedirs(upper_abs, exist_ok=True)
                return self._rp(upper_abs, rp.rel, None)
            self._mirror_parents(rp.rel)
            if not os.path.lexists(upper_abs):
                if rp.is_file:
                    self.store.record_base(self.overlay_id, rp.rel, True, sha256_file(rp.abs_path))
                    shutil.copy2(rp.abs_path, upper_abs)
                else:
                    self.store.record_base(self.overlay_id, rp.rel, False, None)
            return self._rp(upper_abs, rp.rel, None)

        if os.path.lexists(upper_abs):
            if os.path.islink(upper_abs):
                raise PathError("PATH_SYMLINK_WRITE", "Symbolic links are not allowed in the overlay.")
            if os.path.isdir(upper_abs) and rp.is_dir:
                return rp            # both exist: walk() merges the two views
            return self._rp(upper_abs, rp.rel, rp.sensitive)
        return rp

    def rel_of(self, abs_path: str) -> str:
        if _within(abs_path, self.upper):
            rel = os.path.relpath(abs_path, self.upper).replace("\\", "/")
            return "." if rel in ("", ".") else rel
        return super().rel_of(abs_path)

    # ----------------------------------------------------------------- walk
    def walk(self, start: ResolvedPath, *, max_depth: int, max_entries: int, include_hidden: bool = True,
             check=None) -> Tuple[List[dict], bool, List[str]]:
        rel = start.rel
        merged: Dict[str, dict] = {}
        order: List[str] = []
        truncated, skipped = False, []
        for base in (self._lower_abs(rel), self._upper_abs(rel)):
            if not os.path.isdir(base):
                continue
            view = ResolvedPath(abs_path=base, rel=rel, exists=True, is_dir=True, is_file=False,
                                is_symlink=False, sensitive=start.sensitive)
            entries, t, s = super().walk(view, max_depth=max_depth, max_entries=max_entries,
                                         include_hidden=include_hidden, check=check)
            truncated = truncated or t
            skipped.extend(x for x in s if x not in skipped)
            for item in entries:
                if _is_temp(item["name"]):
                    continue
                if item["path"] not in merged:
                    order.append(item["path"])
                merged[item["path"]] = item
        entries = [merged[p] for p in order]
        if len(entries) > max_entries:
            entries, truncated = entries[:max_entries], True
        return entries, truncated, skipped

    # ------------------------------------------------------- change set
    def changes(self) -> List[dict]:
        """Files whose upper content differs from their recorded base."""
        bases = self.store.overlay_bases(self.overlay_id)
        out: List[dict] = []
        for dirpath, dirnames, filenames in os.walk(self.upper):
            dirnames.sort()
            for name in sorted(filenames):
                if _is_temp(name):
                    continue
                upper_abs = os.path.join(dirpath, name)
                if os.path.islink(upper_abs):
                    continue
                rel = PurePosixPath(*os.path.relpath(upper_abs, self.upper).replace("\\", "/").split("/")).as_posix()
                base = bases.get(rel) or {"exists": False, "sha256": None}
                after = sha256_file(upper_abs)
                if base["exists"] and base["sha256"] == after:
                    continue
                out.append({
                    "rel_path": rel, "op": "modify" if base["exists"] else "create",
                    "base_exists": base["exists"], "base_sha256": base["sha256"], "after_sha256": after,
                    "upper_abs": upper_abs, "lower_abs": self._lower_abs(rel),
                    "size": os.path.getsize(upper_abs),
                })
        return out

    def has_changes(self) -> bool:
        return bool(self.changes())

    def diff(self, max_files: int = 50) -> List[dict]:
        out = []
        for ch in self.changes()[:max_files]:
            item = {"path": ch["rel_path"], "op": ch["op"], "size": ch["size"], "after_sha256": ch["after_sha256"]}
            try:
                after, _, _ = read_text_file(ch["upper_abs"], DIFF_READ_LIMIT)
                before = ""
                if ch["base_exists"] and os.path.isfile(ch["lower_abs"]):
                    before, _, _ = read_text_file(ch["lower_abs"], DIFF_READ_LIMIT)
                text, truncated = unified_diff(before, after, ch["rel_path"])
                item.update({"diff": text, "truncated": truncated})
            except (ToolError, OSError):
                item.update({"diff": None, "note": "binary or too large to diff"})
            out.append(item)
        return out

    def discard(self) -> None:
        shutil.rmtree(self.upper, ignore_errors=True)
        os.makedirs(self.upper, exist_ok=True)
        self.store.clear_overlay(self.overlay_id)

    def destroy(self) -> None:
        shutil.rmtree(os.path.dirname(self.upper) if os.path.basename(self.upper) == "upper" else self.upper,
                      ignore_errors=True)
        self.store.clear_overlay(self.overlay_id)
