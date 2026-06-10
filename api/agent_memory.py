from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import json
import math
import os
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .app_state import CURRENT_SESSION_ID, CURRENT_USER_ID
from .storage.supabase import get_agent_memory_chunks_table_status, get_latest_agent_job_result, has_supabase, list_agent_memory_chunks, upsert_agent_memory_chunks

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_:-]{2,}")
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "your", "you", "are", "was", "were",
    "yang", "dan", "untuk", "dari", "atau", "dengan", "ini", "itu", "saat", "jadi", "agar", "buat",
    "bisa", "lebih", "kaya", "seperti", "karena", "supaya", "lagi", "udah", "akan", "tetap",
}
_MAX_DOC_SOURCE_CHARS = 80_000
_DOC_CHUNK_CHARS = 1_100
_DOC_CHUNK_OVERLAP_CHARS = 180
_MAX_DOC_CHUNKS_PER_SOURCE = 24
_VECTOR_DIMS = 96
_VECTOR_CACHE: dict[str, list[float]] = {}


@dataclass
class MemoryHit:
    kind: str
    source: str
    title: str
    text: str
    score: float


@dataclass(frozen=True)
class MemoryChunk:
    source: str
    title: str
    text: str
    chunk_index: int
    chunk_count: int
    content_hash: str
    updated_at: str
    embedding: list[float] | None = None
    embedding_backend: str = "hash"


@dataclass
class AgentMemoryBundle:
    short_term: list[MemoryHit]
    long_term: list[MemoryHit]
    warnings: list[str] = field(default_factory=list)
    backend: str = "local-hash-vector-chunks"

    @property
    def prompt(self) -> str:
        sections: list[str] = []
        if self.short_term:
            lines = ["SHORT-TERM MEMORY (recent runs / working context):"]
            for hit in self.short_term:
                lines.append(f"- {hit.title} [{hit.source}]\n  {hit.text}")
            sections.append("\n".join(lines))
        if self.long_term:
            lines = [f"LONG-TERM MEMORY ({self.backend}):"]
            for hit in self.long_term:
                lines.append(f"- {hit.title} [{hit.source}]\n  {hit.text}")
            sections.append("\n".join(lines))
        return "\n\n".join(sections)


@dataclass(frozen=True)
class AgentMemoryOverview:
    session_entries: int
    project_entries: int
    latest_session_ts: int | None
    latest_project_ts: int | None
    has_project_profile: bool = False
    project_profile_updated_at: int | None = None


def _tokenize(text: str) -> set[str]:
    out: set[str] = set()
    for token in _TOKEN_RE.findall((text or "").lower()):
        if token in _STOPWORDS or token.isdigit():
            continue
        out.add(token)
    return out


def _score(query_tokens: set[str], text: str, freshness: float = 0.0) -> float:
    if not text.strip():
        return 0.0
    hay = _tokenize(text)
    if not hay:
        return 0.0
    overlap = query_tokens & hay
    if not overlap:
        return 0.0
    density = len(overlap) / max(1.0, len(query_tokens))
    focus = len(overlap) / max(6.0, min(60.0, float(len(hay))))
    return density * 3.0 + focus + freshness


def _hash_vector(text: str, *, dims: int = _VECTOR_DIMS) -> list[float]:
    tokens = [token for token in _TOKEN_RE.findall((text or "").lower()) if token and not token.isdigit()]
    if not tokens:
        return [0.0] * dims

    vec = [0.0] * dims
    weighted_tokens: list[tuple[str, float]] = []
    for token in tokens:
        weight = 0.35 if token in _STOPWORDS else 1.0
        weighted_tokens.append((token, weight))
    for index in range(len(tokens) - 1):
        pair = f"{tokens[index]}::{tokens[index + 1]}"
        weighted_tokens.append((pair, 0.7))

    for token, weight in weighted_tokens:
        digest = hashlib.sha1(token.encode("utf-8", errors="ignore")).digest()
        idx_a = int.from_bytes(digest[0:2], "big") % dims
        idx_b = int.from_bytes(digest[2:4], "big") % dims
        sign_a = 1.0 if digest[4] % 2 else -1.0
        sign_b = 1.0 if digest[5] % 2 else -1.0
        vec[idx_a] += weight * sign_a
        vec[idx_b] += (weight * 0.55) * sign_b

    norm = math.sqrt(sum(value * value for value in vec))
    if norm <= 1e-9:
        return [0.0] * dims
    return [round(value / norm, 6) for value in vec]


def _embed_text_cached(text: str, *, cache_key: str) -> list[float]:
    cached = _VECTOR_CACHE.get(cache_key)
    if cached is not None:
        return cached
    vector = _hash_vector(text)
    _VECTOR_CACHE[cache_key] = vector
    return vector


def _real_embedding_model() -> str:
    return str(os.getenv("AGENT_MEMORY_EMBEDDING_MODEL") or "text-embedding-3-small").strip() or "text-embedding-3-small"


def _real_embedding_backend_ready() -> bool:
    return bool(str(os.getenv("OPENAI_API_KEY") or "").strip())


def _normalize_real_embedding(raw: Any) -> list[float] | None:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    if not isinstance(raw, list):
        return None
    out: list[float] = []
    for item in raw:
        try:
            out.append(float(item))
        except Exception:
            return None
    return out if out else None


def _embed_real_texts(texts: list[str]) -> list[list[float]]:
    clean = [str(text or "")[:8_000] for text in texts]
    if not clean:
        return []
    api_key = str(os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return []
    body = json.dumps({"model": _real_embedding_model(), "input": clean}).encode("utf-8")
    req = Request(
        "https://api.openai.com/v1/embeddings",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return []
    rows = payload.get("data") if isinstance(payload, dict) else []
    vectors: list[list[float]] = []
    for row in rows if isinstance(rows, list) else []:
        vector = _normalize_real_embedding(row.get("embedding") if isinstance(row, dict) else None)
        if vector:
            vectors.append(vector)
    return vectors if len(vectors) == len(clean) else []


def _with_real_embeddings(chunks: list[MemoryChunk]) -> list[MemoryChunk]:
    if not chunks or not _real_embedding_backend_ready():
        return chunks
    vectors = _embed_real_texts([f"{chunk.title}\n{chunk.text}" for chunk in chunks])
    if len(vectors) != len(chunks):
        return chunks
    return [
        MemoryChunk(
            source=chunk.source,
            title=chunk.title,
            text=chunk.text,
            chunk_index=chunk.chunk_index,
            chunk_count=chunk.chunk_count,
            content_hash=chunk.content_hash,
            updated_at=chunk.updated_at,
            embedding=vectors[index],
            embedding_backend="real",
        )
        for index, chunk in enumerate(chunks)
    ]


def _cosine_similarity(left: list[float] | None, right: list[float] | None) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right))


def _memory_root(ws_root: Path) -> Path:
    return ws_root / ".voiceide" / "agent-memory"


def _session_memory_path(ws_root: Path, *, user_id: str, session_id: str) -> Path:
    return _memory_root(ws_root) / "short" / user_id / f"{session_id}.jsonl"


def _project_memory_key(project_root: str) -> str:
    raw = str(project_root or ".").strip() or "."
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw).strip("-.")
    return cleaned[:120] or "workspace-root"


def _project_memory_path(ws_root: Path, *, user_id: str, project_root: str) -> Path:
    return _memory_root(ws_root) / "project-short" / user_id / f"{_project_memory_key(project_root)}.jsonl"


def _project_profile_path(ws_root: Path, *, user_id: str, project_root: str) -> Path:
    return _memory_root(ws_root) / "project-profile" / user_id / f"{_project_memory_key(project_root)}.json"


def _safe_project_dir(ws_root: Path, project_root: str) -> Path | None:
    try:
        root = ws_root.resolve()
        project_dir = (root / (project_root or ".")).resolve()
        project_dir.relative_to(root)
        return project_dir
    except Exception:
        return None


def _read_project_profile(ws_root: Path, *, user_id: str, project_root: str) -> dict[str, Any]:
    path = _project_profile_path(ws_root, user_id=user_id, project_root=project_root)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_project_profile(ws_root: Path, *, user_id: str, project_root: str, profile: dict[str, Any]) -> None:
    path = _project_profile_path(ws_root, user_id=user_id, project_root=project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _detect_project_stack(project_dir: Path | None) -> list[str]:
    if not project_dir or not project_dir.exists():
        return []
    signals: list[str] = []
    package_json = project_dir / "package.json"
    package_data: dict[str, Any] = {}
    if package_json.exists():
        try:
            parsed = json.loads(package_json.read_text(encoding="utf-8"))
            package_data = parsed if isinstance(parsed, dict) else {}
        except Exception:
            package_data = {}
    deps: dict[str, Any] = {}
    for bucket in ("dependencies", "devDependencies"):
        raw = package_data.get(bucket)
        if isinstance(raw, dict):
            deps.update(raw)
    dep_names = {str(name).lower() for name in deps.keys()}
    if "react" in dep_names:
        signals.append("React")
    if "vite" in dep_names or (project_dir / "vite.config.ts").exists() or (project_dir / "vite.config.js").exists():
        signals.append("Vite")
    if "next" in dep_names:
        signals.append("Next.js")
    if "@supabase/supabase-js" in dep_names:
        signals.append("Supabase client")
    if "react-router-dom" in dep_names:
        signals.append("React Router")
    if "tailwindcss" in dep_names:
        signals.append("Tailwind CSS")
    src_dir = project_dir / "src"
    has_tsx = src_dir.exists() and any(src_dir.glob("**/*.tsx"))
    if (project_dir / "tsconfig.json").exists() or has_tsx:
        signals.append("TypeScript")
    if (project_dir / "src" / "app.css").exists() or (project_dir / "src" / "App.css").exists():
        signals.append("CSS modules/global CSS")
    out: list[str] = []
    seen: set[str] = set()
    for signal in signals:
        if signal in seen:
            continue
        seen.add(signal)
        out.append(signal)
    return out[:12]


def _infer_project_conventions(entry: dict[str, Any], change_paths: list[str], actions: list[dict[str, Any]]) -> list[str]:
    text = " ".join([
        str(entry.get("input") or ""),
        str(entry.get("spoken") or ""),
        " ".join(change_paths),
        " ".join(str(action.get("command") or "") for action in actions if isinstance(action, dict)),
    ]).lower()
    conventions: list[str] = []
    if any(token in text for token in ["minimalist", "minimalis", "elegant", "elegan"]):
        conventions.append("Design direction: minimalist, elegant, restrained UI.")
    if any(token in text for token in ["cursor", "antigravity", "ide", "hybrid"]):
        conventions.append("IDE surfaces should feel like a serious coding workspace: dense, clear, and tool-focused.")
    if "vercel" in text or "serverless" in text:
        conventions.append("Deployment target: Vercel/serverless, avoid local-only assumptions.")
    if "supabase" in text:
        conventions.append("Persistence/auth target: Supabase-backed hosted workflow.")
    if any(path.endswith(".css") for path in change_paths):
        conventions.append("Styling changes are kept in project CSS files alongside the existing UI structure.")
    if any(path.endswith((".tsx", ".jsx")) for path in change_paths):
        conventions.append("UI behavior is implemented in React component files; preserve existing component boundaries when possible.")
    if any("package.json" in path for path in change_paths):
        conventions.append("Dependency/script changes should be validated through package manager commands.")
    return conventions


def _compact_execution_outcome(outcome: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(outcome, dict) or not outcome:
        return {}
    validation_commands = [
        str(item).strip()[:180]
        for item in list(outcome.get("validation_commands") or [])[:6]
        if str(item).strip()
    ]
    compact: dict[str, Any] = {
        "ok": bool(outcome.get("ok")),
        "state": str(outcome.get("state") or "").strip()[:80],
        "summary": str(outcome.get("summary") or "").strip()[:500],
        "validation_ok": bool(outcome.get("validation_ok")) if outcome.get("validation_ok") is not None else None,
        "preview_ok": bool(outcome.get("preview_ok")) if outcome.get("preview_ok") is not None else None,
        "preview_summary": str(outcome.get("preview_summary") or "").strip()[:500],
        "repair_passes": int(outcome.get("repair_passes") or 0) if str(outcome.get("repair_passes") or "").isdigit() else outcome.get("repair_passes"),
        "rollback_count": int(outcome.get("rollback_count") or 0) if str(outcome.get("rollback_count") or "").isdigit() else outcome.get("rollback_count"),
        "rollback_paths": [str(item).strip()[:180] for item in list(outcome.get("rollback_paths") or [])[:8] if str(item).strip()],
        "final_changed_paths": [str(item).strip()[:180] for item in list(outcome.get("final_changed_paths") or [])[:12] if str(item).strip()],
        "validation_commands": validation_commands,
    }
    return {key: value for key, value in compact.items() if value not in ("", None, [])}


def _format_execution_outcome(outcome: dict[str, Any] | None) -> str:
    compact = _compact_execution_outcome(outcome)
    if not compact:
        return ""
    state = str(compact.get("state") or "unknown")
    ok_label = "passed" if compact.get("ok") else "failed"
    validation = compact.get("validation_ok")
    preview = compact.get("preview_ok")
    validation_label = "unknown" if validation is None else ("passed" if validation else "failed")
    preview_label = "unknown" if preview is None else ("passed" if preview else "failed")
    lines = [
        "EXECUTION OUTCOME:",
        f"state={state} ok={ok_label} validation={validation_label} preview={preview_label} repairs={compact.get('repair_passes', 0)} rollbacks={compact.get('rollback_count', 0)}",
    ]
    summary = str(compact.get("summary") or "").strip()
    if summary:
        lines.append(f"summary={summary}")
    commands = [str(item) for item in list(compact.get("validation_commands") or []) if str(item).strip()]
    if commands:
        lines.append("validation_commands=" + ", ".join(commands[:6]))
    preview_summary = str(compact.get("preview_summary") or "").strip()
    if preview_summary:
        lines.append(f"preview_summary={preview_summary}")
    rollback_paths = [str(item) for item in list(compact.get("rollback_paths") or []) if str(item).strip()]
    if rollback_paths:
        lines.append("rollback_paths=" + ", ".join(rollback_paths[:8]))
    final_paths = [str(item) for item in list(compact.get("final_changed_paths") or []) if str(item).strip()]
    if final_paths:
        lines.append("final_changed_paths=" + ", ".join(final_paths[:12]))
    return " | ".join(lines)


def _compact_task_state(task_state: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(task_state, dict) or not task_state:
        return {}
    nodes: list[dict[str, Any]] = []
    for node in list(task_state.get("nodes") or [])[:10]:
        if not isinstance(node, dict):
            continue
        nodes.append({
            "id": str(node.get("id") or "")[:80],
            "stage": str(node.get("stage") or "")[:80],
            "title": str(node.get("title") or "")[:140],
            "status": str(node.get("status") or "")[:60],
            "detail": str(node.get("detail") or "")[:220],
            "files": [str(item)[:180] for item in list(node.get("files") or [])[:6]],
        })
    horizon = task_state.get("horizon") if isinstance(task_state.get("horizon"), dict) else {}
    return {
        "goal": str(task_state.get("goal") or "")[:260],
        "intent": str(task_state.get("intent") or "")[:80],
        "status": str(task_state.get("status") or "")[:80],
        "next_action": str(task_state.get("next_action") or "")[:260],
        "changes": int(task_state.get("changes") or 0) if str(task_state.get("changes") or "").isdigit() else task_state.get("changes"),
        "actions": int(task_state.get("actions") or 0) if str(task_state.get("actions") or "").isdigit() else task_state.get("actions"),
        "blocking_checks": [str(item)[:120] for item in list(task_state.get("blocking_checks") or [])[:8]],
        "nodes": nodes,
        "horizon": _compact_horizon(horizon),
    }


def _compact_horizon(horizon: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(horizon, dict) or not horizon:
        return {}
    checkpoints: list[dict[str, str]] = []
    for item in list(horizon.get("checkpoints") or [])[:8]:
        if not isinstance(item, dict):
            continue
        checkpoints.append({
            "id": str(item.get("id") or "")[:80],
            "title": str(item.get("title") or "")[:120],
            "status": str(item.get("status") or "")[:60],
        })
    return {
        "enabled": bool(horizon.get("enabled")),
        "status": str(horizon.get("status") or "")[:80],
        "complexity": str(horizon.get("complexity") or "")[:80],
        "current_checkpoint": str(horizon.get("current_checkpoint") or "")[:80],
        "checkpoints": checkpoints,
        "completion_criteria": [str(item)[:180] for item in list(horizon.get("completion_criteria") or [])[:6]],
    }


def _compact_completion_report(completion_report: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(completion_report, dict) or not completion_report:
        return {}
    criteria: list[dict[str, str]] = []
    for item in list(completion_report.get("criteria") or [])[:8]:
        if not isinstance(item, dict):
            continue
        criteria.append({
            "label": str(item.get("label") or "")[:80],
            "status": str(item.get("status") or "")[:80],
            "detail": str(item.get("detail") or "")[:220],
        })
    return {
        "ok": bool(completion_report.get("ok")),
        "state": str(completion_report.get("state") or "")[:80],
        "summary": str(completion_report.get("summary") or "")[:360],
        "criteria": criteria,
        "residual_risks": [str(item)[:220] for item in list(completion_report.get("residual_risks") or [])[:6]],
    }


def _compact_failure_analysis(failure_analysis: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(failure_analysis, dict) or not failure_analysis:
        return {}
    failures: list[dict[str, str]] = []
    for item in list(failure_analysis.get("failures") or [])[:4]:
        if not isinstance(item, dict):
            continue
        failures.append({
            "kind": str(item.get("kind") or "")[:80],
            "marker": str(item.get("marker") or "")[:120],
            "command": str(item.get("command") or "")[:180],
            "category": str(item.get("category") or "")[:120],
            "excerpt": str(item.get("excerpt") or "")[:360],
        })
    return {
        "current_signature": str(failure_analysis.get("current_signature") or "")[:80],
        "primary_failure": str(failure_analysis.get("primary_failure") or "")[:240],
        "summary": str(failure_analysis.get("summary") or "")[:360],
        "suggested_next_move": str(failure_analysis.get("suggested_next_move") or "")[:360],
        "evidence_excerpt": str(failure_analysis.get("evidence_excerpt") or "")[:900],
        "failures": failures,
        "repeated_failure": bool(failure_analysis.get("repeated_failure")),
    }


def _build_active_work_state(
    *,
    project_root: str,
    build_mode: str,
    entry: dict[str, Any],
    change_paths: list[str],
    actions: list[dict[str, Any]],
    task_state: dict[str, Any] | None,
    completion_report: dict[str, Any] | None,
    failure_analysis: dict[str, Any] | None,
) -> dict[str, Any]:
    compact_task_state = _compact_task_state(task_state)
    compact_completion = _compact_completion_report(completion_report)
    compact_failure = _compact_failure_analysis(failure_analysis)
    if not (compact_task_state or compact_completion or compact_failure or change_paths or actions):
        return {}
    return {
        "kind": "command",
        "project_root": project_root,
        "build_mode": build_mode,
        "last_input": str(entry.get("input") or "")[:500],
        "last_spoken": str(entry.get("spoken") or "")[:500],
        "change_paths": change_paths[:12],
        "action_commands": [
            str(item.get("command") or "")[:180]
            for item in actions
            if isinstance(item, dict) and str(item.get("command") or "").strip()
        ][:8],
        "task_state": compact_task_state,
        "completion_report": compact_completion,
        "failure_analysis": compact_failure,
        "updated_at": int(entry.get("ts") or time.time()),
    }


def _append_unique_capped(existing: list[Any], new_items: list[Any], *, cap: int) -> list[Any]:
    out: list[Any] = []
    seen: set[str] = set()
    for item in [*existing, *new_items]:
        key = json.dumps(item, sort_keys=True, ensure_ascii=False) if isinstance(item, (dict, list)) else str(item)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out[-cap:]


def _update_project_profile(
    ws_root: Path,
    *,
    user_id: str,
    project_root: str,
    entry: dict[str, Any],
    changes: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    task_state: dict[str, Any] | None = None,
    completion_report: dict[str, Any] | None = None,
    failure_analysis: dict[str, Any] | None = None,
) -> None:
    profile = _read_project_profile(ws_root, user_id=user_id, project_root=project_root)
    now = int(time.time())
    change_paths = [str(item.get("path") or "").strip() for item in changes if isinstance(item, dict) and str(item.get("path") or "").strip()]
    project_dir = _safe_project_dir(ws_root, project_root)
    detected_stack = _detect_project_stack(project_dir)

    files_touched = profile.get("files_touched") if isinstance(profile.get("files_touched"), dict) else {}
    for path in change_paths:
        files_touched[path] = int(files_touched.get(path) or 0) + 1
    files_touched = dict(sorted(files_touched.items(), key=lambda item: int(item[1]), reverse=True)[:80])

    decision = None
    if change_paths or actions:
        decision = {
            "ts": now,
            "task": str(entry.get("input") or "").strip()[:240],
            "summary": str(entry.get("spoken") or "").strip()[:320],
            "files": change_paths[:8],
            "actions": [str(item.get("type") or "").strip() for item in actions if isinstance(item, dict)][:8],
        }

    recent_task = {
        "ts": now,
        "kind": str(entry.get("interaction_kind") or "command"),
        "task": str(entry.get("input") or "").strip()[:220],
        "files": change_paths[:6],
    }

    profile.update({
        "schema_version": 1,
        "project_root": project_root,
        "updated_at": now,
        "stack": _append_unique_capped(list(profile.get("stack") or []), detected_stack, cap=16),
        "conventions": _append_unique_capped(list(profile.get("conventions") or []), _infer_project_conventions(entry, change_paths, actions), cap=18),
        "files_touched": files_touched,
        "recent_tasks": _append_unique_capped(list(profile.get("recent_tasks") or []), [recent_task], cap=20),
    })
    if decision:
        profile["decisions"] = _append_unique_capped(list(profile.get("decisions") or []), [decision], cap=16)
    active_work_state = _build_active_work_state(
        project_root=project_root,
        build_mode=str(entry.get("build_mode") or ""),
        entry=entry,
        change_paths=change_paths,
        actions=actions,
        task_state=task_state,
        completion_report=completion_report,
        failure_analysis=failure_analysis,
    )
    if active_work_state:
        profile["active_work_state"] = active_work_state

    _write_project_profile(ws_root, user_id=user_id, project_root=project_root, profile=profile)


def _format_project_profile(profile: dict[str, Any]) -> str:
    if not profile:
        return ""
    stack = [str(item) for item in (profile.get("stack") or []) if str(item).strip()][:12]
    conventions = [str(item) for item in (profile.get("conventions") or []) if str(item).strip()][:10]
    files_touched = profile.get("files_touched") if isinstance(profile.get("files_touched"), dict) else {}
    hot_files = [f"{path} ({count}x)" for path, count in list(files_touched.items())[:8]]
    decisions = [
        item for item in (profile.get("decisions") or [])
        if isinstance(item, dict) and (str(item.get("task") or "").strip() or str(item.get("summary") or "").strip())
    ][-6:]
    recent_tasks = [
        item for item in (profile.get("recent_tasks") or [])
        if isinstance(item, dict) and str(item.get("task") or "").strip()
    ][-6:]

    lines = ["PROJECT MEMORY PROFILE (stable project context):"]
    if stack:
        lines.append("- Stack: " + ", ".join(stack))
    if conventions:
        lines.append("- Conventions:")
        lines.extend(f"  - {item}" for item in conventions)
    if hot_files:
        lines.append("- Frequently touched files: " + ", ".join(hot_files))
    if decisions:
        lines.append("- Recent implementation decisions:")
        for item in decisions:
            files = item.get("files") if isinstance(item.get("files"), list) else []
            file_text = f" files={', '.join(str(path) for path in files[:4])}" if files else ""
            lines.append(f"  - {str(item.get('task') or '').strip()[:160]} -> {str(item.get('summary') or '').strip()[:180]}{file_text}")
    if recent_tasks:
        lines.append("- Recent project tasks:")
        for item in recent_tasks:
            files = item.get("files") if isinstance(item.get("files"), list) else []
            file_text = f" ({', '.join(str(path) for path in files[:3])})" if files else ""
            lines.append(f"  - {str(item.get('kind') or 'task')}: {str(item.get('task') or '').strip()[:180]}{file_text}")
    active = profile.get("active_work_state") if isinstance(profile.get("active_work_state"), dict) else {}
    if active:
        task_state = active.get("task_state") if isinstance(active.get("task_state"), dict) else {}
        completion = active.get("completion_report") if isinstance(active.get("completion_report"), dict) else {}
        lines.append("- Active task state:")
        lines.append(f"  - Last task: {str(active.get('last_input') or '').strip()[:180]}")
        if task_state:
            lines.append(f"  - Status: {str(task_state.get('status') or '').strip()[:80]} next={str(task_state.get('next_action') or '').strip()[:180]}")
            horizon = task_state.get("horizon") if isinstance(task_state.get("horizon"), dict) else {}
            checkpoints = [item for item in list(horizon.get("checkpoints") or []) if isinstance(item, dict)]
            if horizon or checkpoints:
                current = str(horizon.get("current_checkpoint") or "").strip() or "(none)"
                status = str(horizon.get("status") or "").strip()
                suffix = f" status={status}" if status else ""
                lines.append(f"  - Horizon: current={current}{suffix}")
                if checkpoints:
                    lines.append("  - Checkpoints: " + ", ".join(
                        f"{item.get('id') or '?'}={item.get('status') or '?'}"
                        for item in checkpoints[:8]
                    ))
        if completion:
            lines.append(f"  - Execution: {str(completion.get('state') or '').strip()[:80]} {str(completion.get('summary') or '').strip()[:220]}")
    return "\n".join(lines)


def get_project_active_work_state(ws_root: Path, *, project_root: str) -> dict[str, Any] | None:
    profile = _read_project_profile(ws_root, user_id=CURRENT_USER_ID.get(), project_root=project_root)
    active = profile.get("active_work_state") if isinstance(profile.get("active_work_state"), dict) else None
    if active and str(active.get("kind") or "") == "command":
        return active
    return _get_supabase_job_active_work_state(project_root=project_root)


def _get_supabase_job_active_work_state(*, project_root: str) -> dict[str, Any] | None:
    if not has_supabase():
        return None
    row = get_latest_agent_job_result(owner_id=CURRENT_USER_ID.get(), project_root=project_root)
    if not isinstance(row, dict):
        return None
    result = row.get("result") if isinstance(row.get("result"), dict) else {}
    execution = result.get("execution") if isinstance(result.get("execution"), dict) else {}
    trace = result.get("trace") if isinstance(result.get("trace"), dict) else {}
    task_state = trace.get("task_state") if isinstance(trace.get("task_state"), dict) else {}
    changes = list(result.get("changes") or []) if isinstance(result.get("changes"), list) else []
    actions = list(result.get("actions") or []) if isinstance(result.get("actions"), list) else []
    active = _build_active_work_state(
        project_root=str(row.get("project_root") or project_root or ".").strip() or ".",
        build_mode=str(result.get("build_mode") or row.get("build_mode") or ""),
        entry={
            "input": str(result.get("input") or row.get("input") or ""),
            "spoken": str(result.get("spoken") or ""),
            "build_mode": str(result.get("build_mode") or row.get("build_mode") or ""),
            "ts": int(time.time()),
        },
        change_paths=[str(item.get("path") or "").strip() for item in changes if isinstance(item, dict) and str(item.get("path") or "").strip()],
        actions=actions,
        task_state=task_state,
        completion_report=execution.get("completion_report") if isinstance(execution.get("completion_report"), dict) else {},
        failure_analysis=execution.get("failure_analysis") if isinstance(execution.get("failure_analysis"), dict) else {},
    )
    if not active:
        return None
    active["source"] = "supabase-agent-job"
    active["job_id"] = str(row.get("id") or "")
    return active


def _ltm_candidate_paths(project_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    direct = [
        project_dir / "README.md",
        project_dir / "PRD.md",
        project_dir / ".voiceide" / "memory" / "project.md",
    ]
    for path in direct:
        if path.exists() and path.is_file():
            candidates.append(path)

    for base in [project_dir / "docs", project_dir / "memory", project_dir / ".voiceide" / "memory"]:
        if not base.exists() or not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md"))[:24]:
            if path.is_file() and path not in candidates:
                candidates.append(path)
    return candidates[:28]


def _append_memory_entry(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _read_memory_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except Exception:
        return []
    return [row for row in rows if isinstance(row, dict)]


def remember_agent_run(
    ws_root: Path,
    *,
    project_root: str,
    build_mode: str,
    interaction_kind: str,
    user_input: str,
    spoken: str,
    changes: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    execution_outcome: dict[str, Any] | None = None,
    task_state: dict[str, Any] | None = None,
    completion_report: dict[str, Any] | None = None,
    failure_analysis: dict[str, Any] | None = None,
) -> None:
    user_id = CURRENT_USER_ID.get()
    session_id = CURRENT_SESSION_ID.get()
    session_path = _session_memory_path(ws_root, user_id=user_id, session_id=session_id)
    project_path = _project_memory_path(ws_root, user_id=user_id, project_root=project_root)

    change_paths = [str(item.get("path") or "").strip() for item in changes if isinstance(item, dict)]
    action_types = [str(item.get("type") or "").strip() for item in actions if isinstance(item, dict)]
    summary_parts = [user_input.strip()]
    if spoken.strip():
        summary_parts.append(spoken.strip())
    if change_paths:
        summary_parts.append("files: " + ", ".join(change_paths[:6]))
    if action_types:
        summary_parts.append("actions: " + ", ".join(action_types[:4]))
    outcome_text = _format_execution_outcome(execution_outcome)
    if outcome_text:
        summary_parts.append(outcome_text)
    compact_outcome = _compact_execution_outcome(execution_outcome)

    entry = {
        "ts": int(time.time()),
        "project_root": project_root,
        "build_mode": build_mode,
        "interaction_kind": str(interaction_kind or "command"),
        "input": user_input.strip()[:2500],
        "spoken": spoken.strip()[:2500],
        "change_paths": change_paths[:12],
        "action_types": action_types[:12],
        "execution_outcome": compact_outcome,
        "summary": " | ".join(part for part in summary_parts if part)[:4000],
    }
    _append_memory_entry(session_path, entry)
    _append_memory_entry(project_path, entry)
    _update_project_profile(
        ws_root,
        user_id=user_id,
        project_root=project_root,
        entry=entry,
        changes=changes,
        actions=actions,
        task_state=task_state,
        completion_report=completion_report,
        failure_analysis=failure_analysis,
    )


def _build_short_hits(
    query_tokens: set[str],
    rows: list[dict[str, Any]],
    *,
    source: str,
    title_prefix: str,
    interaction_kind: str,
) -> list[MemoryHit]:
    hits: list[MemoryHit] = []
    now = time.time()
    for row in rows[-40:]:
        text = str(row.get("summary") or "").strip()
        if not text:
            continue
        age_seconds = max(0.0, now - float(row.get("ts") or now))
        freshness = max(0.0, 1.5 - min(1.5, age_seconds / 7200.0))
        row_kind = str(row.get("interaction_kind") or "command").strip() or "command"
        kind_bias = 0.75 if row_kind == interaction_kind else (-0.15 if interaction_kind in {"conversation", "inspection"} else 0.0)
        score = _score(query_tokens, text, freshness=freshness) + kind_bias
        if score <= 0:
            continue
        title = f"{title_prefix} {row.get('build_mode') or 'agent'} {row_kind} run"
        hits.append(MemoryHit(kind="short", source=source, title=title, text=text[:500], score=score))
    return hits


def _iso_utc(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if ts is None else ts))


def _normalize_excerpt(text: str, max_chars: int = 700) -> str:
    return " ".join((text or "").split())[:max_chars]


def _split_oversized_block(block: str, max_chars: int) -> list[str]:
    clean = block.strip()
    if len(clean) <= max_chars:
        return [clean] if clean else []
    sentences = re.split(r"(?<=[.!?])\s+", clean)
    if len(sentences) <= 1:
        return [clean[i:i + max_chars].strip() for i in range(0, len(clean), max_chars) if clean[i:i + max_chars].strip()]
    parts: list[str] = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        candidate = sentence if not current else f"{current} {sentence}"
        if current and len(candidate) > max_chars:
            parts.append(current.strip())
            current = sentence
        else:
            current = candidate
    if current.strip():
        parts.append(current.strip())
    out: list[str] = []
    for part in parts:
        if len(part) <= max_chars:
            out.append(part)
        else:
            out.extend([part[i:i + max_chars].strip() for i in range(0, len(part), max_chars) if part[i:i + max_chars].strip()])
    return out


def _chunk_text(text: str, *, max_chars: int = _DOC_CHUNK_CHARS, overlap_chars: int = _DOC_CHUNK_OVERLAP_CHARS) -> list[str]:
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []

    blocks: list[str] = []
    for raw_block in re.split(r"\n{2,}", normalized):
        raw_block = raw_block.strip()
        if not raw_block:
            continue
        blocks.extend(_split_oversized_block(raw_block, max_chars))

    chunks: list[str] = []
    current = ""
    for block in blocks:
        candidate = block if not current else f"{current}\n\n{block}"
        if current and len(candidate) > max_chars:
            chunks.append(current.strip())
            overlap = current[-overlap_chars:].strip()
            current = f"{overlap}\n\n{block}".strip() if overlap else block
            if len(current) > max_chars:
                slices = _split_oversized_block(current, max_chars)
                chunks.extend(slices[:-1])
                current = slices[-1] if slices else ""
        else:
            current = candidate
    if current.strip():
        chunks.append(current.strip())
    return [chunk for chunk in chunks if chunk][: _MAX_DOC_CHUNKS_PER_SOURCE]


def _build_local_long_term_chunks(project_dir: Path, *, project_root: str) -> list[MemoryChunk]:
    chunks: list[MemoryChunk] = []
    for path in _ltm_candidate_paths(project_dir):
        try:
            raw_text = path.read_text(encoding="utf-8", errors="ignore")[:_MAX_DOC_SOURCE_CHARS]
        except Exception:
            continue
        text = raw_text.strip()
        if not text:
            continue
        pieces = _chunk_text(text)
        if not pieces:
            continue
        try:
            source = str(path.relative_to(project_dir))
        except Exception:
            source = path.name
        title = source
        content_hash = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:16]
        try:
            updated_at = _iso_utc(path.stat().st_mtime)
        except Exception:
            updated_at = _iso_utc()
        chunk_count = len(pieces)
        for index, piece in enumerate(pieces):
            chunks.append(MemoryChunk(
                source=source,
                title=title,
                text=piece,
                chunk_index=index,
                chunk_count=chunk_count,
                content_hash=content_hash,
                updated_at=updated_at,
                embedding=_embed_text_cached(f"{title}\n{piece}", cache_key=f"{content_hash}:{index}"),
            ))
    return chunks


def _chunk_id(owner_id: str, project_root: str, source_path: str, chunk_index: int) -> str:
    raw = f"{owner_id}|{project_root}|{source_path}|{chunk_index}"
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()


def _sync_supabase_doc_chunks(project_root: str, chunks: list[MemoryChunk]) -> bool:
    if not chunks or not has_supabase():
        return False
    owner_id = CURRENT_USER_ID.get()
    payload = [
        {
            "chunk_id": _chunk_id(owner_id, project_root, chunk.source, chunk.chunk_index),
            "source_path": chunk.source,
            "title": chunk.title,
            "content": chunk.text,
            "chunk_index": chunk.chunk_index,
            "chunk_count": chunk.chunk_count,
            "content_hash": chunk.content_hash,
            "updated_at": chunk.updated_at,
            "embedding": chunk.embedding if chunk.embedding_backend == "real" else None,
            "embedding_model": _real_embedding_model() if chunk.embedding_backend == "real" else None,
        }
        for chunk in chunks
    ]
    return upsert_agent_memory_chunks(owner_id=owner_id, project_root=project_root, chunks=payload)


def _load_supabase_doc_chunks(project_root: str, limit: int) -> list[MemoryChunk] | None:
    rows = list_agent_memory_chunks(owner_id=CURRENT_USER_ID.get(), project_root=project_root, limit=limit)
    if rows is None:
        return None

    latest_by_source: dict[str, tuple[str, str, int, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        source = str(row.get("source_path") or "").strip()
        if not source:
            continue
        updated_at = str(row.get("updated_at") or "").strip()
        content_hash = str(row.get("content_hash") or "").strip()
        title = str(row.get("title") or source).strip() or source
        try:
            chunk_count = max(1, int(row.get("chunk_count") or 1))
        except Exception:
            chunk_count = 1
        current = latest_by_source.get(source)
        if current is None or updated_at > current[0]:
            latest_by_source[source] = (updated_at, content_hash, chunk_count, title)

    selected: dict[tuple[str, int], MemoryChunk] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        source = str(row.get("source_path") or "").strip()
        content = str(row.get("content") or "").strip()
        if not source or not content:
            continue
        latest = latest_by_source.get(source)
        if not latest:
            continue
        updated_at, content_hash, chunk_count, title = latest
        row_hash = str(row.get("content_hash") or "").strip()
        try:
            chunk_index = int(row.get("chunk_index") or 0)
        except Exception:
            chunk_index = 0
        if row_hash != content_hash or chunk_index < 0 or chunk_index >= chunk_count:
            continue
        key = (source, chunk_index)
        real_embedding = _normalize_real_embedding(row.get("embedding"))
        selected[key] = MemoryChunk(
            source=source,
            title=title,
            text=content,
            chunk_index=chunk_index,
            chunk_count=chunk_count,
            content_hash=content_hash,
            updated_at=updated_at,
            embedding=real_embedding or _embed_text_cached(f"{title}\n{content}", cache_key=f"supabase:{content_hash}:{chunk_index}"),
            embedding_backend="real" if real_embedding else "hash",
        )

    return [selected[key] for key in sorted(selected.keys(), key=lambda item: (item[0], item[1]))]


def _score_long_term_chunk(
    query_tokens: set[str],
    query_embedding: list[float],
    chunk: MemoryChunk,
    *,
    active_rel: str,
    open_files: list[str],
) -> float:
    lexical_score = _score(query_tokens, f"{chunk.title}\n{chunk.text}")
    vector_score = max(0.0, _cosine_similarity(query_embedding, chunk.embedding))
    if lexical_score <= 0 and vector_score < 0.08:
        return 0.0
    score = lexical_score * 0.72 + vector_score * 2.4
    if active_rel and chunk.source == active_rel:
        score += 0.55
    if active_rel and chunk.source.rsplit("/", 1)[-1] == active_rel.rsplit("/", 1)[-1]:
        score += 0.15
    if any(chunk.source == open_file for open_file in open_files[:4]):
        score += 0.2
    return score


def retrieve_agent_memory(
    ws_root: Path,
    *,
    project_dir: Path,
    project_root: str,
    interaction_kind: str,
    query: str,
    active_rel: str,
    open_files: list[str],
    limit_short: int = 4,
    limit_long: int = 4,
) -> AgentMemoryBundle:
    query_text = "\n".join(filter(None, [query, active_rel, " ".join(open_files[:6]), project_root]))
    query_tokens = _tokenize(query_text)

    user_id = CURRENT_USER_ID.get()
    session_id = CURRENT_SESSION_ID.get()
    session_rows = _read_memory_rows(_session_memory_path(ws_root, user_id=user_id, session_id=session_id))
    project_rows = _read_memory_rows(_project_memory_path(ws_root, user_id=user_id, project_root=project_root))
    project_profile = _read_project_profile(ws_root, user_id=user_id, project_root=project_root)
    project_profile_text = _format_project_profile(project_profile)

    short_hits = _build_short_hits(
        query_tokens,
        session_rows,
        source="session-memory",
        title_prefix="Recent session",
        interaction_kind=interaction_kind,
    )
    short_hits.extend(
        _build_short_hits(
            query_tokens,
            project_rows,
            source="project-memory",
            title_prefix="Recent same-project",
            interaction_kind=interaction_kind,
        )
    )
    if project_profile_text:
        short_hits.append(MemoryHit(
            kind="profile",
            source="project-profile",
            title="Project memory profile",
            text=project_profile_text[:1800],
            score=99.0,
        ))

    deduped_short: list[MemoryHit] = []
    seen_short: set[tuple[str, str]] = set()
    for hit in sorted(short_hits, key=lambda item: item.score, reverse=True):
        key = (hit.source, hit.text)
        if key in seen_short:
            continue
        seen_short.add(key)
        deduped_short.append(hit)

    warnings: list[str] = []
    backend = "local-hash-vector-chunks"
    local_doc_chunks = _build_local_long_term_chunks(project_dir, project_root=project_root)
    candidate_chunks = local_doc_chunks
    query_embedding = _embed_text_cached(
        query_text,
        cache_key=f"query:{hashlib.sha1(query_text.encode('utf-8', errors='ignore')).hexdigest()[:20]}",
    )

    if has_supabase():
        table_status = get_agent_memory_chunks_table_status()
        if table_status == "missing":
            warnings.append("Supabase RAG belum siap karena tabel public.agent_memory_chunks belum dibuat, jadi retrieval doc fallback ke chunk lokal.")
        else:
            real_embedding_ready = _real_embedding_backend_ready()
            sync_chunks = _with_real_embeddings(local_doc_chunks) if real_embedding_ready else local_doc_chunks
            sync_ok = _sync_supabase_doc_chunks(project_root, sync_chunks) if sync_chunks else False
            if local_doc_chunks and not sync_ok:
                warnings.append("Supabase RAG sync gagal, jadi retrieval doc sementara fallback ke chunk lokal.")
            remote_doc_chunks = _load_supabase_doc_chunks(project_root, limit=max(240, len(local_doc_chunks) + 40))
            if remote_doc_chunks:
                candidate_chunks = remote_doc_chunks
                if real_embedding_ready and any(chunk.embedding_backend == "real" for chunk in candidate_chunks):
                    real_query = _embed_real_texts([query_text])
                    if real_query:
                        query_embedding = real_query[0]
                        backend = "supabase-real-embedding-chunks"
                    else:
                        backend = "supabase-hash-vector-chunks"
                        warnings.append("Embedding query real gagal, jadi ranking Supabase sementara fallback hash-vector.")
                else:
                    backend = "supabase-hash-vector-chunks"
            elif remote_doc_chunks is None:
                if table_status == "error":
                    warnings.append("Supabase RAG nggak bisa diverifikasi sekarang, jadi retrieval doc sementara fallback ke chunk lokal.")
                warnings.append("Supabase RAG lookup gagal, jadi retrieval doc sementara fallback ke chunk lokal.")
            elif local_doc_chunks:
                warnings.append("Supabase RAG belum punya chunk project ini, jadi retrieval doc sementara fallback ke chunk lokal.")

    long_hits_scored: list[tuple[float, MemoryChunk]] = []
    for chunk in candidate_chunks:
        score = _score_long_term_chunk(query_tokens, query_embedding, chunk, active_rel=active_rel, open_files=open_files)
        if score <= 0:
            continue
        long_hits_scored.append((score, chunk))

    long_hits_scored.sort(key=lambda item: item[0], reverse=True)
    long_hits: list[MemoryHit] = []
    source_cap: dict[str, int] = {}
    seen_long: set[tuple[str, int]] = set()
    for score, chunk in long_hits_scored:
        key = (chunk.source, chunk.chunk_index)
        if key in seen_long:
            continue
        if source_cap.get(chunk.source, 0) >= 2:
            continue
        seen_long.add(key)
        source_cap[chunk.source] = source_cap.get(chunk.source, 0) + 1
        label = chunk.title if chunk.chunk_count <= 1 else f"{chunk.title}#chunk-{chunk.chunk_index + 1}"
        long_hits.append(MemoryHit(
            kind="long",
            source=chunk.source,
            title=label,
            text=_normalize_excerpt(chunk.text),
            score=score,
        ))
        if len(long_hits) >= limit_long:
            break

    return AgentMemoryBundle(
        short_term=deduped_short[:limit_short],
        long_term=long_hits,
        warnings=warnings,
        backend=backend,
    )


def sync_project_docs_to_supabase(project_dir: Path, *, project_root: str) -> dict[str, Any]:
    chunks = _build_local_long_term_chunks(project_dir, project_root=project_root)
    table_status = get_agent_memory_chunks_table_status(refresh=True) if has_supabase() else "unconfigured"
    synced = False
    warning = None

    if not has_supabase():
        warning = "Supabase belum dikonfigurasi di backend ini."
    elif table_status == "missing":
        warning = "Tabel public.agent_memory_chunks belum ada, jadi sync belum bisa jalan."
    elif table_status == "error":
        warning = "Backend belum bisa verifikasi agent_memory_chunks di Supabase sekarang."
    else:
        synced = _sync_supabase_doc_chunks(project_root, chunks)
        if not synced and chunks:
            warning = "Upsert chunk ke Supabase gagal, jadi belum live-ready sepenuhnya."

    return {
        "project_root": project_root,
        "local_chunk_count": len(chunks),
        "source_count": len({chunk.source for chunk in chunks}),
        "supabase_configured": has_supabase(),
        "table_status": table_status,
        "synced": synced,
        "warning": warning,
    }


def get_agent_memory_overview(ws_root: Path, *, project_root: str) -> AgentMemoryOverview:
    user_id = CURRENT_USER_ID.get()
    session_id = CURRENT_SESSION_ID.get()
    session_rows = _read_memory_rows(_session_memory_path(ws_root, user_id=user_id, session_id=session_id))
    project_rows = _read_memory_rows(_project_memory_path(ws_root, user_id=user_id, project_root=project_root))
    project_profile = _read_project_profile(ws_root, user_id=user_id, project_root=project_root)
    latest_session_ts = int(session_rows[-1].get("ts")) if session_rows else None
    latest_project_ts = int(project_rows[-1].get("ts")) if project_rows else None
    profile_updated_at = None
    try:
        profile_updated_at = int(project_profile.get("updated_at")) if project_profile else None
    except Exception:
        profile_updated_at = None
    return AgentMemoryOverview(
        session_entries=len(session_rows),
        project_entries=len(project_rows),
        latest_session_ts=latest_session_ts,
        latest_project_ts=latest_project_ts,
        has_project_profile=bool(project_profile),
        project_profile_updated_at=profile_updated_at,
    )
