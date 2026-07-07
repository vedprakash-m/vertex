"""Guards the D-09 / Phase 3 peel of the chronic high-risk gate cluster."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core import quality_gates as quality_gates_module
from src.core.models import AttributionTier, Confidence, DimensionRisk, EvidencePacket, RiskLevel
from src.core.models_v2 import Scorecard, ScorecardDimension, Signal, Workstream
from src.core.quality_gates import chronic as chronic_module


def _dimension_risk(name: str, risk: RiskLevel) -> DimensionRisk:
    return DimensionRisk(
        name=name,
        risk=risk,
        summary=f"{name} summary.",
        evidence=EvidencePacket(
            work_item_id=1,
            revisions=(),
            comments=(),
            enrichments=(),
            confidence=Confidence.MEDIUM,
            tier=AttributionTier.TIER2,
            summary_for_reviewer="evidence",
        ),
    )


def _resolved_workstreams() -> tuple[Workstream, ...]:
    return (Workstream(id="velocity", name="Deployment Velocity", area_paths=("Acme\\Velocity",)),)


def _resolved_scorecards() -> tuple[Scorecard, ...]:
    return (
        Scorecard(
            name="Acme Health",
            dimensions=(ScorecardDimension(name="Deployment Velocity", workstream_id="velocity"),),
        ),
    )


def test_phase_1b_chronic_wrapper_uses_chronic_module(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        quality_gates_module,
        "_evaluate_chronic_high_dimension_gate_impl",
        lambda **kwargs: quality_gates_module.GateEvaluation("QG-12", False, "chronic", 2, forceable=True),
    )
    monkeypatch.setattr(
        quality_gates_module,
        "_load_current_risks",
        lambda program_id, *, programs_root: (),
    )

    result = quality_gates_module._evaluate_chronic_high_dimension_gate(
        dimension_risks=(_dimension_risk("Deployment Velocity", RiskLevel.HIGH),),
        edition_name="acme_weekly",
        journal_signals=(),
        program_id="acme",
        workstreams=_resolved_workstreams(),
        scorecards=_resolved_scorecards(),
        archive_root=tmp_path / "archive",
        programs_root=tmp_path / "programs",
    )

    assert result.gate_id == "QG-12"
    assert result.passed is False


def test_chronic_gate_passes_when_inputs_missing(tmp_path: Path) -> None:
    result = chronic_module.evaluate_chronic_high_dimension_gate(
        dimension_risks=(),
        edition_name=None,
        journal_signals=(),
        program_id=None,
        workstreams=(),
        scorecards=(),
        archive_root=tmp_path,
        open_risks=(),
        dimension_workstream_ids={},
        escalation_source="vertex/escalation",
    )

    assert result.gate_id == "QG-12"
    assert result.passed is True


def test_has_risk_or_escalation_coverage_accepts_matching_signal() -> None:
    covered = chronic_module.has_risk_or_escalation_coverage(
        dimension_name="Deployment Velocity",
        linked_workstream_ids=("velocity",),
        workstreams=_resolved_workstreams(),
        open_risks=(),
        journal_signals=(
            Signal(
                id="signal-1",
                timestamp=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
                source="vertex/escalation",
                program_id="acme",
                workstream_id="velocity",
                entity_refs=(),
                text="Deployment Velocity remains escalated.",
                raw_ref=None,
                confidence=Confidence.MEDIUM,
                metadata=None,
                thread_id=None,
            ),
        ),
        escalation_source="vertex/escalation",
    )

    assert covered is True
