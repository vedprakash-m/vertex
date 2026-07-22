from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml
from typer.testing import CliRunner

from cli import app
from src.core.gather_run_manifest import get_legacy_cutoff_at


runner = CliRunner()


def _stage_program(programs_root: Path, program_id: str) -> Path:
    """Write a minimal ``programs/<id>/program.yaml`` and return the programs root."""
    program_dir = programs_root / program_id
    program_dir.mkdir(parents=True, exist_ok=True)
    program_path = program_dir / "program.yaml"
    program_path.write_text(
        yaml.safe_dump({"id": program_id, "name": program_id.upper()}, sort_keys=False),
        encoding="utf-8",
    )
    return program_path


def test_bootstrap_legacy_cutoff_creates_manifest_at_given_timestamp(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _stage_program(programs_root, "acme")

    result = runner.invoke(
        app,
        [
            "admin",
            "bootstrap-legacy-cutoff",
            "--program",
            "acme",
            "--at",
            "2026-01-01T00:00:00+00:00",
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert "Bootstrapped legacy-cutoff manifest for acme" in result.stdout
    assert get_legacy_cutoff_at("acme", programs_root=programs_root) == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_bootstrap_legacy_cutoff_is_idempotent(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _stage_program(programs_root, "acme")

    first = runner.invoke(
        app,
        ["admin", "bootstrap-legacy-cutoff", "--program", "acme", "--at", "2026-01-01T00:00:00+00:00", "--programs-root", str(programs_root)],
    )
    assert first.exit_code == 0

    second = runner.invoke(
        app,
        # A different --at is passed on the second call but must be ignored: the
        # program is already bootstrapped, so the original cutoff sticks.
        ["admin", "bootstrap-legacy-cutoff", "--program", "acme", "--at", "2027-06-01T00:00:00+00:00", "--programs-root", str(programs_root)],
    )

    assert second.exit_code == 0, second.stdout
    assert "already exists" in second.stdout
    assert get_legacy_cutoff_at("acme", programs_root=programs_root) == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_bootstrap_legacy_cutoff_defaults_at_to_now(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _stage_program(programs_root, "acme")

    before = datetime.now(timezone.utc)
    result = runner.invoke(
        app,
        ["admin", "bootstrap-legacy-cutoff", "--program", "acme", "--programs-root", str(programs_root)],
    )
    after = datetime.now(timezone.utc)

    assert result.exit_code == 0, result.stdout
    cutoff = get_legacy_cutoff_at("acme", programs_root=programs_root)
    assert cutoff is not None
    assert before <= cutoff <= after


def test_bootstrap_legacy_cutoff_rejects_unknown_program(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    result = runner.invoke(
        app,
        ["admin", "bootstrap-legacy-cutoff", "--program", "does-not-exist", "--programs-root", str(programs_root)],
    )

    assert result.exit_code != 0
    assert get_legacy_cutoff_at("does-not-exist", programs_root=programs_root) is None


def test_bootstrap_legacy_cutoff_rejects_invalid_timestamp(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _stage_program(programs_root, "acme")

    result = runner.invoke(
        app,
        ["admin", "bootstrap-legacy-cutoff", "--program", "acme", "--at", "not-a-timestamp", "--programs-root", str(programs_root)],
    )

    assert result.exit_code != 0
    assert get_legacy_cutoff_at("acme", programs_root=programs_root) is None
