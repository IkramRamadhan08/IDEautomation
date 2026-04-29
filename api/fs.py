from __future__ import annotations

import difflib
from pathlib import Path


def safe_join(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    if not str(p).startswith(str(root.resolve())):
        raise ValueError("Path escapes workspace")
    return p


def list_tree(root: Path, rel: str = ".") -> list[dict]:
    base = safe_join(root, rel)
    if not base.exists():
        return []

    out: list[dict] = []
    for child in sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        out.append({
            "name": child.name,
            "path": str(child.relative_to(root)),
            "type": "dir" if child.is_dir() else "file",
        })
    return out


def read_text(root: Path, rel: str, max_bytes: int = 400_000) -> str:
    p = safe_join(root, rel)
    data = p.read_text(encoding="utf-8")
    b = data.encode("utf-8")
    if len(b) > max_bytes:
        return b[:max_bytes].decode("utf-8", errors="ignore") + "\n... (truncated)"
    return data


def write_text(root: Path, rel: str, content: str) -> None:
    p = safe_join(root, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def diff_text(old: str, new: str, *, filename: str = "file") -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
        )
    )
