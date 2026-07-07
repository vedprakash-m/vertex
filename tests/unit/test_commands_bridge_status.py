from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.commands.bridge_status import build_bridge_status_report
from src.core.archive_store import ConfirmedIssueArchivePaths, write_confirmed_issue
from src.core.models import ConfirmedDimension, EditionType, RiskLevel, RunManifest, Snapshot, SnapshotItem
from src.core.trusted_baseline_store import advance_trusted_baseline, load_trusted_baseline
from tests.support.report_test_setup import stage_v2_report_workspace


runner = CliRunner()
EDITION_NAME = "acme_weekly"


def test_build_bridge_status_report_marks_missing_historical_readiness_unavailable(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    editions_root = reports_root.parent / "editions"
    programs_root = reports_root.parent / "programs"

    advance_trusted_baseline(
        EDITION_NAME,
        77,
        established_at=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=editions_root,
        programs_root=programs_root,
    )
    _write_bridge_issue(tmp_path, archive_root, issue_number=77, source_issue=None, readiness_score=90)
    _write_bridge_issue(tmp_path, archive_root, issue_number=78, source_issue=77, readiness_score=None)
    _write_bridge_issue(tmp_path, archive_root, issue_number=79, source_issue=78, readiness_score=None)

    report = build_bridge_status_report(
        EDITION_NAME,
        editions_root=editions_root,
        programs_root=programs_root,
        archive_root=archive_root,
    )

    assert report.trusted_issue_number == 77
    assert report.eligible_issue_numbers == (78, 79)
    assert report.criteria[0].status == "pending"
    assert report.criteria[4].status == "unavailable"
    assert any("Draft readiness metadata missing for Issue 078." == item for item in report.data_limitations)
    assert any("Draft readiness metadata missing for Issue 079." == item for item in report.data_limitations)


def test_bridge_status_cli_graduate_marks_trusted_baseline(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    editions_root = reports_root.parent / "editions"
    programs_root = reports_root.parent / "programs"

    advance_trusted_baseline(
        EDITION_NAME,
        77,
        established_at=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=editions_root,
        programs_root=programs_root,
    )
    _write_bridge_issue(tmp_path, archive_root, issue_number=77, source_issue=None, readiness_score=90)
    for issue_number in range(78, 83):
        _write_bridge_issue(
            tmp_path,
            archive_root,
            issue_number=issue_number,
            source_issue=(issue_number - 1),
            readiness_score=92,
        )

    monkeypatch.setattr("src.commands.bridge_status.EDITIONS_ROOT", editions_root)
    monkeypatch.setattr("src.commands.bridge_status.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.bridge_status.ARCHIVE_ROOT", archive_root)

    result = runner.invoke(
        app,
        ["bridge-status", "--edition", EDITION_NAME, "--graduate", "--yes", "--format", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["bridge_graduated"] is True
    assert payload["graduation_issue"] == 82
    assert payload["graduation_ready"] is False
    assert payload["criteria"][0]["status"] == "passed"
    assert payload["criteria"][4]["status"] == "passed"

    trusted_baseline = load_trusted_baseline(
        EDITION_NAME,
        editions_root=editions_root,
        programs_root=programs_root,
    )

    assert trusted_baseline is not None
    assert trusted_baseline.bridge_graduated is True
    assert trusted_baseline.graduation_issue == 82
    assert trusted_baseline.history[-1].action == "graduated"


def test_build_bridge_status_report_flags_passive_acceptance_risk(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    editions_root = reports_root.parent / "editions"
    programs_root = reports_root.parent / "programs"

    advance_trusted_baseline(
        EDITION_NAME,
        77,
        established_at=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=editions_root,
        programs_root=programs_root,
    )
    _write_bridge_issue(tmp_path, archive_root, issue_number=77, source_issue=None, readiness_score=90)
    _write_bridge_issue(tmp_path, archive_root, issue_number=78, source_issue=77, readiness_score=60)
    _write_bridge_issue(tmp_path, archive_root, issue_number=79, source_issue=78, readiness_score=92)
    _write_bridge_issue(tmp_path, archive_root, issue_number=80, source_issue=79, readiness_score=92)
    _write_bridge_issue(tmp_path, archive_root, issue_number=81, source_issue=80, readiness_score=92)
    _write_bridge_issue(tmp_path, archive_root, issue_number=82, source_issue=81, readiness_score=92)

    report = build_bridge_status_report(
        EDITION_NAME,
        editions_root=editions_root,
        programs_root=programs_root,
        archive_root=archive_root,
    )

    assert any(
        "Possible passive acceptance risk" in limitation and "Issue 078" in limitation
        for limitation in report.data_limitations
    )


def test_build_bridge_status_report_tolerates_malformed_archived_manifest(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    editions_root = reports_root.parent / "editions"
    programs_root = reports_root.parent / "programs"

    advance_trusted_baseline(
        EDITION_NAME,
        77,
        established_at=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=editions_root,
        programs_root=programs_root,
    )
    _write_bridge_issue(tmp_path, archive_root, issue_number=77, source_issue=None, readiness_score=90)
    archived_paths = _write_bridge_issue(tmp_path, archive_root, issue_number=78, source_issue=77, readiness_score=92)

    manifest_path = archived_paths.manifest_path
    manifest_path.write_text("{malformed", encoding="utf-8")

    report = build_bridge_status_report(
        EDITION_NAME,
        editions_root=editions_root,
        programs_root=programs_root,
        archive_root=archive_root,
    )

    assert report.eligible_issue_numbers == (78,)
    assert report.criteria[4].status == "unavailable"
    assert any("Archived manifest malformed for Issue 078." == item for item in report.data_limitations)


def test_bridge_status_cli_export_metrics_writes_json_file(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    editions_root = reports_root.parent / "editions"
    programs_root = reports_root.parent / "programs"

    advance_trusted_baseline(
        EDITION_NAME,
        77,
        established_at=datetime(2026, 5, 13, 8, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=editions_root,
        programs_root=programs_root,
    )
    _write_bridge_issue(tmp_path, archive_root, issue_number=77, source_issue=None, readiness_score=90)

    monkeypatch.setattr("src.commands.bridge_status.EDITIONS_ROOT", editions_root)
    monkeypatch.setattr("src.commands.bridge_status.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.bridge_status.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.bridge_status.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["bridge-status", "--edition", EDITION_NAME, "--export-metrics"])

    assert result.exit_code == 0
    metrics_path = programs_root / "acme" / "publications" / EDITION_NAME / "bridge_metrics.json"
    assert metrics_path.exists()
    payload = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert payload["edition"] == EDITION_NAME
    assert "criteria" in payload
    assert "bridge_graduated" in payload


def _write_bridge_issue(
    tmp_path: Path,
    archive_root: Path,
    *,
    issue_number: int,
    source_issue: int | None,
    readiness_score: int | None,
) -> ConfirmedIssueArchivePaths:
    narratives_dir = tmp_path / f"narratives_{issue_number:03d}"
    narratives_dir.mkdir(parents=True, exist_ok=True)
    narratives_dir.joinpath("ws_deployment_readiness.md").write_text(
        _narrative_text(issue_number),
        encoding="utf-8",
    )

    continuation_contract_path = None
    if source_issue is not None:
        continuation_contract_path = tmp_path / f"issue_{issue_number:03d}.continuation_contract.json"
        continuation_contract_path.write_text(
            json.dumps(_continuation_contract_payload(issue_number=issue_number, source_issue=source_issue), indent=2),
            encoding="utf-8",
        )

    return write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=issue_number,
        snapshot=_snapshot(issue_number),
        html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
        markdown_body=f"# Issue {issue_number:03d}\n",
        manifest=_manifest(issue_number, readiness_score),
        narratives_source_dir=narratives_dir,
        continuation_contract_source=continuation_contract_path,
        archive_root=archive_root,
    )


def _snapshot(issue_number: int) -> Snapshot:
    return Snapshot(
        issue_number=issue_number,
        generated_at=datetime(2026, 5, issue_number - 60, 9, 0, tzinfo=timezone.utc),
        ado_data_as_of=datetime(2026, 5, issue_number - 60, 8, 30, tzinfo=timezone.utc),
        edition_type=EditionType.DETAILED,
        items=(
            SnapshotItem(
                id=900000 + issue_number,
                type="Feature",
                title=f"Deployment readiness {issue_number}",
                state="Active",
                assigned_to="Vertex Maintainer",
                area_path="One\\Adventure\\Acme",
                target_date=date(2026, 6, 30),
                risk_level=RiskLevel.LOW,
                tags=["acme"],
            ),
        ),
        scorecards=(
            ConfirmedDimension(
                scorecard_name="Acme Readiness",
                name="Deployment Velocity",
                risk=RiskLevel.LOW,
                prior_risk=RiskLevel.LOW,
                item_count=1,
                ado_query_url="https://dev.azure.com/your-org/One/_queries/query-id",
            ),
        ),
    )


def _manifest(issue_number: int, readiness_score: int | None) -> RunManifest:
    metadata = {}
    if readiness_score is not None:
        metadata["draft_readiness"] = {"score": readiness_score, "summary": f"Draft readiness: {readiness_score}%"}
    return RunManifest(
        manifest_id=f"manifest-{issue_number}",
        issue_number=issue_number,
        edition=EDITION_NAME,
        started_at=datetime(2026, 5, issue_number - 60, 8, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 5, issue_number - 60, 9, 0, tzinfo=timezone.utc),
        config_hash="config",
        snapshot_hash="snapshot",
        html_hash="html",
        md_hash="md",
        ado_calls=1,
        ai_calls=0,
        ai_cost_usd=0.0,
        freshness_summary={"blocks": 0, "warns": 0, "infos": 0},
        qg_results={"QG-B1": True, "QG-B2": True, "QG-B3": True},
        git_sha=None,
        metadata=metadata,
    )


def _continuation_contract_payload(*, issue_number: int, source_issue: int) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "edition": EDITION_NAME,
        "issue_number": issue_number,
        "prior_trusted_issue": 77,
        "first_inherited_at": "2026-05-13T08:00:00+00:00",
        "last_refreshed_at": "2026-05-13T09:00:00+00:00",
        "scorecard_composition": {
            "frozen_from_issue": 77,
            "inherited_dimensions": [["Acme Readiness", "Deployment Velocity"]],
            "proposed_additions": [],
            "proposed_removals": [],
            "removed_by_override": [],
        },
        "section_roster": {
            "inherited_sections": ["exec_summary", "deployment_readiness"],
            "seeded_from_prior": True,
            "sections_missing_evidence": [],
            "added_sections": [],
            "removed_sections": [],
        },
        "narrative_seeding": {
            "seeded": True,
            "source_issue": source_issue,
            "source_path": "archive",
            "files_seeded": ["ws_deployment_readiness.md"],
            "source_hashes": {},
        },
        "overrides_seeding": {
            "seeded": True,
            "source_issue": source_issue,
            "fields_carried": [],
            "fields_cleared": [],
        },
        "evidence_quality": {
            "sections_with_ado_coverage": 1,
            "sections_with_query_only": 0,
            "sections_with_connector_only": 0,
            "sections_manual_only": 0,
        },
        "baseline_gap": None,
    }


def _narrative_text(issue_number: int) -> str:
    return (
        f"Checkpoint {issue_number:03d}: deployment readiness stayed on track through 2026-05-{issue_number - 60:02d}. "
        f"Next gate remains 2026-06-15, with only targeted evidence updates this issue."
    )
