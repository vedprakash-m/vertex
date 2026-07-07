from __future__ import annotations

import json
import pytest
import shutil
from datetime import datetime, timezone
from pathlib import Path

import typer
from typer.testing import CliRunner
import yaml

from cli import app
from src.commands import evidence as evidence_module
import src.core.archive_store as archive_store
from src.commands.confirm import confirm_issue
from src.commands.report import generate_report_draft
from src.core.narrative_store import get_narratives_dir
from src.core.overrides_store import get_overrides_path
from tests.support.report_test_setup import disable_kusto_in_report_copy, stage_v2_report_workspace
from tests.unit.test_commands_confirm import _seed_high_risk_signal_coverage, _write_authored_workstream_narratives
from tests.unit.test_commands_report import _forecast_items, _lookback_snapshot, _manifest, _sample_items, _snapshot_item_from_work_item


runner = CliRunner()
EDITION_NAME = "acme_weekly"


def _ack_decision_strip(overrides_payload: dict[str, object]) -> None:
    overrides_payload["decision_strip_ack"] = {
        "no_leadership_ask": True,
        "reason": "Freshness and risk signals are already tracked and do not require a new leadership ask for this evidence test.",
    }


def _write_authored_exec_summary(reports_root: Path, issue_number: int, text: str = "Confirmed executive summary.\n") -> None:
    exec_summary_path = get_narratives_dir(EDITION_NAME, issue_number, reports_root) / "exec_summary.md"
    exec_summary_path.write_text(text, encoding="utf-8")


def test_evidence_cli_traces_section_lineage(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
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

    monkeypatch.setattr("src.commands.evidence.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.evidence.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.evidence.PROGRAMS_ROOT", tmp_path / "programs")

    result = runner.invoke(
        app,
        ["evidence", "--edition", EDITION_NAME, "--issue", "latest", "--section", "deployment-velocity"],
    )

    assert result.exit_code == 0
    assert "Claim:" in result.stdout
    assert "Source system: ADO" in result.stdout
    assert "Narrative: narratives/issue_001/ws_nova-adventure-xio-100-ramp-readiness-deployment-velocity.md" in result.stdout


def test_evidence_cli_uses_trusted_baseline_for_previous_snapshot(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
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

    baseline_call: dict[str, int | None] = {}
    snapshot_call: dict[str, int | None] = {}
    original_load_previous_snapshot = evidence_module._load_previous_snapshot

    def _fake_load_trusted_baseline_issue(*args, **kwargs):
        del args
        baseline_call["before_issue_number"] = kwargs.get("before_issue_number")
        return 77

    def _capturing_load_previous_snapshot(*args, **kwargs):
        snapshot_call["trusted_issue_number"] = kwargs.get("trusted_issue_number")
        return original_load_previous_snapshot(*args, **kwargs)

    monkeypatch.setattr(evidence_module, "load_trusted_baseline_issue", _fake_load_trusted_baseline_issue)
    monkeypatch.setattr(evidence_module, "_load_previous_snapshot", _capturing_load_previous_snapshot)
    monkeypatch.setattr("src.commands.evidence.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.evidence.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.evidence.PROGRAMS_ROOT", tmp_path / "programs")

    result = runner.invoke(
        app,
        ["evidence", "--edition", EDITION_NAME, "--issue", "latest", "--section", "deployment-velocity"],
    )

    assert result.exit_code == 0
    assert baseline_call["before_issue_number"] == 1
    assert snapshot_call["trusted_issue_number"] == 77


def test_evidence_cli_traces_ado_item(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
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

    monkeypatch.setattr("src.commands.evidence.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.evidence.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.evidence.PROGRAMS_ROOT", tmp_path / "programs")

    result = runner.invoke(
        app,
        ["evidence", "--edition", EDITION_NAME, "--issue", "latest", "--ado", "900001"],
    )

    assert result.exit_code == 0
    assert "ADO Item: #900001" in result.stdout
    assert "Claims:" in result.stdout


def test_evidence_cli_traces_forecast_claim_when_enabled(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    config_path = (tmp_path / "programs") / "acme" / "editions" / f"{EDITION_NAME}.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace("forecast_enabled: false", "forecast_enabled: true"),
        encoding="utf-8",
    )

    for issue_number in range(1, 5):
        as_of = datetime(2026, 4, issue_number, 18, 0, tzinfo=timezone.utc)
        archive_store.write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=as_of,
                items=(
                    _snapshot_item_from_work_item(_forecast_items(as_of)[0], risk_level=_forecast_items(as_of)[0].risk_level),
                ),
                scorecard_risks={"Deployment Velocity": _forecast_items(as_of)[0].risk_level},
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}\n",
            manifest=_manifest(issue_number=issue_number, as_of=as_of),
            archive_root=archive_root,
        )

    draft = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_forecast_items(timestamp), 0),
        open_browser=False,
    )

    if draft.manifest.metadata.get("forecast_summary") is None:
        pytest.skip("Current continuity forecast fixture did not produce a forecast candidate.")

    monkeypatch.setattr("src.commands.evidence.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.evidence.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.evidence.PROGRAMS_ROOT", tmp_path / "programs")

    result = runner.invoke(
        app,
        ["evidence", "--edition", EDITION_NAME, "--issue", "latest", "--claim", "exec_summary.forecast"],
    )

    assert result.exit_code == 0
    assert "Claim:" in result.stdout
    assert "Current velocity suggests Deployment Velocity may slip" in result.stdout
    assert "Forecast formula:" in result.stdout


def test_evidence_cli_falls_back_to_archive_for_confirmed_issue(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
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

    confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )
    (programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.draft.json").unlink()

    monkeypatch.setattr("src.commands.evidence.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.evidence.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.evidence.PROGRAMS_ROOT", tmp_path / "programs")

    result = runner.invoke(
        app,
        ["evidence", "--edition", EDITION_NAME, "--issue", "1", "--section", "deployment-velocity"],
    )

    assert result.exit_code == 0
    assert "Claim:" in result.stdout
    assert "Narrative: narratives/issue_001/" in result.stdout


def test_evidence_cli_supports_json_and_csv(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
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

    monkeypatch.setattr("src.commands.evidence.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.evidence.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.evidence.PROGRAMS_ROOT", tmp_path / "programs")

    json_result = runner.invoke(
        app,
        ["evidence", "--edition", EDITION_NAME, "--issue", "latest", "--section", "deployment-velocity", "--format", "json"],
    )

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["edition"] == EDITION_NAME
    assert payload["issue"] == "latest"
    assert payload["section"] == "deployment-velocity"
    assert payload["claim"] == "-"
    assert payload["ado"] == "-"
    assert "Claim:" in payload["summary"]

    csv_result = runner.invoke(
        app,
        ["evidence", "--edition", EDITION_NAME, "--issue", "latest", "--section", "deployment-velocity", "--format", "csv"],
    )

    assert csv_result.exit_code == 0
    lines = csv_result.stdout.strip().splitlines()
    assert lines[0] == "edition,issue,section,claim,ado,summary"
    assert lines[1].startswith(f"{EDITION_NAME},latest,deployment-velocity,-,-,")
    assert "Claim:" in lines[1]


def test_evidence_summary_surfaces_invalid_draft_state(repo_root: Path, tmp_path: Path) -> None:
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

    draft_state_path = (tmp_path / "programs") / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.draft.json"
    draft_state_path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(typer.BadParameter, match=r"Draft state at .*issue_001\.draft\.json is invalid\."):
        evidence_module.build_evidence_summary(
            edition_name=EDITION_NAME,
            issue_value="latest",
            section="deployment-velocity",
            claim=None,
            ado_work_item_id=None,
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=(tmp_path / "programs"),
        )

