#!/bin/sh
set -eu

APP_PORT="${PORT:-8000}"
NINE_ROUTER_PORT="${NINE_ROUTER_PORT:-20128}"

if [ "${APPORA_EMBED_9ROUTER:-true}" = "true" ]; then
  if command -v 9router >/dev/null 2>&1; then
    export PORT="$NINE_ROUTER_PORT"
    export HOSTNAME="${NINE_ROUTER_HOSTNAME:-127.0.0.1}"
    export NEXT_PUBLIC_BASE_URL="${NINE_ROUTER_PUBLIC_BASE_URL:-http://127.0.0.1:${NINE_ROUTER_PORT}}"
    9router >/tmp/appora-9router.log 2>&1 &
    echo "Started embedded 9Router on ${NEXT_PUBLIC_BASE_URL}"
  else
    echo "9Router binary not found; Appora will require NINE_ROUTER_BASE_URL to point to an external 9Router."
  fi
fi

export PORT="$APP_PORT"
exec uvicorn api.main:app --host 0.0.0.0 --port "$APP_PORT"
