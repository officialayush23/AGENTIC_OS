"""The Agentic OS kernel.

A single-threaded loop owns every kernel data structure (process table,
capability table, run queue). Each tick it:

1. drains the command inbox (approvals, signals, commit/discard/undo from the API),
2. delivers I/O completions ("interrupts") to blocked processes,
3. enforces deadlines,
4. runs exactly one phase ("time slice") of the next READY process,
5. checkpoints that process's PCB to SQLite.

Phases never perform slow I/O. LLM calls and tool runs are submitted to the
I/O driver and the process sleeps in BLOCKED_IO until the completion arrives.
"""

from __future__ import annotations

import copy
import logging
import os
import queue
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..events import EventBus
from ..persistence.db import Database
from ..persistence.repositories import AppRepo, EventRepo, ExecutionRepo, TaskRepo
from ..tools.base import RiskLevel, ToolContext, ToolError
from ..tools.executor import ToolExecutor
from ..tools.pathguard import PathError
from ..util import bounded_json, new_id, utc_now_iso
from .capabilities import GRANTABLE_TOOL_CAPS, IRREVERSIBLE_TOOL_CAPS, TOOL_CAP_MAP, cap_resource, Capability, CapabilityError, CapabilityTable
from .io import Completion, IODriver
from .journal import CommitConflict, Journal
from .mounts import MountedWorkspace, to_home
from .overlay import OverlayWorkspace
from .planner import Planner, RefError, iter_refs, ref_step, resolve_refs, validate_plan
from .process import (DONE_STATES, LIVE_STATES, Budget, ExitCode, Phase, Priority, ProcessControlBlock, ProcState,
                      Signal)
from .scheduler import RunQueue, next_priority
from .store import KernelStore

log = logging.getLogger(__name__)

READ_ONLY_TOOL_CAPS = {"workspace.read", "system.read", "apps.read", "compute"}
STAGED_TOOL_CAPS = {"workspace.write"}
MAX_FORK = 4

TASK_STATUS = {
    ExitCode.COMPLETED.value: "completed", ExitCode.DISCARDED.value: "discarded", ExitCode.FAILED.value: "failed",
    ExitCode.CANCELLED.value: "cancelled", ExitCode.BUDGET_EXCEEDED.value: "failed",
}


class KernelError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class Kernel:
    def __init__(
        self,
        *,
        db: Database,
        registry,
        policy,
        planner: Planner,
        io: IODriver,
        workspace_root: str,
        data_dir: str,
        clock: Callable[[], float] = time.time,
        settings_fn: Callable[[], dict] = dict,
        os_adapter: Any = None,
        events: Optional[EventBus] = None,
        journal_fault: Optional[Callable[[str], None]] = None,
        fork_enabled: bool = True,
        budget: Optional[Budget] = None,
        services: Optional[Dict[str, Any]] = None,
        home_root: Optional[str] = None,
        automation: Any = None,
    ):
        self.db = db
        self.registry = registry
        self.policy = policy
        self.planner = planner
        self.io = io
        self.workspace_root = os.path.realpath(workspace_root)
        self.data_dir = str(data_dir)
        self.clock = clock
        self.settings_fn = settings_fn
        if os_adapter is None:
            from ..os_adapters import get_os_adapter

            os_adapter = get_os_adapter()
        self.os_adapter = os_adapter
        self.store = KernelStore(db)
        self.tasks = TaskRepo(db)
        self.apps = AppRepo(db)
        self.events = events or EventBus(EventRepo(db))
        self.executor = ToolExecutor(ExecutionRepo(db))
        self.caps = CapabilityTable(self.store, clock)
        self.journal = Journal(self.store, data_dir, fault=journal_fault)
        self.fork_enabled = fork_enabled
        self.default_budget = budget or Budget()
        self.services: Dict[str, Any] = dict(services or {})
        self.home_root = os.path.realpath(home_root) if home_root else None
        self.automation = automation
        self._timer_checked = 0.0

        self.procs: Dict[str, ProcessControlBlock] = {}
        self.runq = RunQueue()
        self._cancel: Dict[str, threading.Event] = {}
        self._inbox: "queue.Queue[Tuple[Callable[[], Any], Future]]" = queue.Queue()
        self._loop_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wakeup = threading.Event()
        self.booted = False
        self._phases = {
            Phase.PLAN.value: self._ph_plan,
            Phase.VALIDATE.value: self._ph_validate,
            Phase.DISPATCH.value: self._ph_dispatch,
            Phase.POLICY.value: self._ph_policy,
            Phase.EXECUTE.value: self._ph_execute,
            Phase.VERIFY.value: self._ph_verify,
            Phase.RECOVER.value: self._ph_recover,
            Phase.REAP.value: self._ph_reap,
            Phase.REVIEW.value: self._ph_review,
            Phase.COMMIT.value: self._ph_commit,
            Phase.FINALIZE.value: self._ph_finalize,
            Phase.CHILD_EXECUTE.value: self._ph_child_execute,
            Phase.CHILD_VERIFY.value: self._ph_child_verify,
        }

    # ================================================================ events
    def _emit(self, pcb: ProcessControlBlock, type_: str, message: str, *, level: str = "info",
              step_id: Optional[str] = None, **data: Any) -> None:
        data.setdefault("pid", pcb.pid)
        if pcb.ppid:
            data.setdefault("ppid", pcb.ppid)
        try:
            self.events.publish(pcb.task_id, type_, message, level=level, node=pcb.phase, step_id=step_id, data=data)
        except Exception:  # pragma: no cover - auditing must never crash the kernel
            log.exception("event publish failed")

    def _task_status(self, pcb: ProcessControlBlock, status: str, **extra: Any) -> None:
        if pcb.is_child:
            return
        try:
            self.tasks.update(pcb.task_id, status=status, current_node=pcb.phase, **extra)
        except Exception:  # pragma: no cover
            log.exception("task row update failed")

    def _save(self, pcb: ProcessControlBlock) -> None:
        self.store.save_process(pcb.to_json())

    # ============================================================== commands
    def _command(self, fn: Callable[[], Any]) -> Any:
        """Run `fn` on the kernel loop thread (directly if there is no loop thread or we are on it)."""
        if self._loop_thread is None or threading.current_thread() is self._loop_thread:
            return fn()
        fut: Future = Future()
        self._inbox.put((fn, fut))
        self._wakeup.set()
        return fut.result(timeout=60)

    def _drain_inbox(self) -> bool:
        worked = False
        while True:
            try:
                fn, fut = self._inbox.get_nowait()
            except queue.Empty:
                return worked
            worked = True
            try:
                fut.set_result(fn())
            except BaseException as exc:  # noqa: BLE001
                fut.set_exception(exc)

    def _get(self, pid: str) -> ProcessControlBlock:
        pcb = self.procs.get(pid)
        if pcb is None:
            raw = self.store.get_process(pid)
            if raw is None:
                raise KernelError("NO_SUCH_PROCESS", f"no process {pid}")
            pcb = ProcessControlBlock.from_json(raw)
        return pcb

    # ================================================================== boot
    def boot(self) -> List[dict]:
        """Load persisted state after a (re)start and recover interrupted work."""
        self.caps.load()
        actions = self.journal.recover()
        for raw in self.store.load_processes(include_exited=False):
            pcb = ProcessControlBlock.from_json(raw)
            self.procs[pcb.pid] = pcb
            self._cancel[pcb.pid] = threading.Event()
        for act in actions:
            pcb = self.procs.get(act["pid"])
            if pcb is None:
                continue
            if act["action"] == "rolled_forward" and pcb.phase == Phase.COMMIT.value:
                pcb.txn_id = act["txn_id"]
                self._overlay(pcb, kernel_view=True).discard()
                pcb.phase = Phase.FINALIZE.value if not pcb.outbox else Phase.DISPATCH.value
                pcb.post_commit = bool(pcb.outbox)
                self._emit(pcb, "txn.recovered", "Commit was interrupted and has been completed", txn_id=act["txn_id"])
        for pcb in list(self.procs.values()):
            st = pcb.st()
            if pcb.cancel_requested and st in LIVE_STATES:
                self._exit(pcb, ExitCode.CANCELLED, "cancelled before restart")
                continue
            if st in (ProcState.RUNNING, ProcState.BLOCKED_IO):
                if pcb.pending_io:
                    pcb.phase = pcb.pending_io["issued_by"]
                    self._emit(pcb, "proc.recovered",
                               f"Re-issuing interrupted {pcb.pending_io['kind']} after restart",
                               io_kind=pcb.pending_io["kind"])
                pcb.pending_io = None
                pcb.io_result = None
                pcb.state = ProcState.READY.value
            if pcb.st() == ProcState.STOPPED and pcb.stopped_from in (ProcState.RUNNING.value, ProcState.BLOCKED_IO.value):
                if pcb.pending_io:
                    pcb.phase = pcb.pending_io["issued_by"]
                pcb.pending_io, pcb.io_result, pcb.stopped_from = None, None, ProcState.READY.value
            if pcb.st() == ProcState.BLOCKED_WAIT and self._children_done(pcb):
                pcb.state = ProcState.READY.value
            if pcb.st() == ProcState.READY:
                self.runq.push(pcb.pid, pcb.priority)
            self._save(pcb)
        self.booted = True
        return actions

    # ============================================================== spawning
    def spawn(self, request: str, *, title: Optional[str] = None, budget: Optional[dict] = None,
              auto_commit: bool = False, origin: Optional[dict] = None, plan: Optional[dict] = None) -> str:
        def do() -> str:
            if not str(request or "").strip():
                raise KernelError("REQUEST_EMPTY", "Describe what the task should do.")
            pid, task_id = new_id("proc"), new_id("task")
            now = self.clock()
            b = self.default_budget.as_dict()
            b.update(budget or {})
            pcb = ProcessControlBlock(
                pid=pid, task_id=task_id, request=request.strip(), title=(title or request.strip())[:120],
                created_at=now, deadline_at=now + float(b["wall_clock_s"]), overlay_id=pid, budget=b,
                auto_commit=bool(auto_commit), origin=origin, preset_plan=copy.deepcopy(plan) if plan else None,
            )
            self.tasks.create({"id": task_id, "title": pcb.title, "request": pcb.request, "status": "running",
                               "planner_mode": self.planner.kind, "thread_id": pid})
            for kind, scope, rights in (("fs", "", ("read", "write")), ("sys", "*", ("read",)), ("compute", "*", ("use",)),
                                       ("mail", "*", ("read",))):
                pcb.caps.append(self.caps.grant(pid, kind, scope, rights, origin="spawn").cap_id)
            self.procs[pid] = pcb
            self._cancel[pid] = threading.Event()
            self.runq.push(pid, pcb.priority)
            self._save(pcb)
            self._emit(pcb, "proc.spawn", f"Process started: {pcb.title}", request=pcb.request,
                       capabilities=self.caps.public(pid), origin=pcb.origin or {"kind": "user"})
            return pid

        return self._command(do)

    def _fork(self, parent: ProcessControlBlock, step: dict) -> str:
        pid = new_id("proc")
        child = ProcessControlBlock(
            pid=pid, ppid=parent.pid, task_id=parent.task_id, request=parent.request,
            title=f"{parent.title} › {step.get('title') or step['id']}", phase=Phase.CHILD_EXECUTE.value,
            created_at=self.clock(), deadline_at=parent.deadline_at, overlay_id=parent.overlay_id,
            child_step=copy.deepcopy(step), budget=copy.deepcopy(parent.budget), plan_version=parent.plan_version,
        )
        # Attenuated authority: read-only view of whatever the parent can read.
        for cap in self.caps.held(parent.pid):
            if cap.kind == "fs" and "read" in cap.rights:
                child.caps.append(self.caps.attenuate(cap.cap_id, pid, rights=("read",)).cap_id)
            elif cap.kind in ("sys", "compute"):
                child.caps.append(self.caps.attenuate(cap.cap_id, pid).cap_id)
        self.procs[pid] = child
        self._cancel[pid] = threading.Event()
        parent.children.append(pid)
        self.runq.push(pid, Priority.NORMAL.value)
        self._save(child)
        self._emit(child, "proc.fork", f"Forked child for step {step['id']}", step_id=step["id"],
                   capabilities=self.caps.public(pid))
        return pid

    # ================================================================= loop
    def tick(self) -> bool:
        worked = self._drain_inbox()
        for c in self.io.poll():
            self._deliver(c)
            worked = True
        self._check_deadlines()
        if self._fire_timers():
            worked = True
        pid = self.runq.pop()
        if pid is None:
            return worked
        pcb = self.procs.get(pid)
        if pcb is None or pcb.st() != ProcState.READY:
            return True
        self._run_slice(pcb)
        return True

    def run_until_idle(self, max_ticks: int = 200_000) -> int:
        """Run until no process can make progress without the user. Returns ticks used."""
        ticks = 0
        while ticks < max_ticks:
            ticks += 1
            if self.tick():
                continue
            if self.io.pending():
                for c in self.io.wait(timeout=0.5):
                    self._deliver(c)
                continue
            if self._inbox.empty():
                break
        return ticks

    def start(self) -> None:
        """Run the loop in a background thread (production)."""
        if self._loop_thread is not None:
            return
        if not self.booted:
            self.boot()

        def loop() -> None:
            while not self._stop.is_set():
                if self.tick():
                    continue
                if self.io.pending():
                    for c in self.io.wait(timeout=0.05):
                        self._deliver(c)
                else:
                    self._wakeup.wait(timeout=0.05)
                    self._wakeup.clear()

        self._loop_thread = threading.Thread(target=loop, name="aos-kernel", daemon=True)
        self._loop_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=5)
            self._loop_thread = None
        self.io.shutdown()

    def _deliver(self, c: Completion) -> None:
        pcb = self.procs.get(c.pid)
        if pcb is None:
            return
        if pcb.st() in DONE_STATES:
            if pcb.exit and pcb.exit.get("code") == ExitCode.CANCELLED.value and not pcb.is_child:
                self._overlay(pcb, kernel_view=True).discard()   # late write from a killed process
            return
        if not pcb.pending_io or pcb.pending_io.get("io_id") != c.io_id:
            return  # stale completion (e.g. re-issued after restart)
        pcb.io_result = {"ok": c.ok, "value": c.value, "error": c.error, "error_type": c.error_type}
        pcb.phase = pcb.pending_io["then"]
        pcb.pending_io = None
        if pcb.st() == ProcState.BLOCKED_IO:
            pcb.state = ProcState.READY.value
            self.runq.push(pcb.pid, pcb.priority)
        elif pcb.st() == ProcState.STOPPED:
            pcb.stopped_from = ProcState.READY.value
        self._save(pcb)

    def _fire_timers(self) -> bool:
        """Timer interrupts: start every schedule that is due (checked at most once per second)."""
        if self.automation is None:
            return False
        now = self.clock()
        if now - self._timer_checked < 1.0:
            return False
        self._timer_checked = now
        fired = False
        for sch in self.automation.due(now):
            try:
                pid = self.spawn(sch["request"], title=sch["name"], auto_commit=sch["auto_commit"],
                                 origin={"kind": "schedule", "id": sch["id"], "name": sch["name"]})
            except Exception:  # noqa: BLE001 - a bad schedule must not stop the kernel
                log.exception("schedule %s failed to start", sch["id"])
                pid = None
            self.automation.mark_fired(sch["id"], pid, now)
            fired = True
        return fired

    def _check_deadlines(self) -> None:
        now = self.clock()
        for pcb in list(self.procs.values()):
            if pcb.st() not in LIVE_STATES or pcb.st() in (ProcState.BLOCKED_USER, ProcState.STOPPED):
                continue
            if pcb.post_commit or (pcb.exit or {}).get("pending_failure") or pcb.phase in (
                    Phase.REVIEW.value, Phase.COMMIT.value, Phase.FINALIZE.value):
                continue   # the deadline bounds the agent's work, not the user's review
            if pcb.deadline_at and now > pcb.deadline_at:
                if not pcb.is_child:
                    self._budget_exhausted(pcb, "wall-clock deadline reached")
                else:
                    self._exit(pcb, ExitCode.BUDGET_EXCEEDED, "wall-clock deadline reached")

    def _run_slice(self, pcb: ProcessControlBlock) -> None:
        pcb.state = ProcState.RUNNING.value
        pcb.slices += 1
        if pcb.cancel_requested:
            self._terminate(pcb, "cancelled")
            return
        handler = self._phases.get(pcb.phase)
        try:
            if handler is None:
                raise KernelError("BAD_PHASE", f"no handler for phase {pcb.phase}")
            handler(pcb)
        except Exception as exc:  # noqa: BLE001 - a kernel bug fails one process, never the loop
            log.exception("phase %s crashed for %s", pcb.phase, pcb.pid)
            self._emit(pcb, "kernel.error", f"Internal error in phase {pcb.phase}: {type(exc).__name__}",
                       level="error", error=str(exc)[:500])
            self._fail(pcb, f"internal error in phase {pcb.phase}")
        if pcb.st() == ProcState.RUNNING:
            pcb.state = ProcState.READY.value
            pcb.priority = next_priority(pcb.priority, pcb.slices)
            self.runq.push(pcb.pid, pcb.priority)
        self._save(pcb)

    def _io(self, pcb: ProcessControlBlock, kind: str, fn: Callable[[], Any], issued_by: Phase, then: Phase) -> None:
        io_id = new_id("io")
        pcb.pending_io = {"io_id": io_id, "kind": kind, "issued_by": issued_by.value, "then": then.value,
                          "issued_at": self.clock()}
        pcb.io_result = None
        pcb.state = ProcState.BLOCKED_IO.value
        self._save(pcb)                      # checkpoint before the side effect can start
        self.io.submit(io_id, pcb.pid, fn)

    def _take_result(self, pcb: ProcessControlBlock) -> dict:
        r = pcb.io_result or {"ok": False, "error": "missing I/O result", "error_type": "KernelError"}
        pcb.io_result = None
        return r

    # ============================================================== helpers
    def _overlay(self, pcb: ProcessControlBlock, *, kernel_view: bool = False,
                 caps: Optional[List[Capability]] = None):
        """The process's view of the file system: workspace overlay, plus the Home mount (`~/`) if configured."""
        oid = pcb.overlay_id or pcb.pid
        base = os.path.join(self.data_dir, "overlays", oid)
        check = None
        if not kernel_view:
            snapshot = caps if caps is not None else [copy.copy(c) for c in self.caps.held(pcb.pid)]
            from .capabilities import scope_covers

            def check(rel: str, right: str) -> None:
                for cap in snapshot:
                    if cap.kind == "fs" and right in cap.rights and scope_covers(cap.scope, rel, "fs"):
                        return
                raise PathError("PATH_CAPABILITY_DENIED", f"This task has no '{right}' capability for '{rel}'.")
        settings = self.settings_fn() or {}
        kw = dict(sensitive_patterns=settings.get("sensitive_patterns"), ignored_dirs=settings.get("ignored_dirs"))
        ws = OverlayWorkspace(self.workspace_root, os.path.join(base, "upper"), oid, self.store, cap_check=check, **kw)
        if not self.home_root:
            return ws
        home_check = (lambda rel, right: check(to_home(rel), right)) if check else None
        home = OverlayWorkspace(self.home_root, os.path.join(base, "home_upper"), oid + "~home", self.store,
                                cap_check=home_check, **kw)
        return MountedWorkspace(ws, home)

    def _ctx(self, pcb: ProcessControlBlock, step: dict, workspace, expected_state: Optional[dict] = None) -> ToolContext:
        cancel = self._cancel.setdefault(pcb.pid, threading.Event())
        root_cancel = self._cancel.get(pcb.ppid) if pcb.ppid else None
        return ToolContext(
            task_id=pcb.task_id, step_id=str(step.get("id")), workspace=workspace, os=self.os_adapter, apps=self.apps,
            user_settings=self.settings_fn() or {}, data_dir=self.data_dir,
            cancel_check=lambda: cancel.is_set() or bool(root_cancel and root_cancel.is_set()),
            expected_state=expected_state, services=self.services,
        )

    def _gate(self, pcb: ProcessControlBlock, spec, args: dict) -> Tuple[Optional[str], Optional[dict]]:
        """Capability gate. Returns (deny_message, grant_needed)."""
        for name in spec.capabilities:
            mapping = TOOL_CAP_MAP.get(name)
            if mapping is None:
                return f"tool capability '{name}' is not known to the kernel", None
            kind, right = mapping
            if kind == "fs":
                if not any(c.kind == "fs" and right in c.rights for c in self.caps.held(pcb.pid)):
                    return f"process holds no fs:{right} capability", None
                continue
            resource = cap_resource(kind, args)
            if self.caps.find(pcb.pid, kind, right, resource) is None:
                if name in GRANTABLE_TOOL_CAPS and not pcb.is_child:
                    return None, {"kind": kind, "right": right, "scope": resource}
                return f"process holds no {kind}:{right} capability for {resource!r}", None
        return None, None

    @staticmethod
    def _deps(step: dict) -> set:
        """Explicit dependencies plus every step referenced with $ref."""
        return set(step.get("depends_on") or []) | {ref_step(r) for r in iter_refs(step.get("args") or {})}

    def _args(self, pcb: ProcessControlBlock, step: dict) -> dict:
        """Step arguments with `$ref`s replaced by outputs of completed steps (deterministic)."""
        owner = pcb.ppid if pcb.is_child else pcb.pid
        parent = self.procs.get(pcb.ppid) if pcb.is_child else pcb

        def lookup(step_id: str):
            if parent is not None and parent.steps_state.get(step_id, {}).get("status") != "done":
                return None
            return self.store.get_output(owner, step_id)

        return resolve_refs(step.get("args") or {}, lookup)

    def _done_ids(self, pcb: ProcessControlBlock) -> set:
        return {sid for sid, st in pcb.steps_state.items() if st.get("status") == "done"}

    def _children_done(self, pcb: ProcessControlBlock) -> bool:
        for cpid in pcb.children:
            child = self.procs.get(cpid)
            if child is not None and child.st() not in DONE_STATES:
                return False
        return True

    # ================================================================ phases
    def _ph_plan(self, pcb: ProcessControlBlock) -> None:
        if self.planner.kind in ("llm", "hybrid") and pcb.usage.get("llm_calls", 0) >= int(pcb.budget["max_llm_calls"]):
            self._budget_exhausted(pcb, "LLM call budget used up")
            return
        completed = [{"id": sid, "tool": (pcb.step_by_id(sid) or {}).get("tool"), "result": st.get("summary")}
                     for sid, st in pcb.steps_state.items() if st.get("status") == "done"]
        catalog = self.registry.llm_catalog()
        planner, request, feedback, clar = self.planner, pcb.request, pcb.planner_feedback, pcb.clarification
        if pcb.preset_plan and pcb.plan_version == 0 and not feedback:
            preset = copy.deepcopy(pcb.preset_plan)
            self._task_status(pcb, "planning")
            self._emit(pcb, "syscall.enter", "Using the plan supplied with the request", syscall="plan")
            self._io(pcb, "plan", lambda: {"plan": preset, "usage": {}}, Phase.PLAN, Phase.VALIDATE)
            return
        self._task_status(pcb, "planning")
        self._emit(pcb, "syscall.enter", "Planning" if not feedback else "Replanning", syscall="plan")
        self._io(pcb, "plan", lambda: planner.plan(request, catalog, feedback=feedback, completed=completed,
                                                   clarification=clar),
                 Phase.PLAN, Phase.VALIDATE)

    def _ph_validate(self, pcb: ProcessControlBlock) -> None:
        r = self._take_result(pcb)
        if not r["ok"]:
            self._emit(pcb, "plan.error", f"Planner failed: {r['error']}", level="warning")
            self._replan_or_fail(pcb, f"the planner failed: {r['error']}")
            return
        value = r["value"] or {}
        usage = value.get("usage") or {}
        pcb.usage["llm_calls"] = pcb.usage.get("llm_calls", 0) + int(usage.get("llm_calls", 0))
        pcb.usage["tokens"] = pcb.usage.get("tokens", 0) + int(usage.get("tokens", 0))
        plan = value.get("plan")
        done = self._done_ids(pcb)
        errors = validate_plan(plan, self.registry, max_steps=int(pcb.budget["max_steps"]), completed_ids=done)
        if errors:
            self._emit(pcb, "plan.invalid", "Plan rejected by validation", level="warning", errors=errors)
            self._replan_or_fail(pcb, "; ".join(errors))
            return
        if plan.get("needs_clarification"):
            pcb.pending_user = {"kind": "clarify", "question": str(plan.get("clarification_question"))}
            pcb.state = ProcState.BLOCKED_USER.value
            self._task_status(pcb, "awaiting_clarification")
            self._emit(pcb, "clarification.requested", pcb.pending_user["question"])
            return
        pcb.plan_version += 1
        steps = [copy.deepcopy(s) for s in plan["steps"] if s.get("id") not in done]
        kept = [copy.deepcopy(pcb.step_by_id(sid)) for sid in done if pcb.step_by_id(sid)]
        pcb.plan = {"version": pcb.plan_version, "goal": plan.get("goal"), "steps": kept + steps,
                    "completion_criteria": plan.get("completion_criteria") or []}
        for s in steps:
            pcb.steps_state[s["id"]] = {"status": "pending", "attempts": 0}
        pcb.step_index = 0
        pcb.planner_feedback = None
        self._task_status(pcb, "running", plan_version=pcb.plan_version)
        self._emit(pcb, "plan.ready", f"Plan v{pcb.plan_version}: {len(steps)} step(s)", plan=pcb.plan)
        pcb.phase = Phase.DISPATCH.value

    def _ph_dispatch(self, pcb: ProcessControlBlock) -> None:
        steps = pcb.step_list()
        if pcb.post_commit:
            nxt = next((sid for sid in pcb.outbox if pcb.step_state(sid).get("status") == "deferred"), None)
            if nxt is None:
                pcb.phase = Phase.FINALIZE.value
                return
            pcb.current = {"step_id": nxt}
            pcb.phase = Phase.POLICY.value
            return
        pending = [s for s in steps if pcb.step_state(s["id"]).get("status") == "pending"]
        if not pending:
            pcb.phase = Phase.REVIEW.value
            return
        if pcb.usage.get("executions", 0) >= int(pcb.budget["max_executions"]):
            self._budget_exhausted(pcb, "step execution budget used up")
            return
        done = self._done_ids(pcb)
        # fork: a run of independent, read-only steps whose dependencies are already satisfied
        batch: List[dict] = []
        if self.fork_enabled:
            for s in pending:
                spec = self.registry.get(s.get("tool"))
                if (spec is None or spec.mutates or not set(spec.capabilities) <= READ_ONLY_TOOL_CAPS
                        or not self._deps(s) <= done or s.get("requires_approval")):
                    break
                try:
                    resolved = {**s, "args": self._args(pcb, s)}
                except RefError:
                    break
                batch.append(resolved)
                if len(batch) >= MAX_FORK:
                    break
        if len(batch) >= 2:
            for s in batch:
                cpid = self._fork(pcb, s)
                st = pcb.step_state(s["id"])
                st.update({"status": "running", "child": cpid})
            pcb.state = ProcState.BLOCKED_WAIT.value
            pcb.phase = Phase.REAP.value
            self._emit(pcb, "proc.wait", f"Waiting for {len(batch)} child process(es)", children=pcb.children)
            return
        step = pending[0]
        unmet = self._deps(step) - done
        if unmet:
            self._fail(pcb, f"step {step['id']} depends on steps that did not complete: {sorted(unmet)}")
            return
        pcb.current = {"step_id": step["id"]}
        pcb.phase = Phase.POLICY.value

    def _ph_policy(self, pcb: ProcessControlBlock) -> None:
        sid = pcb.current["step_id"]
        step = pcb.step_by_id(sid)
        spec = self.registry.get(step.get("tool"))
        st = pcb.step_state(sid)
        if spec is None:
            st["last_error"] = {"code": "TOOL_UNKNOWN", "message": f"unknown tool {step.get('tool')}", "retryable": False}
            pcb.phase = Phase.RECOVER.value
            return
        try:
            args = self._args(pcb, step)
        except RefError as exc:
            st["last_error"] = {"code": "REF_INVALID", "message": str(exc), "retryable": False}
            pcb.phase = Phase.RECOVER.value
            return
        step = {**step, "args": args}
        deny_msg, grant = self._gate(pcb, spec, args)
        if deny_msg:
            st["last_error"] = {"code": "CAP_DENIED", "message": deny_msg, "retryable": False}
            self._emit(pcb, "cap.denied", deny_msg, level="warning", step_id=sid)
            pcb.phase = Phase.RECOVER.value
            return
        irreversible = bool(set(spec.capabilities) & IRREVERSIBLE_TOOL_CAPS)
        overlay = self._overlay(pcb)
        if irreversible and not pcb.post_commit:
            staged = {c["rel_path"] for c in overlay.changes()}
            refs = {str(args.get(k)).replace("\\", "/").strip("/") for k in ("file", "path", "draft") if args.get(k)}
            refs |= {str(x).replace("\\", "/").strip("/") for x in (args.get("attachments") or []) if x}
            if refs & staged:
                st["status"] = "deferred"
                pcb.outbox.append(sid)
                self._emit(pcb, "outbox.deferred", f"'{step.get('title') or sid}' will run after you commit, because "
                           "it uses files this task changed", step_id=sid, files=sorted(refs & staged))
                pcb.current = None
                pcb.phase = Phase.DISPATCH.value
                return
        ctx = self._ctx(pcb, step, overlay)
        decision = self.policy.evaluate(
            task_id=pcb.task_id, plan_version=pcb.plan_version, step=step, args=args, ctx=ctx,
            user_settings=self.settings_fn() or {},
            deferred_capabilities=STAGED_TOOL_CAPS if not pcb.post_commit else None,
        )
        d = decision.as_dict()
        pcb.current["decision"] = bounded_json(d, max_chars=40000)
        self._emit(pcb, "policy.decision", f"{decision.decision}: {step.get('tool')}", step_id=sid,
                   decision=decision.decision, risk=decision.risk, rules=decision.rules)
        if decision.decision in ("deny", "precondition_failed"):
            st["last_error"] = {"code": decision.error_code, "message": decision.error_message,
                                "retryable": False}
            pcb.phase = Phase.RECOVER.value
            return
        if decision.decision == "require_approval" or grant is not None:
            if pcb.current.get("approved_hash") == decision.action_hash and grant is None:
                pcb.phase = Phase.EXECUTE.value
                return
            pcb.pending_user = {
                "kind": "approval", "step_id": sid, "tool": spec.name, "args": bounded_json(args, max_chars=20000),
                "action_hash": decision.action_hash, "risk": decision.risk, "reasons": decision.reasons,
                "effect": decision.effect, "preview": d.get("preview"), "grant": grant, "irreversible": irreversible,
            }
            pcb.state = ProcState.BLOCKED_USER.value
            self._task_status(pcb, "awaiting_approval", current_step_id=sid)
            self._emit(pcb, "approval.requested", f"Approval needed: {decision.effect or spec.name}", step_id=sid,
                       action_hash=decision.action_hash, grant=grant, irreversible=irreversible)
            return
        pcb.phase = Phase.EXECUTE.value

    def _ph_execute(self, pcb: ProcessControlBlock) -> None:
        sid = pcb.current["step_id"]
        step = pcb.step_by_id(sid)
        spec = self.registry.get(step["tool"])
        st = pcb.step_state(sid)
        try:
            step = {**step, "args": self._args(pcb, step)}
            args_model = self.registry.validate_args(spec.name, step.get("args") or {})
        except Exception as exc:  # noqa: BLE001
            st["last_error"] = {"code": "ARGS_INVALID", "message": str(exc), "retryable": False}
            pcb.phase = Phase.RECOVER.value
            return
        if pcb.usage.get("executions", 0) >= int(pcb.budget["max_executions"]):
            self._budget_exhausted(pcb, "step execution budget used up")
            return
        # consume one-shot capabilities for irreversible actions (kernel thread)
        for name in spec.capabilities:
            if name in IRREVERSIBLE_TOOL_CAPS:
                kind, right = TOOL_CAP_MAP[name]
                cap = self.caps.find(pcb.pid, kind, right, cap_resource(kind, self._args(pcb, step)))
                if cap is None:
                    st["last_error"] = {"code": "CAP_DENIED", "message": "capability expired before execution",
                                        "retryable": False}
                    pcb.phase = Phase.RECOVER.value
                    return
                self.caps.consume(cap)
        pcb.usage["executions"] = pcb.usage.get("executions", 0) + 1
        st["attempts"] = int(st.get("attempts", 0)) + 1
        st["status"] = "running"
        caps_snapshot = [copy.copy(c) for c in self.caps.held(pcb.pid)]
        expected = ((pcb.current.get("decision") or {}).get("target_state"))
        ctx = self._ctx(pcb, step, self._overlay(pcb, caps=caps_snapshot), expected_state=expected)
        attempt = st["attempts"]
        executor = self.executor
        self._emit(pcb, "syscall.enter", f"{spec.name} (attempt {attempt})", step_id=sid, syscall=spec.name,
                   args=step.get("args"))
        self._io(pcb, "tool", lambda: executor.execute(spec, args_model, ctx, attempt=attempt), Phase.EXECUTE, Phase.VERIFY)

    def _run_verify(self, pcb: ProcessControlBlock, step: dict, result: dict) -> Tuple[bool, List[dict]]:
        spec = self.registry.get(step["tool"])
        if spec.verify is None:
            return True, []
        args_model = self.registry.validate_args(spec.name, self._args(pcb, step))
        ctx = self._ctx(pcb, step, self._overlay(pcb))
        try:
            checks = [c.as_dict() for c in spec.verify(ctx, args_model, result.get("output") or {})]
        except (ToolError, OSError) as exc:
            checks = [{"name": "verification_ran", "passed": False, "detail": str(exc)[:300]}]
        return all(c["passed"] for c in checks), checks

    def _ph_verify(self, pcb: ProcessControlBlock) -> None:
        sid = pcb.current["step_id"]
        step = pcb.step_by_id(sid)
        st = pcb.step_state(sid)
        r = self._take_result(pcb)
        result = r["value"] if r["ok"] else {"success": False, "error_code": "TOOL_INTERNAL_ERROR",
                                             "error_message": r["error"], "retryable": False}
        self._emit(pcb, "syscall.exit", f"{step['tool']}: {result.get('status') or ('ok' if result.get('success') else 'failed')}",
                   step_id=sid, success=bool(result.get("success")), error_code=result.get("error_code"),
                   duration_ms=result.get("duration_ms"), evidence=result.get("evidence"))
        if not result.get("success"):
            st["last_error"] = {"code": result.get("error_code"), "message": result.get("error_message"),
                                "retryable": bool(result.get("retryable"))}
            pcb.phase = Phase.RECOVER.value
            return
        ok, checks = self._run_verify(pcb, step, result)
        st["checks"] = checks
        if not ok:
            failed = [c["name"] for c in checks if not c["passed"]]
            st["last_error"] = {"code": "VERIFY_FAILED", "message": f"verification failed: {', '.join(failed)}",
                                "retryable": True}
            self._emit(pcb, "verify.failed", st["last_error"]["message"], level="warning", step_id=sid, checks=checks)
            pcb.phase = Phase.RECOVER.value
            return
        self.store.save_output(pcb.pid, sid, result.get("output") or {})
        st["status"] = "done"
        st["summary"] = bounded_json(result.get("output"), max_chars=6000, max_str=800, max_items=40)
        st["evidence"] = result.get("evidence")
        st.pop("last_error", None)
        self._emit(pcb, "step.done", f"Verified: {step.get('title') or sid}", step_id=sid, checks=checks)
        pcb.current = None
        pcb.phase = Phase.DISPATCH.value

    def _ph_recover(self, pcb: ProcessControlBlock) -> None:
        sid = (pcb.current or {}).get("step_id")
        st = pcb.step_state(sid) if sid else {}
        err = st.get("last_error") or {"code": "UNKNOWN", "message": "step failed", "retryable": False}
        if err.get("retryable") and int(st.get("attempts", 0)) <= int(pcb.budget["max_retries_per_step"]):
            self._emit(pcb, "step.retry", f"Retrying {sid}: {err.get('message')}", step_id=sid)
            st["status"] = "pending"
            pcb.phase = Phase.POLICY.value
            return
        st["status"] = "failed"
        tool = (pcb.step_by_id(sid) or {}).get("tool") if sid else None
        self._replan_or_fail(pcb, f"step {sid} ({tool}) failed: {err.get('code')}: {err.get('message')}")

    def _replan_or_fail(self, pcb: ProcessControlBlock, feedback: str) -> None:
        if pcb.replans < int(pcb.budget["max_replans"]):
            pcb.replans += 1
            pcb.planner_feedback = feedback[:4000]
            pcb.current = None
            # failed steps go back to pending so a corrected plan may reuse their ids
            for st in pcb.steps_state.values():
                if st.get("status") == "failed":
                    st["status"] = "pending"
            self._emit(pcb, "plan.replan", f"Replanning ({pcb.replans}/{pcb.budget['max_replans']}): {feedback[:300]}",
                       level="warning")
            pcb.phase = Phase.PLAN.value
            return
        self._fail(pcb, feedback)

    def _ph_reap(self, pcb: ProcessControlBlock) -> None:
        failed_sid = None
        for cpid in list(pcb.children):
            child = self.procs.get(cpid)
            if child is None:
                continue
            res = (child.exit or {}).get("result") or {}
            sid = child.child_step["id"]
            st = pcb.step_state(sid)
            st.pop("child", None)
            pcb.usage["executions"] = pcb.usage.get("executions", 0) + int(child.usage.get("executions", 0))
            if res.get("status") == "done":
                st.update({"status": "done", "summary": res.get("summary"), "evidence": res.get("evidence"),
                           "checks": res.get("checks"), "attempts": int(st.get("attempts", 0)) + 1, "via": cpid})
            else:
                st.update({"status": "pending", "attempts": int(st.get("attempts", 0)) + 1,
                           "last_error": res.get("error") or {"code": "CHILD_FAILED", "message": "child failed",
                                                             "retryable": True}})
                failed_sid = failed_sid or sid
            child.state = ProcState.EXITED.value
            self._save(child)
            self._emit(child, "proc.reaped", f"Reaped child for step {sid}", step_id=sid, status=res.get("status"))
        pcb.children = []
        if failed_sid:
            pcb.current = {"step_id": failed_sid}
            pcb.phase = Phase.RECOVER.value
        else:
            pcb.phase = Phase.DISPATCH.value

    def _ph_review(self, pcb: ProcessControlBlock) -> None:
        overlay = self._overlay(pcb, kernel_view=True)
        files = overlay.diff()
        if not files:
            if pcb.outbox and not (pcb.exit or {}).get("pending_failure"):
                pcb.post_commit = True
                pcb.phase = Phase.DISPATCH.value
            else:
                pcb.phase = Phase.FINALIZE.value
            return
        outcome = (pcb.exit or {}).get("pending_failure")
        if pcb.auto_commit and not outcome:
            self._emit(pcb, "txn.auto_commit", f"Committing {len(files)} change(s) automatically (undo is available)",
                       files=[{"path": f["path"], "op": f["op"]} for f in files])
            pcb.phase = Phase.COMMIT.value
            return
        pcb.pending_user = {"kind": "review", "files": bounded_json(files, max_chars=200000, max_str=60000),
                            "outbox": list(pcb.outbox), "outcome": "failed" if outcome else "succeeded",
                            "failure": outcome}
        pcb.state = ProcState.BLOCKED_USER.value
        self._task_status(pcb, "awaiting_review")
        self._emit(pcb, "txn.review", f"Review {len(files)} staged change(s) before anything is written",
                   files=[{"path": f["path"], "op": f["op"]} for f in files], outcome=pcb.pending_user["outcome"])

    def _ph_commit(self, pcb: ProcessControlBlock) -> None:
        overlay = self._overlay(pcb, kernel_view=True)
        try:
            unfinished = [t for t in self.store.txns_in_state(["COMMITTING"]) if t["pid"] == pcb.pid]
            if unfinished:
                self.journal.resume_commit(unfinished[0]["txn_id"])
                overlay.discard()
                res = {"txn_id": unfinished[0]["txn_id"], "files": unfinished[0]["summary"].get("files", [])}
            else:
                res = self.journal.commit(overlay, pcb.pid, pcb.task_id)
        except CommitConflict as exc:
            pcb.pending_user = {"kind": "review", "files": bounded_json(overlay.diff(), max_chars=200000, max_str=60000),
                                "conflicts": exc.conflicts, "outbox": list(pcb.outbox), "outcome": "conflict"}
            pcb.state = ProcState.BLOCKED_USER.value
            self._task_status(pcb, "awaiting_review")
            self._emit(pcb, "txn.conflict", "Some files changed on disk while the task ran; nothing was written",
                       level="warning", conflicts=exc.conflicts)
            return
        except (ToolError, OSError) as exc:
            msg = exc.message if isinstance(exc, ToolError) else str(exc)
            pcb.pending_user = {"kind": "review", "files": [], "outbox": list(pcb.outbox), "outcome": "commit_incomplete",
                                "error": msg[:500]}
            pcb.state = ProcState.BLOCKED_USER.value
            self._task_status(pcb, "awaiting_review")
            self._emit(pcb, "txn.incomplete", f"Commit could not finish ({msg[:200]}). Commit again to resume.",
                       level="error")
            return
        if res is not None:
            pcb.txn_id = res["txn_id"]
            self._emit(pcb, "txn.committed", f"Committed {len(res['files'])} file(s)", txn_id=res["txn_id"],
                       files=res["files"])
            hook = self.services.get("on_commit")
            if callable(hook):
                try:
                    hook(pcb.pid, res["files"])
                except Exception:  # pragma: no cover - a hook never breaks a commit
                    log.exception("on_commit hook failed")
        if pcb.outbox and not (pcb.exit or {}).get("pending_failure"):
            pcb.post_commit = True
            pcb.phase = Phase.DISPATCH.value
        else:
            pcb.phase = Phase.FINALIZE.value

    def _ph_finalize(self, pcb: ProcessControlBlock) -> None:
        failure = (pcb.exit or {}).get("pending_failure")
        steps = [{"id": s["id"], "title": s.get("title"), "tool": s.get("tool"),
                  "status": pcb.step_state(s["id"]).get("status")} for s in pcb.step_list()]
        summary = {"goal": (pcb.plan or {}).get("goal"), "steps": steps, "txn_id": pcb.txn_id,
                   "committed": bool(pcb.txn_id), "usage": pcb.usage}
        if failure:
            self._exit(pcb, ExitCode((pcb.exit or {}).get("pending_code") or "failed"), failure, summary=summary)
        else:
            self._exit(pcb, ExitCode.COMPLETED, "completed", summary=summary)

    # ---------------------------------------------------------- child phases
    def _ph_child_execute(self, pcb: ProcessControlBlock) -> None:
        step = pcb.child_step
        spec = self.registry.get(step.get("tool"))
        deny_msg, grant = self._gate(pcb, spec, step.get("args") or {}) if spec else ("unknown tool", None)
        if deny_msg or grant:
            self._exit(pcb, ExitCode.FAILED, deny_msg or "child cannot request grants",
                       result={"status": "failed", "error": {"code": "CAP_DENIED", "message": deny_msg, "retryable": False}})
            return
        overlay = self._overlay(pcb)
        ctx = self._ctx(pcb, step, overlay)
        decision = self.policy.evaluate(task_id=pcb.task_id, plan_version=pcb.plan_version, step=step,
                                        args=step.get("args") or {}, ctx=ctx, user_settings=self.settings_fn() or {})
        if decision.decision != "allow":
            self._exit(pcb, ExitCode.FAILED, decision.error_message or decision.decision, result={
                "status": "failed", "error": {"code": decision.error_code or "POLICY", "retryable": False,
                                              "message": decision.error_message or "not allowed in a child process"}})
            return
        args_model = self.registry.validate_args(spec.name, step.get("args") or {})
        pcb.usage["executions"] = pcb.usage.get("executions", 0) + 1
        executor = self.executor
        self._emit(pcb, "syscall.enter", f"{spec.name} (child)", step_id=step["id"], syscall=spec.name)
        self._io(pcb, "tool", lambda: executor.execute(spec, args_model, ctx), Phase.CHILD_EXECUTE, Phase.CHILD_VERIFY)

    def _ph_child_verify(self, pcb: ProcessControlBlock) -> None:
        step = pcb.child_step
        r = self._take_result(pcb)
        result = r["value"] if r["ok"] else {"success": False, "error_code": "TOOL_INTERNAL_ERROR",
                                             "error_message": r["error"], "retryable": False}
        if not result.get("success"):
            self._exit(pcb, ExitCode.FAILED, result.get("error_message") or "failed", result={
                "status": "failed", "error": {"code": result.get("error_code"), "message": result.get("error_message"),
                                              "retryable": bool(result.get("retryable"))}})
            return
        ok, checks = self._run_verify(pcb, step, result)
        if not ok:
            self._exit(pcb, ExitCode.FAILED, "verification failed", result={
                "status": "failed", "checks": checks,
                "error": {"code": "VERIFY_FAILED", "message": "verification failed", "retryable": True}})
            return
        self.store.save_output(pcb.ppid, step["id"], result.get("output") or {})
        self._exit(pcb, ExitCode.COMPLETED, "completed", result={
            "status": "done", "checks": checks, "evidence": result.get("evidence"),
            "summary": bounded_json(result.get("output"), max_chars=6000, max_str=800, max_items=40)})

    # ================================================================== exit
    def _fail(self, pcb: ProcessControlBlock, reason: str, code: ExitCode = ExitCode.FAILED) -> None:
        """Unrecoverable failure. Staged work is never thrown away silently: if there is any, the
        user reviews it (commit the partial result or discard it)."""
        if pcb.is_child:
            self._exit(pcb, ExitCode.FAILED, reason, result={"status": "failed", "error": {
                "code": "FAILED", "message": reason, "retryable": False}})
            return
        self._emit(pcb, "task.failing", reason, level="error")
        if not pcb.post_commit and self._overlay(pcb, kernel_view=True).has_changes():
            pcb.exit = {"pending_failure": reason, "pending_code": code.value}
            pcb.current = None
            pcb.pending_io = None
            pcb.phase = Phase.REVIEW.value
            if pcb.st() != ProcState.RUNNING:          # called outside a slice (deadline)
                pcb.state = ProcState.READY.value
                self.runq.push(pcb.pid, pcb.priority)
                self._save(pcb)
            return
        self._exit(pcb, code, reason)

    def _budget_exhausted(self, pcb: ProcessControlBlock, reason: str) -> None:
        self._emit(pcb, "budget.exceeded", reason, level="warning")
        if pcb.pending_io:
            self._cancel.setdefault(pcb.pid, threading.Event()).set()
            pcb.pending_io = None
        self._fail(pcb, f"budget exceeded: {reason}", ExitCode.BUDGET_EXCEEDED)

    def _terminate(self, pcb: ProcessControlBlock, reason: str) -> None:
        for cpid in list(pcb.children):
            child = self.procs.get(cpid)
            if child is not None and child.st() in LIVE_STATES:
                self._exit(child, ExitCode.CANCELLED, reason, result={"status": "failed", "error": {
                    "code": "CANCELLED", "message": reason, "retryable": False}})
            if child is not None:
                child.state = ProcState.EXITED.value
                self._save(child)
        if not pcb.is_child:
            self._overlay(pcb, kernel_view=True).discard()
        self._exit(pcb, ExitCode.CANCELLED, reason)

    def _exit(self, pcb: ProcessControlBlock, code: ExitCode, reason: str, *, summary: Optional[dict] = None,
              result: Optional[dict] = None) -> None:
        pcb.exit = {"code": code.value, "reason": reason, "summary": summary, "result": result, "at": self.clock()}
        pcb.pending_io = None
        pcb.pending_user = None
        pcb.phase = Phase.DONE.value
        self.runq.remove(pcb.pid)
        self.caps.revoke_all(pcb.pid)
        if pcb.is_child:
            pcb.state = ProcState.ZOMBIE.value
            self._save(pcb)
            self._emit(pcb, "proc.exit", f"Child exited: {code.value}", code=code.value, reason=reason)
            parent = self.procs.get(pcb.ppid)
            if parent is not None and parent.st() == ProcState.BLOCKED_WAIT and self._children_done(parent):
                parent.state = ProcState.READY.value
                self.runq.push(parent.pid, parent.priority)
                self._save(parent)
            elif parent is not None and parent.st() == ProcState.STOPPED and self._children_done(parent) \
                    and parent.stopped_from == ProcState.BLOCKED_WAIT.value:
                parent.stopped_from = ProcState.READY.value
                self._save(parent)
            return
        pcb.state = ProcState.EXITED.value
        if code in (ExitCode.DISCARDED, ExitCode.CANCELLED):
            self._overlay(pcb, kernel_view=True).discard()
        self._save(pcb)
        status = TASK_STATUS.get(code.value, "failed")
        extra: Dict[str, Any] = {"finished_at": utc_now_iso(), "summary": summary or {"reason": reason}}
        if code != ExitCode.COMPLETED:
            extra.update({"error_code": code.value.upper(), "error_message": reason[:1000]})
        self._task_status(pcb, status, **extra)
        self._emit(pcb, "proc.exit", f"Process exited: {code.value}", code=code.value, reason=reason,
                   txn_id=pcb.txn_id)
        hook = self.services.get("on_exit")
        if hook is not None:
            try:
                hook(pcb.pid, pcb.task_id, code.value)
            except Exception:  # noqa: BLE001 - resource cleanup must never break the kernel
                log.exception("on_exit hook failed")

    # ============================================================== user API
    def signal(self, pid: str, sig: str) -> dict:
        def do() -> dict:
            pcb = self._get(pid)
            s = Signal(sig)
            if pcb.st() in DONE_STATES:
                raise KernelError("PROCESS_EXITED", "the process has already exited")
            self._emit(pcb, "signal", f"Signal {s.value}", signal=s.value)
            if s == Signal.STOP:
                if pcb.st() != ProcState.STOPPED:
                    pcb.stopped_from = pcb.state if pcb.state != ProcState.RUNNING.value else ProcState.READY.value
                    pcb.state = ProcState.STOPPED.value
                    self.runq.remove(pid)
                    for cpid in pcb.children:
                        child = self.procs.get(cpid)
                        if child is not None and child.st() in LIVE_STATES and child.st() != ProcState.STOPPED:
                            child.stopped_from = child.state
                            child.state = ProcState.STOPPED.value
                            self.runq.remove(cpid)
                            self._save(child)
            elif s == Signal.CONT:
                if pcb.st() == ProcState.STOPPED:
                    pcb.state = pcb.stopped_from or ProcState.READY.value
                    pcb.stopped_from = None
                    if pcb.st() == ProcState.READY:
                        pcb.priority = Priority.BOOSTED.value
                        self.runq.push(pid, pcb.priority)
                    for cpid in pcb.children:
                        child = self.procs.get(cpid)
                        if child is not None and child.st() == ProcState.STOPPED:
                            child.state = child.stopped_from or ProcState.READY.value
                            child.stopped_from = None
                            if child.st() == ProcState.READY:
                                self.runq.push(cpid, child.priority)
                            self._save(child)
            else:  # TERM / KILL
                pcb.cancel_requested = True
                self._cancel.setdefault(pid, threading.Event()).set()
                for cpid in pcb.children:
                    self._cancel.setdefault(cpid, threading.Event()).set()
                self._terminate(pcb, "cancelled by user" if s == Signal.TERM else "killed by user")
            self._save(pcb)
            return pcb.public()

        return self._command(do)

    def approve(self, pid: str, action_hash: str, *, standing_minutes: Optional[float] = None) -> dict:
        def do() -> dict:
            pcb = self._get(pid)
            pend = pcb.pending_user or {}
            if pend.get("kind") != "approval":
                raise KernelError("NO_PENDING_APPROVAL", "nothing is waiting for approval")
            if pend.get("action_hash") != action_hash:
                raise KernelError("APPROVAL_MISMATCH", "this approval is for a different or changed action")
            grant = pend.get("grant")
            if grant:
                ttl = float(standing_minutes) * 60 if standing_minutes else None
                cap = self.caps.grant(pid, grant["kind"], grant["scope"], [grant["right"]], ttl_s=ttl,
                                      uses=None if ttl else 1, origin="approval")
                pcb.caps.append(cap.cap_id)
                self._emit(pcb, "cap.grant", f"Granted {grant['kind']}:{grant['right']} on {grant['scope']}"
                           + (f" for {standing_minutes:g} min" if ttl else " (one use)"), step_id=pend.get("step_id"))
            pcb.current = pcb.current or {"step_id": pend["step_id"]}
            pcb.current["approved_hash"] = action_hash
            pcb.pending_user = None
            pcb.phase = Phase.EXECUTE.value
            self._wake(pcb)
            self._emit(pcb, "approval.decided", "Approved", step_id=pend.get("step_id"), approved=True)
            return pcb.public()

        return self._command(do)

    def reject(self, pid: str, action_hash: str, note: str = "") -> dict:
        def do() -> dict:
            pcb = self._get(pid)
            pend = pcb.pending_user or {}
            if pend.get("kind") != "approval" or pend.get("action_hash") != action_hash:
                raise KernelError("NO_PENDING_APPROVAL", "nothing matching is waiting for approval")
            sid = pend["step_id"]
            pcb.step_state(sid)["last_error"] = {"code": "APPROVAL_DENIED", "retryable": False,
                                                 "message": "the user declined this action" + (f": {note}" if note else "")}
            pcb.pending_user = None
            pcb.current = {"step_id": sid}
            pcb.phase = Phase.RECOVER.value
            self._wake(pcb)
            self._emit(pcb, "approval.decided", "Declined", step_id=sid, approved=False)
            return pcb.public()

        return self._command(do)

    def answer(self, pid: str, text: str) -> dict:
        def do() -> dict:
            pcb = self._get(pid)
            pend = pcb.pending_user or {}
            if pend.get("kind") != "clarify":
                raise KernelError("NO_PENDING_QUESTION", "the task is not waiting for an answer")
            pcb.clarification = {"question": pend.get("question"), "answer": str(text)[:4000]}
            pcb.pending_user = None
            pcb.phase = Phase.PLAN.value
            self._wake(pcb)
            return pcb.public()

        return self._command(do)

    def commit(self, pid: str) -> dict:
        def do() -> dict:
            pcb = self._get(pid)
            if (pcb.pending_user or {}).get("kind") != "review":
                raise KernelError("NO_PENDING_REVIEW", "there is nothing staged to commit")
            pcb.pending_user = None
            pcb.phase = Phase.COMMIT.value
            self._wake(pcb)
            return pcb.public()

        return self._command(do)

    def discard(self, pid: str) -> dict:
        def do() -> dict:
            pcb = self._get(pid)
            if (pcb.pending_user or {}).get("kind") != "review":
                raise KernelError("NO_PENDING_REVIEW", "there is nothing staged to discard")
            self._overlay(pcb, kernel_view=True).discard()
            self._emit(pcb, "txn.discarded", "Staged changes discarded; nothing was written")
            self._exit(pcb, ExitCode.DISCARDED, "discarded by user")
            return pcb.public()

        return self._command(do)

    def undo(self, pid: str) -> dict:
        def do() -> dict:
            pcb = self._get(pid)
            if not pcb.txn_id:
                raise KernelError("NOTHING_TO_UNDO", "this task did not commit any changes")
            try:
                res = self.journal.undo(pcb.txn_id)
            except CommitConflict as exc:
                raise KernelError("UNDO_CONFLICT", "; ".join(f"{c['path']}: {c['reason']}" for c in exc.conflicts))
            self._emit(pcb, "txn.undone", f"Undid {len(res['files'])} file change(s)", txn_id=pcb.txn_id,
                       files=res["files"])
            try:
                self.tasks.update(pcb.task_id, status="undone")
            except Exception:  # pragma: no cover
                pass
            return res

        return self._command(do)

    def _wake(self, pcb: ProcessControlBlock) -> None:
        if pcb.st() == ProcState.STOPPED:
            pcb.stopped_from = ProcState.READY.value
        else:
            pcb.state = ProcState.READY.value
            pcb.priority = Priority.BOOSTED.value
            self.runq.push(pcb.pid, pcb.priority)
        self._save(pcb)

    # ----------------------------------------------------------- inspection
    def ps(self) -> List[dict]:
        """`/proc`-style listing of every non-reaped process."""
        return self._command(lambda: [
            {**p.public(), "capabilities": self.caps.public(p.pid)} for p in self.procs.values()
            if p.st() != ProcState.EXITED or not p.is_child
        ])

    def proc(self, pid: str) -> dict:
        return self._command(lambda: {**self._get(pid).public(), "capabilities": self.caps.public(pid)})

    def staged_diff(self, pid: str) -> List[dict]:
        return self._command(lambda: self._overlay(self._get(pid), kernel_view=True).diff())
