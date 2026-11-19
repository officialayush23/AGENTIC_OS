import { useCallback, useEffect, useState } from "react";
import { del, get, isDesktop, post, put } from "../api";
import type { Provider } from "../types";

export function SettingsView({ onChange }: { onChange: () => void }) {
  const [s, setS] = useState<any>(null);
  const [kinds, setKinds] = useState<Record<string, any>>({});
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [ws, setWs] = useState("");

  const load = useCallback(async () => {
    const r = await get("/api/settings");
    setS(r.settings);
    setKinds(r.embedder_kinds);
    setWs(r.settings.workspace_root ?? "");
  }, []);
  useEffect(() => { void load(); }, [load]);

  const save = async (patch: Record<string, unknown>, label = "Saved") => {
    setMsg(null);
    try {
      const r = await put("/api/settings", patch);
      setS(r.settings);
      setMsg({ ok: true, text: label });
      onChange();
    } catch (e: any) {
      setMsg({ ok: false, text: e.message });
    }
  };

  const pickFolder = async () => {
    const { invoke } = await import("@tauri-apps/api/core");
    const path = await invoke<string | null>("pick_folder");
    if (path) setWs(path);
  };

  if (!s) return <div className="pad muted">Loading…</div>;
  const emb = s.rag_embedder ?? { kind: "local" };
  return (
    <div className="page">
      <header className="page-head"><h2>Settings</h2></header>
      {msg && <div className={`notice ${msg.ok ? "" : "notice-bad"}`}>{msg.text}</div>}

      <div className="card">
        <h3>Workspace</h3>
        <p className="muted small">The folder tasks can read and (after your review) change. Sensitive files such as .env and keys are always off-limits.</p>
        <div className="row">
          <input className="grow mono" value={ws} onChange={(e) => setWs(e.target.value)} />
          {isDesktop() && <button onClick={pickFolder}>Browse…</button>}
          <button className="primary" disabled={ws === s.workspace_root}
                  onClick={() => save({ workspace_root: ws }, "Workspace changed. Re-index it in Knowledge.")}>Use folder</button>
        </div>
      </div>

      <div className="card">
        <h3>Behaviour</h3>
        <label className="field">
          <span>Planner</span>
          <select value={s.planner_mode} onChange={(e) => save({ planner_mode: e.target.value })}>
            <option value="auto">Rules first, then AI provider (recommended)</option>
            <option value="rules">Rules only (offline, no AI calls)</option>
            <option value="llm">Always use the AI provider</option>
          </select>
        </label>
        <label className="check">
          <input type="checkbox" checked={!!s.require_approval_for_writes}
                 onChange={(e) => save({ require_approval_for_writes: e.target.checked })} />
          Also ask before each staged write (the commit review always happens)
        </label>
        <label className="check">
          <input type="checkbox" checked={!!s.allow_app_launch} onChange={(e) => save({ allow_app_launch: e.target.checked })} />
          Allow launching allowlisted apps
        </label>
        <label className="check">
          <input type="checkbox" checked={!!s.rag_auto_index} onChange={(e) => save({ rag_auto_index: e.target.checked })} />
          Keep the knowledge base in sync (re-index on startup, commit and undo)
        </label>
      </div>

      <EmbedderCard current={emb} kinds={kinds} onSave={(cfg) => save({ rag_embedder: cfg }, "Embedder saved. Re-index to embed existing documents.")} />
      <ProvidersCard onChange={onChange} />
      <AppsCard />
    </div>
  );
}

function EmbedderCard({ current, kinds, onSave }: { current: any; kinds: Record<string, any>; onSave: (c: any) => void }) {
  const [cfg, setCfg] = useState<any>(current);
  const [providers, setProviders] = useState<Provider[]>([]);
  useEffect(() => setCfg(current), [current]);
  useEffect(() => { void get("/api/providers").then((r) => setProviders(r.providers)); }, []);
  const def = kinds[cfg.kind] ?? {};
  return (
    <div className="card">
      <h3>Embeddings (knowledge base)</h3>
      <p className="muted small">
        <b>Local</b> works offline with no downloads. <b>Ollama</b> runs a real embedding model on this machine.
        <b> OpenAI</b>/<b>Gemini</b> use the key of a provider you set up below.
      </p>
      <div className="grid2">
        <label className="field"><span>Type</span>
          <select value={cfg.kind} onChange={(e) => setCfg({ kind: e.target.value })}>
            <option value="local">Local (hashing, offline)</option>
            <option value="ollama">Ollama (local model)</option>
            <option value="openai">OpenAI / compatible</option>
            <option value="gemini">Google Gemini</option>
          </select>
        </label>
        {cfg.kind !== "local" && (
          <label className="field"><span>Model</span>
            <input value={cfg.model ?? ""} placeholder={def.model} onChange={(e) => setCfg({ ...cfg, model: e.target.value })} />
          </label>
        )}
        {(cfg.kind === "ollama" || cfg.kind === "openai") && (
          <label className="field"><span>Base URL</span>
            <input value={cfg.base_url ?? ""} placeholder={def.base_url} onChange={(e) => setCfg({ ...cfg, base_url: e.target.value })} />
          </label>
        )}
        {(cfg.kind === "openai" || cfg.kind === "gemini") && (
          <label className="field"><span>Use key from provider</span>
            <select value={cfg.provider_id ?? ""} onChange={(e) => setCfg({ ...cfg, provider_id: e.target.value || undefined })}>
              <option value="">Choose…</option>
              {providers.map((p) => <option key={p.id} value={p.id}>{p.display_name} ({p.id})</option>)}
            </select>
          </label>
        )}
      </div>
      <button className="primary" onClick={() => onSave(cfg)}>Save embedder</button>
    </div>
  );
}

function ProvidersCard({ onChange }: { onChange: () => void }) {
  const [list, setList] = useState<Provider[]>([]);
  const [kinds, setKinds] = useState<Record<string, any>>({});
  const [form, setForm] = useState<any>({ id: "", kind: "openai", model: "", base_url: "", api_key: "" });
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const load = useCallback(async () => {
    const r = await get("/api/providers");
    setList(r.providers);
    setKinds(r.kinds);
  }, []);
  useEffect(() => { void load(); }, [load]);

  const run = async (fn: () => Promise<unknown>, ok: string) => {
    setMsg(null);
    try {
      const r: any = await fn();
      setMsg({ ok: r?.ok !== false, text: r?.ok === false ? r?.message ?? "Test failed" : ok });
      await load();
      onChange();
    } catch (e: any) {
      setMsg({ ok: false, text: e.message });
    }
  };

  const add = () => run(async () => {
    const id = form.id.trim() || form.kind.replace("_", "-");
    await put(`/api/providers/${id}`, { kind: form.kind, model: form.model || undefined, base_url: form.base_url || undefined });
    if (form.api_key.trim()) await post(`/api/providers/${id}/credential`, { api_key: form.api_key.trim() });
    if (!list.some((p) => p.is_default)) await post(`/api/providers/${id}/default`);
    setForm({ ...form, id: "", api_key: "" });
  }, "Provider saved. Use Test to check it.");

  const k = kinds[form.kind] ?? {};
  return (
    <div className="card">
      <h3>AI providers</h3>
      <p className="muted small">Used for free-form planning and written answers. Keys are stored in your OS credential store, never in the database.</p>
      {msg && <div className={`notice ${msg.ok ? "" : "notice-bad"}`}>{msg.text}</div>}
      <ul className="prov-list">
        {list.map((p) => (
          <li key={p.id} className="row wrap">
            <b>{p.display_name}</b>
            <span className="mono small">{p.model}</span>
            {p.is_default && <span className="tag">default</span>}
            <span className={`tag ${p.credential.stored || !p.credential.required ? "" : "tag-bad"}`}>
              {p.credential.required ? (p.credential.stored ? "key stored" : "no key") : "no key needed"}
            </span>
            <span className="push" />
            <button onClick={() => run(() => post(`/api/providers/${p.id}/test`), "Connection works")}>Test</button>
            {!p.is_default && <button onClick={() => run(() => post(`/api/providers/${p.id}/default`), "Default set")}>Make default</button>}
            <button className="danger-ghost" onClick={() => run(() => del(`/api/providers/${p.id}`), "Removed")}>Remove</button>
          </li>
        ))}
      </ul>
      <div className="grid2">
        <label className="field"><span>Type</span>
          <select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value, model: "" })}>
            {Object.entries(kinds).map(([id, m]: any) => <option key={id} value={id}>{m.label}</option>)}
          </select>
        </label>
        <label className="field"><span>Model</span>
          <input value={form.model} placeholder={k.default_model || "model id"} onChange={(e) => setForm({ ...form, model: e.target.value })} />
        </label>
        {k.requires_base_url && (
          <label className="field"><span>Base URL</span>
            <input value={form.base_url} placeholder="https://host/v1" onChange={(e) => setForm({ ...form, base_url: e.target.value })} />
          </label>
        )}
        <label className="field"><span>API key</span>
          <input type="password" value={form.api_key} placeholder={k.key_hint} onChange={(e) => setForm({ ...form, api_key: e.target.value })} />
        </label>
        <label className="field"><span>Id (optional)</span>
          <input value={form.id} placeholder={form.kind.replace("_", "-")} onChange={(e) => setForm({ ...form, id: e.target.value })} />
        </label>
      </div>
      <button className="primary" onClick={add}>Add provider</button>
    </div>
  );
}

function AppsCard() {
  const [apps, setApps] = useState<any[]>([]);
  const [f, setF] = useState({ id: "", name: "", path: "", args_policy: "workspace_file" });
  const [err, setErr] = useState<string | null>(null);
  const load = useCallback(async () => setApps((await get("/api/apps")).apps), []);
  useEffect(() => { void load(); }, [load]);
  return (
    <div className="card">
      <h3>Allowlisted apps</h3>
      <p className="muted small">The only programs a task may open. Each launch needs your approval (once, or for a while).</p>
      <ul className="prov-list">
        {apps.map((a) => (
          <li key={a.id} className="row">
            <b>{a.name}</b><span className="mono small grow ellipsis">{a.path}</span>
            <button className="danger-ghost" onClick={async () => { await del(`/api/apps/${a.id}`); void load(); }}>Remove</button>
          </li>
        ))}
      </ul>
      <div className="grid2">
        <input placeholder="id (e.g. notepad)" value={f.id} onChange={(e) => setF({ ...f, id: e.target.value })} />
        <input placeholder="Name" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} />
        <input className="mono" placeholder="C:\\Windows\\System32\\notepad.exe" value={f.path} onChange={(e) => setF({ ...f, path: e.target.value })} />
        <select value={f.args_policy} onChange={(e) => setF({ ...f, args_policy: e.target.value })}>
          <option value="workspace_file">Can open a workspace file</option>
          <option value="none">No arguments</option>
        </select>
      </div>
      {err && <p className="error">{err}</p>}
      <button disabled={!f.id || !f.name || !f.path} onClick={async () => {
        try { await put(`/api/apps/${f.id}`, { name: f.name, path: f.path, args_policy: f.args_policy }); setF({ id: "", name: "", path: "", args_policy: "workspace_file" }); setErr(null); void load(); }
        catch (e: any) { setErr(e.message); }
      }}>Add app</button>
    </div>
  );
}
