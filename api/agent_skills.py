from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_:-]{2,}")


@dataclass(frozen=True)
class SkillDoc:
    skill_id: str
    title: str
    body: str
    source: str


_BUILTIN_SKILLS: list[SkillDoc] = [
    SkillDoc(
        skill_id="ui-polish",
        title="UI polish",
        source="builtin",
        body=(
            "Use when the request touches layout, visual hierarchy, spacing, copy clarity, empty/loading/error states, or perceived product quality. "
            "Prefer coherent product decisions over cosmetic one-off tweaks."
        ),
    ),
    SkillDoc(
        skill_id="react-vite-typescript",
        title="React + Vite + TypeScript delivery",
        source="builtin",
        body=(
            "Use existing React/Vite/TypeScript patterns, keep imports clean, avoid broad rewrites when a local fix is enough, "
            "and return complete file contents that still build."
        ),
    ),
    SkillDoc(
        skill_id="preview-and-validation",
        title="Preview and validation discipline",
        source="builtin",
        body=(
            "When a task affects runnable UX, optimize for the live preview, not only code shape. "
            "Leave the project in a state that is easier to validate and demo."
        ),
    ),
    SkillDoc(
        skill_id="scoped-copilot",
        title="Scoped copilot discipline",
        source="builtin",
        body=(
            "In hybrid mode, stay close to the active file and user momentum. Touch the fewest files that still make the fix complete."
        ),
    ),
]


def _tokenize(text: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(text or "")}


def _score(query_tokens: set[str], text: str) -> float:
    hay = _tokenize(text)
    if not hay:
        return 0.0
    overlap = query_tokens & hay
    if not overlap:
        return 0.0
    return len(overlap) / max(1.0, len(query_tokens)) + len(overlap) / max(8.0, len(hay))


def _custom_skill_paths(ws_root: Path, project_dir: Path) -> list[Path]:
    out: list[Path] = []
    for base in [ws_root / ".voiceide" / "skills", project_dir / ".voiceide" / "skills"]:
        if not base.exists() or not base.is_dir():
            continue
        for path in sorted(base.glob("*.md")):
            if path.is_file():
                out.append(path)
    return out


def _load_custom_skills(ws_root: Path, project_dir: Path) -> list[SkillDoc]:
    skills: list[SkillDoc] = []
    for path in _custom_skill_paths(ws_root, project_dir):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception:
            continue
        if not text:
            continue
        lines = text.splitlines()
        title = lines[0].lstrip("# ").strip() or path.stem.replace("-", " ")
        skills.append(SkillDoc(skill_id=path.stem, title=title, body=text[:4000], source=str(path)))
    return skills


def resolve_agent_skills(
    ws_root: Path,
    *,
    project_dir: Path,
    query: str,
    build_mode: str,
    active_rel: str,
    preview_url: str | None,
    limit: int = 4,
) -> list[SkillDoc]:
    query_tokens = _tokenize("\n".join(filter(None, [query, build_mode, active_rel, preview_url or ""])))
    pool = list(_BUILTIN_SKILLS) + _load_custom_skills(ws_root, project_dir)
    scored: list[tuple[float, SkillDoc]] = []
    for skill in pool:
        bonus = 0.0
        if build_mode == "hybrid" and skill.skill_id == "scoped-copilot":
            bonus += 0.6
        if preview_url and skill.skill_id == "preview-and-validation":
            bonus += 0.4
        score = _score(query_tokens, f"{skill.title}\n{skill.body}") + bonus
        if score > 0:
            scored.append((score, skill))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [skill for _score_value, skill in scored[:limit]]


def format_skill_prompt(skills: list[SkillDoc]) -> str:
    if not skills:
        return ""
    lines = ["APPLICABLE SKILLS:"]
    for skill in skills:
        lines.append(f"- {skill.title} ({skill.skill_id}) [{skill.source}]\n  {skill.body}")
    return "\n".join(lines)
