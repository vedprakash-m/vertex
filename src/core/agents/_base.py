from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from src.core.models import Confidence
from src.core.models_v2 import Program, Signal


@dataclass(frozen=True, slots=True)
class AgentContext:
    program_id: str
    program: Program
    author_alias: str | None
    dry_run: bool


@dataclass(frozen=True, slots=True)
class Proposal:
    proposal_id: str
    agent_name: str
    action_type: str
    summary: str
    evidence_refs: tuple[str, ...]
    confidence: Confidence


@dataclass(frozen=True, slots=True)
class ApplyResult:
    success: bool
    action_id: str | None = None
    error_message: str | None = None


@runtime_checkable
class AutonomyGovernor(Protocol):
    pass


@runtime_checkable
class DetectorAgent(Protocol):
    def detect(self, ctx: AgentContext) -> list[Signal]: ...


@runtime_checkable
class ActionableAgent(DetectorAgent, Protocol):
    def propose(self, ctx: AgentContext) -> list[Proposal]: ...

    def apply(self, proposal: Proposal, governor: AutonomyGovernor) -> ApplyResult: ...