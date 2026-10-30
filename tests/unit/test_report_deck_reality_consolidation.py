from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

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


@pytest.mark.parametrize("selected_disputed", [False, True])
def test_health_and_deck_bind_tied_risk_metadata_to_the_named_record(
    monkeypatch, selected_disputed: bool,
) -> None:
    selected = replace(
        _risk_assessment(),
        record=replace(_risk_assessment().record, id="alpha", title="Alpha current"),
        truth_level=TruthLevel.RAW_OBSERVED if selected_disputed else TruthLevel.HUMAN_CONFIRMED,
        disputed=selected_disputed,
        stale=selected_disputed,
    )
    other = replace(
        _risk_assessment(),
        record=replace(_risk_assessment().record, id="zulu", title="Zulu stale review"),
        truth_level=TruthLevel.HUMAN_CONFIRMED if selected_disputed else TruthLevel.RAW_OBSERVED,
        disputed=not selected_disputed,
        stale=not selected_disputed,
    )
    loader = MagicMock(side_effect=AssertionError("Use supplied records and reality"))
    monkeypatch.setattr(report_health_module.ProgramReality, "load", loader)
    reality = MagicMock()
    reality.risks.return_value = (other, selected)

    health = report_health_module._build_health_summary(
        (), None, risks=(other.record, selected.record),
        risk_assessments=(other, selected), stale_risk_ids=("zulu",),
        program_id="acme", programs_root=_PROGRAMS_ROOT, as_of=_AS_OF,
    )
    rows = report_deck_module._build_deck_risk_rows(
        program_id="acme", programs_root=_PROGRAMS_ROOT, as_of=_AS_OF, reality=reality,
    )

    assert health.risk_register_summary == (
        "Risk register: 2 active entries, 1 stale review. "
        "Highest active: Alpha current (owner operator, score 9)."
    )
    assert rows[0].title == selected.record.title
    assert health.risk_register_truth_level == rows[0].evidence_truth_level == selected.truth_level.value
    assert health.risk_register_disputed == rows[0].evidence_disputed == selected.disputed
    assert health.risk_register_stale_evidence == rows[0].evidence_stale == selected.stale
    assert health.risk_register_includes_unconfirmed_sources is True
    loader.assert_not_called()


@pytest.mark.parametrize("assessment_kind", ["missing", "unrelated", "same_id_changed", "equal_copy"])
def test_health_metadata_requires_the_exact_summarized_risk(monkeypatch, assessment_kind: str) -> None:
    selected = _risk_assessment()
    candidate = {
        "missing": (),
        "unrelated": (replace(selected, record=replace(selected.record, id="other", title="Another risk")),),
        "same_id_changed": (replace(selected, record=replace(selected.record, description="Different revision")),),
        "equal_copy": (replace(selected, record=replace(selected.record)),),
    }[assessment_kind]
    loader = MagicMock(side_effect=AssertionError("Do not reload supplied risks"))
    monkeypatch.setattr(report_health_module.ProgramReality, "load", loader)

    health = report_health_module._build_health_summary(
        (), None, risks=(selected.record,), risk_assessments=candidate,
        program_id="acme", programs_root=_PROGRAMS_ROOT,
    )

    assert f"Highest active: {selected.record.title}" in health.risk_register_summary
    assert health.risk_register_truth_level == (
        selected.truth_level.value if assessment_kind == "equal_copy" else None
    )
    assert health.risk_register_disputed is (assessment_kind == "equal_copy")
    assert health.risk_register_stale_evidence is (assessment_kind == "equal_copy")
    loader.assert_not_called()


@pytest.mark.parametrize("status", [RiskStatus.CLOSED, RiskStatus.ACCEPTED, RiskStatus.MITIGATED])
def test_inactive_risk_has_no_health_metadata_or_deck_row(status: RiskStatus) -> None:
    assessment = replace(_risk_assessment(), record=replace(_risk_assessment().record, status=status))
    health = report_health_module._build_health_summary(
        (), None, risks=(assessment.record,), risk_assessments=(assessment,),
    )
    reality = MagicMock()
    reality.risks.return_value = (assessment,)
    rows = report_deck_module._build_deck_risk_rows(
        program_id="acme", programs_root=_PROGRAMS_ROOT, as_of=_AS_OF, reality=reality,
    )
    assert health.risk_register_summary is None
    assert health.risk_register_truth_level is None
    assert health.risk_register_disputed is False
    assert health.risk_register_stale_evidence is False
    assert rows == ()


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
