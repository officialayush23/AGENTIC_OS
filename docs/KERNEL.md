# Agentic OS Kernel — Design

Status: v0.1 design, adopted 2026-11-15. Supersedes the LangGraph orchestration design in
`BUILD_PLAN.md` §2 rows 10–12 and §6.

## 1. Thesis

**The LLM is an untrusted user-space program. The runtime is a kernel.**

The model never touches the machine. It can only ask the kernel to do things through
syscalls (tool calls). The kernel decides whether a call runs (capabilities + policy),
when it runs (scheduler), where its writes land (a per-task copy-on-write overlay), and
how to undo it (a journal).

**Product promise: every task is a transaction.** The agent works against a staged copy
of the workspace. Nothing touches real files until the user reviews one diff and clicks
**Commit**. After a commit, **Undo** restores the previous state from the journal.
This replaces per-step write approvals with one informed decision.

Agentic OS is a *userspace* runtime: it does not sandbox with OS primitives (Job Objects,
AppContainer, namespaces). Section 9 is honest about what that does and does not
guarantee.

## 2. Concept map

| OS concept | Agentic OS implementation | Module |
|---|---|---|
| Process / PCB | One process per task; JSON process control block | `kernel/process.py` |
| Process states | `READY RUNNING BLOCKED_IO BLOCKED_USER BLOCKED_WAIT STOPPED ZOMBIE EXITED` | `kernel/process.py` |
| Program counter | Agent phase: `plan → validate → dispatch → policy → execute → verify → … → review → commit → outbox → finalize` | `kernel/kernel.py` |
| Scheduler | Priority run queue (boosted / normal / background), FIFO within a level, one phase per time slice | `kernel/scheduler.py` |
| Syscalls | Tool calls dispatched by the kernel after capability + policy checks | `kernel/kernel.py` |
| Async I/O + interrupts | LLM and tool calls run on an I/O driver; completions are interrupts that wake the process | `kernel/io.py` |
| Signals | `STOP CONT TERM KILL` | `kernel/kernel.py` |
| fork / wait / zombie / reap | Parallel read-only steps run as child processes with attenuated capabilities | `kernel/kernel.py` |
| Capabilities | Unforgeable, attenuable, expiring grants held in a kernel table | `kernel/capabilities.py` |
| MMU / address space | Overlay resolver: every path a tool touches is checked against the process's capabilities and mapped into its overlay | `kernel/overlay.py` |
| Copy-on-write FS (overlayfs) | Per-task upper dir over the real workspace (lower) | `kernel/overlay.py` |
| Journal / WAL | Two-phase commit log with before/after blobs; roll-forward recovery; undo | `kernel/journal.py` |
| Transactional outbox | Irreversible actions that depend on staged files are deferred until after commit | `kernel/kernel.py` |
| rlimits | Per-process budgets: steps, executions, retries, replans, LLM calls, wall clock | `kernel/process.py` |
| Checkpoint / crash recovery | PCB persisted after every slice; boot reloads and re-issues in-flight I/O | `kernel/store.py` |

## 3. Process model

Each task is a process. The PCB (`ProcessControlBlock`) is plain JSON, so it checkpoints
safely (no pickle, no msgpack).

Two independent pieces of state:

- **Scheduler state** (kernel-owned): `READY · RUNNING · BLOCKED_IO · BLOCKED_USER ·
  BLOCKED_WAIT · STOPPED · ZOMBIE · EXITED`.
- **Phase** (the program counter): what the agent does next.

```
plan ─► validate ─► dispatch ─┬─► policy ─► execute ─► verify ─► dispatch …
   ▲        │                 │      │                    │
   │        ▼                 │      ▼                    ▼
   └──── recover ◄────────────┼── (deny / precondition / verify failed)
                              ├─► fork children ─► reap ─► dispatch
                              └─► review ─► commit ─► outbox ─► finalize
```

A phase is pure kernel logic. It never performs slow I/O itself. When it needs an LLM
call or a tool run it issues a request to the I/O driver and the process moves to
`BLOCKED_IO`. When the driver finishes, it raises an interrupt: the completion is
recorded in the PCB and the process becomes `READY`.

### Budgets (rlimits)

| Limit | Default |
|---|---|
| Steps per plan | 12 |
| Step executions per task | 20 |
| Retries per step | 2 |
| Replans per task | 2 |
| LLM calls per task | 8 |
| Wall-clock deadline | 15 minutes |

Exceeding any limit terminates the process with a clear exit reason. Staged changes are
kept for review, never silently committed.

## 4. Scheduler

The kernel loop is **single-threaded**. Kernel data structures (process table,
capability table, run queue) are only touched by the loop thread, so they need no locks.
Calls from other threads (HTTP handlers) are posted to an inbox the loop drains at the
start of each tick.

One **time slice** is one phase. Phases are short because they don't do I/O.

The run queue has three levels:

- **boosted (0):** a process the user just unblocked (approval, answer, CONT). One slice only.
- **normal (1):** default.
- **background (2):** a process that has run more than 64 slices drops here.

FIFO within a level. Boosts are one-shot, so nothing starves.

The product priority is one task done well, so concurrency is mostly *inside* a task
(fork) rather than across tasks. The scheduler still supports many processes.

## 5. Capabilities

A capability is a kernel-held record: `(cap_id, pid, kind, scope, rights, expires_at,
uses_left, parent_cap, origin)`. Processes refer to capabilities by id. The model never
sees or produces them, so it can't forge one.

| Kind | Scope | Rights | Covers tool capability |
|---|---|---|---|
| `fs` | path prefix inside the workspace (`""` = whole) | `read`, `write` | `workspace.read`, `workspace.write` |
| `app` | app id or `*` | `launch` | `apps.launch` |
| `sys` | `*` | `read` | `system.read`, `apps.read` |
| `compute` | `*` | `use` | `compute` |

Rules:

1. **Default grant at spawn:** `fs:read ""`, `fs:write ""` (writes are staged, so they are
   reversible), `sys:read`, `compute:use`. **No `app:launch`**: launching an app is
   irreversible.
2. **Attenuation only:** a child capability's scope must be inside the parent's, its
   rights a subset, its expiry no later. `attenuate()` enforces this.
3. **Approval is a grant:** approving an irreversible action creates an `app:launch`
   capability, either one-shot (`uses_left=1`) or standing for N minutes. With a
   standing grant, the same action later doesn't ask again.
4. **Two enforcement points:** the syscall gate (tool-level capability names) and the
   overlay resolver (path-level `fs` scope and rights). Policy still runs after both.
   Capabilities can only narrow what policy allows.

## 6. Overlay workspace (copy-on-write)

Each root process gets an overlay: `lower` = the real workspace, `upper` =
`<data_dir>/overlays/<overlay_id>/upper`.

- **Read** of a path: the upper copy if one exists, otherwise lower.
- **Write** resolve of an existing file: copy-up into upper first, recording the base
  SHA-256 in `kernel_overlay_files`; the tool then writes the upper copy.
- **New file:** created in upper; base recorded as "did not exist".
- **Listing / search:** a merged view of lower and upper.
- All `WorkspaceGuard` safety checks (traversal, junctions, reserved names, sensitive
  files) still run against the real lower path first.

Existing tools need **no changes**. They already resolve every path through
`ctx.workspace`, and the kernel hands them an `OverlayWorkspace` instead of the plain
guard.

Policy is told the writes are transactional, so `workspace.write` steps no longer need
per-step approval (rule `P016_deferred_to_commit`). The commit review is the approval.
Sensitive files are still denied, and `require_approval_for_writes` is still honoured.

## 7. Journal, commit, undo

Commit is two-phase and crash-safe:

1. **Compute the change set** by scanning upper; skip files whose hash equals the base.
2. **Conflict check:** each lower file must still match its recorded base (or still be
   absent). If the user edited a file meanwhile, the commit is refused with a conflict
   list. Nothing is written.
3. **Prepare:** copy before/after blobs into `<data_dir>/journal/<txn_id>/` and write one
   `PREPARED` journal row per file. The transaction is in state `PREPARING`.
4. **Commit point:** the transaction moves to `COMMITTING`. From here recovery rolls
   *forward*.
5. **Apply:** atomic replace of each lower file (creating parent folders and recording
   them); each row becomes `APPLIED`.
6. **Done:** the transaction is `COMMITTED` and upper is cleared.

Recovery on boot: `COMMITTING` transactions are rolled forward (rows already matching
their after-hash are just marked). `PREPARING` transactions are aborted (nothing was
applied). `UNDOING` transactions are rolled forward as undo.

**Undo** walks a committed transaction in reverse. Each lower file must still match its
after-hash (otherwise there's a conflict and nothing changes). It restores the before
blob, or removes a file the task created, then removes folders the commit created if
they're empty.

## 8. Irreversible actions and the outbox

Tools are classified per call:

- **Reversible** (`workspace.write`): staged in the overlay.
- **Irreversible** (`apps.launch`, and later network, email, and so on): require an
  `app:launch` capability, which is obtained by approval. If the action refers to a file
  with staged changes (for example "open the report in Notepad"), the step is moved to
  the **outbox** and runs only after a successful commit, against the committed file.

## 9. Security boundaries (honest version)

- The kernel guarantees that **tools reached through the kernel** only see paths their
  process holds capabilities for, and that writes land in the overlay.
- It does **not** stop a buggy tool implementation from calling `open()` on an arbitrary
  path: there is no OS-level sandbox in v0.1. Tools are first-party code reviewed against
  this rule. Third-party (MCP) tools must run out of process when they arrive.
- The model's output never selects a capability, a policy decision, or a commit.

## 10. Persistence

Migration 2 adds `kernel_processes`, `kernel_capabilities`, `kernel_overlay_files`,
`kernel_txns` and `kernel_journal`. Root processes also get a `tasks` row (mirrored
status), so existing events, executions and idempotency records keep their foreign keys.

## 11. Deterministic simulation testing

All I/O goes through an `IODriver`, and time comes from an injectable clock. The test
`SimIO` driver completes requests in a seeded random order, optionally performing a side
effect at issue time and delivering it later. That models "crashed after the effect,
before the kernel heard about it". Tests run many seeds and crash/reboot at random slices,
then assert the invariants:

- the real workspace is unchanged until commit;
- commit then undo restores the exact original bytes;
- crash at any point plus reboot reaches the same final workspace as a crash-free run;
- no step executes after `TERM`/`KILL`; `STOP` freezes progress; `CONT` resumes it.

## 12. Data flow between steps and planners

A step can use an earlier step's output as an argument: `{"$ref": "list.output"}` or
`{"$ref": "report.output.markdown"}`. References count as dependencies. They are checked at plan
validation (they must point to an earlier step), and they are resolved by the kernel right before policy,
so the action hash binds the *resolved* arguments. Full outputs are kept in `kernel_step_outputs`; the
PCB only keeps a bounded summary.

Planners: `RulesPlanner` (deterministic, offline) handles common requests. `HybridPlanner` tries rules
first and falls back to `LLMPlanner` when an AI provider is configured (Settings → Planner chooses
auto / rules / llm).

## 13. Not in v0.1

Multilevel feedback queue tuning, lock manager and deadlock detection (no cross-task
writes to the same files in the single-task product), VFS mounts for connectors, context
paging, interrupts from file watchers or email, OS-level sandboxing.
