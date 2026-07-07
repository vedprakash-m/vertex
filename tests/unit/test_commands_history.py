from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.core.archive_store import ConfirmedIssueArchivePaths, write_confirmed_issue, write_skipped_issue
from src.core.incident_journal_store import append_incident_entry
from src.core.models import Confidence
from src.core.models import ConfirmedDimension, EditionType, RunManifest, RiskLevel, Snapshot, SnapshotItem
from src.core.models_v2 import IncidentEntry


runner = CliRunner()
EDITION_NAME = "acme_weekly"


def test_history_lists_recent_issues(monkeypatch, tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    _write_confirmed_issue(archive_root, issue_number=1, markdown_body="# Issue 001\nAlpha status\n")
    _write_confirmed_issue(
        archive_root,
        issue_number=2,
        markdown_body="# Issue 002\nBeta status\n",
        freshness_summary={"blocks": 1, "warns": 2, "infos": 0},
        qg_results={"QG-1": False, "QG-4": True},
    )
    write_skipped_issue(EDITION_NAME, 3, "Holiday week", archive_root=archive_root)

    monkeypatch.setattr("src.commands.history.ARCHIVE_ROOT", archive_root)

    result = runner.invoke(app, ["history", "--edition", EDITION_NAME, "--last", "2"])

    assert result.exit_code == 0
    assert "003\t" in result.stdout
    assert "skipped\t-\t-\t-\tHoliday week" in result.stdout
    assert "002\t2026-05-05\tconfirmed\tdetailed\tb1/w2/i0\tfail:QG-1\t-" in result.stdout


def test_history_issue_shows_archived_markdown(monkeypatch, tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    _write_confirmed_issue(archive_root, issue_number=7, markdown_body="# Issue 007\nDeployment readiness\n")

    monkeypatch.setattr("src.commands.history.ARCHIVE_ROOT", archive_root)

    result = runner.invoke(app, ["history", "--edition", EDITION_NAME, "--issue", "7"])

    assert result.exit_code == 0
    assert "Issue 007" in result.stdout
    assert "Edition type: detailed" in result.stdout
    assert "Deployment readiness" in result.stdout


def test_history_search_scans_archived_markdown(monkeypatch, tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    _write_confirmed_issue(archive_root, issue_number=4, markdown_body="# Issue 004\nSCHIE gap remains open\n")
    _write_confirmed_issue(archive_root, issue_number=5, markdown_body="# Issue 005\nNo material change\n")

    monkeypatch.setattr("src.commands.history.ARCHIVE_ROOT", archive_root)

    result = runner.invoke(app, ["history", "--edition", EDITION_NAME, "--search", "SCHIE"])

    assert result.exit_code == 0
    assert "Issue 004\tL2\tSCHIE gap remains open" in result.stdout
    assert "Issue 005" not in result.stdout


def test_history_semantic_search_returns_ranked_archive_matches(monkeypatch, tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    _write_confirmed_issue(
        archive_root,
        issue_number=6,
        markdown_body="# Issue 006\nUD chunking latency regressed again and remains the gating risk for the week.\n",
    )
    _write_confirmed_issue(
        archive_root,
        issue_number=7,
        markdown_body="# Issue 007\nRepair follow-through improved with no meaningful chunking regression.\n",
    )

    monkeypatch.setattr("src.commands.history.ARCHIVE_ROOT", archive_root)

    result = runner.invoke(app, ["history", "--edition", EDITION_NAME, "--semantic", "UD chunking regression"])

    assert result.exit_code == 0
    first_line = result.stdout.strip().splitlines()[0]
    assert first_line.startswith("Issue 006\t2026-05-05\tmedium\tUD chunking latency regressed again")


def test_history_semantic_search_supports_json_and_csv_formats(monkeypatch, tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    _write_confirmed_issue(
        archive_root,
        issue_number=12,
        markdown_body="# Issue 012\nFleet capacity alert is driving the current risk posture.\n",
    )

    monkeypatch.setattr("src.commands.history.ARCHIVE_ROOT", archive_root)

    json_result = runner.invoke(
        app,
        ["history", "--edition", EDITION_NAME, "--semantic", "fleet capacity risk", "--format", "json"],
    )

    assert json_result.exit_code == 0
    json_payload = json.loads(json_result.stdout)
    assert json_payload["matches"][0]["issue_number"] == 12
    assert json_payload["matches"][0]["reference"] == "Issue 012"
    assert json_payload["matches"][0]["source_type"] == "narrative"

    csv_result = runner.invoke(
        app,
        ["history", "--edition", EDITION_NAME, "--semantic", "fleet capacity risk", "--format", "csv"],
    )

    assert csv_result.exit_code == 0
    csv_lines = csv_result.stdout.strip().splitlines()
    assert csv_lines[0] == "issue_number,reference,generated_at,source_type,risk_level,score,excerpt"
    assert csv_lines[1].startswith("12,Issue 012,2026-05-05,narrative,medium,")


def test_history_semantic_search_surfaces_incident_journal_matches(monkeypatch, tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    _write_edition_scaffold(tmp_path, program_id="acme")
    _write_confirmed_issue(
        archive_root,
        issue_number=14,
        markdown_body="# Issue 014\nArchive note about delivery stability.\n",
    )
    append_incident_entry(
        IncidentEntry(
            program_id="acme",
            incident_id="7654321",
            signal_id="sig-7654321",
            observed_at=datetime(2026, 5, 6, 8, 0, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 5, 6, 8, 15, tzinfo=timezone.utc),
            belief_change_summary="Bridge outage forced rollback sequencing and revealed hidden runtime coupling.",
            workstream_id="runtime",
            owning_team="Acme Runtime",
            severity=2,
            linked_work_item_ids=(9001,),
            ado_entity_refs=("wi:9001",),
            confidence=Confidence.HIGH,
        ),
        programs_root=tmp_path / "programs",
    )

    monkeypatch.setattr("src.commands.history.ARCHIVE_ROOT", archive_root)

    result = runner.invoke(app, ["history", "--edition", EDITION_NAME, "--semantic", "rollback runtime coupling"])

    assert result.exit_code == 0
    first_line = result.stdout.strip().splitlines()[0]
    assert first_line.startswith("IcM 7654321\t2026-05-06\thigh\tIcM 7654321. sev 2. Acme Runtime")


def test_history_diff_shows_markdown_changes(monkeypatch, tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    _write_confirmed_issue(archive_root, issue_number=10, markdown_body="# Issue 010\nAlpha risk\n")
    _write_confirmed_issue(archive_root, issue_number=11, markdown_body="# Issue 011\nBeta risk\n")

    monkeypatch.setattr("src.commands.history.ARCHIVE_ROOT", archive_root)

    result = runner.invoke(app, ["history", "--edition", EDITION_NAME, "--diff", "10", "11"])

    assert result.exit_code == 0
    assert "--- issue_010.md" in result.stdout
    assert "+++ issue_011.md" in result.stdout
    assert "-# Issue 010" in result.stdout
    assert "+# Issue 011" in result.stdout


def test_history_supports_json_and_csv_formats(monkeypatch, tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    _write_confirmed_issue(
        archive_root,
        issue_number=8,
        markdown_body="# Issue 008\nEscalation note\n",
        freshness_summary={"blocks": 2, "warns": 1, "infos": 0},
        qg_results={"QG-1": False, "QG-8": True},
    )

    monkeypatch.setattr("src.commands.history.ARCHIVE_ROOT", archive_root)

    list_json = runner.invoke(app, ["history", "--edition", EDITION_NAME, "--format", "json"])

    assert list_json.exit_code == 0
    list_payload = json.loads(list_json.stdout)
    assert list_payload[0]["issue_number"] == 8
    assert list_payload[0]["kind"] == "confirmed"
    assert list_payload[0]["freshness_summary"] == "b2/w1/i0"
    assert list_payload[0]["qg_status"] == "fail:QG-1"

    list_csv = runner.invoke(app, ["history", "--edition", EDITION_NAME, "--format", "csv"])

    assert list_csv.exit_code == 0
    csv_lines = list_csv.stdout.strip().splitlines()
    assert csv_lines[0] == "issue_number,generated_at,kind,edition_type,freshness_summary,qg_status,note"
    assert "8,2026-05-05,confirmed,detailed,b2/w1/i0,fail:QG-1,-" in csv_lines[1]

    issue_json = runner.invoke(app, ["history", "--edition", EDITION_NAME, "--issue", "8", "--format", "json"])

    assert issue_json.exit_code == 0
    issue_payload = json.loads(issue_json.stdout)
    assert issue_payload["issue_number"] == 8
    assert issue_payload["edition_type"] == "detailed"
    assert "Escalation note" in issue_payload["markdown_body"]

    search_csv = runner.invoke(app, ["history", "--edition", EDITION_NAME, "--search", "Escalation", "--format", "csv"])

    assert search_csv.exit_code == 0
    search_lines = search_csv.stdout.strip().splitlines()
    assert search_lines[0] == "issue_number,line_number,line"
    assert search_lines[1] == "8,2,Escalation note"


def test_history_list_tolerates_malformed_archived_manifest(monkeypatch, tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    archive_paths = _write_confirmed_issue(
        archive_root,
        issue_number=9,
        markdown_body="# Issue 009\nGamma status\n",
        freshness_summary={"blocks": 1, "warns": 1, "infos": 0},
        qg_results={"QG-1": False, "QG-4": True},
    )
    archive_paths.manifest_path.write_text("{malformed", encoding="utf-8")

    monkeypatch.setattr("src.commands.history.ARCHIVE_ROOT", archive_root)

    result = runner.invoke(app, ["history", "--edition", EDITION_NAME])

    assert result.exit_code == 0
    assert "009\t2026-05-05\tconfirmed\tdetailed\t-\t-\t-" in result.stdout


def _write_confirmed_issue(
    archive_root: Path,
    *,
    issue_number: int,
    markdown_body: str,
    freshness_summary: dict[str, int] | None = None,
    qg_results: dict[str, bool] | None = None,
) -> ConfirmedIssueArchivePaths:
    as_of = datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc)
    return write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=issue_number,
        snapshot=Snapshot(
            issue_number=issue_number,
            generated_at=as_of,
            ado_data_as_of=as_of,
            edition_type=EditionType.DETAILED,
            items=(
                SnapshotItem(
                    id=issue_number,
                    type="Feature",
                    title=f"Issue {issue_number}",
                    state="Active",
                    assigned_to="Vertex Maintainer",
                    area_path="One\\Adventure\\Acme",
                    target_date=date(2026, 5, 12),
                    risk_level=RiskLevel.MEDIUM,
                    tags=["acme"],
                ),
            ),
            scorecards=(
                ConfirmedDimension(
                    scorecard_name="Acme Readiness",
                    name="Deployment Velocity",
                    risk=RiskLevel.MEDIUM,
                    prior_risk=RiskLevel.LOW,
                    item_count=1,
                    ado_query_url="https://dev.azure.com/your-org/One/_queries/query-id",
                ),
            ),
        ),
        html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
        markdown_body=markdown_body,
        manifest=RunManifest(
            manifest_id=f"manifest-{issue_number}",
            issue_number=issue_number,
            edition=EDITION_NAME,
            started_at=as_of,
            ended_at=as_of,
            config_hash="config",
            snapshot_hash="snapshot",
            html_hash="html",
            md_hash="md",
            ado_calls=1,
            ai_calls=0,
            ai_cost_usd=0.0,
            freshness_summary=dict(freshness_summary or {"blocks": 0, "warns": 0, "infos": 0}),
            qg_results=dict(qg_results or {"QG-4": True, "QG-5": True, "QG-6": True, "QG-8": True}),
            git_sha=None,
        ),
        archive_root=archive_root,
    )


def _write_edition_scaffold(root: Path, *, program_id: str) -> None:
    editions_dir = root / "editions"
    programs_dir = root / "programs" / program_id
    editions_dir.mkdir(parents=True, exist_ok=True)
    programs_dir.mkdir(parents=True, exist_ok=True)
    (editions_dir / f"{EDITION_NAME}.yaml").write_text(
        "\n".join(
            (
                f"id: {EDITION_NAME}",
                f"program_id: {program_id}",
            )
        )
        + "\n",
        encoding="utf-8",
    )