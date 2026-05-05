from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import re
import time
from typing import Any

from .app_state import CURRENT_SESSION_ID, CURRENT_USER_ID

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_:-]{2,}")
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "your", "you", "are", "was", "were",
    "yang", "dan", "untuk", "dari", "atau", "dengan", "ini", "itu", "saat", "jadi", "agar", "buat",
    "bisa", "lebih", "kaya", "seperti", "karena", "supaya", "lagi", "udah", "akan", "tetap",
}


@dataclass
class MemoryHit:
    kind: str
    source: str
    title: str
    text: str
    score: float


@dataclass
class AgentMemoryBundle:
    short_term: list[MemoryHit]
    long_term: list[MemoryHit]

    @property
    def prompt(self) -> str:
        sections: list[str] = []
        if self.short_term:
            lines = ["SHORT-TERM MEMORY (recent runs / working context):"]
            for hit in self.short_term:
                lines.append(f"- {hit.title} [{hit.source}]\n  {hit.text}")
            sections.append("\n".join(lines))
        if self.long_term:
            lines = ["LONG-TERM MEMORY (durable project knowledge):"]
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
    user_input: str,
    spoken: str,
    changes: list[dict[str, Any]],
    actions: list[dict[str, Any]],
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

    entry = {
        "ts": int(time.time()),
        "project_root": project_root,
        "build_mode": build_mode,
        "input": user_input.strip()[:2500],
        "spoken": spoken.strip()[:2500],
        "change_paths": change_paths[:12],
        "action_types": action_types[:12],
        "summary": " | ".join(part for part in summary_parts if part)[:4000],
    }
    _append_memory_entry(session_path, entry)
    _append_memory_entry(project_path, entry)


def _build_short_hits(query_tokens: set[str], rows: list[dict[str, Any]], *, source: str, title_prefix: str) -> list[MemoryHit]:
    hits: list[MemoryHit] = []
    now = time.time()
    for row in rows[-40:]:
        text = str(row.get("summary") or "").strip()
        if not text:
            continue
        age_seconds = max(0.0, now - float(row.get("ts") or now))
        freshness = max(0.0, 1.5 - min(1.5, age_seconds / 7200.0))
        score = _score(query_tokens, text, freshness=freshness)
        if score <= 0:
            continue
        title = f"{title_prefix} {row.get('build_mode') or 'agent'} run"
        hits.append(MemoryHit(kind="short", source=source, title=title, text=text[:500], score=score))
    return hits


def retrieve_agent_memory(
    ws_root: Path,
    *,
    project_dir: Path,
    project_root: str,
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

    short_hits = _build_short_hits(query_tokens, session_rows, source="session-memory", title_prefix="Recent session")
    short_hits.extend(_build_short_hits(query_tokens, project_rows, source="project-memory", title_prefix="Recent same-project"))

    deduped_short: list[MemoryHit] = []
    seen_short: set[tuple[str, str]] = set()
    for hit in sorted(short_hits, key=lambda item: item.score, reverse=True):
        key = (hit.source, hit.text)
        if key in seen_short:
            continue
        seen_short.add(key)
        deduped_short.append(hit)

    long_hits: list[MemoryHit] = []
    for path in _ltm_candidate_paths(project_dir):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")[:12000]
        except Exception:
            continue
        score = _score(query_tokens, f"{path.name}\n{text}")
        if score <= 0:
            continue
        excerpt = " ".join(text.split())[:700]
        title = str(path.relative_to(project_dir))
        long_hits.append(MemoryHit(kind="long", source=title, title=title, text=excerpt, score=score))

    long_hits.sort(key=lambda item: item.score, reverse=True)
    return AgentMemoryBundle(short_term=deduped_short[:limit_short], long_term=long_hits[:limit_long])


def get_agent_memory_overview(ws_root: Path, *, project_root: str) -> AgentMemoryOverview:
    user_id = CURRENT_USER_ID.get()
    session_id = CURRENT_SESSION_ID.get()
    session_rows = _read_memory_rows(_session_memory_path(ws_root, user_id=user_id, session_id=session_id))
    project_rows = _read_memory_rows(_project_memory_path(ws_root, user_id=user_id, project_root=project_root))
    latest_session_ts = int(session_rows[-1].get("ts")) if session_rows else None
    latest_project_ts = int(project_rows[-1].get("ts")) if project_rows else None
    return AgentMemoryOverview(
        session_entries=len(session_rows),
        project_entries=len(project_rows),
        latest_session_ts=latest_session_ts,
        latest_project_ts=latest_project_ts,
    )
