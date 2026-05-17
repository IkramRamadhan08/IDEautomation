from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from api.secrets_store import get_provider_secret, has_provider_secret

OPENAI_PROVIDER = "openai"
NINE_ROUTER_PROVIDER = "nine_router"
ANTHROPIC_PROVIDER = "anthropic"
OPENROUTER_PROVIDER = "openrouter"
GROQ_PROVIDER = "groq"
GEMINI_PROVIDER = "gemini"
TOGETHER_PROVIDER = "together"
CEREBRAS_PROVIDER = "cerebras"
XAI_PROVIDER = "xai"
SUPPORTED_PROVIDERS = (
    NINE_ROUTER_PROVIDER,
    OPENAI_PROVIDER,
    ANTHROPIC_PROVIDER,
    OPENROUTER_PROVIDER,
    GROQ_PROVIDER,
    GEMINI_PROVIDER,
    TOGETHER_PROVIDER,
    CEREBRAS_PROVIDER,
    XAI_PROVIDER,
)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_IDENTITY_SCOPES = ["openid"]

OPENAI_LOGIN_HINT = "Masukkan OpenAI API key di Settings. Kalau akun punya free trial/account credits, API akan memakai credit itu dulu; setelah habis OpenAI tetap billed per token."
ANTHROPIC_LOGIN_HINT = "Masukkan Anthropic API key di Settings."
OPENROUTER_LOGIN_HINT = "Masukkan OpenRouter API key di Settings. OpenRouter paling cocok untuk user yang mau coba model gratis/hemat."
GROQ_LOGIN_HINT = "Masukkan Groq API key di Settings. Groq cocok buat user yang mau coba agent cepat dengan free plan/rate limit."
GEMINI_LOGIN_HINT = "Masukkan Gemini API key dari Google AI Studio. Gemini cocok buat user Google yang mau mulai dari free quota/rate limit."
TOGETHER_LOGIN_HINT = "Masukkan Together AI API key. Together cocok buat akses banyak model open-source lewat API OpenAI-compatible."
CEREBRAS_LOGIN_HINT = "Masukkan Cerebras API key. Cerebras cocok buat model open-source cepat dengan free/dev tier limit."
NINE_ROUTER_LOGIN_HINT = "Jalankan 9Router, buka dashboard 9Router, lalu paste endpoint /v1 dan API key. Default lokal: http://127.0.0.1:20128/v1. Kalau Appora backend hosted/Railway, endpoint harus reachable dari backend."
XAI_LOGIN_HINT = "Masukkan xAI API key. xAI cocok buat user yang ingin model Grok, biasanya paid/API-credit based."

CURRENT_PROFILE_ID: ContextVar[str | None] = ContextVar("voiceide_profile_id", default=None)
_PROVIDER_COOLDOWN_LOCK = threading.Lock()
_PROVIDER_COOLDOWN_UNTIL: dict[str, float] = {}
_MANAGED_QUOTA_LOCK = threading.Lock()
_MANAGED_QUOTA_USAGE: dict[str, dict[str, int]] = {}
_MANAGED_FREE_MODELS = {"free-forever", "always-on", "openclaw-free", "fast-free"}
_MANAGED_FREE_MODEL_PREFIXES = (
    "kr/",
    "oc/",
    "vertex/",
    "gemini/",
    "openrouter/openrouter/free",
    "openrouter/free",
)

HOSTED_PROVIDER_CATALOG: dict[str, dict[str, Any]] = {
    NINE_ROUTER_PROVIDER: {
        "label": "9Router",
        "positioning": "Single OpenAI-compatible router for Claude Code, Codex, Cursor, Kiro, OpenCode, OpenRouter, Gemini, Groq, and other 9Router models.",
        "recommended_model": "free-forever",
        "free_tier_models": [
            "free-forever",
            "always-on",
            "openclaw-free",
            "kr/claude-sonnet-4.5",
            "kr/glm-5",
            "oc/<auto>",
            "vertex/gemini-3-flash-preview",
            "openrouter/openrouter/free",
        ],
        "paid_models": [
            "maximize-claude",
            "coding-auto",
            "cheap-auto",
            "quality-auto",
            "cx/gpt-5.5",
            "cx/gpt-5.4",
            "cc/claude-opus-4-7",
            "cc/claude-sonnet-4-6",
            "gh/claude-sonnet-4.6",
            "cu/gpt-5.3-codex",
        ],
        "hint": "Recommended. Jalankan/host 9Router, lalu paste endpoint /v1 dan API key dari dashboard 9Router.",
    },
    OPENROUTER_PROVIDER: {
        "label": "OpenRouter",
        "positioning": "Best for trial users, smart free-first routing, and free/cheap model routing.",
        "recommended_model": "free-forever",
        "free_tier_models": [
            "free-forever",
            "openrouter/free",
            "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
            "deepseek/deepseek-v4-flash:free",
            "deepseek/deepseek-chat-v3-0324:free",
            "deepseek/deepseek-r1:free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "google/gemini-2.0-flash-exp:free",
        ],
        "paid_models": [
            "x-ai/grok-4.3",
            "openai/gpt-5.5",
            "openai/gpt-5.5-pro",
            "google/gemini-3.1-pro-preview",
            "google/gemini-3.1-flash-lite-preview",
            "anthropic/claude-opus-4.7",
            "deepseek/deepseek-v4-pro",
            "moonshotai/kimi-k2.6",
            "openai/gpt-4o-mini",
            "openai/gpt-4.1-mini",
            "google/gemini-2.5-flash",
            "anthropic/claude-3.5-haiku",
            "openai/gpt-5.4",
            "anthropic/claude-sonnet-4",
        ],
        "hint": "Recommended default for public demos because users can bring one OpenRouter key and route to free models first.",
    },
    OPENAI_PROVIDER: {
        "label": "OpenAI",
        "positioning": "Most familiar for non-coders; works with trial/account credits when available, otherwise token-billed.",
        "recommended_model": "gpt-5.5",
        "free_tier_models": [],
        "paid_models": ["gpt-5.5", "gpt-5.5-pro", "gpt-5.4", "gpt-5.4-pro", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-4.1-nano", "gpt-4o-mini"],
        "hint": "Familiar choice for most users. It can feel free while trial/account credits exist, but it is not an unlimited free-tier model.",
    },
    ANTHROPIC_PROVIDER: {
        "label": "Anthropic",
        "positioning": "Strong for careful edits and long context if the user has Anthropic credits.",
        "recommended_model": "claude-opus-4-7",
        "free_tier_models": [],
        "paid_models": ["claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5", "claude-3-7-sonnet-latest", "claude-3-5-haiku-latest"],
        "hint": "Good reasoning quality, but usually paid/API-credit based.",
    },
    GROQ_PROVIDER: {
        "label": "Groq",
        "positioning": "Fast OpenAI-compatible free-plan path for quick web/app generation with rate limits.",
        "recommended_model": "groq/compound",
        "free_tier_models": [
            "groq/compound",
            "groq/compound-mini",
            "meta-llama/llama-4-scout-17b-16e-instruct",
            "qwen/qwen3-32b",
            "llama-3.3-70b-versatile",
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
            "llama-3.1-8b-instant",
        ],
        "paid_models": [],
        "hint": "Good for users who want a free-plan key and fast generation. It may hit 429 limits sooner than paid providers.",
    },
    GEMINI_PROVIDER: {
        "label": "Gemini",
        "positioning": "Google AI Studio path with free quota/rate limits on supported Gemini API models.",
        "recommended_model": "gemini-3-pro-preview",
        "free_tier_models": [
            "gemini-3-flash-preview",
            "gemini-3.1-flash-lite-preview",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
        ],
        "paid_models": ["gemini-3-pro-preview", "gemini-3.1-pro-preview", "gemini-2.5-pro", "gemini-1.5-pro"],
        "hint": "Good for users who already know Google/Gemini. Free quota exists but can hit 429 quickly.",
    },
    TOGETHER_PROVIDER: {
        "label": "Together AI",
        "positioning": "Large open-source model catalog through an OpenAI-compatible API.",
        "recommended_model": "deepseek-ai/DeepSeek-V4-Pro",
        "free_tier_models": [],
        "paid_models": [
            "deepseek-ai/DeepSeek-V4-Pro",
            "MiniMaxAI/MiniMax-M2.5",
            "Qwen/Qwen3.5-397B-A17B",
            "moonshotai/Kimi-K2.5",
            "meta-llama/Llama-3.3-70B-Instruct-Turbo",
            "openai/gpt-oss-120b",
            "meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8",
            "deepseek-ai/DeepSeek-R1",
            "deepseek-ai/DeepSeek-V3",
        ],
        "hint": "Good if users want many open models in one place. Usually pay-as-you-go/trial-credit based.",
    },
    CEREBRAS_PROVIDER: {
        "label": "Cerebras",
        "positioning": "Very fast inference for supported open models with free/dev tier limits.",
        "recommended_model": "zai-glm-4.7",
        "free_tier_models": ["zai-glm-4.7", "gpt-oss-120b", "llama3.1-8b"],
        "paid_models": ["qwen-3-235b-a22b-instruct-2507"],
        "hint": "Good for fast generation. Model catalog is smaller, but latency is strong.",
    },
    XAI_PROVIDER: {
        "label": "xAI",
        "positioning": "Grok model path for users with xAI API access.",
        "recommended_model": "grok-4.3",
        "free_tier_models": [],
        "paid_models": ["grok-4.3", "grok-4-fast-reasoning", "grok-4-fast-non-reasoning", "grok-4", "grok-3", "grok-3-mini"],
        "hint": "Good for Grok users. Treat as paid/API-credit based unless the account has credits.",
    },
}

def _google_oauth_client_id() -> str:
    return (os.getenv("GOOGLE_OAUTH_CLIENT_ID") or os.getenv("GEMINI_OAUTH_CLIENT_ID") or "").strip()


def _google_oauth_client_secret() -> str:
    return (os.getenv("GOOGLE_OAUTH_CLIENT_SECRET") or os.getenv("GEMINI_OAUTH_CLIENT_SECRET") or "").strip()


def _provider_key_from_env_or_secret(provider: str) -> str:
    return str(_provider_key_info(provider).get("key") or "")


def _managed_nine_router_base_url() -> str:
    return (
        os.getenv("APPORA_MANAGED_9ROUTER_BASE_URL")
        or os.getenv("APPORA_FREE_9ROUTER_BASE_URL")
        or ""
    ).strip().rstrip("/")


def _managed_nine_router_api_key() -> str:
    return (
        os.getenv("APPORA_MANAGED_9ROUTER_API_KEY")
        or os.getenv("APPORA_FREE_9ROUTER_API_KEY")
        or ""
    ).strip()


def _managed_free_model() -> str:
    return (os.getenv("APPORA_MANAGED_FREE_MODEL") or os.getenv("APPORA_FREE_MODEL") or "free-forever").strip() or "free-forever"


def managed_nine_router_available() -> bool:
    return bool(_managed_nine_router_base_url() and _managed_nine_router_api_key())


def _is_managed_free_model(model: str) -> bool:
    clean = (model or "").strip()
    return (
        clean in _MANAGED_FREE_MODELS
        or clean == _managed_free_model()
        or any(clean.startswith(prefix) for prefix in _MANAGED_FREE_MODEL_PREFIXES)
    )


def _resolve_managed_free_route_model(model: str, source: str) -> str:
    clean = (model or "").strip() or "free-forever"
    if source != "appora_managed_free":
        return clean
    resolved = _managed_free_model()
    if clean in _MANAGED_FREE_MODELS and resolved and resolved != clean:
        return resolved
    return clean


def _managed_quota_limit() -> int:
    raw = (os.getenv("APPORA_FREE_DAILY_MESSAGES") or os.getenv("APPORA_FREE_DAILY_AGENT_RUNS") or "30").strip()
    try:
        value = int(raw)
    except Exception:
        value = 30
    return max(1, min(value, 500))


def _managed_quota_subject() -> str:
    return CURRENT_PROFILE_ID.get() or "anonymous"


def _managed_quota_key() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _consume_managed_quota() -> tuple[bool, int, int]:
    limit = _managed_quota_limit()
    subject = _managed_quota_subject()
    day = _managed_quota_key()
    key = f"{day}:{subject}"
    with _MANAGED_QUOTA_LOCK:
        usage = _MANAGED_QUOTA_USAGE.setdefault(key, {"count": 0})
        if usage["count"] >= limit:
            return False, usage["count"], limit
        usage["count"] += 1
        return True, usage["count"], limit


def _nine_router_credentials_for_model(model: str, *, api_key: str | None = None, base_url: str | None = None) -> dict[str, Any]:
    user_key = (api_key or _provider_key_from_env_or_secret(NINE_ROUTER_PROVIDER)).strip()
    user_base = (base_url or _nine_router_base_url()).strip().rstrip("/")
    if user_key:
        return {"api_key": user_key, "base_url": user_base, "source": "user"}
    if _is_managed_free_model(model) and managed_nine_router_available():
        return {
            "api_key": _managed_nine_router_api_key(),
            "base_url": _managed_nine_router_base_url(),
            "source": "appora_managed_free",
        }
    return {"api_key": "", "base_url": user_base, "source": None}


def _env_key_for_provider(provider: str) -> str:
    env_map = {
        NINE_ROUTER_PROVIDER: "NINE_ROUTER_API_KEY",
        OPENAI_PROVIDER: "OPENAI_API_KEY",
        ANTHROPIC_PROVIDER: "ANTHROPIC_API_KEY",
        OPENROUTER_PROVIDER: "OPENROUTER_API_KEY",
        GROQ_PROVIDER: "GROQ_API_KEY",
        GEMINI_PROVIDER: "GEMINI_API_KEY",
        TOGETHER_PROVIDER: "TOGETHER_API_KEY",
        CEREBRAS_PROVIDER: "CEREBRAS_API_KEY",
        XAI_PROVIDER: "XAI_API_KEY",
    }
    env_key = env_map.get(provider, "")
    if not env_key:
        return ""
    value = (os.getenv(env_key) or "").strip()
    if value:
        return value
    if provider == NINE_ROUTER_PROVIDER:
        return (os.getenv("APPORA_9ROUTER_API_KEY") or "").strip()
    return ""


def _provider_key_info(provider: str) -> dict[str, Any]:
    profile_id = CURRENT_PROFILE_ID.get()
    if profile_id:
        try:
            stored = has_provider_secret(profile_id=profile_id, provider=provider)
            secret = (get_provider_secret(profile_id=profile_id, provider=provider) or "").strip()
            if secret:
                return {"key": secret, "source": "hosted_secret", "stored": True, "decrypt_error": False}
            if stored:
                return {"key": "", "source": "hosted_secret_unreadable", "stored": True, "decrypt_error": True}
        except Exception:
            pass

    env_key = _env_key_for_provider(provider)
    if env_key:
        return {"key": env_key, "source": ".env", "stored": False, "decrypt_error": False}
    return {"key": "", "source": None, "stored": False, "decrypt_error": False}


def _provider_status(provider: str, login_hint: str) -> dict[str, Any]:
    key_info = _provider_key_info(provider)
    catalog = HOSTED_PROVIDER_CATALOG[provider]
    source = str(key_info.get("source") or "") or None
    connected = bool(key_info.get("key"))
    auth_type = "byok" if connected or source == "hosted_secret_unreadable" else None
    if provider == NINE_ROUTER_PROVIDER and not connected and managed_nine_router_available():
        connected = True
        source = "appora_managed_free"
        auth_type = "managed_free"
    hint = catalog["hint"] if connected else login_hint
    if source == "appora_managed_free":
        hint = "Appora Free Router aktif. free-forever bisa langsung dipakai; model premium butuh 9Router pribadi atau paket premium."
    if source == "hosted_secret_unreadable":
        hint = "API key tersimpan di akun ini, tapi backend tidak bisa decrypt. Re-save key di Settings atau cek VOICEIDE_SECRET_KEY deploy."
    return {
        "provider": provider,
        "connected": connected,
        "auth_type": auth_type,
        "source": source,
        "managed_free": source == "appora_managed_free",
        "hint": hint,
        "recommended_model": catalog["recommended_model"],
        "free_tier_models": catalog["free_tier_models"],
    }


def openai_status() -> dict[str, Any]:
    return {**_provider_status(OPENAI_PROVIDER, OPENAI_LOGIN_HINT), "profile_id": None, "account_id": None}


def nine_router_status() -> dict[str, Any]:
    status = _provider_status(NINE_ROUTER_PROVIDER, NINE_ROUTER_LOGIN_HINT)
    if status.get("source") == "appora_managed_free":
        status["base_url"] = _managed_nine_router_base_url()
    else:
        status["base_url"] = _nine_router_base_url()
    return status


def anthropic_status() -> dict[str, Any]:
    return _provider_status(ANTHROPIC_PROVIDER, ANTHROPIC_LOGIN_HINT)


def openrouter_status() -> dict[str, Any]:
    return _provider_status(OPENROUTER_PROVIDER, OPENROUTER_LOGIN_HINT)


def groq_status() -> dict[str, Any]:
    return _provider_status(GROQ_PROVIDER, GROQ_LOGIN_HINT)


def _catalog_status(provider: str, login_hint: str) -> dict[str, Any]:
    return _provider_status(provider, login_hint)


def auth_snapshot(workspace: Path | None = None) -> dict[str, Any]:
    return {
        "nine_router": nine_router_status(),
        "openai": openai_status(),
        "anthropic": anthropic_status(),
        "openrouter": openrouter_status(),
        "groq": groq_status(),
        "gemini": _catalog_status(GEMINI_PROVIDER, GEMINI_LOGIN_HINT),
        "together": _catalog_status(TOGETHER_PROVIDER, TOGETHER_LOGIN_HINT),
        "cerebras": _catalog_status(CEREBRAS_PROVIDER, CEREBRAS_LOGIN_HINT),
        "xai": _catalog_status(XAI_PROVIDER, XAI_LOGIN_HINT),
    }


def require_provider_connected(provider: str, workspace: Path | None = None) -> None:
    snapshot = auth_snapshot(workspace)
    status = snapshot.get(provider)
    if not status:
        raise RuntimeError(f"Unsupported provider: {provider}")
    if not status.get("connected"):
        if provider == NINE_ROUTER_PROVIDER:
            raise RuntimeError("9Router belum connected. free-forever butuh Appora managed router aktif, atau isi endpoint/API key 9Router pribadi di Settings.")
        if provider == OPENAI_PROVIDER:
            raise RuntimeError("OpenAI belum connected. Isi OPENAI_API_KEY (BYOK) di Settings.")
        if provider == ANTHROPIC_PROVIDER:
            raise RuntimeError("Anthropic belum connected. Isi ANTHROPIC_API_KEY di Settings.")
        if provider == OPENROUTER_PROVIDER:
            raise RuntimeError("OpenRouter belum connected. Isi OPENROUTER_API_KEY di Settings.")
        if provider == GROQ_PROVIDER:
            raise RuntimeError("Groq belum connected. Isi GROQ_API_KEY di Settings.")
        if provider == GEMINI_PROVIDER:
            raise RuntimeError("Gemini belum connected. Isi GEMINI_API_KEY di Settings.")
        if provider == TOGETHER_PROVIDER:
            raise RuntimeError("Together AI belum connected. Isi TOGETHER_API_KEY di Settings.")
        if provider == CEREBRAS_PROVIDER:
            raise RuntimeError("Cerebras belum connected. Isi CEREBRAS_API_KEY di Settings.")
        if provider == XAI_PROVIDER:
            raise RuntimeError("xAI belum connected. Isi XAI_API_KEY di Settings.")
        raise RuntimeError(f"Provider belum connected: {provider}")


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _http_form_json(url: str, form: dict[str, Any], headers: dict[str, str] | None = None) -> tuple[int, str, dict[str, Any] | None]:
    body = urlencode({k: "" if v is None else str(v) for k, v in form.items()}).encode("utf-8")
    req = Request(url, data=body, method="POST", headers={"Content-Type": "application/x-www-form-urlencoded", **(headers or {})})
    try:
        with urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, raw, json.loads(raw) if raw else None
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        return exc.code, raw, None


def generate_pkce() -> tuple[str, str]:
    verifier = _base64url(os.urandom(32))
    challenge = _base64url(hashlib.sha256(verifier.encode("utf-8")).digest())
    return verifier, challenge


def create_state() -> str:
    return os.urandom(16).hex()


def build_google_identity_authorization_url(*, redirect_uri: str, verifier: str, state: str) -> str:
    challenge = _base64url(hashlib.sha256(verifier.encode("utf-8")).digest())
    return GOOGLE_AUTH_URL + "?" + urlencode(
        {
            "client_id": _google_oauth_client_id(),
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(GOOGLE_IDENTITY_SCOPES),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "prompt": "select_account",
        }
    )


def _jwt_payload_unverified(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise RuntimeError("Invalid Google id_token")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("Could not decode Google id_token") from exc


def exchange_google_identity_code(*, code: str, verifier: str, redirect_uri: str) -> dict[str, Any]:
    status, raw, token_data = _http_form_json(
        GOOGLE_TOKEN_URL,
        {
            "client_id": _google_oauth_client_id(),
            "client_secret": _google_oauth_client_secret(),
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
    )
    if status < 200 or status >= 300 or not isinstance(token_data, dict):
        raise RuntimeError(f"Google token exchange failed: {status} {raw[:400]}")
    id_token = str(token_data.get("id_token") or "").strip()
    if not id_token:
        raise RuntimeError("Google token response missing id_token")
    payload = _jwt_payload_unverified(id_token)
    sub = str(payload.get("sub") or "").strip()
    if not sub:
        raise RuntimeError("Google id_token missing sub")
    return {
        "sub": sub,
    }


def _friendly_free_tier_mode() -> bool:
    return bool(getattr(__import__("api.settings", fromlist=["settings"]).settings, "friendly_free_tier_mode", True))


def _is_serverless_runtime() -> bool:
    return bool(os.getenv("VERCEL") or os.getenv("VERCEL_ENV") or os.getenv("AWS_LAMBDA_FUNCTION_NAME") or os.getenv("LAMBDA_TASK_ROOT"))


def _extract_retry_after_seconds(exc: HTTPError) -> float | None:
    header = (exc.headers.get("Retry-After") or "").strip()
    if not header:
        return None
    try:
        return max(0.0, float(header))
    except Exception:
        return None


def _set_provider_cooldown(provider: str | None, wait_seconds: float) -> None:
    if not provider or wait_seconds <= 0:
        return
    until = time.time() + max(0.0, wait_seconds)
    with _PROVIDER_COOLDOWN_LOCK:
        _PROVIDER_COOLDOWN_UNTIL[provider] = max(until, _PROVIDER_COOLDOWN_UNTIL.get(provider, 0.0))


def get_provider_cooldown_remaining(provider: str | None) -> float:
    if not provider:
        return 0.0
    with _PROVIDER_COOLDOWN_LOCK:
        until = _PROVIDER_COOLDOWN_UNTIL.get(provider, 0.0)
    return max(0.0, until - time.time())


def _nine_router_base_url() -> str:
    from api import settings as settings_mod

    profile_id = CURRENT_PROFILE_ID.get()
    if profile_id:
        try:
            stored_url = (get_provider_secret(profile_id=profile_id, provider="nine_router_base_url") or "").strip()
            if stored_url:
                return stored_url.rstrip("/")
        except Exception:
            pass
    value = (
        os.getenv("NINE_ROUTER_BASE_URL")
        or os.getenv("APPORA_9ROUTER_BASE_URL")
        or getattr(settings_mod.settings, "nine_router_base_url", "")
        or "http://127.0.0.1:20128/v1"
    )
    return str(value).strip().rstrip("/") or "http://127.0.0.1:20128/v1"


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], *, provider: str | None = None) -> tuple[int, dict[str, Any] | None, str]:
    body = json.dumps(payload).encode("utf-8")
    req = Request(url, data=body, method="POST", headers={"Content-Type": "application/json", **headers})
    serverless = _is_serverless_runtime()
    max_attempts = 1 if serverless else (3 if _friendly_free_tier_mode() else 1)
    cooldown = get_provider_cooldown_remaining(provider)
    if cooldown > 0:
        if serverless:
            return 429, {"error": {"message": f"Provider masih cooldown {int(cooldown)} detik. Coba lagi sebentar atau pilih model yang lebih ringan."}}, ""
        time.sleep(min(cooldown, 20.0))
    for attempt in range(1, max_attempts + 1):
        try:
            with urlopen(req, timeout=180) as resp:
                raw = resp.read().decode("utf-8", "replace")
                try:
                    data = json.loads(raw) if raw else {}
                except Exception:
                    data = {"error": {"message": raw[:500] or "Provider returned a non-JSON response."}}
                return resp.status, data, raw
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                data = json.loads(raw) if raw else None
            except Exception:
                data = None
            if exc.code == 429:
                wait_seconds = _extract_retry_after_seconds(exc)
                if wait_seconds is None:
                    wait_seconds = min(20.0, 4.0 * attempt)
                _set_provider_cooldown(provider, wait_seconds)
                if attempt < max_attempts:
                    time.sleep(wait_seconds)
                    continue
            return exc.code, data, raw
        except URLError as exc:
            if attempt < max_attempts:
                time.sleep(min(8.0, 1.5 * attempt))
                continue
            return 599, None, str(exc)


def _friendly_error(provider: str, status: int, data: dict[str, Any] | None, fallback: str) -> str:
    raw_message = ""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            raw_message = str(err.get("message") or "")
        elif isinstance(err, str):
            raw_message = err
    message = raw_message or fallback
    lowered = message.lower()
    if status in {401, 403}:
        return f"{provider} key ditolak. Cek ulang API key di Settings."
    if status == 402 or "quota" in lowered or "credit" in lowered or "billing" in lowered:
        if provider == NINE_ROUTER_PROVIDER:
            return "9Router route/model ini belum punya quota/credential aktif. Buka dashboard 9Router, connect provider atau pilih combo/model free lain."
        if provider == OPENROUTER_PROVIDER:
            return "OpenRouter belum punya credit untuk model ini, atau model free sedang penuh. Pilih model ':free' lain di Settings."
        return f"{provider} butuh billing/API credit untuk model ini. Pakai OpenRouter + model ':free' kalau mau coba gratis."
    if status == 429 or "rate" in lowered or "cooldown" in lowered:
        return f"{provider} sedang kena rate limit. Tunggu sebentar, atau pilih model yang lebih ringan/free-tier friendly."
    return message


_NINE_ROUTER_MODELS = [
    "free-forever",
    "always-on",
    "maximize-claude",
    "openclaw-free",
    "coding-auto",
    "cheap-auto",
    "quality-auto",
    "fast-free",
    "kr/claude-sonnet-4.5",
    "kr/claude-haiku-4.5",
    "kr/deepseek-3.2",
    "kr/qwen3-coder-next",
    "kr/glm-5",
    "kr/MiniMax-M2.5",
    "oc/<auto>",
    "cc/claude-opus-4-7",
    "cc/claude-opus-4-6",
    "cc/claude-sonnet-4-6",
    "cc/claude-opus-4-5-20251101",
    "cc/claude-sonnet-4-5-20250929",
    "cc/claude-haiku-4-5-20251001",
    "cx/gpt-5.5",
    "cx/gpt-5.4",
    "cx/gpt-5.3-codex",
    "cx/gpt-5.3-codex-xhigh",
    "cx/gpt-5.3-codex-high",
    "cx/gpt-5.3-codex-low",
    "cx/gpt-5.3-codex-spark",
    "cx/gpt-5.2-codex",
    "cx/gpt-5.2",
    "cx/gpt-5.1-codex-max",
    "cx/gpt-5.1-codex",
    "cx/gpt-5.1",
    "cx/gpt-5-codex",
    "cx/gpt-5-codex-mini",
    "gc/gemini-3-flash-preview",
    "gc/gemini-3-pro-preview",
    "ag/gemini-3.1-pro-high",
    "ag/gemini-3.1-pro-low",
    "ag/gemini-3-flash",
    "ag/claude-sonnet-4-6",
    "ag/claude-opus-4-6-thinking",
    "gh/gpt-5.4",
    "gh/gpt-5.4-mini",
    "gh/gpt-5.3-codex",
    "gh/gpt-5.2-codex",
    "gh/gpt-5.2",
    "gh/claude-sonnet-4.6",
    "gh/claude-opus-4.7",
    "gh/claude-sonnet-4.5",
    "gh/gemini-3.1-pro-preview",
    "gh/gemini-3-flash-preview",
    "gh/grok-code-fast-1",
    "cu/default",
    "cu/claude-4.5-sonnet",
    "cu/claude-4.5-sonnet-thinking",
    "cu/claude-4.5-opus",
    "cu/claude-4.6-opus-max",
    "cu/gpt-5.3-codex",
    "cu/gpt-5.2",
    "cu/kimi-k2.5",
    "cu/gemini-3-flash-preview",
    "qw/qwen3-coder-plus",
    "qw/qwen3-coder-flash",
    "if/qwen3-coder-plus",
    "if/qwen3-max",
    "if/qwen3-235b",
    "if/deepseek-v3.2",
    "if/deepseek-r1",
    "if/glm-4.7",
    "kmc/kimi-k2.6",
    "kmc/kimi-k2.5",
    "kmc/kimi-k2.5-thinking",
    "opencode-go/kimi-k2.6",
    "opencode-go/glm-5.1",
    "opencode-go/glm-5",
    "opencode-go/qwen3.6-plus",
    "opencode-go/minimax-m2.7",
    "openai/gpt-5.4",
    "openai/gpt-5.4-mini",
    "openai/gpt-5.4-nano",
    "openai/gpt-5.2",
    "openai/gpt-5.1",
    "openai/gpt-4.1",
    "openai/gpt-4o-mini",
    "anthropic/claude-sonnet-4-20250514",
    "anthropic/claude-opus-4-20250514",
    "gemini/gemini-3.1-pro-preview",
    "gemini/gemini-3-flash-preview",
    "gemini/gemini-2.5-pro",
    "gemini/gemini-2.5-flash",
    "openrouter/openrouter/free",
    "openrouter/deepseek/deepseek-v4-flash:free",
    "openrouter/deepseek/deepseek-chat-v3-0324:free",
    "openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "openrouter/x-ai/grok-4.3",
    "openrouter/anthropic/claude-opus-4.7",
    "openrouter/google/gemini-3.1-pro-preview",
    "openrouter/moonshotai/kimi-k2.6",
    "groq/llama-3.3-70b-versatile",
    "groq/qwen/qwen3-32b",
    "groq/openai/gpt-oss-120b",
    "together/deepseek-ai/DeepSeek-R1",
    "together/Qwen/Qwen3-235B-A22B",
    "together/meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "cerebras/gpt-oss-120b",
    "cerebras/zai-glm-4.7",
    "cerebras/qwen-3-235b-a22b-instruct-2507",
    "xai/grok-4",
    "xai/grok-4-fast-reasoning",
    "xai/grok-code-fast-1",
    "deepseek/deepseek-v4-pro",
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-chat",
    "glm/glm-5.1",
    "glm/glm-5",
    "minimax/minimax-m2.7",
    "minimax/minimax-m2.5",
    "kimi/kimi-k2.6",
    "kimi/kimi-k2.5",
    "commandcode/deepseek/deepseek-v4-pro",
    "commandcode/moonshotai/Kimi-K2.6",
    "commandcode/zai-org/GLM-5.1",
    "cloudflare-ai/@cf/meta/llama-3.3-70b-instruct-fp8-fast",
    "cloudflare-ai/@cf/moonshotai/kimi-k2.6",
    "cloudflare-ai/@cf/zai-org/glm-4.7-flash",
    "vertex/gemini-3.1-pro-preview",
    "vertex/gemini-3-flash-preview",
    "vertex-partner/zai-org/glm-5-maas",
    "grok-web/grok-4.2",
    "grok-web/grok-4.1-fast",
    "perplexity-web/pplx-auto",
    "perplexity-web/pplx-sonnet",
    "perplexity-web/pplx-gemini",
]


def list_models(provider: str) -> list[str]:
    provider = provider.strip().lower()
    if provider in {"9router", "nine-router"}:
        provider = NINE_ROUTER_PROVIDER
    if provider not in SUPPORTED_PROVIDERS:
        raise RuntimeError(f"Unsupported provider: {provider}")
    if provider == NINE_ROUTER_PROVIDER:
        return list(dict.fromkeys(_NINE_ROUTER_MODELS))
    if provider == OPENAI_PROVIDER:
        return [
            "gpt-5.5",
            "gpt-5.5-pro",
            "gpt-5.4",
            "gpt-5.4-pro",
            "gpt-5.4-mini",
            "gpt-5.4-nano",
            "gpt-5-mini",
            "gpt-5-nano",
            "gpt-4.1",
            "gpt-4.1-mini",
            "gpt-4.1-nano",
            "o4-mini",
            "gpt-4o",
            "gpt-4o-mini",
            "o3-mini",
            "o3",
        ]
    if provider == ANTHROPIC_PROVIDER:
        return [
            "claude-opus-4-7",
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "claude-opus-4-5",
            "claude-haiku-4-5",
            "claude-sonnet-4-5",
            "claude-opus-4-1-20250805",
            "claude-opus-4-0",
            "claude-sonnet-4-0",
            "claude-3-5-haiku-latest",
            "claude-3-5-sonnet-latest",
            "claude-3-7-sonnet-latest",
        ]
    if provider == OPENROUTER_PROVIDER:
        return [
            "free-forever",
            "fast-free",
            "coding-auto",
            "openrouter/free",
            "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
            "deepseek/deepseek-v4-flash:free",
            "deepseek/deepseek-chat-v3-0324:free",
            "deepseek/deepseek-r1:free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "google/gemini-2.0-flash-exp:free",
            "x-ai/grok-4.3",
            "openai/gpt-5.5",
            "openai/gpt-5.5-pro",
            "google/gemini-3.1-pro-preview",
            "google/gemini-3.1-flash-lite-preview",
            "google/gemini-3-flash-preview",
            "anthropic/claude-opus-4.7",
            "deepseek/deepseek-v4-pro",
            "moonshotai/kimi-k2.6",
            "moonshotai/kimi-k2.5",
            "openai/gpt-5.4",
            "openai/gpt-5.4-pro",
            "openai/gpt-5.4-mini",
            "openai/gpt-5.4-nano",
            "openai/gpt-4o-mini",
            "openai/gpt-4.1-mini",
            "google/gemini-2.5-flash",
            "anthropic/claude-3.5-haiku",
            "openai/gpt-5.4",
            "openai/gpt-4.1",
            "anthropic/claude-sonnet-4",
            "anthropic/claude-3.7-sonnet",
            "google/gemini-2.5-pro",
            "meta-llama/llama-3.3-70b-instruct",
            "deepseek/deepseek-chat-v3-0324",
            "deepseek/deepseek-r1",
        ]
    if provider == GROQ_PROVIDER:
        return [
            "groq/compound",
            "groq/compound-mini",
            "meta-llama/llama-4-scout-17b-16e-instruct",
            "qwen/qwen3-32b",
            "llama-3.3-70b-versatile",
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
            "llama-3.1-8b-instant",
        ]
    if provider == GEMINI_PROVIDER:
        return [
            "gemini-3-pro-preview",
            "gemini-3.1-pro-preview",
            "gemini-3-flash-preview",
            "gemini-3.1-flash-lite-preview",
            "gemini-3-pro-image-preview",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-2.5-pro",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
            "gemini-1.5-flash",
            "gemini-1.5-flash-8b",
            "gemini-1.5-pro",
        ]
    if provider == TOGETHER_PROVIDER:
        return [
            "deepseek-ai/DeepSeek-V4-Pro",
            "MiniMaxAI/MiniMax-M2.5",
            "Qwen/Qwen3.5-397B-A17B",
            "Qwen/Qwen3.5-122B-A10B",
            "moonshotai/Kimi-K2.5",
            "moonshotai/Kimi-K2-0905",
            "deepseek-ai/DeepSeek-V3.1",
            "meta-llama/Llama-3.3-70B-Instruct-Turbo",
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
            "meta-llama/Llama-4-Scout-17B-16E-Instruct",
            "deepseek-ai/DeepSeek-R1",
            "deepseek-ai/DeepSeek-V3",
            "Qwen/Qwen3-235B-A22B-fp8-tput",
            "Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8",
            "moonshotai/Kimi-K2-Instruct",
            "mistralai/Mixtral-8x7B-Instruct-v0.1",
        ]
    if provider == CEREBRAS_PROVIDER:
        return [
            "zai-glm-4.7",
            "gpt-oss-120b",
            "qwen-3-235b-a22b-instruct-2507",
            "llama3.1-8b",
        ]
    if provider == XAI_PROVIDER:
        return [
            "grok-4.3",
            "grok-4-fast-reasoning",
            "grok-4-fast-non-reasoning",
            "grok-4",
            "grok-3",
            "grok-3-mini",
            "grok-2-vision-1212",
        ]
    return []


def provider_catalog() -> dict[str, dict[str, Any]]:
    return HOSTED_PROVIDER_CATALOG


def nine_router_generate_json(*, model: str, system: str, user: str) -> dict[str, Any]:
    credentials = _nine_router_credentials_for_model(model)
    api_key = str(credentials.get("api_key") or "")
    base_url = str(credentials.get("base_url") or "").rstrip("/")
    source = str(credentials.get("source") or "")
    if not api_key or not base_url:
        return {"text": "", "error_message": NINE_ROUTER_LOGIN_HINT}
    if source == "appora_managed_free":
        allowed, used, limit = _consume_managed_quota()
        if not allowed:
            return {
                "text": "",
                "error_message": f"Appora Free Router quota hari ini habis ({used}/{limit}). Pakai 9Router pribadi di Settings atau coba lagi besok.",
            }
    route_model = _resolve_managed_free_route_model(model, source)
    status, data, _raw = _post_json(
        base_url + "/chat/completions",
        {
            "model": route_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": 2800 if _friendly_free_tier_mode() else 4000,
            "temperature": 0.2,
            "stream": False,
        },
        {"Authorization": f"Bearer {api_key}"},
        provider=NINE_ROUTER_PROVIDER,
    )
    if status < 200 or status >= 300:
        return {"text": "", "error_message": _friendly_error(NINE_ROUTER_PROVIDER, status, data, f"9Router error {status}")}
    choices = (data or {}).get("choices") if isinstance(data, dict) else []
    first = choices[0] if isinstance(choices, list) and choices else {}
    message = first.get("message") if isinstance(first, dict) else {}
    return {"text": str((message or {}).get("content") or "")}


def test_nine_router_route(*, model: str, base_url: str | None = None, api_key: str | None = None) -> dict[str, Any]:
    selected_model = (model or "free-forever").strip() or "free-forever"
    credentials = _nine_router_credentials_for_model(selected_model, api_key=api_key, base_url=base_url)
    key = str(credentials.get("api_key") or "").strip()
    endpoint = str(credentials.get("base_url") or "").strip().rstrip("/")
    source = str(credentials.get("source") or "")
    if not key or not endpoint:
        return {"ok": False, "status": 0, "summary": NINE_ROUTER_LOGIN_HINT, "model": model}
    route_model = _resolve_managed_free_route_model(selected_model, source)
    status, data, _raw = _post_json(
        endpoint + "/chat/completions",
        {
            "model": route_model,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 8,
            "temperature": 0,
            "stream": False,
        },
        {"Authorization": f"Bearer {key}"},
        provider=NINE_ROUTER_PROVIDER,
    )
    if status < 200 or status >= 300:
        return {
            "ok": False,
            "status": status,
            "summary": _friendly_error(NINE_ROUTER_PROVIDER, status, data, f"9Router error {status}"),
            "model": model,
        }
    choices = (data or {}).get("choices") if isinstance(data, dict) else []
    first = choices[0] if isinstance(choices, list) and choices else {}
    message = first.get("message") if isinstance(first, dict) else {}
    text = str((message or {}).get("content") or "").strip()
    return {
        "ok": True,
        "status": status,
        "summary": "Connected. Appora Free Router accepted the selected model/combo." if source == "appora_managed_free" else "Connected. 9Router accepted the selected model/combo.",
        "model": model,
        "resolved_model": route_model if route_model != selected_model else None,
        "response": text[:200],
    }


def openai_generate_json(*, model: str, system: str, user: str) -> dict[str, Any]:
    api_key = _provider_key_from_env_or_secret(OPENAI_PROVIDER)
    if not api_key:
        return {"text": "", "error_message": "OPENAI_API_KEY is not set"}
    status, data, _raw = _post_json(
        "https://api.openai.com/v1/responses",
        {
            "model": model,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system}]},
                {"role": "user", "content": [{"type": "input_text", "text": user}]},
            ],
            "text": {"format": {"type": "text"}},
        },
        {"Authorization": f"Bearer {api_key}"},
        provider=OPENAI_PROVIDER,
    )
    if status < 200 or status >= 300:
        return {"text": "", "error_message": _friendly_error(OPENAI_PROVIDER, status, data, f"OpenAI error {status}")}
    return {"text": str((data or {}).get("output_text") or "")}


def anthropic_generate_json(*, model: str, system: str, user: str) -> dict[str, Any]:
    api_key = _provider_key_from_env_or_secret(ANTHROPIC_PROVIDER)
    if not api_key:
        return {"text": "", "error_message": "ANTHROPIC_API_KEY is not set"}
    status, data, _raw = _post_json(
        "https://api.anthropic.com/v1/messages",
        {
            "model": model,
            "max_tokens": 2800 if _friendly_free_tier_mode() else 4000,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        provider=ANTHROPIC_PROVIDER,
    )
    if status < 200 or status >= 300:
        return {"text": "", "error_message": _friendly_error(ANTHROPIC_PROVIDER, status, data, f"Anthropic error {status}")}
    chunks = (data or {}).get("content") if isinstance(data, dict) else []
    if not isinstance(chunks, list):
        chunks = []
    text = "\n".join(str(item.get("text") or "") for item in chunks if isinstance(item, dict) and item.get("type") == "text")
    return {"text": text}


def openrouter_generate_json(*, model: str, system: str, user: str) -> dict[str, Any]:
    api_key = _provider_key_from_env_or_secret(OPENROUTER_PROVIDER)
    if not api_key:
        return {"text": "", "error_message": "OPENROUTER_API_KEY is not set"}
    status, data, _raw = _post_json(
        "https://openrouter.ai/api/v1/chat/completions",
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": 2800 if _friendly_free_tier_mode() else 4000,
        },
        {"Authorization": f"Bearer {api_key}"},
        provider=OPENROUTER_PROVIDER,
    )
    if status < 200 or status >= 300:
        return {"text": "", "error_message": _friendly_error(OPENROUTER_PROVIDER, status, data, f"OpenRouter error {status}")}
    choices = (data or {}).get("choices") if isinstance(data, dict) else []
    first = choices[0] if isinstance(choices, list) and choices else {}
    message = first.get("message") if isinstance(first, dict) else {}
    return {"text": str((message or {}).get("content") or "")}


def _chat_completions_generate_json(*, provider: str, api_key: str, base_url: str, model: str, system: str, user: str, max_tokens: int = 2400) -> dict[str, Any]:
    if not api_key:
        return {"text": "", "error_message": f"{provider} API key belum diisi."}
    status, data, _raw = _post_json(
        base_url.rstrip("/") + "/chat/completions",
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens if _friendly_free_tier_mode() else 4000,
            "temperature": 0.2,
        },
        {"Authorization": f"Bearer {api_key}"},
        provider=provider,
    )
    if status < 200 or status >= 300:
        return {"text": "", "error_message": _friendly_error(provider, status, data, f"{provider} error {status}")}
    choices = (data or {}).get("choices") if isinstance(data, dict) else []
    first = choices[0] if isinstance(choices, list) and choices else {}
    message = first.get("message") if isinstance(first, dict) else {}
    return {"text": str((message or {}).get("content") or "")}


def groq_generate_json(*, model: str, system: str, user: str) -> dict[str, Any]:
    return _chat_completions_generate_json(
        provider=GROQ_PROVIDER,
        api_key=_provider_key_from_env_or_secret(GROQ_PROVIDER),
        base_url="https://api.groq.com/openai/v1",
        model=model,
        system=system,
        user=user,
        max_tokens=2400,
    )


def together_generate_json(*, model: str, system: str, user: str) -> dict[str, Any]:
    return _chat_completions_generate_json(
        provider=TOGETHER_PROVIDER,
        api_key=_provider_key_from_env_or_secret(TOGETHER_PROVIDER),
        base_url="https://api.together.xyz/v1",
        model=model,
        system=system,
        user=user,
        max_tokens=2800,
    )


def cerebras_generate_json(*, model: str, system: str, user: str) -> dict[str, Any]:
    return _chat_completions_generate_json(
        provider=CEREBRAS_PROVIDER,
        api_key=_provider_key_from_env_or_secret(CEREBRAS_PROVIDER),
        base_url="https://api.cerebras.ai/v1",
        model=model,
        system=system,
        user=user,
        max_tokens=2600,
    )


def xai_generate_json(*, model: str, system: str, user: str) -> dict[str, Any]:
    return _chat_completions_generate_json(
        provider=XAI_PROVIDER,
        api_key=_provider_key_from_env_or_secret(XAI_PROVIDER),
        base_url="https://api.x.ai/v1",
        model=model,
        system=system,
        user=user,
        max_tokens=2800,
    )


def gemini_generate_json(*, model: str, system: str, user: str) -> dict[str, Any]:
    api_key = _provider_key_from_env_or_secret(GEMINI_PROVIDER)
    if not api_key:
        return {"text": "", "error_message": GEMINI_LOGIN_HINT}
    safe_model = model.removeprefix("models/")
    status, data, _raw = _post_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{safe_model}:generateContent?key={api_key}",
        {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 2600 if _friendly_free_tier_mode() else 4000,
            },
        },
        {},
        provider=GEMINI_PROVIDER,
    )
    if status < 200 or status >= 300:
        return {"text": "", "error_message": _friendly_error(GEMINI_PROVIDER, status, data, f"Gemini error {status}")}
    candidates = (data or {}).get("candidates") if isinstance(data, dict) else []
    first = candidates[0] if isinstance(candidates, list) and candidates else {}
    content = first.get("content") if isinstance(first, dict) else {}
    parts = content.get("parts") if isinstance(content, dict) else []
    if not isinstance(parts, list):
        return {"text": ""}
    return {"text": "\n".join(str(part.get("text") or "") for part in parts if isinstance(part, dict))}
