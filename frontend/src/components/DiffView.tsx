import { useState } from "react";
import type { FileDiff } from "../types";
import { diffStats, parseUnifiedDiff } from "./diff";

export function DiffView({ file, defaultOpen = true }: { file: FileDiff; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const stats = file.diff ? diffStats(file.diff) : null;
  return (
    <div className="diff-file">
      <button className="diff-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className={`op op-${file.op}`}>{file.op === "create" ? "new" : "edit"}</span>
        <span className="mono">{file.path}</span>
        {stats && (
          <span className="diff-stats">
            <span className="add">+{stats.added}</span> <span className="del">−{stats.removed}</span>
          </span>
        )}
        <span className="chev">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="diff-body">
          {file.diff ? (
            <table className="diff-table">
              <tbody>
                {parseUnifiedDiff(file.diff)
                  .filter((l) => l.kind !== "meta")
                  .map((l, i) => (
                    <tr key={i} className={`dl dl-${l.kind}`}>
                      <td className="ln">{l.oldNo ?? ""}</td>
                      <td className="ln">{l.newNo ?? ""}</td>
                      <td className="sign">{l.kind === "add" ? "+" : l.kind === "del" ? "−" : ""}</td>
                      <td className="code">{l.text}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          ) : (
            <p className="muted pad">{file.note ?? "No preview available."}</p>
          )}
          {file.truncated && <p className="muted pad">Diff truncated.</p>}
        </div>
      )}
    </div>
  );
}
