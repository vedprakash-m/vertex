"""Unit tests for ADF-W4.4: inferred-dependency evidence hierarchy + metrics."""

from __future__ import annotations

from datetime import date

import pytest

from src.core.inferred_dependency_evidence import (
    DependencyEvidenceReport,
    build_evidence_report,
    cap_confidence_for_tier,
    is_actuation_eligible,
    tier_for_dependency,
)
from src.core.models import Confidence
from src.core.models_v2 import (
    Dependency,
    DependencyEvidenceTier,
    DependencyStatus,
    DependencyType,
)


def _dep(
    *,
    tier: DependencyEvidenceTier = DependencyEvidenceTier.AUTHORED,
    item_id: int = 1,
) -> Dependency:
    return Dependency(
        id=f"dep-{item_id}",
        from_program_id="xpf",
        from_workstream_id=None,
        from_item_id=item_id,
        from_milestone_id=None,
        to_program_id="armada",
        to_workstream_id=None,
        to_item_id=item_id + 100,
        to_milestone_id=None,
        dependency_type=DependencyType.BLOCKS,
        risk_if_broken="delivery slips",
        mitigation=None,
        status=DependencyStatus.ACTIVE,
        owner_alias="owner",
        evidence_tier=tier,
    )


def test_build_evidence_report_counts_each_tier() -> None:
    deps = (
        _dep(tier=DependencyEvidenceTier.AUTHORITATIVE_RELATION, item_id=1),
        _dep(tier=DependencyEvidenceTier.AUTHORED, item_id=2),
        _dep(tier=DependencyEvidenceTier.AUTHORED, item_id=3),
        _dep(tier=DependencyEvidenceTier.SOURCE_STATEMENT, item_id=4),
        _dep(tier=DependencyEvidenceTier.ETA_CO_MOVEMENT, item_id=5),
        _dep(tier=DependencyEvidenceTier.INFERRED_COMENTION, item_id=6),
        _dep(tier=DependencyEvidenceTier.INFERRED_COMENTION, item_id=7),
    )
    report = build_evidence_report(deps)
    assert report.total == 7
    assert report.authoritative_relation == 1
    assert report.authored == 2
    assert report.source_statement == 1
    assert report.eta_co_movement == 1
    assert report.inferred_comention == 2
    # deterministic = authoritative + authored = 3
    assert report.deterministic_count == 3
    assert report.inferred_count == 4
    assert report.deterministic_ratio == pytest.approx(3 / 7)


def test_empty_dependency_set_report() -> None:
    report = build_evidence_report(())
    assert report.total == 0
    assert report.deterministic_ratio == 0.0


def test_cap_confidence_comention_never_above_low() -> None:
    assert cap_confidence_for_tier(DependencyEvidenceTier.INFERRED_COMENTION, Confidence.HIGH) is Confidence.LOW
    assert cap_confidence_for_tier(DependencyEvidenceTier.INFERRED_COMENTION, Confidence.MEDIUM) is Confidence.LOW
    assert cap_confidence_for_tier(DependencyEvidenceTier.INFERRED_COMENTION, Confidence.LOW) is Confidence.LOW
    assert cap_confidence_for_tier(DependencyEvidenceTier.INFERRED_COMENTION, Confidence.NONE) is Confidence.NONE


def test_cap_confidence_strong_tiers_unchanged() -> None:
    for tier in (
        DependencyEvidenceTier.AUTHORITATIVE_RELATION,
        DependencyEvidenceTier.AUTHORED,
        DependencyEvidenceTier.SOURCE_STATEMENT,
        DependencyEvidenceTier.ETA_CO_MOVEMENT,
    ):
        assert cap_confidence_for_tier(tier, Confidence.HIGH) is Confidence.HIGH


def test_is_actuation_eligible_excludes_comention() -> None:
    assert is_actuation_eligible(DependencyEvidenceTier.AUTHORITATIVE_RELATION) is True
    assert is_actuation_eligible(DependencyEvidenceTier.AUTHORED) is True
    assert is_actuation_eligible(DependencyEvidenceTier.INFERRED_COMENTION) is False


def test_tier_for_dependency_returns_evidence_tier() -> None:
    dep = _dep(tier=DependencyEvidenceTier.SOURCE_STATEMENT)
    assert tier_for_dependency(dep) is DependencyEvidenceTier.SOURCE_STATEMENT


def test_default_evidence_tier_is_authored() -> None:
    """Backward compat: a Dependency constructed without evidence_tier defaults to AUTHORED."""
    dep = Dependency(
        id="dep-x",
        from_program_id="xpf",
        from_workstream_id=None,
        from_item_id=1,
        from_milestone_id=None,
        to_program_id="armada",
        to_workstream_id=None,
        to_item_id=2,
        to_milestone_id=None,
        dependency_type=DependencyType.BLOCKS,
        risk_if_broken="slip",
        mitigation=None,
        status=DependencyStatus.ACTIVE,
        owner_alias="owner",
    )
    assert dep.evidence_tier is DependencyEvidenceTier.AUTHORED


def test_deterministic_vs_inferred_separation() -> None:
    """The acceptance evidence: 'deterministic vs inferred metrics separated'."""
    deps = (
        _dep(tier=DependencyEvidenceTier.AUTHORITATIVE_RELATION, item_id=1),
        _dep(tier=DependencyEvidenceTier.INFERRED_COMENTION, item_id=2),
    )
    report = build_evidence_report(deps)
    assert report.deterministic_count == 1
    assert report.inferred_count == 1
    assert report.deterministic_count + report.inferred_count == report.total
