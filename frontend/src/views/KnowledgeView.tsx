import { useCallback, useEffect, useState } from "react";
import { BookOpen, Database, FileSearch, Layers, RefreshCw, Search, Square, Trash2 } from "lucide-react";
import { del, get, post } from "../api";
import { useLive } from "../App";
import type { KbHit } from "../types";
import { Banner, Card, CardHead, Empty, ICON, ICON_SM, Kpi, PageHeader, Seg } from "../ui";

interface KbStatus {
  documents: number; chunks: number; embedder: string; stale_documents: number; keyword_index: string;
  index_job: { running: boolean; progress: any; result: any; error: string | null };
}

export function KnowledgeView() {
  const { sel, bump } = useLive();
  const [status, setStatus] = useState<KbStatus | null>(null);
  const [docs, setDocs] = useState<any[]>([]);
  const [mode, setMode] = useState<"ask" | "search">(sel ? "search" : "ask");
  const [q, setQ] = useState(sel ?? "");
  const [hits, setHits] = useState<KbHit[] | null>(null);
  const [answer, setAnswer] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [showDocs, setShowDocs] = useState(false);

  const load = useCallback(async () => {
    setStatus(await get<KbStatus>("/api/kb/status"));
    setDocs((await get<{ documents: any[] }>("/api/kb/documents")).documents);
  }, []);
  useEffect(() => { void load(); }, [load, bump]);

  const go = useCallback(async (text: string, m: "ask" | "search") => {
    if (!text.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      if (m === "search") { setAnswer(null); setHits((await post<{ hits: KbHit[] }>("/api/kb/search", { query: text, k: 12 })).hits); }
      else { setHits(null); setAnswer(await post("/api/kb/ask", { question: text })); }
    } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  }, []);
  useEffect(() => { if (sel) { setQ(sel); setMode("search"); void go(sel, "search"); } }, [sel, go]);

  const job = status?.index_job;
  const homeDocs = docs.filter((d) => d.path.startsWith("~/")).length;
  const loc = (h: { source: string; path: string; start_line: number; end_line: number }) =>
    h.source === "note" ? `Note: ${h.path}` : `${h.path} · lines ${h.start_line}–${h.end_line}`;

  return (
    <>
      <PageHeader section="Memory" dot="--ws-review" title="Knowledge"
                  sub="Your workspace and Home folder, indexed on this computer for search by meaning and by keyword. Tasks search it too."
                  actions={job?.running
                    ? <button className="ee-btn outline" onClick={() => post("/api/kb/index/cancel")}><Square {...ICON_SM} /> Stop indexing</button>
                    : <button className="ee-btn outline" onClick={async () => { await post("/api/kb/index", { path: "." }); void load(); }}><RefreshCw {...ICON_SM} /> Re-index</button>} />
      <div className="kpis">
        <Kpi label="Documents" value={status?.documents ?? "–"} icon={<Database {...ICON_SM} />} hint={`${homeDocs} from your Home folder`} />
        <Kpi label="Passages" value={status?.chunks ?? "–"} icon={<Layers {...ICON_SM} />} hint="Searchable chunks" />
        <Kpi label="Embedder" value={<span style={{ fontSize: 22 }}>{status?.embedder ?? "–"}</span>} icon={<BookOpen {...ICON_SM} />} hint={`Keywords: ${status?.keyword_index ?? "…"}`} />
        <Kpi label="Index" value={job?.running ? `${job.progress?.done ?? 0}/${job.progress?.total ?? "?"}` : "Idle"} icon={<FileSearch {...ICON_SM} />}
             hint={job?.result && !job.running ? `${job.result.indexed} indexed, ${job.result.unchanged} unchanged, ${job.result.removed} removed` : job?.running ? "Indexing now" : "Up to date"} />
      </div>
      {status && status.stale_documents > 0 && <Banner tone="note">{status.stale_documents} document(s) were embedded with a different model. Re-index to search them by meaning.</Banner>}
      {job?.error && <Banner tone="bad">Indexing failed: {job.error}</Banner>}

      <Card>
        <Seg compact label="Mode" value={mode} onChange={setMode} items={[{ id: "ask", label: "Ask a question" }, { id: "search", label: "Search" }]} />
        <form className="row nowrap" style={{ marginTop: 12 }} onSubmit={(e) => { e.preventDefault(); void go(q, mode); }}>
          <input className="input" value={q} onChange={(e) => setQ(e.target.value)} aria-label={mode === "ask" ? "Question" : "Search"}
                 placeholder={mode === "ask" ? "How does the app start?" : "Search by meaning and keywords"} />
          <button className="ee-btn primary" disabled={busy || !q.trim()}><Search {...ICON_SM} /> {mode === "ask" ? "Ask" : "Search"}</button>
        </form>
        {err && <div style={{ marginTop: 12 }}><Banner tone="bad">{err}</Banner></div>}
      </Card>

      {answer && (
        <Card>
          {answer.mode === "generated" && <p style={{ whiteSpace: "pre-wrap", marginTop: 0, fontSize: 15, lineHeight: 1.6 }}>{answer.answer}</p>}
          {answer.mode === "retrieval_only" && <Banner tone="info">No AI provider is set up, so here are the most relevant passages. Add one in Settings for written answers.</Banner>}
          {answer.mode === "empty" && <p className="muted">{answer.answer}</p>}
          <div className="stack" style={{ marginTop: 12 }}>
            {answer.sources?.map((s: any) => (
              <div key={s.n}><div className="muted mono">[{s.n}] {loc(s)}</div><pre className="code" style={{ background: "var(--soft)" }}>{s.text}</pre></div>
            ))}
          </div>
        </Card>
      )}
      {hits && (
        <Card>
          <CardHead title={`${hits.length} match${hits.length === 1 ? "" : "es"}`} sub={`for “${q}”`} />
          {hits.length === 0 ? <Empty icon={<Search {...ICON} />}>Nothing matches. Try other words, or re-index.</Empty> : (
            <div className="stack">
              {hits.map((h) => (
                <div key={h.chunk_id}>
                  <div className="row"><span className="mono grow ellipsis">{loc(h)}</span>
                    {h.vector_rank && <span className="tag">Meaning #{h.vector_rank}</span>}
                    {h.keyword_rank && <span className="tag">Keyword #{h.keyword_rank}</span>}</div>
                  <pre className="code" style={{ background: "var(--soft)", marginTop: 6 }}>{h.text}</pre>
                </div>
              ))}
            </div>
          )}
        </Card>
      )}

      <Card>
        <CardHead title="Indexed documents" sub={`${docs.length} total`}
                  right={<button className="ee-btn outline sm" onClick={() => setShowDocs(!showDocs)}>{showDocs ? "Hide" : "Show all"}</button>} />
        {showDocs && (
          <ul className="plain-list list-scroll">
            {docs.map((d) => (
              <li key={d.id} className="plain-row">
                <span className="m"><b className="mono ellipsis" style={{ fontWeight: 500 }}>{d.path}</b><small>{d.chunk_count} passages · {d.source === "note" ? "Note" : d.path.startsWith("~/") ? "Home folder" : "Workspace"}</small></span>
                <button className="ee-btn ghost round sm" aria-label={`Remove ${d.path} from the index`} onClick={async () => { await del(`/api/kb/documents/${d.id}`); void load(); }}><Trash2 {...ICON_SM} /></button>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </>
  );
}
