"""Guards the D-09 / Phase 3 peel of the narrative-focused phase-1b gates."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from src.core import quality_gates as quality_gates_module
from src.core.config_loader import NarrativeProgramContext, ProgramWorkstream
from src.core.models import AttributionTier, Confidence, DeltaKind, DeltaSet, DimensionRisk, EvidencePacket, ItemDelta, RiskLevel, WorkItem
from src.core.models_v2 import Scorecard, ScorecardDimension, Workstream
from src.core.overrides_store import DimensionOverride, OverridesDocument, ScorecardOverrides
from src.core.quality_gates import narrative as narrative_module


def _work_item(work_item_id: int, *, area_path: str = "Acme\\Velocity", risk_level: RiskLevel = RiskLevel.MEDIUM) -> WorkItem:
    return WorkItem(
        id=work_item_id,
        type="Feature",
        title=f"Item {work_item_id}",
        state="Active",
        assigned_to="owner",
        assigned_to_email="owner@example.com",
        area_path=area_path,
        iteration_path="Acme",
        target_date=None,
        risk_level=risk_level,
        tags=[],
        custom_fields={},
        revisions=[],
        comments=[],
        fetched_at=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
    )


def _delta(work_item_id: int, kind: DeltaKind) -> DeltaSet:
    evidence = EvidencePacket(
        work_item_id=work_item_id,
        revisions=(),
        comments=(),
        enrichments=(),
        confidence=Confidence.MEDIUM,
        tier=AttributionTier.TIER2,
        summary_for_reviewer="evidence",
    )
    item_delta = ItemDelta(
        work_item_id=work_item_id,
        kind=kind,
        field_changes={},
        old_risk=RiskLevel.MEDIUM,
        new_risk=RiskLevel.HIGH,
        old_eta=date(2026, 5, 12),
        new_eta=date(2026, 5, 15),
        evidence=evidence,
    )
    return DeltaSet(
        issue_number=78,
        previous_issue_number=77,
        new_items=(),
        closed_items=(),
        risk_changes=(item_delta,) if kind == DeltaKind.RISK_UP else (),
        eta_changes=(item_delta,) if kind == DeltaKind.ETA_CHANGED else (),
        unchanged_count=0,
        owner_changes=(),
    )


def _program_context() -> NarrativeProgramContext:
    return NarrativeProgramContext(
        schema_version="1.0",
        program_name="Acme",
        objective=None,
        mission=None,
        pillars=(),
        glossary={},
        workstreams=(
            ProgramWorkstream(
                name="Deployment Velocity",
                aliases=(),
                area_paths=("Acme\\Velocity",),
                dri_email=None,
                alternate_owner=None,
                description=None,
            ),
        ),
        people=(),
    )


def _resolved_workstreams() -> tuple[Workstream, ...]:
    return (
        Workstream(id="velocity", name="Deployment Velocity", area_paths=("Acme\\Velocity",)),
    )


def _resolved_scorecards() -> tuple[Scorecard, ...]:
    return (
        Scorecard(
            name="Acme Health",
            dimensions=(ScorecardDimension(name="Deployment Velocity", workstream_id="velocity"),),
        ),
    )


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


def test_phase_1b_private_aliases_point_to_narrative_module() -> None:
    assert quality_gates_module._evaluate_material_change_narrative_gate is narrative_module.evaluate_material_change_narrative_gate
    assert quality_gates_module._evaluate_claim_contradiction_gate is narrative_module.evaluate_claim_contradiction_gate
    assert quality_gates_module._evaluate_contradiction_narrative_gate is narrative_module.evaluate_contradiction_narrative_gate
    assert quality_gates_module._evaluate_high_risk_next_action_gate is narrative_module.evaluate_high_risk_next_action_gate


def test_material_change_narrative_gate_passes_when_inputs_are_missing(tmp_path: Path) -> None:
    result = narrative_module.evaluate_material_change_narrative_gate(
        items=(),
        deltas=None,
        edition_name=None,
        issue_number=None,
        workstream_blurbs=None,
        program_context=None,
        archive_root=tmp_path,
    )

    assert result.gate_id == "QG-10"
    assert result.passed is True


def test_high_risk_next_action_gate_passes_when_override_contains_action() -> None:
    result = narrative_module.evaluate_high_risk_next_action_gate(
        dimension_risks=(_dimension_risk("Deployment Velocity", RiskLevel.HIGH),),
        overrides_document=OverridesDocument(
            issue_number=78,
            top_3_now=(),
            scorecards=(
                ScorecardOverrides(
                    name="Acme Health",
                    dimensions=(
                        DimensionOverride(
                            name="Deployment Velocity",
                            risk=RiskLevel.HIGH,
                            note="Next step: confirm mitigation owner by 2026-05-20.",
                        ),
                    ),
                ),
            ),
        ),
        workstream_blurbs={"deployment-velocity": "Risk remains elevated."},
        scorecards=_resolved_scorecards(),
        workstreams=_resolved_workstreams(),
    )

    assert result.gate_id == "QG-14"
    assert result.passed is True


def test_phase_1b_gates_continue_to_use_narrative_helpers(monkeypatch) -> None:
    monkeypatch.setattr(
        quality_gates_module,
        "_evaluate_material_change_narrative_gate",
        lambda **kwargs: quality_gates_module.GateEvaluation("QG-10", False, "material", 2, forceable=True),
    )
    monkeypatch.setattr(
        quality_gates_module,
        "_evaluate_claim_contradiction_gate",
        lambda **kwargs: quality_gates_module.GateEvaluation("QG-11", True, "claim", 2, forceable=True),
    )
    monkeypatch.setattr(
        quality_gates_module,
        "_evaluate_contradiction_narrative_gate",
        lambda **kwargs: quality_gates_module.GateEvaluation("QG-17", True, "contradiction", 3, forceable=True),
    )
    monkeypatch.setattr(
        quality_gates_module,
        "_evaluate_high_risk_next_action_gate",
        lambda **kwargs: quality_gates_module.GateEvaluation("QG-14", True, "next action", 2, forceable=True),
    )

    report = quality_gates_module.evaluate_phase_1b_gates(
        freshness_report=quality_gates_module.FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        items=(_work_item(1001),),
        as_of=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        deltas=_delta(1001, DeltaKind.RISK_UP),
        edition_name="acme_weekly",
        issue_number=78,
        workstream_blurbs={"deployment-velocity": "Steady narrative."},
        program_context=_program_context(),
        dimension_risks=(),
        workstreams=_resolved_workstreams(),
        scorecards=_resolved_scorecards(),
    )

    assert report.qg_results["QG-10"] is False
    assert report.qg_results["QG-11"] is True
    assert report.qg_results["QG-17"] is True
    assert report.qg_results["QG-14"] is True
