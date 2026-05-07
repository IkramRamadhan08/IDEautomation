from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.auth_policy import require_hosted_user
from api.projects import ProjectCreateReq, ProjectListResp, ProjectRenameReq, ProjectResp, ProjectTemplateListResp, archive_project, available_project_templates, create_project, list_projects, rename_project


def build_projects_router(*, session_state, ensure_workspace=None):
    router = APIRouter(prefix="/api/projects", tags=["projects"])

    @router.get("", response_model=ProjectListResp)
    def get_projects(user=Depends(require_hosted_user)):
        ws_root = session_state().get("workspace")
        return ProjectListResp(projects=list_projects(workspace_root=ws_root, owner_id=user.user_id))

    @router.get("/templates", response_model=ProjectTemplateListResp)
    def get_project_templates(user=Depends(require_hosted_user)):
        _ = user
        return ProjectTemplateListResp(templates=available_project_templates())

    @router.post("", response_model=ProjectResp)
    def create_project_route(req: ProjectCreateReq, user=Depends(require_hosted_user)):
        ws_root = session_state().get("workspace")
        if not ws_root and ensure_workspace:
            ws_root, _created = ensure_workspace()
            session_state()["workspace"] = ws_root
        if not ws_root:
            raise HTTPException(400, "Workspace not set")
        project = create_project(workspace_root=ws_root, owner_id=user.user_id, req=req)
        return ProjectResp(project=project)

    @router.put("/{project_id}", response_model=ProjectResp)
    def rename_project_route(project_id: str, req: ProjectRenameReq, user=Depends(require_hosted_user)):
        project = rename_project(owner_id=user.user_id, project_id=project_id, req=req)
        return ProjectResp(project=project)

    @router.delete("/{project_id}", response_model=ProjectResp)
    def archive_project_route(project_id: str, user=Depends(require_hosted_user)):
        project = archive_project(owner_id=user.user_id, project_id=project_id)
        return ProjectResp(project=project)

    return router
