from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.commands import report_deck as report_deck_module
from src.commands import report_health as report_health_module
from src.core.models import RiskLevel
from src.core.models_v2 import (
    Assumption,
    AssumptionStatus,
    DecisionEntry,
    DecisionStatus,
    RiskCategory,
    RiskEntry,
    RiskImpact,
    RiskProbability,
    RiskStatus,
)
from src.core.program_reality import FactAssessment, TruthLevel


_PROGRAMS_ROOT = Path("Q:\\Workspace\\vertex\\programs")
_AS_OF = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)


def test_build_deck_rows_preserve_fact_assessment_metadata_and_skip_reloads(monkeypatch) -> None:
    shared_reality = MagicMock()
    shared_reality.risks.return_value = (_risk_assessment(),)
    shared_reality.decisions.return_value = (_decision_assessment(),)
    shared_reality.assumptions.return_value = (_assumption_assessment(),)

    load_mock = MagicMock(return_value=shared_reality)
    monkeypatch.setattr(report_deck_module.ProgramReality, "load", load_mock)

    report_deck_module._build_deck_risk_rows(
        program_id="acme",
        as_of=_AS_OF,
        programs_root=_PROGRAMS_ROOT,
    )
    report_deck_module._build_deck_decision_rows(
        program_id="acme",
        as_of=_AS_OF,
        programs_root=_PROGRAMS_ROOT,
    )
    report_deck_module._build_deck_assumption_rows(
        program_id="acme",
        as_of=_AS_OF,
        programs_root=_PROGRAMS_ROOT,
    )
    assert load_mock.call_count == 3

    load_mock.reset_mock()
    risk_rows = report_deck_module._build_deck_risk_rows(
        program_id="acme",
        as_of=_AS_OF,
        programs_root=_PROGRAMS_ROOT,
        reality=shared_reality,
    )
    decision_rows = report_deck_module._build_deck_decision_rows(
        program_id="acme",
        as_of=_AS_OF,
        programs_root=_PROGRAMS_ROOT,
        reality=shared_reality,
    )
    assumption_rows = report_deck_module._build_deck_assumption_rows(
        program_id="acme",
        as_of=_AS_OF,
        programs_root=_PROGRAMS_ROOT,
        reality=shared_reality,
    )

    assert load_mock.call_count == 0
    assert risk_rows[0].evidence_truth_level == TruthLevel.CORROBORATED.value
    assert risk_rows[0].evidence_disputed is True
    assert risk_rows[0].evidence_stale is True
    assert decision_rows[0].evidence_truth_level == TruthLevel.SOURCE_VALIDATED.value
    assert decision_rows[0].evidence_disputed is False
    assert decision_rows[0].evidence_stale is True
    assert assumption_rows[0].evidence_truth_level == TruthLevel.HUMAN_CONFIRMED.value
    assert assumption_rows[0].evidence_disputed is True
    assert assumption_rows[0].evidence_stale is False


def test_build_health_summary_preserves_risk_register_assessment_metadata(monkeypatch) -> None:
    assessment = _health_risk_assessment()
    mock_reality = MagicMock()
    mock_reality.risks.return_value = (assessment,)
    load_mock = MagicMock(return_value=mock_reality)
    monkeypatch.setattr(report_health_module.ProgramReality, "load", load_mock)

    health = report_health_module._build_health_summary(
        (SimpleNamespace(risk=RiskLevel.HIGH, name="Execution", summary="High risk"),),
        previous_snapshot=None,
        program_id="acme",
        programs_root=_PROGRAMS_ROOT,
        as_of=_AS_OF,
        reality=mock_reality,
    )

    assert load_mock.call_count == 0
    assert health.risk_register_truth_level == TruthLevel.GOVERNANCE_LOCKED.value
    assert health.risk_register_disputed is True
    assert health.risk_register_stale_evidence is True


def _risk_assessment() -> FactAssessment:
    return FactAssessment(
        record=RiskEntry(
            id="risk-1",
            program_id="acme",
            title="Deployment telemetry may miss the weekly gate",
            description="desc",
            probability=RiskProbability.LIKELY,
            impact=RiskImpact.HIGH,
            category=RiskCategory.SCHEDULE,
            owner_alias="operator",
            mitigation_plan="Mitigate",
            mitigation_due_date=date(2026, 5, 20),
            linked_workstream_ids=("deployment",),
            linked_work_item_ids=(),
            linked_milestone_ids=(),
            linked_claim_ids=(),
            linked_action_ids=(),
            status=RiskStatus.OPEN,
            identified_date=date(2026, 5, 1),
            identified_in_vertex_issue=None,
            last_reviewed_date=date(2026, 5, 10),
            entity_refs=(),
        ),
        fact_id="fact-risk-1",
        truth_level=TruthLevel.CORROBORATED,
        disputed=True,
        stale=True,
        provisional_inputs=False,
        evidence=("sig-1",),
    )


def _decision_assessment() -> FactAssessment:
    return FactAssessment(
        record=DecisionEntry(
            id="decision-1",
            program_id="acme",
            title="Guard the rollout",
            context="ctx",
            decision="Proceed in phases",
            rationale="because",
            alternatives_considered=(),
            decided_by="operator",
            decision_date=date(2026, 5, 20),
            status=DecisionStatus.DECIDED,
            superseded_by=None,
            linked_claim_id=None,
            linked_risk_id=None,
            linked_action_ids=(),
            workstream_id="deployment",
            entity_refs=(),
            review_by=None,
        ),
        fact_id="fact-decision-1",
        truth_level=TruthLevel.SOURCE_VALIDATED,
        disputed=False,
        stale=True,
        provisional_inputs=False,
        evidence=("sig-2",),
    )


def _assumption_assessment() -> FactAssessment:
    return FactAssessment(
        record=Assumption(
            id="assumption-1",
            program_id="acme",
            text="Schema stays stable through the release window.",
            validation_method="Review with partner",
            validation_due=date(2026, 5, 20),
            status=AssumptionStatus.UNVALIDATED,
            linked_risk_id=None,
            linked_milestone_id=None,
            owner_alias="operator",
            identified_date=date(2026, 5, 1),
            entity_refs=(),
        ),
        fact_id="fact-assumption-1",
        truth_level=TruthLevel.HUMAN_CONFIRMED,
        disputed=True,
        stale=False,
        provisional_inputs=False,
        evidence=("sig-3",),
    )


def _health_risk_assessment() -> FactAssessment:
    return FactAssessment(
        record=RiskEntry(
            id="risk-1",
            program_id="acme",
            title="Deployment telemetry may miss the weekly gate",
            description="desc",
            probability=RiskProbability.LIKELY,
            impact=RiskImpact.HIGH,
            category=RiskCategory.SCHEDULE,
            owner_alias="operator",
            mitigation_plan=None,
            mitigation_due_date=None,
            linked_workstream_ids=(),
            linked_work_item_ids=(),
            linked_milestone_ids=(),
            linked_claim_ids=(),
            linked_action_ids=(),
            status=RiskStatus.OPEN,
            identified_date=date(2026, 5, 1),
            identified_in_vertex_issue=None,
            last_reviewed_date=date(2026, 5, 10),
            entity_refs=(),
        ),
        fact_id="fact-risk-1",
        truth_level=TruthLevel.GOVERNANCE_LOCKED,
        disputed=True,
        stale=True,
        provisional_inputs=False,
        evidence=("sig-1",),
    )
