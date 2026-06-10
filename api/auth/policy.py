from __future__ import annotations

import os

from fastapi import Header, HTTPException

from api.auth.identity import CURRENT_REQUEST_USER, AuthenticatedUser, resolve_request_user
from api.storage.supabase import has_supabase


HOSTED_AUTH_ERROR = (
    "Hosted project routes now require verified login. "
    "Sign in with Supabase/Google in the app so the frontend can send a bearer token."
)


def get_optional_user(authorization: str | None = Header(default=None), x_voiceide_user: str | None = Header(default=None)) -> AuthenticatedUser:
    current = CURRENT_REQUEST_USER.get()
    if current:
        return current
    return resolve_request_user(authorization=authorization, x_voiceide_user=x_voiceide_user)


def is_hosted_auth_runtime() -> bool:
    return bool(
        os.environ.get("VERCEL")
        or os.environ.get("VERCEL_ENV")
        or os.environ.get("RAILWAY_ENVIRONMENT")
        or os.environ.get("RAILWAY_PROJECT_ID")
        or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
        or os.environ.get("LAMBDA_TASK_ROOT")
    )


def require_hosted_user(authorization: str | None = Header(default=None), x_voiceide_user: str | None = Header(default=None)) -> AuthenticatedUser:
    user = CURRENT_REQUEST_USER.get() or resolve_request_user(authorization=authorization, x_voiceide_user=x_voiceide_user)
    if has_supabase() and is_hosted_auth_runtime() and user.auth_source != "supabase":
        raise HTTPException(401, HOSTED_AUTH_ERROR)
    return user
