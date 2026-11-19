import { useEffect, useState } from "react";
import { FilePlus2, NotebookPen, Save, Sparkles, Trash2 } from "lucide-react";
import { api, get, put } from "../api";
import { useLive } from "../App";
import type { NoteInfo } from "../types";
import { Banner, Empty, ICON, ICON_SM, PageHeader, SplitPanes, ago, useAsync, useToast } from "../ui";

export function NotesView() {
  const { sel, nav, bump } = useLive();
  const toast = useToast();
  const list = useAsync(() => get<{ notes: NoteInfo[] }>("/api/notes"), [bump]);
  const [filter, setFilter] = useState("");
  const notes = (list.data?.notes ?? []).filter((n) => !filter || (n.title + n.snippet + n.path).toLowerCase().includes(filter.toLowerCase()));
  const current = sel ?? notes[0]?.path ?? null;

  const create = async () => {
    const title = prompt("Name of the new note");
    if (!title?.trim()) return;
    try {
      const n = await put<NoteInfo>("/api/notes/file", { title, content: `# ${title.trim()}\n\n` });
      list.reload();
      nav("notes", n.path);
    } catch (e: any) { toast({ title: "Could not create the note", text: e.message, bad: true }); }
  };

  return (
    <>
      <PageHeader section="Home folder" dot="--ws-setup" title="Notes"
                  sub="Markdown files in ~/Notes. You edit them here directly; the agent's edits arrive as staged changes for you to review."
                  actions={<button className="ee-btn outline" onClick={create}><FilePlus2 {...ICON_SM} /> New note</button>} />
      <SplitPanes id="notes" initial={360} left={
        <>
          <input className="input" style={{ marginBottom: 12 }} placeholder="Filter notes" aria-label="Filter notes" value={filter} onChange={(e) => setFilter(e.target.value)} />
          <div className="pane-scroll">
            {notes.length === 0 ? <Empty icon={<NotebookPen {...ICON} />}>No notes yet. Create one, or ask the agent to take a note.</Empty> : (
              <ul className="plain-list">
                {notes.map((n) => (
                  <li key={n.path}>
                    <button className="ee-item" style={{ background: "var(--soft)" }} aria-selected={current === n.path} onClick={() => nav("notes", n.path)}>
                      <span className="m"><b>{n.title}</b><small>{n.snippet || n.path}</small></span>
                      <span className="r">{ago(n.modified)}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </>
      } right={current ? <Editor key={current + bump} path={current} onSaved={list.reload} /> : <Empty icon={<NotebookPen {...ICON} />}>Choose a note.</Empty>} />
    </>
  );
}

function Editor({ path, onSaved }: { path: string; onSaved: () => void }) {
  const { nav, run } = useLive();
  const toast = useToast();
  const [note, setNote] = useState<NoteInfo | null>(null);
  const [text, setText] = useState("");
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    get<NoteInfo>(`/api/notes/file?path=${encodeURIComponent(path)}`).then((n) => { setNote(n); setText(n.content ?? ""); }).catch((e) => setErr(e.message));
  }, [path]);
  const dirty = note !== null && text !== note.content;
  const save = async () => {
    if (!note) return;
    try {
      const r = await put<NoteInfo>("/api/notes/file", { path, content: text, expected_sha256: note.sha256 });
      setNote({ ...note, ...r, content: text });
      setErr(null);
      toast({ title: "Saved", text: path });
      onSaved();
    } catch (e: any) { setErr(e.message); }
  };
  useEffect(() => {
    const k = (e: KeyboardEvent) => { if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); if (dirty) void save(); } };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  });
  if (!note) return <p className="muted">{err ?? "Loading…"}</p>;
  return (
    <div className="stack" style={{ flex: 1, minHeight: 0, gap: 12 }}>
      <div className="row" style={{ paddingRight: 40 }}>
        <div className="grow"><p className="note-title ellipsis">{note.title}</p><span className="muted mono">{path}</span></div>
      </div>
      <div className="row">
        <button className="ee-btn primary" disabled={!dirty} onClick={save}><Save {...ICON_SM} /> {dirty ? "Save" : "Saved"}</button>
        <button className="ee-btn outline" onClick={() => run(`What does my note ${path} say? Summarise it.`).then((t) => nav("tasks", t.pid))}><Sparkles {...ICON_SM} /> Ask about it</button>
        <button className="ee-btn ghost push" onClick={async () => {
          if (!confirm(`Move ${path} to the trash?`)) return;
          await api("DELETE", `/api/notes/file?path=${encodeURIComponent(path)}`);
          toast({ title: "Moved to trash", text: path }); onSaved(); nav("notes");
        }}><Trash2 {...ICON_SM} /> Delete</button>
      </div>
      {err && <Banner tone="bad">{err}</Banner>}
      <div className="guide" style={{ flex: 1, display: "flex", minHeight: 0, padding: 16 }}>
        <textarea className="note-editor" aria-label="Note text" value={text} onChange={(e) => setText(e.target.value)} spellCheck />
      </div>
    </div>
  );
}
