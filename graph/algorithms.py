"""
graph/algorithms.py — Graph algorithms for code analysis.

All functions accept a pre-loaded nx.DiGraph (from GraphLoader.load_repo)
and return plain dicts/lists for JSON serialisation.

Functions:
    find_path(G, source_id, target_id)      → shortest call/dependency path
    analyze_impact(G, node_id, depth)        → what breaks if this node changes
    find_cycles(G)                           → all dependency cycles
    get_critical_nodes(G, top_n)             → highest betweenness centrality
    get_communities(G, algorithm)            → Louvain/Leiden community detection
    get_entry_points(G)                      → nodes with no incoming edges
    get_dead_ends(G)                         → nodes with no outgoing edges
    subgraph_around(G, node_id, radius)      → ego graph for visualisation
"""
from __future__ import annotations

import networkx as nx


# ---------------------------------------------------------------------------
# Shortest path
# ---------------------------------------------------------------------------

def find_path(
    G: nx.DiGraph,
    source_id: str,
    target_id: str,
    weight: str = "weight",
) -> dict:
    """
    Find the shortest dependency path between two nodes.

    Uses Dijkstra on edge weights (higher confidence = lower cost so we
    invert: cost = 1 / weight).

    Returns:
        {
            "found": True,
            "path": ["node_id_1", "node_id_2", ...],
            "path_labels": ["ClassName::method", ...],
            "edges": [{"relation": "calls", "confidence": "EXTRACTED"}, ...],
            "length": 3,
        }
        or {"found": False, "reason": "..."}
    """
    if source_id not in G:
        return {"found": False, "reason": f"source not in graph: {source_id}"}
    if target_id not in G:
        return {"found": False, "reason": f"target not in graph: {target_id}"}

    # Build inverted-weight graph for Dijkstra.
    # Start from a copy so isolated nodes (no edges) are still present.
    inverted = nx.DiGraph()
    inverted.add_nodes_from(G.nodes)  # include isolated nodes
    for u, v, data in G.edges(data=True):
        w = data.get("weight", 1.0)
        inverted.add_edge(u, v, inv_weight=1.0 / max(w, 0.01), **data)

    try:
        path = nx.shortest_path(inverted, source_id, target_id, weight="inv_weight")
    except nx.NetworkXNoPath:
        return {"found": False, "reason": "no path found"}
    except nx.NodeNotFound as e:
        return {"found": False, "reason": str(e)}

    # Collect edge metadata along the path
    edges_info = []
    for i in range(len(path) - 1):
        u, v = path[i], path[i + 1]
        data = G[u][v]
        edges_info.append({
            "from": u,
            "to": v,
            "relation": data.get("relation"),
            "confidence": data.get("confidence"),
            "weight": data.get("weight"),
        })

    return {
        "found": True,
        "path": path,
        "path_labels": [G.nodes[n].get("name", n) for n in path],
        "edges": edges_info,
        "length": len(path) - 1,
    }


# ---------------------------------------------------------------------------
# Impact analysis
# ---------------------------------------------------------------------------

def analyze_impact(
    G: nx.DiGraph,
    node_id: str,
    depth: int = 3,
    relations: list[str] | None = None,
) -> dict:
    """
    "What breaks if I change node_id?"

    Traverses all INCOMING edges (reverse direction) up to `depth` hops.
    Returns every node that directly or transitively depends on this node.

    Returns:
        {
            "node": {"id": ..., "name": ...},
            "affected": [
                {"id": ..., "name": ..., "file_path": ..., "depth": 1},
                ...
            ],
            "total_affected": 42,
            "by_file": {"app/Services/Order.php": [...], ...}
        }
    """
    if node_id not in G:
        return {"error": f"node not found: {node_id}"}

    visited: dict[str, int] = {}
    queue = [(node_id, 0)]

    while queue:
        current, d = queue.pop(0)
        if d >= depth:
            continue
        for src, _, edge_data in G.in_edges(current, data=True):
            if relations and edge_data.get("relation") not in relations:
                continue
            if src not in visited:
                visited[src] = d + 1
                queue.append((src, d + 1))

    affected = []
    by_file: dict[str, list] = {}
    for nid, d in sorted(visited.items(), key=lambda x: (x[1], x[0])):
        attrs = G.nodes.get(nid, {})
        entry = {
            "id": nid,
            "name": attrs.get("name", nid),
            "file_path": attrs.get("file_path", ""),
            "type": attrs.get("type", ""),
            "depth": d,
        }
        affected.append(entry)
        by_file.setdefault(entry["file_path"], []).append(entry)

    node_attrs = G.nodes.get(node_id, {})
    return {
        "node": {"id": node_id, "name": node_attrs.get("name", node_id)},
        "affected": affected,
        "total_affected": len(affected),
        "by_file": by_file,
        "affected_files": len(by_file),
    }


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------

def find_cycles(G: nx.DiGraph, max_cycles: int = 50) -> dict:
    """
    Find dependency cycles in the graph.

    Returns:
        {
            "found": True,
            "count": 3,
            "cycles": [
                {
                    "length": 2,
                    "nodes": ["id1", "id2"],
                    "labels": ["ClassName::method", ...],
                    "files": ["app/A.php", "app/B.php"],
                }
            ]
        }
    """
    raw_cycles = list(nx.simple_cycles(G))
    raw_cycles = raw_cycles[:max_cycles]  # cap to avoid explosion on large graphs

    cycles = []
    for cycle in raw_cycles:
        labels = [G.nodes[n].get("name", n) for n in cycle]
        files = list({G.nodes[n].get("file_path", "") for n in cycle})
        cycles.append({
            "length": len(cycle),
            "nodes": cycle,
            "labels": labels,
            "files": files,
        })

    # Sort: shortest cycles first (more actionable)
    cycles.sort(key=lambda c: c["length"])

    return {
        "found": len(cycles) > 0,
        "count": len(cycles),
        "cycles": cycles,
    }


# ---------------------------------------------------------------------------
# Critical nodes (betweenness centrality)
# ---------------------------------------------------------------------------

def get_critical_nodes(G: nx.DiGraph, top_n: int = 20) -> dict:
    """
    Identify the most critical nodes by betweenness centrality.

    High betweenness = many shortest paths pass through this node.
    These are your architectural choke-points — changing them has high impact.

    Returns:
        {
            "nodes": [
                {"id": ..., "name": ..., "file_path": ..., "type": ...,
                 "betweenness": 0.42, "in_degree": 12, "out_degree": 5},
                ...
            ]
        }
    """
    if G.number_of_nodes() == 0:
        return {"nodes": []}

    # Use approximate betweenness for large graphs (k=sample size)
    n = G.number_of_nodes()
    k = min(n, 500)  # sample up to 500 nodes for approximation
    centrality = nx.betweenness_centrality(G, k=k, normalized=True, weight="weight")

    ranked = sorted(centrality.items(), key=lambda x: -x[1])[:top_n]

    nodes = []
    for nid, score in ranked:
        attrs = G.nodes.get(nid, {})
        nodes.append({
            "id": nid,
            "name": attrs.get("name", nid),
            "file_path": attrs.get("file_path", ""),
            "type": attrs.get("type", ""),
            "language": attrs.get("language", ""),
            "betweenness": round(score, 6),
            "in_degree": G.in_degree(nid),
            "out_degree": G.out_degree(nid),
        })

    return {"nodes": nodes}


# ---------------------------------------------------------------------------
# Community detection
# ---------------------------------------------------------------------------

def get_communities(
    G: nx.DiGraph,
    algorithm: str = "louvain",
    min_size: int = 2,
) -> dict:
    """
    Detect communities (clusters of tightly coupled components).

    Each community = a likely module or bounded context.

    Args:
        algorithm:  "louvain" (default) or "greedy" (faster, less accurate)
        min_size:   Skip communities smaller than this

    Returns:
        {
            "count": 5,
            "communities": [
                {
                    "id": 0,
                    "size": 42,
                    "label": "OrderService / PaymentService / ...",
                    "files": ["app/Services/..."],
                    "nodes": [...]
                }
            ]
        }
    """
    if G.number_of_nodes() == 0:
        return {"count": 0, "communities": []}

    # NetworkX community algorithms work on undirected graphs
    UG = G.to_undirected()

    try:
        if algorithm == "louvain":
            # Requires networkx >= 3.3 or python-louvain
            try:
                from networkx.algorithms.community import louvain_communities
                raw = louvain_communities(UG, seed=42)
            except ImportError:
                # Fall back to greedy modularity
                from networkx.algorithms.community import greedy_modularity_communities
                raw = list(greedy_modularity_communities(UG))
        else:
            from networkx.algorithms.community import greedy_modularity_communities
            raw = list(greedy_modularity_communities(UG))
    except Exception as e:
        return {"count": 0, "communities": [], "error": str(e)}

    communities = []
    for idx, community in enumerate(sorted(raw, key=len, reverse=True)):
        nodes = list(community)
        if len(nodes) < min_size:
            continue

        # Collect file paths in this community
        files = sorted({G.nodes[n].get("file_path", "") for n in nodes if G.nodes.get(n)})

        # Build a human-readable label from the top 3 node names
        top_names = sorted(
            [G.nodes[n].get("name", n) for n in nodes if G.nodes.get(n)],
            key=len,
        )[:3]
        label = " / ".join(top_names)

        communities.append({
            "id": idx,
            "size": len(nodes),
            "label": label,
            "files": files[:20],  # cap file list
            "nodes": nodes[:50],  # cap node list
        })

    return {
        "count": len(communities),
        "algorithm": algorithm,
        "communities": communities,
    }


# ---------------------------------------------------------------------------
# Entry points & dead ends
# ---------------------------------------------------------------------------

def get_entry_points(G: nx.DiGraph, node_types: list[str] | None = None) -> list[dict]:
    """
    Nodes with no REAL incoming edges = entry points / public API surface.
    (Things nothing else calls — likely controllers, commands, cron jobs.)

    We exclude 'contains' edges (structural parent→child relationships) from
    the incoming count: every function has a 'contains' edge from its class,
    every class has a 'contains' edge from its file — those are not callers.
    """
    result = []
    for nid in G.nodes:
        # Count only non-structural incoming edges
        non_structural_in = sum(
            1 for _, _, d in G.in_edges(nid, data=True)
            if d.get("relation") != "contains"
        )
        if non_structural_in > 0:
            continue
        attrs = G.nodes[nid]
        if node_types and attrs.get("type") not in node_types:
            continue
        result.append({
            "id": nid,
            "name": attrs.get("name", nid),
            "file_path": attrs.get("file_path", ""),
            "type": attrs.get("type", ""),
            "out_degree": G.out_degree(nid),
        })
    return sorted(result, key=lambda x: -x["out_degree"])


def get_god_objects(
    G: nx.DiGraph,
    min_in: int = 8,
    min_out: int = 8,
    top_n: int = 10,
) -> list[dict]:
    """
    Detect God Objects / God Classes — nodes with BOTH very high in-degree
    AND very high out-degree (excluding 'contains' structural edges).

    These components violate the Single Responsibility Principle: they are
    called from many places AND depend on many others. Refactoring them has
    the highest architectural impact.

    Args:
        min_in:  Minimum non-structural in-degree to qualify (default: 8)
        min_out: Minimum out-degree to qualify (default: 8)
        top_n:   How many to return (default: 10)

    Returns:
        [
            {
                "id": "...",
                "name": "OrderService",
                "file_path": "app/Services/OrderService.php",
                "type": "class",
                "in_degree": 24,
                "out_degree": 18,
                "coupling_score": 42,
                "warning": "God Object: called from 24 places, depends on 18"
            },
            ...
        ]
    """
    candidates = []
    for nid in G.nodes:
        # Exclude 'contains' from in-degree (same logic as get_entry_points)
        real_in = sum(
            1 for _, _, d in G.in_edges(nid, data=True)
            if d.get("relation") != "contains"
        )
        real_out = G.out_degree(nid)
        if real_in >= min_in and real_out >= min_out:
            attrs = G.nodes[nid]
            coupling = real_in + real_out
            candidates.append({
                "id": nid,
                "name": attrs.get("name", nid),
                "file_path": attrs.get("file_path", ""),
                "type": attrs.get("type", ""),
                "language": attrs.get("language", ""),
                "in_degree": real_in,
                "out_degree": real_out,
                "coupling_score": coupling,
                "warning": f"God Object: called from {real_in} places, depends on {real_out}",
            })
    return sorted(candidates, key=lambda x: -x["coupling_score"])[:top_n]


def get_dead_ends(G: nx.DiGraph, node_types: list[str] | None = None) -> list[dict]:
    """
    Nodes with no outgoing edges = leaf nodes.
    (Things that call nothing — likely pure utilities, models, DTOs.)
    """
    result = []
    for nid in G.nodes:
        if G.out_degree(nid) == 0:
            attrs = G.nodes[nid]
            if node_types and attrs.get("type") not in node_types:
                continue
            result.append({
                "id": nid,
                "name": attrs.get("name", nid),
                "file_path": attrs.get("file_path", ""),
                "type": attrs.get("type", ""),
                "in_degree": G.in_degree(nid),
            })
    return sorted(result, key=lambda x: -x["in_degree"])


# ---------------------------------------------------------------------------
# Ego subgraph (for visualisation)
# ---------------------------------------------------------------------------

def subgraph_around(G: nx.DiGraph, node_id: str, radius: int = 2) -> nx.DiGraph:
    """
    Return a subgraph centred on node_id, containing all nodes
    within `radius` hops in either direction.

    This is what the vis.js frontend sends when a user clicks a node.
    """
    if node_id not in G:
        return nx.DiGraph()

    # Ego graph in undirected sense (both directions)
    UG = G.to_undirected()
    ego = nx.ego_graph(UG, node_id, radius=radius)
    return G.subgraph(ego.nodes).copy()
