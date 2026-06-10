from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class WorkspaceSetReq(BaseModel):
    path: str


class IdentityUpdateReq(BaseModel):
    display_name: str | None = None
    email: str | None = None


class IdentityInfo(BaseModel):
    user_id: str
    display_name: str | None
    email: str | None
    has_profile: bool
    managed_workspace_mode: Literal["user", "session"]
    managed_workspace_path: str


class WorkspaceInfo(BaseModel):
    path: str | None
    default: str | None


class WorkspaceProvisionResp(BaseModel):
    ok: bool
    path: str
    created: bool
    managed: bool = True
