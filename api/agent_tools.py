from __future__ import annotations

import json
import re
import shlex
import sqlite3
import subprocess
import time
from dataclasses import dataclass
import difflib
import html as html_lib
import os
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from api.agent_skills import build_validation_plan, detect_project_stack, list_imported_skills, read_imported_skill
from api.fs import read_text, safe_join


@dataclass(frozen=True)
class LocalToolInfo:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class LocalToolCallResult:
    tool: str
    arguments: dict[str, Any]
    ok: bool
    text: str
    raw: dict[str, Any]
    duration_ms: int
    error: str | None = None


def _workspace_or_project_path(ws_root: Path, project_dir: Path, raw_path: str) -> str:
    """Accept either workspace-relative or current-project-relative file paths."""
    clean = str(raw_path or "").strip().lstrip("/")
    if clean.startswith("./"):
        clean = clean[2:]
    if not clean:
        return clean
    try:
        if safe_join(ws_root, clean).exists():
            return clean
    except Exception:
        pass
    try:
        project_rel = project_dir.resolve().relative_to(ws_root.resolve()).as_posix()
    except Exception:
        project_rel = ""
    if project_rel and not clean.startswith(project_rel + "/"):
        candidate = f"{project_rel}/{clean}"
        try:
            if safe_join(ws_root, candidate).exists():
                return candidate
        except Exception:
            pass
    return clean


_LOCAL_TOOLS: list[LocalToolInfo] = [
    LocalToolInfo(
        name="repo_list",
        description="List a shallow file tree for the current project (read-only).",
        input_schema={
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "Project root relative to workspace"},
                "max_files": {"type": "integer", "description": "Max files to return", "default": 300},
            },
        },
    ),
    LocalToolInfo(
        name="repo_read",
        description="Read a text file from the workspace (read-only).",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to workspace"},
                "max_chars": {"type": "integer", "default": 20000},
            },
            "required": ["path"],
        },
    ),
    LocalToolInfo(
        name="repo_read_many",
        description="Read multiple text files from the workspace in one call (read-only, bounded output).",
        input_schema={
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}, "description": "Paths relative to workspace"},
                "max_chars_per_file": {"type": "integer", "default": 12000},
                "max_total_chars": {"type": "integer", "default": 50000},
            },
            "required": ["paths"],
        },
    ),
    LocalToolInfo(
        name="repo_search",
        description="Search for a substring/regex in project files (read-only).",
        input_schema={
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "Project root relative to workspace"},
                "query": {"type": "string", "description": "Substring or regex"},
                "regex": {"type": "boolean", "default": False},
                "max_matches": {"type": "integer", "default": 120},
            },
            "required": ["query"],
        },
    ),
    LocalToolInfo(
        name="repo_map",
        description="Build a concise aider-style repo map with important files, imports, and key symbols/signatures (read-only).",
        input_schema={
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "Project root relative to workspace"},
                "query": {"type": "string", "description": "Task/query used to rank relevant files"},
                "max_files": {"type": "integer", "default": 80},
                "max_symbols_per_file": {"type": "integer", "default": 12},
            },
        },
    ),
    LocalToolInfo(
        name="file_window",
        description="Open a line-numbered window around a file line, like SWE-agent open/goto (read-only).",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to workspace"},
                "line": {"type": "integer", "default": 1},
                "context": {"type": "integer", "default": 80},
            },
            "required": ["path"],
        },
    ),
    LocalToolInfo(
        name="symbol_search",
        description="Search classes/functions/types/components by symbol name and return line-numbered definitions (read-only).",
        input_schema={
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "Project root relative to workspace"},
                "query": {"type": "string", "description": "Symbol substring or regex"},
                "regex": {"type": "boolean", "default": False},
                "max_matches": {"type": "integer", "default": 80},
            },
            "required": ["query"],
        },
    ),
    LocalToolInfo(
        name="style_stack",
        description="Detect styling system and CSS conventions: Tailwind, CSS modules, styled-components, shadcn, tokens, and utility usage (read-only).",
        input_schema={
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "Project root relative to workspace"},
                "max_files": {"type": "integer", "default": 180},
            },
        },
    ),
    LocalToolInfo(
        name="line_replace_preview",
        description="Preview a SWE-agent style line-range replacement and return the resulting file content as a suggested_change without writing files.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to workspace"},
                "start_line": {"type": "integer", "description": "1-based first line to replace"},
                "end_line": {"type": "integer", "description": "1-based last line to replace, inclusive"},
                "replacement": {"type": "string", "description": "Replacement text for the selected line range"},
                "context": {"type": "integer", "default": 4},
            },
            "required": ["path", "start_line", "end_line", "replacement"],
        },
    ),
    LocalToolInfo(
        name="line_replace_apply",
        description="Apply a SWE-agent style line-range replacement to a workspace file and return a unified diff plus an after-window.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to workspace"},
                "start_line": {"type": "integer", "description": "1-based first line to replace"},
                "end_line": {"type": "integer", "description": "1-based last line to replace, inclusive"},
                "replacement": {"type": "string", "description": "Replacement text for the selected line range"},
                "context": {"type": "integer", "default": 4},
            },
            "required": ["path", "start_line", "end_line", "replacement"],
        },
    ),
    LocalToolInfo(
        name="search_replace_preview",
        description="Preview an aider-style SEARCH/REPLACE edit with exact, whitespace-flexible, ellipsis, and fuzzy line-window matching; returns suggested_change without writing files.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to workspace"},
                "search": {"type": "string", "description": "Original text to find"},
                "replace": {"type": "string", "description": "Replacement text"},
                "context": {"type": "integer", "default": 4},
                "allow_fuzzy": {"type": "boolean", "default": True},
            },
            "required": ["path", "search", "replace"],
        },
    ),
    LocalToolInfo(
        name="search_replace_apply",
        description="Apply an aider-style SEARCH/REPLACE edit to a workspace file and return a unified diff plus an after-window.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to workspace"},
                "search": {"type": "string", "description": "Original text to find"},
                "replace": {"type": "string", "description": "Replacement text"},
                "context": {"type": "integer", "default": 4},
                "allow_fuzzy": {"type": "boolean", "default": True},
            },
            "required": ["path", "search", "replace"],
        },
    ),
    LocalToolInfo(
        name="package_scripts",
        description="Inspect package.json scripts, dependencies, package manager hints, and validation candidates (read-only).",
        input_schema={
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "Project root relative to workspace"},
            },
        },
    ),
    LocalToolInfo(
        name="repo_overview",
        description="Summarize project shape, key files, language mix, and likely stack from the file tree (read-only).",
        input_schema={
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "Project root relative to workspace"},
                "max_files": {"type": "integer", "default": 500},
            },
        },
    ),
    LocalToolInfo(
        name="stack_profile",
        description="Detect languages, frameworks, runtimes, package managers, database/infra signals, and preview surface for any code stack (read-only).",
        input_schema={
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "Project root relative to workspace"},
            },
        },
    ),
    LocalToolInfo(
        name="validation_plan",
        description="Recommend stack-specific build/test/lint/typecheck commands for JS, Python, Go, Rust, Java, PHP, Ruby, .NET, infra, and DB projects (read-only).",
        input_schema={
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "Project root relative to workspace"},
            },
        },
    ),
    LocalToolInfo(
        name="test_runner",
        description="Run a bounded project test/build/lint command and capture stdout/stderr for debugging. Use for unit tests and validation loops.",
        input_schema={
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "Project root relative to workspace"},
                "commands": {"type": "array", "items": {"type": "string"}, "description": "Validation commands to run from project_root"},
                "timeout_seconds": {"type": "integer", "default": 120},
                "max_output_chars": {"type": "integer", "default": 12000},
            },
        },
    ),
    LocalToolInfo(
        name="format_lint",
        description="Run bounded formatter/linter fixes or checks for selected files. Supports built-in JSON formatting plus local Prettier/ESLint/Ruff/Black when available.",
        input_schema={
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "Project root relative to workspace"},
                "mode": {"type": "string", "enum": ["check", "fix"], "default": "fix"},
                "files": {"type": "array", "items": {"type": "string"}, "description": "Project-relative files to format/lint"},
                "tools": {"type": "array", "items": {"type": "string"}, "description": "Subset of json, prettier, eslint, ruff, black"},
                "timeout_seconds": {"type": "integer", "default": 90},
            },
        },
    ),
    LocalToolInfo(
        name="database_client",
        description="Inspect or mutate a local project database through a bounded client. Supports SQLite query/migrate/schema now; Supabase/Postgres should be wired through MCP or backend config later.",
        input_schema={
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "Project root relative to workspace"},
                "backend": {"type": "string", "enum": ["sqlite"], "default": "sqlite"},
                "database": {"type": "string", "description": "SQLite database path relative to project_root"},
                "mode": {"type": "string", "enum": ["query", "migrate", "schema"], "default": "query"},
                "sql": {"type": "string", "description": "SQL to execute"},
                "max_rows": {"type": "integer", "default": 100},
            },
        },
    ),
    LocalToolInfo(
        name="git_manager",
        description="Inspect git status/diff, produce commit plans, or perform guarded local git mutations. Remote push/PR modes require allow_remote=true.",
        input_schema={
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "Project root relative to workspace"},
                "mode": {"type": "string", "enum": ["status", "diff", "commit_plan", "branch", "commit", "push", "pr_plan"], "default": "status"},
                "message": {"type": "string", "description": "Commit message for commit/commit_plan"},
                "branch": {"type": "string", "description": "Branch name for branch mode"},
                "paths": {"type": "array", "items": {"type": "string"}, "description": "Project-relative paths for commit staging"},
                "allow_remote": {"type": "boolean", "default": False},
                "max_diff_chars": {"type": "integer", "default": 12000},
            },
        },
    ),
    LocalToolInfo(
        name="docs_browser",
        description="Fetch a public HTTPS documentation page and return cleaned, bounded text for current API/library research.",
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Public HTTPS documentation URL"},
                "max_chars": {"type": "integer", "default": 12000},
            },
            "required": ["url"],
        },
    ),
    LocalToolInfo(
        name="skill_catalog",
        description="Search imported Appora/Codex/Claude/OpenClaw-style SKILL.md metadata without loading full skill bodies (read-only).",
        input_schema={
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "Project root relative to workspace"},
                "query": {"type": "string", "description": "Task or keyword query for ranking skills"},
                "limit": {"type": "integer", "default": 12},
            },
        },
    ),
    LocalToolInfo(
        name="skill_read",
        description="Read one imported skill body by skill_id after skill_catalog identifies it as relevant (read-only).",
        input_schema={
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "Project root relative to workspace"},
                "skill_id": {"type": "string", "description": "Skill id from skill_catalog"},
                "max_chars": {"type": "integer", "default": 10000},
            },
            "required": ["skill_id"],
        },
    ),
    LocalToolInfo(
        name="dependency_graph",
        description="Build a bounded import/dependency graph for JS/TS source files (read-only).",
        input_schema={
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "Project root relative to workspace"},
                "max_files": {"type": "integer", "default": 180},
            },
        },
    ),
    LocalToolInfo(
        name="component_index",
        description="Index exported React components/functions, hooks, props types, and likely component files (read-only).",
        input_schema={
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "Project root relative to workspace"},
                "max_files": {"type": "integer", "default": 220},
            },
        },
    ),
    LocalToolInfo(
        name="route_map",
        description="Extract likely app routes, navigation links, route components, and router usage from JS/TS/HTML files (read-only).",
        input_schema={
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "Project root relative to workspace"},
                "max_files": {"type": "integer", "default": 220},
            },
        },
    ),
    LocalToolInfo(
        name="quality_scan",
        description="Scan source files for production-readiness signals and risks: TODOs, console logs, a11y labels, loading/error/empty states, responsive CSS, and placeholders (read-only).",
        input_schema={
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "Project root relative to workspace"},
                "max_files": {"type": "integer", "default": 260},
            },
        },
    ),
    LocalToolInfo(
        name="memory_overview",
        description="Inspect Appora agent memory readiness and counts for the current project/session (read-only).",
        input_schema={
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "Project root relative to workspace"},
            },
        },
    ),
    LocalToolInfo(
        name="mcp_status",
        description="Inspect configured MCP servers and optionally their live tool names for this workspace/project (read-only).",
        input_schema={
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "Project root relative to workspace"},
                "include_live_tools": {"type": "boolean", "default": False},
            },
        },
    ),
    LocalToolInfo(
        name="preview_capabilities",
        description="Inspect whether the project has a runnable preview surface and which scripts/entry points are available (read-only).",
        input_schema={
            "type": "object",
            "properties": {
                "project_root": {"type": "string", "description": "Project root relative to workspace"},
            },
        },
    ),
]


def list_local_tools() -> list[LocalToolInfo]:
    return list(_LOCAL_TOOLS)


_IGNORED_DIRS = {
    ".git",
    "node_modules",
    "dist",
    "build",
    ".next",
    ".vercel",
    ".voiceide",
    "api/.venv",
}

_BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".zip", ".pdf", ".ttf", ".woff", ".woff2"}
_IMPORT_RE = re.compile(r'(?:import\s+(?:[^"\']+?\s+from\s+)?|export\s+[^"\']*?\s+from\s+|import\()\s*["\']([^"\']+)["\']')
_SOURCE_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
_FRONTEND_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".css", ".scss", ".sass", ".less", ".html"}
_COMPONENT_EXPORT_RE = re.compile(
    r"\bexport\s+(?:default\s+)?(?:function|const)\s+([A-Z][A-Za-z0-9_]*)\b|\bfunction\s+([A-Z][A-Za-z0-9_]*)\s*\(",
    re.MULTILINE,
)
_HOOK_RE = re.compile(r"\b(?:export\s+)?(?:function|const)\s+(use[A-Z][A-Za-z0-9_]*)\b")
_TYPE_RE = re.compile(r"\bexport\s+(?:type|interface)\s+([A-Z][A-Za-z0-9_]*(?:Props|State|Config|Options)?)\b")
_ROUTE_RE = re.compile(r'\b(?:path|to|href)=["\']([^"\']+)["\']|createBrowserRouter\s*\(|<Route\b|react-router-dom|@tanstack/react-router')
_PLACEHOLDER_RE = re.compile(r"\b(lorem ipsum|todo app|placeholder|coming soon|dummy data|mock data|example\.com)\b", re.IGNORECASE)
_SYMBOL_DEFINITION_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:(?:async\s+)?function|class|interface|type|const|let|var)\s+([A-Za-z_$][\w$]*)\b[^\n{;=]*",
    re.MULTILINE,
)
_PY_SYMBOL_DEFINITION_RE = re.compile(r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_][\w]*)\b[^\n:]*", re.MULTILINE)
_GO_SYMBOL_DEFINITION_RE = re.compile(r"^\s*(?:func|type)\s+(?:\([^)]+\)\s*)?([A-Za-z_][\w]*)\b[^\n{]*", re.MULTILINE)
_RUST_SYMBOL_DEFINITION_RE = re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?(?:fn|struct|enum|trait|impl)\s+([A-Za-z_][\w]*)?\b[^\n{;]*", re.MULTILINE)
_JAVA_SYMBOL_DEFINITION_RE = re.compile(r"^\s*(?:public|private|protected|static|final|abstract|\s)+\s*(?:class|interface|enum|record|[\w<>\[\]]+\s+)([A-Za-z_][\w]*)\s*\(", re.MULTILINE)
_UTILITY_CLASS_RE = re.compile(
    r"\b(?:flex|grid|hidden|block|inline-flex|items-[\w-]+|justify-[\w-]+|gap-\d|p[trblxy]?-\d|m[trblxy]?-\d|text-[\w\-/]+|bg-[\w\-/]+|border(?:-[\w\-/]+)?|rounded(?:-[\w]+)?|shadow(?:-[\w]+)?|w-[\w\-/\[\]]+|h-[\w\-/\[\]]+|min-h-[\w\-/\[\]]+|max-w-[\w\-/\[\]]+)\b"
)
_SHELL_META_RE = re.compile(r"[;&|`$<>]")
_SAFE_VALIDATION_EXECUTABLES = {
    "npm",
    "pnpm",
    "yarn",
    "bun",
    "python",
    "python3",
    "pytest",
    "go",
    "cargo",
    "mvn",
    "gradle",
    "gradlew",
    "dotnet",
    "deno",
    "cmake",
    "make",
    "swift",
    "mix",
    "docker",
    "docker-compose",
    "kubectl",
    "composer",
    "bundle",
    "ruby",
}


def _should_ignore_path(rel_posix: str) -> bool:
    rel = rel_posix.strip().lstrip("/")
    if not rel:
        return False
    parts = rel.split("/")
    if not parts:
        return False
    if parts[0] in _IGNORED_DIRS:
        return True
    if len(parts) >= 2 and f"{parts[0]}/{parts[1]}" in _IGNORED_DIRS:
        return True
    return False


def _run_process(args: list[str], cwd: Path, *, timeout: int = 120) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": 124,
            "stdout": str(exc.stdout or ""),
            "stderr": str(exc.stderr or "") + f"\nTimed out after {timeout}s",
        }
    except FileNotFoundError as exc:
        return {"ok": False, "returncode": 127, "stdout": "", "stderr": str(exc)}


def _normalize_validation_command(command: str, project_root: str) -> str:
    clean = " ".join(str(command or "").strip().split())
    prefix = f"cd {project_root} && "
    if project_root not in {"", "."} and clean.startswith(prefix):
        clean = clean[len(prefix):].strip()
    return clean


def _safe_validation_command_args(command: str) -> list[str]:
    clean = str(command or "").strip()
    if not clean:
        raise RuntimeError("command is required")
    if _SHELL_META_RE.search(clean):
        raise RuntimeError("test_runner only accepts a single validation command without shell operators")
    parts = shlex.split(clean)
    if not parts:
        raise RuntimeError("command is required")
    exe = Path(parts[0]).name
    if exe not in _SAFE_VALIDATION_EXECUTABLES:
        raise RuntimeError(f"unsupported validation executable: {exe}")
    if exe in {"npm", "pnpm", "yarn", "bun"} and len(parts) >= 2:
        allowed = {"run", "test", "exec", "x"}
        if parts[1] not in allowed:
            raise RuntimeError(f"unsupported package-manager validation subcommand: {parts[1]}")
    if exe in {"python", "python3"} and "-c" in parts:
        raise RuntimeError("inline python -c is not allowed in test_runner")
    return parts


def _extract_git_status_paths(status_text: str) -> list[str]:
    paths: list[str] = []
    for line in status_text.splitlines():
        raw = line[3:] if len(line) > 3 else ""
        if " -> " in raw:
            raw = raw.split(" -> ", 1)[1]
        clean = raw.strip()
        if clean:
            paths.append(clean)
    return paths


def _clean_docs_html(html: str, max_chars: int) -> tuple[str, str]:
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    title = html_lib.unescape(re.sub(r"\s+", " ", title_match.group(1)).strip()) if title_match else ""
    body = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    body = re.sub(r"<style\b[^>]*>.*?</style>", " ", body, flags=re.IGNORECASE | re.DOTALL)
    body = re.sub(r"<[^>]+>", " ", body)
    text = html_lib.unescape(re.sub(r"\s+", " ", body).strip())
    return title[:240], text[:max_chars]


def _validate_public_docs_url(url: str) -> str:
    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError("docs_browser only accepts public https URLs")
    host = (parsed.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
        raise RuntimeError("docs_browser refuses local/private URLs")
    return parsed.geturl()


def _walk_candidate_files(project_dir: Path, *, limit_files: int = 1400) -> list[Path]:
    out: list[Path] = []
    for root, dirnames, filenames in os.walk(project_dir):
        root_path = Path(root)
        try:
            root_rel = root_path.relative_to(project_dir).as_posix()
        except Exception:
            continue
        dirnames[:] = [
            dirname
            for dirname in dirnames
            if not _should_ignore_path(dirname if root_rel == "." else f"{root_rel}/{dirname}")
        ]
        for filename in filenames:
            if len(out) >= limit_files:
                return out
            path = root_path / filename
            try:
                rel = path.relative_to(project_dir).as_posix()
            except Exception:
                continue
            if _should_ignore_path(rel):
                continue
            if path.suffix.lower() in _BINARY_SUFFIXES:
                continue
            out.append(path)
    return out


def _repo_tree_lines(project_dir: Path, *, max_files: int = 300) -> list[str]:
    max_files = max(10, min(int(max_files or 300), 2000))
    lines: list[str] = []
    for root, dirnames, filenames in os.walk(project_dir):
        root_path = Path(root)
        try:
            root_rel = root_path.relative_to(project_dir).as_posix()
        except Exception:
            continue
        dirnames[:] = [
            dirname
            for dirname in dirnames
            if not _should_ignore_path(dirname if root_rel == "." else f"{root_rel}/{dirname}")
        ]
        for filename in filenames:
            if len(lines) >= max_files:
                return sorted(lines)
            path = root_path / filename
            try:
                rel = path.relative_to(project_dir).as_posix()
            except Exception:
                continue
            if _should_ignore_path(rel):
                continue
            if path.suffix.lower() in _BINARY_SUFFIXES:
                continue
            lines.append(rel)
    return sorted(lines)


def _safe_project_dir(ws_root: Path, project_root: str) -> Path:
    req_root = str(project_root or ".").strip() or "."
    proj = safe_join(ws_root, req_root)
    if not proj.exists() or not proj.is_dir():
        raise RuntimeError("project_root must exist inside workspace")
    return proj


def _read_package_json(project_dir: Path) -> dict[str, Any]:
    package_json = project_dir / "package.json"
    if not package_json.exists():
        return {}
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _package_manager_hint(project_dir: Path, package_json: dict[str, Any]) -> str:
    package_manager = str(package_json.get("packageManager") or "").strip()
    if package_manager:
        return package_manager
    if (project_dir / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (project_dir / "yarn.lock").exists():
        return "yarn"
    if (project_dir / "bun.lockb").exists() or (project_dir / "bun.lock").exists():
        return "bun"
    if (project_dir / "package-lock.json").exists():
        return "npm"
    return "unknown"


def _resolve_relative_import(source_rel: str, specifier: str, candidates: set[str]) -> str | None:
    if not specifier.startswith("."):
        return None
    raw = (Path(source_rel).parent / specifier).as_posix()
    names: list[str] = []
    if Path(raw).suffix:
        names.append(raw)
    else:
        for suffix in [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".css"]:
            names.append(raw + suffix)
            names.append(f"{raw}/index{suffix}")
    for candidate in names:
        clean = candidate.lstrip("./")
        if clean in candidates:
            return clean
    return None


def _source_candidates(project_dir: Path, *, max_files: int, suffixes: set[str] | None = None) -> list[Path]:
    wanted = suffixes or _SOURCE_SUFFIXES
    return [
        path for path in _walk_candidate_files(project_dir, limit_files=max_files * 4)
        if path.suffix.lower() in wanted
    ][:max_files]


def _line_number(text: str, index: int) -> int:
    return text.count("\n", 0, max(0, index)) + 1


def _line_at(text: str, line: int) -> str:
    lines = text.splitlines()
    if line < 1 or line > len(lines):
        return ""
    return lines[line - 1][:180]


def _line_window_text(path: str, content: str, *, center_line: int, context: int = 4) -> str:
    lines = content.splitlines()
    if not lines:
        return f"FILE: {path}\n(empty file)"
    center_line = max(1, min(int(center_line or 1), len(lines)))
    context = max(0, min(int(context or 4), 40))
    start_line = max(1, center_line - context)
    end_line = min(len(lines), center_line + context)
    width = len(str(end_line))
    rendered = [f"{line_no:>{width}}| {lines[line_no - 1]}" for line_no in range(start_line, end_line + 1)]
    return f"FILE: {path} lines {start_line}-{end_line} of {len(lines)}\n" + "\n".join(rendered)


def _line_for_span(text: str, start: int) -> int:
    return text.count("\n", 0, max(0, start)) + 1


def _with_original_trailing_newline(original: str, candidate: str) -> str:
    if original.endswith("\n") and not candidate.endswith("\n"):
        return candidate + "\n"
    if not original.endswith("\n") and candidate.endswith("\n"):
        return candidate[:-1]
    return candidate


def _write_workspace_text(ws_root: Path, rel_path: str, content: str) -> Path:
    target = safe_join(ws_root, rel_path)
    if target.exists() and not target.is_file():
        raise RuntimeError("path must be a file")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _unified_diff_for_text(path: str, before: str, after: str, *, context: int = 3) -> str:
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    diff = difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile=path,
        tofile=path,
        n=max(0, min(int(context or 3), 12)),
    )
    return "".join(diff)


def _project_relative_file(project_dir: Path, raw_path: str) -> Path:
    clean = str(raw_path or "").strip().lstrip("/")
    if not clean:
        raise RuntimeError("file path is required")
    if clean.startswith("./"):
        clean = clean[2:]
    target = safe_join(project_dir, clean)
    if not target.exists() or not target.is_file():
        raise RuntimeError(f"file does not exist: {clean}")
    return target


def _format_json_text(content: str) -> str:
    parsed = json.loads(content)
    return json.dumps(parsed, ensure_ascii=False, indent=2) + "\n"


def _node_bin(project_dir: Path, name: str) -> Path | None:
    candidates = [
        project_dir / "node_modules" / ".bin" / name,
        Path(__file__).resolve().parents[1] / "node_modules" / ".bin" / name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _available_format_commands(project_dir: Path, *, mode: str, tools: set[str], files: list[str]) -> list[tuple[str, list[str]]]:
    commands: list[tuple[str, list[str]]] = []
    if not files:
        return commands
    prettier = _node_bin(project_dir, "prettier")
    if prettier and "prettier" in tools:
        commands.append(("prettier", [str(prettier), "--write" if mode == "fix" else "--check", *files]))
    eslint = _node_bin(project_dir, "eslint")
    if eslint and "eslint" in tools:
        command = [str(eslint), *(( "--fix",) if mode == "fix" else ()), *files]
        commands.append(("eslint", command))
    if "ruff" in tools:
        ruff = "ruff"
        commands.append(("ruff", [ruff, "check", "--fix" if mode == "fix" else "--no-fix", *files]))
    if "black" in tools:
        black = "black"
        commands.append(("black", [black, *(("--check",) if mode == "check" else ()), *files]))
    return commands


def _safe_sqlite_database_path(project_dir: Path, raw_database: str) -> Path:
    clean = str(raw_database or "").strip().lstrip("/")
    if not clean:
        clean = "app.db"
    if clean.startswith("./"):
        clean = clean[2:]
    if Path(clean).suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
        raise RuntimeError("sqlite database path must end with .db, .sqlite, or .sqlite3")
    return safe_join(project_dir, clean)


def _looks_like_readonly_sql(sql: str) -> bool:
    clean = str(sql or "").strip().lower()
    if not clean:
        return False
    return clean.startswith(("select", "with", "pragma", "explain"))


def _sqlite_schema_rows(conn: sqlite3.Connection) -> tuple[list[str], list[list[Any]]]:
    cursor = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE type IN ('table','index','view','trigger') ORDER BY type, name"
    )
    columns = [item[0] for item in cursor.description or []]
    rows = [list(row) for row in cursor.fetchall()]
    return columns, rows


def _git_safe_paths(project_dir: Path, raw_paths: Any, changed_paths: list[str]) -> list[str]:
    if isinstance(raw_paths, list) and raw_paths:
        candidates = [str(item or "").strip().lstrip("/") for item in raw_paths if str(item or "").strip()]
    else:
        candidates = list(changed_paths)
    safe: list[str] = []
    for rel in candidates:
        if not rel or rel.startswith("../") or "/../" in rel or rel == ".":
            raise RuntimeError(f"unsafe git path: {rel}")
        safe_join(project_dir, rel)
        safe.append(rel)
    return safe


def _span_from_ellipsis_search(content: str, search: str) -> tuple[int, int] | None:
    if "..." not in search:
        return None
    parts = [part for part in search.split("...") if part]
    if not parts:
        return None
    position = 0
    start: int | None = None
    end = 0
    for part in parts:
        found = content.find(part, position)
        if found < 0:
            return None
        if start is None:
            start = found
        end = found + len(part)
        position = end
    if start is None or start >= end:
        return None
    return start, end


def _find_search_replace_span(content: str, search: str, *, allow_fuzzy: bool = True) -> dict[str, Any]:
    if not search:
        return {"ok": False, "error": "search is required"}

    exact_indexes = [match.start() for match in re.finditer(re.escape(search), content)]
    if len(exact_indexes) == 1:
        start = exact_indexes[0]
        return {"ok": True, "strategy": "exact", "start": start, "end": start + len(search), "confidence": 1.0}
    if len(exact_indexes) > 1:
        return {"ok": False, "error": "search matched multiple locations; provide more surrounding context", "matches": len(exact_indexes)}

    whitespace_pattern = re.escape(search)
    whitespace_pattern = re.sub(r"(?:\\ |\s)+", r"\\s+", whitespace_pattern)
    try:
        ws_matches = list(re.finditer(whitespace_pattern, content, re.MULTILINE))
    except re.error:
        ws_matches = []
    if len(ws_matches) == 1:
        match = ws_matches[0]
        return {"ok": True, "strategy": "whitespace-flexible", "start": match.start(), "end": match.end(), "confidence": 0.98}
    if len(ws_matches) > 1:
        return {"ok": False, "error": "whitespace-flexible search matched multiple locations; provide more context", "matches": len(ws_matches)}

    ellipsis_span = _span_from_ellipsis_search(content, search)
    if ellipsis_span:
        return {"ok": True, "strategy": "ellipsis", "start": ellipsis_span[0], "end": ellipsis_span[1], "confidence": 0.92}

    if not allow_fuzzy:
        return {"ok": False, "error": "search text was not found"}

    search_lines = search.splitlines()
    if not search_lines:
        return {"ok": False, "error": "search text was not found"}
    content_lines = content.splitlines(keepends=True)
    window_size = max(1, min(len(search_lines) + 4, 40))
    search_norm = "\n".join(line.strip() for line in search_lines).strip()
    best: tuple[float, int, int] | None = None
    for start_line in range(0, max(1, len(content_lines) - window_size + 1)):
        for size in range(max(1, len(search_lines) - 2), window_size + 1):
            end_line = min(len(content_lines), start_line + size)
            candidate = "".join(content_lines[start_line:end_line])
            candidate_norm = "\n".join(line.strip() for line in candidate.splitlines()).strip()
            score = difflib.SequenceMatcher(None, search_norm, candidate_norm).ratio()
            if best is None or score > best[0]:
                start_offset = sum(len(line) for line in content_lines[:start_line])
                end_offset = sum(len(line) for line in content_lines[:end_line])
                best = (score, start_offset, end_offset)
    if best and best[0] >= 0.88:
        return {"ok": True, "strategy": "fuzzy-line-window", "start": best[1], "end": best[2], "confidence": round(best[0], 3)}
    return {"ok": False, "error": "search text was not found with enough confidence", "confidence": round(best[0], 3) if best else 0}


def _symbol_definitions_for_file(rel: str, text: str, *, limit: int = 40) -> list[dict[str, Any]]:
    suffix = Path(rel).suffix.lower()
    patterns = [_SYMBOL_DEFINITION_RE]
    if suffix == ".py":
        patterns = [_PY_SYMBOL_DEFINITION_RE]
    elif suffix == ".go":
        patterns = [_GO_SYMBOL_DEFINITION_RE]
    elif suffix == ".rs":
        patterns = [_RUST_SYMBOL_DEFINITION_RE]
    elif suffix in {".java", ".kt"}:
        patterns = [_JAVA_SYMBOL_DEFINITION_RE, _SYMBOL_DEFINITION_RE]

    symbols: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for pattern in patterns:
        for match in pattern.finditer(text):
            name = match.group(1) if match.lastindex else ""
            if not name:
                continue
            line = _line_number(text, match.start())
            key = (name, line)
            if key in seen:
                continue
            seen.add(key)
            signature = _line_at(text, line)
            symbols.append({"name": name, "line": line, "signature": signature})
            if len(symbols) >= limit:
                return symbols
    return symbols


def _imports_for_file(rel: str, text: str, candidates: set[str]) -> tuple[list[str], list[str]]:
    internal: list[str] = []
    external: list[str] = []
    for spec in _IMPORT_RE.findall(text):
        resolved = _resolve_relative_import(rel, spec, candidates)
        if resolved:
            internal.append(resolved)
        elif not spec.startswith("."):
            package_name = spec.split("/", 1)[0] if not spec.startswith("@") else "/".join(spec.split("/")[:2])
            external.append(package_name)
    return sorted(dict.fromkeys(internal)), sorted(dict.fromkeys(external))


def _score_repo_map_file(rel: str, text: str, query_tokens: set[str], incoming_count: int) -> int:
    lowered = f"{rel}\n{text[:3000]}".lower()
    score = incoming_count * 4
    score += 8 if rel in {"package.json", "pyproject.toml", "README.md", "PRD.md"} else 0
    score += 6 if Path(rel).name in {"App.tsx", "App.jsx", "main.tsx", "index.ts", "index.tsx"} else 0
    score += sum(3 for token in query_tokens if token and token in lowered)
    score += min(len(_symbol_definitions_for_file(rel, text, limit=30)), 12)
    return score


def _count_emoji_chars(text: str) -> int:
    count = 0
    for char in str(text or ""):
        code = ord(char)
        if (
            0x1F300 <= code <= 0x1FAFF
            or 0x2600 <= code <= 0x27BF
            or 0x2300 <= code <= 0x23FF
        ):
            count += 1
    return count


def _looks_like_component_file(rel: str, text: str) -> bool:
    name = Path(rel).stem
    return (
        (name[:1].isupper() and Path(rel).suffix.lower() in {".tsx", ".jsx"})
        or "React.FC" in text
        or bool(_COMPONENT_EXPORT_RE.search(text))
        or bool(re.search(r"return\s*\(\s*<|=>\s*<", text))
    )


def _quality_signals_for_file(rel: str, text: str) -> tuple[dict[str, bool], list[dict[str, Any]]]:
    lowered = text.lower()
    signals = {
        "responsive": bool(re.search(r"@media\b|clamp\(|minmax\(|grid-template|matchMedia\(|\b(sm|md|lg|xl):", text)),
        "loading_state": bool(re.search(r"\bloading\b|isLoading|pending|skeleton|spinner", text, re.IGNORECASE)),
        "error_state": bool(re.search(r"\berror\b|failed|retry|try again|catch\s*\(", text, re.IGNORECASE)),
        "empty_state": bool(re.search(r"empty state|no results|no items|not found|belum ada|kosong", text, re.IGNORECASE)),
        "a11y_labels": bool(re.search(r"<label\b|htmlFor=|aria-label=|aria-labelledby=", text)),
        "theme_tokens": bool(re.search(r"--[a-z0-9-]+:\s*|var\(--|data-app-theme|ThemeProvider", text, re.IGNORECASE)),
    }
    risks: list[dict[str, Any]] = []
    patterns = [
        ("todo", re.compile(r"\b(TODO|FIXME|HACK)\b")),
        ("console-log", re.compile(r"\bconsole\.(log|debug|warn)\s*\(")),
        ("placeholder", _PLACEHOLDER_RE),
        ("starter-residue", re.compile(r"\b(vite|react \+ vite|seeded template|lorem ipsum|template starter)\b", re.IGNORECASE)),
        ("inline-style", re.compile(r"\bstyle=\{\{")),
        ("unlabeled-button", re.compile(r"<button(?![^>]*(aria-label|aria-labelledby|title=|>[^<A-Za-z0-9]*[A-Za-z0-9]))", re.IGNORECASE)),
        ("dangerous-html", re.compile(r"dangerouslySetInnerHTML")),
        ("any-type", re.compile(r":\s*any\b|as\s+any\b")),
    ]
    for risk, pattern in patterns:
        for match in pattern.finditer(text):
            line = _line_number(text, match.start())
            risks.append({"path": rel, "line": line, "risk": risk, "text": _line_at(text, line)})
            if len(risks) >= 24:
                return signals, risks
    for match in re.finditer(r"(?<![-\w])(?:min-)?width\s*:\s*(\d{3,4})px", text, re.IGNORECASE):
        try:
            width = int(match.group(1))
        except ValueError:
            continue
        if width >= 390:
            line = _line_number(text, match.start())
            risks.append({"path": rel, "line": line, "risk": "mobile-overflow-width", "text": _line_at(text, line)})
            if len(risks) >= 24:
                return signals, risks
    for risk, pattern in (
        ("mobile-overflow-100vw", re.compile(r"(?<![-\w])width\s*:\s*100vw\b", re.IGNORECASE)),
        ("mobile-overflow-max-content", re.compile(r"(?<![-\w])(?:min-)?width\s*:\s*(?:max-content|fit-content)\b", re.IGNORECASE)),
        ("mobile-overflow-nowrap", re.compile(r"\bwhite-space\s*:\s*nowrap\b", re.IGNORECASE)),
    ):
        for match in pattern.finditer(text):
            line = _line_number(text, match.start())
            risks.append({"path": rel, "line": line, "risk": risk, "text": _line_at(text, line)})
            if len(risks) >= 24:
                return signals, risks
    emoji_count = _count_emoji_chars(text)
    if emoji_count > 4:
        risks.append({"path": rel, "line": 1, "risk": "emoji-heavy-ui", "text": f"{emoji_count} emoji-like characters detected"})
    if "onclick=" in lowered and rel.endswith(".html"):
        risks.append({"path": rel, "line": 1, "risk": "inline-handler", "text": "HTML contains inline event handlers"})
    return signals, risks



def execute_local_tool(ws_root: Path, project_dir: Path, *, tool_name: str, arguments: dict[str, Any] | None = None) -> LocalToolCallResult:
    started = time.perf_counter()
    args = arguments if isinstance(arguments, dict) else {}
    name = str(tool_name or "").strip()
    try:
        if name == "repo_list":
            req_root = str(args.get("project_root") or ".").strip() or "."
            max_files = int(args.get("max_files") or 300)
            max_files = max(10, min(max_files, 1500))
            proj = safe_join(ws_root, req_root)
            lines = _repo_tree_lines(proj, max_files=max_files)
            text = "\n".join(lines)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=True, text=text[:6000], raw={"files": lines}, duration_ms=duration_ms)

        if name == "repo_read":
            path = _workspace_or_project_path(ws_root, project_dir, str(args.get("path") or ""))
            if not path:
                raise RuntimeError("path is required")
            max_chars = int(args.get("max_chars") or 20000)
            max_chars = max(1000, min(max_chars, 120_000))
            content = read_text(ws_root, path)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(
                tool=name,
                arguments=args,
                ok=True,
                text=content[:max_chars],
                raw={"path": path, "truncated": len(content) > max_chars},
                duration_ms=duration_ms,
            )

        if name == "repo_read_many":
            raw_paths = args.get("paths")
            if not isinstance(raw_paths, list) or not raw_paths:
                raise RuntimeError("paths is required")
            max_chars_per_file = int(args.get("max_chars_per_file") or 12000)
            max_chars_per_file = max(1000, min(max_chars_per_file, 50_000))
            max_total_chars = int(args.get("max_total_chars") or 50_000)
            max_total_chars = max(4000, min(max_total_chars, 160_000))
            files: list[dict[str, Any]] = []
            chunks: list[str] = []
            used = 0
            for raw_path in raw_paths[:24]:
                path = _workspace_or_project_path(ws_root, project_dir, str(raw_path or ""))
                if not path:
                    continue
                try:
                    content = read_text(ws_root, path)
                except Exception as exc:
                    files.append({"path": path, "ok": False, "error": str(exc)})
                    continue
                remaining = max_total_chars - used
                if remaining <= 0:
                    files.append({"path": path, "ok": False, "error": "total output budget reached"})
                    continue
                clipped = content[: min(max_chars_per_file, remaining)]
                used += len(clipped)
                files.append({"path": path, "ok": True, "chars": len(content), "truncated": len(clipped) < len(content)})
                chunks.append(f"FILE: {path}\n{clipped}")
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=True, text="\n\n".join(chunks), raw={"files": files}, duration_ms=duration_ms)

        if name == "repo_search":
            req_root = str(args.get("project_root") or ".").strip() or "."
            query = str(args.get("query") or "").strip()
            if not query:
                raise RuntimeError("query is required")
            use_regex = bool(args.get("regex") or False)
            max_matches = int(args.get("max_matches") or 120)
            max_matches = max(10, min(max_matches, 400))
            proj = safe_join(ws_root, req_root)
            if not proj.exists() or not proj.is_dir():
                raise RuntimeError("project_root must exist inside workspace")

            pattern = re.compile(query, flags=re.IGNORECASE) if use_regex else None
            matches: list[dict[str, Any]] = []
            for file_path in _walk_candidate_files(proj):
                if len(matches) >= max_matches:
                    break
                try:
                    rel = file_path.relative_to(ws_root).as_posix()
                except Exception:
                    continue
                if _should_ignore_path(rel):
                    continue
                try:
                    text = file_path.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                for idx, line in enumerate(text.splitlines()[:4000], start=1):
                    hit = False
                    if pattern:
                        hit = bool(pattern.search(line))
                    else:
                        hit = query.lower() in line.lower()
                    if not hit:
                        continue
                    matches.append({"path": rel, "line": idx, "text": line[:240]})
                    if len(matches) >= max_matches:
                        break

            preview = "\n".join(f"{m['path']}:{m['line']} {m['text']}" for m in matches[:120])
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=True, text=preview[:6000], raw={"matches": matches}, duration_ms=duration_ms)

        if name == "file_window":
            path = _workspace_or_project_path(ws_root, project_dir, str(args.get("path") or ""))
            if not path:
                raise RuntimeError("path is required")
            center = int(args.get("line") or 1)
            context = int(args.get("context") or 80)
            context = max(1, min(context, 240))
            content = read_text(ws_root, path)
            lines = content.splitlines()
            if not lines:
                duration_ms = int((time.perf_counter() - started) * 1000)
                return LocalToolCallResult(tool=name, arguments=args, ok=True, text=f"FILE: {path}\n(empty file)", raw={"path": path, "total_lines": 0}, duration_ms=duration_ms)
            center = max(1, min(center, len(lines)))
            start_line = max(1, center - context)
            end_line = min(len(lines), center + context)
            width = len(str(end_line))
            rendered = [
                f"{line_no:>{width}}| {lines[line_no - 1]}"
                for line_no in range(start_line, end_line + 1)
            ]
            header = f"FILE: {path} lines {start_line}-{end_line} of {len(lines)}"
            if start_line > 1:
                header += f" ({start_line - 1} lines above)"
            if end_line < len(lines):
                header += f" ({len(lines) - end_line} lines below)"
            text = f"{header}\n" + "\n".join(rendered)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(
                tool=name,
                arguments=args,
                ok=True,
                text=text[:40_000],
                raw={"path": path, "start_line": start_line, "end_line": end_line, "total_lines": len(lines)},
                duration_ms=duration_ms,
            )

        if name == "line_replace_preview":
            path = _workspace_or_project_path(ws_root, project_dir, str(args.get("path") or ""))
            if not path:
                raise RuntimeError("path is required")
            start_line = int(args.get("start_line") or 0)
            end_line = int(args.get("end_line") or 0)
            replacement = str(args.get("replacement") if args.get("replacement") is not None else "")
            context = int(args.get("context") or 4)
            if start_line < 1 or end_line < start_line:
                raise RuntimeError("start_line/end_line must be a valid 1-based inclusive range")
            content = read_text(ws_root, path)
            lines = content.splitlines(keepends=True)
            if end_line > len(lines):
                raise RuntimeError(f"line range exceeds file length ({len(lines)} lines)")
            replacement_text = replacement
            if replacement_text and not replacement_text.endswith("\n") and end_line < len(lines):
                replacement_text += "\n"
            new_content = "".join(lines[: start_line - 1]) + replacement_text + "".join(lines[end_line:])
            new_content = _with_original_trailing_newline(content, new_content)
            payload = {
                "path": path,
                "strategy": "line-range",
                "start_line": start_line,
                "end_line": end_line,
                "suggested_change": {"path": path, "new_content": new_content},
                "note": "Preview only. Return this suggested_change as a final file change/patch if it is the intended edit.",
            }
            window = _line_window_text(path, new_content, center_line=start_line, context=context)
            text = json.dumps({key: value for key, value in payload.items() if key != "suggested_change"}, ensure_ascii=False, indent=2)
            text += "\n\nAFTER WINDOW:\n" + window
            text += "\n\n\"suggested_change\":\n" + json.dumps(payload["suggested_change"], ensure_ascii=False, indent=2)[:20_000]
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=True, text=text[:30_000], raw=payload, duration_ms=duration_ms)

        if name == "line_replace_apply":
            path = _workspace_or_project_path(ws_root, project_dir, str(args.get("path") or ""))
            if not path:
                raise RuntimeError("path is required")
            start_line = int(args.get("start_line") or 0)
            end_line = int(args.get("end_line") or 0)
            replacement = str(args.get("replacement") if args.get("replacement") is not None else "")
            context = int(args.get("context") or 4)
            if start_line < 1 or end_line < start_line:
                raise RuntimeError("start_line/end_line must be a valid 1-based inclusive range")
            content = read_text(ws_root, path)
            lines = content.splitlines(keepends=True)
            if end_line > len(lines):
                raise RuntimeError(f"line range exceeds file length ({len(lines)} lines)")
            replacement_text = replacement
            if replacement_text and not replacement_text.endswith("\n") and end_line < len(lines):
                replacement_text += "\n"
            new_content = "".join(lines[: start_line - 1]) + replacement_text + "".join(lines[end_line:])
            new_content = _with_original_trailing_newline(content, new_content)
            _write_workspace_text(ws_root, path, new_content)
            diff = _unified_diff_for_text(path, content, new_content, context=context)
            payload = {
                "path": path,
                "applied": True,
                "strategy": "line-range",
                "start_line": start_line,
                "end_line": end_line,
                "diff": diff,
            }
            window = _line_window_text(path, new_content, center_line=start_line, context=context)
            text = json.dumps({key: value for key, value in payload.items() if key != "diff"}, ensure_ascii=False, indent=2)
            text += "\n\nDIFF:\n" + diff[:20_000]
            text += "\n\nAFTER WINDOW:\n" + window
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=True, text=text[:30_000], raw=payload, duration_ms=duration_ms)

        if name == "search_replace_preview":
            path = _workspace_or_project_path(ws_root, project_dir, str(args.get("path") or ""))
            search = str(args.get("search") if args.get("search") is not None else "")
            replace = str(args.get("replace") if args.get("replace") is not None else "")
            context = int(args.get("context") or 4)
            allow_fuzzy = bool(args.get("allow_fuzzy", True))
            if not path:
                raise RuntimeError("path is required")
            if not search:
                raise RuntimeError("search is required")
            content = read_text(ws_root, path)
            match = _find_search_replace_span(content, search, allow_fuzzy=allow_fuzzy)
            if not match.get("ok"):
                payload = {"path": path, "ok": False, **match}
                duration_ms = int((time.perf_counter() - started) * 1000)
                return LocalToolCallResult(
                    tool=name,
                    arguments=args,
                    ok=False,
                    text=json.dumps(payload, ensure_ascii=False, indent=2),
                    raw=payload,
                    duration_ms=duration_ms,
                    error=str(match.get("error") or "search text was not found"),
                )
            start = int(match["start"])
            end = int(match["end"])
            new_content = content[:start] + replace + content[end:]
            new_content = _with_original_trailing_newline(content, new_content)
            line = _line_for_span(content, start)
            payload = {
                "path": path,
                "strategy": match.get("strategy"),
                "confidence": match.get("confidence"),
                "start_line": line,
                "suggested_change": {"path": path, "new_content": new_content},
                "note": "Preview only. Return this suggested_change as a final file change/patch if it is the intended edit.",
            }
            window = _line_window_text(path, new_content, center_line=line, context=context)
            text = json.dumps({key: value for key, value in payload.items() if key != "suggested_change"}, ensure_ascii=False, indent=2)
            text += "\n\nAFTER WINDOW:\n" + window
            text += "\n\n\"suggested_change\":\n" + json.dumps(payload["suggested_change"], ensure_ascii=False, indent=2)[:20_000]
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=True, text=text[:30_000], raw=payload, duration_ms=duration_ms)

        if name == "search_replace_apply":
            path = _workspace_or_project_path(ws_root, project_dir, str(args.get("path") or ""))
            search = str(args.get("search") if args.get("search") is not None else "")
            replace = str(args.get("replace") if args.get("replace") is not None else "")
            context = int(args.get("context") or 4)
            allow_fuzzy = bool(args.get("allow_fuzzy", True))
            if not path:
                raise RuntimeError("path is required")
            if not search:
                raise RuntimeError("search is required")
            content = read_text(ws_root, path)
            match = _find_search_replace_span(content, search, allow_fuzzy=allow_fuzzy)
            if not match.get("ok"):
                payload = {"path": path, "ok": False, **match}
                duration_ms = int((time.perf_counter() - started) * 1000)
                return LocalToolCallResult(
                    tool=name,
                    arguments=args,
                    ok=False,
                    text=json.dumps(payload, ensure_ascii=False, indent=2),
                    raw=payload,
                    duration_ms=duration_ms,
                    error=str(match.get("error") or "search text was not found"),
                )
            start = int(match["start"])
            end = int(match["end"])
            new_content = content[:start] + replace + content[end:]
            new_content = _with_original_trailing_newline(content, new_content)
            _write_workspace_text(ws_root, path, new_content)
            line = _line_for_span(content, start)
            diff = _unified_diff_for_text(path, content, new_content, context=context)
            payload = {
                "path": path,
                "applied": True,
                "strategy": match.get("strategy"),
                "confidence": match.get("confidence"),
                "start_line": line,
                "diff": diff,
            }
            window = _line_window_text(path, new_content, center_line=line, context=context)
            text = json.dumps({key: value for key, value in payload.items() if key != "diff"}, ensure_ascii=False, indent=2)
            text += "\n\nDIFF:\n" + diff[:20_000]
            text += "\n\nAFTER WINDOW:\n" + window
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=True, text=text[:30_000], raw=payload, duration_ms=duration_ms)

        if name == "symbol_search":
            req_root = str(args.get("project_root") or ".").strip() or "."
            query = str(args.get("query") or "").strip()
            if not query:
                raise RuntimeError("query is required")
            use_regex = bool(args.get("regex") or False)
            max_matches = int(args.get("max_matches") or 80)
            max_matches = max(5, min(max_matches, 240))
            proj = _safe_project_dir(ws_root, req_root)
            matcher = re.compile(query, re.IGNORECASE) if use_regex else None
            matches: list[dict[str, Any]] = []
            suffixes = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".go", ".rs", ".java", ".kt", ".php", ".rb", ".cs"}
            for file_path in _source_candidates(proj, max_files=600, suffixes=suffixes):
                if len(matches) >= max_matches:
                    break
                rel = file_path.relative_to(proj).as_posix()
                try:
                    content = file_path.read_text(encoding="utf-8", errors="ignore")[:120_000]
                except Exception:
                    continue
                for symbol in _symbol_definitions_for_file(rel, content, limit=80):
                    symbol_name = str(symbol.get("name") or "")
                    hit = bool(matcher.search(symbol_name)) if matcher else query.lower() in symbol_name.lower()
                    if not hit:
                        continue
                    matches.append({
                        "name": symbol_name,
                        "path": rel,
                        "line": symbol.get("line"),
                        "signature": symbol.get("signature"),
                    })
                    if len(matches) >= max_matches:
                        break
            payload = {"project_root": req_root, "query": query, "matches": matches}
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=True, text=text[:16000], raw=payload, duration_ms=duration_ms)

        if name == "style_stack":
            req_root = str(args.get("project_root") or ".").strip() or "."
            max_files = int(args.get("max_files") or 180)
            max_files = max(20, min(max_files, 800))
            proj = _safe_project_dir(ws_root, req_root)
            package_json = _read_package_json(proj)
            deps: dict[str, Any] = {}
            for bucket in ("dependencies", "devDependencies"):
                raw_bucket = package_json.get(bucket)
                if isinstance(raw_bucket, dict):
                    deps.update(raw_bucket)
            dep_names = {str(item) for item in deps.keys()}
            css_files: list[str] = []
            module_css_files: list[str] = []
            styled_components_files: list[str] = []
            utility_class_files: list[str] = []
            token_files: list[str] = []
            css_imports_tailwind = False
            css_custom_properties = False
            class_samples: list[dict[str, Any]] = []
            for file_path in _source_candidates(proj, max_files=max_files, suffixes={".ts", ".tsx", ".js", ".jsx", ".css", ".scss", ".sass", ".less", ".html"}):
                rel = file_path.relative_to(proj).as_posix()
                try:
                    content = file_path.read_text(encoding="utf-8", errors="ignore")[:120_000]
                except Exception:
                    continue
                suffix = file_path.suffix.lower()
                if suffix in {".css", ".scss", ".sass", ".less"}:
                    css_files.append(rel)
                    if ".module." in file_path.name:
                        module_css_files.append(rel)
                    if re.search(r"@import\s+[\"']tailwindcss[\"']|@tailwind\s+(?:base|components|utilities)", content):
                        css_imports_tailwind = True
                    if re.search(r"--[a-z0-9-]+\s*:", content, re.IGNORECASE):
                        css_custom_properties = True
                        token_files.append(rel)
                if re.search(r"styled\.[A-Za-z]+|styled\(", content):
                    styled_components_files.append(rel)
                class_hits = re.findall(r"className\s*=\s*[\"'`]([^\"'`]+)[\"'`]", content)
                utility_hits = [hit for hit in class_hits if _UTILITY_CLASS_RE.search(hit)]
                if utility_hits:
                    utility_class_files.append(rel)
                    for sample in utility_hits[:3]:
                        class_samples.append({"path": rel, "classes": sample[:180]})
            payload = {
                "project_root": req_root,
                "tailwind": (
                    "tailwindcss" in dep_names
                    or "@tailwindcss/vite" in dep_names
                    or "tailwindcss-animate" in dep_names
                    or bool(css_imports_tailwind)
                    or (proj / "tailwind.config.js").exists()
                    or (proj / "tailwind.config.ts").exists()
                ),
                "tailwind_config": [
                    rel for rel in ["tailwind.config.js", "tailwind.config.ts", "postcss.config.js", "postcss.config.cjs"]
                    if (proj / rel).exists()
                ],
                "css_files": css_files[:80],
                "css_modules": bool(module_css_files),
                "css_module_files": module_css_files[:40],
                "styled_components": "styled-components" in dep_names or bool(styled_components_files),
                "styled_components_files": styled_components_files[:40],
                "shadcn": (proj / "components.json").exists() or "class-variance-authority" in dep_names or "tailwind-merge" in dep_names,
                "utility_class_usage": bool(utility_class_files),
                "utility_class_files": sorted(dict.fromkeys(utility_class_files))[:80],
                "class_samples": class_samples[:16],
                "css_custom_properties": css_custom_properties,
                "token_files": sorted(dict.fromkeys(token_files))[:40],
                "recommended_guidance": (
                    "Tailwind/utilities are available; use existing utility conventions and avoid inventing unsupported CSS frameworks."
                    if ("tailwindcss" in dep_names or "@tailwindcss/vite" in dep_names or css_imports_tailwind)
                    else "Do not assume Tailwind utility classes are compiled; prefer existing CSS files/classes or add proper dependency/config deliberately."
                ),
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=True, text=text[:16000], raw=payload, duration_ms=duration_ms)

        if name == "repo_map":
            req_root = str(args.get("project_root") or ".").strip() or "."
            query = str(args.get("query") or "").strip().lower()
            max_files = int(args.get("max_files") or 80)
            max_files = max(10, min(max_files, 260))
            max_symbols_per_file = int(args.get("max_symbols_per_file") or 12)
            max_symbols_per_file = max(1, min(max_symbols_per_file, 40))
            proj = _safe_project_dir(ws_root, req_root)
            query_tokens = {token for token in re.findall(r"[a-zA-Z0-9_:@./-]{2,}", query)}
            suffixes = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".go", ".rs", ".java", ".kt", ".php", ".rb", ".cs", ".css", ".scss", ".html", ".json", ".md"}
            candidates = [
                path for path in _walk_candidate_files(proj, limit_files=max_files * 10)
                if path.suffix.lower() in suffixes or path.name in {"Dockerfile", "Makefile", "Gemfile"}
            ][: max_files * 4]
            rel_candidates = {path.relative_to(proj).as_posix() for path in candidates}
            contents: dict[str, str] = {}
            internal_edges: dict[str, list[str]] = {}
            incoming = Counter()
            external = Counter()
            for file_path in candidates:
                rel = file_path.relative_to(proj).as_posix()
                try:
                    content = file_path.read_text(encoding="utf-8", errors="ignore")[:120_000]
                except Exception:
                    continue
                contents[rel] = content
                internal, external_imports = _imports_for_file(rel, content, rel_candidates)
                if internal:
                    internal_edges[rel] = internal
                    incoming.update(internal)
                external.update(external_imports)
            ranked_rels = sorted(
                contents.keys(),
                key=lambda rel: (_score_repo_map_file(rel, contents[rel], query_tokens, incoming[rel]), -len(rel), rel),
                reverse=True,
            )[:max_files]
            files_payload: list[dict[str, Any]] = []
            for rel in ranked_rels:
                content = contents[rel]
                symbols = _symbol_definitions_for_file(rel, content, limit=max_symbols_per_file)
                files_payload.append({
                    "path": rel,
                    "score": _score_repo_map_file(rel, content, query_tokens, incoming[rel]),
                    "incoming": incoming[rel],
                    "imports": internal_edges.get(rel, [])[:20],
                    "symbols": symbols,
                })
            payload = {
                "project_root": req_root,
                "query": query,
                "files_considered": len(contents),
                "files": files_payload,
                "external_imports": external.most_common(40),
                "note": "Aider-style concise repo map: use this to choose files before calling file_window/repo_read or editing.",
            }
            lines = [f"REPO MAP: {req_root} considered={len(contents)} shown={len(files_payload)}"]
            if external:
                lines.append("External imports: " + ", ".join(f"{pkg}({count})" for pkg, count in external.most_common(12)))
            for item in files_payload:
                lines.append(f"\n{item['path']} score={item['score']} incoming={item['incoming']}")
                imports = item.get("imports") or []
                if imports:
                    lines.append("  imports: " + ", ".join(imports[:12]))
                for symbol in item.get("symbols") or []:
                    lines.append(f"  L{symbol['line']}: {symbol['signature']}")
            text = "\n".join(lines)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=True, text=text[:18000], raw=payload, duration_ms=duration_ms)

        if name == "package_scripts":
            req_root = str(args.get("project_root") or ".").strip() or "."
            proj = _safe_project_dir(ws_root, req_root)
            package_json = _read_package_json(proj)
            scripts = package_json.get("scripts") if isinstance(package_json.get("scripts"), dict) else {}
            deps = package_json.get("dependencies") if isinstance(package_json.get("dependencies"), dict) else {}
            dev_deps = package_json.get("devDependencies") if isinstance(package_json.get("devDependencies"), dict) else {}
            validation_candidates = [script for script in ["typecheck", "check", "lint", "test", "build", "preview", "dev"] if script in scripts]
            raw = {
                "name": package_json.get("name"),
                "package_manager": _package_manager_hint(proj, package_json),
                "scripts": scripts,
                "validation_candidates": validation_candidates,
                "dependencies": sorted(str(name) for name in deps.keys())[:80],
                "devDependencies": sorted(str(name) for name in dev_deps.keys())[:80],
            }
            text = json.dumps(raw, ensure_ascii=False, indent=2)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=True, text=text[:8000], raw=raw, duration_ms=duration_ms)

        if name == "repo_overview":
            req_root = str(args.get("project_root") or ".").strip() or "."
            max_files = int(args.get("max_files") or 500)
            proj = _safe_project_dir(ws_root, req_root)
            files = _repo_tree_lines(proj, max_files=max_files)
            stack = detect_project_stack(proj)
            suffixes = Counter(Path(rel).suffix.lower() or "(none)" for rel in files)
            key_files = [
                rel for rel in files
                if rel in {
                    "package.json", "vite.config.ts", "vite.config.js", "tsconfig.json", "src/App.tsx", "src/main.tsx", "src/app.css",
                    "pyproject.toml", "requirements.txt", "pytest.ini", "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "composer.json",
                    "Gemfile", "Dockerfile", "README.md", "PRD.md",
                }
            ]
            package_json = _read_package_json(proj)
            overview = {
                "project_root": req_root,
                "file_count_sample": len(files),
                "top_extensions": suffixes.most_common(12),
                "key_files": key_files,
                "languages": stack.languages,
                "frameworks": stack.frameworks,
                "runtimes": stack.runtimes,
                "package_manager": _package_manager_hint(proj, package_json),
                "package_name": package_json.get("name"),
                "scripts": sorted((package_json.get("scripts") or {}).keys()) if isinstance(package_json.get("scripts"), dict) else [],
                "sample_files": files[:120],
            }
            text = json.dumps(overview, ensure_ascii=False, indent=2)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=True, text=text[:10000], raw=overview, duration_ms=duration_ms)

        if name == "stack_profile":
            req_root = str(args.get("project_root") or ".").strip() or "."
            proj = _safe_project_dir(ws_root, req_root)
            stack = detect_project_stack(proj)
            payload = {
                "project_root": req_root,
                "languages": stack.languages,
                "frameworks": stack.frameworks,
                "runtimes": stack.runtimes,
                "package_managers": stack.package_managers,
                "validation_files": stack.validation_files,
                "component_libraries": stack.component_libraries,
                "has_playwright": stack.has_playwright,
                "has_headless_browser": stack.has_headless_browser,
                "has_webcontainer": stack.has_webcontainer,
                "has_database_schema": stack.has_database_schema,
                "has_infra": stack.has_infra,
                "has_preview_surface": stack.has_preview_surface,
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=True, text=text[:10000], raw=payload, duration_ms=duration_ms)

        if name == "validation_plan":
            req_root = str(args.get("project_root") or ".").strip() or "."
            proj = _safe_project_dir(ws_root, req_root)
            payload = build_validation_plan(proj, project_root=req_root)
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=True, text=text[:12000], raw=payload, duration_ms=duration_ms)

        if name == "test_runner":
            req_root = str(args.get("project_root") or ".").strip() or "."
            proj = _safe_project_dir(ws_root, req_root)
            timeout = int(args.get("timeout_seconds") or 120)
            timeout = max(5, min(timeout, 300))
            max_output_chars = int(args.get("max_output_chars") or 12000)
            max_output_chars = max(1000, min(max_output_chars, 50_000))
            raw_commands = args.get("commands")
            if isinstance(raw_commands, list) and raw_commands:
                commands = [str(command or "").strip() for command in raw_commands if str(command or "").strip()]
            else:
                plan = build_validation_plan(proj, project_root=req_root)
                commands = [
                    str(item.get("command") or "").strip()
                    for item in list(plan.get("commands") or [])
                    if isinstance(item, dict) and str(item.get("command") or "").strip()
                ][:3]
            if not commands:
                raise RuntimeError("no validation/test commands were provided or inferred")

            results: list[dict[str, Any]] = []
            for command in commands[:5]:
                normalized = _normalize_validation_command(command, req_root)
                cmd_args = _safe_validation_command_args(normalized)
                ran = _run_process(cmd_args, proj, timeout=timeout)
                combined = "\n".join(part for part in [ran.get("stdout", ""), ran.get("stderr", "")] if str(part).strip())
                results.append({
                    "command": normalized,
                    "ok": bool(ran.get("ok")),
                    "returncode": ran.get("returncode"),
                    "stdout": str(ran.get("stdout") or "")[:max_output_chars],
                    "stderr": str(ran.get("stderr") or "")[:max_output_chars],
                    "output_excerpt": combined[:max_output_chars],
                })
            payload = {
                "project_root": req_root,
                "ok": all(bool(item.get("ok")) for item in results),
                "ran": len(results),
                "failed": sum(1 for item in results if not item.get("ok")),
                "results": results,
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=bool(payload["ok"]), text=text[:max_output_chars + 4000], raw=payload, duration_ms=duration_ms)

        if name == "format_lint":
            req_root = str(args.get("project_root") or ".").strip() or "."
            proj = _safe_project_dir(ws_root, req_root)
            mode = str(args.get("mode") or "fix").strip().lower()
            if mode not in {"check", "fix"}:
                raise RuntimeError("mode must be check or fix")
            timeout = int(args.get("timeout_seconds") or 90)
            timeout = max(5, min(timeout, 180))
            raw_files = args.get("files")
            files = [str(item or "").strip().lstrip("/") for item in raw_files if str(item or "").strip()] if isinstance(raw_files, list) else []
            if not files:
                candidates = [
                    path.relative_to(proj).as_posix()
                    for path in _walk_candidate_files(proj, limit_files=80)
                    if path.suffix.lower() in {".json", ".js", ".jsx", ".ts", ".tsx", ".css", ".scss", ".py"}
                ]
                files = candidates[:24]
            files = files[:40]
            raw_tools = args.get("tools")
            requested_tools = {str(item or "").strip().lower() for item in raw_tools if str(item or "").strip()} if isinstance(raw_tools, list) else set()
            tools = requested_tools or {"json", "prettier", "eslint", "ruff", "black"}

            results: list[dict[str, Any]] = []
            changed_paths: list[str] = []
            ok = True

            if "json" in tools:
                for rel in files:
                    if Path(rel).suffix.lower() != ".json":
                        continue
                    target = _project_relative_file(proj, rel)
                    before = target.read_text(encoding="utf-8")
                    try:
                        formatted = _format_json_text(before)
                    except Exception as exc:
                        ok = False
                        results.append({"tool": "json", "file": rel, "ok": False, "error": str(exc)})
                        continue
                    diff = _unified_diff_for_text(rel, before, formatted, context=3)
                    changed = before != formatted
                    if changed and mode == "fix":
                        target.write_text(formatted, encoding="utf-8")
                        changed_paths.append(rel)
                    if changed and mode == "check":
                        ok = False
                    results.append({
                        "tool": "json",
                        "file": rel,
                        "ok": not changed or mode == "fix",
                        "changed": changed,
                        "applied": bool(changed and mode == "fix"),
                        "diff": diff[:12000],
                    })

            command_files = [rel for rel in files if Path(rel).suffix.lower() != ".json" or "json" not in tools]
            command_tools = tools.difference({"json"})
            for tool, command in _available_format_commands(proj, mode=mode, tools=command_tools, files=command_files):
                ran = _run_process(command, proj, timeout=timeout)
                if not ran.get("ok"):
                    ok = False
                results.append({
                    "tool": tool,
                    "command": " ".join(shlex.quote(part) for part in command),
                    "ok": bool(ran.get("ok")),
                    "returncode": ran.get("returncode"),
                    "stdout": str(ran.get("stdout") or "")[:8000],
                    "stderr": str(ran.get("stderr") or "")[:8000],
                })

            payload = {
                "project_root": req_root,
                "mode": mode,
                "ok": ok,
                "applied": bool(changed_paths),
                "changed_paths": changed_paths,
                "results": results,
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=ok, text=text[:30000], raw=payload, duration_ms=duration_ms)

        if name == "database_client":
            req_root = str(args.get("project_root") or ".").strip() or "."
            proj = _safe_project_dir(ws_root, req_root)
            backend = str(args.get("backend") or "sqlite").strip().lower()
            if backend != "sqlite":
                raise RuntimeError("database_client currently supports backend=sqlite only")
            mode = str(args.get("mode") or "query").strip().lower()
            if mode not in {"query", "migrate", "schema"}:
                raise RuntimeError("mode must be one of: query, migrate, schema")
            sql = str(args.get("sql") or "").strip()
            if mode in {"query", "migrate"} and not sql:
                raise RuntimeError("sql is required for query/migrate")
            if mode == "query" and not _looks_like_readonly_sql(sql):
                raise RuntimeError("query mode only accepts read-only SELECT/WITH/PRAGMA/EXPLAIN SQL; use mode=migrate for writes")
            max_rows = int(args.get("max_rows") or 100)
            max_rows = max(1, min(max_rows, 500))
            db_path = _safe_sqlite_database_path(proj, str(args.get("database") or "app.db"))
            db_path.parent.mkdir(parents=True, exist_ok=True)
            rows: list[list[Any]] = []
            columns: list[str] = []
            writes_performed = False
            conn = sqlite3.connect(str(db_path))
            try:
                if mode == "schema":
                    columns, rows = _sqlite_schema_rows(conn)
                elif mode == "query":
                    cursor = conn.execute(sql)
                    columns = [item[0] for item in cursor.description or []]
                    rows = [list(row) for row in cursor.fetchmany(max_rows)]
                else:
                    conn.executescript(sql)
                    conn.commit()
                    writes_performed = True
                    columns, rows = _sqlite_schema_rows(conn)
            finally:
                conn.close()
            payload = {
                "project_root": req_root,
                "backend": "sqlite",
                "database": db_path.relative_to(proj).as_posix(),
                "mode": mode,
                "ok": True,
                "writes_performed": writes_performed,
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "truncated": bool(mode == "query" and len(rows) >= max_rows),
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=True, text=text[:30000], raw=payload, duration_ms=duration_ms)

        if name == "git_manager":
            req_root = str(args.get("project_root") or ".").strip() or "."
            mode = str(args.get("mode") or "status").strip() or "status"
            message = str(args.get("message") or "Update project").strip() or "Update project"
            branch = str(args.get("branch") or "").strip()
            allow_remote = bool(args.get("allow_remote") or False)
            max_diff_chars = int(args.get("max_diff_chars") or 12000)
            max_diff_chars = max(1000, min(max_diff_chars, 80_000))
            if mode not in {"status", "diff", "commit_plan", "branch", "commit", "push", "pr_plan"}:
                raise RuntimeError("mode must be one of: status, diff, commit_plan, branch, commit, push, pr_plan")
            if mode in {"push", "pr_plan"} and not allow_remote:
                raise RuntimeError("remote git modes require allow_remote=true")
            proj = _safe_project_dir(ws_root, req_root)
            inside = _run_process(["git", "rev-parse", "--is-inside-work-tree"], proj, timeout=10)
            if not inside.get("ok") or str(inside.get("stdout") or "").strip() != "true":
                raise RuntimeError("project_root is not inside a git worktree")
            writes_performed = False
            operation_results: list[dict[str, Any]] = []
            if mode == "branch":
                if not re.fullmatch(r"[A-Za-z0-9._/-]{1,120}", branch) or branch.startswith("-") or ".." in branch:
                    raise RuntimeError("branch must be a safe git branch name")
                ran = _run_process(["git", "switch", "-c", branch], proj, timeout=20)
                operation_results.append({"command": f"git switch -c {branch}", **ran})
                writes_performed = bool(ran.get("ok"))
            elif mode == "commit":
                pre_status = _run_process(["git", "status", "--short"], proj, timeout=10)
                pre_changed = _extract_git_status_paths(str(pre_status.get("stdout") or ""))
                safe_paths = _git_safe_paths(proj, args.get("paths"), pre_changed)
                if not safe_paths:
                    raise RuntimeError("no changed paths to commit")
                add = _run_process(["git", "add", "--", *safe_paths], proj, timeout=20)
                operation_results.append({"command": "git add -- " + " ".join(shlex.quote(path) for path in safe_paths), **add})
                if add.get("ok"):
                    commit = _run_process(["git", "commit", "-m", message], proj, timeout=60)
                    operation_results.append({"command": f"git commit -m {shlex.quote(message)}", **commit})
                    writes_performed = bool(commit.get("ok"))
            elif mode == "push":
                push = _run_process(["git", "push"], proj, timeout=120)
                operation_results.append({"command": "git push", **push})
                writes_performed = bool(push.get("ok"))
            status = _run_process(["git", "status", "--short"], proj, timeout=10)
            diff_stat = _run_process(["git", "diff", "--stat"], proj, timeout=10)
            diff: dict[str, Any] | None = None
            if mode in {"diff", "commit_plan", "commit"}:
                diff = _run_process(["git", "diff", "--"], proj, timeout=20)
            changed_paths = _extract_git_status_paths(str(status.get("stdout") or ""))
            quoted_paths = " ".join(shlex.quote(path) for path in changed_paths)
            command_plan = []
            if mode == "commit_plan":
                command_plan = [
                    f"git add {quoted_paths}" if quoted_paths else "git add <paths>",
                    f"git commit -m {shlex.quote(message)}",
                ]
            payload = {
                "project_root": req_root,
                "mode": mode,
                "ok": bool(status.get("ok")),
                "dirty": bool(changed_paths),
                "changed_paths": changed_paths,
                "status": str(status.get("stdout") or ""),
                "diff_stat": str(diff_stat.get("stdout") or ""),
                "diff": str((diff or {}).get("stdout") or "")[:max_diff_chars] if diff is not None else "",
                "command_plan": command_plan,
                "operation_results": operation_results,
                "writes_performed": writes_performed,
            }
            if mode == "pr_plan":
                payload["command_plan"] = ["git push -u origin HEAD", "open pull request in GitHub/GitLab UI or MCP connector"]
            if operation_results and not all(bool(item.get("ok")) for item in operation_results):
                payload["ok"] = False
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=bool(payload["ok"]), text=text[:max_diff_chars + 5000], raw=payload, duration_ms=duration_ms)

        if name == "docs_browser":
            url = _validate_public_docs_url(str(args.get("url") or ""))
            max_chars = int(args.get("max_chars") or 12000)
            max_chars = max(1000, min(max_chars, 60_000))
            req = Request(
                url,
                headers={
                    "User-Agent": "ApporaDocsBrowser/0.1",
                    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
                },
                method="GET",
            )
            with urlopen(req, timeout=12) as response:  # nosec B310 - docs_browser validates public HTTPS URL shape.
                raw = response.read(min(max_chars * 4, 500_000))
                final_url = response.geturl()
            decoded = raw.decode("utf-8", errors="ignore")
            title, clean_text = _clean_docs_html(decoded, max_chars=max_chars)
            payload = {
                "url": url,
                "final_url": final_url,
                "title": title,
                "text": clean_text,
                "truncated": len(clean_text) >= max_chars,
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=True, text=text[:max_chars + 2000], raw=payload, duration_ms=duration_ms)

        if name == "skill_catalog":
            req_root = str(args.get("project_root") or ".").strip() or "."
            query = str(args.get("query") or "").strip().lower()
            limit = int(args.get("limit") or 12)
            limit = max(1, min(limit, 40))
            proj = _safe_project_dir(ws_root, req_root)
            warnings: list[str] = []
            skills = list_imported_skills(ws_root, proj, warnings=warnings)
            try:
                stack = detect_project_stack(proj, warnings=warnings)
            except Exception:
                stack = None
            query_tokens = {token for token in re.findall(r"[a-zA-Z0-9_:@./-]{2,}", query)}
            if stack:
                query_tokens.update(str(item).lower() for item in [*stack.languages, *stack.frameworks, *stack.runtimes, *stack.component_libraries] if item)
                if stack.has_preview_surface:
                    query_tokens.update({"frontend", "ui", "preview", "web", "app"})
            if any(token in query_tokens for token in {"dashboard", "landing", "ui", "ux", "frontend", "react", "vite", "tsx", "page", "website", "web"}):
                query_tokens.update({"frontend", "react", "vite", "ui", "component", "layout", "responsive", "app"})

            ws_resolved = ws_root.resolve()
            proj_resolved = proj.resolve()

            def score(skill: Any) -> tuple[int, int, int, str]:
                hay = f"{skill.skill_id} {skill.title} {skill.description} {skill.body[:800]}".lower()
                overlap = sum(1 for token in query_tokens if token in hay)
                title_overlap = sum(1 for token in query_tokens if token in f"{skill.skill_id} {skill.title}".lower())
                try:
                    source_path = Path(str(skill.source)).resolve()
                    local_boost = 2 if source_path.is_relative_to(proj_resolved) else (1 if source_path.is_relative_to(ws_resolved) else 0)
                except Exception:
                    local_boost = 0
                return local_boost, title_overlap, overlap, skill.skill_id

            ranked = sorted(skills, key=score, reverse=True)
            if query_tokens:
                matched = [skill for skill in ranked if score(skill)[2] > 0]
                ranked = matched
            payload = {
                "project_root": req_root,
                "count": len(skills),
                "matched_count": len(ranked),
                "skills": [
                    {
                        "skill_id": skill.skill_id,
                        "title": skill.title,
                        "description": str(skill.description or skill.body[:240]).replace("\n", " ")[:360],
                        "provider": skill.provider,
                        "source": skill.source,
                        "match_score": score(skill)[:3],
                    }
                    for skill in ranked[:limit]
                ],
                "warnings": warnings[:8],
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=True, text=text[:5000], raw=payload, duration_ms=duration_ms)

        if name == "skill_read":
            req_root = str(args.get("project_root") or ".").strip() or "."
            skill_id = str(args.get("skill_id") or "").strip()
            if not skill_id:
                raise RuntimeError("skill_id is required")
            max_chars = int(args.get("max_chars") or 10000)
            max_chars = max(1000, min(max_chars, 30_000))
            proj = _safe_project_dir(ws_root, req_root)
            warnings: list[str] = []
            skill = read_imported_skill(ws_root, proj, skill_id, warnings=warnings)
            if not skill:
                raise RuntimeError(f"Skill '{skill_id}' not found")
            payload = {
                "skill_id": skill.skill_id,
                "title": skill.title,
                "description": skill.description,
                "provider": skill.provider,
                "source": skill.source,
                "body": skill.body[:max_chars],
                "truncated": len(skill.body) > max_chars,
                "warnings": warnings[:8],
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=True, text=text[: max_chars + 2000], raw=payload, duration_ms=duration_ms)

        if name == "dependency_graph":
            req_root = str(args.get("project_root") or ".").strip() or "."
            max_files = int(args.get("max_files") or 180)
            proj = _safe_project_dir(ws_root, req_root)
            candidates = [
                path for path in _walk_candidate_files(proj, limit_files=max_files * 3)
                if path.suffix.lower() in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
            ][:max_files]
            rel_candidates = {path.relative_to(proj).as_posix() for path in candidates}
            graph: dict[str, list[str]] = {}
            external = Counter()
            for file_path in candidates:
                rel = file_path.relative_to(proj).as_posix()
                try:
                    content = file_path.read_text(encoding="utf-8", errors="ignore")[:80_000]
                except Exception:
                    continue
                imports: list[str] = []
                for spec in _IMPORT_RE.findall(content):
                    resolved = _resolve_relative_import(rel, spec, rel_candidates)
                    if resolved:
                        imports.append(resolved)
                    elif not spec.startswith("."):
                        package_name = spec.split("/", 1)[0] if not spec.startswith("@") else "/".join(spec.split("/")[:2])
                        external.update([package_name])
                if imports:
                    graph[rel] = sorted(dict.fromkeys(imports))
            payload = {
                "project_root": req_root,
                "files_scanned": len(candidates),
                "internal_edges": sum(len(value) for value in graph.values()),
                "external_imports": external.most_common(40),
                "graph": dict(list(graph.items())[:120]),
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=True, text=text[:14000], raw=payload, duration_ms=duration_ms)

        if name == "component_index":
            req_root = str(args.get("project_root") or ".").strip() or "."
            max_files = int(args.get("max_files") or 220)
            proj = _safe_project_dir(ws_root, req_root)
            components: list[dict[str, Any]] = []
            hooks: list[dict[str, Any]] = []
            types: list[dict[str, Any]] = []
            component_files: list[str] = []
            for file_path in _source_candidates(proj, max_files=max_files, suffixes={".ts", ".tsx", ".js", ".jsx"}):
                rel = file_path.relative_to(proj).as_posix()
                try:
                    content = file_path.read_text(encoding="utf-8", errors="ignore")[:100_000]
                except Exception:
                    continue
                if _looks_like_component_file(rel, content):
                    component_files.append(rel)
                for match in _COMPONENT_EXPORT_RE.finditer(content):
                    comp = match.group(1) or match.group(2)
                    if comp:
                        components.append({"name": comp, "path": rel, "line": _line_number(content, match.start())})
                for match in _HOOK_RE.finditer(content):
                    hooks.append({"name": match.group(1), "path": rel, "line": _line_number(content, match.start())})
                for match in _TYPE_RE.finditer(content):
                    types.append({"name": match.group(1), "path": rel, "line": _line_number(content, match.start())})
            payload = {
                "project_root": req_root,
                "component_files": component_files[:120],
                "components": components[:160],
                "hooks": hooks[:80],
                "types": types[:100],
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=True, text=text[:14000], raw=payload, duration_ms=duration_ms)

        if name == "route_map":
            req_root = str(args.get("project_root") or ".").strip() or "."
            max_files = int(args.get("max_files") or 220)
            proj = _safe_project_dir(ws_root, req_root)
            routes: list[dict[str, Any]] = []
            router_files: list[str] = []
            nav_links: list[dict[str, Any]] = []
            for file_path in _source_candidates(proj, max_files=max_files, suffixes={".ts", ".tsx", ".js", ".jsx", ".html"}):
                rel = file_path.relative_to(proj).as_posix()
                try:
                    content = file_path.read_text(encoding="utf-8", errors="ignore")[:120_000]
                except Exception:
                    continue
                if re.search(r"react-router-dom|createBrowserRouter|<Route\b|RouterProvider|@tanstack/react-router", content):
                    router_files.append(rel)
                for match in _ROUTE_RE.finditer(content):
                    value = match.group(1)
                    if not value or not value.startswith(("/", "#")):
                        continue
                    item = {"path": value, "file": rel, "line": _line_number(content, match.start())}
                    if "href=" in match.group(0) or "to=" in match.group(0):
                        nav_links.append(item)
                    else:
                        routes.append(item)
            payload = {
                "project_root": req_root,
                "router_files": sorted(dict.fromkeys(router_files))[:80],
                "routes": routes[:160],
                "nav_links": nav_links[:160],
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=True, text=text[:12000], raw=payload, duration_ms=duration_ms)

        if name == "quality_scan":
            req_root = str(args.get("project_root") or ".").strip() or "."
            max_files = int(args.get("max_files") or 260)
            proj = _safe_project_dir(ws_root, req_root)
            aggregate = {
                "responsive": False,
                "loading_state": False,
                "error_state": False,
                "empty_state": False,
                "a11y_labels": False,
                "theme_tokens": False,
            }
            risks: list[dict[str, Any]] = []
            scanned = 0
            for file_path in _source_candidates(proj, max_files=max_files, suffixes=_FRONTEND_SUFFIXES):
                rel = file_path.relative_to(proj).as_posix()
                try:
                    content = file_path.read_text(encoding="utf-8", errors="ignore")[:120_000]
                except Exception:
                    continue
                scanned += 1
                signals, file_risks = _quality_signals_for_file(rel, content)
                for key, value in signals.items():
                    aggregate[key] = aggregate[key] or value
                risks.extend(file_risks)
                if len(risks) >= 80:
                    risks = risks[:80]
                    break
            payload = {
                "project_root": req_root,
                "files_scanned": scanned,
                "signals": aggregate,
                "missing_signals": [key for key, value in aggregate.items() if not value],
                "risk_counts": dict(Counter(str(item.get("risk") or "unknown") for item in risks)),
                "risks": risks[:80],
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=True, text=text[:14000], raw=payload, duration_ms=duration_ms)

        if name == "memory_overview":
            from api.agent_memory import get_agent_memory_overview, get_agent_memory_chunks_table_status, has_supabase

            req_root = str(args.get("project_root") or ".").strip() or "."
            overview = get_agent_memory_overview(ws_root, project_root=req_root)
            supabase_enabled = has_supabase()
            supabase_rag_status = get_agent_memory_chunks_table_status() if supabase_enabled else "unconfigured"
            payload = {
                "project_root": req_root,
                "session_entries": overview.session_entries,
                "project_entries": overview.project_entries,
                "latest_session_ts": overview.latest_session_ts,
                "latest_project_ts": overview.latest_project_ts,
                "has_project_profile": overview.has_project_profile,
                "project_profile_updated_at": overview.project_profile_updated_at,
                "retrieval_backend": "supabase-hash-vector-chunks" if supabase_rag_status == "ready" else "local-hash-vector-chunks",
                "supabase_enabled": supabase_enabled,
                "supabase_rag_status": supabase_rag_status,
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=True, text=text[:8000], raw=payload, duration_ms=duration_ms)

        if name == "mcp_status":
            from api.agent_mcp import discover_mcp_servers, list_mcp_tools

            req_root = str(args.get("project_root") or ".").strip() or "."
            include_live_tools = bool(args.get("include_live_tools") or False)
            proj = _safe_project_dir(ws_root, req_root)
            warnings: list[str] = []
            servers = discover_mcp_servers(ws_root, proj, warnings=warnings)
            tool_catalog = list_mcp_tools(ws_root, proj, refresh=False, warnings=warnings) if include_live_tools and servers else {}
            payload = {
                "project_root": req_root,
                "servers": [
                    {
                        "name": server.name,
                        "transport": server.transport,
                        "target": server.target,
                        "tools_declared": list(server.tools or [])[:24],
                        "source": server.source,
                        "live_tools": [
                            {"name": tool.name, "description": tool.description[:180]}
                            for tool in (tool_catalog.get(server.name) or [])[:24]
                        ],
                    }
                    for server in servers
                ],
                "warnings": warnings[:8],
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=True, text=text[:10000], raw=payload, duration_ms=duration_ms)

        if name == "preview_capabilities":
            req_root = str(args.get("project_root") or ".").strip() or "."
            proj = _safe_project_dir(ws_root, req_root)
            package_json = _read_package_json(proj)
            scripts = package_json.get("scripts") if isinstance(package_json.get("scripts"), dict) else {}
            preview_scripts = [name for name in ["dev", "preview", "start", "build"] if name in scripts]
            entries = [
                rel
                for rel in ["index.html", "src/main.tsx", "src/main.jsx", "src/App.tsx", "src/App.jsx", "vite.config.ts", "vite.config.js"]
                if (proj / rel).exists()
            ]
            deps = {}
            for bucket in ("dependencies", "devDependencies"):
                raw = package_json.get(bucket)
                if isinstance(raw, dict):
                    deps.update(raw)
            dep_names = {str(name) for name in deps.keys()}
            payload = {
                "project_root": req_root,
                "has_package_json": bool(package_json),
                "package_manager": _package_manager_hint(proj, package_json),
                "preview_scripts": preview_scripts,
                "scripts": scripts,
                "entry_candidates": entries,
                "has_static_index": (proj / "index.html").exists(),
                "likely_vite": "vite" in dep_names or (proj / "vite.config.ts").exists() or (proj / "vite.config.js").exists(),
                "likely_next": "next" in dep_names,
                "can_attempt_preview": bool(preview_scripts or (proj / "index.html").exists()),
                "recommended_action": (
                    "Use Appora preview start/audit after file changes."
                    if preview_scripts or (proj / "index.html").exists()
                    else "No obvious preview surface yet; create package scripts or index.html first."
                ),
            }
            text = json.dumps(payload, ensure_ascii=False, indent=2)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=True, text=text[:10000], raw=payload, duration_ms=duration_ms)

        raise RuntimeError(f"Unknown local tool: {name}")

    except Exception as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        return LocalToolCallResult(tool=name or tool_name, arguments=args, ok=False, text="", raw={}, duration_ms=duration_ms, error=str(exc))


def format_local_tools_prompt() -> str:
    lines = [
        "LOCAL TOOLS:",
        "These tools run inside this backend, no external MCP server required.",
        "If you need one, return an action like {\"type\": \"tool\", \"tool\": \"repo_search\", \"arguments\": { ... }}.",
        "Most tools are read-only. Tools ending in `_apply` write workspace files and return a diff; use them only after reading enough context.",
        "Use local tools before MCP for repo-local facts. Use MCP only for external systems or integrations not represented in the local workspace.",
    ]
    for tool in _LOCAL_TOOLS:
        lines.append(f"- {tool.name}: {tool.description}")
    return "\n".join(lines)


def format_local_tool_results_prompt(results: list[LocalToolCallResult]) -> str:
    if not results:
        return ""
    lines = ["LOCAL TOOL RESULTS:"]
    for res in results:
        status = "ok" if res.ok else "error"
        args = res.arguments or {}
        arg_preview = (json.dumps(args, ensure_ascii=False)[:180] + "…") if len(json.dumps(args, ensure_ascii=False)) > 180 else json.dumps(args, ensure_ascii=False)
        lines.append(f"- {res.tool} ({status}, {res.duration_ms}ms) args={arg_preview}")
        if res.text:
            lines.append(res.text[:6000])
        if res.error:
            lines.append(f"ERROR: {res.error}"[:500])
        lines.append("")
    return "\n".join(lines).strip()
