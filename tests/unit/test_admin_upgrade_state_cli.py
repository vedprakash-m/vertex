from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from cli import app


runner = CliRunner()


def _stage_program(programs_root: Path, program_id: str, schema_version: str) -> Path:
    """Write a minimal ``programs/<id>/program.yaml`` and return the programs root."""
    program_dir = programs_root / program_id
    program_dir.mkdir(parents=True, exist_ok=True)
    program_path = program_dir / "program.yaml"
    program_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": schema_version,
                "program_id": program_id,
                "name": program_id.upper(),
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return program_path


def _migration_log_lines(programs_root: Path, program_id: str) -> list[str]:
    log_path = programs_root / program_id / "migration_log.jsonl"
    if not log_path.exists():
        return []
    return [line for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_upgrade_state_dry_run_reports_steps_and_writes_no_migration_log(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _stage_program(programs_root, "acme", "2.0")

    result = runner.invoke(
        app,
        ["admin", "upgrade-state", "--program", "acme", "--dry-run", "--programs-root", str(programs_root)],
    )

    assert result.exit_code == 0
    # 2.0 → 3.0 → 4.0 is two steps; the engine plans both in dry-run mode.
    assert "2.0" in result.stdout
    assert "4.0" in result.stdout
    assert "steps=2" in result.stdout
    assert "dry-run" in result.stdout
    # Dry-run must not touch disk.
    assert not (programs_root / "acme" / "migration_log.jsonl").exists()
    # program.yaml schema_version unchanged.
    rewritten = yaml.safe_load((programs_root / "acme" / "program.yaml").read_text(encoding="utf-8"))
    assert rewritten["schema_version"] == "2.0"


def test_upgrade_state_apply_walks_2_to_4_and_writes_one_log_line_per_step(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _stage_program(programs_root, "acme", "2.0")

    result = runner.invoke(
        app,
        ["admin", "upgrade-state", "--program", "acme", "--apply", "--programs-root", str(programs_root)],
    )

    assert result.exit_code == 0
    assert "2.0" in result.stdout
    assert "4.0" in result.stdout
    assert "steps=2" in result.stdout
    assert "mode=apply" in result.stdout

    # program.yaml persisted at 4.0 with the 3.0→4.0 additions.
    rewritten = yaml.safe_load((programs_root / "acme" / "program.yaml").read_text(encoding="utf-8"))
    assert rewritten["schema_version"] == "4.0"
    assert rewritten["retention_days"] == 365
    assert rewritten["fact_store_sor"] == "legacy"
    assert rewritten["decisions_corroboration_required"] is False

    # One migration_log.jsonl line per applied step (2 steps).
    log_lines = _migration_log_lines(programs_root, "acme")
    assert len(log_lines) == 2
    import json

    first = json.loads(log_lines[0])
    second = json.loads(log_lines[1])
    assert first["from_version"] == "2.0" and first["to_version"] == "3.0"
    assert second["from_version"] == "3.0" and second["to_version"] == "4.0"
    assert first["artifact"] == "program_yaml"
    assert first["operator"] == "vertex.admin"


def test_upgrade_state_is_noop_for_program_already_at_4(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _stage_program(programs_root, "acme", "4.0")

    result = runner.invoke(
        app,
        ["admin", "upgrade-state", "--program", "acme", "--apply", "--programs-root", str(programs_root)],
    )

    assert result.exit_code == 0
    assert "steps=0" in result.stdout
    assert "4.0" in result.stdout
    # No migration log rows are emitted when nothing evolves.
    assert _migration_log_lines(programs_root, "acme") == []
    # program.yaml unchanged.
    rewritten = yaml.safe_load((programs_root / "acme" / "program.yaml").read_text(encoding="utf-8"))
    assert rewritten["schema_version"] == "4.0"