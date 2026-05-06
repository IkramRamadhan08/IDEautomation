from __future__ import annotations

import base64
import hashlib
import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

from api.supabase_store import get_supabase_admin, has_supabase


SECRET_TABLE = "user_provider_secrets"


def _legacy_profile_id(profile_id: str) -> str | None:
    value = str(profile_id or "").strip()
    if value.startswith("sb-") and len(value) > 3:
        return value[3:]
    return None


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
    try:
        client.table(SECRET_TABLE).upsert(payload).execute()
        legacy_profile_id = _legacy_profile_id(profile_id)
        if legacy_profile_id and legacy_profile_id != profile_id:
            client.table(SECRET_TABLE).delete().eq("profile_id", legacy_profile_id).eq("provider", provider).execute()
    except Exception as exc:
        raise HTTPException(400, f"Could not store provider secret: {exc}")


def delete_provider_secret(*, profile_id: str, provider: str) -> None:
    client = _require_supabase()
    try:
        client.table(SECRET_TABLE).delete().eq("profile_id", profile_id).eq("provider", provider).execute()
        legacy_profile_id = _legacy_profile_id(profile_id)
        if legacy_profile_id and legacy_profile_id != profile_id:
            client.table(SECRET_TABLE).delete().eq("profile_id", legacy_profile_id).eq("provider", provider).execute()
    except Exception as exc:
        raise HTTPException(400, f"Could not delete provider secret: {exc}")


def get_provider_secret(*, profile_id: str, provider: str) -> str | None:
    client = _require_supabase()
    res = client.table(SECRET_TABLE).select("secret_ciphertext").eq("profile_id", profile_id).eq("provider", provider).limit(1).execute()
    data = getattr(res, "data", None) or []
    row = data[0] if data and isinstance(data[0], dict) else None

    if not row:
        legacy_profile_id = _legacy_profile_id(profile_id)
        if legacy_profile_id and legacy_profile_id != profile_id:
            legacy_res = client.table(SECRET_TABLE).select("secret_ciphertext").eq("profile_id", legacy_profile_id).eq("provider", provider).limit(1).execute()
            legacy_data = getattr(legacy_res, "data", None) or []
            legacy_row = legacy_data[0] if legacy_data and isinstance(legacy_data[0], dict) else None
            if legacy_row:
                row = legacy_row
                ciphertext = str(legacy_row.get("secret_ciphertext") or "").strip()
                if ciphertext:
                    try:
                        client.table(SECRET_TABLE).upsert({
                            "profile_id": profile_id,
                            "provider": provider,
                            "secret_ciphertext": ciphertext,
                        }).execute()
                    except Exception:
                        pass

    if not row:
        return None
    ciphertext = str(row.get("secret_ciphertext") or "").strip()
    if not ciphertext:
        return None
    return _decrypt(ciphertext)


def has_provider_secret(*, profile_id: str, provider: str) -> bool:
    client = _require_supabase()
    res = client.table(SECRET_TABLE).select("profile_id").eq("profile_id", profile_id).eq("provider", provider).limit(1).execute()
    data = getattr(res, "data", None) or []
    if data:
        return True
    legacy_profile_id = _legacy_profile_id(profile_id)
    if legacy_profile_id and legacy_profile_id != profile_id:
        legacy_res = client.table(SECRET_TABLE).select("profile_id").eq("profile_id", legacy_profile_id).eq("provider", provider).limit(1).execute()
        legacy_data = getattr(legacy_res, "data", None) or []
        return bool(legacy_data)
    return False
