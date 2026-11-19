"""Workspace path guard.

All filesystem tools go through `WorkspaceGuard.resolve()`. It rejects:

* empty, overlong or control-character paths;
* UNC (``\\\\server\\share``) and device paths (``\\\\.\\``, ``\\\\?\\``);
* drive-relative paths (``C:file``) and absolute paths outside the root;
* ``..`` components (even ones that would stay inside the root);
* NTFS alternate data streams (``file.txt:stream``);
* Windows reserved device names (``CON``, ``NUL``, ``COM1``, ...);
* components ending in a dot or space (Windows silently strips them);
* anything whose *real* path (after resolving symlinks and junctions) is
  outside the workspace root.

It also classifies sensitive files (``.env``, private keys, credential files,
``.git``/``.ssh`` directories...). Policy denies reading or writing them.
"""

from __future__ import annotations

import fnmatch
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple

from .base import ToolError

MAX_PATH_CHARS = 1024

WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul", "conin$", "conout$", "clock$",
    *(f"com{i}" for i in range(0, 10)), *(f"lpt{i}" for i in range(0, 10)),
    "com\u00b9", "com\u00b2", "com\u00b3", "lpt\u00b9", "lpt\u00b2", "lpt\u00b3",
}

SENSITIVE_DIR_NAMES = {
    ".git", ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".gcloud", ".password-store",
}

DEFAULT_SENSITIVE_PATTERNS = [
    ".env", ".env.*", "*.env",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.ppk", "*.kdbx", "*.ovpn",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    ".npmrc", ".pypirc", ".netrc", "_netrc", ".git-credentials", ".htpasswd",
    "credentials", "credentials.*", "*credentials*.json", "secrets.*", "*.secret", "*.secrets",
    "service-account*.json", "*serviceaccount*.json", "client_secret*.json", "token.json",
    "*.sqlite", "*.sqlite3", "*.db", "wallet.dat",
]

SENSITIVE_EXCEPTIONS = [".env.example", ".env.sample", ".env.template", ".env.dist", "*.example", "*.sample"]

DEFAULT_IGNORED_DIRS = [
    "node_modules", ".git", ".venv", "venv", "env", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", "dist", "build", ".next", ".nuxt", ".svelte-kit", ".turbo", ".cache",
    ".idea", ".vs", ".vscode-test", "target", "bin", "obj", ".gradle", "coverage", ".parcel-cache",
]

_DRIVE_RE = re.compile(r"^([A-Za-z]):(.*)$")


class PathError(ToolError):
    def __init__(self, code: str, message: str):
        super().__init__(code, message, retryable=False)


@dataclass(frozen=True)
class ResolvedPath:
    abs_path: str
    rel: str
    exists: bool
    is_dir: bool
    is_file: bool
    is_symlink: bool
    sensitive: Optional[str]

    @property
    def name(self) -> str:
        return PurePosixPath(self.rel).name if self.rel != "." else "."


def _normcase(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def _within(path: str, root: str) -> bool:
    p, r = _normcase(path), _normcase(root)
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:  # different drives on Windows
        return False


def _is_link_or_junction(path: str) -> bool:
    try:
        if os.path.islink(path):
            return True
        isjunction = getattr(os.path, "isjunction", None)
        if isjunction is not None and isjunction(path):
            return True
        st = os.lstat(path)
        attrs = getattr(st, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(attrs & reparse)
    except OSError:
        return False


def validate_component(part: str) -> None:
    if part == "..":
        raise PathError("PATH_TRAVERSAL", "Paths may not contain '..' segments.")
    if ":" in part:
        raise PathError("PATH_INVALID", "Paths may not contain ':' (drive letters inside paths or alternate data streams).")
    if part != part.rstrip(" .") and part not in (".",):
        raise PathError("PATH_INVALID", f"Path segment {part!r} ends with a dot or space, which Windows silently strips.")
    if any(ch in part for ch in '<>"|?*'):
        raise PathError("PATH_INVALID", f"Path segment {part!r} contains a character that is not allowed in file names.")
    stem = part.split(".")[0].strip().lower()
    if stem in WINDOWS_RESERVED:
        raise PathError("PATH_RESERVED_NAME", f"{part!r} is a reserved device name on Windows.")
    if len(part) > 255:
        raise PathError("PATH_INVALID", "A path segment is longer than 255 characters.")


class WorkspaceGuard:
    def __init__(
        self,
        root: str | os.PathLike,
        *,
        sensitive_patterns: Optional[Sequence[str]] = None,
        ignored_dirs: Optional[Sequence[str]] = None,
    ):
        root_str = os.path.abspath(os.fspath(root))
        if not os.path.isdir(root_str):
            raise PathError("WORKSPACE_MISSING", "The configured workspace folder does not exist.")
        self.root_lexical = root_str
        self.root = os.path.realpath(root_str)
        self.sensitive_patterns = [p.lower() for p in (sensitive_patterns or DEFAULT_SENSITIVE_PATTERNS)]
        self.ignored_dirs = {d.lower() for d in (ignored_dirs or DEFAULT_IGNORED_DIRS)}

    @property
    def name(self) -> str:
        return Path(self.root).name or self.root

    # ------------------------------------------------------------ sensitivity
    def sensitive_reason(self, parts: Iterable[str]) -> Optional[str]:
        parts = [p for p in parts if p not in ("", ".")]
        for part in parts[:-1]:
            if part.lower() in SENSITIVE_DIR_NAMES:
                return f"inside protected folder '{part}'"
        if not parts:
            return None
        last = parts[-1].lower()
        if last in SENSITIVE_DIR_NAMES:
            return f"protected folder '{parts[-1]}'"
        if any(fnmatch.fnmatchcase(last, pat) for pat in SENSITIVE_EXCEPTIONS):
            return None
        for pat in self.sensitive_patterns:
            if fnmatch.fnmatchcase(last, pat):
                return f"matches sensitive-file pattern '{pat}'"
        return None

    def is_ignored_dir(self, name: str) -> bool:
        return name.lower() in self.ignored_dirs

    # -------------------------------------------------------------- resolve
    def resolve(self, user_path: Optional[str], *, for_write: bool = False) -> ResolvedPath:
        if user_path is None:
            user_path = "."
        if not isinstance(user_path, str):
            raise PathError("PATH_INVALID", "Path must be text.")
        raw = user_path.strip()
        if len(raw) > MAX_PATH_CHARS:
            raise PathError("PATH_INVALID", "Path is too long.")
        if any(ord(c) < 32 for c in raw):
            raise PathError("PATH_INVALID", "Path contains control characters.")
        norm = raw.replace("\\", "/")
        if norm.startswith("//"):
            raise PathError("PATH_UNC_NOT_ALLOWED", "Network (UNC) and device paths are not allowed.")

        m = _DRIVE_RE.match(norm)
        if m:
            if not m.group(2).startswith("/"):
                raise PathError("PATH_INVALID", "Drive-relative paths such as 'C:file' are not allowed.")
            parts = self._parts_from_absolute(norm)
        elif norm.startswith("/"):
            if os.name == "nt":
                raise PathError("PATH_INVALID", "Use a path relative to the workspace (for example 'docs/notes.md').")
            parts = self._parts_from_absolute(norm)
        else:
            parts = [p for p in norm.split("/") if p not in ("", ".")]
        for part in parts:
            validate_component(part)
        return self._finalize(parts, for_write=for_write)

    def _parts_from_absolute(self, norm: str) -> List[str]:
        raw_parts = norm.split("/")
        if ".." in raw_parts:
            raise PathError("PATH_TRAVERSAL", "Paths may not contain '..' segments.")
        absolute = os.path.normpath(norm)
        for base in (self.root, self.root_lexical):
            if _within(absolute, base):
                rel = os.path.relpath(absolute, base)
                if rel == ".":
                    return []
                return [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
        raise PathError("PATH_OUTSIDE_WORKSPACE", "That path is outside the configured workspace.")

    def _finalize(self, parts: List[str], *, for_write: bool) -> ResolvedPath:
        candidate = os.path.join(self.root, *parts) if parts else self.root
        real = os.path.realpath(candidate)
        if not _within(real, self.root):
            raise PathError(
                "PATH_OUTSIDE_WORKSPACE",
                "That path resolves outside the workspace (through a symbolic link or junction).",
            )
        is_link = bool(parts) and _is_link_or_junction(candidate)
        if for_write:
            if is_link:
                raise PathError("PATH_SYMLINK_WRITE", "Refusing to write through a symbolic link or junction.")
            parent = os.path.dirname(real) if parts else None
            if parent is not None and not _within(parent, self.root):
                raise PathError("PATH_OUTSIDE_WORKSPACE", "The parent folder is outside the workspace.")
        real_rel_parts: List[str] = []
        if _normcase(real) != _normcase(self.root):
            real_rel_parts = os.path.relpath(real, self.root).replace("\\", "/").split("/")
        sensitive = self.sensitive_reason(parts) or self.sensitive_reason(real_rel_parts)
        rel = PurePosixPath(*parts).as_posix() if parts else "."
        return ResolvedPath(
            abs_path=real,
            rel=rel,
            exists=os.path.exists(real),
            is_dir=os.path.isdir(real),
            is_file=os.path.isfile(real),
            is_symlink=is_link,
            sensitive=sensitive,
        )

    def rel_of(self, abs_path: str) -> str:
        rel = os.path.relpath(abs_path, self.root).replace("\\", "/")
        return "." if rel in ("", ".") else rel

    # ----------------------------------------------------------------- walk
    def walk(
        self,
        start: ResolvedPath,
        *,
        max_depth: int,
        max_entries: int,
        include_hidden: bool = True,
        check=None,
    ) -> Tuple[List[dict], bool, List[str]]:
        """Breadth-first listing below `start`. Never follows links into directories.

        Returns (entries, truncated, skipped_dirs).
        """
        entries: List[dict] = []
        skipped: List[str] = []
        truncated = False
        queue: List[Tuple[str, int]] = [(start.abs_path, 1)]
        while queue:
            current, depth = queue.pop(0)
            try:
                with os.scandir(current) as it:
                    items = sorted(it, key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
            except (PermissionError, FileNotFoundError, NotADirectoryError, OSError):
                skipped.append(self.rel_of(current))
                continue
            for entry in items:
                if check is not None:
                    check()
                name = entry.name
                if not include_hidden and name.startswith("."):
                    continue
                rel = self.rel_of(entry.path)
                rel_parts = rel.split("/")
                try:
                    is_link = entry.is_symlink() or _is_link_or_junction(entry.path)
                    is_dir = entry.is_dir(follow_symlinks=False) and not is_link
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                item = {
                    "path": rel,
                    "name": name,
                    "type": "symlink" if is_link else ("dir" if is_dir else "file"),
                    "depth": depth,
                    "size": None if is_dir else int(st.st_size),
                    "modified": _mtime_iso(st.st_mtime),
                }
                reason = self.sensitive_reason(rel_parts)
                if reason:
                    item["sensitive"] = True
                if is_dir and (self.is_ignored_dir(name) or name.lower() in SENSITIVE_DIR_NAMES):
                    item["skipped"] = True
                    skipped.append(rel)
                entries.append(item)
                if len(entries) >= max_entries:
                    truncated = True
                    return entries, truncated, skipped
                if is_dir and not item.get("skipped") and depth < max_depth:
                    queue.append((entry.path, depth + 1))
        return entries, truncated, skipped

    def iter_files(self, start: ResolvedPath, *, max_depth: int, max_files: int, check=None) -> Iterator[str]:
        entries, _, _ = self.walk(start, max_depth=max_depth, max_entries=max_files * 4, check=check)
        count = 0
        for e in entries:
            if e["type"] == "file":
                yield e["path"]
                count += 1
                if count >= max_files:
                    return


def _mtime_iso(ts: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
_FORBIDDEN_ROOTS_WIN = ("windows", "program files", "program files (x86)", "programdata", "$recycle.bin", "system volume information")


def validate_workspace_root(path: str, data_dir: Optional[str] = None) -> str:
    """Validate a folder chosen as the workspace root. Returns the absolute real path."""
    if not path or not isinstance(path, str):
        raise PathError("WORKSPACE_INVALID", "Choose a workspace folder.")
    raw = path.strip().strip('"')
    if raw.startswith("\\\\") or raw.startswith("//"):
        raise PathError("WORKSPACE_INVALID", "Network (UNC) folders are not supported as a workspace.")
    expanded = os.path.expanduser(raw)
    if not os.path.isabs(expanded):
        raise PathError("WORKSPACE_INVALID", "Enter the full path of the workspace folder (for example D:\\Projects\\demo).")
    real = os.path.realpath(expanded)
    if not os.path.isdir(real):
        raise PathError("WORKSPACE_MISSING", "That folder does not exist.")
    parts = Path(real).parts
    if len(parts) <= 1 or real == os.path.abspath(os.sep) or re.match(r"^[A-Za-z]:\\?$", real):
        raise PathError("WORKSPACE_INVALID", "A whole drive cannot be the workspace. Choose a project folder.")
    if os.name == "nt" and len(parts) >= 2 and parts[1].lower() in _FORBIDDEN_ROOTS_WIN:
        raise PathError("WORKSPACE_INVALID", "System folders cannot be used as the workspace.")
    if os.name != "nt" and real in ("/etc", "/usr", "/bin", "/sbin", "/var", "/proc", "/sys", "/dev", "/boot", "/root"):
        raise PathError("WORKSPACE_INVALID", "System folders cannot be used as the workspace.")
    home = os.path.realpath(os.path.expanduser("~"))
    if _normcase(real) == _normcase(home):
        raise PathError("WORKSPACE_INVALID", "Your whole home folder cannot be the workspace. Choose a project folder.")
    if data_dir:
        data_real = os.path.realpath(data_dir)
        if _within(data_real, real):
            raise PathError("WORKSPACE_INVALID", "The workspace cannot contain the Agentic OS data folder.")
    return real
