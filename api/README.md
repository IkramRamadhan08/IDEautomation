# Voice IDE API

FastAPI API service for Voice IDE:
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
