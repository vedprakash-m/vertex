from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli import app
from src.commands import freshness as freshness_module
from src.commands.freshness import _record_confirmed_notify_run, generate_freshness_report
from src.core.action_tracker import append_action
from src.core.exceptions import QueryError
from src.core.milestone_engine import save_milestones
from src.core.models import ReviewSection, ReviewState, ReviewStatus
from src.core.notification_state_store import load_latest_notification_state
from src.core.models import EditionType, RiskLevel, Snapshot, SnapshotItem, WorkItem
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus, Milestone, MilestoneStatus
from src.core.review_status_store import save_review_status
from src.core.snapshot_store import get_archive_root, write_confirmed
from tests.support.report_test_setup import reset_overrides_to_seed_state, stage_v2_report_workspace


runner = CliRunner()
EDITION_NAME = "acme_weekly"
AS_OF = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)


def _stage_freshness_workspace(repo_root: Path, tmp_path: Path) -> tuple[Path, Path, Path]:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    return reports_root, tmp_path / "archive", tmp_path / "output"


def test_generate_freshness_report_writes_outputs(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _stage_freshness_workspace(repo_root, tmp_path)
    reset_overrides_to_seed_state(reports_root)

    artifacts = generate_freshness_report(
        edition_name=EDITION_NAME,
        as_of=AS_OF,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=lambda bundle, timestamp, since: (_sample_items(timestamp), 0),
    )

    assert artifacts.exit_code == 3
    assert artifacts.report.blocks >= 1
    assert artifacts.slice_findings
    assert artifacts.md_path.exists()
    assert artifacts.html_path.exists()
    assert "VERTEX FRESHNESS REPORT" in artifacts.plaintext_body
    assert "SLICE INPUT FINDINGS" in artifacts.plaintext_body
    assert "## Slice Input Findings" in artifacts.markdown_body


def test_generate_freshness_report_uses_trusted_baseline_for_previous_snapshot(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    reports_root, archive_root, output_root = _stage_freshness_workspace(repo_root, tmp_path)
    reset_overrides_to_seed_state(reports_root)

    baseline_call: dict[str, int | None] = {}
    snapshot_call: dict[str, int | None] = {}
    original_load_previous_snapshot = freshness_module._load_previous_snapshot

    def _fake_load_trusted_baseline_issue(*args, **kwargs):
        del args
        baseline_call["before_issue_number"] = kwargs.get("before_issue_number")
        return 77

    def _capturing_load_previous_snapshot(*args, **kwargs):
        snapshot_call["trusted_issue_number"] = kwargs.get("trusted_issue_number")
        return original_load_previous_snapshot(*args, **kwargs)

    monkeypatch.setattr(freshness_module, "load_trusted_baseline_issue", _fake_load_trusted_baseline_issue)
    monkeypatch.setattr(freshness_module, "_load_previous_snapshot", _capturing_load_previous_snapshot)

    artifacts = generate_freshness_report(
        edition_name=EDITION_NAME,
        as_of=AS_OF,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=lambda bundle, timestamp, since: (_sample_items(timestamp), 0),
    )

    assert artifacts.exit_code == 3
    assert baseline_call["before_issue_number"] == 1
    assert snapshot_call["trusted_issue_number"] == 77


def test_generate_freshness_report_renders_review_summary_and_action_labels(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _stage_freshness_workspace(repo_root, tmp_path)
    reset_overrides_to_seed_state(reports_root)

    save_review_status(
        EDITION_NAME,
        ReviewStatus(
            issue_number=1,
            sections=(
                ReviewSection(section_id="exec_summary", state=ReviewState.APPROVED, reviewer="Lead PM", note=None, updated_at=AS_OF),
                ReviewSection(section_id="ws:deployment", state=ReviewState.SENT, reviewer="Jordan Rivera", note=None, updated_at=AS_OF),
                ReviewSection(section_id="ws:networking", state=ReviewState.PENDING, reviewer=None, note=None, updated_at=None),
            ),
        ),
        reports_root=reports_root,
    )

    artifacts = generate_freshness_report(
        edition_name=EDITION_NAME,
        as_of=AS_OF,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=lambda bundle, timestamp, since: (_sample_items(timestamp), 0),
    )

    assert "REVIEW: 1 approved · 1 pending · 1 not sent" in artifacts.plaintext_body
    assert "status=NOT READY (1 section pending review)" in artifacts.plaintext_body
    assert "Overdue" in artifacts.markdown_body
    assert "FR-21" not in artifacts.markdown_body


def test_generate_freshness_report_surfaces_action_summary(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _stage_freshness_workspace(repo_root, tmp_path)
    reset_overrides_to_seed_state(reports_root)
    _write_freshness_action(reports_root.parent / "programs")

    artifacts = generate_freshness_report(
        edition_name=EDITION_NAME,
        as_of=AS_OF,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=lambda bundle, timestamp, since: (_sample_items(timestamp), 0),
    )

    assert "ACTIONS: 1 open, 1 overdue" in artifacts.plaintext_body
    assert "ACTIONS: 1 open, 1 overdue" in artifacts.markdown_body


def test_generate_freshness_report_surfaces_milestone_exit_criteria_staleness(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _stage_freshness_workspace(repo_root, tmp_path)
    reset_overrides_to_seed_state(reports_root)
    save_milestones(
        "acme",
        (
            Milestone(
                id="m3-code-complete",
                program_id="acme",
                name="M3 - Code Complete",
                target_date=date(2026, 5, 30),
                owner_alias="owner",
                status=MilestoneStatus.AT_RISK,
                exit_criteria=("Ramp blocker closed",),
                linked_workstream_ids=("acme",),
                linked_work_item_ids=(901001,),
                notes=None,
            ),
        ),
        programs_root=reports_root.parent / "programs",
    )

    artifacts = generate_freshness_report(
        edition_name=EDITION_NAME,
        as_of=AS_OF,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=lambda bundle, timestamp, since: (_sample_items(timestamp), 0),
    )

    assert "MILESTONES: 1 milestone with stale exit criteria (M3 - Code Complete: 1 linked item stale)" in artifacts.plaintext_body
    assert "MILESTONES: 1 milestone with stale exit criteria (M3 - Code Complete: 1 linked item stale)" in artifacts.markdown_body


def test_generate_freshness_report_surfaces_ncfl_summary(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _stage_freshness_workspace(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    reset_overrides_to_seed_state(reports_root)

    proposals_dir = programs_root / "acme" / "context_proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)
    (proposals_dir / "issue_001.proposals.json").write_text(
        json.dumps(
            [
                {
                    "proposal_id": "prop-1",
                    "program_id": "acme",
                    "issue_number": 1,
                    "edition_id": EDITION_NAME,
                    "source_type": "confirmed_overrides",
                    "extracted_at": "2026-05-01T12:00:00Z",
                    "extractor_version": "1.0.0",
                    "source_artifact": "overrides/issue_001.yaml",
                    "source_field": "scorecards.delivery.control-plane.risk",
                    "extraction_method": "overrides_yaml",
                    "target_store": "risk_register",
                    "target_key": "control-plane",
                    "target_field": "dimension_risk_level",
                    "source_value": "high",
                    "current_value": "medium",
                    "current_value_hash": "abc",
                    "confidence": "high",
                    "batch_eligible": True,
                    "extraction_method_rationale": "test",
                    "conflict_key": "risk_register:control-plane:dimension_risk_level",
                    "status": "pending",
                    "superseded_by": None,
                    "decision_history": [],
                    "rationale": None,
                    "applied_at": None,
                    "applied_by": None,
                    "dismissed_at": None,
                    "dismissed_by": None,
                }
            ],
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    (proposals_dir / "issue_004.proposals.json").write_text(
        json.dumps(
            [
                {
                    "proposal_id": "prop-2",
                    "program_id": "acme",
                    "issue_number": 4,
                    "edition_id": EDITION_NAME,
                    "source_type": "confirmed_overrides",
                    "extracted_at": "2026-05-04T12:00:00Z",
                    "extractor_version": "1.0.0",
                    "source_artifact": "overrides/issue_004.yaml",
                    "source_field": "scorecards.delivery.control-plane.risk",
                    "extraction_method": "overrides_yaml",
                    "target_store": "risk_register",
                    "target_key": "control-plane",
                    "target_field": "dimension_risk_level",
                    "source_value": "blocked",
                    "current_value": "high",
                    "current_value_hash": "def",
                    "confidence": "high",
                    "batch_eligible": True,
                    "extraction_method_rationale": "test",
                    "conflict_key": "risk_register:control-plane:dimension_risk_level",
                    "status": "pending",
                    "superseded_by": None,
                    "decision_history": [],
                    "rationale": None,
                    "applied_at": None,
                    "applied_by": None,
                    "dismissed_at": None,
                    "dismissed_by": None,
                }
            ],
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    artifacts = generate_freshness_report(
        edition_name=EDITION_NAME,
        as_of=AS_OF,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        work_item_loader=lambda bundle, timestamp, since: (_sample_items(timestamp), 0),
    )

    assert artifacts.proposal_summary is not None
    assert "NCFL: 2 pending context proposals" in artifacts.proposal_summary
    assert "issues [001, 004]" in artifacts.proposal_summary
    assert "1 stale (>2 issues old)" in artifacts.proposal_summary
    assert "NCFL: 2 pending context proposals" in artifacts.plaintext_body
    assert "NCFL: 2 pending context proposals" in artifacts.markdown_body


def test_freshness_cli_notify_preview(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _stage_freshness_workspace(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    reset_overrides_to_seed_state(reports_root)

    monkeypatch.setattr("src.commands.freshness.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.freshness.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr(
        "src.commands.freshness._load_live_freshness_items",
        lambda bundle, timestamp, since: (_sample_items(timestamp), 0),
    )

    result = runner.invoke(
        app,
        ["freshness", "--edition", EDITION_NAME, "--notify", "--dry-run", "--since", "14d"],
    )

    assert result.exit_code == 3
    assert "NOTIFY PREVIEW" in result.stdout
    assert "Send disabled until Phase 2 (Graph permissions required)." in result.stdout
    assert (programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.freshness.md").exists()
    assert not (programs_root / "acme" / "publications" / EDITION_NAME / "notifications").exists()


def test_build_action_summary_lines_uses_program_fact_projection(monkeypatch) -> None:
    sentinel = object()
    projected_action = ActionItem(
        id="acme-action-1",
        program_id="acme",
        text="Follow up with the firmware team",
        owner_alias="owner",
        due_date=date(2026, 5, 1),
        status=ActionStatus.OPEN,
        source_signal_id="signal-1",
        source_type=ActionSourceType.SIGNAL,
        linked_work_item_ids=(1001,),
        linked_claim_id=None,
        linked_risk_id=None,
        workstream_id="acme",
        created_at=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
        resolved_at=None,
        resolution_note=None,
    )

    monkeypatch.setattr(freshness_module, "load_program_facts", lambda program_id, db_root, programs_root: sentinel)
    monkeypatch.setattr(freshness_module, "project_action_items", lambda snapshot: (projected_action,) if snapshot is sentinel else ())

    lines = freshness_module._build_action_summary_lines(
        program_id="acme",
        programs_root=Path("programs"),
        as_of=AS_OF,
    )

    assert lines == ("ACTIONS: 1 open, 1 overdue",)


def _write_freshness_action(programs_root: Path) -> None:
    append_action(
        "acme",
        ActionItem(
            id="acme-action-1",
            program_id="acme",
            text="Follow up with the firmware team",
            owner_alias="owner",
            due_date=date(2026, 5, 1),
            status=ActionStatus.OPEN,
            source_signal_id="signal-1",
            source_type=ActionSourceType.SIGNAL,
            linked_work_item_ids=(1001,),
            linked_claim_id=None,
            linked_risk_id=None,
            workstream_id="acme",
            created_at=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
            resolved_at=None,
            resolution_note=None,
        ),
        programs_root=programs_root,
    )


def test_freshness_cli_confirmed_notify_records_notification_state(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _stage_freshness_workspace(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    reset_overrides_to_seed_state(reports_root)

    monkeypatch.setattr("src.commands.freshness.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.freshness.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.freshness.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        "src.commands.freshness._load_live_freshness_items",
        lambda bundle, timestamp, since: (_sample_items(timestamp), 0),
    )

    result = runner.invoke(
        app,
        ["freshness", "--edition", EDITION_NAME, "--notify", "--since", "14d"],
        input="y\n",
    )

    assert result.exit_code == 3
    assert "Notification log:" in result.stdout
    state = load_latest_notification_state(edition=EDITION_NAME, programs_root=programs_root)
    assert state is not None
    assert {item.work_item_id for item in state.items} == {901001, 901002}


def test_generate_freshness_report_surfaces_non_responder_from_previous_notify(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _stage_freshness_workspace(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    reset_overrides_to_seed_state(reports_root)

    first_artifacts = generate_freshness_report(
        edition_name=EDITION_NAME,
        as_of=AS_OF - timedelta(days=2),
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        notify=True,
        work_item_loader=lambda bundle, timestamp, since: (_sample_items(timestamp), 0),
    )
    _record_confirmed_notify_run(
        edition_name=EDITION_NAME,
        issue_number=first_artifacts.issue_number,
        dri_summaries=first_artifacts.dri_summaries,
        notify_previews=first_artifacts.notify_previews,
        programs_root=programs_root,
        confirmed_at=AS_OF - timedelta(days=2),
    )

    second_artifacts = generate_freshness_report(
        edition_name=EDITION_NAME,
        as_of=AS_OF,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        work_item_loader=lambda bundle, timestamp, since: (_sample_items(timestamp), 0),
    )

    findings = {(item.work_item_id, item.rule_id): item for item in second_artifacts.report.items}
    assert (901001, "FR-45") in findings
    assert (901001, "FR-47") in findings
    assert findings[(901001, "FR-47")].message == "Route follow-up to alternate owner igregory."


def test_generate_freshness_report_falls_back_to_stale_snapshot_after_third_ado_failure(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, archive_root, output_root = _stage_freshness_workspace(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    reset_overrides_to_seed_state(reports_root)
    _seed_confirmed_snapshot(archive_root)

    for offset in range(2):
        with pytest.raises(QueryError):
            generate_freshness_report(
                edition_name=EDITION_NAME,
                as_of=AS_OF + timedelta(minutes=offset),
                reports_root=reports_root,
                archive_root=archive_root,
                programs_root=(tmp_path / "programs"),
                work_item_loader=_always_fail_loader,
            )

    artifacts = generate_freshness_report(
        edition_name=EDITION_NAME,
        as_of=AS_OF + timedelta(minutes=2),
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        work_item_loader=_always_fail_loader,
    )

    assert artifacts.exit_code == 3
    assert artifacts.stale_banner is not None
    assert "STALE DATA" in artifacts.plaintext_body
    assert "snapshot from issue 001" in artifacts.markdown_body
    assert artifacts.md_path.name == "issue_002.freshness.md"


def test_generate_freshness_report_allow_stale_returns_clean_exit_when_snapshot_has_no_findings(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, archive_root, output_root = _stage_freshness_workspace(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    reset_overrides_to_seed_state(reports_root)
    _seed_confirmed_snapshot(archive_root)

    for offset in range(2):
        with pytest.raises(QueryError):
            generate_freshness_report(
                edition_name=EDITION_NAME,
                as_of=AS_OF + timedelta(minutes=offset),
                reports_root=reports_root,
                archive_root=archive_root,
                programs_root=(tmp_path / "programs"),
                work_item_loader=_always_fail_loader,
            )

    artifacts = generate_freshness_report(
        edition_name=EDITION_NAME,
        as_of=AS_OF + timedelta(minutes=2),
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        work_item_loader=_always_fail_loader,
        allow_stale=True,
    )

    assert artifacts.report.is_clean is True
    assert artifacts.exit_code == 0
    assert artifacts.stale_banner is not None


def test_freshness_cli_supports_json_and_csv(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _stage_freshness_workspace(repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    reset_overrides_to_seed_state(reports_root)

    monkeypatch.setattr("src.commands.freshness.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.freshness.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr(
        "src.commands.freshness._load_live_freshness_items",
        lambda bundle, timestamp, since: (_sample_items(timestamp), 0),
    )

    json_result = runner.invoke(
        app,
        ["freshness", "--edition", EDITION_NAME, "--since", "14d", "--format", "json"],
    )

    assert json_result.exit_code == 3
    payload = json.loads(json_result.stdout)
    assert payload["edition"] == EDITION_NAME
    assert payload["issue_number"] == 1
    assert payload["report"]["blocks"] >= 1
    assert payload["report"]["finding_count"] == len(payload["findings"])
    assert payload["proposal_summary"] == "NCFL: 0 pending context proposals"
    assert payload["outputs"]["markdown_path"].endswith("issue_001.freshness.md")

    csv_result = runner.invoke(
        app,
        ["freshness", "--edition", EDITION_NAME, "--since", "14d", "--format", "csv"],
    )

    assert csv_result.exit_code == 3
    lines = csv_result.stdout.strip().splitlines()
    assert lines[0] == (
        "edition,issue_number,dri_name,dri_email,work_item_id,item_title,severity,rule_id,action_label,"
        "message,action_message,suggested_fix,item_url,blocks,warns,infos,stale_banner,markdown_path,html_path"
    )
    assert any("acme_weekly,1," in line for line in lines[1:])
    assert any("901001" in line for line in lines[1:])


def _sample_items(as_of: datetime) -> tuple[WorkItem, ...]:
    return (
        WorkItem(
            id=901001,
            type="Feature",
            title="Deployment safety remediation",
            state="Active",
            assigned_to="Jordan Rivera",
            assigned_to_email="jordan@example.com",
            area_path="One\\Adventure\\Acme\\Deployment",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 1),
            risk_level=RiskLevel.LOW,
            tags=["Safety"],
            custom_fields={"changed_date": (as_of - timedelta(days=18)).isoformat()},
            revisions=[],
            comments=[],
            fetched_at=as_of,
        ),
        WorkItem(
            id=901002,
            type="Risk",
            title="Networking readiness gap",
            state="At Risk",
            assigned_to="Priya Mehta",
            assigned_to_email="priya@example.com",
            area_path="One\\Adventure\\Acme\\Networking",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 8),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={
                "changed_date": (as_of - timedelta(days=1)).isoformat(),
                "description": "WIP, updating soon",
            },
            revisions=[],
            comments=[],
            fetched_at=as_of,
        ),
    )


def _always_fail_loader(bundle, timestamp, since):
    del bundle, timestamp, since
    raise QueryError("ADO query failed")


def _seed_confirmed_snapshot(archive_root: Path) -> None:
    snapshot = Snapshot(
        issue_number=1,
        generated_at=AS_OF - timedelta(days=1),
        ado_data_as_of=AS_OF - timedelta(days=1),
        edition_type=EditionType.DETAILED,
        items=(
            SnapshotItem(
                id=990001,
                type="Feature",
                title="Confirmed fallback item",
                state="Active",
                assigned_to="Casey Howard",
                area_path="One\\Adventure\\Acme\\Deployment",
                target_date=AS_OF.date() + timedelta(days=10),
                risk_level=RiskLevel.LOW,
                tags=[],
            ),
        ),
        scorecards=(),
        schema_version="1.0",
    )
    write_confirmed(EDITION_NAME, 1, snapshot, archive_root=archive_root)

    edition_root = get_archive_root(EDITION_NAME, archive_root=archive_root)
    edition_root.mkdir(parents=True, exist_ok=True)
    index_path = edition_root / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "edition": EDITION_NAME,
                "issues": [
                    {
                        "issue_number": 1,
                        "generated_at": snapshot.generated_at.isoformat(),
                        "html_path": str(edition_root / "html" / "issue_001.html"),
                        "md_path": str(edition_root / "md" / "issue_001.md"),
                        "snapshot_path": str(edition_root / "snapshots" / "issue_001.snapshot.json"),
                        "manifest_path": str(edition_root / "manifests" / "issue_001.json"),
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

