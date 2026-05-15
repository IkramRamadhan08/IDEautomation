from __future__ import annotations

import json
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, field_validator

from api.settings import ROOT
from api.project_templates import get_project_template, list_project_templates, render_project_template
from api.supabase_store import archive_project as supabase_archive_project
from api.supabase_store import has_supabase, insert_project, list_project_files as supabase_list_project_files, list_projects as supabase_list_projects, touch_project as supabase_touch_project, update_project_name, upsert_project_files


class ProjectRecord(BaseModel):
    id: str
    owner_id: str
    name: str
    slug: str
    root: str
    created_at: int
    updated_at: int
    archived: bool = False

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def _parse_supabase_timestamp(cls, value: Any) -> int:
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return int(time.time())
            if raw.isdigit():
                return int(raw)
            try:
                return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
            except ValueError:
                return int(time.time())
        return int(time.time())


class ProjectCreateReq(BaseModel):
    name: str
    slug: str | None = None
    template_id: str | None = None


class ProjectRenameReq(BaseModel):
    name: str


class ProjectDuplicateReq(BaseModel):
    name: str | None = None


class ProjectListResp(BaseModel):
    ok: bool = True
    projects: list[ProjectRecord]


class ProjectResp(BaseModel):
    ok: bool = True
    project: ProjectRecord


class ProjectTemplateListResp(BaseModel):
    ok: bool = True
    templates: list[dict[str, Any]]


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


def _available_project_slug(*, workspace_root: Path, owner_id: str, base_slug: str) -> str:
    existing_remote_slugs: set[str] = set()
    if has_supabase():
        try:
            for raw in supabase_list_projects(owner_id=owner_id) or []:
                if isinstance(raw, dict):
                    existing_remote_slugs.add(str(raw.get("slug") or raw.get("root") or "").strip())
        except Exception:
            existing_remote_slugs = set()

    for index in range(1, 100):
        slug = base_slug if index == 1 else f"{base_slug}-{index}"
        root = _project_root(workspace_root, slug)
        if slug in existing_remote_slugs:
            continue
        if root.exists() and any(root.iterdir()):
            continue
        return slug

    return f"{base_slug}-{uuid.uuid4().hex[:8]}"


def _is_text_file(path: Path) -> bool:
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".mp4", ".mov", ".zip"}:
        return False
    return True


def _snapshot_project_files(project_dir: Path, *, max_files: int = 800, max_bytes: int = 1_000_000) -> list[dict[str, str]]:
    if not project_dir.exists():
        return []
    ignored = {".git", "node_modules", "dist", "build", ".next", ".nuxt", ".output", ".turbo", ".cache", "coverage"}
    files: list[dict[str, str]] = []
    for path in sorted(project_dir.rglob("*")):
        if len(files) >= max_files:
            break
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(project_dir)
        except ValueError:
            continue
        if any(part in ignored for part in rel.parts) or not _is_text_file(path):
            continue
        try:
            if path.stat().st_size > max_bytes:
                continue
            files.append({"path": str(rel), "content": path.read_text(encoding="utf-8")})
        except Exception:
            continue
    return files


def _find_project_record(*, workspace_root: Path | None, owner_id: str, project_id: str) -> ProjectRecord:
    for project in list_projects(workspace_root=workspace_root, owner_id=owner_id):
        if project.id == project_id:
            return project
    raise HTTPException(404, "Project not found")


def list_projects(*, workspace_root: Path | None, owner_id: str) -> list[ProjectRecord]:
    if has_supabase():
        try:
            remote = supabase_list_projects(owner_id=owner_id) or []
            records: list[ProjectRecord] = []
            for raw in remote:
                if not isinstance(raw, dict):
                    continue
                try:
                    rec = ProjectRecord(**raw)
                except Exception:
                    continue
                if rec.archived:
                    continue
                records.append(rec)
            return records
        except Exception:
            pass

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
            if not root.is_absolute():
                root = workspace_root / root
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

    slug = _available_project_slug(workspace_root=workspace_root, owner_id=owner_id, base_slug=_slugify(req.slug or name))
    root = _project_root(workspace_root, slug)
    if req.template_id and req.template_id.strip() not in {"", "blank"} and not get_project_template(req.template_id):
        raise HTTPException(400, "Unknown project template")

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

    template_files = render_project_template(template_id=req.template_id, project_root=slug, project_name=name)
    if template_files:
        for rel, content in template_files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    else:
        readme = root / "README.md"
        if not readme.exists():
            readme.write_text(f"# {name}\n\nCreated by Appora project setup.\n", encoding="utf-8")
        template_files = {"README.md": readme.read_text(encoding="utf-8")}

    if has_supabase():
        try:
            upsert_project_files(
                owner_id=owner_id,
                project_root=slug,
                files=[{"path": rel, "content": content} for rel, content in template_files.items()],
            )
            remote = insert_project(owner_id=owner_id, name=name, slug=slug, root=slug)
            if remote:
                return ProjectRecord(**remote)
        except Exception:
            pass

    state = _read_state()
    projects = state.get("projects") if isinstance(state.get("projects"), list) else []
    projects.append(rec.model_dump())
    state["projects"] = projects
    _write_state(state)
    return rec


def available_project_templates() -> list[dict[str, Any]]:
    return [{"id": "blank", "name": "Blank", "category": "Starter", "description": "Minimal project with a README only.", "best_for": "Manual setup or fully custom agent builds.", "tags": ["blank"]}, *list_project_templates()]


def rename_project(*, owner_id: str, project_id: str, req: ProjectRenameReq) -> ProjectRecord:
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(400, "Project name is required")

    if has_supabase():
        try:
            remote = update_project_name(project_id=project_id, owner_id=owner_id, name=name)
            if remote:
                return ProjectRecord(**remote)
        except Exception:
            pass

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


def duplicate_project(*, workspace_root: Path, owner_id: str, project_id: str, req: ProjectDuplicateReq) -> ProjectRecord:
    source = _find_project_record(workspace_root=workspace_root, owner_id=owner_id, project_id=project_id)
    next_name = (req.name or f"{source.name} Copy").strip()
    created = create_project(
        workspace_root=workspace_root,
        owner_id=owner_id,
        req=ProjectCreateReq(name=next_name, slug=f"{source.slug}-copy", template_id="blank"),
    )

    source_dir = _project_root(workspace_root, source.root)
    dest_dir = _project_root(workspace_root, created.root)
    if source_dir.exists():
        for item in source_dir.iterdir():
            target = dest_dir / item.name
            if item.is_dir():
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(item, target, ignore=shutil.ignore_patterns(".git", "node_modules", "dist", "build", ".next", ".nuxt", ".output", ".turbo", ".cache", "coverage"))
            elif item.is_file():
                shutil.copy2(item, target)

    if has_supabase():
        try:
            remote_rows = supabase_list_project_files(owner_id=owner_id, project_root=source.root) or []
        except Exception:
            remote_rows = []
        files = [
            {"path": str(row.get("path") or ""), "content": str(row.get("content") or "")}
            for row in remote_rows
            if isinstance(row, dict) and row.get("path") and isinstance(row.get("content"), str)
        ] or _snapshot_project_files(dest_dir)
        if files:
            for item in files:
                rel = str(item.get("path") or "").strip().lstrip("/")
                content = item.get("content")
                if not rel or not isinstance(content, str):
                    continue
                target = dest_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            try:
                upsert_project_files(owner_id=owner_id, project_root=created.root, files=files)
            except Exception:
                pass

    return save_project_snapshot(workspace_root=workspace_root, owner_id=owner_id, project_id=created.id)


def save_project_snapshot(*, workspace_root: Path, owner_id: str, project_id: str) -> ProjectRecord:
    project = _find_project_record(workspace_root=workspace_root, owner_id=owner_id, project_id=project_id)
    project_dir = _project_root(workspace_root, project.root)
    files = _snapshot_project_files(project_dir)
    now = int(time.time())

    if has_supabase():
        try:
            if files:
                upsert_project_files(owner_id=owner_id, project_root=project.root, files=files)
            remote = supabase_touch_project(project_id=project_id, owner_id=owner_id)
            if remote:
                return ProjectRecord(**remote)
        except Exception:
            pass

    state = _read_state()
    projects = state.get("projects") if isinstance(state.get("projects"), list) else []
    for index, raw in enumerate(projects):
        if not isinstance(raw, dict):
            continue
        if str(raw.get("id")) != project_id or str(raw.get("owner_id")) != owner_id:
            continue
        raw["updated_at"] = now
        projects[index] = raw
        state["projects"] = projects
        _write_state(state)
        return ProjectRecord(**raw)
    project.updated_at = now
    return project


def archive_project(*, owner_id: str, project_id: str) -> ProjectRecord:
    if has_supabase():
        try:
            remote = supabase_archive_project(project_id=project_id, owner_id=owner_id)
            if remote:
                return ProjectRecord(**remote)
        except Exception:
            pass

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
