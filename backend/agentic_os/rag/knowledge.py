"""Local knowledge base: SQLite documents + chunks + float32 vectors + FTS5, hybrid retrieval.

Everything lives in the app's SQLite database. Vectors are stored as float32
BLOBs; search is exact cosine similarity over the chunks embedded with the
*current* embedder (fine for a personal workspace: ~100k chunks search in
well under a second with NumPy). Keyword search uses SQLite FTS5 (BM25) when
available. The two rankings are fused with Reciprocal Rank Fusion.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import threading
from array import array
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..persistence.db import Database
from ..tools.fileio import decode_text, looks_binary, read_bytes_limited, sha256_text
from ..tools.pathguard import PathError, WorkspaceGuard
from ..util import utc_now_iso
from .chunker import chunk_text
from .embedders import Embedder

try:  # optional acceleration
    import numpy as _np
except Exception:  # pragma: no cover
    _np = None

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 1_000_000
RRF_K = 60
TEXT_EXTENSIONS_SKIP = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".exe", ".dll",
                        ".so", ".dylib", ".bin", ".woff", ".woff2", ".ttf", ".mp3", ".mp4", ".mov", ".lock"}

Progress = Callable[[dict], None]


def _pack(vec: List[float]) -> bytes:
    return array("f", vec).tobytes()


def _unpack(blob: bytes) -> List[float]:
    a = array("f")
    a.frombytes(blob)
    return a.tolist()


def _fts_query(text: str) -> Optional[str]:
    toks = [t for t in re.findall(r"[A-Za-z0-9_]+", text.lower()) if len(t) > 1][:24]
    if not toks:
        return None
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(toks))


class KnowledgeBase:
    def __init__(self, db: Database, embedder: Embedder):
        self.db = db
        self.embedder = embedder
        self._lock = threading.RLock()
        self._cache: Optional[Tuple[str, int, List[int], Any]] = None   # (embedder, version, ids, matrix)
        self._version = 0
        self.fts = self._ensure_fts()

    # ----------------------------------------------------------------- setup
    def _ensure_fts(self) -> bool:
        try:
            self.db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS rag_fts USING fts5("
                " text, chunk_id UNINDEXED, tokenize='porter unicode61')"
            )
            return True
        except Exception:  # FTS5 not compiled into this SQLite: fall back to LIKE scoring
            log.warning("SQLite FTS5 unavailable; keyword search falls back to substring matching")
            return False

    def set_embedder(self, embedder: Embedder) -> None:
        with self._lock:
            self.embedder = embedder
            self._cache = None

    # ---------------------------------------------------------------- status
    def status(self) -> dict:
        docs = self.db.query_one("SELECT COUNT(*) AS n, COALESCE(SUM(chunk_count), 0) AS c FROM rag_documents")
        stale = self.db.query_one("SELECT COUNT(*) AS n FROM rag_documents WHERE embedder != ?", (self.embedder.name,))
        by_source = {r["source"]: r["n"] for r in self.db.query(
            "SELECT source, COUNT(*) AS n FROM rag_documents GROUP BY source")}
        return {"documents": docs["n"], "chunks": docs["c"], "embedder": self.embedder.name,
                "stale_documents": stale["n"], "by_source": by_source, "keyword_index": "fts5" if self.fts else "like"}

    def documents(self, limit: int = 500) -> List[dict]:
        return [dict(r) for r in self.db.query(
            "SELECT id, source, path, title, size, chunk_count, embedder, indexed_at FROM rag_documents"
            " ORDER BY source, path LIMIT ?", (limit,))]

    # --------------------------------------------------------------- writing
    def _delete_doc_rows(self, conn, doc_id: str) -> None:
        if self.fts:
            conn.execute("DELETE FROM rag_fts WHERE chunk_id IN (SELECT id FROM rag_chunks WHERE doc_id = ?)", (doc_id,))
        conn.execute("DELETE FROM rag_chunks WHERE doc_id = ?", (doc_id,))

    def upsert_document(self, *, source: str, path: str, title: str, text: str, mtime: Optional[float] = None,
                        force: bool = False) -> Optional[dict]:
        """Index one document. Returns None when it is unchanged and already embedded with this embedder."""
        sha = sha256_text(text)
        existing = self.db.query_one("SELECT * FROM rag_documents WHERE source = ? AND path = ?", (source, path))
        if existing and not force and existing["sha256"] == sha and existing["embedder"] == self.embedder.name:
            return None
        chunks = chunk_text(text)
        vectors: List[List[float]] = []
        for i in range(0, len(chunks), self.embedder.batch_size):
            batch = chunks[i:i + self.embedder.batch_size]
            vectors.extend(self.embedder.embed([f"{title}\n{c.text}" for c in batch]))
        doc_id = existing["id"] if existing else "doc_" + secrets.token_hex(8)
        now = utc_now_iso()
        with self.db.transaction() as conn:
            if existing:
                self._delete_doc_rows(conn, doc_id)
                conn.execute("UPDATE rag_documents SET title=?, sha256=?, size=?, mtime=?, embedder=?, chunk_count=?,"
                             " indexed_at=? WHERE id=?",
                             (title, sha, len(text), mtime, self.embedder.name, len(chunks), now, doc_id))
            else:
                conn.execute("INSERT INTO rag_documents(id, source, path, title, sha256, size, mtime, embedder,"
                             " chunk_count, indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (doc_id, source, path, title, sha, len(text), mtime, self.embedder.name, len(chunks), now))
            for c, vec in zip(chunks, vectors):
                cur = conn.execute(
                    "INSERT INTO rag_chunks(doc_id, seq, text, start_line, end_line, embedder, dim, embedding)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (doc_id, c.seq, c.text, c.start_line, c.end_line, self.embedder.name, len(vec), _pack(vec)))
                if self.fts:
                    conn.execute("INSERT INTO rag_fts(text, chunk_id) VALUES (?, ?)", (f"{path}\n{c.text}", cur.lastrowid))
        with self._lock:
            self._version += 1
        return {"id": doc_id, "path": path, "chunks": len(chunks)}

    def delete_document(self, doc_id: str) -> bool:
        with self.db.transaction() as conn:
            self._delete_doc_rows(conn, doc_id)
            cur = conn.execute("DELETE FROM rag_documents WHERE id = ?", (doc_id,))
        with self._lock:
            self._version += 1
        return cur.rowcount > 0

    def clear(self) -> int:
        with self.db.transaction() as conn:
            if self.fts:
                conn.execute("DELETE FROM rag_fts")
            conn.execute("DELETE FROM rag_chunks")
            cur = conn.execute("DELETE FROM rag_documents")
        with self._lock:
            self._version += 1
        return cur.rowcount

    def add_note(self, title: str, text: str) -> dict:
        title = (title or "").strip()[:200] or "Untitled note"
        if not text.strip():
            raise ValueError("note text is empty")
        res = self.upsert_document(source="note", path=title, title=title, text=text, force=True)
        return res or {}

    def index_workspace(self, guard: WorkspaceGuard, path: str = ".", *, max_files: int = 5000,
                        progress: Optional[Progress] = None, cancel: Callable[[], bool] = lambda: False) -> dict:
        start = guard.resolve(path)
        if not start.is_dir:
            raise PathError("PATH_NOT_DIRECTORY", "Choose a folder to index.")
        files = list(guard.iter_files(start, max_depth=8, max_files=max_files))
        seen = set()
        stats = {"scanned": 0, "indexed": 0, "unchanged": 0, "skipped": 0, "removed": 0, "chunks": 0, "errors": []}
        for n, rel in enumerate(files, start=1):
            if cancel():
                stats["cancelled"] = True
                break
            stats["scanned"] += 1
            try:
                target = guard.resolve(rel)
                if target.sensitive or os.path.splitext(rel)[1].lower() in TEXT_EXTENSIONS_SKIP:
                    stats["skipped"] += 1
                    continue
                if os.path.getsize(target.abs_path) > MAX_FILE_BYTES:
                    stats["skipped"] += 1
                    continue
                data = read_bytes_limited(target.abs_path, MAX_FILE_BYTES)
                if looks_binary(data[:8192]):
                    stats["skipped"] += 1
                    continue
                text, _enc = decode_text(data)
                seen.add(rel)
                res = self.upsert_document(source="workspace", path=rel, title=rel, text=text,
                                           mtime=os.path.getmtime(target.abs_path))
                if res is None:
                    stats["unchanged"] += 1
                else:
                    stats["indexed"] += 1
                    stats["chunks"] += res["chunks"]
            except Exception as exc:  # noqa: BLE001 - one bad file never stops indexing
                stats["errors"].append({"path": rel, "error": str(exc)[:200]})
            if progress and (n % 10 == 0 or n == len(files)):
                progress({"done": n, "total": len(files), **{k: v for k, v in stats.items() if k != "errors"}})
        # Remove documents for files that were deleted, became binary/large/sensitive, or left the folder.
        # Skipped when the scan was cancelled or truncated, so a partial scan never deletes good entries.
        if not stats.get("cancelled") and len(files) < max_files:
            prefix = "" if start.rel == "." else start.rel + "/"
            for row in self.db.query("SELECT id, path FROM rag_documents WHERE source = 'workspace'"):
                if row["path"].startswith(prefix) and row["path"] not in seen:
                    self.delete_document(row["id"])
                    stats["removed"] += 1
        stats["errors"] = stats["errors"][:50]
        return stats

    # ---------------------------------------------------------------- search
    def _matrix(self) -> Tuple[List[int], Any]:
        with self._lock:
            name = self.embedder.name
            if self._cache and self._cache[0] == name and self._cache[1] == self._version:
                return self._cache[2], self._cache[3]
            rows = self.db.query("SELECT id, embedding FROM rag_chunks WHERE embedder = ?", (name,))
            ids = [r["id"] for r in rows]
            if _np is not None:
                mat = (_np.frombuffer(b"".join(r["embedding"] for r in rows), dtype=_np.float32)
                       .reshape(len(rows), -1) if rows else _np.zeros((0, 1), dtype=_np.float32))
            else:
                mat = [_unpack(r["embedding"]) for r in rows]
            self._cache = (name, self._version, ids, mat)
            return ids, mat

    def _vector_search(self, query: str, k: int) -> List[Tuple[int, float]]:
        ids, mat = self._matrix()
        if not ids:
            return []
        q = self.embedder.embed_query(query)
        if _np is not None:
            scores = mat @ _np.asarray(q, dtype=_np.float32)
            top = _np.argsort(-scores)[:k]
            return [(ids[i], float(scores[i])) for i in top]
        scored = [(ids[i], sum(a * b for a, b in zip(vec, q))) for i, vec in enumerate(mat)]
        scored.sort(key=lambda x: -x[1])
        return scored[:k]

    def _keyword_search(self, query: str, k: int) -> List[Tuple[int, float]]:
        if self.fts:
            q = _fts_query(query)
            if not q:
                return []
            try:
                rows = self.db.query("SELECT chunk_id, bm25(rag_fts) AS s FROM rag_fts WHERE rag_fts MATCH ?"
                                     " ORDER BY s LIMIT ?", (q, k))
            except Exception:  # malformed query: no keyword hits
                return []
            return [(int(r["chunk_id"]), -float(r["s"])) for r in rows]
        toks = [t for t in re.findall(r"[a-z0-9_]+", query.lower()) if len(t) > 2][:8]
        if not toks:
            return []
        where = " OR ".join("lower(text) LIKE ?" for _ in toks)
        rows = self.db.query(f"SELECT id, text FROM rag_chunks WHERE {where} LIMIT 500", [f"%{t}%" for t in toks])
        scored = [(r["id"], float(sum(r["text"].lower().count(t) for t in toks))) for r in rows]
        scored.sort(key=lambda x: -x[1])
        return scored[:k]

    def search(self, query: str, k: int = 8, *, source: Optional[str] = None,
               path_prefix: Optional[str] = None, allow: Optional[Callable[[dict], bool]] = None) -> List[dict]:
        query = (query or "").strip()
        if not query:
            return []
        pool = max(k * 4, 30)
        vec = self._vector_search(query, pool)
        kw = self._keyword_search(query, pool)
        fused: Dict[int, dict] = {}
        for rank, (cid, score) in enumerate(vec):
            fused.setdefault(cid, {"rrf": 0.0})
            fused[cid]["rrf"] += 1.0 / (RRF_K + rank + 1)
            fused[cid]["vector_score"] = round(score, 4)
            fused[cid]["vector_rank"] = rank + 1
        for rank, (cid, score) in enumerate(kw):
            fused.setdefault(cid, {"rrf": 0.0})
            fused[cid]["rrf"] += 1.0 / (RRF_K + rank + 1)
            fused[cid]["keyword_rank"] = rank + 1
        if not fused:
            return []
        ids = list(fused)
        rows = self.db.query(
            f"SELECT c.id, c.text, c.start_line, c.end_line, d.path, d.source, d.title, d.id AS doc_id"
            f" FROM rag_chunks c JOIN rag_documents d ON d.id = c.doc_id WHERE c.id IN ({', '.join('?' for _ in ids)})",
            ids)
        out = []
        for r in rows:
            hit = {"chunk_id": r["id"], "doc_id": r["doc_id"], "source": r["source"], "path": r["path"],
                   "title": r["title"], "start_line": r["start_line"], "end_line": r["end_line"], "text": r["text"],
                   "score": round(fused[r["id"]]["rrf"], 6), **{k2: v for k2, v in fused[r["id"]].items() if k2 != "rrf"}}
            if source and hit["source"] != source:
                continue
            if path_prefix and not hit["path"].startswith(path_prefix.strip("/")):
                continue
            if allow is not None and not allow(hit):
                continue
            out.append(hit)
        out.sort(key=lambda h: -h["score"])
        return out[:k]


ANSWER_SYSTEM = (
    "You answer questions using ONLY the numbered sources from the user's local knowledge base. Cite sources inline "
    "like [1] or [2][3]. If the sources do not contain the answer, say so plainly. Be concise."
)


def build_answer_prompt(question: str, hits: List[dict]) -> str:
    parts = []
    for i, h in enumerate(hits, start=1):
        loc = f"{h['path']}:{h['start_line']}-{h['end_line']}" if h["source"] == "workspace" else f"note: {h['path']}"
        parts.append(f"[{i}] {loc}\n{h['text'][:2500]}")
    return "Sources:\n\n" + "\n\n".join(parts) + f"\n\nQuestion: {question}"
