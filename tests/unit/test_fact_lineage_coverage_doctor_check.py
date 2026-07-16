"""ADF-W2.4/W2.5: doctor-check coverage for _fact_lineage_coverage_check."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.commands.doctor_checks.storage_checks import _fact_lineage_coverage_check


def test_no_facts_yet_is_ok(tmp_path: Path) -> None:
    check = _fact_lineage_coverage_check("fixture_prog", programs_root=tmp_path / "programs")
    assert check.status == "ok"
    assert check.metadata["total_count"] == 0


def test_no_defects_is_ok(monkeypatch, tmp_path: Path) -> None:
    from src.core.fact_lineage_coverage import LineageCoverageReport

    report = LineageCoverageReport(
        program_id="fixture_prog", total_count=5, lineaged_count=5, waived_count=0, defect_count=0,
        sample_defect_natural_keys=(), computed_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("src.core.fact_lineage_coverage.compute_lineage_coverage", lambda *a, **k: report)

    check = _fact_lineage_coverage_check("fixture_prog", programs_root=tmp_path / "programs")
    assert check.status == "ok"
    assert check.metadata["defect_count"] == 0


def test_high_defect_ratio_warns(monkeypatch, tmp_path: Path) -> None:
    from src.core.fact_lineage_coverage import LineageCoverageReport

    report = LineageCoverageReport(
        program_id="fixture_prog", total_count=10, lineaged_count=5, waived_count=0, defect_count=5,
        sample_defect_natural_keys=("risk:a", "risk:b"), computed_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("src.core.fact_lineage_coverage.compute_lineage_coverage", lambda *a, **k: report)

    check = _fact_lineage_coverage_check("fixture_prog", programs_root=tmp_path / "programs")
    assert check.status == "warn"
    assert "risk:a" in check.detail


def test_low_defect_ratio_stays_ok(monkeypatch, tmp_path: Path) -> None:
    from src.core.fact_lineage_coverage import LineageCoverageReport

    report = LineageCoverageReport(
        program_id="fixture_prog", total_count=100, lineaged_count=95, waived_count=0, defect_count=5,
        sample_defect_natural_keys=("risk:a",), computed_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
    )
    monkeypatch.setattr("src.core.fact_lineage_coverage.compute_lineage_coverage", lambda *a, **k: report)

    check = _fact_lineage_coverage_check("fixture_prog", programs_root=tmp_path / "programs")
    assert check.status == "ok"  # 5% defect ratio, below the 10% warn threshold


def test_computation_error_never_crashes_doctor(monkeypatch, tmp_path: Path) -> None:
    def _raise(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr("src.core.fact_lineage_coverage.compute_lineage_coverage", _raise)

    check = _fact_lineage_coverage_check("fixture_prog", programs_root=tmp_path / "programs")
    assert check.status == "warn"
    assert "simulated failure" in check.detail
