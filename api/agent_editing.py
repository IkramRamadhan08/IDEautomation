from __future__ import annotations

import difflib
import re
from typing import Any


_EXPLICIT_REWRITE_RE = re.compile(r"\b(rewrite|rombak|rebuild|replace all|hapus semua|ulang dari awal|bongkar)\b", re.IGNORECASE)


def _line_delta(old: str, new: str) -> tuple[int, int, int]:
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    inserted = 0
    deleted = 0
    replaced = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "insert":
            inserted += j2 - j1
        elif tag == "delete":
            deleted += i2 - i1
        elif tag == "replace":
            replaced += max(i2 - i1, j2 - j1)
    return inserted, deleted, replaced


def assess_edit_strategy(path: str, old_content: str, new_content: str, *, user_input: str = "") -> dict[str, Any]:
    old = str(old_content or "")
    new = str(new_content or "")
    if not old:
        return {
            "path": path,
            "classification": "new-file",
            "score": 100,
            "changed_line_ratio": 1.0,
            "similarity": 0.0,
            "warnings": [],
        }

    old_lines = old.splitlines()
    new_lines = new.splitlines()
    similarity = difflib.SequenceMatcher(None, old, new).ratio()
    inserted, deleted, replaced = _line_delta(old, new)
    changed_lines = inserted + deleted + replaced
    changed_line_ratio = changed_lines / max(1, len(old_lines))
    size_ratio = len(new_lines) / max(1, len(old_lines))
    explicit_rewrite = bool(_EXPLICIT_REWRITE_RE.search(user_input or ""))

    warnings: list[str] = []
    classification = "surgical"
    score = 100

    if changed_line_ratio <= 0.05 and 0.8 <= size_ratio <= 1.2:
        classification = "surgical"
        score = 100
    elif changed_line_ratio > 0.85 or (similarity < 0.25 and changed_line_ratio > 0.45):
        classification = "rewrite"
        score = 30
    elif changed_line_ratio > 0.45 or (similarity < 0.55 and changed_line_ratio > 0.18):
        classification = "broad-edit"
        score = 60
    elif changed_line_ratio > 0.18:
        classification = "moderate-edit"
        score = 78

    if len(old_lines) >= 80 and classification in {"rewrite", "broad-edit"} and not explicit_rewrite:
        warnings.append(
            f"{path} changed {round(changed_line_ratio * 100)}% of existing lines; prefer unified patch/search-replace unless the task explicitly asks for a rewrite."
        )
        score = min(score, 55)
    if len(old_lines) >= 40 and size_ratio < 0.35 and not explicit_rewrite:
        warnings.append(f"{path} shrank from {len(old_lines)} to {len(new_lines)} lines without explicit delete/rewrite intent.")
        score = min(score, 45)
    if classification == "surgical" and changed_lines <= 12:
        score = max(score, 92)

    return {
        "path": path,
        "classification": classification,
        "score": max(0, min(100, score)),
        "changed_lines": changed_lines,
        "old_lines": len(old_lines),
        "new_lines": len(new_lines),
        "changed_line_ratio": round(changed_line_ratio, 4),
        "similarity": round(similarity, 4),
        "warnings": warnings,
    }


def summarize_edit_strategy(assessments: list[dict[str, Any]]) -> dict[str, Any]:
    if not assessments:
        return {"ok": True, "score": 100, "summary": "No existing-file edits to assess.", "warnings": [], "assessments": []}
    score = min(int(item.get("score") or 0) for item in assessments)
    warnings: list[str] = []
    for item in assessments:
        warnings.extend(str(warning) for warning in list(item.get("warnings") or []) if str(warning).strip())
    classes = {}
    for item in assessments:
        key = str(item.get("classification") or "unknown")
        classes[key] = classes.get(key, 0) + 1
    ok = not warnings
    summary = ", ".join(f"{key}={value}" for key, value in sorted(classes.items()))
    return {
        "ok": ok,
        "score": score,
        "summary": summary or "Edit strategy assessed.",
        "warnings": warnings[:8],
        "assessments": assessments,
    }
