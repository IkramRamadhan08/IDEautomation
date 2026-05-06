from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel

from api.settings import ROOT
from api.supabase_store import archive_project as supabase_archive_project
from api.supabase_store import has_supabase, insert_project, list_projects as supabase_list_projects, update_project_name, upsert_project_files


class ProjectRecord(BaseModel):
    id: str
    owner_id: str
    name: str
    slug: str
    root: str
    created_at: int
    updated_at: int
    archived: bool = False


class ProjectCreateReq(BaseModel):
    name: str
    slug: str | None = None


class ProjectRenameReq(BaseModel):
    name: str


class ProjectListResp(BaseModel):
    ok: bool = True
    projects: list[ProjectRecord]


class ProjectResp(BaseModel):
    ok: bool = True
    project: ProjectRecord


PROJECTS_STATE_PATH = ROOT / ".voiceide-projects.json"


def _slugify(value: str) -> str:
    safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in (value or "").strip())
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe.strip("-") or f"project-{uuid.uuid4().hex[:8]}"


def _read_state() -> dict[str, Any]:
    if not PROJECTS_STATE_PATH.exists():
        return {"projects": []}
    try:
        data = json.loads(PROJECTS_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"projects": []}
    except Exception:
        return {"projects": []}


def _write_state(data: dict[str, Any]) -> None:
    PROJECTS_STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _project_root(workspace_root: Path, slug: str) -> Path:
    root = (workspace_root / slug).resolve()
    if workspace_root != root and workspace_root not in root.parents:
        raise HTTPException(400, "Invalid project root")
    return root


def list_projects(*, workspace_root: Path | None, owner_id: str) -> list[ProjectRecord]:
    if has_supabase():
        remote = supabase_list_projects(owner_id=owner_id) or []
        records: list[ProjectRecord] = []
        for raw in remote:
            if not isinstance(raw, dict):
                continue
            try:
                records.append(ProjectRecord(**raw))
            except Exception:
                continue
        return records

    state = _read_state()
    records: list[ProjectRecord] = []
    for raw in state.get("projects") or []:
        if not isinstance(raw, dict):
            continue
        try:
            rec = ProjectRecord(**raw)
        except Exception:
            continue
        if rec.owner_id != owner_id or rec.archived:
            continue
        if workspace_root is not None:
            root = Path(rec.root)
            if not root.exists():
                continue
            if workspace_root != root and workspace_root not in root.parents:
                continue
        records.append(rec)
    records.sort(key=lambda item: (item.updated_at, item.created_at), reverse=True)
    return records


def create_project(*, workspace_root: Path, owner_id: str, req: ProjectCreateReq) -> ProjectRecord:
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(400, "Project name is required")

    slug = _slugify(req.slug or name)
    root = _project_root(workspace_root, slug)
    if root.exists() and any(root.iterdir()):
        raise HTTPException(409, "A project with that slug already exists in this workspace")

    root.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    rec = ProjectRecord(
        id=str(uuid.uuid4()),
        owner_id=owner_id,
        name=name,
        slug=slug,
        root=slug,
        created_at=now,
        updated_at=now,
        archived=False,
    )

    readme = root / "README.md"
    if not readme.exists():
        readme.write_text(f"# {name}\n\nCreated by Voice IDE project setup.\n", encoding="utf-8")

    if has_supabase():
        upsert_project_files(owner_id=owner_id, project_root=slug, files=[{"path": "README.md", "content": readme.read_text(encoding="utf-8")}])
        remote = insert_project(owner_id=owner_id, name=name, slug=slug, root=slug)
        if remote:
            return ProjectRecord(**remote)

    state = _read_state()
    projects = state.get("projects") if isinstance(state.get("projects"), list) else []
    projects.append(rec.model_dump())
    state["projects"] = projects
    _write_state(state)
    return rec


def rename_project(*, owner_id: str, project_id: str, req: ProjectRenameReq) -> ProjectRecord:
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(400, "Project name is required")

    if has_supabase():
        remote = update_project_name(project_id=project_id, owner_id=owner_id, name=name)
        if remote:
            return ProjectRecord(**remote)

    state = _read_state()
    projects = state.get("projects") if isinstance(state.get("projects"), list) else []
    for index, raw in enumerate(projects):
        if not isinstance(raw, dict):
            continue
        if str(raw.get("id")) != project_id or str(raw.get("owner_id")) != owner_id:
            continue
        raw["name"] = name
        raw["updated_at"] = int(time.time())
        projects[index] = raw
        state["projects"] = projects
        _write_state(state)
        return ProjectRecord(**raw)
    raise HTTPException(404, "Project not found")


def archive_project(*, owner_id: str, project_id: str) -> ProjectRecord:
    if has_supabase():
        remote = supabase_archive_project(project_id=project_id, owner_id=owner_id)
        if remote:
            return ProjectRecord(**remote)

    state = _read_state()
    projects = state.get("projects") if isinstance(state.get("projects"), list) else []
    for index, raw in enumerate(projects):
        if not isinstance(raw, dict):
            continue
        if str(raw.get("id")) != project_id or str(raw.get("owner_id")) != owner_id:
            continue
        raw["archived"] = True
        raw["updated_at"] = int(time.time())
        projects[index] = raw
        state["projects"] = projects
        _write_state(state)
        return ProjectRecord(**raw)
    raise HTTPException(404, "Project not found")
