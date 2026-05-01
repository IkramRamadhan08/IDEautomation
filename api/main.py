from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Literal
import threading
import time
import json
import re
import shlex
import subprocess
from html import unescape
from urllib.request import Request as URLRequest, urlopen

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from api.settings import ROOT, ENV_PATH, load_settings
from api.supabase_store import upsert_profile
from api import settings as settings_mod
from api.app_state import CURRENT_SESSION_ID, CURRENT_USER_ID, STATE
from api.auth_router import build_auth_router
from api.auth_identity import resolve_request_user, sanitize_user_id
from api.projects_router import build_projects_router
from api.preferences_router import build_preferences_router
from api.settings_router import build_settings_router
from api.fs import list_tree, read_text, write_text, diff_text, safe_join
from api.agent_modes import get_agent_mode_spec


app = FastAPI(title="Voice IDE Backend", version="0.1.0")

# Serialize LLM calls to avoid provider rate-limit bursts (429).
SCAFFOLD_LOCK = threading.Lock()
AGENT_LOCK = threading.Lock()


def _reload_settings():
    settings_mod.settings = load_settings()


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


_RELATIVE_IMPORT_RE = re.compile(r'(?:import\s+(?:[^\"\']+?\s+from\s+)?|export\s+[^\"\']*?\s+from\s+|import\()\s*["\']([^"\']+)["\']')
_FRONTEND_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".css", ".scss", ".sass", ".less", ".html"}


def _localize_project_rel(rel_path: str | None, project_root: str) -> str:
    rel = str(rel_path or "").strip().lstrip("/")
    if rel and project_root != "." and rel.startswith(project_root + "/"):
        rel = rel[len(project_root) + 1 :]
    return rel


def _resolve_related_files(active_rel: str, content: str, file_candidates: set[str]) -> list[str]:
    if not active_rel or not content:
        return []

    active_path = PurePosixPath(active_rel)
    base_dir = active_path.parent
    resolved: list[str] = []
    seen: set[str] = set()
    candidate_exts = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".css", ".scss", ".sass", ".less"]

    def add_candidate(rel: str) -> None:
        rel = str(PurePosixPath(rel)).lstrip("/")
        if rel in file_candidates and rel not in seen:
            seen.add(rel)
            resolved.append(rel)

    for spec in _RELATIVE_IMPORT_RE.findall(content):
        if not spec.startswith('.'):
            continue
        joined = PurePosixPath(base_dir, spec)
        if joined.suffix:
            add_candidate(str(joined))
            continue
        joined_str = str(joined)
        for ext in candidate_exts:
            add_candidate(joined_str + ext)
            add_candidate(f"{joined_str}/index{ext}")

    base_name = str(active_path.with_suffix(''))
    for suffix in [".css", ".scss", ".sass", ".less", ".module.css", ".module.scss"]:
        add_candidate(f"{base_name}{suffix}")

    if active_path.parent != PurePosixPath('.'):
        for name in ("index.ts", "index.tsx", "index.js", "index.jsx"):
            add_candidate(str(active_path.parent / name))

    return resolved


def _should_run_refinement(*, build_mode: str, instruction: str, active_rel: str, preview_url: str | None, attached_assets: list[str]) -> bool:
    if build_mode == "full-agent":
        return True
    if preview_url:
        return True
    if attached_assets:
        return True

    hint = (instruction or "").lower()
    refine_keywords = (
        "polish", "refine", "audit", "review", "production", "ux", "ui", "layout", "spacing",
        "responsive", "design", "landing", "dashboard", "improve", "better", "fix", "bug",
        "preview", "hero", "copy", "theme", "style", "visual", "state",
    )
    if any(word in hint for word in refine_keywords):
        return True

    return PurePosixPath(active_rel or "").suffix in _FRONTEND_EXTS


def _merge_change_sets(*batches: list[dict[str, str]]) -> list[dict[str, str]]:
    order: list[str] = []
    merged: dict[str, str] = {}
    for batch in batches:
        for item in batch or []:
            if not isinstance(item, dict):
                continue
            rel = str(item.get("path") or "").strip().lstrip("/")
            new_content = item.get("new_content")
            if not rel or not isinstance(new_content, str):
                continue
            if rel not in merged:
                order.append(rel)
            merged[rel] = new_content
    return [{"path": rel, "new_content": merged[rel]} for rel in order]


def _merge_action_sets(*batches: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for batch in batches:
        for item in batch or []:
            if not isinstance(item, dict):
                continue
            try:
                key = json.dumps(item, ensure_ascii=False, sort_keys=True)
            except Exception:
                key = str(item)
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
    return out

# Local app: allow frontend dev server
app.add_middleware(
    CORSMiddleware,
    # Vite dev server ports can shift if one is already in use.
    # Keep this permissive for local dev.
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:5175",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
        "http://127.0.0.1:5175",
        "http://localhost:8788",
    ],
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?$",
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
            "oauth_pending": {},
            "google_user": None,
        }
    return sessions[sid]


def _effective_user_id(fallback_raw: str | None = None) -> str:
    google_user = _session_state().get("google_user") or {}
    sub = str(google_user.get("sub") or "").strip()
    email = str(google_user.get("email") or "").strip().lower()
    if sub:
        return sanitize_user_id(f"google-{sub}")
    if email:
        return sanitize_user_id(f"google-{email}")

    explicit = sanitize_user_id(fallback_raw)
    return explicit


@app.middleware("http")
async def bind_voiceide_session(request: Request, call_next):
    session_token = CURRENT_SESSION_ID.set(_sanitize_session_id(request.headers.get("X-VoiceIDE-Session")))
    resolved_user = resolve_request_user(
        authorization=request.headers.get("Authorization"),
        x_voiceide_user=request.headers.get("X-VoiceIDE-User"),
    )
    user_token = CURRENT_USER_ID.set(resolved_user.user_id)
    try:
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
        response.headers["X-VoiceIDE-Auth-Source"] = resolved_user.auth_source
        return response
    finally:
        CURRENT_USER_ID.reset(user_token)
        CURRENT_SESSION_ID.reset(session_token)


@app.get("/api/healthz")
def healthz():
    return {
        "ok": True,
        "service": "voice-ide-api",
        "session": CURRENT_SESSION_ID.get(),
        "user": CURRENT_USER_ID.get(),
    }


DANGEROUS_COMMAND_FRAGMENTS = ["rm -rf /", "mkfs", "dd if="]
VALIDATION_SCRIPT_NAMES = ("lint", "build", "typecheck", "check")
PREVIEW_AUDIT_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _normalize_preview_url(preview_url: str) -> str:
    parsed = urlsplit((preview_url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(400, "preview_url must be http(s)")
    if (parsed.hostname or "").lower() not in PREVIEW_AUDIT_HOSTS:
        raise HTTPException(400, "preview audit only supports localhost preview URLs")
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


def _fetch_preview_html(preview_url: str, attempts: int = 3) -> str:
    last_error: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            req = URLRequest(
                preview_url,
                headers={
                    "User-Agent": "VoiceIDE/0.1 (+local-preview-audit)",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
                method="GET",
            )
            with urlopen(req, timeout=8) as resp:  # nosec B310 - internal localhost preview fetch
                raw = resp.read(250_000)
            return raw.decode("utf-8", errors="ignore")
        except Exception as exc:
            last_error = exc
            if attempt < max(1, attempts) - 1:
                time.sleep(0.8)
    raise HTTPException(502, f"Preview audit fetch failed: {last_error}")


def _audit_preview_html(preview_url: str, html: str, max_excerpt_chars: int = 800) -> dict:
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

    form_count = len(re.findall(r"<form\b", html, flags=re.IGNORECASE))
    input_count = len(re.findall(r"<(input|textarea|select)\b", html, flags=re.IGNORECASE))
    word_count = len(re.findall(r"\b\w+\b", body_text))

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

    summary_parts = [
        f"title={title or '(missing)'}",
        f"h1={headings[0] if headings else '(missing)'}",
        f"buttons={len(buttons)}",
        f"links={len(links)}",
        f"forms={form_count}",
        f"words={word_count}",
    ]

    return {
        "ok": len(issues) == 0,
        "preview_url": preview_url,
        "title": title,
        "meta_description": meta_description,
        "headings": headings,
        "subheadings": subheadings,
        "buttons": buttons,
        "links": links,
        "form_count": form_count,
        "input_count": input_count,
        "word_count": word_count,
        "image_count": len(image_tags),
        "images_missing_alt": images_missing_alt,
        "issues": issues,
        "excerpt": excerpt,
        "summary": "; ".join(summary_parts),
    }


def _run_shell_command(command: str, cwd: Path, timeout: int = 120) -> dict:
    if any(fragment in command for fragment in DANGEROUS_COMMAND_FRAGMENTS):
        raise HTTPException(403, "Command blocked for safety")

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
            "stdout": proc.stdout,
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

    package_json = project_dir / "package.json"
    if package_json.exists():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
            scripts = data.get("scripts") or {}
            if isinstance(scripts, dict):
                for name in VALIDATION_SCRIPT_NAMES:
                    if isinstance(scripts.get(name), str) and scripts.get(name, "").strip():
                        commands.append(f"npm run {name}")
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
        return _run_shell_command(req.command, cwd)
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
    base_raw = settings_mod.settings.default_workspace or "~/.voiceide-home"
    return Path(base_raw).expanduser().resolve()


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
        raise HTTPException(400, "Invalid managed workspace root")
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

    readme = target_dir / "README.md"
    if not readme.exists():
        readme.write_text(
            "# Voice IDE Workspace\n\n"
            f"This managed workspace was provisioned automatically for the current {mode}.\n"
            "You can build apps here, and later replace it with a stronger authenticated runtime model.\n",
            encoding="utf-8",
        )

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
    return WorkspaceInfo(path=str(p) if p else None, default=settings_mod.settings.default_workspace)


@app.post("/api/workspace")
def set_workspace(req: WorkspaceSetReq):
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

    if target_root is None:
        raise HTTPException(400, "No folder content received")

    _session_state()["workspace"] = target_root
    return WorkspaceProvisionResp(ok=True, path=str(target_root), created=not target_root_preexisting, managed=True)


# Settings endpoints
def _env_set(key: str, value: str) -> None:
    import subprocess
    import sys

    script = ROOT / "scripts" / "env.py"
    if not script.exists():
        raise RuntimeError(f"Missing env helper: {script}")

    subprocess.run(
        [sys.executable, str(script), "set", key, value],
        cwd=str(ROOT),
        check=True,
        capture_output=True,
        text=True,
    )


def _env_unset(key: str) -> None:
    import subprocess
    import sys

    script = ROOT / "scripts" / "env.py"
    if not script.exists():
        raise RuntimeError(f"Missing env helper: {script}")

    subprocess.run(
        [sys.executable, str(script), "unset", key],
        cwd=str(ROOT),
        check=True,
        capture_output=True,
        text=True,
    )


app.include_router(build_auth_router(session_state=_session_state, sanitize_session_id=_sanitize_session_id, sanitize_user_id=sanitize_user_id, upsert_current_user_profile=_upsert_current_user_profile))
app.include_router(build_projects_router(session_state=_session_state))
app.include_router(build_preferences_router())
app.include_router(build_settings_router(session_state=_session_state, env_set=_env_set, env_unset=_env_unset, reload_settings=_reload_settings))


def _ws() -> Path:
    p: Path | None = _session_state()["workspace"]
    if p is None:
        raise HTTPException(400, "Workspace not set")
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
def run_start(req: RunStartReq):
    import subprocess
    import threading
    import time
    import uuid
    import sys

    base = _ws()
    proj = safe_join(base, req.project_root)
    if not proj.exists() or not proj.is_dir():
        raise HTTPException(400, "project_root must exist inside workspace")

    _ensure_runner_capacity(req.project_root)

    port = req.port or _next_port()
    rid = uuid.uuid4().hex[:8]
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
        # npm project with dev script
        logs.append("$ npm install")
        install = subprocess.run(["npm", "install"], cwd=str(proj), capture_output=True, text=True)
        if install.stdout:
            logs.extend([l for l in install.stdout.splitlines() if l.strip()])
        if install.stderr:
            logs.extend([l for l in install.stderr.splitlines() if l.strip()])
        if install.returncode != 0:
            tail = "\n".join((logs or [])[-120:])
            raise HTTPException(400, f"npm install failed\n\n--- npm output (tail) ---\n{tail}")

        # strictPort so we know the port; if it's taken, user can run again (we'll pick a new port)
        cmd = ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--strictPort", "--port", str(port)]
        logs.append(f"$ {' '.join(cmd)}")
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
    return {"ok": True, "id": rid, "pid": proc.pid, "url": f"http://localhost:{port}", "project_root": req.project_root}


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
    return {"items": list_tree(_ws(), req.path)}


class ReadReq(BaseModel):
    path: str


@app.post("/api/fs/read")
def fs_read(req: ReadReq):
    try:
        return {"content": (_ws() / req.path).read_text(encoding="utf-8")}
    except FileNotFoundError:
        raise HTTPException(404, "Not found")


class WriteReq(BaseModel):
    path: str
    content: str
    expected_sha256: str | None = None  # reserved for optimistic locking


@app.post("/api/fs/write")
def fs_write(req: WriteReq):
    write_text(_ws(), req.path, req.content)
    return {"ok": True}


class WriteOp(BaseModel):
    path: str
    content: str


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
        if p.exists() and not req.overwrite:
            conflicts.append(op.path)

    if conflicts:
        raise HTTPException(409, f"Conflicts (already exist): {', '.join(conflicts[:20])}")

    for op in req.ops:
        write_text(root, op.path, op.content)

    return {"ok": True, "count": len(req.ops)}


class DiffReq(BaseModel):
    path: str
    new_content: str


@app.post("/api/fs/diff")
def fs_diff(req: DiffReq):
    old = read_text(_ws(), req.path)
    d = diff_text(old, req.new_content, filename=req.path)
    return {"diff": d}


class PreviewAuditReq(BaseModel):
    preview_url: str
    attempts: int = 3
    max_excerpt_chars: int = 800


@app.post("/api/preview/audit")
def preview_audit(req: PreviewAuditReq):
    preview_url = _normalize_preview_url(req.preview_url)
    html = _fetch_preview_html(preview_url, attempts=max(1, min(req.attempts, 5)))
    return _audit_preview_html(preview_url, html, max_excerpt_chars=max(200, min(req.max_excerpt_chars, 4000)))


class ProjectValidateReq(BaseModel):
    project_root: str = "."
    max_commands: int = 4


@app.post("/api/project/validate")
def project_validate(req: ProjectValidateReq):
    ws_root = _ws()
    project_root = (req.project_root or ".").strip() or "."
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


class ImageAssetResp(BaseModel):
    ok: bool
    path: str
    name: str
    content_type: str | None = None
    size: int


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


@app.post("/api/agent")
def agent(req: AgentReq):
    """Suggest a multi-file patch. Adds per-file unified diffs."""
    ws_root = _ws()
    project_root = (req.project_root or ".").strip() or "."
    project_dir = safe_join(ws_root, project_root)
    build_mode = (req.build_mode or settings_mod.settings.build_mode or "hybrid").strip().lower()
    mode_spec = get_agent_mode_spec(build_mode)
    build_mode = mode_spec.build_mode
    is_full_agent = build_mode == "full-agent"

    active_rel = _localize_project_rel(req.active_file, project_root)
    open_files = [
        rel
        for rel in (_localize_project_rel(path, project_root) for path in (req.open_files or []))
        if rel
    ]
    current_from_buffer = isinstance(req.current_content, str) and bool(active_rel)

    try:
        from .hybrid import build_hybrid_seed, merge_hybrid_seed, should_seed_hybrid

        project_name = project_dir.name if project_root != "." else ws_root.name
        current = req.current_content if current_from_buffer else (read_text(project_dir, active_rel) if active_rel else "")

        all_files = [
            str(p.relative_to(project_dir))
            for p in project_dir.rglob("*")
            if p.is_file() and "node_modules" not in str(p) and ".git" not in str(p)
        ]
        all_file_set = set(all_files)

        relevant_files: dict[str, str] = {}

        def add_relevant(rel_path: str, max_chars: int = 20_000, content_override: str | None = None):
            rel_path = _localize_project_rel(rel_path, project_root)
            if not rel_path:
                return
            try:
                if content_override is None:
                    p = project_dir / rel_path
                    if not p.exists() or not p.is_file():
                        return
                    txt = read_text(project_dir, rel_path)
                else:
                    txt = content_override
                relevant_files[rel_path] = txt[:max_chars]
            except Exception:
                return

        if active_rel:
            add_relevant(active_rel, content_override=current if current_from_buffer else None)
        for open_rel in open_files[:4]:
            if open_rel != active_rel and open_rel not in relevant_files:
                add_relevant(open_rel, max_chars=16_000)

        for key_file in [
            "package.json",
            "README.md",
            "PRD.md",
            "docs/PRD.md",
            "src/App.tsx",
            "src/main.tsx",
            "src/app.css",
            "index.html",
            "vite.config.ts",
        ]:
            if key_file not in relevant_files:
                add_relevant(key_file, max_chars=30_000 if key_file.endswith("PRD.md") else 20_000)

        for rel in _resolve_related_files(active_rel, current, all_file_set)[:10]:
            if rel not in relevant_files:
                add_relevant(rel, max_chars=20_000)

        hint = (req.input or "").lower()
        wants_style = any(k in hint for k in ["css", "style", "styles", "tema", "theme", "warna", "color", "font", "spacing", "layout", "ui", "ux"])
        if wants_style:
            styles_dir = project_dir / "src" / "styles"
            if styles_dir.exists():
                for p in styles_dir.glob("*.css"):
                    try:
                        rel = str(p.relative_to(project_dir))
                    except Exception:
                        continue
                    if rel not in relevant_files:
                        add_relevant(rel, max_chars=30_000)

        if active_rel.endswith(".html"):
            try:
                html = current if active_rel in relevant_files else read_text(project_dir, active_rel)
                for match in re.findall(r'href=["\']([^"\']+\.css)["\']', html, flags=re.IGNORECASE):
                    css_path = match.lstrip("/")
                    if css_path and css_path not in relevant_files:
                        add_relevant(css_path)
            except Exception:
                pass

        hybrid_seed_needed = is_full_agent and should_seed_hybrid(project_dir)
        if hybrid_seed_needed:
            seed_files = build_hybrid_seed(project_root, project_name, req.input)
            for rel_path, content in seed_files.items():
                rel_local = _localize_project_rel(rel_path, project_root)
                if rel_local not in relevant_files:
                    relevant_files[rel_local] = content
                if rel_local not in all_file_set:
                    all_files.append(rel_local)
                    all_file_set.add(rel_local)

        attached_assets: list[str] = []
        for asset_path in req.asset_paths or []:
            asset_rel = str(asset_path or "").strip().lstrip("/")
            if not asset_rel:
                continue
            try:
                asset_abs = safe_join(ws_root, asset_rel)
            except Exception:
                continue
            if not asset_abs.exists() or not asset_abs.is_file():
                continue
            attached_assets.append(asset_rel)

    except Exception:
        current = req.current_content if isinstance(req.current_content, str) else ""
        all_files = []
        relevant_files = {}
        hybrid_seed_needed = False
        project_name = project_dir.name if project_root != "." else ws_root.name
        active_rel = _localize_project_rel(req.active_file, project_root)
        attached_assets = []

    try:
        from .agent import suggest
        from .hybrid import merge_hybrid_seed

        context_parts: list[str] = [
            f"Build mode: {build_mode}",
            f"Agent persona: {mode_spec.persona_name} ({mode_spec.persona_label})",
            f"Project root: {project_root}",
            f"Active file: {active_rel or '(none)'}",
        ]
        if req.editor_status:
            context_parts.append(f"Editor status: {req.editor_status.strip()}")
        if current_from_buffer:
            context_parts.append("Current content was supplied from the live editor buffer and may be newer than disk.")
        if req.selection:
            context_parts.append("Selected code/text:\n" + req.selection[:4000])
        if open_files:
            context_parts.append("Open files:\n- " + "\n- ".join(open_files[:8]))
        if req.preview_url:
            context_parts.append(f"Live preview URL: {req.preview_url}")
            context_parts.append("When relevant, optimize for visible product quality in the running preview.")
        if PurePosixPath(active_rel or "").suffix in _FRONTEND_EXTS:
            context_parts.append("This request appears to touch a user-facing surface. Prioritize UI hierarchy, spacing, states, and polish.")

        asset_prompt = ""
        if attached_assets:
            asset_lines: list[str] = []
            for asset_rel in attached_assets:
                local_rel = _localize_project_rel(asset_rel, project_root)
                public_hint = None
                if "/public/" in f"/{local_rel}":
                    public_hint = "/" + local_rel.split("public/", 1)[1]
                asset_lines.append(f"- {local_rel}" + (f" (public URL hint: {public_hint})" if public_hint else ""))
            asset_prompt = (
                "ATTACHED IMAGE ASSETS:\n"
                "The user uploaded these image assets into the project. Use them directly in the implementation when relevant instead of placeholder images.\n"
                + "\n".join(asset_lines)
                + "\n\n"
            )

        base_instruction = mode_spec.instruction_prefix + asset_prompt + req.input
        extra_context = "\n\n".join(context_parts)

        try:
            with AGENT_LOCK:
                sug = suggest(
                    instruction=base_instruction,
                    path=active_rel or "(no-active-file)",
                    content=current,
                    file_tree=all_files,
                    relevant_files=relevant_files,
                    extra_context=extra_context,
                    workspace_root=project_dir,
                    system=mode_spec.system_prompt,
                )
            sug_spoken = sug.spoken
            sug_log = sug.log
            normalized_changes = list(sug.changes or [])
            normalized_actions = list(sug.actions or [])
        except RuntimeError:
            if not (is_full_agent and hybrid_seed_needed):
                raise
            sug_spoken = "I prepared a runnable full-agent baseline for the selected project so preview can work, then you can iterate again."
            sug_log = f"provider={settings_mod.settings.llm_provider} persona={mode_spec.persona_name.lower()} full-agent-mode=seed-only"
            normalized_changes = []
            normalized_actions = []

        if normalized_changes and _should_run_refinement(
            build_mode=build_mode,
            instruction=req.input,
            active_rel=active_rel,
            preview_url=req.preview_url,
            attached_assets=attached_assets,
        ):
            draft_relevant = dict(relevant_files)
            for item in normalized_changes:
                rel = _localize_project_rel(item.get("path"), project_root)
                content = item.get("new_content")
                if rel and isinstance(content, str):
                    draft_relevant[rel] = content[:30_000]

            refinement_instruction = mode_spec.instruction_prefix + asset_prompt + mode_spec.refinement_prefix + req.input
            try:
                with AGENT_LOCK:
                    refined = suggest(
                        instruction=refinement_instruction,
                        path=active_rel or "(no-active-file)",
                        content=draft_relevant.get(active_rel, current),
                        file_tree=all_files,
                        relevant_files=draft_relevant,
                        extra_context=extra_context + "\n\nThis is a second-pass review over a draft solution.",
                        workspace_root=project_dir,
                        system=mode_spec.system_prompt,
                    )
                normalized_changes = _merge_change_sets(normalized_changes, list(refined.changes or []))
                normalized_actions = _merge_action_sets(normalized_actions, list(refined.actions or []))
                sug_spoken = refined.spoken or sug_spoken
                sug_log = f"{sug_log} persona={mode_spec.persona_name.lower()} passes=2"
            except Exception:
                sug_log = f"{sug_log} persona={mode_spec.persona_name.lower()} passes=1 refine=skipped"

        if f"persona={mode_spec.persona_name.lower()}" not in sug_log:
            sug_log = f"{sug_log} persona={mode_spec.persona_name.lower()}"

        if project_root != ".":
            scoped_changes: list[dict[str, str]] = []
            for item in normalized_changes:
                rel = str(item.get("path") or "").strip().lstrip("/")
                content = item.get("new_content")
                if not rel or not isinstance(content, str):
                    continue
                rel = _localize_project_rel(rel, project_root)
                if not rel:
                    continue
                scoped_changes.append({"path": f"{project_root}/{rel}", "new_content": content})
            normalized_changes = scoped_changes

        if is_full_agent:
            normalized_changes = merge_hybrid_seed(
                project_root=project_root,
                project_name=project_name,
                instruction=req.input,
                changes=normalized_changes,
                should_seed=hybrid_seed_needed,
            )
            if hybrid_seed_needed and "full-agent-mode" not in sug_log:
                sug_log = f"{sug_log} full-agent-mode=seeded"

        out_changes: list[dict[str, str]] = []
        for ch in normalized_changes:
            if not isinstance(ch, dict):
                continue
            p = str(ch.get("path") or "").strip()
            nc = ch.get("new_content")
            if not p or not isinstance(nc, str):
                continue

            try:
                old = read_text(ws_root, p)
            except FileNotFoundError:
                old = ""

            out_changes.append({
                "path": p,
                "new_content": nc,
                "diff": diff_text(old, nc, filename=p),
            })

        return {
            "spoken": sug_spoken,
            "log": sug_log,
            "changes": out_changes,
            "actions": normalized_actions,
            "no_changes": len(out_changes) == 0 and len(normalized_actions) == 0,
        }
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))
