from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .clara import CLARA_SPEC
from .raka import RAKA_SPEC

BuildMode = Literal["full-agent", "hybrid"]


@dataclass(frozen=True)
class AgentModeSpec:
    build_mode: BuildMode
    persona_name: str
    persona_label: str
    system_prompt: str
    instruction_prefix: str
    refinement_prefix: str
    request_status: str


FULL_AGENT_SPEC = AgentModeSpec(**CLARA_SPEC.__dict__)
HYBRID_SPEC = AgentModeSpec(**RAKA_SPEC.__dict__)

_MODE_SPECS = {
    FULL_AGENT_SPEC.build_mode: FULL_AGENT_SPEC,
    HYBRID_SPEC.build_mode: HYBRID_SPEC,
}


def get_agent_mode_spec(build_mode: str | None) -> AgentModeSpec:
    mode = (build_mode or "hybrid").strip().lower()
    return _MODE_SPECS.get(mode, HYBRID_SPEC)
