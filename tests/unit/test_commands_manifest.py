from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner
import yaml

from cli import app
from src.commands.confirm import _read_confirming_author, confirm_issue
from src.commands.manifest import show_manifest
from src.commands.report import generate_report_draft
from src.core.overrides_store import get_overrides_path
from tests.support.report_test_setup import disable_kusto_in_report_copy, stage_v2_report_workspace
from tests.unit.test_commands_confirm import _ack_decision_strip, _seed_high_risk_signal_coverage, _write_authored_exec_summary, _write_authored_workstream_narratives
from tests.unit.test_commands_report import _sample_items


runner = CliRunner()
EDITION_NAME = "acme_weekly"


def test_read_confirming_author_reads_vertex_alias(monkeypatch) -> None:
    monkeypatch.setenv("VERTEX_AUTHOR", "Vertex Author")

    assert _read_confirming_author() == "Vertex Author"


def test_manifest_command_reads_confirmed_manifest(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )
    _seed_high_risk_signal_coverage(
        programs_root,
        captured_at=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
    )

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=1)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    _ack_decision_strip(overrides_payload)
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    _write_authored_exec_summary(reports_root, 1)
    _write_authored_workstream_narratives(reports_root, 1, edition_name=EDITION_NAME)

    monkeypatch.setenv("VERTEX_AUTHOR", "Vertex Maintainer")
    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    assert result.exit_code == 0
    assert result.archive_paths is not None
    archived_manifest = json.loads(result.archive_paths.manifest_path.read_text(encoding="utf-8"))
    assert archived_manifest["metadata"]["confirmed_by"] == "Vertex Maintainer"
    assert archived_manifest["metadata"]["confirmed_at"]

    monkeypatch.setattr("src.commands.manifest.PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr("src.commands.manifest.ARCHIVE_ROOT", archive_root)

    command_result = runner.invoke(app, ["manifest", "--edition", EDITION_NAME, "--issue", "1"])

    assert command_result.exit_code == 0
    assert f"Manifest:       {result.manifest.manifest_id}" in command_result.stdout
    assert "Confirmed by:   Vertex Maintainer" in command_result.stdout
    assert "AI Budget:      " in command_result.stdout
    assert "AI Trace Run:   " in command_result.stdout
    assert "Quality Gates:  " in command_result.stdout
    assert "QG-8: PASS" in command_result.stdout

    json_result = runner.invoke(app, ["manifest", "--edition", EDITION_NAME, "--issue", "1", "--format", "json"])

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    expected_overall = "PASS" if all(result.manifest.qg_results.values()) else "FAIL"
    assert payload["manifest_id"] == result.manifest.manifest_id
    assert payload["issue_number"] == 1
    assert payload["edition"] == EDITION_NAME
    assert payload["confirmed_by"] == "Vertex Maintainer"
    assert isinstance(payload["ai_safety"]["enabled"], bool)
    assert payload["ai_safety"]["budget_usd"] == pytest.approx(0.5)
    assert payload["ai_safety"]["spent_usd"] == pytest.approx(result.manifest.ai_cost_usd)
    assert payload["quality_gates_overall"] == expected_overall
    assert payload["qg_results"]["QG-8"] is True

    csv_result = runner.invoke(app, ["manifest", "--edition", EDITION_NAME, "--issue", "1", "--format", "csv"])

    assert csv_result.exit_code == 0
    header, row = csv_result.stdout.strip().splitlines()
    assert header == "manifest_id,issue_number,edition,data_pulled,confirmed_by,confirmed_at,source,ai_safety,quality_gates_overall,qg_results"
    assert result.manifest.manifest_id in row
    assert ",1,acme_weekly," in row
    assert "Vertex Maintainer" in row
    assert "budget_usd" in row
    assert "within_budget" in row
    assert expected_overall in row


def test_manifest_command_surfaces_malformed_ai_safety_metadata(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    manifest_path = (tmp_path / "programs") / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["metadata"] = {"ai_safety": "broken"}
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    monkeypatch.setattr("src.commands.manifest.PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr("src.commands.manifest.ARCHIVE_ROOT", archive_root)

    human_result = runner.invoke(app, ["manifest", "--edition", EDITION_NAME, "--issue", "1"])
    json_result = runner.invoke(app, ["manifest", "--edition", EDITION_NAME, "--issue", "1", "--format", "json"])
    csv_result = runner.invoke(app, ["manifest", "--edition", EDITION_NAME, "--issue", "1", "--format", "csv"])

    assert human_result.exit_code == 0
    assert "AI Safety:      malformed metadata" in human_result.stdout

    assert json_result.exit_code == 0
    assert json.loads(json_result.stdout)["ai_safety"] == "malformed"

    assert csv_result.exit_code == 0
    assert '"malformed"' in csv_result.stdout


def test_manifest_command_tolerates_malformed_metadata_container(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    manifest_path = (tmp_path / "programs") / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["metadata"] = "broken"
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    monkeypatch.setattr("src.commands.manifest.PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr("src.commands.manifest.ARCHIVE_ROOT", archive_root)

    human_result = runner.invoke(app, ["manifest", "--edition", EDITION_NAME, "--issue", "1"])
    json_result = runner.invoke(app, ["manifest", "--edition", EDITION_NAME, "--issue", "1", "--format", "json"])

    assert human_result.exit_code == 0
    assert "AI Safety:      malformed metadata" in human_result.stdout

    assert json_result.exit_code == 0
    assert json.loads(json_result.stdout)["ai_safety"] == "malformed"


def test_manifest_command_surfaces_invalid_manifest_error(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    manifest_path = (tmp_path / "programs") / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.manifest.json"
    manifest_path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(typer.BadParameter, match=r"Manifest at .*issue_001\.manifest\.json is invalid\."):
        show_manifest(
            edition_name=EDITION_NAME,
            issue_number=1,
            programs_root=(tmp_path / "programs"),
            archive_root=archive_root,
        )

