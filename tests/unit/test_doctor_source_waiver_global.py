from __future__ import annotations

from pathlib import Path

from src.commands.doctor import run_doctor


def test_source_waiver_audit_is_fleet_scoped_and_needs_no_edition(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "alpha").mkdir(parents=True)
    (programs_root / "alpha" / "program.yaml").write_text("id: alpha\n", encoding="utf-8")
    (programs_root / "beta").mkdir()
    (programs_root / "beta" / "program.yaml").write_text("id: beta\n", encoding="utf-8")

    report = run_doctor(
        source_waivers=True,
        reports_root=tmp_path / "reports",
        programs_root=programs_root,
    )

    assert report.edition == "fleet"
    assert report.failures == 0
    assert report.checks[0].metadata is not None
    assert report.checks[0].metadata["program_count"] == 2
