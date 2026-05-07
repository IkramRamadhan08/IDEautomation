from __future__ import annotations

import time
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from api.app_state import CURRENT_SESSION_ID, CURRENT_USER_ID, STATE


class GoogleUserInfo(BaseModel):
    sub: str
    email: str | None = None
    name: str | None = None
    picture: str | None = None


class GoogleAuthStatus(BaseModel):
    ok: bool = True
    authenticated: bool
    phase: str = "idle"
    auth_url: str | None = None
    user: GoogleUserInfo | None = None


def build_auth_router(*, session_state, sanitize_session_id, sanitize_user_id, upsert_current_user_profile):
    router = APIRouter(prefix="/api/auth/google", tags=["auth"])

    def _public_url_for(request: Request, route_name: str, **path_params) -> str:
        raw = str(request.url_for(route_name, **path_params))
        base = urlsplit(raw)

        for header_name in ("origin", "referer"):
            candidate = (request.headers.get(header_name) or "").strip()
            if not candidate:
                continue
            parsed = urlsplit(candidate)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                return urlunsplit((parsed.scheme, parsed.netloc, base.path, base.query, base.fragment))

        forwarded_host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").strip()
        forwarded_proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http").strip()
        if forwarded_host and forwarded_proto in {"http", "https"}:
            return urlunsplit((forwarded_proto, forwarded_host, base.path, base.query, base.fragment))

        return raw

    def _google_auth_status() -> GoogleAuthStatus:
        session = session_state()
        user = session.get("google_user")
        if isinstance(user, dict) and user.get("sub"):
            return GoogleAuthStatus(authenticated=True, phase="done", user=GoogleUserInfo(**user))
        pending = next(
            (item for item in STATE["google_auth_pending"].values() if item.get("session_id") == CURRENT_SESSION_ID.get()),
            None,
        )
        return GoogleAuthStatus(
            authenticated=False,
            phase=str((pending or {}).get("phase") or "idle"),
            auth_url=(pending or {}).get("auth_url"),
            user=None,
        )

    @router.get("/status", response_model=GoogleAuthStatus)
    def google_auth_status():
        return _google_auth_status()

    @router.post("/login-start", response_model=GoogleAuthStatus)
    def google_auth_login_start(request: Request):
        from .oauth_runtime import build_google_identity_authorization_url, create_state, generate_pkce

        verifier, _challenge = generate_pkce()
        state = create_state()
        redirect_uri = _public_url_for(request, "google_auth_callback")
        try:
            auth_url = build_google_identity_authorization_url(redirect_uri=redirect_uri, verifier=verifier, state=state)
        except RuntimeError as exc:
            raise HTTPException(400, str(exc))
        STATE["google_auth_pending"][state] = {
            "session_id": CURRENT_SESSION_ID.get(),
            "user_id": CURRENT_USER_ID.get(),
            "verifier": verifier,
            "redirect_uri": redirect_uri,
            "started": time.time(),
            "phase": "awaiting_browser",
            "auth_url": auth_url,
        }
        return GoogleAuthStatus(authenticated=False, phase="awaiting_browser", auth_url=auth_url, user=None)

    @router.post("/logout")
    def google_auth_logout():
        session = session_state()
        session["google_user"] = None
        session["workspace"] = None
        for key, pending in list(STATE["google_auth_pending"].items()):
            if pending.get("session_id") == CURRENT_SESSION_ID.get():
                STATE["google_auth_pending"].pop(key, None)
        return {"ok": True}

    @router.get("/callback", name="google_auth_callback", response_class=HTMLResponse)
    def google_auth_callback(request: Request):
        from .oauth_runtime import exchange_google_identity_code

        qs = request.query_params
        oauth_state = str(qs.get("state") or "").strip()
        code = str(qs.get("code") or "").strip()
        pending = STATE["google_auth_pending"].get(oauth_state)
        if not pending:
            raise HTTPException(400, "OAuth session not found or expired")

        if qs.get("error"):
            pending["phase"] = "error"
            return HTMLResponse(f"<html><body><h1>Google login failed</h1><p>{qs.get('error')}</p></body></html>", status_code=400)

        if not code:
            pending["phase"] = "error"
            return HTMLResponse("<html><body><h1>Invalid Google callback</h1></body></html>", status_code=400)

        try:
            auth = exchange_google_identity_code(
                code=code,
                verifier=str(pending["verifier"]),
                redirect_uri=str(pending["redirect_uri"]),
            )

            user = {
                "sub": str(auth.get("sub") or "").strip(),
                "email": None,
                "name": None,
                "picture": None,
            }
            if not user["sub"]:
                raise RuntimeError("Google login missing user id")

            session_id = sanitize_session_id(str(pending.get("session_id") or ""))
            session = STATE["sessions"].setdefault(session_id, {"workspace": None, "runners": {}, "oauth_pending": {}, "google_user": None})
            session["google_user"] = user
            pending["phase"] = "done"
            STATE["google_auth_pending"].pop(oauth_state, None)

            current_session_token = CURRENT_SESSION_ID.set(session_id)
            current_user_token = CURRENT_USER_ID.set(sanitize_user_id(f"google-{user['sub']}"))
            try:
                upsert_current_user_profile(display_name=None, email=None)
            finally:
                CURRENT_USER_ID.reset(current_user_token)
                CURRENT_SESSION_ID.reset(current_session_token)

            return HTMLResponse("<html><body><h1>Google login successful</h1><p>Google user ID is now your Appora user ID. You can close this tab and return to the app.</p><script>window.close()</script></body></html>")
        except Exception as exc:
            pending["phase"] = "error"
            return HTMLResponse(f"<html><body><h1>Google login failed</h1><pre>{str(exc)}</pre></body></html>", status_code=400)

    return router
