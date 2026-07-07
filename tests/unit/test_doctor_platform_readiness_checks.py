from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.commands.doctor_checks.models import DoctorCheck, DoctorReport
from src.commands.doctor_checks.platform_readiness_checks import run_platform_readiness_doctor


def test_run_platform_readiness_doctor_builds_summary_counts(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "src.commands.doctor_checks.platform_readiness_checks.build_fleet_report",
        lambda *, programs_root: SimpleNamespace(programs=[SimpleNamespace(program_id="alpha")]),
    )
    monkeypatch.setattr(
        "src.commands.doctor_checks.platform_readiness_checks.load_platform_proof_records_by_program",
        lambda *, programs_root: {"alpha": ()},
    )
    monkeypatch.setattr(
        "src.commands.doctor_checks.platform_readiness_checks.platform_fleet_active_programs_check",
        lambda fleet_report: DoctorCheck("PR:Fleet Active Programs", "ok", "ok"),
    )
    monkeypatch.setattr(
        "src.commands.doctor_checks.platform_readiness_checks.platform_adapter_coverage_check",
        lambda fleet_report, *, programs_root: DoctorCheck("PR:Adapter Coverage", "fail", "missing"),
    )
    monkeypatch.setattr(
        "src.commands.doctor_checks.platform_readiness_checks.platform_confirmed_program_channel_health_check",
        lambda **kwargs: DoctorCheck("PR:Confirmed Program Channel Health", "warn", "warn"),
    )
    monkeypatch.setattr(
        "src.commands.doctor_checks.platform_readiness_checks.platform_required_proof_check",
        lambda definition, *, proof_records_by_program: DoctorCheck(definition.label, "ok", definition.proof_id),
    )
    monkeypatch.setattr(
        "src.commands.doctor_checks.platform_readiness_checks.platform_archetype_proof_check",
        lambda *, proof_records_by_program: DoctorCheck("PR:P6b Archetype Proofs", "ok", "ok"),
    )
    monkeypatch.setattr(
        "src.commands.doctor_checks.platform_readiness_checks.platform_s7_position_check",
        lambda *, programs_root: DoctorCheck("PR:S7 Position", "warn", "warn"),
    )

    report = run_platform_readiness_doctor(
        programs_root=tmp_path / "programs",
        reports_root=tmp_path / "reports",
        editions_root=tmp_path / "editions",
        run_channel_doctor_fn=lambda **kwargs: DoctorReport(edition="demo", checks=(DoctorCheck("Channels", "ok", "ok"),)),
    )

    summary = report.checks[0]
    assert summary.label == "Platform Readiness"
    assert summary.status == "fail"
    assert summary.metadata == {"program_count": 1, "ok_count": 6, "fail_count": 1, "warn_count": 2}
