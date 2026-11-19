import { describe, expect, it } from "vitest";
import { diffStats, parseUnifiedDiff } from "./diff";
import { parseSse } from "../api";

const DIFF = `--- a/notes.md
+++ b/notes.md
@@ -1,2 +1,2 @@
 Line one
-TODO: write docs
+Docs written by the agent
`;

describe("parseUnifiedDiff", () => {
  it("numbers lines on both sides", () => {
    const lines = parseUnifiedDiff(DIFF);
    expect(lines.map((l) => l.kind)).toEqual(["meta", "meta", "hunk", "ctx", "del", "add"]);
    expect(lines[3]).toMatchObject({ oldNo: 1, newNo: 1 });
    expect(lines[4]).toMatchObject({ kind: "del", oldNo: 2 });
    expect(lines[5]).toMatchObject({ kind: "add", newNo: 2 });
  });
  it("counts changes", () => {
    expect(diffStats(DIFF)).toEqual({ added: 1, removed: 1 });
  });
});

describe("parseSse", () => {
  it("splits frames and keeps the partial tail", () => {
    const { events, rest } = parseSse('retry: 2000\n\ndata: {"type":"a"}\n\n: keep-alive\n\ndata: {"type":"b"}\n\ndata: {"ty');
    expect(events.map((e) => e.type)).toEqual(["a", "b"]);
    expect(rest).toBe('data: {"ty');
  });
});
