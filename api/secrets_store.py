from __future__ import annotations

import base64
import hashlib
import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

from api.supabase_store import get_supabase_admin, has_supabase


SECRET_TABLE = "user_provider_secrets"


def _require_supabase() -> Any:
    if not has_supabase():
        raise HTTPException(503, "Supabase must be configured for hosted secrets")
    client = get_supabase_admin()
    if not client:
        raise HTTPException(503, "Supabase admin client unavailable")
    return client


def _cipher() -> Fernet:
    raw = (os.getenv("VOICEIDE_SECRET_KEY") or "").strip()
    if not raw:
        raise HTTPException(503, "VOICEIDE_SECRET_KEY is not configured")
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def _encrypt(value: str) -> str:
    return _cipher().encrypt(value.encode("utf-8")).decode("utf-8")


def _decrypt(value: str) -> str | None:
    try:
        return _cipher().decrypt(value.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


def upsert_provider_secret(*, profile_id: str, provider: str, api_key: str) -> None:
    client = _require_supabase()
    payload = {
        "profile_id": profile_id,
        "provider": provider,
        "secret_ciphertext": _encrypt(api_key),
    }
    client.table(SECRET_TABLE).upsert(payload).execute()


def delete_provider_secret(*, profile_id: str, provider: str) -> None:
    client = _require_supabase()
    client.table(SECRET_TABLE).delete().eq("profile_id", profile_id).eq("provider", provider).execute()


def get_provider_secret(*, profile_id: str, provider: str) -> str | None:
    client = _require_supabase()
    res = client.table(SECRET_TABLE).select("secret_ciphertext").eq("profile_id", profile_id).eq("provider", provider).limit(1).execute()
    data = getattr(res, "data", None) or []
    if not data:
        return None
    row = data[0] if isinstance(data[0], dict) else {}
    ciphertext = str(row.get("secret_ciphertext") or "").strip()
    if not ciphertext:
        return None
    return _decrypt(ciphertext)


def has_provider_secret(*, profile_id: str, provider: str) -> bool:
    client = _require_supabase()
    res = client.table(SECRET_TABLE).select("profile_id").eq("profile_id", profile_id).eq("provider", provider).limit(1).execute()
    data = getattr(res, "data", None) or []
    return bool(data)
