import { useCallback, useEffect, useState } from "react";
import { ApiError, connect, get, post, type Connection } from "./api";
import { PairView } from "./views/PairView";
import { TasksView } from "./views/TasksView";
import { KnowledgeView } from "./views/KnowledgeView";
import { SettingsView } from "./views/SettingsView";

type Tab = "tasks" | "knowledge" | "settings";

export function App() {
  const [conn, setConn] = useState<Connection | null>(null);
  const [paired, setPaired] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("tasks");
  const [status, setStatus] = useState<any>(null);

  const refreshStatus = useCallback(async () => {
    try {
      setStatus(await get("/api/status"));
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) setPaired(false);
    }
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const c = await connect();
        setConn(c);
        let s = await get<{ paired: boolean }>("/api/auth/session");
        const m = /[#&]pair=([A-Za-z0-9-]{4,32})/.exec(window.location.hash);
        if (m) {
          history.replaceState(null, "", window.location.pathname);   // drop the code from the address bar
          if (!s.paired) {
            try {
              await post("/api/auth/pair", { code: m[1] });
              s = { paired: true };
            } catch {
              /* expired or used: fall back to the manual pairing screen */
            }
          }
        }
        setPaired(s.paired);
      } catch (e: any) {
        setError(e?.message ?? String(e));
      }
    })();
  }, []);

  useEffect(() => {
    if (!paired) return;
    void refreshStatus();
    const t = setInterval(refreshStatus, 5000);
    return () => clearInterval(t);
  }, [paired, refreshStatus]);

  if (error) {
    return (
      <div className="center-screen">
        <div className="card narrow">
          <h1>Can't reach the Agentic OS backend</h1>
          <p className="muted">{error}</p>
          <button onClick={() => location.reload()}>Try again</button>
        </div>
      </div>
    );
  }
  if (!conn || paired === null) return <div className="center-screen muted">Starting…</div>;
  if (!paired) return <PairView onPaired={() => setPaired(true)} />;

  return (
    <div className="shell">
      <nav className="sidebar">
        <div className="brand">
          <span className="logo">◆</span> Agentic OS
        </div>
        {(["tasks", "knowledge", "settings"] as Tab[]).map((t) => (
          <button key={t} className={`nav ${tab === t ? "active" : ""}`} onClick={() => setTab(t)}>
            {{ tasks: "Tasks", knowledge: "Knowledge", settings: "Settings" }[t]}
            {t === "tasks" && status?.kernel?.awaiting_user > 0 && <span className="dot">{status.kernel.awaiting_user}</span>}
          </button>
        ))}
        <div className="sidebar-foot">
          <div className="muted small" title={status?.workspace_root}>
            Workspace
            <div className="mono ellipsis">{status?.workspace_root ?? "…"}</div>
          </div>
          <div className="muted small">
            Planner: {status?.planner?.llm_provider ? `AI (${status.planner.llm_provider})` : "rules (offline)"}
          </div>
          <div className="muted small">
            KB: {status?.knowledge?.documents ?? 0} docs · {status?.knowledge?.embedder}
          </div>
        </div>
      </nav>
      <main className="content">
        {tab === "tasks" && <TasksView onChange={refreshStatus} />}
        {tab === "knowledge" && <KnowledgeView />}
        {tab === "settings" && <SettingsView onChange={refreshStatus} />}
      </main>
    </div>
  );
}
