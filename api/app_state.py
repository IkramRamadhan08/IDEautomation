from __future__ import annotations

from contextvars import ContextVar

CURRENT_SESSION_ID: ContextVar[str] = ContextVar("voiceide_session_id", default="voiceide-default")
CURRENT_USER_ID: ContextVar[str] = ContextVar("voiceide_user_id", default="voiceide-user-default")

# Session-scoped state so multiple browser clients don't trample each other's workspace selection.
STATE = {
    "sessions": {},  # session_id -> dict(workspace, runners, oauth_pending)
    "google_auth_pending": {},  # oauth_state -> dict(session_id, verifier, redirect_uri, started, phase, auth_url)
}
