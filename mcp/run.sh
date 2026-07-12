#!/bin/bash
# speakr-mcp launcher — sets env vars then runs the MCP server.
# Pi's mcp.json doesn't reliably pass `env` to the subprocess, so we do it here.
set -euo pipefail
cd /home/siva/speakr-mcp

# Source secrets from .env (gitignored)
if [ -f .env ]; then
	set -a
	source .env
	set +a
fi

# Defaults if not set in .env
export SPEAKR_URL="${SPEAKR_URL:-http://127.0.0.1:8899}"

exec python3 /home/siva/speakr-mcp/server.py
