from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json
import os
import re

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_:@./-]{2,}")


@dataclass(frozen=True)
class SkillDoc:
    skill_id: str
    title: str
    body: str
    source: str
    description: str = ""
    provider: str = "appora"


@dataclass(frozen=True)
class ProjectStackSignals:
    component_libraries: list[str] = field(default_factory=list)
    has_playwright: bool = False
    has_headless_browser: bool = False
    has_webcontainer: bool = False
    languages: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    runtimes: list[str] = field(default_factory=list)
    package_managers: list[str] = field(default_factory=list)
    validation_files: list[str] = field(default_factory=list)
    has_database_schema: bool = False
    has_infra: bool = False
    has_preview_surface: bool = False


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
    SkillDoc(
        skill_id="agentic-tool-discipline",
        title="Agentic tool discipline",
        source="builtin",
        body=(
            "Use local read-only tools for repo-local facts before editing: repo_overview for shape, stack_profile for language/framework detection, "
            "validation_plan for stack-specific test/build commands, package_scripts for JS scripts, dependency_graph for imports, component_index for React surfaces, "
            "route_map for navigation, and quality_scan for production-readiness risks. Use MCP only for external systems or live integrations."
        ),
    ),
    SkillDoc(
        skill_id="large-app-delivery",
        title="Large app delivery",
        source="builtin",
        body=(
            "For app-scale requests, identify routes, state boundaries, shared components, validation scripts, and risk hotspots before generating edits. "
            "Keep implementation, types, styles, and tests coherent so the result can scale beyond a demo."
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
    bases = [
        ws_root / ".voiceide" / "skills",
        project_dir / ".voiceide" / "skills",
        ws_root / ".codex" / "skills",
        project_dir / ".codex" / "skills",
        ws_root / ".agents" / "skills",
        project_dir / ".agents" / "skills",
        project_dir / ".claude" / "skills",
    ]
    home = Path.home()
    if ws_root != home:
        bases.extend([
            home / ".codex" / "skills",
            home / ".codex" / "plugins" / "cache",
            home / ".agents" / "skills",
            home / ".claude" / "skills",
        ])
    seen: set[Path] = set()
    for base in bases:
        try:
            root = base.expanduser().resolve()
        except Exception:
            continue
        if not root.exists() or not root.is_dir():
            continue
        patterns = ["*.md", "*/SKILL.md", "**/skills/**/SKILL.md"] if root.name == "cache" else ["*.md", "*/SKILL.md"]
        for pattern in patterns:
            for path in sorted(root.glob(pattern)):
                if not path.is_file():
                    continue
                try:
                    resolved = path.resolve()
                except Exception:
                    continue
                if resolved in seen:
                    continue
                seen.add(resolved)
                out.append(resolved)
    return out


def _parse_skill_frontmatter(text: str) -> tuple[dict[str, str], str]:
    raw = str(text or "")
    if not raw.startswith("---"):
        return {}, raw
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, raw
    meta: dict[str, str] = {}
    end_index = -1
    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = idx
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip().strip('"').strip("'")
        if key:
            meta[key] = value
    if end_index < 0:
        return {}, raw
    return meta, "\n".join(lines[end_index + 1 :]).strip()


def _skill_provider(path: Path) -> str:
    text = path.as_posix().lower()
    if "/.claude/" in text:
        return "claude"
    if "/.codex/" in text:
        return "codex"
    if "/.agents/" in text:
        return "agent"
    return "appora"


def _skill_id_from_path(path: Path, meta: dict[str, str]) -> str:
    raw = str(meta.get("name") or meta.get("id") or "").strip()
    if raw:
        return re.sub(r"[^a-zA-Z0-9_.:-]+", "-", raw).strip("-").lower()
    if path.name == "SKILL.md":
        return path.parent.name.strip().lower()
    return path.stem.strip().lower()


def _load_custom_skills(ws_root: Path, project_dir: Path, *, warnings: list[str] | None = None) -> list[SkillDoc]:
    skills: list[SkillDoc] = []
    seen_ids: set[str] = set()
    for path in _custom_skill_paths(ws_root, project_dir):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception as exc:
            if warnings is not None:
                warnings.append(f"Custom skill '{path.name}' gagal dibaca ({exc}).")
            continue
        if not text:
            continue
        meta, body = _parse_skill_frontmatter(text)
        skill_id = _skill_id_from_path(path, meta)
        if not skill_id or skill_id in seen_ids:
            continue
        seen_ids.add(skill_id)
        lines = body.splitlines() if body else text.splitlines()
        title = str(meta.get("title") or meta.get("name") or "").strip()
        if not title:
            title = (lines[0].lstrip("# ").strip() if lines else "") or path.parent.name.replace("-", " ").title()
        description = str(meta.get("description") or "").strip()
        prompt_body = "\n".join(part for part in [description, body or text] if part).strip()
        skills.append(
            SkillDoc(
                skill_id=skill_id,
                title=title,
                body=prompt_body[:6000],
                source=str(path),
                description=description,
                provider=_skill_provider(path),
            )
        )
    return skills


def list_imported_skills(ws_root: Path, project_dir: Path, *, warnings: list[str] | None = None) -> list[SkillDoc]:
    return _load_custom_skills(ws_root, project_dir, warnings=warnings)


def read_imported_skill(ws_root: Path, project_dir: Path, skill_id: str, *, warnings: list[str] | None = None) -> SkillDoc | None:
    wanted = str(skill_id or "").strip().lower()
    if not wanted:
        return None
    for skill in _load_custom_skills(ws_root, project_dir, warnings=warnings):
        if skill.skill_id.lower() == wanted:
            return skill
    return None


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


def _read_json_file(path: Path, *, warnings: list[str] | None = None) -> dict:
    if not path.exists() or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        if warnings is not None:
            warnings.append(f"{path.name} nggak kebaca buat stack detection ({exc}).")
        return {}
    return data if isinstance(data, dict) else {}


def _project_file_set(project_dir: Path, *, limit: int = 1800) -> set[str]:
    files: set[str] = set()
    ignored = {
        ".git",
        "node_modules",
        "dist",
        "build",
        ".next",
        ".vercel",
        ".voiceide",
        "__pycache__",
        ".venv",
        "venv",
        "target",
        "vendor",
    }
    for root, dirnames, filenames in os.walk(project_dir):
        root_path = Path(root)
        try:
            rel_root = root_path.relative_to(project_dir).as_posix()
        except Exception:
            continue
        dirnames[:] = [name for name in dirnames if name not in ignored and f"{rel_root}/{name}".strip("./") not in ignored]
        for filename in filenames:
            if len(files) >= limit:
                return files
            try:
                rel = (root_path / filename).relative_to(project_dir).as_posix()
            except Exception:
                continue
            if any(part in ignored for part in rel.split("/")):
                continue
            files.add(rel)
    return files


def _append_unique(items: list[str], *values: str) -> None:
    for value in values:
        clean = str(value or "").strip()
        if clean and clean not in items:
            items.append(clean)


def _package_runner(package_manager: str) -> str:
    manager = str(package_manager or "").split("@", 1)[0].strip().lower()
    return manager if manager in {"pnpm", "yarn", "bun"} else "npm"


def detect_project_stack(project_dir: Path, *, warnings: list[str] | None = None) -> ProjectStackSignals:
    pkg = _read_package_json(project_dir, warnings=warnings)
    deps = pkg.get("dependencies") if isinstance(pkg.get("dependencies"), dict) else {}
    dev_deps = pkg.get("devDependencies") if isinstance(pkg.get("devDependencies"), dict) else {}
    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
    all_names = {str(name).strip() for name in [*deps.keys(), *dev_deps.keys()] if str(name).strip()}
    files = _project_file_set(project_dir)
    root_files = {rel for rel in files if "/" not in rel}

    languages: list[str] = []
    frameworks: list[str] = []
    runtimes: list[str] = []
    package_managers: list[str] = []
    validation_files: list[str] = []

    if pkg:
        _append_unique(languages, "javascript")
        if "typescript" in all_names or "tsconfig.json" in root_files or any(rel.endswith((".ts", ".tsx")) for rel in files):
            _append_unique(languages, "typescript")
        package_manager = str(pkg.get("packageManager") or "").strip()
        if package_manager:
            _append_unique(package_managers, package_manager.split("@", 1)[0])
        elif "pnpm-lock.yaml" in root_files:
            _append_unique(package_managers, "pnpm")
        elif "yarn.lock" in root_files:
            _append_unique(package_managers, "yarn")
        elif "bun.lock" in root_files or "bun.lockb" in root_files:
            _append_unique(package_managers, "bun")
        elif "package-lock.json" in root_files:
            _append_unique(package_managers, "npm")
        else:
            _append_unique(package_managers, "npm")
    for name, framework in [
        ("react", "react"),
        ("vite", "vite"),
        ("next", "nextjs"),
        ("@remix-run/react", "remix"),
        ("astro", "astro"),
        ("svelte", "svelte"),
        ("vue", "vue"),
        ("@angular/core", "angular"),
        ("express", "express"),
        ("fastify", "fastify"),
        ("@nestjs/core", "nestjs"),
    ]:
        if name in all_names:
            _append_unique(frameworks, framework)
    if (
        "vite.config.ts" in root_files
        or "vite.config.js" in root_files
        or "@vitejs/plugin-react" in all_names
        or any("vite" in str(value).lower() for value in scripts.values())
    ):
        _append_unique(frameworks, "vite")
    if "next.config.js" in root_files or "next.config.mjs" in root_files or "next.config.ts" in root_files:
        _append_unique(frameworks, "nextjs")
    if pkg or any(rel.endswith((".js", ".mjs", ".cjs", ".ts")) for rel in files):
        _append_unique(runtimes, "node")
    if "deno.json" in root_files or "deno.jsonc" in root_files:
        _append_unique(languages, "javascript")
        _append_unique(languages, "typescript")
        _append_unique(runtimes, "deno")
        _append_unique(package_managers, "deno")
        _append_unique(validation_files, "deno.json" if "deno.json" in root_files else "deno.jsonc")

    if {"pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "Pipfile", "poetry.lock", "pytest.ini"} & root_files or any(rel.endswith(".py") for rel in files):
        _append_unique(languages, "python")
        _append_unique(package_managers, "pip" if "requirements.txt" in root_files else ("poetry" if "poetry.lock" in root_files else "python"))
    if "manage.py" in root_files:
        _append_unique(frameworks, "django")
    if any(rel.endswith(".py") for rel in files):
        for rel in files:
            if not rel.endswith(".py"):
                continue
            try:
                text = (project_dir / rel).read_text(encoding="utf-8", errors="ignore")[:20_000]
            except Exception:
                continue
            if "from fastapi" in text or "import fastapi" in text:
                _append_unique(frameworks, "fastapi")
            if "from flask" in text or "import flask" in text:
                _append_unique(frameworks, "flask")
            if "django" in text.lower():
                _append_unique(frameworks, "django")
            if {"fastapi", "flask", "django"} & set(frameworks):
                break

    if "go.mod" in root_files or any(rel.endswith(".go") for rel in files):
        _append_unique(languages, "go")
        _append_unique(package_managers, "go")
        if "go.mod" in root_files:
            _append_unique(validation_files, "go.mod")
    if "Cargo.toml" in root_files or any(rel.endswith(".rs") for rel in files):
        _append_unique(languages, "rust")
        _append_unique(package_managers, "cargo")
        if "Cargo.toml" in root_files:
            _append_unique(validation_files, "Cargo.toml")
    if "pom.xml" in root_files or "build.gradle" in root_files or "build.gradle.kts" in root_files or "gradlew" in root_files or any(rel.endswith(".java") for rel in files):
        _append_unique(languages, "java")
        if "pom.xml" in root_files:
            _append_unique(frameworks, "maven")
            _append_unique(package_managers, "maven")
            _append_unique(validation_files, "pom.xml")
        if "build.gradle" in root_files or "build.gradle.kts" in root_files or "gradlew" in root_files:
            _append_unique(frameworks, "gradle")
            _append_unique(package_managers, "gradle")
            _append_unique(validation_files, "build.gradle" if "build.gradle" in root_files else "gradlew")
    if any(rel.endswith(".kt") for rel in files):
        _append_unique(languages, "kotlin")
    if "composer.json" in root_files or any(rel.endswith(".php") for rel in files):
        _append_unique(languages, "php")
        _append_unique(package_managers, "composer")
        if "composer.json" in root_files:
            _append_unique(validation_files, "composer.json")
    if "Gemfile" in root_files or any(rel.endswith(".rb") for rel in files):
        _append_unique(languages, "ruby")
        _append_unique(package_managers, "bundler")
        if "Gemfile" in root_files:
            _append_unique(validation_files, "Gemfile")
    if any(rel.endswith(".csproj") for rel in files) or any(rel.endswith(".sln") for rel in files):
        _append_unique(languages, "csharp")
        _append_unique(package_managers, "dotnet")
    if "CMakeLists.txt" in root_files or "Makefile" in root_files or any(rel.endswith((".c", ".h")) for rel in files):
        _append_unique(languages, "c")
        if "CMakeLists.txt" in root_files:
            _append_unique(package_managers, "cmake")
            _append_unique(validation_files, "CMakeLists.txt")
        if "Makefile" in root_files:
            _append_unique(package_managers, "make")
            _append_unique(validation_files, "Makefile")
    if "CMakeLists.txt" in root_files or any(rel.endswith((".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx")) for rel in files):
        _append_unique(languages, "cpp")
        if "CMakeLists.txt" in root_files:
            _append_unique(package_managers, "cmake")
            _append_unique(validation_files, "CMakeLists.txt")
    if "Package.swift" in root_files or any(rel.endswith(".swift") for rel in files):
        _append_unique(languages, "swift")
        _append_unique(package_managers, "swift")
        if "Package.swift" in root_files:
            _append_unique(validation_files, "Package.swift")
    if "mix.exs" in root_files or any(rel.endswith((".ex", ".exs")) for rel in files):
        _append_unique(languages, "elixir")
        _append_unique(package_managers, "mix")
        if "mix.exs" in root_files:
            _append_unique(validation_files, "mix.exs")

    for rel in ["package.json", "tsconfig.json", "pyproject.toml", "requirements.txt", "pytest.ini", "manage.py"]:
        if rel in root_files:
            _append_unique(validation_files, rel)

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
    has_database_schema = any(
        rel.startswith(("supabase/migrations/", "migrations/", "prisma/"))
        or rel in {"schema.prisma", "drizzle.config.ts", "drizzle.config.js"}
        for rel in files
    ) or (project_dir / "supabase" / "migrations").is_dir() or (project_dir / "migrations").is_dir() or (project_dir / "prisma").is_dir()
    compose_file = next((rel for rel in ["docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"] if rel in root_files), "")
    has_kubernetes = any(
        rel.startswith(("k8s/", "kubernetes/", "manifests/"))
        and rel.endswith((".yaml", ".yml", ".json"))
        for rel in files
    )
    if compose_file:
        _append_unique(frameworks, "docker-compose")
        _append_unique(validation_files, compose_file)
    if has_kubernetes:
        _append_unique(frameworks, "kubernetes")
    has_infra = any(
        rel in {"Dockerfile", "docker-compose.yml", "compose.yaml", "terraform.tf", "serverless.yml"}
        or rel.endswith(".tf")
        or rel.startswith((".github/workflows/", "infra/", "k8s/", "kubernetes/", "manifests/"))
        for rel in files
    ) or bool(compose_file) or has_kubernetes
    has_preview_surface = bool(
        pkg
        or "index.html" in root_files
        or any(framework in frameworks for framework in {"vite", "nextjs", "astro", "svelte", "vue", "react"})
    )

    return ProjectStackSignals(
        component_libraries=component_libraries,
        has_playwright=has_playwright,
        has_headless_browser=has_headless_browser,
        has_webcontainer=has_webcontainer,
        languages=languages,
        frameworks=frameworks,
        runtimes=runtimes,
        package_managers=package_managers,
        validation_files=validation_files,
        has_database_schema=has_database_schema,
        has_infra=has_infra,
        has_preview_surface=has_preview_surface,
    )


def build_validation_plan(project_dir: Path, *, project_root: str = ".") -> dict:
    stack = detect_project_stack(project_dir)
    pkg = _read_package_json(project_dir)
    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
    root = str(project_root or ".").strip() or "."
    prefix = "" if root == "." else f"cd {root} && "
    files = _project_file_set(project_dir)
    commands: list[dict[str, str]] = []
    optional: list[dict[str, str]] = []

    def add(command: str, reason: str, *, kind: str = "primary") -> None:
        item = {"command": f"{prefix}{command}", "reason": reason, "kind": kind}
        target = optional if kind == "optional" else commands
        if item["command"] not in {entry["command"] for entry in commands + optional}:
            target.append(item)

    if pkg:
        manager = _package_runner(str(pkg.get("packageManager") or (stack.package_managers[0] if stack.package_managers else "npm")))
        for script in ["typecheck", "check", "lint", "test", "build"]:
            if script in scripts:
                add(f"{manager} run {script}", f"package.json exposes `{script}` script.")
        if "dev" in scripts or "preview" in scripts:
            add(f"{manager} run {'preview' if 'preview' in scripts else 'dev'}", "Preview smoke command exists; use only when a live preview check is needed.", kind="optional")

    root_files = {rel for rel in files if "/" not in rel}
    if "deno" in stack.runtimes:
        deno_config = _read_json_file(project_dir / "deno.json")
        deno_tasks = deno_config.get("tasks") if isinstance(deno_config.get("tasks"), dict) else {}
        if "test" in deno_tasks or any(rel.endswith(("_test.ts", "_test.tsx", ".test.ts", ".test.tsx")) for rel in files):
            add("deno test", "Deno project test validation.")
        add("deno check .", "Deno type/runtime check across project files.")
    if "python" in stack.languages:
        has_tests_dir = any(rel.startswith("tests/") for rel in files)
        if "pytest.ini" in root_files:
            add("python3 -m pytest", "Python test discovery via pytest config/tests directory.")
        elif has_tests_dir:
            add("python3 -m unittest discover -s tests", "Python stdlib unittest discovery from tests directory.")
        elif "manage.py" in root_files:
            add("python3 manage.py test", "Django project has manage.py.")
        add("python3 -m compileall .", "Python syntax smoke check across project files.")
    if "go" in stack.languages:
        add("go test ./...", "Go module/package validation.")
    if "rust" in stack.languages:
        add("cargo test", "Rust crate test validation.")
        add("cargo check", "Rust type/build smoke validation.", kind="optional")
    if "java" in stack.languages or "kotlin" in stack.languages:
        if "gradlew" in root_files:
            add("./gradlew test", "Gradle wrapper test validation.")
        elif "build.gradle" in root_files or "build.gradle.kts" in root_files:
            add("gradle test", "Gradle test validation.")
        if "pom.xml" in root_files:
            add("mvn test", "Maven test validation.")
    if "php" in stack.languages:
        composer = _read_json_file(project_dir / "composer.json")
        composer_scripts = composer.get("scripts") if isinstance(composer.get("scripts"), dict) else {}
        if "test" in composer_scripts:
            add("composer test", "composer.json exposes test script.")
        else:
            add("composer validate", "composer.json dependency/config validation.", kind="optional")
    if "ruby" in stack.languages:
        if "Rakefile" in root_files:
            add("bundle exec rake test", "Ruby project has Rakefile test entry.")
        elif any(rel.startswith("spec/") for rel in root_files):
            add("bundle exec rspec", "Ruby project has spec directory.")
    if "csharp" in stack.languages:
        add("dotnet test", ".NET solution/project test validation.")
    if "c" in stack.languages or "cpp" in stack.languages:
        if "build" in {rel.split("/", 1)[0] for rel in files} or (project_dir / "build").is_dir():
            add("cmake --build build", "CMake build directory exists; build native project.")
        elif "Makefile" in root_files:
            add("make test", "Makefile-based native project test target.")
        elif "CMakeLists.txt" in root_files:
            add("cmake -S . -B build", "Configure CMake project before building.", kind="optional")
    if "swift" in stack.languages:
        add("swift test", "Swift package test validation.")
    if "elixir" in stack.languages:
        add("mix test", "Elixir Mix test validation.")
    if any(rel.endswith(".tf") for rel in root_files):
        add("terraform validate", "Terraform configuration validation.", kind="optional")
    if "docker-compose" in stack.frameworks:
        add("docker compose config", "Docker Compose configuration validation.", kind="optional")
    if "kubernetes" in stack.frameworks:
        for candidate in ["k8s", "kubernetes", "manifests"]:
            if any(rel.startswith(f"{candidate}/") for rel in files):
                add(f"kubectl apply --dry-run=client -f {candidate}", "Kubernetes manifest client-side validation.", kind="optional")
                break

    return {
        "project_root": root,
        "detected_stack": {
            "languages": stack.languages,
            "frameworks": stack.frameworks,
            "runtimes": stack.runtimes,
            "package_managers": stack.package_managers,
            "validation_files": stack.validation_files,
            "has_database_schema": stack.has_database_schema,
            "has_infra": stack.has_infra,
            "has_preview_surface": stack.has_preview_surface,
        },
        "commands": commands[:12],
        "optional_commands": optional[:8],
        "confidence": "high" if commands else ("medium" if optional else "low"),
        "note": "Run the smallest relevant validation set for the files changed; do not assume this is only a frontend project.",
    }


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
                    " ".join(stack.languages),
                    " ".join(stack.frameworks),
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
        if skill.skill_id == "agentic-tool-discipline" and any(token in query_tokens for token in {"tool", "tools", "mcp", "skill", "agent", "agentic", "codex", "cursor"}):
            bonus += 0.85
        if skill.skill_id == "large-app-delivery" and any(token in query_tokens for token in {"app", "project", "large", "gede", "production", "cursor", "claude", "feature"}):
            bonus += 0.65
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
