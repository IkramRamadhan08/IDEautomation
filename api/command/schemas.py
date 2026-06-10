from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class CommandPolicyDecision(BaseModel):
    ok: bool
    command: str
    risk_level: Literal["safe", "approval_required", "blocked"]
    reason: str
    requires_approval: bool = False


class AgentHarnessShellAction(BaseModel):
    command: str
    cwd: str | None = None
    reason: str | None = None
