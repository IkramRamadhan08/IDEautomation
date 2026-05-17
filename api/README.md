# Appora API

FastAPI API service for Appora:
- File tree / read / write
- Diff preview
- Agent planning via local OAuth-backed runtimes

## Run (dev)
```bash
cd ~/voice-ide/api
python -m venv .venv && source .venv/bin/activate
pip install -e .
cd ..
uvicorn api.main:app --reload --port 8787
```

Config is loaded from `~/voice-ide/.env` if present.

To avoid manually editing `.env`, use:
```bash
cd ~/voice-ide
./scripts/env.py wizard
```

(or copy from `.env.example`).

## 9Router
Appora is configured as a 9Router client. For local BYOK/dev, set:
```bash
NINE_ROUTER_BASE_URL=http://127.0.0.1:20128/v1
NINE_ROUTER_API_KEY=...
NINE_ROUTER_MODEL=free-forever
```

For hosted Appora Free Router, set these on the backend deployment:
```bash
APPORA_MANAGED_9ROUTER_BASE_URL=https://router.appora.ai/v1
APPORA_MANAGED_9ROUTER_API_KEY=...
APPORA_MANAGED_FREE_MODEL=free-forever
APPORA_FREE_DAILY_MESSAGES=30
```

When the managed variables are present, users can run `free-forever` without entering their own key. Premium/custom 9Router models still use the user's own 9Router endpoint and API key from Settings.
