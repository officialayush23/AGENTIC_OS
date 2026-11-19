import { useCallback, useEffect, useState } from "react";
import { del, get, post, subscribe } from "../api";
import type { KbHit } from "../types";

interface KbStatus {
  documents: number;
  chunks: number;
  embedder: string;
  stale_documents: number;
  keyword_index: string;
  index_job: { running: boolean; progress: any; result: any; error: string | null };
}

export function KnowledgeView() {
  const [status, setStatus] = useState<KbStatus | null>(null);
  const [docs, setDocs] = useState<any[]>([]);
  const [mode, setMode] = useState<"ask" | "search">("ask");
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<KbHit[] | null>(null);
  const [answer, setAnswer] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [noteTitle, setNoteTitle] = useState("");
  const [noteText, setNoteText] = useState("");
  const [showDocs, setShowDocs] = useState(false);

  const load = useCallback(async () => {
    setStatus(await get<KbStatus>("/api/kb/status"));
    setDocs((await get<{ documents: any[] }>("/api/kb/documents")).documents);
  }, []);

  useEffect(() => {
    void load();
    const ctl = new AbortController();
    subscribe("/api/events", (ev) => {
      if (ev.type === "kb.progress") setStatus((s) => (s ? { ...s, index_job: { ...s.index_job, running: true, progress: ev.data } } : s));
      if (ev.type === "kb.done") void load();
    }, ctl.signal);
    return () => ctl.abort();
  }, [load]);

  const run = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!q.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      if (mode === "search") {
        setAnswer(null);
        setHits((await post<{ hits: KbHit[] }>("/api/kb/search", { query: q, k: 10 })).hits);
      } else {
        setHits(null);
        setAnswer(await post("/api/kb/ask", { question: q }));
      }
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  const job = status?.index_job;
  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h2>Knowledge</h2>
          <p className="muted">
            {status ? `${status.documents} documents · ${status.chunks} passages · ${status.embedder} · keywords: ${status.keyword_index}` : "…"}
          </p>
        </div>
        <div className="actions">
          {job?.running ? (
            <>
              <span className="muted small">Indexing {job.progress?.done ?? 0}/{job.progress?.total ?? "?"}</span>
              <button onClick={() => post("/api/kb/index/cancel")}>Stop</button>
            </>
          ) : (
            <button className="primary" onClick={async () => { await post("/api/kb/index", { path: "." }); void load(); }}>
              Index workspace
            </button>
          )}
        </div>
      </header>
      {status && status.stale_documents > 0 && (
        <div className="notice">{status.stale_documents} document(s) were embedded with a different model. Re-index to search them by meaning.</div>
      )}
      {job?.error && <p className="error">Indexing failed: {job.error}</p>}
      {job?.result && !job.running && (
        <p className="muted small">
          Last index: {job.result.indexed} indexed, {job.result.unchanged} unchanged, {job.result.skipped} skipped,
          {" "}{job.result.removed} removed{job.result.errors?.length ? `, ${job.result.errors.length} errors` : ""}.
        </p>
      )}

      <form className="card" onSubmit={run}>
        <div className="seg">
          <button type="button" className={mode === "ask" ? "active" : ""} onClick={() => setMode("ask")}>Ask</button>
          <button type="button" className={mode === "search" ? "active" : ""} onClick={() => setMode("search")}>Search</button>
        </div>
        <div className="row">
          <input className="grow" value={q} onChange={(e) => setQ(e.target.value)}
                 placeholder={mode === "ask" ? "Ask a question about your files and notes" : "Search by meaning and keywords"} />
          <button className="primary" disabled={busy || !q.trim()}>{busy ? "…" : mode === "ask" ? "Ask" : "Search"}</button>
        </div>
        {err && <p className="error">{err}</p>}
      </form>

      {answer && (
        <div className="card">
          {answer.mode === "generated" && <div className="answer">{answer.answer}</div>}
          {answer.mode === "retrieval_only" && (
            <p className="muted small">No AI provider is set up, so here are the most relevant passages. Add one in Settings to get written answers.</p>
          )}
          {answer.mode === "empty" && <p className="muted">{answer.answer}</p>}
          {answer.sources?.map((s: any) => (
            <div key={s.n} className="hit">
              <div className="mono small">[{s.n}] {s.source === "note" ? `note: ${s.path}` : `${s.path}:${s.start_line}-${s.end_line}`}</div>
              <pre className="snippet">{s.text}</pre>
            </div>
          ))}
        </div>
      )}
      {hits && (
        <div className="card">
          {hits.length === 0 && <p className="muted">No matches.</p>}
          {hits.map((h) => (
            <div key={h.chunk_id} className="hit">
              <div className="row small">
                <span className="mono grow">{h.source === "note" ? `note: ${h.path}` : `${h.path}:${h.start_line}-${h.end_line}`}</span>
                {h.vector_rank && <span className="tag">meaning #{h.vector_rank}</span>}
                {h.keyword_rank && <span className="tag">keyword #{h.keyword_rank}</span>}
              </div>
              <pre className="snippet">{h.text}</pre>
            </div>
          ))}
        </div>
      )}

      <div className="grid2">
        <form className="card" onSubmit={async (e) => {
          e.preventDefault();
          try {
            await post("/api/kb/notes", { title: noteTitle, text: noteText });
            setNoteTitle(""); setNoteText(""); void load();
          } catch (e: any) { setErr(e.message); }
        }}>
          <h3>Add a note</h3>
          <input value={noteTitle} onChange={(e) => setNoteTitle(e.target.value)} placeholder="Title" />
          <textarea rows={4} value={noteText} onChange={(e) => setNoteText(e.target.value)} placeholder="Anything the agent should know" />
          <button disabled={!noteTitle.trim() || !noteText.trim()}>Save note</button>
        </form>
        <div className="card">
          <div className="row">
            <h3 className="grow">Documents</h3>
            <button onClick={() => setShowDocs(!showDocs)}>{showDocs ? "Hide" : `Show ${docs.length}`}</button>
          </div>
          {showDocs && (
            <ul className="doc-list">
              {docs.map((d) => (
                <li key={d.id} className="row small">
                  <span className="tag">{d.source}</span>
                  <span className="mono grow ellipsis">{d.path}</span>
                  <span className="muted">{d.chunk_count}</span>
                  <button className="link" onClick={async () => { await del(`/api/kb/documents/${d.id}`); void load(); }}>remove</button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
