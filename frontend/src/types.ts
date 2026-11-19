export type ProcState =
  | "READY" | "RUNNING" | "BLOCKED_IO" | "BLOCKED_USER" | "BLOCKED_WAIT" | "STOPPED" | "ZOMBIE" | "EXITED";

export interface PlanStep {
  id: string;
  title?: string;
  tool: string;
  args: Record<string, unknown>;
  depends_on?: string[];
}

export interface StepState {
  status: "pending" | "running" | "done" | "failed" | "deferred";
  attempts?: number;
  summary?: unknown;
  checks?: { name: string; passed: boolean; detail?: string }[];
  last_error?: { code: string; message: string; retryable?: boolean };
  via?: string;
}

export interface FileDiff {
  path: string;
  op: "create" | "modify";
  size?: number;
  diff?: string | null;
  truncated?: boolean;
  note?: string;
}

export type PendingUser =
  | {
      kind: "approval";
      step_id: string;
      tool: string;
      args: Record<string, unknown>;
      action_hash: string;
      risk: string;
      reasons: string[];
      effect: string;
      grant: { kind: string; right: string; scope: string } | null;
      irreversible: boolean;
      preview?: any;
    }
  | { kind: "clarify"; question: string }
  | {
      kind: "review";
      files: FileDiff[];
      outbox: string[];
      outcome: "succeeded" | "failed" | "conflict" | "commit_incomplete";
      failure?: string;
      conflicts?: { path: string; reason: string }[];
      error?: string;
    };

export interface Task {
  pid: string;
  ppid: string | null;
  task_id: string;
  title: string;
  request: string;
  state: ProcState;
  phase: string;
  priority: number;
  slices: number;
  plan: { version: number; goal?: string; steps: PlanStep[] } | null;
  plan_version: number;
  steps_state: Record<string, StepState>;
  pending_user: PendingUser | null;
  outbox: string[];
  children: string[] | Task[];
  usage: { executions: number; llm_calls: number; tokens: number };
  exit: { code: string; reason: string; summary?: unknown } | null;
  txn_id: string | null;
  replans: number;
  auto_commit?: boolean;
  origin?: { kind: "user" | "schedule" | "listener"; id?: string; name?: string; trigger?: string; manual?: boolean };
  created_at?: string;
  status?: string;
}

export interface KernelEvent {
  seq?: number;
  task_id?: string;
  type: string;
  message?: string;
  level?: string;
  node?: string;
  step_id?: string;
  data?: Record<string, any>;
  ts?: string;
  created_at?: string;
}

export interface KbHit {
  chunk_id: number;
  doc_id: string;
  source: "workspace" | "note";
  path: string;
  title: string;
  start_line: number;
  end_line: number;
  text: string;
  score: number;
  vector_rank?: number;
  keyword_rank?: number;
}

export interface Provider {
  id: string;
  kind: string;
  kind_label: string;
  display_name: string;
  model: string;
  base_url?: string | null;
  is_default: boolean;
  enabled: boolean;
  credential: { stored: boolean; required: boolean; store_available: boolean; error?: string | null };
  last_test?: { ok?: boolean; message?: string } | null;
}

export interface Schedule {
  id: string; name: string; request: string; kind: "cron" | "interval" | "once"; spec: string; label: string;
  enabled: boolean; auto_commit: boolean; next_run: number | null; last_run: number | null; last_pid: string | null;
  run_count: number; created_at: string;
}

export interface Listener {
  id: string; name: string; kind: "file" | "email" | "webhook"; request: string; config: Record<string, any>;
  enabled: boolean; auto_commit: boolean; cooldown_s: number; last_fired: number | null; fire_count: number;
  last_error: string | null; has_secret: boolean; secret?: string; created_at: string;
}

export interface MailMsg {
  id: string; uid: string; folder: string; subject: string; from: string; to: string; cc: string; date: string | null;
  message_id: string; seen: boolean; snippet: string; attachments: string[]; body?: string;
}

export interface CalEvent {
  uid: string; title: string; start: string; end?: string; location?: string; description?: string; url?: string;
  attendees: string[]; organizer?: string;
}

export interface NoteInfo { path: string; title: string; snippet: string; size: number; modified: number; content?: string; sha256?: string }
