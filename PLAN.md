# Personal Desktop Agent — Architecture & Requirements Plan

## 1. Overview

A personal automation agent that watches Gmail + Outlook, local files/folders, Notion, Calendar, and Zoom/Meet, and gives you: a single dashboard of work status/leads/tasks, an email summarizer, and a reply bot that drafts replies and auto-sends only for rules you whitelist.

It has two parts: a desktop app on this machine, and a small always-on cloud watcher so alerts still reach you when the PC is off.

**Non-goals for v1:** multi-user support, a mobile app, a fully-autonomous "send everything" mode, full remote control of the dashboard from another device (alerts yes, full remote UI no).

## 2. Requirements Summary

**Functional**
- Monitor Gmail and Outlook/M365 inboxes; summarize new mail; flag what needs action
- Draft replies; auto-send only for user-defined whitelist rules; everything else queues for approval
- Track leads/tasks against Notion as source of truth, plus agent-suggested candidates pulled from email/files
- Watch local files/folders the user points it at
- Surface upcoming Calendar events and Meet/Zoom meeting details
- Single dashboard combining all of the above
- Email the user an alert when something needs attention, even if the desktop app/PC is off

**Non-functional**
- Privacy: sensitive reasoning (drafting, summarizing content) happens locally by default
- Reliability: alerting must not silently die (webhook/watch renewal, cron health-check)
- Security: OAuth tokens/API keys in OS keychain locally and Supabase Vault/env secrets in the cloud; minimal scopes
- Single user, Windows desktop as primary client; no multi-tenant design needed

## 3. Architecture: Hybrid, Two-Tier

**Why hybrid:** a pure local app can't satisfy "runs/alerts when the PC is off" — that's a hard requirement. A pure cloud design would need a paid hosted LLM for all reasoning and still can't reach local files directly, so it doesn't actually remove the local component either. Hybrid keeps the free/private local model for the heavy lifting, with only a thin, cheap always-on piece in the cloud.

**Local Desktop Tier** (runs while the app is open)
- Electron app, React + TypeScript UI
- Local FastAPI backend (sidecar process)
- Ollama running Gemma locally — inbox summarization, reply drafting, lead/task extraction, dashboard narrative
- Local SQLite cache, synced with Supabase
- Local filesystem watcher (only this tier touches local files)

**Always-On Cloud Tier** (runs even when the PC is off)
- Supabase: Postgres (shared state, rules, cached leads/tasks) + Edge Functions (webhook receivers, polling jobs) + Supabase Cron/pg_cron (scheduling) + pg_net (outbound HTTP)
- No separate Render service planned for v1 — Supabase's cron + edge functions cover polling Gmail/Graph/Calendar/Zoom, evaluating whitelist rules, and sending alert emails. Render is easy to add later if a long-running Python process ever becomes necessary.
- Sends email alerts (via Resend/SendGrid, or plain SMTP) when a whitelisted rule auto-sends a reply, or when something needs attention

Both tiers read/write the same Supabase Postgres database, so rules and state stay in sync whether you're at your desk or not.

## 4. Tech Stack

| Layer | Choice | Notes |
|---|---|---|
| Frontend | React + TypeScript (JSX/TSX), Vite | matches existing skills |
| Desktop shell | Electron | more mature Python-sidecar packaging pattern than Tauri; revisit Tauri later if resource use matters |
| Local backend | FastAPI + Uvicorn | matches existing FastAPI experience |
| Local LLM | Ollama + Gemma 4 (or Gemma 3 4B if hardware is limited) | offline, private, free per-token |
| Local cache | SQLite | offline-friendly, syncs to Supabase |
| Cloud DB/backend | Supabase (Postgres, Edge Functions, Cron, pg_net) | one platform for the always-on tier |
| Alert delivery | Resend or SendGrid (or SMTP) | triggered from an Edge Function |
| Secrets | OS keychain locally (`keyring`), Supabase Vault/env in cloud | |

## 5. Connectors

- **Gmail** — Gmail API, OAuth2, Pub/Sub push notifications (watch expires every 7 days, needs a daily renewal job)
- **Outlook/M365** — Microsoft Graph API, MSAL OAuth2, webhook subscriptions (also need periodic renewal)
- **Google Calendar** — Calendar API (covers Meet links embedded in events)
- **Zoom** — Server-to-Server OAuth app via Zoom Marketplace
- **Notion** — internal integration token, scoped to specific databases
- **Local files** — Python `watchdog`, scoped to folders you choose

## 6. Decision Log

| Decision | Alternatives considered | Why |
|---|---|---|
| Hybrid local + cloud architecture | Pure local, pure cloud | "Off" requirement rules out pure local; pure cloud doesn't remove the local file access need and adds LLM cost |
| Supabase-only cloud tier | Supabase + Render | Cron/Edge Functions/pg_net already cover the workload; fewer moving parts |
| Electron | Tauri | Faster path given current JS/TS + Python skill mix |
| Gemma via Ollama | Cloud-hosted API for all reasoning | Privacy + no per-token cost for the local tier |
| Whitelist-rule auto-send, draft everything else | Full autosend, full manual | User's explicit choice |
| Notion as source of truth + agent-suggested candidates | Notion only, inference only | User's explicit choice |
| Build all connectors together | Phase one connector first | User's explicit choice — noted as a risk below |

## 7. Key Risks

- **Google OAuth testing mode**: while the Google Cloud OAuth consent screen is in "Testing" status, refresh tokens expire every 7 days — this will quietly break the "always on" Gmail watcher. The consent screen needs to move to "In Production," which for Gmail's sensitive scopes may involve Google's app verification process. Start this early; it can take time.
- **Webhook renewal**: Gmail watch (7 days) and Graph subscriptions both expire and need proactive renewal crons. A missed renewal fails silently — no error, mail just stops arriving.
- **Supabase Cron reliability**: skipped runs aren't retried, and failure visibility is just a log row. Since this tier's whole job is alerting, it needs its own "did today's job actually run" check, or a failure could go unnoticed.
- **Parallel build scope**: building every connector and the reply bot at once means bugs are harder to isolate and nothing is fully usable until most pieces land. Recommend proving Gmail → rule → alert end-to-end first internally, even while scaffolding the rest in parallel.

## 8. Setup Steps

Prerequisites and accounts to get in place before writing code. No code or infrastructure gets created until you say "start."

**Phase 0 — Local tooling**
1. Install Node.js (LTS), Python 3.11+, Git, and Ollama
2. Pull a Gemma model in Ollama and sanity-test it from the command line

**Phase 1 — Cloud accounts**
3. Create a Supabase project (free tier to start); note the project URL and keys
4. Sign up for a transactional email sender for alerts (Resend or SendGrid free tier)

**Phase 2 — Provider API access** (start early — approval/config steps here are the slowest part)
5. Google Cloud: create a project, enable Gmail API + Google Calendar API, configure the OAuth consent screen, add yourself as a test user, create OAuth client credentials, and plan for moving out of "Testing" status
6. Microsoft Entra (Azure AD): register an app for Microsoft Graph API (Outlook mail + calendar), configure delegated permissions, generate a client secret
7. Zoom: create a Server-to-Server OAuth app in the Zoom Marketplace
8. Notion: create an internal integration, generate its token, share the specific leads/tasks databases with it

**Phase 3 — Project setup**
9. Confirm repo layout (this repo vs. a fresh one) before any scaffolding happens

## Sources

- [Google OAuth Refresh Token: Expiration, 7-Day Limit & Lifetime Explained (2026)](https://www.unipile.com/google-oauth-refresh-token/)
- [Configure push notifications in Gmail API — Google for Developers](https://developers.google.com/workspace/gmail/api/guides/push)
- [Supabase Cron](https://supabase.com/blog/supabase-cron)
- [Cron | Supabase Docs](https://supabase.com/docs/guides/cron)
- [Run Gemma with Ollama | Google AI for Developers](https://ai.google.dev/gemma/docs/integrations/ollama)
