"""
parser/indexer.py — Orchestrates repo indexing.

Flow:
  1. Read .codeobsidian.yml config
  2. Walk repo, skip excluded paths
  3. For each file: check file_hash for incremental skip
  4. Call extract_file() → get nodes + edges
  5. Resolve cross-file edge targets
  6. Save to SQLite via GraphStorage
  7. Update repo stats
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from dataclasses import dataclass, field

from graph.db import Database
from graph.storage import GraphStorage, Node, Edge
from parser.extract import extract_file, supported_extensions

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


# ── Config ────────────────────────────────────────────────────────────────────

DEFAULT_EXCLUDE = {
    "vendor", "node_modules", "public/js", "public/css", "public/build",
    "storage", "bootstrap/cache", ".git", ".idea", "__pycache__",
    "dist", "build", ".next", ".nuxt",
}

@dataclass
class RepoConfig:
    name: str
    languages: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    include_docs: list[str] = field(default_factory=list)

    def all_excludes(self) -> set[str]:
        return DEFAULT_EXCLUDE | set(self.exclude)


def load_config(repo_root: Path) -> RepoConfig:
    config_file = repo_root / ".codeobsidian.yml"
    name = repo_root.name

    if config_file.exists() and HAS_YAML:
        try:
            with open(config_file) as f:
                data = yaml.safe_load(f) or {}
            return RepoConfig(
                name=data.get("name", name),
                languages=data.get("languages", []),
                exclude=data.get("exclude", []),
                include_docs=data.get("include_docs", []),
            )
        except Exception:
            pass

    return RepoConfig(name=name)


# ── Exclude logic ─────────────────────────────────────────────────────────────

def is_excluded(path: Path, repo_root: Path, excludes: set[str]) -> bool:
    """
    Return True if path should be skipped.
    Checks each part of the relative path against exclude patterns.
    """
    try:
        rel = path.relative_to(repo_root)
    except ValueError:
        return False

    parts = rel.parts
    rel_str = str(rel)

    for pattern in excludes:
        pattern = pattern.strip("/")
        # Match any directory component or path prefix
        if pattern in parts:
            return True
        if rel_str.startswith(pattern + "/") or rel_str == pattern:
            return True
        # Glob-style wildcard at the end (e.g. "**/migrations/**")
        if "**" in pattern:
            seg = pattern.replace("**/", "").replace("/**", "")
            if seg in parts:
                return True

    return False


def collect_files(repo_root: Path, excludes: set[str]) -> list[Path]:
    """Walk repo and return all supported source files, respecting excludes."""
    ext = supported_extensions()
    result = []
    for p in repo_root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in ext:
            continue
        if is_excluded(p, repo_root, excludes):
            continue
        result.append(p)
    return sorted(result)


# ── Cross-file resolution ─────────────────────────────────────────────────────

def resolve_edges(
    raw_edges: list[dict],
    name_to_nid: dict[str, str],
    repo_id: str,
) -> list[Edge]:
    """
    Convert raw edge dicts (with _tgt or _tgt_name) to Edge objects.
    Edges with unresolvable targets are dropped.
    """
    resolved: list[Edge] = []
    skipped = 0

    for e in raw_edges:
        src = e.get("_src")
        tgt = e.get("_tgt")
        tgt_name = e.get("_tgt_name")

        if not src:
            skipped += 1
            continue

        if tgt is None and tgt_name:
            # Try to resolve by name (case-insensitive)
            tgt = name_to_nid.get(tgt_name.lower())
            if tgt is None:
                skipped += 1
                continue

        if tgt is None or src == tgt:
            skipped += 1
            continue

        resolved.append(Edge(
            source_id=src,
            target_id=tgt,
            repo_id=repo_id,
            relation=e["relation"],
            confidence=e.get("confidence", "EXTRACTED"),
            source_line=e.get("line"),
            metadata=e.get("metadata", {}),
        ))

    if skipped:
        print(f"  [resolve] dropped {skipped} unresolvable edges")

    return resolved


# ── Main indexer ──────────────────────────────────────────────────────────────

def index_repo(
    repo_path: str | Path,
    db_path: str | Path,
    force: bool = False,
) -> dict:
    """
    Index a repository into SQLite.

    Args:
        repo_path: Path to the repo root.
        db_path:   Path to the SQLite database file.
        force:     If True, re-index all files regardless of file_hash.

    Returns:
        {"repo_id": str, "nodes": int, "edges": int, "files": int,
         "skipped": int, "errors": int, "elapsed_sec": float}
    """
    t0 = time.time()
    repo_root = Path(repo_path).resolve()
    db = Database(db_path)
    storage = GraphStorage(db)

    # Load config
    config = load_config(repo_root)
    repo_id = repo_root.name  # e.g. "mercuryx-api"
    excludes = config.all_excludes()

    print(f"[indexer] repo={repo_id}  root={repo_root}")
    print(f"[indexer] excluding: {sorted(excludes)}")

    # Upsert repo record
    db.upsert_repo({
        "id": repo_id,
        "path": str(repo_root),
        "name": config.name,
        "languages": json.dumps(config.languages),
        "status": "indexing",
    })

    # Get existing file hashes for incremental updates
    existing_hashes: dict[str, str] = {} if force else db.get_file_hashes(repo_id)

    # Collect files
    files = collect_files(repo_root, excludes)
    print(f"[indexer] found {len(files)} files after exclude filter")

    # --- Pass 1: Extract all files ---
    all_nodes: list[Node] = []
    all_raw_edges: list[dict] = []
    total_files = 0
    skipped_files = 0
    error_files = 0

    for file_path in files:
        try:
            rel = str(file_path.relative_to(repo_root))
        except ValueError:
            rel = str(file_path)

        # Incremental: skip unchanged files
        if not force and rel in existing_hashes:
            result = extract_file(file_path, repo_id, repo_root)
            if result.get("file_hash") == existing_hashes[rel]:
                skipped_files += 1
                continue
            # File changed — delete old nodes first
            db.delete_nodes_by_file(repo_id, rel)

        result = extract_file(file_path, repo_id, repo_root)

        if result.get("error"):
            print(f"  [error] {rel}: {result['error']}")
            error_files += 1
            continue

        # Convert nodes
        for n in result["nodes"]:
            all_nodes.append(Node(
                id=n["id"],
                repo_id=n["repo_id"],
                type=n["type"],
                name=n["name"],
                file_path=n["file_path"],
                language=n["language"],
                line_start=n.get("line_start"),
                line_end=n.get("line_end"),
                docstring=n.get("docstring"),
                file_hash=n.get("file_hash"),
            ))

        all_raw_edges.extend(result.get("edges", []))
        total_files += 1

    print(f"[indexer] extracted {len(all_nodes)} nodes from {total_files} files "
          f"({skipped_files} skipped, {error_files} errors)")

    # --- Pass 2: Save nodes ---
    n_saved = storage.save_nodes(all_nodes)

    # --- Pass 3: Resolve & save edges ---
    # Build global name → nid index from all extracted nodes
    name_to_nid: dict[str, str] = {}
    for node in all_nodes:
        name_to_nid[node.name.lower()] = node.id
        short = node.name.split("::")[-1].lower()
        if short not in name_to_nid:
            name_to_nid[short] = node.id

    known_ids = {n.id for n in all_nodes}
    resolved_edges = resolve_edges(all_raw_edges, name_to_nid, repo_id)
    e_saved = storage.save_edges(resolved_edges, known_ids)

    # --- Finalize ---
    db.update_repo_counts(repo_id)
    db.set_repo_status(repo_id, "ready")

    elapsed = round(time.time() - t0, 2)
    print(f"[indexer] done in {elapsed}s: {n_saved} nodes, {e_saved} edges")

    return {
        "repo_id": repo_id,
        "nodes": n_saved,
        "edges": e_saved,
        "files": total_files,
        "skipped": skipped_files,
        "errors": error_files,
        "elapsed_sec": elapsed,
    }


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Index a code repo into the graph DB")
    parser.add_argument("repo_path", help="Path to the repository root")
    parser.add_argument("--db", default="data/graph.db", help="Path to SQLite DB (default: data/graph.db)")
    parser.add_argument("--force", action="store_true", help="Force full re-index")
    args = parser.parse_args()

    result = index_repo(args.repo_path, args.db, force=args.force)
    print(json.dumps(result, indent=2))
