from __future__ import annotations

from fastapi import APIRouter, Depends

from api.auth_policy import require_hosted_user
from api.preferences import (
    ProjectPreferencesResp,
    ProjectPreferencesUpdateReq,
    UserPreferencesResp,
    UserPreferencesUpdateReq,
    get_project_preferences,
    get_user_preferences,
    upsert_project_preferences,
    upsert_user_preferences,
)


def build_preferences_router():
    router = APIRouter(prefix="/api/preferences", tags=["preferences"])

    @router.get("/user", response_model=UserPreferencesResp)
    def get_user_preferences_route(user=Depends(require_hosted_user)):
        profile_id = user.supabase_user_id or user.user_id
        return UserPreferencesResp(preferences=get_user_preferences(profile_id=profile_id))

    @router.put("/user", response_model=UserPreferencesResp)
    def update_user_preferences_route(req: UserPreferencesUpdateReq, user=Depends(require_hosted_user)):
        profile_id = user.supabase_user_id or user.user_id
        return UserPreferencesResp(preferences=upsert_user_preferences(profile_id=profile_id, req=req))

    @router.get("/projects/{project_id}", response_model=ProjectPreferencesResp)
    def get_project_preferences_route(project_id: str, _user=Depends(require_hosted_user)):
        return ProjectPreferencesResp(preferences=get_project_preferences(project_id=project_id))

    @router.put("/projects/{project_id}", response_model=ProjectPreferencesResp)
    def update_project_preferences_route(project_id: str, req: ProjectPreferencesUpdateReq, _user=Depends(require_hosted_user)):
        return ProjectPreferencesResp(preferences=upsert_project_preferences(project_id=project_id, req=req))

    return router
