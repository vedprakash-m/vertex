from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone

from typer.testing import CliRunner

import cli
from src.commands.calibration import generate_calibration_report, render_calibration_report
from src.core.claim_tracker import append_claim_entry, append_claim_status_update
from src.core.models_v2 import ClaimEntry, ClaimStatusUpdate


runner = CliRunner()


def test_generate_calibration_report_matches_golden_fixture(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_claims(programs_root, "acme")
    _patch_calibration_loaders(monkeypatch)

    artifacts = generate_calibration_report(
        "acme",
        since="2026-W01",
        dry_run=True,
        programs_root=programs_root,
    )
    rendered = render_calibration_report(artifacts.report, artifacts.modifier)
    fixture_path = Path(__file__).resolve().parents[1] / "golden" / "calibration_report.txt"

    assert rendered == fixture_path.read_text(encoding="utf-8").rstrip("\n")


def test_calibration_report_command_dry_run_renders_without_writing_feedback(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_claims(programs_root, "acme")
    _patch_calibration_loaders(monkeypatch)
    monkeypatch.setattr("src.commands.calibration.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(cli, "_stdout_supports_interactive_catchup", lambda: False)

    result = runner.invoke(cli.app, ["calibration", "report", "--program", "acme", "--since", "2026-W01", "--dry-run"])

    assert result.exit_code == 0
    assert "Claim Accuracy (14 weeks, 6 terminal claims)" in result.stdout
    assert "Dry-run: skipped writing forecast_calibration.yaml." in result.stdout
    assert not any(programs_root.rglob("forecast_calibration.yaml"))


def _seed_claims(programs_root: Path, program_id: str) -> None:
    for claim_id, workstream_id, owner_alias, claim_date, status in (
        ("old-1", "deployment", "alex", date(2026, 2, 1), "contradicted"),
        ("old-2", "deployment", "alex", date(2026, 2, 8), "stale"),
        ("old-3", "repair", "jamie", date(2026, 2, 15), "met"),
        ("new-1", "deployment", "alex", date(2026, 4, 20), "met"),
        ("new-2", "repair", "jamie", date(2026, 4, 27), "met"),
        ("new-3", "repair", "jamie", date(2026, 5, 3), "met"),
    ):
        append_claim_entry(
            ClaimEntry(
                id=claim_id,
                program_id=program_id,
                edition_id="acme_weekly",
                issue_number=78,
                workstream_id=workstream_id,
                text=f"Claim {claim_id}",
                entity_refs=(),
                claim_date=claim_date,
                owner_alias=owner_alias,
                due_date=None,
                status="open",
            ),
            programs_root=programs_root,
        )
        if status != "open":
            append_claim_status_update(
                program_id,
                ClaimStatusUpdate(
                    claim_id=claim_id,
                    new_status=status,
                    updated_at=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
                    updated_by="tester",
                ),
                programs_root=programs_root,
            )


def _patch_calibration_loaders(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.commands.calibration.gather_helpers._load_program_context",
        lambda program_id, programs_root: (SimpleNamespace(ado=None), ()),
    )
    monkeypatch.setattr(
        "src.commands.calibration.gather_helpers._load_ado_items_via_uil",
        lambda program, workstreams, as_of, **_: ((), (), 0),
    )
    monkeypatch.setattr(
        "src.commands.calibration._utc_now",
        lambda: datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
    )