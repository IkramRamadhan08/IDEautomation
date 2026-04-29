from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Header, HTTPException

from api.app_state import CURRENT_USER_ID
from api.supabase_store import get_supabase_admin, upsert_profile


@dataclass
class AuthenticatedUser:
    user_id: str
    auth_source: str
    email: str | None = None
    display_name: str | None = None
    supabase_user_id: str | None = None


def sanitize_user_id(raw: str | None) -> str:
    value = (raw or "").strip().lower()
    if not value:
        return "voiceide-user-default"
    safe = "".join(ch for ch in value if ch.isalnum() or ch in {"-", "_", ".", ":"})
    return safe[:120] or "voiceide-user-default"


def _coerce_metadata_name(user: Any) -> str | None:
    metadata = getattr(user, "user_metadata", None) or {}
    if not isinstance(metadata, dict):
        return None
    for key in ("name", "full_name", "display_name"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value
    return None


def verify_supabase_bearer_token(authorization: str | None) -> AuthenticatedUser | None:
    raw = str(authorization or "").strip()
    if not raw.lower().startswith("bearer "):
        return None
    token = raw.split(" ", 1)[1].strip()
    if not token:
        return None

    client = get_supabase_admin()
    if not client:
        return None

    try:
        result = client.auth.get_user(token)
        user = getattr(result, "user", None)
    except Exception as exc:
        raise HTTPException(401, f"Invalid Supabase token: {exc}")

    if not user or not getattr(user, "id", None):
        raise HTTPException(401, "Invalid Supabase token")

    supabase_user_id = str(getattr(user, "id", "") or "").strip()
    email = str(getattr(user, "email", "") or "").strip() or None
    display_name = _coerce_metadata_name(user)
    user_id = sanitize_user_id(f"sb-{supabase_user_id}")

    try:
        upsert_profile(user_id=user_id, display_name=display_name, email=email)
    except Exception:
        pass

    return AuthenticatedUser(
        user_id=user_id,
        auth_source="supabase",
        email=email,
        display_name=display_name,
        supabase_user_id=supabase_user_id,
    )


def resolve_request_user(*, authorization: str | None, x_voiceide_user: str | None) -> AuthenticatedUser:
    verified = verify_supabase_bearer_token(authorization)
    if verified:
        return verified

    fallback = sanitize_user_id(x_voiceide_user)
    return AuthenticatedUser(user_id=fallback, auth_source="header-fallback")


def bind_request_user(authorization: str | None = Header(default=None), x_voiceide_user: str | None = Header(default=None)) -> AuthenticatedUser:
    user = resolve_request_user(authorization=authorization, x_voiceide_user=x_voiceide_user)
    CURRENT_USER_ID.set(user.user_id)
    return user
