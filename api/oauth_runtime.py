from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

OPENAI_PROVIDER = "openai-codex"
ANTHROPIC_PROVIDER = "anthropic"
OPENROUTER_PROVIDER = "openrouter"
SUPPORTED_PROVIDERS = (OPENAI_PROVIDER, ANTHROPIC_PROVIDER, OPENROUTER_PROVIDER)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_IDENTITY_SCOPES = ["openid"]

OPENAI_LOGIN_HINT = "Masukkan OPENAI_API_KEY (BYOK) di Settings. OAuth model provider sudah dinonaktifkan."
ANTHROPIC_LOGIN_HINT = "Masukkan ANTHROPIC_API_KEY (BYOK) di Settings."
OPENROUTER_LOGIN_HINT = "Masukkan OPENROUTER_API_KEY (BYOK) di Settings."

def _google_oauth_client_id() -> str:
    return (os.getenv("GOOGLE_OAUTH_CLIENT_ID") or os.getenv("GEMINI_OAUTH_CLIENT_ID") or "").strip()


def _google_oauth_client_secret() -> str:
    return (os.getenv("GOOGLE_OAUTH_CLIENT_SECRET") or os.getenv("GEMINI_OAUTH_CLIENT_SECRET") or "").strip()


def openai_status() -> dict[str, Any]:
    has_key = bool((os.getenv("OPENAI_API_KEY") or "").strip())
    return {
        "provider": OPENAI_PROVIDER,
        "connected": has_key,
        "profile_id": None,
        "account_id": None,
        "source": ".env" if has_key else None,
        "hint": None if has_key else OPENAI_LOGIN_HINT,
    }


def anthropic_status() -> dict[str, Any]:
    has_key = bool((os.getenv("ANTHROPIC_API_KEY") or "").strip())
    return {
        "provider": ANTHROPIC_PROVIDER,
        "connected": has_key,
        "auth_type": "byok" if has_key else None,
        "source": ".env" if has_key else None,
        "hint": None if has_key else ANTHROPIC_LOGIN_HINT,
    }


def openrouter_status() -> dict[str, Any]:
    has_key = bool((os.getenv("OPENROUTER_API_KEY") or "").strip())
    return {
        "provider": OPENROUTER_PROVIDER,
        "connected": has_key,
        "auth_type": "byok" if has_key else None,
        "source": ".env" if has_key else None,
        "hint": None if has_key else OPENROUTER_LOGIN_HINT,
    }


def auth_snapshot(workspace: Path | None = None) -> dict[str, Any]:
    return {
        "openai_codex": openai_status(),
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
            raise RuntimeError("OpenAI/Codex belum connected. Isi OPENAI_API_KEY (BYOK) di Settings.")
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


def _bridge_path() -> Path:
    return Path(__file__).with_name("oauth_bridge.mjs")


def _run_bridge(command: str, payload: dict[str, Any]) -> dict[str, Any]:
    bridge = _bridge_path()
    if not bridge.exists():
        raise RuntimeError(f"Missing OAuth bridge script: {bridge}")

    proc = subprocess.run(
        ["node", str(bridge), command],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "Bridge call failed").strip()
        raise RuntimeError(err)

    raw = (proc.stdout or "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception as exc:
        raise RuntimeError(f"Bridge returned invalid JSON: {raw[:400]}") from exc


def list_models(provider: str) -> list[str]:
    provider = provider.strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise RuntimeError(f"Unsupported provider: {provider}")
    bridge_provider = OPENAI_PROVIDER if provider == OPENAI_PROVIDER else provider
    data = _run_bridge("models", {"provider": bridge_provider})
    models = data.get("models") or []
    return [str(m) for m in models if str(m).strip()]


def openai_generate_json(*, model: str, system: str, user: str) -> dict[str, Any]:
    return _run_bridge(
        "openai-byok-json",
        {
            "model": model,
            "system": system,
            "user": user,
        },
    )


def anthropic_generate_json(*, model: str, system: str, user: str) -> dict[str, Any]:
    return _run_bridge(
        "anthropic-byok-json",
        {
            "model": model,
            "system": system,
            "user": user,
        },
    )


def openrouter_generate_json(*, model: str, system: str, user: str) -> dict[str, Any]:
    return _run_bridge(
        "openrouter-byok-json",
        {
            "model": model,
            "system": system,
            "user": user,
        },
    )
