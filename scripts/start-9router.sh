#!/usr/bin/env bash
set -euo pipefail

HOST="${NINE_ROUTER_HOST:-0.0.0.0}"
PORT="${NINE_ROUTER_PORT:-20128}"

if ! command -v 9router >/dev/null 2>&1; then
  echo "9router is not installed. Install it with: npm install -g 9router" >&2
  exit 1
fi

NPM_ROOT="$(npm root -g)"
ROUTER_DIR="${NPM_ROOT}/9router"
SERVER_PATH="${ROUTER_DIR}/app/server.js"

if [[ ! -f "$SERVER_PATH" ]]; then
  echo "9router server bundle not found at: $SERVER_PATH" >&2
  echo "Try reinstalling with: npm install -g 9router" >&2
  exit 1
fi

echo "Starting 9Router on http://${HOST}:${PORT}"
echo "Dashboard: http://${HOST}:${PORT}/dashboard"
echo "OpenAI-compatible endpoint: http://${HOST}:${PORT}/v1"

export PORT="$PORT"
export HOSTNAME="$HOST"
export NODE_ENV=production
export NODE_PATH="${HOME}/.9router/runtime/node_modules:${ROUTER_DIR}/app/node_modules:${NODE_PATH:-}"

exec node --max-old-space-size=6144 "$SERVER_PATH"
