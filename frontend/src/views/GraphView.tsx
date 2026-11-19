import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Network, RefreshCw, Search, Sparkles, X } from "lucide-react";
import { get, post } from "../api";
import { useLive } from "../App";
import { Banner, Card, Empty, ICON, ICON_SM, PageHeader, useToast } from "../ui";

type N = { id: string; label: string; type: string; mentions: number; meta: any; x?: number; y?: number; vx?: number; vy?: number; deg?: number };
type E = { src: string; dst: string; rel: string; weight: number; source?: string };

export const TYPE_COLOR: Record<string, string> = {
  document: "--ws-setup", person: "--lc-insp", concept: "--phys", topic: "--lc-review", tag: "--nc",
  module: "--ws-closeout", site: "--lc-reinsp", event: "--lc-punch", organization: "--ws-review", project: "--lc-ready",
};
const TYPE_WORD: Record<string, string> = {
  document: "Documents", person: "People", concept: "Concepts", topic: "Topics", tag: "Tags", module: "Code modules",
  site: "Websites", event: "Events", organization: "Organizations", project: "Projects",
};

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
}

export function GraphView() {
  const { sel, nav, bump } = useLive();
  const toast = useToast();
  const [data, setData] = useState<{ nodes: N[]; edges: E[]; stats: any; job: any } | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [q, setQ] = useState("");
  const [detail, setDetail] = useState<any>(null);
  const [focus, setFocus] = useState<string | null>(null);

  const load = useCallback(async () => {
    setData(await get(`/api/kg?limit=400${focus ? `&focus=${encodeURIComponent(focus)}` : ""}`));
  }, [focus]);
  useEffect(() => { void load(); }, [load, bump]);
  useEffect(() => { if (sel) void open(sel); }, [sel]); // eslint-disable-line react-hooks/exhaustive-deps

  const open = async (id: string) => {
    try { setDetail(await get(`/api/kg/node?id=${encodeURIComponent(id)}`)); } catch { setDetail(null); }
  };
  const build = async (deep: boolean) => {
    try { await post("/api/kg/build", { deep }); toast({ title: deep ? "Deep build started" : "Building the graph" }); void load(); }
    catch (e: any) { toast({ title: "Couldn't build", text: e.message, bad: true }); }
  };
  const find = async () => {
    if (!q.trim()) return;
    const r = await get<{ matches: any[] }>(`/api/kg/search?q=${encodeURIComponent(q)}`);
    if (r.matches[0]) { void open(r.matches[0].id); setFocus(r.matches[0].id); }
    else toast({ title: "Not in the graph", text: q });
  };

  const types = useMemo(() => Object.entries(data?.stats?.types ?? {}).sort((a: any, b: any) => b[1] - a[1]), [data]);
  const job = data?.job;
  return (
    <>
      <PageHeader section="Memory" dot="--ws-review" title="Knowledge graph"
                  sub="People, projects, concepts, topics and documents from your files, notes, email and calendar, and how they connect."
                  actions={<>
                    <button className="ee-btn outline" onClick={() => build(true)}><Sparkles {...ICON_SM} /> Deep build with AI</button>
                    <button className="ee-btn primary" onClick={() => build(false)}><RefreshCw {...ICON_SM} /> {data?.stats?.nodes ? "Rebuild" : "Build graph"}</button>
                  </>} />
      {job?.running && <Banner tone="info" icon={<Network {...ICON_SM} />}><p>Building… {job.progress?.stage === "deep" ? `AI reading document ${job.progress.done} of ${job.progress.total}` : ""}</p></Banner>}
      {job?.error && <Banner tone="bad">{job.error}</Banner>}
      <div className="kpis">
        {[["Entities", data?.stats?.nodes ?? 0], ["Links", data?.stats?.edges ?? 0], ["Kinds", types.length],
          ["Built", data?.stats?.built_at ? new Date(data.stats.built_at).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "Never"]]
          .map(([l, v]) => <div key={l as string} className="ee-card ee-kpi"><div className="top"><span>{l}</span></div><div className="num" style={{ fontSize: typeof v === "string" ? 26 : undefined }}>{v}</div></div>)}
      </div>
      <Card>
        <div className="row" style={{ marginBottom: 12 }}>
          <form className="row nowrap" style={{ flex: "1 1 320px" }} onSubmit={(e) => { e.preventDefault(); void find(); }}>
            <input className="input" placeholder="Find a person, project or concept" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Find in graph" />
            <button className="ee-btn secondary round" aria-label="Find"><Search {...ICON_SM} /></button>
          </form>
          {focus && <button className="ee-btn outline sm" onClick={() => { setFocus(null); setDetail(null); }}><X {...ICON_SM} /> Show everything</button>}
        </div>
        <div className="chips" style={{ marginBottom: 12 }}>
          {types.map(([t, n]: any) => (
            <button key={t} className="suggest" aria-pressed={!hidden.has(t)} style={{ opacity: hidden.has(t) ? 0.45 : 1 }}
                    title={hidden.has(t) ? "Show" : "Hide"} onClick={() => { const h = new Set(hidden); h.has(t) ? h.delete(t) : h.add(t); setHidden(h); }}>
              <i className="dot" style={{ background: `var(${TYPE_COLOR[t] ?? "--nc"})` }} /> {TYPE_WORD[t] ?? t} <span className="muted num">{n}</span>
            </button>
          ))}
        </div>
        {!data ? <p className="muted">Loading…</p> : data.nodes.length === 0 ? (
          <Empty icon={<Network {...ICON} />} action={<button className="ee-btn outline" onClick={() => build(false)}>Build the graph</button>}>
            No graph yet. Index your files in Knowledge, then build it.
          </Empty>
        ) : (
          <div className="graph-wrap">
            <ForceGraph nodes={data.nodes.filter((n) => !hidden.has(n.type))} edges={data.edges} selected={detail?.id} onPick={(id) => { void open(id); nav("graph", id); }} />
            {detail && <NodePanel d={detail} onClose={() => { setDetail(null); nav("graph"); }} onFocus={(id) => setFocus(id)} onOpen={open} />}
          </div>
        )}
      </Card>
    </>
  );
}

function NodePanel({ d, onClose, onFocus, onOpen }: { d: any; onClose: () => void; onFocus: (id: string) => void; onOpen: (id: string) => void }) {
  const { nav } = useLive();
  const links = [...d.out.map((x: any) => ({ ...x, dir: "out" })), ...d.in.map((x: any) => ({ ...x, dir: "in" }))];
  return (
    <aside className="graph-panel">
      <div className="row nowrap"><i className="dot lg" style={{ background: `var(${TYPE_COLOR[d.type] ?? "--nc"})` }} />
        <span className="muted">{TYPE_WORD[d.type] ?? d.type}</span>
        <button className="ee-btn ghost round sm push" aria-label="Close" onClick={onClose}><X {...ICON_SM} /></button></div>
      <h3 style={{ margin: "6px 0 2px", fontSize: 20, fontWeight: 500, overflowWrap: "anywhere" }}>{d.label}</h3>
      <p className="muted" style={{ margin: 0 }}>{d.mentions} mention{d.mentions === 1 ? "" : "s"} · {links.length} link{links.length === 1 ? "" : "s"}</p>
      <div className="row" style={{ margin: "10px 0" }}>
        <button className="ee-btn outline sm" onClick={() => onFocus(d.id)}>Focus</button>
        {d.meta?.path?.startsWith("~/Notes") && <button className="ee-btn outline sm" onClick={() => nav("notes", d.meta.path)}>Open note</button>}
        {d.meta?.email && <button className="ee-btn outline sm" onClick={() => nav("inbox")}>Mail</button>}
      </div>
      <div className="pane-scroll" style={{ maxHeight: 420 }}>
        <ul className="plain-list">
          {links.slice(0, 60).map((l: any, i: number) => (
            <li key={i} className="plain-row" style={{ cursor: "pointer", minHeight: 44 }} onClick={() => onOpen(l.id)}>
              <i className="dot" style={{ background: `var(${TYPE_COLOR[l.type] ?? "--nc"})` }} />
              <span className="m"><b className="ellipsis" style={{ fontSize: 13 }}>{l.label}</b>
                <small>{l.dir === "out" ? "" : "← "}{l.rel.replace(/_/g, " ")}</small></span>
            </li>
          ))}
        </ul>
        {d.sources.length > 0 && <><p className="muted" style={{ marginBottom: 4 }}>Evidence from</p>
          <div className="chips">{d.sources.slice(0, 12).map((s: string) => <span key={s} className="tag">{s}</span>)}</div></>}
      </div>
    </aside>
  );
}

/** A small force-directed layout drawn on a canvas, with pan, zoom, hover and click. */
function ForceGraph({ nodes, edges, selected, onPick }: { nodes: N[]; edges: E[]; selected?: string; onPick: (id: string) => void }) {
  const cv = useRef<HTMLCanvasElement | null>(null);
  const state = useRef({ nodes: [] as N[], edges: [] as E[], view: { x: 0, y: 0, k: 1 }, hover: null as N | null, alpha: 1, drag: null as any });
  const raf = useRef(0);

  useEffect(() => {
    const prev = new Map(state.current.nodes.map((n) => [n.id, n]));
    const deg = new Map<string, number>();
    const ids = new Set(nodes.map((n) => n.id));
    const es = edges.filter((e) => ids.has(e.src) && ids.has(e.dst));
    es.forEach((e) => { deg.set(e.src, (deg.get(e.src) ?? 0) + 1); deg.set(e.dst, (deg.get(e.dst) ?? 0) + 1); });
    state.current.nodes = nodes.map((n, i) => {
      const p = prev.get(n.id);
      const a = (i / Math.max(1, nodes.length)) * Math.PI * 2 * 7;
      return { ...n, deg: deg.get(n.id) ?? 0, x: p?.x ?? Math.cos(a) * (40 + i * 1.5), y: p?.y ?? Math.sin(a) * (40 + i * 1.5), vx: 0, vy: 0 };
    });
    state.current.edges = es;
    state.current.alpha = 1;
  }, [nodes, edges]);

  useEffect(() => {
    const c = cv.current!;
    const ctx = c.getContext("2d")!;
    const colors: Record<string, string> = {};
    const color = (t: string) => (colors[t] ??= cssVar(TYPE_COLOR[t] ?? "--nc"));
    const ink = cssVar("--ink"), ink3 = cssVar("--ink-3"), line = cssVar("--line"), ring = cssVar("--ring"), card = cssVar("--card");
    const tick = () => {
      const s = state.current;
      const ns = s.nodes;
      const byId = new Map(ns.map((n) => [n.id, n]));
      if (s.alpha > 0.02) {
        for (let i = 0; i < ns.length; i++) {
          const a = ns[i];
          for (let j = i + 1; j < ns.length; j++) {
            const b = ns[j];
            let dx = a.x! - b.x!, dy = a.y! - b.y!;
            let d2 = dx * dx + dy * dy || 0.01;
            if (d2 > 90000) continue;
            const f = (900 * s.alpha) / d2;
            dx *= f; dy *= f;
            a.vx! += dx; a.vy! += dy; b.vx! -= dx; b.vy! -= dy;
          }
        }
        for (const e of s.edges) {
          const a = byId.get(e.src)!, b = byId.get(e.dst)!;
          const dx = b.x! - a.x!, dy = b.y! - a.y!;
          const d = Math.sqrt(dx * dx + dy * dy) || 1;
          const f = ((d - 70) / d) * 0.04 * s.alpha * Math.min(2, e.weight);
          a.vx! += dx * f; a.vy! += dy * f; b.vx! -= dx * f; b.vy! -= dy * f;
        }
        for (const n of ns) {
          if (s.drag?.node === n) continue;
          n.vx! -= n.x! * 0.002 * s.alpha; n.vy! -= n.y! * 0.002 * s.alpha;
          n.x! += Math.max(-20, Math.min(20, n.vx!)); n.y! += Math.max(-20, Math.min(20, n.vy!));
          n.vx! *= 0.6; n.vy! *= 0.6;
        }
        s.alpha *= 0.985;
      }
      const dpr = window.devicePixelRatio || 1;
      const w = c.clientWidth, h = c.clientHeight;
      if (c.width !== w * dpr || c.height !== h * dpr) { c.width = w * dpr; c.height = h * dpr; }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      ctx.translate(w / 2 + s.view.x, h / 2 + s.view.y);
      ctx.scale(s.view.k, s.view.k);
      const hot = s.hover?.id ?? selected;
      ctx.lineWidth = 1 / s.view.k;
      for (const e of s.edges) {
        const a = byId.get(e.src)!, b = byId.get(e.dst)!;
        const on = hot && (e.src === hot || e.dst === hot);
        ctx.strokeStyle = on ? ink3 : line;
        ctx.globalAlpha = on ? 0.9 : 0.7;
        ctx.beginPath(); ctx.moveTo(a.x!, a.y!); ctx.lineTo(b.x!, b.y!); ctx.stroke();
      }
      ctx.globalAlpha = 1;
      for (const n of ns) {
        const r = 3.5 + Math.min(10, Math.sqrt(n.deg! + n.mentions) * 1.4);
        ctx.fillStyle = color(n.type);
        ctx.beginPath(); ctx.arc(n.x!, n.y!, r, 0, Math.PI * 2); ctx.fill();
        if (n.id === hot) { ctx.strokeStyle = ring; ctx.lineWidth = 2.5 / s.view.k; ctx.stroke(); }
        const showLabel = n.id === hot || n.deg! >= 4 || s.view.k > 1.6;
        if (showLabel) {
          const fs = 11 / Math.max(0.7, s.view.k);
          ctx.font = `${n.id === hot ? 600 : 500} ${fs}px "Segoe UI Variable Text", system-ui, sans-serif`;
          const label = n.label.length > 28 ? n.label.slice(0, 27) + "…" : n.label;
          ctx.fillStyle = card; ctx.globalAlpha = 0.8;
          const tw = ctx.measureText(label).width;
          ctx.fillRect(n.x! + r + 2, n.y! - fs * 0.7, tw + 4, fs * 1.3);
          ctx.globalAlpha = 1; ctx.fillStyle = ink;
          ctx.fillText(label, n.x! + r + 4, n.y! + fs * 0.35);
        }
      }
      raf.current = requestAnimationFrame(tick);
    };
    raf.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf.current);
  }, [selected]);

  const toWorld = (ev: { clientX: number; clientY: number }) => {
    const c = cv.current!.getBoundingClientRect();
    const s = state.current;
    return { x: (ev.clientX - c.left - c.width / 2 - s.view.x) / s.view.k, y: (ev.clientY - c.top - c.height / 2 - s.view.y) / s.view.k };
  };
  const hit = (p: { x: number; y: number }) => {
    let best: N | null = null, bd = 14 / state.current.view.k;
    for (const n of state.current.nodes) { const d = Math.hypot(n.x! - p.x, n.y! - p.y); if (d < bd) { bd = d; best = n; } }
    return best;
  };
  return (
    <canvas ref={cv} className="graph-canvas" role="img" aria-label="Knowledge graph"
            onWheel={(e) => { const s = state.current; s.view.k = Math.max(0.2, Math.min(4, s.view.k * (e.deltaY < 0 ? 1.1 : 0.9))); }}
            onPointerDown={(e) => { const n = hit(toWorld(e)); state.current.drag = { node: n, x: e.clientX, y: e.clientY, moved: false }; (e.target as Element).setPointerCapture(e.pointerId); }}
            onPointerMove={(e) => {
              const s = state.current;
              if (s.drag) {
                s.drag.moved = s.drag.moved || Math.hypot(e.clientX - s.drag.x, e.clientY - s.drag.y) > 3;
                if (s.drag.node) { const p = toWorld(e); s.drag.node.x = p.x; s.drag.node.y = p.y; s.alpha = Math.max(s.alpha, 0.3); }
                else { s.view.x += e.clientX - s.drag.x; s.view.y += e.clientY - s.drag.y; s.drag.x = e.clientX; s.drag.y = e.clientY; }
              } else { s.hover = hit(toWorld(e)); (e.target as HTMLCanvasElement).style.cursor = s.hover ? "pointer" : "grab"; }
            }}
            onPointerUp={() => { const d = state.current.drag; state.current.drag = null; if (d?.node && !d.moved) onPick(d.node.id); }} />
  );
}
