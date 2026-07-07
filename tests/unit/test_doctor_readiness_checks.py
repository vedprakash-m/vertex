from __future__ import annotations

from src.commands.doctor_checks.readiness_checks import readiness_gate_settings, run_readiness_doctor


def test_readiness_gate_settings_defaults_when_program_is_missing() -> None:
    assert readiness_gate_settings(None) == (False, 7)


def test_readiness_gate_settings_uses_positive_snapshot_age_override() -> None:
    assert readiness_gate_settings({"readiness": {"gate": True, "snapshot_max_age_days": 14}}) == (True, 14)


def test_readiness_gate_settings_preserves_bool_snapshot_age_behavior() -> None:
    assert readiness_gate_settings({"readiness": {"gate": True, "snapshot_max_age_days": True}}) == (True, 1)


def test_run_readiness_doctor_fails_when_edition_is_missing(tmp_path) -> None:
    report = run_readiness_doctor(
        edition_name="missing_weekly",
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
        readiness_gate_settings_fn=lambda raw_program: (False, 7),
    )

    assert report.checks[0].label == "Readiness"
    assert report.checks[0].status == "fail"
