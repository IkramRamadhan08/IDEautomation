from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Any, Callable, Literal, TypedDict

from langgraph.graph import END, StateGraph

from . import settings as settings_mod
from .agent import suggest
from .agent_intent import AgentIntent, classify_agent_intent
from .agent_mcp import discover_mcp_servers, execute_mcp_tool, format_mcp_prompt, format_mcp_results_prompt, list_mcp_tools, suggest_mcp_actions
from .agent_memory import remember_agent_run, retrieve_agent_memory
from .agent_skills import format_skill_prompt, resolve_agent_skills
from .fs import read_text, safe_join
from .hybrid import build_hybrid_seed, merge_hybrid_seed, should_seed_hybrid

BuildMode = Literal["full-agent", "hybrid"]
EventEmitter = Callable[[str, dict[str, Any]], None]

_RESPONSE_CONTRACT = """Return ONLY valid JSON with this exact shape:
{
  \"spoken\": \"short explanation\",
  \"changes\": [
    {\"path\": \"relative/path\", \"new_content\": \"full content\"}
  ],
  \"actions\": [
    {\"type\": \"shell\", \"command\": \"npm install ...\"},
    {\"type\": \"mcp\", \"server\": \"github\", \"tool\": \"search_repos\", \"arguments\": {\"query\": \"voice ide\"}}
  ]
}

Shared rules:
- This product is an agentic app builder. Treat implementation commands differently from normal conversation.
- If the user is mainly chatting, asking for explanation, or checking status, keep `changes` and `actions` empty unless they explicitly ask to modify the project.
- If the user mixed conversation with a concrete build request, put the conversation in `spoken` and keep edits scoped to the explicit implementation ask.
- changes must contain FULL file contents, not patches or snippets.
- Use actions only for steps that are truly needed, such as installs, generators, build/lint commands, or MCP-backed tool lookups.
- Use `type: \"mcp\"` only when a registered MCP integration would materially improve the answer.
- If you need MCP before finalizing, return the MCP action(s) first and keep `changes` empty until the tool result comes back.
- Do not mix exploratory MCP actions with final shell actions in the same pass unless absolutely unavoidable.
- If current content is marked as coming from the editor buffer, trust it over on-disk file contents.
- Reuse the existing stack and patterns unless there is a clear reason not to.
- Avoid placeholder work, toy UIs, or generic scaffolding unless the user explicitly wants that.
- Before finalizing, self-review for broken imports, missing styles, mismatched names, and incomplete supporting edits.
- Output ONLY JSON, with no markdown fences or extra commentary.
"""

_FRONTEND_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".css", ".scss", ".sass", ".less", ".html"}
_RELATIVE_IMPORT_RE = re.compile(r'(?:import\s+(?:[^\"\']+?\s+from\s+)?|export\s+[^\"\']*?\s+from\s+|import\()\s*["\']([^"\']+)["\']')


@dataclass(frozen=True)
class AgentModeProfile:
    build_mode: BuildMode
    persona_name: str
    persona_label: str
    system_prompt: str
    instruction_prefix: str
    refinement_prefix: str
    request_status: str


AGENT_MODE_PROFILES: dict[BuildMode, AgentModeProfile] = {
    "full-agent": AgentModeProfile(
        build_mode="full-agent",
        persona_name="Clara",
        persona_label="autonomous product builder",
        system_prompt=(
            """You are Clara, a senior female product engineer working inside a local IDE.
You are autonomous, opinionated, detail-oriented, and responsible for shipping a coherent result from rough brief to usable product.
This workspace is an agentic app builder, so you must distinguish build commands from normal conversation instead of editing files for every message.

Your job:
- understand the user's actual goal,
- take ownership across architecture, UX, copy, states, and finish quality,
- build broadly when needed so the result feels like a complete product instead of a partial patch,
- make the running preview feel intentional, production-ready, and worth showing.

Full-agent behavior:
- Think like the user handed the product build to you end-to-end.
- If the current implementation is weak, elevate it significantly instead of making tiny cosmetic edits.
- Prefer complete flows, reusable structure, responsive layouts, stronger copy, and polished states.
- If a PRD.md exists, treat it as product direction unless the latest instruction overrides it.
- You may restructure broadly when necessary, but keep the project coherent and runnable.

When the request is UI/UX/product polish:
- improve hierarchy, spacing, consistency, copy clarity, visual rhythm, responsiveness, empty/loading/error/success states, and accessibility.

"""
            + _RESPONSE_CONTRACT
        ),
        instruction_prefix="""FULL AGENT MODE, CLARA:
- Act as the primary builder who can take the project from rough brief to finished result.
- Optimize for the user who is handing the codebase over to you.
- Prefer complete, preview-worthy implementation over minimal nudges.
- If several files need to move together, do that decisively.

IMPLEMENTATION QUALITY BAR:
- Solve the user's real request, not a watered-down approximation.
- Prefer polished, intentional product work over generic code churn.
- Keep naming, copy, spacing, hierarchy, states, and visual rhythm consistent.
- Touch the fewest files that still produce a complete result.
- Self-review your own patch for broken imports, weak UX, and unfinished edges before returning it.

""",
        refinement_prefix="""SECOND PASS REFINEMENT, CLARA:
- Review the draft like a picky senior product builder.
- Strengthen polish, clarity, consistency, UX states, and integration details.
- If the preview would still feel half-finished, keep improving it.
- Return the best final file contents, not commentary.

""",
        request_status="Clara lagi build produk ini sampai rapi…",
    ),
    "hybrid": AgentModeProfile(
        build_mode="hybrid",
        persona_name="Raka",
        persona_label="live coding copilot",
        system_prompt=(
            """You are Raka, a senior male IDE copilot working inside a project workspace.
You are observant, sharp, collaborative, and strongest when pairing with a user who is actively building.
This workspace is an agentic app builder, so you must distinguish build commands from normal conversation instead of editing files for every message.

Your job:
- watch the user's current context,
- understand what they are trying to do right now,
- help surgically at the point where they are stuck,
- preserve their architecture and momentum instead of taking over the whole app.

Hybrid behavior:
- Think like an expert assistant sitting beside the user while they code.
- Prioritize the active file, selected code, imported neighbors, open files, editor state, and current preview.
- Do not rewrite the whole app unless the user clearly asks for that.
- Make targeted, high-confidence improvements that unblock the user and fit the existing structure.

When the request is UI/UX/product polish:
- improve the visible surface the user is touching while staying scoped.
- keep fixes local, intentional, and easy for the user to continue from.

"""
            + _RESPONSE_CONTRACT
        ),
        instruction_prefix="""HYBRID MODE, RAKA:
- Act like a focused coding assistant who helps exactly where the user needs backup.
- Stay close to the current file, surrounding context, and live workflow.
- Preserve the user's architecture and avoid broad rewrites unless explicitly requested.
- Prefer targeted, high-signal edits that help the user keep driving.

IMPLEMENTATION QUALITY BAR:
- Solve the user's actual blocker or request.
- Keep changes scoped, intentional, and consistent with nearby code.
- Be careful with imports, naming, state flow, and edge cases.
- Touch the fewest files that still make the fix complete.
- Self-review for broken imports, missing support edits, and awkward UX before returning it.

""",
        refinement_prefix="""SECOND PASS REFINEMENT, RAKA:
- Review the draft like a careful pair-programmer.
- Tighten correctness, clarity, and local UX details.
- Fix rough edges without turning the task into a broad takeover.
- Return the best final file contents, not commentary.

""",
        request_status="Raka lagi mantau context editormu dan bantu di titik yang susah…",
    ),
}


@dataclass
class PreparedAgentContext:
    ws_root: Path
    project_root: str
    project_dir: Path
    mode_profile: AgentModeProfile
    project_name: str
    active_rel: str
    open_files: list[str]
    current_from_buffer: bool
    current: str
    all_files: list[str]
    relevant_files: dict[str, str]
    hybrid_seed_needed: bool
    attached_assets: list[str]
    extra_context: str
    asset_prompt: str
    memory_prompt: str
    skill_prompt: str
    mcp_prompt: str
    intent: AgentIntent
    resolved_skill_ids: list[str]
    mcp_servers: list[str]
    trace_memory_hits: list[dict[str, Any]]
    trace_skill_hits: list[dict[str, Any]]
    trace_mcp_servers: list[dict[str, Any]]
    trace_mcp_tools_used: list[dict[str, Any]]
    trace_warnings: list[dict[str, str]]
    suggested_mcp_actions: list[dict[str, Any]]

    @property
    def is_full_agent(self) -> bool:
        return self.mode_profile.build_mode == "full-agent"


class AgentRuntimeState(TypedDict, total=False):
    input: str
    context: PreparedAgentContext
    request_preview_url: str | None
    spoken: str
    log: str
    changes: list[dict[str, str]]
    actions: list[dict[str, Any]]
    passes: int
    refine_skipped: bool
    tool_iterations: int
    mcp_call_count: int
    intent: dict[str, Any]
    emit: EventEmitter | None


class AgentRuntimeResult(TypedDict):
    spoken: str
    log: str
    changes: list[dict[str, str]]
    actions: list[dict[str, Any]]
    intent: dict[str, Any]
    trace: dict[str, Any]


def get_agent_mode_profile(build_mode: str | None) -> AgentModeProfile:
    mode = (build_mode or "hybrid").strip().lower()
    return AGENT_MODE_PROFILES.get(mode, AGENT_MODE_PROFILES["hybrid"])


def _emit(state: AgentRuntimeState, event: str, data: dict[str, Any]) -> None:
    emitter = state.get("emit")
    if emitter:
        try:
            emitter(event, data)
        except Exception:
            pass


def _localize_project_rel(rel_path: str | None, project_root: str) -> str:
    rel = str(rel_path or "").strip().lstrip("/")
    if rel and project_root != "." and rel.startswith(project_root + "/"):
        rel = rel[len(project_root) + 1 :]
    return rel


def _resolve_related_files(active_rel: str, content: str, file_candidates: set[str]) -> list[str]:
    if not active_rel or not content:
        return []

    active_path = PurePosixPath(active_rel)
    base_dir = active_path.parent
    resolved: list[str] = []
    seen: set[str] = set()
    candidate_exts = [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".css", ".scss", ".sass", ".less"]

    def add_candidate(rel: str) -> None:
        rel = str(PurePosixPath(rel)).lstrip("/")
        if rel in file_candidates and rel not in seen:
            seen.add(rel)
            resolved.append(rel)

    for spec in _RELATIVE_IMPORT_RE.findall(content):
        if not spec.startswith('.'):
            continue
        joined = PurePosixPath(base_dir, spec)
        if joined.suffix:
            add_candidate(str(joined))
            continue
        joined_str = str(joined)
        for ext in candidate_exts:
            add_candidate(joined_str + ext)
            add_candidate(f"{joined_str}/index{ext}")

    base_name = str(active_path.with_suffix(''))
    for suffix in [".css", ".scss", ".sass", ".less", ".module.css", ".module.scss"]:
        add_candidate(f"{base_name}{suffix}")

    if active_path.parent != PurePosixPath('.'):
        for name in ("index.ts", "index.tsx", "index.js", "index.jsx"):
            add_candidate(str(active_path.parent / name))

    return resolved


def _merge_change_sets(*batches: list[dict[str, str]]) -> list[dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for batch in batches:
        for item in batch or []:
            if not isinstance(item, dict):
                continue
            rel = str(item.get("path") or "").strip()
            content = item.get("new_content")
            if not rel or not isinstance(content, str):
                continue
            if rel not in merged:
                order.append(rel)
            merged[rel] = {"path": rel, "new_content": content}
    return [merged[rel] for rel in order]


def _merge_action_sets(*batches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for batch in batches:
        for item in batch or []:
            if not isinstance(item, dict):
                continue
            key = repr(sorted(item.items()))
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


_MAX_MCP_TOOL_LOOPS = 2
_MAX_MCP_ACTIONS_PER_LOOP = 2


def _normalize_mcp_action(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    if str(item.get("type") or "").strip().lower() != "mcp":
        return None
    server = str(item.get("server") or "").strip()
    tool = str(item.get("tool") or item.get("name") or "").strip()
    arguments = item.get("arguments")
    if not isinstance(arguments, dict):
        arguments = item.get("args") if isinstance(item.get("args"), dict) else {}
    if not server or not tool:
        return None
    return {"type": "mcp", "server": server, "tool": tool, "arguments": arguments}


def _split_runtime_actions(actions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    mcp_actions: list[dict[str, Any]] = []
    other_actions: list[dict[str, Any]] = []
    for item in actions or []:
        normalized_mcp = _normalize_mcp_action(item)
        if normalized_mcp:
            mcp_actions.append(normalized_mcp)
        elif isinstance(item, dict):
            other_actions.append(item)
    return mcp_actions, other_actions


def _should_run_refinement(*, build_mode: str, instruction: str, active_rel: str, preview_url: str | None, attached_assets: list[str]) -> bool:
    refinement_mode = str(getattr(settings_mod.settings, "agent_refinement_mode", "auto") or "auto").strip().lower()
    if refinement_mode == "off":
        return False
    if refinement_mode == "always":
        return True

    friendly_mode = bool(getattr(settings_mod.settings, "friendly_free_tier_mode", True))
    if build_mode == "full-agent":
        return not friendly_mode
    if preview_url or attached_assets:
        return not friendly_mode

    hint = (instruction or "").lower()
    strong_refine_keywords = (
        "polish", "refine", "audit", "review", "production", "ux", "ui", "layout", "spacing",
        "responsive", "design", "landing", "dashboard", "improve", "better", "theme", "style", "visual", "state",
    )
    bugfix_keywords = ("fix", "bug", "error", "broken", "crash")

    if any(word in hint for word in strong_refine_keywords):
        return True
    if any(word in hint for word in bugfix_keywords) and active_rel.endswith((".tsx", ".ts", ".jsx", ".js", ".css", ".html")):
        return True
    return False


def _build_context_parts(ctx: PreparedAgentContext, req: Any) -> list[str]:
    parts: list[str] = [
        f"Build mode: {ctx.mode_profile.build_mode}",
        f"Agent persona: {ctx.mode_profile.persona_name} ({ctx.mode_profile.persona_label})",
        f"Project root: {ctx.project_root}",
        f"Active file: {ctx.active_rel or '(none)'}",
    ]
    if getattr(req, "editor_status", None):
        parts.append(f"Editor status: {str(req.editor_status).strip()}")
    if ctx.current_from_buffer:
        parts.append("Current content was supplied from the live editor buffer and may be newer than disk.")
    if getattr(req, "selection", None):
        parts.append("Selected code/text:\n" + str(req.selection)[:4000])
    if ctx.open_files:
        parts.append("Open files:\n- " + "\n- ".join(ctx.open_files[:8]))
    if getattr(req, "preview_url", None):
        parts.append(f"Live preview URL: {req.preview_url}")
        parts.append("When relevant, optimize for visible product quality in the running preview.")
    if PurePosixPath(ctx.active_rel or "").suffix in _FRONTEND_EXTS:
        parts.append("This request appears to touch a user-facing surface. Prioritize UI hierarchy, spacing, states, and polish.")
    return parts


def _build_asset_prompt(ctx: PreparedAgentContext) -> str:
    if not ctx.attached_assets:
        return ""

    asset_lines: list[str] = []
    for asset_rel in ctx.attached_assets:
        local_rel = _localize_project_rel(asset_rel, ctx.project_root)
        public_hint = None
        if "/public/" in f"/{local_rel}":
            public_hint = "/" + local_rel.split("public/", 1)[1]
        asset_lines.append(f"- {local_rel}" + (f" (public URL hint: {public_hint})" if public_hint else ""))

    return (
        "ATTACHED IMAGE ASSETS:\n"
        "The user uploaded these image assets into the project. Use them directly in the implementation when relevant instead of placeholder images.\n"
        + "\n".join(asset_lines)
        + "\n\n"
    )


def prepare_agent_context(req: Any, ws_root: Path) -> PreparedAgentContext:
    prep_warnings: list[dict[str, str]] = []
    project_root = (getattr(req, "project_root", ".") or ".").strip() or "."
    project_dir = safe_join(ws_root, project_root)
    mode_profile = get_agent_mode_profile(getattr(req, "build_mode", None) or settings_mod.settings.build_mode or "hybrid")

    active_rel = _localize_project_rel(getattr(req, "active_file", None), project_root)
    open_files = [
        rel
        for rel in (_localize_project_rel(path, project_root) for path in (getattr(req, "open_files", None) or []))
        if rel
    ]
    current_from_buffer = isinstance(getattr(req, "current_content", None), str) and bool(active_rel)
    project_name = project_dir.name if project_root != "." else ws_root.name
    intent = classify_agent_intent(
        getattr(req, "input", "") or "",
        build_mode=mode_profile.build_mode,
        active_file=active_rel,
        open_files=open_files,
    )

    try:
        current = req.current_content if current_from_buffer else (read_text(project_dir, active_rel) if active_rel else "")
        all_files = [
            str(p.relative_to(project_dir))
            for p in project_dir.rglob("*")
            if p.is_file() and "node_modules" not in str(p) and ".git" not in str(p)
        ]
        all_file_set = set(all_files)
        relevant_files: dict[str, str] = {}

        def add_relevant(rel_path: str, max_chars: int = 20_000, content_override: str | None = None) -> None:
            rel_local = _localize_project_rel(rel_path, project_root)
            if not rel_local:
                return
            try:
                if content_override is None:
                    p = project_dir / rel_local
                    if not p.exists() or not p.is_file():
                        return
                    txt = read_text(project_dir, rel_local)
                else:
                    txt = content_override
                relevant_files[rel_local] = txt[:max_chars]
            except Exception as exc:
                prep_warnings.append({"phase": "context", "message": f"File context '{rel_local}' nggak kebaca ({exc})."[:240]})
                return

        if active_rel:
            add_relevant(active_rel, content_override=current if current_from_buffer else None)
        for open_rel in open_files[:4]:
            if open_rel != active_rel and open_rel not in relevant_files:
                add_relevant(open_rel, max_chars=16_000)

        for key_file in [
            "package.json",
            "README.md",
            "PRD.md",
            "docs/PRD.md",
            "src/App.tsx",
            "src/main.tsx",
            "src/app.css",
            "index.html",
            "vite.config.ts",
        ]:
            if key_file not in relevant_files:
                add_relevant(key_file, max_chars=30_000 if key_file.endswith("PRD.md") else 20_000)

        for rel in _resolve_related_files(active_rel, current, all_file_set)[:10]:
            if rel not in relevant_files:
                add_relevant(rel, max_chars=20_000)

        hint = (getattr(req, "input", "") or "").lower()
        wants_style = any(k in hint for k in ["css", "style", "styles", "tema", "theme", "warna", "color", "font", "spacing", "layout", "ui", "ux"])
        if wants_style:
            styles_dir = project_dir / "src" / "styles"
            if styles_dir.exists():
                for p in styles_dir.glob("*.css"):
                    try:
                        rel = str(p.relative_to(project_dir))
                    except Exception as exc:
                        prep_warnings.append({"phase": "context", "message": f"Style context '{p.name}' nggak kebaca ({exc})."[:240]})
                        continue
                    if rel not in relevant_files:
                        add_relevant(rel, max_chars=30_000)

        if active_rel.endswith(".html"):
            try:
                html = current if active_rel in relevant_files else read_text(project_dir, active_rel)
                for match in re.findall(r'href=["\']([^"\']+\.css)["\']', html, flags=re.IGNORECASE):
                    css_path = match.lstrip("/")
                    if css_path and css_path not in relevant_files:
                        add_relevant(css_path)
            except Exception as exc:
                prep_warnings.append({"phase": "context", "message": f"Linked CSS discovery dari '{active_rel}' gagal ({exc})."[:240]})

        hybrid_seed_needed = mode_profile.build_mode == "full-agent" and should_seed_hybrid(project_dir)
        if hybrid_seed_needed:
            seed_files = build_hybrid_seed(project_root, project_name, getattr(req, "input", ""))
            for rel_path, content in seed_files.items():
                rel_local = _localize_project_rel(rel_path, project_root)
                if rel_local not in relevant_files:
                    relevant_files[rel_local] = content
                if rel_local not in all_file_set:
                    all_files.append(rel_local)
                    all_file_set.add(rel_local)

        attached_assets: list[str] = []
        for asset_path in getattr(req, "asset_paths", None) or []:
            asset_rel = str(asset_path or "").strip().lstrip("/")
            if not asset_rel:
                continue
            try:
                asset_abs = safe_join(ws_root, asset_rel)
            except Exception as exc:
                prep_warnings.append({"phase": "context", "message": f"Asset path '{asset_rel}' nggak valid ({exc})."[:240]})
                continue
            if not asset_abs.exists() or not asset_abs.is_file():
                continue
            attached_assets.append(asset_rel)
    except Exception as exc:
        prep_warnings.append({"phase": "context", "message": f"Project context fallback kepake, jadi context file disederhanain ({exc})."[:240]})
        current = req.current_content if isinstance(getattr(req, "current_content", None), str) else ""
        all_files = []
        relevant_files = {}
        hybrid_seed_needed = False
        attached_assets = []

    ctx_stub = PreparedAgentContext(
        ws_root=ws_root,
        project_root=project_root,
        project_dir=project_dir,
        mode_profile=mode_profile,
        project_name=project_name,
        active_rel=active_rel,
        open_files=open_files,
        current_from_buffer=current_from_buffer,
        current=current,
        all_files=all_files,
        relevant_files=relevant_files,
        hybrid_seed_needed=hybrid_seed_needed,
        attached_assets=attached_assets,
        extra_context="",
        asset_prompt="",
        memory_prompt="",
        skill_prompt="",
        mcp_prompt="",
        intent=intent,
        resolved_skill_ids=[],
        mcp_servers=[],
        trace_memory_hits=[],
        trace_skill_hits=[],
        trace_mcp_servers=[],
        trace_mcp_tools_used=[],
        trace_warnings=list(prep_warnings),
        suggested_mcp_actions=[],
    )
    extra_context = "\n\n".join([*_build_context_parts(ctx_stub, req), intent.prompt_block])
    asset_prompt = _build_asset_prompt(ctx_stub)
    ctx_stub.extra_context = extra_context
    ctx_stub.asset_prompt = asset_prompt
    return ctx_stub


def _classify_intent_node(state: AgentRuntimeState) -> AgentRuntimeState:
    ctx = state["context"]
    _emit(
        state,
        "status",
        {
            "phase": "intent",
            "message": (
                "Aku bedain dulu ini perintah build, audit baca-saja, percakapan biasa, atau campuran dua-duanya..."
            ),
        },
    )
    return {
        "context": ctx,
        "intent": {
            "kind": ctx.intent.kind,
            "confidence": ctx.intent.confidence,
            "rationale": ctx.intent.rationale,
            "should_write_files": ctx.intent.should_write_files,
            "should_run_tools": ctx.intent.should_run_tools,
            "wants_app_builder": ctx.intent.wants_app_builder,
        },
    }


def _hydrate_memory_node(state: AgentRuntimeState) -> AgentRuntimeState:
    ctx = state["context"]
    _emit(state, "status", {"phase": "memory", "message": "Ngambil short-term sama long-term memory dulu..."})
    memory_bundle = retrieve_agent_memory(
        ctx.ws_root,
        project_dir=ctx.project_dir,
        project_root=ctx.project_root,
        interaction_kind=ctx.intent.kind,
        query=state["input"],
        active_rel=ctx.active_rel,
        open_files=ctx.open_files,
    )
    ctx.memory_prompt = memory_bundle.prompt
    ctx.trace_memory_hits = [
        {
            "kind": hit.kind,
            "source": hit.source,
            "title": hit.title,
            "score": round(float(hit.score), 3),
            "text": hit.text[:240],
        }
        for hit in [*memory_bundle.short_term, *memory_bundle.long_term]
    ]
    for warning in memory_bundle.warnings:
        ctx.trace_warnings.append({"phase": "memory", "message": str(warning)[:240]})
    if ctx.memory_prompt:
        ctx.extra_context = f"{ctx.extra_context}\n\n{ctx.memory_prompt}".strip()
    return {"context": ctx}


def _resolve_skills_node(state: AgentRuntimeState) -> AgentRuntimeState:
    ctx = state["context"]
    _emit(state, "status", {"phase": "skills", "message": "Nyocokin skill yang relevan buat task ini..."})
    skill_warnings: list[str] = []
    skills = resolve_agent_skills(
        ctx.ws_root,
        project_dir=ctx.project_dir,
        query=state["input"],
        build_mode=ctx.mode_profile.build_mode,
        active_rel=ctx.active_rel,
        preview_url=state.get("request_preview_url"),
        warnings=skill_warnings,
    )
    ctx.resolved_skill_ids = [skill.skill_id for skill in skills]
    ctx.trace_skill_hits = [
        {
            "skill_id": skill.skill_id,
            "title": skill.title,
            "source": skill.source,
        }
        for skill in skills
    ]
    for warning in skill_warnings:
        ctx.trace_warnings.append({"phase": "skills", "message": str(warning)[:240]})
    ctx.skill_prompt = format_skill_prompt(skills)
    if ctx.skill_prompt:
        ctx.extra_context = f"{ctx.extra_context}\n\n{ctx.skill_prompt}".strip()
    return {"context": ctx}


def _inspect_mcp_node(state: AgentRuntimeState) -> AgentRuntimeState:
    ctx = state["context"]
    _emit(state, "status", {"phase": "mcp", "message": "Cek capability boundary dari MCP registry..."})
    mcp_warnings: list[str] = []
    servers = discover_mcp_servers(ctx.ws_root, ctx.project_dir, warnings=mcp_warnings)
    ctx.mcp_servers = [server.name for server in servers]
    ctx.trace_mcp_servers = [
        {
            "name": server.name,
            "transport": server.transport,
            "target": server.target,
            "tools": list(server.tools or []),
            "source": server.source,
        }
        for server in servers
    ]
    tool_catalog = list_mcp_tools(ctx.ws_root, ctx.project_dir, warnings=mcp_warnings) if servers else {}
    ctx.suggested_mcp_actions = suggest_mcp_actions(state["input"], tool_catalog=tool_catalog, limit=2) if tool_catalog else []
    for warning in mcp_warnings:
        ctx.trace_warnings.append({"phase": "mcp", "message": str(warning)[:240]})
    if ctx.suggested_mcp_actions:
        ctx.trace_warnings.append({"phase": "mcp", "message": f"Auto MCP hints siap: {len(ctx.suggested_mcp_actions)} tool read-only bisa dipakai buat audit/refine."})
    ctx.mcp_prompt = format_mcp_prompt(servers, tool_catalog=tool_catalog)
    if ctx.mcp_prompt:
        ctx.extra_context = f"{ctx.extra_context}\n\n{ctx.mcp_prompt}".strip()
    return {"context": ctx}


def _draft_node(state: AgentRuntimeState) -> AgentRuntimeState:
    ctx = state["context"]
    _emit(state, "status", {"phase": "context_ready", "message": "Konteks siap, agent mulai mikir..."})
    is_tool_follow_up = int(state.get("tool_iterations") or 0) > 0
    _emit(
        state,
        "status",
        {
            "phase": "drafting",
            "message": "Lagi nulis draft perubahan pertama..." if not is_tool_follow_up else "Hasil tool udah masuk, sekarang agent nyusun solusi finalnya...",
        },
    )

    follow_up_prefix = ""
    if is_tool_follow_up:
        follow_up_prefix = (
            "MCP FOLLOW-UP MODE:\n"
            "- Tool results are already included in context.\n"
            "- Prefer producing the final implementation now.\n"
            "- Ask for another MCP tool only if the current tool result is still insufficient.\n\n"
        )

    intent_prefix = ctx.intent.prompt_block + "\n"
    base_instruction = ctx.mode_profile.instruction_prefix + intent_prefix + ctx.asset_prompt + follow_up_prefix + state["input"]
    try:
        sug = suggest(
            instruction=base_instruction,
            path=ctx.active_rel or "(no-active-file)",
            content=ctx.current,
            file_tree=ctx.all_files,
            relevant_files=ctx.relevant_files,
            extra_context=ctx.extra_context,
            workspace_root=ctx.project_dir,
            system=ctx.mode_profile.system_prompt,
        )
        spoken = sug.spoken
        log = sug.log
        changes = list(sug.changes or [])
        actions = list(sug.actions or [])
    except RuntimeError as exc:
        if not (ctx.is_full_agent and ctx.hybrid_seed_needed and ctx.intent.should_write_files):
            raise
        ctx.trace_warnings.append({"phase": "draft", "message": f"LLM draft gagal, jadi fallback ke seed-only baseline ({exc})."[:240]})
        spoken = ""
        log = f"provider={settings_mod.settings.llm_provider} full-agent-mode=seed-only"
        changes = []
        actions = []

    _emit(state, "delta", {"message": "Draft pertama jadi, lagi rapihin hasilnya...", "changes_so_far": len(changes)})
    return {
        "spoken": spoken,
        "log": log,
        "changes": changes,
        "actions": actions,
        "passes": 1,
        "refine_skipped": False,
    }


def _route_after_draft(state: AgentRuntimeState) -> str:
    ctx = state["context"]
    if not ctx.intent.should_write_files:
        return "finalize"

    mcp_actions, _other_actions = _split_runtime_actions(list(state.get("actions") or []))
    if ctx.intent.should_run_tools and int(state.get("tool_iterations") or 0) < _MAX_MCP_TOOL_LOOPS:
        if mcp_actions or (not state.get("actions") and ctx.suggested_mcp_actions):
            return "tooling"

    changes = state.get("changes") or []
    if not changes:
        return "finalize"
    if _should_run_refinement(
        build_mode=ctx.mode_profile.build_mode,
        instruction=state["input"],
        active_rel=ctx.active_rel,
        preview_url=state.get("request_preview_url"),
        attached_assets=ctx.attached_assets,
    ):
        return "refine"
    return "finalize"


def _execute_mcp_node(state: AgentRuntimeState) -> AgentRuntimeState:
    ctx = state["context"]
    raw_actions = list(state.get("actions") or [])
    mcp_actions, _other_actions = _split_runtime_actions(raw_actions)
    if not mcp_actions and int(state.get("tool_iterations") or 0) == 0:
        mcp_actions = list(ctx.suggested_mcp_actions or [])
    if not mcp_actions:
        return {"actions": raw_actions}

    _emit(state, "status", {"phase": "tooling", "message": "Aku jalanin tool MCP dulu biar context-nya makin tajam..."})
    if not raw_actions and mcp_actions:
        _emit(state, "delta", {"message": "Aku nemu tool read-only yang cocok, jadi aku pakai dulu buat audit/refine awal."})
    executed_results = []
    for action in mcp_actions[:_MAX_MCP_ACTIONS_PER_LOOP]:
        server = str(action.get("server") or "").strip()
        tool = str(action.get("tool") or "").strip()
        arguments = action.get("arguments") if isinstance(action.get("arguments"), dict) else {}
        _emit(state, "delta", {"message": f"MCP {server}.{tool} lagi dipanggil..."})
        result = execute_mcp_tool(
            ctx.ws_root,
            ctx.project_dir,
            server_name=server,
            tool_name=tool,
            arguments=arguments,
        )
        executed_results.append(result)
        ctx.trace_mcp_tools_used.append(
            {
                "server": result.server,
                "tool": result.tool,
                "ok": result.ok,
                "duration_ms": result.duration_ms,
                "error": result.error,
                "arguments": result.arguments,
                "text": result.text[:240],
            }
        )
        if not result.ok:
            ctx.trace_warnings.append({"phase": "mcp", "message": f"MCP {result.server}.{result.tool} gagal ({result.error or 'unknown error'})."[:240]})
        _emit(
            state,
            "delta",
            {
                "message": (
                    f"MCP {server}.{tool} selesai, hasilnya masuk ke context."
                    if result.ok
                    else f"MCP {server}.{tool} gagal, tapi error-nya tetap kusimpen buat reasoning berikutnya."
                )
            },
        )

    results_prompt = format_mcp_results_prompt(executed_results)
    if results_prompt:
        ctx.extra_context = f"{ctx.extra_context}\n\n{results_prompt}".strip()

    return {
        "context": ctx,
        "tool_iterations": int(state.get("tool_iterations") or 0) + 1,
        "mcp_call_count": int(state.get("mcp_call_count") or 0) + len(executed_results),
        "changes": [],
        "actions": [],
    }


def _refine_node(state: AgentRuntimeState) -> AgentRuntimeState:
    ctx = state["context"]
    draft_relevant = dict(ctx.relevant_files)
    for item in state.get("changes") or []:
        rel = _localize_project_rel(item.get("path"), ctx.project_root)
        content = item.get("new_content")
        if rel and isinstance(content, str):
            draft_relevant[rel] = content[:30_000]

    refinement_instruction = (
        ctx.mode_profile.instruction_prefix
        + ctx.intent.prompt_block
        + "\n"
        + ctx.asset_prompt
        + ctx.mode_profile.refinement_prefix
        + state["input"]
    )
    _emit(state, "status", {"phase": "refining", "message": "Lagi cek ulang biar hasilnya lebih rapi..."})
    try:
        refined = suggest(
            instruction=refinement_instruction,
            path=ctx.active_rel or "(no-active-file)",
            content=draft_relevant.get(ctx.active_rel, ctx.current),
            file_tree=ctx.all_files,
            relevant_files=draft_relevant,
            extra_context=ctx.extra_context + "\n\nThis is a second-pass review over a draft solution.",
            workspace_root=ctx.project_dir,
            system=ctx.mode_profile.system_prompt,
        )
        return {
            "spoken": refined.spoken or state.get("spoken") or "",
            "changes": _merge_change_sets(state.get("changes") or [], list(refined.changes or [])),
            "actions": _merge_action_sets(state.get("actions") or [], list(refined.actions or [])),
            "passes": 2,
        }
    except Exception:
        ctx.trace_warnings.append({"phase": "refine", "message": "Refinement pass gagal, jadi draft pertama dipakai apa adanya."})
        return {"refine_skipped": True}


def _finalize_node(state: AgentRuntimeState) -> AgentRuntimeState:
    ctx = state["context"]
    normalized_changes = list(state.get("changes") or [])
    normalized_actions = list(state.get("actions") or [])
    spoken = str(state.get("spoken") or "")
    log = str(state.get("log") or "")
    intent_payload = dict(state.get("intent") or {
        "kind": ctx.intent.kind,
        "confidence": ctx.intent.confidence,
        "rationale": ctx.intent.rationale,
        "should_write_files": ctx.intent.should_write_files,
        "should_run_tools": ctx.intent.should_run_tools,
        "wants_app_builder": ctx.intent.wants_app_builder,
    })
    if not ctx.intent.should_write_files:
        normalized_changes = []
        normalized_actions = []
    persona_tag = f"persona={ctx.mode_profile.persona_name.lower()}"
    if persona_tag not in log:
        log = f"{log} {persona_tag}".strip()
    log = f"{log} intent={ctx.intent.kind}".strip()
    if ctx.resolved_skill_ids:
        log = f"{log} skills={','.join(ctx.resolved_skill_ids)}".strip()
    if ctx.mcp_servers:
        log = f"{log} mcp={','.join(ctx.mcp_servers)}".strip()
    if ctx.memory_prompt:
        log = f"{log} memory=on".strip()
    if int(state.get("mcp_call_count") or 0) > 0:
        log = f"{log} mcp_calls={int(state.get('mcp_call_count') or 0)}".strip()

    passes = int(state.get("passes") or 1)
    if passes >= 2:
        if "passes=2" not in log:
            log = f"{log} passes=2".strip()
    else:
        pass_note = "passes=1 refine=skipped" if state.get("refine_skipped") else "passes=1"
        if "passes=1" not in log:
            log = f"{log} {pass_note}".strip()

    if ctx.project_root != ".":
        scoped_changes: list[dict[str, str]] = []
        for item in normalized_changes:
            rel = str(item.get("path") or "").strip().lstrip("/")
            content = item.get("new_content")
            if not rel or not isinstance(content, str):
                continue
            rel = _localize_project_rel(rel, ctx.project_root)
            if not rel:
                continue
            scoped_changes.append({"path": f"{ctx.project_root}/{rel}", "new_content": content})
        normalized_changes = scoped_changes

    if ctx.is_full_agent and ctx.intent.should_write_files:
        normalized_changes = merge_hybrid_seed(
            project_root=ctx.project_root,
            project_name=ctx.project_name,
            instruction=state["input"],
            changes=normalized_changes,
            should_seed=ctx.hybrid_seed_needed,
        )
        if ctx.hybrid_seed_needed and "full-agent-mode" not in log:
            log = f"{log} full-agent-mode=seeded".strip()

    trace = {
        "passes": passes,
        "memory_hits": list(ctx.trace_memory_hits),
        "skills": list(ctx.trace_skill_hits),
        "mcp_servers": list(ctx.trace_mcp_servers),
        "mcp_tools_used": list(ctx.trace_mcp_tools_used),
        "warnings": list(ctx.trace_warnings),
    }

    return {
        "spoken": spoken,
        "log": log,
        "changes": normalized_changes,
        "actions": normalized_actions,
        "intent": intent_payload,
        "trace": trace,
    }


_AGENT_GRAPH_BUILDER = StateGraph(AgentRuntimeState)
_AGENT_GRAPH_BUILDER.add_node("intent", _classify_intent_node)
_AGENT_GRAPH_BUILDER.add_node("memory", _hydrate_memory_node)
_AGENT_GRAPH_BUILDER.add_node("skills", _resolve_skills_node)
_AGENT_GRAPH_BUILDER.add_node("mcp", _inspect_mcp_node)
_AGENT_GRAPH_BUILDER.add_node("draft", _draft_node)
_AGENT_GRAPH_BUILDER.add_node("tooling", _execute_mcp_node)
_AGENT_GRAPH_BUILDER.add_node("refine", _refine_node)
_AGENT_GRAPH_BUILDER.add_node("finalize", _finalize_node)
_AGENT_GRAPH_BUILDER.set_entry_point("intent")
_AGENT_GRAPH_BUILDER.add_edge("intent", "memory")
_AGENT_GRAPH_BUILDER.add_edge("memory", "skills")
_AGENT_GRAPH_BUILDER.add_edge("skills", "mcp")
_AGENT_GRAPH_BUILDER.add_edge("mcp", "draft")
_AGENT_GRAPH_BUILDER.add_conditional_edges("draft", _route_after_draft, {"tooling": "tooling", "refine": "refine", "finalize": "finalize"})
_AGENT_GRAPH_BUILDER.add_edge("tooling", "draft")
_AGENT_GRAPH_BUILDER.add_edge("refine", "finalize")
_AGENT_GRAPH_BUILDER.add_edge("finalize", END)
_AGENT_GRAPH = _AGENT_GRAPH_BUILDER.compile()


def run_agent_pipeline(req: Any, *, ws_root: Path, emit: EventEmitter | None = None) -> AgentRuntimeResult:
    ctx = prepare_agent_context(req, ws_root)
    result = _AGENT_GRAPH.invoke(
        {
            "input": str(getattr(req, "input", "") or ""),
            "context": ctx,
            "request_preview_url": getattr(req, "preview_url", None),
            "tool_iterations": 0,
            "mcp_call_count": 0,
            "emit": emit,
        }
    )
    final_result = {
        "spoken": str(result.get("spoken") or ""),
        "log": str(result.get("log") or ""),
        "changes": list(result.get("changes") or []),
        "actions": list(result.get("actions") or []),
        "intent": dict(result.get("intent") or {
            "kind": ctx.intent.kind,
            "confidence": ctx.intent.confidence,
            "rationale": ctx.intent.rationale,
            "should_write_files": ctx.intent.should_write_files,
            "should_run_tools": ctx.intent.should_run_tools,
            "wants_app_builder": ctx.intent.wants_app_builder,
        }),
        "trace": dict(result.get("trace") or {
            "passes": int(result.get("passes") or 1),
            "memory_hits": list(ctx.trace_memory_hits),
            "skills": list(ctx.trace_skill_hits),
            "mcp_servers": list(ctx.trace_mcp_servers),
            "mcp_tools_used": list(ctx.trace_mcp_tools_used),
            "warnings": list(ctx.trace_warnings),
        }),
    }
    try:
        remember_agent_run(
            ws_root,
            project_root=ctx.project_root,
            build_mode=ctx.mode_profile.build_mode,
            interaction_kind=ctx.intent.kind,
            user_input=str(getattr(req, "input", "") or ""),
            spoken=final_result["spoken"],
            changes=final_result["changes"],
            actions=final_result["actions"],
        )
    except Exception as exc:
        trace = final_result.get("trace")
        if isinstance(trace, dict):
            warnings = trace.get("warnings")
            if isinstance(warnings, list):
                warnings.append({"phase": "memory-write", "message": f"Agent run nggak bisa disimpan ke short-term memory ({exc})."[:240]})
    return final_result
