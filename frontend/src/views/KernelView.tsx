import { useEffect, useState } from "react";
import { Cpu, GitFork, KeyRound, Pause, Play, ScrollText, ShieldCheck, Skull, Timer, Undo2, X, Zap } from "lucide-react";
import { get, post } from "../api";
import { useLive } from "../App";
import { Card, CardHead, Chip, Empty, ICON_SM, Kpi, PageHeader, ago, useAsync, useToast } from "../ui";
import type { Tone } from "../ui";

/** Kernel monitor: the OS inside Agentic OS, made visible. A live `ps`/`top`, the run queue,
 *  per-process capability tables, the transaction journal, the kernel log, and one-click demos. */

const STATE_TONE: Record<string, Tone> = {
  RUNNING: "info", READY: "info", BLOCKED_IO: "review", BLOCKED_USER: "review", BLOCKED_WAIT: "review",
  STOPPED: "nc", ZOMBIE: "nc", EXITED: "pass",
};
const STATE_WORD: Record<string, string> = {
  RUNNING: "Running", READY: "Ready", BLOCKED_IO: "Blocked: I/O", BLOCKED_USER: "Blocked: user", BLOCKED_WAIT: "Blocked: wait()",
  STOPPED: "Stopped", ZOMBIE: "Zombie", EXITED: "Exited",
};
const PRIO = ["boosted", "normal", "background"];

const DEMOS: { id: string; title: string; icon: any; shows: string; try: string }[] = [
  { id: "transaction", title: "Transaction", icon: ScrollText, shows: "Copy-on-write overlay, review, two-phase commit, undo from the journal",
    try: "Open the task, look at the diff, Commit, then Undo. Watch the Journal." },
  { id: "fork", title: "fork / wait", icon: GitFork, shows: "Parallel read-only steps become child processes with attenuated capabilities",
    try: "Watch children appear under the parent, the parent sit in wait(), then get reaped." },
  { id: "signals", title: "Signals", icon: Pause, shows: "STOP / CONT / TERM / KILL on a 30-second job, and crash recovery",
    try: "Pause and Resume it here. Or close the server window mid-way and start it again: it resumes." },
  { id: "capability", title: "Capabilities", icon: KeyRound, shows: "A syscall without a capability blocks; approving creates a one-shot grant",
    try: "Approve once, then look at its capability table: app:launch, uses_left 1." },
  { id: "rlimit", title: "rlimits", icon: Timer, shows: "A budget of 2 step executions for a 5-step plan",
    try: "The kernel terminates it with 'budget used up'. Nothing is committed." },
  { id: "scheduler", title: "Scheduler", icon: Cpu, shows: "Three processes share one kernel loop; one phase per time slice",
    try: "Watch states interleave in the table and the slice counters grow." },
];

export function KernelView() {
  const { bump, nav } = useLive();
  const toast = useToast();
  const [tick, setTick] = useState(0);
  const [sel, setSel] = useState<string | null>(null);
  const k = useAsync(() => get<any>("/api/kernel"), [bump, tick]);
  const procs: any[] = k.data?.processes ?? [];
  const live = procs.filter((p) => !["EXITED", "ZOMBIE"].includes(p.state));

  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), live.length ? 800 : 4000);
    return () => clearInterval(t);
  }, [live.length]);

  const run = async (id: string) => {
    try {
      const r = await post<{ pids: string[] }>(`/api/kernel/demo/${id}`);
      setSel(r.pids[0]);
      toast({ title: `Started the ${id} demo` });
      setTick((n) => n + 1);
    } catch (e: any) { toast({ title: "Could not start the demo", text: e?.message, bad: true }); }
  };
  const signal = async (pid: string, s: string) => {
    try { await post(`/api/tasks/${pid}/signal`, { signal: s }); toast({ title: `Sent ${s}` }); setTick((n) => n + 1); }
    catch (e: any) { toast({ title: "Signal failed", text: e?.message, bad: true }); }
  };
  const undo = async (pid: string) => {
    if (!confirm("Undo this transaction? Files return to their state before the commit.")) return;
    try { await post(`/api/tasks/${pid}/undo`); toast({ title: "Undone from the journal" }); setTick((n) => n + 1); }
    catch (e: any) { toast({ title: "Undo failed", text: e?.message, bad: true }); }
  };

  // process tree: roots first, children indented under their parent
  const roots = procs.filter((p) => !p.ppid);
  const kids = (pid: string) => procs.filter((p) => p.ppid === pid);
  const ordered: { p: any; depth: number }[] = [];
  const add = (p: any, d: number) => { ordered.push({ p, depth: d }); kids(p.pid).forEach((c) => add(c, d + 1)); };
  roots.slice().reverse().forEach((p) => add(p, 0));
  const selected = procs.find((p) => p.pid === sel) ?? null;
  const c = k.data?.counters ?? {};

  return (
    <>
      <PageHeader section="System" dot="--ws-system" title="Kernel monitor"
                  sub="The OS inside Agentic OS: processes, scheduler, capabilities, transactions and the kernel log, live." />
      <div className="kpis">
        <Kpi label="Live processes" value={k.data ? live.length : "–"} icon={<Cpu {...ICON_SM} />} inverted={live.length > 0}
             hint={`${procs.length} in the process table`} />
        <Kpi label="Syscalls" value={c.syscalls ?? "–"} icon={<Zap {...ICON_SM} />} hint="Tool calls through the capability + policy gate" />
        <Kpi label="fork / signals" value={k.data ? `${c.forks} / ${c.signals}` : "–"} icon={<GitFork {...ICON_SM} />} hint={`${c.recoveries ?? 0} crash recoveries`} />
        <Kpi label="Commits / undos" value={k.data ? `${c.commits} / ${c.undos}` : "–"} icon={<ShieldCheck {...ICON_SM} />}
             hint={`${c.grants ?? 0} grants · ${c.denials ?? 0} denials · ${c.rlimit_kills ?? 0} rlimit kills`} />
      </div>

      <Card>
        <CardHead title="Demonstrate an OS mechanism" sub="Each button spawns a scripted process (no AI needed) that exercises one kernel feature." />
        <div className="kdemos">
          {DEMOS.map((d) => (
            <button key={d.id} className="kdemo" onClick={() => void run(d.id)}>
              <span className="kd-ic"><d.icon {...ICON_SM} /></span>
              <b>{d.title}</b>
              <small>{d.shows}</small>
              <em>{d.try}</em>
            </button>
          ))}
        </div>
      </Card>

      <div className="two" style={{ marginTop: 16 }}>
        <Card>
          <CardHead title="Process table" sub="ps: one process per task; children are forked steps" />
          {!ordered.length ? <Empty icon={<Cpu {...ICON_SM} />}>No processes yet. Start a demo above.</Empty> : (
            <div className="ktable-wrap">
              <table className="ktable">
                <thead><tr><th>PID</th><th>Process</th><th>State</th><th>Phase</th><th>Prio</th><th>Slices</th><th>Steps</th></tr></thead>
                <tbody>
                  {ordered.slice(0, 40).map(({ p, depth }) => (
                    <tr key={p.pid} className={p.pid === sel ? "on" : ""} onClick={() => setSel(p.pid)}>
                      <td className="mono">{p.pid.slice(-6)}</td>
                      <td className="kt-title" style={{ paddingLeft: 8 + depth * 18 }} title={p.title}>
                        {depth ? `└ ${String(p.title).split(" › ").pop()}` : p.title}</td>
                      <td><Chip tone={STATE_TONE[p.state] ?? "nc"}>{STATE_WORD[p.state] ?? p.state}</Chip></td>
                      <td className="mono">{p.phase}</td>
                      <td>{PRIO[p.priority] ?? p.priority}</td>
                      <td className="mono">{p.slices}</td>
                      <td className="mono">{p.steps_done}/{p.steps_total}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <Card>
          {!selected ? (
            <>
              <CardHead title="Process details" sub="Select a process to see its PCB, capabilities and budget" />
              <Empty icon={<KeyRound {...ICON_SM} />}>Nothing selected.</Empty>
            </>
          ) : (
            <>
              <CardHead title={selected.title} sub={<span className="mono">pid {selected.pid}{selected.ppid ? ` · parent ${selected.ppid}` : ""}</span>}
                right={<button className="ee-btn ghost sm" onClick={() => nav("tasks", selected.ppid || selected.pid)}>Open task</button>} />
              <div className="chips" style={{ marginBottom: 10 }}>
                <Chip tone={STATE_TONE[selected.state] ?? "nc"}>{STATE_WORD[selected.state] ?? selected.state}</Chip>
                <span className="tag">phase {selected.phase}</span>
                <span className="tag">{PRIO[selected.priority]} priority</span>
                {selected.exit && <span className="tag">exit: {selected.exit.code}{selected.exit.reason ? ` (${selected.exit.reason})` : ""}</span>}
              </div>
              {!["EXITED", "ZOMBIE"].includes(selected.state) && (
                <div className="chips" style={{ marginBottom: 12 }}>
                  {selected.state !== "STOPPED"
                    ? <button className="ee-btn outline sm" onClick={() => void signal(selected.pid, "STOP")}><Pause {...ICON_SM} /> STOP</button>
                    : <button className="ee-btn outline sm" onClick={() => void signal(selected.pid, "CONT")}><Play {...ICON_SM} /> CONT</button>}
                  <button className="ee-btn outline sm" onClick={() => void signal(selected.pid, "TERM")}><X {...ICON_SM} /> TERM</button>
                  <button className="ee-btn destructive sm" onClick={() => void signal(selected.pid, "KILL")}><Skull {...ICON_SM} /> KILL</button>
                </div>
              )}
              <h3 className="ksub">Budget (rlimits)</h3>
              <Meter label="Step executions" used={selected.usage?.executions ?? 0} max={selected.budget?.max_executions} />
              <Meter label="Model calls" used={selected.usage?.llm_calls ?? 0} max={selected.budget?.max_llm_calls} />
              <h3 className="ksub">Capabilities held</h3>
              {!(selected.capabilities ?? []).length ? <p className="muted">None (released at exit).</p> : (
                <table className="ktable small">
                  <thead><tr><th>Kind</th><th>Scope</th><th>Rights</th><th>Uses left</th><th>Origin</th></tr></thead>
                  <tbody>{selected.capabilities.map((cap: any, i: number) => (
                    <tr key={i}><td className="mono">{cap.kind}</td><td className="mono">{cap.scope || "(all)"}</td>
                      <td className="mono">{(cap.rights || []).join(", ")}</td><td>{cap.uses_left ?? "∞"}</td><td>{cap.origin}</td></tr>))}
                  </tbody>
                </table>
              )}
            </>
          )}
        </Card>
      </div>

      <div className="grid3" style={{ marginTop: 16 }}>
        <Card>
          <CardHead title="Run queue" sub="Multi-level, FIFO per level; boosts are one-shot" />
          {PRIO.map((lvl) => (
            <div key={lvl} className="klane">
              <span className="muted">{lvl}</span>
              <div className="chips">{(k.data?.runq?.[lvl] ?? []).map((pid: string) => (
                <span key={pid} className="tag mono" onClick={() => setSel(pid)}>{pid.slice(-6)}</span>))}
                {!(k.data?.runq?.[lvl] ?? []).length && <span className="muted">empty</span>}</div>
            </div>
          ))}
        </Card>
        <Card>
          <CardHead title="Journal" sub="Committed transactions (two-phase, with before/after blobs)" />
          {!(k.data?.txns ?? []).length ? <p className="muted">No commits yet. Run the Transaction demo.</p> : (
            <ul className="rc-list">{k.data.txns.map((t: any) => (
              <li key={t.txn_id}><span className="m"><b className="mono">{t.txn_id.slice(-8)}</b>
                <small>{t.files} file(s) · {t.state} · {ago(t.created_at)}</small></span>
                {t.state === "committed" && <button className="ee-btn outline sm" onClick={() => void undo(t.pid)}><Undo2 {...ICON_SM} /> Undo</button>}</li>))}
            </ul>
          )}
        </Card>
        <Card>
          <CardHead title="Kernel log" sub="Syscalls, interrupts, signals, grants, commits" />
          <div className="klog">
            {(k.data?.log ?? []).slice(0, 60).map((e: any) => (
              <div key={e.id} className={`kl ${e.level}`}>
                <span className="mono t">{new Date(e.ts).toLocaleTimeString([], { hour12: false })}</span>
                <span className="mono ty">{e.type}</span>
                <span className="msg">{e.message}{e.pid ? <span className="muted mono"> · {String(e.pid).slice(-6)}</span> : null}</span>
              </div>
            ))}
            {!(k.data?.log ?? []).length && <p className="muted">Quiet.</p>}
          </div>
        </Card>
      </div>
    </>
  );
}

function Meter({ label, used, max }: { label: string; used: number; max?: number }) {
  const pct = max ? Math.min(100, Math.round((used / max) * 100)) : 0;
  return (
    <div className="kmeter">
      <div className="row"><span>{label}</span><span className="mono">{used} / {max ?? "–"}</span></div>
      <div className="bar"><i style={{ width: `${pct}%`, background: pct >= 100 ? "var(--fail-dot)" : "var(--ink)" }} /></div>
    </div>
  );
}
