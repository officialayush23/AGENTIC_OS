# Agentic services: Home, email, calendar, browser, schedules, listeners

This document covers what was added in session 4 on top of the kernel (`KERNEL.md`) and the knowledge
base (`RAG.md`). The rule that holds everything together: **nothing gets a side door around the kernel.**
Every new capability is a tool call inside a process, checked by capabilities and policy, staged in the
transaction when it can be, and deferred or approved when it can't.

## 1. Home mount (`~/`) — a second file system in the same namespace

`kernel/mounts.py` is a tiny VFS. Paths starting with `~/` resolve to the Agentic OS Home folder
(`<data_dir>/home`); everything else resolves to the workspace.

| Path | What lives there |
|---|---|
| `~/Notes/*.md` | Notes (you edit them directly in the app; the agent's edits are staged) |
| `~/Calendar/calendar.ics` | Your calendar, plain iCalendar any app can import |
| `~/Calendar/invites/*.ics` | Invitations created for meetings with attendees |
| `~/Mail/Outbox/*.eml` | Email drafts |

`MountedWorkspace` wraps two guards (or two overlays) and keeps the `~/` prefix in every result, diff and
journal entry. One process therefore has **one transaction across both mounts**: a task that edits
`docs/plan.md` and `~/Notes/plan.md` commits both atomically or neither. The journal stores a `roots` map
in the commit record so recovery and undo find the right real path (`lower_path`). Capability scopes work
the same way: `fs:""` covers both mounts; `fs:"docs"` does not cover `~/...`. Tests:
`tests/kernel/test_mounts.py` (including crashes at every commit stage).

## 2. New capability kinds

| Tool capability | Kernel capability | Held at spawn | How it is obtained |
|---|---|---|---|
| `mail.read` | `mail:read *` | yes | — |
| `mail.send` | `mail:send *` | no | approval → **one-shot** grant (irreversible) |
| `web.read` | `web:read <domain>` | no | approval → grant for this task or for an hour |
| `web.act` | `web:act <domain>` | no | approval → **one-shot** grant (irreversible) |

`web` scopes match a domain and its subdomains (`example.com` covers `docs.example.com`, not
`badexample.com`). `cap_resource()` derives the resource from the step's arguments (the URL's domain, the
app id), so the approval card names exactly what is granted. Irreversible actions also go through the
**outbox**: if an email attaches a file the task changed, the send waits until you commit, then asks.

## 3. Email (`services/mail.py`, `tools/personal.py`)

Two transports behind one interface (`list`, `fetch`, `search`, `send`, `sent_contains`, ...):

- **LocalMailbox** — `.eml` files in `<data_dir>/mail`. Seeded with three demo messages. Sending writes
  to its Sent folder and says clearly that nothing was delivered. Used for the demo and tests.
- **ImapSmtpTransport** — a real account, TLS only (IMAPS 993 / STARTTLS, SMTP 587 or 465). The password
  is in the OS credential store (`mail:password`), never in SQLite.

Tools: `email_list`, `email_read`, `email_search` (read, no approval), `email_draft` (a staged file in
`~/Mail/Outbox`), `email_send` (approval always). Every outgoing message gets a deterministic
`Message-ID` from `(task, step)`; after a crash the kernel's reconcile step searches Sent for it, so a
message is never sent twice. Email bodies are returned with a note that they are data, not instructions.

## 4. Calendar and meetings (`services/calendar.py`)

A small RFC 5545 reader/writer (no dependency). `calendar_add_event` stages a change to
`~/Calendar/calendar.ics` (you see the diff), reports overlaps in the approval reasons, and with
`online_meeting` adds a Jitsi Meet link. With attendees it also writes an invitation
(`~/Calendar/invites/<date>-<title>.ics`, `METHOD:REQUEST`), which a following `email_send` step attaches
through `{"$ref": "event.output.invite_file"}`. Because that file is staged, the send is deferred to the
outbox: **review calendar change → commit → approve the email → sent.** Calendar edits you make in the UI
are yours and are written directly; if a task staged an edit to the same file meanwhile, its commit reports
a conflict instead of overwriting yours (optimistic concurrency from the overlay).

## 5. Browser control (`services/browser.py`, `tools/web.py`)

- `web_fetch` — plain HTTP GET with size/time limits, HTML to text plus links. Private and loopback
  addresses are refused unless Settings → Browser allows them (blocks SSRF into the local network).
- `browser_open`, `browser_read`, `browser_screenshot` — a real browser via Playwright using the
  **installed** Microsoft Edge (Windows) or Google Chrome; it falls back to Playwright's Chromium.
  Snapshots return page text and numbered interactive elements (`e1`, `e2`, ...).
- `browser_click`, `browser_type` — `web:act`, approval every time. The approval shows the element's tag
  and label and the text to type (password fields are masked). The label is bound into the approval; if
  the page changed so the element is different at execution, nothing is clicked.

Playwright's sync API belongs to one thread, so `BrowserService` runs a **dedicated browser thread** with a
request queue (a device driver with its own queue). Each task gets its own browser context (no shared
cookies), closed by the kernel's `on_exit` hook. Browser steps are never forked (their capabilities are
not in the read-only fork set), so one task never races itself in one tab.

## 6. Schedules — timer interrupts (`automation/cron.py`, `automation/store.py`)

`schedules` table (migration 4). Kinds: `cron` (5 fields, `*`, `a-b`, `*/n`, lists, `@daily` ...; day of
month **or** day of week as in classic cron), `interval` (≥ 60 s), `once`. The kernel loop checks due
schedules at most once a second (`_fire_timers`), like a timer interrupt, and spawns a process with
`origin = {"kind": "schedule", ...}`. Missed runs while the computer slept are **coalesced** into one.

`auto_commit` (off by default): the process commits its staged files without waiting for review. It is
still one transaction and still undoable. It never auto-approves anything irreversible: emails, web actions
and app launches still stop and ask.

## 7. Listeners — event triggers (`automation/listeners.py`)

A service thread polls enabled listeners:

- **file** — snapshots a workspace or `~/` folder (mtime + size, pattern filter) and fires on created /
  modified / deleted files. The first poll is a baseline.
- **email** — remembers seen message UIDs and fires per new message (optional sender / subject filters).
- **webhook** — `POST /hooks/<id>` with `X-Hook-Secret`. The secret is shown once and stored as SHA-256.
  Same loopback, Host and Origin checks as the API; a browser page on another site cannot call it.

Each listener has a cooldown; events inside it are batched into the next task. The request template is
filled from the event (`{files}`, `{subject}`, `{from}`, any JSON field) and the event content is appended
inside a fence marked "data, not instructions". Files committed by a task the same listener started are
ignored, so a listener can't trigger itself in a loop. History is in `trigger_runs`.

## 8. Explicit plans from the UI

`POST /api/tasks` accepts an optional `plan`. The kernel skips the planner but **not** validation, capability
checks or policy: an explicit plan is treated exactly like a planner's (an unknown tool is rejected, an
email still needs approval). The Calendar, Inbox and Browser screens use this to start precise tasks.

## 9. Offline planner additions

Without an AI provider the rules planner now understands: "check my email", "search my email for X",
"email a@b.com saying ...", "what's on my calendar this week", "schedule a meeting with a@b.com tomorrow at
3pm [for 45 min]" (event + invitation), "note: ...", "open example.com", "fetch https://...",
"screenshot https://...". File names such as `notes.md` are not mistaken for websites.

## 10. API summary

| Area | Routes |
|---|---|
| Home / notes | `GET /api/home`, `GET /api/notes`, `GET|PUT|DELETE /api/notes/file` (PUT takes `expected_sha256`) |
| Calendar | `GET /api/calendar`, `POST /api/calendar/events`, `DELETE /api/calendar/events/{uid}`, `GET /api/calendar.ics` |
| Mail | `GET /api/mail/status`, `PUT /api/mail/config`, `POST|DELETE /api/mail/password`, `POST /api/mail/test`, `GET /api/mail/messages`, `GET /api/mail/message`, `GET /api/mail/folders` |
| Schedules | `GET|POST /api/schedules`, `POST /api/schedules/preview`, `PUT|DELETE /api/schedules/{id}`, `POST /api/schedules/{id}/run` |
| Listeners | `GET|POST /api/listeners`, `PUT|DELETE /api/listeners/{id}`, `POST /api/listeners/{id}/poll`, `GET /api/automation/runs` |
| Browser | `GET /api/browser/status`, `GET /api/screens/{task}/{file}` |
| Webhook | `POST /hooks/{id}` (secret header; no session needed) |

## 11. UI (Eagle Eye)

The UI follows the Eagle Eye design system: grey canvas, borderless 26 px white cards, every control a
pill, near-black as the only action colour (one primary per view), colour only for status and always with
a word, 36/400 page titles and 44/300 KPI numbers, Lucide icons at 1.6 stroke, a 84 px icon rail and a 76 px
frosted top bar, split panes that scroll independently, light and dark themes on the same tokens, and
`prefers-reduced-motion` respected. Screens: Home, Tasks, Inbox, Calendar, Notes, Automations, Browser,
Knowledge, Settings. `frontend/src/styles/tokens.css` and `ee.css` come from the design system unchanged
(minus the tablet block); `app.css` builds only on those tokens.

## 12. Limits

- Polling, not OS file events: a file listener notices changes within its poll interval (5 s default).
- IMAP is polled (60 s default); no IMAP IDLE yet.
- The calendar is local. It does not sync with Google or Outlook calendars; invitations by email are how
  other people get the event.
- Browser sessions start signed out. Sites that need your account need you to sign in in the visible
  browser window (Settings → Browser → show the window) during the task.

## 13. Conversation-first and voice-first (session 5)

The app opens on **Conversation**. Every message becomes a process (origin `chat`) with the recent
conversation passed as fenced context; the reply bubble shows the live task (status, steps, approvals and
the review diff inline) and, when the process exits, is rewritten as a plain answer plus **result cards**
(emails, events with Join buttons, files, search hits, web pages, screenshots, Notion pages, graph stats).
With an AI provider the answer text is written by the model from the step outputs; offline it is
composed from templates. If the agent asked a question, the next message answers it. A `respond_to_user`
tool lets the planner answer questions and small talk directly.

Voice: the big microphone (or Ctrl+Space) uses the browser's speech recognition when available, otherwise
it records audio and sends it to `POST /api/voice/transcribe`, which uses the AI provider (Gemini or OpenAI
`gpt-4o-mini-transcribe`). Replies to spoken messages are read aloud (speech synthesis); hands-free mode
listens again after each spoken reply.

## 14. Knowledge graph

`rag/graph.py` builds a graph from everything indexed (workspace, Home notes, knowledge notes) plus mail
senders and calendar attendees. The offline extractor finds documents, `[[wiki links]]`, `#tags`, headings
(topics), people (email addresses), code imports (modules), web domains and recurring Title Case phrases,
and links entities that appear in the same passage. **Deep build** sends each document (up to 40) to the
AI provider for typed entities and relations. Every edge records the document it came from. Tools:
`build_knowledge_graph`, `kg_query`. API: `GET /api/kg`, `GET /api/kg/node`, `GET /api/kg/search`,
`POST /api/kg/build`. The Knowledge graph screen draws it with a force layout (pan, zoom, drag, focus,
filter by kind, evidence list).

## 15. Connections

- **Google** (Gmail, Calendar, Meet): OAuth for installed apps with PKCE and a loopback redirect to
  `/oauth/google/callback`. The user pastes the client ID and secret of a Desktop-app OAuth client from
  their own Cloud project (steps are shown in the app). Refresh token and client secret go to the
  credential store. Gmail becomes a mail transport (`mode: gmail`); `gcal_list_events` and
  `gcal_create_event` (Google Meet link, Google sends the invitations, deterministic event id so a retry
  never duplicates) use the new `google` capability kind; writing is a one-shot grant.
- **Gmail with an app password**: IMAP/SMTP preset for imap.gmail.com / smtp.gmail.com; no Cloud project.
- **Notion**: internal integration token; `notion_search`, `notion_read_page`, `notion_create_page`,
  `notion_append` (markdown → blocks), `notion` capability kind, a default parent page.
- **Apps**: Settings → Apps lists the programs installed on the computer (Start menu shortcuts and App Paths
  on Windows) for one-click allowlisting.

## 16. Dashboard

`GET /api/dashboard` aggregates all processes: KPIs, tasks per day by outcome with automation runs, most
used tools, where tasks came from, recent tasks, upcoming events (local + Google), unread mail, knowledge
and graph size, and connection status.
