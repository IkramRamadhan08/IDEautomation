from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values, load_dotenv
from pydantic import BaseModel


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"

Provider = Literal["openai", "anthropic", "openrouter", "groq", "xai"]
BuildMode = Literal["full-agent", "hybrid"]


def load_env() -> None:
    load_dotenv(ENV_PATH, override=True)


class Settings(BaseModel):
    default_workspace: str | None = None
    llm_provider: Provider | None = None
    build_mode: BuildMode = "hybrid"
    openai_model: str = "gpt-5.4"
    anthropic_model: str = "claude-sonnet-4-0"
    openrouter_model: str = "openai/gpt-5.4"
    friendly_free_tier_mode: bool = True
    agent_refinement_mode: Literal["auto", "off", "always"] = "auto"
    agent_min_gap_seconds: float = 4.0
    agent_requests_per_minute: int = 8
    openai_requests_per_minute: int | None = None
    anthropic_requests_per_minute: int | None = None
    openrouter_requests_per_minute: int | None = None
    openai_api_key_set: bool = False
    anthropic_api_key_set: bool = False
    openrouter_api_key_set: bool = False
    supabase_url: str | None = None
    supabase_anon_key_set: bool = False
    supabase_service_role_key: str | None = None
    supabase_service_role_key_set: bool = False
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
    if raw_provider == "openai-codex":
        raw_provider = "openai"
    llm_provider = raw_provider if raw_provider in {"openai", "anthropic", "openrouter"} else None

    build_mode = str(g("BUILD_MODE", "hybrid") or "hybrid").strip().lower()
    if build_mode not in {"full-agent", "hybrid"}:
        build_mode = "hybrid"

    raw_friendly_mode = str(g("FRIENDLY_FREE_TIER_MODE", "true") or "true").strip().lower()
    raw_refinement_mode = str(g("AGENT_REFINEMENT_MODE", "auto") or "auto").strip().lower()
    if raw_refinement_mode not in {"auto", "off", "always"}:
        raw_refinement_mode = "auto"
    try:
        agent_min_gap_seconds = float(str(g("AGENT_MIN_GAP_SECONDS", "4.0") or "4.0").strip())
    except Exception:
        agent_min_gap_seconds = 4.0
    if agent_min_gap_seconds < 0.0:
        agent_min_gap_seconds = 0.0

    default_agent_rpm = 8 if raw_friendly_mode not in {"0", "false", "no", "off"} else 15

    def parse_optional_int(key: str, default: int | None = None) -> int | None:
        raw = str(g(key, "") or "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except Exception:
            return default
        return max(0, value)

    return Settings(
        default_workspace=(g("DEFAULT_WORKSPACE", "") or "").strip() or None,
        llm_provider=llm_provider,  # type: ignore[arg-type]
        build_mode=build_mode,  # type: ignore[arg-type]
        openai_model=str(g("OPENAI_MODEL", g("OPENAI_CODEX_MODEL", "gpt-5.4")) or "gpt-5.4").strip(),
        anthropic_model=str(g("ANTHROPIC_MODEL", "claude-sonnet-4-0") or "claude-sonnet-4-0").strip(),
        openrouter_model=str(g("OPENROUTER_MODEL", "openai/gpt-5.4") or "openai/gpt-5.4").strip(),
        friendly_free_tier_mode=raw_friendly_mode not in {"0", "false", "no", "off"},
        agent_refinement_mode=raw_refinement_mode,  # type: ignore[arg-type]
        agent_min_gap_seconds=agent_min_gap_seconds,
        agent_requests_per_minute=parse_optional_int("AGENT_REQUESTS_PER_MINUTE", default_agent_rpm) or default_agent_rpm,
        openai_requests_per_minute=parse_optional_int("OPENAI_REQUESTS_PER_MINUTE"),
        anthropic_requests_per_minute=parse_optional_int("ANTHROPIC_REQUESTS_PER_MINUTE"),
        openrouter_requests_per_minute=parse_optional_int("OPENROUTER_REQUESTS_PER_MINUTE"),
        openai_api_key_set=bool((g("OPENAI_API_KEY", "") or "").strip()),
        anthropic_api_key_set=bool((g("ANTHROPIC_API_KEY", "") or "").strip()),
        openrouter_api_key_set=bool((g("OPENROUTER_API_KEY", "") or "").strip()),
        supabase_url=(g("SUPABASE_URL", "") or "").strip() or None,
        supabase_anon_key_set=bool((g("SUPABASE_ANON_KEY", "") or "").strip()),
        supabase_service_role_key=(g("SUPABASE_SERVICE_ROLE_KEY", "") or "").strip() or None,
        supabase_service_role_key_set=bool((g("SUPABASE_SERVICE_ROLE_KEY", "") or "").strip()),
        supabase_enabled=bool((g("SUPABASE_URL", "") or "").strip() and (g("SUPABASE_SERVICE_ROLE_KEY", "") or "").strip()),
    )


settings: Settings = load_settings()
