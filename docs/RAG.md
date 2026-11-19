# Knowledge base (RAG)

Local retrieval over your workspace files and notes. Everything is stored in the app's SQLite database; nothing
leaves the machine unless you choose a remote embedder.

## Storage

| Table | Contents |
|---|---|
| `rag_documents` | one row per indexed file or note: source, path, SHA-256, embedder name, chunk count |
| `rag_chunks` | passages with their line range and a float32 embedding stored as a BLOB |
| `rag_fts` | SQLite FTS5 index (porter stemming) over the same passages, created at runtime |

If this SQLite build lacks FTS5, keyword search falls back to substring scoring.

## Indexing

- Walks the workspace with the same `WorkspaceGuard` the tools use, so ignored folders (`node_modules`, `.git`,
  `dist`, and so on) are skipped and **sensitive files are never indexed**.
- Skips binaries, images, archives and files over 1 MB.
- **Incremental:** unchanged files (same hash, same embedder) are not re-embedded. Deleted or now-excluded files
  are removed, except when a scan was cancelled or truncated.
- Chunks follow lines (about 1,200 characters, breaking at blank lines and headings, 2-line overlap), so
  results cite `path:start-end`.
- Runs in the background with progress events. It runs automatically on startup and after every commit or undo
  (Settings → "Keep the knowledge base in sync").

## Embedders

| Type | Name stored | Notes |
|---|---|---|
| `local` (default) | `local:hash-512-v1` | Feature hashing of words, sub-words, character trigrams and bigrams. Offline, deterministic, no download. Strong on exact and partial terms, weaker on paraphrase. |
| `ollama` | `ollama:<model>` | Any Ollama embedding model, e.g. `nomic-embed-text` (`ollama pull nomic-embed-text`). Local and semantic. |
| `openai` | `openai:<model>` | `/v1/embeddings` on OpenAI or any compatible endpoint; reuses the key of a configured provider. |
| `gemini` | `gemini:<model>` | `batchEmbedContents`; reuses the key of a configured Gemini provider. |

Vectors from different embedders are never compared. After switching, the Knowledge page shows how many
documents are stale; re-index to embed them with the new model.

## Retrieval

`search(query, k)` runs two rankings and fuses them with **Reciprocal Rank Fusion** (k = 60):

1. **Vector:** exact cosine similarity against every passage embedded with the current embedder (a NumPy
   matrix kept in memory and invalidated on change; pure Python if NumPy is missing).
2. **Keyword:** FTS5 BM25.

Results report their vector and keyword ranks, so you can see why a passage matched.

**Ask** retrieves the top passages and, if an AI provider is configured, asks it to answer *only* from those
numbered sources with inline citations. Without a provider it returns the passages.

## The agent uses it too

The `search_knowledge` tool (read-only) lets plans retrieve passages. Every hit from a workspace file is
re-checked through the calling process's overlay resolver, so a process scoped to `docs/` only sees passages
from `docs/`, and sensitive files never appear.

## API

`GET /api/kb/status`, `GET /api/kb/documents`, `POST /api/kb/index {path}`, `POST /api/kb/index/cancel`,
`POST /api/kb/search {query, k, source?, path_prefix?}`, `POST /api/kb/ask {question, k}`,
`POST /api/kb/notes {title, text}`, `DELETE /api/kb/documents/{id}`, `DELETE /api/kb`.
