"""
test_phase2.py — Interactive smoke test for Phase 2 (Query Engine).

Run from the repo root:
    python test_phase2.py --db data/graph.db --repo mercuryx-api

What it tests:
    1. GraphLoader.get_stats()           — basic repo stats
    2. GraphLoader.load_repo()           — full graph load
    3. search_component()               — FTS search
    4. find_by_file()                   — all nodes in a file
    5. node_detail()                    — single node with edges
    6. list_dependencies()              — outgoing BFS
    7. list_dependents()                — incoming BFS
    8. find_path()                      — Dijkstra between two nodes
    9. analyze_impact()                 — reverse BFS impact
   10. get_critical_nodes()             — betweenness centrality
   11. find_cycles()                    — cycle detection
   12. get_communities()               — community detection
   13. get_entry_points()              — nodes with no incoming edges
"""

import argparse
import json
import sys
import time
from pathlib import Path

# Make sure imports resolve when running from repo root
sys.path.insert(0, str(Path(__file__).parent))

from graph.db import Database
from graph.loader import GraphLoader
from graph.queries import search_component, find_by_file, node_detail, list_dependencies, list_dependents
from graph.algorithms import (
    find_path, analyze_impact, find_cycles,
    get_critical_nodes, get_communities, get_entry_points,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def pp(obj, indent=2):
    """Pretty-print a dict/list, truncating long lists."""
    if isinstance(obj, list):
        preview = obj[:5]
        print(json.dumps(preview, indent=indent, default=str))
        if len(obj) > 5:
            print(f"  ... and {len(obj) - 5} more")
    else:
        print(json.dumps(obj, indent=indent, default=str))


def section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)


def ok(label: str, result):
    status = "✓" if result else "✗ EMPTY"
    print(f"  [{status}] {label}")
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Phase 2 smoke test")
    parser.add_argument("--db",   default="data/graph.db", help="Path to SQLite DB")
    parser.add_argument("--repo", default=None,            help="Repo ID (auto-detect if omitted)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print full results")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: Database not found at {db_path}")
        print("Run the indexer first:  python -m parser.indexer /path/to/repo --db data/graph.db")
        sys.exit(1)

    db = Database(db_path)
    loader = GraphLoader(db)

    # Auto-detect repo if not specified
    repos = db.list_repos()
    if not repos:
        print("ERROR: No repos indexed in this database.")
        sys.exit(1)

    if args.repo:
        repo_id = args.repo
    else:
        repo_id = repos[0]["id"]
        print(f"Auto-detected repo: {repo_id}")

    # -----------------------------------------------------------------------
    # 1. Stats (no graph load)
    # -----------------------------------------------------------------------
    section("1. Repo stats (loader.get_stats)")
    t0 = time.perf_counter()
    stats = loader.get_stats(repo_id)
    print(f"  Time: {time.perf_counter()-t0:.3f}s")
    pp(stats)

    # -----------------------------------------------------------------------
    # 2. Load full graph
    # -----------------------------------------------------------------------
    section("2. Load full graph into NetworkX")
    t0 = time.perf_counter()
    G = loader.load_repo(repo_id)
    elapsed = time.perf_counter() - t0
    print(f"  Nodes: {G.number_of_nodes():,}")
    print(f"  Edges: {G.number_of_edges():,}")
    print(f"  Time:  {elapsed:.3f}s")
    ok("graph loaded", G.number_of_nodes() > 0)

    # -----------------------------------------------------------------------
    # 3. FTS Search
    # -----------------------------------------------------------------------
    section("3. FTS search_component")

    # Pick a search term from actual node names
    sample_nodes = list(G.nodes(data=True))[:20]
    # Find a class-level node for a good search term
    search_term = None
    for nid, attrs in sample_nodes:
        name = attrs.get("name", "")
        if "::" in name:
            search_term = name.split("::")[0]  # class name
            break
    search_term = search_term or "Service"

    print(f"  Searching for: '{search_term}'")
    t0 = time.perf_counter()
    results = search_component(db, repo_id, search_term, limit=5)
    print(f"  Time: {time.perf_counter()-t0:.3f}s  |  Results: {len(results)}")
    ok("search returned results", len(results) > 0)
    if args.verbose:
        pp(results)
    else:
        for r in results[:3]:
            print(f"    • {r['name']} ({r['type']}) — {r['file_path']}")

    # -----------------------------------------------------------------------
    # 4. find_by_file
    # -----------------------------------------------------------------------
    section("4. find_by_file")

    # Get a file that has multiple nodes
    file_rows = db.execute(
        "SELECT file_path, COUNT(*) as c FROM nodes WHERE repo_id=? GROUP BY file_path ORDER BY c DESC LIMIT 1",
        (repo_id,),
    )
    target_file = file_rows[0]["file_path"] if file_rows else None

    if target_file:
        print(f"  File: {target_file}")
        t0 = time.perf_counter()
        result = find_by_file(db, repo_id, target_file)
        print(f"  Time: {time.perf_counter()-t0:.3f}s  |  Nodes: {result.get('total', 0)}")
        ok("find_by_file", result.get("total", 0) > 0)
        if args.verbose:
            pp(result)
        else:
            for type_, nodes in result.get("by_type", {}).items():
                print(f"    • {type_}: {len(nodes)} nodes")

    # -----------------------------------------------------------------------
    # 5. node_detail
    # -----------------------------------------------------------------------
    section("5. node_detail")

    # Pick a node with the most edges
    edge_rows = db.execute(
        """
        SELECT source_id, COUNT(*) as c FROM edges
        WHERE repo_id=? GROUP BY source_id ORDER BY c DESC LIMIT 1
        """,
        (repo_id,),
    )
    busy_node_id = edge_rows[0]["source_id"] if edge_rows else (list(G.nodes)[0] if G.nodes else None)

    if busy_node_id:
        print(f"  Node: {busy_node_id}")
        t0 = time.perf_counter()
        detail = node_detail(db, busy_node_id)
        print(f"  Time: {time.perf_counter()-t0:.3f}s")
        print(f"  Name: {detail.get('name')}  in_degree={detail.get('in_degree')}  out_degree={detail.get('out_degree')}")
        ok("node_detail", "name" in detail)
        if args.verbose:
            pp(detail)

    # -----------------------------------------------------------------------
    # 6. list_dependencies
    # -----------------------------------------------------------------------
    section("6. list_dependencies (depth=2)")

    if busy_node_id:
        t0 = time.perf_counter()
        deps = list_dependencies(G, busy_node_id, depth=2)
        print(f"  Time: {time.perf_counter()-t0:.3f}s  |  Found: {deps.get('total', 0)}")
        ok("list_dependencies", deps.get("total", 0) >= 0)
        if args.verbose:
            pp(deps)
        else:
            for d in deps.get("dependencies", [])[:5]:
                print(f"    → {d['node']['name']} via {d['relation']} (depth {d['depth']})")

    # -----------------------------------------------------------------------
    # 7. list_dependents
    # -----------------------------------------------------------------------
    section("7. list_dependents (depth=2)")

    if busy_node_id:
        t0 = time.perf_counter()
        dpts = list_dependents(G, busy_node_id, depth=2)
        print(f"  Time: {time.perf_counter()-t0:.3f}s  |  Found: {dpts.get('total', 0)}")
        ok("list_dependents", dpts.get("total", 0) >= 0)
        if args.verbose:
            pp(dpts)

    # -----------------------------------------------------------------------
    # 8. find_path
    # -----------------------------------------------------------------------
    section("8. find_path (Dijkstra)")

    # Pick two nodes from different files
    all_nodes = list(G.nodes)
    src, tgt = None, None
    if len(all_nodes) >= 2:
        src = all_nodes[0]
        # Try to find a node in a different file
        src_file = G.nodes[src].get("file_path", "")
        for n in all_nodes[1:]:
            if G.nodes[n].get("file_path", "") != src_file:
                tgt = n
                break
        tgt = tgt or all_nodes[-1]

    if src and tgt:
        print(f"  From: {G.nodes[src].get('name', src)}")
        print(f"  To:   {G.nodes[tgt].get('name', tgt)}")
        t0 = time.perf_counter()
        path = find_path(G, src, tgt)
        print(f"  Time: {time.perf_counter()-t0:.3f}s")
        print(f"  Found: {path['found']}")
        if path["found"]:
            print(f"  Path length: {path['length']} hops")
            print(f"  Path: {' → '.join(path['path_labels'])}")
        else:
            print(f"  Reason: {path.get('reason')}")

    # -----------------------------------------------------------------------
    # 9. analyze_impact
    # -----------------------------------------------------------------------
    section("9. analyze_impact (depth=3)")

    if busy_node_id:
        t0 = time.perf_counter()
        impact = analyze_impact(G, busy_node_id, depth=3)
        print(f"  Time: {time.perf_counter()-t0:.3f}s")
        print(f"  Affected nodes: {impact.get('total_affected', 0)}")
        print(f"  Affected files: {impact.get('affected_files', 0)}")
        ok("analyze_impact", impact.get("total_affected", 0) >= 0)
        if args.verbose:
            pp(impact)

    # -----------------------------------------------------------------------
    # 10. get_critical_nodes
    # -----------------------------------------------------------------------
    section("10. get_critical_nodes (betweenness centrality, top 10)")

    t0 = time.perf_counter()
    critical = get_critical_nodes(G, top_n=10)
    print(f"  Time: {time.perf_counter()-t0:.3f}s")
    ok("get_critical_nodes", len(critical.get("nodes", [])) > 0)
    for node in critical.get("nodes", [])[:10]:
        print(f"    • {node['name']:50s}  betweenness={node['betweenness']:.4f}  in={node['in_degree']}  out={node['out_degree']}")

    # -----------------------------------------------------------------------
    # 11. find_cycles
    # -----------------------------------------------------------------------
    section("11. find_cycles")

    t0 = time.perf_counter()
    cycles = find_cycles(G, max_cycles=20)
    print(f"  Time: {time.perf_counter()-t0:.3f}s")
    print(f"  Cycles found: {cycles['count']}")
    ok("find_cycles ran", True)
    if cycles["found"]:
        for c in cycles["cycles"][:3]:
            print(f"    • length={c['length']}  labels={c['labels']}")
    else:
        print("  No cycles found (good!)")

    # -----------------------------------------------------------------------
    # 12. get_communities
    # -----------------------------------------------------------------------
    section("12. get_communities (Louvain)")

    t0 = time.perf_counter()
    communities = get_communities(G, algorithm="louvain", min_size=3)
    print(f"  Time: {time.perf_counter()-t0:.3f}s")
    print(f"  Communities: {communities['count']}")
    print(f"  Algorithm: {communities.get('algorithm', 'unknown')}")
    ok("get_communities", communities["count"] > 0)
    if args.verbose:
        pp(communities)
    else:
        for c in communities.get("communities", [])[:5]:
            print(f"    • size={c['size']:4d}  label='{c['label']}'")

    # -----------------------------------------------------------------------
    # 13. get_entry_points
    # -----------------------------------------------------------------------
    section("13. get_entry_points (no incoming edges)")

    t0 = time.perf_counter()
    entries = get_entry_points(G, node_types=["function", "class"])
    print(f"  Time: {time.perf_counter()-t0:.3f}s  |  Found: {len(entries)}")
    ok("get_entry_points", len(entries) >= 0)
    for e in entries[:5]:
        print(f"    • {e['name']:50s}  out_degree={e['out_degree']}")

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    section("SUMMARY")
    print(f"  Repo:   {repo_id}")
    print(f"  Nodes:  {G.number_of_nodes():,}")
    print(f"  Edges:  {G.number_of_edges():,}")
    print(f"  DB:     {db_path}")
    print()
    print("  Phase 2 smoke test complete.")
    print("  Run with --verbose to see full JSON output for each step.")


if __name__ == "__main__":
    main()
