import { useEffect, useState } from "react";
import { Inbox, Mail, MailPlus, Paperclip, Reply, Search, Sparkles } from "lucide-react";
import { get, post } from "../api";
import { useLive } from "../App";
import type { MailMsg, Task } from "../types";
import { Banner, Chip, Dialog, Empty, Field, Filter, ICON, ICON_SM, PageHeader, Seg, SplitPanes, ago, useAsync, useToast } from "../ui";

export function ComposeMail({ init, onClose }: { init?: Partial<{ to: string; subject: string; body: string; in_reply_to: string }>; onClose: () => void }) {
  const { nav, refresh } = useLive();
  const toast = useToast();
  const [to, setTo] = useState(init?.to ?? "");
  const [subject, setSubject] = useState(init?.subject ?? "");
  const [body, setBody] = useState(init?.body ?? "");
  const [err, setErr] = useState<string | null>(null);
  const go = async (tool: "email_send" | "email_draft") => {
    setErr(null);
    const args: any = { to: to.split(/[,;\s]+/).filter(Boolean), subject, body };
    if (init?.in_reply_to) args.in_reply_to = init.in_reply_to;
    try {
      const t = await post<Task>("/api/tasks", {
        request: `${tool === "email_send" ? "Send" : "Draft"} email to ${args.to.join(", ")}: ${subject}`,
        title: `${tool === "email_send" ? "Send" : "Draft"}: ${subject || "(no subject)"}`,
        plan: { goal: tool === "email_send" ? "Send an email" : "Save a draft", steps: [{ id: "mail", title: tool === "email_send" ? "Send the email" : "Save the draft", tool, args }] },
      });
      toast({ title: tool === "email_send" ? "Waiting for your approval" : "Draft staged for review", text: subject });
      refresh();
      onClose();
      nav("tasks", t.pid);
    } catch (e: any) { setErr(e.message); }
  };
  return (
    <Dialog title={init?.in_reply_to ? "Reply" : "New email"} onClose={onClose}>
      <Field label="To" hint="Separate addresses with commas."><input className="input" value={to} onChange={(e) => setTo(e.target.value)} placeholder="name@example.com" /></Field>
      <Field label="Subject"><input className="input" value={subject} onChange={(e) => setSubject(e.target.value)} /></Field>
      <Field label="Message"><textarea className="textarea" rows={8} value={body} onChange={(e) => setBody(e.target.value)} /></Field>
      {err && <Banner tone="bad">{err}</Banner>}
      <p className="muted" style={{ margin: 0 }}>Sending always asks you once more before the email leaves this computer.</p>
      <div className="row">
        <button className="ee-btn primary" disabled={!to.trim()} onClick={() => go("email_send")}><Mail {...ICON_SM} /> Send</button>
        <button className="ee-btn outline" onClick={() => go("email_draft")}>Save draft</button>
      </div>
    </Dialog>
  );
}

export function InboxView() {
  const { status, sel, nav, run, bump } = useLive();
  const toast = useToast();
  const [folder, setFolder] = useState<"INBOX" | "Sent">("INBOX");
  const [unread, setUnread] = useState(false);
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [compose, setCompose] = useState<any>(null);
  const list = useAsync(() => get<{ messages: MailMsg[]; transport: string }>(
    `/api/mail/messages?folder=${folder}&limit=60${unread ? "&unread_only=true" : ""}${query ? `&q=${encodeURIComponent(query)}` : ""}`),
    [folder, unread, query, bump]);
  const msgs = list.data?.messages ?? [];
  const current = sel ?? msgs[0]?.id ?? null;
  const [msg, setMsg] = useState<MailMsg | null>(null);
  useEffect(() => {
    setMsg(null);
    if (!current) return;
    get<MailMsg>(`/api/mail/message?id=${encodeURIComponent(current)}`).then(setMsg).catch(() => setMsg(null));
  }, [current]);
  const mode = status?.mail?.mode;
  const ask = async (text: string) => {
    try { const t = await run(text); nav("tasks", t.pid); } catch (e: any) { toast({ title: "Could not start", text: e.message, bad: true }); }
  };

  return (
    <>
      <PageHeader section="Communication" dot="--ws-setup" title="Inbox"
                  sub={mode === "imap" ? `Connected to ${status.mail.address}` : mode === "off" ? "Email is turned off." : "A local demo mailbox. Connect a real account in Settings to send and receive."}
                  actions={<>
                    <button className="ee-btn outline" onClick={() => ask("Check my email")}><Sparkles {...ICON_SM} /> Summarise unread</button>
                    <button className="ee-btn outline" onClick={() => setCompose({})}><MailPlus {...ICON_SM} /> New email</button>
                  </>} />
      {mode === "local" && <Banner tone="info" icon={<Mail {...ICON_SM} />}>Mail sent from the demo mailbox is saved to its Sent folder and never delivered.</Banner>}
      <SplitPanes id="inbox" initial={420} left={
        <>
          <div className="stack" style={{ gap: 10, marginBottom: 12 }}>
            <Seg label="Folder" value={folder} onChange={(v) => { setFolder(v); nav("inbox"); }} items={[{ id: "INBOX", label: "Inbox" }, { id: "Sent", label: "Sent" }]} />
            <form className="row nowrap" onSubmit={(e) => { e.preventDefault(); setQuery(q.trim()); }}>
              <input className="input" placeholder="Search mail" aria-label="Search mail" value={q} onChange={(e) => setQ(e.target.value)} />
              <button className="ee-btn secondary round" aria-label="Search"><Search {...ICON_SM} /></button>
            </form>
            <div className="chips"><Filter on={!unread} onClick={() => setUnread(false)}>All</Filter><Filter on={unread} onClick={() => setUnread(true)}>Unread</Filter></div>
          </div>
          <div className="pane-scroll">
            {list.error ? <Banner tone="bad">{list.error}</Banner> : msgs.length === 0 ? (
              <Empty icon={<Inbox {...ICON} />}>{query ? "No messages match." : "No messages here."}</Empty>
            ) : (
              <ul className="plain-list">
                {msgs.map((m) => (
                  <li key={m.id}>
                    <button className={`ee-item ${m.seen ? "" : "unread"}`} style={{ background: "var(--soft)" }} aria-selected={current === m.id} onClick={() => nav("inbox", m.id)}>
                      <span className="av">{(m.from.replace(/<.*>/, "").trim()[0] ?? "?").toUpperCase()}</span>
                      <span className="m"><b>{m.subject}</b><small>{m.from.replace(/<.*>/, "").trim() || m.from} · {m.snippet}</small></span>
                      <span className="r">{m.attachments.length > 0 && <Paperclip {...ICON_SM} />}{ago(m.date)}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </>
      } right={
        !msg ? <Empty icon={<Mail {...ICON} />}>Choose a message.</Empty> : (
          <div className="pane-scroll stack" style={{ gap: 14 }}>
            <div className="stack" style={{ gap: 6, paddingRight: 40 }}>
              <div className="row">{!msg.seen && <Chip tone="info">Unread</Chip>}{msg.attachments.map((a) => <span key={a} className="tag"><Paperclip size={12} />&nbsp;{a}</span>)}</div>
              <h2 style={{ margin: 0, fontSize: 24, fontWeight: 500 }}>{msg.subject}</h2>
              <dl className="kv"><dt>From</dt><dd>{msg.from}</dd><dt>To</dt><dd>{msg.to}</dd>{msg.cc && <><dt>Cc</dt><dd>{msg.cc}</dd></>}<dt>Date</dt><dd>{msg.date ? new Date(msg.date).toLocaleString() : ""}</dd></dl>
            </div>
            <div className="row">
              <button className="ee-btn primary" onClick={() => setCompose({
                to: (/<([^>]+)>/.exec(msg.from)?.[1] ?? msg.from), subject: msg.subject.startsWith("Re:") ? msg.subject : `Re: ${msg.subject}`,
                body: `\n\nOn ${msg.date ? new Date(msg.date).toLocaleString() : ""}, ${msg.from} wrote:\n> ${(msg.body ?? "").split("\n").join("\n> ")}`, in_reply_to: msg.message_id,
              })}><Reply {...ICON_SM} /> Reply</button>
              <button className="ee-btn outline" onClick={() => ask(`Note: follow up on "${msg.subject}" from ${msg.from}`)}>Save as note</button>
            </div>
            <div className="guide" style={{ background: "var(--soft)" }}><pre className="mail-body">{msg.body}</pre></div>
          </div>
        )
      } />
      {compose && <ComposeMail init={compose} onClose={() => setCompose(null)} />}
    </>
  );
}
