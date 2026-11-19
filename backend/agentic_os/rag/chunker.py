"""Line-aware chunking. Chunks keep their line range so answers can cite `file:12-40`."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s|^(def |class |function |export |public |private |fn |func )")


@dataclass
class Chunk:
    seq: int
    text: str
    start_line: int
    end_line: int


def chunk_text(text: str, *, target_chars: int = 1200, max_chars: int = 2000, overlap_lines: int = 2,
               max_chunks: int = 400) -> List[Chunk]:
    lines = text.splitlines()
    chunks: List[Chunk] = []
    buf: List[str] = []
    start = 1
    size = 0

    def flush(end_line: int) -> None:
        nonlocal buf, start, size
        body = "\n".join(buf).strip()
        if body:
            chunks.append(Chunk(len(chunks), body[:max_chars * 2], start, end_line))
        keep = buf[-overlap_lines:] if overlap_lines and len(buf) > overlap_lines else []
        start = end_line - len(keep) + 1
        buf = list(keep)
        size = sum(len(x) + 1 for x in buf)

    for i, line in enumerate(lines, start=1):
        boundary = size >= target_chars * 0.6 and (not line.strip() or _HEADING.match(line))
        if buf and (boundary or size + len(line) > max_chars):
            flush(i - 1)
            if len(chunks) >= max_chunks:
                return chunks
        if not buf:
            start = i
        buf.append(line[:max_chars])
        size += len(line) + 1
        if size >= target_chars and not line.strip():
            flush(i)
    if any(x.strip() for x in buf):
        flush(len(lines))
    return chunks[:max_chunks]
