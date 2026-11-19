import { useMemo, useState } from "react";
import { ArrowRight, CalendarDays, CheckCircle2, Clock, Hand, Inbox, Loader, Mail, Play, Workflow } from "lucide-react";
import { get } from "../api";
import { useLive } from "../App";
import type { CalEvent, MailMsg, Schedule, Task } from "../types";
import { Card, CardHead, Chip, Empty, Group, ICON, ICON_SM, Kpi, PageHeader, Seg, ago, greeting, taskStatus, useAsync, useToast, when } from "../ui";

export const SUGGESTIONS = [
  "Check my email",
  "What's on my calendar this week",
  "Schedule a meeting with priya@example.com tomorrow at 3pm",
  "Note: ideas for the kernel demo",
  "Write a project report",
  "Open example.com",
];

export function Composer({ autoFocus }: { autoFocus?: boolean }) {
  const { run, nav } = useLive();
  const toast = useToast();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    if (!text.trim() || busy) return;
    setBusy(true);
    try {
      const t = await run(text.trim());
      setText("");
      toast({ title: "Task started", text: t.title });
      nav("tasks", t.pid);
    } catch (e: any) {
      toast({ title: "Could not start the task", text: e.message, bad: true });
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="composer">
      <textarea autoFocus={autoFocus} aria-label="Describe a task" value={text} onChange={(e) => setText(e.target.value)}
                placeholder="What should Agentic OS do? Changes are staged; you review them before anything is written or sent."
                onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) void submit(); }} />
      <div className="bar">
        {SUGGESTIONS.slice(0, 4).map((s) => <button key={s} type="button" className="suggest" onClick={() => setText(s)}>{s}</button>)}
        <span className="push muted">Ctrl+Enter</span>
        <button className="ee-btn primary" disabled={!text.trim() || busy} onClick={submit}>
          <Play {...ICON_SM} /> Run task
        </button>
      </div>
    </div>
  );
}

type Bucket = "needs" | "running" | "done";

export function bucketOf(t: Task): Bucket {
  if (t.pending_user) return "needs";
  if (t.state !== "EXITED") return "running";
  return "done";
}

export function WorkList({ tasks, max = 30, selected, onPick }: {
  tasks: Task[]; max?: number; selected?: string | null; onPick: (pid: string) => void;
}) {
  const groups = useMemo(() => {
    const by: Record<string, Task[]> = {};
    for (const t of tasks.slice(0, max)) {
      const s = taskStatus(t);
      (by[s.word] ??= []).push(t);
    }
    return Object.entries(by);
  }, [tasks, max]);
  const color: Record<string, string> = {
    pass: "--pass-dot", fail: "--fail-dot", review: "--review-dot", info: "--lc-ready", nc: "--nc", phys: "--phys", retake: "--retake",
  };
  if (!tasks.length) return <Empty icon={<CheckCircle2 {...ICON} />}>Nothing here yet.</Empty>;
  return (
    <div>
      {groups.map(([word, list]) => (
        <Group key={word} name={word} count={list.length} color={color[taskStatus(list[0]).tone]}>
          {list.map((t) => {
            const s = taskStatus(t);
            return (
              <li key={t.pid}>
                <button className="ee-item" aria-selected={selected === t.pid} onClick={() => onPick(t.pid)}>
                  <span className="m">
                    <b>{t.title}</b>
                    <small>{s.next}{t.origin && t.origin.kind !== "user" ? ` · by ${t.origin.kind === "schedule" ? "schedule" : "listener"} “${t.origin.name}”` : ""}</small>
                  </span>
                  <span className="r">{ago(t.created_at)}<ArrowRight {...ICON_SM} /></span>
                </button>
              </li>
            );
          })}
        </Group>
      ))}
    </div>
  );
}

export function HomeView() {
  const { tasks, status, nav, bump } = useLive();
  const [bucket, setBucket] = useState<Bucket | null>(null);
  const roots = tasks.filter((t) => !t.ppid);
  const counts = { needs: 0, running: 0, done: 0 } as Record<Bucket, number>;
  roots.forEach((t) => { counts[bucketOf(t)]++; });
  const active: Bucket = bucket ?? (counts.needs ? "needs" : counts.running ? "running" : "done");
  const today = new Date().toDateString();
  const committedToday = roots.filter((t) => t.txn_id && t.created_at && new Date(t.created_at).toDateString() === today).length;

  const cal = useAsync(() => get<{ events: CalEvent[] }>("/api/calendar?days=3"), [bump]);
  const mail = useAsync(() => get<{ messages: MailMsg[] }>("/api/mail/messages?unread_only=true&limit=4"), [bump]);
  const sched = useAsync(() => get<{ schedules: Schedule[] }>("/api/schedules"), [bump]);
  const nextSched = (sched.data?.schedules ?? []).filter((s) => s.enabled && s.next_run).sort((a, b) => (a.next_run! - b.next_run!))[0];

  const steps = [
    { t: "Describe a task", d: "Say it in plain words, or start from a suggestion.", done: roots.length > 0 },
    { t: "Review one diff", d: "The agent works on a private copy. You see every change.", done: roots.some((t) => t.txn_id || t.exit?.code === "discarded") },
    { t: "Commit or discard", d: "Commit writes everything at once. Undo is always there.", done: roots.some((t) => t.txn_id) },
    { t: "Automate it", d: "Run a task on a schedule or when a file or email arrives.", done: (status?.automation?.schedules ?? 0) + (status?.automation?.listeners ?? 0) > 0 },
  ];

  return (
    <>
      <PageHeader section="Overview" dot="--ws-overview" title={`${greeting()}`}
                  sub="Ask for anything. Every task is a transaction: staged, reviewed by you, then committed and undoable." />
      <Card><Composer /></Card>

      <div className="kpis">
        <Kpi label="Needs you" value={counts.needs} icon={<Hand {...ICON_SM} />} inverted={counts.needs > 0}
             hint={counts.needs ? "Approvals, questions or reviews" : "Nothing waiting"} />
        <Kpi label="Running" value={counts.running} icon={<Loader {...ICON_SM} />} hint={`${status?.kernel?.processes ?? 0} tasks in total`} />
        <Kpi label="Committed today" value={committedToday} icon={<CheckCircle2 {...ICON_SM} />} hint="Each one can be undone" />
        <Kpi label="Next scheduled run" value={nextSched ? new Date(nextSched.next_run! * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "None"}
             icon={<Clock {...ICON_SM} />} hint={nextSched ? `${nextSched.name} · ${when(nextSched.next_run)}` : "Add one in Automations"} />
      </div>

      <div className="two">
        <Card>
          <CardHead title="My work" sub={`${roots.length} task${roots.length === 1 ? "" : "s"}`}
                    right={<button className="ee-btn outline sm" onClick={() => nav("tasks")}>Open tasks</button>} />
          <Seg label="Task buckets" value={active} onChange={setBucket} items={[
            { id: "needs", label: "Needs you", count: counts.needs },
            { id: "running", label: "Running", count: counts.running },
            { id: "done", label: "Finished", count: counts.done },
          ]} />
          <div className="list-scroll" style={{ marginTop: 12 }}>
            <WorkList tasks={roots.filter((t) => bucketOf(t) === active)} onPick={(pid) => nav("tasks", pid)} />
          </div>
        </Card>

        <div className="stack" style={{ gap: 16 }}>
          <Card>
            <CardHead title="Coming up" sub="From your local calendar"
                      right={<button className="ee-btn outline sm" onClick={() => nav("calendar")}><CalendarDays {...ICON_SM} /> Calendar</button>} />
            {(cal.data?.events ?? []).length === 0 ? (
              <Empty icon={<CalendarDays {...ICON} />}>No events in the next 3 days. Ask the agent to schedule a meeting.</Empty>
            ) : (
              <ul className="plain-list">
                {cal.data!.events.slice(0, 4).map((e) => (
                  <li key={e.uid} className="plain-row">
                    <span className="m"><b>{e.title}</b><small>{new Date(e.start).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })}{e.attendees.length ? ` · ${e.attendees.length} invited` : ""}</small></span>
                    {e.url && <a className="ee-btn outline sm" href={e.url} target="_blank" rel="noreferrer">Join</a>}
                  </li>
                ))}
              </ul>
            )}
          </Card>
          <Card>
            <CardHead title="Unread mail" sub={status?.mail?.delivers ? status.mail.address : "Local demo mailbox"}
                      right={<button className="ee-btn outline sm" onClick={() => nav("inbox")}><Inbox {...ICON_SM} /> Inbox</button>} />
            {mail.error ? <p className="muted">{mail.error}</p> : (mail.data?.messages ?? []).length === 0 ? (
              <Empty icon={<Mail {...ICON} />}>You're all caught up.</Empty>
            ) : (
              <ul className="plain-list">
                {mail.data!.messages.map((m) => (
                  <li key={m.id} className="plain-row">
                    <span className="m"><b className="ellipsis">{m.subject}</b><small className="ellipsis">{m.from.replace(/<.*>/, "").trim()} · {m.snippet}</small></span>
                    <Chip tone="info">Unread</Chip>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      </div>

      <section>
        <h2 className="ee-h2" style={{ marginBottom: 12 }}>How a task runs</h2>
        <ol className="ee-steps">
          {steps.map((s, i) => (
            <li key={s.t} className={`ee-step ${s.done ? "done" : ""}`}>
              <span className="n"><b>{s.done ? <CheckCircle2 size={16} strokeWidth={1.8} /> : i + 1}</b>{s.t}</span>
              <p>{s.d}</p>
              {i === 3 && !s.done && <button className="ee-btn outline sm" style={{ marginTop: "auto", alignSelf: "flex-start" }} onClick={() => nav("automations")}><Workflow {...ICON_SM} /> Automations</button>}
            </li>
          ))}
        </ol>
      </section>
    </>
  );
}
