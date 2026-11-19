"""Process model: scheduler states, phases, signals, budgets and the PCB.

The process control block is plain JSON-compatible data so it can be
checkpointed to SQLite after every time slice and reloaded after a crash.
Nothing in here performs I/O.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ProcState(str, Enum):
    READY = "READY"
    RUNNING = "RUNNING"
    BLOCKED_IO = "BLOCKED_IO"        # waiting for an LLM call or tool run (interrupt wakes it)
    BLOCKED_USER = "BLOCKED_USER"    # waiting for approval / answer / commit review
    BLOCKED_WAIT = "BLOCKED_WAIT"    # waiting for child processes (wait())
    STOPPED = "STOPPED"              # SIGSTOP
    ZOMBIE = "ZOMBIE"                # exited, not yet reaped by its parent
    EXITED = "EXITED"                # exited and reaped


LIVE_STATES = {ProcState.READY, ProcState.RUNNING, ProcState.BLOCKED_IO, ProcState.BLOCKED_USER,
               ProcState.BLOCKED_WAIT, ProcState.STOPPED}
DONE_STATES = {ProcState.ZOMBIE, ProcState.EXITED}


class Phase(str, Enum):
    PLAN = "plan"
    VALIDATE = "validate"
    DISPATCH = "dispatch"
    POLICY = "policy"
    EXECUTE = "execute"
    VERIFY = "verify"
    RECOVER = "recover"
    REAP = "reap"
    REVIEW = "review"
    COMMIT = "commit"
    OUTBOX = "outbox"
    FINALIZE = "finalize"
    CHILD_EXECUTE = "child_execute"   # children run exactly one read-only step
    CHILD_VERIFY = "child_verify"
    DONE = "done"


class Signal(str, Enum):
    STOP = "STOP"   # freeze at the next slice boundary
    CONT = "CONT"   # resume a stopped process
    TERM = "TERM"   # graceful cancel: discard staged changes, exit
    KILL = "KILL"   # immediate cancel (same effect, no cleanup hooks, cannot be ignored)


class Priority(int, Enum):
    BOOSTED = 0
    NORMAL = 1
    BACKGROUND = 2


class ExitCode(str, Enum):
    COMPLETED = "completed"          # committed (or nothing to commit)
    DISCARDED = "discarded"          # user discarded the staged changes
    FAILED = "failed"
    CANCELLED = "cancelled"          # TERM / KILL
    BUDGET_EXCEEDED = "budget_exceeded"


@dataclass
class Budget:
    max_steps: int = 12
    max_executions: int = 20
    max_retries_per_step: int = 2
    max_replans: int = 2
    max_llm_calls: int = 8
    wall_clock_s: float = 15 * 60

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProcessControlBlock:
    pid: str
    task_id: str
    request: str
    title: str
    ppid: Optional[str] = None
    state: str = ProcState.READY.value
    phase: str = Phase.PLAN.value
    priority: int = Priority.NORMAL.value
    stopped_from: Optional[str] = None           # state to return to on CONT
    created_at: float = 0.0
    deadline_at: float = 0.0
    slices: int = 0
    overlay_id: Optional[str] = None
    caps: List[str] = field(default_factory=list)

    # program
    plan: Optional[dict] = None                   # {"version", "goal", "steps": [...]}
    plan_version: int = 0
    step_index: int = 0
    steps_state: Dict[str, dict] = field(default_factory=dict)   # step_id -> {status, attempts, result, ...}
    current: Optional[dict] = None                # in-flight step context {step_id, decision, ...}
    replans: int = 0
    planner_feedback: Optional[str] = None
    clarification: Optional[dict] = None

    # I/O + user interaction
    pending_io: Optional[dict] = None             # {io_id, kind, issued_by, payload}
    io_result: Optional[dict] = None
    pending_user: Optional[dict] = None           # {kind: approval|clarify|review, ...}
    outbox: List[str] = field(default_factory=list)   # step ids deferred until after commit
    post_commit: bool = False

    # children
    children: List[str] = field(default_factory=list)
    child_step: Optional[dict] = None             # for child processes: the one step they run

    # accounting
    usage: Dict[str, int] = field(default_factory=lambda: {"executions": 0, "llm_calls": 0, "tokens": 0})
    budget: Dict[str, Any] = field(default_factory=lambda: Budget().as_dict())

    # signals + exit
    signals: List[str] = field(default_factory=list)
    cancel_requested: bool = False
    exit: Optional[dict] = None                   # {code, reason, summary}
    txn_id: Optional[str] = None
    auto_commit: bool = False                     # set by schedules/listeners: commit without review (still undoable)
    origin: Optional[dict] = None                 # {"kind": "user" | "schedule" | "listener", ...}
    preset_plan: Optional[dict] = None            # a plan supplied with the request (still validated and policy-checked)

    # ------------------------------------------------------------------
    def to_json(self) -> dict:
        return copy.deepcopy(asdict(self))

    @classmethod
    def from_json(cls, data: dict) -> "ProcessControlBlock":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: copy.deepcopy(v) for k, v in data.items() if k in known})

    # convenience -------------------------------------------------------
    @property
    def is_child(self) -> bool:
        return self.ppid is not None

    def st(self) -> ProcState:
        return ProcState(self.state)

    def step_list(self) -> List[dict]:
        return list((self.plan or {}).get("steps") or [])

    def step_by_id(self, step_id: str) -> Optional[dict]:
        for s in self.step_list():
            if s.get("id") == step_id:
                return s
        return None

    def step_state(self, step_id: str) -> dict:
        return self.steps_state.setdefault(step_id, {"status": "pending", "attempts": 0})

    def public(self) -> dict:
        """What the UI / API may show (no capability ids)."""
        return {
            "pid": self.pid, "ppid": self.ppid, "task_id": self.task_id, "title": self.title,
            "request": self.request, "state": self.state, "phase": self.phase, "priority": self.priority,
            "slices": self.slices, "plan": self.plan, "plan_version": self.plan_version,
            "steps_state": self.steps_state, "pending_user": self.pending_user, "outbox": self.outbox,
            "children": self.children, "usage": self.usage, "budget": self.budget, "exit": self.exit,
            "txn_id": self.txn_id, "replans": self.replans, "auto_commit": self.auto_commit,
            "origin": self.origin or {"kind": "user"},
        }
