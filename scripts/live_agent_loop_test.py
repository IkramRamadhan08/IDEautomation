from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api import main as main_mod
from api.app_state import CURRENT_SESSION_ID, CURRENT_USER_ID, STATE


def main() -> None:
    session_id = "live-codex-loop-test"
    user_id = "live-codex-loop-user"
    root = Path(".tmp-agent-live").resolve()
    project = root / "loop-demo"
    if project.exists():
        shutil.rmtree(project)
    (project / "src").mkdir(parents=True, exist_ok=True)
    (project / "package.json").write_text(
        json.dumps(
            {
                "name": "loop-demo",
                "version": "0.1.0",
                "type": "module",
                "scripts": {"build": "vite build"},
                "dependencies": {
                    "@vitejs/plugin-react": "latest",
                    "vite": "latest",
                    "typescript": "latest",
                    "react": "latest",
                    "react-dom": "latest",
                },
                "devDependencies": {},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (project / "index.html").write_text(
        '<!doctype html><html><head><title>Loop Demo</title></head><body><div id="root"></div>'
        '<script type="module" src="/src/main.jsx"></script></body></html>\n',
        encoding="utf-8",
    )
    (project / "src" / "main.jsx").write_text(
        "import React from 'react';\n"
        "import { createRoot } from 'react-dom/client';\n"
        "import './styles.css';\n"
        "import App from './App.jsx';\n\n"
        "createRoot(document.getElementById('root')).render(<App />);\n",
        encoding="utf-8",
    )
    (project / "src" / "App.jsx").write_text(
        "export default function App() {\n"
        "  return <main><h1>Starter</h1><p>Make this useful.</p></main>;\n"
        "}\n",
        encoding="utf-8",
    )
    (project / "src" / "styles.css").write_text(
        "body { margin: 0; font-family: system-ui, sans-serif; }\n",
        encoding="utf-8",
    )

    STATE["sessions"][session_id] = {
        "workspace": str(root),
        "runners": {},
        "agent_jobs": {},
        "oauth_pending": {},
        "google_user": None,
        "hydrated_projects": set(),
    }
    session_token = CURRENT_SESSION_ID.set(session_id)
    user_token = CURRENT_USER_ID.set(user_id)
    started = time.time()
    events: list[dict] = []

    def emit(event: str, data: dict) -> None:
        payload = dict(data or {})
        if event not in {"status", "delta", "tool_call", "tool_output", "command_start", "command_output", "done", "error"}:
            return
        message = payload.get("message") or payload.get("text") or payload.get("summary")
        item = {
            "t": round(time.time() - started, 2),
            "event": event,
            "phase": payload.get("phase"),
            "message": message,
        }
        events.append({**item, "payload": payload})
        print(json.dumps(item, ensure_ascii=False), flush=True)

    try:
        req = main_mod.AgentReq(
            input=(
                "Bikin mini dashboard task tracker profesional untuk tim produk. "
                "Harus edit UI yang ada, tambahkan state visual task, prioritas, progress ring sederhana, "
                "dan jalankan build kalau perlu."
            ),
            project_root="loop-demo",
            build_mode="full-agent",
            active_file="src/App.jsx",
            open_files=["src/App.jsx", "src/styles.css"],
            auto_execute=True,
            stream=False,
        )
        result = main_mod._run_agent_impl(req, event_cb=emit)
        execution = result.get("execution") if isinstance(result.get("execution"), dict) else {}
        trace = result.get("trace") if isinstance(result.get("trace"), dict) else {}
        summary = {
            "ok": True,
            "spoken": result.get("spoken"),
            "log": result.get("log"),
            "changes": [item.get("path") for item in result.get("changes") or []],
            "actions": result.get("actions"),
            "execution_ok": execution.get("ok"),
            "execution_summary": (execution.get("completion_report") or {}).get("summary")
            if isinstance(execution.get("completion_report"), dict)
            else None,
            "task_state": trace.get("task_state"),
            "verification": trace.get("verification"),
            "warnings": trace.get("warnings"),
            "event_count": len(events),
            "phase_counts": {
                phase: sum(1 for item in events if item.get("phase") == phase)
                for phase in sorted({str(item.get("phase")) for item in events if item.get("phase")})
            },
            "visible_changes": {
                "App.jsx": (project / "src" / "App.jsx").read_text(encoding="utf-8")[:4000],
                "styles.css": (project / "src" / "styles.css").read_text(encoding="utf-8")[:4000],
            },
        }
        print("@@RESULT@@")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        CURRENT_USER_ID.reset(user_token)
        CURRENT_SESSION_ID.reset(session_token)


if __name__ == "__main__":
    main()
