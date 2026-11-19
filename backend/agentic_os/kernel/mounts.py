"""Mounts: one path namespace over two folders (a tiny VFS).

    notes.md        -> the user's workspace (project folder)
    ~/Notes/a.md    -> the Agentic OS Home folder (notes, calendar, mail drafts)

`MountedWorkspace` is a drop-in for `WorkspaceGuard` / `OverlayWorkspace`. Every
existing tool resolves paths through it, so the same file tools, capability
checks, staging, review, commit and undo apply to both mounts. Home paths keep
their `~/` prefix in every result, diff and journal entry.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Iterator, List, Optional, Tuple

from ..tools.pathguard import ResolvedPath, _within

HOME = "~"


def is_home(path: Optional[str]) -> bool:
    p = (path or "").replace("\\", "/").strip()
    return p == HOME or p.startswith(HOME + "/")


def split_home(path: Optional[str]) -> Tuple[bool, str]:
    p = (path or ".").replace("\\", "/").strip() or "."
    if p == HOME:
        return True, "."
    if p.startswith(HOME + "/"):
        return True, p[2:].strip("/") or "."
    return False, p


def to_home(rel: str) -> str:
    return HOME if rel in (".", "") else f"{HOME}/{rel}"


class MountedWorkspace:
    """Routes `~/...` to the home guard and everything else to the workspace guard."""

    transactional = False

    def __init__(self, workspace, home):
        self.ws = workspace
        self.home = home
        self.root = workspace.root          # tools use this as the working directory
        self.transactional = bool(getattr(workspace, "transactional", False))
        self.overlay_id = getattr(workspace, "overlay_id", None)

    # guard API -----------------------------------------------------------
    @property
    def name(self) -> str:
        return self.ws.name

    @property
    def roots(self) -> dict:
        return {"": self.ws.root, HOME: self.home.root}

    def sensitive_reason(self, parts):
        return self.ws.sensitive_reason(parts)

    def is_ignored_dir(self, name: str) -> bool:
        return self.ws.is_ignored_dir(name)

    def _pick(self, path: Optional[str]):
        home, rel = split_home(path)
        return (self.home, rel, True) if home else (self.ws, rel, False)

    def resolve(self, user_path: Optional[str], *, for_write: bool = False) -> ResolvedPath:
        guard, rel, home = self._pick(user_path)
        rp = guard.resolve(rel, for_write=for_write)
        return dataclasses.replace(rp, rel=to_home(rp.rel)) if home else rp

    def rel_of(self, abs_path: str) -> str:
        for attr in ("upper",):
            up = getattr(self.home, attr, None)
            if up and _within(abs_path, up):
                return to_home(self.home.rel_of(abs_path))
        if _within(abs_path, self.home.root):
            return to_home(self.home.rel_of(abs_path))
        return self.ws.rel_of(abs_path)

    def walk(self, start: ResolvedPath, *, max_depth: int, max_entries: int, include_hidden: bool = True,
             check=None):
        home, rel = split_home(start.rel)
        if not home:
            return self.ws.walk(start, max_depth=max_depth, max_entries=max_entries,
                                include_hidden=include_hidden, check=check)
        inner = dataclasses.replace(start, rel=rel)
        entries, truncated, skipped = self.home.walk(inner, max_depth=max_depth, max_entries=max_entries,
                                                     include_hidden=include_hidden, check=check)
        out = [{**e, "path": to_home(e["path"])} for e in entries]
        return out, truncated, [to_home(s) for s in skipped]

    def iter_files(self, start: ResolvedPath, *, max_depth: int, max_files: int, check=None) -> Iterator[str]:
        entries, _, _ = self.walk(start, max_depth=max_depth, max_entries=max_files * 4 + 100, check=check)
        n = 0
        for e in entries:
            if e.get("type") == "file":
                yield e["path"]
                n += 1
                if n >= max_files:
                    return

    # overlay API (only when both sides are overlays) ------------------------
    def changes(self) -> List[dict]:
        out = list(self.ws.changes())
        for ch in self.home.changes():
            out.append({**ch, "rel_path": to_home(ch["rel_path"])})
        return out

    def has_changes(self) -> bool:
        return bool(self.changes())

    def diff(self, max_files: int = 50) -> List[dict]:
        out = list(self.ws.diff(max_files))
        for d in self.home.diff(max_files):
            out.append({**d, "path": to_home(d["path"])})
        return out[:max_files]

    def discard(self) -> None:
        self.ws.discard()
        self.home.discard()


def lower_path(roots: dict, rel_path: str) -> str:
    """Real path of a journal entry: `~/x` lives under the home root, everything else under the workspace."""
    home, rel = split_home(rel_path)
    base = roots[HOME] if home else roots[""]
    return os.path.join(base, *[p for p in rel.split("/") if p not in ("", ".")])
