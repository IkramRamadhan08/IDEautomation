from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import settings as settings_mod
from .preferences import UserPreferencesRecord, get_user_preferences
from .oauth_runtime import (
    CURRENT_PROFILE_ID,
    NINE_ROUTER_PROVIDER,
    nine_router_generate_json,
    require_provider_connected,
    get_provider_cooldown_remaining,
    auth_snapshot,
)
from .agent_router import normalize_smart_route

_REF_CACHE: dict[str, tuple[float, str]] = {}
_SCAFFOLD_CACHE: dict[str, tuple[float, "ScaffoldResult"]] = {}
_PRD_CACHE: dict[str, tuple[float, dict[str, str]]] = {}
_LLM_LAST_CALL_TS: float = 0.0
_LLM_RATE_LOCK = threading.Lock()
_LLM_CALL_HISTORY: dict[str, deque[float]] = {}
_DEFAULT_MIN_LLM_GAP_SECONDS: float = 4.0
_DEFAULT_FRIENDLY_RPM: int = 8
_DEFAULT_STANDARD_RPM: int = 15
_DEFAULT_FRIENDLY_CONTEXT_CHARS: int = 48_000
_DEFAULT_STANDARD_CONTEXT_CHARS: int = 140_000
_SUPPORTED_AGENT_PROVIDERS = {
    NINE_ROUTER_PROVIDER,
}

def _normalize_smart_route(model: str) -> str:
    return normalize_smart_route(model)


def _effective_requests_per_minute(provider: str) -> int:
    settings = settings_mod.settings
    per_provider = None
    default_rpm = _DEFAULT_FRIENDLY_RPM if bool(getattr(settings, "friendly_free_tier_mode", True)) else _DEFAULT_STANDARD_RPM
    candidates = [per_provider, getattr(settings, "agent_requests_per_minute", default_rpm), default_rpm]
    for value in candidates:
        try:
            rpm = int(value)
        except Exception:
            continue
        return max(0, rpm)
    return default_rpm


def _throttle_llm_calls(provider: str, min_gap_seconds: float) -> None:
    global _LLM_LAST_CALL_TS
    rpm = _effective_requests_per_minute(provider)
    bucket = provider or "default"

    while True:
        with _LLM_RATE_LOCK:
            now = time.time()
            history = _LLM_CALL_HISTORY.setdefault(bucket, deque())
            while history and now - history[0] >= 60.0:
                history.popleft()

            wait_for_gap = max(0.0, min_gap_seconds - (now - _LLM_LAST_CALL_TS))
            wait_for_rpm = 0.0
            if rpm > 0 and len(history) >= rpm:
                wait_for_rpm = max(0.0, 60.0 - (now - history[0]))
            wait_for_provider_cooldown = get_provider_cooldown_remaining(provider)
            wait_seconds = max(wait_for_gap, wait_for_rpm, wait_for_provider_cooldown)
            if wait_seconds <= 0:
                reserve_ts = time.time()
                history.append(reserve_ts)
                _LLM_LAST_CALL_TS = reserve_ts
                return
        time.sleep(wait_seconds)


def _effective_min_gap_seconds() -> float:
    try:
        value = float(getattr(settings_mod.settings, "agent_min_gap_seconds", _DEFAULT_MIN_LLM_GAP_SECONDS) or _DEFAULT_MIN_LLM_GAP_SECONDS)
    except Exception:
        value = _DEFAULT_MIN_LLM_GAP_SECONDS
    return max(0.0, value)


def _friendly_free_tier_mode() -> bool:
    return bool(getattr(settings_mod.settings, "friendly_free_tier_mode", True))


def _effective_context_char_budget() -> int:
    default_budget = _DEFAULT_FRIENDLY_CONTEXT_CHARS if _friendly_free_tier_mode() else _DEFAULT_STANDARD_CONTEXT_CHARS
    try:
        value = int(getattr(settings_mod.settings, "agent_context_char_budget", default_budget) or default_budget)
    except Exception:
        value = default_budget
    return max(12_000, min(value, 260_000))


def _rough_token_estimate(text: str) -> int:
    return max(1, int(len(text or "") / 4))


def _bounded_relevant_files(relevant_files: dict[str, str], *, active_path: str, budget_chars: int) -> tuple[dict[str, str], int]:
    budget = max(4_000, budget_chars)
    used = 0
    skipped = 0
    out: dict[str, str] = {}
    for rel, txt in relevant_files.items():
        if rel == active_path:
            skipped += 1
            continue
        header_cost = len(rel) + 16
        remaining = budget - used - header_cost
        if remaining <= 0:
            skipped += 1
            continue
        clean = str(txt or "")
        clipped = clean[:remaining]
        out[rel] = clipped
        used += header_cost + len(clipped)
        if len(clipped) < len(clean):
            skipped += 1
    return out, skipped


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


DEFAULT_SYSTEM_PATCH = """You are a senior product engineer working inside a hosted browser app builder for non-coders.
You are strong at both implementation and product taste.

Your job:
- understand the user's real intent,
- make the project better in a way that feels intentional and production-ready,
- stay tightly scoped in hybrid/copilot mode,
- go broader only when the supplied mode/context explicitly allows it.

Return ONLY valid JSON with this exact shape:
{
  "spoken": "short explanation",
  "changes": [
    {"path": "relative/path", "new_content": "full content"}
  ],
  "patches": [
    {"path": "relative/path", "unified_diff": "--- a/relative/path\n+++ b/relative/path\n@@ ..."}
  ],
  "actions": [
    {"type": "shell", "command": "npm install ..."}
  ]
}

Rules:
- Prefer `patches` for edits to existing files when the current file content was provided. Use `changes` with FULL file contents for new files, generated files, or when a patch would be ambiguous.
- `patches` must be standard unified diff hunks and must apply cleanly to the provided file content. Do not use snippets.
- The runtime target is Vercel serverless + Supabase. Direct file changes are durable, and shell actions are available when project tooling, installs, validation, or inspection are useful.
- The user accepts terminal risk. Use shell actions when they materially help the build, while keeping commands project-scoped unless the user asks otherwise.
- Respect the provided mode/context block. If it says hybrid/IDE mode, keep the scope surgical and preserve the existing architecture.
- If current content is marked as coming from the editor buffer, trust it over on-disk file contents.
- When the request is UI/UX/product polish, improve hierarchy, spacing, consistency, copy clarity, visual rhythm, responsiveness, and accessible states.
- When changing product flows, think about happy path plus loading, empty, success, and error states where relevant.
- Reuse the existing stack and patterns unless there is a clear reason not to.
- Avoid placeholder work, toy UIs, or generic scaffolding unless the user explicitly wants that.
- Before finalizing, self-review for broken imports, missing styles, mismatched names, and incomplete supporting edits.
- Behave like a pragmatic coding agent in a shared workspace: inspect first, preserve user work, keep unrelated files untouched, validate when useful, and finish the task instead of stopping at advice.
- Keep chat read-only. Only edit files when the user clearly asks you to build, fix, update, or run project work.
- `spoken` is for the orb conversation. Operational activity belongs in `actions` and file `changes`.
- If you need to call tools or run project actions, use `spoken` as a short progress update before those actions. Say what you are checking/running and why, without pretending the result is known yet.
- When tool results or command output are already in the provided context, use `spoken` to state the concrete finding and the next step. This should feel like a coding agent narrating evidence-based progress, not a final-only report.
- Output ONLY JSON, with no markdown fences or extra commentary.
"""


class PatchApplyError(RuntimeError):
    pass


def _split_lines_keepends(value: str) -> list[str]:
    if value == "":
        return []
    return value.splitlines(keepends=True)


def _parse_hunk_header(line: str) -> tuple[int, int, int, int]:
    match = re.match(r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@", line)
    if not match:
        raise PatchApplyError(f"Invalid unified diff hunk header: {line[:120]}")
    old_count = int(match.group("old_count") or "1")
    new_count = int(match.group("new_count") or "1")
    return int(match.group("old_start")), old_count, int(match.group("new_start")), new_count


def _apply_unified_diff_to_text(original: str, diff: str) -> str:
    lines = _split_lines_keepends(original)
    diff_lines = _split_lines_keepends(diff)
    if not diff_lines or not any(line.startswith("@@ ") for line in diff_lines):
        raise PatchApplyError("Patch has no unified diff hunks")

    out: list[str] = []
    cursor = 0
    idx = 0
    while idx < len(diff_lines):
        line = diff_lines[idx]
        if line.startswith(("--- ", "+++ ", "diff --git ", "index ")):
            idx += 1
            continue
        if not line.startswith("@@ "):
            idx += 1
            continue

        old_start, _old_count, _new_start, _new_count = _parse_hunk_header(line)
        hunk_pos = max(0, old_start - 1)
        if hunk_pos < cursor:
            raise PatchApplyError("Patch hunks overlap or move backwards")
        out.extend(lines[cursor:hunk_pos])
        cursor = hunk_pos
        idx += 1

        while idx < len(diff_lines):
            hunk_line = diff_lines[idx]
            if hunk_line.startswith("@@ "):
                break
            if hunk_line.startswith(("\\ No newline at end of file", "--- ", "+++ ")):
                idx += 1
                continue
            if not hunk_line:
                idx += 1
                continue

            marker = hunk_line[0]
            body = hunk_line[1:]
            if marker == " ":
                if cursor >= len(lines) or lines[cursor] != body:
                    raise PatchApplyError("Patch context did not match current file content")
                out.append(lines[cursor])
                cursor += 1
            elif marker == "-":
                if cursor >= len(lines) or lines[cursor] != body:
                    raise PatchApplyError("Patch removal did not match current file content")
                cursor += 1
            elif marker == "+":
                out.append(body)
            else:
                raise PatchApplyError(f"Unsupported patch line: {hunk_line[:120]}")
            idx += 1

    out.extend(lines[cursor:])
    return "".join(out)


def _workspace_read_for_patch(workspace_root: str | Path | None, rel: str) -> str | None:
    if not workspace_root:
        return None
    try:
        root = Path(workspace_root)
        target = (root / rel).resolve()
        target.relative_to(root.resolve())
        if target.exists() and target.is_file():
            return target.read_text(encoding="utf-8")
    except Exception:
        return None
    return None


def _source_text_for_patch(
    *,
    rel: str,
    active_path: str,
    active_content: str,
    relevant_files: dict[str, str],
    workspace_root: str | Path | None,
) -> str | None:
    if rel == active_path:
        return active_content
    if rel in relevant_files:
        return relevant_files[rel]
    return _workspace_read_for_patch(workspace_root, rel)


def _patches_to_changes(
    patches: Any,
    *,
    active_path: str,
    active_content: str,
    relevant_files: dict[str, str],
    workspace_root: str | Path | None,
) -> tuple[list[dict[str, str]], list[str]]:
    if not isinstance(patches, list):
        return [], []
    out: list[dict[str, str]] = []
    warnings: list[str] = []
    for item in patches:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("path") or "").strip()
        diff = item.get("unified_diff") or item.get("diff") or item.get("patch")
        if not rel or not isinstance(diff, str) or not diff.strip():
            continue
        if ".." in rel.split("/"):
            warnings.append(f"patch rejected for invalid path: {rel}")
            continue
        source = _source_text_for_patch(
            rel=rel,
            active_path=active_path,
            active_content=active_content,
            relevant_files=relevant_files,
            workspace_root=workspace_root,
        )
        if source is None:
            warnings.append(f"patch skipped because source file was not available: {rel}")
            continue
        try:
            new_content = _apply_unified_diff_to_text(source, diff)
        except PatchApplyError as exc:
            warnings.append(f"patch failed for {rel}: {exc}")
            continue
        out.append({"path": rel, "new_content": new_content})
    return out, warnings

SYSTEM_SCAFFOLD = """You are an expert product engineer and front-end architect.
Create a brand new React + Vite + TypeScript website/app for a non-coder using a hosted web builder.
The result should feel production-ready, understandable, and pleasantly “overbuilt” without requiring terminal commands.

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
- Prefer code that can run from persisted text files in a serverless-hosted builder.
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
            "User-Agent": "Appora/0.1 (+local)",
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


def _model_for_provider(provider: str) -> str:
    prefs = _current_user_preferences()
    if prefs:
        model = _model_from_preferences(provider, prefs)
        if model:
            return model
    return _model_from_global_settings(provider)


def _model_from_global_settings(provider: str) -> str:
    s = settings_mod.settings
    if provider == NINE_ROUTER_PROVIDER:
        return getattr(s, "nine_router_model", "free-forever")
    raise RuntimeError(f"Unsupported provider: {provider}")


def _model_from_preferences(provider: str, prefs: UserPreferencesRecord) -> str:
    if provider == NINE_ROUTER_PROVIDER:
        return prefs.nine_router_model or getattr(settings_mod.settings, "nine_router_model", "free-forever")
    return ""


def _current_user_preferences() -> UserPreferencesRecord | None:
    profile_id = CURRENT_PROFILE_ID.get()
    if not profile_id:
        return None
    try:
        return get_user_preferences(profile_id=profile_id)
    except Exception:
        return None


def _selected_provider() -> str:
    prefs = _current_user_preferences()
    provider = str((prefs.llm_provider if prefs else None) or settings_mod.settings.llm_provider or "").strip().lower()
    if provider in {"", "9router", "nine-router", "ninerouter", NINE_ROUTER_PROVIDER}:
        return NINE_ROUTER_PROVIDER
    return NINE_ROUTER_PROVIDER


def _provider_and_model() -> tuple[str, str]:
    provider = _selected_provider()
    if not provider:
        raise RuntimeError("No provider selected yet. Open Settings, choose a provider, then save credentials first.")
    require_provider_connected(provider)
    return provider, _model_for_provider(provider)


def _fallback_provider_order(selected_provider: str) -> list[str]:
    snapshot = auth_snapshot()
    status = snapshot.get(NINE_ROUTER_PROVIDER) or {}
    return [NINE_ROUTER_PROVIDER] if status.get("connected") else []


def _dedupe_models(models: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for model in models:
        clean = str(model or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


def _is_openrouter_free_model(model: str) -> bool:
    clean = str(model or "").strip().lower()
    return clean == "openrouter/free" or clean.endswith(":free")


def _free_tier_models_for_provider(provider: str) -> list[str]:
    return []


def _candidate_models_for_provider(provider: str) -> list[str]:
    configured_model = _model_for_provider(provider)
    if provider == NINE_ROUTER_PROVIDER:
        return [configured_model or "free-forever"]
    return []


def _combo_attempts_for_selected_model(selected_provider: str, selected_model: str) -> tuple[list[tuple[str, str]], dict[str, Any]]:
    route = _normalize_smart_route(selected_model)
    metadata: dict[str, Any] = {"route": route, "skipped": []}
    return [], metadata


def _is_fallback_worthy_error(message: str) -> bool:
    lowered = (message or "").lower()
    return any(token in lowered for token in [
        "rate limit",
        "cooldown",
        "quota",
        "credit",
        "billing",
        "overloaded",
        "temporarily",
        "timeout",
        "timed out",
        "empty response",
        "returned an empty",
        "unavailable",
        "error 429",
        "error 500",
        "error 502",
        "error 503",
        "error 504",
    ])


def _extract_spoken_prefix_from_json_stream(raw: str) -> str:
    match = re.search(r'"spoken"\s*:\s*"', raw)
    if not match:
        return ""
    i = match.end()
    out: list[str] = []
    while i < len(raw):
        ch = raw[i]
        if ch == '"':
            break
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= len(raw):
            break
        esc = raw[i]
        if esc == "u":
            hex_part = raw[i + 1:i + 5]
            if len(hex_part) < 4 or not re.fullmatch(r"[0-9a-fA-F]{4}", hex_part):
                break
            out.append(chr(int(hex_part, 16)))
            i += 5
            continue
        out.append({
            '"': '"',
            "\\": "\\",
            "/": "/",
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
        }.get(esc, esc))
        i += 1
    return "".join(out)


class _StreamingSpokenExtractor:
    def __init__(self, on_spoken_delta: Callable[[str], None] | None):
        self.on_spoken_delta = on_spoken_delta
        self.raw = ""
        self.emitted_chars = 0

    def feed(self, chunk: str) -> None:
        if not self.on_spoken_delta:
            return
        self.raw += str(chunk or "")
        spoken = _extract_spoken_prefix_from_json_stream(self.raw)
        if len(spoken) <= self.emitted_chars:
            return
        delta = spoken[self.emitted_chars:]
        self.emitted_chars = len(spoken)
        if delta:
            self.on_spoken_delta(delta)


def _generate_json_once(
    provider: str,
    model: str,
    *,
    system: str,
    user: str,
    on_text_delta: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    if provider == NINE_ROUTER_PROVIDER:
        raw = nine_router_generate_json(model=model, system=system, user=user, on_text_delta=on_text_delta)
        text = str(raw.get("text") or "")
        if not text.strip():
            err = str(raw.get("error_message") or "").strip()
            if err:
                raise RuntimeError(err)
            raise RuntimeError("9Router returned an empty response")
        return _extract_json_object(text)

    raise RuntimeError(f"Unsupported provider: {provider}")


def _generate_json(
    *,
    system: str,
    user: str,
    on_spoken_delta: Callable[[str], None] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    provider = NINE_ROUTER_PROVIDER
    model = _model_for_provider(provider) or "free-forever"
    require_provider_connected(provider)
    _throttle_llm_calls(provider, _effective_min_gap_seconds())
    spoken_extractor = _StreamingSpokenExtractor(on_spoken_delta)
    if on_spoken_delta:
        data = _generate_json_once(provider, model, system=system, user=user, on_text_delta=spoken_extractor.feed)
    else:
        data = _generate_json_once(provider, model, system=system, user=user)
    if _normalize_smart_route(model) and isinstance(data, dict):
        data["_appora_route"] = {
            "name": _normalize_smart_route(model),
            "used_provider": provider,
            "used_model": model,
            "skipped": [],
            "monthly_cost": "handled by 9Router",
            "quality": "handled by 9Router",
        }
    return provider, model, data


def _provider_fallback_log(data: dict[str, Any]) -> str:
    fallback = data.get("_voiceide_provider_fallback") if isinstance(data, dict) else None
    if not isinstance(fallback, dict):
        return ""
    selected = str(fallback.get("selected_provider") or "").strip()
    used = str(fallback.get("used_provider") or "").strip()
    model = str(fallback.get("used_model") or "").strip()
    if not selected or not used or selected == used:
        return ""
    return f" fallback_from={selected} fallback_to={used} fallback_model={model}".strip()


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
    on_spoken_delta: Callable[[str], None] | None = None,
) -> AgentSuggestion:
    file_tree = file_tree or []
    relevant_files = relevant_files or {}
    context_budget = _effective_context_char_budget()
    active_content_budget = 28_000 if _friendly_free_tier_mode() else 60_000
    bounded_relevant, skipped_context = _bounded_relevant_files(
        relevant_files,
        active_path=path,
        budget_chars=context_budget,
    )

    relevant_blob = "\n\n".join(
        f"FILE: {rel}\n{txt}" for rel, txt in bounded_relevant.items()
    )
    context_block = f"Context:\n{extra_context}\n\n" if extra_context else ""
    current_content = str(content or "")[:active_content_budget]
    tree_limit = 300 if _friendly_free_tier_mode() else 600
    user = (
        f"Active file: {path}\n\n"
        f"Instruction:\n{instruction}\n\n"
        f"{context_block}"
        f"Current content:\n{current_content}\n\n"
        f"File tree:\n" + "\n".join(file_tree[:tree_limit]) + "\n\n"
        f"Relevant files:\n{relevant_blob}"
    )

    provider, model, data = _generate_json(
        system=system or DEFAULT_SYSTEM_PATCH,
        user=user,
        on_spoken_delta=on_spoken_delta,
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

    patch_changes, patch_warnings = _patches_to_changes(
        data.get("patches"),
        active_path=path,
        active_content=str(content or ""),
        relevant_files=relevant_files,
        workspace_root=workspace_root,
    )
    if patch_changes:
        existing_paths = {item["path"] for item in out_changes}
        for patch_change in patch_changes:
            if patch_change["path"] not in existing_paths:
                out_changes.append(patch_change)
                existing_paths.add(patch_change["path"])

    actions = data.get("actions") or []
    if not isinstance(actions, list):
        actions = []

    spoken = str(data.get("spoken") or ("I prepared the requested changes." if out_changes else "I reviewed the request but did not propose any file edits."))
    patch_note = f" patches={len(patch_changes)}" if patch_changes else ""
    patch_warning_note = f" patch_warnings={len(patch_warnings)}" if patch_warnings else ""
    log = (
        f"provider={provider} model={model} files={len(out_changes)} actions={len(actions)} "
        f"{patch_note}{patch_warning_note} "
        f"{_provider_fallback_log(data)} "
        f"prompt_chars={len(user)} prompt_tokens_est={_rough_token_estimate(user)} context_budget={context_budget} context_skipped={skipped_context}"
    )
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
        log=f"provider={provider} model={model} {_provider_fallback_log(data)} files={len(ops)} actions={len(actions)}",
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
        "log": f"provider={provider} model={model} {_provider_fallback_log(data)}",
    }
    _PRD_CACHE[cache_key] = (time.time(), result)
    return result
