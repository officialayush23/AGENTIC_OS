import { useState } from "react";
import { ChevronDown, ChevronRight, FilePlus2, FilePen } from "lucide-react";
import type { FileDiff } from "../types";
import { ICON_SM } from "../ui";
import { diffStats, parseUnifiedDiff } from "./diff";

export function DiffView({ file, defaultOpen = true }: { file: FileDiff; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const stats = file.diff ? diffStats(file.diff) : null;
  const home = file.path.startsWith("~/");
  return (
    <div className="diff">
      <button onClick={() => setOpen(!open)} aria-expanded={open}>
        {open ? <ChevronDown {...ICON_SM} /> : <ChevronRight {...ICON_SM} />}
        {file.op === "create" ? <FilePlus2 {...ICON_SM} /> : <FilePen {...ICON_SM} />}
        <span className="mono grow ellipsis">{file.path}</span>
        {home && <span className="tag">Home</span>}
        <span className="tag">{file.op === "create" ? "New file" : "Edited"}</span>
        {stats && <span className="small num ink2">+{stats.added} −{stats.removed}</span>}
      </button>
      {open && (
        <div className="wrap">
          {file.diff ? (
            <table>
              <tbody>
                {parseUnifiedDiff(file.diff).filter((l) => l.kind !== "meta").map((l, i) => (
                  <tr key={i} className={l.kind}>
                    <td className="ln">{l.oldNo ?? ""}</td>
                    <td className="ln">{l.newNo ?? ""}</td>
                    <td className="t">{l.kind === "add" ? "+ " : l.kind === "del" ? "− " : "  "}{l.text}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <p className="muted" style={{ padding: "8px 14px", margin: 0 }}>{file.note ?? "No preview available."}</p>}
          {file.truncated && <p className="muted" style={{ padding: "8px 14px", margin: 0 }}>Diff shortened.</p>}
        </div>
      )}
    </div>
  );
}
