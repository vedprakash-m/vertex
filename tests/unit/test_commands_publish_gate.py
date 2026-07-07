from __future__ import annotations
from tests.support.synthetic_report_source import synthetic_report_root as repo_root
import pytest

from dataclasses import replace
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import yaml
from typer.testing import CliRunner

from cli import app
from src.commands.report import generate_report_draft
from src.core.archive_store import write_confirmed_issue
from src.core.models import RiskLevel
from src.core.quality_gates import QualityGateReport
from src.core.narrative_store import get_narratives_dir
from src.core.overrides_store import get_overrides_path
from src.core.review_status_store import load_review_status
from src.core.snapshot_store import get_archive_root
from tests.support.report_test_setup import disable_kusto_in_report_copy, stage_v2_report_workspace
from tests.unit.test_commands_confirm import _confirmable_items, _seed_high_risk_signal_coverage
from tests.unit.test_commands_report import _issue_077_snapshot_items, _lookback_snapshot, _manifest, _snapshot_item_from_work_item


runner = CliRunner()
EDITION_NAME = "acme_weekly"


@pytest.fixture(autouse=True)
def _isolate_fact_store(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "vertex-db"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr("src.commands.publish_gate.PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr("src.commands.publish_gate.REPORTS_ROOT", tmp_path / "reports")
    monkeypatch.setattr("src.commands.publish_gate.ARCHIVE_ROOT", tmp_path / "archive")
    monkeypatch.setattr("src.commands.confirm._CONFIRM_PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr("src.commands.confirm.REPORTS_ROOT", tmp_path / "reports")
    monkeypatch.setattr("src.commands.confirm.ARCHIVE_ROOT", tmp_path / "archive")


def _patch_m3_linked_wi(programs_root: Path, *, work_item_id: int = 900001) -> None:
    """Patch m3-code-complete in the test workspace to reference the given WI (test fixture only)."""
    milestones_path = programs_root / "acme" / "milestones.yaml"
    if not milestones_path.exists():
        return
    data = yaml.safe_load(milestones_path.read_text(encoding="utf-8")) or {}
    for m in data.get("milestones", []):
        if m.get("id") == "m3-code-complete":
            m["linked_work_item_ids"] = [work_item_id]
    milestones_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _review_workflow_items(as_of: datetime):
    baseline_items = list(_confirmable_items(as_of))
    seed_item = baseline_items[0]
    return tuple(
        baseline_items
        + [
            replace(
                seed_item,
                id=910001,
                title="Deployment operations readiness",
                area_path="ExampleProject\\Acme\\Deployment",
                target_date=as_of.date() + timedelta(days=20),
                risk_level=RiskLevel.LOW,
            ),
            replace(
                seed_item,
                id=910002,
                title="Repairs safety readiness",
                area_path="ExampleProject\\Acme\\Deployment",
                target_date=as_of.date() + timedelta(days=21),
                risk_level=RiskLevel.LOW,
                tags=["Safety"],
            ),
            replace(
                seed_item,
                id=910003,
                title="Deployment platform runway",
                area_path="ExampleProject\\Acme\\Deployment",
                target_date=as_of.date() + timedelta(days=22),
                risk_level=RiskLevel.LOW,
            ),
            replace(
                seed_item,
                id=910004,
                title="Package firmware readiness",
                area_path="ExampleProject\\Acme\\Packages",
                target_date=as_of.date() + timedelta(days=23),
                risk_level=RiskLevel.LOW,
            ),
            replace(
                seed_item,
                id=910011,
                title="Network parity validation",
                area_path="ExampleProject\\Contoso\\Networking",
                target_date=as_of.date() + timedelta(days=24),
                risk_level=RiskLevel.LOW,
                tags=["Networking"],
            ),
            replace(
                seed_item,
                id=910012,
                title="Package readiness",
                area_path="ExampleProject\\Acme\\Packages",
                target_date=as_of.date() + timedelta(days=25),
                risk_level=RiskLevel.LOW,
            ),
            replace(
                seed_item,
                id=910005,
                title="Artifact service performance signoff",
                area_path="ExampleProject\\Fabrikam\\Scenarios",
                target_date=as_of.date() + timedelta(days=26),
                risk_level=RiskLevel.LOW,
                tags=["Performance", "Pilot"],
            ),
            replace(
                seed_item,
                id=910006,
                title="Artifact service scenario validation",
                area_path="ExampleProject\\Fabrikam\\Scenarios",
                target_date=as_of.date() + timedelta(days=27),
                risk_level=RiskLevel.LOW,
            ),
            replace(
                seed_item,
                id=910007,
                title="Artifact service startup validation",
                area_path="ExampleProject\\Fabrikam\\Scenarios",
                target_date=as_of.date() + timedelta(days=28),
                risk_level=RiskLevel.LOW,
                tags=["controlplane"],
            ),
            replace(
                seed_item,
                id=910008,
                title="firmware version collection",
                area_path="ExampleProject\\Fabrikam\\Scenarios",
                target_date=as_of.date() + timedelta(days=29),
                risk_level=RiskLevel.LOW,
                tags=["Dataplane"],
            ),
            replace(
                seed_item,
                id=910009,
                title="Storage buildout validation",
                area_path="ExampleProject\\Fabrikam\\Scenarios",
                target_date=as_of.date() + timedelta(days=30),
                risk_level=RiskLevel.LOW,
                tags=["Firmware"],
            ),
            replace(
                seed_item,
                id=910010,
                title="Diagnostics dashboard alerting",
                area_path="ExampleProject\\Diagnostics",
                target_date=as_of.date() + timedelta(days=31),
                risk_level=RiskLevel.LOW,
            ),
        ]
    )


def _prepare_publishable_overrides(
    reports_root: Path,
    archive_root: Path,
    output_root: Path,
    issue_number: int,
    *,
    edition_name: str = EDITION_NAME,
) -> None:
    del archive_root, output_root
    overrides_path = get_overrides_path(edition_name, reports_root, issue_number=issue_number)
    narratives_dir = get_narratives_dir(edition_name, issue_number, reports_root)

    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    overrides_payload["decision_strip_ack"] = {
        "no_leadership_ask": True,
        "reason": "Current signals are being driven through the owning teams, and no additional leadership intervention changes this week\'s execution path.",
    }
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"

    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    for narrative_path in narratives_dir.glob("ws_*.md"):
        narrative_path.write_text(
            "Execution is tracking to plan this week. No new leadership decision is required today.",
            encoding="utf-8",
        )
    for narrative_path in narratives_dir.glob("chapter_*.md"):
        narrative_path.write_text(
            "Execution is tracking to plan this week. No new leadership decision is required today.",
            encoding="utf-8",
        )
    (narratives_dir / "exec_summary.md").write_text(
        "The release remains conditional. Deployment parity and service performance remain the next leadership decisions.",
        encoding="utf-8",
    )


def test_publish_gate_blocks_when_review_sections_are_pending(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)

    _seed_high_risk_signal_coverage(programs_root, captured_at=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc))

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_review_workflow_items(timestamp), 0),
        open_browser=False,
    )

    _prepare_publishable_overrides(reports_root, archive_root, (tmp_path / "programs" / "acme" / "publications"), issue_number=1, edition_name=EDITION_NAME)

    monkeypatch.setattr("src.commands.confirm.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.confirm.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.confirm._CONFIRM_PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.publish_gate.ARCHIVE_ROOT", archive_root)
    empty_report = QualityGateReport(results=())
    monkeypatch.setattr("src.commands.confirm.evaluate_context_integrity_gates", lambda **kwargs: empty_report)

    result = runner.invoke(app, ["publish-gate", "--edition", EDITION_NAME, "--issue", "1"])

    assert result.exit_code == 3
    assert "Publish gate blocked for issue 001." in result.stdout
    assert "Review gate failed" in result.stdout


def test_publish_gate_supports_json_and_csv(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    stage_v2_report_workspace(repo_root, tmp_path)
    monkeypatch.setattr("src.commands.publish_gate.ARCHIVE_ROOT", tmp_path / "archive")
    monkeypatch.setattr("src.commands.publish_gate.read_archive_index", lambda edition, archive_root: object())
    monkeypatch.setattr(
        "src.commands.publish_gate.confirm_issue",
        lambda edition_name, issue_number, dry_run, force, **kwargs: SimpleNamespace(
            issue_number=issue_number,
            next_issue_number=issue_number + 1,
            exit_code=3,
            failures=("Review gate failed",),
            warnings=("Forced past QG-1",),
        ),
    )

    json_result = runner.invoke(app, ["publish-gate", "--edition", EDITION_NAME, "--issue", "77", "--force", "--format", "json"])

    assert json_result.exit_code == 3
    payload = json.loads(json_result.stdout)
    assert payload["edition_name"] == EDITION_NAME
    assert payload["issue_number"] == 77
    assert payload["next_issue_number"] == 78
    assert payload["forced"] is True
    assert payload["status"] == "blocked"
    assert payload["failures"] == ["Review gate failed"]
    assert payload["warnings"] == [
        "Forced past QG-1",
        "Persona signal coverage artifact not found — skipping persona gate check",
    ]

    csv_result = runner.invoke(app, ["publish-gate", "--edition", EDITION_NAME, "--issue", "77", "--force", "--format", "csv"])

    assert csv_result.exit_code == 3
    lines = csv_result.stdout.strip().splitlines()
    assert lines[0] == "entry_type,edition_name,issue_number,next_issue_number,exit_code,forced,status,message"
    assert "summary,acme_weekly,77,78,3,True,blocked," in lines[1]
    assert any(line.endswith(",Review gate failed") and line.startswith("failure,acme_weekly,77,78,3,True,blocked,") for line in lines[1:])
    assert any(line.endswith(",Forced past QG-1") and line.startswith("warning,acme_weekly,77,78,3,True,blocked,") for line in lines[1:])


def test_review_workflow_clears_review_gate_after_section_approvals(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)
    _patch_m3_linked_wi(programs_root, work_item_id=900001)

    empty_report = QualityGateReport(results=())
    monkeypatch.setattr("src.commands.confirm.evaluate_context_integrity_gates", lambda **kwargs: empty_report)
    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.report.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.report._load_live_work_items", lambda bundle, timestamp: (_review_workflow_items(timestamp), 0))
    monkeypatch.setattr("src.commands.report.webbrowser.open", lambda url: True)
    monkeypatch.setattr("src.commands.review_sections.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.review_sections.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.review_sections.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.review_full.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.review_full.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.review_full.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.confirm.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.confirm.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.confirm._CONFIRM_PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.publish_gate.ARCHIVE_ROOT", archive_root)

    _seed_high_risk_signal_coverage(
        programs_root,
        captured_at=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
    )

    report_result = runner.invoke(app, ["report", "--edition", EDITION_NAME, "--dry-run", "--as-of", "2026-05-05T18:00:00"])

    assert report_result.exit_code in (0, 2, 3)

    _prepare_publishable_overrides(reports_root, archive_root, programs_root / "acme" / "publications", issue_number=1, edition_name=EDITION_NAME)

    refreshed_report_result = runner.invoke(app, ["report", "--edition", EDITION_NAME, "--dry-run", "--as-of", "2026-05-05T18:00:00"])

    assert refreshed_report_result.exit_code in (0, 2, 3)

    _prepare_publishable_overrides(reports_root, archive_root, programs_root / "acme" / "publications", issue_number=1, edition_name=EDITION_NAME)

    review_status = load_review_status(EDITION_NAME, reports_root=reports_root)
    assert review_status is not None
    assert review_status.sections

    for section in review_status.sections:
        result = runner.invoke(
            app,
            [
                "review-sections",
                "set",
                "--edition",
                EDITION_NAME,
                "--section",
                section.section_id,
                "--state",
                "approved",
                "--note",
                "LGTM",
            ],
        )
        assert result.exit_code == 0

    updated_review_status = load_review_status(EDITION_NAME, reports_root=reports_root)
    assert updated_review_status is not None
    assert all(section.state.value == "approved" for section in updated_review_status.sections)

    review_full_result = runner.invoke(app, ["review-full", "--edition", EDITION_NAME, "--no-open"])

    assert review_full_result.exit_code == 0
    assert (programs_root / "acme" / "publications" / EDITION_NAME / "review" / "issue_001.html").exists()

    publish_gate_result = runner.invoke(app, ["publish-gate", "--edition", EDITION_NAME, "--issue", "1"])

    assert publish_gate_result.exit_code == 3
    assert "Publish gate blocked for issue 001." in publish_gate_result.stdout
    assert "Review gate failed" not in publish_gate_result.stdout
    assert "At-risk or missed milestones without linked risk register coverage" in publish_gate_result.stdout


def test_issue_077_operating_loop_writes_artifacts_and_confirms(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    edition_name = EDITION_NAME
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = tmp_path / "programs"
    output_root = programs_root / "acme" / "publications"
    disable_kusto_in_report_copy(reports_root)
    prior_as_of = datetime(2026, 5, 3, 18, tzinfo=timezone.utc)
    write_confirmed_issue(
        edition=edition_name,
        issue_number=76,
        snapshot=_lookback_snapshot(
            issue_number=76,
            as_of=prior_as_of,
            items=tuple(
                _snapshot_item_from_work_item(item, risk_level=RiskLevel.MEDIUM)
                for item in _issue_077_snapshot_items(repo_root, prior_as_of)[:2]
            ),
            scorecard_risks={
                "Deployment Velocity": RiskLevel.MEDIUM,
                "Deployment Safety": RiskLevel.MEDIUM,
                "Networking": RiskLevel.MEDIUM,
            },
        ),
        html_body="<html><body>Issue 076</body></html>",
        markdown_body="# Issue 076\n",
        manifest=_manifest(76, prior_as_of),
        archive_root=archive_root,
    )

    monkeypatch.setattr("src.commands.report.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.report.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.report.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        "src.commands.report._load_live_work_items",
        lambda bundle, timestamp: (_issue_077_snapshot_items(repo_root, timestamp), 0),
    )
    monkeypatch.setattr("src.commands.report.webbrowser.open", lambda url: True)
    monkeypatch.setattr("src.commands.review_sections.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.review_sections.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.review_sections.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.review_full.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.review_full.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.review_full.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.confirm.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.confirm.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.confirm._CONFIRM_PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.publish_gate.ARCHIVE_ROOT", archive_root)

    first_report_result = runner.invoke(app, ["report", "--edition", edition_name, "--issue", "77", "--dry-run"])

    assert first_report_result.exit_code in (0, 2, 3)
    assert "Quality Matrix (JSON):" in first_report_result.stdout
    assert "Remediation (JSON):" in first_report_result.stdout
    assert (programs_root / "acme" / "publications" / edition_name / "issue_077" / "issue_077.quality_matrix.md").exists()
    assert (programs_root / "acme" / "publications" / edition_name / "issue_077" / "issue_077.quality_matrix.json").exists()
    assert (programs_root / "acme" / "publications" / edition_name / "issue_077" / "issue_077.remediation.md").exists()
    assert (programs_root / "acme" / "publications" / edition_name / "issue_077" / "issue_077.remediation.json").exists()

    _prepare_publishable_overrides(
        reports_root,
        archive_root,
        output_root,
        issue_number=77,
        edition_name=edition_name,
    )

    second_report_result = runner.invoke(app, ["report", "--edition", edition_name, "--issue", "77", "--dry-run"])

    assert second_report_result.exit_code in (0, 2, 3)

    # Re-fill any scaffold narratives the second report may have created for new dimensions
    # (e.g. dimensions excluded from the first run due to DONE risk become visible after overrides set all risks to low)
    _prepare_publishable_overrides(
        reports_root,
        archive_root,
        output_root,
        issue_number=77,
        edition_name=edition_name,
    )

    review_status = load_review_status(edition_name, reports_root=reports_root)
    assert review_status is not None
    assert review_status.issue_number == 77
    assert review_status.sections

    review_full_result = runner.invoke(app, ["review-full", "--edition", edition_name, "--issue", "77", "--no-open"])

    assert review_full_result.exit_code == 0
    assert (programs_root / "acme" / "publications" / edition_name / "review" / "issue_077.html").exists()

    for section in review_status.sections:
        result = runner.invoke(
            app,
            [
                "review-sections",
                "set",
                "--edition",
                edition_name,
                "--issue",
                "77",
                "--section",
                section.section_id,
                "--state",
                "approved",
                "--note",
                "Validated in operating-loop regression.",
            ],
        )
        assert result.exit_code == 0

    publish_gate_result = runner.invoke(app, ["publish-gate", "--edition", edition_name, "--issue", "77", "--force"])

    assert publish_gate_result.exit_code == 0, publish_gate_result.stdout
    assert "Publish gate passed for issue 077." in publish_gate_result.stdout
    assert "Forced past QG-1" in publish_gate_result.stdout

    confirm_result = runner.invoke(
        app,
        ["confirm", "--edition", edition_name, "--issue", "77", "--force"],
        input="y\n",
    )

    assert confirm_result.exit_code == 0
    assert "Confirmed issue 077 for acme_weekly." in confirm_result.stdout
    edition_archive_root = get_archive_root(edition_name, archive_root)
    assert (edition_archive_root / "snapshots" / "issue_077.snapshot.json").exists()
    assert (edition_archive_root / "html" / "issue_077.html").exists()
    assert (edition_archive_root / "md" / "issue_077.md").exists()
    assert (edition_archive_root / "manifests" / "issue_077.json").exists()
    assert (edition_archive_root / "review" / "issue_077.review.yaml").exists()
    assert (edition_archive_root / "narratives" / "issue_077" / "exec_summary.md").exists()

    active_review_status = load_review_status(edition_name, reports_root=reports_root)
    assert active_review_status is not None
    assert active_review_status.issue_number == 78
    assert all(section.state.value == "pending" for section in active_review_status.sections)
