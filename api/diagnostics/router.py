from __future__ import annotations

from fastapi import APIRouter, Request

from api.app_state import CURRENT_SESSION_ID, CURRENT_USER_ID
from api.auth.identity import CURRENT_REQUEST_USER
from api.storage.supabase import has_supabase


def build_diagnostics_router():
    router = APIRouter(prefix="/api", tags=["diagnostics"])

    @router.get("/healthz")
    def healthz():
        return {
            "ok": True,
            "service": "appora-api",
            "session": CURRENT_SESSION_ID.get(),
            "user": CURRENT_USER_ID.get(),
        }

    @router.get("/auth/debug")
    def auth_debug(request: Request):
        user = CURRENT_REQUEST_USER.get()
        authorization = request.headers.get("Authorization") or ""
        has_bearer = authorization.lower().startswith("bearer ") and len(authorization.split(" ", 1)[-1].strip()) > 0
        return {
            "ok": True,
            "auth_source": user.auth_source if user else "none",
            "user_id": user.user_id if user else CURRENT_USER_ID.get(),
            "supabase_user_id": user.supabase_user_id if user else None,
            "email_set": bool(user.email) if user else False,
            "has_bearer": has_bearer,
            "has_supabase_backend": has_supabase(),
        }

    return router
