// Backend connection. Two modes:
//  - Desktop (Tauri): the Rust shell starts the Python backend with a random bearer token and hands
//    us {url, token} through the `backend_info` command.
//  - Browser: same origin as the backend (or the Vite dev proxy); a paired session cookie authenticates.

export interface Connection {
  base: string;
  token: string | null;
  desktop: boolean;
}

let conn: Connection = { base: "", token: null, desktop: false };

export function isDesktop(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export async function connect(): Promise<Connection> {
  if (isDesktop()) {
    const { invoke } = await import("@tauri-apps/api/core");
    // The shell resolves this once the backend has printed its READY line.
    const info = await invoke<{ url: string; token: string }>("backend_info");
    conn = { base: info.url.replace(/\/$/, ""), token: info.token, desktop: true };
  } else {
    conn = { base: "", token: null, desktop: false };
  }
  return conn;
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public details?: unknown) {
    super(message);
  }
}

function headers(json: boolean): Record<string, string> {
  const h: Record<string, string> = { "X-Agentic-OS": "1" };
  if (json) h["Content-Type"] = "application/json";
  if (conn.token) h["Authorization"] = `Bearer ${conn.token}`;
  return h;
}

export async function api<T = any>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(conn.base + path, {
    method,
    headers: headers(body !== undefined),
    body: body !== undefined ? JSON.stringify(body) : undefined,
    credentials: conn.token ? "omit" : "include",
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) {
    const e = data?.error ?? {};
    throw new ApiError(res.status, e.code ?? "HTTP_" + res.status, e.message ?? res.statusText, e.details);
  }
  return data as T;
}

export const get = <T = any>(p: string) => api<T>("GET", p);
export const post = <T = any>(p: string, b: unknown = {}) => api<T>("POST", p, b);
export const put = <T = any>(p: string, b: unknown) => api<T>("PUT", p, b);
export const del = <T = any>(p: string) => api<T>("DELETE", p);

/** Parse a text/event-stream body. Exported for tests. */
export function parseSse(buffer: string): { events: any[]; rest: string } {
  const events: any[] = [];
  const blocks = buffer.split(/\r?\n\r?\n/);
  const rest = blocks.pop() ?? "";
  for (const block of blocks) {
    const data = block
      .split(/\r?\n/)
      .filter((l) => l.startsWith("data:"))
      .map((l) => l.slice(5).replace(/^ /, ""))
      .join("\n");
    if (!data) continue;
    try {
      events.push(JSON.parse(data));
    } catch {
      /* ignore malformed frame */
    }
  }
  return { events, rest };
}

/**
 * Server-sent events via fetch (EventSource cannot send our auth headers).
 * Reconnects with backoff until `signal` aborts.
 */
export function subscribe(path: string, onEvent: (ev: any) => void, signal: AbortSignal): void {
  let delay = 500;
  const run = async () => {
    while (!signal.aborted) {
      try {
        const res = await fetch(conn.base + path, {
          headers: headers(false),
          credentials: conn.token ? "omit" : "include",
          signal,
        });
        if (!res.ok || !res.body) throw new Error("stream " + res.status);
        delay = 500;
        const reader = res.body.getReader();
        const dec = new TextDecoder();
        let buf = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          const { events, rest } = parseSse(buf);
          buf = rest;
          events.forEach(onEvent);
        }
      } catch {
        if (signal.aborted) return;
      }
      await new Promise((r) => setTimeout(r, delay));
      delay = Math.min(delay * 2, 8000);
    }
  };
  void run();
}

/** Fetch an authenticated binary (e.g. a screenshot) and return an object URL for <img>. */
export async function blobUrl(path: string): Promise<string> {
  const res = await fetch(conn.base + path, { headers: headers(false), credentials: conn.token ? "omit" : "include" });
  if (!res.ok) throw new ApiError(res.status, "HTTP_" + res.status, res.statusText);
  return URL.createObjectURL(await res.blob());
}

/** URL of a backend resource for links the browser opens itself (downloads). Desktop mode needs blobUrl instead. */
export function rawUrl(path: string): string {
  return conn.base + path;
}
