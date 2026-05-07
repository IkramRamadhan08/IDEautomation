from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values, load_dotenv
from pydantic import BaseModel


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"

Provider = Literal["openai", "anthropic", "openrouter", "groq", "gemini", "together", "cerebras", "xai"]
BuildMode = Literal["full-agent", "hybrid"]


def load_env() -> None:
    load_dotenv(ENV_PATH, override=True)


class Settings(BaseModel):
    default_workspace: str | None = None
    llm_provider: Provider | None = None
    build_mode: BuildMode = "hybrid"
    openai_model: str = "gpt-5.5"
    anthropic_model: str = "claude-opus-4-7"
    openrouter_model: str = "x-ai/grok-4.3"
    groq_model: str = "groq/compound"
    gemini_model: str = "gemini-3-pro-preview"
    together_model: str = "deepseek-ai/DeepSeek-V4-Pro"
    cerebras_model: str = "zai-glm-4.7"
    xai_model: str = "grok-4.3"
    friendly_free_tier_mode: bool = True
    agent_refinement_mode: Literal["auto", "off", "always"] = "auto"
    agent_min_gap_seconds: float = 4.0
    agent_requests_per_minute: int = 8
    agent_context_char_budget: int = 48_000
    openai_requests_per_minute: int | None = None
    anthropic_requests_per_minute: int | None = None
    openrouter_requests_per_minute: int | None = None
    groq_requests_per_minute: int | None = None
    gemini_requests_per_minute: int | None = None
    together_requests_per_minute: int | None = None
    cerebras_requests_per_minute: int | None = None
    xai_requests_per_minute: int | None = None
    openai_api_key_set: bool = False
    anthropic_api_key_set: bool = False
    openrouter_api_key_set: bool = False
    groq_api_key_set: bool = False
    gemini_api_key_set: bool = False
    together_api_key_set: bool = False
    cerebras_api_key_set: bool = False
    xai_api_key_set: bool = False
    supabase_url: str | None = None
    supabase_frontend_ready: bool = False
    supabase_anon_key_set: bool = False
    supabase_service_role_key: str | None = None
    supabase_service_role_key_set: bool = False
    supabase_enabled: bool = False
    supabase_missing_env: list[str] = []


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

    def first_non_empty(*keys: str) -> str | None:
        for key in keys:
            value = str(g(key, "") or "").strip()
            if value:
                return value
        return None

    raw_provider = str(g("LLM_PROVIDER", "") or "").strip().lower()
    if raw_provider == "openai-codex":
        raw_provider = "openai"
    llm_provider = raw_provider if raw_provider in {"openai", "anthropic", "openrouter", "groq", "gemini", "together", "cerebras", "xai"} else None

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

    friendly_free_tier = raw_friendly_mode not in {"0", "false", "no", "off"}
    default_agent_rpm = 8 if friendly_free_tier else 15
    default_context_budget = 48_000 if friendly_free_tier else 140_000

    def parse_optional_int(key: str, default: int | None = None) -> int | None:
        raw = str(g(key, "") or "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except Exception:
            return default
        return max(0, value)

    supabase_url = first_non_empty("SUPABASE_URL", "VITE_SUPABASE_URL")
    supabase_anon_key = first_non_empty("SUPABASE_ANON_KEY", "VITE_SUPABASE_ANON_KEY")
    supabase_service_role_key = first_non_empty("SUPABASE_SERVICE_ROLE_KEY")
    supabase_frontend_ready = bool(supabase_url and supabase_anon_key)
    supabase_enabled = bool(supabase_url and supabase_service_role_key)
    supabase_missing_env: list[str] = []
    if not supabase_url:
        supabase_missing_env.append("SUPABASE_URL or VITE_SUPABASE_URL")
    if not supabase_anon_key:
        supabase_missing_env.append("VITE_SUPABASE_ANON_KEY")
    if not supabase_service_role_key:
        supabase_missing_env.append("SUPABASE_SERVICE_ROLE_KEY")

    return Settings(
        default_workspace=(g("DEFAULT_WORKSPACE", "") or "").strip() or None,
        llm_provider=llm_provider,  # type: ignore[arg-type]
        build_mode=build_mode,  # type: ignore[arg-type]
        openai_model=str(g("OPENAI_MODEL", g("OPENAI_CODEX_MODEL", "gpt-5.5")) or "gpt-5.5").strip(),
        anthropic_model=str(g("ANTHROPIC_MODEL", "claude-opus-4-7") or "claude-opus-4-7").strip(),
        openrouter_model=str(g("OPENROUTER_MODEL", "x-ai/grok-4.3") or "x-ai/grok-4.3").strip(),
        groq_model=str(g("GROQ_MODEL", "groq/compound") or "groq/compound").strip(),
        gemini_model=str(g("GEMINI_MODEL", "gemini-3-pro-preview") or "gemini-3-pro-preview").strip(),
        together_model=str(g("TOGETHER_MODEL", "deepseek-ai/DeepSeek-V4-Pro") or "deepseek-ai/DeepSeek-V4-Pro").strip(),
        cerebras_model=str(g("CEREBRAS_MODEL", "zai-glm-4.7") or "zai-glm-4.7").strip(),
        xai_model=str(g("XAI_MODEL", "grok-4.3") or "grok-4.3").strip(),
        friendly_free_tier_mode=raw_friendly_mode not in {"0", "false", "no", "off"},
        agent_refinement_mode=raw_refinement_mode,  # type: ignore[arg-type]
        agent_min_gap_seconds=agent_min_gap_seconds,
        agent_requests_per_minute=parse_optional_int("AGENT_REQUESTS_PER_MINUTE", default_agent_rpm) or default_agent_rpm,
        agent_context_char_budget=parse_optional_int("AGENT_CONTEXT_CHAR_BUDGET", default_context_budget) or default_context_budget,
        openai_requests_per_minute=parse_optional_int("OPENAI_REQUESTS_PER_MINUTE"),
        anthropic_requests_per_minute=parse_optional_int("ANTHROPIC_REQUESTS_PER_MINUTE"),
        openrouter_requests_per_minute=parse_optional_int("OPENROUTER_REQUESTS_PER_MINUTE"),
        groq_requests_per_minute=parse_optional_int("GROQ_REQUESTS_PER_MINUTE"),
        gemini_requests_per_minute=parse_optional_int("GEMINI_REQUESTS_PER_MINUTE"),
        together_requests_per_minute=parse_optional_int("TOGETHER_REQUESTS_PER_MINUTE"),
        cerebras_requests_per_minute=parse_optional_int("CEREBRAS_REQUESTS_PER_MINUTE"),
        xai_requests_per_minute=parse_optional_int("XAI_REQUESTS_PER_MINUTE"),
        openai_api_key_set=bool((g("OPENAI_API_KEY", "") or "").strip()),
        anthropic_api_key_set=bool((g("ANTHROPIC_API_KEY", "") or "").strip()),
        openrouter_api_key_set=bool((g("OPENROUTER_API_KEY", "") or "").strip()),
        groq_api_key_set=bool((g("GROQ_API_KEY", "") or "").strip()),
        gemini_api_key_set=bool((g("GEMINI_API_KEY", "") or "").strip()),
        together_api_key_set=bool((g("TOGETHER_API_KEY", "") or "").strip()),
        cerebras_api_key_set=bool((g("CEREBRAS_API_KEY", "") or "").strip()),
        xai_api_key_set=bool((g("XAI_API_KEY", "") or "").strip()),
        supabase_url=supabase_url,
        supabase_frontend_ready=supabase_frontend_ready,
        supabase_anon_key_set=bool(supabase_anon_key),
        supabase_service_role_key=supabase_service_role_key,
        supabase_service_role_key_set=bool(supabase_service_role_key),
        supabase_enabled=supabase_enabled,
        supabase_missing_env=supabase_missing_env,
    )


settings: Settings = load_settings()
