from __future__ import annotations

from src.commands.doctor_checks.kusto_checks import run_kusto_doctor


def test_run_kusto_doctor_fails_when_edition_is_missing(tmp_path) -> None:
    report = run_kusto_doctor(
        edition_name="missing_weekly",
        editions_root=tmp_path / "editions",
        programs_root=tmp_path / "programs",
        kusto_probe=None,
        live_kusto_probe_fn=lambda: (lambda query: None),
    )

    assert report.checks[0].label == "Kusto Queries"
    assert report.checks[0].status == "fail"
