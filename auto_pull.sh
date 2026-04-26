#!/usr/bin/env bash
# auto_pull.sh — pull all repos and trigger incremental re-index via graph_build
# Runs as a cron fallback (every 30 min) in case webhook misses a push.
#
# How re-index works:
#   git pull runs on the HOST (host has git + credentials).
#   Re-index is triggered via `docker exec` into the running container,
#   because the container mounts ./repos:/app/repos — same files, different path.
#   NOTE: /sse is a streaming endpoint, NOT a tool-call endpoint — don't use curl there.

set -euo pipefail

REPOS_DIR="${REPOS_DIR:-/opt/Personal-Code-Obsidian/repos}"
CONTAINER="${CONTAINER:-code-obsidian}"   # docker container name
LOG="/opt/Personal-Code-Obsidian/auto_pull.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

log "=== auto_pull started ==="

for repo_dir in "$REPOS_DIR"/*/; do
    [[ -d "$repo_dir/.git" ]] || continue
    repo=$(basename "$repo_dir")

    result=$(git -C "$repo_dir" pull --ff-only 2>&1 | tail -1)
    log "$repo: $result"

    # Trigger re-index only if there were changes
    if [[ "$result" != "Already up to date." ]]; then
        log "$repo: changes detected — triggering graph_build via docker exec..."

        # Container path = /app/repos/<repo> (see docker-compose volumes: ./repos:/app/repos)
        container_repo_path="/app/repos/$repo"

        docker exec "$CONTAINER" python -c "
import sys, json
from graph.indexer import index_repo
result = index_repo(repo_path='$container_repo_path', db_path='/app/data/graph.db', force=False)
print(json.dumps(result))
" >> "$LOG" 2>&1 && log "$repo: re-index done" \
          || log "$repo: re-index FAILED (check docker logs)"
    fi
done

log "=== auto_pull done ==="
