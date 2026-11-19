"""Thin, thread-safe wrapper around a single SQLite connection.

Write volume is tiny (a desktop app with a handful of concurrent tasks), so a
single connection guarded by a re-entrant lock is simpler and safer than a
pool. WAL mode keeps readers from blocking.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, List, Optional

from .migrations import MIGRATIONS


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        self._closed = False

    # ------------------------------------------------------------ migrations
    def migrate(self) -> List[int]:
        """Apply pending migrations in order. Returns the versions applied."""
        applied: List[int] = []
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version INTEGER PRIMARY KEY, description TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            done = {row[0] for row in self._conn.execute("SELECT version FROM schema_migrations")}
            for version, description, script in MIGRATIONS:
                if version in done:
                    continue
                safe_desc = description.replace("'", "''")
                full = (
                    "BEGIN;\n" + script.strip().rstrip(";") + ";\n"
                    f"INSERT INTO schema_migrations(version, description, applied_at) "
                    f"VALUES ({int(version)}, '{safe_desc}', strftime('%Y-%m-%dT%H:%M:%fZ','now'));\n"
                    "COMMIT;"
                )
                try:
                    self._conn.executescript(full)
                except Exception:
                    try:
                        self._conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
                applied.append(version)
        return applied

    def schema_version(self) -> int:
        row = self.query_one("SELECT MAX(version) AS v FROM schema_migrations")
        return int(row["v"] or 0) if row else 0

    # --------------------------------------------------------------- queries
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def healthcheck(self) -> bool:
        try:
            self.query_one("SELECT 1")
            return True
        except sqlite3.Error:
            return False

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True
