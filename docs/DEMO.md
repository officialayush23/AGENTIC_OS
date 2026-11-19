# Demonstrating the OS in Agentic OS

## What the "OS" actually is

Agentic OS runs AI tasks the way an operating system runs programs. The AI model is treated like an
**untrusted user program**: it never touches your files, mail or apps directly. It can only ask the
**kernel** (`backend/agentic_os/kernel/`) through **syscalls** (tool calls), and the kernel decides
whether, when and where each one runs, and how to undo it.

| OS concept | In Agentic OS | Where to see it | Code |
|---|---|---|---|
| Process + PCB | Every task is a process with a JSON process control block (state, phase, budget, children) | Kernel monitor → Process table | `kernel/process.py` |
| Process states | READY, RUNNING, BLOCKED_IO, BLOCKED_USER, BLOCKED_WAIT, STOPPED, ZOMBIE, EXITED | State column | `kernel/process.py` |
| Program counter | The phase: plan → validate → dispatch → policy → execute → verify → review → commit | Phase column | `kernel/kernel.py` |
| Scheduler | Single-threaded kernel loop; multi-level run queue (boosted / normal / background); one phase per time slice; aging demotes long runners | Run queue card, Slices column | `kernel/scheduler.py` |
| System calls | Tool calls, checked against capabilities and policy before they run | Kernel log: `syscall.enter`, `policy.decision` | `kernel/kernel.py` |
| Async I/O + interrupts | Model and tool calls run on an I/O driver; completion is an interrupt that wakes the process | State flips BLOCKED_IO → READY | `kernel/io.py` |
| fork / wait / zombie / reap | Independent read-only steps run as child processes; parent waits; children become zombies until reaped | fork demo | `kernel/kernel.py` `_fork` |
| Signals | STOP, CONT, TERM, KILL | STOP/CONT/TERM/KILL buttons | `kernel/kernel.py` `signal` |
| Capabilities | Unforgeable, attenuable, expiring grants in a kernel table; children get narrower ones; approving an action is a grant | Capabilities held table | `kernel/capabilities.py` |
| MMU / address space | Every path a tool touches is checked against the process's `fs` capability and mapped into its overlay | (denials in the log) | `kernel/overlay.py` |
| Copy-on-write file system | Each task writes into its own upper layer over the real workspace | Task → Changes (diff) | `kernel/overlay.py` |
| Journal / write-ahead log | Two-phase commit with before/after blobs; roll-forward on crash; Undo | Journal card, Undo | `kernel/journal.py` |
| Transactional outbox | Irreversible actions (send mail) that depend on staged files wait until after commit | `outbox.deferred` in the log | `kernel/kernel.py` |
| rlimits | Per-process budgets: steps, executions, retries, replans, model calls, wall clock | Budget meters | `kernel/process.py` |
| Checkpoint + crash recovery | PCB saved after every slice; on boot the kernel reloads processes and re-issues in-flight I/O | `proc.recovered` in the log | `kernel/store.py` |
| Timer interrupts | Schedules fire tasks like cron | Schedules and listeners | `automation/` |
| Mounts | `~/` (Home: notes, mail, calendar) is a second file system in the same namespace | Notes / Calendar | `kernel/overlay.py` |

## The 8-minute demo

Start the app (`.\start.bat`) and open **Kernel monitor** in the left rail (the chip icon). Every
demo button spawns a scripted process, so it works without an AI provider.

1. **Processes and the scheduler (1 min).** Click **Scheduler**. Three processes appear. Point at the
   State column (READY ↔ BLOCKED_IO), the Slices counter, and the Run queue card. Say: "one kernel loop,
   one phase per time slice, I/O never blocks the loop."
2. **fork / wait / reap (1 min).** Click **fork / wait**. Four children appear under the parent; the
   parent shows *Blocked: wait()*. Finished children show *Zombie* until the parent reaps them, then the
   parent resumes. Click a child: its capability table is narrower than the parent's (attenuation).
3. **Signals (1 min).** Click **Signals** (a 30-second job). Press **STOP**: state becomes *Stopped*,
   the run queue empties. Press **CONT**: it comes back with *boosted* priority for one slice. Press
   **TERM** or **KILL** to end it.
4. **Crash recovery (1 min).** Start **Signals** again. Halfway through, close the server window (or
   press Ctrl+C). Run `.\start.bat` again and open Kernel monitor: the same process is back and finishes.
   The log shows `proc.recovered`.
5. **Transactions: copy-on-write, commit, undo (2 min).** Click **Transaction**, then **Open task**.
   Show the diff: two files exist only in the task's overlay; the real folder is unchanged (open it in
   Explorer to prove it). Click **Commit**: the Journal card shows the transaction with 2 files. Click
   **Undo**: the files are restored from the journal's before-images.
6. **Capabilities (1 min).** Add any app in Settings → Apps (one click). Click **Capabilities**. The
   process blocks (*Blocked: user*) because it holds no `app:launch` capability. Approve once: the
   capability table shows `app · launch · uses left 1`, the app opens, and the grant is used up.
7. **rlimits (30 s).** Click **rlimits**: a 5-step plan with a budget of 2 executions. The kernel
   terminates it (`budget.exceeded`) and nothing is committed.
8. **The agent on top (30 s).** In Conversation, say "schedule a meeting with priya@example.com tomorrow
   at 3pm". The same kernel runs it: plan, policy, approval for the external action, review, commit.

## Proof that it is correct

`cd backend && .venv\Scripts\python -m pytest tests/kernel -q` runs the kernel tests, including
deterministic simulation tests: a simulated clock and I/O driver inject crashes at every journal step
and check that commit and recovery always end in a consistent state (see `docs/KERNEL.md` §11).

## Honest limits

It is a **userspace** kernel: it does not use OS sandboxing (Job Objects, namespaces). It controls
everything the agent does through its tools, but it is not a security boundary against code running
on your machine. `docs/KERNEL.md` §9 explains this.
