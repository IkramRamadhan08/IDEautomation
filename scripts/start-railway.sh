#!/bin/sh
set -eu

APP_PORT="${PORT:-8000}"
NINE_ROUTER_PORT="${NINE_ROUTER_PORT:-20128}"

if [ "${APPORA_EMBED_9ROUTER:-true}" = "true" ]; then
  if command -v 9router >/dev/null 2>&1; then
    NINE_ROUTER_HOST="${NINE_ROUTER_HOSTNAME:-127.0.0.1}"
    export NEXT_PUBLIC_BASE_URL="${NINE_ROUTER_PUBLIC_BASE_URL:-http://${NINE_ROUTER_HOST}:${NINE_ROUTER_PORT}}"
    9router --host "$NINE_ROUTER_HOST" --port "$NINE_ROUTER_PORT" --no-browser --skip-update --log >/tmp/appora-9router.log 2>&1 &
    echo "Started embedded 9Router on ${NEXT_PUBLIC_BASE_URL}"
    sleep 5
    if command -v curl >/dev/null 2>&1; then
      if curl -fsS "${NEXT_PUBLIC_BASE_URL}/api/health" >/dev/null 2>&1; then
        echo "Embedded 9Router health check passed"
      else
        echo "Embedded 9Router health check failed; recent 9Router log follows"
        tail -80 /tmp/appora-9router.log || true
      fi
    fi
    if [ -n "${APPORA_9ROUTER_SEED_SQL_B64:-}" ] && command -v node >/dev/null 2>&1; then
      if node - <<'NODE'
const { DatabaseSync } = require("node:sqlite");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const encoded = process.env.APPORA_9ROUTER_SEED_SQL_B64 || "";
if (encoded) {
  const dbPath = path.join(os.homedir(), ".9router", "db", "data.sqlite");
  fs.mkdirSync(path.dirname(dbPath), { recursive: true });
  const sql = Buffer.from(encoded, "base64").toString("utf8");
  const db = new DatabaseSync(dbPath);
  db.exec(sql);
  db.close();
  console.log("Embedded 9Router seed applied");
}
NODE
      then
        true
      else
        echo "Embedded 9Router seed failed; continuing without seeded provider credentials"
      fi
    fi
  else
    echo "9Router binary not found; Appora will require NINE_ROUTER_BASE_URL to point to an external 9Router."
  fi
fi

export PORT="$APP_PORT"
exec uvicorn api.main:app --host 0.0.0.0 --port "$APP_PORT"
