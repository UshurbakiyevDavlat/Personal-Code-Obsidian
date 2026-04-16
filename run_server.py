"""
run_server.py — Wrapper script for the code-obsidian MCP server.

This wrapper ensures:
1. The working directory is always the project root (so .env and data/ are found)
2. The project root is on sys.path (so graph.*, parser.* imports work)
3. Transport is configurable via MCP_TRANSPORT env var

Usage:
    # stdio (for local Claude Code)
    python run_server.py

    # SSE / HTTP (for Cowork or remote access)
    MCP_TRANSPORT=sse MCP_PORT=8000 python run_server.py

Register with Claude Code:
    claude mcp add code-obsidian /path/to/venv/bin/python /path/to/personal-code-obsidian/run_server.py
"""
import os
import sys

# Pin working directory to the project root regardless of where this is launched from
_HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(_HERE)
sys.path.insert(0, _HERE)

from server.server import mcp

if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    port = int(os.environ.get("MCP_PORT", "8000"))

    if transport == "sse":
        mcp.run(transport="sse", host="0.0.0.0", port=port)
    else:
        mcp.run()
