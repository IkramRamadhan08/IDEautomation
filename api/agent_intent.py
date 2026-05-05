from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

InteractionKind = Literal["command", "conversation", "mixed"]

_COMMAND_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(fix|build|ship|implement|create|add|remove|update|change|edit|refactor|debug|audit|repair|polish|review|wire|connect|integrate|generate|scaffold|run|start|launch|deploy|validate)\b", re.IGNORECASE), "explicit build verb"),
    (re.compile(r"\b(bikin|buat|tambahin|tambah|hapus|ubah|rapihin|benahin|perbaiki|perbaikin|jalanin|gas|lanjut|lanjutin|pasang|sambungin|integrasi|debug|cek|audit|validasi|review)\b", re.IGNORECASE), "explicit Indonesian build verb"),
    (re.compile(r"\b(app|builder|feature|ui|ux|preview|project|repo|component|state|style|css|tsx|react|vite|file|folder|mcp|memory|agentic|agent)\b", re.IGNORECASE), "app-builder context"),
    (re.compile(r"(^|\n)\s*[-*]\s+", re.IGNORECASE), "task list structure"),
]

_CONVERSATION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(hi|hello|hey|thanks|thank you|thx|good job|nice|status|update|udah|sudah|gimana|gmn|bro|bang|sip|mantap)\b", re.IGNORECASE), "chat/status language"),
    (re.compile(r"\b(explain|jelasin|jelaskan|why|kenapa|what do you think|menurutmu|opinion|pendapat|brainstorm|ngobrol|chat)\b", re.IGNORECASE), "discussion language"),
    (re.compile(r"^(ok|oke|sip|siap|bro|bang|udah bro\??)$", re.IGNORECASE), "short conversational prompt"),
]

_EXPLICIT_BUILD_REQUEST_RE = re.compile(
    r"\b(can you|please|tolong|pastiin|make sure|lanjut|gas|implement|build|fix|bikin|buat|tambahin|ubah|rapihin|perbaiki|audit|review)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AgentIntent:
    kind: InteractionKind
    confidence: float
    rationale: str
    should_write_files: bool
    should_run_tools: bool
    wants_app_builder: bool

    @property
    def prompt_block(self) -> str:
        mode_line = {
            "command": "The user is primarily giving an app-building command.",
            "conversation": "The user is primarily having a conversation, asking for explanation, or checking status.",
            "mixed": "The user mixed conversation with a concrete app-building request.",
        }[self.kind]
        behavior = {
            "command": "Do the requested app-building work. Keep the spoken reply short and action-oriented.",
            "conversation": "Reply in spoken text only. Keep changes and actions empty unless the user explicitly asks to modify the project.",
            "mixed": "Answer the conversational part in spoken text, but only make edits for the explicit implementation request.",
        }[self.kind]
        return (
            "INTERACTION INTENT:\n"
            f"- kind: {self.kind}\n"
            f"- confidence: {self.confidence:.2f}\n"
            f"- rationale: {self.rationale}\n"
            "- This product is an agentic app builder, not a generic chatbot.\n"
            f"- {mode_line}\n"
            f"- {behavior}\n"
            "- Do not invent code changes just to be helpful during normal conversation.\n"
        )


def classify_agent_intent(
    text: str,
    *,
    build_mode: str | None = None,
    active_file: str | None = None,
    open_files: list[str] | None = None,
) -> AgentIntent:
    raw = str(text or "").strip()
    lowered = raw.lower()
    command_score = 0.0
    conversation_score = 0.0
    signals: list[str] = []

    for pattern, label in _COMMAND_PATTERNS:
        matches = pattern.findall(raw)
        if not matches:
            continue
        boost = min(1.6, 0.45 * len(matches))
        command_score += boost
        signals.append(label)

    for pattern, label in _CONVERSATION_PATTERNS:
        matches = pattern.findall(raw)
        if not matches:
            continue
        boost = min(1.4, 0.35 * len(matches))
        conversation_score += boost
        signals.append(label)

    if active_file:
        command_score += 0.35
        signals.append("active file context present")
    if open_files:
        command_score += min(0.35, 0.08 * len(open_files[:4]))
        signals.append("open editor context present")
    if "?" in raw:
        conversation_score += 0.2
    if raw.count("\n") >= 2:
        command_score += 0.2
    if re.search(r"\b(agentic app builder|app builder|builder agent)\b", lowered):
        command_score += 0.5
        signals.append("agentic builder framing")

    explicit_build_request = bool(_EXPLICIT_BUILD_REQUEST_RE.search(raw))
    wants_app_builder = bool(re.search(r"\b(app|builder|ui|ux|feature|project|repo|mcp|memory|agentic|agent)\b", lowered))

    if explicit_build_request:
        command_score += 0.75
    if raw and len(raw.split()) <= 4 and conversation_score > 0 and command_score < 1.4:
        conversation_score += 0.45

    if command_score >= 1.65 and conversation_score >= 0.95:
        kind: InteractionKind = "mixed"
    elif command_score >= 1.65:
        kind = "command"
    elif conversation_score >= 0.9 and command_score < 1.4:
        kind = "conversation"
    else:
        kind = "command" if explicit_build_request or (build_mode == "full-agent" and command_score >= 1.1) else "conversation"

    if kind == "mixed" and not explicit_build_request and command_score < 1.9:
        kind = "conversation"

    should_write_files = kind in {"command", "mixed"} and (explicit_build_request or command_score >= 1.8)
    should_run_tools = should_write_files and command_score >= 2.0

    total = max(command_score + conversation_score, 0.001)
    confidence = max(command_score, conversation_score) / total
    rationale_bits = signals[:4] or ["fallback heuristic"]
    rationale = ", ".join(dict.fromkeys(rationale_bits))

    return AgentIntent(
        kind=kind,
        confidence=max(0.51, min(0.99, round(confidence, 2))),
        rationale=rationale,
        should_write_files=should_write_files,
        should_run_tools=should_run_tools,
        wants_app_builder=wants_app_builder,
    )
