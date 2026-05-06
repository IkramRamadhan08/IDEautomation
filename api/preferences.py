from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel

from api.supabase_store import get_supabase_admin, has_supabase


USER_SETTINGS_TABLE = "user_settings"
PROJECT_PREFERENCES_TABLE = "project_preferences"
HAS_PROJECT_PREFERENCES = False
OPTIONAL_USER_SETTINGS_COLUMNS = ("groq_model", "gemini_model", "together_model", "cerebras_model", "xai_model")


class UserPreferencesRecord(BaseModel):
    profile_id: str
    llm_provider: str | None = None
    build_mode: str | None = None
    openai_model: str | None = None
    anthropic_model: str | None = None
    openrouter_model: str | None = None
    groq_model: str | None = None
    gemini_model: str | None = None
    together_model: str | None = None
    cerebras_model: str | None = None
    xai_model: str | None = None


class ProjectPreferencesRecord(BaseModel):
    project_id: str
    build_mode: str | None = None
    preview_entry: str | None = None
    default_prompt_style: str | None = None


class UserPreferencesUpdateReq(BaseModel):
    llm_provider: str | None = None
    build_mode: str | None = None
    openai_model: str | None = None
    anthropic_model: str | None = None
    openrouter_model: str | None = None
    groq_model: str | None = None
    gemini_model: str | None = None
    together_model: str | None = None
    cerebras_model: str | None = None
    xai_model: str | None = None


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
    res = client.table(USER_SETTINGS_TABLE).select("*").eq("user_id", profile_id).limit(1).execute()
    data = getattr(res, "data", None) or []
    if data:
        row = data[0] if isinstance(data[0], dict) else {}
        return UserPreferencesRecord(
            profile_id=profile_id,
            llm_provider=row.get("llm_provider"),
            build_mode=row.get("build_mode"),
            openai_model=row.get("openai_codex_model"),
            anthropic_model=row.get("anthropic_model"),
            openrouter_model=row.get("openrouter_model"),
            groq_model=row.get("groq_model"),
            gemini_model=row.get("gemini_model"),
            together_model=row.get("together_model"),
            cerebras_model=row.get("cerebras_model"),
            xai_model=row.get("xai_model"),
        )
    return UserPreferencesRecord(profile_id=profile_id)


def upsert_user_preferences(*, profile_id: str, req: UserPreferencesUpdateReq) -> UserPreferencesRecord:
    client = _require_supabase()
    payload: dict[str, Any] = {
        "user_id": profile_id,
        "llm_provider": req.llm_provider,
        "build_mode": req.build_mode,
        "openai_codex_model": req.openai_model,
        "anthropic_model": req.anthropic_model,
        "openrouter_model": req.openrouter_model,
        "groq_model": req.groq_model,
        "gemini_model": req.gemini_model,
        "together_model": req.together_model,
        "cerebras_model": req.cerebras_model,
        "xai_model": req.xai_model,
    }
    try:
        res = client.table(USER_SETTINGS_TABLE).upsert(payload, on_conflict="user_id").execute()
    except Exception as exc:
        if not _is_missing_optional_column_error(exc):
            raise
        fallback_payload = {key: value for key, value in payload.items() if key not in OPTIONAL_USER_SETTINGS_COLUMNS}
        res = client.table(USER_SETTINGS_TABLE).upsert(fallback_payload, on_conflict="user_id").execute()
        payload = fallback_payload
    data = getattr(res, "data", None) or []
    row = data[0] if data and isinstance(data[0], dict) else payload
    return UserPreferencesRecord(
        profile_id=profile_id,
        llm_provider=row.get("llm_provider"),
        build_mode=row.get("build_mode"),
        openai_model=row.get("openai_codex_model"),
        anthropic_model=row.get("anthropic_model"),
        openrouter_model=row.get("openrouter_model"),
        groq_model=row.get("groq_model"),
        gemini_model=row.get("gemini_model"),
        together_model=row.get("together_model"),
        cerebras_model=row.get("cerebras_model"),
        xai_model=row.get("xai_model"),
    )


def _is_missing_optional_column_error(exc: Exception) -> bool:
    text = str(exc)
    return "PGRST204" in text and any(column in text for column in OPTIONAL_USER_SETTINGS_COLUMNS)


def get_project_preferences(*, project_id: str) -> ProjectPreferencesRecord:
    if not HAS_PROJECT_PREFERENCES:
        return ProjectPreferencesRecord(project_id=project_id)
    client = _require_supabase()
    res = client.table(PROJECT_PREFERENCES_TABLE).select("*").eq("project_id", project_id).limit(1).execute()
    data = getattr(res, "data", None) or []
    if data:
        return ProjectPreferencesRecord(**data[0])
    return ProjectPreferencesRecord(project_id=project_id)


def upsert_project_preferences(*, project_id: str, req: ProjectPreferencesUpdateReq) -> ProjectPreferencesRecord:
    if not HAS_PROJECT_PREFERENCES:
        return ProjectPreferencesRecord(
            project_id=project_id,
            build_mode=req.build_mode,
            preview_entry=req.preview_entry,
            default_prompt_style=req.default_prompt_style,
        )
    client = _require_supabase()
    payload = {
        "project_id": project_id,
        **req.model_dump(),
    }
    res = client.table(PROJECT_PREFERENCES_TABLE).upsert(payload).execute()
    data = getattr(res, "data", None) or []
    if not data:
        raise HTTPException(500, "Failed to save project preferences")
    return ProjectPreferencesRecord(**data[0])
