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

    if supabase_user_id:
        payload_uuid = {
            "id": supabase_user_id,
            "supabase_user_id": supabase_user_id,
            "display_name": display_name,
            "email": email,
            "updated_at": updated_at,
        }
        try:
            res_uuid = client.table("profiles").upsert(payload_uuid).execute()
            data_uuid = getattr(res_uuid, "data", None) or []
            if data_uuid:
                return data_uuid[0]
        except Exception:
            pass

    payload = {
        "id": user_id,
        "supabase_user_id": supabase_user_id,
        "display_name": display_name,
        "email": email,
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
        "agent_mode_default": "hybrid",
        "runtime_status": "idle",
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
            client.table("agent_memory_chunks").select("chunk_id").limit(1).execute()
            status = "ready"
        except Exception as exc:
            status = _classify_agent_memory_chunks_probe_error(exc)

    _AGENT_MEMORY_CHUNKS_STATUS_CACHE = (now, status)
    return status


def upsert_agent_memory_chunks(*, project_root: str, chunks: list[dict[str, Any]]) -> bool:
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


def list_agent_memory_chunks(*, project_root: str, limit: int = 240) -> list[dict[str, Any]] | None:
    client = get_supabase_admin()
    if not client:
        return None
    try:
        res = (
            client.table("agent_memory_chunks")
            .select("chunk_id, project_root, source_path, title, content, chunk_index, chunk_count, content_hash, updated_at")
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
