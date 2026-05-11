from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterable


APPORA_SUPPORTED_PROVIDERS = {
    "openai",
    "anthropic",
    "openrouter",
    "groq",
    "gemini",
    "together",
    "cerebras",
    "xai",
}

PROVIDER_ALIASES: dict[str, str] = {
    # 9router OAuth/subscription-style aliases. Appora can parse these and either
    # route supported providers directly or report an actionable unsupported hint.
    "cc": "claude",
    "cx": "codex",
    "gc": "gemini-cli",
    "qw": "qwen",
    "if": "iflow",
    "ag": "antigravity",
    "gh": "github",
    "kr": "kiro",
    "cu": "cursor",
    "kc": "kilocode",
    "kmc": "kimi-coding",
    "cl": "cline",
    "oc": "opencode",
    "ocg": "opencode-go",
    # API-key providers that Appora can call today.
    "openai": "openai",
    "oa": "openai",
    "anthropic": "anthropic",
    "claude": "anthropic",
    "gemini": "gemini",
    "google": "gemini",
    "gm": "gemini",
    "openrouter": "openrouter",
    "or": "openrouter",
    "groq": "groq",
    "gq": "groq",
    "together": "together",
    "tg": "together",
    "cerebras": "cerebras",
    "cb": "cerebras",
    "xai": "xai",
    "grok": "xai",
}

UNSUPPORTED_9ROUTER_PROVIDERS: dict[str, str] = {
    "kiro": "Kiro OAuth adapter belum tersedia di Appora hosted. Pakai OpenRouter/Gemini/Groq/Cerebras route dulu.",
    "opencode": "OpenCode local/free adapter butuh local gateway/runtime; Appora hosted belum bisa memanggilnya langsung.",
    "opencode-go": "OpenCode Go adapter butuh local gateway/runtime; Appora hosted belum bisa memanggilnya langsung.",
    "claude": "Claude Code subscription adapter belum tersedia di Appora hosted. Pakai Anthropic API key untuk provider anthropic.",
    "codex": "Codex subscription adapter belum tersedia di Appora hosted. Pakai OpenAI API key untuk provider openai.",
    "gemini-cli": "Gemini CLI OAuth adapter belum tersedia di Appora hosted. Pakai Gemini API key untuk provider gemini.",
    "qwen": "Qwen OAuth adapter belum tersedia di Appora hosted. Pakai OpenRouter/Together/Groq model Qwen.",
    "iflow": "iFlow adapter belum tersedia di Appora hosted.",
    "antigravity": "Antigravity adapter belum tersedia di Appora hosted.",
    "github": "GitHub Copilot subscription adapter belum tersedia di Appora hosted.",
    "cursor": "Cursor subscription adapter belum tersedia di Appora hosted.",
    "kilocode": "Kilo Code adapter belum tersedia di Appora hosted.",
    "kimi-coding": "Kimi coding adapter belum tersedia di Appora hosted.",
    "cline": "Cline adapter belum tersedia di Appora hosted.",
}


@dataclass(frozen=True)
class ModelAttempt:
    provider: str
    model: str
    source: str = "model"
    tier: str = "unknown"


@dataclass(frozen=True)
class ParsedModelRef:
    provider: str | None
    model: str
    provider_alias: str | None = None
    unsupported_provider: str | None = None
    unsupported_reason: str | None = None

    @property
    def is_supported(self) -> bool:
        return bool(self.provider and self.model and self.provider in APPORA_SUPPORTED_PROVIDERS)


@dataclass(frozen=True)
class SmartRoute:
    name: str
    description: str
    models: tuple[str, ...]
    strategy: str = "fallback"
    sticky_limit: int = 1
    monthly_cost_label: str = "depends on provider quota"
    quality_label: str = "mixed"
    caveat: str = "Provider limits and account quotas still apply."


@dataclass
class RoutePlan:
    name: str
    attempts: list[ModelAttempt] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)


_ROUTE_ROTATION_LOCK = threading.Lock()
_ROUTE_ROTATION_STATE: dict[str, tuple[int, int]] = {}


BUILTIN_SMART_ROUTES: dict[str, SmartRoute] = {
    "free-forever": SmartRoute(
        name="free-forever",
        description="9router-inspired zero-platform-cost route: free/OAuth-like aliases first, then Appora-supported free/quota-friendly APIs.",
        models=(
            "kr/claude-sonnet-4.5",
            "kr/glm-5",
            "oc/<auto>",
            "openrouter/openrouter/free",
            "openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
            "openrouter/deepseek/deepseek-v4-flash:free",
            "openrouter/deepseek/deepseek-chat-v3-0324:free",
            "gemini/gemini-3-flash-preview",
            "gemini/gemini-2.5-flash",
            "groq/groq/compound",
            "groq/qwen/qwen3-32b",
            "cerebras/zai-glm-4.7",
            "cerebras/gpt-oss-120b",
        ),
        monthly_cost_label="$0 Appora routing cost; provider quotas/credits still apply",
        quality_label="free-first production-capable",
    ),
    "fast-free": SmartRoute(
        name="fast-free",
        description="Low-latency free/quota-friendly route for quick chat and small edits.",
        models=(
            "groq/groq/compound-mini",
            "groq/llama-3.1-8b-instant",
            "cerebras/llama3.1-8b",
            "openrouter/openrouter/free",
            "gemini/gemini-2.0-flash-lite",
        ),
        monthly_cost_label="$0 Appora routing cost; provider quotas/credits still apply",
        quality_label="fast/free",
    ),
    "coding-auto": SmartRoute(
        name="coding-auto",
        description="Balanced route for app-builder coding tasks.",
        models=(
            "openrouter/openrouter/free",
            "openrouter/deepseek/deepseek-v4-flash:free",
            "gemini/gemini-3-flash-preview",
            "groq/qwen/qwen3-32b",
            "cerebras/zai-glm-4.7",
            "openai/gpt-5.4",
            "anthropic/claude-sonnet-4-6",
        ),
        monthly_cost_label="free-first, paid fallback only when configured/allowed",
        quality_label="coding balanced",
    ),
    "cheap-auto": SmartRoute(
        name="cheap-auto",
        description="Cheap/open-model route inspired by 9router tier 2 before paid premium fallback.",
        models=(
            "together/deepseek-ai/DeepSeek-V4-Pro",
            "together/MiniMaxAI/MiniMax-M2.5",
            "openrouter/deepseek/deepseek-v4-flash:free",
            "openrouter/moonshotai/kimi-k2.6",
            "groq/qwen/qwen3-32b",
        ),
        monthly_cost_label="cheap/free-first; provider billing applies",
        quality_label="cost-efficient coding",
    ),
    "quality-auto": SmartRoute(
        name="quality-auto",
        description="Quality-first route for larger projects when the user has API credits.",
        models=(
            "openai/gpt-5.5",
            "anthropic/claude-opus-4-7",
            "openrouter/anthropic/claude-opus-4.7",
            "gemini/gemini-3-pro-preview",
            "xai/grok-4.3",
            "openrouter/openrouter/free",
        ),
        monthly_cost_label="quality-first; paid/provider credits likely",
        quality_label="highest quality",
    ),
}


def smart_route_names() -> list[str]:
    return list(BUILTIN_SMART_ROUTES.keys())


def normalize_smart_route(model: str) -> str:
    clean = str(model or "").strip().lower()
    return clean if clean in BUILTIN_SMART_ROUTES else ""


def resolve_provider_alias(alias_or_id: str) -> str:
    clean = str(alias_or_id or "").strip().lower()
    return PROVIDER_ALIASES.get(clean, clean)


def parse_model_ref(model_ref: str, *, default_provider: str | None = None) -> ParsedModelRef:
    raw = str(model_ref or "").strip()
    if not raw:
        return ParsedModelRef(provider=default_provider, model="")
    if "/" not in raw:
        return ParsedModelRef(provider=default_provider, model=raw)

    provider_alias, model = raw.split("/", 1)
    provider = resolve_provider_alias(provider_alias)
    if provider in APPORA_SUPPORTED_PROVIDERS:
        return ParsedModelRef(provider=provider, model=model, provider_alias=provider_alias)
    return ParsedModelRef(
        provider=None,
        model=model,
        provider_alias=provider_alias,
        unsupported_provider=provider,
        unsupported_reason=UNSUPPORTED_9ROUTER_PROVIDERS.get(provider, f"Provider {provider} belum tersedia di Appora hosted."),
    )


def _custom_routes_from_env() -> dict[str, SmartRoute]:
    raw = (os.getenv("APPORA_MODEL_COMBOS_JSON") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    combos = parsed.get("combos") if isinstance(parsed, dict) else parsed
    if not isinstance(combos, list):
        return {}
    out: dict[str, SmartRoute] = {}
    for item in combos:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip().lower()
        raw_models = item.get("models")
        if not name or name in BUILTIN_SMART_ROUTES or not isinstance(raw_models, list):
            continue
        models = tuple(str(model).strip() for model in raw_models if str(model or "").strip())
        if not models:
            continue
        out[name] = SmartRoute(
            name=name,
            description=str(item.get("description") or "Custom Appora model combo."),
            models=models,
            strategy=str(item.get("strategy") or "fallback"),
            sticky_limit=max(1, int(item.get("sticky_limit") or 1)),
            monthly_cost_label=str(item.get("monthly_cost_label") or "custom route"),
            quality_label=str(item.get("quality_label") or "custom"),
            caveat=str(item.get("caveat") or "Provider limits and account quotas still apply."),
        )
    return out


def route_catalog() -> dict[str, SmartRoute]:
    return {**BUILTIN_SMART_ROUTES, **_custom_routes_from_env()}


def _rotate_attempts(route: SmartRoute, refs: list[str]) -> list[str]:
    if route.strategy != "round-robin" or len(refs) <= 1:
        return refs
    key = route.name
    sticky = max(1, int(route.sticky_limit or 1))
    with _ROUTE_ROTATION_LOCK:
        index, uses = _ROUTE_ROTATION_STATE.get(key, (0, 0))
        current = index % len(refs)
        rotated = refs[current:] + refs[:current]
        uses += 1
        if uses >= sticky:
            _ROUTE_ROTATION_STATE[key] = ((current + 1) % len(refs), 0)
        else:
            _ROUTE_ROTATION_STATE[key] = (current, uses)
    return rotated


def build_route_plan(
    *,
    route_name: str,
    selected_provider: str,
    connected_providers: set[str],
    cooldown_remaining: Callable[[str], float],
) -> RoutePlan:
    routes = route_catalog()
    route = routes.get(normalize_smart_route(route_name))
    if not route:
        return RoutePlan(name=route_name, skipped=[f"Unknown route: {route_name}"])

    plan = RoutePlan(
        name=route.name,
        metadata={
            "description": route.description,
            "monthly_cost": route.monthly_cost_label,
            "quality": route.quality_label,
            "caveat": route.caveat,
        },
    )
    seen: set[tuple[str, str]] = set()
    for ref in _rotate_attempts(route, list(route.models)):
        parsed = parse_model_ref(ref, default_provider=selected_provider)
        if not parsed.is_supported:
            if parsed.unsupported_reason:
                plan.skipped.append(f"{ref}: {parsed.unsupported_reason}")
            continue
        provider = parsed.provider or selected_provider
        if provider not in connected_providers:
            plan.skipped.append(f"{ref}: provider {provider} belum connected")
            continue
        if provider != selected_provider and cooldown_remaining(provider) > 0:
            plan.skipped.append(f"{ref}: provider {provider} masih cooldown")
            continue
        key = (provider, parsed.model)
        if key in seen:
            continue
        seen.add(key)
        plan.attempts.append(ModelAttempt(provider=provider, model=parsed.model, source=route.name, tier=route.quality_label))
    return plan


def build_direct_model_attempt(model_ref: str, *, selected_provider: str) -> tuple[ModelAttempt | None, str | None]:
    parsed = parse_model_ref(model_ref, default_provider=selected_provider)
    if parsed.is_supported and parsed.provider and parsed.model:
        return ModelAttempt(provider=parsed.provider, model=parsed.model, source="direct"), None
    if parsed.unsupported_reason:
        return None, parsed.unsupported_reason
    return None, None


def dedupe_attempts(attempts: Iterable[ModelAttempt]) -> list[ModelAttempt]:
    out: list[ModelAttempt] = []
    seen: set[tuple[str, str]] = set()
    for attempt in attempts:
        if not attempt.provider or not attempt.model:
            continue
        key = (attempt.provider, attempt.model)
        if key in seen:
            continue
        seen.add(key)
        out.append(attempt)
    return out
