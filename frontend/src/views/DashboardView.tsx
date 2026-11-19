import { useState } from "react";
import { Activity, CheckCircle2, GitCommitHorizontal, Hand, Network, Plug, Workflow } from "lucide-react";
import { get } from "../api";
import { useLive } from "../App";
import type { CalEvent, MailMsg } from "../types";
import { Card, CardHead, Chip, Empty, ICON, ICON_SM, Kpi, PageHeader, Seg, ago, useAsync } from "../ui";

const OUT_WORD: Record<string, [string, any]> = {
  completed: ["Done", "pass"], failed: ["Failed", "fail"], budget_exceeded: ["Failed", "fail"], discarded: ["Discarded", "nc"],
  cancelled: ["Cancelled", "nc"], killed: ["Cancelled", "nc"], waiting: ["Needs you", "review"], running: ["Running", "info"],
};

export function DashboardView() {
  const { bump, nav } = useLive();
  const [days, setDays] = useState<"14" | "30">("14");
  const d = useAsync(() => get<any>(`/api/dashboard?days=${days}`), [bump, days]);
  const cal = useAsync(() => get<{ events: CalEvent[] }>("/api/calendar?days=7"), [bump]);
  const gcal = useAsync(() => get<{ events: any[] }>("/api/connections/google/events?days=7"), [bump]);
  const mail = useAsync(() => get<{ messages: MailMsg[] }>("/api/mail/messages?unread_only=true&limit=5"), [bump]);
  const k = d.data?.kpis;
  const events = [...(cal.data?.events ?? []), ...(gcal.data?.events ?? [])].sort((a, b) => a.start.localeCompare(b.start)).slice(0, 6);

  return (
    <>
      <PageHeader section="Overview" dot="--ws-overview" title="Dashboard"
                  sub="Everything the agent did and everything waiting for you, in one place."
                  actions={<Seg compact label="Range" value={days} onChange={setDays} items={[{ id: "14", label: "14 days" }, { id: "30", label: "30 days" }]} />} />
      <div className="kpis">
        <Kpi label="Needs you" value={k?.waiting ?? "–"} icon={<Hand {...ICON_SM} />} inverted={(k?.waiting ?? 0) > 0} hint="Approvals, reviews, questions" />
        <Kpi label="Tasks" value={k?.tasks ?? "–"} icon={<Activity {...ICON_SM} />} hint={k?.success_rate != null ? `${k.success_rate}% finished successfully` : "No finished tasks yet"} />
        <Kpi label="Commits" value={k?.commits ?? "–"} icon={<GitCommitHorizontal {...ICON_SM} />} hint="Each one can be undone" />
        <Kpi label="Automations" value={k?.automations ?? "–"} icon={<Workflow {...ICON_SM} />} hint={`${k?.automation_runs ?? 0} runs so far`} />
      </div>

      <div className="two">
        <Card>
          <CardHead title="Activity" sub="Tasks per day by outcome, and automation runs" />
          {d.data ? <DayChart data={d.data.per_day} /> : <div className="skeleton" style={{ height: 220 }} />}
          <div className="chips" style={{ marginTop: 10 }}>
            <span className="legend"><i style={{ background: "var(--pass-dot)" }} />Done</span>
            <span className="legend"><i style={{ background: "var(--fail-dot)" }} />Failed</span>
            <span className="legend"><i style={{ background: "var(--nc)" }} />Other</span>
            <span className="legend"><i style={{ background: "var(--lc-ready)" }} />Automation runs</span>
          </div>
        </Card>
        <Card>
          <CardHead title="Most used tools" sub="Steps planned across all tasks" />
          {d.data?.tools?.length ? <Bars items={d.data.tools.map((t: any) => ({ label: t.tool.replace(/_/g, " "), value: t.count }))} /> :
            <Empty icon={<Activity {...ICON} />}>No tasks yet.</Empty>}
          {d.data && (
            <div className="rc-stats" style={{ marginTop: 14 }}>
              {Object.entries(d.data.origins as Record<string, number>).map(([o, n]) => (
                <div key={o}><b>{n}</b><small>{{ user: "typed", chat: "from chat", schedule: "scheduled", listener: "from events" }[o] ?? o}</small></div>))}
            </div>
          )}
        </Card>
      </div>

      <div className="grid3">
        <Card>
          <CardHead title="Recent tasks" right={<button className="ee-btn outline sm" onClick={() => nav("tasks")}>All tasks</button>} />
          <ul className="plain-list">
            {(d.data?.recent ?? []).map((r: any) => {
              const [w, tone] = OUT_WORD[r.code] ?? [r.code, "nc"];
              return (
                <li key={r.pid}><button className="ee-item" style={{ background: "var(--soft)" }} onClick={() => nav("tasks", r.pid)}>
                  <span className="m"><b>{r.title}</b><small>{ago(r.created_at)} · {r.origin}</small></span><Chip tone={tone}>{w}</Chip></button></li>
              );
            })}
          </ul>
        </Card>
        <Card>
          <CardHead title="Coming up" right={<button className="ee-btn outline sm" onClick={() => nav("calendar")}>Calendar</button>} />
          {events.length === 0 ? <Empty icon={<CheckCircle2 {...ICON} />}>Nothing in the next 7 days.</Empty> : (
            <ul className="rc-list">{events.map((e: any, i) => (
              <li key={e.uid ?? e.id ?? i}><span className="rc-date"><b>{new Date(e.start).getDate()}</b><small>{new Date(e.start).toLocaleDateString([], { month: "short" })}</small></span>
                <span className="m"><b>{e.title}</b><small>{new Date(e.start).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}{e.source === "google" ? " · Google" : ""}</small></span>
                {e.url && <a className="ee-btn outline sm" href={e.url} target="_blank" rel="noreferrer">Join</a>}</li>))}</ul>
          )}
          <CardHead title="Unread mail" right={<button className="ee-btn outline sm" onClick={() => nav("inbox")}>Inbox</button>} />
          {(mail.data?.messages ?? []).length === 0 ? <p className="muted">All caught up.</p> : (
            <ul className="rc-list">{mail.data!.messages.map((m) => (
              <li key={m.id} onClick={() => nav("inbox", m.id)}><span className="m"><b className="ellipsis">{m.subject}</b><small className="ellipsis">{m.from.replace(/<.*>/, "")}</small></span></li>))}</ul>
          )}
        </Card>
        <Card>
          <CardHead title="Knowledge" right={<button className="ee-btn outline sm" onClick={() => nav("graph")}><Network {...ICON_SM} /> Graph</button>} />
          <div className="rc-stats">
            <div><b>{d.data?.knowledge?.documents ?? 0}</b><small>documents</small></div>
            <div><b>{d.data?.knowledge?.graph?.nodes ?? 0}</b><small>entities</small></div>
            <div><b>{d.data?.knowledge?.graph?.edges ?? 0}</b><small>links</small></div>
          </div>
          {d.data?.knowledge?.graph?.types && <Bars items={Object.entries(d.data.knowledge.graph.types as Record<string, number>).map(([t, n]) => ({ label: t, value: n }))} />}
          <CardHead title="Connections" right={<button className="ee-btn outline sm" onClick={() => nav("connections")}><Plug {...ICON_SM} /> Manage</button>} />
          <div className="chips">
            <Chip tone={d.data?.connections?.google ? "pass" : "nc"}>Google {d.data?.connections?.google ? "connected" : "off"}</Chip>
            <Chip tone={d.data?.connections?.notion ? "pass" : "nc"}>Notion {d.data?.connections?.notion ? "connected" : "off"}</Chip>
            <Chip tone={d.data?.connections?.mail === "local" ? "nc" : "pass"}>Mail: {d.data?.connections?.mail}</Chip>
          </div>
        </Card>
      </div>
    </>
  );
}

function DayChart({ data }: { data: { day: string; done: number; failed: number; other: number; automations: number }[] }) {
  const W = 640, H = 220, pad = 28;
  const max = Math.max(1, ...data.map((d) => d.done + d.failed + d.other), ...data.map((d) => d.automations));
  const bw = (W - pad * 2) / data.length;
  const y = (v: number) => H - pad - (v / max) * (H - pad * 2);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="chart" role="img" aria-label="Tasks per day">
      {[0, 0.5, 1].map((f) => <g key={f}><line x1={pad} x2={W - pad} y1={y(max * f)} y2={y(max * f)} stroke="var(--line)" />
        <text x={pad - 6} y={y(max * f) + 4} textAnchor="end" className="chart-t">{Math.round(max * f)}</text></g>)}
      {data.map((d, i) => {
        const x = pad + i * bw + bw * 0.18, w = bw * 0.64;
        let base = 0;
        const seg = (v: number, c: string) => { const r = <rect key={c} x={x} width={w} y={y(base + v)} height={y(base) - y(base + v)} rx={3} fill={`var(${c})`} />; base += v; return v ? r : null; };
        return (
          <g key={d.day}>
            {seg(d.done, "--pass-dot")}{seg(d.failed, "--fail-dot")}{seg(d.other, "--nc")}
            {d.automations > 0 && <circle cx={x + w / 2} cy={y(d.automations)} r={3.5} fill="var(--lc-ready)" />}
            {(i % Math.ceil(data.length / 7) === 0 || i === data.length - 1) &&
              <text x={x + w / 2} y={H - 8} textAnchor="middle" className="chart-t">{new Date(d.day).toLocaleDateString([], { day: "numeric", month: "short" })}</text>}
            <title>{`${d.day}: ${d.done} done, ${d.failed} failed, ${d.other} other, ${d.automations} automation runs`}</title>
          </g>
        );
      })}
    </svg>
  );
}

function Bars({ items }: { items: { label: string; value: number }[] }) {
  const max = Math.max(1, ...items.map((i) => i.value));
  return (
    <ul className="hbars">
      {items.map((i) => (
        <li key={i.label}><span className="ellipsis">{i.label}</span>
          <span className="ee-bar"><i style={{ width: `${(i.value / max) * 100}%`, background: "var(--primary)" }} /></span>
          <b className="num">{i.value}</b></li>
      ))}
    </ul>
  );
}
