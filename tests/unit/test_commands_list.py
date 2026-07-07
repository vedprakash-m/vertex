from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from cli import app
from src.core.archive_store import read_archive_index
from src.core.program_fact_store import load_program_facts, project_skip_issues


runner = CliRunner()
EDITION_NAME = "acme_weekly"


def test_list_workstreams(monkeypatch) -> None:
    bundle = SimpleNamespace(
        program_context=SimpleNamespace(
            workstreams=(
                SimpleNamespace(
                    name="Acme",
                    dri_email="owner@example.com",
                    area_paths=("One\\Adventure\\Acme",),
                ),
            ),
            people=(),
        )
    )
    monkeypatch.setattr("src.commands.list.load_bundle", lambda edition: bundle)

    result = runner.invoke(app, ["list", "workstreams", "--edition", EDITION_NAME])

    assert result.exit_code == 0
    assert "Acme\towner@example.com\tOne\\Adventure\\Acme" in result.stdout


def test_list_dris_prefers_people(monkeypatch) -> None:
    bundle = SimpleNamespace(
        program_context=SimpleNamespace(
            workstreams=(),
            people=(
                SimpleNamespace(
                    display_name="Vertex Maintainer",
                    email="maintainer@example.com",
                    workstreams=("Acme", "DD on PF"),
                ),
            ),
        )
    )
    monkeypatch.setattr("src.commands.list.load_bundle", lambda edition: bundle)

    result = runner.invoke(app, ["list", "dris", "--edition", EDITION_NAME])

    assert result.exit_code == 0
    assert "Vertex Maintainer\tmaintainer@example.com\tAcme, DD on PF" in result.stdout


def test_list_commands_support_json_and_csv(monkeypatch) -> None:
    edition_bundle = SimpleNamespace(
        config=SimpleNamespace(
            edition=SimpleNamespace(
                name="acme_weekly",
                type="detailed",
                cadence="weekly",
            )
        ),
        program_context=SimpleNamespace(
            workstreams=(
                SimpleNamespace(
                    name="Acme",
                    dri_email="owner@example.com",
                    area_paths=("One\\Adventure\\Acme",),
                ),
            ),
            people=(
                SimpleNamespace(
                    display_name="Vertex Maintainer",
                    email="maintainer@example.com",
                    workstreams=("Acme",),
                ),
            ),
        ),
    )
    monkeypatch.setattr("src.commands.list.discover_report_editions", lambda: ("acme_weekly",))
    monkeypatch.setattr("src.commands.list.load_bundle", lambda edition: edition_bundle)

    editions_json = runner.invoke(app, ["list", "editions", "--format", "json"])
    workstreams_csv = runner.invoke(app, ["list", "workstreams", "--edition", EDITION_NAME, "--format", "csv"])
    dris_json = runner.invoke(app, ["list", "dris", "--edition", EDITION_NAME, "--format", "json"])

    assert editions_json.exit_code == 0
    assert '"name": "acme_weekly"' in editions_json.stdout
    assert '"type": "detailed"' in editions_json.stdout
    assert '"cadence": "weekly"' in editions_json.stdout

    assert workstreams_csv.exit_code == 0
    assert "name,dri_email,area_paths" in workstreams_csv.stdout
    assert "Acme,owner@example.com,One\\Adventure\\Acme" in workstreams_csv.stdout

    assert dris_json.exit_code == 0
    assert '"display_name": "Vertex Maintainer"' in dris_json.stdout
    assert '"email": "maintainer@example.com"' in dris_json.stdout


def test_root_skip_issue_records_next_issue(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("src.commands.skip_issue.ARCHIVE_ROOT", tmp_path / "archive")
    monkeypatch.setenv("VERTEX_DEFAULT_EDITION", EDITION_NAME)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "vertex-db"))

    result = runner.invoke(app, ["--skip-issue", "--reason", "Holiday week"])
    index = read_archive_index(EDITION_NAME, tmp_path / "archive")
    snapshot = load_program_facts("acme", as_of=datetime.now(timezone.utc), db_root=tmp_path / "vertex-db")

    assert result.exit_code == 0
    assert f"Skipped issue 001 for {EDITION_NAME}." in result.stdout
    assert len(index.issues) == 1
    assert index.issues[0].kind == "skipped"
    assert index.issues[0].reason == "Holiday week"
    assert project_skip_issues(snapshot)[0].edition_id == EDITION_NAME
    assert project_skip_issues(snapshot)[0].issue_number == 1
    assert project_skip_issues(snapshot)[0].reason == "Holiday week"


def test_root_skip_issue_honors_vertex_default_edition(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("src.commands.skip_issue.ARCHIVE_ROOT", tmp_path / "archive")
    monkeypatch.delenv("VERTEX_DEFAULT_EDITION", raising=False)
    monkeypatch.setenv("VERTEX_DEFAULT_EDITION", "fabrikam_weekly")
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "vertex-db"))

    result = runner.invoke(app, ["--skip-issue", "--reason", "Holiday week"])
    index = read_archive_index("fabrikam_weekly", tmp_path / "archive")
    snapshot = load_program_facts("fabrikam", as_of=datetime.now(timezone.utc), db_root=tmp_path / "vertex-db")

    assert result.exit_code == 0
    assert "Skipped issue 001 for fabrikam_weekly." in result.stdout
    assert len(index.issues) == 1
    assert index.issues[0].kind == "skipped"
    assert index.issues[0].reason == "Holiday week"
    assert project_skip_issues(snapshot)[0].edition_id == "fabrikam_weekly"


def test_root_skip_issue_falls_back_to_legacy_default_edition(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("src.commands.skip_issue.ARCHIVE_ROOT", tmp_path / "archive")
    monkeypatch.delenv("VERTEX_DEFAULT_EDITION", raising=False)
    monkeypatch.setenv("VERTEX_DEFAULT_EDITION", "fabrikam_weekly")
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "vertex-db"))

    result = runner.invoke(app, ["--skip-issue", "--reason", "Holiday week"])
    index = read_archive_index("fabrikam_weekly", tmp_path / "archive")
    snapshot = load_program_facts("fabrikam", as_of=datetime.now(timezone.utc), db_root=tmp_path / "vertex-db")

    assert result.exit_code == 0
    assert "Skipped issue 001 for fabrikam_weekly." in result.stdout
    assert len(index.issues) == 1
    assert index.issues[0].kind == "skipped"
    assert index.issues[0].reason == "Holiday week"
    assert project_skip_issues(snapshot)[0].issue_number == 1