"""WI-3.9: Per-gap firing fixture tests + <100ms perf test.

Structural-gap rules in `attention()` (§6.1.3):
  - critical_risk_no_mitigation
  - commitment_due_untracked
  - decision_stuck_proposed
  - workstream_no_fresh_state

Perf contract: structural-gap evaluation over 1,000 seeded facts < 100ms.
"""
from __future__ import annotations

import time as _time
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from src.core.program_reality import (
    AttentionItem,
    AttentionKind,
    FactAssessment,
    ProgramReality,
    _check_structural_gaps,
)
from src.core.models_v2 import (
    DecisionEntry,
    DecisionStatus,
    RiskEntry,
    RiskImpact,
    RiskProbability,
    RiskStatus,
    RiskCategory,
    Workstream,
)
from src.core.commitment_store import CommitmentEntry
from src.core.truth_levels import TruthLevel

_NOW = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
_TODAY = _NOW.date()
_PROGRAM = "test_program"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _risk_assessment(
    risk_id: str,
    impact: RiskImpact = RiskImpact.HIGH,
    status: RiskStatus = RiskStatus.OPEN,
) -> FactAssessment:
    risk = RiskEntry(
        id=risk_id,
        program_id=_PROGRAM,
        title=f"Risk {risk_id}",
        description="",
        probability=RiskProbability.LIKELY,
        impact=impact,
        category=RiskCategory.TECHNICAL,
        owner_alias="eng@example.com",
        mitigation_plan=None,
        mitigation_due_date=None,
        linked_workstream_ids=(),
        linked_work_item_ids=(),
        linked_milestone_ids=(),
        linked_claim_ids=(),
        linked_action_ids=(),
        status=status,
        identified_date=_TODAY,
        identified_in_vertex_issue=None,
        last_reviewed_date=None,
        entity_refs=(f"risk:{risk_id}",),
    )
    return FactAssessment(
        record=risk,
        fact_id=f"pf_{risk_id}",
        truth_level=TruthLevel.HUMAN_CONFIRMED,
        disputed=False,
        stale=False,
        provisional_inputs=False,
        evidence=(),
    )


def _commitment_assessment(
    commitment_id: str,
    due_date: date,
    status: str = "open",
) -> FactAssessment:
    commitment = CommitmentEntry(
        commitment_id=commitment_id,
        title=f"Commitment {commitment_id}",
        dri="owner@example.com",
        due_date=due_date.isoformat(),
        direction="inbound",
        status=status,
        description="",
        entity_ref=None,
        slip_history=(),
        program_id=_PROGRAM,
    )
    return FactAssessment(
        record=commitment,
        fact_id=f"pf_{commitment_id}",
        truth_level=TruthLevel.HUMAN_CONFIRMED,
        disputed=False,
        stale=False,
        provisional_inputs=False,
        evidence=(),
    )


def _decision_assessment(
    decision_id: str,
    status: DecisionStatus = DecisionStatus.PROPOSED,
    decision_date: date | None = None,
) -> FactAssessment:
    decision = DecisionEntry(
        id=decision_id,
        program_id=_PROGRAM,
        title=f"Decision {decision_id}",
        context="",
        decision="Proceed",
        rationale=None,
        alternatives_considered=(),
        decided_by="pm@example.com",
        decision_date=decision_date or _TODAY,
        status=status,
        superseded_by=None,
        linked_claim_id=None,
        linked_risk_id=None,
        linked_action_ids=(),
        workstream_id=None,
        entity_refs=(f"decision:{decision_id}",),
    )
    return FactAssessment(
        record=decision,
        fact_id=f"pf_{decision_id}",
        truth_level=TruthLevel.HUMAN_CONFIRMED,
        disputed=False,
        stale=False,
        provisional_inputs=False,
        evidence=(),
    )


def _workstream_assessment(ws_id: str, stale: bool = True) -> FactAssessment:
    ws = Workstream(id=ws_id, name=f"WS {ws_id}")
    return FactAssessment(
        record=ws,
        fact_id=f"pf_{ws_id}",
        truth_level=TruthLevel.HUMAN_CONFIRMED,
        disputed=False,
        stale=stale,
        provisional_inputs=False,
        evidence=(),
    )


def _make_index(*assessments: FactAssessment) -> dict[str, list[FactAssessment]]:
    """Build entity_fact_index from assessments."""
    idx: dict[str, list[FactAssessment]] = {}
    for a in assessments:
        record = a.record
        entity_refs: tuple[str, ...] = ()
        if hasattr(record, "entity_refs"):
            entity_refs = record.entity_refs
        elif hasattr(record, "entity_ref") and record.entity_ref:
            entity_refs = (record.entity_ref,)
        key = entity_refs[0] if entity_refs else f"anon_{id(a)}"
        idx.setdefault(key, []).append(a)
    return idx


# ---------------------------------------------------------------------------
# Test: critical_risk_no_mitigation
# ---------------------------------------------------------------------------

class TestCriticalRiskNoMitigation:
    def test_fires_for_high_open_risk(self) -> None:
        idx = _make_index(_risk_assessment("R-001", impact=RiskImpact.HIGH, status=RiskStatus.OPEN))
        items = _check_structural_gaps(idx, _NOW)
        kinds = [i.kind for i in items]
        assert AttentionKind.STRUCTURAL_GAP in kinds
        gap = next(i for i in items if "critical_risk_no_mitigation" in i.description)
        assert "critical_risk_no_mitigation" in gap.description

    def test_fires_for_critical_open_risk(self) -> None:
        idx = _make_index(_risk_assessment("R-002", impact=RiskImpact.CRITICAL, status=RiskStatus.OPEN))
        items = _check_structural_gaps(idx, _NOW)
        gap = next((i for i in items if "critical_risk_no_mitigation" in i.description), None)
        assert gap is not None

    def test_does_not_fire_for_mitigated_high_risk(self) -> None:
        idx = _make_index(_risk_assessment("R-003", impact=RiskImpact.HIGH, status=RiskStatus.MITIGATED))
        items = _check_structural_gaps(idx, _NOW)
        gap = next((i for i in items if "critical_risk_no_mitigation" in i.description), None)
        assert gap is None

    def test_does_not_fire_for_medium_risk(self) -> None:
        idx = _make_index(_risk_assessment("R-004", impact=RiskImpact.MEDIUM, status=RiskStatus.OPEN))
        items = _check_structural_gaps(idx, _NOW)
        gap = next((i for i in items if "critical_risk_no_mitigation" in i.description), None)
        assert gap is None


# ---------------------------------------------------------------------------
# Test: commitment_due_untracked
# ---------------------------------------------------------------------------

class TestCommitmentDueUntracked:
    def test_fires_when_commitment_due_in_7_days(self) -> None:
        due = _TODAY + timedelta(days=7)
        idx = _make_index(_commitment_assessment("CM-001", due_date=due, status="open"))
        items = _check_structural_gaps(idx, _NOW)
        gap = next((i for i in items if "commitment_due_untracked" in i.description), None)
        assert gap is not None
        assert gap.kind == AttentionKind.STRUCTURAL_GAP

    def test_fires_when_commitment_already_overdue(self) -> None:
        due = _TODAY - timedelta(days=5)
        idx = _make_index(_commitment_assessment("CM-002", due_date=due, status="open"))
        items = _check_structural_gaps(idx, _NOW)
        gap = next((i for i in items if "commitment_due_untracked" in i.description), None)
        assert gap is not None

    def test_does_not_fire_when_far_future(self) -> None:
        due = _TODAY + timedelta(days=60)
        idx = _make_index(_commitment_assessment("CM-003", due_date=due, status="open"))
        items = _check_structural_gaps(idx, _NOW)
        gap = next((i for i in items if "commitment_due_untracked" in i.description), None)
        assert gap is None

    def test_does_not_fire_for_closed_commitment(self) -> None:
        due = _TODAY + timedelta(days=5)
        idx = _make_index(_commitment_assessment("CM-004", due_date=due, status="closed"))
        items = _check_structural_gaps(idx, _NOW)
        gap = next((i for i in items if "commitment_due_untracked" in i.description), None)
        assert gap is None


# ---------------------------------------------------------------------------
# Test: decision_stuck_proposed
# ---------------------------------------------------------------------------

class TestDecisionStuckProposed:
    def test_fires_for_old_proposed_decision(self) -> None:
        old_date = _TODAY - timedelta(days=45)  # >30 days
        idx = _make_index(_decision_assessment("D-001", status=DecisionStatus.PROPOSED, decision_date=old_date))
        items = _check_structural_gaps(idx, _NOW)
        gap = next((i for i in items if "decision_stuck_proposed" in i.description), None)
        assert gap is not None
        assert gap.kind == AttentionKind.STRUCTURAL_GAP

    def test_does_not_fire_for_recent_proposed_decision(self) -> None:
        recent_date = _TODAY - timedelta(days=10)  # <30 days
        idx = _make_index(_decision_assessment("D-002", status=DecisionStatus.PROPOSED, decision_date=recent_date))
        items = _check_structural_gaps(idx, _NOW)
        gap = next((i for i in items if "decision_stuck_proposed" in i.description), None)
        assert gap is None

    def test_does_not_fire_for_decided_decision(self) -> None:
        old_date = _TODAY - timedelta(days=60)
        idx = _make_index(_decision_assessment("D-003", status=DecisionStatus.DECIDED, decision_date=old_date))
        items = _check_structural_gaps(idx, _NOW)
        gap = next((i for i in items if "decision_stuck_proposed" in i.description), None)
        assert gap is None


# ---------------------------------------------------------------------------
# Test: workstream_no_fresh_state
# ---------------------------------------------------------------------------

class TestWorkstreamNoFreshState:
    def test_fires_for_stale_workstream(self) -> None:
        idx = _make_index(_workstream_assessment("WS-001", stale=True))
        items = _check_structural_gaps(idx, _NOW)
        gap = next((i for i in items if "workstream_no_fresh_state" in i.description), None)
        assert gap is not None
        assert gap.kind == AttentionKind.STRUCTURAL_GAP

    def test_does_not_fire_for_fresh_workstream(self) -> None:
        idx = _make_index(_workstream_assessment("WS-002", stale=False))
        items = _check_structural_gaps(idx, _NOW)
        gap = next((i for i in items if "workstream_no_fresh_state" in i.description), None)
        assert gap is None

    def test_empty_index_no_items(self) -> None:
        items = _check_structural_gaps({}, _NOW)
        assert items == []


# ---------------------------------------------------------------------------
# Test: perf contract — <100ms over 1,000 seeded facts (WI-3.9 M-5)
# ---------------------------------------------------------------------------

class TestStructuralGapPerf:
    def test_gap_evaluation_under_100ms_for_1000_facts(self) -> None:
        """Structural-gap evaluation over 1,000 seeded facts must complete in <100ms."""
        # Build a mixed index of 1,000 facts distributed across families
        assessments: list[FactAssessment] = []

        # ~250 workstreams (all fresh)
        for i in range(250):
            assessments.append(_workstream_assessment(f"WS-{i:04d}", stale=False))

        # ~250 risks (all medium, no gap)
        for i in range(250):
            assessments.append(_risk_assessment(f"R-{i:04d}", impact=RiskImpact.MEDIUM, status=RiskStatus.OPEN))

        # ~250 decisions (all recently decided)
        for i in range(250):
            recent_date = _TODAY - timedelta(days=5)
            assessments.append(_decision_assessment(f"D-{i:04d}", status=DecisionStatus.DECIDED, decision_date=recent_date))

        # ~250 commitments (all due in 60 days)
        for i in range(250):
            future_due = _TODAY + timedelta(days=60)
            assessments.append(_commitment_assessment(f"CM-{i:04d}", due_date=future_due, status="open"))

        idx = _make_index(*assessments)

        start = _time.perf_counter()
        items = _check_structural_gaps(idx, _NOW)
        elapsed_ms = (_time.perf_counter() - start) * 1000

        assert elapsed_ms < 100, (
            f"structural-gap evaluation took {elapsed_ms:.1f}ms — budget is <100ms"
        )
        # No gaps should fire (all facts are clean)
        assert items == []

    def test_gap_evaluation_with_all_rules_firing(self) -> None:
        """All 4 gap rules can fire simultaneously without crossing 100ms."""
        old_date = _TODAY - timedelta(days=60)
        imminent_due = _TODAY + timedelta(days=5)
        assessments = [
            _risk_assessment("R-001", impact=RiskImpact.CRITICAL, status=RiskStatus.OPEN),
            _commitment_assessment("CM-001", due_date=imminent_due, status="open"),
            _decision_assessment("D-001", status=DecisionStatus.PROPOSED, decision_date=old_date),
            _workstream_assessment("WS-001", stale=True),
        ]
        idx = _make_index(*assessments)

        start = _time.perf_counter()
        items = _check_structural_gaps(idx, _NOW)
        elapsed_ms = (_time.perf_counter() - start) * 1000

        assert elapsed_ms < 100
        # All 4 rules should fire
        descriptions = " ".join(i.description for i in items)
        assert "critical_risk_no_mitigation" in descriptions
        assert "commitment_due_untracked" in descriptions
        assert "decision_stuck_proposed" in descriptions
        assert "workstream_no_fresh_state" in descriptions
