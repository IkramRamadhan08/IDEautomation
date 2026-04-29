from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values, load_dotenv
from pydantic import BaseModel


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"

Provider = Literal["openai-codex", "anthropic", "openrouter", "groq", "xai"]
BuildMode = Literal["full-agent", "hybrid"]


def load_env() -> None:
    load_dotenv(ENV_PATH, override=True)


class Settings(BaseModel):
    default_workspace: str | None = None
    llm_provider: Provider | None = None
    build_mode: BuildMode = "hybrid"
    openai_codex_model: str = "gpt-5.4"
    anthropic_model: str = "claude-sonnet-4-0"
    openrouter_model: str = "openai/gpt-5.4"
    openai_api_key_set: bool = False
    anthropic_api_key_set: bool = False
    openrouter_api_key_set: bool = False
    supabase_url: str | None = None
    supabase_anon_key_set: bool = False
    supabase_service_role_key: str | None = None
    supabase_enabled: bool = False


def load_settings() -> Settings:
    load_env()
    file_values = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}

    def g(key: str, default: str | None = None) -> str | None:
        if key in file_values:
            v = file_values.get(key)
            return None if v is None else str(v)
        v = os.getenv(key)
        if v is None:
            return default
        return v

    raw_provider = str(g("LLM_PROVIDER", "") or "").strip().lower()
    llm_provider = raw_provider if raw_provider in {"openai-codex", "anthropic", "openrouter"} else None

    build_mode = str(g("BUILD_MODE", "hybrid") or "hybrid").strip().lower()
    if build_mode not in {"full-agent", "hybrid"}:
        build_mode = "hybrid"

    return Settings(
        default_workspace=(g("DEFAULT_WORKSPACE", "") or "").strip() or None,
        llm_provider=llm_provider,  # type: ignore[arg-type]
        build_mode=build_mode,  # type: ignore[arg-type]
        openai_codex_model=str(g("OPENAI_CODEX_MODEL", "gpt-5.4") or "gpt-5.4").strip(),
        anthropic_model=str(g("ANTHROPIC_MODEL", "claude-sonnet-4-0") or "claude-sonnet-4-0").strip(),
        openrouter_model=str(g("OPENROUTER_MODEL", "openai/gpt-5.4") or "openai/gpt-5.4").strip(),
        openai_api_key_set=bool((g("OPENAI_API_KEY", "") or "").strip()),
        anthropic_api_key_set=bool((g("ANTHROPIC_API_KEY", "") or "").strip()),
        openrouter_api_key_set=bool((g("OPENROUTER_API_KEY", "") or "").strip()),
        supabase_url=(g("SUPABASE_URL", "") or "").strip() or None,
        supabase_anon_key_set=bool((g("SUPABASE_ANON_KEY", "") or "").strip()),
        supabase_service_role_key=(g("SUPABASE_SERVICE_ROLE_KEY", "") or "").strip() or None,
        supabase_enabled=bool((g("SUPABASE_URL", "") or "").strip() and (g("SUPABASE_SERVICE_ROLE_KEY", "") or "").strip()),
    )


settings: Settings = load_settings()
