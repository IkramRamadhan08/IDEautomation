from __future__ import annotations

from fastapi import Header, HTTPException

from api.auth_identity import CURRENT_REQUEST_USER, AuthenticatedUser, resolve_request_user
from api.supabase_store import has_supabase


HOSTED_AUTH_ERROR = (
    "Hosted project routes now require verified login. "
    "Sign in with Supabase/Google in the app so the frontend can send a bearer token."
)


def get_optional_user(authorization: str | None = Header(default=None), x_voiceide_user: str | None = Header(default=None)) -> AuthenticatedUser:
    current = CURRENT_REQUEST_USER.get()
    if current:
        return current
    return resolve_request_user(authorization=authorization, x_voiceide_user=x_voiceide_user)


def require_hosted_user(authorization: str | None = Header(default=None), x_voiceide_user: str | None = Header(default=None)) -> AuthenticatedUser:
    user = CURRENT_REQUEST_USER.get() or resolve_request_user(authorization=authorization, x_voiceide_user=x_voiceide_user)
    if has_supabase() and user.auth_source != "supabase":
        raise HTTPException(401, HOSTED_AUTH_ERROR)
    return user
