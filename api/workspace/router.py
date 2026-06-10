from __future__ import annotations

from pathlib import Path, PurePosixPath

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from api import settings as settings_mod
from api.app_state import CURRENT_USER_ID
from api.storage.supabase import has_supabase, upsert_project_files as supabase_upsert_project_files
from api.workspace.schemas import IdentityInfo, IdentityUpdateReq, WorkspaceInfo, WorkspaceProvisionResp, WorkspaceSetReq


def build_workspace_router(
    *,
    session_state,
    identity_info,
    upsert_current_user_profile,
    provision_managed_workspace,
    is_serverless_runtime,
    is_text_rel_path,
):
    router = APIRouter(prefix="/api", tags=["workspace"])

    @router.get("/identity", response_model=IdentityInfo)
    def get_identity():
        return identity_info()

    @router.put("/identity", response_model=IdentityInfo)
    def update_identity_profile(req: IdentityUpdateReq):
        upsert_current_user_profile(display_name=req.display_name, email=req.email)
        return identity_info()

    @router.get("/workspace", response_model=WorkspaceInfo)
    def get_workspace():
        p: Path | None = session_state()["workspace"]
        if p is None and has_supabase():
            p, _created = provision_managed_workspace()
            session_state()["workspace"] = p
        return WorkspaceInfo(path=str(p) if p else None, default=settings_mod.settings.default_workspace)

    @router.post("/workspace")
    def set_workspace(req: WorkspaceSetReq):
        if is_serverless_runtime():
            raise HTTPException(400, "Picking arbitrary host folders is disabled in hosted/serverless deployments.")
        p = Path(req.path).expanduser().resolve()
        if not p.exists() or not p.is_dir():
            raise HTTPException(400, "Workspace path must be an existing directory")
        session_state()["workspace"] = p
        session_state()["hydrated_projects"] = set()
        return {"ok": True, "path": str(p)}

    @router.post("/workspace/clear")
    def clear_workspace():
        session_state()["workspace"] = None
        session_state()["hydrated_projects"] = set()
        return {"ok": True}

    @router.post("/workspace/provision", response_model=WorkspaceProvisionResp)
    def provision_workspace():
        session_dir, created = provision_managed_workspace()
        session_state()["workspace"] = session_dir
        session_state()["hydrated_projects"] = set()
        return WorkspaceProvisionResp(ok=True, path=str(session_dir), created=created)

    @router.post("/workspace/pick")
    def pick_workspace():
        import shutil
        import subprocess

        if is_serverless_runtime():
            raise HTTPException(400, "Native folder picking is only available in local desktop/dev mode.")

        if shutil.which("zenity"):
            try:
                r = subprocess.run(
                    ["zenity", "--file-selection", "--directory", "--title=Pick workspace folder"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if r.returncode == 0:
                    p = (r.stdout or "").strip()
                    if p:
                        return {"ok": True, "path": p}
                return {"ok": False, "path": None}
            except Exception:
                pass

        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            p = filedialog.askdirectory(title="Pick workspace folder")
            try:
                root.destroy()
            except Exception:
                pass

            if p:
                return {"ok": True, "path": p}
            return {"ok": False, "path": None}
        except Exception:
            return {"ok": False, "path": None}

    @router.post("/workspace/import-browser-folder", response_model=WorkspaceProvisionResp)
    async def import_browser_folder(files: list[UploadFile] = File(...), paths: list[str] = Form(...)):
        if not files:
            raise HTTPException(400, "No files uploaded")
        if len(files) != len(paths):
            raise HTTPException(400, "Uploaded files/path metadata mismatch")

        workspace_dir, _created = provision_managed_workspace()

        root_name: str | None = None
        target_root: Path | None = None
        target_root_preexisting = False

        for upload, rel_raw in zip(files, paths):
            rel = PurePosixPath((rel_raw or "").strip())
            if not rel.parts:
                raise HTTPException(400, "Invalid uploaded path")
            if rel.is_absolute() or any(part in {"", ".", ".."} for part in rel.parts):
                raise HTTPException(400, "Unsafe uploaded path")

            if root_name is None:
                root_name = rel.parts[0]
                target_root = (workspace_dir / root_name).resolve()
                if workspace_dir != target_root and workspace_dir not in target_root.parents:
                    raise HTTPException(400, "Invalid target import root")
                target_root_preexisting = target_root.exists()
            elif rel.parts[0] != root_name:
                raise HTTPException(400, "Please choose exactly one folder")

            assert target_root is not None
            inner_parts = rel.parts[1:] if len(rel.parts) > 1 else (upload.filename or rel.parts[-1],)
            dest = target_root.joinpath(*inner_parts).resolve()
            if target_root != dest and target_root not in dest.parents:
                raise HTTPException(400, "Unsafe destination path")
            dest.parent.mkdir(parents=True, exist_ok=True)
            content = await upload.read()
            dest.write_bytes(content)
            if has_supabase() and is_text_rel_path(str(PurePosixPath(*inner_parts))):
                try:
                    supabase_upsert_project_files(
                        owner_id=CURRENT_USER_ID.get(),
                        project_root=root_name,
                        files=[{"path": str(PurePosixPath(*inner_parts)), "content": content.decode("utf-8")}],
                    )
                except Exception:
                    pass

        if target_root is None:
            raise HTTPException(400, "No folder content received")

        session_state()["workspace"] = target_root
        session_state()["hydrated_projects"] = set()
        return WorkspaceProvisionResp(ok=True, path=str(target_root), created=not target_root_preexisting, managed=True)

    return router
