from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import re

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_:@./-]{2,}")


@dataclass(frozen=True)
class SkillDoc:
    skill_id: str
    title: str
    body: str
    source: str


@dataclass(frozen=True)
class ProjectStackSignals:
    component_libraries: list[str]
    has_playwright: bool
    has_headless_browser: bool
    has_webcontainer: bool


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
    SkillDoc(
        skill_id="component-library-awareness",
        title="Component library awareness",
        source="builtin",
        body=(
            "If the project already uses component primitives or a UI kit, extend that library first instead of inventing a parallel design system. "
            "Prefer composition, accessibility, and consistent tokens over one-off handcrafted widgets."
        ),
    ),
    SkillDoc(
        skill_id="browser-runtime-boundaries",
        title="Browser runtime boundaries",
        source="builtin",
        body=(
            "Be honest about browser automation and container limits. If headless browser or webcontainer support is not available, do not pretend those runtimes exist. "
            "Use available preview audit and validation paths instead."
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


def _load_custom_skills(ws_root: Path, project_dir: Path, *, warnings: list[str] | None = None) -> list[SkillDoc]:
    skills: list[SkillDoc] = []
    for path in _custom_skill_paths(ws_root, project_dir):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception as exc:
            if warnings is not None:
                warnings.append(f"Custom skill '{path.name}' gagal dibaca ({exc}).")
            continue
        if not text:
            continue
        lines = text.splitlines()
        title = lines[0].lstrip("# ").strip() or path.stem.replace("-", " ")
        skills.append(SkillDoc(skill_id=path.stem, title=title, body=text[:4000], source=str(path)))
    return skills


def _read_package_json(project_dir: Path, *, warnings: list[str] | None = None) -> dict:
    path = project_dir / "package.json"
    if not path.exists() or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        if warnings is not None:
            warnings.append(f"package.json nggak kebaca buat stack detection ({exc}).")
        return {}
    if not isinstance(data, dict):
        if warnings is not None:
            warnings.append("package.json kebaca tapi formatnya bukan object JSON, jadi stack detection diskip.")
        return {}
    return data


def detect_project_stack(project_dir: Path, *, warnings: list[str] | None = None) -> ProjectStackSignals:
    pkg = _read_package_json(project_dir, warnings=warnings)
    deps = pkg.get("dependencies") if isinstance(pkg.get("dependencies"), dict) else {}
    dev_deps = pkg.get("devDependencies") if isinstance(pkg.get("devDependencies"), dict) else {}
    all_names = {str(name).strip() for name in [*deps.keys(), *dev_deps.keys()] if str(name).strip()}

    component_libraries: list[str] = []
    if any(name.startswith("@radix-ui/") for name in all_names):
        component_libraries.append("radix-ui")
    if "@headlessui/react" in all_names:
        component_libraries.append("headless-ui")
    if any(name.startswith("@ariakit/") for name in all_names):
        component_libraries.append("ariakit")
    if "@mui/material" in all_names:
        component_libraries.append("mui")
    if "@chakra-ui/react" in all_names:
        component_libraries.append("chakra-ui")
    if "antd" in all_names:
        component_libraries.append("antd")
    if "react-aria-components" in all_names or "react-aria" in all_names:
        component_libraries.append("react-aria")
    if "class-variance-authority" in all_names or "tailwind-merge" in all_names:
        component_libraries.append("shadcn-style")

    has_playwright = "playwright" in all_names or "@playwright/test" in all_names
    has_headless_browser = has_playwright or "puppeteer" in all_names
    has_webcontainer = "@webcontainer/api" in all_names

    return ProjectStackSignals(
        component_libraries=component_libraries,
        has_playwright=has_playwright,
        has_headless_browser=has_headless_browser,
        has_webcontainer=has_webcontainer,
    )


def _stack_skills(project_dir: Path, *, warnings: list[str] | None = None) -> list[SkillDoc]:
    stack = detect_project_stack(project_dir, warnings=warnings)
    out: list[SkillDoc] = []
    if stack.component_libraries:
        libs = ", ".join(stack.component_libraries)
        out.append(
            SkillDoc(
                skill_id="project-component-libraries",
                title="Project component libraries",
                source="detected:package.json",
                body=(
                    f"Detected component libraries: {libs}. Prefer using or extending those primitives first. "
                    "Keep accessibility, focus management, portals, and overlay behavior aligned with the installed primitives."
                ),
            )
        )
    if stack.has_headless_browser:
        driver = "Playwright" if stack.has_playwright else "Puppeteer"
        out.append(
            SkillDoc(
                skill_id="project-headless-browser",
                title="Project headless browser tooling",
                source="detected:package.json",
                body=(
                    f"Detected {driver} in the project. If browser-level testing or interaction coverage is relevant, keep selectors and flows testable. "
                    "Prefer stable roles, labels, and deterministic UI states."
                ),
            )
        )
    if stack.has_webcontainer:
        out.append(
            SkillDoc(
                skill_id="project-webcontainer",
                title="Project WebContainer runtime",
                source="detected:package.json",
                body=(
                    "Detected @webcontainer/api. If the task touches in-browser runtime or sandbox execution, preserve that path instead of assuming a host-only preview flow."
                ),
            )
        )
    return out


def resolve_agent_skills(
    ws_root: Path,
    *,
    project_dir: Path,
    query: str,
    build_mode: str,
    active_rel: str,
    preview_url: str | None,
    limit: int = 4,
    warnings: list[str] | None = None,
) -> list[SkillDoc]:
    stack = detect_project_stack(project_dir, warnings=warnings)
    query_tokens = _tokenize(
        "\n".join(
            filter(
                None,
                [
                    query,
                    build_mode,
                    active_rel,
                    preview_url or "",
                    " ".join(stack.component_libraries),
                    "playwright" if stack.has_playwright else "",
                    "headless-browser" if stack.has_headless_browser else "",
                    "webcontainer" if stack.has_webcontainer else "",
                ],
            )
        )
    )
    pool = list(_BUILTIN_SKILLS) + _stack_skills(project_dir, warnings=warnings) + _load_custom_skills(ws_root, project_dir, warnings=warnings)
    scored: list[tuple[float, SkillDoc]] = []
    for skill in pool:
        bonus = 0.0
        if build_mode == "hybrid" and skill.skill_id == "scoped-copilot":
            bonus += 0.6
        if preview_url and skill.skill_id == "preview-and-validation":
            bonus += 0.4
        if stack.component_libraries and skill.skill_id in {"component-library-awareness", "project-component-libraries"}:
            bonus += 0.8
        if (stack.has_headless_browser or stack.has_webcontainer) and skill.skill_id == "browser-runtime-boundaries":
            bonus += 0.35
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
