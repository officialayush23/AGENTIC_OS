import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import {
  Bell, BookOpen, CalendarDays, CircleHelp, Globe, Home, Inbox, ListChecks, Moon, NotebookPen, Search, Settings, Sun,
  Workflow,
} from "lucide-react";
import { ApiError, connect, get, post, subscribe, type Connection } from "./api";
import type { Task } from "./types";
import { ICON, ToastHost, taskStatus } from "./ui";
import { PairView } from "./views/PairView";
import { HomeView } from "./views/HomeView";
import { TasksView } from "./views/TasksView";
import { InboxView } from "./views/InboxView";
import { CalendarView } from "./views/CalendarView";
import { NotesView } from "./views/NotesView";
import { AutomationsView } from "./views/AutomationsView";
import { BrowserView } from "./views/BrowserView";
import { KnowledgeView } from "./views/KnowledgeView";
import { SettingsView } from "./views/SettingsView";

export type View = "home" | "tasks" | "inbox" | "calendar" | "notes" | "automations" | "browser" | "knowledge" | "settings";

const NAV: ({ id: View; label: string; icon: any } | { sep: string })[] = [
  { id: "home", label: "Home", icon: Home },
  { id: "tasks", label: "Tasks", icon: ListChecks },
  { sep: "--ws-overview" },
  { id: "inbox", label: "Inbox", icon: Inbox },
  { id: "calendar", label: "Calendar and meetings", icon: CalendarDays },
  { id: "notes", label: "Notes", icon: NotebookPen },
  { sep: "--ws-setup" },
  { id: "automations", label: "Schedules and listeners", icon: Workflow },
  { id: "browser", label: "Browser", icon: Globe },
  { id: "knowledge", label: "Knowledge", icon: BookOpen },
  { sep: "--ws-system" },
  { id: "settings", label: "Settings", icon: Settings },
];
const TITLES: Record<View, string> = {
  home: "Home", tasks: "Tasks", inbox: "Inbox", calendar: "Calendar", notes: "Notes", automations: "Automations",
  browser: "Browser", knowledge: "Knowledge", settings: "Settings",
};

// ---------------------------------------------------------------- live state
interface Live {
  tasks: Task[];
  status: any;
  refresh: () => void;
  nav: (v: View, sel?: string | null) => void;
  view: View;
  sel: string | null;
  run: (request: string) => Promise<Task>;
  bump: number;               // increments on any server event (views reload on it)
}
const LiveCtx = createContext<Live>(null as unknown as Live);
export const useLive = () => useContext(LiveCtx);

function parseHash(): { view: View; sel: string | null } {
  const m = /^#\/(\w+)(?:\/(.+))?/.exec(window.location.hash);
  const v = (m?.[1] ?? "home") as View;
  return { view: v in TITLES ? v : "home", sel: m?.[2] ? decodeURIComponent(m[2]) : null };
}

export function App() {
  const [conn, setConn] = useState<Connection | null>(null);
  const [paired, setPaired] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const c = await connect();
        setConn(c);
        let s = await get<{ paired: boolean }>("/api/auth/session");
        const m = /[#&]pair=([A-Za-z0-9-]{4,32})/.exec(window.location.hash);
        if (m) {
          history.replaceState(null, "", window.location.pathname + "#/home");
          if (!s.paired) {
            try { await post("/api/auth/pair", { code: m[1] }); s = { paired: true }; } catch { /* fall back to manual */ }
          }
        }
        setPaired(s.paired);
      } catch (e: any) {
        setError(e?.message ?? String(e));
      }
    })();
  }, []);

  if (error) {
    return (
      <div className="center">
        <div className="ee-card pair">
          <h1 className="page-title">Can't reach Agentic OS</h1>
          <p className="ink2">The backend did not answer: {error}</p>
          <button className="ee-btn primary" onClick={() => location.reload()}>Try again</button>
        </div>
      </div>
    );
  }
  if (!conn || paired === null) return <div className="center"><div className="skeleton" style={{ width: 320, height: 160 }} /></div>;
  if (!paired) return <PairView onPaired={() => setPaired(true)} />;
  return <ToastHost><Shell /></ToastHost>;
}

function Shell() {
  const [{ view, sel }, setRoute] = useState(parseHash);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [status, setStatus] = useState<any>(null);
  const [bump, setBump] = useState(0);
  const [theme, setTheme] = useState<string>(() => {
    try { return localStorage.getItem("aos.theme") || "light"; } catch { return "light"; }
  });
  const [bell, setBell] = useState(false);
  const [q, setQ] = useState("");
  const timer = useRef<number | null>(null);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("aos.theme", theme); } catch { /* ignore */ }
  }, [theme]);

  useEffect(() => {
    const on = () => setRoute(parseHash());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);

  const refresh = useCallback(async () => {
    try {
      const [t, s] = await Promise.all([get<{ tasks: Task[] }>("/api/tasks"), get("/api/status")]);
      setTasks(t.tasks);
      setStatus(s);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) location.reload();
    }
  }, []);

  useEffect(() => {
    void refresh();
    const ctl = new AbortController();
    subscribe("/api/events", () => {
      // coalesce bursts of kernel events into one refresh
      if (timer.current) return;
      timer.current = window.setTimeout(() => { timer.current = null; void refresh(); setBump((b) => b + 1); }, 150);
    }, ctl.signal);
    const poll = setInterval(refresh, 5000);
    return () => { ctl.abort(); clearInterval(poll); };
  }, [refresh]);

  const nav = useCallback((v: View, s?: string | null) => {
    window.location.hash = `#/${v}${s ? "/" + encodeURIComponent(s) : ""}`;
  }, []);

  const run = useCallback(async (request: string) => {
    const t = await post<Task>("/api/tasks", { request });
    void refresh();
    return t;
  }, [refresh]);

  const waiting = useMemo(() => tasks.filter((t) => t.pending_user && t.state === "BLOCKED_USER"), [tasks]);
  const live = useMemo<Live>(() => ({ tasks, status, refresh, nav, view, sel, run, bump }), [tasks, status, refresh, nav, view, sel, run, bump]);
  const running = status?.kernel?.live ?? 0;
  const meter = waiting.length ? { cls: "attention", word: `${waiting.length} need${waiting.length === 1 ? "s" : ""} you` }
    : running ? { cls: "working", word: `Working on ${running}` } : { cls: "", word: "Ready" };

  return (
    <LiveCtx.Provider value={live}>
      <div className="app">
        <aside className="rail-col">
          <div className="ee-rail">
            <button className="ee-mark" aria-label="Agentic OS home" onClick={() => nav("home")}>
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
                <path d="M12 3 4 7.5v9L12 21l8-4.5v-9L12 3Z" /><path d="M12 12 4 7.5M12 12l8-4.5M12 12v9" />
              </svg>
            </button>
            <nav aria-label="Main">
              {NAV.map((n, i) => "sep" in n
                ? <span key={i} className="sep" style={{ background: `var(${n.sep})` }} aria-hidden />
                : (
                  <button key={n.id} className="nav" aria-label={n.label} title={n.label}
                          aria-current={view === n.id ? "page" : undefined} onClick={() => nav(n.id)}>
                    <n.icon {...ICON} />
                    {n.id === "tasks" && waiting.length > 0 && <span className="dot" aria-label={`${waiting.length} waiting`} />}
                  </button>
                ))}
            </nav>
          </div>
        </aside>
        <div className="main-col">
          <header className="ee-top">
            <button className="ee-crumb" onClick={() => nav("home")}>Agentic OS</button>
            {view !== "home" && <><span className="crumb-sep" aria-hidden>›</span><span className="ee-crumb on">{TITLES[view]}</span></>}
            <form className="ee-search" role="search" onSubmit={(e) => { e.preventDefault(); if (q.trim()) { nav("knowledge", q.trim()); setQ(""); } }}>
              <Search size={16} strokeWidth={1.6} aria-hidden />
              <input aria-label="Search notes, files and knowledge" placeholder="Search notes and files" value={q}
                     onChange={(e) => setQ(e.target.value)} />
            </form>
            <button className="ee-round" aria-label="Help" title="Every task is a transaction: staged, reviewed, committed, undoable."
                    onClick={() => nav("home")}><CircleHelp {...ICON} /></button>
            <button className="ee-round" aria-label={theme === "dark" ? "Use light theme" : "Use dark theme"}
                    onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>
              {theme === "dark" ? <Sun {...ICON} /> : <Moon {...ICON} />}
            </button>
            <div style={{ position: "relative" }}>
              <button className="ee-round" aria-label="Notifications" aria-expanded={bell} onClick={() => setBell(!bell)}>
                <Bell {...ICON} />{waiting.length > 0 && <span className="dot" />}
              </button>
              {bell && (
                <div className="popover" onMouseLeave={() => setBell(false)}>
                  {waiting.length === 0 ? <p className="muted" style={{ padding: 12, margin: 0 }}>Nothing needs you right now.</p>
                    : waiting.map((t) => {
                      const s = taskStatus(t);
                      return (
                        <button key={t.pid} className="ee-item" onClick={() => { setBell(false); nav("tasks", t.pid); }}>
                          <span className="m"><b>{t.title}</b><small>{s.next}</small></span>
                          <span className={`ee-chip ${s.tone}`}><i />{s.word}</span>
                        </button>
                      );
                    })}
                </div>
              )}
            </div>
            <button className={`ee-meter ${meter.cls}`} onClick={() => nav("tasks")}
                    title={status ? `${status.kernel?.processes ?? 0} tasks · planner: ${status.planner?.llm_provider ? "AI" : "offline rules"}` : ""}>
              <i aria-hidden />{meter.word}
            </button>
          </header>
          <div className="scroll">
            <main className="content" key={view}>
              {view === "home" && <HomeView />}
              {view === "tasks" && <TasksView />}
              {view === "inbox" && <InboxView />}
              {view === "calendar" && <CalendarView />}
              {view === "notes" && <NotesView />}
              {view === "automations" && <AutomationsView />}
              {view === "browser" && <BrowserView />}
              {view === "knowledge" && <KnowledgeView />}
              {view === "settings" && <SettingsView />}
            </main>
          </div>
        </div>
      </div>
    </LiveCtx.Provider>
  );
}
