"""WI-7.4: NullProjection — demonstrates O-15 (new application builds against
the ProgramReality facade without modifying src/core/ or existing commands).

A NullProjection consumes ProgramReality read-only and produces a projection
artifact, proving the facade is extensible without touching Zone A internals.

Zone A module (INV-1 applies).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.core.program_reality import ProgramReality


@dataclass(frozen=True, slots=True)
class NullProjectionResult:
    """The projection artifact produced by NullProjection.

    A trivially correct projection: counts of each domain's assessments.
    Used by O-15 contract test to prove the facade is extensible.
    """
    program_id: str
    action_count: int
    risk_count: int
    decision_count: int
    dependency_count: int
    milestone_count: int
    attention_count: int
    commitment_count: int


class NullProjection:
    """O-15 demo: a read-only projection built purely against ProgramReality.

    This class never imports from src.commands.* or modifies any core module.
    It is the canonical proof that a new BI consumer can be added without
    changing Zone A.
    """

    def project(self, reality: ProgramReality) -> NullProjectionResult:
        """Produce a NullProjectionResult from a loaded ProgramReality."""
        return NullProjectionResult(
            program_id=reality.program_id,
            action_count=len(reality.actions()),
            risk_count=len(reality.risks()),
            decision_count=len(reality.decisions()),
            dependency_count=len(reality.dependencies()),
            milestone_count=len(reality.milestones()),
            attention_count=len(reality.attention()),
            commitment_count=len(reality.commitments()),
        )

    def to_dict(self, result: NullProjectionResult) -> dict[str, Any]:
        """Serialise a NullProjectionResult to a plain dict."""
        return {
            "program_id": result.program_id,
            "action_count": result.action_count,
            "risk_count": result.risk_count,
            "decision_count": result.decision_count,
            "dependency_count": result.dependency_count,
            "milestone_count": result.milestone_count,
            "attention_count": result.attention_count,
            "commitment_count": result.commitment_count,
        }
