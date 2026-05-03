from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from api import settings as settings_mod
from api.settings import ENV_PATH, ROOT


class ProviderStatus(BaseModel):
    provider: str
    connected: bool
    hint: str | None = None
    profile_id: str | None = None
    account_id: str | None = None
    auth_type: str | None = None
    project_id: str | None = None
    source: str | None = None


class SettingsInfo(BaseModel):
    default_workspace: str | None
    llm_provider: str | None
    build_mode: str
    openai_model: str
    anthropic_model: str
    openrouter_model: str
    openai_api_key_set: bool = False
    anthropic_api_key_set: bool = False
    openrouter_api_key_set: bool = False
    supabase_url: str | None = None
    supabase_anon_key_set: bool = False
    supabase_enabled: bool = False
    providers: dict[str, ProviderStatus]


class SettingsUpdateReq(BaseModel):
    default_workspace: str | None = None
    llm_provider: str | None = None
    build_mode: str | None = None
    openai_model: str | None = None
    anthropic_model: str | None = None
    openrouter_model: str | None = None
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    openrouter_api_key: str | None = None


def build_settings_router(*, session_state, env_set, env_unset, reload_settings):
    router = APIRouter(prefix="/api", tags=["settings"])

    @router.get("/settings", response_model=SettingsInfo)
    def get_settings():
        from .oauth_runtime import auth_snapshot

        s = settings_mod.settings
        statuses = auth_snapshot(session_state().get("workspace"))
        return SettingsInfo(
            default_workspace=s.default_workspace,
            llm_provider=s.llm_provider,
            build_mode=s.build_mode,
            openai_model=s.openai_model,
            anthropic_model=getattr(s, "anthropic_model", "claude-sonnet-4-0"),
            openrouter_model=getattr(s, "openrouter_model", "openai/gpt-5.4"),
            openai_api_key_set=s.openai_api_key_set,
            anthropic_api_key_set=getattr(s, "anthropic_api_key_set", False),
            openrouter_api_key_set=getattr(s, "openrouter_api_key_set", False),
            supabase_url=getattr(s, "supabase_url", None),
            supabase_anon_key_set=getattr(s, "supabase_anon_key_set", False),
            supabase_enabled=getattr(s, "supabase_enabled", False),
            providers={
                "openai": ProviderStatus(**statuses.get("openai", statuses.get("openai_codex", {}))),
                "anthropic": ProviderStatus(**statuses.get("anthropic", {})),
                "openrouter": ProviderStatus(**statuses.get("openrouter", {})),
            },
        )

    @router.get("/models")
    def list_models(provider: str = Query("", description="llm provider, e.g. openai|anthropic|openrouter")):
        from .oauth_runtime import list_models as oauth_list_models

        prov = provider.lower().strip()
        try:
            return {"provider": prov, "models": oauth_list_models(prov)}
        except RuntimeError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:
            raise HTTPException(500, str(exc))

    @router.put("/settings")
    def update_settings(req: SettingsUpdateReq):
        mapping: list[tuple[str, str | None]] = [
            ("DEFAULT_WORKSPACE", req.default_workspace if req.default_workspace is not None else None),
            ("LLM_PROVIDER", req.llm_provider),
            ("BUILD_MODE", req.build_mode),
            ("OPENAI_MODEL", req.openai_model),
            ("OPENAI_CODEX_MODEL", req.openai_model),
            ("ANTHROPIC_MODEL", req.anthropic_model),
            ("OPENROUTER_MODEL", req.openrouter_model),
            ("OPENAI_API_KEY", req.openai_api_key),
            ("ANTHROPIC_API_KEY", req.anthropic_api_key),
            ("OPENROUTER_API_KEY", req.openrouter_api_key),
        ]

        changed: list[str] = []

        if not ENV_PATH.exists():
            import shutil

            example = ROOT / ".env.example"
            if example.exists():
                shutil.copyfile(example, ENV_PATH)
            else:
                ENV_PATH.write_text("", encoding="utf-8")

        for env_key, val in mapping:
            if val is None:
                continue
            if env_key in {"LLM_PROVIDER", "DEFAULT_WORKSPACE", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"} and not str(val).strip():
                env_unset(env_key)
                changed.append(env_key)
                continue
            env_set(env_key, val)
            changed.append(env_key)

        reload_settings()
        return {"ok": True, "changed": changed}

    return router
