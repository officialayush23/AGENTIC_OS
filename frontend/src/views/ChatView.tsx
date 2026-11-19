import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowUp, Bot, Mic, MicOff, Plus, Square, Trash2, Volume2, VolumeX } from "lucide-react";
import { api, del, get, post } from "../api";
import { useLive } from "../App";
import { ResultCards } from "../components/ResultCards";
import { speak, useVoice } from "../lib/voice";
import type { Task } from "../types";
import { Chip, ICON, ICON_SM, ago, greeting, taskStatus, useToast } from "../ui";
import { Decision, PlanView } from "./TasksView";

type Msg = { id: string; role: "user" | "assistant"; text: string; pid: string | null; meta: any; created_at: string; updated_at: string };
type Conv = { id: string; title: string; updated_at: string; last?: string; count?: number };

const STARTERS = [
  "What's on my calendar this week?",
  "Check my email and summarise anything urgent",
  "Schedule a meeting with priya@example.com tomorrow at 3pm",
  "Build a knowledge graph of my notes",
  "Note: ideas for the demo",
  "Open news.ycombinator.com",
];

function pref(key: string, def: boolean): boolean {
  try { const v = localStorage.getItem(key); return v === null ? def : v === "1"; } catch { return def; }
}
function setPref(key: string, v: boolean) { try { localStorage.setItem(key, v ? "1" : "0"); } catch { /* ignore */ } }

export function ChatView() {
  const { sel, nav, bump, refresh } = useLive();
  const toast = useToast();
  const [convs, setConvs] = useState<Conv[]>([]);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [aloud, setAloud] = useState(() => pref("aos.voice.aloud", true));
  const [handsFree, setHandsFree] = useState(() => pref("aos.voice.handsfree", false));
  const cid = sel;
  const end = useRef<HTMLDivElement | null>(null);
  const spoken = useRef<Set<string>>(new Set());
  const lastVoice = useRef(false);

  const loadList = useCallback(async () => setConvs((await get<{ chats: Conv[] }>("/api/chats")).chats), []);
  const loadConv = useCallback(async () => {
    if (!cid) { setMsgs([]); return; }
    try { setMsgs((await get<{ messages: Msg[] }>(`/api/chats/${cid}`)).messages); }
    catch { nav("chat"); }
  }, [cid, nav]);
  useEffect(() => { void loadList(); }, [loadList, bump]);
  useEffect(() => { void loadConv(); }, [loadConv, bump]);
  useEffect(() => { end.current?.scrollIntoView({ behavior: "smooth", block: "end" }); }, [msgs.length, msgs[msgs.length - 1]?.text]);

  const send = useCallback(async (t: string, voice = false) => {
    const body = t.trim();
    if (!body || busy) return;
    setBusy(true);
    lastVoice.current = voice;
    try {
      // If the last task is waiting for an answer to its question, the message answers it.
      const last = [...msgs].reverse().find((m) => m.role === "assistant" && m.pid);
      if (last?.pid) {
        const task = await get<Task>(`/api/tasks/${last.pid}`).catch(() => null);
        if (task?.pending_user?.kind === "clarify") {
          await api("POST", `/api/tasks/${last.pid}/answer`, { text: body });
          setText("");
          await loadConv();
          return;
        }
      }
      let id = cid;
      if (!id) { id = (await post<Conv>("/api/chats", {})).id; nav("chat", id); }
      await post(`/api/chats/${id}/messages`, { text: body, voice });
      setText("");
      refresh();
      if (id === cid) await loadConv();
      void loadList();
    } catch (e: any) {
      toast({ title: "Couldn't send", text: e.message, bad: true });
    } finally {
      setBusy(false);
    }
  }, [busy, cid, msgs, nav, loadConv, loadList, refresh, toast]);

  const voice = useVoice(useCallback((t: string) => { void send(t, true); }, [send]));

  // Speak finished answers (only for voice turns, or always when "read aloud" is on), then listen again if hands-free.
  useEffect(() => {
    const m = msgs[msgs.length - 1];
    if (!m || m.role !== "assistant" || !m.meta?.status || m.meta.status === "running" || spoken.current.has(m.id)) return;
    spoken.current.add(m.id);
    const ago_s = (Date.now() - Date.parse(m.created_at)) / 1000;
    if (ago_s > 120) return;
    if (aloud && (m.meta.voice || lastVoice.current)) {
      speak(m.text, true);
      if (handsFree && voice.mode !== "none") {
        const wait = () => { if (window.speechSynthesis?.speaking) setTimeout(wait, 300); else void voice.start(); };
        setTimeout(wait, 600);
      }
    }
  }, [msgs, aloud, handsFree, voice]);

  // Hold Ctrl+Space (or press it) to talk.
  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.code === "Space") { e.preventDefault(); voice.listening ? voice.stop() : void voice.start(); }
    };
    window.addEventListener("keydown", k);
    return () => window.removeEventListener("keydown", k);
  }, [voice]);

  const empty = msgs.length === 0;
  return (
    <div className="chat">
      <aside className="chat-list ee-card">
        <button className="ee-btn primary" onClick={() => nav("chat")}><Plus {...ICON_SM} /> New conversation</button>
        <div className="pane-scroll" style={{ marginTop: 12 }}>
          {convs.length === 0 && <p className="muted">Your conversations appear here.</p>}
          <ul className="plain-list">
            {convs.map((c) => (
              <li key={c.id}>
                <button className="ee-item" style={{ background: c.id === cid ? "var(--soft)" : "transparent", minHeight: 48 }}
                        aria-selected={c.id === cid} onClick={() => nav("chat", c.id)}>
                  <span className="m"><b>{c.title}</b><small>{ago(c.updated_at)} · {c.count ?? 0} messages</small></span>
                  <span className="r"><span role="button" aria-label="Delete conversation" className="chat-del"
                        onClick={async (e) => { e.stopPropagation(); await del(`/api/chats/${c.id}`); if (c.id === cid) nav("chat"); void loadList(); }}>
                    <Trash2 size={14} /></span></span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      </aside>

      <section className="chat-main">
        <div className="chat-scroll">
          {empty ? (
            <div className="chat-hero">
              <button className={`orb ${voice.listening ? "on" : ""}`} aria-label={voice.listening ? "Stop listening" : "Talk"}
                      onClick={() => (voice.listening ? voice.stop() : voice.start())}>
                {voice.listening ? <Square size={34} strokeWidth={1.6} /> : <Mic size={38} strokeWidth={1.6} />}
              </button>
              <h1 className="page-title" style={{ margin: "22px 0 6px" }}>{greeting()}</h1>
              <p className="ink2" style={{ margin: 0 }}>{voice.listening ? (voice.interim || "Listening…") : "Tap and talk, or type below. Ctrl+Space to talk anytime."}</p>
              {voice.error && <p className="err-text">{voice.error}</p>}
              <div className="chips" style={{ justifyContent: "center", marginTop: 24, maxWidth: 760 }}>
                {STARTERS.map((s) => <button key={s} className="suggest" onClick={() => send(s)}>{s}</button>)}
              </div>
            </div>
          ) : (
            <div className="thread">
              {msgs.map((m) => m.role === "user"
                ? <div key={m.id} className="bubble user">{m.meta?.voice && <Mic size={13} className="muted" />} {m.text}</div>
                : <AssistantMsg key={m.id} m={m} onChanged={loadConv} />)}
              <div ref={end} />
            </div>
          )}
        </div>

        <form className="chat-input ee-card" onSubmit={(e) => { e.preventDefault(); void send(text); }}>
          <button type="button" className={`mic ${voice.listening ? "on" : ""}`} aria-label={voice.listening ? "Stop listening" : "Talk"}
                  onClick={() => (voice.listening ? voice.stop() : voice.start())}>
            {voice.listening ? <MicOff {...ICON} /> : <Mic {...ICON} />}
          </button>
          <textarea rows={1} aria-label="Message" value={voice.listening ? voice.interim : text}
                    placeholder={voice.listening ? "Listening…" : "Ask or tell Agentic OS to do something"}
                    onChange={(e) => setText(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void send(text); } }} />
          <button type="button" className="ee-btn ghost round" aria-label={aloud ? "Stop reading replies aloud" : "Read replies aloud"}
                  title={aloud ? "Replies to voice are read aloud" : "Replies are not read aloud"}
                  onClick={() => { setAloud(!aloud); setPref("aos.voice.aloud", !aloud); window.speechSynthesis?.cancel(); }}>
            {aloud ? <Volume2 {...ICON_SM} /> : <VolumeX {...ICON_SM} />}
          </button>
          <label className="check small" title="After a spoken reply, listen again automatically">
            <input type="checkbox" checked={handsFree} onChange={(e) => { setHandsFree(e.target.checked); setPref("aos.voice.handsfree", e.target.checked); }} /> Hands-free
          </label>
          <button className="ee-btn primary round" aria-label="Send" disabled={busy || !text.trim()}><ArrowUp {...ICON} /></button>
        </form>
        {!empty && voice.error && <p className="err-text" style={{ textAlign: "center" }}>{voice.error}</p>}
      </section>
    </div>
  );
}

function AssistantMsg({ m, onChanged }: { m: Msg; onChanged: () => void }) {
  const { bump, refresh, nav } = useLive();
  const toast = useToast();
  const [task, setTask] = useState<Task | null>(null);
  const [showPlan, setShowPlan] = useState(false);
  useEffect(() => { if (m.pid) get<Task>(`/api/tasks/${m.pid}`).then(setTask).catch(() => setTask(null)); }, [m.pid, bump, m.updated_at]);
  const act = async (path: string, body: unknown = {}, done?: string) => {
    try { await api("POST", `/api/tasks/${m.pid}/${path}`, body); if (done) toast({ title: done }); refresh(); onChanged(); }
    catch (e: any) { toast({ title: "That didn't work", text: e.message, bad: true }); }
  };
  const s = task ? taskStatus(task) : null;
  const running = !m.meta?.status || m.meta.status === "running";
  return (
    <div className="bubble bot">
      <div className="bot-head">
        <span className="bot-av"><Bot size={16} strokeWidth={1.6} /></span>
        {s && <Chip tone={s.tone}>{s.word}</Chip>}
        {task?.plan && <button className="ee-btn ghost sm" onClick={() => setShowPlan(!showPlan)}>{showPlan ? "Hide steps" : `${task.plan.steps.length} step${task.plan.steps.length === 1 ? "" : "s"}`}</button>}
        {m.pid && <button className="ee-btn ghost sm" onClick={() => nav("tasks", m.pid!)}>Open task</button>}
      </div>
      {running && task && !task.pending_user ? <p className="bot-text muted">{s?.next ?? "Working on it…"}</p>
        : !task?.pending_user && <p className="bot-text">{m.text}</p>}
      {task?.pending_user && <Decision pending={task.pending_user} task={task} act={act} />}
      {showPlan && task && <div style={{ marginTop: 10 }}><PlanView task={task} /></div>}
      <ResultCards cards={m.meta?.cards ?? []} />
    </div>
  );
}
