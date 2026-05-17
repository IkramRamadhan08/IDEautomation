from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Depends, Header
from pydantic import BaseModel

from api import settings as settings_mod
from api.settings import ENV_PATH, ROOT
from api.auth_identity import resolve_request_user
from api.supabase_store import get_agent_memory_chunks_table_status, has_supabase
from api.preferences import UserPreferencesUpdateReq, upsert_user_preferences
from api.secrets_store import delete_provider_secret, upsert_provider_secret
import os


class ProviderStatus(BaseModel):
    provider: str
    connected: bool
    hint: str | None = None
    profile_id: str | None = None
    account_id: str | None = None
    auth_type: str | None = None
    project_id: str | None = None
    source: str | None = None
    managed_free: bool = False
    recommended_model: str | None = None
    free_tier_models: list[str] = []


class SettingsInfo(BaseModel):
    default_workspace: str | None
    llm_provider: str | None
    build_mode: str
    nine_router_base_url: str = "http://127.0.0.1:20128/v1"
    nine_router_model: str = "free-forever"
    openai_model: str
    anthropic_model: str
    openrouter_model: str
    groq_model: str
    gemini_model: str
    together_model: str
    cerebras_model: str
    xai_model: str
    friendly_free_tier_mode: bool = True
    agent_refinement_mode: str = "auto"
    agent_min_gap_seconds: float = 4.0
    agent_requests_per_minute: int = 8
    nine_router_api_key_set: bool = False
    managed_nine_router_enabled: bool = False
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
    supabase_service_role_key_set: bool = False
    supabase_enabled: bool = False
    supabase_rag_status: str = "unconfigured"
    supabase_warning: str | None = None
    supabase_missing_env: list[str] = []
    providers: dict[str, ProviderStatus]


class SettingsUpdateReq(BaseModel):
    default_workspace: str | None = None
    llm_provider: str | None = None
    build_mode: str | None = None
    nine_router_base_url: str | None = None
    nine_router_model: str | None = None
    openai_model: str | None = None
    anthropic_model: str | None = None
    openrouter_model: str | None = None
    groq_model: str | None = None
    gemini_model: str | None = None
    together_model: str | None = None
    cerebras_model: str | None = None
    xai_model: str | None = None
    friendly_free_tier_mode: bool | None = None
    agent_refinement_mode: str | None = None
    agent_min_gap_seconds: float | None = None
    agent_requests_per_minute: int | None = None
    nine_router_api_key: str | None = None
    openai_requests_per_minute: int | None = None
    anthropic_requests_per_minute: int | None = None
    openrouter_requests_per_minute: int | None = None
    groq_requests_per_minute: int | None = None
    gemini_requests_per_minute: int | None = None
    together_requests_per_minute: int | None = None
    cerebras_requests_per_minute: int | None = None
    xai_requests_per_minute: int | None = None
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    openrouter_api_key: str | None = None
    groq_api_key: str | None = None
    gemini_api_key: str | None = None
    together_api_key: str | None = None
    cerebras_api_key: str | None = None
    xai_api_key: str | None = None


class NineRouterTestReq(BaseModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str = "free-forever"


def build_settings_router(*, session_state, env_set, env_unset, reload_settings):
    router = APIRouter(prefix="/api", tags=["settings"])

    @router.get("/settings", response_model=SettingsInfo)
    def get_settings():
        from .oauth_runtime import auth_snapshot, managed_nine_router_available

        s = settings_mod.settings
        statuses = auth_snapshot(session_state().get("workspace"))
        supabase_rag_status = get_agent_memory_chunks_table_status() if getattr(s, "supabase_enabled", False) else "unconfigured"
        supabase_warning = None
        if getattr(s, "supabase_frontend_ready", False) and not getattr(s, "supabase_service_role_key_set", False):
            supabase_warning = "Frontend auth Supabase udah keisi, tapi backend belum punya SUPABASE_SERVICE_ROLE_KEY. Login bisa siap, tapi sync RAG dan persistence server-side belum live."
        elif not getattr(s, "supabase_url", None):
            supabase_warning = "Supabase belum dikonfigurasi. Isi SUPABASE_URL/VITE_SUPABASE_URL, VITE_SUPABASE_ANON_KEY, dan SUPABASE_SERVICE_ROLE_KEY."
        elif supabase_rag_status == "missing":
            supabase_warning = "Supabase udah nyambung, tapi tabel public.agent_memory_chunks belum dibuat. Jalankan docs/supabase-agent-rag.sql dulu."
        elif supabase_rag_status == "error":
            supabase_warning = "Supabase kebaca, tapi backend belum bisa verifikasi tabel agent_memory_chunks sekarang."

        if has_supabase() and not (os.getenv("VOICEIDE_SECRET_KEY") or "").strip():
            extra = "Hosted provider secret storage belum aktif (VOICEIDE_SECRET_KEY belum di-set). BYOK key akan disimpan ke .env di mode lokal."
            supabase_warning = f"{supabase_warning} {extra}".strip() if supabase_warning else extra
        return SettingsInfo(
            default_workspace=s.default_workspace,
            llm_provider=s.llm_provider,
            build_mode=s.build_mode,
            nine_router_base_url=str((statuses.get("nine_router") or {}).get("base_url") or getattr(s, "nine_router_base_url", "http://127.0.0.1:20128/v1")),
            nine_router_model=getattr(s, "nine_router_model", "free-forever"),
            openai_model=s.openai_model,
            anthropic_model=getattr(s, "anthropic_model", "claude-sonnet-4-0"),
            openrouter_model=getattr(s, "openrouter_model", "openrouter/free"),
            groq_model=getattr(s, "groq_model", "llama-3.3-70b-versatile"),
            gemini_model=getattr(s, "gemini_model", "gemini-2.5-flash"),
            together_model=getattr(s, "together_model", "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
            cerebras_model=getattr(s, "cerebras_model", "gpt-oss-120b"),
            xai_model=getattr(s, "xai_model", "grok-4-fast-reasoning"),
            friendly_free_tier_mode=bool(getattr(s, "friendly_free_tier_mode", True)),
            agent_refinement_mode=str(getattr(s, "agent_refinement_mode", "auto")),
            agent_min_gap_seconds=float(getattr(s, "agent_min_gap_seconds", 4.0) or 4.0),
            agent_requests_per_minute=int(getattr(s, "agent_requests_per_minute", 8) or 8),
            nine_router_api_key_set=getattr(s, "nine_router_api_key_set", False),
            managed_nine_router_enabled=managed_nine_router_available(),
            openai_requests_per_minute=getattr(s, "openai_requests_per_minute", None),
            anthropic_requests_per_minute=getattr(s, "anthropic_requests_per_minute", None),
            openrouter_requests_per_minute=getattr(s, "openrouter_requests_per_minute", None),
            groq_requests_per_minute=getattr(s, "groq_requests_per_minute", None),
            gemini_requests_per_minute=getattr(s, "gemini_requests_per_minute", None),
            together_requests_per_minute=getattr(s, "together_requests_per_minute", None),
            cerebras_requests_per_minute=getattr(s, "cerebras_requests_per_minute", None),
            xai_requests_per_minute=getattr(s, "xai_requests_per_minute", None),
            openai_api_key_set=s.openai_api_key_set,
            anthropic_api_key_set=getattr(s, "anthropic_api_key_set", False),
            openrouter_api_key_set=getattr(s, "openrouter_api_key_set", False),
            groq_api_key_set=getattr(s, "groq_api_key_set", False),
            gemini_api_key_set=getattr(s, "gemini_api_key_set", False),
            together_api_key_set=getattr(s, "together_api_key_set", False),
            cerebras_api_key_set=getattr(s, "cerebras_api_key_set", False),
            xai_api_key_set=getattr(s, "xai_api_key_set", False),
            supabase_url=getattr(s, "supabase_url", None),
            supabase_frontend_ready=getattr(s, "supabase_frontend_ready", False),
            supabase_anon_key_set=getattr(s, "supabase_anon_key_set", False),
            supabase_service_role_key_set=getattr(s, "supabase_service_role_key_set", False),
            supabase_enabled=getattr(s, "supabase_enabled", False),
            supabase_rag_status=supabase_rag_status,
            supabase_warning=supabase_warning,
            supabase_missing_env=list(getattr(s, "supabase_missing_env", []) or []),
            providers={
                "nine_router": ProviderStatus(**statuses.get("nine_router", {})),
                "openai": ProviderStatus(**statuses.get("openai", statuses.get("openai_codex", {}))),
                "anthropic": ProviderStatus(**statuses.get("anthropic", {})),
                "openrouter": ProviderStatus(**statuses.get("openrouter", {})),
                "groq": ProviderStatus(**statuses.get("groq", {})),
                "gemini": ProviderStatus(**statuses.get("gemini", {})),
                "together": ProviderStatus(**statuses.get("together", {})),
                "cerebras": ProviderStatus(**statuses.get("cerebras", {})),
                "xai": ProviderStatus(**statuses.get("xai", {})),
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

    @router.get("/providers")
    def get_provider_catalog():
        from .oauth_runtime import provider_catalog

        return {"ok": True, "providers": provider_catalog()}

    @router.get("/model-routes/diagnose")
    def diagnose_model_route(provider: str = Query("nine_router"), model: str = Query("free-forever")):
        from .agent_router import normalize_smart_route
        from .oauth_runtime import auth_snapshot

        selected_provider = "nine_router"
        selected_model = (model or "free-forever").strip()
        snapshot = auth_snapshot(session_state().get("workspace"))
        connected = bool((snapshot.get("nine_router") or {}).get("connected"))
        statuses = {
            name: {
                "connected": bool(status.get("connected")) if isinstance(status, dict) else False,
                "hint": status.get("hint") if isinstance(status, dict) else None,
            }
            for name, status in snapshot.items()
        }
        route = normalize_smart_route(selected_model)
        skipped: list[str] = []
        attempts: list[dict] = [{
            "provider": "nine_router",
            "model": selected_model,
            "source": route or "direct",
            "tier": "9Router gateway",
            "connected": connected,
        }]
        metadata: dict[str, str] = {
            "description": "Appora passes the model/combo through to 9Router unchanged.",
            "fallback": "Provider fallback, quota checks, and combo expansion happen inside 9Router.",
        }
        if not connected:
            skipped.append("nine_router: endpoint/API key belum connected")
        if route:
            summary = (
                f"Combo {route} siap dikirim ke 9Router."
                if connected
                else f"Combo {route} butuh 9Router endpoint dan API key."
            )
        else:
            summary = (
                f"Model {selected_model} siap dikirim ke 9Router."
                if connected
                else f"Model {selected_model} butuh 9Router endpoint dan API key."
            )

        return {
            "ok": connected,
            "provider": selected_provider,
            "model": selected_model,
            "route": route or None,
            "summary": summary,
            "attempts": attempts,
            "skipped": skipped[:20],
            "metadata": metadata,
            "provider_status": statuses,
        }

    @router.post("/model-routes/test")
    def test_model_route(req: NineRouterTestReq):
        from .oauth_runtime import test_nine_router_route

        try:
            return test_nine_router_route(model=req.model, base_url=req.base_url, api_key=req.api_key)
        except Exception as exc:
            return {"ok": False, "status": 0, "summary": f"9Router test gagal: {exc}", "model": req.model}

    @router.put("/settings")
    def update_settings(req: SettingsUpdateReq, authorization: str | None = Header(default=None), x_voiceide_user: str | None = Header(default=None)):
        user = resolve_request_user(authorization=authorization, x_voiceide_user=x_voiceide_user)
        changed: list[str] = []

        is_serverless = bool(
            (os.getenv("VERCEL") or "").strip()
            or (os.getenv("VERCEL_ENV") or "").strip()
            or (os.getenv("RAILWAY_ENVIRONMENT") or "").strip()
            or (os.getenv("RAILWAY_PROJECT_ID") or "").strip()
        )
        secrets_ready = has_supabase() and bool((os.getenv("VOICEIDE_SECRET_KEY") or "").strip())
        hosted_mode = secrets_ready and user.auth_source == "supabase"
        secret_updates = [req.nine_router_api_key]
        has_secret_updates = any(value is not None for value in secret_updates)
        storage_note = None
        if user.auth_source == "supabase" and has_secret_updates and not secrets_ready:
            storage_note = (
                "Supabase login terdeteksi, tapi hosted secret storage belum siap (VOICEIDE_SECRET_KEY belum di-set). "
                "Jadi API key disimpan ke .env (local/dev) bukan per-akun."
            )

        all_updates = [
            req.default_workspace,
            req.llm_provider,
            req.build_mode,
            req.nine_router_base_url,
            req.nine_router_model,
            req.friendly_free_tier_mode,
            req.agent_refinement_mode,
            req.agent_min_gap_seconds,
            req.agent_requests_per_minute,
            req.nine_router_api_key,
            *secret_updates,
        ]
        if is_serverless and not hosted_mode and any(value is not None for value in all_updates):
            raise HTTPException(
                503,
                "Hosted deploy tidak mendukung penyimpanan settings ke file .env. "
                "Login Supabase + set VOICEIDE_SECRET_KEY (untuk hosted secret storage) agar settings & BYOK key tersimpan aman per-akun."
            )

        if hosted_mode:
            try:
                secret_profile_id = (user.user_id or "").strip()
                if not secret_profile_id:
                    raise HTTPException(400, "Hosted secret storage membutuhkan profile internal yang valid.")

                if req.nine_router_api_key is not None:
                    key = req.nine_router_api_key.strip()
                    if key:
                        upsert_provider_secret(profile_id=secret_profile_id, provider="nine_router", api_key=key)
                    else:
                        delete_provider_secret(profile_id=secret_profile_id, provider="nine_router")
                    changed.append("nine_router_api_key")
                if req.nine_router_base_url is not None:
                    base_url = req.nine_router_base_url.strip().rstrip("/")
                    if base_url:
                        upsert_provider_secret(profile_id=secret_profile_id, provider="nine_router_base_url", api_key=base_url)
                    else:
                        delete_provider_secret(profile_id=secret_profile_id, provider="nine_router_base_url")
                    changed.append("nine_router_base_url")
                pref_profile_id = user.user_id
                pref_req = UserPreferencesUpdateReq(
                    llm_provider="nine_router",
                    build_mode=req.build_mode,
                    openai_model=None,
                    anthropic_model=None,
                    nine_router_model=req.nine_router_model,
                    openrouter_model=None,
                    groq_model=None,
                    gemini_model=None,
                    together_model=None,
                    cerebras_model=None,
                    xai_model=None,
                )
                upsert_user_preferences(profile_id=pref_profile_id, req=pref_req)
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(400, f"Hosted settings save failed: {exc}")
            changed.append("llm_provider")
            if req.build_mode is not None:
                changed.append("build_mode")
            if req.nine_router_base_url is not None:
                changed.append("nine_router_base_url")
            if req.nine_router_model is not None:
                changed.append("nine_router_model")
            return {"ok": True, "changed": changed, "storage": "hosted_preferences", "note": "Advanced local runtime knobs stay env-backed for now."}

        mapping: list[tuple[str, str | None]] = [
            ("DEFAULT_WORKSPACE", req.default_workspace if req.default_workspace is not None else None),
            ("LLM_PROVIDER", "nine_router" if req.llm_provider is not None else None),
            ("BUILD_MODE", req.build_mode),
            ("NINE_ROUTER_BASE_URL", req.nine_router_base_url.strip().rstrip("/") if req.nine_router_base_url is not None else None),
            ("NINE_ROUTER_MODEL", req.nine_router_model),
            ("FRIENDLY_FREE_TIER_MODE", None if req.friendly_free_tier_mode is None else ("true" if req.friendly_free_tier_mode else "false")),
            ("AGENT_REFINEMENT_MODE", req.agent_refinement_mode),
            ("AGENT_MIN_GAP_SECONDS", None if req.agent_min_gap_seconds is None else str(req.agent_min_gap_seconds)),
            ("AGENT_REQUESTS_PER_MINUTE", None if req.agent_requests_per_minute is None else str(req.agent_requests_per_minute)),
            ("NINE_ROUTER_API_KEY", req.nine_router_api_key),
        ]

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
            if env_key in {"LLM_PROVIDER", "DEFAULT_WORKSPACE", "NINE_ROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "TOGETHER_API_KEY", "CEREBRAS_API_KEY", "XAI_API_KEY"} and not str(val).strip():
                env_unset(env_key)
                changed.append(env_key)
                continue
            env_set(env_key, val)
            changed.append(env_key)

        reload_settings()
        return {"ok": True, "changed": changed, "storage": "env", "note": storage_note}

    return router
