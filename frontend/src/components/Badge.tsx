import type { Task } from "../types";

export function stateLabel(t: Pick<Task, "state" | "pending_user" | "exit" | "phase">): { text: string; tone: string } {
  if (t.state === "EXITED") {
    const code = t.exit?.code ?? "done";
    const tone = code === "completed" ? "ok" : code === "discarded" || code === "cancelled" ? "neutral" : "bad";
    const text = { completed: "Done", discarded: "Discarded", cancelled: "Cancelled", failed: "Failed",
                   budget_exceeded: "Out of budget" }[code] ?? code;
    return { text, tone };
  }
  if (t.state === "BLOCKED_USER") {
    const k = t.pending_user?.kind;
    return { text: k === "review" ? "Review changes" : k === "approval" ? "Needs approval" : "Question", tone: "warn" };
  }
  if (t.state === "STOPPED") return { text: "Paused", tone: "neutral" };
  return { text: t.phase === "plan" || t.phase === "validate" ? "Planning" : "Working", tone: "busy" };
}

export function Badge({ task }: { task: Pick<Task, "state" | "pending_user" | "exit" | "phase"> }) {
  const { text, tone } = stateLabel(task);
  return <span className={`badge badge-${tone}`}>{text}</span>;
}
