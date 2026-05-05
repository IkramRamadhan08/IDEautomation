from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
from typing import Any


@dataclass(frozen=True)
class MCPServerInfo:
    name: str
    transport: str
    target: str
    tools: list[str]
    source: str
    enabled: bool = True


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


def _normalize_server(name: str, raw: dict[str, Any], source: str) -> MCPServerInfo | None:
    if not isinstance(raw, dict):
        return None
    enabled = bool(raw.get("enabled", True))
    command = str(raw.get("command") or "").strip()
    url = str(raw.get("url") or "").strip()
    args = raw.get("args") if isinstance(raw.get("args"), list) else []
    tools = [str(item).strip() for item in (raw.get("tools") or []) if str(item).strip()]
    if command:
        target = " ".join([command, *[str(arg) for arg in args[:6]]]).strip()
        transport = "stdio"
    elif url:
        target = url
        transport = "http"
    else:
        return None
    return MCPServerInfo(name=name, transport=transport, target=target, tools=tools[:12], source=source, enabled=enabled)


def discover_mcp_servers(ws_root: Path, project_dir: Path) -> list[MCPServerInfo]:
    servers: list[MCPServerInfo] = []
    seen: set[tuple[str, str]] = set()
    for config_path in _candidate_configs(ws_root, project_dir):
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        raw_servers = payload.get("servers") if isinstance(payload, dict) else None
        if not isinstance(raw_servers, dict):
            continue
        for name, raw in raw_servers.items():
            server = _normalize_server(str(name), raw, str(config_path))
            if not server or not server.enabled:
                continue
            key = (server.name, server.target)
            if key in seen:
                continue
            seen.add(key)
            servers.append(server)
    return servers


def format_mcp_prompt(servers: list[MCPServerInfo]) -> str:
    if not servers:
        return ""
    lines = [
        "REGISTERED MCP INTEGRATIONS:",
        "These integrations exist for this workspace. Treat them as available capability boundaries and prefer matching your plan to them when relevant.",
    ]
    for server in servers:
        tools = ", ".join(server.tools) if server.tools else "(tool list not declared)"
        lines.append(f"- {server.name} via {server.transport} [{server.source}] -> {server.target}\n  tools: {tools}")
    return "\n".join(lines)
