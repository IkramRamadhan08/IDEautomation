from __future__ import annotations

import re
import time
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from api.assets.schemas import ImageAssetResp
from api.fs import safe_join


def _sanitize_uploaded_filename(name: str) -> str:
    stem = Path(name or "image").stem or "image"
    stem = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in stem).strip("-") or "image"
    suffix = Path(name or "").suffix.lower()
    allowed = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}
    if suffix not in allowed:
        suffix = ".png"
    return f"{stem[:50]}{suffix}"


def _sanitize_asset_alias(value: str | None, fallback: str = "image") -> str:
    raw = (value or "").strip().lstrip("@")
    alias = "".join(ch.lower() if ch.isalnum() else "-" for ch in raw).strip("-")
    alias = re.sub(r"-{2,}", "-", alias)
    return (alias or fallback)[:40]


def build_assets_router(*, workspace_root, hydrate_hosted_project):
    router = APIRouter(prefix="/api/assets", tags=["assets"])

    @router.post("/image", response_model=ImageAssetResp)
    async def upload_image_asset(project_root: str = Form("."), file: UploadFile = File(...), title: str | None = Form(None)):
        ws_root = workspace_root()
        proj_root = (project_root or ".").strip() or "."
        hydrate_hosted_project(ws_root, proj_root)
        project_dir = safe_join(ws_root, proj_root)
        if not project_dir.exists() or not project_dir.is_dir():
            raise HTTPException(400, "project_root must exist inside workspace")

        content_type = (file.content_type or "").strip().lower()
        if not content_type.startswith("image/"):
            raise HTTPException(400, "Only image uploads are supported")

        data = await file.read()
        if not data:
            raise HTTPException(400, "Uploaded image is empty")
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(400, "Image too large (max 10 MB)")

        filename = _sanitize_uploaded_filename(file.filename or "image")
        target_dir = safe_join(project_dir, "public/uploads")
        target_dir.mkdir(parents=True, exist_ok=True)

        candidate = target_dir / filename
        if candidate.exists():
            candidate = target_dir / f"{candidate.stem}-{int(time.time())}{candidate.suffix}"
        candidate.write_bytes(data)

        rel = str(candidate.relative_to(ws_root))
        fallback_alias = _sanitize_asset_alias(candidate.stem, "image")
        alias = _sanitize_asset_alias(title, fallback_alias)
        return ImageAssetResp(ok=True, path=rel, name=candidate.name, title=title or alias, alias=alias, content_type=content_type or None, size=len(data))

    return router
