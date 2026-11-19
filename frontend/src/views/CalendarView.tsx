import { useMemo, useState } from "react";
import { CalendarPlus, ChevronLeft, ChevronRight, Download, Users, Video } from "lucide-react";
import { blobUrl, del, get, post } from "../api";
import { useLive } from "../App";
import type { CalEvent, Task } from "../types";
import { Banner, Card, CardHead, Dialog, Field, ICON_SM, PageHeader, Switch, useAsync, useToast } from "../ui";

const iso = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const hm = (s: string) => s.slice(11, 16);

function startOfWeek(d: Date): Date {
  const x = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  x.setDate(x.getDate() - ((x.getDay() + 6) % 7));       // Monday
  return x;
}

export function CalendarView() {
  const { bump, nav, refresh } = useLive();
  const toast = useToast();
  const [week, setWeek] = useState(() => startOfWeek(new Date()));
  const [open, setOpen] = useState<CalEvent | null>(null);
  const [adding, setAdding] = useState(false);
  const [meeting, setMeeting] = useState(false);
  const data = useAsync(() => get<{ events: CalEvent[] }>(`/api/calendar?start=${iso(week)}&days=7`), [week, bump]);
  const days = useMemo(() => Array.from({ length: 7 }, (_, i) => { const d = new Date(week); d.setDate(d.getDate() + i); return d; }), [week]);
  const evs = data.data?.events ?? [];
  const todayIso = iso(new Date());
  const shift = (n: number) => setWeek((w) => { const d = new Date(w); d.setDate(d.getDate() + n * 7); return d; });

  const download = async () => {
    const u = await blobUrl("/api/calendar.ics");
    const a = document.createElement("a");
    a.href = u; a.download = "calendar.ics"; a.click();
    setTimeout(() => URL.revokeObjectURL(u), 2000);
  };

  return (
    <>
      <PageHeader section="Communication" dot="--ws-setup" title="Calendar and meetings"
                  sub="A local calendar file in your Home folder. Meetings get a video-call link; invitations go out by email after you approve."
                  actions={<>
                    <button className="ee-btn outline" onClick={() => setAdding(true)}><CalendarPlus {...ICON_SM} /> Add event</button>
                    <button className="ee-btn primary" onClick={() => setMeeting(true)}><Users {...ICON_SM} /> Schedule a meeting</button>
                  </>} />
      <Card>
        <CardHead title={`${days[0].toLocaleDateString([], { month: "long", day: "numeric" })} – ${days[6].toLocaleDateString([], { month: "long", day: "numeric", year: "numeric" })}`}
                  sub={`${evs.length} event${evs.length === 1 ? "" : "s"} this week`}
                  right={<div className="row">
                    <button className="ee-btn secondary round" aria-label="Previous week" onClick={() => shift(-1)}><ChevronLeft {...ICON_SM} /></button>
                    <button className="ee-btn secondary sm" onClick={() => setWeek(startOfWeek(new Date()))}>This week</button>
                    <button className="ee-btn secondary round" aria-label="Next week" onClick={() => shift(1)}><ChevronRight {...ICON_SM} /></button>
                    <button className="ee-btn ghost sm" onClick={download}><Download {...ICON_SM} /> .ics</button>
                  </div>} />
        {data.error && <Banner tone="bad">{data.error}</Banner>}
        <div className="week">
          {days.map((d) => {
            const k = iso(d);
            const list = evs.filter((e) => e.start.slice(0, 10) === k);
            return (
              <div key={k} className={`day ${k === todayIso ? "today" : ""}`}>
                <h4>{d.toLocaleDateString([], { weekday: "short" })} <span className="num">{d.getDate()}</span></h4>
                {list.length === 0 && <span className="muted">Free</span>}
                {list.map((e) => (
                  <button key={e.uid} className="ev" onClick={() => setOpen(e)}>
                    <small>{hm(e.start)}{e.end ? `–${hm(e.end)}` : ""}</small>
                    <b>{e.title}</b>
                    {(e.url || e.attendees.length > 0) && <small>{e.url ? "Video call" : ""}{e.url && e.attendees.length ? " · " : ""}{e.attendees.length ? `${e.attendees.length} invited` : ""}</small>}
                  </button>
                ))}
              </div>
            );
          })}
        </div>
      </Card>

      {open && (
        <Dialog title={open.title} onClose={() => setOpen(null)}>
          <dl className="kv">
            <dt>When</dt><dd>{new Date(open.start).toLocaleString()}{open.end ? ` – ${hm(open.end)}` : ""}</dd>
            {open.location && <><dt>Where</dt><dd>{open.location}</dd></>}
            {open.attendees.length > 0 && <><dt>Invited</dt><dd>{open.attendees.join(", ")}</dd></>}
            {open.description && <><dt>Notes</dt><dd style={{ whiteSpace: "pre-wrap" }}>{open.description}</dd></>}
          </dl>
          <div className="row">
            {open.url && <a className="ee-btn primary" href={open.url} target="_blank" rel="noreferrer"><Video {...ICON_SM} /> Join call</a>}
            <button className="ee-btn destructive" onClick={async () => {
              await del(`/api/calendar/events/${encodeURIComponent(open.uid)}`); setOpen(null); data.reload(); toast({ title: "Event removed" });
            }}>Remove</button>
          </div>
        </Dialog>
      )}
      {adding && <AddEvent onClose={() => setAdding(false)} onDone={() => { setAdding(false); data.reload(); }} />}
      {meeting && <MeetingDialog onClose={() => setMeeting(false)} onStarted={(pid) => { setMeeting(false); refresh(); nav("tasks", pid); }} />}
    </>
  );
}

function AddEvent({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [title, setTitle] = useState("");
  const [start, setStart] = useState(() => { const d = new Date(Date.now() + 3600e3); d.setMinutes(0); return `${iso(d)}T${String(d.getHours()).padStart(2, "0")}:00`; });
  const [dur, setDur] = useState(30);
  const [online, setOnline] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  return (
    <Dialog title="Add event" onClose={onClose}>
      <p className="muted" style={{ margin: 0 }}>You're editing your own calendar, so this is saved straight away.</p>
      <Field label="Title"><input className="input" value={title} onChange={(e) => setTitle(e.target.value)} autoFocus /></Field>
      <div className="form-grid">
        <Field label="Starts"><input className="input" type="datetime-local" value={start} onChange={(e) => setStart(e.target.value)} /></Field>
        <Field label="Length (minutes)"><input className="input" type="number" min={5} step={5} value={dur} onChange={(e) => setDur(Number(e.target.value))} /></Field>
      </div>
      <Switch checked={online} onChange={setOnline} label="Add a video-call link" />
      {err && <Banner tone="bad">{err}</Banner>}
      <div className="row"><button className="ee-btn primary" disabled={!title.trim()} onClick={async () => {
        try { await post("/api/calendar/events", { title, start, duration_minutes: dur, online_meeting: online }); toast({ title: "Event added", text: title }); onDone(); }
        catch (e: any) { setErr(e.message); }
      }}>Add</button></div>
    </Dialog>
  );
}

function MeetingDialog({ onClose, onStarted }: { onClose: () => void; onStarted: (pid: string) => void }) {
  const [title, setTitle] = useState("");
  const [who, setWho] = useState("");
  const [start, setStart] = useState(() => { const d = new Date(Date.now() + 86400e3); return `${iso(d)}T11:00`; });
  const [dur, setDur] = useState(30);
  const [online, setOnline] = useState(true);
  const [note, setNote] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const go = async () => {
    const attendees = who.split(/[,;\s]+/).filter(Boolean);
    const steps: any[] = [{ id: "event", title: `Add '${title}' to the calendar`, tool: "calendar_add_event",
      args: { title, start, duration_minutes: dur, attendees, online_meeting: online, description: note || undefined } }];
    if (attendees.length) {
      steps.push({ id: "invite", title: "Email the invitation", tool: "email_send", args: {
        to: attendees, subject: `Invitation: ${title}`,
        body: `Hi,\n\nYou're invited to '${title}' on ${start.replace("T", " at ")} (${dur} minutes).${online ? "\nThe video-call link is in the attached invitation." : ""}${note ? `\n\n${note}` : ""}\n`,
        attachments: [{ $ref: "event.output.invite_file" }] } });
    }
    try {
      const t = await post<Task>("/api/tasks", { request: `Schedule '${title}' on ${start}${attendees.length ? ` with ${attendees.join(", ")}` : ""}`,
        title: `Meeting: ${title}`, plan: { goal: "Schedule a meeting", steps } });
      onStarted(t.pid);
    } catch (e: any) { setErr(e.message); }
  };
  return (
    <Dialog title="Schedule a meeting" onClose={onClose}>
      <p className="muted" style={{ margin: 0 }}>The agent adds it to your calendar (you review the change), then emails the invitation after you commit and approve.</p>
      <Field label="Title"><input className="input" value={title} onChange={(e) => setTitle(e.target.value)} autoFocus /></Field>
      <Field label="People to invite" hint="Email addresses, separated by commas."><input className="input" value={who} onChange={(e) => setWho(e.target.value)} placeholder="priya@example.com" /></Field>
      <div className="form-grid">
        <Field label="Starts"><input className="input" type="datetime-local" value={start} onChange={(e) => setStart(e.target.value)} /></Field>
        <Field label="Length (minutes)"><input className="input" type="number" min={5} step={5} value={dur} onChange={(e) => setDur(Number(e.target.value))} /></Field>
      </div>
      <Field label="Agenda (optional)"><textarea className="textarea" rows={3} value={note} onChange={(e) => setNote(e.target.value)} /></Field>
      <Switch checked={online} onChange={setOnline} label="Video call (Jitsi Meet link)" />
      {err && <Banner tone="bad">{err}</Banner>}
      <div className="row"><button className="ee-btn primary" disabled={!title.trim()} onClick={go}>Plan the meeting</button></div>
    </Dialog>
  );
}
