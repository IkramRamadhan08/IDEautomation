from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
import os
from collections import Counter
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
                path = str(raw_path or "").strip().lstrip("/")
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
            suffixes = Counter(Path(rel).suffix.lower() or "(none)" for rel in files)
            key_files = [
                rel for rel in files
                if rel in {"package.json", "vite.config.ts", "vite.config.js", "tsconfig.json", "src/App.tsx", "src/main.tsx", "src/app.css", "README.md", "PRD.md"}
            ]
            package_json = _read_package_json(proj)
            overview = {
                "project_root": req_root,
                "file_count_sample": len(files),
                "top_extensions": suffixes.most_common(12),
                "key_files": key_files,
                "package_manager": _package_manager_hint(proj, package_json),
                "package_name": package_json.get("name"),
                "scripts": sorted((package_json.get("scripts") or {}).keys()) if isinstance(package_json.get("scripts"), dict) else [],
                "sample_files": files[:120],
            }
            text = json.dumps(overview, ensure_ascii=False, indent=2)
            duration_ms = int((time.perf_counter() - started) * 1000)
            return LocalToolCallResult(tool=name, arguments=args, ok=True, text=text[:10000], raw=overview, duration_ms=duration_ms)

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
