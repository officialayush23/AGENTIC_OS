import { useEffect, useRef, useState } from "react";
import { CalendarDays, CheckCircle2, ExternalLink, Mail, NotebookPen, Plug, RefreshCw, Unplug } from "lucide-react";
import { del, get, post, put } from "../api";
import { useLive } from "../App";
import { Banner, Card, CardHead, Chip, Field, ICON_SM, PageHeader, useToast } from "../ui";

type Conn = { google: any; notion: any; mail: any; apps: number };

function Steps({ items }: { items: (string | JSX.Element)[] }) {
  return (
    <ol className="steps-list" style={{ margin: "4px 0 14px" }}>
      {items.map((t, i) => (
        <li key={i} className="row nowrap" style={{ alignItems: "flex-start", gap: 12 }}>
          <span className="stp-n">{i + 1}</span><span className="ink2" style={{ fontSize: 13.5, paddingTop: 3 }}>{t}</span>
        </li>
      ))}
    </ol>
  );
}

const A = ({ href, children }: { href: string; children: any }) =>
  <a href={href} target="_blank" rel="noreferrer">{children} <ExternalLink size={12} /></a>;

export function ConnectionsView() {
  const { bump, nav } = useLive();
  const toast = useToast();
  const [c, setC] = useState<Conn | null>(null);
  const load = async () => setC(await get<Conn>("/api/connections"));
  useEffect(() => { void load(); }, [bump]);
  const run = async (fn: () => Promise<any>, ok?: string) => {
    try { const r = await fn(); if (ok) toast({ title: ok }); await load(); return r; }
    catch (e: any) { toast({ title: "That didn't work", text: e.message, bad: true }); }
  };
  if (!c) return <p className="muted">Loading…</p>;
  const g = c.google, n = c.notion, m = c.mail;
  const count = [g.connected, n.connected, m.mode === "imap" || m.mode === "gmail"].filter(Boolean).length;

  return (
    <>
      <PageHeader section="System" dot="--ws-system" title="Connections"
                  sub="Connect your accounts once. Reading is free; anything that sends, books or writes outside this computer asks you first." />
      <div className="kpis">
        <ConnTile icon={<Mail {...ICON_SM} />} label="Email" on={m.mode === "imap" || m.mode === "gmail"} word={m.mode === "gmail" ? "Gmail" : m.mode === "imap" ? m.address : "Demo mailbox"} />
        <ConnTile icon={<CalendarDays {...ICON_SM} />} label="Calendar & Meet" on={g.connected && g.services.includes("calendar")} word={g.connected && g.services.includes("calendar") ? "Google Calendar" : "Local calendar"} />
        <ConnTile icon={<NotebookPen {...ICON_SM} />} label="Notion" on={n.connected} word={n.connected ? (n.workspace || "Connected") : "Not connected"} />
        <ConnTile icon={<Plug {...ICON_SM} />} label="Connected" on={count > 0} word={`${count} of 3 accounts · ${c.apps} apps`} />
      </div>
      <div className="two">
        <GoogleCard g={g} run={run} reload={load} />
        <div className="stack" style={{ gap: 16 }}>
          <GmailQuickCard m={m} run={run} />
          <NotionCard n={n} run={run} />
          <Card>
            <CardHead title="Built in" sub="Always available, no account needed" />
            <div className="row">
              <button className="ee-btn outline sm" onClick={() => nav("notes")}><NotebookPen {...ICON_SM} /> Local notes</button>
              <button className="ee-btn outline sm" onClick={() => nav("calendar")}><CalendarDays {...ICON_SM} /> Local calendar (.ics)</button>
              <button className="ee-btn outline sm" onClick={() => nav("settings")}><Plug {...ICON_SM} /> Apps and AI providers</button>
            </div>
          </Card>
        </div>
      </div>
    </>
  );
}

function ConnTile({ icon, label, on, word }: { icon: any; label: string; on: boolean; word: string }) {
  return (
    <div className="ee-card ee-kpi">
      <div className="top"><span>{label}</span><span className="ic">{icon}</span></div>
      <div><Chip tone={on ? "pass" : "nc"}>{on ? "Connected" : "Not connected"}</Chip></div>
      <div className="muted ellipsis">{word}</div>
    </div>
  );
}

function GoogleCard({ g, run, reload }: { g: any; run: (f: () => Promise<any>, ok?: string) => Promise<any>; reload: () => Promise<void> }) {
  const toast = useToast();
  const [id, setId] = useState("");
  const [secret, setSecret] = useState("");
  const [svc, setSvc] = useState<{ gmail: boolean; calendar: boolean }>({ gmail: true, calendar: true });
  const [waiting, setWaiting] = useState<string | null>(null);
  const poll = useRef<number | null>(null);
  useEffect(() => () => { if (poll.current) clearInterval(poll.current); }, []);
  const start = async () => {
    const r = await run(() => post("/api/connections/google/start", { services: Object.entries(svc).filter(([, v]) => v).map(([k]) => k) }));
    if (!r) return;
    setWaiting(r.url);
    if (!r.opened) window.open(r.url, "_blank");
    let n = 0;
    poll.current = window.setInterval(async () => {
      n++;
      const s = await get<any>("/api/connections");
      if (s.google.connected || n > 90) {
        if (poll.current) clearInterval(poll.current);
        setWaiting(null);
        await reload();
        if (s.google.connected) toast({ title: "Google connected", text: s.google.email });
      }
    }, 2000);
  };
  return (
    <Card>
      <CardHead title="Google: Gmail, Calendar and Meet"
                sub={g.connected ? `Signed in as ${g.email}` : "Sign in once; the agent reads mail and calendar, and asks before sending or booking"}
                right={<Chip tone={g.connected ? "pass" : g.configured ? "review" : "nc"}>{g.connected ? "Connected" : g.configured ? "Ready to sign in" : "Not set up"}</Chip>} />
      {g.connected ? (
        <div className="stack">
          <div className="row">{g.services.map((s: string) => <span key={s} className="tag"><CheckCircle2 size={12} />&nbsp;{s === "gmail" ? "Gmail: read, search, send" : "Calendar: events and Google Meet links"}</span>)}</div>
          <div className="row">
            <button className="ee-btn outline" onClick={() => run(() => post("/api/connections/google/test"), "Google works")}><RefreshCw {...ICON_SM} /> Test</button>
            <button className="ee-btn outline" onClick={start}>Change permissions</button>
            <button className="ee-btn ghost" onClick={() => run(() => post("/api/connections/google/disconnect"), "Google disconnected")}><Unplug {...ICON_SM} /> Disconnect</button>
          </div>
        </div>
      ) : (
        <>
          {!g.configured && <>
            <p className="ink2" style={{ marginTop: 0 }}>Google requires your own sign-in app. It takes about five minutes, once:</p>
            <Steps items={[
              <>Open <A href="https://console.cloud.google.com/projectcreate">Google Cloud</A> and create a project (any name).</>,
              <>Enable the <A href="https://console.cloud.google.com/apis/library/gmail.googleapis.com">Gmail API</A> and the <A href="https://console.cloud.google.com/apis/library/calendar-json.googleapis.com">Google Calendar API</A>.</>,
              <>Set up the <A href="https://console.cloud.google.com/auth/branding">OAuth consent screen</A>: External, then add your own address under Audience → Test users.</>,
              <>Create <A href="https://console.cloud.google.com/auth/clients/create">an OAuth client</A> of type <b>Desktop app</b>, and paste its ID and secret here.</>,
            ]} />
            <div className="form-grid">
              <Field label="Client ID"><input className="input mono" value={id} onChange={(e) => setId(e.target.value)} placeholder="1234-abc.apps.googleusercontent.com" /></Field>
              <Field label="Client secret" hint="Stored in the Windows credential store."><input className="input mono" type="password" value={secret} onChange={(e) => setSecret(e.target.value)} placeholder="GOCSPX-…" /></Field>
            </div>
            <div className="row" style={{ marginTop: 12 }}>
              <button className="ee-btn primary" disabled={!id || !secret} onClick={() => run(() => put("/api/connections/google/client", { client_id: id, client_secret: secret }), "Saved. Now sign in.")}>Save</button>
            </div>
          </>}
          {g.configured && (
            <div className="stack">
              <div className="row">
                <label className="check"><input type="checkbox" checked={svc.gmail} onChange={(e) => setSvc({ ...svc, gmail: e.target.checked })} /> Gmail</label>
                <label className="check"><input type="checkbox" checked={svc.calendar} onChange={(e) => setSvc({ ...svc, calendar: e.target.checked })} /> Calendar and Meet</label>
              </div>
              <div className="row">
                <button className="ee-btn primary" disabled={!svc.gmail && !svc.calendar} onClick={start}>Sign in with Google</button>
              </div>
              {waiting && <Banner tone="info"><p>Finish signing in in the browser tab that opened. If none opened, <a href={waiting} target="_blank" rel="noreferrer">open the Google sign-in page</a>. Google may warn that the app is unverified: it is your own app, choose Continue.</p></Banner>}
            </div>
          )}
        </>
      )}
    </Card>
  );
}

function GmailQuickCard({ m, run }: { m: any; run: (f: () => Promise<any>, ok?: string) => Promise<any> }) {
  const [addr, setAddr] = useState("");
  const [pw, setPw] = useState("");
  const on = m.mode === "imap";
  return (
    <Card>
      <CardHead title="Gmail with an app password" sub="Quicker than Google sign-in: email only, no Cloud project"
                right={<Chip tone={on ? "pass" : "nc"}>{on ? `Connected: ${m.address}` : "Not used"}</Chip>} />
      <Steps items={[<>Turn on <A href="https://myaccount.google.com/signinoptions/twosv">2-Step Verification</A>.</>,
                     <>Create an <A href="https://myaccount.google.com/apppasswords">app password</A> named “Agentic OS”, and paste the 16 letters below.</>]} />
      <div className="form-grid">
        <Field label="Gmail address"><input className="input" value={addr} onChange={(e) => setAddr(e.target.value)} placeholder="you@gmail.com" /></Field>
        <Field label="App password"><input className="input mono" type="password" value={pw} onChange={(e) => setPw(e.target.value)} placeholder="abcd efgh ijkl mnop" /></Field>
      </div>
      <div className="row" style={{ marginTop: 12 }}>
        <button className="ee-btn outline" disabled={!addr || pw.replace(/\s/g, "").length < 16} onClick={() => run(() => put("/api/connections/gmail/app-password", { address: addr, password: pw }), "Gmail connected")}>Connect Gmail</button>
        {on && <button className="ee-btn ghost" onClick={() => run(() => post("/api/connections/mail/mode", { mode: "local" }), "Switched to the demo mailbox")}>Stop using it</button>}
      </div>
    </Card>
  );
}

function NotionCard({ n, run }: { n: any; run: (f: () => Promise<any>, ok?: string) => Promise<any> }) {
  const [tok, setTok] = useState("");
  const [q, setQ] = useState("");
  const [pages, setPages] = useState<any[] | null>(null);
  return (
    <Card>
      <CardHead title="Notion" sub={n.connected ? `Workspace: ${n.workspace ?? "connected"}` : "Search, read and write pages you share with Agentic OS"}
                right={<Chip tone={n.connected ? "pass" : "nc"}>{n.connected ? "Connected" : "Not connected"}</Chip>} />
      {!n.connected ? (
        <>
          <Steps items={[<>Open <A href="https://www.notion.so/profile/integrations">Notion integrations</A> → New integration (internal), and copy its secret.</>,
                         "Paste the secret here.",
                         "In Notion, open each page the agent may use → ··· → Connections → add your integration."]} />
          <div className="row nowrap">
            <input className="input mono" type="password" value={tok} onChange={(e) => setTok(e.target.value)} placeholder="ntn_…" aria-label="Notion secret" />
            <button className="ee-btn outline" disabled={tok.length < 10} onClick={() => run(() => put("/api/connections/notion", { token: tok }), "Notion connected")}>Connect</button>
          </div>
        </>
      ) : (
        <div className="stack">
          <p className="ink2" style={{ margin: 0 }}>New pages go under: <b>{n.default_parent_title ?? "not chosen yet"}</b></p>
          <form className="row nowrap" onSubmit={async (e) => { e.preventDefault(); const r = await run(() => get(`/api/connections/notion/pages?q=${encodeURIComponent(q)}`)); if (r) setPages(r.pages); }}>
            <input className="input" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Find a page for new notes" />
            <button className="ee-btn outline">Find</button>
          </form>
          {pages && (pages.length === 0 ? <p className="muted">No shared pages match. Share pages with the integration in Notion.</p> : (
            <ul className="plain-list">{pages.map((p) => (
              <li key={p.id} className="plain-row"><span className="m"><b>{p.title}</b><small className="ellipsis">{p.url}</small></span>
                <button className="ee-btn outline sm" onClick={() => run(() => put("/api/connections/notion/default", { page: p.id, title: p.title }), "Default page set")}>Use</button></li>))}</ul>
          ))}
          <div className="row"><button className="ee-btn ghost" onClick={() => run(() => del("/api/connections/notion"), "Notion disconnected")}><Unplug {...ICON_SM} /> Disconnect</button></div>
        </div>
      )}
    </Card>
  );
}
