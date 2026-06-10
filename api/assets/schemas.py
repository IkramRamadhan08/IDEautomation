from __future__ import annotations

from pydantic import BaseModel


class ImageAssetResp(BaseModel):
    ok: bool
    path: str
    name: str
    title: str | None = None
    alias: str | None = None
    content_type: str | None = None
    size: int
