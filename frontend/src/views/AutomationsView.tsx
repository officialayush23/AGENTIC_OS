import { useEffect, useState } from "react";
import { CalendarClock, FolderSearch, History, Mail, Play, Plus, RadioTower, RefreshCw, Trash2, Webhook } from "lucide-react";
import { del, get, post, put, rawUrl } from "../api";
import { useLive } from "../App";
import type { Listener, Schedule } from "../types";
import { Banner, Card, Chip, Dialog, Empty, Field, ICON, ICON_SM, PageHeader, Seg, Switch, ago, shortWhen, useAsync, useToast } from "../ui";

type Tab = "schedules" | "listeners" | "history";

export function AutomationsView() {
  const { bump, nav, refresh } = useLive();
  const toast = useToast();
  const [tab, setTab] = useState<Tab>("schedules");
  const [newS, setNewS] = useState(false);
  const [newL, setNewL] = useState(false);
  const [secret, setSecret] = useState<Listener | null>(null);
  const sch = useAsync(() => get<{ schedules: Schedule[] }>("/api/schedules"), [bump]);
  const lst = useAsync(() => get<{ listeners: Listener[] }>("/api/listeners"), [bump]);
  const runs = useAsync(() => get<{ runs: any[] }>("/api/automation/runs?limit=100"), [bump, tab]);
  const schedules = sch.data?.schedules ?? [];
  const listeners = lst.data?.listeners ?? [];
  const names: Record<string, string> = Object.fromEntries([...schedules, ...listeners].map((x) => [x.id, x.name]));

  const call = async (fn: () => Promise<any>, ok?: string) => {
    try { const r = await fn(); if (ok) toast({ title: ok }); sch.reload(); lst.reload(); refresh(); return r; }
    catch (e: any) { toast({ title: "That didn't work", text: e.message, bad: true }); }
  };

  return (
    <>
      <PageHeader section="Automation" dot="--ws-review" title="Schedules and listeners"
                  sub="Start tasks on a timer or when something happens. Their changes still wait for your review unless you allow automatic commits."
                  actions={tab === "listeners"
                    ? <button className="ee-btn primary" onClick={() => setNewL(true)}><Plus {...ICON_SM} /> New listener</button>
                    : <button className="ee-btn primary" onClick={() => setNewS(true)}><Plus {...ICON_SM} /> New schedule</button>} />
      <Seg label="Automation" value={tab} onChange={setTab} items={[
        { id: "schedules", label: "Schedules", count: schedules.length },
        { id: "listeners", label: "Listeners", count: listeners.length },
        { id: "history", label: "History", count: runs.data?.runs.length ?? 0 },
      ]} />

      {tab === "schedules" && (
        <div className="ee-grid stagger">
          {schedules.map((s) => (
            <Card key={s.id} className="ee-proj">
              <div className="head">
                <span className="av"><CalendarClock {...ICON} /></span>
                <div className="grow"><div className="t">{s.name}</div><div className="s">{s.label}{s.auto_commit ? " · commits automatically" : ""}</div></div>
                <Chip tone={s.enabled ? "pass" : "nc"}>{s.enabled ? "On" : "Off"}</Chip>
              </div>
              <p className="ink2" style={{ margin: 0, fontSize: 13 }}>{s.request}</p>
              <div className="ee-stats">
                <div><b>{s.run_count}</b><small>Runs</small></div>
                <div><b style={{ fontSize: 13, lineHeight: "22px" }}>{s.enabled && s.next_run ? shortWhen(s.next_run) : "—"}</b><small>Next</small></div>
                <div><b style={{ fontSize: 13, lineHeight: "22px" }}>{s.last_run ? ago(s.last_run) : "Never"}</b><small>Last</small></div>
              </div>
              <div className="row">
                <Switch checked={s.enabled} onChange={(v) => call(() => put(`/api/schedules/${s.id}`, { enabled: v }), v ? "Schedule on" : "Schedule off")} label="Enabled" />
                <button className="ee-btn outline sm push" onClick={async () => { const r = await call(() => post(`/api/schedules/${s.id}/run`), "Started"); if (r?.pid) nav("tasks", r.pid); }}><Play {...ICON_SM} /> Run now</button>
                <button className="ee-btn ghost round sm" aria-label={`Delete ${s.name}`} onClick={() => { if (confirm(`Delete the schedule “${s.name}”?`)) void call(() => del(`/api/schedules/${s.id}`), "Deleted"); }}><Trash2 {...ICON_SM} /></button>
              </div>
            </Card>
          ))}
          <button className="ee-new" style={{ background: "transparent", cursor: "pointer" }} onClick={() => setNewS(true)}><i><Plus {...ICON} /></i>New schedule</button>
        </div>
      )}

      {tab === "listeners" && (
        <div className="ee-grid stagger">
          {listeners.map((l) => {
            const Icon = l.kind === "file" ? FolderSearch : l.kind === "email" ? Mail : Webhook;
            const what = l.kind === "file" ? `Files in ${l.config.path || "."}${l.config.pattern && l.config.pattern !== "*" ? ` matching ${l.config.pattern}` : ""}`
              : l.kind === "email" ? `New mail in ${l.config.folder || "INBOX"}${l.config.from_contains ? ` from “${l.config.from_contains}”` : ""}${l.config.subject_contains ? ` about “${l.config.subject_contains}”` : ""}`
              : "Local webhook (POST)";
            return (
              <Card key={l.id} className="ee-proj">
                <div className="head">
                  <span className="av"><Icon {...ICON} /></span>
                  <div className="grow"><div className="t">{l.name}</div><div className="s">{what}{l.auto_commit ? " · commits automatically" : ""}</div></div>
                  <Chip tone={l.last_error ? "fail" : l.enabled ? "pass" : "nc"}>{l.last_error ? "Error" : l.enabled ? "Listening" : "Off"}</Chip>
                </div>
                <p className="ink2" style={{ margin: 0, fontSize: 13 }}>{l.request}</p>
                {l.last_error && <Banner tone="bad">{l.last_error}</Banner>}
                {l.kind === "webhook" && <code className="mono ellipsis" title="Send the secret in the X-Hook-Secret header">POST {webhookUrl(l.id)}</code>}
                <div className="ee-stats">
                  <div><b>{l.fire_count}</b><small>Fired</small></div>
                  <div><b style={{ fontSize: 13, lineHeight: "22px" }}>{l.last_fired ? ago(l.last_fired) : "Never"}</b><small>Last</small></div>
                  <div><b>{Math.round(l.cooldown_s)}s</b><small>Cooldown</small></div>
                </div>
                <div className="row">
                  <Switch checked={l.enabled} onChange={(v) => call(() => put(`/api/listeners/${l.id}`, { enabled: v }), v ? "Listening" : "Listener off")} label="Enabled" />
                  {l.kind !== "webhook" && <button className="ee-btn outline sm push" onClick={() => call(() => post(`/api/listeners/${l.id}/poll`), "Checked")}><RefreshCw {...ICON_SM} /> Check now</button>}
                  <button className={`ee-btn ghost round sm ${l.kind === "webhook" ? "push" : ""}`} aria-label={`Delete ${l.name}`} onClick={() => { if (confirm(`Delete the listener “${l.name}”?`)) void call(() => del(`/api/listeners/${l.id}`), "Deleted"); }}><Trash2 {...ICON_SM} /></button>
                </div>
              </Card>
            );
          })}
          <button className="ee-new" style={{ background: "transparent", cursor: "pointer" }} onClick={() => setNewL(true)}><i><Plus {...ICON} /></i>New listener</button>
        </div>
      )}

      {tab === "history" && (
        <Card>
          {(runs.data?.runs ?? []).length === 0 ? <Empty icon={<History {...ICON} />}>Nothing has fired yet.</Empty> : (
            <ul className="plain-list">
              {runs.data!.runs.map((r) => (
                <li key={r.id}>
                  <button className="ee-item" style={{ background: "var(--soft)" }} onClick={() => r.pid && nav("tasks", r.pid)}>
                    <span className="av">{r.source_kind === "schedule" ? <CalendarClock {...ICON_SM} /> : <RadioTower {...ICON_SM} />}</span>
                    <span className="m"><b>{names[r.source_id] ?? r.detail?.name ?? r.source_id}</b>
                      <small>{r.source_kind === "schedule" ? (r.detail?.manual ? "Run by hand" : "On schedule") : `Trigger: ${r.detail?.trigger ?? "event"}${r.detail?.subject ? ` · ${r.detail.subject}` : r.detail?.files ? ` · ${r.detail.files}` : ""}`}</small></span>
                    <span className="r">{ago(r.fired_at)}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>
      )}

      {newS && <NewSchedule onClose={() => setNewS(false)} onDone={() => { setNewS(false); sch.reload(); refresh(); }} />}
      {newL && <NewListener onClose={() => setNewL(false)} onDone={(l) => { setNewL(false); lst.reload(); refresh(); if (l.secret) setSecret(l); }} />}
      {secret && (
        <Dialog title="Webhook ready" onClose={() => setSecret(null)}>
          <Banner tone="note">Copy the secret now. It is stored only as a hash and won't be shown again.</Banner>
          <Field label="Secret"><input className="input mono" readOnly value={secret.secret} onFocus={(e) => e.target.select()} /></Field>
          <Field label="Try it">
            <pre className="code" style={{ background: "var(--soft)" }}>{`curl -X POST ${webhookUrl(secret.id)} \\\n  -H "X-Hook-Secret: ${secret.secret}" \\\n  -H "Content-Type: application/json" \\\n  -d '{"text": "Deploy finished"}'`}</pre>
          </Field>
          <p className="muted" style={{ margin: 0 }}>Only programs on this computer can reach it. Fields in the JSON fill placeholders like {"{text}"} in the request.</p>
        </Dialog>
      )}
    </>
  );
}

function webhookUrl(id: string): string {
  const u = rawUrl(`/hooks/${id}`);
  return u.startsWith("http") ? u : `${window.location.origin}${u}`;
}

const PRESETS = [
  { label: "Weekdays at 9:00", kind: "cron" as const, spec: "0 9 * * 1-5" },
  { label: "Every day at 18:00", kind: "cron" as const, spec: "0 18 * * *" },
  { label: "Every hour", kind: "interval" as const, spec: "3600" },
  { label: "Mondays at 8:30", kind: "cron" as const, spec: "30 8 * * 1" },
];

function NewSchedule({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [name, setName] = useState("");
  const [request, setRequest] = useState("Check my email");
  const [kind, setKind] = useState<"cron" | "interval" | "once">("cron");
  const [spec, setSpec] = useState("0 9 * * 1-5");
  const [mins, setMins] = useState(60);
  const [once, setOnce] = useState(() => new Date(Date.now() + 3600e3).toISOString().slice(0, 16));
  const [auto, setAuto] = useState(false);
  const [preview, setPreview] = useState<{ label?: string; next_runs?: string[]; error?: string }>({});
  const realSpec = kind === "interval" ? String(mins * 60) : kind === "once" ? once : spec;
  useEffect(() => {
    const t = setTimeout(() => {
      post("/api/schedules/preview", { kind, spec: realSpec }).then(setPreview).catch((e) => setPreview({ error: e.message }));
    }, 250);
    return () => clearTimeout(t);
  }, [kind, realSpec]);
  const [err, setErr] = useState<string | null>(null);
  return (
    <Dialog title="New schedule" onClose={onClose}>
      <Field label="What should run" hint="Written like any task. With no AI provider, use one of the offline phrases.">
        <textarea className="textarea" rows={3} value={request} onChange={(e) => setRequest(e.target.value)} />
      </Field>
      <div className="chips">{["Check my email", "What's on my calendar today", "Write a project report", "Note: daily check-in"].map((x) =>
        <button key={x} className="suggest" onClick={() => setRequest(x)}>{x}</button>)}</div>
      <Field label="Name"><input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder={request.slice(0, 40)} /></Field>
      <Seg label="When" value={kind} onChange={setKind} items={[{ id: "cron", label: "Repeat" }, { id: "interval", label: "Every few minutes" }, { id: "once", label: "Once" }]} />
      {kind === "cron" && <>
        <div className="chips">{PRESETS.filter((p) => p.kind === "cron").map((p) => <button key={p.spec} className="suggest" onClick={() => setSpec(p.spec)}>{p.label}</button>)}</div>
        <Field label="Cron expression" hint="minute hour day month weekday, for example 30 9 * * 1-5"><input className="input mono" value={spec} onChange={(e) => setSpec(e.target.value)} /></Field>
      </>}
      {kind === "interval" && <Field label="Every (minutes)" hint="At least 1 minute."><input className="input" type="number" min={1} value={mins} onChange={(e) => setMins(Math.max(1, Number(e.target.value)))} /></Field>}
      {kind === "once" && <Field label="At"><input className="input" type="datetime-local" value={once} onChange={(e) => setOnce(e.target.value)} /></Field>}
      {preview.error ? <Banner tone="bad">{preview.error}</Banner> : preview.label && (
        <Banner tone="info" icon={<CalendarClock {...ICON_SM} />}><p><b>{preview.label}.</b> Next: {(preview.next_runs ?? []).map((r) => new Date(r).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })).join(" · ") || "never"}</p></Banner>
      )}
      <Switch checked={auto} onChange={setAuto} label="Commit changes automatically (still undoable)" />
      {auto && <Banner tone="note"><p>Files will be written without asking. Emails, web actions and app launches still ask you first.</p></Banner>}
      {err && <Banner tone="bad">{err}</Banner>}
      <div className="row"><button className="ee-btn primary" disabled={!request.trim() || !!preview.error} onClick={async () => {
        try { await post("/api/schedules", { name: name || request.slice(0, 60), request, kind, spec: realSpec, auto_commit: auto }); onDone(); }
        catch (e: any) { setErr(e.message); }
      }}>Create schedule</button></div>
    </Dialog>
  );
}

function NewListener({ onClose, onDone }: { onClose: () => void; onDone: (l: Listener) => void }) {
  const [kind, setKind] = useState<"file" | "email" | "webhook">("file");
  const [name, setName] = useState("");
  const [path, setPath] = useState("~/Notes");
  const [pattern, setPattern] = useState("*.md");
  const [from, setFrom] = useState("");
  const [subject, setSubject] = useState("");
  const [cooldown, setCooldown] = useState(60);
  const [auto, setAuto] = useState(false);
  const defaults: Record<string, string> = {
    file: "Summarise what changed in {files}",
    email: "Read the email “{subject}” from {from} and save the action items as a note",
    webhook: "Handle this event: {text}",
  };
  const [request, setRequest] = useState(defaults.file);
  const [err, setErr] = useState<string | null>(null);
  const pick = (k: typeof kind) => { setKind(k); setRequest(defaults[k]); };
  const config = kind === "file" ? { path, pattern } : kind === "email" ? { from_contains: from || undefined, subject_contains: subject || undefined } : {};
  return (
    <Dialog title="New listener" onClose={onClose}>
      <Seg label="Listen for" value={kind} onChange={pick} items={[{ id: "file", label: "File changes" }, { id: "email", label: "New email" }, { id: "webhook", label: "Webhook" }]} />
      {kind === "file" && <div className="form-grid">
        <Field label="Folder" hint="In your workspace, or ~/ for your Home folder."><input className="input mono" value={path} onChange={(e) => setPath(e.target.value)} /></Field>
        <Field label="File names" hint="For example *.md or * for all."><input className="input mono" value={pattern} onChange={(e) => setPattern(e.target.value)} /></Field>
      </div>}
      {kind === "email" && <div className="form-grid">
        <Field label="From contains (optional)"><input className="input" value={from} onChange={(e) => setFrom(e.target.value)} placeholder="ci@example.com" /></Field>
        <Field label="Subject contains (optional)"><input className="input" value={subject} onChange={(e) => setSubject(e.target.value)} placeholder="build failed" /></Field>
      </div>}
      {kind === "webhook" && <p className="muted" style={{ margin: 0 }}>You'll get a local address and a secret. Scripts on this computer can POST JSON to it.</p>}
      <Field label="What should happen" hint={`Placeholders: ${kind === "file" ? "{files} {path} {changes}" : kind === "email" ? "{subject} {from} {date}" : "any JSON field, e.g. {text}"}. Event content is passed to the agent as data, not as instructions.`}>
        <textarea className="textarea" rows={3} value={request} onChange={(e) => setRequest(e.target.value)} />
      </Field>
      <div className="form-grid">
        <Field label="Name"><input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder={kind === "file" ? "Notes watcher" : kind === "email" ? "Build failures" : "CI hook"} /></Field>
        <Field label="Cooldown (seconds)" hint="Events inside it are batched."><input className="input" type="number" min={5} value={cooldown} onChange={(e) => setCooldown(Math.max(5, Number(e.target.value)))} /></Field>
      </div>
      <Switch checked={auto} onChange={setAuto} label="Commit changes automatically (still undoable)" />
      {err && <Banner tone="bad">{err}</Banner>}
      <div className="row"><button className="ee-btn primary" disabled={!request.trim()} onClick={async () => {
        try {
          const l = await post<Listener>("/api/listeners", { name: name || { file: "Files changed", email: "New email", webhook: "Webhook" }[kind], kind, request, config, cooldown_s: cooldown, auto_commit: auto });
          onDone(l);
        } catch (e: any) { setErr(e.message); }
      }}>Create listener</button></div>
    </Dialog>
  );
}
