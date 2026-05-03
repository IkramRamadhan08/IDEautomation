from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import settings as settings_mod
from .oauth_runtime import (
    ANTHROPIC_PROVIDER,
    OPENAI_PROVIDER,
    OPENROUTER_PROVIDER,
    anthropic_generate_json,
    openai_generate_json,
    openrouter_generate_json,
    require_provider_connected,
)

_REF_CACHE: dict[str, tuple[float, str]] = {}
_SCAFFOLD_CACHE: dict[str, tuple[float, "ScaffoldResult"]] = {}
_PRD_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_LLM_LAST_CALL_TS: float = 0.0
_MIN_LLM_GAP_SECONDS: float = 1.2


def _throttle_llm_calls(min_gap_seconds: float) -> None:
    global _LLM_LAST_CALL_TS
    now = time.time()
    gap = now - _LLM_LAST_CALL_TS
    if gap < min_gap_seconds:
        time.sleep(min_gap_seconds - gap)
    _LLM_LAST_CALL_TS = time.time()


@dataclass
class AgentSuggestion:
    spoken: str
    log: str
    changes: list[dict[str, str]]
    actions: list[dict[str, Any]]


@dataclass
class ScaffoldFile:
    path: str
    content: str


@dataclass
class ScaffoldResult:
    spoken: str
    log: str
    project_root: str
    ops: list[ScaffoldFile]
    actions: list[dict[str, Any]]


DEFAULT_SYSTEM_PATCH = """You are a senior product engineer working inside a local IDE.
You are strong at both implementation and product taste.

Your job:
- understand the user's real intent,
- make the project better in a way that feels intentional and production-ready,
- stay tightly scoped in hybrid/IDE mode,
- go broader only when the supplied mode/context explicitly allows it.

Return ONLY valid JSON with this exact shape:
{
  "spoken": "short explanation",
  "changes": [
    {"path": "relative/path", "new_content": "full content"}
  ],
  "actions": [
    {"type": "shell", "command": "npm install ..."}
  ]
}

Rules:
- changes must contain FULL file contents, not patches or snippets.
- Use actions only for terminal steps that are truly needed, such as installs, generators, build/lint commands, or other project commands.
- Respect the provided mode/context block. If it says hybrid/IDE mode, keep the scope surgical and preserve the existing architecture.
- If current content is marked as coming from the editor buffer, trust it over on-disk file contents.
- When the request is UI/UX/product polish, improve hierarchy, spacing, consistency, copy clarity, visual rhythm, responsiveness, and accessible states.
- When changing product flows, think about happy path plus loading, empty, success, and error states where relevant.
- Reuse the existing stack and patterns unless there is a clear reason not to.
- Avoid placeholder work, toy UIs, or generic scaffolding unless the user explicitly wants that.
- Before finalizing, self-review for broken imports, missing styles, mismatched names, and incomplete supporting edits.
- Output ONLY JSON, with no markdown fences or extra commentary.
"""

SYSTEM_SCAFFOLD = """You are an expert product engineer and front-end architect.
Create a brand new React + Vite + TypeScript website that feels production-ready and pleasantly “overbuilt”.

Return ONLY valid JSON with this shape:
{
  "spoken": "short explanation",
  "project_root": "relative-folder-name",
  "files": [
    {"path": "relative/path/from/project_root", "content": "full file content"}
  ]
}

Rules:
- Include package.json, index.html, src/main.tsx, src/App.tsx.
- Implement multi-page navigation (React Router) with at least 4 pages that match the goal (e.g., Home, Features, Pricing, Dashboard).
- Create a small reusable component system (Layout, Header/Nav, Footer, Button, Card, FormField, Modal or Drawer).
- Add responsive styling using CSS variables design tokens and include a light/dark theme toggle.
- Include loading/error/empty states and basic accessibility (semantic HTML, aria labels where needed, focus states).
- Keep dependencies reasonable; adding react-router-dom is OK.
- Keep the output bounded: aim for <= 30 files and avoid huge files.
- Output only JSON.
"""

SYSTEM_PRD = """You are a senior product engineer (coder-pro vibe).
Write a practical, implementation-ready Product Requirements Document (PRD) as Markdown.

Return ONLY valid JSON:
{
  "spoken": "short explanation",
  "prd_markdown": "markdown content"
}

Rules:
- IMPORTANT: Write the PRD in the SAME LANGUAGE as the user's goal/instruction (Indonesian stays Indonesian; English stays English). Match their tone and terminology.
- Be detailed but not fluffy. Prefer clear headings, bullet lists, and concrete acceptance criteria.
- Include: Vision, Target Users, Jobs-to-be-done, User Stories, MVP Scope, Later Scope, Information Architecture (routes/pages), Key UI Components, Design Tokens, Non-functional requirements (perf, a11y, SEO), Analytics/Events, Acceptance Criteria, Risks/Assumptions, and a step-by-step Build Plan.
- Make it specific to the provided product name/goal; make reasonable assumptions if details are missing.
- Output only JSON.
"""


def _slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "app"


def _extract_json_object(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise RuntimeError("LLM returned empty output")

    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    decoder = json.JSONDecoder()
    for idx, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _end = decoder.raw_decode(text[idx:])
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    raise RuntimeError(f"LLM did not return valid JSON: {text[:400]}")


def _safe_fetch_reference(ref_url: str) -> str:
    now = time.time()
    cached = _REF_CACHE.get(ref_url)
    if cached and (now - cached[0]) < 1800:
        return cached[1]

    parsed = urllib.parse.urlparse(ref_url)
    if parsed.scheme not in {"http", "https"}:
        raise RuntimeError("Reference URL must be http(s)")

    req = urllib.request.Request(
        ref_url,
        headers={
            "User-Agent": "VoiceIDE/0.1 (+local)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310 - user requested fetch
        raw = resp.read(250_000)
    html = raw.decode("utf-8", errors="ignore")
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"<style\b[^>]*>.*?</style>", "", html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()[:6000]
    _REF_CACHE[ref_url] = (now, text)
    return text


def _provider_and_model() -> tuple[str, str]:
    s = settings_mod.settings
    provider = (s.llm_provider or "").strip().lower()
    if not provider:
        raise RuntimeError("No provider selected yet. Open Settings, choose a provider, then save credentials first.")
    require_provider_connected(provider)
    if provider == OPENAI_PROVIDER:
        return provider, s.openai_model
    if provider == ANTHROPIC_PROVIDER:
        return provider, s.anthropic_model
    if provider == OPENROUTER_PROVIDER:
        return provider, s.openrouter_model
    raise RuntimeError(f"Unsupported provider: {provider}")


def _generate_json(*, system: str, user: str) -> tuple[str, str, dict[str, Any]]:
    provider, model = _provider_and_model()
    _throttle_llm_calls(_MIN_LLM_GAP_SECONDS)

    if provider == OPENAI_PROVIDER:
        raw = openai_generate_json(model=model, system=system, user=user)
        text = str(raw.get("text") or "")
        if not text.strip():
            err = str(raw.get("error_message") or "").strip()
            if err:
                raise RuntimeError(err)
            raise RuntimeError("OpenAI returned an empty response")
        return provider, model, _extract_json_object(text)

    if provider == ANTHROPIC_PROVIDER:
        raw = anthropic_generate_json(model=model, system=system, user=user)
        text = str(raw.get("text") or "")
        if not text.strip():
            err = str(raw.get("error_message") or "").strip()
            if err:
                raise RuntimeError(err)
            raise RuntimeError("Anthropic returned an empty response")
        return provider, model, _extract_json_object(text)

    if provider == OPENROUTER_PROVIDER:
        raw = openrouter_generate_json(model=model, system=system, user=user)
        text = str(raw.get("text") or "")
        if not text.strip():
            err = str(raw.get("error_message") or "").strip()
            if err:
                raise RuntimeError(err)
            raise RuntimeError("OpenRouter returned an empty response")
        return provider, model, _extract_json_object(text)

    raise RuntimeError(f"Unsupported provider: {provider}")


def suggest(
    *,
    instruction: str,
    path: str,
    content: str,
    file_tree: list[str] | None = None,
    relevant_files: dict[str, str] | None = None,
    extra_context: str | None = None,
    workspace_root: str | Path | None = None,
    system: str | None = None,
) -> AgentSuggestion:
    file_tree = file_tree or []
    relevant_files = relevant_files or {}

    relevant_blob = "\n\n".join(
        f"FILE: {rel}\n{txt}" for rel, txt in relevant_files.items()
    )
    context_block = f"Context:\n{extra_context}\n\n" if extra_context else ""
    user = (
        f"Active file: {path}\n\n"
        f"Instruction:\n{instruction}\n\n"
        f"{context_block}"
        f"Current content:\n{content}\n\n"
        f"File tree:\n" + "\n".join(file_tree[:600]) + "\n\n"
        f"Relevant files:\n{relevant_blob}"
    )

    provider, model, data = _generate_json(
        system=system or DEFAULT_SYSTEM_PATCH,
        user=user,
    )
    changes = data.get("changes") or []
    if not isinstance(changes, list):
        changes = []

    out_changes: list[dict[str, str]] = []
    for item in changes:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("path") or "").strip()
        new_content = item.get("new_content")
        if rel and isinstance(new_content, str):
            out_changes.append({"path": rel, "new_content": new_content})
    
    actions = data.get("actions") or []
    if not isinstance(actions, list):
        actions = []

    spoken = str(data.get("spoken") or ("I prepared the requested changes." if out_changes else "I reviewed the request but did not propose any file edits."))
    log = f"provider={provider} model={model} files={len(out_changes)} actions={len(actions)}"
    return AgentSuggestion(spoken=spoken, log=log, changes=out_changes, actions=actions)


def scaffold_webapp(*, name: str, goal: str, ref_url: str | None = None) -> ScaffoldResult:
    cache_key = json.dumps({"name": name, "goal": goal, "ref_url": ref_url}, sort_keys=True)
    cached = _SCAFFOLD_CACHE.get(cache_key)
    if cached and (time.time() - cached[0]) < 1800:
        return cached[1]

    ref_text = _safe_fetch_reference(ref_url) if ref_url else ""
    user = f"App name: {name}\n\nGoal: {goal}"
    if ref_text:
        user += f"\n\nReference snapshot:\n{ref_text}"

    provider, model, data = _generate_json(system=SYSTEM_SCAFFOLD, user=user)
    project_root = _slugify(str(data.get("project_root") or name or "app"))
    files = data.get("files") or []
    if not isinstance(files, list) or not files:
        raise RuntimeError("LLM returned no scaffold files")

    ops: list[ScaffoldFile] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("path") or "").strip().lstrip("/")
        content = item.get("content")
        if rel and isinstance(content, str):
            ops.append(ScaffoldFile(path=f"{project_root}/{rel}", content=content))
    if not ops:
        raise RuntimeError("LLM returned no valid scaffold files")

    actions = data.get("actions") or []
    if not isinstance(actions, list):
        actions = []

    result = ScaffoldResult(
        spoken=str(data.get("spoken") or f"I created a new app in {project_root}."),
        log=f"provider={provider} model={model} files={len(ops)} actions={len(actions)}",
        project_root=project_root,
        ops=ops,
        actions=actions,
    )
    _SCAFFOLD_CACHE[cache_key] = (time.time(), result)
    return result


def generate_prd(*, name: str, goal: str, ref_url: str | None = None) -> dict[str, str]:
    cache_key = json.dumps({"name": name, "goal": goal, "ref_url": ref_url}, sort_keys=True)
    cached = _PRD_CACHE.get(cache_key)
    if cached and (time.time() - cached[0]) < 1800:
        return cached[1]

    ref_text = _safe_fetch_reference(ref_url) if ref_url else ""
    user = f"Product name: {name}\n\nGoal: {goal}"
    if ref_text:
        user += f"\n\nReference snapshot:\n{ref_text}"

    provider, model, data = _generate_json(system=SYSTEM_PRD, user=user)
    prd_markdown = str(data.get("prd_markdown") or "").strip()
    if not prd_markdown:
        raise RuntimeError("LLM returned empty PRD")

    result = {
        "spoken": str(data.get("spoken") or "I drafted the PRD."),
        "prd_markdown": prd_markdown,
        "log": f"provider={provider} model={model}",
    }
    _PRD_CACHE[cache_key] = (time.time(), result)
    return result
