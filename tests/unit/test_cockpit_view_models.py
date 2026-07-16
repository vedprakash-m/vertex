"""Unit tests for ADF-W0.8 cockpit models, serialization, and the builder."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.cockpit_builder import build_cockpit_snapshot
from src.core.cockpit_models import (
    CockpitFinding,
    CockpitSnapshot,
    EconomicsCockpitSummary,
    IntelligenceCockpitSummary,
    ProgramCockpitSummary,
    ReliabilityCockpitSummary,
    SourceCockpitSummary,
    ValueCockpitSummary,
    cockpit_history_filename,
    cockpit_snapshot_to_json_dict,
    compute_cockpit_input_hash,
    finalize_cockpit_snapshot,
)

_NOW = datetime(2026, 7, 12, 3, 4, 5, tzinfo=timezone.utc)


def _bare_snapshot(**overrides: object) -> CockpitSnapshot:
    base = CockpitSnapshot(
        schema_version="1",
        program_id="fixture_prog",
        edition_id="fixture_prog_weekly",
        generated_at=_NOW,
        as_of=_NOW,
        program_summary=ProgramCockpitSummary(
            overall_risk="green", readiness_percent=None, blocker_count=0, top_three_candidates=(), next_action=None
        ),
        source_summary=SourceCockpitSummary(
            required_healthy=0, required_total=0, stale_sources=(), degraded_sources=(), manual_sources=(), newest_watermarks={}
        ),
        intelligence_summary=IntelligenceCockpitSummary(
            lineage_coverage=None, verification_coverage=None, extraction_quality=(), contradiction_count=0
        ),
        economics_summary=EconomicsCockpitSummary(
            frontier_avoidance=None, frontier_cost_usd=0.0, cache_hit_rate=None, context_tokens_in=0
        ),
        value_summary=ValueCockpitSummary(metrics=(), time_savings_certification=None),
        reliability_summary=ReliabilityCockpitSummary(
            outbox_pending=0, uncertain_remote_state=0, dead_letter_count=0, duplicate_preventions=0, audit_coverage=None
        ),
        findings=(),
        input_hash="",
    )
    return replace(base, **overrides)


def test_cockpit_finding_rejects_invalid_status() -> None:
    with pytest.raises(ValueError):
        CockpitFinding(
            finding_id="x",
            area="program",
            status="not-a-status",
            summary="s",
            detail="d",
            owner=None,
            next_command=None,
            evidence_refs=(),
            observed_at=_NOW,
        )


def test_cockpit_finding_rejects_invalid_area() -> None:
    with pytest.raises(ValueError):
        CockpitFinding(
            finding_id="x",
            area="not-an-area",
            status="ok",
            summary="s",
            detail="d",
            owner=None,
            next_command=None,
            evidence_refs=(),
            observed_at=_NOW,
        )


def test_serialization_uses_iso8601_and_enum_values() -> None:
    snapshot = finalize_cockpit_snapshot(_bare_snapshot())
    payload = cockpit_snapshot_to_json_dict(snapshot)
    assert payload["generated_at"] == "2026-07-12T03:04:05+00:00"
    assert isinstance(payload["findings"], list)
    assert payload["program_summary"]["overall_risk"] == "green"


def test_input_hash_excludes_generated_at_and_is_stable() -> None:
    snapshot_a = finalize_cockpit_snapshot(_bare_snapshot())
    snapshot_b = finalize_cockpit_snapshot(_bare_snapshot(generated_at=_NOW.replace(hour=9)))
    assert snapshot_a.input_hash == snapshot_b.input_hash


def test_input_hash_changes_when_content_changes() -> None:
    snapshot_a = finalize_cockpit_snapshot(_bare_snapshot())
    snapshot_b = finalize_cockpit_snapshot(
        _bare_snapshot(program_summary=replace(_bare_snapshot().program_summary, overall_risk="red"))
    )
    assert snapshot_a.input_hash != snapshot_b.input_hash


def test_input_hash_matches_direct_computation() -> None:
    snapshot = _bare_snapshot()
    expected = compute_cockpit_input_hash(snapshot)
    finalized = finalize_cockpit_snapshot(snapshot)
    assert finalized.input_hash == expected


def test_history_filename_format() -> None:
    snapshot = _bare_snapshot(generated_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
    assert cockpit_history_filename(snapshot) == "20260102T030405Z.json"


def test_build_cockpit_snapshot_on_empty_fixture_program_never_raises(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    snapshot = build_cockpit_snapshot("fixture_prog", programs_root=programs_root, now=_NOW)
    assert snapshot.program_id == "fixture_prog"
    assert snapshot.schema_version == "1"
    assert snapshot.program_summary.overall_risk == "green"
    assert snapshot.economics_summary.frontier_cost_usd == 0.0
    assert len(snapshot.input_hash) == 64
    # Every empty/unavailable summary field is backed by an explanatory finding.
    assert any(finding.finding_id == "economics.ai_telemetry.empty" for finding in snapshot.findings)
    assert any(finding.finding_id == "source.health.not_probed" for finding in snapshot.findings)
    assert any(finding.finding_id == "value.metrics.formula_derived_retired" for finding in snapshot.findings)
    assert any(finding.finding_id == "value.report_wall_time.insufficient_history" for finding in snapshot.findings)
    assert any(finding.finding_id == "reliability.outbox.not_wired" for finding in snapshot.findings)
    assert any(finding.finding_id == "intelligence.lineage.no_facts" for finding in snapshot.findings)
    assert snapshot.intelligence_summary.lineage_coverage is None


def test_build_cockpit_snapshot_is_deterministic_for_same_inputs(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    first = build_cockpit_snapshot("fixture_prog", programs_root=programs_root, now=_NOW)
    second = build_cockpit_snapshot("fixture_prog", programs_root=programs_root, now=_NOW)
    assert first.input_hash == second.input_hash


def test_build_cockpit_snapshot_reports_real_lineage_coverage(monkeypatch, tmp_path: Path) -> None:
    """ADF-W2.4/W2.5: lineage_coverage is a real measured ratio, not a
    placeholder, once the fact store has facts."""
    from src.core.fact_lineage_coverage import LineageCoverageReport

    report = LineageCoverageReport(
        program_id="fixture_prog",
        total_count=10,
        lineaged_count=7,
        waived_count=1,
        defect_count=2,
        sample_defect_natural_keys=("risk:item-1", "risk:item-2"),
        computed_at=_NOW,
    )
    monkeypatch.setattr("src.core.cockpit_builder.compute_lineage_coverage", lambda *args, **kwargs: report)

    programs_root = tmp_path / "programs"
    snapshot = build_cockpit_snapshot("fixture_prog", programs_root=programs_root, now=_NOW)

    assert snapshot.intelligence_summary.lineage_coverage == 0.7
    defect_finding = next(f for f in snapshot.findings if f.finding_id == "intelligence.lineage.defects_present")
    assert defect_finding.status == "warn"
    assert "risk:item-1" in defect_finding.evidence_refs


def test_build_cockpit_snapshot_lineage_ok_status_when_no_defects(monkeypatch, tmp_path: Path) -> None:
    from src.core.fact_lineage_coverage import LineageCoverageReport

    report = LineageCoverageReport(
        program_id="fixture_prog",
        total_count=5,
        lineaged_count=5,
        waived_count=0,
        defect_count=0,
        sample_defect_natural_keys=(),
        computed_at=_NOW,
    )
    monkeypatch.setattr("src.core.cockpit_builder.compute_lineage_coverage", lambda *args, **kwargs: report)

    programs_root = tmp_path / "programs"
    snapshot = build_cockpit_snapshot("fixture_prog", programs_root=programs_root, now=_NOW)

    assert snapshot.intelligence_summary.lineage_coverage == 1.0
    assert any(f.finding_id == "intelligence.lineage.fully_covered" and f.status == "ok" for f in snapshot.findings)
