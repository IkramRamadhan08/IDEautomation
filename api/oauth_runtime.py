from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from api.secrets_store import get_provider_secret, has_provider_secret

OPENAI_PROVIDER = "openai"
ANTHROPIC_PROVIDER = "anthropic"
OPENROUTER_PROVIDER = "openrouter"
SUPPORTED_PROVIDERS = (OPENAI_PROVIDER, ANTHROPIC_PROVIDER, OPENROUTER_PROVIDER)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_IDENTITY_SCOPES = ["openid"]

OPENAI_LOGIN_HINT = "Masukkan OPENAI API key di Settings."
ANTHROPIC_LOGIN_HINT = "Masukkan Anthropic API key di Settings."
OPENROUTER_LOGIN_HINT = "Masukkan OpenRouter API key di Settings."

CURRENT_PROFILE_ID: ContextVar[str | None] = ContextVar("voiceide_profile_id", default=None)

def _google_oauth_client_id() -> str:
    return (os.getenv("GOOGLE_OAUTH_CLIENT_ID") or os.getenv("GEMINI_OAUTH_CLIENT_ID") or "").strip()


def _google_oauth_client_secret() -> str:
    return (os.getenv("GOOGLE_OAUTH_CLIENT_SECRET") or os.getenv("GEMINI_OAUTH_CLIENT_SECRET") or "").strip()


def _provider_key_from_env_or_secret(provider: str) -> str:
    env_map = {
        OPENAI_PROVIDER: "OPENAI_API_KEY",
        ANTHROPIC_PROVIDER: "ANTHROPIC_API_KEY",
        OPENROUTER_PROVIDER: "OPENROUTER_API_KEY",
    }
    env_key = env_map.get(provider, "")
    if env_key:
        direct = (os.getenv(env_key) or "").strip()
        if direct:
            return direct
    profile_id = CURRENT_PROFILE_ID.get()
    if profile_id:
        try:
            return (get_provider_secret(profile_id=profile_id, provider=provider) or "").strip()
        except Exception:
            return ""
    return ""


def openai_status() -> dict[str, Any]:
    has_key = bool(_provider_key_from_env_or_secret(OPENAI_PROVIDER))
    return {
        "provider": OPENAI_PROVIDER,
        "connected": has_key,
        "profile_id": None,
        "account_id": None,
        "source": ".env" if has_key else None,
        "hint": None if has_key else OPENAI_LOGIN_HINT,
    }


def anthropic_status() -> dict[str, Any]:
    has_key = bool(_provider_key_from_env_or_secret(ANTHROPIC_PROVIDER))
    return {
        "provider": ANTHROPIC_PROVIDER,
        "connected": has_key,
        "auth_type": "byok" if has_key else None,
        "source": ".env" if has_key else None,
        "hint": None if has_key else ANTHROPIC_LOGIN_HINT,
    }


def openrouter_status() -> dict[str, Any]:
    has_key = bool(_provider_key_from_env_or_secret(OPENROUTER_PROVIDER))
    return {
        "provider": OPENROUTER_PROVIDER,
        "connected": has_key,
        "auth_type": "byok" if has_key else None,
        "source": ".env" if has_key else None,
        "hint": None if has_key else OPENROUTER_LOGIN_HINT,
    }


def auth_snapshot(workspace: Path | None = None) -> dict[str, Any]:
    return {
        "openai": openai_status(),
        "anthropic": anthropic_status(),
        "openrouter": openrouter_status(),
    }


def require_provider_connected(provider: str, workspace: Path | None = None) -> None:
    snapshot = auth_snapshot(workspace)
    status = snapshot.get(provider)
    if not status:
        raise RuntimeError(f"Unsupported provider: {provider}")
    if not status.get("connected"):
        if provider == OPENAI_PROVIDER:
            raise RuntimeError("OpenAI belum connected. Isi OPENAI_API_KEY (BYOK) di Settings.")
        if provider == ANTHROPIC_PROVIDER:
            raise RuntimeError("Anthropic belum connected. Isi ANTHROPIC_API_KEY di Settings.")
        if provider == OPENROUTER_PROVIDER:
            raise RuntimeError("OpenRouter belum connected. Isi OPENROUTER_API_KEY di Settings.")
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


def _extract_retry_after_seconds(exc: HTTPError) -> float | None:
    header = (exc.headers.get("Retry-After") or "").strip()
    if not header:
        return None
    try:
        return max(0.0, float(header))
    except Exception:
        return None


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[int, dict[str, Any] | None, str]:
    body = json.dumps(payload).encode("utf-8")
    req = Request(url, data=body, method="POST", headers={"Content-Type": "application/json", **headers})
    max_attempts = 3 if _friendly_free_tier_mode() else 1
    for attempt in range(1, max_attempts + 1):
        try:
            with urlopen(req, timeout=180) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, json.loads(raw) if raw else {}, raw
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                data = json.loads(raw) if raw else None
            except Exception:
                data = None
            if exc.code == 429 and attempt < max_attempts:
                wait_seconds = _extract_retry_after_seconds(exc)
                if wait_seconds is None:
                    wait_seconds = min(12.0, 2.0 * attempt)
                time.sleep(wait_seconds)
                continue
            return exc.code, data, raw
        except URLError as exc:
            if attempt < max_attempts:
                time.sleep(min(8.0, 1.5 * attempt))
                continue
            return 599, None, str(exc)


def list_models(provider: str) -> list[str]:
    provider = provider.strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise RuntimeError(f"Unsupported provider: {provider}")
    if provider == OPENAI_PROVIDER:
        return ["gpt-5.4", "gpt-5.4-mini"]
    if provider == ANTHROPIC_PROVIDER:
        return ["claude-sonnet-4-0"]
    if provider == OPENROUTER_PROVIDER:
        return ["openai/gpt-5.4", "anthropic/claude-sonnet-4"]
    return []


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
    )
    if status < 200 or status >= 300:
        error = ((data or {}).get("error") or {}).get("message") if isinstance(data, dict) else None
        return {"text": "", "error_message": error or f"OpenAI error {status}"}
    return {"text": str((data or {}).get("output_text") or "")}


def anthropic_generate_json(*, model: str, system: str, user: str) -> dict[str, Any]:
    api_key = _provider_key_from_env_or_secret(ANTHROPIC_PROVIDER)
    if not api_key:
        return {"text": "", "error_message": "ANTHROPIC_API_KEY is not set"}
    status, data, _raw = _post_json(
        "https://api.anthropic.com/v1/messages",
        {
            "model": model,
            "max_tokens": 4000,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
    )
    if status < 200 or status >= 300:
        error = ((data or {}).get("error") or {}).get("message") if isinstance(data, dict) else None
        return {"text": "", "error_message": error or f"Anthropic error {status}"}
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
        },
        {"Authorization": f"Bearer {api_key}"},
    )
    if status < 200 or status >= 300:
        error = ((data or {}).get("error") or {}).get("message") if isinstance(data, dict) else None
        return {"text": "", "error_message": error or f"OpenRouter error {status}"}
    choices = (data or {}).get("choices") if isinstance(data, dict) else []
    first = choices[0] if isinstance(choices, list) and choices else {}
    message = first.get("message") if isinstance(first, dict) else {}
    return {"text": str((message or {}).get("content") or "")}
