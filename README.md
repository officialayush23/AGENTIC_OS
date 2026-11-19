# Agentic OS

A desktop agent built like an operating system. You describe a task; a userspace **kernel** plans it, runs it
as a **process** with only the **capabilities** it needs, stages every file change in a **copy-on-write
overlay**, and shows you **one diff** to commit or discard. Committed changes can be **undone** from the
journal. A local **RAG knowledge base** (SQLite + embeddings + full-text search) lets both you and the agent
search your files and notes by meaning.

It also works outside your project folder, through the same kernel:

- **Email** (IMAP/SMTP, or a local demo mailbox): read, search and draft freely; every send asks you first.
- **Calendar and meetings**: a local `.ics` calendar, video-call links, invitations by email after approval.
- **Notes** in a Home folder (`~/Notes`) that shares one transaction with your workspace.
- **Browser control** of the installed Edge or Chrome: reading a site needs a per-domain grant, every click
  or keystroke is shown to you first.
- **Schedules** (cron, intervals, one-off) as timer interrupts, and **listeners** for file changes, new
  email and local webhooks. Their changes still wait for your review unless you allow automatic commits.

See `docs/AGENTIC.md`.

```
┌──────────────────────────── Tauri 2 desktop shell (Rust) ─────────────────────────────┐
│  React + TypeScript UI  ── HTTP + SSE (Bearer token) ──►  FastAPI backend (Python)     │
│                                                           │                            │
│  starts the backend sidecar, hands it a random token      ├─ Kernel: processes, run    │
│  over stdin, kills it on exit                             │  queue, signals, fork/wait, │
│                                                           │  capabilities, overlay FS,  │
│                                                           │  journal (commit/undo)      │
│                                                           ├─ Tools (workspace, apps,    │
│                                                           │  system, knowledge, notes,  │
│                                                           │  calendar, email, browser)  │
│                                                           ├─ Automation: schedules      │
│                                                           │  (timers), listeners        │
│                                                           ├─ RAG: chunker, embedders,   │
│                                                           │  vectors + FTS5, hybrid RRF │
│                                                           └─ SQLite (one local file)    │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

## Quick start (Windows)

Prerequisites: Python 3.10+, Node.js 20+. For the desktop app also Rust (`rustup`, with the MSVC build tools)
and WebView2 (already on Windows 10/11).

**One command does everything:** install, run every test, build the UI, start the server, and open the
app in your browser, already paired:

```bat
start.bat                  :: double-click it, or run it from a terminal
start.bat --skip-tests     :: faster on later runs
start.bat --desktop        :: the Tauri desktop app instead (needs Rust)
```

Individual steps, if you prefer:

```bat
setup.bat          :: creates backend\.venv, installs Python + npm packages
run.bat            :: browser mode: opens http://127.0.0.1:8765, enter the pairing code from the console
dev-desktop.bat    :: desktop app in development (Tauri window, hot reload)
test.bat           :: backend + frontend tests
build.bat          :: Windows installer (.exe/.msi) with the backend bundled as a sidecar
```

No API key is needed to start: a deterministic rules planner handles common requests ("write a project
report", "search for "TODO"", "read docs/notes.md", "create notes/today.md with "..."") and the default
embedder works offline. Add an OpenAI, Anthropic, Gemini or OpenAI-compatible provider in **Settings** for
free-form tasks and written answers in **Knowledge → Ask**.

## What happens when you run a task

1. The kernel **spawns a process** with default capabilities: read and (staged) write on the workspace, no app
   launching.
2. The **planner** (rules first, then your AI provider) produces a plan of typed tool calls. Steps can pass
   data with `{"$ref": "step.output.field"}`. Plans are validated deterministically.
3. Independent read-only steps **fork** into child processes that hold only read capabilities.
4. Every step passes the **capability gate** and the **policy engine**; sensitive files (`.env`, keys,
   credentials) are always denied.
5. Writes land in the task's **overlay**. Your real files are untouched.
6. You review **one diff**, then **Commit** (two-phase, crash-safe) or **Discard**. **Undo** reverses a commit
   later, refusing if you have edited those files since.
7. Launching an app is irreversible, so it needs an approval that grants a one-shot or time-limited
   capability. If it opens a file the task changed, it waits until after the commit.

Pause / Resume / Cancel map to the STOP / CONT / TERM signals. Everything is in one SQLite file under
`%LOCALAPPDATA%\AgenticOS`; API keys go to the Windows Credential Manager, never the database.

## Repository layout

| Path | Contents |
|---|---|
| `backend/agentic_os/kernel/` | processes, scheduler, I/O drivers, capabilities, overlay, journal, planners |
| `backend/agentic_os/rag/` | chunker, embedders (local / Ollama / OpenAI / Gemini), knowledge base |
| `backend/agentic_os/api/` | FastAPI app, app state, SSE, agentic routes |
| `backend/agentic_os/services/` | mail (IMAP/SMTP + local mailbox), calendar (iCalendar), browser (Playwright) |
| `backend/agentic_os/automation/` | cron parser, schedules and listeners store, listener service |
| `backend/agentic_os/tools/` | typed tools, path guard, executor |
| `backend/agentic_os/policy/`, `security/`, `providers/`, `persistence/` | policy engine, auth + secrets, LLM providers, SQLite |
| `backend/tests/` | kernel (incl. deterministic crash simulation), RAG, API |
| `frontend/src/` | React UI (Eagle Eye design system): Home, Tasks, Inbox, Calendar, Notes, Automations, Browser, Knowledge, Settings |
| `frontend/src-tauri/` | Tauri 2 shell (Rust) |
| `docs/` | `KERNEL.md`, `RAG.md`, `AGENTIC.md`, `BUILD_PLAN.md`, `PROGRESS.md`, `TESTING.md` |

## Security model, briefly

- Loopback only (`127.0.0.1`). Every API call needs the `X-Agentic-OS: 1` header, a valid Host, an allowed
  Origin, and either a paired session cookie (browser) or the shell's bearer token (desktop).
- The model never chooses capabilities, policy outcomes or commits. It can only propose a plan.
- Userspace limits: tools are first-party code reviewed to go through the kernel's resolver; there is no
  OS-level sandbox in v0.1. See `docs/KERNEL.md` §9.
