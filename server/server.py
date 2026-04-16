"""
server/server.py — FastMCP server for personal-code-obsidian.

7 tools:
    graph_list_repos      — list all indexed repos
    graph_build           — index or re-index a repo
    graph_query           — FTS search across a repo
    graph_dependencies    — outgoing/incoming edges for a node
    graph_impact          — what breaks if this node changes
    graph_path            — shortest path between two nodes
    graph_overview        — critical nodes, communities, stats

Transport:
    stdio  (default)   — for local Claude Code / claude mcp add
    sse                — for Cowork / remote access (set MCP_TRANSPORT=sse)

Run locally:
    python run_server.py

Run as HTTP server for Cowork:
    MCP_TRANSPORT=sse MCP_PORT=8000 python run_server.py
"""
from __future__ import annotations

import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import networkx as nx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

# Ensure project root is on path (works when launched via run_server.py)
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

from graph.db import Database
from graph.loader import GraphLoader
from graph.queries import (
    find_by_file,
    list_dependencies,
    list_dependents,
    node_detail,
    search_component,
)
from graph.algorithms import (
    analyze_impact,
    find_cycles,
    find_path,
    get_communities,
    get_critical_nodes,
    get_entry_points,
)
from parser.indexer import index_repo

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_DB = str(_ROOT / "data" / "graph.db")
DB_PATH = os.environ.get("DB_PATH", DEFAULT_DB)
MAX_SEARCH_RESULTS = 50
MAX_DEPTH = 5

# ---------------------------------------------------------------------------
# Lifespan — shared state across all tools
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(server: FastMCP):
    """
    Initialize Database and graph cache once at startup.
    State is available in all tools via ctx.request_context.lifespan_state.
    """
    db = Database(DB_PATH)
    loader = GraphLoader(db)
    graph_cache: dict[str, nx.DiGraph] = {}

    yield {
        "db": db,
        "loader": loader,
        "graphs": graph_cache,
    }


mcp = FastMCP("code_obsidian_mcp", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _get_graph(repo_id: str, state: dict) -> nx.DiGraph:
    """Load graph from cache or from DB. Raises ValueError if repo not found."""
    graphs: dict = state["graphs"]
    if repo_id not in graphs:
        db: Database = state["db"]
        repo = db.get_repo(repo_id)
        if not repo:
            raise ValueError(f"Repo '{repo_id}' not found. Run graph_build first or check graph_list_repos.")
        loader: GraphLoader = state["loader"]
        graphs[repo_id] = loader.load_repo(repo_id)
    return graphs[repo_id]


def _invalidate_cache(repo_id: str, state: dict) -> None:
    """Drop cached graph so next access reloads from DB."""
    state["graphs"].pop(repo_id, None)


def _resolve_node_id(
    name_or_id: str,
    repo_id: str,
    db: Database,
    G: nx.DiGraph,
) -> str | None:
    """
    Accept either a full node ID or a partial name and return the best matching node ID.
    Full ID: contains '::' and exists in G → return as-is.
    Name: search FTS and return the first match.
    """
    if name_or_id in G:
        return name_or_id

    # Try FTS search
    results = search_component(db, repo_id, name_or_id, limit=5)
    if results:
        return results[0]["id"]

    return None


def _err(msg: str) -> str:
    return json.dumps({"error": msg})


def _ok(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


# ---------------------------------------------------------------------------
# Pydantic input models
# ---------------------------------------------------------------------------

class BuildInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    repo_path: str = Field(..., description="Absolute path to the repository root on disk (e.g. /Users/aruzhan/projects/my-api)")
    force: bool = Field(default=False, description="Force full re-index even if files are unchanged")

class QueryInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    repo_id: str = Field(..., description="Repo identifier returned by graph_list_repos (e.g. 'mercuryx-api')")
    query: str = Field(..., description="Search term — class name, method name, file path or any keyword (e.g. 'OrderService', 'fromArray', 'payment')", min_length=1)
    node_type: Optional[str] = Field(default=None, description="Filter by node type: 'function', 'class', 'file', or 'module'")
    limit: int = Field(default=20, description="Max results to return", ge=1, le=MAX_SEARCH_RESULTS)

class NodeInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    repo_id: str = Field(..., description="Repo identifier (e.g. 'mercuryx-api')")
    node: str = Field(..., description="Node name or full node ID. Examples: 'OrderService::create', 'PermissionHelper::can', or a full ID from a previous result")

class DependenciesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    repo_id: str = Field(..., description="Repo identifier (e.g. 'mercuryx-api')")
    node: str = Field(..., description="Node name or full node ID (e.g. 'OrderService::create')")
    depth: int = Field(default=1, description="How many hops to traverse (1=direct, 2=transitive)", ge=1, le=MAX_DEPTH)
    direction: str = Field(default="both", description="'out' = what this node depends on, 'in' = what depends on this node, 'both' = both directions")

class PathInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    repo_id: str = Field(..., description="Repo identifier (e.g. 'mercuryx-api')")
    source: str = Field(..., description="Source node name or ID (e.g. 'OrderController::store')")
    target: str = Field(..., description="Target node name or ID (e.g. 'PaymentService::charge')")

class OverviewInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    repo_id: str = Field(..., description="Repo identifier (e.g. 'mercuryx-api')")
    top_n: int = Field(default=10, description="How many critical nodes to return", ge=1, le=50)

class ImpactInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    repo_id: str = Field(..., description="Repo identifier (e.g. 'mercuryx-api')")
    node: str = Field(..., description="Node name or full node ID whose change impact to analyse (e.g. 'PermissionHelper::can')")
    depth: int = Field(default=3, description="How many hops upstream to trace (1=direct callers, 3=full cascade)", ge=1, le=MAX_DEPTH)


# ---------------------------------------------------------------------------
# Tool 1 — graph_list_repos
# ---------------------------------------------------------------------------

@mcp.tool(
    name="graph_list_repos",
    annotations={
        "title": "List Indexed Repositories",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def graph_list_repos() -> str:
    """
    List all repositories that have been indexed in the graph database.

    Returns a JSON array of repos with their ID, name, status, language breakdown,
    node/edge counts, and last_indexed timestamp.

    Use this tool first to discover available repo_ids before calling other tools.

    Returns:
        str: JSON array of repo objects:
        [
            {
                "repo_id": "mercuryx-api",
                "name": "mercuryx-api",
                "status": "ready",
                "last_indexed": "2026-04-16 10:18:18",
                "node_count": 5518,
                "edge_count": 9691,
                "languages": {"php": 5508, "javascript": 10}
            }
        ]
    """
    from mcp.server.fastmcp import Context
    from mcp.server.fastmcp import FastMCP

    # Access DB without lifespan state (use module-level DB_PATH)
    db = Database(DB_PATH)
    loader = GraphLoader(db)

    repos = db.list_repos()
    if not repos:
        return _ok([])

    result = []
    for repo in repos:
        stats = loader.get_stats(repo["id"])
        result.append(stats)

    return _ok(result)


# ---------------------------------------------------------------------------
# Tool 2 — graph_build
# ---------------------------------------------------------------------------

@mcp.tool(
    name="graph_build",
    annotations={
        "title": "Build or Update Repository Graph",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def graph_build(params: BuildInput) -> str:
    """
    Index a repository and build its dependency graph in the database.

    Parses all supported source files (PHP, Go, TypeScript, Python, Java, Rust,
    C#, Kotlin, Scala, Ruby, JS/JSX/TSX), extracts nodes (functions, classes,
    files) and edges (calls, imports, inherits), and stores them in SQLite.

    Automatically skips vendor/, node_modules/, and other irrelevant directories.
    On subsequent calls, only re-indexes files whose content has changed (MD5 check).
    Use force=true to force a full re-index.

    Args:
        params (BuildInput):
            - repo_path (str): Absolute path to the repository root
            - force (bool): Force full re-index (default: false)

    Returns:
        str: JSON object with indexing results:
        {
            "repo_id": "mercuryx-api",
            "status": "ready",
            "nodes": 5518,
            "edges": 9691,
            "files_indexed": 1218,
            "files_skipped": 842,
            "errors": 0,
            "elapsed_sec": 2.76
        }
    """
    repo_path = Path(params.repo_path)
    if not repo_path.exists():
        return _err(f"Path does not exist: {params.repo_path}")
    if not repo_path.is_dir():
        return _err(f"Path is not a directory: {params.repo_path}")

    try:
        result = index_repo(
            repo_path=repo_path,
            db_path=DB_PATH,
            force=params.force,
        )
        return _ok(result)
    except Exception as e:
        return _err(f"Indexing failed: {e}")


# ---------------------------------------------------------------------------
# Tool 3 — graph_query
# ---------------------------------------------------------------------------

@mcp.tool(
    name="graph_query",
    annotations={
        "title": "Search Graph Components",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def graph_query(params: QueryInput) -> str:
    """
    Full-text search for nodes (functions, classes, files) in a repo's graph.

    Searches across node names, docstrings, and file paths using SQLite FTS5.
    Results are ranked by relevance. Use this to find node IDs before calling
    graph_dependencies, graph_impact, or graph_path.

    Args:
        params (QueryInput):
            - repo_id (str): Repo identifier from graph_list_repos
            - query (str): Search term (e.g. 'OrderService', 'fromArray', 'payment')
            - node_type (str, optional): Filter — 'function', 'class', 'file', 'module'
            - limit (int): Max results (default: 20, max: 50)

    Returns:
        str: JSON object:
        {
            "query": "OrderService",
            "repo_id": "mercuryx-api",
            "count": 3,
            "results": [
                {
                    "id": "mercuryx-api::app/Services/Order/OrderService.php::OrderService",
                    "name": "OrderService",
                    "type": "class",
                    "file_path": "app/Services/Order/OrderService.php",
                    "language": "php",
                    "line_start": 12,
                    "docstring": "Handles order creation and fulfilment"
                }
            ]
        }
    """
    db = Database(DB_PATH)
    try:
        results = search_component(
            db, params.repo_id, params.query,
            limit=params.limit, node_type=params.node_type,
        )
        return _ok({
            "query": params.query,
            "repo_id": params.repo_id,
            "count": len(results),
            "results": results,
        })
    except Exception as e:
        return _err(str(e))


# ---------------------------------------------------------------------------
# Tool 4 — graph_dependencies
# ---------------------------------------------------------------------------

@mcp.tool(
    name="graph_dependencies",
    annotations={
        "title": "List Node Dependencies",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def graph_dependencies(params: DependenciesInput) -> str:
    """
    Show what a node depends on (outgoing) and/or what depends on it (incoming).

    Traverses the dependency graph up to `depth` hops. Use direction='out' to see
    what this node calls/imports. Use direction='in' to see what calls this node.
    Use direction='both' (default) to get the full picture.

    The `node` parameter accepts:
    - A full node ID from a previous result
    - A name like 'OrderService::create' or 'PermissionHelper::can'
    - A class name like 'OrderService' (returns first match)

    Args:
        params (DependenciesInput):
            - repo_id (str): Repo identifier
            - node (str): Node name or ID
            - depth (int): Traversal depth (1=direct, 2=transitive) — default: 1
            - direction (str): 'out', 'in', or 'both' — default: 'both'

    Returns:
        str: JSON object with node info plus dependencies/dependents lists, each item
             containing: node id/name/file_path/type, relation, confidence, depth.
    """
    db = Database(DB_PATH)
    try:
        loader = GraphLoader(db)
        repo = db.get_repo(params.repo_id)
        if not repo:
            return _err(f"Repo '{params.repo_id}' not found. Run graph_build first.")

        G = loader.load_repo(params.repo_id)
        node_id = _resolve_node_id(params.node, params.repo_id, db, G)
        if not node_id:
            return _err(f"Node not found: '{params.node}'. Try graph_query to search.")

        result: dict = {"node_id": node_id}

        if params.direction in ("out", "both"):
            result["dependencies"] = list_dependencies(G, node_id, depth=params.depth)

        if params.direction in ("in", "both"):
            result["dependents"] = list_dependents(G, node_id, depth=params.depth)

        return _ok(result)
    except Exception as e:
        return _err(str(e))


# ---------------------------------------------------------------------------
# Tool 5 — graph_impact
# ---------------------------------------------------------------------------

@mcp.tool(
    name="graph_impact",
    annotations={
        "title": "Analyse Change Impact",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def graph_impact(params: ImpactInput) -> str:
    """
    Analyse what breaks if a given node is changed or removed.

    Traverses all upstream callers/importers to find every node that directly or
    transitively depends on the target. Groups results by file so you can see which
    files need to be reviewed or updated.

    Use this before refactoring, renaming, or removing a function or class.

    Args:
        params (ImpactInput):
            - repo_id (str): Repo identifier
            - node (str): Node to analyse (name or ID, e.g. 'PermissionHelper::can')
            - depth (int): How many hops to trace upstream (default: 3, max: 5)

    Returns:
        str: JSON object:
        {
            "node": {"id": "...", "name": "PermissionHelper::can"},
            "total_affected": 42,
            "affected_files": 18,
            "affected": [
                {"name": "...", "file_path": "...", "type": "function", "depth": 1},
                ...
            ],
            "by_file": {
                "app/Http/Controllers/OrderController.php": [...],
                ...
            }
        }
    """
    db = Database(DB_PATH)
    try:
        loader = GraphLoader(db)
        repo = db.get_repo(params.repo_id)
        if not repo:
            return _err(f"Repo '{params.repo_id}' not found. Run graph_build first.")

        G = loader.load_repo(params.repo_id)
        node_id = _resolve_node_id(params.node, params.repo_id, db, G)
        if not node_id:
            return _err(f"Node not found: '{params.node}'. Try graph_query to search.")

        result = analyze_impact(G, node_id, depth=params.depth)
        return _ok(result)
    except Exception as e:
        return _err(str(e))


# ---------------------------------------------------------------------------
# Tool 6 — graph_path
# ---------------------------------------------------------------------------

@mcp.tool(
    name="graph_path",
    annotations={
        "title": "Find Dependency Path Between Nodes",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def graph_path(params: PathInput) -> str:
    """
    Find the shortest dependency path between two nodes.

    Uses Dijkstra's algorithm weighted by edge confidence (EXTRACTED > INFERRED > AMBIGUOUS).
    Useful for understanding how two seemingly unrelated components are connected,
    or verifying that there is no unexpected dependency between modules.

    Args:
        params (PathInput):
            - repo_id (str): Repo identifier
            - source (str): Starting node (name or ID, e.g. 'OrderController::store')
            - target (str): Ending node (name or ID, e.g. 'PaymentService::charge')

    Returns:
        str: JSON object:
        {
            "found": true,
            "path": ["id1", "id2", "id3"],
            "path_labels": ["OrderController::store", "OrderService::create", "PaymentService::charge"],
            "edges": [
                {"from": "id1", "to": "id2", "relation": "calls", "confidence": "EXTRACTED"},
                ...
            ],
            "length": 2
        }
        or {"found": false, "reason": "no path found"}
    """
    db = Database(DB_PATH)
    try:
        loader = GraphLoader(db)
        repo = db.get_repo(params.repo_id)
        if not repo:
            return _err(f"Repo '{params.repo_id}' not found. Run graph_build first.")

        G = loader.load_repo(params.repo_id)

        source_id = _resolve_node_id(params.source, params.repo_id, db, G)
        if not source_id:
            return _err(f"Source node not found: '{params.source}'. Try graph_query to search.")

        target_id = _resolve_node_id(params.target, params.repo_id, db, G)
        if not target_id:
            return _err(f"Target node not found: '{params.target}'. Try graph_query to search.")

        result = find_path(G, source_id, target_id)
        return _ok(result)
    except Exception as e:
        return _err(str(e))


# ---------------------------------------------------------------------------
# Tool 7 — graph_overview
# ---------------------------------------------------------------------------

@mcp.tool(
    name="graph_overview",
    annotations={
        "title": "Repository Graph Overview",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def graph_overview(params: OverviewInput) -> str:
    """
    Get a high-level architectural overview of a repository's dependency graph.

    Returns:
    - Repo stats (node/edge counts, languages, relation types)
    - Top N critical nodes by betweenness centrality (architectural choke-points)
    - Dependency cycles (potential design issues)
    - Community clusters (likely modules/bounded contexts)
    - Entry points (controllers, commands, cron jobs with no incoming dependencies)

    Use this as your starting point when exploring an unfamiliar codebase.

    Args:
        params (OverviewInput):
            - repo_id (str): Repo identifier
            - top_n (int): How many critical nodes to include (default: 10)

    Returns:
        str: JSON object with stats, critical_nodes, cycles, communities, entry_points.
    """
    db = Database(DB_PATH)
    try:
        loader = GraphLoader(db)
        repo = db.get_repo(params.repo_id)
        if not repo:
            return _err(f"Repo '{params.repo_id}' not found. Run graph_build first.")

        stats = loader.get_stats(params.repo_id)
        G = loader.load_repo(params.repo_id)

        critical = get_critical_nodes(G, top_n=params.top_n)
        cycles = find_cycles(G, max_cycles=20)
        communities = get_communities(G, min_size=3)
        entries = get_entry_points(G, node_types=["function", "class"])[:20]

        return _ok({
            "stats": stats,
            "critical_nodes": critical["nodes"],
            "cycles": cycles,
            "communities": {
                "count": communities["count"],
                "algorithm": communities.get("algorithm"),
                "top": communities["communities"][:10],
            },
            "entry_points": entries,
        })
    except Exception as e:
        return _err(str(e))
