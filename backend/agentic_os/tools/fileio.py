"""Low-level file helpers: hashing, text decoding, atomic writes, backups."""

from __future__ import annotations

import codecs
import difflib
import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional, Tuple

from .base import ToolError

BINARY_SNIFF_BYTES = 8192


def sha256_file(path: str, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def looks_binary(data: bytes) -> bool:
    head = data[:BINARY_SNIFF_BYTES]
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return False
    return b"\x00" in head


def decode_text(data: bytes) -> Tuple[str, str]:
    """Decode bytes as text. Returns (text, encoding_label)."""
    if data.startswith(codecs.BOM_UTF8):
        return data[len(codecs.BOM_UTF8):].decode("utf-8", errors="replace"), "utf-8-sig"
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors="replace"), "utf-16"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), "utf-8 (invalid bytes replaced)"


def read_bytes_limited(path: str, max_bytes: int) -> bytes:
    size = os.path.getsize(path)
    if size > max_bytes:
        raise ToolError(
            "FILE_TOO_LARGE",
            f"The file is {size:,} bytes; the limit is {max_bytes:,} bytes.",
            details={"size": size, "limit": max_bytes},
        )
    with open(path, "rb") as f:
        return f.read(max_bytes + 1)


def read_text_file(path: str, max_bytes: int) -> Tuple[str, str, bytes]:
    data = read_bytes_limited(path, max_bytes)
    if looks_binary(data):
        raise ToolError("FILE_IS_BINARY", "This looks like a binary file; only text files can be read.")
    text, encoding = decode_text(data)
    return text, encoding, data


def dominant_newline(text: str) -> str:
    return "\r\n" if text.count("\r\n") > (text.count("\n") - text.count("\r\n")) else "\n"


def encode_like(text: str, encoding: str) -> bytes:
    if encoding == "utf-8-sig":
        return codecs.BOM_UTF8 + text.encode("utf-8")
    if encoding == "utf-16":
        return text.encode("utf-16")
    return text.encode("utf-8")


def create_exclusive(path: str, data: bytes) -> None:
    """Create a new file; fails with FileExistsError if it already exists."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def atomic_replace(path: str, data: bytes) -> None:
    """Write to a temporary file in the same folder, then atomically replace the target."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".aos-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            shutil.copymode(path, tmp)
        except OSError:
            pass
        os.replace(tmp, path)
    except PermissionError as exc:
        _cleanup(tmp)
        raise ToolError(
            "FILE_LOCKED",
            "The file is locked by another program (for example, open in an editor). Close it and retry.",
            retryable=True,
        ) from exc
    except BaseException:
        _cleanup(tmp)
        raise


def _cleanup(tmp: str) -> None:
    try:
        os.unlink(tmp)
    except OSError:
        pass


def backup_file(src: str, data_dir: Optional[str], task_id: str, step_id: str) -> Optional[str]:
    """Copy `src` into the per-task backup folder. Returns the backup path relative to data_dir."""
    if not data_dir:
        return None
    base = Path(data_dir) / "backups" / _safe(task_id)
    base.mkdir(parents=True, exist_ok=True)
    target = base / f"{_safe(step_id)}--{_safe(Path(src).name)}"
    n = 1
    while target.exists():
        target = base / f"{_safe(step_id)}--{n}--{_safe(Path(src).name)}"
        n += 1
    shutil.copy2(src, target)
    return target.relative_to(Path(data_dir)).as_posix()


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:80] or "file"


def unified_diff(before: str, after: str, path: str, max_lines: int = 400) -> Tuple[str, bool]:
    lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile=f"a/{path}", tofile=f"b/{path}", n=3,
        )
    )
    truncated = len(lines) > max_lines
    text = "".join(lines[:max_lines])
    if truncated:
        text += f"\n... diff truncated ({len(lines) - max_lines} more lines)\n"
    return text, truncated


def ensure_parent_dirs(guard, target_abs: str, max_new_levels: int = 5) -> list:
    """Create missing parent folders inside the workspace. Returns the created folders (relative)."""
    parent = Path(target_abs).parent
    missing = []
    probe = parent
    while not probe.exists():
        missing.append(probe)
        if len(missing) > max_new_levels:
            raise ToolError("PATH_TOO_DEEP", f"Refusing to create more than {max_new_levels} new folders.")
        probe = probe.parent
    created = []
    for folder in reversed(missing):
        rel = guard.rel_of(str(folder))
        guard.resolve(rel, for_write=True)  # re-validate every level
        folder.mkdir(exist_ok=True)
        created.append(rel)
    return created
