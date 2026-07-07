from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

import cli
from src.ai.edit_learner import append_edit_patterns, build_edit_patterns


runner = CliRunner()


def test_salience_show_refreshes_and_renders_model(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    append_edit_patterns(
        "acme",
        build_edit_patterns(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            recorded_at=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
            draft_exec_summary_text="",
            confirmed_exec_summary_text="",
            draft_workstream_blurbs={"deployment": "ETA risk remains elevated and needs escalation."},
            confirmed_workstream_blurbs={"deployment": "Deployment ETA risk remains elevated and now needs escalation."},
        ),
        programs_root=programs_root,
    )

    monkeypatch.setattr("src.commands.salience.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(cli, "_stdout_supports_interactive_catchup", lambda: False)

    result = runner.invoke(cli.app, ["salience", "show", "--program", "acme"])

    assert result.exit_code == 0
    assert "Author Salience - acme" in result.stdout
    assert "deployment: weight=" in result.stdout
    assert "Cached model:" in result.stdout


def test_salience_show_no_refresh_reports_missing_cache(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    monkeypatch.setattr("src.commands.salience.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(cli, "_stdout_supports_interactive_catchup", lambda: False)

    result = runner.invoke(cli.app, ["salience", "show", "--program", "acme", "--no-refresh"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "No author salience model for acme."


def test_salience_show_refresh_honors_program_config(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        "\n".join(
            (
                "schema_version: '3.0'",
                "id: acme",
                "name: Acme",
                "salience:",
                "  min_weight: 0.4",
                "  ema_alpha: 0.3",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    append_edit_patterns(
        "acme",
        build_edit_patterns(
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            recorded_at=datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc),
            draft_exec_summary_text="",
            confirmed_exec_summary_text="",
            draft_workstream_blurbs={"deployment": "ETA risk remains elevated and needs escalation."},
            confirmed_workstream_blurbs={"deployment": "Deployment ETA risk remains elevated and now needs escalation."},
        ),
        programs_root=programs_root,
    )

    monkeypatch.setattr("src.commands.salience.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(cli, "_stdout_supports_interactive_catchup", lambda: False)

    result = runner.invoke(cli.app, ["salience", "show", "--program", "acme"])

    assert result.exit_code == 0
    assert "EMA alpha: 0.30 | Min weight: 0.40" in result.stdout