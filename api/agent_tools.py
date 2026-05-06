from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def _walk_candidate_files(project_dir: Path, *, limit_files: int = 1400) -> list[Path]:
    out: list[Path] = []
    for path in project_dir.rglob("*"):
        if len(out) >= limit_files:
            break
        try:
            rel = path.relative_to(project_dir).as_posix()
        except Exception:
            continue
        if _should_ignore_path(rel):
            if path.is_dir():
                # rglob still walks into it, but cheap early skip isn't available without custom walk
                continue
        if path.is_file():
            if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".zip", ".pdf", ".ttf", ".woff", ".woff2"}:
                continue
            out.append(path)
    return out


def _repo_tree_lines(project_dir: Path, *, max_files: int = 300) -> list[str]:
    max_files = max(10, min(int(max_files or 300), 2000))
    lines: list[str] = []
    count = 0
    for path in project_dir.rglob("*"):
        if count >= max_files:
            break
        try:
            rel = path.relative_to(project_dir).as_posix()
        except Exception:
            continue
        if _should_ignore_path(rel):
            continue
        if path.is_dir():
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".zip", ".pdf", ".ttf", ".woff", ".woff2"}:
            continue
        lines.append(rel)
        count += 1
    return sorted(lines)



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
            path = str(args.get("path") or "").strip().lstrip("/")
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

        raise RuntimeError(f"Unknown local tool: {name}")

    except Exception as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        return LocalToolCallResult(tool=name or tool_name, arguments=args, ok=False, text="", raw={}, duration_ms=duration_ms, error=str(exc))


def format_local_tools_prompt() -> str:
    lines = [
        "LOCAL TOOLS (read-only):",
        "These tools run inside this backend, no external MCP server required.",
        "If you need one, return an action like {\"type\": \"tool\", \"tool\": \"repo_search\", \"arguments\": { ... }}.",
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
