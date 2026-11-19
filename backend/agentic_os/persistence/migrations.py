"""Versioned schema migrations.

Each entry is (version, description, SQL script). Migrations are append-only:
never edit a released migration, add a new one instead. `Database.migrate()`
applies pending versions inside a transaction and records them in
`schema_migrations`.
"""

MIGRATIONS = [
    (
        1,
        "initial schema",
        """
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS providers (
    id                  TEXT PRIMARY KEY,
    kind                TEXT NOT NULL,
    display_name        TEXT NOT NULL,
    model               TEXT NOT NULL,
    base_url            TEXT,
    timeout_s           REAL NOT NULL DEFAULT 60,
    max_output_tokens   INTEGER NOT NULL DEFAULT 4096,
    temperature         REAL,
    structured_mode     TEXT NOT NULL DEFAULT 'auto',
    max_calls_per_task  INTEGER NOT NULL DEFAULT 8,
    extra_json          TEXT NOT NULL DEFAULT '{}',
    is_default          INTEGER NOT NULL DEFAULT 0,
    enabled             INTEGER NOT NULL DEFAULT 1,
    last_test_json      TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id                  TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    request             TEXT NOT NULL,
    status              TEXT NOT NULL,
    planner_mode        TEXT NOT NULL,
    provider_id         TEXT,
    provider_kind       TEXT,
    model               TEXT,
    options_json        TEXT NOT NULL DEFAULT '{}',
    client_request_id   TEXT UNIQUE,
    attempt             INTEGER NOT NULL DEFAULT 1,
    thread_id           TEXT NOT NULL,
    plan_version        INTEGER NOT NULL DEFAULT 0,
    current_node        TEXT,
    current_step_id     TEXT,
    cancel_requested    INTEGER NOT NULL DEFAULT 0,
    error_code          TEXT,
    error_message       TEXT,
    summary_json        TEXT,
    metrics_json        TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    started_at          TEXT,
    finished_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);

CREATE TABLE IF NOT EXISTS task_steps (
    task_id                   TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    plan_version              INTEGER NOT NULL,
    step_id                   TEXT NOT NULL,
    position                  INTEGER NOT NULL,
    title                     TEXT NOT NULL,
    tool                      TEXT NOT NULL,
    args_json                 TEXT NOT NULL,
    depends_on_json           TEXT NOT NULL DEFAULT '[]',
    expected_outcome          TEXT,
    risk                      TEXT,
    requires_approval         INTEGER NOT NULL DEFAULT 0,
    verification_json         TEXT,
    status                    TEXT NOT NULL,
    attempts                  INTEGER NOT NULL DEFAULT 0,
    policy_json               TEXT,
    result_json               TEXT,
    verification_result_json  TEXT,
    error_code                TEXT,
    error_message             TEXT,
    started_at                TEXT,
    finished_at               TEXT,
    updated_at                TEXT NOT NULL,
    PRIMARY KEY (task_id, plan_version, step_id)
);

CREATE TABLE IF NOT EXISTS task_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    ts              TEXT NOT NULL,
    type            TEXT NOT NULL,
    level           TEXT NOT NULL DEFAULT 'info',
    node            TEXT,
    step_id         TEXT,
    message         TEXT NOT NULL,
    data_json       TEXT NOT NULL DEFAULT '{}',
    correlation_id  TEXT,
    UNIQUE (task_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_task_seq ON task_events(task_id, seq);

CREATE TABLE IF NOT EXISTS approvals (
    id                  TEXT PRIMARY KEY,
    task_id             TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    kind                TEXT NOT NULL,
    step_id             TEXT,
    plan_version        INTEGER NOT NULL,
    tool                TEXT,
    action_hash         TEXT NOT NULL,
    args_preview_json   TEXT NOT NULL DEFAULT '{}',
    risk                TEXT,
    reasons_json        TEXT NOT NULL DEFAULT '[]',
    effect              TEXT,
    preview_json        TEXT,
    status              TEXT NOT NULL,
    decision_note       TEXT,
    created_at          TEXT NOT NULL,
    decided_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_task ON approvals(task_id);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);

CREATE TABLE IF NOT EXISTS tool_executions (
    idempotency_key  TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step_id          TEXT NOT NULL,
    tool             TEXT NOT NULL,
    tool_version     TEXT NOT NULL,
    status           TEXT NOT NULL,
    args_hash        TEXT NOT NULL,
    result_json      TEXT,
    attempt          INTEGER NOT NULL DEFAULT 1,
    started_at       TEXT NOT NULL,
    finished_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_exec_task ON tool_executions(task_id);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash    TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    last_seen_at  TEXT,
    user_agent    TEXT
);

CREATE TABLE IF NOT EXISTS allowed_apps (
    id                 TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    path               TEXT NOT NULL,
    args_policy        TEXT NOT NULL DEFAULT 'none',
    requires_approval  INTEGER NOT NULL DEFAULT 0,
    enabled            INTEGER NOT NULL DEFAULT 1,
    builtin            INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_items (
    id                TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,
    key               TEXT,
    content           TEXT NOT NULL,
    source_task_id    TEXT,
    approved_by_user  INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory_items(kind, created_at);
""",
    ),
    (
        2,
        "kernel: processes, capabilities, overlay, journal",
        """
CREATE TABLE IF NOT EXISTS kernel_processes (
    pid         TEXT PRIMARY KEY,
    ppid        TEXT,
    task_id     TEXT NOT NULL,
    state       TEXT NOT NULL,
    phase       TEXT NOT NULL,
    pcb_json    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kproc_state ON kernel_processes(state);
CREATE INDEX IF NOT EXISTS idx_kproc_task ON kernel_processes(task_id);

CREATE TABLE IF NOT EXISTS kernel_capabilities (
    cap_id      TEXT PRIMARY KEY,
    pid         TEXT NOT NULL,
    kind        TEXT NOT NULL,
    scope       TEXT NOT NULL,
    rights      TEXT NOT NULL,
    expires_at  REAL,
    uses_left   INTEGER,
    parent_cap  TEXT,
    origin      TEXT NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kcap_pid ON kernel_capabilities(pid);

CREATE TABLE IF NOT EXISTS kernel_overlay_files (
    overlay_id   TEXT NOT NULL,
    rel_path     TEXT NOT NULL,
    base_exists  INTEGER NOT NULL,
    base_sha256  TEXT,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (overlay_id, rel_path)
);

CREATE TABLE IF NOT EXISTS kernel_txns (
    txn_id        TEXT PRIMARY KEY,
    pid           TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    overlay_id    TEXT NOT NULL,
    state         TEXT NOT NULL,
    summary_json  TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ktxn_state ON kernel_txns(state);

CREATE TABLE IF NOT EXISTS kernel_journal (
    txn_id             TEXT NOT NULL REFERENCES kernel_txns(txn_id) ON DELETE CASCADE,
    seq                INTEGER NOT NULL,
    rel_path           TEXT NOT NULL,
    op                 TEXT NOT NULL,
    before_sha256      TEXT,
    after_sha256       TEXT NOT NULL,
    before_blob        TEXT,
    after_blob         TEXT NOT NULL,
    created_dirs_json  TEXT NOT NULL DEFAULT '[]',
    state              TEXT NOT NULL,
    PRIMARY KEY (txn_id, seq)
);
""",
    ),
    (
        3,
        "kernel step outputs; RAG knowledge base (documents, chunks, embeddings, FTS)",
        """
CREATE TABLE IF NOT EXISTS kernel_step_outputs (
    pid          TEXT NOT NULL,
    step_id      TEXT NOT NULL,
    output_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (pid, step_id)
);

CREATE TABLE IF NOT EXISTS rag_documents (
    id           TEXT PRIMARY KEY,
    source       TEXT NOT NULL,              -- workspace | note
    path         TEXT NOT NULL,              -- workspace-relative path, or note title
    title        TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    size         INTEGER NOT NULL,
    mtime        REAL,
    embedder     TEXT NOT NULL,
    chunk_count  INTEGER NOT NULL DEFAULT 0,
    indexed_at   TEXT NOT NULL,
    UNIQUE (source, path)
);

CREATE TABLE IF NOT EXISTS rag_chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      TEXT NOT NULL REFERENCES rag_documents(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    text        TEXT NOT NULL,
    start_line  INTEGER NOT NULL,
    end_line    INTEGER NOT NULL,
    embedder    TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    embedding   BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_doc ON rag_chunks(doc_id, seq);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_embedder ON rag_chunks(embedder);
""",
    ),
]
