# personal-code-obsidian

> Build a dependency graph of your codebase and answer architectural questions through MCP in Cowork.

**Problem:** when working with a large or unfamiliar repo it's hard to know — where things live, what depends on what, and what breaks if you change a given service.

**Solution:** parse the repo → build a graph (functions, classes, files, modules) → ask questions like "show me dependencies of OrderService" or "what breaks if I change AuthMiddleware" via MCP from Cowork.

---

## Features

- **Multi-language parsing** — PHP, Go, TypeScript, Python, Java, Rust, C#, Kotlin, Scala, Ruby, JS/JSX/TSX via tree-sitter
- **Smart exclude filtering** — vendor/, node_modules/, generated files automatically excluded
- **Unambiguous node names** — always `ClassName::methodName`, never bare `methodName()`
- **Incremental indexing** — only re-indexes changed files via MD5 file hash
- **Full-text search** — FTS5 index on names, docstrings, file paths
- **Graph algorithms** — shortest path, impact analysis, cycle detection, community detection, centrality (NetworkX)
- **MCP API** — 7 tools callable from Cowork
- **Interactive visualization** — vis.js graph in browser

---

## Stack

| Layer | Technology |
|---|---|
| Parsing | tree-sitter |
| Graph algorithms | NetworkX |
| Storage | SQLite + FTS5 |
| Server | FastAPI |
| Visualization | vis.js |
| Deploy | Docker + Nginx |
| Integration | MCP protocol |

---

## Quick Start

### 1. Install dependencies

```bash
pip install tree-sitter networkx fastapi uvicorn pyyaml
```

### 2. Index a repository

```bash
python -m parser.indexer /path/to/your/repo --db data/graph.db
```

Force full re-index:

```bash
python -m parser.indexer /path/to/your/repo --db data/graph.db --force
```

### 3. Configure the repo (optional)

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
include_docs:
  - README.md
  - docs/
```

---

## MCP Tools

| Tool | Description |
|---|---|
| `graph_build` | Build or update the graph for a repo |
| `graph_query` | Search components by name or description |
| `graph_impact` | What breaks if this component changes |
| `graph_path` | Shortest path between two components |
| `graph_dependencies` | Incoming and outgoing edges for a component |
| `graph_overview` | Critical nodes, top communities, repo stats |
| `graph_list_repos` | List all indexed repos with status |

---

## Deploy

Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
```

Start with Docker Compose:

```bash
docker-compose up -d
```

The API will be available at `http://YOUR_IP:8080` and the vis.js graph at `http://YOUR_IP:3000`.

For HTTPS, point Nginx to the server and run certbot for SSL.

### Connect to Cowork via MCP

```json
{
  "name": "code-obsidian",
  "url": "https://YOUR_DOMAIN/mcp",
  "auth": "Bearer YOUR_TOKEN"
}
```

---

## Project Structure

```
personal-code-obsidian/
├── parser/
│   ├── extract.py       ← AST extraction (13 languages)
│   └── indexer.py       ← orchestrator + exclude filtering
├── graph/
│   ├── db.py            ← SQLite + FTS5
│   ├── storage.py       ← batch CRUD
│   ├── loader.py        ← SQLite → NetworkX
│   ├── queries.py       ← search, dependencies
│   └── algorithms.py    ← paths, impact, cycles, centrality
├── server/
│   ├── main.py          ← FastAPI app
│   ├── mcp.py           ← MCP protocol handler
│   ├── auth.py
│   └── tools/           ← one file per MCP tool
├── web/
│   └── index.html       ← vis.js visualization
├── tests/
├── docker-compose.yml
├── Dockerfile
├── .env.example
└── SPEC.md              ← full technical specification
```

---

## Roadmap

- [x] Phase 0 — Research & spec
- [x] Phase 1 — Parser layer (tree-sitter, SQLite, exclude filtering)
- [ ] Phase 2 — Query engine (NetworkX, FTS5, algorithms)
- [ ] Phase 3 — MCP server (FastAPI, 7 tools, auth)
- [ ] Phase 4 — VPS deploy (Docker, Nginx, GitHub Actions CI/CD)
- [ ] Phase 5 — Cowork integration (MCP plugin)
- [ ] Phase 6 — Enhancements (embeddings, webhooks, git history)

See [SPEC.md](./SPEC.md) for the full technical specification.

---

## License

MIT
