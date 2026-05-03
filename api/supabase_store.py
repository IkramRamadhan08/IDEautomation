from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from api import settings as settings_mod

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
