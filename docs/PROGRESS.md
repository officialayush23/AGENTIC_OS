# Progress & Handoff Log

This file is the single place to see **where the build stands** and **what to do next**. Update it at the end of every work session.

Legend: ✅ done and verified · 🟡 implemented, not yet executed on a real install · ⏳ in progress · ⬜ not started · ⏸ deferred

## Current status (2026-11-19)

| Area | Status | Notes |
|------|--------|-------|
| Kernel (processes, scheduler, signals, fork/wait, capabilities, overlay, journal) | ✅ | `docs/KERNEL.md`; now with `$ref` data flow between steps |
| Planners | ✅ / 🟡 | Rules planner (offline) ✅; hybrid rules→LLM ✅; LLM planning not yet run against a real provider key |
| Knowledge base (RAG) | ✅ | `docs/RAG.md`; local embedder ✅; Ollama/OpenAI/Gemini embedders 🟡 (written to their HTTP APIs, not run with real keys) |
| FastAPI backend + SSE | ✅ | `agentic_os/api/`; verified with a real server, pairing, live SSE |
| React UI | ✅ | Tasks (diff review, commit/discard/undo, approvals, trace), Knowledge, Settings |
| Tauri 2 desktop shell | ✅ / 🟡 | Built and run on Linux. **Not yet built on Windows.** |
| Windows installer (`build.bat`) | 🟡 | PyInstaller sidecar verified on Linux; the NSIS/MSI bundle step not yet run |
| Scripts + README | ✅ | `setup.bat`, `run.bat`, `dev-desktop.bat`, `dev-browser.bat`, `test.bat`, `build.bat` |
| Tests | ✅ | 184 backend + 3 frontend, all passing; see `docs/TESTING.md` |

## Session log

### Session 1 — 2026-10-16

- Inspected the repository. It contained only `PLAN.md` (the original Electron/Ollama/Supabase concept), with no source code, manifests or tests. Nothing needed preserving except that document.
- The build sandbox had **no network access**: pip, npm, crates.io and GitHub were all blocked, and there was no Rust toolchain. FastAPI, LangGraph, Pydantic and React could not be executed there.
- Researched current provider model IDs and library security advisories, which led to the LangGraph and checkpoint pins (now dropped, see session 2).

### Session 2 — 2026-11-15: the kernel

**Decisions (from a product brainstorm with the owner)**

1. Agentic OS gets real OS concepts that change behaviour, not just names. Thesis: *the LLM is an untrusted user-space program; the runtime is a kernel.*
2. It's a product; it stays a userspace runtime (no OS-level sandboxing in v0.1).
3. Users want **one task done well**, not many in parallel. So the product promise became **every task is a transaction**: staged writes, one review, commit or discard, undo later. A multi-task "task manager" was considered and dropped as the hero.
4. LangGraph is dropped (option B). The kernel owns the loop so it can preempt, stop, fork, checkpoint and be simulated deterministically.

**What was built** (`backend/agentic_os/kernel/`, about 2,400 lines)

| File | Contents |
|---|---|
| `process.py` | `ProcState`, `Phase`, `Signal`, `Priority`, `ExitCode`, `Budget`, JSON `ProcessControlBlock` |
| `scheduler.py` | three-level run queue, one-shot boost, demotion after 64 slices |
| `io.py` | `ThreadPoolIO` (real), `SimIO` + `SimClock` (deterministic, seeded, effect-at-issue to model crashes) |
| `capabilities.py` | capability table: grant, attenuate (never amplify), expiry, one-shot, cascading revoke, persistence; tool-cap mapping |
| `overlay.py` | `OverlayWorkspace`, a drop-in `WorkspaceGuard`: safety checks on lower, capability check per path, copy-up on write, merged listings, change set and diff |
| `journal.py` | two-phase commit (PREPARING → COMMITTING → COMMITTED), conflict detection, roll-forward recovery, undo with conflict check |
| `planner.py` | `StaticPlanner`, `LLMPlanner`, plan JSON schema, deterministic `validate_plan` |
| `store.py` | SQLite persistence for processes, capabilities, overlay bases, transactions, journal |
| `kernel.py` | the loop (inbox → interrupts → deadlines → one phase → checkpoint), all phases, fork/reap, signals, approvals as capability grants, outbox, review/commit/discard/undo, boot recovery |

**Changes to existing code**

- `persistence/migrations.py`: migration 2 adds `kernel_processes`, `kernel_capabilities`, `kernel_overlay_files`, `kernel_txns`, `kernel_journal`.
- `policy/engine.py`: new `deferred_capabilities` argument and rule `P016_deferred_to_commit`. Staged `workspace.write` steps skip per-step risk approval. It never relaxes a deny, a tool's `approval="always"`, the `require_approval_for_writes` setting, a plan's explicit request, or a model-escalated DESTRUCTIVE risk.
- `tools/workspace.py`: `edit_workspace_file` changed from `approval="always"` to `approval="policy"`. Outside the kernel its SENSITIVE_WRITE risk still forces approval (tested). Inside the kernel the commit review covers it.

**Behaviour verified by tests**

- The real workspace is byte-identical before commit, in every test and every simulation seed.
- Commit then undo restores exact bytes and removes folders the commit created.
- Commit refuses (writes nothing) if a file changed on disk meanwhile; undo refuses if a file was edited after commit.
- Crash at any tick (1–3 crashes per run, half of side effects landing before the kernel hears about them) converges to the crash-free result: 60 seeds by default, 400 checked.
- Crash at each journal point (prepared, commit point, mid-apply) recovers correctly.
- Read-only steps fork into child processes holding only attenuated read capabilities; parent waits, reaps, merges results.
- STOP gives a process zero slices; CONT resumes; TERM/KILL discard staged work, revoke capabilities, and drop late writes from in-flight tools.
- App launch has no default capability: approval grants a one-shot or N-minute capability bound to the action hash. A launch that opens a file the task changed is deferred to the outbox and runs after commit against the real file.
- A failure after partial work offers the partial result for review instead of losing it silently.
- Replan on invalid plan (for example an invented `run_shell` tool), clarification round trip, execution and wall-clock budgets, capability scope denial, review surviving a restart, and the threaded loop with real I/O.

**Environment notes**

- The device shell used for this session could not install packages (no pydantic), so the code was built and tested in a separate workspace with Python 3.13 and 3.11, then written back to this folder.
- Not yet run on Windows. Watch for: long paths under `%LOCALAPPDATA%\Agentic OS\overlays\...`, files locked by editors during commit (surfaces as `txn.incomplete`, and committing again resumes), and junctions inside the workspace (still refused by the path guard).

**Known limits (honest list)**

- Userspace only: a buggy first-party tool could still call `open()` on any path. Third-party (MCP) tools must run out of process.
- Kernel commands are only callable from Python so far; there is no HTTP API yet.
- Plans pass data between steps only through static arguments.
- One standing grant per approval; no UI yet to list or revoke grants.

### Session 3 — 2026-11-19: the full app

**Asked for:** a Tauri desktop app with a React frontend, a FastAPI backend, local SQL storage, and RAG with
embeddings.

**Built**

- **Kernel fix: data flow.** `$ref` arguments (`{"$ref": "list.output"}`) are now resolved by the kernel from
  stored step outputs (`kernel_step_outputs`, migration 3). They count as dependencies, are validated at plan
  time, and are resolved before policy so approvals bind the real arguments. Before this, the report tool
  could not receive its inputs.
- **Planners:** `RulesPlanner` (offline, deterministic) for reports, search, find, read, list, create, append,
  replace and knowledge questions; `HybridPlanner` = rules first, then the default AI provider.
- **RAG** (`agentic_os/rag/`): line-aware chunker; embedders `local` (feature hashing, offline default),
  `ollama`, `openai`, `gemini` (HTTP, reusing provider keys from the OS credential store); SQLite storage of
  float32 vectors + FTS5; hybrid search with Reciprocal Rank Fusion; incremental indexing that skips sensitive,
  binary and large files and removes deleted ones; notes; grounded Ask with citations.
- **`search_knowledge` tool:** lets plans use the knowledge base; hits are re-checked through the process's
  capabilities.
- **FastAPI app** (`agentic_os/api/`): auth (pairing cookie or bearer), status, settings (workspace switch is
  blocked while a task awaits review), tasks (spawn, list, detail, diff, events, outputs, approve, reject,
  answer, commit, discard, undo, signal), SSE (`/api/events`, global or per task, with replay), knowledge,
  providers, tools, apps; serves the built UI in browser mode. `python -m agentic_os` prints
  `AGENTIC_OS_READY`, and with `--token-stdin` exits when the desktop shell's stdin closes.
- **React UI** (`frontend/`): pairing, task composer and list, plan with step results and checks, live trace,
  approval cards (allow once / 10 min / 1 h), clarification, review with a line-numbered diff, Commit / Discard
  / Undo, Pause / Resume / Cancel, knowledge index/search/ask/notes, settings for workspace, planner,
  embeddings, providers and allowlisted apps.
- **Tauri 2 shell** (`frontend/src-tauri/`): picks a free port, generates a 256-bit token, starts the backend
  (dev: `backend/.venv` Python; release: PyInstaller sidecar), writes the token to its stdin, waits for READY,
  exposes `backend_info` and a native folder picker, and kills the backend on exit.
- Commit and undo now re-index the knowledge base automatically.

**Verified:** the real server with pairing and live SSE; the full UI flow in headless Chrome; the Tauri app
compiled and running on a virtual display (task → review → commit from the window); the packaged sidecar;
and backend cleanup on both a window close and a hard kill of the app.

**Bugs found and fixed during verification**

- Kernel events were not on the global SSE allow-list, so the task list didn't live-update.
- Diff line numbers wrapped at two digits; card buttons stretched full width.
- The PyInstaller binary crashed at start in the `pkg_resources` runtime hook; it is excluded now.
- The backend outlived a force-killed shell: it now watches stdin and shuts down *before* logging, because
  stderr goes to the same dead pipe.

**Not done / to check on Windows**

- Nothing has been built or run on Windows yet. In this session the Windows machine's shell could not start
  (it needs up to 17 GB of free disk), so files were copied across without being run there.
- Real provider keys: LLM planning, generated answers and remote embedders still need a smoke test.
- The installer step of `build.bat` (NSIS/MSI) has not been run.

### Session 3b — 2026-11-19: one-command start

- `start.bat`: checks prerequisites, creates the venv, installs Python and npm packages (skips what is
  already done), runs backend and frontend tests, builds the UI, starts the server and opens the browser.
  Flags: `--skip-tests`, `--desktop`.
- The backend opens the browser at `/#pair=<one-time code>`. The UI pairs automatically and strips the
  code from the address bar; URL fragments are never sent to a server. A used or expired code falls back
  to the manual pairing screen.
- If port 8765 is busy, the backend picks a free port and opens that one.
- Verified on Linux: auto-pair, one-time reuse refused, port fallback. The `.bat` itself has not been run
  on Windows yet.

### Session 4 — 2026-11-19: truly agentic, and the Eagle Eye UI

**Kernel**
- Home mount (`kernel/mounts.py`): `~/` paths go to `<data_dir>/home`; one transaction spans the workspace
  and Home; the journal stores a roots map so recovery and undo work across both. Crash tests at every
  commit stage.
- Capability kinds `mail` (read at spawn; send = one-shot grant) and `web` (domain-scoped read grant;
  act = one-shot grant). `GRANTABLE_TOOL_CAPS`, `cap_resource()`.
- Timer interrupts: due schedules spawn processes from the kernel loop. `auto_commit` commits staged files
  without review (still undoable; irreversible steps still ask). `origin` on every process.
- `on_exit` hook (closes the task's browser context). Explicit plans on spawn (still validated + policed).
- Migration 4: `schedules`, `listeners`, `trigger_runs`.

**Services and tools**
- Email: local demo mailbox + IMAP/SMTP (TLS, password in keyring); list/read/search/draft/send; idempotent
  send via deterministic Message-ID + reconcile.
- Calendar: iCalendar read/write, overlaps, Jitsi links, invitation files; meeting = calendar change
  (reviewed) → commit → email with the invite (approved).
- Notes: `create_note`, `append_note`; direct editing in the UI with optimistic concurrency.
- Browser: `web_fetch` (SSRF guard), Playwright on a dedicated thread with Edge/Chrome, snapshots with
  element refs, click/type with approvals bound to the element.
- Listeners: file (polling snapshot), email (seen UIDs), webhook (`/hooks/<id>`, hashed secret); cooldown
  batching; self-trigger suppression; prompt-injection fencing of event content.
- Offline rules for email, calendar, meetings, notes and web requests.

**UI**: rebuilt on the Eagle Eye design system (tokens and component CSS from the zip; Lucide icons).
Rail + frosted top bar with search, theme, notifications and a status meter. Home (greeting, composer,
KPI row, My work with segmented buckets and grouped list, agenda, unread mail, how-it-works steps), Tasks
(resizable split panes, plain-language summary, decision block with email / web / diff previews), Inbox,
Calendar (week grid, add event, schedule meeting), Notes (editor), Automations (schedules, listeners,
history, webhook secret dialog), Browser, Knowledge, Settings (General, Email, AI, Knowledge, Apps). Light
and dark.

**Verified**: 214 backend tests (15 new kernel tests incl. a real Chrome click flow, 8 new API tests);
frontend typecheck, tests and build; the whole app in headless Chrome: seeded tasks, a meeting scheduled
from the dialog → review → commit → email approval → invitation in Sent with the .ics attached; a calendar
edit made by the user while a task had staged one was reported as a conflict instead of being overwritten.

**Not done / to check on Windows**
- Not run on Windows yet (Edge channel, keyring for the mail password, `start.bat` with Playwright).
- A real IMAP/SMTP account has not been tested; only the local mailbox.
- If Edge and Chrome are both missing, run `python -m playwright install chromium` once.

### Session 5 — 2026-11-19: connections, conversation, voice, knowledge graph, dashboard

- Fixed on Windows: one-time schedules near 1970 (OSError), `start.bat` now always runs `npm install`.
- Provider test: shows the provider's error message; the test request now leaves room for thinking models.
- Connections screen: Google OAuth (PKCE, loopback) for Gmail + Calendar + Meet, Gmail app-password preset,
  Notion integration token, default Notion page. New capability kinds `google` and `notion`.
- One-click apps: installed programs are discovered and added with a click.
- Conversation-first home with result cards; voice input (browser speech or provider transcription),
  spoken replies, hands-free mode; `respond_to_user` tool.
- Knowledge graph (offline + AI deep extraction) with an interactive graph screen; `build_knowledge_graph`,
  `kg_query` tools.
- Dashboard with activity chart, tool usage, recent tasks, agenda, mail, knowledge and connections.
- Migration 5. 225 backend tests pass; frontend typecheck/tests/build pass; screens checked in headless Chrome.
- Not verified here: a real Google or Notion account (tested against fake HTTP servers), microphone input.

### Session 6 — 2026-11-19: answers that fit the question, and the OS made visible

- Fixed Windows app discovery (PowerShell returned paths as objects) and quiet Ctrl+C shutdown.
- Chat: focus/briefing questions, small talk to the model, local-time answers, only used sources shown, markdown.
- Email: topic search with alternatives ("hackathon, voyage hack"), follow-up narrowing, HTML snippets as text.
- Providers: retries + fallback models for 503/504; planner degrades to offline rules.
- Kernel monitor screen with six one-click OS demos; `docs/DEMO.md` is the demo script.
- Verified crash recovery by `kill -9` of the server mid-step: the process resumed and completed.

## How to continue

1. `start.bat` (installs, tests, opens the app). Record the Windows results in `docs/TESTING.md`.
2. `run.bat` (browser) to try it; then install Rust and run `dev-desktop.bat` for the desktop app.
3. Add a provider key in Settings and smoke-test free-form planning and Knowledge → Ask.
4. `build.bat` for the installer (needs about 5 GB free for the Rust build cache).
5. Settings → Email: connect a real account (an app password for Gmail/Outlook) and test sending to yourself.
6. Automations: create a schedule ("Check my email", weekdays 9:00) and a file listener on `~/Notes`.
