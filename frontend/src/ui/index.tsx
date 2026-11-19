// Eagle Eye components for React. Thin wrappers over the ee-* classes; no colours outside tokens.
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { ChevronDown, Maximize2, Minimize2, X } from "lucide-react";
import type { Task } from "../types";

export const ICON = { size: 18, strokeWidth: 1.6 } as const;
export const ICON_SM = { size: 16, strokeWidth: 1.6 } as const;

export type Tone = "pass" | "fail" | "review" | "info" | "nc" | "phys" | "retake";

export function Chip({ tone, children, dot = true }: { tone: Tone; children: ReactNode; dot?: boolean }) {
  return <span className={`ee-chip ${tone}`}>{dot && <i aria-hidden />}{children}</span>;
}

/** Plain-language status of a task: a colour and, always, a word. */
export function taskStatus(t: Task): { tone: Tone; word: string; next: string } {
  const p = t.pending_user;
  if (p?.kind === "approval") return { tone: "review", word: "Needs approval", next: p.effect || "Decide whether to allow the next step" };
  if (p?.kind === "clarify") return { tone: "review", word: "Question", next: "Answer the agent's question" };
  if (p?.kind === "review") {
    if (p.outcome === "conflict") return { tone: "fail", word: "Conflict", next: "Files changed on disk while it ran" };
    if (p.outcome === "commit_incomplete") return { tone: "fail", word: "Commit incomplete", next: "Retry the commit" };
    const n = p.files.length;
    return { tone: "review", word: "Ready to review", next: n ? `Review ${n} change${n === 1 ? "" : "s"}, then commit` : "Nothing to commit" };
  }
  if (t.state === "STOPPED") return { tone: "nc", word: "Paused", next: "Resume when ready" };
  if (t.state !== "EXITED") {
    const total = t.plan?.steps.length ?? 0;
    const done = Object.values(t.steps_state || {}).filter((s) => s.status === "done").length;
    return { tone: "info", word: "Running", next: total ? `Step ${Math.min(done + 1, total)} of ${total}` : "Planning" };
  }
  if (t.status === "undone") return { tone: "nc", word: "Undone", next: "Changes were rolled back" };
  const code = t.exit?.code;
  if (code === "completed") return { tone: "pass", word: "Done", next: t.txn_id ? "Committed. Undo is available" : "Finished" };
  if (code === "discarded") return { tone: "nc", word: "Discarded", next: "Nothing was written" };
  if (code === "cancelled" || code === "killed") return { tone: "nc", word: "Cancelled", next: t.exit?.reason ?? "" };
  return { tone: "fail", word: "Failed", next: t.exit?.reason ?? "" };
}

export function TaskChip({ task }: { task: Task }) {
  const s = taskStatus(task);
  return <Chip tone={s.tone}>{s.word}</Chip>;
}

export function Card({ children, className = "", onClick, as: As = "section" }: {
  children: ReactNode; className?: string; onClick?: () => void; as?: any;
}) {
  return <As className={`ee-card ${onClick ? "click" : ""} ${className}`} onClick={onClick}>{children}</As>;
}

export function CardHead({ title, sub, right }: { title: ReactNode; sub?: ReactNode; right?: ReactNode }) {
  return (
    <div className="card-head">
      <div className="grow">
        <h2 className="ee-h2">{title}</h2>
        {sub && <p className="muted">{sub}</p>}
      </div>
      {right}
    </div>
  );
}

export function PageHeader({ section, dot, title, sub, actions }: {
  section: string; dot: string; title: ReactNode; sub?: ReactNode; actions?: ReactNode;
}) {
  return (
    <header className="ee-ph">
      <div>
        <span className="ws"><i style={{ background: `var(${dot})` }} />{section}</span>
        <h1>{title}</h1>
        {sub && <p>{sub}</p>}
      </div>
      {actions && <div className="row">{actions}</div>}
    </header>
  );
}

export function Kpi({ label, value, hint, icon, inverted }: {
  label: string; value: ReactNode; hint?: ReactNode; icon: ReactNode; inverted?: boolean;
}) {
  return (
    <div className={`ee-card ee-kpi ${inverted ? "inv" : ""}`}>
      <div className="top"><span>{label}</span><span className="ic">{icon}</span></div>
      <div className="num">{value}</div>
      {hint && <div className="muted">{hint}</div>}
    </div>
  );
}

export function Seg<T extends string>({ value, onChange, items, compact, label }: {
  value: T; onChange: (v: T) => void; items: { id: T; label: string; count?: number }[]; compact?: boolean; label: string;
}) {
  return (
    <div className={`ee-seg ${compact ? "compact" : ""}`} role="tablist" aria-label={label}>
      {items.map((it) => (
        <button key={it.id} role="tab" aria-selected={value === it.id} onClick={() => onChange(it.id)}>
          {it.label}{it.count !== undefined && <span>{it.count}</span>}
        </button>
      ))}
    </div>
  );
}

export function Filter({ on, onClick, children, count }: { on: boolean; onClick: () => void; children: ReactNode; count?: number }) {
  return (
    <button className="ee-filter" aria-pressed={on} onClick={onClick}>
      {children}{count !== undefined && <span>{count}</span>}
    </button>
  );
}

export function Group({ name, color, count, children, defaultOpen = true }: {
  name: string; color: string; count: number; children: ReactNode; defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="ee-group">
      <button aria-expanded={open} onClick={() => setOpen(!open)}>
        <i style={{ background: `var(${color})` }} />{name} <em>{count}</em>
        <ChevronDown className="chev" {...ICON_SM} />
      </button>
      {open && <ul>{children}</ul>}
    </div>
  );
}

export function Switch({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <label className="check">
      <button type="button" className="switch" role="switch" aria-checked={checked} aria-label={label}
              onClick={() => onChange(!checked)} />
      <span>{label}</span>
    </label>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return <label className="field"><span>{label}</span>{children}{hint && <small>{hint}</small>}</label>;
}

export function Empty({ icon, children, action }: { icon: ReactNode; children: ReactNode; action?: ReactNode }) {
  return <div className="empty"><span className="ic">{icon}</span><div>{children}</div>{action}</div>;
}

export function Banner({ tone, icon, children }: { tone: "note" | "info" | "bad" | "good"; icon?: ReactNode; children: ReactNode }) {
  return <div className={`banner ${tone}`} role={tone === "bad" ? "alert" : "status"}>{icon}<div>{children}</div></div>;
}

/** Resizable master / detail panes; each pane scrolls on its own; widths saved per screen. */
export function SplitPanes({ id, left, right, initial = 420 }: { id: string; left: ReactNode; right: ReactNode; initial?: number }) {
  const key = `aos.split.${id}`;
  const [w, setW] = useState<number>(() => {
    try { return Number(localStorage.getItem(key)) || initial; } catch { return initial; }
  });
  const [full, setFull] = useState(false);
  const drag = useRef<{ x: number; w: number } | null>(null);
  useEffect(() => {
    const move = (e: PointerEvent) => {
      if (!drag.current) return;
      setW(Math.max(280, Math.min(window.innerWidth * 0.6, drag.current.w + e.clientX - drag.current.x)));
    };
    const up = () => { drag.current = null; };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    return () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); };
  }, []);
  useEffect(() => { try { localStorage.setItem(key, String(Math.round(w))); } catch { /* storage unavailable */ } }, [key, w]);
  useEffect(() => {
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setFull(false); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, []);
  return (
    <div className="ee-split">
      <div className="ee-pane" style={{ width: w, flexShrink: 0 }}>{left}</div>
      <button className="ee-divider" aria-label="Resize panes"
              onPointerDown={(e) => { drag.current = { x: e.clientX, w }; }}
              onKeyDown={(e) => { if (e.key === "ArrowLeft") setW((x) => Math.max(280, x - 24)); if (e.key === "ArrowRight") setW((x) => x + 24); }} />
      <div className={`ee-pane fill ${full ? "pane-full" : ""}`}>
        <div className="row" style={{ justifyContent: "flex-end", marginBottom: -36, position: "relative", zIndex: 2 }}>
          <button className="ee-btn ghost round sm" aria-label={full ? "Exit full screen" : "Expand pane"} onClick={() => setFull(!full)}>
            {full ? <Minimize2 {...ICON_SM} /> : <Maximize2 {...ICON_SM} />}
          </button>
        </div>
        {right}
      </div>
    </div>
  );
}

export function Dialog({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);
  return (
    <div className="scrim" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="dialog" role="dialog" aria-modal="true" aria-label={title}>
        <div className="row nowrap">
          <h2 className="grow">{title}</h2>
          <button className="ee-btn ghost round" aria-label="Close" onClick={onClose}><X {...ICON} /></button>
        </div>
        {children}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ toasts
type Toast = { id: number; title: string; text?: string; bad?: boolean };
const ToastCtx = createContext<(t: Omit<Toast, "id">) => void>(() => undefined);
export const useToast = () => useContext(ToastCtx);

export function ToastHost({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const push = useCallback((t: Omit<Toast, "id">) => {
    const id = Date.now() + Math.random();
    setItems((x) => [...x.slice(-3), { ...t, id }]);
    setTimeout(() => setItems((x) => x.filter((i) => i.id !== id)), t.bad ? 7000 : 4000);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" aria-live="polite">
        {items.map((t) => (
          <div key={t.id} className={`toast ${t.bad ? "bad" : ""}`}>
            <div><b>{t.title}</b>{t.text && <small>{t.text}</small>}</div>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

// ------------------------------------------------------------------ helpers
export function ago(input?: string | number | null): string {
  if (input === undefined || input === null) return "";
  const t = typeof input === "number" ? input * (input < 1e12 ? 1000 : 1) : Date.parse(input);
  if (!Number.isFinite(t)) return "";
  const s = Math.round((Date.now() - t) / 1000);
  const abs = Math.abs(s);
  const fmt = (n: number, u: string) => `${n} ${u}${n === 1 ? "" : "s"}`;
  const v = abs < 60 ? "just now" : abs < 3600 ? fmt(Math.round(abs / 60), "min") : abs < 86400 ? fmt(Math.round(abs / 3600), "hour") : fmt(Math.round(abs / 86400), "day");
  if (v === "just now") return v;
  return s >= 0 ? `${v} ago` : `in ${v}`;
}

export function when(epoch?: number | null): string {
  if (!epoch) return "";
  const d = new Date(epoch * 1000);
  return d.toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit", month: "short", day: "numeric" });
}

/** Compact "Thu 08:30" (or "Today 08:30") for small stat tiles. */
export function shortWhen(epoch?: number | null): string {
  if (!epoch) return "—";
  const d = new Date(epoch * 1000);
  const now = new Date();
  const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return `Today ${t}`;
  const tm = new Date(now); tm.setDate(now.getDate() + 1);
  if (d.toDateString() === tm.toDateString()) return `Tomorrow ${t}`;
  return `${d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })} ${t}`;
}

export function greeting(): string {
  const h = new Date().getHours();
  return h < 5 ? "Working late" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]): { data: T | null; error: string | null; reload: () => void; loading: boolean } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [n, setN] = useState(0);
  useEffect(() => {
    let alive = true;
    setLoading(true);
    fn().then((d) => { if (alive) { setData(d); setError(null); } })
      .catch((e) => { if (alive) setError(e?.message ?? String(e)); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, n]);
  return { data, error, loading, reload: () => setN((x) => x + 1) };
}
