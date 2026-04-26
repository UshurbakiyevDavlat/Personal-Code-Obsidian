# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# Install build deps (needed for some tree-sitter wheels)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps into /app/venv
COPY requirements.txt .
RUN python -m venv /app/venv \
    && /app/venv/bin/pip install --upgrade pip \
    && /app/venv/bin/pip install --no-cache-dir -r requirements.txt \
    && /app/venv/bin/pip install --no-cache-dir \
        tree-sitter-python \
        tree-sitter-php \
        tree-sitter-go \
        tree-sitter-typescript \
        tree-sitter-javascript \
        tree-sitter-java \
        tree-sitter-rust \
        tree-sitter-c-sharp \
        tree-sitter-kotlin \
        tree-sitter-scala \
        tree-sitter-ruby \
        tree-sitter-c \
        tree-sitter-cpp


# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

WORKDIR /app

# Install git (required for webhook auto-pull in _rebuild_repo_async)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy venv from builder
COPY --from=builder /app/venv /app/venv

# Copy project source
COPY parser/    ./parser/
COPY graph/     ./graph/
COPY server/    ./server/
COPY run_server.py .

# Data dir (SQLite DB) and repos dir (mounted from host)
RUN mkdir -p /app/data /app/repos

ENV PATH="/app/venv/bin:$PATH"
ENV PYTHONPATH="/app"
ENV MCP_TRANSPORT=sse
ENV MCP_PORT=8000
ENV DB_PATH=/app/data/graph.db

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["python", "run_server.py"]
