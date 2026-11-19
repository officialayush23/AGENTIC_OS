"""I/O drivers: where slow work (LLM calls, tool runs) actually happens.

Processes never block the kernel loop. A phase submits a request to the driver
and the process goes to BLOCKED_IO. When the work finishes the driver produces
a completion ("interrupt") that the loop delivers on its next tick.

* `ThreadPoolIO` - real driver for the app.
* `SimIO` + `SimClock` - deterministic driver for simulation tests: seeded
  latencies, seeded completion order, optional "side effect at issue time,
  completion delivered later" to model crashes between the two.
"""

from __future__ import annotations

import queue
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

Work = Callable[[], Any]


@dataclass
class Completion:
    io_id: str
    pid: str
    ok: bool
    value: Any = None
    error: Optional[str] = None
    error_type: Optional[str] = None


def _run(io_id: str, pid: str, fn: Work) -> Completion:
    try:
        return Completion(io_id, pid, True, fn())
    except BaseException as exc:  # noqa: BLE001 - delivered to the process, never raised in the loop
        return Completion(io_id, pid, False, None, str(exc)[:2000], type(exc).__name__)


class IODriver:
    def submit(self, io_id: str, pid: str, fn: Work) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def poll(self) -> List[Completion]:  # pragma: no cover
        raise NotImplementedError

    def wait(self, timeout: float) -> List[Completion]:  # pragma: no cover
        raise NotImplementedError

    def pending(self) -> int:  # pragma: no cover
        raise NotImplementedError

    def shutdown(self) -> None:
        pass


class ThreadPoolIO(IODriver):
    def __init__(self, workers: int = 4):
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="aos-io")
        self._done: "queue.Queue[Completion]" = queue.Queue()
        self._inflight = 0
        self._lock = threading.Lock()

    def submit(self, io_id: str, pid: str, fn: Work) -> None:
        with self._lock:
            self._inflight += 1

        def job() -> None:
            c = _run(io_id, pid, fn)
            with self._lock:
                self._inflight -= 1
            self._done.put(c)

        self._pool.submit(job)

    def poll(self) -> List[Completion]:
        out = []
        while True:
            try:
                out.append(self._done.get_nowait())
            except queue.Empty:
                return out

    def wait(self, timeout: float) -> List[Completion]:
        try:
            first = self._done.get(timeout=timeout)
        except queue.Empty:
            return []
        return [first, *self.poll()]

    def pending(self) -> int:
        with self._lock:
            return self._inflight + self._done.qsize()

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


class SimClock:
    def __init__(self, start: float = 1_000_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += max(0.0, dt)


@dataclass
class _SimReq:
    seq: int
    io_id: str
    pid: str
    fn: Work
    ready_at: float
    executed: Optional[Completion] = None


class SimIO(IODriver):
    def __init__(self, clock: SimClock, seed: int = 0, *, latency=(0.05, 3.0), effect_at_issue: float = 0.0):
        self.clock = clock
        self.rng = random.Random(seed)
        self.latency = latency
        self.effect_at_issue = effect_at_issue
        self._pending: List[_SimReq] = []
        self._seq = 0
        self.log: List[str] = []

    def submit(self, io_id: str, pid: str, fn: Work) -> None:
        self._seq += 1
        req = _SimReq(self._seq, io_id, pid, fn, self.clock() + self.rng.uniform(*self.latency))
        if self.rng.random() < self.effect_at_issue:
            req.executed = _run(io_id, pid, fn)     # side effect now, completion delivered later
            self.log.append(f"exec-at-issue {io_id}")
        self._pending.append(req)

    def _ready(self) -> List[_SimReq]:
        now = self.clock()
        ready = [r for r in self._pending if r.ready_at <= now]
        ready.sort(key=lambda r: (r.ready_at, r.seq))
        return ready

    def poll(self) -> List[Completion]:
        out = []
        for r in self._ready():
            self._pending.remove(r)
            out.append(r.executed or _run(r.io_id, r.pid, r.fn))
            self.log.append(f"complete {r.io_id}")
        return out

    def wait(self, timeout: float) -> List[Completion]:
        if not self._pending:
            return []
        nxt = min(r.ready_at for r in self._pending)
        self.clock.advance(nxt - self.clock())
        return self.poll()

    def pending(self) -> int:
        return len(self._pending)

    def crash(self) -> None:
        """Lose every in-flight completion (side effects already performed stay performed)."""
        self._pending.clear()
