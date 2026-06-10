from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.command.schemas import AgentHarnessShellAction, CommandPolicyDecision
from api.fs import safe_join


class TerminalRunReq(BaseModel):
    command: str
    cwd: str | None = None
    reason: str | None = None


class AgentHarnessRunShellReq(BaseModel):
    project_root: str = "."
    actions: list[AgentHarnessShellAction]


class CommandPolicyReq(BaseModel):
    command: str
    cwd: str | None = None
    reason: str | None = None


def build_command_router(
    *,
    session_state,
    command_policy_decision,
    run_shell_command,
    sync_hosted_project_text_files_after_shell,
    hydrate_hosted_project,
    run_harness_shell_actions_internal,
):
    router = APIRouter(prefix="/api", tags=["command"])

    @router.post("/agent/command-policy/check", response_model=CommandPolicyDecision)
    def command_policy_check(req: CommandPolicyReq):
        return command_policy_decision(req.command, project_root=req.cwd)

    @router.post("/terminal/run")
    def terminal_run(req: TerminalRunReq):
        ws_root = session_state().get("workspace")
        if not ws_root:
            raise HTTPException(400, "No workspace selected")

        cwd = ws_root
        if req.cwd:
            cwd = safe_join(ws_root, req.cwd)

        try:
            policy = command_policy_decision(req.command, project_root=req.cwd)
            if not policy.ok:
                raise HTTPException(403, {"message": policy.reason, "policy": policy.model_dump()})
            result = run_shell_command(req.command, cwd)
            result["policy"] = policy.model_dump()
            result["synced_files"] = sync_hosted_project_text_files_after_shell(ws_root, cwd)
            return result
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, str(e))

    @router.post("/agent/harness/run-shell")
    def agent_harness_run_shell(req: AgentHarnessRunShellReq):
        ws_root = session_state().get("workspace")
        if not ws_root:
            raise HTTPException(400, "No workspace selected")

        ws_root_path = Path(ws_root)
        project_root = str(req.project_root or ".").strip().strip("/") or "."
        hydrate_hosted_project(ws_root_path, project_root)

        return run_harness_shell_actions_internal(
            ws_root_path=ws_root_path,
            project_root=project_root,
            actions=req.actions,
        )

    return router
