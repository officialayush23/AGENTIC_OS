# Agentic OS v0.1 — Build Plan

This is the working plan for v0.1: what we're building, what has to be in it, and how the work is ordered. It merges the two specifications the project owner provided (the *Desktop Product & Integration* spec and the *Master Build Prompt v0.1*). Where the two disagree, the newer Master Build Prompt wins: the browser-first prototype comes first, and desktop packaging is kept compatible but does not block anything.

`PLAN.md` at the repository root is the original Electron/Ollama/Supabase concept. It is kept for history and is **superseded** by this document.

> **2026-11-19: v0.1 is feature-complete** (kernel, RAG knowledge base, FastAPI API, React UI, Tauri 2
> desktop shell with a PyInstaller sidecar). Status per item lives in `docs/PROGRESS.md`.
>
> **2026-11-15 revision: the kernel.** Orchestration is now a userspace OS kernel instead of a LangGraph graph: processes, a scheduler, capabilities, a copy-on-write overlay workspace and a commit journal. **Every task is a transaction**: the agent's writes are staged, the user reviews one diff, then commits or discards, and can undo a commit later. Full design: `docs/KERNEL.md`. Rows and sections changed by this revision are marked *(kernel)*.

---

## 1. What we are building

Agentic OS is a desktop agent platform, not a chatbot and not an OS kernel. The user types a goal in natural language. The system then:

1. spawns a **process** for the goal, with default capabilities and its own overlay workspace *(kernel)*,
2. produces a structured, validated plan made of registered tools,
3. checks every step against capabilities and a deterministic policy engine,
4. stages every file write in the overlay; irreversible actions (app launch) need an approved capability grant *(kernel)*,
5. executes steps through typed tools, running independent read-only steps in parallel child processes *(kernel)*,
6. observes and **verifies** the outcome with deterministic postconditions,
7. retries or replans within strict budgets,
8. shows the user **one diff of all staged changes** to commit or discard, with undo after commit *(kernel)*, and
9. reports what actually happened, with evidence.

v0.1 runs as **two local processes**: a FastAPI backend bound to `127.0.0.1` and a React UI. In the default mode the backend serves the built UI itself, so there is only one process to start. The design keeps a clean HTTP boundary so a Tauri 2 shell can wrap it later.

## 2. Must-haves (P0, required for the demo / definition of done)

| # | Capability | Notes |
|---|------------|-------|
| 1 | Backend starts and serves `/health` | loopback only; random pairing code printed at startup |
| 2 | Local request authentication | pairing code → HttpOnly SameSite=Strict session cookie, or Bearer token (Tauri/CLI); Host + Origin validation; strict CORS; custom header required |
| 3 | Provider settings saved securely | non-secret config in SQLite, API key in OS credential store (`keyring`); never returned to the UI; clear error if the store is unavailable |
| 4 | Provider adapters | OpenAI, Anthropic, Gemini, OpenAI-compatible, Fake (tests). Normalized errors, usage and latency; test-connection with capability probe; no silent fallback |
| 5 | Tool registry | typed Pydantic input/output schemas, capabilities, risk, timeout, idempotency, verification, audit fields |
| 6 | Workspace tools | list, search, read, create, edit, metadata, plus deterministic config-file identification and report composition; path guard against traversal, symlinks, reserved names and sensitive files; atomic writes; hash-based conflict detection |
| 7 | Application tools | list and launch **allowlisted** apps only (no shell, no arbitrary args, no elevation) |
| 8 | System tools | minimal system info and a read-only process summary (names, PIDs, memory only) |
| 9 | Policy engine | deterministic and authoritative; risk levels READ_ONLY → PRIVILEGED_OR_UNSUPPORTED; model output can only *raise* strictness |
| 10 | Kernel process model *(kernel)* | PCB per task; states READY/RUNNING/BLOCKED_IO/BLOCKED_USER/BLOCKED_WAIT/STOPPED/ZOMBIE/EXITED; phases plan → validate → dispatch → policy → execute → verify → recover/replan → review → commit → outbox → finalize; single-threaded loop, async I/O, signals, fork/wait/reap |
| 11 | Approvals and transactions *(kernel)* | Staged writes are approved once, at commit review; irreversible actions need an approval that grants a one-shot or time-limited capability, bound to the exact action hash; PCB checkpointed to SQLite after every time slice |
| 12 | Bounded execution | ≤12 steps, ≤20 step executions, ≤2 retries per step, ≤2 replans, wall-clock deadline, LLM call budget; a budget stop keeps staged work for review instead of discarding it |
| 13 | Idempotency | execution records keyed by task + tool + canonical args; completed side effects are reused rather than repeated on resume or retry |
| 14 | Events & audit | structured, redacted events with correlation IDs, persisted in SQLite, streamed via SSE |
| 15 | UI | pairing, onboarding, composer, live timeline, plan preview, evidence, approvals with exact arguments and diff, provider settings, history and search, settings, status indicators |
| 16 | Restart safety | tasks awaiting approval or review survive restarts; in-flight I/O is re-issued (idempotency + reconcile prevent duplicates); interrupted commits roll forward from the journal *(kernel)* |
| 17 | Tests | unit, graph, and API tests with a fake provider and temp workspace; opt-in real-provider smoke test; frontend component tests |
| 18 | Docs | README, architecture diagram, security notes, API reference, implemented-vs-deferred list, test results, defects |

## 3. Should-haves (P1, done if time allows; each is marked clearly when complete)

- ✅ A Tauri 2 desktop shell that spawns the backend as a sidecar and pairs over a stdin token handshake.
- ✅ *(added)* Local RAG knowledge base: SQLite documents/chunks/vectors + FTS5, hybrid retrieval, pluggable
  embedders (offline hashing, Ollama, OpenAI, Gemini), `search_knowledge` tool. See `docs/RAG.md`.
- Web search and page extraction tools (provider interface; Brave or Tavily key), with SSRF guards and robots.txt.
- An LLM-written narrative final summary, used only when the deterministic summary isn't enough.
- An MCP client adapter (stdio) whose tools default to `SENSITIVE_WRITE` until the user reclassifies them.

## 4. Deferred (designed, not built in v0.1)

Gmail, Google Calendar/Meet, Notion, Playwright browser automation, Windows UI Automation, clipboard, notifications, screenshots, scheduling, and local inference (Ollama or llama.cpp). Interfaces and extension points exist so these can be added without redesign; see `docs/ROADMAP.md`. Nothing deferred is presented as working in the UI.

## 5. Delivery slices

| Slice | Content | Exit check |
|-------|---------|------------|
| 1 | Repo inspection, backend skeleton, health, auth, secure provider settings | `/health` 200; provider credential round-trips through keyring and is never returned |
| 2 | Provider adapters, tool registry, workspace tools, policy engine | Tools and policy importable; done before the kernel revision |
| K1–K2 *(kernel)* | PCB, phase machine, checkpoint per slice, kernel loop, async I/O, interrupts, signals, fork/wait/reap | One process runs a plan end to end on the fake planner; STOP/CONT/TERM/KILL tested |
| K3 *(kernel)* | Capabilities and the overlay resolver as the path-level enforcement point | Attenuation, expiry, one-shot, revocation and scope denial tested |
| K4 *(kernel)* | Overlay workspace, journal, review, commit, undo, outbox | Real workspace untouched before commit; undo restores exact bytes; conflicts refuse to write |
| K6 *(kernel)* | Deterministic simulation harness | Crash at any tick or during commit converges to the crash-free result |
| 5 | Full UI, SSE progress, history and details | Real backend state shown; no fake progress |
| 6 | Test suite, security review, smoke test, docs | Tests pass or failures are documented; README matches reality |

## 6. Key engineering decisions

| Decision | Why |
|----------|-----|
| Backend serves the built UI on the same origin | One process for demos; simpler CORS; cookie auth works same-origin |
| Pairing code → session cookie for browsers; Bearer for Tauri/CLI | Binding to localhost does **not** stop other local software or malicious web pages (DNS rebinding, CSRF) from reaching the port |
| Own kernel loop instead of LangGraph *(kernel)* | A scheduler must own every step to preempt, stop, fork and checkpoint; a framework's run-to-completion loop competes for that control. The phase machine is ~1k lines we fully own and can simulate deterministically |
| JSON-only PCB | Safe to checkpoint; no pickle/msgpack deserialization |
| Every task is a transaction *(kernel)* | Users want one task done well and trustworthy; one informed review beats many per-step prompts, and undo makes approving safe |
| Writes staged in a copy-on-write overlay; tools unchanged *(kernel)* | `OverlayWorkspace` is a drop-in `WorkspaceGuard`, so every existing tool became transactional without edits |
| Capabilities, not just risk levels *(kernel)* | Least authority per process; children get attenuated read-only copies; prompt injection cannot use authority the process never held |
| Deterministic rules router for known operations | Spec requires determinism; the demo works with no API key; saves cost and latency |
| Model-declared risk/approval is advisory and can only escalate | The LLM must never override policy |
| No delete tool in v0.1 | Destructive operations are unsupported by default |
| `keyring` with no plaintext fallback | The spec forbids secrets in SQLite or logs; failing closed is safer |
| ~~LangGraph ≥ 1.0.10, checkpoint-sqlite ≥ 3.0.1~~ | No longer a dependency after the kernel revision |

## 7. Risks

- **Build-environment limits:** the environment this was built in had no network access, so third-party packages could not be installed there. Logic that depends only on the standard library was executed there; the full suite must be run on the target machine (see `docs/TESTING.md`).
- **Provider drift:** model IDs and parameter support change frequently. Defaults are editable, and the connection test reports the actual error.
- **Windows specifics:** execution policy, long paths, junctions and file locking. The scripts are `.bat` to avoid PowerShell execution-policy prompts, and the path guard resolves junctions with `realpath`.
