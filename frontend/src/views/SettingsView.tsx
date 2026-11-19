import { useCallback, useEffect, useState } from "react";
import { Cpu, Download, FolderOpen, KeyRound, Mail, Plug, Trash2 } from "lucide-react";
import { del, get, isDesktop, post, put } from "../api";
import { useLive } from "../App";
import type { Provider } from "../types";
import { Banner, Card, CardHead, Chip, Field, ICON_SM, PageHeader, Seg, Switch, useToast } from "../ui";

type Tab = "general" | "email" | "ai" | "knowledge" | "apps";

export function SettingsView() {
  const { refresh } = useLive();
  const toast = useToast();
  const [tab, setTab] = useState<Tab>("general");
  const [s, setS] = useState<any>(null);
  const [kinds, setKinds] = useState<Record<string, any>>({});
  const [ws, setWs] = useState("");

  const load = useCallback(async () => {
    const r = await get("/api/settings");
    setS(r.settings);
    setKinds(r.embedder_kinds);
    setWs(r.settings.workspace_root ?? "");
  }, []);
  useEffect(() => { void load(); }, [load]);

  const save = async (patch: Record<string, unknown>, label = "Saved") => {
    try { const r = await put("/api/settings", patch); setS(r.settings); toast({ title: label }); refresh(); }
    catch (e: any) { toast({ title: "Not saved", text: e.message, bad: true }); }
  };
  const pickFolder = async () => {
    const { invoke } = await import("@tauri-apps/api/core");
    const path = await invoke<string | null>("pick_folder");
    if (path) setWs(path);
  };

  if (!s) return <p className="muted">Loading…</p>;
  return (
    <>
      <PageHeader section="System" dot="--ws-system" title="Settings" sub="Everything stays on this computer. Passwords and API keys live in the system credential store." />
      <Seg label="Settings sections" value={tab} onChange={setTab} items={[
        { id: "general", label: "General" }, { id: "email", label: "Email" }, { id: "ai", label: "AI providers" },
        { id: "knowledge", label: "Knowledge" }, { id: "apps", label: "Apps" },
      ]} />
      {tab === "general" && <>
        <Card>
          <CardHead title="Workspace" sub="The project folder tasks can read and, after your review, change. Files like .env and keys are always off-limits." />
          <div className="row nowrap">
            <input className="input mono" aria-label="Workspace folder" value={ws} onChange={(e) => setWs(e.target.value)} />
            {isDesktop() && <button className="ee-btn outline" onClick={pickFolder}><FolderOpen {...ICON_SM} /> Browse</button>}
            <button className="ee-btn primary" disabled={ws === s.workspace_root} onClick={() => save({ workspace_root: ws }, "Workspace changed")}>Use folder</button>
          </div>
          <p className="muted" style={{ marginBottom: 0 }}>Your Home folder (notes, calendar, mail drafts) is separate and always available as <span className="mono">~/</span>.</p>
        </Card>
        <Card>
          <CardHead title="Behaviour" />
          <div className="stack" style={{ gap: 4 }}>
            <Field label="Planner">
              <select className="select" value={s.planner_mode} onChange={(e) => save({ planner_mode: e.target.value })}>
                <option value="auto">Offline rules first, then the AI provider (recommended)</option>
                <option value="rules">Offline rules only (no AI calls)</option>
                <option value="llm">Always use the AI provider</option>
              </select>
            </Field>
            <Switch checked={!!s.require_approval_for_writes} onChange={(v) => save({ require_approval_for_writes: v })} label="Also ask before each staged write (the commit review always happens)" />
            <Switch checked={!!s.allow_app_launch} onChange={(v) => save({ allow_app_launch: v })} label="Allow opening allowlisted apps" />
            <Switch checked={!!s.rag_auto_index} onChange={(v) => save({ rag_auto_index: v })} label="Keep the knowledge base in sync after commits and undo" />
          </div>
        </Card>
      </>}
      {tab === "email" && <EmailCard />}
      {tab === "ai" && <><LocalModelCard /><ProvidersCard /></>}
      {tab === "knowledge" && <EmbedderCard current={s.rag_embedder ?? { kind: "local" }} kinds={kinds} onSave={(cfg) => save({ rag_embedder: cfg }, "Embedder saved. Re-index to use it.")} />}
      {tab === "apps" && <AppsCard />}
    </>
  );
}

function EmailCard() {
  const toast = useToast();
  const [st, setSt] = useState<any>(null);
  const [cfg, setCfg] = useState<any>({ mode: "local" });
  const [pw, setPw] = useState("");
  const load = useCallback(async () => {
    const [status, settings] = await Promise.all([get("/api/mail/status"), get("/api/settings")]);
    setSt(status);
    setCfg({ mode: "local", ...(settings.settings.mail ?? {}) });
  }, []);
  useEffect(() => { void load(); }, [load]);
  const run = async (fn: () => Promise<any>, ok: string) => {
    try { await fn(); toast({ title: ok }); await load(); } catch (e: any) { toast({ title: "That didn't work", text: e.message, bad: true }); }
  };
  const f = (k: string) => ({ value: cfg[k] ?? "", onChange: (e: any) => setCfg({ ...cfg, [k]: e.target.value }) });
  return (
    <Card>
      <CardHead title="Email account" sub="Reading and drafting are free; every send asks you first."
                right={st && <Chip tone={st.mode === "imap" ? (st.password_stored ? "pass" : "review") : st.mode === "off" ? "nc" : "info"}>
                  {st.mode === "imap" ? (st.password_stored ? "Connected" : "Password missing") : st.mode === "off" ? "Off" : "Demo mailbox"}</Chip>} />
      <Seg label="Email mode" value={cfg.mode} onChange={(m) => setCfg({ ...cfg, mode: m })} items={[{ id: "local", label: "Demo mailbox" }, { id: "imap", label: "My account (IMAP / SMTP)" }, { id: "off", label: "Off" }]} />
      <div className="form-grid" style={{ marginTop: 16 }}>
        <Field label="Your address"><input className="input" {...f("address")} placeholder="you@example.com" /></Field>
        <Field label="Display name"><input className="input" {...f("display_name")} /></Field>
        {cfg.mode === "imap" && <>
          <Field label="IMAP server" hint="Port 993 (TLS)."><input className="input" {...f("imap_host")} placeholder="imap.gmail.com" /></Field>
          <Field label="SMTP server" hint="Port 587 (STARTTLS) or 465."><input className="input" {...f("smtp_host")} placeholder="smtp.gmail.com" /></Field>
          <Field label="User name"><input className="input" {...f("username")} /></Field>
          <Field label="Sent folder"><input className="input" {...f("sent_folder")} placeholder="Sent" /></Field>
        </>}
      </div>
      {cfg.mode === "imap" && (
        <div className="row nowrap" style={{ marginTop: 14 }}>
          <input className="input" type="password" aria-label="Email password" value={pw} onChange={(e) => setPw(e.target.value)}
                 placeholder={st?.password_stored ? "Password stored. Type to replace" : "Password or app password"} />
          <button className="ee-btn outline" disabled={!pw} onClick={() => run(async () => { await post("/api/mail/password", { password: pw }); setPw(""); }, "Password stored securely")}><KeyRound {...ICON_SM} /> Store</button>
          {st?.password_stored && <button className="ee-btn ghost" onClick={() => run(() => del("/api/mail/password"), "Password removed")}>Remove</button>}
        </div>
      )}
      {cfg.mode === "imap" && <p className="muted">For Gmail and Outlook, create an app password in your account's security settings.</p>}
      <div className="row" style={{ marginTop: 14 }}>
        <button className="ee-btn primary" onClick={() => run(() => put("/api/mail/config", cfg), "Email settings saved")}>Save</button>
        <button className="ee-btn outline" onClick={() => run(() => post("/api/mail/test"), "Connection works")}><Plug {...ICON_SM} /> Test connection</button>
      </div>
      {cfg.mode === "local" && <div style={{ marginTop: 14 }}><Banner tone="info" icon={<Mail {...ICON_SM} />}>The demo mailbox lives in your data folder. Sending saves to its Sent folder; nothing is delivered.</Banner></div>}
    </Card>
  );
}

function EmbedderCard({ current, kinds, onSave }: { current: any; kinds: Record<string, any>; onSave: (c: any) => void }) {
  const [cfg, setCfg] = useState<any>(current);
  const [providers, setProviders] = useState<Provider[]>([]);
  useEffect(() => setCfg(current), [current]);
  useEffect(() => { void get("/api/providers").then((r) => setProviders(r.providers)); }, []);
  const def = kinds[cfg.kind] ?? {};
  return (
    <Card>
      <CardHead title="Embeddings" sub="Local works offline with no downloads. Ollama runs a real model on this computer. OpenAI and Gemini use a provider's key." />
      <div className="form-grid">
        <Field label="Type">
          <select className="select" value={cfg.kind} onChange={(e) => setCfg({ kind: e.target.value })}>
            <option value="local">Local (offline)</option><option value="ollama">Ollama (local model)</option>
            <option value="openai">OpenAI or compatible</option><option value="gemini">Google Gemini</option>
          </select>
        </Field>
        {cfg.kind !== "local" && <Field label="Model"><input className="input" value={cfg.model ?? ""} placeholder={def.model} onChange={(e) => setCfg({ ...cfg, model: e.target.value })} /></Field>}
        {(cfg.kind === "ollama" || cfg.kind === "openai") && <Field label="Base URL"><input className="input" value={cfg.base_url ?? ""} placeholder={def.base_url} onChange={(e) => setCfg({ ...cfg, base_url: e.target.value })} /></Field>}
        {(cfg.kind === "openai" || cfg.kind === "gemini") && (
          <Field label="Use the key from">
            <select className="select" value={cfg.provider_id ?? ""} onChange={(e) => setCfg({ ...cfg, provider_id: e.target.value || undefined })}>
              <option value="">Choose a provider</option>
              {providers.map((p) => <option key={p.id} value={p.id}>{p.display_name} ({p.id})</option>)}
            </select>
          </Field>
        )}
      </div>
      <div className="row" style={{ marginTop: 14 }}><button className="ee-btn primary" onClick={() => onSave(cfg)}>Save embedder</button></div>
    </Card>
  );
}

function ProvidersCard() {
  const { refresh } = useLive();
  const toast = useToast();
  const [list, setList] = useState<Provider[]>([]);
  const [kinds, setKinds] = useState<Record<string, any>>({});
  const [form, setForm] = useState<any>({ id: "", kind: "openai", model: "", base_url: "", api_key: "" });
  const load = useCallback(async () => { const r = await get("/api/providers"); setList(r.providers); setKinds(r.kinds); }, []);
  useEffect(() => { void load(); }, [load]);
  const run = async (fn: () => Promise<any>, ok: string) => {
    try {
      const r: any = await fn();
      if (r?.ok === false) toast({ title: "Test failed", text: r?.error?.message ?? r?.message ?? "No details", bad: true });
      else if (r?.error) toast({ title: "Connected, with a warning", text: r.error.message });
      else toast({ title: ok });
      await load(); refresh();
    } catch (e: any) { toast({ title: "That didn't work", text: e.message, bad: true }); }
  };
  const add = () => run(async () => {
    const id = form.id.trim() || form.kind.replace("_", "-");
    await put(`/api/providers/${id}`, { kind: form.kind, model: form.model || undefined, base_url: form.base_url || undefined });
    if (form.api_key.trim()) await post(`/api/providers/${id}/credential`, { api_key: form.api_key.trim() });
    if (!list.some((p) => p.is_default)) await post(`/api/providers/${id}/default`);
    setForm({ ...form, id: "", api_key: "" });
  }, "Provider saved");
  const k = kinds[form.kind] ?? {};
  return (
    <>
      <Card>
        <CardHead title="AI providers" sub="Used for free-form requests and written answers. Without one, the offline planner handles common requests." />
        {list.length === 0 ? <p className="muted">None yet.</p> : (
          <ul className="plain-list">
            {list.map((p) => (
              <li key={p.id} className="plain-row">
                <span className="m"><b>{p.display_name}</b><small className="mono">{p.model}</small></span>
                {p.is_default && <span className="tag">Default</span>}
                <Chip tone={p.credential.stored || !p.credential.required ? "pass" : "review"}>{p.credential.required ? (p.credential.stored ? "Key stored" : "No key") : "No key needed"}</Chip>
                <button className="ee-btn outline sm" onClick={() => run(() => post(`/api/providers/${p.id}/test`), "Connection works")}>Test</button>
                {!p.is_default && <button className="ee-btn ghost sm" onClick={() => run(() => post(`/api/providers/${p.id}/default`), "Default set")}>Make default</button>}
                <button className="ee-btn ghost round sm" aria-label={`Remove ${p.display_name}`} onClick={() => run(() => del(`/api/providers/${p.id}`), "Removed")}><Trash2 {...ICON_SM} /></button>
              </li>
            ))}
          </ul>
        )}
      </Card>
      <Card>
        <CardHead title="Add a provider" sub="Keys are stored in the system credential store, never in the database." />
        <div className="form-grid">
          <Field label="Type"><select className="select" value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value, model: "" })}>
            {Object.entries(kinds).map(([id, m]: any) => <option key={id} value={id}>{m.label}</option>)}</select></Field>
          <Field label="Model"><input className="input" value={form.model} placeholder={k.default_model || "model id"} onChange={(e) => setForm({ ...form, model: e.target.value })} /></Field>
          {k.requires_base_url && <Field label="Base URL"><input className="input" value={form.base_url} placeholder="https://host/v1" onChange={(e) => setForm({ ...form, base_url: e.target.value })} /></Field>}
          {k.requires_key !== false && <Field label="API key"><input className="input" type="password" value={form.api_key} placeholder={k.key_hint} onChange={(e) => setForm({ ...form, api_key: e.target.value })} /></Field>}
          <Field label="Id (optional)"><input className="input" value={form.id} placeholder={form.kind.replace("_", "-")} onChange={(e) => setForm({ ...form, id: e.target.value })} /></Field>
        </div>
        <div className="row" style={{ marginTop: 14 }}><button className="ee-btn primary" onClick={add}>Add provider</button></div>
      </Card>
    </>
  );
}

function AppsCard() {
  const toast = useToast();
  const [apps, setApps] = useState<any[]>([]);
  const [found, setFound] = useState<any[] | null>(null);
  const [q, setQ] = useState("");
  const [f, setF] = useState({ name: "", path: "" });
  const load = useCallback(async () => setApps((await get("/api/apps")).apps), []);
  const scan = useCallback(async (refresh = false) => {
    setFound(null);
    try { setFound((await get(`/api/apps/discover${refresh ? "?refresh=true" : ""}`)).apps); }
    catch (e: any) { toast({ title: "Couldn't list apps", text: e.message, bad: true }); setFound([]); }
  }, [toast]);
  useEffect(() => { void load(); void scan(); }, [load, scan]);
  const add = async (name: string, path: string) => {
    try { await post("/api/apps/add", { name, path }); toast({ title: "Added", text: name }); await load(); await scan(); }
    catch (e: any) { toast({ title: "Not added", text: e.message, bad: true }); }
  };
  const shown = (found ?? []).filter((a) => !q || a.name.toLowerCase().includes(q.toLowerCase()));
  return (
    <>
      <Card>
        <CardHead title="Apps the agent may open" sub="Each launch still asks you first, once or for a while." />
        {apps.length === 0 ? <p className="muted">None yet. Pick from the list below.</p> : (
          <ul className="plain-list">
            {apps.map((a) => (
              <li key={a.id} className="plain-row">
                <span className="m"><b>{a.name}</b><small className="mono ellipsis">{a.path}</small></span>
                <button className="ee-btn ghost round sm" aria-label={`Remove ${a.name}`} onClick={async () => { await del(`/api/apps/${a.id}`); void load(); void scan(); }}><Trash2 {...ICON_SM} /></button>
              </li>
            ))}
          </ul>
        )}
      </Card>
      <Card>
        <CardHead title="Installed on this computer" sub={found ? `${found.length} found` : "Looking…"}
                  right={<button className="ee-btn outline sm" onClick={() => scan(true)}>Scan again</button>} />
        <input className="input" style={{ marginBottom: 12 }} placeholder="Filter apps" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Filter apps" />
        <ul className="plain-list list-scroll">
          {shown.map((a) => (
            <li key={a.path} className="plain-row">
              <span className="m"><b>{a.name}</b><small className="mono ellipsis">{a.path}</small></span>
              {a.added ? <Chip tone="pass">Added</Chip> : <button className="ee-btn outline sm" onClick={() => add(a.name, a.path)}>Add</button>}
            </li>
          ))}
          {found && shown.length === 0 && <p className="muted">Nothing matches.</p>}
        </ul>
        <details style={{ marginTop: 12 }}><summary className="muted" style={{ cursor: "pointer" }}>Not listed? Add a program by its path</summary>
          <div className="form-grid" style={{ marginTop: 10 }}>
            <Field label="Name"><input className="input" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
            <Field label="Program"><input className="input mono" placeholder="C:\\Program Files\\App\\app.exe" value={f.path} onChange={(e) => setF({ ...f, path: e.target.value })} /></Field>
          </div>
          <div className="row" style={{ marginTop: 10 }}><button className="ee-btn outline" disabled={!f.name || !f.path} onClick={() => add(f.name, f.path)}>Add</button></div>
        </details>
      </Card>
    </>
  );
}

const USE_WORD: Record<string, string> = { backup: "Backup", main: "Main model", off: "Off" };

function LocalModelCard() {
  const { refresh } = useLive();
  const toast = useToast();
  const [st, setSt] = useState<any>(null);
  const [pick, setPick] = useState<string>("");
  const load = useCallback(async () => { const r = await get<any>("/api/local-model"); setSt(r); setPick((p) => p || r.recommended); }, []);
  useEffect(() => { void load(); }, [load]);
  const job = st?.job;
  useEffect(() => {
    if (!job?.running) return;
    const t = setInterval(() => void load(), 1500);
    return () => clearInterval(t);
  }, [job?.running, load]);
  useEffect(() => { if (job && !job.running) refresh(); }, [job?.running]); // eslint-disable-line react-hooks/exhaustive-deps
  const act = async (fn: () => Promise<any>, ok?: string) => {
    try { await fn(); if (ok) toast({ title: ok }); await load(); refresh(); }
    catch (e: any) { toast({ title: "That didn't work", text: e.message, bad: true }); }
  };
  if (!st) return null;
  const installed: string[] = st.installed ?? [];
  const active = st.provider?.model;
  const test = st.provider?.last_test;
  return (
    <Card>
      <CardHead title="Local AI model (runs on this PC)"
        sub="A quantized Qwen2.5 on your GPU (DirectML) or CPU with ONNX Runtime. Works offline, no quota. Used as a backup when the online model is busy or out of quota, or as the main model."
        right={st.provider ? <Seg compact label="How the local model is used" value={st.use as string}
          onChange={(v: string) => void act(() => put("/api/local-model/use", { use: v }), `Local model: ${USE_WORD[v]}`)}
          items={[{ id: "backup", label: "Backup" }, { id: "main", label: "Main" }, { id: "off", label: "Off" }]} /> : undefined} />
      <div className="lm-folder">
        <FolderOpen {...ICON_SM} />
        <span className="m"><small className="muted">Models are saved in</small><b className="mono">{st.storage?.folder}</b></span>
        {st.storage?.free_gb != null && <span className="tag">{st.storage.free_gb} GB free</span>}
        <button className="ee-btn outline sm" disabled={job?.running} onClick={() => {
          const path = prompt("Folder for local AI models (for example D:\\AI\\models):", st.storage?.folder ?? "");
          if (path && path !== st.storage?.folder) void act(() => put("/api/local-model/folder", { path }), "Model folder changed");
        }}>Change</button>
      </div>
      <div className="lm-list">
        {st.catalog.map((m: any) => (
          <label key={m.id} className={`lm-item ${pick === m.id ? "on" : ""}`}>
            <input type="radio" name="lm" checked={pick === m.id} onChange={() => setPick(m.id)} />
            <span className="m"><b>{m.label}</b>
              <small>{m.note} · {(m.size_mb / 1024).toFixed(1)} GB download{m.vram_gb ? ` · ~${m.vram_gb} GB GPU memory` : ""}</small></span>
            {m.id === st.recommended && <span className="tag">Recommended here</span>}
            {installed.includes(m.id) && <Chip tone={m.id === active ? "pass" : "nc"}>{m.id === active ? "In use" : "Downloaded"}</Chip>}
            {installed.includes(m.id) && <button className="ee-btn ghost round sm" aria-label={`Delete ${m.label}`}
              onClick={(e) => { e.preventDefault(); if (confirm("Delete this model's files?")) void act(() => del(`/api/local-model/${m.id}`), "Deleted"); }}><Trash2 {...ICON_SM} /></button>}
          </label>
        ))}
      </div>
      {job && (job.running || job.error || job.stage === "done") && (
        <div className="lm-job">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <span>{job.error ? <b className="bad-text">Failed: {job.error}</b> : job.message || job.stage}</span>
            {job.total_mb > 0 && <span className="mono">{job.done_mb} / {job.total_mb} MB</span>}
          </div>
          {job.running && <div className="kmeter"><div className="bar"><i style={{ width: `${job.percent ?? 5}%`, background: "var(--ink)" }} /></div></div>}
          {job.result && <small className="muted">Self-test: {job.result.ok ? `passed in ${(job.result.latency_ms / 1000).toFixed(1)} s` : (job.result.error?.message ?? "failed")}</small>}
        </div>
      )}
      <div className="row" style={{ marginTop: 14, gap: 8, flexWrap: "wrap" }}>
        {job?.running
          ? <button className="ee-btn outline" onClick={() => act(() => post("/api/local-model/cancel"))}>Cancel</button>
          : <button className="ee-btn primary" disabled={!pick} onClick={() => act(() => post("/api/local-model/install", { model: pick }))}>
              <Download {...ICON_SM} /> {installed.includes(pick) ? (pick === active ? "Re-check" : "Use this model") : "Install"}</button>}
        {st.provider && <button className="ee-btn outline" onClick={() => act(async () => {
          const r: any = await post("/api/local-model/test");
          if (!r.ok) throw new Error(r.error?.message ?? "Test failed");
        }, "Local model works")}><Cpu {...ICON_SM} /> Test</button>}
        <span className="muted">Runtime: {st.runtime.installed ? `${st.runtime.package} ${st.runtime.version ?? ""}` : "not installed yet (installed with the model)"}
          {test ? ` · last test ${test.ok ? "passed" : "failed"}` : ""}</span>
      </div>
    </Card>
  );
}
