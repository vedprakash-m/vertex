from __future__ import annotations

from dataclasses import replace

from src.core.agents._base import ActionableAgent, AgentContext, ApplyResult, AutonomyGovernor, DetectorAgent, Proposal
from src.core.models import Confidence
from src.core.models_v2 import Program, Signal


class _DetectorOnly:
    def detect(self, ctx: AgentContext) -> list[Signal]:
        return []


class _Actionable:
    def detect(self, ctx: AgentContext) -> list[Signal]:
        return []

    def propose(self, ctx: AgentContext) -> list[Proposal]:
        return [
            Proposal(
                proposal_id="proposal-1",
                agent_name="ado-steward",
                action_type="vitality_nudge",
                summary="Draft a vitality nudge.",
                evidence_refs=("WI:1001",),
                confidence=Confidence.HIGH,
            )
        ]

    def apply(self, proposal: Proposal, governor: AutonomyGovernor) -> ApplyResult:
        return ApplyResult(success=True, action_id=proposal.proposal_id)


def test_detector_agent_protocol_accepts_read_only_implementations() -> None:
    program = Program(schema_version="3.0", id="acme", name="Acme")
    context = AgentContext(program_id="acme", program=program, author_alias="demo", dry_run=True)

    detector = _DetectorOnly()

    assert isinstance(detector, DetectorAgent)
    assert detector.detect(context) == []
    assert not isinstance(detector, ActionableAgent)


def test_actionable_agent_protocol_requires_propose_and_apply() -> None:
    program = Program(schema_version="3.0", id="acme", name="Acme")
    context = AgentContext(program_id="acme", program=program, author_alias="demo", dry_run=False)
    proposal = Proposal(
        proposal_id="proposal-1",
        agent_name="ado-steward",
        action_type="vitality_nudge",
        summary="Draft a vitality nudge.",
        evidence_refs=("WI:1001",),
        confidence=Confidence.HIGH,
    )

    actionable = _Actionable()

    assert isinstance(actionable, DetectorAgent)
    assert isinstance(actionable, ActionableAgent)
    assert actionable.propose(context) == [proposal]
    assert actionable.apply(replace(proposal, confidence=Confidence.LOW), actionable).success is True