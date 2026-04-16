"""
graph/loader.py — Load the dependency graph from SQLite into NetworkX.

Usage:
    from graph.db import Database
    from graph.loader import GraphLoader

    db = Database("data/graph.db")
    loader = GraphLoader(db)
    G = loader.load_repo("mercuryx-api")
    # G is a networkx.DiGraph with node/edge attributes
"""
from __future__ import annotations

import json
import networkx as nx

from graph.db import Database


class GraphLoader:
    """Load a repo's graph from SQLite into a NetworkX DiGraph."""

    def __init__(self, db: Database):
        self.db = db

    def load_repo(
        self,
        repo_id: str,
        node_types: list[str] | None = None,
        relations: list[str] | None = None,
    ) -> nx.DiGraph:
        """
        Build a directed graph for the given repo.

        Args:
            repo_id:    Repo identifier (e.g. "mercuryx-api")
            node_types: If set, only include nodes of these types
                        (function | class | file | module)
            relations:  If set, only include edges with these relations
                        (calls | imports | inherits | uses | contains)

        Returns:
            nx.DiGraph where:
              - node attributes: type, name, file_path, language,
                                 line_start, line_end, docstring, metadata
              - edge attributes: relation, confidence, weight, source_line
        """
        G = nx.DiGraph()
        G.graph["repo_id"] = repo_id

        # --- Load nodes ---
        node_sql = "SELECT * FROM nodes WHERE repo_id=?"
        params: list = [repo_id]

        if node_types:
            placeholders = ",".join("?" * len(node_types))
            node_sql += f" AND type IN ({placeholders})"
            params.extend(node_types)

        rows = self.db.execute(node_sql, tuple(params))
        node_ids: set[str] = set()

        for row in rows:
            nid = row["id"]
            node_ids.add(nid)
            G.add_node(
                nid,
                type=row["type"],
                name=row["name"],
                file_path=row["file_path"],
                language=row["language"],
                line_start=row["line_start"],
                line_end=row["line_end"],
                docstring=row["docstring"] or "",
                metadata=json.loads(row["metadata"] or "{}"),
            )

        # --- Load edges ---
        edge_sql = "SELECT * FROM edges WHERE repo_id=?"
        eparams: list = [repo_id]

        if relations:
            placeholders = ",".join("?" * len(relations))
            edge_sql += f" AND relation IN ({placeholders})"
            eparams.extend(relations)

        edge_rows = self.db.execute(edge_sql, tuple(eparams))
        skipped = 0

        for row in edge_rows:
            src, tgt = row["source_id"], row["target_id"]
            # Only add edges between nodes that are loaded (respects node_types filter)
            if src not in node_ids or tgt not in node_ids:
                skipped += 1
                continue
            G.add_edge(
                src,
                tgt,
                relation=row["relation"],
                confidence=row["confidence"],
                weight=row["weight"],
                source_line=row["source_line"],
            )

        G.graph["node_count"] = G.number_of_nodes()
        G.graph["edge_count"] = G.number_of_edges()
        G.graph["skipped_edges"] = skipped

        return G

    def load_file_subgraph(self, repo_id: str, file_path: str) -> nx.DiGraph:
        """
        Load all nodes from a specific file plus their immediate neighbours.
        Useful for "show me everything in OrderService.php".
        """
        # Get node IDs from this file
        rows = self.db.execute(
            "SELECT id FROM nodes WHERE repo_id=? AND file_path=?",
            (repo_id, file_path),
        )
        file_node_ids = {row["id"] for row in rows}
        if not file_node_ids:
            return nx.DiGraph()

        # Get all edges touching any of these nodes
        connected_ids: set[str] = set(file_node_ids)

        placeholders = ",".join("?" * len(file_node_ids))
        edge_rows = self.db.execute(
            f"""
            SELECT * FROM edges
            WHERE repo_id=?
              AND (source_id IN ({placeholders}) OR target_id IN ({placeholders}))
            """,
            (repo_id, *file_node_ids, *file_node_ids),
        )
        for row in edge_rows:
            connected_ids.add(row["source_id"])
            connected_ids.add(row["target_id"])

        # Load only those nodes
        full_graph = self.load_repo(repo_id)
        return full_graph.subgraph(connected_ids).copy()

    def get_stats(self, repo_id: str) -> dict:
        """Quick stats without loading the full graph into memory."""
        rows = self.db.execute("SELECT * FROM repos WHERE id=?", (repo_id,))
        if not rows:
            return {}
        repo = rows[0]

        lang_rows = self.db.execute(
            "SELECT language, COUNT(*) as cnt FROM nodes WHERE repo_id=? GROUP BY language ORDER BY cnt DESC",
            (repo_id,),
        )
        type_rows = self.db.execute(
            "SELECT type, COUNT(*) as cnt FROM nodes WHERE repo_id=? GROUP BY type ORDER BY cnt DESC",
            (repo_id,),
        )
        rel_rows = self.db.execute(
            "SELECT relation, COUNT(*) as cnt FROM edges WHERE repo_id=? GROUP BY relation ORDER BY cnt DESC",
            (repo_id,),
        )

        return {
            "repo_id": repo_id,
            "name": repo["name"],
            "status": repo["status"],
            "last_indexed": repo["last_indexed"],
            "node_count": repo["node_count"],
            "edge_count": repo["edge_count"],
            "languages": {row["language"]: row["cnt"] for row in lang_rows},
            "node_types": {row["type"]: row["cnt"] for row in type_rows},
            "relations": {row["relation"]: row["cnt"] for row in rel_rows},
        }
