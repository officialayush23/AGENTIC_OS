import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { get, post, subscribe } from "../api";
import { Badge } from "../components/Badge";
import { DiffView } from "../components/DiffView";
import type { KernelEvent, PendingUser, PlanStep, StepState, Task } from "../types";

const LIVE = new Set(["READY", "RUNNING", "BLOCKED_IO", "BLOCKED_USER", "BLOCKED_WAIT", "STOPPED"]);
const EXAMPLES = [
  "Write a project report",
  "search for \"TODO\"",
  "how does the app start?",
  "create notes/today.md with \"Plan for today\"",
];

export function TasksView({ onChange }: { onChange: () => void }) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [request, setRequest] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    const r = await get<{ tasks: Task[] }>("/api/tasks");
    setTasks(r.tasks);
    setSelected((cur) => cur ?? r.tasks[0]?.pid ?? null);
  }, []);

  useEffect(() => {
    void load();
    const ctl = new AbortController();
    subscribe("/api/events", () => { void load(); onChange(); }, ctl.signal);
    const poll = setInterval(load, 4000);
    return () => { ctl.abort(); clearInterval(poll); };
  }, [load, onChange]);

  const submit = async (e?: React.FormEvent) => {
    e?.preventDefault();
    if (!request.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      const t = await post<Task>("/api/tasks", { request });
      setRequest("");
      setSelected(t.pid);
      await load();
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="tasks">
      <section className="task-list">
        <form className="composer" onSubmit={submit}>
          <textarea
            placeholder="What should Agentic OS do? Changes are staged; you review them before anything is written."
            value={request}
            onChange={(e) => setRequest(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void submit(); }}
            rows={3}
          />
          <div className="row">
            <button className="primary" disabled={busy || !request.trim()}>Run task</button>
            <span className="muted small">Ctrl+Enter</span>
          </div>
          {err && <p className="error">{err}</p>}
          {tasks.length === 0 && (
            <div className="examples">
              {EXAMPLES.map((x) => (
                <button type="button" key={x} className="chip" onClick={() => setRequest(x)}>{x}</button>
              ))}
            </div>
          )}
        </form>
        <ul className="list">
          {tasks.map((t) => (
            <li key={t.pid}>
              <button className={`list-item ${selected === t.pid ? "selected" : ""}`} onClick={() => setSelected(t.pid)}>
                <span className="ellipsis">{t.title}</span>
                <Badge task={t} />
              </button>
            </li>
          ))}
        </ul>
      </section>
      <section className="task-detail">
        {selected ? <TaskDetail key={selected} pid={selected} onChange={load} /> : (
          <div className="empty">
            <h2>Every task is a transaction</h2>
            <p className="muted">The agent works on a private copy of your workspace. You see one diff, then commit or
              discard. Committed changes can be undone.</p>
          </div>
        )}
      </section>
    </div>
  );
}

function TaskDetail({ pid, onChange }: { pid: string; onChange: () => void }) {
  const [task, setTask] = useState<Task | null>(null);
  const [events, setEvents] = useState<KernelEvent[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [tab, setTab] = useState<"plan" | "trace">("plan");
  const lastSeq = useRef(0);

  const load = useCallback(async () => {
    try {
      setTask(await get<Task>(`/api/tasks/${pid}`));
    } catch (e: any) {
      setErr(e.message);
    }
  }, [pid]);

  useEffect(() => {
    lastSeq.current = 0;
    setEvents([]);
    void load();
  }, [load]);

  useEffect(() => {
    if (!task?.task_id) return;
    const ctl = new AbortController();
    subscribe(`/api/events?task_id=${task.task_id}&after=${lastSeq.current}`, (ev: KernelEvent) => {
      if (ev.type === "stream.resync") return void load();
      if (ev.seq && ev.seq <= lastSeq.current) return;
      if (ev.seq) lastSeq.current = ev.seq;
      setEvents((prev) => [...prev, ev].slice(-1000));
      void load();
    }, ctl.signal);
    return () => ctl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [task?.task_id]);

  const act = async (path: string, body: unknown = {}) => {
    setErr(null);
    try {
      await post(`/api/tasks/${pid}/${path}`, body);
      await load();
      onChange();
    } catch (e: any) {
      setErr(e.message);
    }
  };

  if (!task) return <div className="pad muted">{err ?? "Loading…"}</div>;
  const live = LIVE.has(task.state);
  const undone = task.status === "undone";

  return (
    <div className="detail">
      <header className="detail-head">
        <div>
          <h2>{task.title}</h2>
          {task.plan?.goal && task.plan.goal !== task.title && <p className="muted">{task.plan.goal}</p>}
        </div>
        <div className="actions">
          <Badge task={task} />
          {live && task.state !== "STOPPED" && <button onClick={() => act("signal", { signal: "STOP" })}>Pause</button>}
          {task.state === "STOPPED" && <button onClick={() => act("signal", { signal: "CONT" })}>Resume</button>}
          {live && <button className="danger-ghost" onClick={() => act("signal", { signal: "TERM" })}>Cancel</button>}
          {task.txn_id && !undone && !live && (
            <button onClick={() => { if (confirm("Undo every change this task committed?")) void act("undo"); }}>
              Undo changes
            </button>
          )}
        </div>
      </header>
      {err && <p className="error">{err}</p>}
      {undone && <div className="notice">The committed changes were undone.</div>}
      {task.exit && task.exit.code !== "completed" && task.state === "EXITED" && (
        <div className={`notice ${task.exit.code === "failed" || task.exit.code === "budget_exceeded" ? "notice-bad" : ""}`}>
          {task.exit.reason}
        </div>
      )}
      {task.pending_user && <PendingCard pending={task.pending_user} task={task} act={act} />}

      <div className="tabs">
        <button className={tab === "plan" ? "active" : ""} onClick={() => setTab("plan")}>Plan</button>
        <button className={tab === "trace" ? "active" : ""} onClick={() => setTab("trace")}>
          Trace <span className="muted">{events.length}</span>
        </button>
        <span className="muted small push">
          {task.usage.executions} syscalls · {task.usage.llm_calls} LLM calls · {task.slices} slices
        </span>
      </div>
      {tab === "plan" ? <PlanView task={task} /> : <TraceView events={events} />}
    </div>
  );
}

function PendingCard({ pending, task, act }: { pending: PendingUser; task: Task; act: (p: string, b?: unknown) => void }) {
  const [answer, setAnswer] = useState("");
  if (pending.kind === "clarify") {
    return (
      <div className="card pending">
        <h3>Question</h3>
        <p>{pending.question}</p>
        <form onSubmit={(e) => { e.preventDefault(); if (answer.trim()) act("answer", { text: answer }); }} className="row">
          <input className="grow" value={answer} onChange={(e) => setAnswer(e.target.value)} placeholder="Your answer" />
          <button className="primary" disabled={!answer.trim()}>Send</button>
        </form>
      </div>
    );
  }
  if (pending.kind === "approval") {
    return (
      <div className="card pending">
        <h3>{pending.irreversible ? "Allow an action that can't be staged?" : "Approve this step?"}</h3>
        <p>{pending.effect}</p>
        <ul className="reasons">{pending.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
        <pre className="args">{JSON.stringify(pending.args, null, 2)}</pre>
        <div className="row wrap">
          <button className="primary" onClick={() => act("approve", { action_hash: pending.action_hash })}>
            {pending.grant ? "Allow once" : "Approve"}
          </button>
          {pending.grant && (
            <>
              <button onClick={() => act("approve", { action_hash: pending.action_hash, standing_minutes: 10 })}>
                Allow for 10 min
              </button>
              <button onClick={() => act("approve", { action_hash: pending.action_hash, standing_minutes: 60 })}>
                Allow for 1 hour
              </button>
            </>
          )}
          <button className="danger-ghost" onClick={() => act("reject", { action_hash: pending.action_hash })}>Decline</button>
          <span className="muted small push">risk: {pending.risk}</span>
        </div>
      </div>
    );
  }
  const deferred = pending.outbox.map((id) => task.plan?.steps.find((s) => s.id === id)?.title ?? id);
  return (
    <div className="card pending review">
      <div className="row">
        <h3 className="grow">
          {pending.outcome === "conflict" ? "Files changed on disk while the task ran"
            : pending.outcome === "commit_incomplete" ? "Commit didn't finish"
            : pending.outcome === "failed" ? "The task stopped early. Keep what it did?"
            : `Review ${pending.files.length} staged change${pending.files.length === 1 ? "" : "s"}`}
        </h3>
        <button className="primary" onClick={() => act("commit")}>
          {pending.outcome === "commit_incomplete" ? "Retry commit" : "Commit"}
        </button>
        <button className="danger-ghost" onClick={() => act("discard")}>Discard</button>
      </div>
      {pending.failure && <p className="error">{pending.failure}</p>}
      {pending.error && <p className="error">{pending.error}</p>}
      {pending.conflicts && (
        <ul className="reasons">{pending.conflicts.map((c) => <li key={c.path}><b>{c.path}</b>: {c.reason}</li>)}</ul>
      )}
      {deferred.length > 0 && (
        <p className="muted small">After commit: {deferred.join(", ")}</p>
      )}
      <p className="muted small">Nothing has been written to your files yet.</p>
      {pending.files.map((f) => <DiffView key={f.path} file={f} defaultOpen={pending.files.length <= 3} />)}
    </div>
  );
}

const ICON: Record<string, string> = { done: "✓", failed: "✕", running: "◌", pending: "○", deferred: "⏸" };

function PlanView({ task }: { task: Task }) {
  if (!task.plan) return <p className="muted pad">Planning…</p>;
  return (
    <ol className="steps">
      {task.plan.steps.map((s) => <StepRow key={s.id} step={s} st={task.steps_state[s.id]} />)}
    </ol>
  );
}

function StepRow({ step, st }: { step: PlanStep; st?: StepState }) {
  const [open, setOpen] = useState(false);
  const status = st?.status ?? "pending";
  return (
    <li className={`step step-${status}`}>
      <button className="step-head" onClick={() => setOpen(!open)}>
        <span className="step-icon">{ICON[status] ?? "○"}</span>
        <span className="grow">{step.title || step.id}</span>
        <span className="mono muted small">{step.tool}</span>
        {st?.via && <span className="tag">forked</span>}
        {(st?.attempts ?? 0) > 1 && <span className="tag">{st?.attempts} attempts</span>}
      </button>
      {st?.last_error && status !== "done" && <p className="error small">{st.last_error.message}</p>}
      {open && (
        <div className="step-body">
          <div className="muted small">Arguments</div>
          <pre className="args">{JSON.stringify(step.args, null, 2)}</pre>
          {st?.checks && st.checks.length > 0 && (
            <ul className="checks">
              {st.checks.map((c) => <li key={c.name} className={c.passed ? "ok" : "bad"}>{c.passed ? "✓" : "✕"} {c.name} <span className="muted">{c.detail}</span></li>)}
            </ul>
          )}
          {st?.summary !== undefined && <StepOutput tool={step.tool} output={st.summary} />}
        </div>
      )}
    </li>
  );
}

function StepOutput({ tool, output }: { tool: string; output: any }) {
  if (tool === "search_knowledge" && output?.hits) {
    return (
      <div className="hits">
        {output.hits.map((h: any, i: number) => (
          <div key={i} className="hit">
            <div className="mono small">{h.path}:{h.start_line}-{h.end_line}</div>
            <pre className="snippet">{h.text}</pre>
          </div>
        ))}
      </div>
    );
  }
  if (tool === "read_workspace_file" && typeof output?.content === "string") {
    return <pre className="snippet">{output.content}</pre>;
  }
  return <pre className="args">{JSON.stringify(output, null, 2)}</pre>;
}

const TRACE_LABEL: Record<string, string> = {
  "syscall.enter": "→", "syscall.exit": "←", "policy.decision": "§", "cap.grant": "🔑", "cap.denied": "⛔",
  "proc.fork": "⑂", "proc.reaped": "⑂", "txn.committed": "✓", "txn.review": "Δ", "signal": "⚑",
};

function TraceView({ events }: { events: KernelEvent[] }) {
  const ordered = useMemo(() => [...events].reverse(), [events]);
  if (!events.length) return <p className="muted pad">No events yet.</p>;
  return (
    <ul className="trace">
      {ordered.map((e, i) => (
        <li key={`${e.seq ?? i}-${e.type}`} className={`trace-row lvl-${e.level ?? "info"}`}>
          <span className="trace-icon">{TRACE_LABEL[e.type] ?? "·"}</span>
          <span className="mono small type">{e.type}</span>
          <span className="grow">{e.message}</span>
          {e.data?.pid && e.data?.ppid && <span className="tag">child</span>}
        </li>
      ))}
    </ul>
  );
}
