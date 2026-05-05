from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Literal
import os
import shutil
import threading
import time
import json
import re
import shlex
import subprocess
from html import unescape
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request as URLRequest, urlopen

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from api.settings import ROOT, ENV_PATH, load_settings
from api.supabase_store import has_supabase, upsert_profile
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
from api.agent_memory import get_agent_memory_overview
from api.agent_runtime import run_agent_pipeline
from api.agent_skills import detect_project_stack


app = FastAPI(title="Voice IDE Backend", version="0.1.0")

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


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


_RELATIVE_IMPORT_RE = re.compile(r'(?:import\s+(?:[^\"\']+?\s+from\s+)?|export\s+[^\"\']*?\s+from\s+|import\()\s*["\']([^"\']+)["\']')
_FRONTEND_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".css", ".scss", ".sass", ".less", ".html"}
_KNOWN_PACKAGE_MANAGERS = ("npm", "pnpm", "yarn", "bun")


def _localize_project_rel(rel_path: str | None, project_root: str) -> str:
    rel = str(rel_path or "").strip().lstrip("/")
    if rel and project_root != "." and rel.startswith(project_root + "/"):
        rel = rel[len(project_root) + 1 :]
    return rel


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
    refinement_mode = str(getattr(settings_mod.settings, "agent_refinement_mode", "auto") or "auto").strip().lower()
    if refinement_mode == "off":
        return False
    if refinement_mode == "always":
        return True

    friendly_mode = bool(getattr(settings_mod.settings, "friendly_free_tier_mode", True))
    if build_mode == "full-agent":
        return not friendly_mode
    if preview_url or attached_assets:
        return not friendly_mode

    hint = (instruction or "").lower()
    strong_refine_keywords = (
        "polish", "refine", "audit", "review", "production", "ux", "ui", "layout", "spacing",
        "responsive", "design", "landing", "dashboard", "improve", "better", "theme", "style", "visual", "state",
    )
    bugfix_keywords = ("fix", "bug", "error", "broken", "crash")
    if any(word in hint for word in strong_refine_keywords):
        return not friendly_mode
    if any(word in hint for word in bugfix_keywords):
        return False

    return PurePosixPath(active_rel or "").suffix in _FRONTEND_EXTS and not friendly_mode


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
    profile_token = CURRENT_PROFILE_ID.set(resolved_user.supabase_user_id or resolved_user.user_id)
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
        CURRENT_PROFILE_ID.reset(profile_token)
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
    return bool(_resolve_node_binary() and _playwright_audit_script().exists() and _project_uses_playwright(project_dir))


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


def _build_preview_audit_result(
    preview_url: str,
    snapshot: dict,
    *,
    audit_mode: str,
    max_excerpt_chars: int = 800,
    runtime_warnings: list[str] | None = None,
) -> dict:
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
    console_errors = [str(item).strip()[:240] for item in (snapshot.get("console_errors") or []) if str(item).strip()][:8]
    page_errors = [str(item).strip()[:240] for item in (snapshot.get("page_errors") or []) if str(item).strip()][:6]

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
    if page_errors:
        issues.append(f"Preview threw {len(page_errors)} runtime browser error(s).")
    if console_errors:
        issues.append(f"Preview logged {len(console_errors)} browser console warning/error message(s).")

    summary_parts = [
        f"mode={audit_mode}",
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
        "audit_mode": audit_mode,
        "title": title,
        "meta_description": meta_description,
        "headings": headings,
        "subheadings": subheadings,
        "buttons": buttons,
        "links": links,
        "form_count": form_count,
        "input_count": input_count,
        "word_count": word_count,
        "image_count": image_count,
        "images_missing_alt": images_missing_alt,
        "console_errors": console_errors,
        "page_errors": page_errors,
        "runtime_warnings": runtime_warnings or [],
        "issues": issues,
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

    return {
        "title": title,
        "meta_description": meta_description,
        "headings": headings,
        "subheadings": subheadings,
        "buttons": buttons,
        "links": links,
        "form_count": len(re.findall(r"<form\b", html, flags=re.IGNORECASE)),
        "input_count": len(re.findall(r"<(input|textarea|select)\b", html, flags=re.IGNORECASE)),
        "word_count": len(re.findall(r"\b\w+\b", body_text)),
        "image_count": len(image_tags),
        "images_missing_alt": images_missing_alt,
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
                    "User-Agent": "VoiceIDE/0.1 (+preview-audit)",
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


def _run_playwright_preview_audit(preview_url: str, project_dir: Path, max_excerpt_chars: int = 800) -> tuple[dict | None, str | None]:
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
    ), None


def _audit_preview_html(preview_url: str, html: str, max_excerpt_chars: int = 800) -> dict:
    snapshot = _extract_preview_snapshot_from_html(html, max_excerpt_chars=max_excerpt_chars)
    return _build_preview_audit_result(
        preview_url,
        snapshot,
        audit_mode="html",
        max_excerpt_chars=max_excerpt_chars,
    )


def _run_shell_command(command: str, cwd: Path, timeout: int = 120) -> dict:
    if any(fragment in command for fragment in DANGEROUS_COMMAND_FRAGMENTS):
        raise HTTPException(403, "Command blocked for safety")

    translated_command, translated_note = _translate_package_manager_command(command, cwd)
    if translated_command is None:
        return {
            "ok": False,
            "stdout": "",
            "stderr": translated_note or "Command could not be translated for this runtime.",
            "returncode": 127,
        }

    try:
        proc = subprocess.run(
            translated_command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout = proc.stdout or ""
        if translated_note:
            stdout = f"{translated_note}\n{stdout}".strip()
        return {
            "ok": proc.returncode == 0,
            "stdout": stdout,
            "stderr": proc.stderr,
            "returncode": proc.returncode,
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        if translated_note:
            stdout = f"{translated_note}\n{stdout}".strip()
        return {
            "ok": False,
            "stdout": stdout,
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
    import os

    base_raw = settings_mod.settings.default_workspace
    if base_raw:
        return Path(base_raw).expanduser().resolve()

    if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or os.environ.get("LAMBDA_TASK_ROOT"):
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

    readme = target_dir / "README.md"
    if not readme.exists():
        readme.write_text(
            "# Voice IDE Workspace\n\n"
            f"This workspace was provisioned automatically for the current {mode}.\n"
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

    if os.getenv("VERCEL"):
        raise HTTPException(400, "Embedded preview is not available in this deployment.")

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
    project_root: str = "."
    mode: Literal["auto", "html", "browser"] = "auto"


@app.post("/api/preview/audit")
def preview_audit(req: PreviewAuditReq):
    preview_url = _normalize_preview_url(req.preview_url)
    max_excerpt_chars = max(200, min(req.max_excerpt_chars, 4000))
    project_root = (req.project_root or ".").strip() or "."
    project_dir = safe_join(_ws(), project_root)
    warnings: list[str] = []

    requested_mode = str(req.mode or "auto").strip().lower()
    if requested_mode not in {"auto", "html", "browser"}:
        requested_mode = "auto"

    if requested_mode != "html":
        browser_audit, browser_warning = _run_playwright_preview_audit(preview_url, project_dir, max_excerpt_chars=max_excerpt_chars)
        if browser_audit:
            return browser_audit
        if browser_warning:
            warnings.append(browser_warning)

    html = _fetch_preview_html(preview_url, attempts=max(1, min(req.attempts, 5)))
    audit = _audit_preview_html(preview_url, html, max_excerpt_chars=max_excerpt_chars)
    if warnings:
        audit["runtime_warnings"] = [*audit.get("runtime_warnings", []), *warnings]
    return audit


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
    stream: bool = False


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
    project_dir = safe_join(ws_root, proj_root)
    servers = discover_mcp_servers(ws_root, project_dir) if project_dir.exists() else []
    tool_catalog = list_mcp_tools(ws_root, project_dir, refresh=False) if include_live_tools and servers else {}
    memory_overview = get_agent_memory_overview(ws_root, project_root=proj_root)
    stack = detect_project_stack(project_dir) if project_dir.exists() else None
    node_runtime = bool(_resolve_node_binary())
    browser_audit_ready = bool(project_dir.exists() and _browser_preview_audit_ready(project_dir))
    memory_backend = "supabase-doc-chunks" if has_supabase() else "local-doc-chunks"
    return {
        "ok": True,
        "runtime": "langgraph",
        "supports": {
            "graph_runtime": True,
            "short_term_memory_rag": True,
            "project_scoped_short_memory": True,
            "long_term_memory_rag": True,
            "skill_registry": True,
            "mcp_registry": True,
            "mcp_tool_execution": True,
            "autonomous_mcp_loop": True,
            "interaction_intent_detection": True,
            "command_conversation_boundary": True,
            "read_only_inspection_boundary": True,
            "supabase_memory_backend": has_supabase(),
            "component_library_awareness": True,
            "headless_browser_runtime": browser_audit_ready,
            "playwright_preview_audit": browser_audit_ready,
            "webcontainer_runtime": False,
            "browser_dom_audit": browser_audit_ready,
            "preview_audit_mode": "browser" if browser_audit_ready else "html",
            "tool_actions": ["shell", "mcp"],
            "streaming_transport": True,
            "native_provider_token_streaming": False,
        },
        "boundaries": {
            "project_root": proj_root,
            "memory_store": ".voiceide/agent-memory",
            "custom_skills_dir": [".voiceide/skills", f"{proj_root}/.voiceide/skills" if proj_root != "." else ".voiceide/skills"],
            "mcp_config_candidates": [".voiceide/mcp.json", f"{proj_root}/.voiceide/mcp.json" if proj_root != "." else ".voiceide/mcp.json", f"{proj_root}/mcp.json" if proj_root != "." else "mcp.json"],
            "supabase_rag_table": "agent_memory_chunks" if has_supabase() else None,
            "mcp_loop_budget": 2,
        },
        "memory": {
            "session_entries": memory_overview.session_entries,
            "project_entries": memory_overview.project_entries,
            "latest_session_ts": memory_overview.latest_session_ts,
            "latest_project_ts": memory_overview.latest_project_ts,
            "retrieval_backend": memory_backend,
        },
        "stack": {
            "component_libraries": list(stack.component_libraries) if stack else [],
            "headless_browser": bool(stack.has_headless_browser) if stack else False,
            "playwright": bool(stack.has_playwright) if stack else False,
            "webcontainer": bool(stack.has_webcontainer) if stack else False,
            "node_runtime": node_runtime,
            "preview_audit_mode": "browser" if browser_audit_ready else "html",
        },
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


def _run_agent_impl(req: AgentReq, event_cb=None):
    def emit(event: str, data: dict):
        if event_cb:
            try:
                event_cb(event, data)
            except Exception:
                pass

    emit("status", {"phase": "starting", "message": "Nyusun konteks kerja dulu..."})
    ws_root = _ws()

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

            try:
                old = read_text(ws_root, p)
            except FileNotFoundError:
                old = ""

            out_changes.append({
                "path": p,
                "new_content": nc,
                "diff": diff_text(old, nc, filename=p),
            })

        result = {
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
        emit("done", {"message": "Beres, hasil agent siap dipakai.", "result": result})
        return result
    except RuntimeError as exc:
        emit("error", {"message": str(exc)})
        raise HTTPException(400, str(exc))
    except Exception as exc:
        emit("error", {"message": str(exc)})
        raise HTTPException(500, str(exc))


@app.post("/api/agent")
def agent(req: AgentReq):
    """Suggest a multi-file patch. Adds per-file unified diffs."""
    if req.stream:
        def event_stream():
            import queue as queue_mod

            stream_queue: queue_mod.Queue[str | None] = queue_mod.Queue()

            def push(event: str, data: dict):
                stream_queue.put(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n")

            def worker():
                push("status", {"phase": "queued", "message": "Agent diterima, mulai jalan..."})
                try:
                    _run_agent_impl(req, event_cb=push)
                except HTTPException as exc:
                    push("error", {"message": str(exc.detail)})
                except Exception as exc:
                    push("error", {"message": str(exc)})
                finally:
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

    return _run_agent_impl(req)
