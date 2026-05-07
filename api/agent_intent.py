from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

InteractionKind = Literal["command", "conversation", "mixed", "inspection"]

_WRITE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(fix|build|ship|implement|create|add|remove|update|change|edit|refactor|repair|wire|connect|integrate|generate|scaffold|run|start|launch|deploy)\b", re.IGNORECASE), "explicit write/build verb"),
    (re.compile(r"\b(bikin|buat|tambahin|tambah|hapus|ubah|rapihin|benahin|perbaiki|perbaikin|jalanin|pasang|sambungin|integrasi|implementasiin|kerjain|garap|eksekusi)\b", re.IGNORECASE), "explicit Indonesian write/build verb"),
]

_INSPECTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(audit|review|debug|validate|check|inspect|analyze|analyse)\b", re.IGNORECASE), "explicit inspection verb"),
    (re.compile(r"\b(cek|audit|validasi|review|analisa|analisis|debug)\b", re.IGNORECASE), "explicit Indonesian inspection verb"),
]

_BUILDER_CONTEXT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(app|builder|feature|ui|ux|preview|project|repo|component|state|style|css|tsx|react|vite|file|folder|mcp|memory|agentic|agent|graph|rag)\b", re.IGNORECASE), "app-builder context"),
    (re.compile(r"(^|\n)\s*[-*]\s+", re.IGNORECASE), "task list structure"),
]

_CONVERSATION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(hi|hello|hey|hai|halo|hei|thanks|thank you|thx|good job|nice|status|update|udah|sudah|gimana|gmn|bro|bang|sip|mantap)\b", re.IGNORECASE), "chat/status language"),
    (re.compile(r"\b(explain|jelasin|jelaskan|why|kenapa|apa|apaan|what|how|what do you think|menurutmu|opinion|pendapat|brainstorm|ngobrol|chat)\b", re.IGNORECASE), "discussion language"),
    (re.compile(r"^(ok|oke|sip|siap|bro|bang|p|hi|hello|hey|hai|halo|hei|udah bro\??)$", re.IGNORECASE), "short conversational prompt"),
]

_EXPLICIT_WRITE_REQUEST_RE = re.compile(
    r"\b(can you|please|tolong|implement|build|fix|bikin|buat|tambahin|ubah|rapihin|perbaiki|implementasiin|kerjain|garap|eksekusi)\b",
    re.IGNORECASE,
)
_FOLLOWUP_WRITE_RE = re.compile(r"^\s*(gas|lanjut|lanjutin|go|execute|eksekusi|oke lanjut|yaudah lanjut)\b", re.IGNORECASE)
_READONLY_AUDIT_RE = re.compile(
    r"\b(audit|review|cek|check|inspect|analy[sz]e|jelasin|explain|laporin|report)\b",
    re.IGNORECASE,
)
_QUESTION_RE = re.compile(r"\?|^(apa|apaan|gimana|gmn|kenapa|mengapa|why|what|how|can|could|would|is|are|do|does|did)\b", re.IGNORECASE)
_SHORT_CHAT_RE = re.compile(r"^(p+|hi+|hello+|hey+|hai+|halo+|hei+|yo+|ok|oke|sip|siap|bro|bang|thanks|makasih|mantap)[!.?\\s]*$", re.IGNORECASE)
_BARE_FOLLOWUP_RE = re.compile(r"^(gas|lanjut|lanjutin|next|continue|go|oke lanjut|yaudah lanjut)[!.?\\s]*$", re.IGNORECASE)
_WRITE_OBJECT_RE = re.compile(
    r"\b(file|page|screen|ui|ux|component|button|modal|form|layout|style|css|tsx|react|vite|route|api|endpoint|database|schema|table|auth|login|project|app|landing|navbar|sidebar|terminal|agent|memory|provider|model)\b",
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
            "inspection": "The user wants read-only inspection, analysis, or audit work without modifying the project.",
        }[self.kind]
        behavior = {
            "command": "Do the requested app-building work. Keep the spoken reply short and action-oriented.",
            "conversation": "Reply in spoken text only. Keep changes and actions empty unless the user explicitly asks to modify the project.",
            "mixed": "Answer the conversational part in spoken text, but only make edits for the explicit implementation request.",
            "inspection": "Inspect, analyze, or audit in spoken text only. Keep changes and actions empty unless the user explicitly asks you to modify the project.",
        }[self.kind]
        return (
            "INTERACTION INTENT:\n"
            f"- kind: {self.kind}\n"
            f"- confidence: {self.confidence:.2f}\n"
            f"- rationale: {self.rationale}\n"
            "- This product is an agentic app builder, not a generic chatbot.\n"
            f"- {mode_line}\n"
            f"- {behavior}\n"
            "- Do not invent code changes just to be helpful during normal conversation or read-only inspection.\n"
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
    word_count = len(raw.split())
    has_question = bool(_QUESTION_RE.search(raw))
    is_short_chat = bool(_SHORT_CHAT_RE.match(raw))
    is_bare_followup = bool(_BARE_FOLLOWUP_RE.match(raw))
    has_write_object = bool(_WRITE_OBJECT_RE.search(raw))

    if not raw or is_short_chat:
        return AgentIntent(
            kind="conversation",
            confidence=0.99,
            rationale="short conversational prompt",
            should_write_files=False,
            should_run_tools=False,
            wants_app_builder=False,
        )

    write_score = 0.0
    inspection_score = 0.0
    conversation_score = 0.0
    signals: list[str] = []

    for pattern, label in _WRITE_PATTERNS:
        matches = pattern.findall(raw)
        if not matches:
            continue
        write_score += min(1.8, 0.55 * len(matches))
        signals.append(label)

    for pattern, label in _INSPECTION_PATTERNS:
        matches = pattern.findall(raw)
        if not matches:
            continue
        inspection_score += min(1.5, 0.4 * len(matches))
        signals.append(label)

    for pattern, label in _BUILDER_CONTEXT_PATTERNS:
        matches = pattern.findall(raw)
        if not matches:
            continue
        write_score += 0.3
        inspection_score += 0.25
        signals.append(label)

    for pattern, label in _CONVERSATION_PATTERNS:
        matches = pattern.findall(raw)
        if not matches:
            continue
        conversation_score += min(1.4, 0.35 * len(matches))
        signals.append(label)

    if active_file:
        write_score += 0.2
        inspection_score += 0.25
        signals.append("active file context present")
    if open_files:
        context_boost = min(0.35, 0.08 * len(open_files[:4]))
        write_score += context_boost * 0.6
        inspection_score += context_boost
        signals.append("open editor context present")
    if "?" in raw:
        conversation_score += 0.2
    if raw.count("\n") >= 2:
        write_score += 0.15
        inspection_score += 0.1
    if re.search(r"\b(agentic app builder|app builder|builder agent)\b", lowered):
        write_score += 0.45
        inspection_score += 0.2
        signals.append("agentic builder framing")

    explicit_write_request = bool(_EXPLICIT_WRITE_REQUEST_RE.search(raw)) or bool(_FOLLOWUP_WRITE_RE.search(raw) and has_write_object)
    readonly_audit_request = bool(_READONLY_AUDIT_RE.search(raw))
    wants_app_builder = bool(re.search(r"\b(app|builder|ui|ux|feature|project|repo|mcp|memory|agentic|agent|graph|rag)\b", lowered))

    if explicit_write_request:
        write_score += 0.8
    if has_question and not explicit_write_request:
        conversation_score += 0.65
        write_score *= 0.55
    if raw and len(raw.split()) <= 4 and conversation_score > 0 and write_score < 1.4 and inspection_score < 1.35:
        conversation_score += 0.45
    if is_bare_followup and not has_write_object:
        conversation_score += 0.7
        write_score = min(write_score, 0.8)

    kind: InteractionKind
    if has_question and not explicit_write_request and inspection_score < 1.1:
        kind = "conversation"
    elif readonly_audit_request and not explicit_write_request and write_score < 2.1:
        kind = "inspection"
    elif write_score >= 1.7 and conversation_score >= 0.95:
        kind = "mixed"
    elif explicit_write_request or write_score >= 1.85:
        kind = "command"
    elif inspection_score >= 1.1 and write_score < 1.7:
        kind = "inspection"
    elif conversation_score >= 0.9 and write_score < 1.4:
        kind = "conversation"
    else:
        if explicit_write_request or (build_mode == "full-agent" and write_score >= 1.1):
            kind = "command" if explicit_write_request or (has_write_object and word_count >= 5) else "conversation"
        elif readonly_audit_request and write_score < 1.7:
            kind = "inspection"
        else:
            kind = "conversation"

    if kind == "mixed" and not explicit_write_request and write_score < 1.95:
        kind = "inspection" if readonly_audit_request else "conversation"

    if kind in {"command", "mixed"} and not explicit_write_request and not has_write_object:
        kind = "conversation"

    should_write_files = kind in {"command", "mixed"} and (explicit_write_request or (write_score >= 1.95 and has_write_object))
    should_run_tools = should_write_files and (write_score + inspection_score) >= 2.15

    dominant = max(write_score, inspection_score, conversation_score, 0.001)
    total = max(write_score + inspection_score + conversation_score, 0.001)
    confidence = dominant / total
    rationale_bits = signals[:5] or ["fallback heuristic"]
    rationale = ", ".join(dict.fromkeys(rationale_bits))

    return AgentIntent(
        kind=kind,
        confidence=max(0.51, min(0.99, round(confidence, 2))),
        rationale=rationale,
        should_write_files=should_write_files,
        should_run_tools=should_run_tools,
        wants_app_builder=wants_app_builder,
    )
