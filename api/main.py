from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Literal
import hashlib
import os
import shutil
import threading
import time
import json
import re
import shlex
import subprocess
import uuid
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
from api.auth_identity import resolve_request_user, sanitize_user_id
from api.oauth_runtime import CURRENT_PROFILE_ID
from api.projects_router import build_projects_router
from api.preferences_router import build_preferences_router
from api.settings_router import build_settings_router
from api.fs import list_tree, read_text, write_text, diff_text, safe_join
from api.agent_mcp import discover_mcp_servers, list_mcp_tools
from api.agent_memory import get_agent_memory_overview, sync_project_docs_to_supabase
from api.agent_runtime import run_agent_pipeline
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
    "/api/project/validate",
    "/api/preview/audit",
    "/api/supabase/rag",
)


def _requires_verified_hosted_user(path: str) -> bool:
    if not has_supabase():
        return False
    if path in {"/api/healthz", "/api/settings", "/api/models", "/api/agent/worker/run"}:
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


@app.middleware("http")
async def bind_voiceide_session(request: Request, call_next):
    session_token = CURRENT_SESSION_ID.set(_sanitize_session_id(request.headers.get("X-Appora-Session") or request.headers.get("X-VoiceIDE-Session")))
    resolved_user = resolve_request_user(
        authorization=request.headers.get("Authorization"),
        x_voiceide_user=request.headers.get("X-Appora-User") or request.headers.get("X-VoiceIDE-User"),
    )
    user_token = CURRENT_USER_ID.set(resolved_user.user_id)
    profile_token = CURRENT_PROFILE_ID.set(resolved_user.user_id)
    try:
        if _requires_verified_hosted_user(request.url.path) and resolved_user.auth_source != "supabase":
            return JSONResponse(
                status_code=401,
                content={
                    "detail": (
                        "Hosted agent/workspace routes require verified login. "
                        "Sign in so the frontend can send a Supabase bearer token."
                    )
                },
            )
        session = _session_state()
        google_user = session.get("google_user") or {}
        if resolved_user.auth_source != "supabase":
            sub = str(google_user.get("sub") or "").strip()
            email = str(google_user.get("email") or "").strip().lower()
            if sub:
                CURRENT_USER_ID.set(sanitize_user_id(f"google-{sub}"))
            elif email:
                CURRENT_USER_ID.set(sanitize_user_id(f"google-{email}"))
        response = await call_next(request)
        response.headers["X-Appora-Auth-Source"] = resolved_user.auth_source
        return response
    finally:
        CURRENT_PROFILE_ID.reset(profile_token)
        CURRENT_USER_ID.reset(user_token)
        CURRENT_SESSION_ID.reset(session_token)


@app.get("/api/healthz")
def healthz():
    return {
        "ok": True,
        "service": "appora-api",
        "session": CURRENT_SESSION_ID.get(),
        "user": CURRENT_USER_ID.get(),
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

    issues: list[str] = []
    if not title:
        issues.append("Preview page is missing a <title> tag.")
    if not meta_description:
        issues.append("Preview page is missing a meta description.")
    if not headings:
        issues.append("Preview page has no visible H1 heading.")
    if word_count < 40:
        issues.append("Preview content is very sparse, which usually means the page feels unfinished.")
    if not buttons and form_count == 0 and len(links) < 2:
        issues.append("Preview has very little obvious interaction or navigation.")
    if images_missing_alt > 0:
        issues.append(f"Preview has {images_missing_alt} image(s) without useful alt text.")
    if broken_images:
        issues.append(f"Preview has {len(broken_images)} broken image(s): {', '.join(broken_images[:3])}.")
    if unlabeled_interactive:
        issues.append(f"Preview has {len(unlabeled_interactive)} unlabeled interactive element(s).")
    if mobile_text_overflow_nodes:
        issues.append(f"Preview has {len(mobile_text_overflow_nodes)} mobile text overflow issue(s).")
    if fixed_overlays or mobile_fixed_overlays:
        issues.append("Preview has large fixed overlay(s) that may block interaction.")
    if page_errors:
        issues.append(f"Preview threw {len(page_errors)} runtime browser error(s).")
    if console_errors:
        issues.append(f"Preview logged {len(console_errors)} browser console warning/error message(s).")
    if quality_failures:
        issues.append(f"Preview quality checks flagged {len(quality_failures)} area(s) across responsive/a11y/state readiness.")

    summary_parts = [
        f"mode={audit_mode}",
        f"title={title or '(missing)'}",
        f"h1={headings[0] if headings else '(missing)'}",
        f"buttons={len(buttons)}",
        f"links={len(links)}",
        f"forms={form_count}",
        f"interactive={interactive_count}",
        f"words={word_count}",
        f"quality_failures={len(quality_failures)}",
    ]

    return {
        "ok": len(issues) == 0,
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
        "viewport": snapshot.get("viewport") if isinstance(snapshot.get("viewport"), dict) else {},
        "mobile_viewport": snapshot.get("mobile_viewport") if isinstance(snapshot.get("mobile_viewport"), dict) else {},
        "console_errors": console_errors,
        "page_errors": page_errors,
        "runtime_warnings": runtime_warnings or [],
        "issues": issues,
        "quality_checks": quality_checks,
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


@app.post("/api/terminal/run")
def terminal_run(req: TerminalRunReq):
    ws_root = _session_state().get("workspace")
    if not ws_root:
        raise HTTPException(400, "No workspace selected")

    cwd = ws_root
    if req.cwd:
        cwd = safe_join(ws_root, req.cwd)

    try:
        result = _run_shell_command(req.command, cwd)
        result["synced_files"] = _sync_hosted_project_text_files_after_shell(ws_root, cwd)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


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
    p: Path | None = _session_state()["workspace"]
    if p is None:
        try:
            p, _created = _provision_managed_workspace()
            _session_state()["workspace"] = p
        except Exception:
            p = None
    if p is None:
        raise HTTPException(400, "Workspace not set")
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

    # Check if this is a static project (no package.json or no dev script)
    pj_path = proj / "package.json"
    is_static = not pj_path.exists()

    if not is_static:
        try:
            import json
            data = json.loads(pj_path.read_text(encoding="utf-8"))
            scripts = data.get("scripts") or {}
            is_static = "dev" not in scripts
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
        install_cmd = [*manager_cmd, "install"]
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
        cmd = [*manager_cmd, "run", "dev", "--", "--host", "127.0.0.1", "--strictPort", "--port", str(port)]
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


@app.post("/api/fs/apply_many")
def fs_apply_many(req: ApplyManyReq):
    root = _ws()
    conflicts: list[str] = []

    # preflight
    for op in req.ops:
        p = safe_join(root, op.path)
        if op.expected_exists is not None:
            exists = p.exists()
            if op.expected_exists is False and exists:
                conflicts.append(f"{op.path} changed: expected file to be absent")
                continue
            if op.expected_exists is True and not exists:
                conflicts.append(f"{op.path} changed: expected file to exist")
                continue
            if exists and op.expected_sha256:
                try:
                    current_hash = _sha256_text(p.read_text(encoding="utf-8"))
                except UnicodeDecodeError:
                    conflicts.append(f"{op.path} changed: current file is not UTF-8 text")
                    continue
                if current_hash != op.expected_sha256:
                    conflicts.append(f"{op.path} changed since agent prepared the patch")
                    continue
        elif p.exists() and not req.overwrite:
            conflicts.append(op.path)

    if conflicts:
        raise HTTPException(409, f"Conflicts (already exist): {', '.join(conflicts[:20])}")

    for op in req.ops:
        write_text(root, op.path, op.content)
        _persist_hosted_file(op.path, op.content)

    return {"ok": True, "count": len(req.ops)}


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
                "vibe": "autonomous product builder",
                "default_scope": "broad, end-to-end delivery (preview-ready)",
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
            "native_provider_token_streaming": False,
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
        job = get_agent_job_any(job_id=job_id)
        if not job:
            local = _local_agent_jobs().get(job_id)
            if isinstance(local, dict):
                job = local
        if not job:
            raise HTTPException(404, "agent job not found")
        jobs = [job]
    else:
        remote_jobs = list_agent_jobs_by_status(status="queued", limit=limit)
        if remote_jobs is not None:
            jobs = remote_jobs
        else:
            jobs = [
                job for job in _local_agent_jobs().values()
                if isinstance(job, dict) and str(job.get("status") or "") == "queued"
            ][: max(1, min(int(limit or 1), 5))]

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


@app.post("/api/agent/worker/run")
def agent_worker_run(req: AgentWorkerRunReq, request: Request):
    _require_worker_auth(request)
    return _run_agent_worker_jobs(job_id=req.job_id, limit=req.limit)


@app.get("/api/agent/worker/run")
def agent_worker_run_get(request: Request, job_id: str | None = None, limit: int = 1):
    _require_worker_auth(request)
    return _run_agent_worker_jobs(job_id=job_id, limit=limit)


def _run_agent_impl(req: AgentReq, event_cb=None, job_id: str | None = None):
    def emit(event: str, data: dict):
        payload = dict(data or {})
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
        out_changes: list[dict[str, str]] = []
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
