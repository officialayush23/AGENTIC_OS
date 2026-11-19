# Testing

## Running

```bat
test.bat
```

or by hand:

```bat
cd backend
.venv\Scripts\python.exe -m pytest tests -q --timeout 120
cd ..\frontend
npm run typecheck && npm test
```

Long simulation run (more random crash schedules):

```bat
set AOS_SIM_SEEDS=400
python -m pytest tests/kernel/test_simulation.py -q
```

## Suites

| File | What it covers |
|---|---|
| `tests/kernel/test_capabilities.py` | scope semantics, grant/check, attenuation never amplifies, cascading revoke, expiry, one-shot, persistence |
| `tests/kernel/test_overlay_journal.py` | staged writes, read-your-writes, merged listings, sensitive files never copied, capability scope at resolve, commit/undo exact bytes, commit and undo conflicts, crash at every journal point |
| `tests/kernel/test_kernel.py` | end-to-end transaction, fork/reap, child capabilities, discard, STOP/CONT/TERM/KILL, approval-as-grant, action-hash binding, rejection, outbox, sensitive denial, replan, partial-failure review, clarification, budgets, capability scope, restart safety, threaded loop, policy P016 |
| `tests/kernel/test_simulation.py` | deterministic simulation: random crashes (with side effects landing before completion), crash during commit, two concurrent tasks isolated |

| `tests/kernel/test_rules_planner.py` | the offline planner produces valid plans for every supported phrasing; report end to end |
| `tests/rag/test_rag.py` | local embedder, chunk line ranges, incremental indexing and cleanup, sensitive/binary skipping, notes, filters, stale embedders, embedder config errors |
| `tests/api/test_api.py` | host/origin/header/token checks, CORS preflight for the desktop webview, task lifecycle over HTTP, clarification, errors, knowledge endpoints, settings and workspace switching, providers, SSE replay |
| `frontend/src/components/diff.test.ts` | unified-diff parsing and SSE frame parsing |

`tests/kernel/harness.py` builds a kernel over a temporary workspace with a fake OS adapter (records app launches instead of starting programs).

## Results

| Date | Environment | Result |
|---|---|---|
| 2026-11-15 | Linux, Python 3.13.18, pydantic 2.13.5, pytest 9.1.1 | 154 passed (7.2 s) |
| 2026-11-15 | Linux, Python 3.11 | 154 passed (8.8 s) |
| 2026-11-15 | Linux, Python 3.13, `AOS_SIM_SEEDS=400` crash-anywhere suite | 400 passed (21.9 s) |
| 2026-11-19 | Linux, Python 3.13 | 184 passed (8.2 s): kernel + RAG + API |
| 2026-11-19 | Linux, Python 3.11 | 184 passed (10.6 s) |
| 2026-11-19 | `AOS_SIM_SEEDS=200` simulation suite | 250 passed |
| 2026-11-19 | Frontend: `tsc -b`, vitest, `vite build` | clean; 3 passed; 175 kB JS (56 kB gzip) |
| 2026-11-19 | Manual end to end: real server + headless Chrome (pair → task → review → commit → undo → knowledge → settings) | no console errors |
| 2026-11-19 | Tauri shell: `cargo build` on Linux (WebKitGTK), app run on a virtual display | window opens; shell starts backend with token; task, review and commit work from the window; backend exits on window close and on a hard kill of the app |
| 2026-11-19 | PyInstaller sidecar (Linux) | single 55 MB binary; starts with stdin token, runs a task, rejects a wrong token |
| — | Windows (target machine) | not yet run |
