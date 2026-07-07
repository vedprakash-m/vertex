from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.commands.doctor_checks.governance_checks import assess_schema_version, run_config_governance_check


def test_assess_schema_version_warns_on_minor_drift(tmp_path: Path) -> None:
    path = tmp_path / "program.yaml"
    path.write_text('schema_version: "3.1"\n', encoding="utf-8")

    assessment = assess_schema_version(path, expected_major=3, expected_minor=0, required=True)

    assert assessment.status == "warn"
    assert assessment.version == "3.1"
    assert "expected baseline 3.0" in assessment.detail


def test_run_config_governance_check_reports_schema_assessments(tmp_path: Path) -> None:
    editions_root = tmp_path / "editions"
    programs_root = tmp_path / "programs"
    editions_root.mkdir()
    program_root = programs_root / "demo"
    program_root.mkdir(parents=True)

    (editions_root / "demo_weekly.yaml").write_text('schema_version: "2.0"\n', encoding="utf-8")
    (program_root / "program.yaml").write_text('schema_version: "3.0"\n', encoding="utf-8")
    (program_root / "readiness.yaml").write_text('schema_version: "1.1"\n', encoding="utf-8")

    resolved = SimpleNamespace(paths=SimpleNamespace(program_id="demo"))

    check = run_config_governance_check(
        edition_name="demo_weekly",
        resolved=resolved,
        editions_root=editions_root,
        programs_root=programs_root,
    )

    assert check.label == "Config Governance"
    assert check.status == "warn"
    assert "readiness:" in check.detail
    assert check.metadata is not None
    assert check.metadata["program_id"] == "demo"
    assert check.metadata["assessments"]["edition"]["status"] == "ok"
    assert check.metadata["assessments"]["program"]["status"] == "ok"
    assert check.metadata["assessments"]["readiness"]["status"] == "warn"
