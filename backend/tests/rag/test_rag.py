import os

import pytest

from agentic_os.persistence.db import Database
from agentic_os.rag import KnowledgeBase, LocalHashEmbedder, build_embedder, chunk_text
from agentic_os.rag.embedders import EmbedderError
from agentic_os.tools.pathguard import WorkspaceGuard

DOCS = {
    "README.md": "# Billing service\n\nHandles invoices and payment retries for customers.\n",
    "docs/auth.md": "# Authentication\n\nUsers sign in with a pairing code. Sessions use HttpOnly cookies.\n"
                    "Tokens rotate every seven days.\n",
    "docs/deploy.md": "# Deployment\n\nRun the build script, then copy the installer to the release share.\n",
    "src/retry_policy.py": "def exponential_backoff(attempt):\n    return min(60, 2 ** attempt)\n",
    ".env": "STRIPE_KEY=sk_live_secret\n",
    "logo.png": "not really a png",
}


@pytest.fixture()
def ws(tmp_path):
    root = tmp_path / "ws"
    for rel, text in DOCS.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    db = Database(str(tmp_path / "kb.db"))
    db.migrate()
    return root, db


def test_local_embedder_is_deterministic_and_normalized():
    e = LocalHashEmbedder(256)
    a, b = e.embed(["payment retries", "payment retries"])
    assert a == b and abs(sum(x * x for x in a) - 1.0) < 1e-6
    q = e.embed_query("retry payments")
    near, far = e.embed(["invoices and payment retries", "copy the installer to the share"])
    dot = lambda u, v: sum(x * y for x, y in zip(u, v))
    assert dot(q, near) > dot(q, far)


def test_chunker_keeps_line_ranges():
    text = "\n".join(f"line {i} " + "word " * 30 for i in range(1, 120))
    chunks = chunk_text(text, target_chars=800)
    assert len(chunks) > 3
    assert chunks[0].start_line == 1
    assert all(c.start_line <= c.end_line for c in chunks)
    assert chunks[-1].end_line == 119


def test_index_search_incremental_and_cleanup(ws):
    root, db = ws
    kb = KnowledgeBase(db, LocalHashEmbedder())
    guard = WorkspaceGuard(str(root))
    stats = kb.index_workspace(guard)
    assert stats["indexed"] == 4                       # .env (sensitive) and logo.png skipped
    paths = {d["path"] for d in kb.documents()}
    assert ".env" not in paths and "logo.png" not in paths
    hits = kb.search("how do users sign in", k=3)
    assert hits[0]["path"] == "docs/auth.md"
    assert hits[0]["start_line"] >= 1
    hits = kb.search("exponential backoff", k=2)
    assert hits[0]["path"] == "src/retry_policy.py"
    # incremental: nothing changed
    assert kb.index_workspace(guard)["unchanged"] == 4
    # change one, delete one
    (root / "docs" / "deploy.md").write_text("# Deployment\n\nShip with the release pipeline.\n", encoding="utf-8")
    os.remove(root / "README.md")
    stats = kb.index_workspace(guard)
    assert stats["indexed"] == 1 and stats["removed"] == 1
    assert "README.md" not in {d["path"] for d in kb.documents()}
    assert kb.search("invoices", k=3) == [] or kb.search("invoices", k=3)[0]["path"] != "README.md"


def test_notes_and_filters(ws):
    root, db = ws
    kb = KnowledgeBase(db, LocalHashEmbedder())
    kb.index_workspace(WorkspaceGuard(str(root)))
    kb.add_note("Standup", "We agreed to migrate billing to the new payment provider next sprint.")
    assert kb.search("payment provider migration", k=1)[0]["source"] == "note"
    only_docs = kb.search("deployment", k=5, path_prefix="docs")
    assert only_docs and all(h["path"].startswith("docs") for h in only_docs)
    blocked = kb.search("sign in", k=5, allow=lambda h: h["path"] != "docs/auth.md")
    assert all(h["path"] != "docs/auth.md" for h in blocked)


def test_changing_embedder_marks_documents_stale(ws):
    root, db = ws
    kb = KnowledgeBase(db, LocalHashEmbedder(512))
    kb.index_workspace(WorkspaceGuard(str(root)))
    kb.set_embedder(LocalHashEmbedder(256))
    assert kb.status()["stale_documents"] == 4
    assert kb.search("sign in", k=3) == [] or all(h.get("vector_rank") is None for h in kb.search("sign in", k=3))
    kb.index_workspace(WorkspaceGuard(str(root)))
    assert kb.status()["stale_documents"] == 0


def test_build_embedder_validation():
    assert build_embedder(None).name == "local:hash-512-v1"
    assert build_embedder({"kind": "ollama"}).name == "ollama:nomic-embed-text"
    with pytest.raises(EmbedderError):
        build_embedder({"kind": "gemini"})                 # no key
    with pytest.raises(EmbedderError):
        build_embedder({"kind": "nope"})


def test_ollama_unreachable_is_a_clean_error():
    e = build_embedder({"kind": "ollama", "base_url": "http://127.0.0.1:9"})
    with pytest.raises(EmbedderError) as exc:
        e.embed(["x"])
    assert exc.value.code == "EMBED_UNREACHABLE"
