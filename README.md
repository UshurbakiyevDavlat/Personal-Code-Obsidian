# personal-code-obsidian

> Build a dependency graph of your codebase and answer architectural questions through MCP in Cowork.

**Problem:** when working with a large or unfamiliar repo it's hard to know — where things live, what depends on what, and what breaks if you change a given service.

**Solution:** parse the repo → build a graph (functions, classes, files, modules) → ask questions like "show me dependencies of OrderService" or "what breaks if I change AuthMiddleware" directly from Cowork.

---

## Features

- **21-language parsing** — PHP, Go, TypeScript, JavaScript (JSX/TSX), Python, Java, Rust, C#, Kotlin, Scala, Ruby, **C, C++** via tree-sitter
- **Smart exclude filtering** — vendor/, node_modules/, generated files auto-excluded via `.codeobsidian.yml`
- **Unambiguous node names** — always `ClassName::methodName`, never bare `methodName()`
- **Incremental indexing** — only re-indexes changed files via MD5 hash
- **Full-text search** — FTS5 index on names, docstrings, file paths
- **Graph algorithms** — Dijkstra path finding, BFS impact analysis, cycle detection, Louvain community detection, betweenness centrality (NetworkX)
- **8 MCP tools** — callable from Cowork via SSE transport
- **KB integration** — `graph_sync_kb` generates an architectural document ready for `kb_add_document`

---

## Stack

| Layer | Technology |
|---|---|
| Parsing | tree-sitter (21 extensions) |
| Graph algorithms | NetworkX |
| Storage | SQLite + FTS5 |
| MCP server | FastMCP (SSE transport) |
| Deploy | Docker + Nginx + DuckDNS SSL |
| Integration | MCP protocol (Cowork) |

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
pip install tree-sitter-python tree-sitter-php tree-sitter-go tree-sitter-typescript \
            tree-sitter-javascript tree-sitter-java tree-sitter-rust tree-sitter-c-sharp \
            tree-sitter-kotlin tree-sitter-scala tree-sitter-ruby tree-sitter-c tree-sitter-cpp
```

### 2. Run the MCP server (stdio, for local Claude Code)

```bash
python run_server.py
```

### 3. Run as SSE server (for Cowork / remote access)

```bash
MCP_TRANSPORT=sse MCP_PORT=8000 MCP_AUTH_TOKEN=your-token python run_server.py
```

### 4. Configure a repo (optional)

Place `.codeobsidian.yml` in the root of the target repo:

```yaml
name: my-project
languages:
  - php
  - go
exclude:
  - vendor/
  - node_modules/
  - public/
  - storage/
```

---

## MCP Tools

| Tool | Description |
|---|---|
| `graph_list_repos` | List all indexed repos with status and stats |
| `graph_build` | Build or update the graph for a repo |
| `graph_query` | Full-text search for components by name, path, or docstring |
| `graph_dependencies` | Incoming and outgoing edges for a component (configurable depth) |
| `graph_impact` | What breaks if this component changes |
| `graph_path` | Shortest dependency path between two components |
| `graph_overview` | Critical nodes, communities, cycles, repo stats |
| `graph_sync_kb` | Generate an architectural KB document → push via `kb_add_document` |

### KB Sync workflow (2 steps in Cowork)

```
1. graph_sync_kb(repo_id="my-repo")
   → {title: "my-repo — Architecture Graph — 2026-04-17", text: "..."}

2. kb_add_document(title=..., text=...)
   → ✅ Indexed — now kb_search("how does auth work") returns structural facts
```

---

## Deploy (VPS)

Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
```

Start with Docker Compose:

```bash
docker-compose up -d
```

### Connect to Cowork via MCP

Add the SSE URL in Cowork settings:

```
https://YOUR_DOMAIN/sse?token=YOUR_TOKEN
```

Nginx config is in `nginx/code-obsidian.conf` — includes Bearer token auth, SSE headers, and `/health` without auth.

---

## Project Structure

```
personal-code-obsidian/
├── parser/
│   ├── extract.py       ← LanguageConfig + AST extraction (21 extensions)
│   └── indexer.py       ← orchestrator, exclude filtering, incremental MD5
├── graph/
│   ├── db.py            ← SQLite + FTS5 schema
│   ├── storage.py       ← batch CRUD
│   ├── loader.py        ← SQLite → NetworkX DiGraph
│   ├── queries.py       ← search_component, list_dependencies, list_dependents
│   └── algorithms.py    ← find_path, analyze_impact, find_cycles, get_critical_nodes,
│                           get_communities, get_entry_points, subgraph_around
├── server/
│   └── server.py        ← FastMCP, 8 tools, SSE, /health, Bearer auth
├── run_server.py        ← entry point (sets cwd + sys.path, stdio or SSE)
├── nginx/
│   └── code-obsidian.conf
├── docker-compose.yml
├── Dockerfile           ← multi-stage, python:3.12-slim
├── requirements.txt
├── .env.example
└── SPEC.md              ← full technical specification
```

---

## Node ID format

```
{repo_id}::{relative_file_path}::{ClassName}::{method_name}

# Examples:
mercuryx-api::app/Services/Order/OrderService.php::OrderService
mercuryx-api::app/Services/Order/OrderService.php::OrderService::create
```

## Edge confidence

| Type | Weight | When |
|---|---|---|
| EXTRACTED | 1.0 | Explicit: call, import, inheritance |
| INFERRED | 0.7 | Logical: similar names, shared objects |
| AMBIGUOUS | 0.4 | Dynamic calls, polymorphism |

---

## Roadmap

- [x] Phase 0 — Research & spec
- [x] Phase 1 — Parser layer (21 languages, SQLite, incremental indexing)
- [x] Phase 2 — Query engine (NetworkX, FTS5, algorithms)
- [x] Phase 3 — MCP server (FastMCP, 8 tools, SSE, auth)
- [x] Phase 4 — VPS deploy (Docker, Nginx, DuckDNS SSL)
- [x] Phase 5 — Cowork integration (connected, all 8 tools working)
- [ ] Phase 6 — Enhancements (GitHub webhooks, embeddings, git history diff)

See [SPEC.md](./SPEC.md) for the full technical specification.

---

## License

MIT
