#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_PORT="${API_PORT:-8787}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
API_HOST="${API_HOST:-0.0.0.0}"
START_FRONTEND="${START_FRONTEND:-1}"

cd "$ROOT_DIR"

if [[ ! -x "api/.venv/bin/uvicorn" ]]; then
  echo "api/.venv is missing. Create it first, then run:"
  echo "  python3 -m venv api/.venv"
  echo "  api/.venv/bin/pip install -r api/requirements.txt"
  exit 1
fi

if ! curl -fsS "http://localhost:20128/v1/models" >/dev/null 2>&1; then
  echo "Warning: 9Router local gateway is not responding at http://localhost:20128/v1"
  echo "Agent settings can still load, but LLM calls may fail until 9Router is running."
fi

cleanup() {
  if [[ -n "${API_PID:-}" ]]; then
    kill "$API_PID" >/dev/null 2>&1 || true
  fi
  if [[ -n "${WEB_PID:-}" ]]; then
    kill "$WEB_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

echo "Starting Appora API on http://localhost:${API_PORT}"
api/.venv/bin/uvicorn api.main:app --host "$API_HOST" --port "$API_PORT" &
API_PID="$!"

for _ in {1..40}; do
  if curl -fsS "http://localhost:${API_PORT}/api/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

curl -fsS "http://localhost:${API_PORT}/api/healthz" >/dev/null
echo "API ready."

if [[ "$START_FRONTEND" != "0" ]]; then
  echo "Starting Vite on http://localhost:${FRONTEND_PORT}"
  npm run dev -- --host 0.0.0.0 --port "$FRONTEND_PORT" &
  WEB_PID="$!"
  echo "Local Appora UI: http://localhost:${FRONTEND_PORT}"
else
  echo "API-only mode. Open the Vercel UI and it will call http://localhost:${API_PORT}."
fi

wait
