import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle, CalendarClock, Check, CircleSlash, GitFork, Globe, KeyRound, ListChecks, Mail, Pause, Play, Plus,
  RadioTower, Undo2, X,
} from "lucide-react";
import { api, get, subscribe } from "../api";
import { useLive } from "../App";
import { DiffView } from "../components/DiffView";
import type { KernelEvent, PendingUser, PlanStep, StepState, Task } from "../types";
import {
  Banner, Chip, Dialog, Empty, Filter, ICON, ICON_SM, PageHeader, Seg, SplitPanes, TaskChip, ago, taskStatus, useToast,
} from "../ui";
import { Composer, WorkList, bucketOf } from "./HomeView";

const LIVE = new Set(["READY", "RUNNING", "BLOCKED_IO", "BLOCKED_USER", "BLOCKED_WAIT", "STOPPED"]);

export function TasksView() {
  const { tasks, sel, nav } = useLive();
  const [filter, setFilter] = useState<"all" | "needs" | "running" | "done">("all");
  const [compose, setCompose] = useState(false);
  const roots = tasks.filter((t) => !t.ppid);
  const shown = filter === "all" ? roots : roots.filter((t) => bucketOf(t) === filter);
  const count = (b: string) => roots.filter((t) => bucketOf(t) === b).length;
  const selected = sel ?? roots[0]?.pid ?? null;

  return (
    <>
      <PageHeader section="Work" dot="--ws-overview" title="Tasks"
                  sub="Each task runs as a process with its own capabilities and a private, staged copy of your files."
                  actions={<button className="ee-btn outline" onClick={() => setCompose(true)}><Plus {...ICON_SM} /> New task</button>} />
      <SplitPanes id="tasks" initial={400} left={
        <>
          <div className="chips" style={{ marginBottom: 12 }}>
            <Filter on={filter === "all"} onClick={() => setFilter("all")} count={roots.length}>All</Filter>
            <Filter on={filter === "needs"} onClick={() => setFilter("needs")} count={count("needs")}>Needs you</Filter>
            <Filter on={filter === "running"} onClick={() => setFilter("running")} count={count("running")}>Running</Filter>
            <Filter on={filter === "done"} onClick={() => setFilter("done")} count={count("done")}>Finished</Filter>
          </div>
          <div className="pane-scroll">
            {roots.length === 0
              ? <Empty icon={<ListChecks {...ICON} />} action={<button className="ee-btn outline" onClick={() => setCompose(true)}>Start a task</button>}>No tasks yet.</Empty>
              : <WorkList tasks={shown} max={500} selected={selected} onPick={(pid) => nav("tasks", pid)} />}
          </div>
        </>
      } right={
        selected ? <TaskDetail key={selected} pid={selected} /> : (
          <Empty icon={<ListChecks {...ICON} />}>Choose a task to see its plan, its changes and what it needs from you.</Empty>
        )
      } />
      {compose && <Dialog title="New task" onClose={() => setCompose(false)}><Composer autoFocus /></Dialog>}
    </>
  );
}

function summary(t: Task): string {
  const s = taskStatus(t);
  const steps = t.plan?.steps.length ?? 0;
  const done = Object.values(t.steps_state || {}).filter((x) => x.status === "done").length;
  if (t.pending_user?.kind === "review") {
    const n = t.pending_user.files.length;
    if (t.pending_user.outcome === "failed") return `It stopped before finishing. ${n} change${n === 1 ? " is" : "s are"} staged; keep them or throw them away.`;
    return n ? `It finished ${done} of ${steps} steps. ${n} change${n === 1 ? " is" : "s are"} staged and nothing has been written yet.`
             : "It finished and changed nothing.";
  }
  if (t.pending_user?.kind === "approval") return "It is waiting for your decision before the next step.";
  if (t.pending_user?.kind === "clarify") return "It needs an answer before it can plan.";
  if (s.word === "Done") return t.txn_id ? `Finished and committed. You can undo all of it in one step.` : "Finished. Nothing needed committing.";
  if (s.word === "Running") return `${done} of ${steps || "?"} steps done.`;
  return s.next;
}

function TaskDetail({ pid }: { pid: string }) {
  const { refresh, bump } = useLive();
  const toast = useToast();
  const [task, setTask] = useState<Task | null>(null);
  const [events, setEvents] = useState<KernelEvent[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [tab, setTab] = useState<"plan" | "trace">("plan");
  const lastSeq = useRef(0);

  const load = useCallback(async () => {
    try { setTask(await get<Task>(`/api/tasks/${pid}`)); setErr(null); } catch (e: any) { setErr(e.message); }
  }, [pid]);
  useEffect(() => { void load(); }, [load, bump]);
  useEffect(() => {
    if (!task?.task_id) return;
    const ctl = new AbortController();
    subscribe(`/api/events?task_id=${task.task_id}&after=0`, (ev: KernelEvent) => {
      if (ev.type === "stream.resync") return;
      if (ev.seq && ev.seq <= lastSeq.current) return;
      if (ev.seq) lastSeq.current = ev.seq;
      setEvents((prev) => [...prev, ev].slice(-1000));
    }, ctl.signal);
    return () => ctl.abort();
  }, [task?.task_id]);

  const act = async (path: string, body: unknown = {}, done?: string) => {
    setErr(null);
    try {
      await api("POST", `/api/tasks/${pid}/${path}`, body);
      if (done) toast({ title: done, text: task?.title });
      await load();
      refresh();
    } catch (e: any) {
      setErr(e.message);
      toast({ title: "That didn't work", text: e.message, bad: true });
    }
  };

  if (!task) return <div className="pane-scroll"><p className="muted">{err ?? "Loading…"}</p></div>;
  const live = LIVE.has(task.state);
  const origin = task.origin && task.origin.kind !== "user" ? task.origin : null;

  return (
    <div className="pane-scroll stack" style={{ gap: 16 }}>
      <div className="stack" style={{ gap: 8, paddingRight: 40 }}>
        <div className="row"><TaskChip task={task} />
          {origin && <span className="tag">{origin.kind === "schedule" ? <CalendarClock size={12} /> : <RadioTower size={12} />}&nbsp;{origin.kind === "schedule" ? "Schedule" : "Listener"}: {origin.name}</span>}
          {task.auto_commit && <span className="tag">Commits automatically</span>}
          <span className="muted">{ago(task.created_at)}</span>
        </div>
        <h2 style={{ margin: 0, fontSize: 24, fontWeight: 500 }}>{task.title}</h2>
        <p className="ink2" style={{ margin: 0 }}>{summary(task)}</p>
        <div className="row">
          {live && task.state !== "STOPPED" && <button className="ee-btn outline sm" onClick={() => act("signal", { signal: "STOP" }, "Paused")}><Pause {...ICON_SM} /> Pause</button>}
          {task.state === "STOPPED" && <button className="ee-btn outline sm" onClick={() => act("signal", { signal: "CONT" }, "Resumed")}><Play {...ICON_SM} /> Resume</button>}
          {live && <button className="ee-btn ghost sm" onClick={() => act("signal", { signal: "TERM" }, "Cancelled")}><X {...ICON_SM} /> Cancel</button>}
          {task.txn_id && task.status !== "undone" && !live && (
            <button className="ee-btn destructive sm" onClick={() => { if (confirm("Undo every change this task committed?")) void act("undo", {}, "Changes undone"); }}>
              <Undo2 {...ICON_SM} /> Undo changes
            </button>
          )}
          <span className="push muted num">{task.usage.executions} syscalls · {task.usage.llm_calls} model calls · {task.slices} time slices</span>
        </div>
      </div>
      {err && <Banner tone="bad" icon={<AlertTriangle {...ICON_SM} />}>{err}</Banner>}
      {task.status === "undone" && <Banner tone="info">The committed changes were undone.</Banner>}
      {task.exit && task.state === "EXITED" && !["completed", "discarded"].includes(task.exit.code) && task.status !== "undone" && (
        <Banner tone={task.exit.code === "cancelled" ? "note" : "bad"} icon={<AlertTriangle {...ICON_SM} />}>{task.exit.reason}</Banner>
      )}
      {task.pending_user && <Decision pending={task.pending_user} task={task} act={act} />}

      <Seg compact label="Task details" value={tab} onChange={setTab} items={[
        { id: "plan", label: "Plan", count: task.plan?.steps.length ?? 0 },
        { id: "trace", label: "Kernel trace", count: events.length },
      ]} />
      {tab === "plan" ? <PlanView task={task} /> : <TraceView events={events} />}
    </div>
  );
}

function ApprovalPreview({ p }: { p: any }) {
  if (!p) return null;
  if (p.type === "email") {
    return (
      <div className="preview-mail">
        <div className="row"><Mail {...ICON_SM} /><b>{p.subject || "(no subject)"}</b>{!p.delivers && <Chip tone="nc">Saved locally, not delivered</Chip>}</div>
        <div className="small ink2">To {p.to.join(", ")}{p.cc?.length ? ` · Cc ${p.cc.join(", ")}` : ""}</div>
        {p.attachments?.length > 0 && <div className="small ink2">Attachments: {p.attachments.join(", ")}</div>}
        <div className="body">{p.body}</div>
      </div>
    );
  }
  if (p.type === "web_action") {
    return (
      <div className="preview-mail">
        <div className="row"><Globe {...ICON_SM} /><b>{p.action === "click" ? "Click" : "Type"} on {new URL(p.element.url).host}</b></div>
        <div className="small ink2">Element: {p.element.tag} “{p.element.label}”{p.element.href ? ` → ${p.element.href}` : ""}</div>
        {p.text !== undefined && <div className="small ink2">Text: {p.text}{p.submit ? " (then Enter)" : ""}</div>}
      </div>
    );
  }
  if (p.type === "diff") return <DiffView file={{ path: p.path, op: "modify", diff: p.diff, truncated: p.truncated }} />;
  if (p.type === "new_file") return <DiffView file={{ path: p.path, op: "create", diff: (p.content || "").split("\n").map((l: string) => "+" + l).join("\n").replace(/^/, "@@ -0,0 +1 @@\n") }} />;
  return null;
}

function Decision({ pending, task, act }: { pending: PendingUser; task: Task; act: (p: string, b?: unknown, done?: string) => void }) {
  const [answer, setAnswer] = useState("");
  if (pending.kind === "clarify") {
    return (
      <div className="decision">
        <h3>The agent has a question</h3>
        <p>{pending.question}</p>
        <form className="row nowrap" onSubmit={(e) => { e.preventDefault(); if (answer.trim()) act("answer", { text: answer }, "Answer sent"); }}>
          <input className="input" style={{ background: "var(--card)" }} value={answer} onChange={(e) => setAnswer(e.target.value)} placeholder="Your answer" aria-label="Your answer" />
          <button className="ee-btn primary" disabled={!answer.trim()}>Send</button>
        </form>
      </div>
    );
  }
  if (pending.kind === "approval") {
    const g = pending.grant;
    const grantWord = g ? ({ mail: "send email", web: g.right === "act" ? `act on ${g.scope}` : `read ${g.scope}`, app: `start ${g.scope}` } as any)[g.kind] ?? `${g.kind}:${g.right}` : null;
    return (
      <div className="decision">
        <div className="row"><Chip tone="review">Needs approval</Chip>{pending.irreversible && <Chip tone="fail">Can't be undone</Chip>}
          {g && <span className="tag"><KeyRound size={12} />&nbsp;Grants permission to {grantWord}</span>}</div>
        <h3>{pending.effect || pending.tool}</h3>
        {pending.reasons.length > 0 && <ul>{pending.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>}
        <ApprovalPreview p={pending.preview} />
        <details><summary className="muted" style={{ cursor: "pointer" }}>Exact arguments</summary>
          <pre className="code" style={{ marginTop: 8 }}>{JSON.stringify(pending.args, null, 2)}</pre></details>
        <div className="row">
          <button className="ee-btn primary" onClick={() => act("approve", { action_hash: pending.action_hash }, "Approved")}>
            <Check {...ICON_SM} /> {g && !pending.irreversible ? "Allow for this task" : "Allow once"}
          </button>
          {g && !pending.irreversible && (
            <button className="ee-btn outline" onClick={() => act("approve", { action_hash: pending.action_hash, standing_minutes: 60 }, "Allowed for 1 hour")}>Allow for 1 hour</button>
          )}
          <button className="ee-btn ghost" onClick={() => act("reject", { action_hash: pending.action_hash }, "Declined")}><CircleSlash {...ICON_SM} /> Decline</button>
          <span className="push muted">Risk: {pending.risk.toLowerCase().replace(/_/g, " ")}</span>
        </div>
      </div>
    );
  }
  const deferred = pending.outbox.map((id) => task.plan?.steps.find((s) => s.id === id)?.title ?? id);
  const n = pending.files.length;
  return (
    <div className="decision">
      <div className="row">
        <Chip tone={pending.outcome === "conflict" || pending.outcome === "commit_incomplete" ? "fail" : "review"}>
          {pending.outcome === "conflict" ? "Conflict" : pending.outcome === "commit_incomplete" ? "Commit incomplete" : "Ready to review"}
        </Chip>
        <span className="muted">Nothing has been written to your files yet.</span>
      </div>
      <h3>{pending.outcome === "conflict" ? "Some files changed on disk while the task ran"
        : pending.outcome === "commit_incomplete" ? "The commit didn't finish. Retrying is safe."
        : pending.outcome === "failed" ? "The task stopped early. Keep what it did?"
        : `${n} staged change${n === 1 ? "" : "s"}`}</h3>
      {pending.failure && <Banner tone="bad">{pending.failure}</Banner>}
      {pending.error && <Banner tone="bad">{pending.error}</Banner>}
      {pending.conflicts && <ul>{pending.conflicts.map((c) => <li key={c.path}><b className="mono">{c.path}</b>: {c.reason}</li>)}</ul>}
      {deferred.length > 0 && (
        <Banner tone="info" icon={<Mail {...ICON_SM} />}>
          <p><b>After you commit:</b> {deferred.join(", ")}. These leave your computer, so each will ask you first.</p>
        </Banner>
      )}
      <div className="stack" style={{ gap: 8 }}>{pending.files.map((f) => <DiffView key={f.path} file={f} defaultOpen={n <= 3} />)}</div>
      <div className="row">
        <button className="ee-btn primary" onClick={() => act("commit", {}, "Committed")}>
          <Check {...ICON_SM} /> {pending.outcome === "commit_incomplete" ? "Retry commit" : n ? `Commit ${n} change${n === 1 ? "" : "s"}` : "Finish"}
        </button>
        <button className="ee-btn destructive" onClick={() => act("discard", {}, "Discarded")}><X {...ICON_SM} /> Discard</button>
      </div>
    </div>
  );
}

function PlanView({ task }: { task: Task }) {
  if (!task.plan) return <p className="muted">Planning…</p>;
  if (!task.plan.steps.length) return <p className="muted">No steps.</p>;
  return (
    <ol className="steps-list">
      {task.plan.steps.map((s, i) => <StepRow key={s.id} n={i + 1} step={s} st={task.steps_state[s.id]} />)}
    </ol>
  );
}

const STEP_WORD: Record<string, [string, any]> = {
  done: ["Done", "pass"], failed: ["Failed", "fail"], running: ["Running", "info"], pending: ["Waiting", "nc"], deferred: ["After commit", "phys"],
};

function StepRow({ n, step, st }: { n: number; step: PlanStep; st?: StepState }) {
  const [open, setOpen] = useState(false);
  const status = st?.status ?? "pending";
  const [word, tone] = STEP_WORD[status] ?? ["Waiting", "nc"];
  return (
    <li className={`stp ${status}`}>
      <button onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="n">{status === "done" ? <Check size={14} strokeWidth={2} /> : n}</span>
        <span className="grow" style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
          <b style={{ fontWeight: 600, fontSize: 14 }} className="ellipsis">{step.title || step.id}</b>
          <small className="muted mono">{step.tool}</small>
        </span>
        {st?.via && <span className="tag"><GitFork size={12} />&nbsp;forked</span>}
        {(st?.attempts ?? 0) > 1 && <span className="tag">{st?.attempts} attempts</span>}
        <Chip tone={tone}>{word}</Chip>
      </button>
      {(open || (st?.last_error && status !== "done")) && (
        <div className="body">
          {st?.last_error && status !== "done" && <Banner tone="bad">{st.last_error.message}</Banner>}
          {open && <>
            <div className="muted">Arguments</div>
            <pre className="code">{JSON.stringify(step.args, null, 2)}</pre>
            {st?.checks && st.checks.length > 0 && (
              <ul className="checks">{st.checks.map((c) => <li key={c.name}><Chip tone={c.passed ? "pass" : "fail"}>{c.passed ? "Passed" : "Failed"}</Chip>{c.name} <span className="muted">{c.detail}</span></li>)}</ul>
            )}
            {st?.summary !== undefined && <StepOutput tool={step.tool} output={st.summary} />}
          </>}
        </div>
      )}
    </li>
  );
}

function Screenshot({ src }: { src: string }) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    let u: string | null = null;
    (async () => {
      const { blobUrl } = await import("../api");
      u = await blobUrl(src);
      setUrl(u);
    })().catch(() => setUrl(null));
    return () => { if (u) URL.revokeObjectURL(u); };
  }, [src]);
  return url ? <img className="shot" src={url} alt="Screenshot of the page" /> : <p className="muted">Loading screenshot…</p>;
}

function StepOutput({ tool, output }: { tool: string; output: any }) {
  if (output?.screenshot) return <Screenshot src={output.screenshot} />;
  if (tool === "search_knowledge" && output?.hits) {
    return <div className="stack" style={{ gap: 6 }}>{output.hits.map((h: any, i: number) => (
      <div key={i}><div className="mono muted">{h.path}:{h.start_line}-{h.end_line}</div><pre className="code">{h.text}</pre></div>))}</div>;
  }
  if ((tool === "email_list" || tool === "email_search") && output?.messages) {
    return <ul className="plain-list">{output.messages.map((m: any) => (
      <li key={m.id} className="plain-row" style={{ background: "var(--card)" }}><span className="m"><b className="ellipsis">{m.subject}</b><small className="ellipsis">{m.from} · {m.snippet}</small></span>{!m.seen && <Chip tone="info">Unread</Chip>}</li>))}</ul>;
  }
  if (tool === "calendar_list" && output?.events) {
    return output.events.length ? <ul className="plain-list">{output.events.map((e: any) => (
      <li key={e.uid} className="plain-row" style={{ background: "var(--card)" }}><span className="m"><b>{e.title}</b><small>{e.start.replace("T", " ")}</small></span></li>))}</ul>
      : <p className="muted">No events in that range.</p>;
  }
  if (typeof output?.content === "string") return <pre className="code">{output.content}</pre>;
  if (typeof output?.body === "string") return <pre className="code">{output.body}</pre>;
  if (typeof output?.text === "string") return <pre className="code">{output.title ? `# ${output.title}\n\n` : ""}{output.text}</pre>;
  return <pre className="code">{JSON.stringify(output, null, 2)}</pre>;
}

function TraceView({ events }: { events: KernelEvent[] }) {
  const ordered = useMemo(() => [...events].reverse(), [events]);
  if (!events.length) return <p className="muted">No kernel events yet.</p>;
  return (
    <ul className="trace">
      {ordered.map((e, i) => (
        <li key={`${e.seq ?? i}-${e.type}`}>
          <span className="ty">{e.type}</span>
          <span className={`lvl-${e.level ?? "info"}`}>{e.message}</span>
        </li>
      ))}
    </ul>
  );
}
