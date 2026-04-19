# personal-code-obsidian

> Build a dependency graph of your entire codebase and answer architectural questions directly from Cowork.

**Problem:** when working with a large or unfamiliar repo it's hard to know — where things live, what depends on what, and what breaks if you change a given service.

**Solution:** parse the repo → build a graph (functions, classes, files, modules) → ask questions like "what breaks if I change OrderService" or "show me god objects in this codebase" directly from Cowork via the `code-intelligence` plugin.

---

## Features

- **21-language parsing** — PHP, Go, TypeScript, JavaScript (JSX/TSX), Python, Java, Rust, C#, Kotlin, Scala, Ruby, C, C++ via tree-sitter
- **Smart exclude filtering** — vendor/, node_modules/, minified files auto-excluded; nested path matching (`public/js` inside any subdirectory); `.codeobsidian.yml` per-repo config
- **Incremental indexing** — only re-indexes changed files via MD5 hash; stale nodes from newly-excluded files are cleaned automatically
- **Pre-computed graph metrics** — god objects, critical nodes, communities, cycles, entry points computed once at index time and served instantly (no timeout on 40k+ node repos)
- **Dual-mode centrality** — betweenness centrality for small graphs (≤5k nodes), degree-based for large graphs (O(E), instant)
- **Full-text search** — FTS5 index on names, docstrings, file paths
- **God Object detection** — nodes with both high in-degree AND out-degree (real coupling, excludes structural `contains` edges)
- **8 MCP tools** — callable from Cowork via SSE transport with Bearer token auth
- **KB integration** — `graph_sync_kb` generates an architectural document ready for `kb_add_document`
- **Auto-pull + cron** — scheduled git pull every 30 min with automatic re-index on changes
- **GitHub webhooks** — instant re-index on push via HMAC-verified `/webhook/github`

---

## Stack

| Layer | Technology |
|---|---|
| Parsing | tree-sitter (21 extensions) |
| Graph algorithms | NetworkX |
| Storage | SQLite + FTS5 + `repo_metrics` cache table |
| MCP server | FastMCP (SSE transport) |
| Deploy | Docker + Nginx + DuckDNS SSL |
| Integration | MCP protocol (Cowork) + `code-intelligence` plugin |

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
pip install tree-sitter-python tree-sitter-php tree-sitter-go tree-sitter-typescript \
            tree-sitter-javascript tree-sitter-java tree-sitter-rust tree-sitter-c-sharp \
            tree-sitter-kotlin tree-sitter-scala tree-sitter-ruby tree-sitter-c tree-sitter-cpp
```

### 2. Run locally (stdio, for Claude Code CLI)

```bash
python run_server.py
```

### 3. Run as SSE server (for Cowork / remote access)

```bash
MCP_TRANSPORT=sse MCP_PORT=8002 MCP_AUTH_TOKEN=your-token python run_server.py
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
  - public/js
  - storage/
```

---

## MCP Tools

| Tool | Description |
|---|---|
| `graph_list_repos` | List all indexed repos with status and stats |
| `graph_build` | Index or re-index a repo; pre-computes metrics into `repo_metrics` table |
| `graph_query` | Full-text search for components by name, path, or docstring |
| `graph_dependencies` | Incoming and outgoing edges for a component (configurable depth) |
| `graph_impact` | What breaks if this component changes (BFS up to depth 4) |
| `graph_path` | Shortest dependency path between two components |
| `graph_overview` | Instant: critical nodes, god objects, communities, cycles, entry points — served from cache |
| `graph_sync_kb` | Generate an architectural KB document → push via `kb_add_document` |

### graph_overview cache architecture

`graph_build` pre-computes all expensive metrics (betweenness, Louvain communities, god objects, cycles) and stores them in the `repo_metrics` SQLite table. `graph_overview` reads from this cache — instant response regardless of repo size.

```
graph_build(repo_path=...) → Pass 4: compute metrics → store in repo_metrics
graph_overview(repo_id=...)  → SELECT from repo_metrics → instant response
```

---

## Deploy (VPS)

### 1. Clone repos and set up auto-pull

```bash
GITHUB_TOKEN=ghp_xxxx ./scripts/vps_setup.sh
```

This clones all repos to `/opt/Personal-Code-Obsidian/repos/`, creates `/opt/Personal-Code-Obsidian/auto_pull.sh`, and adds a cron job (every 30 min).

### 2. Start with Docker Compose

```bash
cp .env.example .env  # fill in MCP_AUTH_TOKEN, GITHUB_WEBHOOK_SECRET
docker-compose up -d
```

### 3. Connect to Cowork via MCP

Add the SSE URL in Cowork settings:

```
https://YOUR_DOMAIN/sse
Authorization: Bearer YOUR_TOKEN
```

Nginx config is in `nginx/code-obsidian.conf` — includes Bearer token auth, SSE headers, `/health` without auth, `/webhook/github` for push events.

### 4. Set up GitHub webhooks (for instant re-index on push)

In each repo → Settings → Webhooks:
- URL: `https://YOUR_DOMAIN/webhook/github`
- Content type: `application/json`
- Secret: `GITHUB_WEBHOOK_SECRET` from `.env`
- Events: `push`

---

## Cowork Plugin

Install the **`code-intelligence`** plugin for natural-language orchestration of all graph tools:

| Skill | What it does |
|---|---|
| `tech-debt` | Prioritized tech debt report — god objects, cycles, critical nodes |
| `refactor` | Sequenced refactoring plan with blast radius and file list |
| `pre-pr` | Impact check before committing — risk level + test checklist |
| `write-smart` | Architecture-aware code generation matching codebase patterns |

With the plugin, instead of calling tools manually you just say: *"Что самое хрупкое в mps?"* or *"Сделай план рефакторинга OrderService"*.

---

## Project Structure

```
personal-code-obsidian/
├── parser/
│   ├── extract.py       ← LanguageConfig + AST extraction (21 extensions)
│   └── indexer.py       ← orchestrator, exclude filtering, incremental MD5,
│                           EXCLUDE_SUFFIXES (.min.js etc), Pass 4 metrics
├── graph/
│   ├── db.py            ← SQLite + FTS5 schema + repo_metrics table
│   ├── storage.py       ← batch CRUD
│   ├── loader.py        ← SQLite → NetworkX DiGraph
│   ├── queries.py       ← search_component, list_dependencies, list_dependents
│   └── algorithms.py    ← find_path, analyze_impact, find_cycles,
│                           get_critical_nodes (dual-mode), get_communities,
│                           get_entry_points, get_god_objects, subgraph_around
├── server/
│   └── server.py        ← FastMCP, 8 tools, SSE, /health, Bearer auth,
│                           /webhook/github (HMAC), cache-first graph_overview
├── scripts/
│   └── vps_setup.sh     ← clone all repos, create auto_pull.sh, add cron
├── run_server.py        ← entry point (stdio or SSE)
├── nginx/
│   └── code-obsidian.conf
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
└── SPEC.md
```

---

## Exclude logic

Three layers of noise filtering:

1. **Directory excludes** — `vendor`, `node_modules`, `public/js`, `stories`, `umd`, etc. Supports nested path matching: `public/js` pattern matches `oms/public/js/app.js`
2. **Suffix excludes** — `.min.js`, `.min.css`, `.bundle.js`, `.chunk.js` always excluded regardless of directory
3. **Stale cleanup** — when exclude rules change, nodes from newly-excluded files are removed from the DB automatically on the next `graph_build`

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
- [x] Phase 6 — Enhancements v2:
  - [x] Pre-computed metrics (no timeout on large repos)
  - [x] God Object detection
  - [x] JS noise filtering (nested excludes, suffix excludes)
  - [x] GitHub webhooks (instant re-index on push)
  - [x] Auto-pull cron (every 30 min)
  - [x] `code-intelligence` Cowork plugin (tech-debt, refactor, pre-pr, write-smart)
- [ ] Phase 7 — Future:
  - [ ] Semantic search via embeddings
  - [ ] Git history diff — how the graph changed between commits
  - [ ] Cross-repo dependency analysis

See [SPEC.md](./SPEC.md) for the full technical specification.

---

## License

MIT
