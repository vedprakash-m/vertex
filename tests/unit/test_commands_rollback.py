from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.core.checkpoint_store import create_checkpoint_snapshot
from tests.support.report_test_setup import stage_v2_report_workspace


EDITION_NAME = "acme_weekly"
runner = CliRunner()


def test_rollback_cli_fails_loud_when_no_checkpoints(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    import shutil
    shutil.rmtree(programs_root / "acme" / "checkpoints", ignore_errors=True)

    result = runner.invoke(
        app,
        ["rollback", "--edition", EDITION_NAME, "--programs-root", str(programs_root)],
    )

    assert result.exit_code == 1
    assert "No checkpoints found for program 'acme'." in result.output
    assert "vertex confirm" in result.output


def test_rollback_cli_dry_run_lists_restore_paths(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    chronicle_path = programs_root / "acme" / "chronicle.jsonl"
    chronicle_path.write_text('{"event":"baseline"}\n', encoding="utf-8")
    checkpoint_path = create_checkpoint_snapshot("acme", 1, programs_root=programs_root)

    result = runner.invoke(
        app,
        [
            "rollback",
            "--edition",
            EDITION_NAME,
            "--to",
            checkpoint_path.name,
            "--dry-run",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0
    assert f"Dry-run: would restore from {checkpoint_path}" in result.output
    assert "chronicle.jsonl" in result.output


def test_rollback_cli_restores_checkpointed_file(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    chronicle_path = programs_root / "acme" / "chronicle.jsonl"
    chronicle_path.write_text('{"event":"baseline"}\n', encoding="utf-8")
    checkpoint_path = create_checkpoint_snapshot("acme", 1, programs_root=programs_root)
    chronicle_path.write_text('{"event":"mutated"}\n', encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "rollback",
            "--edition",
            EDITION_NAME,
            "--to",
            checkpoint_path.name,
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0
    assert f"Restoring from {checkpoint_path.name}:" in result.output
    assert "chronicle.jsonl" in result.output
    assert chronicle_path.read_text(encoding="utf-8") == '{"event":"baseline"}\n'


def test_rollback_cli_drill_records_s7a_proof_and_does_not_mutate_live(
    repo_root: Path, tmp_path: Path
) -> None:
    """Phase 6 §22 Step 10: `vertex rollback --drill` runs the rollback
    in a temporary sandbox, verifies the post-rollback state is
    queryable, and records proof `s7a_rollback_drill` in
    `platform_proof_log.yaml`. The live program state is unchanged."""
    from src.core.platform_proof_log_store import load_platform_proof_records

    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    chronicle_path = programs_root / "acme" / "chronicle.jsonl"
    chronicle_path.write_text('{"event":"baseline"}\n', encoding="utf-8")
    checkpoint_path = create_checkpoint_snapshot("acme", 1, programs_root=programs_root)
    # Mutate the live program so we can verify the drill did NOT revert it.
    chronicle_path.write_text('{"event":"mutated_post_drill"}\n', encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "rollback",
            "--edition",
            EDITION_NAME,
            "--to",
            checkpoint_path.name,
            "--drill",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Rollback drill passed" in result.output
    assert "Recorded proof s7a_rollback_drill (passed)" in result.output
    # Live program is unchanged.
    assert chronicle_path.read_text(encoding="utf-8") == '{"event":"mutated_post_drill"}\n'
    # Proof was recorded.
    records = load_platform_proof_records("acme", programs_root=programs_root)
    s7a = [r for r in records if r.proof_id == "s7a_rollback_drill"]
    assert len(s7a) == 1
    assert s7a[0].status == "passed"
    assert s7a[0].program_id == "acme"
    assert s7a[0].no_code_changes is True
    assert s7a[0].edition == EDITION_NAME
    assert f"checkpoint={checkpoint_path.name}" in (s7a[0].notes or "")


def test_rollback_cli_drill_defaults_to_newest_checkpoint(
    repo_root: Path, tmp_path: Path
) -> None:
    """When --drill is passed without --to, the drill uses the newest
    checkpoint (the first in the sorted list, which is reverse-chrono)."""
    from src.core.platform_proof_log_store import load_platform_proof_records

    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    chronicle_path = programs_root / "acme" / "chronicle.jsonl"
    chronicle_path.write_text('{"event":"baseline"}\n', encoding="utf-8")
    checkpoint_path = create_checkpoint_snapshot("acme", 1, programs_root=programs_root)

    result = runner.invoke(
        app,
        [
            "rollback",
            "--edition",
            EDITION_NAME,
            "--drill",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0, result.output
    records = load_platform_proof_records("acme", programs_root=programs_root)
    s7a = [r for r in records if r.proof_id == "s7a_rollback_drill"]
    assert len(s7a) == 1
    assert s7a[0].status == "passed"
    assert f"checkpoint={checkpoint_path.name}" in (s7a[0].notes or "")


def test_rollback_cli_drill_fails_when_no_checkpoints(
    repo_root: Path, tmp_path: Path
) -> None:
    """--drill still requires at least one checkpoint; without one, exit
    code 1 with a clear error message."""
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    result = runner.invoke(
        app,
        [
            "rollback",
            "--edition",
            EDITION_NAME,
            "--drill",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 1
    assert "No checkpoints found" in result.output
