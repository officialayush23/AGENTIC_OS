"""Run queue.

Three priority levels, FIFO within a level. A process the user just unblocked
gets one boosted slice; a process that has used many slices is demoted to
background so a long task cannot monopolise the loop. Boosts are one-shot, so
nothing starves.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Dict, Optional

from .process import Priority

DEMOTE_AFTER_SLICES = 64


class RunQueue:
    def __init__(self) -> None:
        self._levels: Dict[int, Deque[str]] = {p.value: deque() for p in Priority}
        self._queued: Dict[str, int] = {}

    def push(self, pid: str, priority: int) -> None:
        if pid in self._queued:
            return
        level = priority if priority in self._levels else Priority.NORMAL.value
        self._levels[level].append(pid)
        self._queued[pid] = level

    def remove(self, pid: str) -> None:
        level = self._queued.pop(pid, None)
        if level is not None:
            try:
                self._levels[level].remove(pid)
            except ValueError:
                pass

    def pop(self) -> Optional[str]:
        for level in sorted(self._levels):
            q = self._levels[level]
            if q:
                pid = q.popleft()
                self._queued.pop(pid, None)
                return pid
        return None

    def __len__(self) -> int:
        return len(self._queued)

    def __contains__(self, pid: str) -> bool:
        return pid in self._queued


def next_priority(current: int, slices: int) -> int:
    """Priority to use when re-queueing after a slice."""
    if current == Priority.BOOSTED.value:
        return Priority.NORMAL.value
    if slices > DEMOTE_AFTER_SLICES:
        return Priority.BACKGROUND.value
    return current
