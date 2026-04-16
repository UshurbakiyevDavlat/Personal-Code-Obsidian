"""
graph/storage.py — Batch saving of nodes and edges to SQLite.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict

from graph.db import Database, CONFIDENCE_WEIGHTS


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Node:
    id: str            # "{repo_id}::{file_path}::{ClassName}::{method}"
    repo_id: str
    type: str          # function | class | file | module
    name: str          # "ClassName::methodName"
    file_path: str     # relative to repo root
    language: str      # php | go | typescript | python
    line_start: int | None = None
    line_end: int | None = None
    docstring: str | None = None
    metadata: dict = field(default_factory=dict)
    file_hash: str | None = None

    def to_db_tuple(self) -> tuple:
        return (
            self.id,
            self.repo_id,
            self.type,
            self.name,
            self.file_path,
            self.language,
            self.line_start,
            self.line_end,
            self.docstring,
            json.dumps(self.metadata),
            self.file_hash,
        )


@dataclass
class Edge:
    source_id: str
    target_id: str
    repo_id: str
    relation: str      # calls | imports | inherits | uses | contains
    confidence: str    # EXTRACTED | INFERRED | AMBIGUOUS
    source_line: int | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.source_id}::{self.relation}::{self.target_id}"

    @property
    def weight(self) -> float:
        return CONFIDENCE_WEIGHTS.get(self.confidence, 0.5)

    def to_db_tuple(self) -> tuple:
        return (
            self.id,
            self.repo_id,
            self.source_id,
            self.target_id,
            self.relation,
            self.confidence,
            self.weight,
            self.source_line,
            json.dumps(self.metadata),
        )


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

NODE_INSERT_SQL = """
    INSERT OR REPLACE INTO nodes
        (id, repo_id, type, name, file_path, language,
         line_start, line_end, docstring, metadata, file_hash)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

EDGE_INSERT_SQL = """
    INSERT OR REPLACE INTO edges
        (id, repo_id, source_id, target_id, relation, confidence, weight, source_line, metadata)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

BATCH_SIZE = 500


class GraphStorage:
    """Write nodes and edges to the database in batches."""

    def __init__(self, db: Database):
        self.db = db

    def save_nodes(self, nodes: list[Node]) -> int:
        """Batch insert nodes. Returns count saved."""
        if not nodes:
            return 0

        tuples = [n.to_db_tuple() for n in nodes]
        total = 0
        for i in range(0, len(tuples), BATCH_SIZE):
            batch = tuples[i : i + BATCH_SIZE]
            self.db.execute_many(NODE_INSERT_SQL, batch)
            total += len(batch)
        return total

    def save_edges(self, edges: list[Edge], known_node_ids: set[str]) -> int:
        """
        Batch insert edges. Skips edges where source or target is unknown
        (avoids FK violations when a call target is in vendor/excluded path).
        Returns count saved.
        """
        if not edges:
            return 0

        valid = [
            e for e in edges
            if e.source_id in known_node_ids and e.target_id in known_node_ids
        ]
        skipped = len(edges) - len(valid)
        if skipped:
            print(f"  [storage] skipped {skipped} edges with unknown nodes")

        tuples = [e.to_db_tuple() for e in valid]
        total = 0
        for i in range(0, len(tuples), BATCH_SIZE):
            batch = tuples[i : i + BATCH_SIZE]
            self.db.execute_many(EDGE_INSERT_SQL, batch)
            total += len(batch)
        return total

    def save_file_results(
        self,
        nodes: list[Node],
        edges: list[Edge],
        known_node_ids: set[str],
    ) -> tuple[int, int]:
        """Save nodes and edges for a single file. Returns (nodes_saved, edges_saved)."""
        # Add newly saved node IDs so edges within same file resolve
        new_ids = {n.id for n in nodes}
        known_node_ids.update(new_ids)

        n_saved = self.save_nodes(nodes)
        e_saved = self.save_edges(edges, known_node_ids)
        return n_saved, e_saved


def make_node_id(repo_id: str, file_path: str, class_name: str | None, name: str) -> str:
    """
    Build a canonical node ID.
    Examples:
        make_node_id("myrepo", "app/Services/OrderService.php", "OrderService", "__construct")
        → "myrepo::app/Services/OrderService.php::OrderService::__construct"

        make_node_id("myrepo", "app/Services/OrderService.php", None, "OrderService")
        → "myrepo::app/Services/OrderService.php::OrderService"
    """
    parts = [repo_id, file_path]
    if class_name:
        parts.append(class_name)
    parts.append(name)
    return "::".join(parts)


def make_node_display_name(class_name: str | None, method_name: str) -> str:
    """
    Build the human-readable name stored in nodes.name.
    Always "ClassName::methodName", never bare "methodName".
    """
    if class_name:
        return f"{class_name}::{method_name}"
    return method_name
