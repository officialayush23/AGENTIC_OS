export type DiffLine = { kind: "add" | "del" | "ctx" | "hunk" | "meta"; text: string; oldNo?: number; newNo?: number };

/** Turn a unified diff into numbered lines for rendering. */
export function parseUnifiedDiff(diff: string): DiffLine[] {
  const out: DiffLine[] = [];
  let oldNo = 0;
  let newNo = 0;
  for (const line of diff.split("\n")) {
    if (line.startsWith("---") || line.startsWith("+++")) {
      out.push({ kind: "meta", text: line });
    } else if (line.startsWith("@@")) {
      const m = /@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(line);
      if (m) {
        oldNo = parseInt(m[1], 10);
        newNo = parseInt(m[2], 10);
      }
      out.push({ kind: "hunk", text: line });
    } else if (line.startsWith("+")) {
      out.push({ kind: "add", text: line.slice(1), newNo: newNo++ });
    } else if (line.startsWith("-")) {
      out.push({ kind: "del", text: line.slice(1), oldNo: oldNo++ });
    } else if (line.length || out.length) {
      out.push({ kind: "ctx", text: line.startsWith(" ") ? line.slice(1) : line, oldNo: oldNo++, newNo: newNo++ });
    }
  }
  while (out.length && out[out.length - 1].kind === "ctx" && out[out.length - 1].text === "") out.pop();
  return out;
}

export function diffStats(diff: string): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const l of diff.split("\n")) {
    if (l.startsWith("+") && !l.startsWith("+++")) added++;
    else if (l.startsWith("-") && !l.startsWith("---")) removed++;
  }
  return { added, removed };
}
