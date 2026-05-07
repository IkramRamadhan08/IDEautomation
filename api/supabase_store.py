from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from api import settings as settings_mod

_AGENT_MEMORY_CHUNKS_STATUS_CACHE: tuple[float, str] | None = None

try:
    from supabase import Client, create_client
except Exception:  # pragma: no cover
    Client = Any  # type: ignore
    create_client = None  # type: ignore


@dataclass
class SupabaseConfig:
    url: str | None
    service_role_key: str | None


def get_supabase_config() -> SupabaseConfig:
    return SupabaseConfig(
        url=getattr(settings_mod.settings, "supabase_url", None),
        service_role_key=getattr(settings_mod.settings, "supabase_service_role_key", None),
    )


def has_supabase() -> bool:
    cfg = get_supabase_config()
    return bool(cfg.url and cfg.service_role_key and create_client)


_client: Client | None = None


def get_supabase_admin() -> Client | None:
    global _client
    if _client is not None:
        return _client
    cfg = get_supabase_config()
    if not cfg.url or not cfg.service_role_key or not create_client:
        return None
    _client = create_client(cfg.url, cfg.service_role_key)
    return _client


def upsert_profile(*, user_id: str, supabase_user_id: str | None = None, display_name: str | None, email: str | None) -> dict[str, Any] | None:
    client = get_supabase_admin()
    if not client:
        return None

    updated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    legacy_row: dict[str, Any] | None = None

    if supabase_user_id:
        try:
            res_legacy = client.table("profiles").select("*").eq("supabase_user_id", supabase_user_id).limit(1).execute()
            legacy_data = getattr(res_legacy, "data", None) or []
            if legacy_data and isinstance(legacy_data[0], dict):
                legacy_row = legacy_data[0]
        except Exception:
            legacy_row = None

        legacy_id = str((legacy_row or {}).get("id") or "").strip()
        if legacy_id and legacy_id != user_id:
            try:
                client.table("profiles").update({"supabase_user_id": None, "updated_at": updated_at}).eq("id", legacy_id).execute()
            except Exception:
                pass

    payload = {
        "id": user_id,
        "supabase_user_id": supabase_user_id,
        "display_name": display_name if display_name is not None else (legacy_row or {}).get("display_name"),
        "email": email if email is not None else (legacy_row or {}).get("email"),
        "updated_at": updated_at,
    }
    res = client.table("profiles").upsert(payload).execute()
    data = getattr(res, "data", None) or []
    return data[0] if data else payload


def list_projects(*, owner_id: str) -> list[dict[str, Any]] | None:
    client = get_supabase_admin()
    if not client:
        return None
    res = client.table("projects").select("*").eq("owner_id", owner_id).order("updated_at", desc=True).execute()
    data = getattr(res, "data", None)
    return data if isinstance(data, list) else []


def insert_project(*, owner_id: str, name: str, slug: str, root: str) -> dict[str, Any] | None:
    client = get_supabase_admin()
    if not client:
        return None
    payload = {
        "owner_id": owner_id,
        "name": name,
        "slug": slug,
        "root": root,
    }
    res = client.table("projects").insert(payload).execute()
    data = getattr(res, "data", None) or []
    project = data[0] if data else payload
    project_id = str(project.get("id") or "").strip() if isinstance(project, dict) else ""
    if project_id:
        try:
            client.table("project_members").upsert({
                "project_id": project_id,
                "profile_id": owner_id,
                "role": "owner",
            }).execute()
        except Exception:
            pass
    return project


def update_project_name(*, project_id: str, owner_id: str, name: str) -> dict[str, Any] | None:
    client = get_supabase_admin()
    if not client:
        return None
    res = client.table("projects").update({"name": name}).eq("id", project_id).eq("owner_id", owner_id).execute()
    data = getattr(res, "data", None) or []
    return data[0] if data else None


def archive_project(*, project_id: str, owner_id: str) -> dict[str, Any] | None:
    client = get_supabase_admin()
    if not client:
        return None
    res = client.table("projects").update({"archived": True}).eq("id", project_id).eq("owner_id", owner_id).execute()
    data = getattr(res, "data", None) or []
    return data[0] if data else None


def list_project_files(*, owner_id: str, project_root: str, limit: int = 2000) -> list[dict[str, Any]] | None:
    client = get_supabase_admin()
    if not client:
        return None
    res = (
        client.table("project_files")
        .select("project_root,path,content,updated_at")
        .eq("owner_id", owner_id)
        .eq("project_root", str(project_root or ".").strip() or ".")
        .order("path")
        .limit(max(1, min(int(limit or 2000), 5000)))
        .execute()
    )
    data = getattr(res, "data", None)
    return data if isinstance(data, list) else []


def upsert_project_files(*, owner_id: str, project_root: str, files: list[dict[str, str]]) -> bool:
    client = get_supabase_admin()
    if not client or not files:
        return False
    normalized_root = str(project_root or ".").strip() or "."
    payload: list[dict[str, str]] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip().lstrip("/")
        content = item.get("content")
        if not path or path in {".", ".."} or ".." in path.split("/") or not isinstance(content, str):
            continue
        payload.append({
            "owner_id": owner_id,
            "project_root": normalized_root,
            "path": path,
            "content": content,
        })
    if not payload:
        return False
    client.table("project_files").upsert(payload).execute()
    return True


def delete_project_file(*, owner_id: str, project_root: str, path: str) -> bool:
    client = get_supabase_admin()
    if not client:
        return False
    normalized_root = str(project_root or ".").strip() or "."
    normalized_path = str(path or "").strip().lstrip("/")
    if not normalized_path or normalized_path in {".", ".."} or ".." in normalized_path.split("/"):
        return False
    client.table("project_files").delete().eq("owner_id", owner_id).eq("project_root", normalized_root).eq("path", normalized_path).execute()
    return True


def create_agent_job(*, owner_id: str, job_id: str, project_root: str, build_mode: str | None, input_text: str, request_payload: dict[str, Any] | None = None) -> dict[str, Any] | None:
    client = get_supabase_admin()
    if not client:
        return None
    payload = {
        "id": job_id,
        "owner_id": owner_id,
        "project_root": str(project_root or ".").strip() or ".",
        "build_mode": build_mode,
        "status": "queued",
        "input": str(input_text or "")[:20_000],
        "request_payload": request_payload if isinstance(request_payload, dict) else {},
    }
    try:
        res = client.table("agent_jobs").insert(payload).execute()
        data = getattr(res, "data", None) or []
        return data[0] if data else payload
    except Exception:
        return None


def update_agent_job(*, owner_id: str, job_id: str, status: str, result: dict[str, Any] | None = None, error: str | None = None) -> dict[str, Any] | None:
    client = get_supabase_admin()
    if not client:
        return None
    payload: dict[str, Any] = {"status": status}
    if status == "running":
        payload["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if status in {"completed", "failed", "cancelled"}:
        payload["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if result is not None:
        payload["result"] = result
    if error is not None:
        payload["error"] = str(error)[:4000]
    try:
        res = client.table("agent_jobs").update(payload).eq("id", job_id).eq("owner_id", owner_id).execute()
        data = getattr(res, "data", None) or []
        return data[0] if data else None
    except Exception:
        return None


def append_agent_job_event(*, owner_id: str, job_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    client = get_supabase_admin()
    if not client:
        return None
    row = {
        "job_id": job_id,
        "owner_id": owner_id,
        "event_type": str(event_type or "status").strip() or "status",
        "payload": payload if isinstance(payload, dict) else {},
    }
    try:
        res = client.table("agent_job_events").insert(row).execute()
        data = getattr(res, "data", None) or []
        return data[0] if data else row
    except Exception:
        return None


def get_agent_job(*, owner_id: str, job_id: str) -> dict[str, Any] | None:
    client = get_supabase_admin()
    if not client:
        return None
    try:
        res = client.table("agent_jobs").select("*").eq("id", job_id).eq("owner_id", owner_id).limit(1).execute()
        data = getattr(res, "data", None) or []
        return data[0] if data else None
    except Exception:
        return None


def get_agent_job_any(*, job_id: str) -> dict[str, Any] | None:
    client = get_supabase_admin()
    if not client:
        return None
    try:
        res = client.table("agent_jobs").select("*").eq("id", job_id).limit(1).execute()
        data = getattr(res, "data", None) or []
        return data[0] if data else None
    except Exception:
        return None


def list_agent_jobs_by_status(*, status: str = "queued", limit: int = 3) -> list[dict[str, Any]] | None:
    client = get_supabase_admin()
    if not client:
        return None
    try:
        res = (
            client.table("agent_jobs")
            .select("*")
            .eq("status", status)
            .order("created_at")
            .limit(max(1, min(int(limit or 3), 10)))
            .execute()
        )
        data = getattr(res, "data", None)
        return data if isinstance(data, list) else []
    except Exception:
        return None


def list_agent_job_events(*, owner_id: str, job_id: str, after_id: int = 0, limit: int = 200) -> list[dict[str, Any]] | None:
    client = get_supabase_admin()
    if not client:
        return None
    try:
        query = (
            client.table("agent_job_events")
            .select("id, job_id, event_type, payload, created_at")
            .eq("job_id", job_id)
            .eq("owner_id", owner_id)
            .order("id")
            .limit(max(1, min(int(limit or 200), 1000)))
        )
        if after_id > 0:
            query = query.gt("id", after_id)
        res = query.execute()
        data = getattr(res, "data", None)
        return data if isinstance(data, list) else []
    except Exception:
        return None


def _classify_agent_memory_chunks_probe_error(exc: Exception) -> str:
    message = str(exc or "").lower()
    if "agent_memory_chunks" in message and (
        "does not exist" in message
        or "could not find the table" in message
        or "relation" in message
        or "schema cache" in message
    ):
        return "missing"
    return "error"


def get_agent_memory_chunks_table_status(*, refresh: bool = False) -> str:
    global _AGENT_MEMORY_CHUNKS_STATUS_CACHE
    if not has_supabase():
        return "unconfigured"
    now = time.time()
    if not refresh and _AGENT_MEMORY_CHUNKS_STATUS_CACHE and (now - _AGENT_MEMORY_CHUNKS_STATUS_CACHE[0]) < 60:
        return _AGENT_MEMORY_CHUNKS_STATUS_CACHE[1]

    client = get_supabase_admin()
    if not client:
        status = "unconfigured"
    else:
        try:
            client.table("agent_memory_chunks").select("chunk_id,owner_id").limit(1).execute()
            status = "ready"
        except Exception as exc:
            status = _classify_agent_memory_chunks_probe_error(exc)

    _AGENT_MEMORY_CHUNKS_STATUS_CACHE = (now, status)
    return status


def upsert_agent_memory_chunks(*, owner_id: str, project_root: str, chunks: list[dict[str, Any]]) -> bool:
    client = get_supabase_admin()
    if not client or not chunks:
        return False

    payload: list[dict[str, Any]] = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        chunk_id = str(chunk.get("chunk_id") or "").strip()
        source_path = str(chunk.get("source_path") or "").strip()
        content = str(chunk.get("content") or "").strip()
        if not chunk_id or not source_path or not content:
            continue
        payload.append({
            "owner_id": owner_id,
            "chunk_id": chunk_id,
            "project_root": str(project_root or ".").strip() or ".",
            "source_path": source_path,
            "title": str(chunk.get("title") or source_path).strip() or source_path,
            "content": content,
            "chunk_index": int(chunk.get("chunk_index") or 0),
            "chunk_count": max(1, int(chunk.get("chunk_count") or 1)),
            "content_hash": str(chunk.get("content_hash") or chunk_id).strip() or chunk_id,
            "updated_at": str(chunk.get("updated_at") or "").strip() or None,
        })
    if not payload:
        return False

    try:
        client.table("agent_memory_chunks").upsert(payload).execute()
        global _AGENT_MEMORY_CHUNKS_STATUS_CACHE
        _AGENT_MEMORY_CHUNKS_STATUS_CACHE = (time.time(), "ready")
        return True
    except Exception:
        return False


def list_agent_memory_chunks(*, owner_id: str, project_root: str, limit: int = 240) -> list[dict[str, Any]] | None:
    client = get_supabase_admin()
    if not client:
        return None
    try:
        res = (
            client.table("agent_memory_chunks")
            .select("chunk_id, owner_id, project_root, source_path, title, content, chunk_index, chunk_count, content_hash, updated_at")
            .eq("owner_id", owner_id)
            .eq("project_root", str(project_root or ".").strip() or ".")
            .order("updated_at", desc=True)
            .limit(max(1, min(int(limit or 240), 1000)))
            .execute()
        )
        global _AGENT_MEMORY_CHUNKS_STATUS_CACHE
        _AGENT_MEMORY_CHUNKS_STATUS_CACHE = (time.time(), "ready")
    except Exception:
        return None
    data = getattr(res, "data", None)
    return data if isinstance(data, list) else []


def get_agent_memory_chunks_summary(*, owner_id: str, project_root: str, limit: int = 1000) -> dict[str, Any] | None:
    rows = list_agent_memory_chunks(owner_id=owner_id, project_root=project_root, limit=limit)
    if rows is None:
        return None

    sources: set[str] = set()
    latest_updated_at: str | None = None
    sample_sources: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        source = str(row.get("source_path") or "").strip()
        updated_at = str(row.get("updated_at") or "").strip() or None
        if source:
            sources.add(source)
            if len(sample_sources) < 6 and source not in sample_sources:
                sample_sources.append(source)
        if updated_at and (latest_updated_at is None or updated_at > latest_updated_at):
            latest_updated_at = updated_at

    return {
        "project_root": str(project_root or ".").strip() or ".",
        "chunk_count": len(rows),
        "source_count": len(sources),
        "latest_updated_at": latest_updated_at,
        "sample_sources": sample_sources,
    }
