from __future__ import annotations

import time
from typing import Any


def _payload_from_event(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    if isinstance(payload, dict):
        return dict(payload)
    data = event.get("data")
    if isinstance(data, dict):
        return dict(data)
    return {}


def _event_type(event: dict[str, Any]) -> str:
    return str(event.get("event_type") or event.get("event") or "status")


def _short_text(value: Any, max_chars: int = 2000) -> str:
    text = str(value or "").strip()
    return text if len(text) <= max_chars else f"{text[:max_chars]}...[truncated]"


def _event_created_at(event: dict[str, Any]) -> Any:
    return event.get("created_at") or event.get("t") or event.get("timestamp")


def normalize_agent_event(event: dict[str, Any], index: int) -> dict[str, Any]:
    event_type = _event_type(event)
    payload = _payload_from_event(event)
    phase = str(payload.get("phase") or "stream")
    tool = payload.get("tool") or payload.get("name")
    command = payload.get("command")
    message = payload.get("message") or payload.get("summary") or payload.get("text") or payload.get("spoken_chunk")
    ok = payload.get("ok")
    if event_type == "error":
        ok = False
    return {
        "seq": index + 1,
        "event": event_type,
        "phase": phase,
        "kind": str(payload.get("kind") or event_type),
        "tool": str(tool) if tool is not None else None,
        "command": str(command) if command is not None else None,
        "ok": ok if isinstance(ok, bool) else None,
        "message": _short_text(message, 500),
        "created_at": _event_created_at(event),
    }


def _command_key(item: dict[str, Any]) -> tuple[str, str, int]:
    payload = _payload_from_event(item)
    return (
        str(payload.get("phase") or ""),
        str(payload.get("command") or ""),
        int(payload.get("index") or 0),
    )


def _summarize_commands(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    commands: dict[tuple[str, str, int], dict[str, Any]] = {}
    for index, event in enumerate(events):
        event_type = _event_type(event)
        payload = _payload_from_event(event)
        if not payload.get("command"):
            continue
        if payload.get("kind") not in {"agent_harness_command", "agent_harness_command_chunk"}:
            continue
        key = _command_key(event)
        command = commands.setdefault(
            key,
            {
                "seq_first": index + 1,
                "seq_last": index + 1,
                "phase": str(payload.get("phase") or ""),
                "tool": str(payload.get("tool") or ""),
                "group": str(payload.get("group") or ""),
                "command": str(payload.get("command") or ""),
                "ok": None,
                "returncode": None,
                "stdout_preview": "",
                "stderr_preview": "",
                "chunk_count": 0,
                "summary": "",
            },
        )
        command["seq_last"] = index + 1
        if event_type == "tool_output" and payload.get("kind") == "agent_harness_command_chunk":
            command["chunk_count"] = int(command.get("chunk_count") or 0) + 1
        if isinstance(payload.get("ok"), bool):
            command["ok"] = bool(payload.get("ok"))
        if payload.get("returncode") is not None:
            command["returncode"] = payload.get("returncode")
        if payload.get("stdout_preview"):
            command["stdout_preview"] = _short_text(payload.get("stdout_preview"), 1000)
        if payload.get("stderr_preview"):
            command["stderr_preview"] = _short_text(payload.get("stderr_preview"), 1000)
        if payload.get("summary"):
            command["summary"] = _short_text(payload.get("summary"), 500)
    return list(commands.values())


def build_agent_observability(
    events: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    result: dict[str, Any] | None = None,
    started_at: float | None = None,
    finished_at: float | None = None,
) -> dict[str, Any]:
    event_list = [event for event in events if isinstance(event, dict)]
    timeline = [normalize_agent_event(event, index) for index, event in enumerate(event_list)]
    commands = _summarize_commands(event_list)
    phase_counts: dict[str, int] = {}
    for item in timeline:
        phase = str(item.get("phase") or "stream")
        phase_counts[phase] = phase_counts.get(phase, 0) + 1

    result = result if isinstance(result, dict) else {}
    changes = result.get("changes") if isinstance(result.get("changes"), list) else []
    actions = result.get("actions") if isinstance(result.get("actions"), list) else []
    execution = result.get("execution") if isinstance(result.get("execution"), dict) else {}
    failed_commands = [item for item in commands if item.get("ok") is False]
    error_events = [item for item in timeline if item.get("event") == "error" or item.get("ok") is False]
    failure_points = []
    for item in failed_commands:
        detail = item.get("stderr_preview") or item.get("stdout_preview") or item.get("summary") or item.get("command")
        failure_points.append({
            "kind": "command",
            "phase": item.get("phase"),
            "command": item.get("command"),
            "detail": _short_text(detail, 1000),
        })
    for item in error_events:
        if item.get("event") == "tool_output" and item.get("command"):
            continue
        failure_points.append({
            "kind": str(item.get("event") or "event"),
            "phase": item.get("phase"),
            "detail": _short_text(item.get("message"), 1000),
        })

    recovered_failure_points: list[dict[str, Any]] = []
    if started_at is not None:
        finished = finished_at if finished_at is not None else time.time()
        duration_ms = max(0, int((finished - started_at) * 1000))
    else:
        duration_ms = None

    execution_ok = execution.get("ok") if isinstance(execution.get("ok"), bool) else None
    if execution_ok is True and failure_points:
        recovered_failure_points = list(failure_points)
        failure_points = []

    ok = not failure_points
    if execution_ok is False:
        ok = False
    elif execution_ok is True and not failure_points:
        ok = True

    summary = {
        "event_count": len(timeline),
        "phase_counts": phase_counts,
        "tool_call_count": sum(1 for item in timeline if item.get("event") == "tool_call"),
        "tool_output_count": sum(1 for item in timeline if item.get("event") == "tool_output"),
        "command_count": len(commands),
        "failed_command_count": len(failed_commands),
        "recovered_failure_count": len(recovered_failure_points),
        "changes": len(changes),
        "actions": len(actions),
        "execution_ok": execution_ok,
        "duration_ms": duration_ms,
    }
    return {
        "ok": ok,
        "summary": summary,
        "timeline": timeline,
        "commands": commands,
        "failure_points": failure_points,
        "recovered_failure_points": recovered_failure_points,
    }
