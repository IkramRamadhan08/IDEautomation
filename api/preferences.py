from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel

from api.supabase_store import get_supabase_admin, has_supabase


class UserPreferencesRecord(BaseModel):
    profile_id: str
    llm_provider: str | None = None
    build_mode: str | None = None
    openai_codex_model: str | None = None
    anthropic_model: str | None = None
    openrouter_model: str | None = None


class ProjectPreferencesRecord(BaseModel):
    project_id: str
    build_mode: str | None = None
    preview_entry: str | None = None
    default_prompt_style: str | None = None


class UserPreferencesUpdateReq(BaseModel):
    llm_provider: str | None = None
    build_mode: str | None = None
    openai_codex_model: str | None = None
    anthropic_model: str | None = None
    openrouter_model: str | None = None


class ProjectPreferencesUpdateReq(BaseModel):
    build_mode: str | None = None
    preview_entry: str | None = None
    default_prompt_style: str | None = None


class UserPreferencesResp(BaseModel):
    ok: bool = True
    preferences: UserPreferencesRecord


class ProjectPreferencesResp(BaseModel):
    ok: bool = True
    preferences: ProjectPreferencesRecord


def _require_supabase() -> Any:
    if not has_supabase():
        raise HTTPException(503, "Supabase must be configured for hosted preferences")
    client = get_supabase_admin()
    if not client:
        raise HTTPException(503, "Supabase admin client unavailable")
    return client


def get_user_preferences(*, profile_id: str) -> UserPreferencesRecord:
    client = _require_supabase()
    res = client.table("user_preferences").select("*").eq("profile_id", profile_id).limit(1).execute()
    data = getattr(res, "data", None) or []
    if data:
        return UserPreferencesRecord(**data[0])
    return UserPreferencesRecord(profile_id=profile_id)


def upsert_user_preferences(*, profile_id: str, req: UserPreferencesUpdateReq) -> UserPreferencesRecord:
    client = _require_supabase()
    payload = {
        "profile_id": profile_id,
        **req.model_dump(),
    }
    res = client.table("user_preferences").upsert(payload).execute()
    data = getattr(res, "data", None) or []
    if not data:
        raise HTTPException(500, "Failed to save user preferences")
    return UserPreferencesRecord(**data[0])


def get_project_preferences(*, project_id: str) -> ProjectPreferencesRecord:
    client = _require_supabase()
    res = client.table("project_preferences").select("*").eq("project_id", project_id).limit(1).execute()
    data = getattr(res, "data", None) or []
    if data:
        return ProjectPreferencesRecord(**data[0])
    return ProjectPreferencesRecord(project_id=project_id)


def upsert_project_preferences(*, project_id: str, req: ProjectPreferencesUpdateReq) -> ProjectPreferencesRecord:
    client = _require_supabase()
    payload = {
        "project_id": project_id,
        **req.model_dump(),
    }
    res = client.table("project_preferences").upsert(payload).execute()
    data = getattr(res, "data", None) or []
    if not data:
        raise HTTPException(500, "Failed to save project preferences")
    return ProjectPreferencesRecord(**data[0])
