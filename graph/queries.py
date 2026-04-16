"""
graph/queries.py — High-level query API over the graph.

All functions take a `db` (Database) and/or a pre-loaded `nx.DiGraph`.
They return plain dicts/lists so results can be serialised to JSON directly
by the MCP server.

Quick reference:
    search_component(db, repo_id, query, limit)
    list_dependencies(G, node_id, depth, relations)
    list_dependents(G, node_id, depth, relations)
    find_by_file(db, repo_id, file_path)
    node_detail(db, node_id)
    edges_between(G, source_id, target_id)
"""
from __future__ import annotations

import json
from typing import Any

import networkx as nx

from graph.db import Database


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_component(
    db: Database,
    repo_id: str,
    query: str,
    limit: int = 20,
    node_type: str | None = None,
) -> list[dict]:
    """
    Full-text search over node names, docstrings and file paths.

    Returns a ranked list of matching nodes as dicts.
    If `query` is a plain name (no FTS operators), wraps it in a prefix search.
    """
    # Escape special FTS5 characters so plain names don't crash the query
    safe_query = _fts_escape(query)

    rows = db.search_fts(safe_query, repo_id, limit=limit * 3)  # over-fetch for type filter

    results = []
    for row in rows:
        if node_type and row["type"] != node_type:
            continue
        results.append(_row_to_dict(row))
        if len(results) >= limit:
            break

    return results


def _fts_escape(query: str) -> str:
    """
    Try as a prefix search first (fast, handles most cases).
    Falls back to quoted phrase search for multi-word queries.
    """
    query = query.strip()
    if " " in query:
        # Multi-word: phrase match
        escaped = query.replace('"', '""')
        return f'"{escaped}"'
    # Single token: prefix match
    return f'"{query}"*'


# ---------------------------------------------------------------------------
# Dependencies / dependents
# ---------------------------------------------------------------------------

def list_dependencies(
    G: nx.DiGraph,
    node_id: str,
    depth: int = 1,
    relations: list[str] | None = None,
) -> dict:
    """
    Find everything that `node_id` depends on (outgoing edges).

    Args:
        G:          Loaded graph (from GraphLoader.load_repo)
        node_id:    Starting node
        depth:      How many hops to traverse (1 = direct, 2 = transitive, etc.)
        relations:  If set, only follow edges with these relation types

    Returns:
        {
            "node": {...},
            "dependencies": [
                {"node": {...}, "relation": "calls", "depth": 1, "confidence": "EXTRACTED"},
                ...
            ]
        }
    """
    if node_id not in G:
        return {"error": f"node not found: {node_id}"}

    visited: dict[str, int] = {}  # node_id -> depth first reached
    queue = [(node_id, 0)]
    deps = []

    while queue:
        current, d = queue.pop(0)
        if d >= depth:
            continue
        for _, tgt, edge_data in G.out_edges(current, data=True):
            if relations and edge_data.get("relation") not in relations:
                continue
            if tgt not in visited:
                visited[tgt] = d + 1
                deps.append({
                    "node": _node_attrs(G, tgt),
                    "relation": edge_data.get("relation"),
                    "confidence": edge_data.get("confidence"),
                    "weight": edge_data.get("weight"),
                    "depth": d + 1,
                })
                queue.append((tgt, d + 1))

    return {
        "node": _node_attrs(G, node_id),
        "dependencies": sorted(deps, key=lambda x: (x["depth"], x["node"]["name"])),
        "total": len(deps),
    }


def list_dependents(
    G: nx.DiGraph,
    node_id: str,
    depth: int = 1,
    relations: list[str] | None = None,
) -> dict:
    """
    Find everything that depends on `node_id` (incoming edges = reverse traversal).
    Same structure as list_dependencies.
    """
    if node_id not in G:
        return {"error": f"node not found: {node_id}"}

    visited: dict[str, int] = {}
    queue = [(node_id, 0)]
    deps = []

    while queue:
        current, d = queue.pop(0)
        if d >= depth:
            continue
        for src, _, edge_data in G.in_edges(current, data=True):
            if relations and edge_data.get("relation") not in relations:
                continue
            if src not in visited:
                visited[src] = d + 1
                deps.append({
                    "node": _node_attrs(G, src),
                    "relation": edge_data.get("relation"),
                    "confidence": edge_data.get("confidence"),
                    "weight": edge_data.get("weight"),
                    "depth": d + 1,
                })
                queue.append((src, d + 1))

    return {
        "node": _node_attrs(G, node_id),
        "dependents": sorted(deps, key=lambda x: (x["depth"], x["node"]["name"])),
        "total": len(deps),
    }


# ---------------------------------------------------------------------------
# File-level queries
# ---------------------------------------------------------------------------

def find_by_file(db: Database, repo_id: str, file_path: str) -> dict:
    """
    Return all nodes in a file, grouped by type.

    Useful for: "show me everything in app/Services/OrderService.php"
    """
    rows = db.execute(
        "SELECT * FROM nodes WHERE repo_id=? AND file_path=? ORDER BY line_start",
        (repo_id, file_path),
    )
    if not rows:
        return {"error": f"no nodes found for {file_path}"}

    grouped: dict[str, list] = {}
    for row in rows:
        t = row["type"]
        grouped.setdefault(t, []).append(_row_to_dict(row))

    return {
        "file_path": file_path,
        "repo_id": repo_id,
        "total": len(rows),
        "by_type": grouped,
    }


def list_files(db: Database, repo_id: str, language: str | None = None) -> list[dict]:
    """
    Return all unique file paths in the repo with node counts.
    Optionally filter by language.
    """
    if language:
        rows = db.execute(
            """
            SELECT file_path, language, COUNT(*) as node_count
            FROM nodes WHERE repo_id=? AND language=?
            GROUP BY file_path ORDER BY file_path
            """,
            (repo_id, language),
        )
    else:
        rows = db.execute(
            """
            SELECT file_path, language, COUNT(*) as node_count
            FROM nodes WHERE repo_id=?
            GROUP BY file_path ORDER BY file_path
            """,
            (repo_id,),
        )
    return [{"file_path": r["file_path"], "language": r["language"], "node_count": r["node_count"]} for r in rows]


# ---------------------------------------------------------------------------
# Node detail
# ---------------------------------------------------------------------------

def node_detail(db: Database, node_id: str) -> dict:
    """
    Return full node data plus its direct in/out edges from the DB.
    Does NOT require a loaded NetworkX graph — suitable for quick lookups.
    """
    rows = db.execute("SELECT * FROM nodes WHERE id=?", (node_id,))
    if not rows:
        return {"error": f"node not found: {node_id}"}

    node = _row_to_dict(rows[0])

    out_edges = db.execute(
        "SELECT target_id, relation, confidence, weight, source_line FROM edges WHERE source_id=?",
        (node_id,),
    )
    in_edges = db.execute(
        "SELECT source_id, relation, confidence, weight, source_line FROM edges WHERE target_id=?",
        (node_id,),
    )

    node["outgoing"] = [dict(r) for r in out_edges]
    node["incoming"] = [dict(r) for r in in_edges]
    node["out_degree"] = len(node["outgoing"])
    node["in_degree"] = len(node["incoming"])

    return node


def edges_between(G: nx.DiGraph, source_id: str, target_id: str) -> list[dict]:
    """Return all edges between two nodes (both directions)."""
    result = []
    if G.has_edge(source_id, target_id):
        data = G[source_id][target_id]
        result.append({"direction": "→", "source": source_id, "target": target_id, **data})
    if G.has_edge(target_id, source_id):
        data = G[target_id][source_id]
        result.append({"direction": "←", "source": target_id, "target": source_id, **data})
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _node_attrs(G: nx.DiGraph, node_id: str) -> dict:
    attrs = G.nodes.get(node_id, {})
    return {"id": node_id, **attrs}


def _row_to_dict(row: Any) -> dict:
    d = dict(row)
    if "metadata" in d and isinstance(d["metadata"], str):
        try:
            d["metadata"] = json.loads(d["metadata"])
        except (json.JSONDecodeError, TypeError):
            d["metadata"] = {}
    return d
