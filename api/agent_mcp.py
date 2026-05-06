from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import json
import os
import time
from typing import Any

from anyio import BrokenResourceError
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client


@dataclass(frozen=True)
class MCPServerInfo:
    name: str
    transport: str
    target: str
    tools: list[str]
    source: str
    enabled: bool = True
    command: str | None = None
    args: list[str] | None = None
    cwd: str | None = None
    env: dict[str, str] | None = None
    headers: dict[str, str] | None = None
    timeout_seconds: float = 30.0


@dataclass(frozen=True)
class MCPToolInfo:
    server: str
    name: str
    description: str
    input_schema: dict[str, Any]
    source: str


@dataclass(frozen=True)
class MCPToolCallResult:
    server: str
    tool: str
    arguments: dict[str, Any]
    ok: bool
    text: str
    raw: dict[str, Any]
    duration_ms: int
    error: str | None = None


_TOOL_CACHE_TTL_SECONDS = 45.0
_LIVE_TOOL_CACHE: dict[str, tuple[float, list[MCPToolInfo]]] = {}


def _candidate_configs(ws_root: Path, project_dir: Path) -> list[Path]:
    out: list[Path] = []
    for path in [
        ws_root / ".voiceide" / "mcp.json",
        project_dir / ".voiceide" / "mcp.json",
        project_dir / "mcp.json",
    ]:
        if path.exists() and path.is_file():
            out.append(path)
    return out


def _normalize_dict_of_str(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    out: dict[str, str] = {}
    for key, value in raw.items():
        key_str = str(key or "").strip()
        if not key_str:
            continue
        out[key_str] = str(value or "")
    return out or None


def _resolve_cwd(raw_cwd: str | None, project_dir: Path, ws_root: Path) -> str | None:
    cwd = str(raw_cwd or "").strip()
    if not cwd:
        return None
    candidate = Path(cwd)
    if not candidate.is_absolute():
        candidate = (project_dir / candidate).resolve()
    try:
        candidate.relative_to(ws_root.resolve())
    except Exception:
        return str(project_dir)
    return str(candidate)


def _normalize_server(name: str, raw: dict[str, Any], source: str, *, project_dir: Path, ws_root: Path) -> MCPServerInfo | None:
    if not isinstance(raw, dict):
        return None
    enabled = bool(raw.get("enabled", True))
    command = str(raw.get("command") or "").strip()
    url = str(raw.get("url") or "").strip()
    args = [str(arg) for arg in raw.get("args", [])] if isinstance(raw.get("args"), list) else []
    tools = [str(item).strip() for item in (raw.get("tools") or []) if str(item).strip()]
    env = _normalize_dict_of_str(raw.get("env"))
    headers = _normalize_dict_of_str(raw.get("headers"))
    try:
        timeout_seconds = float(raw.get("timeout_seconds") or 30.0)
    except Exception:
        timeout_seconds = 30.0
    timeout_seconds = max(3.0, min(120.0, timeout_seconds))
    cwd = _resolve_cwd(str(raw.get("cwd") or "").strip() or None, project_dir, ws_root)

    if command:
        target = " ".join([command, *args[:6]]).strip()
        transport = "stdio"
    elif url:
        target = url
        transport = "http"
    else:
        return None
    return MCPServerInfo(
        name=name,
        transport=transport,
        target=target,
        tools=tools[:24],
        source=source,
        enabled=enabled,
        command=command or None,
        args=args[:24] or None,
        cwd=cwd,
        env=env,
        headers=headers,
        timeout_seconds=timeout_seconds,
    )


def discover_mcp_servers(ws_root: Path, project_dir: Path, *, warnings: list[str] | None = None) -> list[MCPServerInfo]:
    servers: list[MCPServerInfo] = []
    seen: set[tuple[str, str]] = set()
    for config_path in _candidate_configs(ws_root, project_dir):
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            if warnings is not None:
                warnings.append(f"MCP config '{config_path.name}' nggak kebaca ({exc}).")
            continue
        raw_servers = payload.get("servers") if isinstance(payload, dict) else None
        if not isinstance(raw_servers, dict):
            if warnings is not None:
                warnings.append(f"MCP config '{config_path.name}' nggak punya object 'servers' yang valid.")
            continue
        for name, raw in raw_servers.items():
            server = _normalize_server(str(name), raw, str(config_path), project_dir=project_dir, ws_root=ws_root)
            if not server or not server.enabled:
                continue
            key = (server.name, server.target)
            if key in seen:
                continue
            seen.add(key)
            servers.append(server)
    return servers


def _server_cache_key(server: MCPServerInfo) -> str:
    return f"{server.name}|{server.transport}|{server.target}"


def _resolve_server(ws_root: Path, project_dir: Path, server_name: str) -> MCPServerInfo:
    wanted = str(server_name or "").strip().lower()
    for server in discover_mcp_servers(ws_root, project_dir):
        if server.name.strip().lower() == wanted:
            return server
    raise RuntimeError(f"MCP server '{server_name}' is not configured for this workspace")


async def _list_tools_async(server: MCPServerInfo) -> list[MCPToolInfo]:
    if server.transport == "stdio":
        if not server.command:
            raise RuntimeError(f"MCP stdio server '{server.name}' is missing a command")
        params = StdioServerParameters(
            command=server.command,
            args=list(server.args or []),
            env={**os.environ, **(server.env or {})},
            cwd=server.cwd,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
    elif server.transport == "http":
        async with streamablehttp_client(
            server.target,
            headers=server.headers or None,
            timeout=server.timeout_seconds,
            sse_read_timeout=max(server.timeout_seconds, 60.0),
        ) as (read, write, _get_session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
    else:
        raise RuntimeError(f"Unsupported MCP transport: {server.transport}")

    tools: list[MCPToolInfo] = []
    for tool in result.tools:
        schema = tool.inputSchema if isinstance(tool.inputSchema, dict) else {}
        tools.append(
            MCPToolInfo(
                server=server.name,
                name=str(tool.name),
                description=str(tool.description or "").strip(),
                input_schema=schema,
                source=server.source,
            )
        )
    return tools


def list_mcp_tools(ws_root: Path, project_dir: Path, *, refresh: bool = False, warnings: list[str] | None = None) -> dict[str, list[MCPToolInfo]]:
    now = time.time()
    out: dict[str, list[MCPToolInfo]] = {}
    for server in discover_mcp_servers(ws_root, project_dir, warnings=warnings):
        key = _server_cache_key(server)
        cached = _LIVE_TOOL_CACHE.get(key)
        if not refresh and cached and (now - cached[0]) < _TOOL_CACHE_TTL_SECONDS:
            out[server.name] = cached[1]
            continue
        try:
            tools = asyncio.run(_list_tools_async(server))
        except Exception as exc:
            if warnings is not None:
                warnings.append(f"Live MCP tools buat server '{server.name}' gagal di-load ({exc}).")
            tools = []
        _LIVE_TOOL_CACHE[key] = (now, tools)
        out[server.name] = tools
    return out


def _stringify_tool_payload(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    parts: list[str] = []
    if isinstance(content, list):
        for item in content[:8]:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").strip().lower()
            if item_type == "text" and str(item.get("text") or "").strip():
                parts.append(str(item.get("text") or "").strip())
            elif item_type == "resource_link":
                uri = str(item.get("uri") or "").strip()
                name = str(item.get("name") or uri or "resource").strip()
                if uri:
                    parts.append(f"{name}: {uri}")
            elif item_type == "image" and item.get("mimeType"):
                parts.append(f"[image result: {item.get('mimeType')}]")

    structured = payload.get("structuredContent")
    if isinstance(structured, (dict, list)) and not parts:
        try:
            parts.append(json.dumps(structured, ensure_ascii=False))
        except Exception:
            parts.append(str(structured))

    if not parts:
        try:
            parts.append(json.dumps(payload, ensure_ascii=False))
        except Exception:
            parts.append(str(payload))

    text = "\n".join(part for part in parts if part).strip()
    return text[:6000]


def _is_ignorable_exit_error(exc: BaseException) -> bool:
    if isinstance(exc, BrokenResourceError):
        return True
    nested = getattr(exc, "exceptions", None)
    if isinstance(nested, tuple) and nested:
        return all(_is_ignorable_exit_error(item) for item in nested)
    return False


async def _call_tool_async(server: MCPServerInfo, tool_name: str, arguments: dict[str, Any]) -> MCPToolCallResult:
    started = time.perf_counter()
    payload: dict[str, Any] | None = None

    async def _capture_result(session: ClientSession) -> None:
        nonlocal payload
        await session.list_tools()
        result = await asyncio.wait_for(session.call_tool(tool_name, arguments=arguments), timeout=server.timeout_seconds)
        payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else {}

    try:
        if server.transport == "stdio":
            if not server.command:
                raise RuntimeError(f"MCP stdio server '{server.name}' is missing a command")
            params = StdioServerParameters(
                command=server.command,
                args=list(server.args or []),
                env={**os.environ, **(server.env or {})},
                cwd=server.cwd,
            )
            try:
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        await _capture_result(session)
            except BaseException as exc:
                if payload is None or not _is_ignorable_exit_error(exc):
                    raise
        elif server.transport == "http":
            async with streamablehttp_client(
                server.target,
                headers=server.headers or None,
                timeout=server.timeout_seconds,
                sse_read_timeout=max(server.timeout_seconds, 60.0),
            ) as (read, write, _get_session_id):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await _capture_result(session)
        else:
            raise RuntimeError(f"Unsupported MCP transport: {server.transport}")

        payload = payload or {}
        duration_ms = int((time.perf_counter() - started) * 1000)
        ok = not bool(payload.get("isError") or payload.get("is_error"))
        text = _stringify_tool_payload(payload)
        return MCPToolCallResult(
            server=server.name,
            tool=tool_name,
            arguments=arguments,
            ok=ok,
            text=text,
            raw=payload,
            duration_ms=duration_ms,
            error=None if ok else (text or "MCP tool returned an error result"),
        )
    except Exception as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        return MCPToolCallResult(
            server=server.name,
            tool=tool_name,
            arguments=arguments,
            ok=False,
            text="",
            raw=payload or {},
            duration_ms=duration_ms,
            error=str(exc),
        )


def execute_mcp_tool(
    ws_root: Path,
    project_dir: Path,
    *,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> MCPToolCallResult:
    server = _resolve_server(ws_root, project_dir, server_name)
    args = arguments if isinstance(arguments, dict) else {}
    return asyncio.run(_call_tool_async(server, tool_name, args))


def _tool_required_arguments(schema: dict[str, Any]) -> list[str]:
    required = schema.get("required") if isinstance(schema, dict) else None
    if not isinstance(required, list):
        return []
    return [str(item).strip() for item in required if str(item).strip()]


def suggest_mcp_actions(query: str, tool_catalog: dict[str, list[MCPToolInfo]] | None = None, *, limit: int = 2) -> list[dict[str, Any]]:
    query_text = str(query or "").strip().lower()
    if not query_text or not tool_catalog:
        return []

    query_bits = {bit for bit in [
        "audit" if any(token in query_text for token in ["audit", "review", "inspect", "check", "validate"]) else "",
        "browser" if any(token in query_text for token in ["ui", "layout", "responsive", "a11y", "preview", "browser", "dom"]) else "",
        "search" if any(token in query_text for token in ["search", "find", "lookup", "trace"]) else "",
        "logs" if any(token in query_text for token in ["error", "log", "console", "fail"]) else "",
    ] if bit}
    if not query_bits:
        return []

    candidates: list[tuple[float, dict[str, Any]]] = []
    for server_name, tools in tool_catalog.items():
        for tool in tools:
            required = _tool_required_arguments(tool.input_schema)
            if required:
                continue
            hay = f"{tool.name} {tool.description}".lower()
            score = 0.0
            if any(token in hay for token in ["audit", "inspect", "check", "validate", "axe", "lighthouse"]):
                score += 1.2 if "audit" in query_bits else 0.35
            if any(token in hay for token in ["browser", "playwright", "dom", "snapshot", "screenshot"]):
                score += 1.0 if "browser" in query_bits else 0.25
            if any(token in hay for token in ["search", "read", "fetch", "list", "query", "lookup"]):
                score += 0.9 if "search" in query_bits else 0.2
            if any(token in hay for token in ["log", "console", "trace"]):
                score += 0.8 if "logs" in query_bits else 0.15
            if score <= 0.0:
                continue
            candidates.append((score, {
                "type": "mcp",
                "server": server_name,
                "tool": tool.name,
                "arguments": {},
            }))

    candidates.sort(key=lambda item: item[0], reverse=True)
    actions: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for _score_value, action in candidates:
        key = (str(action["server"]), str(action["tool"]))
        if key in seen:
            continue
        seen.add(key)
        actions.append(action)
        if len(actions) >= max(1, min(limit, 4)):
            break
    return actions


def format_mcp_prompt(servers: list[MCPServerInfo], tool_catalog: dict[str, list[MCPToolInfo]] | None = None) -> str:
    if not servers:
        return ""
    lines = [
        "REGISTERED MCP INTEGRATIONS:",
        "These integrations exist for this workspace. Use MCP only when external project context or tool-backed lookup would materially improve the answer.",
        "If you need MCP before finalizing, return an action like {\"type\": \"mcp\", \"server\": \"name\", \"tool\": \"tool_name\", \"arguments\": {}} and leave changes empty until the tool result comes back.",
    ]
    live_catalog = tool_catalog or {}
    for server in servers:
        declared = ", ".join(server.tools) if server.tools else "(tool list not declared)"
        lines.append(f"- {server.name} via {server.transport} [{server.source}] -> {server.target}\n  declared tools: {declared}")
        live_tools = live_catalog.get(server.name) or []
        if live_tools:
            preview = []
            for tool in live_tools[:8]:
                label = tool.name
                if tool.description:
                    label += f": {tool.description[:100]}"
                preview.append(label)
            lines.append("  live tools: " + " | ".join(preview))
    return "\n".join(lines)


def format_mcp_results_prompt(results: list[MCPToolCallResult]) -> str:
    if not results:
        return ""
    lines = [
        "MCP TOOL RESULTS:",
        "These are real tool results retrieved during the agent loop. Use them directly in the next reasoning pass.",
    ]
    for result in results:
        status = "ok" if result.ok else "error"
        lines.append(
            f"- {result.server}.{result.tool} ({status}, {result.duration_ms}ms)\n"
            f"  arguments: {json.dumps(result.arguments, ensure_ascii=False)}\n"
            f"  result: {(result.text or result.error or '').strip()[:4000]}"
        )
    return "\n".join(lines)
