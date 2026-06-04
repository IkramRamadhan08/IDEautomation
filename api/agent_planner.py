from __future__ import annotations

import re
from typing import Any


_LARGE_TASK_RE = re.compile(
    r"\b("
    r"rombak|besar|besar-besaran|full|production|produksi|siap produksi|migrate|migrasi|refactor|"
    r"deploy|database|auth|billing|multi|dashboard|app|saas|agent|autonomous"
    r")\b",
    re.IGNORECASE,
)


def _unique_files(base_plan: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for item in base_plan:
        files = item.get("files") if isinstance(item, dict) else None
        if not isinstance(files, list):
            continue
        for file in files:
            rel = str(file or "").strip()
            if rel and rel not in out:
                out.append(rel)
    return out[:12]


def _checkpoint(checkpoint_id: str, title: str, detail: str, status: str = "pending", files: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": checkpoint_id,
        "title": title,
        "detail": detail[:260],
        "status": status,
        "files": list(files or [])[:8],
    }


def build_long_horizon_plan(
    *,
    goal: str,
    user_input: str,
    base_plan: list[dict[str, Any]],
    intent_kind: str,
    should_write_files: bool,
    is_full_agent: bool,
    project_root: str,
) -> dict[str, Any]:
    clean_goal = re.sub(r"\s+", " ", str(goal or user_input or "Handle the current task.")).strip()[:260]
    task_text = f"{goal} {user_input}"
    complexity = "high" if is_full_agent or _LARGE_TASK_RE.search(task_text) or len(base_plan) >= 5 else "medium"
    files = _unique_files(base_plan)
    enabled = bool(should_write_files)

    checkpoints = [
        _checkpoint(
            "context",
            "Context and constraints",
            "Map repo shape, relevant files, stack, user constraints, memory, skills, and available tools before changing code.",
            "current" if enabled else "done",
            files[:4],
        )
    ]
    if should_write_files:
        checkpoints.extend(
            [
                _checkpoint(
                    "implementation",
                    "Coherent implementation",
                    "Make the smallest coherent file set that solves the product/code goal without unrelated rewrites.",
                    "pending",
                    files,
                ),
                _checkpoint(
                    "validation",
                    "Validation and repair",
                    "Run or request the strongest available build/test/lint/typecheck/preview checks, then repair failures.",
                    "pending",
                ),
                _checkpoint(
                    "handoff",
                    "Handoff summary",
                    "Summarize what changed, what was verified, and any remaining blocker or follow-up.",
                    "pending",
                ),
            ]
        )
    else:
        checkpoints.append(
            _checkpoint(
                "answer",
                "Read-only answer",
                "Answer from evidence without writing files or executing project-changing commands.",
                "current",
                files[:6],
            )
        )

    if should_write_files and (is_full_agent or _FRONTEND_HINT_RE.search(task_text)):
        checkpoints.insert(
            len(checkpoints) - 1,
            _checkpoint(
                "preview_quality",
                "Preview quality",
                "Check visible UX depth, responsive behavior, broken runtime, placeholder residue, and interaction integrity.",
                "pending",
            ),
        )

    completion_criteria = [
        "User intent and project boundary are reflected in the implementation.",
        "Changed files are coherent with imports, routes, styles, and state.",
    ]
    if should_write_files:
        completion_criteria.extend(
            [
                "Validation/build/test evidence is requested or recorded when available.",
                "Blocking verifier, runtime, or preview issues are repaired or explicitly reported.",
            ]
        )
    else:
        completion_criteria.append("No file writes or shell actions are produced for read-only intent.")

    risk_register = [
        "Missing credentials or external service access may block live integration.",
        "Large UI changes can regress responsive layout without preview evidence.",
    ] if should_write_files else ["Answer quality depends on the files and tool evidence loaded into context."]

    return {
        "enabled": enabled,
        "goal": clean_goal,
        "project_root": str(project_root or ".").strip() or ".",
        "intent": str(intent_kind or ""),
        "complexity": complexity,
        "status": "planned" if enabled else "read_only",
        "current_checkpoint": checkpoints[0]["id"] if checkpoints else "",
        "checkpoints": checkpoints,
        "completion_criteria": completion_criteria,
        "risk_register": risk_register,
    }


_FRONTEND_HINT_RE = re.compile(r"\b(ui|ux|preview|landing|dashboard|website|web|frontend|react|vite|page|tampilan|halaman)\b", re.IGNORECASE)


def update_long_horizon_progress(
    horizon: dict[str, Any] | None,
    *,
    changes_count: int,
    actions_count: int,
    blocking_checks: list[str],
) -> dict[str, Any]:
    if not isinstance(horizon, dict) or not horizon:
        return {}
    updated = dict(horizon)
    checkpoints = [dict(item) for item in list(updated.get("checkpoints") or []) if isinstance(item, dict)]
    blocking = [str(item) for item in blocking_checks if str(item).strip()]
    has_work = changes_count > 0 or actions_count > 0

    def set_status(checkpoint_id: str, status: str) -> None:
        for item in checkpoints:
            if item.get("id") == checkpoint_id:
                item["status"] = status
                return

    set_status("context", "done")
    if not updated.get("enabled"):
        set_status("answer", "done")
        updated.update({"status": "completed", "current_checkpoint": "answer", "checkpoints": checkpoints})
        return updated

    if blocking:
        if has_work:
            set_status("implementation", "done")
            set_status("validation", "blocked")
            current = "validation"
        else:
            set_status("implementation", "blocked")
            current = "implementation"
        updated.update({
            "status": "blocked",
            "current_checkpoint": current,
            "blocking_checks": blocking[:8],
            "checkpoints": checkpoints,
        })
        return updated

    if has_work:
        set_status("implementation", "done")
        set_status("validation", "current")
        current = "validation"
        status = "ready_for_execution"
    else:
        set_status("implementation", "current")
        current = "implementation"
        status = "planned"

    updated.update({
        "status": status,
        "current_checkpoint": current,
        "blocking_checks": [],
        "checkpoints": checkpoints,
    })
    return updated
