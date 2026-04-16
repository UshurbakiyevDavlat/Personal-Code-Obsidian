"""
graph/db.py — SQLite + FTS5 initialization and connection management.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from contextlib import contextmanager


SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS repos (
    id           TEXT PRIMARY KEY,
    path         TEXT NOT NULL,
    name         TEXT NOT NULL,
    languages    TEXT DEFAULT '[]',   -- JSON array: ["php", "go"]
    last_indexed TEXT,                -- ISO 8601
    node_count   INTEGER DEFAULT 0,
    edge_count   INTEGER DEFAULT 0,
    status       TEXT DEFAULT 'pending'  -- pending | indexing | ready | error
);

CREATE TABLE IF NOT EXISTS nodes (
    id           TEXT PRIMARY KEY,   -- "{repo_id}::{file_path}::{ClassName}::{method}"
    repo_id      TEXT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    type         TEXT NOT NULL,      -- function | class | file | module
    name         TEXT NOT NULL,      -- "ClassName::methodName"
    file_path    TEXT NOT NULL,      -- relative to repo root
    language     TEXT NOT NULL,      -- php | go | typescript | python
    line_start   INTEGER,
    line_end     INTEGER,
    docstring    TEXT,
    metadata     TEXT DEFAULT '{}',  -- JSON
    file_hash    TEXT,               -- MD5 of source file for incremental updates
    created_at   TEXT DEFAULT (datetime('now')),
    updated_at   TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_nodes_repo     ON nodes(repo_id);
CREATE INDEX IF NOT EXISTS idx_nodes_file     ON nodes(repo_id, file_path);
CREATE INDEX IF NOT EXISTS idx_nodes_type     ON nodes(repo_id, type);
CREATE INDEX IF NOT EXISTS idx_nodes_name     ON nodes(repo_id, name);
CREATE INDEX IF NOT EXISTS idx_nodes_hash     ON nodes(file_path, file_hash);

CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    name,
    docstring,
    file_path,
    content=nodes,
    content_rowid=rowid,
    tokenize='unicode61'
);

-- Keep FTS in sync with nodes table
CREATE TRIGGER IF NOT EXISTS nodes_fts_insert AFTER INSERT ON nodes BEGIN
    INSERT INTO nodes_fts(rowid, name, docstring, file_path)
    VALUES (new.rowid, new.name, new.docstring, new.file_path);
END;

CREATE TRIGGER IF NOT EXISTS nodes_fts_delete AFTER DELETE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, name, docstring, file_path)
    VALUES ('delete', old.rowid, old.name, old.docstring, old.file_path);
END;

CREATE TRIGGER IF NOT EXISTS nodes_fts_update AFTER UPDATE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, name, docstring, file_path)
    VALUES ('delete', old.rowid, old.name, old.docstring, old.file_path);
    INSERT INTO nodes_fts(rowid, name, docstring, file_path)
    VALUES (new.rowid, new.name, new.docstring, new.file_path);
END;

CREATE TABLE IF NOT EXISTS edges (
    id           TEXT PRIMARY KEY,   -- "{source_id}::{relation}::{target_id}"
    repo_id      TEXT NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    source_id    TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    target_id    TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    relation     TEXT NOT NULL,      -- calls | imports | inherits | uses | contains
    confidence   TEXT NOT NULL,      -- EXTRACTED | INFERRED | AMBIGUOUS
    weight       REAL DEFAULT 1.0,   -- EXTRACTED=1.0, INFERRED=0.7, AMBIGUOUS=0.4
    source_line  INTEGER,
    metadata     TEXT DEFAULT '{}',  -- JSON, e.g. {"is_cycle": true}
    created_at   TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
CREATE INDEX IF NOT EXISTS idx_edges_repo   ON edges(repo_id);
CREATE INDEX IF NOT EXISTS idx_edges_rel    ON edges(repo_id, relation);
"""

CONFIDENCE_WEIGHTS = {
    "EXTRACTED": 1.0,
    "INFERRED": 0.7,
    "AMBIGUOUS": 0.4,
}


class Database:
    """SQLite connection wrapper with WAL mode and FTS5 support."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA_SQL)

    @contextmanager
    def connect(self):
        """Yield a connection with row_factory set to Row for dict-like access."""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def execute(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.connect() as conn:
            cursor = conn.execute(sql, params)
            return cursor.fetchall()

    def execute_many(self, sql: str, params_list: list[tuple]) -> int:
        """Batch insert/update. Returns row count."""
        with self.connect() as conn:
            cursor = conn.executemany(sql, params_list)
            return cursor.rowcount

    def search_fts(self, query: str, repo_id: str, limit: int = 20) -> list[sqlite3.Row]:
        """Full-text search over nodes using FTS5."""
        sql = """
            SELECT n.*
            FROM nodes n
            JOIN nodes_fts f ON n.rowid = f.rowid
            WHERE f.nodes_fts MATCH ?
              AND n.repo_id = ?
            ORDER BY rank
            LIMIT ?
        """
        return self.execute(sql, (query, repo_id, limit))

    # --- Repo helpers ---

    def upsert_repo(self, repo: dict) -> None:
        sql = """
            INSERT INTO repos (id, path, name, languages, status)
            VALUES (:id, :path, :name, :languages, :status)
            ON CONFLICT(id) DO UPDATE SET
                path=excluded.path,
                name=excluded.name,
                languages=excluded.languages,
                status=excluded.status
        """
        self.execute(sql, repo)

    def set_repo_status(self, repo_id: str, status: str) -> None:
        self.execute(
            "UPDATE repos SET status=? WHERE id=?",
            (status, repo_id),
        )

    def update_repo_counts(self, repo_id: str) -> None:
        self.execute(
            """
            UPDATE repos SET
                node_count = (SELECT COUNT(*) FROM nodes WHERE repo_id=?),
                edge_count = (SELECT COUNT(*) FROM edges WHERE repo_id=?),
                last_indexed = datetime('now')
            WHERE id=?
            """,
            (repo_id, repo_id, repo_id),
        )

    def get_repo(self, repo_id: str) -> sqlite3.Row | None:
        rows = self.execute("SELECT * FROM repos WHERE id=?", (repo_id,))
        return rows[0] if rows else None

    def list_repos(self) -> list[sqlite3.Row]:
        return self.execute("SELECT * FROM repos ORDER BY last_indexed DESC")

    # --- Node helpers ---

    def get_file_hashes(self, repo_id: str) -> dict[str, str]:
        """Returns {file_path: file_hash} for incremental updates."""
        rows = self.execute(
            "SELECT DISTINCT file_path, file_hash FROM nodes WHERE repo_id=?",
            (repo_id,),
        )
        return {row["file_path"]: row["file_hash"] for row in rows}

    def delete_nodes_by_file(self, repo_id: str, file_path: str) -> None:
        """Remove all nodes (and cascading edges) for a file before re-indexing."""
        self.execute(
            "DELETE FROM nodes WHERE repo_id=? AND file_path=?",
            (repo_id, file_path),
        )
