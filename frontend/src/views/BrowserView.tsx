import { useState } from "react";
import { Camera, FileText, Globe, KeyRound, MousePointerClick, ShieldCheck } from "lucide-react";
import { get, post, put } from "../api";
import { useLive } from "../App";
import type { Task } from "../types";
import { Banner, Card, CardHead, Chip, Empty, Field, ICON, ICON_SM, PageHeader, Seg, Switch, ago, taskStatus, useAsync, useToast } from "../ui";

export function BrowserView() {
  const { tasks, nav, refresh, bump } = useLive();
  const toast = useToast();
  const st = useAsync(() => get<any>("/api/browser/status"), [bump]);
  const [url, setUrl] = useState("");
  const [mode, setMode] = useState<"browser" | "fetch" | "shot">("browser");
  const webTasks = tasks.filter((t) => !t.ppid && t.plan?.steps.some((s) => s.tool.startsWith("browser_") || s.tool === "web_fetch")).slice(0, 12);

  const go = async () => {
    const u = url.trim();
    if (!u) return;
    const steps = mode === "fetch"
      ? [{ id: "fetch", title: `Read ${u}`, tool: "web_fetch", args: { url: u } }]
      : [{ id: "open", title: `Open ${u}`, tool: "browser_open", args: { url: u } },
         ...(mode === "shot" ? [{ id: "shot", title: "Take a screenshot", tool: "browser_screenshot", args: { url: u }, depends_on: ["open"] }] : [])];
    try {
      const t = await post<Task>("/api/tasks", { request: `${mode === "fetch" ? "Read" : "Open"} ${u}`, title: `${mode === "fetch" ? "Read" : mode === "shot" ? "Screenshot" : "Open"} ${u}`, plan: { goal: "Use the web", steps } });
      setUrl("");
      refresh();
      nav("tasks", t.pid);
    } catch (e: any) { toast({ title: "Could not start", text: e.message, bad: true }); }
  };
  const set = async (patch: any) => {
    try { await put("/api/settings", patch); st.reload(); } catch (e: any) { toast({ title: "Not saved", text: e.message, bad: true }); }
  };
  const s = st.data;

  return (
    <>
      <PageHeader section="Automation" dot="--ws-review" title="Browser"
                  sub="The agent drives Microsoft Edge or Google Chrome. It asks before reading a new site, and before every click or keystroke." />
      <div className="two">
        <Card>
          <CardHead title="Open a page" sub="Starts a task; the first visit to a site asks for permission." />
          <Seg label="Mode" value={mode} onChange={setMode} items={[{ id: "browser", label: "Real browser" }, { id: "shot", label: "With screenshot" }, { id: "fetch", label: "Text only" }]} />
          <form className="row nowrap" style={{ marginTop: 12 }} onSubmit={(e) => { e.preventDefault(); void go(); }}>
            <input className="input" placeholder="example.com" aria-label="Web address" value={url} onChange={(e) => setUrl(e.target.value)} />
            <button className="ee-btn primary" disabled={!url.trim()}><Globe {...ICON_SM} /> Go</button>
          </form>
          <p className="muted" style={{ marginBottom: 0 }}>Ask in plain words too, for example “Open news.ycombinator.com and list the top stories”.</p>
        </Card>
        <Card>
          <CardHead title="Browser engine" sub={s?.channel ? `Using ${s.channel}` : "Edge on Windows, Chrome elsewhere (automatic)"}
                    right={<Chip tone={s?.available === false ? "fail" : s?.available ? "pass" : "nc"}>{s?.available === false ? "Unavailable" : s?.available ? "Ready" : "Not started"}</Chip>} />
          {s?.last_error && <Banner tone="bad">{s.last_error}</Banner>}
          <div className="stack" style={{ gap: 4 }}>
            <Switch checked={!!s?.headed} onChange={(v) => set({ browser_headed: v })} label="Show the browser window while it works" />
            <Switch checked={!!s?.allow_private} onChange={(v) => set({ web_allow_private: v })} label="Allow pages on this computer and the local network" />
          </div>
          <Field label="Browser">
            <select className="select" value={s?.channel ?? "auto"} onChange={(e) => set({ browser_channel: e.target.value })}>
              <option value="auto">Automatic</option><option value="msedge">Microsoft Edge</option><option value="chrome">Google Chrome</option>
            </select>
          </Field>
        </Card>
      </div>

      <ol className="ee-steps">
        <li className="ee-step"><span className="n"><b><KeyRound size={15} strokeWidth={1.8} /></b>Read needs a grant</span><p>The first read of a site asks you once. The grant covers that site and its subdomains, for this task or an hour.</p></li>
        <li className="ee-step"><span className="n"><b><MousePointerClick size={15} strokeWidth={1.8} /></b>Every action asks</span><p>Clicks and typing can submit or buy, so each one shows the exact element and text first, and the grant is used once.</p></li>
        <li className="ee-step"><span className="n"><b><ShieldCheck size={15} strokeWidth={1.8} /></b>Pages are data</span><p>Text from websites is passed to the agent as data. Instructions inside a page are never followed as yours.</p></li>
        <li className="ee-step"><span className="n"><b><Camera size={15} strokeWidth={1.8} /></b>Private sessions</span><p>Each task gets its own browser profile with no saved sign-ins, closed when the task ends.</p></li>
      </ol>

      <Card>
        <CardHead title="Recent web tasks" sub={`${webTasks.length} shown`} />
        {webTasks.length === 0 ? <Empty icon={<FileText {...ICON} />}>No web tasks yet.</Empty> : (
          <ul className="plain-list">
            {webTasks.map((t) => {
              const x = taskStatus(t);
              return (
                <li key={t.pid}><button className="ee-item" style={{ background: "var(--soft)" }} onClick={() => nav("tasks", t.pid)}>
                  <span className="av"><Globe {...ICON_SM} /></span>
                  <span className="m"><b>{t.title}</b><small>{x.next}</small></span>
                  <span className="r"><Chip tone={x.tone}>{x.word}</Chip>{ago(t.created_at)}</span>
                </button></li>
              );
            })}
          </ul>
        )}
      </Card>
    </>
  );
}
