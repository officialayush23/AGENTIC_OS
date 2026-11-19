import { useEffect, useState } from "react";
import { CalendarDays, FileText, Globe, Mail, Network, NotebookPen, Search, Video } from "lucide-react";
import { blobUrl } from "../api";
import { useLive } from "../App";
import { Chip, ICON_SM, ago } from "../ui";

function Head({ icon, title, right }: { icon: any; title: string; right?: any }) {
  return <div className="rc-head"><span className="rc-ic">{icon}</span><b>{title}</b>{right && <span className="push">{right}</span>}</div>;
}

function Img({ src }: { src: string }) {
  const [u, setU] = useState<string | null>(null);
  useEffect(() => { let x: string | null = null; blobUrl(src).then((v) => { x = v; setU(v); }).catch(() => setU(null)); return () => { if (x) URL.revokeObjectURL(x); }; }, [src]);
  return u ? <img className="shot" src={u} alt="Screenshot" /> : null;
}

export function ResultCards({ cards }: { cards: any[] }) {
  const { nav } = useLive();
  if (!cards?.length) return null;
  return (
    <div className="rcards">
      {cards.map((c, i) => {
        switch (c.kind) {
          case "emails":
            return (
              <div key={i} className="rc">
                <Head icon={<Mail {...ICON_SM} />} title={`${c.items.length} email${c.items.length === 1 ? "" : "s"}`}
                      right={<button className="ee-btn ghost sm" onClick={() => nav("inbox")}>Open inbox</button>} />
                <ul className="rc-list">{c.items.map((m: any) => (
                  <li key={m.id} onClick={() => nav("inbox", m.id)}><span className="m"><b className="ellipsis">{m.subject}</b>
                    <small className="ellipsis">{(m.from || "").replace(/<.*>/, "").trim()} · {m.snippet}</small></span>
                    {!m.seen && <Chip tone="info">Unread</Chip>}<small className="muted">{ago(m.date)}</small></li>))}</ul>
              </div>
            );
          case "email":
            return (
              <div key={i} className="rc"><Head icon={<Mail {...ICON_SM} />} title={c.item.subject} />
                <p className="muted" style={{ margin: "0 0 6px" }}>{c.item.from}</p><p className="rc-body">{c.body}</p></div>
            );
          case "events":
            return (
              <div key={i} className="rc">
                <Head icon={<CalendarDays {...ICON_SM} />} title={`${c.items.length} event${c.items.length === 1 ? "" : "s"}`}
                      right={<button className="ee-btn ghost sm" onClick={() => nav("calendar")}>Calendar</button>} />
                <ul className="rc-list">{c.items.map((e: any, j: number) => (
                  <li key={e.uid ?? e.id ?? j}>
                    <span className="rc-date"><b>{new Date(e.start).toLocaleDateString([], { day: "numeric" })}</b>
                      <small>{new Date(e.start).toLocaleDateString([], { month: "short" })}</small></span>
                    <span className="m"><b>{e.title}</b><small>{new Date(e.start).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
                      {e.attendees?.length ? ` · ${e.attendees.length} invited` : ""}{e.source === "google" ? " · Google" : ""}</small></span>
                    {e.url && <a className="ee-btn outline sm" href={e.url} target="_blank" rel="noreferrer"><Video {...ICON_SM} /> Join</a>}
                  </li>))}</ul>
              </div>
            );
          case "file":
            return (
              <div key={i} className="rc"><Head icon={<FileText {...ICON_SM} />} title={c.path}
                right={c.path?.startsWith("~/Notes") ? <button className="ee-btn ghost sm" onClick={() => nav("notes", c.path)}><NotebookPen {...ICON_SM} /> Open</button> : null} />
                {c.text && <pre className="code" style={{ background: "var(--soft)" }}>{c.text}</pre>}</div>
            );
          case "hits":
            return (
              <div key={i} className="rc"><Head icon={<Search {...ICON_SM} />} title={`${c.items.length} passage${c.items.length === 1 ? "" : "s"}`} />
                {c.items.slice(0, 3).map((h: any, j: number) => (
                  <div key={j} style={{ marginTop: 8 }}><div className="mono muted">{h.path}:{h.start_line}–{h.end_line}</div>
                    <p className="rc-body">{(h.text || "").slice(0, 400)}</p></div>))}</div>
            );
          case "page":
            return (
              <div key={i} className="rc"><Head icon={<Globe {...ICON_SM} />} title={c.title || c.url}
                right={c.url ? <a className="ee-btn ghost sm" href={c.url} target="_blank" rel="noreferrer">Open</a> : null} />
                {c.text && <p className="rc-body">{c.text}</p>}</div>
            );
          case "pages":
            return (
              <div key={i} className="rc"><Head icon={<NotebookPen {...ICON_SM} />} title={`${c.items.length} Notion page${c.items.length === 1 ? "" : "s"}`} />
                <ul className="rc-list">{c.items.map((p: any) => (
                  <li key={p.id}><span className="m"><b>{p.title}</b><small>{ago(p.last_edited)}</small></span>
                    {p.url && <a className="ee-btn outline sm" href={p.url} target="_blank" rel="noreferrer">Open</a>}</li>))}</ul></div>
            );
          case "image":
            return <div key={i} className="rc"><Head icon={<Globe {...ICON_SM} />} title={c.title || "Screenshot"} /><Img src={c.src} /></div>;
          case "graph":
            return (
              <div key={i} className="rc"><Head icon={<Network {...ICON_SM} />} title="Knowledge graph"
                right={<button className="ee-btn outline sm" onClick={() => nav("graph")}>Explore</button>} />
                <div className="rc-stats"><div><b>{c.stats.nodes}</b><small>entities</small></div><div><b>{c.stats.edges}</b><small>links</small></div>
                  <div><b>{c.stats.documents}</b><small>documents</small></div></div></div>
            );
          case "graph_nodes":
            return (
              <div key={i} className="rc"><Head icon={<Network {...ICON_SM} />} title="In the knowledge graph" />
                <div className="chips">{c.items.map((n: any) => (
                  <button key={n.id} className="suggest" onClick={() => nav("graph", n.id)}>{n.label} <span className="muted">· {n.type}</span></button>))}</div></div>
            );
          default:
            return null;
        }
      })}
    </div>
  );
}
