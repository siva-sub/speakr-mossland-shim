#!/bin/bash
# speakr-mcp launcher — sets env vars then runs the MCP server.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Source secrets from .env (gitignored)
if [ -f "$SCRIPT_DIR/.env" ]; then
  set -a
  source "$SCRIPT_DIR/.env"
  set +a
fi

# Defaults if not set in .env
export SPEAKR_URL="${SPEAKR_URL:-http://127.0.0.1:8899}"
export SHIM_URL="${SHIM_URL:-http://127.0.0.1:8001}"

exec python3 "$SCRIPT_DIR/server.py"
