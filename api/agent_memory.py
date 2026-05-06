from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import json
import math
import re
import time
from typing import Any

from .app_state import CURRENT_SESSION_ID, CURRENT_USER_ID
from .supabase_store import get_agent_memory_chunks_table_status, has_supabase, list_agent_memory_chunks, upsert_agent_memory_chunks

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
        "interaction_kind": str(interaction_kind or "command"),
        "input": user_input.strip()[:2500],
        "spoken": spoken.strip()[:2500],
        "change_paths": change_paths[:12],
        "action_types": action_types[:12],
        "summary": " | ".join(part for part in summary_parts if part)[:4000],
    }
    _append_memory_entry(session_path, entry)
    _append_memory_entry(project_path, entry)


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


def _chunk_id(project_root: str, source_path: str, chunk_index: int) -> str:
    raw = f"{project_root}|{source_path}|{chunk_index}"
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()


def _sync_supabase_doc_chunks(project_root: str, chunks: list[MemoryChunk]) -> bool:
    if not chunks or not has_supabase():
        return False
    payload = [
        {
            "chunk_id": _chunk_id(project_root, chunk.source, chunk.chunk_index),
            "source_path": chunk.source,
            "title": chunk.title,
            "content": chunk.text,
            "chunk_index": chunk.chunk_index,
            "chunk_count": chunk.chunk_count,
            "content_hash": chunk.content_hash,
            "updated_at": chunk.updated_at,
        }
        for chunk in chunks
    ]
    return upsert_agent_memory_chunks(project_root=project_root, chunks=payload)


def _load_supabase_doc_chunks(project_root: str, limit: int) -> list[MemoryChunk] | None:
    rows = list_agent_memory_chunks(project_root=project_root, limit=limit)
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
        selected[key] = MemoryChunk(
            source=source,
            title=title,
            text=content,
            chunk_index=chunk_index,
            chunk_count=chunk_count,
            content_hash=content_hash,
            updated_at=updated_at,
            embedding=_embed_text_cached(f"{title}\n{content}", cache_key=f"supabase:{content_hash}:{chunk_index}"),
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
            sync_ok = _sync_supabase_doc_chunks(project_root, local_doc_chunks) if local_doc_chunks else False
            if local_doc_chunks and not sync_ok:
                warnings.append("Supabase RAG sync gagal, jadi retrieval doc sementara fallback ke chunk lokal.")
            remote_doc_chunks = _load_supabase_doc_chunks(project_root, limit=max(240, len(local_doc_chunks) + 40))
            if remote_doc_chunks:
                candidate_chunks = remote_doc_chunks
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
