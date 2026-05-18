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
from html import unescape
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request as URLRequest, urlopen

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from api.settings import ROOT, ENV_PATH, load_settings
from api.supabase_store import (
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
from api.auth_router import build_auth_router
from api.auth_identity import CURRENT_REQUEST_USER, resolve_request_user, sanitize_user_id
from api.oauth_runtime import CURRENT_PROFILE_ID
from api.projects_router import build_projects_router
from api.preferences_router import build_preferences_router
from api.settings_router import build_settings_router
from api.fs import list_tree, read_text, write_text, diff_text, safe_join
from api.agent_mcp import discover_mcp_servers, list_mcp_tools
from api.agent_memory import get_agent_memory_overview, sync_project_docs_to_supabase
from api.agent_runtime import _remember_project_work_state, run_agent_pipeline
from api.agent_skills import detect_project_stack
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


@app.get("/api/healthz")
def healthz():
    return {
        "ok": True,
        "service": "appora-api",
        "session": CURRENT_SESSION_ID.get(),
        "user": CURRENT_USER_ID.get(),
    }


@app.get("/api/auth/debug")
def auth_debug(request: Request):
    user = CURRENT_REQUEST_USER.get()
    authorization = request.headers.get("Authorization") or ""
    has_bearer = authorization.lower().startswith("bearer ") and len(authorization.split(" ", 1)[-1].strip()) > 0
    return {
        "ok": True,
        "auth_source": user.auth_source if user else "none",
        "user_id": user.user_id if user else CURRENT_USER_ID.get(),
        "supabase_user_id": user.supabase_user_id if user else None,
        "email_set": bool(user.email) if user else False,
        "has_bearer": has_bearer,
        "has_supabase_backend": has_supabase(),
    }


DANGEROUS_COMMAND_FRAGMENTS = ["rm -rf /", "mkfs", "dd if="]
VALIDATION_SCRIPT_NAMES = ("lint", "build", "typecheck", "check")
PREVIEW_AUDIT_HOSTS = {"localhost", "127.0.0.1", "::1"}
PREVIEW_BROWSER_AUDIT_TIMEOUT_SECONDS = 18
PREVIEW_BROWSER_AUDIT_SETTLE_MS = 600


def _resolve_node_binary() -> str | None:
    return shutil.which("node")


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


def _browser_preview_audit_ready(project_dir: Path) -> bool:
    return bool(_resolve_node_binary() and _playwright_audit_script().exists())


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


def _extract_text_matches(pattern: str, html: str, limit: int) -> list[str]:
    values: list[str] = []
    for match in re.findall(pattern, html, flags=re.IGNORECASE | re.DOTALL):
        text = _clean_html_text(match)
        if text:
            values.append(text[:160])
        if len(values) >= limit:
            break
    return values


def _scan_project_quality_signals(project_dir: Path) -> dict[str, bool]:
    patterns = {
        "responsive": [r"@media\b", r"\bsm:", r"\bmd:", r"\blg:", r"clamp\(", r"minmax\(", r"grid-template", r"useMediaQuery", r"matchMedia\("],
        "loading": [r"\bloading\b", r"isLoading", r"pending", r"skeleton", r"spinner"],
        "error": [r"\berror\b", r"failed", r"retry", r"try again", r"catch \("],
        "empty": [r"empty state", r"no results", r"no items", r"not found", r"belum ada", r"empty"],
        "labels": [r"<label\b", r"htmlFor=", r"aria-label=", r"aria-labelledby="],
    }
    hits = {key: False for key in patterns}
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
        if all(hits.values()):
            break
    return hits


def _build_quality_checks(snapshot: dict, *, project_signals: dict[str, bool] | None = None) -> list[dict[str, str | bool]]:
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
    mobile_text_overflow_nodes = [str(item) for item in (snapshot.get("mobile_text_overflow_nodes") or snapshot.get("text_overflow_nodes") or []) if str(item).strip()]
    broken_images = [str(item) for item in (snapshot.get("broken_images") or []) if str(item).strip()]
    fixed_overlays = [str(item) for item in (snapshot.get("mobile_fixed_overlays") or snapshot.get("fixed_overlays") or []) if str(item).strip()]
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
        "id": "state-loading",
        "label": "Loading state",
        "ok": bool(project_signals.get("loading")),
        "detail": "Ada sinyal loading/skeleton state di source." if project_signals.get("loading") else "Belum ketemu loading/skeleton state yang jelas di source.",
    })
    checks.append({
        "id": "state-error",
        "label": "Error state",
        "ok": bool(project_signals.get("error")),
        "detail": "Ada sinyal error/retry handling di source." if project_signals.get("error") else "Belum ketemu error/retry state yang jelas di source.",
    })
    checks.append({
        "id": "state-empty",
        "label": "Empty state",
        "ok": bool(project_signals.get("empty")),
        "detail": "Ada sinyal empty/no-results state di source." if project_signals.get("empty") else "Belum ketemu empty/no-results state yang jelas di source.",
    })
    return checks


def _build_preview_audit_result(
    preview_url: str,
    snapshot: dict,
    *,
    audit_mode: str,
    max_excerpt_chars: int = 800,
    runtime_warnings: list[str] | None = None,
    project_signals: dict[str, bool] | None = None,
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
    word_count = max(0, int(snapshot.get("word_count") or 0))
    image_count = max(0, int(snapshot.get("image_count") or 0))
    images_missing_alt = max(0, int(snapshot.get("images_missing_alt") or 0))
    interactive_count = max(0, int(snapshot.get("interactive_count") or 0))
    unlabeled_interactive = list_field("unlabeled_interactive")
    small_tap_targets = list_field("small_tap_targets")
    text_overflow_nodes = list_field("text_overflow_nodes")
    mobile_text_overflow_nodes = list_field("mobile_text_overflow_nodes")
    broken_images = list_field("broken_images", 6)
    fixed_overlays = list_field("fixed_overlays", 4)
    mobile_fixed_overlays = list_field("mobile_fixed_overlays", 4)
    console_errors = [str(item).strip()[:240] for item in (snapshot.get("console_errors") or []) if str(item).strip()][:8]
    page_errors = [str(item).strip()[:240] for item in (snapshot.get("page_errors") or []) if str(item).strip()][:6]
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
    if not title:
        detail = "Preview page is missing a <title> tag."
        issues.append(detail)
        add_issue("warning", "metadata", detail, "Tambahkan title yang menjelaskan app/page.")
    if not meta_description:
        detail = "Preview page is missing a meta description."
        issues.append(detail)
        add_issue("warning", "metadata", detail, "Tambahkan meta description singkat untuk kualitas production.")
    if not headings:
        detail = "Preview page has no visible H1 heading."
        issues.append(detail)
        add_issue("blocking", "content", detail, "Tambahkan H1 yang jelas di first viewport.")
    if word_count < 40:
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
            severity = "blocking" if check_id in {"responsive-overflow", "a11y-interactive-labels", "mobile-text-fit", "image-loads", "blocking-overlays"} else "warning"
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
        f"interactive={interactive_count}",
        f"words={word_count}",
        f"blocking={blocking_count}",
        f"warnings={warning_count}",
    ]
    viewport = snapshot.get("viewport") if isinstance(snapshot.get("viewport"), dict) else {}
    mobile_viewport = snapshot.get("mobile_viewport") if isinstance(snapshot.get("mobile_viewport"), dict) else {}
    visual_summary = {
        "mode": audit_mode,
        "title": title,
        "primary_heading": headings[0] if headings else "",
        "desktop_viewport": viewport,
        "mobile_viewport": mobile_viewport,
        "desktop_overflow_x": bool(snapshot.get("desktop_overflow_x")),
        "mobile_overflow_x": bool(snapshot.get("mobile_overflow_x")),
        "word_count": word_count,
        "interactive_count": interactive_count,
        "button_labels": buttons[:6],
        "link_labels": links[:6],
        "top_blockers": [item for item in issue_details if item.get("severity") == "blocking"][:6],
        "top_warnings": [item for item in issue_details if item.get("severity") == "warning"][:6],
        "runtime_errors": [*page_errors, *console_errors][:8],
        "excerpt": excerpt[:600],
    }
    repair_brief_parts = [
        f"Preview audit mode={audit_mode}, blocking={blocking_count}, warnings={warning_count}.",
        f"Title: {title or '(missing)'}; H1: {headings[0] if headings else '(missing)'}.",
    ]
    if bool(snapshot.get("mobile_overflow_x")):
        repair_brief_parts.append("Mobile viewport has horizontal overflow.")
    if page_errors or console_errors:
        repair_brief_parts.append(f"Runtime messages: {' | '.join([*page_errors, *console_errors][:3])}")
    if issue_details:
        repair_brief_parts.append("Top issues: " + " | ".join(
            f"{item.get('severity')} {item.get('category')}: {item.get('detail')}"
            for item in issue_details[:5]
        ))

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
        "repair_brief": " ".join(repair_brief_parts)[:2000],
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


def _run_playwright_preview_audit(
    preview_url: str,
    project_dir: Path,
    max_excerpt_chars: int = 800,
    *,
    project_signals: dict[str, bool] | None = None,
) -> tuple[dict | None, str | None]:
    if not _browser_preview_audit_ready(project_dir):
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
    project_signals: dict[str, bool] | None = None,
) -> dict:
    snapshot = _extract_preview_snapshot_from_html(html, max_excerpt_chars=max_excerpt_chars)
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


class CommandPolicyDecision(BaseModel):
    ok: bool
    command: str
    risk_level: Literal["safe", "approval_required", "blocked"]
    reason: str
    requires_approval: bool = False


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
)

_APPROVAL_COMMANDS = {"git", "npx", "pnpm", "yarn", "bun", "npm"}
_BLOCKED_COMMANDS = {"rm", "sudo", "su", "dd", "mkfs", "mount", "umount", "shutdown", "reboot", "kill", "pkill"}
_DESTRUCTIVE_GIT_ARGS = {"reset", "clean", "checkout", "restore", "rebase"}
_SHELL_CHAIN_OPERATORS = {"&&"}
_SHELL_UNSAFE_TOKENS = {";", "|", "||", "&", ">", ">>", "<", "<<", "$(", "`"}


def _command_policy_decision_for_parts(clean: str, parts: list[str]) -> CommandPolicyDecision:
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


def _command_policy_decision(command: str) -> CommandPolicyDecision:
    clean = str(command or "").strip()
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
            decision = _command_policy_decision_for_parts(" ".join(segment), segment)
            if not decision.ok:
                return CommandPolicyDecision(ok=False, command=clean, risk_level=decision.risk_level, reason=f"Shell chain ditahan: {decision.reason}", requires_approval=True)
        return CommandPolicyDecision(ok=True, command=clean, risk_level="safe", reason="Semua command dalam chain project-scoped dan boleh auto-run.", requires_approval=False)

    if any(part in _SHELL_UNSAFE_TOKENS or any(token in part for token in ("$(", "`")) for part in parts):
        return CommandPolicyDecision(ok=False, command=clean, risk_level="approval_required", reason="Command memakai operator shell yang butuh approval eksplisit.", requires_approval=True)

    return _command_policy_decision_for_parts(clean, parts)


def _infer_validation_commands(project_dir: Path) -> list[str]:
    commands: list[str] = []
    package_manager = _resolve_package_manager(project_dir)

    package_json = project_dir / "package.json"
    if package_json.exists():
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


class TerminalRunReq(BaseModel):
    command: str
    cwd: str | None = None
    reason: str | None = None


class AgentHarnessShellAction(BaseModel):
    command: str
    cwd: str | None = None
    reason: str | None = None


class AgentHarnessRunShellReq(BaseModel):
    project_root: str = "."
    actions: list[AgentHarnessShellAction]


class CommandPolicyReq(BaseModel):
    command: str
    cwd: str | None = None
    reason: str | None = None


@app.post("/api/agent/command-policy/check", response_model=CommandPolicyDecision)
def command_policy_check(req: CommandPolicyReq):
    return _command_policy_decision(req.command)


@app.post("/api/terminal/run")
def terminal_run(req: TerminalRunReq):
    ws_root = _session_state().get("workspace")
    if not ws_root:
        raise HTTPException(400, "No workspace selected")

    cwd = ws_root
    if req.cwd:
        cwd = safe_join(ws_root, req.cwd)

    try:
        policy = _command_policy_decision(req.command)
        if not policy.ok:
            raise HTTPException(403, {"message": policy.reason, "policy": policy.model_dump()})
        result = _run_shell_command(req.command, cwd)
        result["policy"] = policy.model_dump()
        result["synced_files"] = _sync_hosted_project_text_files_after_shell(ws_root, cwd)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/agent/harness/run-shell")
def agent_harness_run_shell(req: AgentHarnessRunShellReq):
    ws_root = _session_state().get("workspace")
    if not ws_root:
        raise HTTPException(400, "No workspace selected")

    ws_root_path = Path(ws_root)
    project_root = str(req.project_root or ".").strip().strip("/") or "."
    _hydrate_hosted_project(ws_root_path, project_root)

    results: list[dict] = []
    for action in req.actions[:8]:
        command = str(action.command or "").strip()
        cwd_label = str(action.cwd or project_root or ".").strip().strip("/") or "."
        policy = _command_policy_decision(command)
        if not policy.ok:
            results.append({
                "command": command,
                "ok": False,
                "stdout": "",
                "stderr": policy.reason,
                "returncode": 126,
                "policy": policy.model_dump(),
                "synced_files": 0,
                "reason": action.reason,
            })
            continue

        try:
            cwd = safe_join(ws_root_path, cwd_label)
        except ValueError as exc:
            results.append({
                "command": command,
                "ok": False,
                "stdout": "",
                "stderr": str(exc),
                "returncode": 126,
                "policy": policy.model_dump(),
                "synced_files": 0,
                "reason": action.reason,
            })
            continue

        result = _run_shell_command(command, cwd)
        result["command"] = command
        result["policy"] = policy.model_dump()
        result["synced_files"] = _sync_hosted_project_text_files_after_shell(ws_root_path, cwd)
        result["reason"] = action.reason
        results.append(result)

    return {
        "ok": all(bool(item.get("ok")) for item in results),
        "project_root": project_root,
        "ran": len(results),
        "results": results,
    }


class WorkspaceSetReq(BaseModel):
    path: str


class IdentityUpdateReq(BaseModel):
    display_name: str | None = None
    email: str | None = None


class IdentityInfo(BaseModel):
    user_id: str
    display_name: str | None
    email: str | None
    has_profile: bool
    managed_workspace_mode: Literal["user", "session"]
    managed_workspace_path: str


class WorkspaceInfo(BaseModel):
    path: str | None
    default: str | None


class WorkspaceProvisionResp(BaseModel):
    ok: bool
    path: str
    created: bool
    managed: bool = True


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


@app.get("/api/identity", response_model=IdentityInfo)
def get_identity():
    return _identity_info()


@app.put("/api/identity", response_model=IdentityInfo)
def update_identity_profile(req: IdentityUpdateReq):
    _upsert_current_user_profile(display_name=req.display_name, email=req.email)
    return _identity_info()


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


@app.get("/api/workspace", response_model=WorkspaceInfo)
def get_workspace():
    # Public-friendly behavior: workspace is session-only.
    # Do not auto-restore from DEFAULT_WORKSPACE or any previously persisted choice.
    p: Path | None = _session_state()["workspace"]
    if p is None and has_supabase():
        p, _created = _provision_managed_workspace()
        _session_state()["workspace"] = p
    if p is not None:
        _hydrate_hosted_projects(p)
    return WorkspaceInfo(path=str(p) if p else None, default=settings_mod.settings.default_workspace)


@app.post("/api/workspace")
def set_workspace(req: WorkspaceSetReq):
    if _is_serverless_runtime():
        raise HTTPException(400, "Picking arbitrary host folders is disabled in hosted/serverless deployments.")
    p = Path(req.path).expanduser().resolve()
    if not p.exists() or not p.is_dir():
        raise HTTPException(400, "Workspace path must be an existing directory")
    # Public-friendly behavior: keep workspace selection in memory only.
    _session_state()["workspace"] = p
    return {"ok": True, "path": str(p)}


@app.post("/api/workspace/clear")
def clear_workspace():
    _session_state()["workspace"] = None
    return {"ok": True}


@app.post("/api/workspace/provision", response_model=WorkspaceProvisionResp)
def provision_workspace():
    session_dir, created = _provision_managed_workspace()
    _session_state()["workspace"] = session_dir
    return WorkspaceProvisionResp(ok=True, path=str(session_dir), created=created)


@app.post("/api/workspace/pick")
def pick_workspace():
    """Open a native directory picker on the machine running the API service and return the chosen path.

    This avoids the browser's webkitdirectory 'upload' UX and lets the user pick a real folder that
    becomes the workspace root for all agent reads/writes.
    """

    import shutil
    import subprocess

    if _is_serverless_runtime():
        raise HTTPException(400, "Native folder picking is only available in local desktop/dev mode.")

    # Prefer zenity on Linux desktops
    if shutil.which("zenity"):
        try:
            r = subprocess.run(
                ["zenity", "--file-selection", "--directory", "--title=Pick workspace folder"],
                check=False,
                capture_output=True,
                text=True,
            )
            if r.returncode == 0:
                p = (r.stdout or "").strip()
                if p:
                    return {"ok": True, "path": p}
            return {"ok": False, "path": None}
        except Exception:
            pass

    # Fallback: tkinter (works cross-platform if GUI available)
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        p = filedialog.askdirectory(title="Pick workspace folder")
        try:
            root.destroy()
        except Exception:
            pass

        if p:
            return {"ok": True, "path": p}
        return {"ok": False, "path": None}
    except Exception:
        return {"ok": False, "path": None}


@app.post("/api/workspace/import-browser-folder", response_model=WorkspaceProvisionResp)
async def import_browser_folder(files: list[UploadFile] = File(...), paths: list[str] = Form(...)):
    if not files:
        raise HTTPException(400, "No files uploaded")
    if len(files) != len(paths):
        raise HTTPException(400, "Uploaded files/path metadata mismatch")

    workspace_dir, _created = _provision_managed_workspace()

    root_name: str | None = None
    target_root: Path | None = None
    target_root_preexisting = False

    for upload, rel_raw in zip(files, paths):
        rel = PurePosixPath((rel_raw or "").strip())
        if not rel.parts:
            raise HTTPException(400, "Invalid uploaded path")
        if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
            raise HTTPException(400, "Unsafe uploaded path")

        if root_name is None:
            root_name = rel.parts[0]
            target_root = (workspace_dir / root_name).resolve()
            if workspace_dir != target_root and workspace_dir not in target_root.parents:
                raise HTTPException(400, "Invalid target import root")
            target_root_preexisting = target_root.exists()
        elif rel.parts[0] != root_name:
            raise HTTPException(400, "Please choose exactly one folder")

        assert target_root is not None
        inner_parts = rel.parts[1:] if len(rel.parts) > 1 else (upload.filename or rel.parts[-1],)
        dest = target_root.joinpath(*inner_parts).resolve()
        if target_root != dest and target_root not in dest.parents:
            raise HTTPException(400, "Unsafe destination path")
        dest.parent.mkdir(parents=True, exist_ok=True)
        content = await upload.read()
        dest.write_bytes(content)
        if has_supabase() and _is_text_rel_path(str(PurePosixPath(*inner_parts))):
            try:
                supabase_upsert_project_files(
                    owner_id=CURRENT_USER_ID.get(),
                    project_root=root_name,
                    files=[{"path": str(PurePosixPath(*inner_parts)), "content": content.decode("utf-8")}],
                )
            except Exception:
                pass

    if target_root is None:
        raise HTTPException(400, "No folder content received")

    _session_state()["workspace"] = target_root
    return WorkspaceProvisionResp(ok=True, path=str(target_root), created=not target_root_preexisting, managed=True)


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


app.include_router(build_auth_router(session_state=_session_state, sanitize_session_id=_sanitize_session_id, sanitize_user_id=sanitize_user_id, upsert_current_user_profile=_upsert_current_user_profile))
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
    _hydrate_hosted_projects(p)
    return p


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
    _hydrate_hosted_projects(base)
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
    preview_script = "dev"

    if not is_static:
        try:
            import json
            data = json.loads(pj_path.read_text(encoding="utf-8"))
            scripts = data.get("scripts") or {}
            if isinstance(scripts, dict) and "dev" in scripts:
                preview_script = "dev"
                is_static = False
            elif isinstance(scripts, dict) and "preview" in scripts:
                preview_script = "preview"
                is_static = False
            else:
                is_static = True
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
        install_cmd = _package_install_command(manager_name, manager_cmd)
        logs.append(f"$ {_shell_join(install_cmd)}")
        install = subprocess.run(install_cmd, cwd=str(proj), capture_output=True, text=True)
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
        cmd = _package_run_script_command(manager_cmd, preview_script, port)
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
    if req.path == ".":
        _hydrate_hosted_projects(root)
    else:
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
    root = _ws()
    checkpoint_path = safe_join(root, req.path)
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

    requested_mode = str(req.mode or "auto").strip().lower()
    if requested_mode not in {"auto", "html", "browser"}:
        requested_mode = "auto"

    if requested_mode != "html":
        browser_audit, browser_warning = _run_playwright_preview_audit(
            preview_url,
            project_dir,
            max_excerpt_chars=max_excerpt_chars,
            project_signals=project_signals,
        )
        if browser_audit:
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
        warning = "Tabel public.agent_memory_chunks belum ada. Jalankan docs/supabase-agent-rag.sql di Supabase SQL editor dulu."
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
        "bootstrap_sql_path": "docs/supabase-agent-rag.sql",
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
        "bootstrap_sql_path": "docs/supabase-agent-rag.sql",
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
    stream: bool = False
    background: bool = False
    auto_execute: bool = False


class AgentWorkerRunReq(BaseModel):
    job_id: str | None = None
    limit: int = 1


class ImageAssetResp(BaseModel):
    ok: bool
    path: str
    name: str
    content_type: str | None = None
    size: int


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
    browser_audit_ready = bool(project_dir.exists() and _browser_preview_audit_ready(project_dir))
    supabase_enabled = has_supabase()
    friendly_free_tier = bool(getattr(settings_mod.settings, "friendly_free_tier_mode", True))
    context_budget = int(getattr(settings_mod.settings, "agent_context_char_budget", 48_000 if friendly_free_tier else 140_000) or 48_000)
    supabase_rag_status = get_agent_memory_chunks_table_status() if supabase_enabled else "unconfigured"
    supabase_rag_ready = supabase_rag_status == "ready"
    memory_backend = "supabase-hash-vector-chunks" if supabase_rag_ready else "local-hash-vector-chunks"
    supabase_warning = None
    if supabase_rag_status == "missing":
        supabase_warning = "Supabase udah dikonfigurasi, tapi tabel public.agent_memory_chunks belum dibuat. Jalankan docs/supabase-agent-rag.sql dulu."
    elif supabase_rag_status == "error":
        supabase_warning = "Supabase RAG belum bisa diverifikasi dari backend ini, jadi retrieval masih fallback ke chunk lokal."
    return {
        "ok": True,
        "runtime": "langgraph",
        "glossary": {
            "tools": "Tools are callable interfaces the agent is allowed to invoke to do work that cannot be reliably done with generative text alone (e.g., read/search repo, call external systems).",
            "mcp": "MCP (Model Context Protocol) is an interoperability layer to standardize how the agent connects to external data sources and tools. MCP servers expose tools, but MCP itself is not a tool.",
            "skills": "Skills are higher-level workflow abstractions: curated instructions + prompting + decision logic + (optionally) one or more tools/MCP calls, to keep complex agentic work consistent, auditable, and scoped.",
        },
        "personas": {
            "clara": {
                "build_mode": "full-agent",
                "name": "Clara",
                "vibe": "full preview product builder",
                "default_scope": "broad, end-to-end delivery with the browser preview as the main surface",
            },
            "raka": {
                "build_mode": "hybrid",
                "name": "Raka",
                "vibe": "live coding copilot",
                "default_scope": "surgical edits near the active file, preserve user architecture",
            },
        },
        "supports": {
            "graph_runtime": True,
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
            "playwright_preview_audit": browser_audit_ready,
            "webcontainer_runtime": False,
            "browser_dom_audit": browser_audit_ready,
            "preview_quality_checks": True,
            "preview_audit_mode": "browser" if browser_audit_ready else "html",
            "tool_actions": ["shell", "mcp", "tool"],
            "streaming_transport": True,
            "native_provider_token_streaming": True,
            "friendly_free_tier_mode": friendly_free_tier,
            "context_budget_chars": context_budget,
            "provider_fallback_routing": True,
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
            "preview_audit_mode": "browser" if browser_audit_ready else "html",
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


def _sanitize_uploaded_filename(name: str) -> str:
    stem = Path(name or "image").stem or "image"
    stem = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in stem).strip("-") or "image"
    suffix = Path(name or "").suffix.lower()
    allowed = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
    if suffix not in allowed:
        suffix = ".png"
    return f"{stem[:50]}{suffix}"


@app.post("/api/assets/image", response_model=ImageAssetResp)
async def upload_image_asset(project_root: str = Form("."), file: UploadFile = File(...)):
    ws_root = _ws()
    proj_root = (project_root or ".").strip() or "."
    _hydrate_hosted_project(ws_root, proj_root)
    project_dir = safe_join(ws_root, proj_root)
    if not project_dir.exists() or not project_dir.is_dir():
        raise HTTPException(400, "project_root must exist inside workspace")

    content_type = (file.content_type or "").strip().lower()
    if not content_type.startswith("image/"):
        raise HTTPException(400, "Only image uploads are supported")

    data = await file.read()
    if not data:
        raise HTTPException(400, "Uploaded image is empty")
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(400, "Image too large (max 10 MB)")

    filename = _sanitize_uploaded_filename(file.filename or "image")
    target_dir = safe_join(project_dir, "public/uploads")
    target_dir.mkdir(parents=True, exist_ok=True)

    candidate = target_dir / filename
    if candidate.exists():
        candidate = target_dir / f"{candidate.stem}-{int(time.time())}{candidate.suffix}"
    candidate.write_bytes(data)

    rel = str(candidate.relative_to(ws_root))
    return ImageAssetResp(ok=True, path=rel, name=candidate.name, content_type=content_type or None, size=len(data))


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


def _execution_needs_repair(execution: dict[str, object]) -> bool:
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


def _failed_command_signatures(container: object, *, kind: str) -> list[dict[str, object]]:
    if not isinstance(container, dict):
        return []
    signatures: list[dict[str, object]] = []
    for result in list(container.get("results") or []):
        if not isinstance(result, dict) or result.get("ok") is not False:
            continue
        command = str(result.get("command") or "").strip()
        stderr = result.get("stderr")
        stdout = result.get("stdout")
        marker = _failure_text_marker(stderr) or _failure_text_marker(stdout) or f"returncode-{result.get('returncode')}"
        excerpt = _failure_evidence_excerpt(stderr, stdout)
        signatures.append({
            "kind": kind,
            "command": command[:180],
            "returncode": result.get("returncode"),
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

    if kind == "validation":
        primary = f"validation failed: {marker}"
        if command:
            primary = f"{primary} in `{command}`"
        next_move = "Read the failing validation output, edit the source that causes it, then rerun the same validation command."
    elif kind == "shell":
        primary = f"shell action failed: {marker}"
        if command:
            primary = f"{primary} in `{command}`"
        next_move = "Fix the command precondition or project files before rerunning the shell action."
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

    signatures.extend(_failed_command_signatures(execution.get("shell"), kind="shell"))
    signatures.extend(_failed_command_signatures(execution.get("validation"), kind="validation"))
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
        "failures": signatures[:12],
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


def _criterion(label: str, status: str, detail: str) -> dict[str, str]:
    return {
        "label": label,
        "status": status,
        "detail": str(detail or "")[:500],
    }


def _execution_completion_report(execution: dict[str, object]) -> dict[str, object]:
    criteria: list[dict[str, str]] = []
    residual_risks: list[str] = []
    ok = bool(execution.get("ok"))

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
            if warnings and preview_audit.get("ok"):
                residual_risks.append(f"Preview audit still has {warnings} warning(s).")
    else:
        criteria.append(_criterion("preview", "skipped", "No preview surface or preview URL was available for this run."))

    repairs = execution.get("repairs")
    repaired_success = False
    if isinstance(repairs, list) and repairs:
        last_execution = repairs[-1].get("execution") if isinstance(repairs[-1], dict) else None
        repaired_success = bool(isinstance(last_execution, dict) and last_execution.get("ok"))
        criteria.append(_criterion(
            "repair-loop",
            "passed" if repaired_success else "failed",
            f"attempts={len(repairs)}",
        ))
    else:
        criteria.append(_criterion("repair-loop", "skipped", "No backend repair pass was needed."))

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
    completion_state = "complete" if ok else "blocked"
    if completion_state == "complete":
        summary = "Complete: backend execution criteria passed or were intentionally skipped."
    else:
        summary = f"Blocked: {', '.join(failed_labels) or 'execution'} still failing."

    return {
        "ok": ok,
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
            "visual_summary": preview_audit.get("visual_summary"),
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
            "previous_repairs": repair_summaries,
            "last_repair_execution": execution.get("last_repair_execution"),
        },
        ensure_ascii=False,
        indent=2,
    )
    return report[:max_chars]


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
        policy = _command_policy_decision(command)
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
    emit("status", {"phase": "executing_replay", "message": "Backend harness replaying previously failing shell commands..."})
    replay = agent_harness_run_shell(AgentHarnessRunShellReq(project_root=project_root, actions=replay_actions))
    if skipped_commands:
        replay["skipped_commands"] = skipped_commands
    steps = replay.setdefault("steps", [])
    if isinstance(steps, list):
        steps.append(_execution_step(
            "replay",
            "Backend repair replay",
            bool(replay.get("ok")),
            f"ran={replay.get('ran')}",
            commands=[action.command for action in replay_actions],
            failed=sum(1 for item in list(replay.get("results") or []) if isinstance(item, dict) and not item.get("ok")),
        ))
    emit(
        "tool_output",
        {
            "kind": "agent_harness",
            "tool": "repair-replay",
            "ok": bool(replay.get("ok")),
            "phase": "executing_replay",
            "text": json.dumps({"ran": replay.get("ran"), "results": replay.get("results")}, ensure_ascii=False)[:1200],
        },
    )
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
    frontend_suffixes = {".html", ".tsx", ".jsx", ".ts", ".js", ".css", ".scss", ".sass", ".vue", ".svelte"}
    for change in out_changes:
        if not isinstance(change, dict):
            continue
        rel = str(change.get("path") or "").strip()
        suffix = PurePosixPath(rel).suffix.lower()
        if suffix in frontend_suffixes:
            return True
    return False


def _run_backend_repair_pass(req: AgentReq, execution: dict[str, object], emit, *, repair_index: int) -> dict[str, object]:
    project_root = str(req.project_root or ".").strip().strip("/") or "."
    failure_analysis = _execution_failure_analysis(execution)
    emit("status", {"phase": "executing_repair", "message": f"Backend harness running repair pass {repair_index} from execution evidence..."})
    repair_prompt = "\n\n".join([
        f"BACKEND AUTO-EXECUTE REPAIR PASS {repair_index}:",
        "The previous backend execution produced failing apply/shell/validation evidence.",
        "Repair the project now with concrete file changes and only safe project-scoped shell actions if needed.",
        "If earlier repair passes failed, use their evidence and choose a different concrete fix.",
        "Use the failure_analysis signature to detect repeated failures. If repeated_failure is true, change strategy instead of making the same local edit again.",
        "Use failure_analysis.summary and failure_analysis.suggested_next_move as the repair objective.",
        "Do not repeat the same failing command blindly unless your changes address the failure.",
        f"Original user request:\n{req.input}",
        f"Failure analysis:\n{json.dumps(failure_analysis, ensure_ascii=False, indent=2)}",
        f"Current file context after failed execution:\n{_repair_file_context(project_root, execution)}",
        f"Repair replay plan:\n{_repair_replay_plan(execution)}",
        f"Execution evidence:\n{_execution_repair_report(execution)}",
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
        stream=False,
        background=False,
        auto_execute=False,
    )
    with _agent_lock_for_current_provider():
        repair_pipeline = run_agent_pipeline(repair_req, ws_root=_ws(), emit=emit)
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

    if out_changes:
        emit("status", {"phase": "executing_apply", "message": "Backend harness applying agent changes..."})
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
        emit(
            "tool_output",
            {
                "kind": "agent_harness",
                "tool": "apply",
                "ok": bool(apply_result.get("ok")),
                "phase": "executing_apply",
                "text": json.dumps({key: apply_result.get(key) for key in ("applied", "count", "paths", "checkpoint_path", "conflicts", "warnings")}, ensure_ascii=False)[:1200],
            },
        )

    shell_actions = _agent_shell_actions(actions)
    if shell_actions:
        emit("status", {"phase": "executing_shell", "message": "Backend harness running agent shell actions..."})
        shell_result = agent_harness_run_shell(AgentHarnessRunShellReq(project_root=project_root, actions=shell_actions))
        execution["shell"] = shell_result
        execution["ok"] = bool(execution["ok"]) and bool(shell_result.get("ok"))
        steps = execution.setdefault("steps", [])
        if isinstance(steps, list):
            steps.append(_execution_step(
                "shell",
                "Backend shell harness",
                bool(shell_result.get("ok")),
                f"ran={shell_result.get('ran')}",
                commands=[action.command for action in shell_actions],
                failed=sum(
                    1
                    for item in list(shell_result.get("results") or [])
                    if isinstance(item, dict) and not item.get("ok")
                ),
            ))
        emit(
            "tool_output",
            {
                "kind": "agent_harness",
                "tool": "run-shell",
                "ok": bool(shell_result.get("ok")),
                "phase": "executing_shell",
                "text": json.dumps({"ran": shell_result.get("ran"), "results": shell_result.get("results")}, ensure_ascii=False)[:1200],
            },
        )

    if out_changes:
        try:
            project_dir = safe_join(_ws(), project_root)
            validation_commands = _infer_validation_commands(project_dir)[:4]
        except Exception:
            validation_commands = []
        if validation_commands:
            emit("status", {"phase": "executing_validation", "message": "Backend harness validating project output..."})
            validation_shell = agent_harness_run_shell(
                AgentHarnessRunShellReq(
                    project_root=project_root,
                    actions=[
                        AgentHarnessShellAction(command=command, cwd=project_root, reason="Backend auto validation")
                        for command in validation_commands
                    ],
                )
            )
            validation_results = list(validation_shell.get("results") or [])
            validation = {
                "ok": all(bool(item.get("ok")) for item in validation_results if isinstance(item, dict)) if validation_results else True,
                "project_root": project_root,
                "commands": validation_commands,
                "results": validation_results,
                "ran": len(validation_results),
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
            emit(
                "tool_output",
                {
                    "kind": "agent_harness",
                    "tool": "validate",
                    "ok": bool(validation.get("ok")),
                    "phase": "executing_validation",
                    "text": json.dumps({key: validation.get(key) for key in ("ok", "commands", "ran", "passed", "failed")}, ensure_ascii=False)[:1200],
                },
            )

    try:
        preview_project_dir = safe_join(_ws(), project_root)
    except Exception:
        preview_project_dir = _ws()
    if out_changes and (str(getattr(req, "preview_url", None) or "").strip() or _project_has_preview_surface(preview_project_dir, out_changes)):
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
                    repair_brief=preview_result.get("repair_brief"),
                ))
            emit(
                "tool_output",
                {
                    "kind": "agent_harness",
                    "tool": "preview-audit",
                    "ok": bool(preview_result.get("ok")) or bool(preview_result.get("skipped")),
                    "phase": "executing_preview_audit",
                    "text": json.dumps({
                        "ok": preview_result.get("ok"),
                        "skipped": preview_result.get("skipped"),
                        "audit_mode": preview_result.get("audit_mode"),
                        "summary": preview_result.get("summary"),
                    }, ensure_ascii=False)[:1200],
                },
            )

    execution["failure_analysis"] = _execution_failure_analysis(execution)
    if allow_repair:
        for repair_index in range(1, max_repair_passes + 1):
            if not _execution_needs_repair(execution) or bool(execution.get("ok")):
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
            if isinstance(repair_execution, dict):
                execution["ok"] = bool(repair_execution.get("ok"))
                execution["failure_analysis"] = _execution_failure_analysis(execution)
            if bool(execution.get("ok")):
                break

    completion_report = _execution_completion_report(execution)
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

    return execution


def _trace_has_blocking_verifier_failures(trace: dict) -> bool:
    checks = trace.get("verification") if isinstance(trace, dict) else None
    if not isinstance(checks, list):
        return False
    for check in checks:
        if not isinstance(check, dict):
            continue
        if check.get("ok") is False and str(check.get("name") or "") != "full-agent-coverage":
            return True
    return False


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

    def emit(event: str, data: dict):
        nonlocal streamed_spoken
        payload = dict(data or {})
        if event == "delta" and payload.get("spoken_chunk"):
            streamed_spoken = True
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
        if req.auto_execute and (out_changes or normalized_actions) and not _trace_has_blocking_verifier_failures(result["trace"]):
            result["execution"] = _auto_execute_agent_result(req, out_changes, normalized_actions, emit)
            _remember_backend_execution_state(req, result)
        elif req.auto_execute and _trace_has_blocking_verifier_failures(result["trace"]):
            result["execution"] = {
                "auto_execute": True,
                "ok": False,
                "skipped": True,
                "reason": "Verifier reported blocking failures before backend execution.",
                "project_root": str(req.project_root or ".").strip().strip("/") or ".",
                "apply": None,
                "shell": None,
            }
        if not streamed_spoken:
            for chunk in _spoken_stream_chunks(sug_spoken):
                emit("delta", {"spoken_chunk": chunk})
        _update_agent_job_record(job_id, "completed", result=result)
        emit("done", {"message": "Beres, hasil agent siap dipakai.", "result": result})
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
