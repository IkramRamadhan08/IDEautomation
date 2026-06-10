from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Literal
import hashlib
import io
import os
import shutil
import threading
import time
import json
import re
import shlex
import subprocess
import uuid
import zipfile
from html import escape, unescape
from types import SimpleNamespace
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request as URLRequest, urlopen

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from api.settings import ROOT, ENV_PATH, load_settings
from api.storage.supabase import (
    append_agent_job_event,
    create_agent_job,
    delete_project_file as supabase_delete_project_file,
    get_agent_job,
    get_agent_job_any,
    get_agent_memory_chunks_summary,
    get_agent_memory_chunks_table_status,
    has_supabase,
    list_agent_job_events,
    list_agent_jobs_by_status,
    list_project_files as supabase_list_project_files,
    list_projects as supabase_list_projects,
    update_agent_job,
    upsert_profile,
    upsert_project_files as supabase_upsert_project_files,
)
from api import settings as settings_mod
from api.app_state import CURRENT_SESSION_ID, CURRENT_USER_ID, STATE
from api.assets.router import build_assets_router
from api.auth.router import build_auth_router
from api.auth.identity import CURRENT_REQUEST_USER, resolve_request_user, sanitize_user_id
from api.command.router import AgentHarnessRunShellReq, build_command_router
from api.command.schemas import AgentHarnessShellAction, CommandPolicyDecision
from api.oauth_runtime import CURRENT_PROFILE_ID
from api.projects.router import build_projects_router
from api.preferences.router import build_preferences_router
from api.preferences.store import get_project_preferences
from api.config.router import build_settings_router
from api.diagnostics.router import build_diagnostics_router
from api.workspace.router import build_workspace_router
from api.workspace.schemas import IdentityInfo
from api.fs import list_tree, read_text, write_text, diff_text, safe_join
from api.agent_mcp import discover_mcp_servers, list_mcp_tools
from api.agent_memory import get_agent_memory_overview, remember_agent_run, sync_project_docs_to_supabase
from api.agent_observability import build_agent_observability
from api.agent_runtime import (
    APPORA_AUTO_SAFE_SHELL_COMMANDS,
    APPORA_BLOCKED_OR_APPROVAL_SHELL_COMMANDS,
    _remember_project_work_state,
    run_agent_pipeline,
)
from api.agent_skills import build_validation_plan, detect_project_stack
from api.agent_tools import list_local_tools


app = FastAPI(title="Appora Backend", version="0.1.0")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@app.exception_handler(ValueError)
async def value_error_handler(_request: Request, exc: ValueError):
    message = str(exc) or "Invalid request"
    status_code = 400 if "workspace" in message.lower() or "path" in message.lower() else 400
    return JSONResponse(status_code=status_code, content={"detail": message})

# Serialize LLM calls per provider to avoid provider-specific rate-limit bursts (429)
# without making unrelated providers block each other.
SCAFFOLD_LOCK = threading.Lock()
_AGENT_LOCKS: dict[str, threading.Lock] = {}
_AGENT_LOCKS_GUARD = threading.Lock()


def _agent_lock_for_current_provider() -> threading.Lock:
    provider = str(getattr(settings_mod.settings, "llm_provider", None) or "default").strip().lower() or "default"
    with _AGENT_LOCKS_GUARD:
        lock = _AGENT_LOCKS.get(provider)
        if lock is None:
            lock = threading.Lock()
            _AGENT_LOCKS[provider] = lock
        return lock


def _reload_settings():
    settings_mod.settings = load_settings()


def _is_serverless_runtime() -> bool:
    return bool(
        os.environ.get("VERCEL")
        or os.environ.get("VERCEL_ENV")
        or os.environ.get("RAILWAY_ENVIRONMENT")
        or os.environ.get("RAILWAY_PROJECT_ID")
        or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
        or os.environ.get("LAMBDA_TASK_ROOT")
    )


SENSITIVE_HOSTED_API_PREFIXES = (
    "/api/workspace",
    "/api/fs",
    "/api/terminal",
    "/api/run",
    "/api/agent",
    "/api/assets",
    "/api/projects/export",
    "/api/project/validate",
    "/api/preview/audit",
    "/api/supabase/rag",
)


def _requires_verified_hosted_user(path: str) -> bool:
    if not has_supabase():
        return False
    if not _is_serverless_runtime():
        return False
    if path in {"/api/healthz", "/api/auth/debug", "/api/settings", "/api/models", "/api/agent/worker/run"}:
        return False
    if path == "/api/run/proxy" or path.startswith("/api/run/proxy/"):
        return False
    return any(path == prefix or path.startswith(prefix + "/") for prefix in SENSITIVE_HOSTED_API_PREFIXES)


def _is_text_rel_path(rel: str) -> bool:
    suffix = PurePosixPath(rel).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".mp4", ".mov", ".zip"}:
        return False
    return True


def _hosted_project_files_enabled() -> bool:
    return _is_serverless_runtime() and has_supabase()


def _split_project_path(rel_path: str) -> tuple[str, str] | None:
    rel = str(PurePosixPath(str(rel_path or "").strip().lstrip("/")))
    if not rel or rel in {".", ".."}:
        return None
    parts = PurePosixPath(rel).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    project_root = parts[0]
    file_rel = str(PurePosixPath(*parts[1:])) if len(parts) > 1 else "README.md"
    return project_root, file_rel


def _persist_hosted_file(rel_path: str, content: str) -> None:
    if not _hosted_project_files_enabled():
        return
    split = _split_project_path(rel_path)
    if not split:
        return
    project_root, file_rel = split
    if not _is_text_rel_path(file_rel):
        return
    try:
        supabase_upsert_project_files(
            owner_id=CURRENT_USER_ID.get(),
            project_root=project_root,
            files=[{"path": file_rel, "content": content}],
        )
    except Exception:
        pass


def _delete_hosted_file(rel_path: str) -> None:
    if not _hosted_project_files_enabled():
        return
    split = _split_project_path(rel_path)
    if not split:
        return
    project_root, file_rel = split
    try:
        supabase_delete_project_file(
            owner_id=CURRENT_USER_ID.get(),
            project_root=project_root,
            path=file_rel,
        )
    except Exception:
        pass


HOSTED_SHELL_SYNC_EXCLUDED_DIRS = {
    ".git",
    ".next",
    ".nuxt",
    ".output",
    ".svelte-kit",
    ".turbo",
    ".venv",
    ".vercel",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "venv",
}

PROJECT_EXPORT_EXCLUDED_DIRS = HOSTED_SHELL_SYNC_EXCLUDED_DIRS | {
    ".voiceide",
    ".idea",
    ".vscode",
}
PROJECT_EXPORT_EXCLUDED_FILES = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
}
PROJECT_EXPORT_MAX_FILES = 2000
PROJECT_EXPORT_MAX_BYTES_PER_FILE = 8_000_000
PROJECT_EXPORT_MAX_TOTAL_BYTES = 80_000_000


def _safe_export_filename(project_root: str) -> str:
    name = str(project_root or "appora-project").strip().strip("/") or "appora-project"
    name = name.split("/")[-1] or "appora-project"
    name = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-._")
    return (name or "appora-project")[:80]


def _project_display_name(project_root: str) -> str:
    name = _safe_export_filename(project_root)
    name = re.sub(r"[-_]+", " ", name).strip()
    return name.title() if name else "Appora Project"


def _iter_project_export_files(project_dir: Path) -> list[Path]:
    files: list[Path] = []
    total_bytes = 0
    for path in sorted(project_dir.rglob("*")):
        if len(files) >= PROJECT_EXPORT_MAX_FILES:
            break
        if not path.is_file():
            continue
        try:
            rel = PurePosixPath(str(path.relative_to(project_dir)))
        except Exception:
            continue
        if any(part in PROJECT_EXPORT_EXCLUDED_DIRS for part in rel.parts):
            continue
        if rel.name in PROJECT_EXPORT_EXCLUDED_FILES or rel.name.startswith(".env."):
            continue
        try:
            size = path.stat().st_size
        except Exception:
            continue
        if size > PROJECT_EXPORT_MAX_BYTES_PER_FILE:
            continue
        if total_bytes + size > PROJECT_EXPORT_MAX_TOTAL_BYTES:
            break
        total_bytes += size
        files.append(path)
    return files


def _sync_hosted_project_text_files_after_shell(ws_root: Path, cwd: Path, max_files: int = 500, max_bytes: int = 400_000) -> int:
    if not _hosted_project_files_enabled():
        return 0
    try:
        rel_cwd = cwd.resolve().relative_to(ws_root.resolve())
    except Exception:
        return 0
    parts = rel_cwd.parts
    if not parts:
        return 0

    project_root = parts[0]
    project_dir = safe_join(ws_root, project_root)
    if not project_dir.exists() or not project_dir.is_dir():
        return 0

    files: list[dict[str, str]] = []
    for path in project_dir.rglob("*"):
        if len(files) >= max_files:
            break
        if not path.is_file():
            continue
        try:
            rel = PurePosixPath(str(path.relative_to(project_dir)))
        except Exception:
            continue
        if any(part in HOSTED_SHELL_SYNC_EXCLUDED_DIRS for part in rel.parts):
            continue
        rel_str = str(rel)
        if not _is_text_rel_path(rel_str):
            continue
        try:
            if path.stat().st_size > max_bytes:
                continue
            files.append({"path": rel_str, "content": path.read_text(encoding="utf-8")})
        except Exception:
            continue

    if not files:
        return 0
    try:
        supabase_upsert_project_files(owner_id=CURRENT_USER_ID.get(), project_root=project_root, files=files)
        return len(files)
    except Exception:
        return 0


def _hydrate_hosted_project(ws_root: Path, project_root: str) -> None:
    if not _hosted_project_files_enabled():
        return
    root = str(project_root or ".").strip().strip("/") or "."
    if root == ".":
        return
    session = _session_state()
    hydrated = session.setdefault("hydrated_projects", set())
    if root in hydrated:
        return
    try:
        rows = supabase_list_project_files(owner_id=CURRENT_USER_ID.get(), project_root=root) or []
    except Exception:
        return
    project_dir = safe_join(ws_root, root)
    project_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        if not isinstance(row, dict):
            continue
        rel = str(row.get("path") or "").strip().lstrip("/")
        content = row.get("content")
        if not rel or not isinstance(content, str):
            continue
        try:
            target = safe_join(project_dir, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except Exception:
            continue
    hydrated.add(root)


def _hydrate_hosted_projects(ws_root: Path) -> None:
    if not _hosted_project_files_enabled():
        return
    try:
        projects = supabase_list_projects(owner_id=CURRENT_USER_ID.get()) or []
    except Exception:
        return
    for project in projects[:50]:
        if not isinstance(project, dict):
            continue
        root = str(project.get("root") or project.get("slug") or "").strip()
        if root:
            _hydrate_hosted_project(ws_root, root)


def _bootstrap_missing_agent_project(ws_root: Path, project_root: str) -> dict[str, object] | None:
    root = str(project_root or ".").strip().strip("/") or "."
    if root == ".":
        return None
    project_dir = safe_join(ws_root, root)
    if project_dir.exists():
        return None

    package_name = re.sub(r"[^a-z0-9-]+", "-", root.lower()).strip("-") or "appora-project"
    files = {
        "package.json": json.dumps(
            {
                "name": package_name,
                "version": "0.1.0",
                "private": True,
                "type": "module",
                "apporaTemplate": True,
                "scripts": {
                    "dev": "vite",
                    "build": "tsc -b && vite build",
                    "preview": "vite preview",
                },
                "dependencies": {
                    "@vitejs/plugin-react": "latest",
                    "vite": "latest",
                    "typescript": "latest",
                    "react": "latest",
                    "react-dom": "latest",
                },
                "devDependencies": {},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        "index.html": (
            "<!doctype html>\n"
            "<html lang=\"en\">\n"
            "  <head>\n"
            "    <meta charset=\"UTF-8\" />\n"
            "    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />\n"
            "    <meta name=\"description\" content=\"Appora agent workspace waiting for a production build.\" />\n"
            "    <title>Appora Agent Workspace</title>\n"
            "  </head>\n"
            "  <body>\n"
            "    <div id=\"root\"></div>\n"
            "    <script type=\"module\" src=\"/src/main.tsx\"></script>\n"
            "  </body>\n"
            "</html>\n"
        ),
        "tsconfig.json": json.dumps(
            {
                "compilerOptions": {
                    "target": "ES2020",
                    "useDefineForClassFields": True,
                    "lib": ["DOM", "DOM.Iterable", "ES2020"],
                    "allowJs": False,
                    "skipLibCheck": True,
                    "esModuleInterop": True,
                    "allowSyntheticDefaultImports": True,
                    "strict": True,
                    "forceConsistentCasingInFileNames": True,
                    "module": "ESNext",
                    "moduleResolution": "Node",
                    "resolveJsonModule": True,
                    "isolatedModules": True,
                    "noEmit": True,
                    "jsx": "react-jsx",
                },
                "include": ["src"],
                "references": [],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        "tsconfig.node.json": json.dumps(
            {
                "compilerOptions": {
                    "composite": True,
                    "skipLibCheck": True,
                    "module": "ESNext",
                    "moduleResolution": "Node",
                    "allowSyntheticDefaultImports": True,
                },
                "include": ["vite.config.ts"],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        "vite.config.ts": "import { defineConfig } from 'vite';\nimport react from '@vitejs/plugin-react';\n\nexport default defineConfig({ plugins: [react()] });\n",
        "src/main.tsx": (
            "import React from 'react';\n"
            "import { createRoot } from 'react-dom/client';\n"
            "import './styles.css';\n"
            "import App from './App';\n\n"
            "createRoot(document.getElementById('root')!).render(\n"
            "  <React.StrictMode>\n"
            "    <App />\n"
            "  </React.StrictMode>,\n"
            ");\n"
        ),
        "src/App.tsx": (
            "export default function App() {\n"
            "  return (\n"
            "    <main className=\"workspaceShell\">\n"
            "      <p className=\"eyebrow\">Appora workspace</p>\n"
            "      <h1>Ready for a production build</h1>\n"
            "      <p>Replace this bootstrap shell with the requested product experience.</p>\n"
            "    </main>\n"
            "  );\n"
            "}\n"
        ),
        "src/styles.css": (
            ":root { color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }\n"
            "* { box-sizing: border-box; }\n"
            "body { margin: 0; min-width: 320px; min-height: 100vh; background: #f6f4ef; color: #161616; }\n"
            ".workspaceShell { min-height: 100vh; display: grid; place-content: center; gap: 12px; padding: 32px; }\n"
            ".eyebrow { margin: 0; text-transform: uppercase; letter-spacing: .08em; font-size: 12px; color: #6f5b35; }\n"
            "h1 { margin: 0; max-width: 760px; font-size: clamp(40px, 8vw, 80px); line-height: .95; }\n"
            "p { max-width: 640px; font-size: 18px; line-height: 1.6; }\n"
        ),
    }
    for rel, content in files.items():
        write_text(ws_root, f"{root}/{rel}", content)

    package_data = _read_json(project_dir / "package.json") or {}
    dependency_logs: list[str] = []
    _ensure_preview_dependencies_ready(project_dir, package_data, dependency_logs)
    return {
        "project_root": root,
        "created": True,
        "paths": [f"{root}/{path}" for path in files],
        "dependency_logs": dependency_logs,
    }


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


_KNOWN_PACKAGE_MANAGERS = ("npm", "pnpm", "yarn", "bun")


def _package_manager_preference_order(project_dir: Path) -> list[str]:
    preferred: list[str] = []
    package_json = _read_json(project_dir / "package.json") or {}
    package_manager = str(package_json.get("packageManager") or "").strip().lower()
    if package_manager:
        name = package_manager.split("@", 1)[0].strip()
        if name in _KNOWN_PACKAGE_MANAGERS:
            preferred.append(name)

    if (project_dir / "pnpm-lock.yaml").exists():
        preferred.append("pnpm")
    if (project_dir / "yarn.lock").exists():
        preferred.append("yarn")
    if (project_dir / "bun.lockb").exists() or (project_dir / "bun.lock").exists():
        preferred.append("bun")
    if (project_dir / "package-lock.json").exists():
        preferred.append("npm")

    preferred.extend(_KNOWN_PACKAGE_MANAGERS)

    ordered: list[str] = []
    seen: set[str] = set()
    for name in preferred:
        if name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    return ordered


def _resolve_package_manager(project_dir: Path) -> tuple[str, list[str]] | None:
    corepack = shutil.which("corepack")
    for name in _package_manager_preference_order(project_dir):
        if shutil.which(name):
            return name, [name]
        if name in {"pnpm", "yarn"} and corepack:
            return name, ["corepack", name]
    return None


def _shell_join(parts: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def _translate_package_manager_command(command: str, project_dir: Path) -> tuple[str | None, str | None]:
    stripped = (command or "").strip()
    if not stripped.startswith("npm "):
        return command, None

    resolved = _resolve_package_manager(project_dir)
    if not resolved:
        return None, (
            "JavaScript tooling is not available in this runtime. npm/pnpm/yarn/bun were not found. "
            "Install Node.js on the API host, or run this project from a local/desktop deployment."
        )

    manager_name, manager_cmd = resolved
    note = None if manager_name == "npm" else f"Using {manager_name} for this project because npm is not available."

    install_match = re.match(r"^\s*npm\s+install(?P<rest>.*)$", stripped)
    if install_match:
        return f"{_shell_join([*manager_cmd, 'install'])}{install_match.group('rest') or ''}", note

    run_match = re.match(r"^\s*npm\s+run\s+(?P<script>[^\s]+)(?P<rest>.*)$", stripped)
    if run_match:
        script = run_match.group("script")
        rest = run_match.group("rest") or ""
        return f"{_shell_join([*manager_cmd, 'run', script])}{rest}", note

    if manager_name == "npm":
        return command, None

    return None, (
        f"This runtime only knows how to translate basic npm install/run commands automatically. "
        f"Unsupported command: {command}"
    )


def _package_install_command(manager_name: str, manager_cmd: list[str]) -> list[str]:
    executable = manager_cmd[0] if manager_cmd else manager_name
    if manager_name == "npm":
        return [executable, "install"]
    if manager_name == "pnpm":
        return [*manager_cmd, "install"]
    if manager_name == "yarn":
        return [*manager_cmd, "install"]
    if manager_name == "bun":
        return [*manager_cmd, "install"]
    return [*manager_cmd, "install"]


def _package_run_script_command(manager_cmd: list[str], script: str, port: int) -> list[str]:
    return [*manager_cmd, "run", script, "--", "--host", "127.0.0.1", "--strictPort", "--port", str(port)]


def _declared_node_packages(package_data: dict) -> set[str]:
    names: set[str] = set()
    for key in ["dependencies", "devDependencies"]:
        deps = package_data.get(key)
        if isinstance(deps, dict):
            names.update(str(name).strip() for name in deps if str(name).strip())
    return names


def _preview_launch_kind(package_data: dict) -> tuple[str, str | None]:
    scripts = package_data.get("scripts") or {}
    if isinstance(scripts, dict) and "dev" in scripts:
        return "script", "dev"
    if isinstance(scripts, dict) and "preview" in scripts:
        return "script", "preview"
    if "vite" in _declared_node_packages(package_data):
        return "vite", None
    return "static", None


def _package_vite_command(project_dir: Path, manager_name: str, manager_cmd: list[str], port: int) -> list[str]:
    local_vite = project_dir / "node_modules" / ".bin" / "vite"
    args = ["--host", "127.0.0.1", "--strictPort", "--port", str(port)]
    if local_vite.exists():
        return [str(local_vite), *args]
    if manager_name == "pnpm":
        return [*manager_cmd, "exec", "vite", *args]
    if manager_name == "yarn":
        return [*manager_cmd, "vite", *args]
    if manager_name == "bun":
        return [*manager_cmd, "x", "vite", *args]
    return [*manager_cmd, "exec", "vite", "--", *args]


def _node_modules_contains(node_modules: Path, package_name: str) -> bool:
    if not package_name:
        return True
    parts = package_name.split("/")
    return (node_modules.joinpath(*parts) / "package.json").exists()


def _appora_template_can_reuse_root_node_modules(package_data: dict) -> bool:
    return package_data.get("apporaTemplate") is True


def _ensure_preview_dependencies_ready(project_dir: Path, package_data: dict, logs: list[str]) -> bool:
    declared = _declared_node_packages(package_data)
    if not declared:
        return True

    project_node_modules = project_dir / "node_modules"
    if project_node_modules.exists() and all(_node_modules_contains(project_node_modules, name) for name in declared):
        logs.append("[runtime] Existing node_modules satisfies declared dependencies; skipping install.")
        return True

    root_node_modules = ROOT / "node_modules"
    if (
        not project_node_modules.exists()
        and _appora_template_can_reuse_root_node_modules(package_data)
        and root_node_modules.exists()
        and all(_node_modules_contains(root_node_modules, name) for name in declared)
    ):
        try:
            project_node_modules.symlink_to(root_node_modules, target_is_directory=True)
            logs.append("[runtime] Reused Appora local node_modules for this starter template; skipping install.")
            return True
        except Exception as exc:
            logs.append(f"[runtime] Could not reuse Appora node_modules ({exc}); falling back to package install.")

    return False


def _preview_install_timeout_seconds() -> int:
    raw = os.getenv("APPORA_PREVIEW_INSTALL_TIMEOUT_SECONDS", "120").strip()
    try:
        return max(10, min(int(raw), 600))
    except Exception:
        return 120


def _spoken_stream_chunks(text: str, *, max_chars: int = 28) -> list[str]:
    clean = " ".join(str(text or "").split())
    if not clean:
        return []

    words = clean.split(" ")
    chunks: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = word
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _local_agent_jobs() -> dict:
    return _session_state().setdefault("agent_jobs", {})


def _best_effort_background(fn, *args, **kwargs) -> None:
    def worker():
        try:
            fn(*args, **kwargs)
        except Exception:
            pass

    threading.Thread(target=worker, daemon=True).start()


def _create_agent_job_record(req: "AgentReq") -> str:
    job_id = uuid.uuid4().hex
    owner_id = CURRENT_USER_ID.get()
    project_root = (getattr(req, "project_root", None) or ".").strip() or "."
    build_mode = getattr(req, "build_mode", None)
    local_jobs = _local_agent_jobs()
    local_jobs[job_id] = {
        "id": job_id,
        "owner_id": owner_id,
        "project_root": project_root,
        "build_mode": build_mode,
        "status": "queued",
        "input": str(getattr(req, "input", "") or "")[:20_000],
        "request_payload": _agent_req_payload(req),
        "result": None,
        "error": None,
        "events": [],
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }
    _best_effort_background(
        create_agent_job,
        owner_id=owner_id,
        job_id=job_id,
        project_root=project_root,
        build_mode=build_mode,
        input_text=str(getattr(req, "input", "") or ""),
        request_payload=_agent_req_payload(req),
    )
    return job_id


def _agent_req_payload(req: "AgentReq") -> dict:
    data = req.model_dump(mode="json")
    data["stream"] = False
    data["background"] = False
    return data


def _agent_req_from_job(job: dict) -> "AgentReq":
    payload = job.get("request_payload")
    if not isinstance(payload, dict) or not payload:
        payload = {
            "input": str(job.get("input") or ""),
            "project_root": str(job.get("project_root") or "."),
            "build_mode": job.get("build_mode"),
        }
    payload = dict(payload)
    payload["stream"] = False
    payload["background"] = False
    return AgentReq.model_validate(payload)


def _record_agent_job_event(job_id: str | None, event: str, data: dict) -> None:
    if not job_id:
        return
    payload = dict(data or {})
    payload.setdefault("job_id", job_id)
    owner_id = CURRENT_USER_ID.get()
    local_jobs = _local_agent_jobs()
    job = local_jobs.get(job_id)
    if isinstance(job, dict):
        events = job.setdefault("events", [])
        events.append({"id": len(events) + 1, "event_type": event, "payload": payload, "created_at": int(time.time())})
        job["updated_at"] = int(time.time())
        if event == "status" and payload.get("phase") == "starting":
            job["status"] = "running"
        elif event == "done":
            job["status"] = "completed"
            job["result"] = payload.get("result")
        elif event == "error":
            job["status"] = "failed"
            job["error"] = str(payload.get("message") or "")[:4000]
    _best_effort_background(append_agent_job_event, owner_id=owner_id, job_id=job_id, event_type=event, payload=payload)


def _update_agent_job_record(job_id: str | None, status: str, *, result: dict | None = None, error: str | None = None) -> None:
    if not job_id:
        return
    owner_id = CURRENT_USER_ID.get()
    job = _local_agent_jobs().get(job_id)
    if isinstance(job, dict):
        job["status"] = status
        job["updated_at"] = int(time.time())
        if result is not None:
            job["result"] = result
        if error is not None:
            job["error"] = error
    _best_effort_background(update_agent_job, owner_id=owner_id, job_id=job_id, status=status, result=result, error=error)

def _cors_origins() -> list[str]:
    defaults = [
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:5175",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
        "http://127.0.0.1:5175",
        "http://localhost:8788",
        "https://voice-ide-rho.vercel.app",
        "https://voice-ide-ikramramadhan08s-projects.vercel.app",
    ]
    extra = os.getenv("APPORA_CORS_ORIGINS") or os.getenv("VOICEIDE_CORS_ORIGINS") or ""
    for origin in extra.split(","):
        value = origin.strip().rstrip("/")
        if value and value not in defaults:
            defaults.append(value)
    return defaults


# Local app: allow frontend dev server; hosted app: allow configured frontend origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?$|https://.*\.vercel\.app$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _sanitize_session_id(raw: str | None) -> str:
    value = (raw or "").strip()
    if not value:
        return "voiceide-default"
    safe = "".join(ch for ch in value if ch.isalnum() or ch in {"-", "_", ".", ":"})
    return safe[:120] or "voiceide-default"


def _users_state_path() -> Path:
    return ROOT / ".voiceide-users.json"


def _load_users_state() -> dict:
    return _read_json(_users_state_path()) or {}


def _save_users_state(data: dict) -> None:
    _users_state_path().write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _current_user_profile() -> dict | None:
    users = _load_users_state().get("users") or {}
    profile = users.get(CURRENT_USER_ID.get())
    return profile if isinstance(profile, dict) else None


def _upsert_current_user_profile(*, display_name: str | None, email: str | None) -> dict:
    state = _load_users_state()
    users = state.get("users") if isinstance(state.get("users"), dict) else {}
    user_id = CURRENT_USER_ID.get()
    existing = users.get(user_id) if isinstance(users.get(user_id), dict) else {}
    profile = {
        "user_id": user_id,
        "display_name": (display_name or "").strip() or None,
        "email": (email or "").strip() or None,
        "updated_at": int(time.time()),
    }
    if existing.get("created_at"):
        profile["created_at"] = existing.get("created_at")
    else:
        profile["created_at"] = int(time.time())
    users[user_id] = profile
    state["users"] = users
    _save_users_state(state)
    try:
        upsert_profile(user_id=user_id, display_name=profile.get("display_name"), email=profile.get("email"))
    except Exception:
        pass
    return profile


def _session_state() -> dict:
    sid = CURRENT_SESSION_ID.get()
    sessions = STATE["sessions"]
    if sid not in sessions:
        sessions[sid] = {
            "workspace": None,
            "hydrated_projects": set(),
            "runners": {},
            "agent_jobs": {},
            "oauth_pending": {},
            "google_user": None,
        }
    return sessions[sid]


class VoiceIDESessionMiddleware:
    def __init__(self, inner_app):
        self.app = inner_app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or str(scope.get("method") or "").upper() == "OPTIONS":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin1").lower(): value.decode("latin1")
            for key, value in scope.get("headers", [])
        }
        session_token = CURRENT_SESSION_ID.set(_sanitize_session_id(headers.get("x-appora-session") or headers.get("x-voiceide-session")))
        resolved_user = resolve_request_user(
            authorization=headers.get("authorization"),
            x_voiceide_user=headers.get("x-appora-user") or headers.get("x-voiceide-user"),
        )
        user_token = CURRENT_USER_ID.set(resolved_user.user_id)
        profile_token = CURRENT_PROFILE_ID.set(resolved_user.user_id)
        request_user_token = CURRENT_REQUEST_USER.set(resolved_user)
        try:
            if _requires_verified_hosted_user(str(scope.get("path") or "")) and resolved_user.auth_source != "supabase":
                response = JSONResponse(
                    status_code=401,
                    content={
                        "detail": (
                            "Hosted agent/workspace routes require verified login. "
                            "Sign in so the frontend can send a Supabase bearer token."
                        )
                    },
                )
                await response(scope, receive, send)
                return

            session = _session_state()
            google_user = session.get("google_user") or {}
            if resolved_user.auth_source != "supabase":
                sub = str(google_user.get("sub") or "").strip()
                email = str(google_user.get("email") or "").strip().lower()
                if sub:
                    CURRENT_USER_ID.set(sanitize_user_id(f"google-{sub}"))
                elif email:
                    CURRENT_USER_ID.set(sanitize_user_id(f"google-{email}"))

            async def send_with_auth_header(message):
                if message.get("type") == "http.response.start":
                    raw_headers = list(message.get("headers") or [])
                    raw_headers.append((b"x-appora-auth-source", resolved_user.auth_source.encode("latin1")))
                    message = {**message, "headers": raw_headers}
                await send(message)

            await self.app(scope, receive, send_with_auth_header)
        finally:
            CURRENT_REQUEST_USER.reset(request_user_token)
            CURRENT_PROFILE_ID.reset(profile_token)
            CURRENT_USER_ID.reset(user_token)
            CURRENT_SESSION_ID.reset(session_token)


app.add_middleware(VoiceIDESessionMiddleware)


DANGEROUS_COMMAND_FRAGMENTS = ["rm -rf /", "mkfs", "dd if="]
VALIDATION_SCRIPT_NAMES = ("lint", "build", "typecheck", "check")
PREVIEW_AUDIT_HOSTS = {"localhost", "127.0.0.1", "::1"}
PREVIEW_BROWSER_AUDIT_TIMEOUT_SECONDS = 18
PREVIEW_BROWSER_AUDIT_SETTLE_MS = 600


def _resolve_node_binary() -> str | None:
    return shutil.which("node")


def _resolve_agent_browser_binary() -> str | None:
    return shutil.which("agent-browser")


def _playwright_audit_script() -> Path:
    return ROOT / "scripts" / "preview-audit.mjs"


def _project_uses_playwright(project_dir: Path) -> bool:
    package_json = _read_json(project_dir / "package.json") or {}
    for bucket in ("dependencies", "devDependencies"):
        deps = package_json.get(bucket)
        if not isinstance(deps, dict):
            continue
        names = {str(name).strip() for name in deps.keys()}
        if "playwright" in names or "@playwright/test" in names:
            return True
    return False


def _playwright_preview_audit_ready(project_dir: Path) -> bool:
    browser_root = ROOT / "node_modules" / "playwright"
    firefox_binary = Path.home() / ".cache" / "ms-playwright" / "firefox-1511" / "firefox" / "firefox"
    return bool(
        _resolve_node_binary()
        and _playwright_audit_script().exists()
        and browser_root.exists()
        and firefox_binary.exists()
    )


def _browser_preview_audit_ready(project_dir: Path) -> bool:
    return bool(_resolve_agent_browser_binary() or _playwright_preview_audit_ready(project_dir))


def _browser_preview_audit_backend(project_dir: Path) -> str:
    if _resolve_agent_browser_binary():
        return "agent-browser"
    if _playwright_preview_audit_ready(project_dir):
        return "playwright"
    return "html"


def _normalize_preview_url(preview_url: str) -> str:
    parsed = urlsplit((preview_url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(400, "preview_url must be http(s)")
    if (parsed.hostname or "").lower() not in PREVIEW_AUDIT_HOSTS:
        raise HTTPException(400, "preview audit only supports private preview URLs")
    return urlunsplit(parsed)


def _clean_html_text(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _count_emoji_chars(text: str) -> int:
    count = 0
    ui_symbols = {"✓", "✔", "✕", "✖", "×"}
    for char in str(text or ""):
        if char in ui_symbols:
            continue
        code = ord(char)
        if (
            0x1F300 <= code <= 0x1FAFF
            or 0x2600 <= code <= 0x27BF
            or 0x2300 <= code <= 0x23FF
        ):
            count += 1
    return count


_STARTER_RESIDUE_RE = re.compile(
    r"\b(vite\s*\+\s*react|react\s*\+\s*vite|click on the vite and react logos|vite logo|edit src/app|seeded template|lorem ipsum|template starter|starter|placeholder\s+(?:copy|content|text|section|card|page))\b",
    re.IGNORECASE,
)
_GENERIC_SAAS_COPY_RE = re.compile(
    r"\b(streamline|seamless|reimagined|next[- ]generation|supercharge|unlock|scale faster|all[- ]in[- ]one|boost productivity|transform your workflow)\b",
    re.IGNORECASE,
)


def _starter_residue_terms(text: str, *, limit: int = 8) -> list[str]:
    terms: list[str] = []
    for match in _STARTER_RESIDUE_RE.finditer(str(text or "")):
        term = match.group(0)
        if term.lower() in {item.lower() for item in terms}:
            continue
        terms.append(term)
        if len(terms) >= limit:
            break
    return terms


def _generic_copy_terms(text: str, *, limit: int = 8) -> list[str]:
    terms: list[str] = []
    for match in _GENERIC_SAAS_COPY_RE.finditer(str(text or "")):
        term = match.group(0)
        if term.lower() in {item.lower() for item in terms}:
            continue
        terms.append(term)
        if len(terms) >= limit:
            break
    return terms


def _extract_text_matches(pattern: str, html: str, limit: int) -> list[str]:
    values: list[str] = []
    for match in re.findall(pattern, html, flags=re.IGNORECASE | re.DOTALL):
        text = _clean_html_text(match)
        if text:
            values.append(text[:160])
        if len(values) >= limit:
            break
    return values


def _clean_jsx_text(fragment: str) -> str:
    text = re.sub(r"\{[^{}]{0,240}\}", " ", fragment or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"['\"`]\s*[,+]\s*['\"`]", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _extract_jsx_text_matches(pattern: str, source: str, limit: int) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for match in re.findall(pattern, source or "", flags=re.IGNORECASE | re.DOTALL):
        text = _clean_jsx_text(match)
        if not text or len(text) < 2:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        values.append(text[:160])
        if len(values) >= limit:
            break
    return values


def _extract_preview_snapshot_from_source(project_dir: Path, max_excerpt_chars: int = 800) -> dict:
    source_files: list[Path] = []
    for rel in ("src/App.tsx", "src/App.jsx", "src/main.tsx", "src/main.jsx", "app/page.tsx", "pages/index.tsx"):
        path = project_dir / rel
        if path.exists() and path.is_file():
            source_files.append(path)
    src_dir = project_dir / "src"
    if src_dir.exists() and src_dir.is_dir():
        for pattern in ("**/*.tsx", "**/*.jsx", "**/*.ts", "**/*.js"):
            for path in src_dir.glob(pattern):
                if path.is_file() and path not in source_files:
                    source_files.append(path)
                if len(source_files) >= 12:
                    break
            if len(source_files) >= 12:
                break

    title = ""
    meta_description = ""
    index_path = project_dir / "index.html"
    if index_path.exists() and index_path.is_file():
        try:
            html = index_path.read_text(encoding="utf-8", errors="ignore")[:24000]
            title_match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
            if title_match:
                title = _clean_html_text(title_match.group(1))
            meta_match = re.search(r"<meta[^>]+name=['\"]description['\"][^>]+content=['\"](.*?)['\"]", html, flags=re.IGNORECASE | re.DOTALL)
            if meta_match:
                meta_description = _clean_html_text(meta_match.group(1))
        except Exception:
            pass

    combined = ""
    for path in source_files[:12]:
        try:
            combined += "\n" + path.read_text(encoding="utf-8", errors="ignore")[:32000]
        except Exception:
            continue

    headings = _extract_jsx_text_matches(r"<h1\b[^>]*>(.*?)</h1>", combined, 3)
    subheadings = _extract_jsx_text_matches(r"<h2\b[^>]*>(.*?)</h2>", combined, 4)
    buttons = _extract_jsx_text_matches(r"<button\b[^>]*>(.*?)</button>", combined, 8)
    links = _extract_jsx_text_matches(r"<a\b[^>]*>(.*?)</a>", combined, 8)
    paragraphs = _extract_jsx_text_matches(r"<(?:p|span|strong|li)\b[^>]*>(.*?)</(?:p|span|strong|li)>", combined, 24)
    excerpt = " ".join([*headings, *subheadings, *paragraphs])[:max_excerpt_chars]
    text_blob = " ".join([title, meta_description, excerpt, " ".join(buttons), " ".join(links)])
    input_count = len(re.findall(r"<(?:input|textarea|select)\b", combined, flags=re.IGNORECASE))
    labeled_input_count = len(re.findall(r"<label\b|aria-label\s*=|aria-labelledby\s*=", combined, flags=re.IGNORECASE))
    class_matches = re.findall(r"className\s*=\s*(?:['\"]([^'\"]+)['\"]|\{`([^`]+)`\})", combined, flags=re.IGNORECASE | re.DOTALL)
    class_text = " ".join(" ".join(part for part in match if part) if isinstance(match, tuple) else str(match) for match in class_matches)
    return {
        "title": title,
        "meta_description": meta_description,
        "headings": headings,
        "subheadings": subheadings,
        "buttons": buttons,
        "links": links,
        "excerpt": excerpt,
        "viewport_meta": bool(re.search(r"<meta[^>]+name=['\"]viewport['\"]", (index_path.read_text(encoding="utf-8", errors="ignore") if index_path.exists() else ""), re.IGNORECASE | re.DOTALL)),
        "document_lang": "source",
        "form_count": len(re.findall(r"<form\b", combined, flags=re.IGNORECASE)),
        "section_count": len(re.findall(r"<(?:section|article)\b", combined, flags=re.IGNORECASE)),
        "card_like_count": len(re.findall(r"<article\b|\b(card|panel|tile)\b", combined + " " + class_text, flags=re.IGNORECASE)),
        "product_surface_count": len(re.findall(r"\b(dashboard|kanban|metric|table|queue|timeline|workflow|panel|card|chart|status|issue|risk)\b", combined, flags=re.IGNORECASE)),
        "table_count": len(re.findall(r"<table\b|role=['\"]table['\"]", combined, flags=re.IGNORECASE)),
        "input_count": input_count,
        "labeled_input_count": labeled_input_count,
        "landmark_count": len(re.findall(r"<(?:main|nav|header|footer|aside)\b", combined, flags=re.IGNORECASE)),
        "main_count": len(re.findall(r"<main\b", combined, flags=re.IGNORECASE)),
        "button_count": len(re.findall(r"<button\b", combined, flags=re.IGNORECASE)),
        "interactive_count": len(re.findall(r"<(?:button|a|input|textarea|select|summary)\b", combined, flags=re.IGNORECASE)),
        "word_count": len(re.findall(r"\b[\w'-]{2,}\b", text_blob)),
        "image_count": len(re.findall(r"<img\b", combined, flags=re.IGNORECASE)),
        "images_missing_alt": 0,
        "source_snapshot": True,
    }


def _merge_preview_snapshots(primary: dict, fallback: dict) -> dict:
    merged = dict(primary or {})
    if not fallback:
        return merged
    for key in ("title", "meta_description", "document_lang", "excerpt"):
        if not str(merged.get(key) or "").strip() and str(fallback.get(key) or "").strip():
            merged[key] = fallback.get(key)
    for key in ("headings", "subheadings", "buttons", "links"):
        current = [str(item).strip() for item in (merged.get(key) or []) if str(item).strip()]
        extra = [str(item).strip() for item in (fallback.get(key) or []) if str(item).strip()]
        seen = {item.lower() for item in current}
        for item in extra:
            if item.lower() not in seen:
                current.append(item)
                seen.add(item.lower())
        if current:
            merged[key] = current[:8]
    for key in (
        "form_count",
        "section_count",
        "card_like_count",
        "product_surface_count",
        "table_count",
        "input_count",
        "labeled_input_count",
        "landmark_count",
        "main_count",
        "button_count",
        "interactive_count",
        "word_count",
        "image_count",
    ):
        merged[key] = max(max(0, int(merged.get(key) or 0)), max(0, int(fallback.get(key) or 0)))
    if not merged.get("viewport_meta") and fallback.get("viewport_meta"):
        merged["viewport_meta"] = True
    if not merged.get("document_lang") and fallback.get("document_lang"):
        merged["document_lang"] = fallback.get("document_lang")
    if fallback.get("source_snapshot"):
        merged["source_snapshot"] = True
    return merged


def _scan_project_quality_signals(project_dir: Path) -> dict[str, object]:
    patterns = {
        "responsive": [r"@media\b", r"\bsm:", r"\bmd:", r"\blg:", r"clamp\(", r"minmax\(", r"grid-template", r"useMediaQuery", r"matchMedia\("],
        "loading": [r"\bloading\b", r"isLoading", r"pending", r"skeleton", r"spinner"],
        "error": [r"\berror\b", r"failed", r"retry", r"try again", r"catch \("],
        "empty": [r"empty state", r"no results", r"no items", r"not found", r"belum ada", r"empty"],
        "labels": [r"<label\b", r"htmlFor=", r"aria-label=", r"aria-labelledby="],
        "dynamic_state_required": [
            r"\bfetch\s*\(",
            r"\baxios\.",
            r"\buseEffect\s*\(",
            r"\buseQuery\s*\(",
            r"\buseSWR\s*\(",
            r"\bsupabase\.",
            r"\bprisma\.",
            r"\b<form\b",
            r"\b<input\b",
            r"\b<textarea\b",
            r"\b<select\b",
            r"\bsearch\b",
            r"\bfilter\b",
            r"\btable\b",
            r"\bdashboard\b",
            r"\bkanban\b",
            r"\bcheckout\b",
            r"\blogin\b",
        ],
    }
    hits = {key: False for key in patterns}
    metrics: dict[str, int] = {
        "inline_style_count": 0,
        "any_cast_count": 0,
        "emoji_count": 0,
        "starter_residue_count": 0,
        "layout_overflow_risk_count": 0,
        "generic_copy_count": 0,
    }
    layout_overflow_risks: list[str] = []
    candidates: list[Path] = []
    for rel in ("index.html",):
        path = project_dir / rel
        if path.exists() and path.is_file():
            candidates.append(path)
    src_dir = project_dir / "src"
    if src_dir.exists() and src_dir.is_dir():
        for pattern in ("**/*.tsx", "**/*.ts", "**/*.jsx", "**/*.js", "**/*.css", "**/*.html"):
            for path in src_dir.glob(pattern):
                if path.is_file():
                    candidates.append(path)
    for path in candidates[:80]:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")[:24000]
        except Exception:
            continue
        for key, variants in patterns.items():
            if hits[key]:
                continue
            if any(re.search(variant, text, flags=re.IGNORECASE) for variant in variants):
                hits[key] = True
        metrics["inline_style_count"] += len(re.findall(r"\bstyle=\{\{", text))
        metrics["any_cast_count"] += len(re.findall(r"\bas\s+any\b|:\s*any\b", text))
        metrics["emoji_count"] += _count_emoji_chars(text)
        metrics["starter_residue_count"] += len(_starter_residue_terms(text, limit=20))
        metrics["generic_copy_count"] += len(_generic_copy_terms(text, limit=20))
        for match in re.finditer(r"(?<![-\w])(?:min-)?width\s*:\s*(\d{3,4})px|(?<![-\w])width\s*:\s*100vw\b|(?<![-\w])(?:min-)?width\s*:\s*(?:max-content|fit-content)\b|\bwhite-space\s*:\s*nowrap\b", text, re.IGNORECASE):
            width = match.group(1)
            if width:
                try:
                    if int(width) < 390:
                        continue
                except ValueError:
                    continue
            metrics["layout_overflow_risk_count"] += 1
            if len(layout_overflow_risks) < 8:
                line = text.count("\n", 0, max(0, match.start())) + 1
                snippet = next((item.strip() for item in text.splitlines()[max(0, line - 1):line] if item.strip()), "")
                layout_overflow_risks.append(f"{path.relative_to(project_dir).as_posix()}:{line}: {snippet[:140]}")
        if all(hits.values()) and all(value > 0 for value in metrics.values()):
            break
    repair_candidate_files = [
        path.relative_to(project_dir).as_posix()
        for path in candidates[:20]
        if path.suffix.lower() in {".tsx", ".jsx", ".ts", ".js", ".css", ".html"}
    ]
    return {**hits, **metrics, "layout_overflow_risks": layout_overflow_risks, "repair_candidate_files": repair_candidate_files}


def _preview_dynamic_state_required(snapshot: dict, rendered_text: str, project_signals: dict[str, object] | None = None) -> bool:
    signals = project_signals or {}
    if bool(signals.get("dynamic_state_required")):
        return True
    if any(bool(signals.get(name)) for name in ("loading", "error", "empty")):
        return True
    form_count = max(0, int(snapshot.get("form_count") or 0))
    input_count = max(0, int(snapshot.get("input_count") or 0))
    table_count = max(0, int(snapshot.get("table_count") or 0))
    if form_count > 0 or input_count > 0 or table_count > 0:
        return True
    text = " ".join(
        [
            str(snapshot.get("title") or ""),
            " ".join(str(item) for item in (snapshot.get("headings") or [])),
            " ".join(str(item) for item in (snapshot.get("subheadings") or [])),
            " ".join(str(item) for item in (snapshot.get("buttons") or [])),
            " ".join(str(item) for item in (snapshot.get("links") or [])),
        ]
    ).lower()
    if any(token in text for token in [
        "dashboard",
        "kanban",
        "task",
        "cart",
        "checkout",
        "search",
        "filter",
        "upload",
        "login",
        "sign in",
        "sign up",
        "booking",
        "order",
        "invoice",
        "analytics",
        "admin",
    ]):
        return True
    buttons = [str(item).strip().lower() for item in (snapshot.get("buttons") or []) if str(item).strip()]
    passive = ("configure", "disabled", "lihat", "view", "see", "bahas", "contact", "kontak")
    action_buttons = [label for label in buttons if not any(token in label for token in passive)]
    return len(action_buttons) >= 2


def _dense_app_surface_ok(*, interactive_count: int, card_like_count: int, product_surface_count: int, table_count: int, word_count: int) -> bool:
    if interactive_count >= 8 and word_count >= 80 and table_count >= 1 and card_like_count >= 4:
        return True
    if interactive_count >= 8 and word_count >= 100 and card_like_count >= 6:
        return True
    if interactive_count >= 10 and word_count >= 70 and product_surface_count >= 2 and card_like_count >= 4:
        return True
    return interactive_count >= 12 and word_count >= 100 and product_surface_count >= 2 and card_like_count >= 4


def _filter_visual_text_overflow_nodes(nodes: list[str]) -> list[str]:
    hidden_markers = (
        ".sr-only",
        "sr-only",
        ".visually-hidden",
        "visually-hidden",
        "[hidden]",
        "aria-hidden",
    )
    filtered: list[str] = []
    for item in nodes:
        text = str(item or "").strip()
        if not text:
            continue
        lowered = text.lower()
        if any(marker in lowered for marker in hidden_markers):
            continue
        filtered.append(text)
    return filtered


def _build_quality_checks(snapshot: dict, *, project_signals: dict[str, object] | None = None) -> list[dict[str, str | bool]]:
    project_signals = project_signals or {}
    viewport_meta = bool(snapshot.get("viewport_meta"))
    document_lang = str(snapshot.get("document_lang") or "").strip()
    main_count = max(0, int(snapshot.get("main_count") or 0))
    landmark_count = max(0, int(snapshot.get("landmark_count") or 0))
    input_count = max(0, int(snapshot.get("input_count") or 0))
    labeled_input_count = max(0, int(snapshot.get("labeled_input_count") or 0))
    mobile_overflow = bool(snapshot.get("mobile_overflow_x"))
    unlabeled_interactive = [str(item) for item in (snapshot.get("unlabeled_interactive") or []) if str(item).strip()]
    mobile_small_tap_targets = [str(item) for item in (snapshot.get("mobile_small_tap_targets") or snapshot.get("small_tap_targets") or []) if str(item).strip()]
    mobile_text_overflow_nodes = _filter_visual_text_overflow_nodes([str(item) for item in (snapshot.get("mobile_text_overflow_nodes") or snapshot.get("text_overflow_nodes") or []) if str(item).strip()])
    broken_images = [str(item) for item in (snapshot.get("broken_images") or []) if str(item).strip()]
    fixed_overlays = [str(item) for item in (snapshot.get("mobile_fixed_overlays") or snapshot.get("fixed_overlays") or []) if str(item).strip()]
    section_count = max(0, int(snapshot.get("section_count") or 0))
    card_like_count = max(0, int(snapshot.get("card_like_count") or 0))
    product_surface_count = max(0, int(snapshot.get("product_surface_count") or 0))
    table_count = max(0, int(snapshot.get("table_count") or 0))
    interactive_count = max(0, int(snapshot.get("interactive_count") or 0))
    word_count = max(0, int(snapshot.get("word_count") or 0))
    rendered_text = " ".join(
        [
            str(snapshot.get("title") or ""),
            " ".join(str(item) for item in (snapshot.get("headings") or [])),
            " ".join(str(item) for item in (snapshot.get("subheadings") or [])),
            " ".join(str(item) for item in (snapshot.get("buttons") or [])),
            " ".join(str(item) for item in (snapshot.get("links") or [])),
            str(snapshot.get("excerpt") or ""),
        ]
    )
    starter_terms = _starter_residue_terms(rendered_text)
    rendered_emoji_count = _count_emoji_chars(rendered_text)
    inline_style_count = max(0, int(project_signals.get("inline_style_count") or 0))
    any_cast_count = max(0, int(project_signals.get("any_cast_count") or 0))
    source_emoji_count = max(0, int(project_signals.get("emoji_count") or 0))
    layout_overflow_risk_count = max(0, int(project_signals.get("layout_overflow_risk_count") or 0))
    generic_copy_count = len(_generic_copy_terms(rendered_text)) + max(0, int(project_signals.get("generic_copy_count") or 0))
    dynamic_state_required = _preview_dynamic_state_required(snapshot, rendered_text, project_signals)
    dense_app_surface = _dense_app_surface_ok(
        interactive_count=interactive_count,
        card_like_count=card_like_count,
        product_surface_count=product_surface_count,
        table_count=table_count,
        word_count=word_count,
    )
    product_depth_ok = (
        section_count >= 5 and (product_surface_count >= 2 or table_count >= 1 or card_like_count >= 4)
    ) or dense_app_surface
    checks: list[dict[str, str | bool]] = []
    checks.append({
        "id": "responsive-foundation",
        "label": "Responsive foundation",
        "ok": viewport_meta or bool(project_signals.get("responsive")),
        "detail": "Viewport/meta responsive basics detected." if (viewport_meta or project_signals.get("responsive")) else "Belum kelihatan viewport meta atau pola responsive layout yang meyakinkan.",
    })
    checks.append({
        "id": "responsive-overflow",
        "label": "Mobile overflow",
        "ok": not mobile_overflow,
        "detail": "Nggak kelihatan overflow horizontal di viewport mobile audit." if not mobile_overflow else "Preview masih overflow secara horizontal di viewport mobile.",
    })
    checks.append({
        "id": "a11y-landmarks",
        "label": "A11y landmarks",
        "ok": main_count > 0 and landmark_count >= 2 and bool(document_lang),
        "detail": "Lang, main landmark, dan struktur dasar aksesibilitas kelihatan ada." if (main_count > 0 and landmark_count >= 2 and bool(document_lang)) else "Lang/main landmark masih lemah atau belum kelihatan lengkap.",
    })
    checks.append({
        "id": "a11y-alt-text",
        "label": "Alt text",
        "ok": max(0, int(snapshot.get("images_missing_alt") or 0)) == 0,
        "detail": "Semua gambar yang ke-detect punya alt text." if max(0, int(snapshot.get("images_missing_alt") or 0)) == 0 else "Masih ada gambar tanpa alt text yang layak.",
    })
    checks.append({
        "id": "a11y-form-labels",
        "label": "Form labels",
        "ok": input_count == 0 or labeled_input_count >= input_count or bool(project_signals.get("labels")),
        "detail": "Field form terlihat punya label/aria yang cukup." if (input_count == 0 or labeled_input_count >= input_count or project_signals.get("labels")) else "Field form belum kelihatan punya label/aria yang rapi.",
    })
    checks.append({
        "id": "a11y-interactive-labels",
        "label": "Interactive labels",
        "ok": len(unlabeled_interactive) == 0,
        "detail": "Elemen interaktif yang ke-detect punya label teks/aria." if not unlabeled_interactive else f"Elemen interaktif tanpa label: {', '.join(unlabeled_interactive[:4])}.",
    })
    checks.append({
        "id": "mobile-tap-targets",
        "label": "Mobile tap targets",
        "ok": len(mobile_small_tap_targets) == 0,
        "detail": "Target tap mobile terlihat cukup besar." if not mobile_small_tap_targets else f"Target tap terlalu kecil: {', '.join(mobile_small_tap_targets[:4])}.",
    })
    checks.append({
        "id": "mobile-text-fit",
        "label": "Mobile text fit",
        "ok": len(mobile_text_overflow_nodes) == 0,
        "detail": "Tidak ada text overflow penting yang ke-detect di viewport mobile." if not mobile_text_overflow_nodes else f"Text overflow mobile: {', '.join(mobile_text_overflow_nodes[:4])}.",
    })
    checks.append({
        "id": "image-loads",
        "label": "Image loading",
        "ok": len(broken_images) == 0,
        "detail": "Gambar yang ke-detect berhasil load." if not broken_images else f"Gambar gagal load: {', '.join(broken_images[:4])}.",
    })
    checks.append({
        "id": "blocking-overlays",
        "label": "Blocking overlays",
        "ok": len(fixed_overlays) == 0,
        "detail": "Tidak ada overlay fixed besar yang terlihat memblokir viewport." if not fixed_overlays else f"Overlay fixed besar terdeteksi: {', '.join(fixed_overlays[:3])}.",
    })
    checks.append({
        "id": "starter-residue",
        "label": "Starter residue",
        "ok": len(starter_terms) == 0,
        "detail": "Tidak ada sisa branding/template starter yang terlihat." if not starter_terms else f"Sisa template/starter masih terlihat: {', '.join(starter_terms[:4])}.",
    })
    checks.append({
        "id": "source-type-discipline",
        "label": "Type discipline",
        "ok": any_cast_count == 0,
        "detail": "Tidak ada `as any`/tipe any yang ke-detect di source." if any_cast_count == 0 else f"Masih ada {any_cast_count} penggunaan any/as any di source.",
    })
    checks.append({
        "id": "source-style-discipline",
        "label": "Style discipline",
        "ok": inline_style_count <= 12,
        "detail": "Inline style masih wajar atau styling sudah dipindah ke class/CSS." if inline_style_count <= 12 else f"Inline style terlalu banyak ({inline_style_count}); pindahkan styling berulang ke CSS/class.",
    })
    checks.append({
        "id": "emoji-polish",
        "label": "Emoji polish",
        "ok": rendered_emoji_count <= 2 and source_emoji_count <= 4,
        "detail": "Emoji decoration tidak dominan." if (rendered_emoji_count <= 2 and source_emoji_count <= 4) else f"Emoji decoration terlalu dominan (rendered={rendered_emoji_count}, source={source_emoji_count}).",
    })
    checks.append({
        "id": "source-overflow-risk",
        "label": "Overflow-prone CSS",
        "ok": layout_overflow_risk_count == 0,
        "detail": "Tidak ada pola CSS yang sering bikin overflow mobile." if layout_overflow_risk_count == 0 else f"Ada {layout_overflow_risk_count} pola CSS rawan overflow mobile seperti min-width besar, 100vw, max-content, atau nowrap.",
    })
    checks.append({
        "id": "product-depth",
        "label": "Product depth",
        "ok": product_depth_ok,
        "detail": (
            "Struktur halaman punya kedalaman produk/app: section, surface, card, atau interaksi cukup terlihat."
            if product_depth_ok
            else f"Kedalaman produk masih tipis (sections={section_count}, surfaces={product_surface_count}, cards={card_like_count}, tables={table_count})."
        ),
    })
    checks.append({
        "id": "copy-specificity",
        "label": "Copy specificity",
        "ok": generic_copy_count <= 2,
        "detail": "Copy tidak didominasi frasa SaaS generik." if generic_copy_count <= 2 else f"Copy masih memakai terlalu banyak frasa SaaS generik ({generic_copy_count}).",
    })
    checks.append({
        "id": "state-loading",
        "label": "Loading state",
        "ok": bool(project_signals.get("loading")) or not dynamic_state_required,
        "detail": (
            "Ada sinyal loading/skeleton state di source."
            if project_signals.get("loading")
            else (
                "Loading state tidak wajib untuk halaman statis/presentational ini."
                if not dynamic_state_required
                else "Belum ketemu loading/skeleton state yang jelas di source."
            )
        ),
    })
    checks.append({
        "id": "state-error",
        "label": "Error state",
        "ok": bool(project_signals.get("error")) or not dynamic_state_required,
        "detail": (
            "Ada sinyal error/retry handling di source."
            if project_signals.get("error")
            else (
                "Error/retry state tidak wajib untuk halaman statis/presentational ini."
                if not dynamic_state_required
                else "Belum ketemu error/retry state yang jelas di source."
            )
        ),
    })
    checks.append({
        "id": "state-empty",
        "label": "Empty state",
        "ok": bool(project_signals.get("empty")) or not dynamic_state_required,
        "detail": (
            "Ada sinyal empty/no-results state di source."
            if project_signals.get("empty")
            else (
                "Empty/no-results state tidak wajib untuk halaman statis/presentational ini."
                if not dynamic_state_required
                else "Belum ketemu empty/no-results state yang jelas di source."
            )
        ),
    })
    return checks


def _preview_file_refs(*values: object) -> list[str]:
    refs: list[str] = []
    pattern = re.compile(r"\b((?:src|app|pages|components|styles|public|lib|api)/[\w./-]+\.(?:tsx|jsx|ts|js|css|scss|html|json))(?::\d+)?\b")
    for value in values:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False) if value is not None else ""
        for match in pattern.finditer(text):
            rel = match.group(1).strip()
            if rel and rel not in refs:
                refs.append(rel)
    return refs[:8]


def _preview_repair_candidates(project_signals: dict[str, object] | None, *fallback_values: object) -> list[str]:
    candidates: list[str] = []
    signals = project_signals or {}
    for value in list(signals.get("repair_candidate_files") or []):
        rel = str(value or "").strip()
        if rel and rel not in candidates:
            candidates.append(rel)
    for rel in _preview_file_refs(*fallback_values):
        if rel not in candidates:
            candidates.append(rel)
    css_first = [item for item in candidates if item.endswith((".css", ".scss"))]
    component_first = [item for item in candidates if item.endswith((".tsx", ".jsx", ".ts", ".js"))]
    html_first = [item for item in candidates if item.endswith(".html")]
    return [*component_first, *css_first, *html_first][:8]


def _build_preview_repair_targets(
    *,
    issue_details: list[dict[str, str]],
    snapshot: dict,
    project_signals: dict[str, object] | None,
    runtime_errors: list[str],
    layout_overflow_risks: list[str],
    unlabeled_interactive: list[str],
    mobile_text_overflow_nodes: list[str],
    small_tap_targets: list[str],
    broken_images: list[str],
    fixed_overlays: list[str],
    mobile_fixed_overlays: list[str],
) -> list[dict[str, object]]:
    targets: list[dict[str, object]] = []
    candidates = _preview_repair_candidates(project_signals, runtime_errors, layout_overflow_risks)
    css_candidates = [item for item in candidates if item.endswith((".css", ".scss"))]
    component_candidates = [item for item in candidates if item.endswith((".tsx", ".jsx", ".ts", ".js", ".html"))]

    def add(kind: str, priority: str, action: str, *, selectors: list[str] | None = None, likely_files: list[str] | None = None, evidence: list[str] | None = None) -> None:
        existing = {str(item.get("kind") or "") for item in targets}
        if kind in existing:
            return
        targets.append({
            "kind": kind,
            "priority": priority,
            "selectors": list(selectors or [])[:8],
            "likely_files": list(likely_files or candidates)[:8],
            "evidence": list(evidence or [])[:8],
            "action": action[:360],
        })

    if runtime_errors:
        add(
            "runtime",
            "critical",
            "Fix the browser runtime exception first; inspect referenced stack files, imports, undefined symbols, and render-time data assumptions.",
            likely_files=_preview_file_refs(runtime_errors) or component_candidates or candidates,
            evidence=runtime_errors,
        )

    responsive_selectors = [*mobile_text_overflow_nodes, *small_tap_targets, *fixed_overlays, *mobile_fixed_overlays]
    has_responsive_issue = bool(responsive_selectors or snapshot.get("mobile_overflow_x") or any(item.get("category") == "responsive" for item in issue_details))
    if has_responsive_issue:
        add(
            "responsive",
            "high",
            "Fix mobile layout at the listed selectors: remove fixed/min widths, add wrapping/overflow containers, resize tap targets, and retest mobile viewport.",
            selectors=responsive_selectors,
            likely_files=css_candidates or component_candidates or candidates,
            evidence=[*layout_overflow_risks, *responsive_selectors],
        )

    if unlabeled_interactive:
        add(
            "accessibility",
            "high",
            "Give each listed interactive element visible text or an aria-label, preserving real click/navigation behavior.",
            selectors=unlabeled_interactive,
            likely_files=component_candidates or candidates,
            evidence=unlabeled_interactive,
        )

    if broken_images:
        add(
            "assets",
            "high",
            "Repair broken image references by using existing public assets, uploaded assets, generated CSS visuals, or removing the broken media.",
            selectors=broken_images,
            likely_files=component_candidates or candidates,
            evidence=broken_images,
        )

    polish_categories = {str(item.get("category") or "") for item in issue_details}
    if polish_categories & {"metadata", "production-polish", "product-depth", "copy-specificity", "content", "interaction"}:
        add(
            "product_polish",
            "medium",
            "Improve the primary product surface: specific title/meta/copy, deeper domain sections, useful cards/tables/workflows, and real navigation/CTA behavior.",
            likely_files=component_candidates or candidates,
            evidence=[str(item.get("detail") or "") for item in issue_details if str(item.get("category") or "") in polish_categories],
        )

    return targets[:6]


def _build_preview_audit_result(
    preview_url: str,
    snapshot: dict,
    *,
    audit_mode: str,
    max_excerpt_chars: int = 800,
    runtime_warnings: list[str] | None = None,
    project_signals: dict[str, object] | None = None,
) -> dict:
    def list_field(name: str, limit: int = 8, chars: int = 180) -> list[str]:
        return [str(item).strip()[:chars] for item in (snapshot.get(name) or []) if str(item).strip()][:limit]

    title = str(snapshot.get("title") or "").strip()
    meta_description = str(snapshot.get("meta_description") or "").strip()
    headings = [str(item).strip()[:160] for item in (snapshot.get("headings") or []) if str(item).strip()][:3]
    subheadings = [str(item).strip()[:160] for item in (snapshot.get("subheadings") or []) if str(item).strip()][:4]
    buttons = [str(item).strip()[:160] for item in (snapshot.get("buttons") or []) if str(item).strip()][:8]
    links = [str(item).strip()[:160] for item in (snapshot.get("links") or []) if str(item).strip()][:8]
    excerpt = str(snapshot.get("excerpt") or "").strip()[:max_excerpt_chars]
    form_count = max(0, int(snapshot.get("form_count") or 0))
    input_count = max(0, int(snapshot.get("input_count") or 0))
    section_count = max(0, int(snapshot.get("section_count") or 0))
    card_like_count = max(0, int(snapshot.get("card_like_count") or 0))
    product_surface_count = max(0, int(snapshot.get("product_surface_count") or 0))
    table_count = max(0, int(snapshot.get("table_count") or 0))
    word_count = max(0, int(snapshot.get("word_count") or 0))
    image_count = max(0, int(snapshot.get("image_count") or 0))
    images_missing_alt = max(0, int(snapshot.get("images_missing_alt") or 0))
    interactive_count = max(0, int(snapshot.get("interactive_count") or 0))
    unlabeled_interactive = list_field("unlabeled_interactive")
    small_tap_targets = list_field("small_tap_targets")
    text_overflow_nodes = _filter_visual_text_overflow_nodes(list_field("text_overflow_nodes"))
    mobile_text_overflow_nodes = _filter_visual_text_overflow_nodes(list_field("mobile_text_overflow_nodes"))
    broken_images = list_field("broken_images", 6)
    fixed_overlays = list_field("fixed_overlays", 4)
    mobile_fixed_overlays = list_field("mobile_fixed_overlays", 4)
    console_errors = [str(item).strip()[:240] for item in (snapshot.get("console_errors") or []) if str(item).strip()][:8]
    page_errors = [str(item).strip()[:240] for item in (snapshot.get("page_errors") or []) if str(item).strip()][:6]
    rendered_text = " ".join([title, " ".join(headings), " ".join(subheadings), " ".join(buttons), " ".join(links), excerpt])
    starter_terms = _starter_residue_terms(rendered_text)
    rendered_emoji_count = _count_emoji_chars(rendered_text)
    source_emoji_count = max(0, int((project_signals or {}).get("emoji_count") or 0))
    inline_style_count = max(0, int((project_signals or {}).get("inline_style_count") or 0))
    any_cast_count = max(0, int((project_signals or {}).get("any_cast_count") or 0))
    layout_overflow_risk_count = max(0, int((project_signals or {}).get("layout_overflow_risk_count") or 0))
    layout_overflow_risks = [str(item).strip()[:180] for item in ((project_signals or {}).get("layout_overflow_risks") or []) if str(item).strip()][:6]
    generic_copy_terms = _generic_copy_terms(rendered_text)
    generic_copy_count = len(generic_copy_terms) + max(0, int((project_signals or {}).get("generic_copy_count") or 0))
    quality_checks = _build_quality_checks(snapshot, project_signals=project_signals)
    quality_failures = [check for check in quality_checks if not check.get("ok")]
    issue_details: list[dict[str, str]] = []

    def add_issue(severity: str, category: str, detail: str, suggested_fix: str = "") -> None:
        issue_details.append({
            "severity": severity,
            "category": category,
            "detail": detail[:300],
            "suggested_fix": suggested_fix[:300],
        })

    issues: list[str] = []
    generic_title = bool(title and _GENERIC_SAAS_COPY_RE.search(title)) or title.lower().startswith("build an ai tool app workspace")
    if not title:
        detail = "Preview page is missing a <title> tag."
        issues.append(detail)
        add_issue("warning", "metadata", detail, "Tambahkan title yang menjelaskan app/page.")
    elif generic_title:
        detail = f"Preview page title still feels generic or template-like: {title}."
        issues.append(detail)
        add_issue("warning", "metadata", detail, "Ganti title dengan nama produk dan fungsi domain yang spesifik.")
    if not meta_description:
        detail = "Preview page is missing a meta description."
        issues.append(detail)
        add_issue("warning", "metadata", detail, "Tambahkan meta description singkat untuk kualitas production.")
    if not headings:
        detail = "Preview page has no visible H1 heading."
        issues.append(detail)
        add_issue("blocking", "content", detail, "Tambahkan H1 yang jelas di first viewport.")
    route_404 = bool(
        any(str(item).strip().lower() in {"404", "not found", "page not found"} for item in headings)
        or re.search(r"\b(404|page not found|not found)\b", excerpt, re.IGNORECASE)
    )
    if route_404:
        detail = "Preview root is rendering a 404/not-found page instead of the primary app surface."
        issues.append(detail)
        add_issue("blocking", "routing", detail, "Add a root route '/' that renders the dashboard/home surface, or redirect '/' to the primary app route before preview audit.")
    if word_count < 20:
        detail = "Preview content is nearly empty or still showing a starter shell."
        issues.append(detail)
        add_issue("blocking", "content", detail, "Pastikan entrypoint memuat app baru dan isi first viewport dengan konten produk yang nyata.")
    elif word_count < 40:
        detail = "Preview content is very sparse, which usually means the page feels unfinished."
        issues.append(detail)
        add_issue("warning", "content", detail, "Lengkapi copy dan section utama agar app tidak terasa placeholder.")
    if not buttons and form_count == 0 and len(links) < 2:
        detail = "Preview has very little obvious interaction or navigation."
        issues.append(detail)
        add_issue("warning", "interaction", detail, "Tambahkan CTA, navigasi, form, atau kontrol yang relevan.")
    if images_missing_alt > 0:
        detail = f"Preview has {images_missing_alt} image(s) without useful alt text."
        issues.append(detail)
        add_issue("warning", "accessibility", detail, "Isi alt text pada image non-dekoratif.")
    if broken_images:
        detail = f"Preview has {len(broken_images)} broken image(s): {', '.join(broken_images[:3])}."
        issues.append(detail)
        add_issue("blocking", "assets", detail, "Perbaiki path asset, pakai asset lokal yang ada, atau hilangkan referensi rusak.")
    if unlabeled_interactive:
        detail = f"Preview has {len(unlabeled_interactive)} unlabeled interactive element(s)."
        issues.append(detail)
        add_issue("blocking", "accessibility", detail, "Tambahkan visible text atau aria-label yang bermakna.")
    if mobile_text_overflow_nodes:
        detail = f"Preview has {len(mobile_text_overflow_nodes)} mobile text overflow issue(s)."
        issues.append(detail)
        add_issue("blocking", "responsive", detail, "Atur wrapping, min-width, grid, atau ukuran kontainer mobile.")
    if fixed_overlays or mobile_fixed_overlays:
        detail = "Preview has large fixed overlay(s) that may block interaction."
        issues.append(detail)
        add_issue("blocking", "responsive", detail, "Pastikan fixed layer tidak menutup konten/aksi penting di mobile.")
    if starter_terms:
        detail = f"Preview still exposes starter/template residue: {', '.join(starter_terms[:4])}."
        issues.append(detail)
        add_issue("blocking", "production-polish", detail, "Hapus branding/footer/copy starter seperti Vite, seeded template, lorem ipsum, atau placeholder.")
    if any_cast_count:
        detail = f"Source still contains {any_cast_count} loose any/as any usage(s)."
        issues.append(detail)
        add_issue("warning", "source-quality", detail, "Ganti dengan tipe data eksplisit supaya hasil lebih production-ready.")
    if inline_style_count > 12:
        detail = f"Source contains {inline_style_count} inline style block(s), which makes the UI harder to polish consistently."
        issues.append(detail)
        add_issue("warning", "source-quality", detail, "Pindahkan styling berulang ke CSS class, token, atau component variants.")
    if rendered_emoji_count > 2 or source_emoji_count > 4:
        detail = f"UI relies heavily on emoji decoration (rendered={rendered_emoji_count}, source={source_emoji_count})."
        issues.append(detail)
        add_issue("warning", "visual-polish", detail, "Untuk UI profesional, pakai ikon library/CSS/typographic hierarchy daripada emoji dekoratif.")
    if layout_overflow_risk_count:
        detail = f"Source has {layout_overflow_risk_count} CSS pattern(s) commonly causing mobile overflow."
        issues.append(detail)
        suffix = f" Examples: {' | '.join(layout_overflow_risks[:3])}" if layout_overflow_risks else ""
        add_issue("warning", "responsive", f"{detail}{suffix}", "Ganti fixed/min-width besar, 100vw, max-content, atau nowrap dengan max-width:100%, overflow wrappers, dan responsive grid.")
    dense_app_surface = _dense_app_surface_ok(
        interactive_count=interactive_count,
        card_like_count=card_like_count,
        product_surface_count=product_surface_count,
        table_count=table_count,
        word_count=word_count,
    )
    product_depth_ok = (
        section_count >= 5 and (product_surface_count >= 2 or table_count >= 1 or card_like_count >= 4)
    ) or dense_app_surface
    if not product_depth_ok:
        detail = f"Product surface feels thin for a professional build (sections={section_count}, surfaces={product_surface_count}, cards={card_like_count}, tables={table_count})."
        issues.append(detail)
        add_issue("warning", "product-depth", detail, "Tambahkan product mock/workflow/security/pricing/detail section yang spesifik dan terlihat usable.")
    if generic_copy_count > 2:
        detail = f"Copy uses generic SaaS phrasing too often ({generic_copy_count} hit(s): {', '.join(generic_copy_terms[:4])})."
        issues.append(detail)
        add_issue("warning", "copy-specificity", detail, "Ganti frasa generik dengan detail domain, metrik, workflow, dan manfaat yang konkret.")
    if page_errors:
        detail = f"Preview threw {len(page_errors)} runtime browser error(s)."
        issues.append(detail)
        add_issue("blocking", "runtime", detail, "Baca stack/error browser lalu perbaiki runtime exception.")
    if console_errors:
        detail = f"Preview logged {len(console_errors)} browser console warning/error message(s)."
        issues.append(detail)
        add_issue("warning", "runtime", detail, "Bersihkan console errors/warnings yang berasal dari app.")
    if quality_failures:
        detail = f"Preview quality checks flagged {len(quality_failures)} area(s) across responsive/a11y/state readiness."
        issues.append(detail)
        for check in quality_failures:
            check_id = str(check.get("id") or "")
            if check_id == "product-depth" and any(item.get("category") == "product-depth" for item in issue_details):
                continue
            severity = "blocking" if check_id in {"responsive-overflow", "a11y-interactive-labels", "mobile-text-fit", "image-loads", "blocking-overlays", "starter-residue"} else "warning"
            add_issue(severity, check_id or "quality", str(check.get("detail") or detail), "Perbaiki area quality check terkait.")

    blocking_count = sum(1 for issue in issue_details if issue.get("severity") == "blocking")
    warning_count = sum(1 for issue in issue_details if issue.get("severity") == "warning")

    summary_parts = [
        f"mode={audit_mode}",
        f"title={title or '(missing)'}",
        f"h1={headings[0] if headings else '(missing)'}",
        f"buttons={len(buttons)}",
        f"links={len(links)}",
        f"forms={form_count}",
        f"sections={section_count}",
        f"surfaces={product_surface_count}",
        f"interactive={interactive_count}",
        f"words={word_count}",
        f"blocking={blocking_count}",
        f"warnings={warning_count}",
    ]
    viewport = snapshot.get("viewport") if isinstance(snapshot.get("viewport"), dict) else {}
    mobile_viewport = snapshot.get("mobile_viewport") if isinstance(snapshot.get("mobile_viewport"), dict) else {}
    screenshot_path = str(snapshot.get("screenshot_path") or "").strip()
    screenshot_viewport = str(snapshot.get("screenshot_viewport") or "").strip()
    has_browser_screen = audit_mode in {"agent-browser", "browser", "playwright"}
    has_dom_snapshot = bool(title or headings or excerpt or word_count or interactive_count or viewport or mobile_viewport)
    has_screenshot = bool(screenshot_path)
    visual_evidence = {
        "has_screen": bool(has_browser_screen and has_dom_snapshot),
        "screen_backend": audit_mode if has_browser_screen else "",
        "has_dom_snapshot": has_dom_snapshot,
        "has_screenshot": has_screenshot,
        "screenshot_path": screenshot_path,
        "screenshot_viewport": screenshot_viewport,
        "desktop_viewport": viewport,
        "mobile_viewport": mobile_viewport,
        "desktop_overflow_x": bool(snapshot.get("desktop_overflow_x")),
        "mobile_overflow_x": bool(snapshot.get("mobile_overflow_x")),
    }
    visual_summary = {
        "mode": audit_mode,
        "title": title,
        "primary_heading": headings[0] if headings else "",
        "screenshot_path": screenshot_path,
        "screenshot_viewport": screenshot_viewport,
        "desktop_viewport": viewport,
        "mobile_viewport": mobile_viewport,
        "desktop_overflow_x": bool(snapshot.get("desktop_overflow_x")),
        "mobile_overflow_x": bool(snapshot.get("mobile_overflow_x")),
        "word_count": word_count,
        "interactive_count": interactive_count,
        "section_count": section_count,
        "card_like_count": card_like_count,
        "product_surface_count": product_surface_count,
        "table_count": table_count,
        "button_labels": buttons[:6],
        "link_labels": links[:6],
        "top_blockers": [item for item in issue_details if item.get("severity") == "blocking"][:6],
        "top_warnings": [item for item in issue_details if item.get("severity") == "warning"][:6],
        "runtime_errors": [*page_errors, *console_errors][:8],
        "starter_residue": starter_terms,
        "inline_style_count": inline_style_count,
        "any_cast_count": any_cast_count,
        "layout_overflow_risk_count": layout_overflow_risk_count,
        "layout_overflow_risks": layout_overflow_risks,
        "generic_copy_count": generic_copy_count,
        "generic_copy_terms": generic_copy_terms,
        "emoji_count": {"rendered": rendered_emoji_count, "source": source_emoji_count},
        "excerpt": excerpt[:600],
    }
    repair_brief_parts = [
        f"Preview audit mode={audit_mode}, blocking={blocking_count}, warnings={warning_count}.",
        f"Title: {title or '(missing)'}; H1: {headings[0] if headings else '(missing)'}.",
    ]
    if bool(snapshot.get("mobile_overflow_x")):
        mobile_scroll = snapshot.get("mobile_scroll_width")
        mobile_width = snapshot.get("mobile_viewport_width")
        repair_brief_parts.append(f"Mobile viewport has horizontal overflow (scroll_width={mobile_scroll}, viewport_width={mobile_width}).")
    if layout_overflow_risks:
        repair_brief_parts.append("Overflow-prone source patterns: " + " | ".join(layout_overflow_risks[:3]))
    if page_errors or console_errors:
        repair_brief_parts.append(f"Runtime messages: {' | '.join([*page_errors, *console_errors][:3])}")
    if issue_details:
        repair_brief_parts.append("Top issues: " + " | ".join(
            f"{item.get('severity')} {item.get('category')}: {item.get('detail')}"
            for item in issue_details[:5]
        ))
    if screenshot_path:
        repair_brief_parts.append(f"Screenshot evidence: {screenshot_path} ({screenshot_viewport or 'viewport unknown'}).")

    repair_targets = _build_preview_repair_targets(
        issue_details=issue_details,
        snapshot=snapshot,
        project_signals=project_signals,
        runtime_errors=[*page_errors, *console_errors],
        layout_overflow_risks=layout_overflow_risks,
        unlabeled_interactive=unlabeled_interactive,
        mobile_text_overflow_nodes=mobile_text_overflow_nodes,
        small_tap_targets=small_tap_targets,
        broken_images=broken_images,
        fixed_overlays=fixed_overlays,
        mobile_fixed_overlays=mobile_fixed_overlays,
    )
    if repair_targets:
        first_target = repair_targets[0]
        likely = ", ".join(str(item) for item in list(first_target.get("likely_files") or [])[:3])
        repair_brief_parts.append(
            f"Repair target: {first_target.get('kind')} priority={first_target.get('priority')} files={likely or '(inspect source)'}."
        )

    repair_brief = " ".join(repair_brief_parts)[:2000]
    evidence_pack = {
        "audit_mode": audit_mode,
        "preview_url": preview_url,
        "repair_brief": repair_brief,
        "visual_evidence": visual_evidence,
        "screenshot_path": screenshot_path,
        "screenshot_viewport": screenshot_viewport,
        "desktop_viewport": viewport,
        "mobile_viewport": mobile_viewport,
        "desktop_overflow_x": bool(snapshot.get("desktop_overflow_x")),
        "mobile_overflow_x": bool(snapshot.get("mobile_overflow_x")),
        "top_blockers": [item for item in issue_details if item.get("severity") == "blocking"][:5],
        "top_warnings": [item for item in issue_details if item.get("severity") == "warning"][:5],
        "runtime_errors": [*page_errors, *console_errors][:8],
        "unlabeled_interactive": unlabeled_interactive[:6],
        "mobile_text_overflow_nodes": mobile_text_overflow_nodes[:6],
        "small_tap_targets": small_tap_targets[:6],
        "broken_images": broken_images[:6],
        "fixed_overlays": [*fixed_overlays, *mobile_fixed_overlays][:6],
        "repair_targets": repair_targets,
        "source_candidates": _preview_repair_candidates(project_signals, layout_overflow_risks, [*page_errors, *console_errors]),
        "source_evidence": {
            "layout_overflow_risks": layout_overflow_risks[:6],
            "inline_style_count": inline_style_count,
            "any_cast_count": any_cast_count,
            "generic_copy_terms": generic_copy_terms[:6],
            "starter_residue": starter_terms[:6],
        },
        "excerpt": excerpt[:600],
    }

    return {
        "ok": blocking_count == 0,
        "preview_url": preview_url,
        "audit_mode": audit_mode,
        "title": title,
        "meta_description": meta_description,
        "headings": headings,
        "subheadings": subheadings,
        "buttons": buttons,
        "links": links,
        "form_count": form_count,
        "section_count": section_count,
        "card_like_count": card_like_count,
        "product_surface_count": product_surface_count,
        "table_count": table_count,
        "input_count": input_count,
        "interactive_count": interactive_count,
        "word_count": word_count,
        "image_count": image_count,
        "images_missing_alt": images_missing_alt,
        "broken_images": broken_images,
        "unlabeled_interactive": unlabeled_interactive,
        "small_tap_targets": small_tap_targets,
        "text_overflow_nodes": text_overflow_nodes,
        "mobile_text_overflow_nodes": mobile_text_overflow_nodes,
        "fixed_overlays": fixed_overlays,
        "mobile_fixed_overlays": mobile_fixed_overlays,
        "viewport": viewport,
        "mobile_viewport": mobile_viewport,
        "console_errors": console_errors,
        "page_errors": page_errors,
        "runtime_warnings": runtime_warnings or [],
        "issues": issues,
        "issue_details": issue_details,
        "quality_checks": quality_checks,
        "visual_summary": visual_summary,
        "visual_evidence": visual_evidence,
        "evidence_pack": evidence_pack,
        "repair_targets": repair_targets,
        "repair_brief": repair_brief,
        "excerpt": excerpt,
        "summary": "; ".join(summary_parts),
    }


def _extract_preview_snapshot_from_html(html: str, max_excerpt_chars: int = 800) -> dict:
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    title = _clean_html_text(title_match.group(1)) if title_match else ""

    meta_match = re.search(r"<meta[^>]+name=['\"]description['\"][^>]+content=['\"](.*?)['\"]", html, flags=re.IGNORECASE | re.DOTALL)
    meta_description = _clean_html_text(meta_match.group(1)) if meta_match else ""

    headings = _extract_text_matches(r"<h1[^>]*>(.*?)</h1>", html, 3)
    subheadings = _extract_text_matches(r"<h2[^>]*>(.*?)</h2>", html, 4)
    buttons = _extract_text_matches(r"<button[^>]*>(.*?)</button>", html, 8)
    links = _extract_text_matches(r"<a[^>]*>(.*?)</a>", html, 8)

    body_match = re.search(r"<body[^>]*>(.*?)</body>", html, flags=re.IGNORECASE | re.DOTALL)
    body_html = body_match.group(1) if body_match else html
    body_without_noise = re.sub(r"<script\b[^>]*>.*?</script>", " ", body_html, flags=re.IGNORECASE | re.DOTALL)
    body_without_noise = re.sub(r"<style\b[^>]*>.*?</style>", " ", body_without_noise, flags=re.IGNORECASE | re.DOTALL)
    body_text = _clean_html_text(body_without_noise)
    excerpt = body_text[:max_excerpt_chars]

    image_tags = re.findall(r"<img\b[^>]*>", html, flags=re.IGNORECASE)
    class_text = " ".join(re.findall(r"class\s*=\s*['\"]([^'\"]+)['\"]", html, flags=re.IGNORECASE | re.DOTALL))
    product_surface_count = len(re.findall(r"\b(dashboard|panel|metric|chart|table|workflow|preview)\b", class_text, flags=re.IGNORECASE))
    images_missing_alt = 0
    for tag in image_tags:
        alt_match = re.search(r"alt\s*=\s*['\"](.*?)['\"]", tag, flags=re.IGNORECASE | re.DOTALL)
        if not alt_match or not _clean_html_text(alt_match.group(1)):
            images_missing_alt += 1

    html_open_match = re.search(r"<html[^>]*lang=['\"]([^'\"]+)['\"]", html, flags=re.IGNORECASE | re.DOTALL)
    labeled_input_count = 0
    input_tags = re.findall(r"<(input|textarea|select)\b[^>]*>", html, flags=re.IGNORECASE | re.DOTALL)
    if input_tags:
        labeled_input_count = len(re.findall(r"<label\b", html, flags=re.IGNORECASE))
        labeled_input_count += len(re.findall(r"aria-label\s*=|aria-labelledby\s*=", html, flags=re.IGNORECASE))

    return {
        "title": title,
        "meta_description": meta_description,
        "viewport_meta": bool(re.search(r"<meta[^>]+name=['\"]viewport['\"]", html, flags=re.IGNORECASE | re.DOTALL)),
        "document_lang": _clean_html_text(html_open_match.group(1)) if html_open_match else "",
        "headings": headings,
        "subheadings": subheadings,
        "buttons": buttons,
        "links": links,
        "form_count": len(re.findall(r"<form\b", html, flags=re.IGNORECASE)),
        "section_count": len(re.findall(r"<(section|article)\b", html, flags=re.IGNORECASE)),
        "card_like_count": len(re.findall(r"<article\b|\b(card|panel|tile)\b", html + " " + class_text, flags=re.IGNORECASE)),
        "product_surface_count": product_surface_count + len(re.findall(r"<table\b|role=['\"]table['\"]", html, flags=re.IGNORECASE)),
        "table_count": len(re.findall(r"<table\b|role=['\"]table['\"]", html, flags=re.IGNORECASE)),
        "input_count": len(re.findall(r"<(input|textarea|select)\b", html, flags=re.IGNORECASE)),
        "labeled_input_count": labeled_input_count,
        "landmark_count": len(re.findall(r"<(main|nav|header|footer|aside)\b", html, flags=re.IGNORECASE)),
        "main_count": len(re.findall(r"<main\b", html, flags=re.IGNORECASE)),
        "button_count": len(re.findall(r"<button\b", html, flags=re.IGNORECASE)),
        "interactive_count": len(re.findall(r"<(button|a|input|textarea|select|summary)\b", html, flags=re.IGNORECASE)),
        "unlabeled_interactive": [],
        "small_tap_targets": [],
        "text_overflow_nodes": [],
        "mobile_text_overflow_nodes": [],
        "fixed_overlays": [],
        "mobile_fixed_overlays": [],
        "mobile_overflow_x": False,
        "word_count": len(re.findall(r"\b\w+\b", body_text)),
        "image_count": len(image_tags),
        "images_missing_alt": images_missing_alt,
        "broken_images": [],
        "viewport": {},
        "mobile_viewport": {},
        "console_errors": [],
        "page_errors": [],
        "excerpt": excerpt,
    }


def _fetch_preview_html(preview_url: str, attempts: int = 3) -> str:
    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            req = URLRequest(
                preview_url,
                headers={
                    "User-Agent": "Appora/0.1 (+preview-audit)",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                method="GET",
            )
            with urlopen(req, timeout=8) as resp:  # nosec B310 - internal preview fetch
                raw = resp.read(250_000)
            return raw.decode("utf-8", errors="ignore")
        except Exception as exc:
            last_error = exc
            if attempt < max(1, attempts) - 1:
                time.sleep(0.8)
    raise HTTPException(502, f"Preview audit fetch failed: {last_error}")


_AGENT_BROWSER_SNAPSHOT_JS = r"""(() => {
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const cssPath = (node) => {
    if (!node || !node.tagName) return '';
    const parts = [];
    let current = node;
    while (current && current.nodeType === Node.ELEMENT_NODE && parts.length < 4) {
      const tag = current.tagName.toLowerCase();
      const id = current.getAttribute('id');
      if (id) {
        parts.unshift(`${tag}#${id}`);
        break;
      }
      const cls = clean(current.getAttribute('class') || '').split(/\s+/).filter(Boolean).slice(0, 2).join('.');
      parts.unshift(cls ? `${tag}.${cls}` : tag);
      current = current.parentElement;
    }
    return parts.join(' > ');
  };
  const listText = (selector, limit = 8) => Array.from(document.querySelectorAll(selector))
    .map((node) => clean(node.textContent || node.getAttribute?.('aria-label') || ''))
    .filter(Boolean)
    .slice(0, limit);
  const buttonNodes = Array.from(document.querySelectorAll('button, [role="button"], input[type="button"], input[type="submit"]'));
  const buttonText = buttonNodes
    .map((node) => clean(node.textContent || node.getAttribute('aria-label') || node.getAttribute('value') || ''))
    .filter(Boolean)
    .slice(0, 8);
  const linkText = Array.from(document.querySelectorAll('a'))
    .map((node) => clean(node.textContent || node.getAttribute('aria-label') || ''))
    .filter(Boolean)
    .slice(0, 8);
  const bodyText = clean(document.body?.innerText || '');
  const imageNodes = Array.from(document.querySelectorAll('img'));
  const formFields = Array.from(document.querySelectorAll('input, textarea, select'));
  const interactiveNodes = Array.from(document.querySelectorAll('button, [role="button"], a[href], input, textarea, select, summary, [tabindex]:not([tabindex="-1"])'));
  const labeledInputCount = formFields.filter((field) => {
    const id = clean(field.getAttribute('id') || '');
    return Boolean(
      clean(field.getAttribute('aria-label') || '') ||
      clean(field.getAttribute('aria-labelledby') || '') ||
      field.closest('label') ||
      (id && document.querySelector(`label[for="${id}"]`))
    );
  }).length;
  const unlabeledInteractive = interactiveNodes
    .filter((node) => !clean(node.textContent || node.getAttribute('aria-label') || node.getAttribute('title') || node.getAttribute('value') || node.getAttribute('alt') || ''))
    .map(cssPath)
    .filter(Boolean)
    .slice(0, 8);
  const smallTapTargets = interactiveNodes
    .map((node) => ({ node, rect: node.getBoundingClientRect() }))
    .filter(({ rect }) => rect.width > 0 && rect.height > 0 && (rect.width < 32 || rect.height < 32))
    .map(({ node, rect }) => `${cssPath(node)} (${Math.round(rect.width)}x${Math.round(rect.height)})`)
    .slice(0, 8);
  const fixedOverlays = Array.from(document.querySelectorAll('*'))
    .filter((node) => {
      const style = window.getComputedStyle(node);
      const rect = node.getBoundingClientRect();
      return style.position === 'fixed' && rect.width > window.innerWidth * 0.8 && rect.height > window.innerHeight * 0.8 && style.pointerEvents !== 'none';
    })
    .map(cssPath)
    .filter(Boolean)
    .slice(0, 4);
  const isVisuallyHidden = (node) => {
    const style = window.getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    const className = String(node.getAttribute('class') || '').toLowerCase();
    return (
      style.display === 'none' ||
      style.visibility === 'hidden' ||
      style.opacity === '0' ||
      node.closest('[hidden], [aria-hidden="true"]') ||
      className.includes('sr-only') ||
      className.includes('visually-hidden') ||
      (style.position === 'absolute' && rect.width <= 2 && rect.height <= 2 && (style.overflow === 'hidden' || style.clip !== 'auto' || style.clipPath !== 'none'))
    );
  };
  const textOverflowNodes = Array.from(document.querySelectorAll('button, a, h1, h2, h3, p, span, label, input'))
    .filter((node) => !isVisuallyHidden(node))
    .filter((node) => node.scrollWidth > node.clientWidth + 4 && node.clientWidth > 0)
    .map((node) => `${cssPath(node)} "${clean(node.textContent || node.getAttribute('value') || '').slice(0, 60)}"`)
    .filter(Boolean)
    .slice(0, 8);
  const productSurfaceNodes = Array.from(document.querySelectorAll('[class*="dashboard" i], [class*="panel" i], [class*="metric" i], [class*="chart" i], [class*="table" i], [class*="workflow" i], [class*="preview" i], table, [role="table"]'));
  const cardLikeNodes = Array.from(document.querySelectorAll('article, [class*="card" i], [class*="panel" i], [class*="tile" i]'));
  return {
    title: clean(document.title || ''),
    meta_description: clean(document.querySelector('meta[name="description"]')?.getAttribute('content') || ''),
    viewport_meta: Boolean(document.querySelector('meta[name="viewport"]')),
    document_lang: clean(document.documentElement.getAttribute('lang') || ''),
    headings: listText('h1', 3),
    subheadings: listText('h2', 4),
    buttons: buttonText,
    links: linkText,
    form_count: document.querySelectorAll('form').length,
    section_count: document.querySelectorAll('section, article').length,
    nav_count: document.querySelectorAll('nav, [role="navigation"]').length,
    table_count: document.querySelectorAll('table, [role="table"]').length,
    card_like_count: cardLikeNodes.length,
    product_surface_count: productSurfaceNodes.length,
    input_count: formFields.length,
    labeled_input_count: labeledInputCount,
    landmark_count: document.querySelectorAll('main, nav, header, footer, aside, section[aria-label], [role="main"], [role="navigation"], [role="contentinfo"]').length,
    main_count: document.querySelectorAll('main, [role="main"]').length,
    button_count: buttonNodes.length,
    interactive_count: interactiveNodes.length,
    unlabeled_interactive: unlabeledInteractive,
    small_tap_targets: smallTapTargets,
    fixed_overlays: fixedOverlays,
    text_overflow_nodes: textOverflowNodes,
    word_count: bodyText ? bodyText.split(/\s+/).filter(Boolean).length : 0,
    image_count: imageNodes.length,
    images_missing_alt: imageNodes.filter((img) => !clean(img.getAttribute('alt') || '')).length,
    broken_images: imageNodes.filter((img) => img.complete && img.naturalWidth === 0).map((img) => clean(img.getAttribute('src') || cssPath(img))).filter(Boolean).slice(0, 6),
    scroll_width: Math.max(document.documentElement?.scrollWidth || 0, document.body?.scrollWidth || 0),
    viewport_width: window.innerWidth || document.documentElement?.clientWidth || 0,
    viewport_height: window.innerHeight || document.documentElement?.clientHeight || 0,
    excerpt: bodyText.slice(0, 1200)
  };
})()"""


def _parse_agent_browser_json(stdout: str) -> object:
    text = (stdout or "").strip()
    if not text:
        raise ValueError("empty output")
    return json.loads(text)


def _agent_browser_run(agent_browser: str, session: str, args: list[str], *, timeout: int = PREVIEW_BROWSER_AUDIT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    return subprocess.run(
        [agent_browser, "--session-name", session, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _agent_browser_eval_snapshot(agent_browser: str, session: str) -> dict:
    proc = _agent_browser_run(agent_browser, session, ["eval", _AGENT_BROWSER_SNAPSHOT_JS])
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "agent-browser eval failed").strip()[:300])
    payload = _parse_agent_browser_json(proc.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("agent-browser returned non-object snapshot")
    return payload


def _agent_browser_console_messages(agent_browser: str, session: str) -> tuple[list[str], list[str]]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    try:
        proc = _agent_browser_run(agent_browser, session, ["console"], timeout=8)
        for line in (proc.stdout or "").splitlines():
            clean = re.sub(r"\s+", " ", line).strip()
            if not clean:
                continue
            if clean.startswith("[error]") or clean.startswith("[warning]"):
                console_errors.append(clean[:240])
    except Exception:
        pass
    try:
        proc = _agent_browser_run(agent_browser, session, ["errors"], timeout=8)
        for line in (proc.stdout or "").splitlines():
            clean = re.sub(r"\s+", " ", line).strip()
            if clean and not clean.startswith("✓") and clean not in {"✗", "✗ "}:
                page_errors.append(clean[:240])
    except Exception:
        pass
    return console_errors[:8], page_errors[:6]


def _looks_like_transient_loading_snapshot(snapshot: dict) -> bool:
    excerpt = str(snapshot.get("excerpt") or "").lower()
    headings = " ".join(str(item) for item in (snapshot.get("headings") or [])).lower()
    word_count = int(snapshot.get("word_count") or 0)
    return word_count <= 8 and bool(re.search(r"\b(loading|memuat|please wait|spinner|preparing)\b", f"{headings} {excerpt}"))


def _run_agent_browser_preview_audit(
    preview_url: str,
    project_dir: Path,
    max_excerpt_chars: int = 800,
    *,
    project_signals: dict[str, object] | None = None,
) -> tuple[dict | None, str | None]:
    agent_browser = _resolve_agent_browser_binary()
    if not agent_browser:
        return None, "agent-browser CLI is not installed, so preview audit fell back to Playwright/HTML inspection."

    session = f"appora-preview-{_sha256_text(preview_url)[:12]}-{uuid.uuid4().hex[:6]}"
    try:
        for args in (["set", "viewport", "1440", "900"], ["open", preview_url], ["wait", str(PREVIEW_BROWSER_AUDIT_SETTLE_MS)]):
            proc = _agent_browser_run(agent_browser, session, args)
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "command failed").strip()[:240]
                return None, f"agent-browser audit command failed ({detail}), so preview audit fell back to Playwright/HTML inspection."

        desktop_snapshot = _agent_browser_eval_snapshot(agent_browser, session)
        if _looks_like_transient_loading_snapshot(desktop_snapshot):
            _agent_browser_run(agent_browser, session, ["wait", str(max(900, PREVIEW_BROWSER_AUDIT_SETTLE_MS))])
            desktop_snapshot = _agent_browser_eval_snapshot(agent_browser, session)

        screenshot_path = str(Path(os.getenv("TMPDIR", "/tmp")) / f"appora-preview-audit-{_sha256_text(preview_url)[:16]}.png")
        try:
            screenshot_proc = _agent_browser_run(agent_browser, session, ["screenshot", screenshot_path], timeout=8)
            if screenshot_proc.returncode == 0 and Path(screenshot_path).exists():
                desktop_snapshot["screenshot_path"] = screenshot_path
                desktop_snapshot["screenshot_viewport"] = "desktop"
        except Exception:
            pass

        proc = _agent_browser_run(agent_browser, session, ["set", "viewport", "390", "844"])
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "viewport command failed").strip()[:240]
            return None, f"agent-browser mobile viewport failed ({detail}), so preview audit fell back to Playwright/HTML inspection."
        _agent_browser_run(agent_browser, session, ["wait", str(min(PREVIEW_BROWSER_AUDIT_SETTLE_MS, 500))])
        mobile_snapshot = _agent_browser_eval_snapshot(agent_browser, session)
        if _looks_like_transient_loading_snapshot(mobile_snapshot):
            _agent_browser_run(agent_browser, session, ["wait", str(max(900, PREVIEW_BROWSER_AUDIT_SETTLE_MS))])
            mobile_snapshot = _agent_browser_eval_snapshot(agent_browser, session)

        console_errors, page_errors = _agent_browser_console_messages(agent_browser, session)
        snapshot = {
            **desktop_snapshot,
            "viewport": {
                "width": desktop_snapshot.get("viewport_width"),
                "height": desktop_snapshot.get("viewport_height"),
            },
            "mobile_viewport": {
                "width": mobile_snapshot.get("viewport_width"),
                "height": mobile_snapshot.get("viewport_height"),
            },
            "mobile_headings": mobile_snapshot.get("headings") or [],
            "mobile_buttons": mobile_snapshot.get("buttons") or [],
            "mobile_links": mobile_snapshot.get("links") or [],
            "mobile_unlabeled_interactive": mobile_snapshot.get("unlabeled_interactive") or [],
            "mobile_small_tap_targets": mobile_snapshot.get("small_tap_targets") or [],
            "mobile_text_overflow_nodes": mobile_snapshot.get("text_overflow_nodes") or [],
            "mobile_fixed_overlays": mobile_snapshot.get("fixed_overlays") or [],
            "mobile_scroll_width": mobile_snapshot.get("scroll_width"),
            "mobile_viewport_width": mobile_snapshot.get("viewport_width"),
            "mobile_overflow_x": int(mobile_snapshot.get("scroll_width") or 0) > int(mobile_snapshot.get("viewport_width") or 0) + 8,
            "desktop_overflow_x": int(desktop_snapshot.get("scroll_width") or 0) > int(desktop_snapshot.get("viewport_width") or 0) + 8,
            "console_errors": console_errors,
            "page_errors": page_errors,
        }
        return _build_preview_audit_result(
            preview_url,
            snapshot,
            audit_mode="agent-browser",
            max_excerpt_chars=max_excerpt_chars,
            project_signals=project_signals,
        ), None
    except subprocess.TimeoutExpired:
        return None, "agent-browser audit timed out, so preview audit fell back to Playwright/HTML inspection."
    except Exception as exc:
        return None, f"agent-browser audit failed ({str(exc)[:240]}), so preview audit fell back to Playwright/HTML inspection."
    finally:
        try:
            _agent_browser_run(agent_browser, session, ["close"], timeout=5)
        except Exception:
            pass


def _run_playwright_preview_audit(
    preview_url: str,
    project_dir: Path,
    max_excerpt_chars: int = 800,
    *,
    project_signals: dict[str, object] | None = None,
) -> tuple[dict | None, str | None]:
    if not _playwright_preview_audit_ready(project_dir):
        return None, "Playwright browser audit is not ready in this project/runtime yet, so preview audit fell back to HTML inspection."

    node_bin = _resolve_node_binary()
    script_path = _playwright_audit_script()
    if not node_bin or not script_path.exists():
        return None, "Node.js or the browser audit script is missing, so preview audit fell back to HTML inspection."

    try:
        proc = subprocess.run(
            [node_bin, str(script_path), preview_url, "12000", str(PREVIEW_BROWSER_AUDIT_SETTLE_MS)],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=PREVIEW_BROWSER_AUDIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None, "Playwright browser audit timed out, so preview audit fell back to HTML inspection."
    except Exception as exc:
        return None, f"Playwright browser audit failed to start ({exc}), so preview audit fell back to HTML inspection."

    lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    payload_text = lines[-1] if lines else ""
    if not payload_text:
        stderr = (proc.stderr or "").strip()
        detail = stderr[:200] if stderr else "no structured output"
        return None, f"Playwright browser audit did not return usable output ({detail}), so preview audit fell back to HTML inspection."

    try:
        payload = json.loads(payload_text)
    except Exception:
        return None, "Playwright browser audit returned unreadable output, so preview audit fell back to HTML inspection."

    if not isinstance(payload, dict) or not payload.get("ok"):
        detail = str(payload.get("error") or proc.stderr or "browser launch failed").strip()[:240] if isinstance(payload, dict) else "browser launch failed"
        return None, f"Playwright browser audit was unavailable ({detail}), so preview audit fell back to HTML inspection."

    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, dict):
        return None, "Playwright browser audit returned an invalid snapshot, so preview audit fell back to HTML inspection."

    return _build_preview_audit_result(
        preview_url,
        snapshot,
        audit_mode="browser",
        max_excerpt_chars=max_excerpt_chars,
        project_signals=project_signals,
    ), None


def _audit_preview_html(
    preview_url: str,
    html: str,
    max_excerpt_chars: int = 800,
    *,
    project_signals: dict[str, object] | None = None,
) -> dict:
    snapshot = _extract_preview_snapshot_from_html(html, max_excerpt_chars=max_excerpt_chars)
    project_dir_value = (project_signals or {}).get("project_dir")
    if isinstance(project_dir_value, str) and project_dir_value.strip():
        try:
            source_snapshot = _extract_preview_snapshot_from_source(Path(project_dir_value), max_excerpt_chars=max_excerpt_chars)
            snapshot = _merge_preview_snapshots(snapshot, source_snapshot)
        except Exception:
            pass
    return _build_preview_audit_result(
        preview_url,
        snapshot,
        audit_mode="html",
        max_excerpt_chars=max_excerpt_chars,
        project_signals=project_signals,
    )


def _run_shell_command(command: str, cwd: Path, timeout: int = 120) -> dict:
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "ok": proc.returncode == 0,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr,
            "returncode": proc.returncode,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "stdout": exc.stdout or "",
            "stderr": (exc.stderr or "") + "\nCommand timed out",
            "returncode": 124,
        }


def _run_shell_command_streaming(command: str, cwd: Path, emit_chunk, timeout: int = 120) -> dict:
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stream_buffers: dict[str, list[str]] = {"stdout": [], "stderr": []}
    last_flush: dict[str, float] = {"stdout": time.monotonic(), "stderr": time.monotonic()}
    started = time.monotonic()

    def flush_stream(stream: str, *, force: bool = False) -> None:
        buffered = "".join(stream_buffers.get(stream) or [])
        if not buffered:
            return
        now = time.monotonic()
        if not force and len(buffered) < 900 and now - last_flush.get(stream, now) < 0.35:
            return
        stream_buffers[stream] = []
        last_flush[stream] = now
        emit_chunk(stream, buffered)

    def append_stream(stream: str, chunk: str) -> None:
        if not chunk:
            return
        stream_buffers.setdefault(stream, []).append(chunk)
        flush_stream(stream)

    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                if time.monotonic() - started > timeout:
                    proc.kill()
                    stderr_chunks.append("\nCommand timed out")
                    return {
                        "ok": False,
                        "stdout": "".join(stdout_chunks),
                        "stderr": "".join(stderr_chunks),
                        "returncode": 124,
                    }
                stdout_chunks.append(line)
                append_stream("stdout", line)
        finally:
            flush_stream("stdout", force=True)
            flush_stream("stderr", force=True)
            try:
                proc.stdout.close()
            except Exception:
                pass
        returncode = proc.wait(timeout=max(1, int(timeout - (time.monotonic() - started))))
        return {
            "ok": returncode == 0,
            "stdout": "".join(stdout_chunks),
            "stderr": "".join(stderr_chunks),
            "returncode": returncode,
        }
    except subprocess.TimeoutExpired:
        try:
            proc.kill()  # type: ignore[possibly-undefined]
        except Exception:
            pass
        stderr_chunks.append("\nCommand timed out")
        append_stream("stderr", "Command timed out\n")
        flush_stream("stdout", force=True)
        flush_stream("stderr", force=True)
        return {
            "ok": False,
            "stdout": "".join(stdout_chunks),
            "stderr": "".join(stderr_chunks),
            "returncode": 124,
        }
    except Exception as exc:
        stderr_chunks.append(str(exc))
        append_stream("stderr", f"{exc}\n")
        flush_stream("stdout", force=True)
        flush_stream("stderr", force=True)
        return {
            "ok": False,
            "stdout": "".join(stdout_chunks),
            "stderr": "".join(stderr_chunks),
            "returncode": 1,
        }


_SAFE_COMMAND_PREFIXES = (
    ("npm", "run"),
    ("npm", "test"),
    ("npm", "install"),
    ("npm", "i"),
    ("npm", "add"),
    ("npm", "ci"),
    ("pnpm", "run"),
    ("pnpm", "test"),
    ("pnpm", "install"),
    ("pnpm", "add"),
    ("yarn", "run"),
    ("yarn", "test"),
    ("yarn", "install"),
    ("yarn", "add"),
    ("bun", "run"),
    ("bun", "test"),
    ("bun", "install"),
    ("bun", "add"),
    ("python3", "-m", "compileall"),
    ("python", "-m", "compileall"),
    ("python3", "-m", "pytest"),
    ("python", "-m", "pytest"),
    ("python3", "-m", "unittest"),
    ("python", "-m", "unittest"),
    ("go", "test"),
    ("go", "vet"),
    ("cargo", "test"),
    ("cargo", "check"),
    ("cargo", "clippy"),
    ("mvn", "test"),
    ("mvn", "-q", "test"),
    ("gradle", "test"),
    ("./gradlew", "test"),
    ("gradlew", "test"),
    ("composer", "test"),
    ("composer", "validate"),
    ("composer", "run-script"),
    ("bundle", "exec", "rake"),
    ("bundle", "exec", "rspec"),
    ("ruby", "-c"),
    ("dotnet", "test"),
    ("terraform", "validate"),
    ("deno", "test"),
    ("deno", "check"),
    ("cmake", "--build"),
    ("cmake", "-S"),
    ("make", "test"),
    ("swift", "test"),
    ("mix", "test"),
    ("docker", "compose", "config"),
    ("docker-compose", "config"),
    ("kubectl", "apply", "--dry-run=client"),
    ("tsc",),
    ("vite", "build"),
    ("vitest",),
    ("jest",),
    ("eslint",),
    ("prettier", "--check"),
    ("playwright", "test"),
    ("git", "status"),
    ("git", "diff"),
    ("git", "log"),
    ("git", "show"),
    ("git", "branch"),
)

_APPROVAL_COMMANDS = {"git", "npx", "pnpm", "yarn", "bun", "npm"}
_BLOCKED_COMMANDS = {"rm", "sudo", "su", "dd", "mkfs", "mount", "umount", "shutdown", "reboot", "kill", "pkill"}
_DESTRUCTIVE_GIT_ARGS = {"reset", "clean", "checkout", "restore", "rebase"}
_SHELL_CHAIN_OPERATORS = {"&&"}
_SHELL_UNSAFE_TOKENS = {";", "|", "||", "&", ">", ">>", "<", "<<", "$(", "`"}
_SAFE_READ_COMMANDS = {"pwd", "ls", "find", "cat", "head", "tail", "wc"}


def _strip_harmless_capture_redirect(command: str) -> tuple[str, bool]:
    clean = str(command or "").strip()
    next_clean = re.sub(r"\s+(?:2>&1|1>&2)\s*$", "", clean).strip()
    return next_clean, next_clean != clean


def _is_safe_cd_command(parts: list[str]) -> bool:
    if len(parts) != 2 or parts[0] != "cd":
        return False
    target = str(parts[1] or "").strip()
    if not target or target in {".", "-"}:
        return target == "."
    if target.startswith(("/", "~")):
        return False
    pure = PurePosixPath(target.replace("\\", "/"))
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        return False
    return True


def _is_safe_read_command(parts: list[str]) -> bool:
    if not parts:
        return False
    executable = Path(parts[0]).name
    if executable == "sed":
        if any(part == "-i" or part.startswith("-i") for part in parts[1:]):
            return False
    elif executable not in _SAFE_READ_COMMANDS:
        return False
    for part in parts[1:]:
        value = str(part or "").strip()
        if not value or value.startswith("-"):
            continue
        if value.startswith(("/", "~")):
            return False
        pure = PurePosixPath(value.replace("\\", "/"))
        if pure.is_absolute() or any(piece == ".." for piece in pure.parts):
            return False
    return True


def _is_project_scoped_shadcn_command(parts: list[str]) -> bool:
    if len(parts) < 2:
        return False
    executable = Path(parts[0]).name
    if executable in {"npx", "pnpm", "bunx"}:
        return any(part == "shadcn@latest" or part == "shadcn" for part in parts[1:4])
    if executable == "yarn":
        return any(part == "shadcn@latest" or part == "shadcn" for part in parts[1:5])
    return False


def _command_policy_decision_for_parts(clean: str, parts: list[str], *, access_mode: str = "safe") -> CommandPolicyDecision:
    if not parts:
        return CommandPolicyDecision(ok=False, command=clean, risk_level="blocked", reason="Command kosong.", requires_approval=True)

    executable = Path(parts[0]).name
    if executable in _BLOCKED_COMMANDS:
        return CommandPolicyDecision(ok=False, command=clean, risk_level="blocked", reason=f"Command '{executable}' diblokir oleh guarded autonomy.", requires_approval=True)

    if executable == "git" and len(parts) > 1 and parts[1] in _DESTRUCTIVE_GIT_ARGS:
        return CommandPolicyDecision(ok=False, command=clean, risk_level="approval_required", reason=f"git {parts[1]} butuh approval eksplisit.", requires_approval=True)

    tuple_parts = tuple(parts)
    if any(tuple_parts[:len(prefix)] == prefix for prefix in _SAFE_COMMAND_PREFIXES):
        return CommandPolicyDecision(ok=True, command=clean, risk_level="safe", reason="Command validasi/install project-scoped yang boleh auto-run.", requires_approval=False)

    if _is_safe_cd_command(parts):
        return CommandPolicyDecision(ok=True, command=clean, risk_level="safe", reason="cd relatif di dalam workspace boleh untuk chain command project-scoped.", requires_approval=False)

    if _is_safe_read_command(parts):
        return CommandPolicyDecision(ok=True, command=clean, risk_level="safe", reason="Command baca/inspect relatif workspace boleh auto-run.", requires_approval=False)

    if _is_project_scoped_shadcn_command(parts):
        if access_mode == "trusted":
            return CommandPolicyDecision(ok=True, command=clean, risk_level="safe", reason="Trusted Project mode: shadcn CLI project-scoped boleh auto-run dengan trace evidence.", requires_approval=False)
        return CommandPolicyDecision(ok=False, command=clean, risk_level="approval_required", reason="shadcn CLI guarded: safe mode harus meminta/menampilkan action project-scoped, bukan menganggap command sudah berjalan.", requires_approval=True)

    if access_mode == "trusted":
        if executable in {"npm", "pnpm", "yarn", "bun", "npx", "node", "python", "python3", "pip", "pip3", "go", "cargo", "mvn", "gradle", "gradlew", "composer", "bundle", "ruby", "dotnet", "terraform", "deno", "cmake", "make", "swift", "mix", "docker", "docker-compose", "kubectl", "tsx", "ts-node", "vite", "vitest", "jest", "eslint", "prettier", "playwright"}:
            return CommandPolicyDecision(ok=True, command=clean, risk_level="safe", reason="Trusted Project mode: command project-scoped boleh auto-run.", requires_approval=False)
        if executable == "git":
            return CommandPolicyDecision(ok=True, command=clean, risk_level="safe", reason="Trusted Project mode: git non-destruktif boleh auto-run.", requires_approval=False)

    if executable in _APPROVAL_COMMANDS:
        return CommandPolicyDecision(ok=False, command=clean, risk_level="approval_required", reason="Command package/git di luar allowlist safe butuh approval eksplisit.", requires_approval=True)

    return CommandPolicyDecision(ok=False, command=clean, risk_level="approval_required", reason="Command belum masuk allowlist guarded autonomy.", requires_approval=True)


def _split_safe_shell_chain(parts: list[str]) -> list[list[str]] | None:
    segments: list[list[str]] = []
    current: list[str] = []
    for part in parts:
        if part in _SHELL_CHAIN_OPERATORS:
            if not current:
                return None
            segments.append(current)
            current = []
            continue
        if part in _SHELL_UNSAFE_TOKENS or any(token in part for token in ("$(", "`")):
            return None
        current.append(part)
    if not current:
        return None
    segments.append(current)
    return segments


def _agent_access_mode_for_project(project_root: str | None) -> str:
    raw_project = str(project_root or "").strip()
    if not raw_project or raw_project == ".":
        return "safe"
    try:
        projects = supabase_list_projects(owner_id=CURRENT_USER_ID.get(), include_archived=True)
        match = next((project for project in projects if str(project.get("root") or "") == raw_project), None)
        if not match:
            return "safe"
        prefs = get_project_preferences(project_id=str(match.get("id") or ""))
        mode = str(prefs.agent_access_mode or "safe").strip().lower()
        return "trusted" if mode == "trusted" else "safe"
    except Exception:
        return "safe"


def _command_policy_decision(command: str, *, access_mode: str | None = None, project_root: str | None = None) -> CommandPolicyDecision:
    clean, _stripped_capture = _strip_harmless_capture_redirect(command)
    resolved_access_mode = str(access_mode or _agent_access_mode_for_project(project_root)).strip().lower()
    if resolved_access_mode not in {"safe", "trusted"}:
        resolved_access_mode = "safe"
    if not clean:
        return CommandPolicyDecision(ok=False, command=clean, risk_level="blocked", reason="Command kosong.", requires_approval=True)

    lowered = clean.lower()
    if any(token in lowered for token in ("curl ", "wget ", "| sh", "| bash", " > /", ">> /", " --global", " -g ")):
        return CommandPolicyDecision(
            ok=False,
            command=clean,
            risk_level="approval_required",
            reason="Command berpotensi mengunduh/menulis di luar project atau mengubah environment global.",
            requires_approval=True,
        )
    if (
        ";" in clean
        or "|" in clean
        or ">" in clean
        or "<" in clean
        or "`" in clean
        or "$(" in clean
        or re.search(r"(?<!&)&(?!&)", clean)
    ):
        return CommandPolicyDecision(
            ok=False,
            command=clean,
            risk_level="approval_required",
            reason="Command memakai operator shell yang butuh approval eksplisit.",
            requires_approval=True,
        )

    try:
        parts = shlex.split(clean)
    except ValueError as exc:
        return CommandPolicyDecision(ok=False, command=clean, risk_level="blocked", reason=f"Command tidak bisa diparse: {exc}", requires_approval=True)

    if not parts:
        return CommandPolicyDecision(ok=False, command=clean, risk_level="blocked", reason="Command kosong.", requires_approval=True)

    if any(part in _SHELL_CHAIN_OPERATORS for part in parts):
        segments = _split_safe_shell_chain(parts)
        if not segments:
            return CommandPolicyDecision(ok=False, command=clean, risk_level="approval_required", reason="Shell chain mengandung operator yang butuh approval eksplisit.", requires_approval=True)
        for segment in segments:
            decision = _command_policy_decision_for_parts(" ".join(segment), segment, access_mode=resolved_access_mode)
            if not decision.ok:
                return CommandPolicyDecision(ok=False, command=clean, risk_level=decision.risk_level, reason=f"Shell chain ditahan: {decision.reason}", requires_approval=True)
        reason = "Trusted Project mode: semua command chain project-scoped boleh auto-run." if resolved_access_mode == "trusted" else "Semua command dalam chain project-scoped dan boleh auto-run."
        return CommandPolicyDecision(ok=True, command=clean, risk_level="safe", reason=reason, requires_approval=False)

    if any(part in _SHELL_UNSAFE_TOKENS or any(token in part for token in ("$(", "`")) for part in parts):
        return CommandPolicyDecision(ok=False, command=clean, risk_level="approval_required", reason="Command memakai operator shell yang butuh approval eksplisit.", requires_approval=True)

    return _command_policy_decision_for_parts(clean, parts, access_mode=resolved_access_mode)


def _infer_validation_commands(project_dir: Path) -> list[str]:
    commands: list[str] = []
    try:
        plan = build_validation_plan(project_dir, project_root=".")
        for item in list(plan.get("commands") or []):
            if not isinstance(item, dict):
                continue
            command = str(item.get("command") or "").strip()
            if command:
                commands.append(command)
    except Exception:
        commands = []

    package_manager = _resolve_package_manager(project_dir)

    package_json = project_dir / "package.json"
    if package_json.exists() and not commands:
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
            scripts = data.get("scripts") or {}
            if isinstance(scripts, dict) and package_manager:
                _manager_name, manager_cmd = package_manager
                for name in VALIDATION_SCRIPT_NAMES:
                    if isinstance(scripts.get(name), str) and scripts.get(name, "").strip():
                        commands.append(_shell_join([*manager_cmd, "run", name]))
        except Exception:
            pass

    python_targets: list[str] = []
    api_dir = project_dir / "api"
    if api_dir.exists() and any(api_dir.rglob("*.py")):
        python_targets.append("api")
    elif any(project_dir.glob("*.py")):
        python_targets.append(".")

    if not commands:
        for target in python_targets:
            commands.append(f"python3 -m compileall {shlex.quote(target)}")

    deduped: list[str] = []
    seen: set[str] = set()
    for command in commands:
        if command in seen:
            continue
        seen.add(command)
        deduped.append(command)
    return deduped


def _managed_workspace_root() -> Path:
    import os

    base_raw = settings_mod.settings.default_workspace
    if base_raw:
        return Path(base_raw).expanduser().resolve()

    if _is_serverless_runtime():
        return Path("/tmp/.voiceide-home").resolve()

    return Path("~/.voiceide-home").expanduser().resolve()


def _managed_workspace_target() -> tuple[Path, Literal["user", "session"]]:
    base = _managed_workspace_root()
    user_id = CURRENT_USER_ID.get()
    if user_id and user_id != "voiceide-user-default":
        target = (base / "users" / user_id).resolve()
        mode: Literal["user", "session"] = "user"
    else:
        target = (base / "sessions" / CURRENT_SESSION_ID.get()).resolve()
        mode = "session"
    if base != target and base not in target.parents:
        raise HTTPException(400, "Invalid workspace root")
    return target, mode


def _identity_info() -> IdentityInfo:
    profile = _current_user_profile() or {}
    managed_path, mode = _managed_workspace_target()
    return IdentityInfo(
        user_id=CURRENT_USER_ID.get(),
        display_name=profile.get("display_name"),
        email=profile.get("email"),
        has_profile=bool(profile),
        managed_workspace_mode=mode,
        managed_workspace_path=str(managed_path),
    )


def _provision_managed_workspace() -> tuple[Path, bool]:
    target_dir, mode = _managed_workspace_target()

    created = not target_dir.exists()
    target_dir.mkdir(parents=True, exist_ok=True)

    profile = _current_user_profile() or {}
    metadata = target_dir / ".voiceide-user.json"
    metadata.write_text(
        json.dumps(
            {
                "user_id": CURRENT_USER_ID.get(),
                "display_name": profile.get("display_name"),
                "email": profile.get("email"),
                "mode": mode,
                "session_id": CURRENT_SESSION_ID.get(),
                "updated_at": int(time.time()),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return target_dir, created


# Settings endpoints
def _read_env_lines() -> list[str]:
    if not ENV_PATH.exists():
        return []
    return ENV_PATH.read_text(encoding="utf-8").splitlines(keepends=True)


def _write_env_lines(lines: list[str]) -> None:
    ENV_PATH.write_text("".join(lines).rstrip("\n") + "\n", encoding="utf-8")


def _find_env_key_index(lines: list[str], key: str) -> int | None:
    pattern = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")
    for i, line in enumerate(lines):
        match = pattern.match(line.rstrip("\n"))
        if match and match.group("key") == key:
            return i
    return None


def _quote_env_value(value: str) -> str:
    needs_quotes = (
        value != value.strip()
        or any(ch in value for ch in [" ", "#"])
        or "\t" in value
        or "\n" in value
        or '"' in value
    )
    if not needs_quotes:
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _env_set(key: str, value: str) -> None:
    if not ENV_PATH.exists():
        ENV_PATH.write_text("", encoding="utf-8")

    lines = _read_env_lines()
    idx = _find_env_key_index(lines, key)
    new_line = f"{key}={_quote_env_value(value)}\n"

    if idx is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(new_line)
    else:
        lines[idx] = new_line

    _write_env_lines(lines)


def _env_unset(key: str) -> None:
    if not ENV_PATH.exists():
        return
    lines = _read_env_lines()
    idx = _find_env_key_index(lines, key)
    if idx is None:
        return
    lines.pop(idx)
    _write_env_lines(lines)


app.include_router(build_diagnostics_router())
app.include_router(build_auth_router(session_state=_session_state, sanitize_session_id=_sanitize_session_id, sanitize_user_id=sanitize_user_id, upsert_current_user_profile=_upsert_current_user_profile))
app.include_router(build_workspace_router(
    session_state=_session_state,
    identity_info=_identity_info,
    upsert_current_user_profile=_upsert_current_user_profile,
    provision_managed_workspace=_provision_managed_workspace,
    is_serverless_runtime=_is_serverless_runtime,
    is_text_rel_path=_is_text_rel_path,
))
app.include_router(build_projects_router(session_state=_session_state, ensure_workspace=_provision_managed_workspace))
app.include_router(build_preferences_router())
app.include_router(build_settings_router(session_state=_session_state, env_set=_env_set, env_unset=_env_unset, reload_settings=_reload_settings))


def _ws() -> Path:
    p: Path | str | None = _session_state()["workspace"]
    if p is None:
        try:
            p, _created = _provision_managed_workspace()
            _session_state()["workspace"] = p
        except Exception:
            p = None
    if p is None:
        raise HTTPException(400, "Workspace not set")
    if isinstance(p, str):
        p = Path(p)
        _session_state()["workspace"] = p
    return p


app.include_router(build_assets_router(workspace_root=_ws, hydrate_hosted_project=_hydrate_hosted_project))


# ---- Runner (v0) ----
# Minimal, guarded process runner for web projects.
# Not a general shell. We only allow: npm install + npm run dev (Vite-like).

MAX_RUNNERS_PER_SESSION = 3
RUNNER_STALE_SECONDS = 60 * 60 * 6


def _runners() -> dict:
    return _session_state()["runners"]


def _terminate_runner_record(r: dict) -> None:
    proc = r.get("proc")
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass


def _cleanup_runners() -> None:
    now = time.time()
    to_remove: list[str] = []
    for rid, r in _runners().items():
        proc = r.get("proc")
        started = float(r.get("started") or 0)
        exited = bool(proc and proc.poll() is not None)
        stale = started and (now - started > RUNNER_STALE_SECONDS)
        if stale and proc and proc.poll() is None:
            _terminate_runner_record(r)
            exited = True
        if exited or stale:
            to_remove.append(rid)
    for rid in to_remove:
        _runners().pop(rid, None)


def _ensure_runner_capacity(target_project_root: str) -> None:
    _cleanup_runners()

    same_project = [
        (rid, r) for rid, r in _runners().items()
        if str(r.get("project_root") or "") == target_project_root
    ]
    for rid, r in same_project:
        _terminate_runner_record(r)
        _runners().pop(rid, None)

    active_count = 0
    for r in _runners().values():
        proc = r.get("proc")
        if proc and proc.poll() is None:
            active_count += 1
    if active_count >= MAX_RUNNERS_PER_SESSION:
        raise HTTPException(400, f"Too many active runners in this session (max {MAX_RUNNERS_PER_SESSION})")


def _is_port_in_use(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0


def _next_port(start: int = 8800, end: int = 8899) -> int:
    _cleanup_runners()
    used = {
        int(r.get("port"))
        for sessions_state in STATE["sessions"].values()
        for r in (sessions_state.get("runners") or {}).values()
        if r.get("port") is not None
    }
    for p in range(start, end + 1):
        if p not in used and not _is_port_in_use(p):
            return p
    raise HTTPException(400, "No free ports")


class DetectedProject(BaseModel):
    root: str
    name: str
    has_dev: bool


@app.get("/api/run/detect")
def run_detect():
    base = _ws()
    if _hosted_project_files_enabled():
        return {"ok": True, "projects": []}
    out: list[dict] = []
    seen = set()

    # Find package.json up to depth 4
    for pj in base.rglob("package.json"):
        try:
            rel = str(pj.parent.relative_to(base)) or "."
        except Exception:
            continue
        if rel.startswith("node_modules") or "/node_modules" in rel:
            continue
        if rel in seen:
            continue
        seen.add(rel)

        try:
            import json

            data = json.loads(pj.read_text(encoding="utf-8"))
            name = str(data.get("name") or pj.parent.name)
            scripts = data.get("scripts") or {}
            has_dev = isinstance(scripts, dict) and ("dev" in scripts)
        except Exception:
            continue

        out.append({"root": rel, "name": name, "has_dev": bool(has_dev)})

    # Also detect folders with only index.html (static preview)
    for idx in base.rglob("index.html"):
        try:
            rel = str(idx.parent.relative_to(base)) or "."
        except Exception:
            continue
        if rel in seen:
            continue
        seen.add(rel)

        out.append({"root": rel, "name": idx.parent.name, "has_dev": True})

    # prefer root-level first
    out.sort(key=lambda x: (x["root"] != ".", x["root"]))
    return {"ok": True, "projects": out}


class RunStartReq(BaseModel):
    project_root: str
    port: int | None = None


@app.post("/api/run/start")
def run_start(req: RunStartReq, request: Request):
    import subprocess
    import threading
    import time
    import uuid
    import sys

    if os.getenv("VERCEL"):
        raise HTTPException(400, "Embedded preview is not available in this deployment.")

    base = _ws()
    _hydrate_hosted_project(base, req.project_root)
    proj = safe_join(base, req.project_root)
    if not proj.exists() or not proj.is_dir():
        raise HTTPException(400, "project_root must exist inside workspace")

    _ensure_runner_capacity(req.project_root)

    port = req.port or _next_port()
    rid = uuid.uuid4().hex[:16]
    logs: list[str] = []

    def pump(proc):
        assert proc.stdout
        for line in proc.stdout:
            logs.append(line.rstrip("\n"))
            if len(logs) > 2000:
                del logs[:500]

    # Check if this is a static project (no package.json or no runnable preview script)
    pj_path = proj / "package.json"
    is_static = not pj_path.exists()
    package_data: dict = {}
    preview_launch_kind = "static"
    preview_script: str | None = None

    if not is_static:
        try:
            data = json.loads(pj_path.read_text(encoding="utf-8"))
            package_data = data if isinstance(data, dict) else {}
            preview_launch_kind, preview_script = _preview_launch_kind(package_data)
            is_static = preview_launch_kind == "static"
        except Exception:
            is_static = True

    if is_static:
        # Serve static files with Python http.server
        logs.append(f"$ {sys.executable} -m http.server {port}")
        proc = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
            cwd=str(proj),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    else:
        package_manager = _resolve_package_manager(proj)
        if not package_manager:
            raise HTTPException(
                400,
                "This deployment can edit the project, but cannot run the JavaScript preview because npm/pnpm/yarn/bun is not installed on the API host.",
            )

        manager_name, manager_cmd = package_manager
        dependencies_ready = _ensure_preview_dependencies_ready(proj, package_data, logs)
        if not dependencies_ready:
            install_cmd = _package_install_command(manager_name, manager_cmd)
            logs.append(f"$ {_shell_join(install_cmd)}")
            try:
                install = subprocess.run(
                    install_cmd,
                    cwd=str(proj),
                    capture_output=True,
                    text=True,
                    timeout=_preview_install_timeout_seconds(),
                )
            except subprocess.TimeoutExpired as exc:
                if exc.stdout:
                    logs.extend([l for l in str(exc.stdout).splitlines() if l.strip()])
                if exc.stderr:
                    logs.extend([l for l in str(exc.stderr).splitlines() if l.strip()])
                tail = "\n".join((logs or [])[-120:])
                raise HTTPException(
                    400,
                    f"Install timed out after {_preview_install_timeout_seconds()}s\n\n--- package manager output (tail) ---\n{tail}",
                )
            if manager_name != "npm":
                logs.append(f"[runtime] Using {manager_name} because npm is not available.")
            if install.stdout:
                logs.extend([l for l in install.stdout.splitlines() if l.strip()])
            if install.stderr:
                logs.extend([l for l in install.stderr.splitlines() if l.strip()])
            if install.returncode != 0:
                tail = "\n".join((logs or [])[-120:])
                raise HTTPException(400, f"Install failed\n\n--- package manager output (tail) ---\n{tail}")

        # strictPort so we know the port; if it's taken, user can run again (we'll pick a new port)
        if preview_launch_kind == "vite":
            cmd = _package_vite_command(proj, manager_name, manager_cmd, port)
        else:
            cmd = _package_run_script_command(manager_cmd, preview_script or "dev", port)
        logs.append(f"$ {_shell_join(cmd)}")
        proc = subprocess.Popen(cmd, cwd=str(proj), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

    t = threading.Thread(target=pump, args=(proc,), daemon=True)
    t.start()

    _runners()[rid] = {
        "proc": proc,
        "logs": logs,
        "started": time.time(),
        "cwd": str(proj),
        "port": port,
        "project_root": req.project_root,
        "session_id": CURRENT_SESSION_ID.get(),
    }
    direct_url = f"http://localhost:{port}"
    preview_url = direct_url
    if _is_serverless_runtime():
        preview_url = str(request.url_for("run_proxy", run_id=rid))
    return {"ok": True, "id": rid, "pid": proc.pid, "url": preview_url, "direct_url": direct_url, "project_root": req.project_root}


def _rewrite_preview_text(text: str, *, run_id: str) -> str:
    prefix = f"/api/run/proxy/{run_id}/"
    replacements = [
        ('="/', f'="{prefix}'),
        ("='/", f"='{prefix}"),
        ('"/@', f'"{prefix}@'),
        ("'/@", f"'{prefix}@"),
        ('"/src/', f'"{prefix}src/'),
        ("'/src/", f"'{prefix}src/"),
        ('"/node_modules/', f'"{prefix}node_modules/'),
        ("'/node_modules/", f"'{prefix}node_modules/"),
        ('"/assets/', f'"{prefix}assets/'),
        ("'/assets/", f"'{prefix}assets/"),
        ('from "/', f'from "{prefix}'),
        ("from '/", f"from '{prefix}"),
        ('import("/', f'import("{prefix}'),
        ("import('/", f"import('{prefix}"),
    ]
    out = text
    for old, new in replacements:
        out = out.replace(old, new)
    return out


@app.get("/api/run/proxy/{run_id}", name="run_proxy")
@app.get("/api/run/proxy/{run_id}/{path:path}", name="run_proxy_path")
def run_proxy(run_id: str, path: str = "", request: Request = None):
    r = _runners().get(run_id)
    if not r:
        raise HTTPException(404, "preview runner not found")
    proc = r.get("proc")
    if not proc or proc.poll() is not None:
        raise HTTPException(410, "preview runner is not running")
    port = r.get("port")
    if not isinstance(port, int):
        raise HTTPException(500, "preview runner has no port")
    clean_path = str(path or "").lstrip("/")
    target = f"http://127.0.0.1:{port}/{clean_path}"
    query = str(request.url.query or "") if request else ""
    if query:
        target = f"{target}?{query}"
    try:
        upstream = URLRequest(target, headers={"User-Agent": "ApporaPreviewProxy/1.0"})
        with urlopen(upstream, timeout=20) as resp:  # nosec B310 - internal runner proxy
            body = resp.read()
            content_type = resp.headers.get("content-type") or "application/octet-stream"
            headers = {"Cache-Control": "no-store"}
            if any(kind in content_type for kind in ("text/html", "javascript", "text/css")):
                text = body.decode("utf-8", errors="replace")
                body = _rewrite_preview_text(text, run_id=run_id).encode("utf-8")
            return Response(content=body, status_code=resp.status, media_type=content_type.split(";", 1)[0], headers=headers)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Preview proxy failed: {exc}")


@app.get("/api/run/list")
def run_list():
    _cleanup_runners()
    items = []
    for rid, r in _runners().items():
        proc = r.get("proc")
        running = bool(proc and proc.poll() is None)
        items.append({
            "id": rid,
            "project_root": r.get("project_root"),
            "port": r.get("port"),
            "url": f"http://localhost:{r.get('port')}",
            "pid": getattr(proc, "pid", None),
            "running": running,
        })
    return {"ok": True, "items": items, "limits": {"max_runners_per_session": MAX_RUNNERS_PER_SESSION}}


@app.get("/api/run/logs")
def run_logs(id: str, limit: int = 300):
    _cleanup_runners()
    r = _runners().get(id)
    if not r:
        raise HTTPException(404, "runner not found")
    logs = r.get("logs") or []
    proc = r.get("proc")
    return {"ok": True, "id": id, "pid": getattr(proc, "pid", None), "running": bool(proc and proc.poll() is None), "logs": logs[-limit:]}


@app.post("/api/run/stop")
def run_stop(id: str):
    r = _runners().get(id)
    if not r:
        raise HTTPException(404, "runner not found")
    _terminate_runner_record(r)
    return {"ok": True}


@app.post("/api/run/close")
def run_close(id: str):
    # stop + remove
    r = _runners().get(id)
    if not r:
        return {"ok": True}
    _terminate_runner_record(r)
    _runners().pop(id, None)
    return {"ok": True}


class ListReq(BaseModel):
    path: str = "."


@app.post("/api/fs/list")
def fs_list(req: ListReq):
    root = _ws()
    split = _split_project_path(req.path)
    if split:
        _hydrate_hosted_project(root, split[0])
    return {"items": list_tree(_ws(), req.path)}


@app.get("/api/projects/export")
def export_project_zip(project_root: str):
    root = _ws()
    project = str(project_root or ".").strip().strip("/") or "."
    if project == ".":
        raise HTTPException(400, "Choose a project before exporting")
    _hydrate_hosted_project(root, project)
    project_dir = safe_join(root, project)
    if not project_dir.exists() or not project_dir.is_dir():
        raise HTTPException(404, "Project not found")

    files = _iter_project_export_files(project_dir)
    if not files:
        raise HTTPException(404, "No exportable files found")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            rel = PurePosixPath(str(path.relative_to(project_dir))).as_posix()
            archive.write(path, rel)
    buffer.seek(0)

    filename = f"{_safe_export_filename(project)}.zip"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
    }
    return Response(content=buffer.getvalue(), media_type="application/zip", headers=headers)


class ReadReq(BaseModel):
    path: str


@app.post("/api/fs/read")
def fs_read(req: ReadReq):
    try:
        root = _ws()
        split = _split_project_path(req.path)
        if split:
            _hydrate_hosted_project(root, split[0])
        return {"content": read_text(root, req.path)}
    except FileNotFoundError:
        raise HTTPException(404, "Not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


class WriteReq(BaseModel):
    path: str
    content: str
    expected_sha256: str | None = None  # reserved for optimistic locking


@app.post("/api/fs/write")
def fs_write(req: WriteReq):
    write_text(_ws(), req.path, req.content)
    _persist_hosted_file(req.path, req.content)
    return {"ok": True}


class WriteOp(BaseModel):
    path: str
    content: str
    expected_sha256: str | None = None
    expected_exists: bool | None = None


class ApplyManyReq(BaseModel):
    ops: list[WriteOp]
    overwrite: bool = False


class AgentHarnessApplyChange(BaseModel):
    path: str
    content: str
    diff: str | None = None
    expected_sha256: str | None = None
    expected_exists: bool | None = None
    old_sha256: str | None = None
    old_exists: bool | None = None


class AgentHarnessApplyReq(BaseModel):
    project_root: str = "."
    label: str = "Applying"
    changes: list[AgentHarnessApplyChange]


def _preflight_apply_many(root: Path, req: ApplyManyReq) -> dict:
    conflicts: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    for op in req.ops:
        if not op.path.strip() or ".." in op.path.split("/"):
            conflicts.append({"path": op.path, "reason": "invalid_path", "detail": f"{op.path} is not a safe project-relative path"})
            continue
        try:
            p = safe_join(root, op.path)
        except ValueError as exc:
            conflicts.append({"path": op.path, "reason": "invalid_path", "detail": str(exc)})
            continue
        if op.expected_exists is not None:
            exists = p.exists()
            if op.expected_exists is False and exists:
                conflicts.append({"path": op.path, "reason": "expected_absent", "detail": f"{op.path} changed: expected file to be absent"})
                continue
            if op.expected_exists is True and not exists:
                conflicts.append({"path": op.path, "reason": "expected_present", "detail": f"{op.path} changed: expected file to exist"})
                continue
            if exists and op.expected_sha256:
                try:
                    current_hash = _sha256_text(p.read_text(encoding="utf-8"))
                except UnicodeDecodeError:
                    conflicts.append({"path": op.path, "reason": "non_utf8", "detail": f"{op.path} changed: current file is not UTF-8 text"})
                    continue
                if current_hash != op.expected_sha256:
                    conflicts.append({"path": op.path, "reason": "stale_hash", "detail": f"{op.path} changed since agent prepared the patch"})
                    continue
        elif p.exists() and not req.overwrite:
            conflicts.append({"path": op.path, "reason": "exists", "detail": f"{op.path} already exists"})

        if not isinstance(op.content, str) or op.content == "":
            warnings.append({"path": op.path, "reason": "empty_content", "detail": f"{op.path} would be written empty"})

    return {
        "ok": not conflicts,
        "count": len(req.ops),
        "conflicts": conflicts,
        "warnings": warnings,
    }


@app.post("/api/fs/apply_many/preflight")
def fs_apply_many_preflight(req: ApplyManyReq):
    return _preflight_apply_many(_ws(), req)


@app.post("/api/fs/apply_many")
def fs_apply_many(req: ApplyManyReq):
    root = _ws()
    preflight = _preflight_apply_many(root, req)
    conflicts = list(preflight.get("conflicts") or [])
    if conflicts:
        details = [str(item.get("detail") or item.get("path") or "") for item in conflicts if isinstance(item, dict)]
        raise HTTPException(409, {"message": f"Conflicts: {', '.join(details[:20])}", **preflight})

    for op in req.ops:
        write_text(root, op.path, op.content)
        _persist_hosted_file(op.path, op.content)

    return {"ok": True, "count": len(req.ops)}


def _agent_checkpoint_path(project_root: str) -> str:
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime())
    suffix = f".voiceide/checkpoints/{stamp}-{uuid.uuid4().hex[:8]}.json"
    return f"{project_root}/{suffix}" if project_root and project_root != "." else suffix


@app.post("/api/agent/harness/apply")
def agent_harness_apply(req: AgentHarnessApplyReq):
    root = _ws()
    project_root = str(req.project_root or ".").strip().strip("/") or "."
    _hydrate_hosted_project(root, project_root)

    checkpoint_files: list[dict[str, object]] = []
    ops: list[WriteOp] = []
    for change in req.changes:
        path = str(change.path or "").strip().lstrip("/")
        content = change.content
        if not path:
            continue
        try:
            target = safe_join(root, path)
            old_exists = target.exists()
            previous_content = target.read_text(encoding="utf-8") if old_exists else None
        except UnicodeDecodeError:
            previous_content = None
            old_exists = True
        old_sha = _sha256_text(previous_content or "")
        expected_sha = change.expected_sha256 or change.old_sha256
        expected_exists = change.expected_exists if change.expected_exists is not None else change.old_exists
        checkpoint_files.append({
            "path": path,
            "previous_content": previous_content,
            "patch": change.diff or "",
            "old_sha256": expected_sha or old_sha,
            "new_sha256": _sha256_text(content),
            "old_exists": old_exists if expected_exists is None else expected_exists,
        })
        ops.append(WriteOp(path=path, content=content, expected_sha256=expected_sha, expected_exists=expected_exists))

    checkpoint_path = _agent_checkpoint_path(project_root)
    checkpoint = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "project_root": project_root,
        "apply_mode": "backend-harness",
        "label": req.label,
        "files": checkpoint_files,
    }
    all_ops = [WriteOp(path=checkpoint_path, content=json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n"), *ops]
    preflight = _preflight_apply_many(root, ApplyManyReq(ops=all_ops, overwrite=True))
    conflicts = list(preflight.get("conflicts") or [])
    if conflicts:
        return {"ok": False, "applied": False, "checkpoint_path": checkpoint_path, **preflight}

    for op in all_ops:
        write_text(root, op.path, op.content)
        _persist_hosted_file(op.path, op.content)

    return {
        "ok": True,
        "applied": True,
        "count": len(ops),
        "paths": [op.path for op in ops],
        "checkpoint_path": checkpoint_path,
        "warnings": list(preflight.get("warnings") or []),
    }


class RestoreCheckpointReq(BaseModel):
    path: str


@app.get("/api/checkpoints")
def list_checkpoints(project_root: str = "."):
    root = _ws()
    base_rel = f"{project_root}/.voiceide/checkpoints" if project_root and project_root != "." else ".voiceide/checkpoints"
    base = safe_join(root, base_rel)
    if not base.exists() or not base.is_dir():
        return {"ok": True, "items": []}
    items = []
    for path in sorted(base.glob("*.json"), key=lambda item: item.name, reverse=True)[:50]:
        try:
            rel = str(path.relative_to(root))
            items.append({"path": rel, "name": path.name, "updated_at": int(path.stat().st_mtime)})
        except Exception:
            continue
    return {"ok": True, "items": items}


@app.post("/api/checkpoints/restore")
def restore_checkpoint(req: RestoreCheckpointReq):
    return _restore_checkpoint_path(req.path)


def _restore_checkpoint_path(path: str) -> dict[str, object]:
    root = _ws()
    checkpoint_path = safe_join(root, path)
    if not checkpoint_path.exists() or checkpoint_path.suffix.lower() != ".json":
        raise HTTPException(404, "Checkpoint not found")
    try:
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except Exception:
        raise HTTPException(400, "Checkpoint is not readable")
    files = payload.get("files")
    if not isinstance(files, list):
        raise HTTPException(400, "Checkpoint has no file list")

    restored = 0
    skipped = 0
    for item in files:
        if not isinstance(item, dict):
            skipped += 1
            continue
        rel = str(item.get("path") or "").strip().lstrip("/")
        previous = item.get("previous_content")
        if not rel or ".." in rel.split("/"):
            skipped += 1
            continue
        if previous is None:
            target = safe_join(root, rel)
            if target.exists() and target.is_file():
                target.unlink()
                _delete_hosted_file(rel)
                restored += 1
            continue
        if not isinstance(previous, str):
            skipped += 1
            continue
        write_text(root, rel, previous)
        _persist_hosted_file(rel, previous)
        restored += 1

    return {"ok": True, "restored": restored, "skipped": skipped}


def _snapshot_project_files(project_dir: Path, rel_paths: list[str]) -> dict[str, str | None]:
    snapshots: dict[str, str | None] = {}
    for raw in rel_paths:
        rel = str(raw or "").strip().lstrip("/")
        if not rel or ".." in rel.split("/") or rel in snapshots:
            continue
        target = (project_dir / rel).resolve()
        try:
            if project_dir != target and project_dir not in target.parents:
                continue
            snapshots[rel] = target.read_text(encoding="utf-8") if target.exists() and target.is_file() else None
        except Exception:
            continue
    return snapshots


def _restore_project_file_snapshots(project_dir: Path, snapshots: dict[str, str | None]) -> list[str]:
    restored: list[str] = []
    for rel, content in snapshots.items():
        target = (project_dir / rel).resolve()
        try:
            if project_dir != target and project_dir not in target.parents:
                continue
            if content is None:
                if target.exists() and target.is_file():
                    target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            restored.append(rel)
        except Exception:
            continue
    return restored


class DiffReq(BaseModel):
    path: str
    new_content: str


@app.post("/api/fs/diff")
def fs_diff(req: DiffReq):
    root = _ws()
    split = _split_project_path(req.path)
    if split:
        _hydrate_hosted_project(root, split[0])
    old = read_text(root, req.path)
    d = diff_text(old, req.new_content, filename=req.path)
    return {"diff": d}


class PreviewAuditReq(BaseModel):
    preview_url: str
    attempts: int = 3
    max_excerpt_chars: int = 800
    project_root: str = "."
    mode: Literal["auto", "html", "browser"] = "auto"


@app.post("/api/preview/audit")
def preview_audit(req: PreviewAuditReq):
    preview_url = _normalize_preview_url(req.preview_url)
    max_excerpt_chars = max(200, min(req.max_excerpt_chars, 4000))
    project_root = (req.project_root or ".").strip() or "."
    _hydrate_hosted_project(_ws(), project_root)
    project_dir = safe_join(_ws(), project_root)
    warnings: list[str] = []
    project_signals = _scan_project_quality_signals(project_dir) if project_dir.exists() else {}
    if project_dir.exists():
        project_signals["project_dir"] = str(project_dir)

    requested_mode = str(req.mode or "auto").strip().lower()
    if requested_mode not in {"auto", "html", "browser"}:
        requested_mode = "auto"

    if requested_mode != "html":
        browser_audit, browser_warning = _run_agent_browser_preview_audit(
            preview_url,
            project_dir,
            max_excerpt_chars=max_excerpt_chars,
            project_signals=project_signals,
        )
        if browser_audit:
            return browser_audit
        if browser_warning:
            warnings.append(browser_warning)

        browser_audit, browser_warning = _run_playwright_preview_audit(
            preview_url,
            project_dir,
            max_excerpt_chars=max_excerpt_chars,
            project_signals=project_signals,
        )
        if browser_audit:
            if warnings:
                browser_audit["runtime_warnings"] = [*browser_audit.get("runtime_warnings", []), *warnings]
            return browser_audit
        if browser_warning:
            warnings.append(browser_warning)

    html = _fetch_preview_html(preview_url, attempts=max(1, min(req.attempts, 5)))
    audit = _audit_preview_html(preview_url, html, max_excerpt_chars=max_excerpt_chars, project_signals=project_signals)
    if warnings:
        audit["runtime_warnings"] = [*audit.get("runtime_warnings", []), *warnings]
    return audit


class ProjectValidateReq(BaseModel):
    project_root: str = "."
    max_commands: int = 4


class SupabaseRagSyncReq(BaseModel):
    project_root: str = "."


@app.get("/api/supabase/rag/status")
def supabase_rag_status(project_root: str = "."):
    ws_root = _ws()
    proj_root = (project_root or ".").strip() or "."
    _hydrate_hosted_project(ws_root, proj_root)
    project_dir = safe_join(ws_root, proj_root)
    supabase_enabled = has_supabase()
    frontend_auth_ready = bool(getattr(settings_mod.settings, "supabase_frontend_ready", False))
    missing_env = list(getattr(settings_mod.settings, "supabase_missing_env", []) or [])
    table_status = get_agent_memory_chunks_table_status(refresh=True) if supabase_enabled else "unconfigured"
    summary = get_agent_memory_chunks_summary(owner_id=CURRENT_USER_ID.get(), project_root=proj_root, limit=1000) if supabase_enabled and table_status == "ready" else None

    warning = None
    if frontend_auth_ready and not supabase_enabled:
        warning = "Frontend Supabase udah siap, tapi backend belum punya SUPABASE_SERVICE_ROLE_KEY. Login bisa jalan, tapi RAG sync dan persistence backend belum live."
    elif not supabase_enabled:
        warning = "Setup Supabase belum lengkap di backend ini."
    elif table_status == "missing":
        warning = "Tabel public.agent_memory_chunks belum ada. Jalankan docs/supabase/agent-rag.sql di Supabase SQL editor dulu."
    elif table_status == "error":
        warning = "Backend belum bisa verifikasi agent_memory_chunks sekarang, jadi RAG masih fallback lokal."

    return {
        "ok": True,
        "project_root": proj_root,
        "project_exists": project_dir.exists() and project_dir.is_dir(),
        "supabase_enabled": supabase_enabled,
        "frontend_auth_ready": frontend_auth_ready,
        "missing_env": missing_env,
        "table_status": table_status,
        "live_ready": bool(supabase_enabled and table_status == "ready"),
        "warning": warning,
        "bootstrap_sql_path": "docs/supabase/agent-rag.sql",
        "summary": summary,
    }


@app.post("/api/supabase/rag/sync")
def supabase_rag_sync(req: SupabaseRagSyncReq):
    ws_root = _ws()
    proj_root = (req.project_root or ".").strip() or "."
    _hydrate_hosted_project(ws_root, proj_root)
    project_dir = safe_join(ws_root, proj_root)
    if not project_dir.exists() or not project_dir.is_dir():
        raise HTTPException(400, "project_root must exist inside workspace")

    sync_result = sync_project_docs_to_supabase(project_dir, project_root=proj_root)
    summary = get_agent_memory_chunks_summary(owner_id=CURRENT_USER_ID.get(), project_root=proj_root, limit=1000) if sync_result.get("table_status") == "ready" else None
    return {
        "ok": True,
        **sync_result,
        "live_ready": bool(sync_result.get("supabase_configured") and sync_result.get("table_status") == "ready" and (summary or sync_result.get("synced"))),
        "summary": summary,
        "bootstrap_sql_path": "docs/supabase/agent-rag.sql",
    }


@app.post("/api/project/validate")
def project_validate(req: ProjectValidateReq):
    ws_root = _ws()
    project_root = (req.project_root or ".").strip() or "."
    _hydrate_hosted_project(ws_root, project_root)
    project_dir = safe_join(ws_root, project_root)
    if not project_dir.exists() or not project_dir.is_dir():
        raise HTTPException(400, "project_root must exist inside workspace")

    commands = _infer_validation_commands(project_dir)[: max(1, min(req.max_commands, 8))]
    results: list[dict] = []
    for command in commands:
        result = _run_shell_command(command, project_dir)
        results.append({"command": command, **result})

    return {
        "ok": all(item.get("ok") for item in results) if results else True,
        "project_root": project_root,
        "commands": commands,
        "results": results,
        "ran": len(results),
        "passed": sum(1 for item in results if item.get("ok")),
        "failed": sum(1 for item in results if not item.get("ok")),
    }


# Agent endpoint (v0): suggest patch for active file, return diff.
class AgentReq(BaseModel):
    input: str
    mode: Literal["type", "voice"] = "type"
    active_file: str | None = None
    selection: str | None = None
    current_content: str | None = None
    open_files: list[str] | None = None
    project_root: str | None = None
    build_mode: Literal["full-agent", "hybrid"] | None = None
    preview_url: str | None = None
    editor_status: str | None = None
    asset_paths: list[str] | None = None
    asset_aliases: dict[str, str] | None = None
    stream: bool = False
    background: bool = False
    auto_execute: bool = False


class AgentWorkerRunReq(BaseModel):
    job_id: str | None = None
    limit: int = 1


@app.get("/api/agent/capabilities")
def agent_capabilities(project_root: str = ".", include_live_tools: bool = False):
    ws_root = _ws()
    proj_root = (project_root or ".").strip() or "."
    _hydrate_hosted_project(ws_root, proj_root)
    project_dir = safe_join(ws_root, proj_root)
    servers = discover_mcp_servers(ws_root, project_dir) if project_dir.exists() else []
    tool_catalog = list_mcp_tools(ws_root, project_dir, refresh=False) if include_live_tools and servers else {}
    memory_overview = get_agent_memory_overview(ws_root, project_root=proj_root)
    stack = detect_project_stack(project_dir) if project_dir.exists() else None
    node_runtime = bool(_resolve_node_binary())
    agent_browser_ready = bool(_resolve_agent_browser_binary())
    playwright_audit_ready = bool(project_dir.exists() and _playwright_preview_audit_ready(project_dir))
    browser_audit_ready = bool(project_dir.exists() and _browser_preview_audit_ready(project_dir))
    preview_audit_backend = _browser_preview_audit_backend(project_dir) if project_dir.exists() else "html"
    supabase_enabled = has_supabase()
    friendly_free_tier = bool(getattr(settings_mod.settings, "friendly_free_tier_mode", True))
    context_budget = int(getattr(settings_mod.settings, "agent_context_char_budget", 48_000 if friendly_free_tier else 140_000) or 48_000)
    supabase_rag_status = get_agent_memory_chunks_table_status() if supabase_enabled else "unconfigured"
    supabase_rag_ready = supabase_rag_status == "ready"
    memory_backend = "supabase-hash-vector-chunks" if supabase_rag_ready else "local-hash-vector-chunks"
    supabase_warning = None
    if supabase_rag_status == "missing":
        supabase_warning = "Supabase udah dikonfigurasi, tapi tabel public.agent_memory_chunks belum dibuat. Jalankan docs/supabase/agent-rag.sql dulu."
    elif supabase_rag_status == "error":
        supabase_warning = "Supabase RAG belum bisa diverifikasi dari backend ini, jadi retrieval masih fallback ke chunk lokal."
    return {
        "ok": True,
        "runtime": "appora-linear-runtime-v2",
        "glossary": {
            "tools": "Tools are callable interfaces the agent is allowed to invoke to do work that cannot be reliably done with generative text alone (e.g., read/search repo, call external systems).",
            "mcp": "MCP (Model Context Protocol) is an interoperability layer to standardize how the agent connects to external data sources and tools. MCP servers expose tools, but MCP itself is not a tool.",
            "skills": "Skills are higher-level workflow abstractions: curated instructions + prompting + decision logic + (optionally) one or more tools/MCP calls, to keep complex agentic work consistent, auditable, and scoped.",
        },
        "agent": {
            "name": "Appora Agent",
            "vibe": "powerful coding agent",
            "modes": {
                "hybrid": "Workspace editor-first layout with the full Appora Agent runtime.",
                "full-agent": "Full Preview layout with the same Appora Agent and a larger preview surface.",
            },
            "same_capabilities": True,
        },
        "supports": {
            "linear_runtime": True,
            "run_controller": True,
            "run_ledger": True,
            "runtime_hooks": True,
            "read_only_scout": True,
            "short_term_memory_rag": True,
            "project_scoped_short_memory": True,
            "long_term_memory_rag": True,
            "vector_memory_retrieval": True,
            "skill_registry": True,
            "mcp_registry": True,
            "mcp_tool_execution": True,
            "autonomous_mcp_loop": True,
            "deep_work_preflight": True,
            "repo_symbol_tools": True,
            "route_analysis_tool": True,
            "quality_scan_tool": True,
            "interaction_intent_detection": True,
            "command_conversation_boundary": True,
            "read_only_inspection_boundary": True,
            "supabase_memory_backend": supabase_enabled,
            "supabase_rag_ready": supabase_rag_ready,
            "component_library_awareness": True,
            "headless_browser_runtime": browser_audit_ready,
            "agent_browser_preview_audit": agent_browser_ready,
            "playwright_preview_audit": playwright_audit_ready,
            "webcontainer_runtime": False,
            "browser_dom_audit": browser_audit_ready,
            "browser_visual_evidence": browser_audit_ready,
            "browser_screenshot_evidence": agent_browser_ready,
            "preview_quality_checks": True,
            "preview_audit_mode": preview_audit_backend,
            "tool_actions": ["shell", "mcp", "tool"],
            "streaming_transport": True,
            "native_provider_token_streaming": True,
            "friendly_free_tier_mode": friendly_free_tier,
            "context_budget_chars": context_budget,
            "provider_fallback_routing": True,
        },
        "shell_policy": {
            "auto_safe": APPORA_AUTO_SAFE_SHELL_COMMANDS,
            "approval_or_blocked": APPORA_BLOCKED_OR_APPROVAL_SHELL_COMMANDS,
            "principle": "Agent should request project-scoped shell actions when useful; backend guarded autonomy decides allow/block and reports evidence.",
        },
        "boundaries": {
            "project_root": proj_root,
            "memory_store": ".voiceide/agent-memory",
            "custom_skills_dir": [".voiceide/skills", f"{proj_root}/.voiceide/skills" if proj_root != "." else ".voiceide/skills"],
            "mcp_config_candidates": [".voiceide/mcp.json", f"{proj_root}/.voiceide/mcp.json" if proj_root != "." else ".voiceide/mcp.json", f"{proj_root}/mcp.json" if proj_root != "." else "mcp.json"],
            "supabase_rag_table": "agent_memory_chunks" if supabase_enabled else None,
            "mcp_loop_budget": 1 if friendly_free_tier else 2,
            "local_tool_names": [tool.name for tool in list_local_tools()],
            "free_tier_call_budget": {
                "conversation": 1,
                "inspection": 2,
                "build": 1,
                "build_after_failed_validation": 2,
            } if friendly_free_tier else None,
        },
        "memory": {
            "session_entries": memory_overview.session_entries,
            "project_entries": memory_overview.project_entries,
            "latest_session_ts": memory_overview.latest_session_ts,
            "latest_project_ts": memory_overview.latest_project_ts,
            "has_project_profile": memory_overview.has_project_profile,
            "project_profile_updated_at": memory_overview.project_profile_updated_at,
            "retrieval_backend": memory_backend,
            "supabase_rag_status": supabase_rag_status,
            "supabase_warning": supabase_warning,
        },
        "stack": {
            "component_libraries": list(stack.component_libraries) if stack else [],
            "headless_browser": bool(stack.has_headless_browser) if stack else False,
            "playwright": bool(stack.has_playwright) if stack else False,
            "webcontainer": bool(stack.has_webcontainer) if stack else False,
            "node_runtime": node_runtime,
            "agent_browser": agent_browser_ready,
            "preview_audit_mode": preview_audit_backend,
        },
        "local_tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in list_local_tools()
        ],
        "discovered_mcp_servers": [
            {
                "name": server.name,
                "transport": server.transport,
                "target": server.target,
                "tools": server.tools,
                "source": server.source,
                "live_tools": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "input_schema": tool.input_schema,
                    }
                    for tool in (tool_catalog.get(server.name) or [])[:12]
                ],
            }
            for server in servers
        ],
    }


# Project builder endpoint (v0): scaffold a new app from scratch.
class ScaffoldReq(BaseModel):
    name: str
    goal: str
    ref_url: str | None = None


class PrdReq(BaseModel):
    name: str
    goal: str
    ref_url: str | None = None


class ScaffoldOp(BaseModel):
    path: str
    content: str


class ScaffoldResp(BaseModel):
    spoken: str
    log: str
    project_root: str
    ops: list[ScaffoldOp]


@app.post("/api/agent/scaffold", response_model=ScaffoldResp)
def scaffold(req: ScaffoldReq):
    # kept for backward compatibility; UI no longer uses "Create App".
    _ws()
    from .agent import scaffold_webapp

    try:
        # Block (queue) scaffold requests instead of failing fast; user prefers waiting over 429 spam.
        with SCAFFOLD_LOCK:
            res = scaffold_webapp(name=req.name, goal=req.goal, ref_url=req.ref_url)

        return ScaffoldResp(
            spoken=res.spoken,
            log=res.log,
            project_root=res.project_root,
            ops=[ScaffoldOp(path=o.path, content=o.content) for o in res.ops],
        )
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/agent/prd")
def prd(req: PrdReq):
    """Generate a Product Requirements Document only (no code)."""
    _ws()
    from .agent import generate_prd

    try:
        with SCAFFOLD_LOCK:
            out = generate_prd(name=req.name, goal=req.goal, ref_url=req.ref_url)
        if not out.get("prd_markdown"):
            raise RuntimeError("LLM returned empty PRD")
        return {"ok": True, **out}
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.get("/api/agent/jobs/{job_id}")
def agent_job_status(job_id: str):
    owner_id = CURRENT_USER_ID.get()
    local = _local_agent_jobs().get(job_id)
    if isinstance(local, dict):
        job = {key: value for key, value in local.items() if key != "events"}
        return {"ok": True, "job": job, "source": "session"}
    remote = get_agent_job(owner_id=owner_id, job_id=job_id)
    if remote:
        return {"ok": True, "job": remote, "source": "supabase"}
    raise HTTPException(404, "agent job not found")


@app.get("/api/agent/jobs/{job_id}/events")
def agent_job_events(job_id: str, after_id: int = 0, limit: int = 200):
    owner_id = CURRENT_USER_ID.get()
    local = _local_agent_jobs().get(job_id)
    if isinstance(local, dict):
        events = [event for event in local.get("events", []) if int(event.get("id") or 0) > max(0, int(after_id or 0))]
        return {"ok": True, "events": events[: max(1, min(int(limit or 200), 1000))], "source": "session"}
    remote = list_agent_job_events(owner_id=owner_id, job_id=job_id, after_id=max(0, int(after_id or 0)), limit=limit)
    if remote is not None:
        return {"ok": True, "events": remote, "source": "supabase"}
    raise HTTPException(404, "agent job not found")


@app.get("/api/agent/jobs/{job_id}/observability")
def agent_job_observability(job_id: str):
    owner_id = CURRENT_USER_ID.get()
    local = _local_agent_jobs().get(job_id)
    if isinstance(local, dict):
        events = list(local.get("events") or [])
        result = local.get("result") if isinstance(local.get("result"), dict) else {}
        return {"ok": True, "observability": build_agent_observability(events, result=result), "source": "session"}
    remote = get_agent_job(owner_id=owner_id, job_id=job_id)
    if remote:
        events = list_agent_job_events(owner_id=owner_id, job_id=job_id, after_id=0, limit=1000) or []
        result = remote.get("result") if isinstance(remote.get("result"), dict) else {}
        return {"ok": True, "observability": build_agent_observability(events, result=result), "source": "supabase"}
    raise HTTPException(404, "agent job not found")


def _worker_secret() -> str:
    return str(os.environ.get("AGENT_WORKER_SECRET") or os.environ.get("CRON_SECRET") or "").strip()


def _require_worker_auth(request: Request) -> None:
    secret = _worker_secret()
    if not secret and not _is_serverless_runtime():
        return
    auth = str(request.headers.get("Authorization") or "").strip()
    if not secret or auth != f"Bearer {secret}":
        raise HTTPException(401, "agent worker authorization required")


def _run_persisted_agent_job(job: dict, *, event_cb=None) -> dict:
    owner_id = str(job.get("owner_id") or "").strip()
    job_id = str(job.get("id") or "").strip()
    if not owner_id or not job_id:
        raise HTTPException(400, "invalid agent job record")
    status = str(job.get("status") or "queued").strip().lower()
    if status not in {"queued", "failed"}:
        return {"job_id": job_id, "status": status, "skipped": True}

    req = _agent_req_from_job(job)
    session_token = CURRENT_SESSION_ID.set(f"agent-worker:{job_id}")
    user_token = CURRENT_USER_ID.set(owner_id)
    profile_token = CURRENT_PROFILE_ID.set(owner_id)
    try:
        return _run_agent_impl(req, event_cb=event_cb, job_id=job_id)
    finally:
        CURRENT_PROFILE_ID.reset(profile_token)
        CURRENT_USER_ID.reset(user_token)
        CURRENT_SESSION_ID.reset(session_token)


def _run_agent_worker_jobs(*, job_id: str | None, limit: int) -> dict:
    jobs: list[dict] = []
    if job_id:
        job = None
        local = _local_agent_jobs().get(job_id)
        if isinstance(local, dict):
            job = local
        if not job and has_supabase():
            job = get_agent_job_any(job_id=job_id)
        if not job:
            raise HTTPException(404, "agent job not found")
        jobs = [job]
    else:
        local_jobs = [
            job for job in _local_agent_jobs().values()
            if isinstance(job, dict) and str(job.get("status") or "") == "queued"
        ][: max(1, min(int(limit or 1), 5))]
        if local_jobs:
            jobs = local_jobs
        elif has_supabase():
            remote_jobs = list_agent_jobs_by_status(status="queued", limit=limit)
            jobs = remote_jobs or []

    results: list[dict] = []
    for job in jobs[: max(1, min(int(limit or 1), 5))]:
        try:
            result = _run_persisted_agent_job(job)
            results.append({"job_id": job.get("id"), "ok": True, "result": result})
        except HTTPException as exc:
            results.append({"job_id": job.get("id"), "ok": False, "error": str(exc.detail), "status_code": exc.status_code})
        except Exception as exc:
            results.append({"job_id": job.get("id"), "ok": False, "error": str(exc)})

    return {"ok": True, "processed": len(results), "results": results}


def _agent_shell_actions(actions: list[dict]) -> list[AgentHarnessShellAction]:
    shell_actions: list[AgentHarnessShellAction] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        if str(action.get("type") or "").strip().lower() != "shell":
            continue
        command = str(action.get("command") or "").strip()
        if not command:
            continue
        shell_actions.append(
            AgentHarnessShellAction(
                command=command,
                cwd=str(action.get("cwd") or "").strip() or None,
                reason=str(action.get("reason") or "").strip() or "Agent requested project command.",
            )
        )
    return shell_actions


_INSTALL_LIKE_COMMAND_RE = re.compile(r"^\s*(?:npm|pnpm|yarn|bun)\s+(?:install|i|add)(?:\s|$)")


def _order_agent_shell_actions(actions: list[AgentHarnessShellAction]) -> list[AgentHarnessShellAction]:
    install_like: list[AgentHarnessShellAction] = []
    other: list[AgentHarnessShellAction] = []
    for action in actions or []:
        command = str(getattr(action, "command", "") or "")
        if _INSTALL_LIKE_COMMAND_RE.search(command):
            install_like.append(action)
        else:
            other.append(action)
    return [*install_like, *other]


def _prepare_agent_out_changes(ws_root: Path, normalized_changes: list) -> list[dict[str, object]]:
    out_changes: list[dict[str, object]] = []
    for ch in normalized_changes:
        if not isinstance(ch, dict):
            continue
        p = str(ch.get("path") or "").strip()
        nc = ch.get("new_content")
        if not p or not isinstance(nc, str):
            continue

        target = safe_join(ws_root, p)
        old_exists = target.exists()
        old = target.read_text(encoding="utf-8") if old_exists else ""

        out_changes.append({
            "path": p,
            "new_content": nc,
            "diff": diff_text(old, nc, filename=p),
            "old_sha256": _sha256_text(old),
            "new_sha256": _sha256_text(nc),
            "old_exists": old_exists,
        })
    return out_changes


def _merge_repair_changes(base_changes: list[dict[str, object]], repair_changes: list[dict[str, object]]) -> list[dict[str, object]]:
    if not repair_changes:
        return list(base_changes)
    merged: list[dict[str, object]] = []
    index_by_path: dict[str, int] = {}
    for change in list(base_changes or []):
        if not isinstance(change, dict):
            continue
        path = str(change.get("path") or "").strip()
        if not path:
            continue
        index_by_path[path] = len(merged)
        merged.append(change)
    for change in list(repair_changes or []):
        if not isinstance(change, dict):
            continue
        path = str(change.get("path") or "").strip()
        if not path:
            continue
        if path in index_by_path:
            merged[index_by_path[path]] = change
        else:
            index_by_path[path] = len(merged)
            merged.append(change)
    return merged


_FINAL_INERT_BUTTON_RE = re.compile(r"<(?P<tag>button|Button)\b(?P<attrs>[^>]*)>(?P<body>.*?)</(?P=tag)>", re.IGNORECASE | re.DOTALL)


def _gate_inert_final_buttons_in_changes(changes: list[dict[str, object]]) -> tuple[list[dict[str, object]], list[str]]:
    gated_paths: list[str] = []
    next_changes: list[dict[str, object]] = []
    for change in list(changes or []):
        if not isinstance(change, dict):
            continue
        path = str(change.get("path") or "").strip()
        content = change.get("new_content")
        if not path or not isinstance(content, str) or PurePosixPath(path).suffix.lower() not in {".tsx", ".jsx", ".html"}:
            next_changes.append(change)
            continue
        touched = False

        def repl(match: re.Match[str]) -> str:
            nonlocal touched
            tag = str(match.group("tag") or "")
            attrs = str(match.group("attrs") or "")
            body = str(match.group("body") or "")
            body_text = re.sub(r"<[^>]+>", " ", body)
            body_text = re.sub(r"\s+", " ", body_text).strip()
            if not body_text:
                return match.group(0)
            lowered_attrs = attrs.lower()
            if any(marker in lowered_attrs for marker in ("onclick=", "href=", "to=", "disabled", "aria-disabled", "aschild")):
                return match.group(0)
            if re.search(r"\btype\s*=\s*['\"]submit['\"]", attrs, flags=re.IGNORECASE):
                return match.group(0)
            touched = True
            spacer = "" if attrs.endswith(" ") or not attrs else " "
            return f"<{tag}{attrs}{spacer}type=\"button\" disabled aria-disabled=\"true\">{body}</{tag}>"

        next_content = _FINAL_INERT_BUTTON_RE.sub(repl, content)
        if touched:
            gated_paths.append(path)
            next_change = dict(change)
            next_change["new_content"] = next_content
            next_changes.append(next_change)
        else:
            next_changes.append(change)
    return next_changes, gated_paths


def _neutralize_fake_business_data(content: str) -> tuple[str, bool]:
    next_content = str(content or "")
    replacements: tuple[tuple[str, str], ...] = (
        (r"href\s*=\s*['\"](?:https?://)?(?:wa\.me|api\.whatsapp\.com)[^'\"]*['\"]", 'aria-disabled="true" data-contact-status="kontak-belum-dikonfigurasi"'),
        (r"(?:\+?62|0)8\d{7,13}\b", "kontak-belum-dikonfigurasi"),
        (r"\b[A-Za-z0-9._%+-]+@(?:example|demo|test|domain)[A-Za-z0-9.-]*\b", "email-belum-dikonfigurasi"),
        (r"\b(?:example\.com|example\.id|test@example|demo@example|nama@domain)\b", "domain-belum-dikonfigurasi"),
        (r"\b(?:Jl\.?|Jalan)\s+(?:Contoh|Dummy|Sample|Placeholder)\b[^<\n]{0,80}", "Alamat belum dikonfigurasi"),
        (r"\bNo\.\s*123\b", "Nomor belum dikonfigurasi"),
        (r"\b(?:alamat|nomor|no hp|whatsapp|wa)\s*:\s*(?:contoh|dummy|isi|ganti|placeholder)\b", "Kontak belum dikonfigurasi"),
        (r"\bsejak\s+(?:19|20)\d{2}\b", "siap dikonfigurasi"),
        (r"\b(?:\d+(?:[.,]\d+)?\s*[Kk]\+|\d{3,}\+)\s+(?:pelanggan|customer|pesanan|order|transaksi|cabang)\b", "banyak pelanggan"),
        (r"\b\d+(?:[.,]\d+)?\s*(?:rating|bintang|star)\b", "ulasan pelanggan"),
        (r"\b(?:buka|jam\s+operasional|open)\s+\d{1,2}[:.]\d{2}\s*(?:-|sampai|–)\s*\d{1,2}[:.]\d{2}\b", "Jam operasional belum dikonfigurasi"),
        (r"\[(?:tambahkan|isi|ganti|placeholder)[^\]]+\]", "Kontak belum dikonfigurasi"),
    )
    for pattern, replacement in replacements:
        next_content = re.sub(pattern, replacement, next_content, flags=re.IGNORECASE)
    return next_content, next_content != content


def _gate_fake_business_data_in_changes(changes: list[dict[str, object]]) -> tuple[list[dict[str, object]], list[str]]:
    gated_paths: list[str] = []
    next_changes: list[dict[str, object]] = []
    for change in list(changes or []):
        if not isinstance(change, dict):
            continue
        path = str(change.get("path") or "").strip()
        content = change.get("new_content")
        if not path or not isinstance(content, str) or PurePosixPath(path).suffix.lower() not in {".tsx", ".jsx", ".html", ".css"}:
            next_changes.append(change)
            continue
        next_content, touched = _neutralize_fake_business_data(content)
        if touched:
            gated_paths.append(path)
            next_change = dict(change)
            next_change["new_content"] = next_content
            next_changes.append(next_change)
        else:
            next_changes.append(change)
    return next_changes, gated_paths


_MISSING_CSS_CLASSES_RE = re.compile(r"custom class\(es\) lack CSS definitions:\s*([A-Za-z0-9_,\s-]+)\.", re.IGNORECASE)


def _gate_missing_css_classes_in_changes(changes: list[dict[str, object]], failure_summary: str) -> tuple[list[dict[str, object]], list[str]]:
    missing: list[str] = []
    for match in _MISSING_CSS_CLASSES_RE.finditer(str(failure_summary or "")):
        for item in str(match.group(1) or "").split(","):
            clean = item.strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", clean) and clean not in missing:
                missing.append(clean)
    if not missing:
        return changes, []

    css_index = -1
    for index, change in enumerate(list(changes or [])):
        if not isinstance(change, dict):
            continue
        path = str(change.get("path") or "").strip()
        content = change.get("new_content")
        if path and isinstance(content, str) and PurePosixPath(path).suffix.lower() in {".css", ".scss"}:
            css_index = index
            break
    if css_index < 0:
        return changes, []

    next_changes = [dict(item) if isinstance(item, dict) else item for item in list(changes or [])]
    css_change = dict(next_changes[css_index])
    css_text = str(css_change.get("new_content") or "")
    appended: list[str] = []
    for class_name in missing:
        if re.search(rf"\.{re.escape(class_name)}\b", css_text):
            continue
        if class_name.endswith(("title", "heading")):
            rule = f".{class_name} {{ font-weight: 700; color: var(--text, inherit); }}"
        elif class_name.endswith(("info", "content", "body")):
            rule = f".{class_name} {{ min-width: 0; display: grid; gap: 0.25rem; }}"
        elif class_name.endswith(("state", "empty-state")):
            rule = f".{class_name} {{ color: #64748b; text-align: center; }}"
        elif class_name.endswith(("actions", "controls")):
            rule = f".{class_name} {{ display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }}"
        else:
            rule = f".{class_name} {{ min-width: 0; }}"
        appended.append(rule)
    if not appended:
        return changes, []

    css_change["new_content"] = css_text.rstrip() + "\n" + "\n".join(appended) + "\n"
    next_changes[css_index] = css_change
    return next_changes, [str(css_change.get("path") or "")]


def _reverify_merged_verifier_output(req: AgentReq, ws_root: Path, changes: list[dict[str, object]], actions: list[dict]) -> dict:
    from . import agent_runtime as runtime

    project_root = str(req.project_root or ".").strip().strip("/") or "."
    try:
        project_dir = safe_join(ws_root, project_root)
    except Exception:
        project_dir = ws_root

    all_files: list[str] = []
    if project_dir.exists():
        try:
            for path in _iter_project_export_files(project_dir):
                all_files.append(str(PurePosixPath(path.relative_to(project_dir))))
        except Exception:
            all_files = []

    relevant_files: dict[str, str] = {}
    for change in list(changes or []):
        if not isinstance(change, dict):
            continue
        path = str(change.get("path") or "").strip()
        content = change.get("new_content")
        if not path or not isinstance(content, str):
            continue
        local_path = runtime._localize_project_rel(path, project_root)
        relevant_files[path.lstrip("/")] = content
        if local_path:
            relevant_files[local_path] = content

    ctx = SimpleNamespace(
        project_root=project_root,
        project_dir=project_dir,
        all_files=all_files,
        relevant_files=relevant_files,
        attached_assets=list(getattr(req, "asset_paths", None) or []),
        attached_asset_aliases=dict(getattr(req, "asset_aliases", None) or {}),
        is_full_agent=str(getattr(req, "build_mode", "") or "").strip() == "full-agent",
    )

    def severity(name: str) -> str:
        return runtime._verifier_check_severity(name)

    def check(name: str, ok: bool, detail: str) -> dict[str, object]:
        return {"name": name, "ok": ok, "detail": str(detail)[:240], "severity": severity(name)}

    invalid_paths = [
        str(item.get("path") or "")
        for item in changes
        if not isinstance(item, dict)
        or not str(item.get("path") or "").strip()
        or ".." in str(item.get("path") or "").split("/")
    ]
    change_paths = [
        str(item.get("path") or "").strip().lstrip("/")
        for item in changes
        if isinstance(item, dict) and str(item.get("path") or "").strip()
    ]
    duplicate_paths = sorted({path for path in change_paths if change_paths.count(path) > 1})
    empty_files = [
        str(item.get("path") or "")
        for item in changes
        if isinstance(item, dict)
        and isinstance(item.get("new_content"), str)
        and not item.get("new_content")
        and not runtime._allows_empty_file(str(item.get("path") or ""))
    ]
    shell_actions = [item for item in actions if isinstance(item, dict) and str(item.get("type") or "").lower() == "shell"]
    invalid_shell = [item for item in shell_actions if not isinstance(item.get("command"), str) or not str(item.get("command") or "").strip()]
    missing_imports = runtime._missing_relative_imports(ctx, changes)
    missing_dependencies = runtime._missing_external_dependencies(ctx, changes, actions)
    root_route_issues = runtime._root_route_entrypoint_issues(ctx, changes)
    style_issues = runtime._frontend_style_runtime_issues(ctx, changes)
    asset_quality_issues = runtime._frontend_asset_quality_issues(ctx, changes)
    referenced_asset_issues = runtime._mentioned_uploaded_asset_usage_issues(ctx, changes, str(req.input or ""))
    prompt_domain_issues = runtime._prompt_domain_adherence_issues(str(req.input or ""), changes)
    prompt_requirement_issues = runtime._prompt_requirement_coverage_issues(str(req.input or ""), changes)
    business_issues = runtime._frontend_business_data_honesty_issues(ctx, changes)
    interaction_issues = runtime._frontend_interaction_integrity_issues(ctx, changes)
    maintainability_issues = runtime._frontend_maintainability_integrity_issues(ctx, changes)
    raw_tool_actions = [
        item for item in actions if isinstance(item, dict) and str(item.get("type") or "").lower() in {"tool", "mcp"}
    ]

    checks = [
        check("has-work-output", bool(changes or actions), "Build request produced file changes or runtime actions." if changes or actions else "Build request produced no file changes/actions."),
        check("valid-change-paths", not invalid_paths, "All change paths look project-relative." if not invalid_paths else f"Invalid paths: {', '.join(invalid_paths[:5])}"),
        check("unique-change-paths", not duplicate_paths, "No duplicate file changes." if not duplicate_paths else f"Duplicate change paths: {', '.join(duplicate_paths[:8])}"),
        check("non-empty-file-content", not empty_files, "Changed files have content." if not empty_files else f"Empty outputs: {', '.join(empty_files[:5])}"),
        check("valid-shell-actions", not invalid_shell, "Shell actions have commands." if not invalid_shell else f"{len(invalid_shell)} shell action(s) missing command."),
        check("relative-imports-resolve", not missing_imports, "Changed relative imports resolve against the project tree." if not missing_imports else f"Missing relative imports: {', '.join(missing_imports[:6])}"),
        check("external-dependencies-declared", not missing_dependencies, "Changed external imports are declared or installed by shell actions." if not missing_dependencies else f"Undeclared external imports: {', '.join(missing_dependencies[:6])}"),
        check("root-route-entrypoint", not root_route_issues, "SPA entrypoint renders a real page at '/'." if not root_route_issues else "; ".join(root_route_issues[:2])),
        check("frontend-style-runtime", not style_issues, "Frontend styling runtime matches the project setup." if not style_issues else "; ".join(style_issues[:2])),
        check("frontend-asset-quality", not asset_quality_issues, "Frontend media/assets avoid fake placeholder sources." if not asset_quality_issues else "; ".join(asset_quality_issues[:2])),
        check("referenced-asset-usage", not referenced_asset_issues, "Explicit @asset references are used in the implementation." if not referenced_asset_issues else "; ".join(referenced_asset_issues[:2])),
        check("prompt-domain-adherence", not prompt_domain_issues, "Frontend output reflects the domain/workflow requested by the user." if not prompt_domain_issues else "; ".join(prompt_domain_issues[:2])),
        check("prompt-requirement-coverage", not prompt_requirement_issues, "Frontend output covers explicit feature/state requirements from the user prompt." if not prompt_requirement_issues else "; ".join(prompt_requirement_issues[:3])),
        check("frontend-business-data-honesty", not business_issues, "Frontend does not invent fake business contact/data." if not business_issues else "; ".join(business_issues[:2])),
        check("frontend-interaction-integrity", not interaction_issues, "Frontend interactions are wired, valid anchors, or visibly gated." if not interaction_issues else "; ".join(interaction_issues[:2])),
        check("frontend-maintainability-integrity", not maintainability_issues, "Frontend implementation stays maintainable for product-scale UI." if not maintainability_issues else "; ".join(maintainability_issues[:2])),
        check("no-unexecuted-tool-actions", not raw_tool_actions, "No raw tool/MCP actions remain in final output." if not raw_tool_actions else f"{len(raw_tool_actions)} raw tool/MCP action(s) were not executed."),
    ]
    if ctx.is_full_agent:
        checks.append(check("full-agent-coverage", len(changes) >= 2 or bool(actions), "Full-agent output touches multiple files or uses project tooling." if len(changes) >= 2 or actions else "Full-agent output may be too small for an app-level task."))
    return {"verification": checks, "warnings": [{"phase": "verifier-repair", "message": "Merged repair output was re-verified before backend apply."}]}


def _execution_needs_repair(execution: dict[str, object]) -> bool:
    if not execution:
        return False
    if _execution_has_primary_failure(execution):
        return True
    if _preview_polish_debt(execution):
        return True
    return False


def _execution_has_primary_failure(execution: dict[str, object]) -> bool:
    if not execution:
        return False
    apply = execution.get("apply")
    if isinstance(apply, dict) and apply.get("ok") is False:
        return True
    shell = execution.get("shell")
    if isinstance(shell, dict) and shell.get("ok") is False:
        return True
    validation = execution.get("validation")
    if isinstance(validation, dict) and validation.get("ok") is False:
        return True
    replay = execution.get("replay")
    if isinstance(replay, dict) and replay.get("ok") is False:
        return True
    preview_audit = execution.get("preview_audit")
    if isinstance(preview_audit, dict) and preview_audit.get("ok") is False and not preview_audit.get("skipped"):
        return True
    return False


def _preview_audit_failed(execution: dict[str, object]) -> bool:
    preview_audit = execution.get("preview_audit")
    return isinstance(preview_audit, dict) and preview_audit.get("ok") is False and not preview_audit.get("skipped")


_PREVIEW_POLISH_DEBT_CATEGORIES = {
    "metadata",
    "mobile-tap-targets",
    "responsive",
    "source-overflow-risk",
    "source-quality",
    "source-type-discipline",
    "visual-polish",
    "product-depth",
    "copy-specificity",
}


def _preview_polish_debt(execution: dict[str, object]) -> list[dict[str, object]]:
    preview_audit = execution.get("preview_audit")
    if not isinstance(preview_audit, dict) or preview_audit.get("skipped") or preview_audit.get("ok") is not True:
        return []
    debts: list[dict[str, object]] = []
    for issue in list(preview_audit.get("issue_details") or []):
        if not isinstance(issue, dict) or issue.get("severity") != "warning":
            continue
        category = str(issue.get("category") or "").strip()
        if category not in _PREVIEW_POLISH_DEBT_CATEGORIES:
            continue
        debts.append(issue)
    return debts[:8]


def _preview_polish_debt_requires_llm_repair(execution: dict[str, object]) -> bool:
    categories = {
        str(item.get("category") or "").strip()
        for item in _preview_polish_debt(execution)
        if isinstance(item, dict)
    }
    return bool(categories & {
        "product-depth",
        "copy-specificity",
        "source-quality",
        "source-type-discipline",
        "mobile-tap-targets",
        "responsive",
        "source-overflow-risk",
        "visual-polish",
    })


def _preview_state_readiness_debt(execution: dict[str, object]) -> list[dict[str, object]]:
    preview_audit = execution.get("preview_audit")
    if not isinstance(preview_audit, dict) or preview_audit.get("skipped") or preview_audit.get("ok") is not True:
        return []
    debts: list[dict[str, object]] = []
    for issue in list(preview_audit.get("issue_details") or []):
        if not isinstance(issue, dict) or issue.get("severity") != "warning":
            continue
        if str(issue.get("category") or "").strip() in {"state-loading", "state-error", "state-empty"}:
            debts.append(issue)
    return debts[:8]


def _repair_resolves_parent_execution(parent_execution: dict[str, object], repair_execution: dict[str, object] | None) -> bool:
    if not isinstance(repair_execution, dict) or not bool(repair_execution.get("ok")):
        return False
    if _preview_polish_debt(repair_execution):
        return False
    if _execution_needs_repair(repair_execution):
        return False

    # A successful build/install replay does not prove that an earlier visual
    # preview failure is gone. Require the repair pass to rerun and pass preview.
    if _preview_audit_failed(parent_execution):
        repair_preview = repair_execution.get("preview_audit")
        return isinstance(repair_preview, dict) and repair_preview.get("ok") is True and not repair_preview.get("skipped")

    return True


def _failure_text_marker(text: object) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    markers = [
        "syntaxerror",
        "typeerror",
        "referenceerror",
        "modulenotfounderror",
        "module not found",
        "cannot find module",
        "eslint",
        "failed",
        "error",
    ]
    for marker in markers:
        if marker in lowered:
            return marker.replace(" ", "-")
    first_line = next((line.strip() for line in raw.splitlines() if line.strip()), "")
    return first_line[:120]


def _failure_evidence_excerpt(*values: object, max_chars: int = 700) -> str:
    lines: list[str] = []
    for value in values:
        raw = str(value or "").strip()
        if not raw:
            continue
        for line in raw.splitlines():
            clean = line.strip()
            if clean:
                lines.append(clean)
            if len(lines) >= 12:
                break
        if len(lines) >= 12:
            break
    return "\n".join(lines)[:max_chars]


def _passed_commands(container: object) -> set[str]:
    if not isinstance(container, dict):
        return set()
    commands: set[str] = set()
    for result in list(container.get("results") or []):
        if not isinstance(result, dict) or result.get("ok") is not True:
            continue
        command = str(result.get("command") or "").strip()
        if command:
            commands.add(command)
    return commands


def _resolved_failure_commands(execution: dict[str, object]) -> set[str]:
    commands: set[str] = set()
    repairs = execution.get("repairs")
    if not isinstance(repairs, list):
        return commands
    for repair in repairs:
        if not isinstance(repair, dict):
            continue
        repair_execution = repair.get("execution")
        if not isinstance(repair_execution, dict):
            continue
        commands.update(_passed_commands(repair_execution.get("shell")))
        commands.update(_passed_commands(repair_execution.get("validation")))
        commands.update(_passed_commands(repair_execution.get("replay")))
    return commands


def _failed_command_signatures(container: object, *, kind: str, resolved_commands: set[str] | None = None) -> list[dict[str, object]]:
    if not isinstance(container, dict):
        return []
    resolved_commands = resolved_commands or set()
    signatures: list[dict[str, object]] = []
    for result in list(container.get("results") or []):
        if not isinstance(result, dict) or result.get("ok") is not False:
            continue
        command = str(result.get("command") or "").strip()
        if command and command in resolved_commands:
            continue
        stderr = result.get("stderr")
        stdout = result.get("stdout")
        marker = _failure_text_marker(stderr) or _failure_text_marker(stdout) or f"returncode-{result.get('returncode')}"
        excerpt = _failure_evidence_excerpt(stderr, stdout)
        policy = result.get("policy") if isinstance(result.get("policy"), dict) else {}
        policy_blocked = result.get("returncode") == 126 and bool(policy)
        signatures.append({
            "kind": kind,
            "command": command[:180],
            "returncode": result.get("returncode"),
            "policy_blocked": policy_blocked,
            "risk_level": policy.get("risk_level") if policy_blocked else None,
            "marker": marker,
            "excerpt": excerpt,
            "signature": f"{kind}:{command}:{marker}",
        })
    return signatures


def _failure_analysis_summary(signatures: list[dict[str, object]], *, repeated_failure: bool, repeated_count: int) -> dict[str, object]:
    if not signatures:
        return {
            "primary_failure": "",
            "summary": "No failing apply, shell, validation, or preview signal was found.",
            "suggested_next_move": "Continue normal execution and verify with the project's validation command.",
        }

    first = signatures[0]
    kind = str(first.get("kind") or "failure")
    marker = str(first.get("marker") or "unknown").strip() or "unknown"
    command = str(first.get("command") or "").strip()
    category = str(first.get("category") or "").strip()
    path = str(first.get("path") or "").strip()
    policy_blocked = bool(first.get("policy_blocked"))

    if kind == "validation":
        primary = f"validation failed: {marker}"
        if command:
            primary = f"{primary} in `{command}`"
        next_move = "Read the failing validation output, edit the source that causes it, then rerun the same validation command."
        excerpt = str(first.get("excerpt") or "")
        lowered_excerpt = excerpt.lower()
        if "not raised" in lowered_excerpt or "did not raise" in lowered_excerpt:
            next_move = (
                "The remaining failure is an expected exception/assertion branch. "
                "add the missing validation branch for the exact failing input, keep the existing passing behavior, "
                "then rerun the same validation command."
            )
    elif kind == "shell":
        primary = f"shell action {'blocked by command policy' if policy_blocked else 'failed'}: {marker}"
        if command:
            primary = f"{primary} in `{command}`"
        next_move = (
            "Do not skip validation. Replace the blocked command with a safe project-scoped equivalent, or produce the file fix and leave this command policy blocker explicit."
            if policy_blocked
            else "Fix the command precondition or project files before rerunning the shell action."
        )
    elif kind == "apply":
        primary = f"apply failed: {marker}"
        if path:
            primary = f"{primary} at `{path}`"
        next_move = "Read the latest file content and generate a fresh patch against current state."
    elif kind == "preview_audit":
        primary = f"preview audit failed: {category or marker}"
        next_move = "Fix the visible UI/runtime issue, restart or refresh preview, then rerun preview audit."
    else:
        primary = f"{kind} failed: {marker}"
        next_move = "Inspect the failing evidence and make the smallest concrete fix before validating again."

    if repeated_failure:
        next_move = f"Repeated failure seen {repeated_count + 1} times. Change strategy: inspect a different source of evidence before editing again. {next_move}"

    return {
        "primary_failure": primary[:240],
        "summary": f"{primary}. failures={len(signatures)} repeated={bool(repeated_failure)}"[:500],
        "suggested_next_move": next_move[:500],
    }


def _execution_failure_analysis(execution: dict[str, object]) -> dict[str, object]:
    signatures: list[dict[str, object]] = []
    resolved_commands = _resolved_failure_commands(execution)
    resolved_command_failures: list[dict[str, object]] = []
    if resolved_commands:
        for kind, container in (("shell", execution.get("shell")), ("validation", execution.get("validation"))):
            for item in _failed_command_signatures(container, kind=kind):
                command = str(item.get("command") or "").strip()
                if command and command in resolved_commands:
                    resolved_command_failures.append(item)
    apply_result = execution.get("apply")
    if isinstance(apply_result, dict) and apply_result.get("ok") is False:
        conflicts = list(apply_result.get("conflicts") or [])
        if conflicts:
            for conflict in conflicts[:8]:
                if not isinstance(conflict, dict):
                    continue
                path = str(conflict.get("path") or "").strip()
                reason = str(conflict.get("reason") or conflict.get("message") or "conflict").strip()
                signatures.append({
                    "kind": "apply",
                    "path": path,
                    "marker": reason[:120],
                    "excerpt": _failure_evidence_excerpt(reason),
                    "signature": f"apply:{path}:{reason[:120]}",
                })
        else:
            signatures.append({"kind": "apply", "marker": "apply-failed", "excerpt": "Apply harness reported failure without conflict details.", "signature": "apply:failed"})

    signatures.extend(_failed_command_signatures(execution.get("shell"), kind="shell", resolved_commands=resolved_commands))
    signatures.extend(_failed_command_signatures(execution.get("validation"), kind="validation", resolved_commands=resolved_commands))
    signatures.extend(_failed_command_signatures(execution.get("replay"), kind="replay"))

    preview_audit = execution.get("preview_audit")
    if isinstance(preview_audit, dict) and preview_audit.get("ok") is False and not preview_audit.get("skipped"):
        for issue in list(preview_audit.get("issue_details") or [])[:8]:
            if not isinstance(issue, dict):
                continue
            severity = str(issue.get("severity") or "issue").strip()
            category = str(issue.get("category") or "preview").strip()
            detail_marker = _failure_text_marker(issue.get("detail")) or str(issue.get("detail") or "")[:120]
            excerpt = _failure_evidence_excerpt(issue.get("detail"), issue.get("suggested_fix"))
            signatures.append({
                "kind": "preview_audit",
                "severity": severity,
                "category": category,
                "marker": detail_marker,
                "excerpt": excerpt,
                "signature": f"preview:{severity}:{category}:{detail_marker}",
            })

    signature_values = [str(item.get("signature") or "") for item in signatures if str(item.get("signature") or "").strip()]
    current_signature = _sha256_text("\n".join(signature_values))[:16] if signature_values else ""

    prior_signatures: list[str] = []
    repairs = execution.get("repairs")
    if isinstance(repairs, list):
        for repair in repairs:
            if not isinstance(repair, dict):
                continue
            repair_execution = repair.get("execution")
            if not isinstance(repair_execution, dict):
                continue
            repair_analysis = repair_execution.get("failure_analysis")
            if isinstance(repair_analysis, dict):
                sig = str(repair_analysis.get("current_signature") or "").strip()
            else:
                sig = str(_execution_failure_analysis(repair_execution).get("current_signature") or "").strip()
            if sig:
                prior_signatures.append(sig)

    repeated_count = prior_signatures.count(current_signature) if current_signature else 0
    summary = _failure_analysis_summary(signatures, repeated_failure=bool(current_signature and repeated_count > 0), repeated_count=repeated_count)
    return {
        "current_signature": current_signature,
        "failure_count": len(signatures),
        "active_failure_count": len(signatures),
        "resolved_failure_count": len(resolved_command_failures),
        "failures": signatures[:12],
        "resolved_failures": resolved_command_failures[:12],
        "evidence_excerpt": "\n---\n".join(
            str(item.get("excerpt") or "").strip()
            for item in signatures[:4]
            if str(item.get("excerpt") or "").strip()
        )[:1800],
        "primary_failure": summary["primary_failure"],
        "summary": summary["summary"],
        "suggested_next_move": summary["suggested_next_move"],
        "prior_signatures": prior_signatures[-5:],
        "repeated_failure": bool(current_signature and repeated_count > 0),
        "repeated_count": repeated_count,
    }


_TS_PARSE_FAILURE_RE = re.compile(
    r"\bTS(?:1003|1005|1109|1128|1351|1381|1382|17002|2657)\b|JSX expressions must have one parent element|Unexpected token|Expected corresponding JSX closing tag",
    re.IGNORECASE,
)


def _execution_has_command_failure(execution: dict[str, object]) -> bool:
    for key in ("shell", "validation", "replay"):
        container = execution.get(key)
        if isinstance(container, dict) and container.get("ok") is False:
            return True
    return False


def _execution_has_parse_failure(execution: dict[str, object]) -> bool:
    for key in ("shell", "validation", "replay"):
        container = execution.get(key)
        if not isinstance(container, dict):
            continue
        for result in list(container.get("results") or []):
            if not isinstance(result, dict):
                continue
            text = f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}"
            if _TS_PARSE_FAILURE_RE.search(text):
                return True
    return False


def _merge_repair_execution_state(parent_execution: dict[str, object], repair_execution: dict[str, object]) -> None:
    if not isinstance(parent_execution, dict) or not isinstance(repair_execution, dict):
        return
    if isinstance(repair_execution.get("preview_audit"), dict):
        parent_execution["preview_audit"] = repair_execution["preview_audit"]
    for key in ("apply", "shell", "validation", "replay"):
        candidate = repair_execution.get(key)
        if isinstance(candidate, dict):
            parent_execution[key] = candidate
    parent_execution["ok"] = not _execution_has_primary_failure(parent_execution)
    parent_execution["failure_analysis"] = _execution_failure_analysis(parent_execution)


def _repair_execution_degrades_parent(parent_execution: dict[str, object], repair_execution: dict[str, object]) -> bool:
    parent_had_command_failure = _execution_has_command_failure(parent_execution)
    repair_has_command_failure = _execution_has_command_failure(repair_execution)
    if repair_has_command_failure and not parent_had_command_failure:
        return True
    if _execution_has_parse_failure(repair_execution) and not _execution_has_parse_failure(parent_execution):
        return True
    parent_preview = parent_execution.get("preview_audit")
    repair_preview = repair_execution.get("preview_audit")
    if isinstance(parent_preview, dict) and isinstance(repair_preview, dict):
        if parent_preview.get("skipped") or repair_preview.get("skipped"):
            return False
        parent_issues = [item for item in list(parent_preview.get("issue_details") or []) if isinstance(item, dict)]
        repair_issues = [item for item in list(repair_preview.get("issue_details") or []) if isinstance(item, dict)]
        parent_blocking = sum(1 for item in parent_issues if item.get("severity") == "blocking")
        repair_blocking = sum(1 for item in repair_issues if item.get("severity") == "blocking")
        parent_warnings = sum(1 for item in parent_issues if item.get("severity") == "warning")
        repair_warnings = sum(1 for item in repair_issues if item.get("severity") == "warning")
        if bool(parent_preview.get("ok")) and not bool(repair_preview.get("ok")):
            return True
        if repair_blocking > parent_blocking:
            return True
        if repair_blocking == parent_blocking and repair_warnings > parent_warnings:
            return True
    return False


def _rollback_repair_checkpoint(repair_execution: dict[str, object]) -> dict[str, object] | None:
    apply_result = repair_execution.get("apply")
    if not isinstance(apply_result, dict):
        return None
    checkpoint_path = str(apply_result.get("checkpoint_path") or "").strip()
    if not checkpoint_path:
        return None
    try:
        restored = _restore_checkpoint_path(checkpoint_path)
        return {"ok": True, "checkpoint_path": checkpoint_path, **restored}
    except Exception as exc:
        return {"ok": False, "checkpoint_path": checkpoint_path, "error": str(exc)[:300]}


def _execution_final_failure_analysis(execution: dict[str, object]) -> dict[str, object]:
    raw = _execution_failure_analysis(execution)
    if not bool(execution.get("ok")):
        return raw
    resolved_failures = list(raw.get("failures") or []) or list(raw.get("resolved_failures") or [])
    resolved_count = len(resolved_failures) or int(raw.get("resolved_failure_count") or 0)
    if not resolved_count:
        return raw
    repairs = execution.get("repairs")
    repair_attempts = len(repairs) if isinstance(repairs, list) else 0
    return {
        "current_signature": "",
        "failure_count": 0,
        "active_failure_count": 0,
        "resolved_failure_count": resolved_count,
        "failures": [],
        "resolved_failures": resolved_failures[:12],
        "evidence_excerpt": "",
        "primary_failure": "",
        "summary": f"Resolved: {resolved_count} previous failure signal(s) were superseded by successful execution.",
        "suggested_next_move": "No active backend failure remains. Continue with user-facing verification or the next task.",
        "prior_signatures": list(raw.get("prior_signatures") or [])[-5:],
        "repeated_failure": False,
        "repeated_count": 0,
        "resolved_by": "repair-loop" if repair_attempts else "successful-execution",
        "repair_attempts": repair_attempts,
    }


def _criterion(label: str, status: str, detail: str) -> dict[str, str]:
    return {
        "label": label,
        "status": status,
        "detail": str(detail or "")[:500],
    }


def _build_repair_stop(execution: dict[str, object], *, max_repair_passes: int) -> dict[str, object]:
    repairs = execution.get("repairs")
    attempts = len(repairs) if isinstance(repairs, list) else 0
    failure_analysis = execution.get("failure_analysis")
    if not isinstance(failure_analysis, dict):
        failure_analysis = _execution_failure_analysis(execution)
    next_move = str(failure_analysis.get("suggested_next_move") or "").strip()
    if not next_move:
        next_move = "Inspect the latest failing execution evidence, change strategy, then run a new repair pass."
    if bool(failure_analysis.get("repeated_failure")) and "change strategy" not in next_move.lower():
        next_move = f"Change strategy before another attempt. {next_move}"
    return {
        "reason": "max_repair_passes_exhausted",
        "max_repair_passes": max_repair_passes,
        "attempts": attempts,
        "failure_analysis": failure_analysis,
        "next_action": next_move[:600],
        "summary": f"Backend repair stopped after {attempts}/{max_repair_passes} repair pass(es); execution is still failing.",
    }


def _execution_completion_report(execution: dict[str, object]) -> dict[str, object]:
    criteria: list[dict[str, str]] = []
    residual_risks: list[str] = []
    ok = bool(execution.get("ok"))
    primary_failure = _execution_has_primary_failure(execution)
    repairs = execution.get("repairs")
    repair_attempted = bool(isinstance(repairs, list) and repairs)
    last_repair_execution = repairs[-1].get("execution") if isinstance(repairs, list) and repairs and isinstance(repairs[-1], dict) else None
    clean_repair_success = bool(
        isinstance(last_repair_execution, dict)
        and last_repair_execution.get("ok")
        and not _execution_has_primary_failure(last_repair_execution)
        and not _preview_polish_debt(last_repair_execution)
    )
    polish_debt = _preview_polish_debt(execution)
    effective_polish_debt = [] if clean_repair_success else polish_debt
    polish_only_after_repair = bool(ok and effective_polish_debt and repair_attempted and not primary_failure)

    apply_result = execution.get("apply")
    if isinstance(apply_result, dict):
        criteria.append(_criterion(
            "apply",
            "passed" if apply_result.get("ok") else "failed",
            f"applied={apply_result.get('applied')} count={apply_result.get('count')}",
        ))
    else:
        criteria.append(_criterion("apply", "skipped", "No file changes were produced for backend apply."))

    shell_result = execution.get("shell")
    if isinstance(shell_result, dict):
        failed = sum(1 for item in list(shell_result.get("results") or []) if isinstance(item, dict) and not item.get("ok"))
        criteria.append(_criterion(
            "shell",
            "passed" if shell_result.get("ok") else "failed",
            f"ran={shell_result.get('ran')} failed={failed}",
        ))
    else:
        criteria.append(_criterion("shell", "skipped", "No shell actions were requested."))

    validation = execution.get("validation")
    if isinstance(validation, dict):
        criteria.append(_criterion(
            "validation",
            "passed" if validation.get("ok") else "failed",
            f"ran={validation.get('ran')} failed={validation.get('failed')} commands={', '.join(str(item) for item in list(validation.get('commands') or [])[:4])}",
        ))
    else:
        criteria.append(_criterion("validation", "skipped", "No validation command was inferred for this project/run."))
        if execution.get("apply") is not None:
            residual_risks.append("No validation command was inferred after file changes.")

    replay = execution.get("replay")
    if isinstance(replay, dict):
        failed = sum(1 for item in list(replay.get("results") or []) if isinstance(item, dict) and not item.get("ok"))
        criteria.append(_criterion(
            "replay",
            "passed" if replay.get("ok") else "failed",
            f"ran={replay.get('ran')} failed={failed}",
        ))

    preview_audit = execution.get("preview_audit")
    if isinstance(preview_audit, dict):
        if preview_audit.get("skipped"):
            criteria.append(_criterion("preview", "skipped", str(preview_audit.get("reason") or preview_audit.get("summary") or "Preview audit skipped.")))
            residual_risks.append("Preview audit was skipped.")
        else:
            issue_details = list(preview_audit.get("issue_details") or [])
            blocking = sum(1 for item in issue_details if isinstance(item, dict) and item.get("severity") == "blocking")
            warnings = sum(1 for item in issue_details if isinstance(item, dict) and item.get("severity") == "warning")
            criteria.append(_criterion(
                "preview",
                "passed" if preview_audit.get("ok") else "failed",
                f"mode={preview_audit.get('audit_mode')} blocking={blocking} warnings={warnings}",
            ))
            visual_evidence = preview_audit.get("visual_evidence") if isinstance(preview_audit.get("visual_evidence"), dict) else {}
            has_screen = bool(visual_evidence.get("has_screen"))
            has_screenshot = bool(visual_evidence.get("has_screenshot"))
            screenshot_path = str(visual_evidence.get("screenshot_path") or "").strip()
            screen_backend = str(visual_evidence.get("screen_backend") or preview_audit.get("audit_mode") or "").strip()
            screen_capable_backend = screen_backend in {"agent-browser", "playwright", "browser"}
            visual_required = bool(
                execution.get("apply") is not None
                and preview_audit.get("ok") is True
                and "visual_evidence" in preview_audit
                and screen_capable_backend
            )
            visual_status = "passed" if has_screen else ("failed" if visual_required else "warning")
            visual_detail = (
                f"screen={screen_backend or 'unavailable'} dom_snapshot={bool(visual_evidence.get('has_dom_snapshot'))} "
                f"screenshot={screenshot_path or ('captured' if has_screenshot else 'missing')} "
                f"desktop={visual_evidence.get('desktop_viewport') or {}} mobile={visual_evidence.get('mobile_viewport') or {}}"
            )
            criteria.append(_criterion("visual-review", visual_status, visual_detail))
            if not has_screen:
                residual_risks.append("Preview audit did not capture live browser visual evidence; review is limited to fallback inspection.")
            if warnings and preview_audit.get("ok"):
                residual_risks.append(f"Preview audit still has {warnings} warning(s).")
            polish_debt = _preview_polish_debt(execution)
            if polish_debt:
                criteria.append(_criterion(
                    "preview-polish",
                    "warning" if polish_only_after_repair else ("failed" if ok else "pending"),
                    f"warnings_to_polish={len(polish_debt)} categories={', '.join(sorted({str(item.get('category') or '') for item in polish_debt})[:4])}",
                ))
                residual_risks.append(f"Preview polish still has {len(polish_debt)} production warning(s).")
    else:
        criteria.append(_criterion("preview", "skipped", "No preview surface or preview URL was available for this run."))

    repaired_success = False
    if isinstance(repairs, list) and repairs:
        repaired_success = bool(isinstance(last_repair_execution, dict) and last_repair_execution.get("ok"))
        criteria.append(_criterion(
            "repair-loop",
            "passed" if repaired_success else ("warning" if polish_only_after_repair else "failed"),
            f"attempts={len(repairs)}",
        ))
    else:
        criteria.append(_criterion("repair-loop", "skipped", "No backend repair pass was needed."))

    repair_stop = execution.get("repair_stop")
    if isinstance(repair_stop, dict):
        criteria.append(_criterion(
            "repair-budget",
            "failed",
            str(repair_stop.get("summary") or "Backend repair budget was exhausted."),
        ))
        next_action = str(repair_stop.get("next_action") or "").strip()
        residual_risks.append(str(repair_stop.get("summary") or "Backend repair stopped before completion."))
        if next_action:
            residual_risks.append(f"Next action after repair stop: {next_action}")

    if ok and repaired_success:
        for item in criteria:
            if item.get("status") == "failed" and item.get("label") != "repair-loop":
                item["status"] = "superseded"
                item["detail"] = f"{item.get('detail', '')} (superseded by successful repair pass)"[:500]

    failure_analysis = execution.get("failure_analysis")
    if not ok and isinstance(failure_analysis, dict):
        summary = str(failure_analysis.get("summary") or "").strip()
        next_move = str(failure_analysis.get("suggested_next_move") or "").strip()
        if summary:
            residual_risks.append(summary)
        if next_move:
            residual_risks.append(f"Next move: {next_move}")

    failed_labels = [item["label"] for item in criteria if item.get("status") == "failed"]
    hard_failed_labels = [label for label in failed_labels if label != "preview-polish"]
    hard_failed = bool(hard_failed_labels)
    completion_state = (
        "blocked"
        if hard_failed
        else (
            "complete"
            if ok and not effective_polish_debt
            else ("complete-with-warnings" if polish_only_after_repair else ("polish-needed" if ok else "blocked"))
        )
    )
    if completion_state == "complete":
        summary = "Complete: backend execution criteria passed or were intentionally skipped."
    elif completion_state == "complete-with-warnings":
        summary = f"Complete with warnings: validation passed, but preview audit still has {len(effective_polish_debt)} production warning(s)."
    elif completion_state == "polish-needed":
        summary = f"Polish needed: preview audit still has {len(effective_polish_debt)} production warning(s)."
    else:
        summary = f"Blocked: {', '.join(hard_failed_labels or failed_labels) or 'execution'} still failing."

    return {
        "ok": bool(not hard_failed and ok and (not effective_polish_debt or completion_state == "complete-with-warnings")),
        "state": completion_state,
        "summary": summary,
        "criteria": criteria,
        "residual_risks": residual_risks[:8],
    }


def _execution_repair_report(execution: dict[str, object], max_chars: int = 9000) -> str:
    repairs = execution.get("repairs")
    repair_summaries = []
    if isinstance(repairs, list):
        for index, repair in enumerate(repairs[-3:], start=max(1, len(repairs) - 2)):
            if not isinstance(repair, dict):
                continue
            repair_execution = repair.get("execution")
            repair_summaries.append({
                "index": index,
                "changes": len(list(repair.get("changes") or [])),
                "actions": len(list(repair.get("actions") or [])),
                "execution_ok": repair_execution.get("ok") if isinstance(repair_execution, dict) else None,
                "validation": repair_execution.get("validation") if isinstance(repair_execution, dict) else None,
                "shell": repair_execution.get("shell") if isinstance(repair_execution, dict) else None,
                "replay": repair_execution.get("replay") if isinstance(repair_execution, dict) else None,
                "preview_audit": repair_execution.get("preview_audit") if isinstance(repair_execution, dict) else None,
                "failure_analysis": repair_execution.get("failure_analysis") if isinstance(repair_execution, dict) else None,
            })
    preview_audit = execution.get("preview_audit")
    preview_repair = None
    if isinstance(preview_audit, dict):
        preview_repair = {
            "ok": preview_audit.get("ok"),
            "skipped": preview_audit.get("skipped"),
            "summary": preview_audit.get("summary"),
            "repair_brief": preview_audit.get("repair_brief"),
            "visual_evidence": preview_audit.get("visual_evidence"),
            "visual_summary": preview_audit.get("visual_summary"),
            "evidence_pack": preview_audit.get("evidence_pack"),
            "issue_details": list(preview_audit.get("issue_details") or [])[:8],
        }
    report = json.dumps(
        {
            "apply": execution.get("apply"),
            "shell": execution.get("shell"),
            "validation": execution.get("validation"),
            "replay": execution.get("replay"),
            "preview_audit": preview_repair,
            "failure_analysis": execution.get("failure_analysis") or _execution_failure_analysis(execution),
            "completion_report": execution.get("completion_report"),
            "run_ledger": execution.get("run_ledger"),
            "previous_repairs": repair_summaries,
            "repair_stop": execution.get("repair_stop"),
            "last_repair_execution": execution.get("last_repair_execution"),
        },
        ensure_ascii=False,
        indent=2,
    )
    return report[:max_chars]


def _preview_repair_targets_report(execution: dict[str, object]) -> str:
    preview_audit = execution.get("preview_audit")
    if not isinstance(preview_audit, dict):
        return ""
    evidence_pack = preview_audit.get("evidence_pack") if isinstance(preview_audit.get("evidence_pack"), dict) else {}
    targets = [item for item in list(evidence_pack.get("repair_targets") or []) if isinstance(item, dict)]
    if not targets:
        return ""
    lines = ["PREVIEW REPAIR TARGETS:"]
    for index, target in enumerate(targets[:6], start=1):
        files = ", ".join(str(item) for item in list(target.get("likely_files") or [])[:5]) or "(inspect relevant component/CSS files)"
        selectors = ", ".join(str(item) for item in list(target.get("selectors") or [])[:5]) or "(none)"
        evidence = " | ".join(str(item) for item in list(target.get("evidence") or [])[:4])
        lines.append(
            f"{index}. {target.get('kind')} priority={target.get('priority')} files={files} selectors={selectors} action={target.get('action')}"
        )
        if evidence:
            lines.append(f"   evidence: {evidence[:600]}")
    lines.append("Use these targets as the repair worklist. Change the listed source files/selectors before rerunning preview; shell/install-only actions are not sufficient for preview blockers.")
    return "\n".join(lines)


def _execution_changed_paths(execution: dict[str, object]) -> list[str]:
    paths: list[str] = []
    apply_result = execution.get("apply")
    if isinstance(apply_result, dict):
        paths.extend(str(item or "").strip() for item in list(apply_result.get("paths") or []))
        for conflict in list(apply_result.get("conflicts") or []):
            if isinstance(conflict, dict):
                paths.append(str(conflict.get("path") or "").strip())
    repairs = execution.get("repairs")
    if isinstance(repairs, list):
        for repair in repairs[-3:]:
            if not isinstance(repair, dict):
                continue
            for change in list(repair.get("changes") or []):
                if isinstance(change, dict):
                    paths.append(str(change.get("path") or "").strip())
            repair_execution = repair.get("execution")
            if isinstance(repair_execution, dict):
                paths.extend(_execution_changed_paths(repair_execution))
    deduped: list[str] = []
    seen: set[str] = set()
    for path in paths:
        clean = path.strip().lstrip("/")
        if not clean or clean in seen:
            continue
        seen.add(clean)
        deduped.append(clean)
    return deduped[:8]


_FAILURE_PATH_RE = re.compile(r"(?P<path>(?:[A-Za-z]:)?/?[\w@./:+-]+\.(?:py|tsx|ts|jsx|js|css|scss|sass|html|json|md|vue|svelte))")


def _normalize_failure_path(candidate: str, ws_root: Path, project_root: str) -> str | None:
    raw = str(candidate or "").strip().strip("\"'`:,;()[]{}")
    if not raw or raw.startswith(("http://", "https://")):
        return None
    raw = raw.replace("\\", "/")
    try:
        candidate_path = Path(raw)
        if candidate_path.is_absolute():
            try:
                return candidate_path.resolve().relative_to(ws_root.resolve()).as_posix()
            except Exception:
                return None
        rel = PurePosixPath(raw.lstrip("./"))
        if str(rel).startswith("../"):
            return None
        project_prefix = str(project_root or ".").strip().strip("/")
        if project_prefix and project_prefix != "." and not str(rel).startswith(f"{project_prefix}/"):
            rel = PurePosixPath(project_prefix) / rel
        return rel.as_posix()
    except Exception:
        return None


def _failure_referenced_paths(execution: dict[str, object], project_root: str, ws_root: Path) -> list[str]:
    texts: list[str] = []
    for container_name in ("shell", "validation"):
        container = execution.get(container_name)
        if not isinstance(container, dict):
            continue
        for result in list(container.get("results") or []):
            if not isinstance(result, dict) or result.get("ok") is not False:
                continue
            texts.append(str(result.get("stderr") or ""))
            texts.append(str(result.get("stdout") or ""))
    preview_audit = execution.get("preview_audit")
    if isinstance(preview_audit, dict):
        for issue in list(preview_audit.get("issue_details") or []):
            if isinstance(issue, dict):
                texts.append(str(issue.get("detail") or ""))
                texts.append(str(issue.get("suggested_fix") or ""))
    repairs = execution.get("repairs")
    if isinstance(repairs, list):
        for repair in repairs[-3:]:
            if not isinstance(repair, dict):
                continue
            repair_execution = repair.get("execution")
            if isinstance(repair_execution, dict):
                texts.extend(_failure_referenced_paths(repair_execution, project_root, ws_root))

    paths: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for match in _FAILURE_PATH_RE.finditer(text):
            normalized = _normalize_failure_path(match.group("path"), ws_root, project_root)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            paths.append(normalized)
    return paths[:8]


def _repair_file_context(project_root: str, execution: dict[str, object], *, max_files: int = 5, max_chars_per_file: int = 2200) -> str:
    snippets: list[dict[str, object]] = []
    ws_root = _ws()
    candidate_paths = [*_execution_changed_paths(execution), *_failure_referenced_paths(execution, project_root, ws_root)]
    deduped_paths: list[str] = []
    seen_paths: set[str] = set()
    for candidate in candidate_paths:
        clean = str(candidate or "").strip().lstrip("/")
        if not clean or clean in seen_paths:
            continue
        seen_paths.add(clean)
        deduped_paths.append(clean)
    for path in deduped_paths[:max_files]:
        try:
            target = safe_join(ws_root, path)
        except Exception:
            continue
        if not target.exists() or not target.is_file():
            continue
        try:
            content = target.read_text(encoding="utf-8")
        except Exception as exc:
            snippets.append({"path": path, "error": str(exc)[:180]})
            continue
        snippets.append({
            "path": path,
            "sha256": _sha256_text(content),
            "excerpt": content[:max_chars_per_file],
            "truncated": len(content) > max_chars_per_file,
        })
    if not snippets:
        return "No current file snippets available for changed paths."
    return json.dumps({
        "project_root": project_root,
        "files": snippets,
    }, ensure_ascii=False, indent=2)[:9000]


def _repair_replay_command_items(execution: dict[str, object]) -> list[dict[str, object]]:
    commands: list[dict[str, object]] = []
    for kind in ("shell", "validation"):
        container = execution.get(kind)
        if not isinstance(container, dict):
            continue
        project_root = str(container.get("project_root") or ".").strip() or "."
        for result in list(container.get("results") or []):
            if not isinstance(result, dict) or result.get("ok") is not False:
                continue
            command = str(result.get("command") or "").strip()
            if not command:
                continue
            commands.append({
                "kind": kind,
                "command": command,
                "cwd": project_root,
                "returncode": result.get("returncode"),
                "reason": f"Replay failing {kind} command after repair.",
            })
    repairs = execution.get("repairs")
    if isinstance(repairs, list):
        for repair in repairs[-2:]:
            if not isinstance(repair, dict):
                continue
            repair_execution = repair.get("execution")
            if isinstance(repair_execution, dict):
                try:
                    replay = json.loads(_repair_replay_plan(repair_execution))
                except Exception:
                    replay = {}
                for item in list(replay.get("commands") or []):
                    if isinstance(item, dict):
                        commands.append(item)
    deduped: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for item in commands:
        key = (str(item.get("kind") or ""), str(item.get("command") or ""))
        if not key[1] or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped[:8]


def _repair_replay_plan(execution: dict[str, object]) -> str:
    deduped = _repair_replay_command_items(execution)
    return json.dumps({
        "commands": deduped,
        "instruction": "After making repair changes, include shell actions for non-validation replay commands that still need explicit rerun. Backend validation commands are rerun automatically after file changes.",
    }, ensure_ascii=False, indent=2)[:5000]


def _run_repair_replay(project_root: str, previous_execution: dict[str, object], repair_actions: list[dict], emit) -> dict[str, object] | None:
    planned = [
        item for item in _repair_replay_command_items(previous_execution)
        if str(item.get("kind") or "") == "shell" and str(item.get("command") or "").strip()
    ]
    if not planned:
        return None
    already_requested = {
        str(item.get("command") or "").strip()
        for item in repair_actions
        if isinstance(item, dict) and str(item.get("type") or "").strip().lower() == "shell"
    }
    replay_actions: list[AgentHarnessShellAction] = []
    skipped_commands: list[dict[str, object]] = []
    for item in planned:
        command = str(item.get("command") or "").strip()
        if not command or command in already_requested:
            continue
        policy = _command_policy_decision(command, project_root=project_root)
        if not policy.ok:
            skipped_commands.append({
                "command": command,
                "reason": policy.reason,
                "risk_level": policy.risk_level,
            })
            continue
        replay_actions.append(AgentHarnessShellAction(
            command=command,
            cwd=str(item.get("cwd") or project_root or "."),
            reason=str(item.get("reason") or "Replay previously failing shell command after repair."),
        ))
    if not replay_actions:
        if skipped_commands:
            return {
                "ok": True,
                "skipped": True,
                "project_root": project_root,
                "ran": 0,
                "results": [],
                "skipped_commands": skipped_commands,
                "summary": "Replay commands skipped because they are not safe for guarded autonomy.",
            }
        return None
    replay_commands = [action.command for action in replay_actions]
    emit("status", {"phase": "executing_replay", "message": "Backend harness replaying previously failing shell commands..."})
    emit("tool_call", _harness_tool_call_payload(
        "repair-replay",
        "executing_replay",
        project_root=project_root,
        summary=f"Replaying {len(replay_commands)} command(s).",
        commands=replay_commands,
        skipped_commands=skipped_commands,
    ))
    _emit_command_start_events(emit, tool="repair-replay", phase="executing_replay", project_root=project_root, commands=replay_commands, group="repair replay")
    replay = _run_harness_shell_actions_internal(
        ws_root_path=_ws(),
        project_root=project_root,
        actions=replay_actions,
        emit=emit,
        tool="repair-replay",
        phase="executing_replay",
        group="repair replay",
    )
    if skipped_commands:
        replay["skipped_commands"] = skipped_commands
    steps = replay.setdefault("steps", [])
    if isinstance(steps, list):
        steps.append(_execution_step(
            "replay",
            "Backend repair replay",
            bool(replay.get("ok")),
            f"ran={replay.get('ran')}",
            commands=replay_commands,
            failed=sum(1 for item in list(replay.get("results") or []) if isinstance(item, dict) and not item.get("ok")),
        ))
    replay_failed = sum(1 for item in list(replay.get("results") or []) if isinstance(item, dict) and not item.get("ok"))
    emit("tool_output", _harness_tool_output_payload(
        "repair-replay",
        "executing_replay",
        project_root=project_root,
        ok=bool(replay.get("ok")),
        summary=f"Replay ran {replay.get('ran')} command(s), failed={replay_failed}.",
        ran=replay.get("ran"),
        failed=replay_failed,
        commands=replay_commands,
        results=_shell_event_results(replay.get("results")),
        skipped_commands=skipped_commands,
    ))
    return replay


def _execution_step(kind: str, label: str, ok: bool, detail: str, **extra: object) -> dict[str, object]:
    step = {
        "id": f"{kind}-{uuid.uuid4().hex[:8]}",
        "kind": kind,
        "label": label,
        "ok": bool(ok),
        "detail": str(detail or "")[:500],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    step.update(extra)
    return step


def _shell_event_results(results: object) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for item in list(results or []):
        if not isinstance(item, dict):
            continue
        policy = item.get("policy")
        out.append({
            "command": str(item.get("command") or "")[:240],
            "ok": bool(item.get("ok")),
            "returncode": item.get("returncode"),
            "stdout_preview": str(item.get("stdout") or "")[:500],
            "stderr_preview": str(item.get("stderr") or "")[:500],
            "reason": str(item.get("reason") or "")[:240],
            "risk_level": policy.get("risk_level") if isinstance(policy, dict) else None,
            "synced_files": item.get("synced_files"),
        })
    return out


def _harness_tool_call_payload(tool: str, phase: str, *, project_root: str, summary: str, **extra: object) -> dict[str, object]:
    payload = {
        "kind": "agent_harness",
        "tool": tool,
        "phase": phase,
        "project_root": project_root,
        "summary": summary,
    }
    payload.update(extra)
    return payload


def _harness_tool_output_payload(tool: str, phase: str, *, project_root: str, ok: bool, summary: str, **extra: object) -> dict[str, object]:
    payload = {
        "kind": "agent_harness",
        "tool": tool,
        "ok": bool(ok),
        "phase": phase,
        "project_root": project_root,
        "summary": summary,
        "text": summary,
    }
    payload.update(extra)
    return payload


def _emit_command_start_events(emit, *, tool: str, phase: str, project_root: str, commands: list[str], group: str) -> None:
    for index, command in enumerate(commands):
        emit("tool_call", {
            "kind": "agent_harness_command",
            "tool": tool,
            "phase": phase,
            "project_root": project_root,
            "group": group,
            "index": index,
            "command": command,
            "summary": f"{group}: running `{command}`",
        })


def _emit_command_end_events(emit, *, tool: str, phase: str, project_root: str, results: object, group: str) -> None:
    for index, item in enumerate(_shell_event_results(results)):
        command = str(item.get("command") or "")
        ok = bool(item.get("ok"))
        emit("tool_output", {
            "kind": "agent_harness_command",
            "tool": tool,
            "phase": phase,
            "project_root": project_root,
            "group": group,
            "index": index,
            "command": command,
            "ok": ok,
            "status": "passed" if ok else "failed",
            "returncode": item.get("returncode"),
            "stdout_preview": item.get("stdout_preview"),
            "stderr_preview": item.get("stderr_preview"),
            "reason": item.get("reason"),
            "risk_level": item.get("risk_level"),
            "synced_files": item.get("synced_files"),
            "summary": f"{group}: {'passed' if ok else 'failed'} `{command}`",
            "text": f"{group}: {'passed' if ok else 'failed'} `{command}`",
        })


def _normalize_project_scoped_shell_command(command: str, *, cwd_label: str, project_root: str) -> tuple[str, str, str | None]:
    clean, stripped_capture = _strip_harmless_capture_redirect(command)
    current_cwd = str(cwd_label or ".").strip().strip("/") or "."
    root = str(project_root or ".").strip().strip("/") or "."
    try:
        parts = shlex.split(clean)
    except Exception:
        return clean, current_cwd, None
    normalized_parts = list(parts)
    note_parts: list[str] = []
    if stripped_capture:
        note_parts.append("Removed harmless shell stream redirection; Appora already captures stdout and stderr.")

    def target_points_to_project(target_value: str) -> bool:
        target = str(target_value or "").strip().strip("\"'")
        if not target:
            return False
        target_posix = target.replace("\\", "/").rstrip("/")
        root_posix = root.replace("\\", "/").rstrip("/")
        root_name = PurePosixPath(root_posix).name
        if target_posix in {root_posix, f"./{root_posix}", root_name, f"./{root_name}", "."}:
            return True
        return bool(root_name and PurePosixPath(target_posix).name == root_name)

    if len(parts) >= 4 and parts[0] == "cd" and parts[2] in _SHELL_CHAIN_OPERATORS:
        target = str(parts[1] or "").strip().strip("/")
        if target and not target.startswith(("/", "~")) and ".." not in PurePosixPath(target.replace("\\", "/")).parts:
            if target_points_to_project(target):
                normalized_parts = parts[3:]
                if normalized_parts:
                    current_cwd = root if current_cwd == "." and target not in {".", "./."} else current_cwd
                    note_parts.append(f"Normalized redundant `cd {target} &&` because backend already runs shell commands inside the project cwd.")
    elif len(parts) >= 5 and parts[0] == "cd" and str(parts[1]).lower() == "/d" and parts[3] in _SHELL_CHAIN_OPERATORS:
        target = str(parts[2] or "").strip()
        if target_points_to_project(target):
            normalized_parts = parts[4:]
            if normalized_parts:
                current_cwd = root if current_cwd == "." else current_cwd
                note_parts.append(
                    f"Normalized Windows-style `cd /d {target} &&` because Appora runs shell commands inside the Linux project cwd."
                )
    if len(normalized_parts) >= 3 and normalized_parts[0] == "python" and normalized_parts[1] == "-m" and normalized_parts[2] in {"compileall", "pytest", "unittest"}:
        normalized_parts[0] = "python3"
        note_parts.append("Normalized `python` to `python3` for Appora's local Python runtime.")
    if current_cwd == root and root != "." and normalized_parts:
        for index, value in enumerate(list(normalized_parts)):
            raw = str(value or "")
            if raw == root:
                normalized_parts[index] = "."
                note_parts.append("Localized project-root path argument because command cwd is already the project root.")
                continue
            prefix = root.rstrip("/") + "/"
            if raw.startswith(prefix):
                normalized_parts[index] = raw[len(prefix):] or "."
                note_parts.append("Localized project-prefixed path argument because command cwd is already the project root.")
    normalized_command = shlex.join(normalized_parts)
    if normalized_command == clean and not note_parts:
        return clean, current_cwd, None
    return normalized_command, current_cwd, " ".join(note_parts) or None


def _run_harness_shell_actions_internal(
    *,
    ws_root_path: Path,
    project_root: str,
    actions: list[AgentHarnessShellAction],
    emit=None,
    tool: str = "run-shell",
    phase: str = "executing_shell",
    group: str = "shell",
) -> dict[str, object]:
    results: list[dict] = []
    for index, action in enumerate(actions[:8]):
        original_command = str(action.command or "").strip()
        command = original_command
        cwd_label = str(action.cwd or project_root or ".").strip().strip("/") or "."
        command, cwd_label, normalization_note = _normalize_project_scoped_shell_command(command, cwd_label=cwd_label, project_root=project_root)
        policy = _command_policy_decision(command, project_root=project_root)
        if not policy.ok:
            result = {
                "command": command,
                "original_command": original_command if original_command != command else None,
                "ok": False,
                "stdout": "",
                "stderr": policy.reason,
                "returncode": 126,
                "policy": policy.model_dump(),
                "synced_files": 0,
                "reason": action.reason,
                "normalization": normalization_note,
            }
            results.append(result)
            if emit:
                _emit_command_end_events(emit, tool=tool, phase=phase, project_root=project_root, results=[result], group=group)
            continue

        try:
            cwd = safe_join(ws_root_path, cwd_label)
        except ValueError as exc:
            result = {
                "command": command,
                "original_command": original_command if original_command != command else None,
                "ok": False,
                "stdout": "",
                "stderr": str(exc),
                "returncode": 126,
                "policy": policy.model_dump(),
                "synced_files": 0,
                "reason": action.reason,
                "normalization": normalization_note,
            }
            results.append(result)
            if emit:
                _emit_command_end_events(emit, tool=tool, phase=phase, project_root=project_root, results=[result], group=group)
            continue

        def emit_chunk(stream: str, chunk: str, command_index: int = index, command_text: str = command) -> None:
            if not emit or not chunk:
                return
            emit("tool_output", {
                "kind": "agent_harness_command_chunk",
                "tool": tool,
                "phase": phase,
                "project_root": project_root,
                "group": group,
                "index": command_index,
                "command": command_text,
                "stream": stream,
                "chunk": chunk[-2000:],
                "summary": f"{group}: {stream} chunk from `{command_text}`",
                "text": chunk[-2000:],
            })

        result = _run_shell_command_streaming(command, cwd, emit_chunk) if emit else _run_shell_command(command, cwd)
        result["command"] = command
        result["original_command"] = original_command if original_command != command else None
        result["policy"] = policy.model_dump()
        result["synced_files"] = _sync_hosted_project_text_files_after_shell(ws_root_path, cwd)
        result["reason"] = action.reason
        result["normalization"] = normalization_note
        results.append(result)
        if emit:
            _emit_command_end_events(emit, tool=tool, phase=phase, project_root=project_root, results=[result], group=group)

    return {
        "ok": all(bool(item.get("ok")) for item in results),
        "project_root": project_root,
        "ran": len(results),
        "results": results,
    }


def agent_harness_run_shell(req: AgentHarnessRunShellReq):
    ws_root = _session_state().get("workspace")
    if not ws_root:
        raise HTTPException(400, "No workspace selected")

    ws_root_path = Path(ws_root)
    project_root = str(req.project_root or ".").strip().strip("/") or "."
    _hydrate_hosted_project(ws_root_path, project_root)

    return _run_harness_shell_actions_internal(
        ws_root_path=ws_root_path,
        project_root=project_root,
        actions=req.actions,
    )


app.include_router(build_command_router(
    session_state=_session_state,
    command_policy_decision=_command_policy_decision,
    run_shell_command=_run_shell_command,
    sync_hosted_project_text_files_after_shell=_sync_hosted_project_text_files_after_shell,
    hydrate_hosted_project=_hydrate_hosted_project,
    run_harness_shell_actions_internal=_run_harness_shell_actions_internal,
))


def _split_validation_commands_by_existing_shell_evidence(validation_commands: list[str], shell_result: dict[str, object] | None) -> tuple[list[str], list[dict]]:
    if not validation_commands or not isinstance(shell_result, dict):
        return validation_commands, []
    passed_by_command: dict[str, dict] = {}
    for item in list(shell_result.get("results") or []):
        if not isinstance(item, dict) or item.get("ok") is not True:
            continue
        command = str(item.get("command") or "").strip()
        if command:
            passed_by_command[command] = item

    pending: list[str] = []
    reused: list[dict] = []
    for command in validation_commands:
        clean = str(command or "").strip()
        existing = passed_by_command.get(clean)
        if existing is None:
            pending.append(command)
            continue
        reused_result = dict(existing)
        reused_result["command"] = clean
        reused_result["reused_from"] = "shell"
        reused.append(reused_result)
    return pending, reused


_EXECUTION_LEDGER_PHASES = {
    "apply": "edit",
    "shell": "run",
    "validation": "verify",
    "preview_audit": "inspect",
    "replay": "verify",
    "repair": "repair",
    "repair_stop": "blocked",
    "completion": "complete",
}


def _execution_run_ledger(execution: dict[str, object]) -> list[dict[str, object]]:
    ledger: list[dict[str, object]] = [{
        "id": "backend-observe",
        "index": 0,
        "phase": "observe",
        "kind": "observe",
        "label": "Backend execution accepted",
        "status": "passed",
        "ok": True,
        "detail": f"project_root={execution.get('project_root') or '.'}",
    }]
    steps = execution.get("steps")
    if not isinstance(steps, list):
        return ledger
    for index, raw_step in enumerate(steps, start=1):
        if not isinstance(raw_step, dict):
            continue
        kind = str(raw_step.get("kind") or "execution").strip() or "execution"
        phase = _EXECUTION_LEDGER_PHASES.get(kind, "execute")
        if kind == "completion" and str(raw_step.get("state") or "") == "blocked":
            phase = "blocked"
        ok = bool(raw_step.get("ok"))
        status = "passed" if ok else "failed"
        if raw_step.get("skipped") is True:
            status = "skipped"
        failure_analysis = raw_step.get("failure_analysis")
        if not isinstance(failure_analysis, dict):
            failure_analysis = raw_step.get("pre_repair_failure_analysis")
        if not isinstance(failure_analysis, dict):
            failure_analysis = {}
        entry: dict[str, object] = {
            "id": str(raw_step.get("id") or f"backend-step-{index}"),
            "index": index,
            "phase": phase,
            "kind": kind,
            "label": str(raw_step.get("label") or kind),
            "status": status,
            "ok": ok,
            "detail": str(raw_step.get("detail") or "")[:500],
            "created_at": raw_step.get("created_at"),
        }
        for key in ("repair_index", "state", "attempts", "max_repair_passes"):
            if raw_step.get(key) is not None:
                entry[key] = raw_step.get(key)
        next_action = str(raw_step.get("next_action") or failure_analysis.get("suggested_next_move") or "").strip()
        failure_signature = str(failure_analysis.get("current_signature") or "").strip()
        diagnosis = str(failure_analysis.get("summary") or "").strip()
        if next_action:
            entry["next_action"] = next_action[:600]
        if failure_signature:
            entry["failure_signature"] = failure_signature
        if diagnosis:
            entry["diagnosis"] = diagnosis[:600]
        ledger.append(entry)
    return ledger[:40]


def _auto_execute_preview_audit(req: AgentReq, project_root: str) -> dict[str, object] | None:
    preview_url = str(getattr(req, "preview_url", None) or "").strip()
    started_preview: dict[str, object] | None = None
    if not preview_url:
        try:
            started = run_start(
                RunStartReq(project_root=project_root),
                Request({
                    "type": "http",
                    "method": "POST",
                    "path": "/api/run/start",
                    "headers": [],
                    "scheme": "http",
                    "server": ("localhost", 80),
                    "client": ("127.0.0.1", 0),
                    "root_path": "",
                    "app": app,
                }),
            )
            if isinstance(started, dict):
                started_preview = started
                preview_url = str(started.get("direct_url") or started.get("url") or "").strip()
        except HTTPException as exc:
            return {
                "ok": True,
                "skipped": True,
                "reason": f"preview start skipped: {exc.detail}",
                "preview_url": "",
                "audit_mode": "unavailable",
                "issues": [],
                "issue_details": [],
                "summary": f"preview audit skipped: {exc.detail}",
            }
        except Exception as exc:
            return {
                "ok": True,
                "skipped": True,
                "reason": f"preview start failed: {str(exc)[:300]}",
                "preview_url": "",
                "audit_mode": "unavailable",
                "issues": [],
                "issue_details": [],
                "summary": f"preview audit skipped: {str(exc)[:240]}",
            }
    if not preview_url:
        return None
    try:
        audit = preview_audit(
            PreviewAuditReq(
                preview_url=preview_url,
                project_root=project_root,
                attempts=2,
                max_excerpt_chars=1200,
                mode="auto",
            )
        )
        if started_preview and isinstance(audit, dict):
            audit["started_preview"] = {
                "id": started_preview.get("id"),
                "url": started_preview.get("url"),
                "direct_url": started_preview.get("direct_url"),
            }
        return audit
    except HTTPException as exc:
        return {
            "ok": True,
            "skipped": True,
            "reason": str(exc.detail),
            "preview_url": preview_url,
            "audit_mode": "unavailable",
            "issues": [],
            "issue_details": [],
            "summary": f"preview audit skipped: {exc.detail}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc)[:500],
            "preview_url": preview_url,
            "audit_mode": "error",
            "issues": [f"Preview audit failed: {exc}"],
            "issue_details": [{"severity": "blocking", "category": "preview-audit", "detail": str(exc)[:300], "suggested_fix": "Pastikan preview URL aktif dan bisa diakses backend."}],
            "summary": f"preview audit failed: {str(exc)[:240]}",
        }


def _project_has_preview_surface(project_dir: Path, out_changes: list[dict[str, object]]) -> bool:
    if (project_dir / "package.json").exists() or (project_dir / "index.html").exists():
        return True
    # A lone frontend-looking file is not enough to start a preview. The
    # runner needs either a JS package or a static index.html; otherwise
    # http.server only exposes a directory listing and the audit becomes
    # noisy/flaky instead of useful execution evidence.
    frontend_suffixes = {".html"}
    for change in out_changes:
        if not isinstance(change, dict):
            continue
        rel = str(change.get("path") or "").strip()
        suffix = PurePosixPath(rel).suffix.lower()
        if suffix in frontend_suffixes:
            return True
    return False


def _is_surgical_shadcn_import_repair(req: AgentReq, out_changes: list[dict[str, object]]) -> bool:
    prompt = str(req.input or "").lower()
    if "import" not in prompt or "build" not in prompt:
        return False
    if "shadcn" not in prompt and "components/ui" not in prompt:
        return False
    if any(term in prompt for term in ("preview", "blank", "redesign", "landing", "dashboard baru", "bikin", "buat")):
        return False
    paths = [
        str(change.get("path") or "").strip()
        for change in list(out_changes or [])
        if isinstance(change, dict)
    ]
    if not paths:
        return False
    allowed_suffixes = (
        "components.json",
        "package.json",
        "vite.config.ts",
        "vite.config.js",
        "vite.config.mts",
        "vite.config.mjs",
    )
    for path in paths:
        local = path.split("/", 1)[1] if "/" in path and not path.startswith("src/") else path
        if "/components/ui/" in path or local in allowed_suffixes or local.startswith("src/lib/"):
            continue
        return False
    return True


def _run_backend_repair_pass(req: AgentReq, execution: dict[str, object], emit, *, repair_index: int) -> dict[str, object]:
    project_root = str(req.project_root or ".").strip().strip("/") or "."
    failure_analysis = _execution_failure_analysis(execution)
    polish_debt = _preview_polish_debt(execution)
    state_readiness_debt = _preview_state_readiness_debt(execution)
    emit("status", {"phase": "executing_repair", "message": f"Backend harness running repair pass {repair_index} from execution evidence..."})
    repair_prompt = "\n\n".join([
        part for part in [
            f"BACKEND AUTO-EXECUTE REPAIR PASS {repair_index}:",
            "The previous backend execution produced failing apply/shell/validation/preview evidence.",
            "If build/validation already passes but preview audit still has production-polish warnings, treat those warnings as the active objective and return concrete source fixes.",
            "Production-polish warning fixes include: specific document title/meta description, accessible tap target sizing, removing loose any/as any, eliminating generic copy, and improving visible product depth.",
            "If failure_analysis.primary_failure starts with preview audit, do not return shell-only or install-only actions; return concrete TSX/CSS/HTML source changes and let backend rerun build/preview.",
            "Use preview_audit.visual_evidence, visual_summary, screenshot_path, and evidence_pack as the visual source of truth. If visual evidence says the screen is blank, overflowing, sparse, or showing the wrong route, repair that visible result before claiming completion.",
            "Repair the project now with concrete file changes and only safe project-scoped shell actions if needed.",
            "If a scaffold generator command such as npm create/npx init was blocked, do not retry it. Create or repair package.json, index.html, src files, and CSS directly, then use npm install/npm run build.",
            "If earlier repair passes failed, use their evidence and choose a different concrete fix.",
            "Use the failure_analysis signature to detect repeated failures. If repeated_failure is true, change strategy instead of making the same local edit again.",
            "Use failure_analysis.summary and failure_analysis.suggested_next_move as the repair objective.",
            "If command failures were already replayed successfully by earlier repairs, focus the remaining preview, responsive, source-quality, or apply blockers.",
            "If the active failure is preview_audit/responsive/visual quality, return CSS/layout/component changes; install-only shell actions do not resolve preview blockers.",
            "If the active failure is a TypeScript/JSX parse error, rewrite the complete failing component/file into valid TSX instead of patching a small JSX fragment.",
            "Prefer simple, balanced JSX structure over clever inline expressions during repair; one valid full-file replacement is better than multiple risky local patches.",
            "If the only remaining preview issues are state-loading/state-error/state-empty, do not redesign or rewrite the page. Add small relevant loading/error/empty UI branches near the existing data/form/list flow, or explain why the page is static if no dynamic flow exists.",
            "Do not repeat the same failing command blindly unless your changes address the failure.",
            f"Original user request:\n{req.input}",
            f"Failure analysis:\n{json.dumps(failure_analysis, ensure_ascii=False, indent=2)}",
            f"Preview polish debt:\n{json.dumps(polish_debt, ensure_ascii=False, indent=2)}",
            f"Preview state-readiness debt:\n{json.dumps(state_readiness_debt, ensure_ascii=False, indent=2)}",
            _preview_repair_targets_report(execution),
            f"Current file context after failed execution:\n{_repair_file_context(project_root, execution)}",
            f"Repair replay plan:\n{_repair_replay_plan(execution)}",
            f"Execution evidence:\n{_execution_repair_report(execution)}",
        ]
        if str(part or "").strip()
    ])
    repair_req = AgentReq(
        input=repair_prompt,
        mode=req.mode,
        active_file=req.active_file,
        selection=req.selection,
        current_content=req.current_content,
        open_files=req.open_files,
        project_root=req.project_root,
        build_mode=req.build_mode,
        preview_url=req.preview_url,
        editor_status="Backend repair after validation failure",
        asset_paths=req.asset_paths,
        asset_aliases=req.asset_aliases,
        stream=False,
        background=False,
        auto_execute=False,
    )
    try:
        with _agent_lock_for_current_provider():
            repair_pipeline = run_agent_pipeline(repair_req, ws_root=_ws(), emit=emit)
    except Exception as exc:
        message = str(exc or "Backend repair model call failed.")[:500]
        repair_execution: dict[str, object] = {
            "auto_execute": True,
            "project_root": project_root,
            "ok": False,
            "steps": [
                _execution_step(
                    "repair_model",
                    "Backend repair model call",
                    False,
                    message,
                    repair_index=repair_index,
                )
            ],
            "apply": None,
            "shell": None,
            "validation": None,
            "preview_audit": execution.get("preview_audit") if isinstance(execution.get("preview_audit"), dict) else None,
            "repairs": [],
            "failure_analysis": {
                "current_signature": "repair-provider-error",
                "failure_count": 1,
                "active_failure_count": 1,
                "resolved_failure_count": 0,
                "failures": [{"kind": "repair_provider", "detail": message}],
                "resolved_failures": [],
                "evidence_excerpt": message,
                "primary_failure": message,
                "summary": f"Backend repair model call failed: {message}",
                "suggested_next_move": "Check 9Router/API key or provider quota, then retry the repair pass.",
                "prior_signatures": [],
                "repeated_failure": False,
                "repeated_count": 0,
            },
        }
        repair_execution["completion_report"] = _execution_completion_report(repair_execution)
        emit("tool_output", _harness_tool_output_payload(
            "repair-model",
            "executing_repair",
            project_root=project_root,
            ok=False,
            summary=f"Backend repair model call failed: {message}",
            repair_index=repair_index,
        ))
        return {
            "spoken": "",
            "log": f"repair_provider_error={message}",
            "changes": [],
            "actions": [],
            "intent": {},
            "trace": {},
            "pre_repair_failure_analysis": failure_analysis,
            "execution": repair_execution,
            "provider_error": message,
        }
    repair_changes = _prepare_agent_out_changes(_ws(), list(repair_pipeline.get("changes") or []))
    repair_actions = list(repair_pipeline.get("actions") or [])
    repair_execution = _auto_execute_agent_result(repair_req, repair_changes, repair_actions, emit, allow_repair=False)
    if isinstance(repair_execution, dict):
        replay = _run_repair_replay(project_root, execution, repair_actions, emit)
        if isinstance(replay, dict):
            repair_execution["replay"] = replay
            repair_execution["ok"] = bool(repair_execution.get("ok")) and bool(replay.get("ok"))
            steps = repair_execution.setdefault("steps", [])
            if isinstance(steps, list):
                steps.append(_execution_step(
                    "replay",
                    "Backend repair replay",
                    bool(replay.get("ok")),
                    f"ran={replay.get('ran')}",
                    commands=[
                        str(item.get("command") or "")
                        for item in list(replay.get("results") or [])
                        if isinstance(item, dict)
                    ],
                    failed=sum(1 for item in list(replay.get("results") or []) if isinstance(item, dict) and not item.get("ok")),
                ))
        repair_execution["failure_analysis"] = _execution_failure_analysis(repair_execution)
        repair_execution["completion_report"] = _execution_completion_report(repair_execution)
    return {
        "spoken": repair_pipeline.get("spoken") or "",
        "log": repair_pipeline.get("log") or "",
        "changes": repair_changes,
        "actions": repair_actions,
        "intent": dict(repair_pipeline.get("intent") or {}),
        "trace": dict(repair_pipeline.get("trace") or {}),
        "pre_repair_failure_analysis": failure_analysis,
        "execution": repair_execution,
    }


def _auto_execute_agent_result(req: AgentReq, out_changes: list[dict[str, object]], actions: list[dict], emit, *, allow_repair: bool = True, max_repair_passes: int = 3) -> dict:
    project_root = str(req.project_root or ".").strip().strip("/") or "."
    max_repair_passes = max(0, min(int(max_repair_passes or 0), 5))
    execution: dict[str, object] = {
        "auto_execute": True,
        "project_root": project_root,
        "max_repair_passes": max_repair_passes if allow_repair else 0,
        "steps": [],
        "apply": None,
        "shell": None,
        "validation": None,
        "preview_audit": None,
        "repairs": [],
        "failure_analysis": None,
        "completion_report": None,
        "ok": True,
    }

    shell_actions = _order_agent_shell_actions(_agent_shell_actions(actions))

    if out_changes:
        apply_paths = [
            str(change.get("path") or "")
            for change in out_changes
            if str(change.get("path") or "").strip() and isinstance(change.get("new_content"), str)
        ]
        emit("status", {"phase": "executing_apply", "message": "Backend harness applying agent changes..."})
        emit("tool_call", _harness_tool_call_payload(
            "apply",
            "executing_apply",
            project_root=project_root,
            summary=f"Applying {len(apply_paths)} file change(s).",
            paths=apply_paths,
            count=len(apply_paths),
        ))
        apply_req = AgentHarnessApplyReq(
            project_root=project_root,
            label="Backend auto execute",
            changes=[
                AgentHarnessApplyChange(
                    path=str(change.get("path") or ""),
                    content=str(change.get("new_content") or ""),
                    diff=str(change.get("diff") or "") or None,
                    expected_sha256=str(change.get("old_sha256") or "") or None,
                    expected_exists=change.get("old_exists") if isinstance(change.get("old_exists"), bool) else None,
                    old_sha256=str(change.get("old_sha256") or "") or None,
                    old_exists=change.get("old_exists") if isinstance(change.get("old_exists"), bool) else None,
                )
                for change in out_changes
                if str(change.get("path") or "").strip() and isinstance(change.get("new_content"), str)
            ],
        )
        apply_result = agent_harness_apply(apply_req)
        execution["apply"] = apply_result
        execution["ok"] = bool(execution["ok"]) and bool(apply_result.get("ok"))
        steps = execution.setdefault("steps", [])
        if isinstance(steps, list):
            steps.append(_execution_step(
                "apply",
                "Backend apply harness",
                bool(apply_result.get("ok")),
                f"applied={apply_result.get('applied')} count={apply_result.get('count')}",
                paths=apply_result.get("paths") or [],
                checkpoint_path=apply_result.get("checkpoint_path"),
                conflicts=apply_result.get("conflicts") or [],
                warnings=apply_result.get("warnings") or [],
            ))
        emit("tool_output", _harness_tool_output_payload(
            "apply",
            "executing_apply",
            project_root=project_root,
            ok=bool(apply_result.get("ok")),
            summary=f"Apply {'completed' if apply_result.get('ok') else 'failed'}: applied={apply_result.get('applied')} count={apply_result.get('count')}.",
            applied=apply_result.get("applied"),
            count=apply_result.get("count"),
            paths=apply_result.get("paths") or [],
            checkpoint_path=apply_result.get("checkpoint_path"),
            conflicts=apply_result.get("conflicts") or [],
            warnings=apply_result.get("warnings") or [],
        ))

    if shell_actions:
        shell_commands = [action.command for action in shell_actions]
        emit("status", {"phase": "executing_shell", "message": "Backend harness running agent shell actions..."})
        emit("tool_call", _harness_tool_call_payload(
            "run-shell",
            "executing_shell",
            project_root=project_root,
            summary=f"Running {len(shell_commands)} shell command(s).",
            commands=shell_commands,
            count=len(shell_commands),
        ))
        _emit_command_start_events(emit, tool="run-shell", phase="executing_shell", project_root=project_root, commands=shell_commands, group="shell")
        shell_result = _run_harness_shell_actions_internal(
            ws_root_path=_ws(),
            project_root=project_root,
            actions=shell_actions,
            emit=emit,
            tool="run-shell",
            phase="executing_shell",
            group="shell",
        )
        execution["shell"] = shell_result
        execution["ok"] = bool(execution["ok"]) and bool(shell_result.get("ok"))
        steps = execution.setdefault("steps", [])
        if isinstance(steps, list):
            steps.append(_execution_step(
                "shell",
                "Backend shell harness",
                bool(shell_result.get("ok")),
                f"ran={shell_result.get('ran')}",
                commands=shell_commands,
                failed=sum(
                    1
                    for item in list(shell_result.get("results") or [])
                    if isinstance(item, dict) and not item.get("ok")
                ),
            ))
        shell_failed = sum(1 for item in list(shell_result.get("results") or []) if isinstance(item, dict) and not item.get("ok"))
        emit("tool_output", _harness_tool_output_payload(
            "run-shell",
            "executing_shell",
            project_root=project_root,
            ok=bool(shell_result.get("ok")),
            summary=f"Shell ran {shell_result.get('ran')} command(s), failed={shell_failed}.",
            ran=shell_result.get("ran"),
            failed=shell_failed,
            commands=shell_commands,
            results=_shell_event_results(shell_result.get("results")),
        ))

    if out_changes or shell_actions:
        try:
            project_dir = safe_join(_ws(), project_root)
            validation_commands = _infer_validation_commands(project_dir)[:4]
        except Exception:
            validation_commands = []
        if validation_commands:
            pending_validation_commands, reused_validation_results = _split_validation_commands_by_existing_shell_evidence(
                validation_commands,
                execution.get("shell") if isinstance(execution.get("shell"), dict) else None,
            )
            emit("status", {"phase": "executing_validation", "message": "Backend harness validating project output..."})
            emit("tool_call", _harness_tool_call_payload(
                "validate",
                "executing_validation",
                project_root=project_root,
                summary=(
                    f"Running {len(pending_validation_commands)} validation command(s), "
                    f"reusing {len(reused_validation_results)} shell result(s)."
                ),
                commands=validation_commands,
                count=len(validation_commands),
                pending=len(pending_validation_commands),
                reused=len(reused_validation_results),
            ))
            validation_results = list(reused_validation_results)
            if pending_validation_commands:
                _emit_command_start_events(emit, tool="validate", phase="executing_validation", project_root=project_root, commands=pending_validation_commands, group="validation")
                validation_shell = _run_harness_shell_actions_internal(
                    ws_root_path=_ws(),
                    project_root=project_root,
                    actions=[
                        AgentHarnessShellAction(command=command, cwd=project_root, reason="Backend auto validation")
                        for command in pending_validation_commands
                    ],
                    emit=emit,
                    tool="validate",
                    phase="executing_validation",
                    group="validation",
                )
                validation_results.extend(list(validation_shell.get("results") or []))
            validation = {
                "ok": all(bool(item.get("ok")) for item in validation_results if isinstance(item, dict)) if validation_results else True,
                "project_root": project_root,
                "commands": validation_commands,
                "results": validation_results,
                "ran": max(0, len(validation_results) - len(reused_validation_results)),
                "reused": len(reused_validation_results),
                "passed": sum(1 for item in validation_results if isinstance(item, dict) and item.get("ok")),
                "failed": sum(1 for item in validation_results if isinstance(item, dict) and not item.get("ok")),
            }
            execution["validation"] = validation
            execution["ok"] = bool(execution["ok"]) and bool(validation.get("ok"))
            steps = execution.setdefault("steps", [])
            if isinstance(steps, list):
                steps.append(_execution_step(
                    "validation",
                    "Backend validation",
                    bool(validation.get("ok")),
                    f"ran={validation.get('ran')} failed={validation.get('failed')}",
                    commands=validation.get("commands") or [],
                    failed=validation.get("failed"),
                ))
            emit("tool_output", _harness_tool_output_payload(
                "validate",
                "executing_validation",
                project_root=project_root,
                ok=bool(validation.get("ok")),
                summary=f"Validation ran {validation.get('ran')} command(s), failed={validation.get('failed')}.",
                commands=validation.get("commands") or [],
                ran=validation.get("ran"),
                passed=validation.get("passed"),
                failed=validation.get("failed"),
                results=_shell_event_results(validation.get("results")),
            ))

    try:
        preview_project_dir = safe_join(_ws(), project_root)
    except Exception:
        preview_project_dir = _ws()
    preview_gate_required = not _is_surgical_shadcn_import_repair(req, out_changes)
    if preview_gate_required and (out_changes or shell_actions) and (str(getattr(req, "preview_url", None) or "").strip() or _project_has_preview_surface(preview_project_dir, out_changes)):
        emit("status", {"phase": "executing_preview_audit", "message": "Backend harness auditing live preview..."})
        preview_result = _auto_execute_preview_audit(req, project_root)
        if isinstance(preview_result, dict):
            execution["preview_audit"] = preview_result
            if not preview_result.get("skipped"):
                execution["ok"] = bool(execution["ok"]) and bool(preview_result.get("ok"))
            issue_details = list(preview_result.get("issue_details") or [])
            blocking = sum(1 for item in issue_details if isinstance(item, dict) and item.get("severity") == "blocking")
            warnings = sum(1 for item in issue_details if isinstance(item, dict) and item.get("severity") == "warning")
            steps = execution.setdefault("steps", [])
            if isinstance(steps, list):
                steps.append(_execution_step(
                    "preview_audit",
                    "Backend preview audit",
                    bool(preview_result.get("ok")) or bool(preview_result.get("skipped")),
                    str(preview_result.get("repair_brief") or preview_result.get("summary") or ""),
                    audit_mode=preview_result.get("audit_mode"),
                    skipped=bool(preview_result.get("skipped")),
                    blocking=blocking,
                    warnings=warnings,
                    visual_summary=preview_result.get("visual_summary") or {},
                    evidence_pack=preview_result.get("evidence_pack") or {},
                    repair_brief=preview_result.get("repair_brief"),
                ))
            emit(
                "tool_output",
                _harness_tool_output_payload(
                    "preview-audit",
                    "executing_preview_audit",
                    project_root=project_root,
                    ok=bool(preview_result.get("ok")) or bool(preview_result.get("skipped")),
                    summary=str(preview_result.get("summary") or "Preview audit finished."),
                    audit_mode=preview_result.get("audit_mode"),
                    skipped=preview_result.get("skipped"),
                    result={
                        "ok": preview_result.get("ok"),
                        "skipped": preview_result.get("skipped"),
                        "audit_mode": preview_result.get("audit_mode"),
                        "summary": preview_result.get("summary"),
                    },
                    text=json.dumps({
                        "ok": preview_result.get("ok"),
                        "skipped": preview_result.get("skipped"),
                        "audit_mode": preview_result.get("audit_mode"),
                        "summary": preview_result.get("summary"),
                    }, ensure_ascii=False)[:1200],
                ),
            )

    execution["failure_analysis"] = _execution_failure_analysis(execution)
    for quick_repair_fn in (
        _try_quick_vite_entrypoint_repair,
        _try_quick_missing_h1_repair,
        _try_quick_missing_package_repair,
        _try_quick_vite_alias_repair,
        _try_quick_ts2304_react_hook_repair,
        _try_quick_ts2741_missing_required_prop_repair,
        _try_quick_ts6133_repair,
        _try_quick_ts2322_unsupported_prop_repair,
        _try_quick_preview_polish_repair,
    ):
        if bool(execution.get("ok")) and not _execution_needs_repair(execution):
            break
        quick_repair = quick_repair_fn(req, execution, emit)
        if not isinstance(quick_repair, dict):
            continue
        execution["quick_repair"] = quick_repair
        if quick_repair.get("ok"):
            quick_shell = quick_repair.get("shell") if isinstance(quick_repair.get("shell"), dict) else {}
            quick_preview = quick_repair.get("preview_audit") if isinstance(quick_repair.get("preview_audit"), dict) else None
            if quick_preview is not None:
                execution["preview_audit"] = quick_preview
            commands = list(quick_repair.get("commands") or [])
            validation = {
                "ok": True,
                "project_root": project_root,
                "commands": commands,
                "results": list(quick_shell.get("results") or []),
                "ran": len(list(quick_shell.get("results") or [])),
                "passed": sum(1 for item in list(quick_shell.get("results") or []) if isinstance(item, dict) and item.get("ok")),
                "failed": sum(1 for item in list(quick_shell.get("results") or []) if isinstance(item, dict) and not item.get("ok")),
                "quick_repair": str(quick_repair.get("kind") or "ts6133-unused-symbol"),
            }
            execution["validation"] = validation
            if isinstance(execution.get("shell"), dict) and not execution["shell"].get("ok"):
                failed_shell_commands = {
                    str(item.get("command") or "").strip()
                    for item in list(execution["shell"].get("results") or [])
                    if isinstance(item, dict) and item.get("ok") is False
                }
                if failed_shell_commands and failed_shell_commands.issubset({str(item).strip() for item in commands}):
                    execution["shell"] = quick_shell
            preview_ok = True
            preview_audit = execution.get("preview_audit")
            if isinstance(preview_audit, dict) and not preview_audit.get("skipped"):
                preview_ok = bool(preview_audit.get("ok"))
            execution["ok"] = bool((execution.get("apply") or {}).get("ok", True)) and bool((execution.get("shell") or {}).get("ok", True)) and bool(validation.get("ok")) and preview_ok
            execution["failure_analysis"] = _execution_failure_analysis(execution)
            steps = execution.setdefault("steps", [])
            if isinstance(steps, list):
                steps.append(_execution_step(
                    "quick_repair",
                    "Backend quick repair",
                    True,
                    str(quick_repair.get("summary") or ""),
                    paths=quick_repair.get("changed_paths") or [],
                    commands=commands,
                ))
    if allow_repair and (_execution_has_primary_failure(execution) or _preview_polish_debt_requires_llm_repair(execution)):
        for repair_index in range(1, max_repair_passes + 1):
            if not _execution_needs_repair(execution) and not _preview_polish_debt_requires_llm_repair(execution):
                break
            repair = _run_backend_repair_pass(req, execution, emit, repair_index=repair_index)
            repairs = execution.setdefault("repairs", [])
            if isinstance(repairs, list):
                repairs.append(repair)
            repair_execution = repair.get("execution") if isinstance(repair, dict) else None
            execution["last_repair_execution"] = repair_execution
            steps = execution.setdefault("steps", [])
            if isinstance(steps, list):
                repair_failure_analysis = repair_execution.get("failure_analysis") if isinstance(repair_execution, dict) else None
                pre_repair_failure_analysis = repair.get("pre_repair_failure_analysis") if isinstance(repair, dict) else None
                steps.append(_execution_step(
                    "repair",
                    "Backend repair pass",
                    bool(repair_execution.get("ok")) if isinstance(repair_execution, dict) else False,
                    f"changes={len(list(repair.get('changes') or [])) if isinstance(repair, dict) else 0}",
                    repair_index=repair_index,
                    pre_repair_failure_analysis=pre_repair_failure_analysis or {},
                    failure_analysis=repair_failure_analysis or {},
                    repeated_failure=bool(pre_repair_failure_analysis.get("repeated_failure")) if isinstance(pre_repair_failure_analysis, dict) else False,
                ))
            parent_before_repair = execution
            if isinstance(repair_execution, dict) and repair_execution.get("ok") and _preview_polish_debt(repair_execution):
                quick_repair = _try_quick_preview_polish_repair(req, repair_execution, emit)
                if isinstance(quick_repair, dict):
                    repair["quick_repair"] = quick_repair
                    repair_execution["quick_repair"] = quick_repair
                    if quick_repair.get("ok"):
                        quick_shell = quick_repair.get("shell") if isinstance(quick_repair.get("shell"), dict) else {}
                        quick_preview = quick_repair.get("preview_audit") if isinstance(quick_repair.get("preview_audit"), dict) else None
                        if quick_preview is not None:
                            repair_execution["preview_audit"] = quick_preview
                        commands = list(quick_repair.get("commands") or [])
                        validation = {
                            "ok": True,
                            "project_root": project_root,
                            "commands": commands,
                            "results": list(quick_shell.get("results") or []),
                            "ran": len(list(quick_shell.get("results") or [])),
                            "passed": sum(1 for item in list(quick_shell.get("results") or []) if isinstance(item, dict) and item.get("ok")),
                            "failed": sum(1 for item in list(quick_shell.get("results") or []) if isinstance(item, dict) and not item.get("ok")),
                            "quick_repair": str(quick_repair.get("kind") or "preview-polish"),
                        }
                        repair_execution["validation"] = validation
                        preview_ok = True
                        preview_audit = repair_execution.get("preview_audit")
                        if isinstance(preview_audit, dict) and not preview_audit.get("skipped"):
                            preview_ok = bool(preview_audit.get("ok"))
                        repair_execution["ok"] = (
                            bool((repair_execution.get("apply") or {}).get("ok", True))
                            and bool((repair_execution.get("shell") or {}).get("ok", True))
                            and bool(validation.get("ok"))
                            and preview_ok
                        )
                        repair_execution["failure_analysis"] = _execution_failure_analysis(repair_execution)
            repair_ok = _repair_resolves_parent_execution(execution, repair_execution if isinstance(repair_execution, dict) else None)
            repair_failure_analysis = repair_execution.get("failure_analysis") if isinstance(repair_execution, dict) else None
            rollback_result = None
            if isinstance(repair_execution, dict) and not repair_ok and _repair_execution_degrades_parent(parent_before_repair, repair_execution):
                rollback_result = _rollback_repair_checkpoint(repair_execution)
                if isinstance(repair, dict):
                    repair["rollback"] = rollback_result
                emit("status", {
                    "phase": "executing_rollback",
                    "message": "Backend harness rolled back a degrading repair checkpoint before the next pass.",
                })
                emit("tool_output", _harness_tool_output_payload(
                    "rollback",
                    "executing_rollback",
                    project_root=project_root,
                    ok=bool(isinstance(rollback_result, dict) and rollback_result.get("ok")),
                    summary=(
                        f"Rolled back repair checkpoint {rollback_result.get('checkpoint_path')}."
                        if isinstance(rollback_result, dict) and rollback_result.get("ok")
                        else "Repair rollback failed or no checkpoint was available."
                    ),
                    result=rollback_result or {},
                ))
            emit("tool_output", _harness_tool_output_payload(
                "repair",
                "executing_repair",
                project_root=project_root,
                ok=repair_ok,
                summary=(
                    f"Repair pass {repair_index} {'completed' if repair_ok else 'still failing'}: "
                    f"changes={len(list(repair.get('changes') or [])) if isinstance(repair, dict) else 0} "
                    f"actions={len(list(repair.get('actions') or [])) if isinstance(repair, dict) else 0}."
                ),
                repair_index=repair_index,
                changes=len(list(repair.get("changes") or [])) if isinstance(repair, dict) else 0,
                actions=len(list(repair.get("actions") or [])) if isinstance(repair, dict) else 0,
                failure_analysis=repair_failure_analysis if isinstance(repair_failure_analysis, dict) else {},
                rollback=rollback_result or None,
            ))
            repair_rolled_back = bool(isinstance(rollback_result, dict) and rollback_result.get("ok"))
            if isinstance(repair_execution, dict) and not repair_rolled_back:
                polish_only_parent = bool(parent_before_repair.get("ok")) and not _execution_has_primary_failure(parent_before_repair) and bool(_preview_polish_debt(parent_before_repair))
                if repair_ok:
                    _merge_repair_execution_state(execution, repair_execution)
                    execution["ok"] = True
                elif polish_only_parent:
                    _merge_repair_execution_state(execution, repair_execution)
                    execution["ok"] = True
                else:
                    _merge_repair_execution_state(execution, repair_execution)
            elif repair_rolled_back:
                execution["failure_analysis"] = _execution_failure_analysis(execution)
            if repair_ok or (bool(execution.get("ok")) and not _execution_needs_repair(execution)):
                break
        if _execution_needs_repair(execution) and not bool(execution.get("ok")):
            execution["failure_analysis"] = _execution_failure_analysis(execution)
            repair_stop = _build_repair_stop(execution, max_repair_passes=max_repair_passes)
            execution["repair_stop"] = repair_stop
            steps = execution.setdefault("steps", [])
            if isinstance(steps, list):
                steps.append(_execution_step(
                    "repair_stop",
                    "Backend repair budget",
                    False,
                    str(repair_stop.get("summary") or ""),
                    reason=repair_stop.get("reason"),
                    max_repair_passes=repair_stop.get("max_repair_passes"),
                    attempts=repair_stop.get("attempts"),
                    next_action=repair_stop.get("next_action"),
                    failure_analysis=repair_stop.get("failure_analysis") or {},
                ))
            emit("status", {"phase": "repair_stop", "message": str(repair_stop.get("summary") or "Backend repair budget exhausted.")})
            emit("tool_output", _harness_tool_output_payload(
                "repair-stop",
                "repair_stop",
                project_root=project_root,
                ok=False,
                summary=str(repair_stop.get("summary") or "Backend repair budget exhausted."),
                reason=repair_stop.get("reason"),
                attempts=repair_stop.get("attempts"),
                max_repair_passes=repair_stop.get("max_repair_passes"),
                next_action=repair_stop.get("next_action"),
                failure_analysis=repair_stop.get("failure_analysis") or {},
            ))

    if bool(execution.get("ok")):
        execution["failure_analysis"] = _execution_final_failure_analysis(execution)

    completion_report = _execution_completion_report(execution)
    execution["ok"] = bool(execution.get("ok")) and bool(completion_report.get("ok"))
    emit("status", {"phase": "completion", "message": str(completion_report.get("summary") or "Backend completion report ready.")})
    execution["completion_report"] = completion_report
    steps = execution.setdefault("steps", [])
    if isinstance(steps, list):
        steps.append(_execution_step(
            "completion",
            "Backend completion report",
            bool(completion_report.get("ok")),
            str(completion_report.get("summary") or ""),
            criteria=completion_report.get("criteria") or [],
            residual_risks=completion_report.get("residual_risks") or [],
            state=completion_report.get("state"),
        ))
    emit("tool_output", _harness_tool_output_payload(
        "completion",
        "completion",
        project_root=project_root,
        ok=bool(completion_report.get("ok")),
        summary=str(completion_report.get("summary") or ""),
        state=completion_report.get("state"),
        criteria=completion_report.get("criteria") or [],
        residual_risks=completion_report.get("residual_risks") or [],
    ))
    execution["run_ledger"] = _execution_run_ledger(execution)

    return execution


_HARD_VERIFIER_CHECKS = {
    "has-work-output",
    "valid-change-paths",
    "unique-change-paths",
    "non-empty-file-content",
    "valid-shell-actions",
    "relative-imports-resolve",
    "root-route-entrypoint",
    "frontend-style-runtime",
    "frontend-asset-quality",
    "referenced-asset-usage",
    "frontend-business-data-honesty",
    "frontend-interaction-integrity",
}


def _is_blocking_verifier_failure(check: dict) -> bool:
    if not isinstance(check, dict) or check.get("ok") is not False:
        return False
    severity = str(check.get("severity") or "").strip().lower()
    if severity:
        return severity == "hard"
    return str(check.get("name") or "") in _HARD_VERIFIER_CHECKS


def _trace_has_blocking_verifier_failures(trace: dict) -> bool:
    checks = trace.get("verification") if isinstance(trace, dict) else None
    if not isinstance(checks, list):
        return False
    for check in checks:
        if _is_blocking_verifier_failure(check):
            return True
    return False


def _trace_verifier_failure_summary(trace: dict) -> str:
    checks = trace.get("verification") if isinstance(trace, dict) else None
    if not isinstance(checks, list):
        return "Verifier reported blocking failures before backend execution."
    failures: list[str] = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        if not _is_blocking_verifier_failure(check):
            continue
        name = str(check.get("name") or "verifier").strip()
        detail = str(check.get("detail") or "").strip()
        failures.append(f"{name}: {detail}" if detail else name)
    return "\n".join(failures[:8]) or "Verifier reported blocking failures before backend execution."


def _build_backend_verifier_repair_prompt(
    req: AgentReq,
    trace: dict,
    *,
    base_changes: list[dict[str, object]] | None = None,
    base_actions: list[dict] | None = None,
) -> str:
    build_mode = str(req.build_mode or "hybrid")
    mode_directive = (
        "Appora Agent, stay in full ownership mode and produce a complete, valid implementation."
        if build_mode == "full-agent"
        else "Appora Agent, use the current Workspace context and produce a complete, valid implementation. Keep it surgical only when the task is surgical."
    )
    failure_summary = _trace_verifier_failure_summary(trace)
    intent_repair_directive = (
        "The previous pass produced concrete work while the runtime classified the request as read-only. "
        "Treat this as an implementation continuation and return command-compatible output."
        if "read-only-boundary" in failure_summary
        else ""
    )
    asset_repair_directive = (
        "The user explicitly referenced an uploaded @asset alias. Use the exact uploaded asset path/public URL from the attached asset context in the corrected implementation; do not replace it with placeholder media or an invented asset."
        if "referenced-asset-usage" in failure_summary
        else ""
    )
    business_data_repair_directive = (
        "Repair objective: remove fake business contact/data completely. Do not mention WhatsApp, WA, phone, email, street address, map details, opening hours, founding year, ratings, customer/order counts, delivery area, or operational claims unless the user provided real values. If contact/order flow is needed, render a disabled/configuration-gated control with neutral copy such as 'Kontak belum dikonfigurasi' and no href."
        if "frontend-business-data-honesty" in failure_summary
        else ""
    )
    interaction_repair_directive = (
        "Repair objective: remove fake/dead interactions. Buttons and links must navigate to an existing route/section, submit a real form, run a real local UI handler, or be visibly disabled/configuration-gated. Do not leave active-looking inert buttons."
        if "frontend-interaction-integrity" in failure_summary
        else ""
    )
    style_runtime_repair_directive = (
        "Repair objective: this project has no Tailwind setup. Remove Tailwind utility classes or add a real Tailwind setup; prefer existing CSS files/classes for a small Vite/React app."
        if "frontend-style-runtime" in failure_summary
        else ""
    )
    previous_changes = []
    for change in list(base_changes or [])[:8]:
        if not isinstance(change, dict):
            continue
        path = str(change.get("path") or "").strip()
        content = str(change.get("new_content") or "")
        if path:
            previous_changes.append(f"- {path} ({len(content)} chars)")
    previous_actions = []
    for action in list(base_actions or [])[:6]:
        if not isinstance(action, dict):
            continue
        action_type = str(action.get("type") or "").strip()
        command = str(action.get("command") or "").strip()
        previous_actions.append(f"- {action_type}: {command}" if command else f"- {action_type}")

    previous_output_context = "\n".join(
        [
            "Previous proposed output summary:",
            *(previous_changes or ["- no file changes"]),
            *(previous_actions or []),
        ]
    )

    return "\n\n".join([
        str(req.input or "").strip(),
        mode_directive,
        "Your previous output failed the backend verifier before it could be safely applied.",
        f"Verifier failures:\n{failure_summary}",
        previous_output_context,
        intent_repair_directive,
        asset_repair_directive,
        business_data_repair_directive,
        interaction_repair_directive,
        style_runtime_repair_directive,
        "Return corrected JSON for the same user task. This is a targeted verifier repair, not a new planning pass. Include only valid corrected file changes or valid shell actions. Do not return raw tool/MCP actions as the final output.",
    ]).strip()


def _first_verifier_failure_path(trace: dict, project_root: str) -> str:
    from . import agent_runtime as runtime

    checks = trace.get("verification") if isinstance(trace, dict) else None
    if not isinstance(checks, list):
        return ""
    for check in checks:
        detail = str((check or {}).get("detail") or "") if isinstance(check, dict) else ""
        match = re.search(r"(?P<path>[A-Za-z0-9_./-]+\.(?:tsx|ts|jsx|js|css|html|json|md))", detail)
        if not match:
            continue
        rel = runtime._localize_project_rel(match.group("path"), project_root)
        if rel:
            return rel
    return ""


def _scope_project_change_path(path: str, project_root: str) -> str:
    clean = str(path or "").strip().lstrip("/")
    root = str(project_root or ".").strip().strip("/") or "."
    if not clean or root == "." or clean == root or clean.startswith(root + "/"):
        return clean
    return f"{root}/{clean}"


def _backend_verifier_repair_context(
    req: AgentReq,
    ws_root: Path,
    trace: dict,
    base_changes: list[dict[str, object]],
) -> tuple[str, str, list[str], dict[str, str], str]:
    from . import agent_runtime as runtime

    project_root = str(req.project_root or ".").strip().strip("/") or "."
    project_dir = safe_join(ws_root, project_root)
    all_files: list[str] = []
    if project_dir.exists():
        try:
            all_files = [str(PurePosixPath(path.relative_to(project_dir))) for path in _iter_project_export_files(project_dir)]
        except Exception:
            all_files = []

    relevant_files: dict[str, str] = {}

    def add_disk_file(rel: str) -> None:
        local = runtime._localize_project_rel(rel, project_root)
        if not local or local in relevant_files:
            return
        try:
            path = safe_join(project_dir, local)
            if path.exists() and path.is_file():
                relevant_files[local] = path.read_text(encoding="utf-8")[:30_000]
        except Exception:
            return

    for change in list(base_changes or []):
        if not isinstance(change, dict):
            continue
        path = str(change.get("path") or "").strip()
        content = change.get("new_content")
        local = runtime._localize_project_rel(path, project_root)
        if local and isinstance(content, str):
            relevant_files[local] = content[:50_000]
            if local not in all_files:
                all_files.append(local)

    for rel in [
        _first_verifier_failure_path(trace, project_root),
        str(req.active_file or ""),
        *(list(req.open_files or [])[:6] if isinstance(req.open_files, list) else []),
        "package.json",
        "src/App.tsx",
        "src/App.jsx",
        "src/main.tsx",
        "src/app.css",
    ]:
        add_disk_file(rel)

    active_rel = (
        _first_verifier_failure_path(trace, project_root)
        or runtime._localize_project_rel(str(req.active_file or ""), project_root)
        or next(iter(relevant_files.keys()), "")
        or "(no-active-file)"
    )
    active_content = relevant_files.get(active_rel, "")
    extra_context = "\n\n".join(
        [
            "BACKEND VERIFIER TARGETED REPAIR CONTEXT:",
            "The previous output already ran through the Appora runtime pipeline. Do not restart planning; repair only the verifier failures.",
            f"Project root: {project_root}",
            f"Changed paths: {', '.join(sorted(relevant_files.keys())[:12]) or '(none)'}",
        ]
    )
    return active_rel, active_content, all_files, relevant_files, extra_context


def _run_backend_verifier_repair_pass(
    req: AgentReq,
    ws_root: Path,
    trace: dict,
    emit,
    *,
    base_changes: list[dict[str, object]] | None = None,
    base_actions: list[dict] | None = None,
) -> dict:
    emit("status", {"phase": "verifier_repair", "message": "Verifier gagal, agent memperbaiki output sebelum backend apply...", "targeted": True})
    from api.agent import suggest
    from api.agent_runtime import get_agent_mode_profile

    base_changes = list(base_changes or [])
    base_actions = list(base_actions or [])
    project_root = str(req.project_root or ".").strip().strip("/") or "."
    prompt = _build_backend_verifier_repair_prompt(req, trace, base_changes=base_changes, base_actions=base_actions)
    active_rel, active_content, all_files, relevant_files, extra_context = _backend_verifier_repair_context(req, ws_root, trace, base_changes)
    mode_profile = get_agent_mode_profile(str(req.build_mode or settings_mod.settings.build_mode or "hybrid"))
    json_recovery = False
    try:
        with _agent_lock_for_current_provider():
            repair_suggestion = suggest(
                instruction=prompt,
                path=active_rel,
                content=active_content,
                file_tree=all_files,
                relevant_files=relevant_files,
                extra_context=extra_context,
                workspace_root=safe_join(ws_root, project_root),
                system=mode_profile.system_prompt,
            )
        raw_repair_changes = [
            {**change, "path": _scope_project_change_path(str(change.get("path") or ""), project_root)}
            for change in list(repair_suggestion.changes or [])
            if isinstance(change, dict)
        ]
        repair_changes = _prepare_agent_out_changes(ws_root, raw_repair_changes)
        repair_actions = list(repair_suggestion.actions or [])
        repair_spoken = str(repair_suggestion.spoken or "")
        repair_log = str(repair_suggestion.log or "")
    except RuntimeError as exc:
        if "valid JSON" not in str(exc):
            raise
        from api import agent_runtime as runtime

        ctx = runtime.prepare_agent_context(req, ws_root)
        fallback_changes, fallback_actions = runtime._emergency_full_agent_changes(ctx, str(req.input or ""))
        if not (fallback_changes or fallback_actions):
            raise
        json_recovery = True
        repair_changes = _prepare_agent_out_changes(ws_root, list(fallback_changes))
        repair_actions = list(fallback_actions)
        repair_spoken = "Verifier repair model returned invalid JSON; backend used executable recovery changes instead."
        repair_log = f"targeted_verifier_repair=1 json_recovery=1 error={str(exc)[:120]}"
    repair_trace = _reverify_merged_verifier_output(req, ws_root, repair_changes, repair_actions)
    return {
        "spoken": repair_spoken,
        "log": f"targeted_verifier_repair=1 {repair_log}".strip(),
        "changes": repair_changes,
        "actions": repair_actions,
        "intent": {"kind": "command", "should_write_files": True, "should_run_tools": False},
        "trace": repair_trace,
        "targeted": True,
        "json_recovery": json_recovery,
        "ok": not _trace_has_blocking_verifier_failures(repair_trace),
        "failure_summary": _trace_verifier_failure_summary(repair_trace) if _trace_has_blocking_verifier_failures(repair_trace) else "",
    }


_TS6133_RE = re.compile(r"(?P<path>[^\s:(]+\.tsx?)\((?P<line>\d+),(?P<col>\d+)\):\s+error TS6133:\s+'(?P<name>[A-Za-z_$][\w$]*)'\s+is declared but its value is never read\.")
_TS2304_CANNOT_FIND_NAME_RE = re.compile(r"(?P<path>[^\s:(]+\.tsx?)\((?P<line>\d+),(?P<col>\d+)\):\s+error TS2304:\s+Cannot find name ['\"](?P<name>[A-Za-z_$][\w$]*)['\"]\.")
_VITE_UNRESOLVED_AT_ALIAS_RE = re.compile(r"Rollup failed to resolve import\s+['\"]@/", re.IGNORECASE)
_TS2322_LOCATION_RE = re.compile(r"(?P<path>[^\s:(]+\.tsx?)\((?P<line>\d+),(?P<col>\d+)\):\s+error TS2322:")
_TS_PROP_NOT_EXIST_RE = re.compile(r"Property ['\"](?P<prop>[A-Za-z_$][\w$]*)['\"] does not exist on type")
_TS2741_MISSING_REQUIRED_PROP_RE = re.compile(r"(?P<path>[^\s:(]+\.tsx?)\((?P<line>\d+),(?P<col>\d+)\):\s+error TS2741:\s+Property ['\"](?P<prop>[A-Za-z_$][\w$]*)['\"] is missing in type")
_TS2307_MODULE_RE = re.compile(r"error TS2307:\s+Cannot find module ['\"](?P<module>[^'\"]+)['\"]")
_VITE_IMPORT_RESOLVE_RE = re.compile(r"Failed to resolve import ['\"](?P<module>[^'\"]+)['\"]")
_VITE_HTML_ENTRY_RESOLVE_RE = re.compile(r"Failed to resolve\s+(?P<entry>/src/[\w./-]+\.(?:tsx|jsx|ts|js))\s+from\s+.*index\.html")
_QUICK_INSTALL_PACKAGE_ALLOWLIST = {
    "@supabase/supabase-js",
    "axios",
    "chart.js",
    "class-variance-authority",
    "clsx",
    "date-fns",
    "embla-carousel-react",
    "framer-motion",
    "gsap",
    "lucide-react",
    "react-hook-form",
    "react-markdown",
    "react-router-dom",
    "recharts",
    "sonner",
    "tailwind-merge",
    "three",
    "zod",
    "zustand",
}
_QUICK_INSTALL_PACKAGE_PREFIXES = (
    "@radix-ui/react-",
    "@react-three/",
    "@tanstack/",
)


def _execution_output_text(execution: dict[str, object]) -> str:
    chunks: list[str] = []
    for container_name in ("shell", "validation", "replay"):
        container = execution.get(container_name)
        if not isinstance(container, dict):
            continue
        for result in list(container.get("results") or []):
            if not isinstance(result, dict):
                continue
            chunks.append(str(result.get("stdout") or ""))
            chunks.append(str(result.get("stderr") or ""))
    return "\n".join(chunks)


def _vite_html_entrypoint_issue_from_execution(execution: dict[str, object]) -> str | None:
    text = _execution_output_text(execution)
    for match in _VITE_HTML_ENTRY_RESOLVE_RE.finditer(text):
        entry = str(match.group("entry") or "").strip()
        if entry:
            return entry.lstrip("/")
    return None


def _preview_missing_h1_issue(execution: dict[str, object]) -> bool:
    preview = execution.get("preview_audit")
    if not isinstance(preview, dict) or preview.get("skipped") or preview.get("ok") is not False:
        return False
    for issue in list(preview.get("issue_details") or []):
        if not isinstance(issue, dict) or issue.get("severity") != "blocking":
            continue
        text = f"{issue.get('category') or ''} {issue.get('detail') or ''}".lower()
        if "h1" in text or "heading" in text:
            return True
    return False


def _existing_vite_entrypoint(project_dir: Path, missing_entry: str) -> str | None:
    missing_path = PurePosixPath(missing_entry)
    stem = missing_path.stem or "main"
    candidates = [
        f"src/{stem}.jsx",
        f"src/{stem}.tsx",
        f"src/{stem}.js",
        f"src/{stem}.ts",
        "src/main.jsx",
        "src/main.tsx",
        "src/main.js",
        "src/main.ts",
    ]
    for rel in candidates:
        if rel == missing_entry:
            continue
        if (project_dir / rel).exists():
            return rel
    return None


def _ts6133_issues_from_execution(execution: dict[str, object]) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    seen: set[tuple[str, int, str]] = set()
    for container_name in ("shell", "validation", "replay"):
        container = execution.get(container_name)
        if not isinstance(container, dict):
            continue
        for result in list(container.get("results") or []):
            if not isinstance(result, dict):
                continue
            text = f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}"
            for match in _TS6133_RE.finditer(text):
                path = str(match.group("path") or "").strip()
                line = int(match.group("line") or 0)
                name = str(match.group("name") or "").strip()
                key = (path, line, name)
                if not path or not name or key in seen:
                    continue
                seen.add(key)
                issues.append({"path": path, "line": line, "name": name})
    return issues[:16]


def _ts2304_react_hook_issues_from_execution(execution: dict[str, object]) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    allowed = {"useCallback", "useEffect", "useMemo", "useRef", "useState"}
    for container_name in ("shell", "validation", "replay"):
        container = execution.get(container_name)
        if not isinstance(container, dict):
            continue
        for result in list(container.get("results") or []):
            if not isinstance(result, dict):
                continue
            text = f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}"
            for match in _TS2304_CANNOT_FIND_NAME_RE.finditer(text):
                path = str(match.group("path") or "").strip()
                name = str(match.group("name") or "").strip()
                key = (path, name)
                if not path or name not in allowed or key in seen:
                    continue
                seen.add(key)
                issues.append({"path": path, "name": name})
    return issues[:16]


def _ts2322_unsupported_prop_issues_from_execution(execution: dict[str, object]) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    seen: set[tuple[str, int, str]] = set()
    for container_name in ("shell", "validation", "replay"):
        container = execution.get(container_name)
        if not isinstance(container, dict):
            continue
        for result in list(container.get("results") or []):
            if not isinstance(result, dict):
                continue
            lines = str(f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}").splitlines()
            current: tuple[str, int] | None = None
            for line in lines:
                location = _TS2322_LOCATION_RE.search(line)
                if location:
                    current = (str(location.group("path") or "").strip(), int(location.group("line") or 0))
                    continue
                prop_match = _TS_PROP_NOT_EXIST_RE.search(line)
                if prop_match and current:
                    prop = str(prop_match.group("prop") or "").strip()
                    path, line_no = current
                    key = (path, line_no, prop)
                    if path and line_no and prop and key not in seen:
                        seen.add(key)
                        issues.append({"path": path, "line": line_no, "prop": prop})
                    current = None
    return issues[:16]


def _ts2741_missing_required_prop_issues_from_execution(execution: dict[str, object]) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    seen: set[tuple[str, int, str]] = set()
    for container_name in ("shell", "validation", "replay"):
        container = execution.get(container_name)
        if not isinstance(container, dict):
            continue
        for result in list(container.get("results") or []):
            if not isinstance(result, dict):
                continue
            text = f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}"
            for match in _TS2741_MISSING_REQUIRED_PROP_RE.finditer(text):
                path = str(match.group("path") or "").strip()
                line = int(match.group("line") or 0)
                prop = str(match.group("prop") or "").strip()
                key = (path, line, prop)
                if not path or not line or not prop or key in seen:
                    continue
                seen.add(key)
                issues.append({"path": path, "line": line, "prop": prop})
    return issues[:16]


def _external_package_from_module_spec(module_spec: str) -> str:
    module_spec = str(module_spec or "").strip()
    if not module_spec or module_spec.startswith((".", "/", "#")):
        return ""
    if re.match(r"^[a-zA-Z][a-zA-Z\d+.-]*:", module_spec):
        return ""
    parts = [part for part in module_spec.split("/") if part]
    if not parts:
        return ""
    if parts[0].startswith("@"):
        if len(parts) < 2:
            return ""
        return f"{parts[0]}/{parts[1]}"
    return parts[0]


def _quick_install_allowed_package(package_name: str) -> bool:
    package_name = str(package_name or "").strip()
    if package_name in _QUICK_INSTALL_PACKAGE_ALLOWLIST:
        return True
    return any(package_name.startswith(prefix) for prefix in _QUICK_INSTALL_PACKAGE_PREFIXES)


def _declared_package_names(project_dir: Path) -> set[str]:
    package_json = project_dir / "package.json"
    try:
        parsed = json.loads(package_json.read_text(encoding="utf-8"))
    except Exception:
        return set()
    if not isinstance(parsed, dict):
        return set()
    names: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        deps = parsed.get(key)
        if isinstance(deps, dict):
            names.update(str(name) for name in deps.keys())
    return names


def _missing_external_packages_from_execution(execution: dict[str, object], project_dir: Path) -> list[str]:
    declared = _declared_package_names(project_dir)
    packages: list[str] = []
    seen: set[str] = set()
    for container_name in ("shell", "validation", "replay"):
        container = execution.get(container_name)
        if not isinstance(container, dict):
            continue
        for result in list(container.get("results") or []):
            if not isinstance(result, dict):
                continue
            text = f"{result.get('stdout') or ''}\n{result.get('stderr') or ''}"
            module_specs = [match.group("module") for match in _TS2307_MODULE_RE.finditer(text)]
            module_specs.extend(match.group("module") for match in _VITE_IMPORT_RESOLVE_RE.finditer(text))
            for module_spec in module_specs:
                package_name = _external_package_from_module_spec(str(module_spec))
                if not package_name or package_name in declared or package_name in seen:
                    continue
                if not _quick_install_allowed_package(package_name):
                    continue
                seen.add(package_name)
                packages.append(package_name)
    return packages[:8]


def _find_import_statement_range(lines: list[str], line_index: int) -> tuple[int, int] | None:
    start = max(0, min(line_index, len(lines) - 1))
    while start >= 0:
        stripped = lines[start].lstrip()
        if stripped.startswith("import "):
            break
        if not stripped or stripped.endswith(";"):
            return None
        start -= 1
    if start < 0:
        return None
    end = start
    while end < len(lines) - 1 and ";" not in lines[end]:
        end += 1
    return start, end


def _remove_unused_import_symbol(lines: list[str], line_index: int, name: str) -> bool:
    span = _find_import_statement_range(lines, line_index)
    if not span:
        return False
    start, end = span
    statement = "\n".join(lines[start:end + 1])
    if not re.search(rf"\b{re.escape(name)}\b", statement):
        return False

    compact = " ".join(part.strip() for part in lines[start:end + 1]).strip()
    from_match = re.search(r"\sfrom\s+['\"][^'\"]+['\"]\s*;?\s*$", compact)
    if not from_match:
        del lines[start:end + 1]
        return True
    from_part = compact[from_match.start():].strip()
    clause = compact[len("import "):from_match.start()].strip()
    changed = False

    if clause == name:
        del lines[start:end + 1]
        return True

    if clause.startswith(name + ","):
        clause = clause[len(name) + 1:].strip()
        changed = True

    brace_match = re.search(r"\{(?P<body>.*)\}", clause)
    if brace_match:
        body = brace_match.group("body")
        specs = [item.strip() for item in body.split(",") if item.strip()]
        kept = [
            item
            for item in specs
            if re.split(r"\s+as\s+", item, flags=re.IGNORECASE)[0].strip() != name
            and re.split(r"\s+as\s+", item, flags=re.IGNORECASE)[-1].strip() != name
        ]
        if len(kept) != len(specs):
            changed = True
            if kept:
                clause = clause[:brace_match.start()] + "{ " + ", ".join(kept) + " }" + clause[brace_match.end():]
            else:
                clause = (clause[:brace_match.start()] + clause[brace_match.end():]).strip().strip(",").strip()

    if not changed:
        return False
    if not clause:
        del lines[start:end + 1]
    else:
        lines[start:end + 1] = [f"import {clause} {from_part}"]
    return True


def _is_type_only_context(lines: list[str], line_index: int) -> bool:
    for probe in range(line_index, max(-1, line_index - 12), -1):
        stripped = lines[probe].strip()
        if re.match(r"^(type|interface)\s+", stripped):
            return True
        if stripped.startswith("function ") or stripped.startswith("export default function ") or "=> {" in stripped:
            return False
        if stripped == "};" or stripped == "}":
            return False
    return False


def _opens_function_body(line: str, *, declaration_is_const: bool) -> bool:
    stripped = line.rstrip()
    if not stripped.endswith("{"):
        return False
    if re.match(r"^\s*(type|interface)\s+", stripped):
        return False
    if not declaration_is_const:
        return True
    arrow_index = stripped.rfind("=>")
    open_index = stripped.rfind("{")
    if arrow_index >= 0:
        return open_index > arrow_index
    return bool(re.search(r"=\s*function\b", stripped))


def _function_body_insertion_index(lines: list[str], line_index: int) -> int | None:
    start = max(0, min(line_index, len(lines) - 1))
    for probe in range(start, max(-1, start - 24), -1):
        stripped = lines[probe].strip()
        is_function_declaration = bool(re.match(r"^(export\s+default\s+)?function\s+", stripped))
        is_const_declaration = bool(re.match(r"^(export\s+)?const\s+[A-Za-z_$][\w$]*\s*=", stripped))
        if is_function_declaration or is_const_declaration:
            for body_probe in range(probe, min(len(lines), probe + 32)):
                if _opens_function_body(lines[body_probe], declaration_is_const=is_const_declaration):
                    return body_probe + 1
            return None
    return None


def _declaration_end_index(lines: list[str], line_index: int) -> int | None:
    depth = 0
    saw_opener = False
    for probe in range(line_index, min(len(lines), line_index + 120)):
        line = lines[probe]
        depth += line.count("(") + line.count("{") + line.count("[")
        if line.count("(") or line.count("{") or line.count("["):
            saw_opener = True
        depth -= line.count(")") + line.count("}") + line.count("]")
        stripped = line.strip()
        if depth <= 0 and (stripped.endswith(";") or (saw_opener and stripped == "}")):
            return probe
    return None


def _unused_declaration_reference(lines: list[str], line_index: int, name: str) -> tuple[int, str] | None:
    if line_index < 0 or line_index >= len(lines):
        return None
    line = lines[line_index]
    value_match = re.match(rf"^(?P<indent>\s*)(?:const|let|var)\s+{re.escape(name)}\b", line)
    function_match = re.match(rf"^(?P<indent>\s*)(?:export\s+)?function\s+{re.escape(name)}\b", line)
    match = value_match or function_match
    if match is None:
        return None
    end_index = _declaration_end_index(lines, line_index)
    if end_index is None:
        return None
    return end_index + 1, f"{match.group('indent')}void {name};"


def _repair_unused_usestate_destructures(lines: list[str], issues: list[dict[str, object]]) -> set[tuple[int, str]]:
    issues_by_line: dict[int, set[str]] = {}
    for issue in issues:
        name = str(issue.get("name") or "").strip()
        line_no = int(issue.get("line") or 0)
        if line_no > 0 and re.match(r"^[A-Za-z_$][\w$]*$", name):
            issues_by_line.setdefault(line_no - 1, set()).add(name)

    handled: set[tuple[int, str]] = set()
    for idx in sorted(issues_by_line.keys(), reverse=True):
        if idx < 0 or idx >= len(lines):
            continue
        line = lines[idx]
        if "useState" not in line:
            continue
        match = re.search(
            r"(?P<prefix>\[\s*)(?P<value>[A-Za-z_$][\w$]*)(?P<middle>\s*,\s*(?P<setter>[A-Za-z_$][\w$]*)\s*)?(?P<suffix>\])",
            line,
        )
        if not match:
            continue
        value = str(match.group("value") or "")
        setter = str(match.group("setter") or "")
        names = issues_by_line[idx]
        value_unused = value in names
        setter_unused = bool(setter and setter in names)

        if value_unused and (not setter or setter_unused):
            del lines[idx]
            handled.add((idx, value))
            if setter:
                handled.add((idx, setter))
            continue
        if setter_unused:
            next_line = line[:match.start()] + f"[{value}]" + line[match.end():]
            if next_line != line:
                lines[idx] = next_line
                handled.add((idx, setter))
            continue
        if value_unused and setter:
            next_line = line[:match.start()] + f"[, {setter}]" + line[match.end():]
            if next_line != line:
                lines[idx] = next_line
                handled.add((idx, value))
    return handled


def _insert_void_usage_for_unused_symbols(project_dir: Path, issues: list[dict[str, object]]) -> list[str]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for issue in issues:
        rel = str(issue.get("path") or "").strip().lstrip("/")
        if not rel or ".." in rel.split("/"):
            continue
        grouped.setdefault(rel, []).append(issue)

    changed: list[str] = []
    for rel, file_issues in grouped.items():
        path = (project_dir / rel).resolve()
        try:
            if project_dir != path and project_dir not in path.parents:
                continue
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        lines = text.splitlines()
        usestate_handled = _repair_unused_usestate_destructures(lines, file_issues)
        insertions: dict[int, list[str]] = {}
        touched = False
        for issue in sorted(file_issues, key=lambda item: int(item.get("line") or 0), reverse=True):
            name = str(issue.get("name") or "").strip()
            if not re.match(r"^[A-Za-z_$][\w$]*$", name):
                continue
            line_no = max(1, int(issue.get("line") or 1))
            idx = min(max(line_no - 1, 0), max(len(lines) - 1, 0))
            if (line_no - 1, name) in usestate_handled:
                touched = True
                continue
            if re.search(rf"\bvoid\s+{re.escape(name)}\s*;", text):
                continue
            if _remove_unused_import_symbol(lines, idx, name):
                touched = True
                continue
            if _remove_unused_usestate_setter(lines, idx, name):
                touched = True
                continue
            if _is_type_only_context(lines, idx):
                continue
            declaration_reference = _unused_declaration_reference(lines, idx, name)
            if declaration_reference is not None:
                insertion_index, insertion = declaration_reference
                insertions.setdefault(insertion_index, []).append(insertion)
                continue
            insertion_index = _function_body_insertion_index(lines, idx)
            if insertion_index is None:
                continue
            indent_match = re.match(r"^(\s*)", lines[insertion_index] if insertion_index < len(lines) else "")
            indent = (indent_match.group(1) if indent_match else "") + "  "
            insertions.setdefault(insertion_index, []).append(f"{indent}void {name};")
        if not insertions and not touched:
            continue
        next_lines: list[str] = []
        for index, line in enumerate(lines):
            for insertion in insertions.get(index, []):
                next_lines.append(insertion)
            next_lines.append(line)
        for insertion in insertions.get(len(lines), []):
            next_lines.append(insertion)
        path.write_text("\n".join(next_lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
        changed.append(rel)
    return changed


def _merge_react_named_import(text: str, names: set[str]) -> tuple[str, bool]:
    missing = {name for name in names if re.match(r"^use[A-Z][A-Za-z0-9_]*$", name)}
    if not missing:
        return text, False
    named_import = re.search(r"import\s+\{(?P<body>[^}]*)\}\s+from\s+['\"]react['\"]\s*;?", text)
    if named_import:
        existing = {
            item.strip().split(" as ", 1)[0].strip()
            for item in named_import.group("body").split(",")
            if item.strip()
        }
        to_add = sorted(missing - existing)
        if not to_add:
            return text, False
        merged = ", ".join(sorted(existing | missing))
        next_text = text[:named_import.start("body")] + f" {merged} " + text[named_import.end("body"):]
        return next_text, True

    default_import = re.search(r"import\s+(?P<default>[A-Za-z_$][\w$]*)\s+from\s+['\"]react['\"]\s*;?", text)
    import_line = f"import {{ {', '.join(sorted(missing))} }} from 'react';\n"
    if default_import:
        insert_at = default_import.end()
        if insert_at < len(text) and text[insert_at:insert_at + 1] == "\n":
            insert_at += 1
        return text[:insert_at] + import_line + text[insert_at:], True
    return import_line + text, True


def _add_missing_react_hook_imports(project_dir: Path, issues: list[dict[str, object]]) -> list[str]:
    grouped: dict[str, set[str]] = {}
    for issue in issues:
        rel = str(issue.get("path") or "").strip().lstrip("/")
        name = str(issue.get("name") or "").strip()
        if not rel or ".." in rel.split("/") or not name:
            continue
        grouped.setdefault(rel, set()).add(name)

    changed: list[str] = []
    for rel, names in grouped.items():
        path = (project_dir / rel).resolve()
        try:
            if project_dir != path and project_dir not in path.parents:
                continue
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        next_text, touched = _merge_react_named_import(text, names)
        if not touched or next_text == text:
            continue
        path.write_text(next_text, encoding="utf-8")
        changed.append(rel)
    return changed


def _remove_unused_usestate_setter(lines: list[str], idx: int, name: str) -> bool:
    if idx < 0 or idx >= len(lines):
        return False
    line = lines[idx]
    if "useState" not in line or name not in line:
        return False
    next_line = re.sub(
        rf"\[\s*(?P<value>[A-Za-z_$][\w$]*)\s*,\s*{re.escape(name)}\s*\]",
        r"[\g<value>]",
        line,
        count=1,
    )
    if next_line == line:
        return False
    lines[idx] = next_line
    return True


def _component_tag_at_line(lines: list[str], line_no: int) -> str:
    start = max(0, line_no - 2)
    end = min(len(lines), line_no + 4)
    fragment = "\n".join(lines[start:end])
    match = re.search(r"<(?P<tag>[A-Z][A-Za-z0-9_]*)\b", fragment)
    return str(match.group("tag") or "") if match else ""


def _resolve_relative_module(project_dir: Path, importer: Path, spec: str) -> Path | None:
    if not spec.startswith("."):
        return None
    base = (importer.parent / spec).resolve()
    candidates = [base]
    if base.suffix:
        candidates.append(base)
    else:
        for suffix in (".tsx", ".ts", ".jsx", ".js"):
            candidates.append(base.with_suffix(suffix))
        for suffix in (".tsx", ".ts", ".jsx", ".js"):
            candidates.append(base / f"index{suffix}")
    for candidate in candidates:
        try:
            if candidate.exists() and candidate.is_file() and (project_dir == candidate or project_dir in candidate.parents):
                return candidate
        except Exception:
            continue
    return None


def _component_import_target(project_dir: Path, importer: Path, component: str, text: str) -> Path | None:
    default_pattern = re.compile(rf"import\s+{re.escape(component)}\s+from\s+['\"](?P<spec>[^'\"]+)['\"]")
    named_pattern = re.compile(rf"import\s+\{{[^}}]*\b{re.escape(component)}\b[^}}]*\}}\s+from\s+['\"](?P<spec>[^'\"]+)['\"]")
    for pattern in (default_pattern, named_pattern):
        match = pattern.search(text)
        if not match:
            continue
        target = _resolve_relative_module(project_dir, importer, str(match.group("spec") or ""))
        if target is not None:
            return target
    return None


def _make_prop_optional_in_component(component_path: Path, prop: str) -> bool:
    try:
        text = component_path.read_text(encoding="utf-8")
    except Exception:
        return False
    if not re.match(r"^[A-Za-z_$][\w$]*$", prop):
        return False
    prop_pattern = re.compile(rf"(?P<prefix>\b{re.escape(prop)})\s*:\s*(?P<type>[^;,\n}}]+)")
    next_text, count = prop_pattern.subn(r"\g<prefix>?: \g<type>", text, count=1)
    if count == 0 or next_text == text:
        return False
    next_text = re.sub(
        rf"(<h[1-6][^>]*>\s*\{{{re.escape(prop)}\}}\s*</h[1-6]>)",
        rf"{{{prop} ? \1 : null}}",
        next_text,
        count=2,
    )
    component_path.write_text(next_text, encoding="utf-8")
    return True


_UI_PRESENTATION_PROPS = {"title", "eyebrow", "label", "description"}


def _ensure_optional_prop_declared(text: str, prop: str) -> tuple[str, bool]:
    if re.search(rf"\b{re.escape(prop)}\??\s*:", text):
        return text, False
    prop_line = f"  {prop}?: string;\n"
    next_text, count = re.subn(r"(interface\s+[A-Za-z_$][\w$]*\s*\{\n)", r"\1" + prop_line, text, count=1)
    if count:
        return next_text, True
    next_text, count = re.subn(r"(type\s+[A-Za-z_$][\w$]*\s*=\s*(?:[^;{}]|\{[^{}]*\})*?&\s*\{\n)", r"\1" + prop_line, text, count=1, flags=re.DOTALL)
    if count:
        return next_text, True
    return text, False


def _ensure_destructured_component_prop(text: str, prop: str) -> tuple[str, bool]:
    if re.search(rf"function\s+[A-Za-z_$][\w$]*\s*\(\s*\{{[^}}]*\b{re.escape(prop)}\b", text):
        return text, False
    pattern = re.compile(r"(function\s+[A-Za-z_$][\w$]*\s*\(\s*\{)(?P<body>[^}]*)\}(\s*:\s*[A-Za-z_$][\w$]*\s*\))")

    def repl(match: re.Match[str]) -> str:
        body = match.group("body").strip()
        next_body = f"{prop}, {body}" if body else prop
        return f"{match.group(1)}{next_body}}}{match.group(3)}"

    next_text, count = pattern.subn(repl, text, count=1)
    return next_text, bool(count)


def _ensure_card_like_prop_rendering(text: str, prop: str) -> tuple[str, bool]:
    if prop not in {"title", "eyebrow", "description"} or re.search(rf"\{{\s*{re.escape(prop)}\s*\?\s*<", text):
        return text, False
    render_line = {
        "eyebrow": '      {eyebrow ? <div className="eyebrow">{eyebrow}</div> : null}\n',
        "title": '      {title ? <h2 className="cardTitle">{title}</h2> : null}\n',
        "description": '      {description ? <p className="muted">{description}</p> : null}\n',
    }[prop]
    next_text, count = re.subn(r"(\s*<div\s+className=\{`card\s+\$\{className\}`\.trim\(\)\}>\n)", r"\1" + render_line, text, count=1)
    if count:
        return next_text, True
    next_text, count = re.subn(r"(\s*<section[^>]*className=\{classes\}[^>]*>\n)", r"\1" + render_line, text, count=1)
    if count:
        return next_text, True
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if "<div" in line and "className=" in line and "card" in line:
            lines.insert(index + 1, render_line.rstrip("\n"))
            return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), True
    return text, False


def _add_optional_ui_prop_to_component(component_path: Path, prop: str) -> bool:
    if prop not in _UI_PRESENTATION_PROPS:
        return False
    try:
        text = component_path.read_text(encoding="utf-8")
    except Exception:
        return False
    next_text, declared = _ensure_optional_prop_declared(text, prop)
    if not declared:
        # The prop may already exist as optional/required. In that case the
        # unsupported-prop error is probably from another component shape.
        return False
    if prop in {"title", "eyebrow", "description"}:
        next_text, destructured = _ensure_destructured_component_prop(next_text, prop)
        if destructured:
            next_text, _ = _ensure_card_like_prop_rendering(next_text, prop)
    component_path.write_text(next_text, encoding="utf-8")
    return True


def _make_missing_required_props_optional(project_dir: Path, issues: list[dict[str, object]]) -> list[str]:
    changed: list[str] = []
    seen_targets: set[tuple[Path, str]] = set()
    for issue in issues:
        rel = str(issue.get("path") or "").strip().lstrip("/")
        prop = str(issue.get("prop") or "").strip()
        if not rel or ".." in rel.split("/") or prop not in {"title", "label", "description", "eyebrow"}:
            continue
        importer = (project_dir / rel).resolve()
        try:
            if project_dir != importer and project_dir not in importer.parents:
                continue
            text = importer.read_text(encoding="utf-8")
        except Exception:
            continue
        component = _component_tag_at_line(text.splitlines(), int(issue.get("line") or 1))
        if not component:
            continue
        target = _component_import_target(project_dir, importer, component, text)
        if target is None:
            continue
        key = (target, prop)
        if key in seen_targets:
            continue
        seen_targets.add(key)
        if _make_prop_optional_in_component(target, prop):
            changed.append(target.relative_to(project_dir).as_posix())
    return changed


def _add_unsupported_ui_props_to_components(project_dir: Path, issues: list[dict[str, object]]) -> list[str]:
    changed: list[str] = []
    seen_targets: set[tuple[Path, str]] = set()
    for issue in issues:
        rel = str(issue.get("path") or "").strip().lstrip("/")
        prop = str(issue.get("prop") or "").strip()
        if not rel or ".." in rel.split("/") or prop not in _UI_PRESENTATION_PROPS:
            continue
        importer = (project_dir / rel).resolve()
        try:
            if project_dir != importer and project_dir not in importer.parents:
                continue
            text = importer.read_text(encoding="utf-8")
        except Exception:
            continue
        component = _component_tag_at_line(text.splitlines(), int(issue.get("line") or 1))
        if not component:
            continue
        target = _component_import_target(project_dir, importer, component, text)
        if target is None:
            continue
        key = (target, prop)
        if key in seen_targets:
            continue
        seen_targets.add(key)
        if _make_prop_optional_in_component(target, prop) or _add_optional_ui_prop_to_component(target, prop):
            changed.append(target.relative_to(project_dir).as_posix())
    return list(dict.fromkeys(changed))


def _remove_unsupported_jsx_props(project_dir: Path, issues: list[dict[str, object]]) -> list[str]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for issue in issues:
        rel = str(issue.get("path") or "").strip().lstrip("/")
        if not rel or ".." in rel.split("/"):
            continue
        grouped.setdefault(rel, []).append(issue)

    changed: list[str] = []
    for rel, file_issues in grouped.items():
        path = (project_dir / rel).resolve()
        try:
            if project_dir != path and project_dir not in path.parents:
                continue
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        lines = text.splitlines()
        touched = False
        for issue in file_issues:
            prop = str(issue.get("prop") or "").strip()
            if not re.match(r"^[A-Za-z_$][\w$]*$", prop):
                continue
            line_no = max(1, int(issue.get("line") or 1))
            start = max(0, line_no - 2)
            end = min(len(lines), line_no + 8)
            prop_pattern = re.compile(
                rf"\s+{re.escape(prop)}=(?:\{{(?:[^{{}}]|\{{[^{{}}]*\}})*\}}|\"[^\"]*\"|'[^']*')"
            )
            for index in range(start, end):
                next_line, replacements = prop_pattern.subn("", lines[index])
                if replacements:
                    lines[index] = next_line
                    touched = True
                    break
        if not touched:
            continue
        path.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
        changed.append(rel)
    return changed


def _rollback_quick_repair_if_failed(project_dir: Path, shell: dict[str, object], snapshots: dict[str, str | None]) -> list[str]:
    if bool(shell.get("ok")):
        return []
    return _restore_project_file_snapshots(project_dir, snapshots)


def _try_quick_ts6133_repair(req: AgentReq, execution: dict[str, object], emit) -> dict[str, object] | None:
    issues = _ts6133_issues_from_execution(execution)
    if not issues:
        return None
    project_root = str(req.project_root or ".").strip().strip("/") or "."
    try:
        project_dir = safe_join(_ws(), project_root)
    except Exception:
        return None
    snapshots = _snapshot_project_files(project_dir, list(dict.fromkeys(
        str(issue.get("path") or "").strip().lstrip("/")
        for issue in issues
        if str(issue.get("path") or "").strip()
    )))
    changed_paths = _insert_void_usage_for_unused_symbols(project_dir, issues)
    if not changed_paths:
        return None

    commands = list(dict.fromkeys(
        str(command)
        for command in list((execution.get("validation") or {}).get("commands") or [])
        if str(command).strip()
    ))
    if not commands:
        commands = _infer_validation_commands(project_dir)[:4]
    if not commands:
        return {"ok": False, "changed_paths": changed_paths, "summary": "Quick TS6133 repair edited files but found no validation command."}

    emit("status", {"phase": "quick_repair", "message": "Backend quick repair fixed unused TypeScript symbols before LLM repair..."})
    _emit_command_start_events(emit, tool="quick-repair", phase="quick_repair", project_root=project_root, commands=commands, group="quick repair")
    shell = _run_harness_shell_actions_internal(
        ws_root_path=_ws(),
        project_root=project_root,
        actions=[AgentHarnessShellAction(command=command, cwd=project_root, reason="Quick TS6133 repair validation") for command in commands],
        emit=emit,
        tool="quick-repair",
        phase="quick_repair",
        group="quick repair",
    )
    rolled_back = _rollback_quick_repair_if_failed(project_dir, shell, snapshots)
    result = {
        "ok": bool(shell.get("ok")),
        "changed_paths": changed_paths,
        "rolled_back_paths": rolled_back,
        "issues": issues,
        "commands": commands,
        "shell": shell,
        "summary": f"Quick TS6133 repair changed {len(changed_paths)} file(s), validation ok={bool(shell.get('ok'))}, rolled_back={len(rolled_back)}.",
    }
    emit("tool_output", _harness_tool_output_payload(
        "quick-repair",
        "quick_repair",
        project_root=project_root,
        ok=bool(shell.get("ok")),
        summary=str(result["summary"]),
        paths=changed_paths,
        commands=commands,
        results=_shell_event_results(shell.get("results")),
    ))
    return result


def _try_quick_ts2304_react_hook_repair(req: AgentReq, execution: dict[str, object], emit) -> dict[str, object] | None:
    issues = _ts2304_react_hook_issues_from_execution(execution)
    if not issues:
        return None
    project_root = str(req.project_root or ".").strip().strip("/") or "."
    try:
        project_dir = safe_join(_ws(), project_root)
    except Exception:
        return None
    snapshots = _snapshot_project_files(project_dir, list(dict.fromkeys(
        str(issue.get("path") or "").strip().lstrip("/")
        for issue in issues
        if str(issue.get("path") or "").strip()
    )))
    changed_paths = _add_missing_react_hook_imports(project_dir, issues)
    if not changed_paths:
        return None

    commands = list(dict.fromkeys(
        str(command)
        for command in list((execution.get("validation") or {}).get("commands") or [])
        if str(command).strip()
    ))
    if not commands:
        commands = _infer_validation_commands(project_dir)[:4]
    if not commands:
        return {"ok": False, "changed_paths": changed_paths, "summary": "Quick TS2304 hook repair edited files but found no validation command."}

    emit("status", {"phase": "quick_repair", "message": "Backend quick repair added missing React hook imports before LLM repair..."})
    _emit_command_start_events(emit, tool="quick-repair", phase="quick_repair", project_root=project_root, commands=commands, group="quick repair")
    shell = _run_harness_shell_actions_internal(
        ws_root_path=_ws(),
        project_root=project_root,
        actions=[AgentHarnessShellAction(command=command, cwd=project_root, reason="Quick TS2304 React hook import repair validation") for command in commands],
        emit=emit,
        tool="quick-repair",
        phase="quick_repair",
        group="quick repair",
    )
    rolled_back = _rollback_quick_repair_if_failed(project_dir, shell, snapshots)
    result = {
        "ok": bool(shell.get("ok")),
        "kind": "ts2304-react-hook-import",
        "changed_paths": changed_paths,
        "rolled_back_paths": rolled_back,
        "issues": issues,
        "commands": commands,
        "shell": shell,
        "summary": f"Quick TS2304 hook repair changed {len(changed_paths)} file(s), validation ok={bool(shell.get('ok'))}, rolled_back={len(rolled_back)}.",
    }
    emit("tool_output", _harness_tool_output_payload(
        "quick-repair",
        "quick_repair",
        project_root=project_root,
        ok=bool(shell.get("ok")),
        summary=str(result["summary"]),
        paths=changed_paths,
        commands=commands,
        results=_shell_event_results(shell.get("results")),
    ))
    return result


def _vite_unresolved_at_alias_from_execution(execution: dict[str, object]) -> bool:
    for section in ("shell", "validation", "replay"):
        block = execution.get(section)
        if not isinstance(block, dict):
            continue
        for result in list(block.get("results") or []):
            if not isinstance(result, dict) or result.get("ok") is not False:
                continue
            text = "\n".join(str(result.get(key) or "") for key in ("stdout", "stderr", "output", "message"))
            if _VITE_UNRESOLVED_AT_ALIAS_RE.search(text):
                return True
    return False


def _add_vite_src_alias(project_dir: Path) -> list[str]:
    config_path = next(
        (project_dir / name for name in ("vite.config.ts", "vite.config.mts", "vite.config.js", "vite.config.mjs") if (project_dir / name).exists()),
        None,
    )
    if config_path is None:
        return []
    try:
        source = config_path.read_text(encoding="utf-8")
    except Exception:
        return []
    if re.search(r"alias\s*:\s*\{[^}]*['\"]@['\"]", source, re.DOTALL):
        return []
    next_source = source
    if "node:url" not in next_source:
        next_source = 'import { fileURLToPath, URL } from "node:url";\n' + next_source
    alias_block = 'resolve: {\n    alias: {\n      "@": fileURLToPath(new URL("./src", import.meta.url)),\n    },\n  },'
    replaced = False
    match = re.search(r"export\s+default\s+defineConfig\s*\(\s*\{", next_source)
    if match:
        insert_at = match.end()
        next_source = next_source[:insert_at] + "\n  " + alias_block + next_source[insert_at:]
        replaced = True
    if not replaced:
        match = re.search(r"defineConfig\s*\(\s*\{", next_source)
        if match:
            insert_at = match.end()
            next_source = next_source[:insert_at] + "\n  " + alias_block + next_source[insert_at:]
            replaced = True
    if not replaced or next_source == source:
        return []
    config_path.write_text(next_source, encoding="utf-8")
    return [config_path.relative_to(project_dir).as_posix()]


def _try_quick_vite_alias_repair(req: AgentReq, execution: dict[str, object], emit) -> dict[str, object] | None:
    if not _vite_unresolved_at_alias_from_execution(execution):
        return None
    project_root = str(req.project_root or ".").strip().strip("/") or "."
    try:
        project_dir = safe_join(_ws(), project_root)
    except Exception:
        return None
    changed_paths = _add_vite_src_alias(project_dir)
    if not changed_paths:
        return None

    commands = list(dict.fromkeys(
        str(command)
        for command in list((execution.get("validation") or {}).get("commands") or [])
        if str(command).strip()
    ))
    if not commands:
        commands = _infer_validation_commands(project_dir)[:4]
    if not commands:
        return {"ok": False, "changed_paths": changed_paths, "summary": "Quick Vite alias repair edited config but found no validation command."}

    emit("status", {"phase": "quick_repair", "message": "Backend quick repair added Vite @ -> src alias before LLM repair..."})
    _emit_command_start_events(emit, tool="quick-repair", phase="quick_repair", project_root=project_root, commands=commands, group="quick repair")
    shell = _run_harness_shell_actions_internal(
        ws_root_path=_ws(),
        project_root=project_root,
        actions=[AgentHarnessShellAction(command=command, cwd=project_root, reason="Quick Vite @ alias repair validation") for command in commands],
        emit=emit,
        tool="quick-repair",
        phase="quick_repair",
        group="quick repair",
    )
    result = {
        "ok": bool(shell.get("ok")),
        "kind": "vite-src-alias",
        "changed_paths": changed_paths,
        "commands": commands,
        "shell": shell,
        "summary": f"Quick Vite alias repair changed {len(changed_paths)} file(s), validation ok={bool(shell.get('ok'))}.",
    }
    emit("tool_output", _harness_tool_output_payload(
        "quick-repair",
        "quick_repair",
        project_root=project_root,
        ok=bool(shell.get("ok")),
        summary=str(result["summary"]),
        paths=changed_paths,
        commands=commands,
        results=_shell_event_results(shell.get("results")),
    ))
    return result


def _try_quick_ts2741_missing_required_prop_repair(req: AgentReq, execution: dict[str, object], emit) -> dict[str, object] | None:
    issues = _ts2741_missing_required_prop_issues_from_execution(execution)
    if not issues:
        return None
    project_root = str(req.project_root or ".").strip().strip("/") or "."
    try:
        project_dir = safe_join(_ws(), project_root)
    except Exception:
        return None
    src_dir = project_dir / "src"
    snapshot_candidates = []
    if src_dir.exists():
        snapshot_candidates = [path.relative_to(project_dir).as_posix() for path in src_dir.glob("**/*.tsx") if path.is_file()][:120]
    snapshots = _snapshot_project_files(project_dir, snapshot_candidates)
    changed_paths = _make_missing_required_props_optional(project_dir, issues)
    if not changed_paths:
        return None

    commands = list(dict.fromkeys(
        str(command)
        for command in list((execution.get("validation") or {}).get("commands") or [])
        if str(command).strip()
    ))
    if not commands:
        commands = _infer_validation_commands(project_dir)[:4]
    if not commands:
        return {"ok": False, "changed_paths": changed_paths, "summary": "Quick TS2741 prop repair edited files but found no validation command."}

    emit("status", {"phase": "quick_repair", "message": "Backend quick repair relaxed missing required UI props before LLM repair..."})
    _emit_command_start_events(emit, tool="quick-repair", phase="quick_repair", project_root=project_root, commands=commands, group="quick repair")
    shell = _run_harness_shell_actions_internal(
        ws_root_path=_ws(),
        project_root=project_root,
        actions=[AgentHarnessShellAction(command=command, cwd=project_root, reason="Quick TS2741 missing prop repair validation") for command in commands],
        emit=emit,
        tool="quick-repair",
        phase="quick_repair",
        group="quick repair",
    )
    rolled_back = _rollback_quick_repair_if_failed(project_dir, shell, snapshots)
    result = {
        "ok": bool(shell.get("ok")),
        "kind": "ts2741-missing-required-prop",
        "changed_paths": changed_paths,
        "rolled_back_paths": rolled_back,
        "issues": issues,
        "commands": commands,
        "shell": shell,
        "summary": f"Quick TS2741 prop repair changed {len(changed_paths)} file(s), validation ok={bool(shell.get('ok'))}, rolled_back={len(rolled_back)}.",
    }
    emit("tool_output", _harness_tool_output_payload(
        "quick-repair",
        "quick_repair",
        project_root=project_root,
        ok=bool(shell.get("ok")),
        summary=str(result["summary"]),
        paths=changed_paths,
        commands=commands,
        results=_shell_event_results(shell.get("results")),
    ))
    return result


def _try_quick_ts2322_unsupported_prop_repair(req: AgentReq, execution: dict[str, object], emit) -> dict[str, object] | None:
    issues = _ts2322_unsupported_prop_issues_from_execution(execution)
    if not issues:
        return None
    project_root = str(req.project_root or ".").strip().strip("/") or "."
    try:
        project_dir = safe_join(_ws(), project_root)
    except Exception:
        return None
    src_dir = project_dir / "src"
    snapshot_candidates = list(dict.fromkeys(
        str(issue.get("path") or "").strip().lstrip("/")
        for issue in issues
        if str(issue.get("path") or "").strip()
    ))
    if src_dir.exists():
        snapshot_candidates.extend(path.relative_to(project_dir).as_posix() for path in src_dir.glob("**/*.tsx") if path.is_file())
    snapshots = _snapshot_project_files(project_dir, list(dict.fromkeys(snapshot_candidates))[:160])
    changed_paths = _add_unsupported_ui_props_to_components(project_dir, issues)
    if not changed_paths:
        changed_paths = _remove_unsupported_jsx_props(project_dir, issues)
    if not changed_paths:
        return None

    commands = list(dict.fromkeys(
        str(command)
        for command in list((execution.get("validation") or {}).get("commands") or [])
        if str(command).strip()
    ))
    if not commands:
        commands = _infer_validation_commands(project_dir)[:4]
    if not commands:
        return {"ok": False, "changed_paths": changed_paths, "summary": "Quick TS2322 prop repair edited files but found no validation command."}

    emit("status", {"phase": "quick_repair", "message": "Backend quick repair removed unsupported JSX props before LLM repair..."})
    _emit_command_start_events(emit, tool="quick-repair", phase="quick_repair", project_root=project_root, commands=commands, group="quick repair")
    shell = _run_harness_shell_actions_internal(
        ws_root_path=_ws(),
        project_root=project_root,
        actions=[AgentHarnessShellAction(command=command, cwd=project_root, reason="Quick TS2322 unsupported prop repair validation") for command in commands],
        emit=emit,
        tool="quick-repair",
        phase="quick_repair",
        group="quick repair",
    )
    rolled_back = _rollback_quick_repair_if_failed(project_dir, shell, snapshots)
    preview_result = (
        _auto_execute_preview_audit(req, project_root)
        if bool(shell.get("ok")) and not rolled_back and _project_has_preview_surface(project_dir, [{"path": path} for path in changed_paths])
        else None
    )
    result = {
        "ok": bool(shell.get("ok")),
        "changed_paths": changed_paths,
        "rolled_back_paths": rolled_back,
        "issues": issues,
        "commands": commands,
        "shell": shell,
        "preview_audit": preview_result,
        "summary": f"Quick TS2322 prop repair changed {len(changed_paths)} file(s), validation ok={bool(shell.get('ok'))}, rolled_back={len(rolled_back)}.",
        "kind": "ts2322-unsupported-jsx-prop",
    }
    emit("tool_output", _harness_tool_output_payload(
        "quick-repair",
        "quick_repair",
        project_root=project_root,
        ok=bool(shell.get("ok")),
        summary=str(result["summary"]),
        paths=changed_paths,
        commands=commands,
        result={"preview_ok": preview_result.get("ok") if isinstance(preview_result, dict) else None},
        results=_shell_event_results(shell.get("results")),
    ))
    return result


def _quick_polish_title_and_description(execution: dict[str, object], project_dir: Path, fallback_name: str) -> tuple[list[str], list[str]]:
    preview = execution.get("preview_audit") if isinstance(execution.get("preview_audit"), dict) else {}
    visual = preview.get("visual_summary") if isinstance(preview.get("visual_summary"), dict) else {}
    heading = str(visual.get("primary_heading") or "").strip()
    title = str(visual.get("title") or "").strip()
    product = str(fallback_name or "Appora Project").strip()
    if heading:
        head_product = re.split(r"\s+(?:helps|is|for|gives|turns|keeps)\s+", heading, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        if 2 <= len(head_product) <= 48:
            product = head_product
    next_title = f"{product} - Production Workspace"
    if heading:
        clean_heading = re.sub(r"\s+", " ", heading).strip().rstrip(".")
        if len(clean_heading) <= 72:
            next_title = clean_heading
        else:
            next_title = f"{product} - {clean_heading[:70 - len(product)].strip().rstrip(',')}"
    next_description = heading or f"{product} production-ready app workspace with responsive UI, clear workflows, and validated preview."
    next_description = re.sub(r"\s+", " ", next_description).strip()
    if len(next_description) < 80:
        next_description = f"{next_description} Built with responsive layout, product detail, and validation-ready interactions."
    next_description = next_description[:180].rstrip()
    next_title_html = escape(next_title, quote=False)
    next_description_html = escape(next_description, quote=True)

    changed: list[str] = []
    notes: list[str] = []
    html_path = project_dir / "index.html"
    try:
        html = html_path.read_text(encoding="utf-8")
    except Exception:
        return changed, notes
    next_html = html
    if re.search(r"<title[^>]*>.*?</title>", next_html, flags=re.IGNORECASE | re.DOTALL):
        if not title or title.lower().startswith("build an ai tool app workspace") or _GENERIC_SAAS_COPY_RE.search(title):
            next_html = re.sub(r"<title[^>]*>.*?</title>", f"<title>{next_title_html}</title>", next_html, count=1, flags=re.IGNORECASE | re.DOTALL)
            notes.append("updated title")
    else:
        next_html = re.sub(r"</head>", f"  <title>{next_title_html}</title>\n</head>", next_html, count=1, flags=re.IGNORECASE)
        notes.append("added title")
    if re.search(r"<meta[^>]+name=['\"]description['\"][^>]*>", next_html, flags=re.IGNORECASE | re.DOTALL):
        next_html = re.sub(
            r"<meta[^>]+name=['\"]description['\"][^>]*>",
            f'<meta name="description" content="{next_description_html}">',
            next_html,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )
        notes.append("updated meta description")
    else:
        next_html = re.sub(
            r"</head>",
            f'  <meta name="description" content="{next_description_html}">\n</head>',
            next_html,
            count=1,
            flags=re.IGNORECASE,
        )
        notes.append("added meta description")
    if next_html != html:
        html_path.write_text(next_html, encoding="utf-8")
        changed.append("index.html")
    return changed, notes


def _quick_polish_tap_targets(project_dir: Path) -> tuple[list[str], list[str]]:
    css_candidates = [
        path for path in [
            project_dir / "src" / "app.css",
            project_dir / "src" / "App.css",
            project_dir / "src" / "index.css",
            project_dir / "src" / "styles.css",
            project_dir / "style.css",
        ]
        if path.exists()
    ]
    if not css_candidates:
        return [], []
    css_path = css_candidates[0]
    try:
        css = css_path.read_text(encoding="utf-8")
    except Exception:
        return [], []
    marker = "Appora quick polish: tap target floor"
    if marker in css:
        return [], []
    addition = """

/* Appora quick polish: tap target floor */
a,
button,
[role="button"],
input,
select,
textarea {
  min-height: 44px;
}

input[type="checkbox"],
input[type="radio"] {
  width: 44px;
  height: 44px;
  min-width: 44px;
  min-height: 44px;
}

nav a,
footer a,
.footer a,
.fallbackNav a {
  display: inline-flex;
  align-items: center;
}
"""
    css_path.write_text(css.rstrip() + addition + "\n", encoding="utf-8")
    return [css_path.relative_to(project_dir).as_posix()], ["added tap target floor"]


def _quick_polish_overflow_prone_css(project_dir: Path) -> tuple[list[str], list[str]]:
    css_candidates = [
        path
        for path in [
            project_dir / "src" / "app.css",
            project_dir / "src" / "App.css",
            project_dir / "src" / "index.css",
            project_dir / "src" / "styles.css",
            project_dir / "style.css",
        ]
        if path.exists()
    ]
    changed: list[str] = []
    notes: list[str] = []
    for css_path in css_candidates[:4]:
        try:
            css = css_path.read_text(encoding="utf-8")
        except Exception:
            continue
        next_css = css
        next_css = re.sub(r"(?<![-\w])white-space\s*:\s*nowrap\s*;", "white-space: normal;", next_css, flags=re.IGNORECASE)
        next_css = re.sub(r"(?<![-\w])width\s*:\s*100vw\s*;", "width: 100%; max-width: 100%;", next_css, flags=re.IGNORECASE)
        next_css = re.sub(r"(?<![-\w])width\s*:\s*(?:max-content|fit-content)\s*;", "width: 100%; max-width: 100%;", next_css, flags=re.IGNORECASE)

        def soften_min_width(match: re.Match[str]) -> str:
            value = int(match.group("value"))
            if value < 390:
                return match.group(0)
            return "min-width: 0; max-width: 100%;"

        next_css = re.sub(
            r"(?<![-\w])min-width\s*:\s*(?P<value>\d{3,4})px\s*;",
            soften_min_width,
            next_css,
            flags=re.IGNORECASE,
        )
        if next_css == css:
            continue
        css_path.write_text(next_css, encoding="utf-8")
        changed.append(css_path.relative_to(project_dir).as_posix())
        notes.append(f"softened overflow-prone CSS in {css_path.relative_to(project_dir).as_posix()}")
    return list(dict.fromkeys(changed)), notes


def _try_quick_preview_polish_repair(req: AgentReq, execution: dict[str, object], emit) -> dict[str, object] | None:
    debt = _preview_polish_debt(execution)
    if not debt:
        return None
    categories = {str(item.get("category") or "") for item in debt if isinstance(item, dict)}
    overflow_debt = any(
        str(item.get("category") or "") in {"source-overflow-risk", "responsive"}
        and "overflow" in str(item.get("detail") or "").lower()
        for item in debt
        if isinstance(item, dict)
    )
    if not (categories & {"metadata", "mobile-tap-targets"} or overflow_debt):
        return None
    project_root = str(req.project_root or ".").strip().strip("/") or "."
    try:
        project_dir = safe_join(_ws(), project_root)
    except Exception:
        return None
    changed_paths: list[str] = []
    notes: list[str] = []
    if "metadata" in categories:
        changed, local_notes = _quick_polish_title_and_description(execution, project_dir, _project_display_name(project_root))
        changed_paths.extend(changed)
        notes.extend(local_notes)
    if "mobile-tap-targets" in categories:
        changed, local_notes = _quick_polish_tap_targets(project_dir)
        changed_paths.extend(changed)
        notes.extend(local_notes)
    if overflow_debt:
        changed, local_notes = _quick_polish_overflow_prone_css(project_dir)
        changed_paths.extend(changed)
        notes.extend(local_notes)
    changed_paths = list(dict.fromkeys(changed_paths))
    if not changed_paths:
        return None

    commands = list(dict.fromkeys(
        str(command)
        for command in list((execution.get("validation") or {}).get("commands") or [])
        if str(command).strip()
    ))
    if not commands:
        commands = _infer_validation_commands(project_dir)[:4]

    emit("status", {"phase": "quick_repair", "message": "Backend quick polish fixed metadata/tap-target preview warnings..."})
    shell = {"ok": True, "results": [], "ran": 0}
    if commands:
        _emit_command_start_events(emit, tool="quick-repair", phase="quick_repair", project_root=project_root, commands=commands, group="quick polish")
        shell = _run_harness_shell_actions_internal(
            ws_root_path=_ws(),
            project_root=project_root,
            actions=[AgentHarnessShellAction(command=command, cwd=project_root, reason="Quick preview polish validation") for command in commands],
            emit=emit,
            tool="quick-repair",
            phase="quick_repair",
            group="quick polish",
        )
    preview_result = _auto_execute_preview_audit(req, project_root) if bool(shell.get("ok")) else None
    result = {
        "ok": bool(shell.get("ok")) and (not isinstance(preview_result, dict) or bool(preview_result.get("ok"))),
        "changed_paths": changed_paths,
        "notes": notes,
        "commands": commands,
        "shell": shell,
        "preview_audit": preview_result,
        "summary": f"Quick preview polish changed {len(changed_paths)} file(s), validation ok={bool(shell.get('ok'))}.",
        "kind": "preview-polish",
    }
    emit("tool_output", _harness_tool_output_payload(
        "quick-repair",
        "quick_repair",
        project_root=project_root,
        ok=bool(result.get("ok")),
        summary=str(result["summary"]),
        paths=changed_paths,
        commands=commands,
        result={"notes": notes, "preview_ok": preview_result.get("ok") if isinstance(preview_result, dict) else None},
        results=_shell_event_results(shell.get("results")),
    ))
    return result


def _try_quick_vite_entrypoint_repair(req: AgentReq, execution: dict[str, object], emit) -> dict[str, object] | None:
    missing_entry = _vite_html_entrypoint_issue_from_execution(execution)
    if not missing_entry:
        return None
    project_root = str(req.project_root or ".").strip().strip("/") or "."
    try:
        project_dir = safe_join(_ws(), project_root)
    except Exception:
        return None
    replacement = _existing_vite_entrypoint(project_dir, missing_entry)
    if not replacement:
        return None
    html_path = project_dir / "index.html"
    if not html_path.exists():
        return None
    try:
        html = html_path.read_text(encoding="utf-8")
    except Exception:
        return None
    next_html = html.replace(f'/{missing_entry}', f'/{replacement}')
    next_html = next_html.replace(missing_entry, replacement)
    if next_html == html:
        return None
    html_path.write_text(next_html, encoding="utf-8")

    commands = list(dict.fromkeys(
        str(command)
        for command in list((execution.get("validation") or {}).get("commands") or [])
        if str(command).strip()
    ))
    if not commands:
        commands = _infer_validation_commands(project_dir)[:4]

    emit("status", {"phase": "quick_repair", "message": "Backend quick repair restored the Vite HTML entrypoint before LLM repair..."})
    shell = {"ok": True, "results": [], "ran": 0}
    if commands:
        _emit_command_start_events(emit, tool="quick-repair", phase="quick_repair", project_root=project_root, commands=commands, group="quick repair")
        shell = _run_harness_shell_actions_internal(
            ws_root_path=_ws(),
            project_root=project_root,
            actions=[AgentHarnessShellAction(command=command, cwd=project_root, reason="Quick Vite entrypoint repair validation") for command in commands],
            emit=emit,
            tool="quick-repair",
            phase="quick_repair",
            group="quick repair",
        )

    result = {
        "ok": bool(shell.get("ok")),
        "changed_paths": ["index.html"],
        "missing_entry": missing_entry,
        "replacement": replacement,
        "commands": commands,
        "shell": shell,
        "summary": f"Quick Vite entrypoint repair changed index.html from {missing_entry} to {replacement}, validation ok={bool(shell.get('ok'))}.",
        "kind": "vite-entrypoint",
    }
    emit("tool_output", _harness_tool_output_payload(
        "quick-repair",
        "quick_repair",
        project_root=project_root,
        ok=bool(shell.get("ok")),
        summary=str(result["summary"]),
        paths=["index.html"],
        commands=commands,
        result={"missing_entry": missing_entry, "replacement": replacement},
        results=_shell_event_results(shell.get("results")),
    ))
    return result


def _promote_title_element_to_h1(project_dir: Path) -> list[str]:
    src_dir = project_dir / "src"
    if not src_dir.exists():
        return []
    candidates = [
        *(src_dir.glob("App.tsx")),
        *(src_dir.glob("App.jsx")),
        *(src_dir.glob("**/*.tsx")),
        *(src_dir.glob("**/*.jsx")),
    ]
    seen: set[Path] = set()
    for path in candidates:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        if re.search(r"<h1\b", text, flags=re.IGNORECASE):
            continue
        next_text = re.sub(
            r"<div(?P<attrs>[^>]*className=(?:\"[^\"]*(?:header-title|page-title|app-title|dashboard-title|title)[^\"]*\"|'[^']*(?:header-title|page-title|app-title|dashboard-title|title)[^']*')[^>]*)>(?P<body>[^<]{3,120})</div>",
            r"<h1\g<attrs>>\g<body></h1>",
            text,
            count=1,
            flags=re.IGNORECASE,
        )
        if next_text == text:
            next_text = re.sub(
                r"<p(?P<attrs>[^>]*className=(?:\"[^\"]*(?:page-title|app-title|dashboard-title|title)[^\"]*\"|'[^']*(?:page-title|app-title|dashboard-title|title)[^']*')[^>]*)>(?P<body>[^<]{3,120})</p>",
                r"<h1\g<attrs>>\g<body></h1>",
                text,
                count=1,
                flags=re.IGNORECASE,
            )
        if next_text == text:
            continue
        path.write_text(next_text, encoding="utf-8")
        return [path.relative_to(project_dir).as_posix()]
    return []


def _try_quick_missing_h1_repair(req: AgentReq, execution: dict[str, object], emit) -> dict[str, object] | None:
    if not _preview_missing_h1_issue(execution):
        return None
    project_root = str(req.project_root or ".").strip().strip("/") or "."
    try:
        project_dir = safe_join(_ws(), project_root)
    except Exception:
        return None
    changed_paths = _promote_title_element_to_h1(project_dir)
    if not changed_paths:
        return None

    commands = list(dict.fromkeys(
        str(command)
        for command in list((execution.get("validation") or {}).get("commands") or [])
        if str(command).strip()
    ))
    if not commands:
        commands = _infer_validation_commands(project_dir)[:4]

    emit("status", {"phase": "quick_repair", "message": "Backend quick repair promoted the visible page title to H1 before LLM repair..."})
    shell = {"ok": True, "results": [], "ran": 0}
    if commands:
        _emit_command_start_events(emit, tool="quick-repair", phase="quick_repair", project_root=project_root, commands=commands, group="quick repair")
        shell = _run_harness_shell_actions_internal(
            ws_root_path=_ws(),
            project_root=project_root,
            actions=[AgentHarnessShellAction(command=command, cwd=project_root, reason="Quick missing H1 repair validation") for command in commands],
            emit=emit,
            tool="quick-repair",
            phase="quick_repair",
            group="quick repair",
        )
    preview_result = _auto_execute_preview_audit(req, project_root) if bool(shell.get("ok")) else None
    result = {
        "ok": bool(shell.get("ok")) and (not isinstance(preview_result, dict) or bool(preview_result.get("ok"))),
        "changed_paths": changed_paths,
        "commands": commands,
        "shell": shell,
        "preview_audit": preview_result,
        "summary": f"Quick missing-H1 repair changed {len(changed_paths)} file(s), validation ok={bool(shell.get('ok'))}.",
        "kind": "missing-h1",
    }
    emit("tool_output", _harness_tool_output_payload(
        "quick-repair",
        "quick_repair",
        project_root=project_root,
        ok=bool(result.get("ok")),
        summary=str(result["summary"]),
        paths=changed_paths,
        commands=commands,
        result={"preview_ok": preview_result.get("ok") if isinstance(preview_result, dict) else None},
        results=_shell_event_results(shell.get("results")),
    ))
    return result


def _try_quick_missing_package_repair(req: AgentReq, execution: dict[str, object], emit) -> dict[str, object] | None:
    project_root = str(req.project_root or ".").strip().strip("/") or "."
    try:
        project_dir = safe_join(_ws(), project_root)
    except Exception:
        return None
    packages = _missing_external_packages_from_execution(execution, project_dir)
    if not packages:
        return None

    validation_commands = list(dict.fromkeys(
        str(command)
        for command in list((execution.get("validation") or {}).get("commands") or [])
        if str(command).strip()
    ))
    if not validation_commands:
        validation_commands = _infer_validation_commands(project_dir)[:4]
    commands = [f"npm install --no-audit --no-fund {' '.join(packages)}", *validation_commands]

    emit("status", {"phase": "quick_repair", "message": f"Backend quick repair installed missing package(s): {', '.join(packages)}..."})
    _emit_command_start_events(emit, tool="quick-repair", phase="quick_repair", project_root=project_root, commands=commands, group="quick repair")
    shell = _run_harness_shell_actions_internal(
        ws_root_path=_ws(),
        project_root=project_root,
        actions=[AgentHarnessShellAction(command=command, cwd=project_root, reason="Quick missing package repair") for command in commands],
        emit=emit,
        tool="quick-repair",
        phase="quick_repair",
        group="quick repair",
    )
    changed_paths = ["package.json", "package-lock.json"]
    result = {
        "ok": bool(shell.get("ok")),
        "changed_paths": changed_paths,
        "packages": packages,
        "commands": commands,
        "shell": shell,
        "summary": f"Quick missing-package repair installed {', '.join(packages)}, validation ok={bool(shell.get('ok'))}.",
        "kind": "missing-package",
    }
    emit("tool_output", _harness_tool_output_payload(
        "quick-repair",
        "quick_repair",
        project_root=project_root,
        ok=bool(shell.get("ok")),
        summary=str(result["summary"]),
        paths=changed_paths,
        commands=commands,
        results=_shell_event_results(shell.get("results")),
    ))
    return result


def _remember_backend_execution_state(req: AgentReq, result: dict) -> None:
    execution = result.get("execution")
    if not isinstance(execution, dict) or execution.get("auto_execute") is not True:
        return
    intent_payload = result.get("intent") if isinstance(result.get("intent"), dict) else {}
    should_write = bool(intent_payload.get("should_write_files")) or bool(result.get("changes")) or bool(result.get("actions"))
    if not should_write:
        return

    class _IntentSnapshot:
        kind = str(intent_payload.get("kind") or "command")
        confidence = float(intent_payload.get("confidence") or 0.86)
        rationale = str(intent_payload.get("rationale") or "backend execution state")
        should_write_files = True
        should_run_tools = True
        wants_app_builder = True

    trace = result.get("trace") if isinstance(result.get("trace"), dict) else {}
    _remember_project_work_state(
        project_root=str(req.project_root or ".").strip().strip("/") or ".",
        build_mode=str(req.build_mode or "full-agent"),
        user_input=str(req.input or ""),
        spoken=str(result.get("spoken") or ""),
        changes=list(result.get("changes") or []),
        actions=list(result.get("actions") or []),
        intent=_IntentSnapshot(),  # type: ignore[arg-type]
        task_state=dict(trace.get("task_state") or {}) if isinstance(trace.get("task_state"), dict) else {},
        completion_report=dict(execution.get("completion_report") or {}) if isinstance(execution.get("completion_report"), dict) else {},
        failure_analysis=dict(execution.get("failure_analysis") or {}) if isinstance(execution.get("failure_analysis"), dict) else {},
    )


def _execution_memory_outcome(execution: dict | None) -> dict:
    if not isinstance(execution, dict):
        return {}
    completion = execution.get("completion_report") if isinstance(execution.get("completion_report"), dict) else {}
    validation = execution.get("validation") if isinstance(execution.get("validation"), dict) else {}
    preview = execution.get("preview_audit") if isinstance(execution.get("preview_audit"), dict) else {}
    repairs = execution.get("repairs") if isinstance(execution.get("repairs"), list) else []
    commands: list[str] = []
    for command in list(validation.get("commands") or [])[:6]:
        text = str(command or "").strip()
        if text:
            commands.append(text)
    final_changed_paths: list[str] = []
    rollback_paths: list[str] = []

    def add_path(target: list[str], raw: object) -> None:
        text = str(raw or "").strip()
        if text and text not in target:
            target.append(text)

    apply_result = execution.get("apply") if isinstance(execution.get("apply"), dict) else {}
    for path in list(apply_result.get("paths") or []):
        add_path(final_changed_paths, path)
    for repair in repairs:
        if not isinstance(repair, dict):
            continue
        for path in list(repair.get("changed_paths") or []):
            add_path(final_changed_paths, path)
        repair_execution = repair.get("execution") if isinstance(repair.get("execution"), dict) else {}
        repair_apply = repair_execution.get("apply") if isinstance(repair_execution.get("apply"), dict) else {}
        for path in list(repair_apply.get("paths") or []):
            add_path(final_changed_paths, path)
        rollback = repair.get("rollback") if isinstance(repair.get("rollback"), dict) else {}
        if rollback and rollback.get("ok"):
            for path in list(rollback.get("paths") or rollback.get("restored_paths") or repair_apply.get("paths") or repair.get("changed_paths") or []):
                add_path(rollback_paths, path)
    for item in list(execution.get("quick_repairs") or []):
        if not isinstance(item, dict):
            continue
        for path in list(item.get("changed_paths") or []):
            add_path(final_changed_paths, path)
        for path in list(item.get("rolled_back") or []):
            add_path(rollback_paths, path)
    return {
        "ok": bool(completion.get("ok", execution.get("ok"))),
        "state": str(completion.get("state") or ("completed" if execution.get("ok") else "blocked")),
        "summary": str(completion.get("summary") or execution.get("summary") or ""),
        "validation_ok": validation.get("ok") if validation else None,
        "preview_ok": preview.get("ok") if preview and not preview.get("skipped") else None,
        "preview_summary": str(preview.get("summary") or "") if preview else "",
        "repair_passes": len(repairs),
        "validation_commands": commands,
        "rollback_count": len(rollback_paths),
        "rollback_paths": rollback_paths[:8],
        "final_changed_paths": final_changed_paths[:12],
    }


def _remember_backend_execution_memory(req: AgentReq, ws_root: Path, result: dict) -> None:
    execution = result.get("execution") if isinstance(result.get("execution"), dict) else None
    if not isinstance(execution, dict) or execution.get("auto_execute") is not True:
        return
    if not (result.get("changes") or result.get("actions")):
        return
    trace = result.get("trace") if isinstance(result.get("trace"), dict) else {}
    execution = result.get("execution") if isinstance(result.get("execution"), dict) else {}
    execution_outcome = _execution_memory_outcome(execution)
    if not execution_outcome.get("final_changed_paths"):
        execution_outcome["final_changed_paths"] = [
            str(item.get("path") or "").strip()
            for item in list(result.get("changes") or [])
            if isinstance(item, dict) and str(item.get("path") or "").strip()
        ][:12]
    remember_agent_run(
        ws_root,
        project_root=str(req.project_root or ".").strip().strip("/") or ".",
        build_mode=str(req.build_mode or "full-agent"),
        interaction_kind=str((result.get("intent") or {}).get("kind") or "command") if isinstance(result.get("intent"), dict) else "command",
        user_input=str(req.input or ""),
        spoken=str(result.get("spoken") or ""),
        changes=list(result.get("changes") or []),
        actions=list(result.get("actions") or []),
        execution_outcome=execution_outcome,
        task_state=dict(trace.get("task_state") or {}) if isinstance(trace.get("task_state"), dict) else {},
        completion_report=dict(execution.get("completion_report") or {}) if isinstance(execution.get("completion_report"), dict) else {},
        failure_analysis=dict(execution.get("failure_analysis") or {}) if isinstance(execution.get("failure_analysis"), dict) else {},
    )


@app.post("/api/agent/worker/run")
def agent_worker_run(req: AgentWorkerRunReq, request: Request):
    _require_worker_auth(request)
    return _run_agent_worker_jobs(job_id=req.job_id, limit=req.limit)


@app.get("/api/agent/worker/run")
def agent_worker_run_get(request: Request, job_id: str | None = None, limit: int = 1):
    _require_worker_auth(request)
    return _run_agent_worker_jobs(job_id=job_id, limit=limit)


def _run_agent_impl(req: AgentReq, event_cb=None, job_id: str | None = None):
    streamed_spoken = False
    streamed_spoken_text = ""
    observed_events: list[dict] = []
    run_started_at = time.time()

    def emit(event: str, data: dict):
        nonlocal streamed_spoken, streamed_spoken_text
        payload = dict(data or {})
        if event == "delta" and payload.get("spoken_chunk"):
            streamed_spoken = True
            streamed_spoken_text += str(payload.get("spoken_chunk") or "")
        observed_events.append({"event_type": event, "payload": dict(payload), "created_at": time.time()})
        if job_id:
            payload.setdefault("job_id", job_id)
            _record_agent_job_event(job_id, event, payload)
        if event_cb:
            try:
                event_cb(event, payload)
            except Exception:
                pass

    _update_agent_job_record(job_id, "running")
    emit("status", {"phase": "starting", "message": "Nyusun konteks kerja dulu..."})
    ws_root = _ws()
    _hydrate_hosted_project(ws_root, getattr(req, "project_root", ".") or ".")
    if req.auto_execute:
        bootstrap = _bootstrap_missing_agent_project(ws_root, getattr(req, "project_root", ".") or ".")
        if bootstrap:
            emit("status", {"phase": "bootstrap", "message": "Project baru belum ada, Appora menyiapkan workspace React/Vite kosong untuk agent..."})
            emit("tool_output", _harness_tool_output_payload(
                "bootstrap",
                "bootstrap",
                project_root=str(req.project_root or ".").strip().strip("/") or ".",
                ok=True,
                summary=f"Bootstrapped missing project with {len(list(bootstrap.get('paths') or []))} file(s).",
                paths=list(bootstrap.get("paths") or []),
                result={"dependency_logs": bootstrap.get("dependency_logs") or []},
            ))

    try:
        with _agent_lock_for_current_provider():
            pipeline = run_agent_pipeline(req, ws_root=ws_root, emit=emit)

        sug_spoken = str(pipeline.get("spoken") or "")
        sug_log = str(pipeline.get("log") or "")
        normalized_actions = list(pipeline.get("actions") or [])
        normalized_changes = list(pipeline.get("changes") or [])

        emit("status", {"phase": "diffing", "message": "Lagi nyusun diff biar siap dipakai UI..."})
        out_changes = _prepare_agent_out_changes(ws_root, normalized_changes)

        result = {
            "job_id": job_id,
            "spoken": sug_spoken,
            "log": sug_log,
            "changes": out_changes,
            "actions": normalized_actions,
            "intent": dict(pipeline.get("intent") or {}),
            "trace": dict(pipeline.get("trace") or {}),
            "no_changes": len(out_changes) == 0 and len(normalized_actions) == 0,
        }
        if req.auto_execute and _trace_has_blocking_verifier_failures(result["trace"]):
            repair = _run_backend_verifier_repair_pass(
                req,
                ws_root,
                result["trace"],
                emit,
                base_changes=out_changes,
                base_actions=normalized_actions,
            )
            merged_repair_changes = _merge_repair_changes(out_changes, list(repair.get("changes") or []))
            merged_repair_actions = list(repair.get("actions") or [])
            merged_repair_trace = _reverify_merged_verifier_output(req, ws_root, merged_repair_changes, merged_repair_actions)
            repair["merged_trace"] = merged_repair_trace
            repair["ok"] = not _trace_has_blocking_verifier_failures(merged_repair_trace)
            repair["failure_summary"] = (
                _trace_verifier_failure_summary(merged_repair_trace)
                if _trace_has_blocking_verifier_failures(merged_repair_trace)
                else ""
            )
            if not repair.get("ok") and "custom class(es) lack CSS definitions" in str(repair.get("failure_summary") or ""):
                gated_changes, gated_paths = _gate_missing_css_classes_in_changes(merged_repair_changes, str(repair.get("failure_summary") or ""))
                if gated_paths:
                    gated_trace = _reverify_merged_verifier_output(req, ws_root, gated_changes, merged_repair_actions)
                    if not _trace_has_blocking_verifier_failures(gated_trace):
                        merged_repair_changes = gated_changes
                        merged_repair_trace = gated_trace
                        repair["merged_trace"] = merged_repair_trace
                        repair["ok"] = True
                        repair["failure_summary"] = ""
                        repair["deterministic_css_class_gate"] = {
                            "paths": gated_paths,
                            "summary": "Added missing CSS class definitions during targeted verifier repair.",
                        }
            if not repair.get("ok") and "frontend-interaction-integrity" in str(repair.get("failure_summary") or ""):
                gated_changes, gated_paths = _gate_inert_final_buttons_in_changes(merged_repair_changes)
                if gated_paths:
                    gated_trace = _reverify_merged_verifier_output(req, ws_root, gated_changes, merged_repair_actions)
                    if not _trace_has_blocking_verifier_failures(gated_trace):
                        merged_repair_changes = gated_changes
                        merged_repair_trace = gated_trace
                        repair["merged_trace"] = merged_repair_trace
                        repair["ok"] = True
                        repair["failure_summary"] = ""
                        repair["deterministic_interaction_gate"] = {
                            "paths": gated_paths,
                            "summary": "Disabled active-looking inert buttons during targeted verifier repair.",
                        }
            if not repair.get("ok") and "frontend-business-data-honesty" in str(repair.get("failure_summary") or ""):
                gated_changes, gated_paths = _gate_fake_business_data_in_changes(merged_repair_changes)
                if gated_paths:
                    gated_trace = _reverify_merged_verifier_output(req, ws_root, gated_changes, merged_repair_actions)
                    if not _trace_has_blocking_verifier_failures(gated_trace):
                        merged_repair_changes = gated_changes
                        merged_repair_trace = gated_trace
                        repair["merged_trace"] = merged_repair_trace
                        repair["ok"] = True
                        repair["failure_summary"] = ""
                        repair["deterministic_business_data_gate"] = {
                            "paths": gated_paths,
                            "summary": "Neutralized invented business contact/claim data during targeted verifier repair.",
                        }
            result["verifier_repair"] = repair
            if repair.get("spoken"):
                result["spoken"] = str(repair.get("spoken") or "")
            if repair.get("log"):
                result["log"] = f"{str(result.get('log') or '').strip()} verifier_repair=1 {str(repair.get('log') or '').strip()}".strip()
            if repair.get("ok"):
                out_changes = merged_repair_changes
                normalized_actions = merged_repair_actions
                result["changes"] = out_changes
                result["actions"] = normalized_actions
                result["intent"] = dict(repair.get("intent") or result.get("intent") or {})
                result["trace"] = merged_repair_trace
                result["no_changes"] = len(out_changes) == 0 and len(normalized_actions) == 0

        if req.auto_execute and (out_changes or normalized_actions) and not _trace_has_blocking_verifier_failures(result["trace"]):
            result["execution"] = _auto_execute_agent_result(req, out_changes, normalized_actions, emit)
            _remember_backend_execution_state(req, result)
            try:
                _remember_backend_execution_memory(req, ws_root, result)
            except Exception:
                pass
        elif req.auto_execute and _trace_has_blocking_verifier_failures(result["trace"]):
            result["execution"] = {
                "auto_execute": True,
                "ok": False,
                "skipped": True,
                "reason": "Verifier reported blocking failures before backend execution.",
                "verifier_failures": _trace_verifier_failure_summary(result["trace"]),
                "project_root": str(req.project_root or ".").strip().strip("/") or ".",
                "apply": None,
                "shell": None,
            }
        result["observability"] = build_agent_observability(observed_events, result=result, started_at=run_started_at, finished_at=time.time())
        final_spoken = str(result.get("spoken") or "")
        should_stream_final_spoken = not streamed_spoken
        execution = result.get("execution") if isinstance(result.get("execution"), dict) else None
        trace_blocked = _trace_has_blocking_verifier_failures(result["trace"])
        if streamed_spoken and final_spoken and (
            trace_blocked
            or (isinstance(execution, dict) and execution.get("auto_execute") and not execution.get("ok"))
        ):
            should_stream_final_spoken = final_spoken.strip() not in streamed_spoken_text.strip()
        if should_stream_final_spoken:
            prefix = "\n\n" if streamed_spoken and streamed_spoken_text.strip() else ""
            for chunk in _spoken_stream_chunks(prefix + final_spoken):
                emit("delta", {"spoken_chunk": chunk})
            result["observability"] = build_agent_observability(observed_events, result=result, started_at=run_started_at, finished_at=time.time())
        _update_agent_job_record(job_id, "completed", result=result)
        done_message = "Beres, hasil agent siap dipakai."
        if isinstance(execution, dict) and execution.get("auto_execute"):
            completion_report = execution.get("completion_report") if isinstance(execution.get("completion_report"), dict) else {}
            if not bool(completion_report.get("ok", execution.get("ok"))):
                summary = str(completion_report.get("summary") or execution.get("summary") or "Agent execution masih gagal.").strip()
                done_message = summary if summary.lower().startswith("blocked") else f"Blocked: {summary}"
        emit("done", {"message": done_message, "result": result})
        return result
    except RuntimeError as exc:
        _update_agent_job_record(job_id, "failed", error=str(exc))
        emit("error", {"message": str(exc)})
        raise HTTPException(400, str(exc))
    except Exception as exc:
        _update_agent_job_record(job_id, "failed", error=str(exc))
        emit("error", {"message": str(exc)})
        raise HTTPException(500, str(exc))


@app.post("/api/agent")
def agent(req: AgentReq):
    """Suggest a multi-file patch. Adds per-file unified diffs."""
    job_id = _create_agent_job_record(req)
    if req.background:
        _record_agent_job_event(job_id, "status", {"phase": "queued", "message": "Agent job queued for background worker.", "job_id": job_id})
        return {"ok": True, "job_id": job_id, "status": "queued", "background": True}
    if req.stream:
        bound_session_id = CURRENT_SESSION_ID.get()
        bound_user_id = CURRENT_USER_ID.get()
        bound_profile_id = CURRENT_PROFILE_ID.get()
        bound_job_id = job_id

        def event_stream():
            import queue as queue_mod

            stream_queue: queue_mod.Queue[str | None] = queue_mod.Queue()

            def push(event: str, data: dict):
                stream_queue.put(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n")

            def worker():
                session_token = CURRENT_SESSION_ID.set(bound_session_id)
                user_token = CURRENT_USER_ID.set(bound_user_id)
                profile_token = CURRENT_PROFILE_ID.set(bound_profile_id)
                push("status", {"phase": "queued", "message": "Agent diterima, mulai jalan...", "job_id": bound_job_id})
                try:
                    _record_agent_job_event(bound_job_id, "status", {"phase": "queued", "message": "Agent diterima, mulai jalan...", "job_id": bound_job_id})
                    _run_agent_impl(req, event_cb=push, job_id=bound_job_id)
                except HTTPException as exc:
                    _update_agent_job_record(bound_job_id, "failed", error=str(exc.detail))
                    push("error", {"message": str(exc.detail)})
                except Exception as exc:
                    _update_agent_job_record(bound_job_id, "failed", error=str(exc))
                    push("error", {"message": str(exc)})
                finally:
                    CURRENT_PROFILE_ID.reset(profile_token)
                    CURRENT_USER_ID.reset(user_token)
                    CURRENT_SESSION_ID.reset(session_token)
                    stream_queue.put(None)

            threading.Thread(target=worker, daemon=True).start()

            while True:
                item = stream_queue.get()
                if item is None:
                    break
                yield item

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return _run_agent_impl(req, job_id=job_id)
