from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.commands.doctor import run_doctor
from src.core.archive_store import write_confirmed_issue
from src.core.models import ConfirmedDimension, EditionType, RiskLevel, RunManifest, Snapshot, SnapshotItem
from src.core.platform_proof_log_store import load_platform_proof_records
from src.core.platform_s7_store import load_platform_s7_state
from src.core.trusted_baseline_store import advance_trusted_baseline, load_trusted_baseline
from tests.support.report_test_setup import stage_v2_report_workspace


EDITION_NAME = "acme_weekly"
runner = CliRunner()


def test_doctor_consistency_detects_baseline_manifest_mismatch(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"

    _write_confirmed_issue(archive_root, issue_number=1)
    _write_confirmed_issue(archive_root, issue_number=2)
    advance_trusted_baseline(
        EDITION_NAME,
        1,
        established_at=datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )

    report = run_doctor(
        edition_name=EDITION_NAME,
        consistency=True,
        reports_root=reports_root,
        archive_root=archive_root,
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )

    assert report.failures == 1
    assert "trusted baseline issue 001 does not match latest confirmed archive issue 002" in report.checks[0].detail


def test_admin_baseline_correction_round_trip(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"

    _write_confirmed_issue(archive_root, issue_number=1)
    _write_confirmed_issue(archive_root, issue_number=2)
    advance_trusted_baseline(
        EDITION_NAME,
        1,
        established_at=datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )

    result = runner.invoke(
        app,
        [
            "admin",
            "baseline",
            "--edition",
            EDITION_NAME,
            "--correct",
            "--issue",
            "2",
            "--reason",
            "Advance after archive recovery validation.",
            "--archive-root",
            str(archive_root),
            "--editions-root",
            str(reports_root.parent / "editions"),
            "--programs-root",
            str(reports_root.parent / "programs"),
        ],
    )
    assert result.exit_code == 0

    baseline = load_trusted_baseline(
        EDITION_NAME,
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )
    assert baseline is not None
    assert baseline.trusted_issue_number == 2
    assert baseline.history[-1].action == "corrected"

    rollback = runner.invoke(
        app,
        [
            "admin",
            "baseline",
            "--edition",
            EDITION_NAME,
            "--correct",
            "--issue",
            "1",
            "--reason",
            "Rollback after verifying issue 002 should not stay trusted.",
            "--archive-root",
            str(archive_root),
            "--editions-root",
            str(reports_root.parent / "editions"),
            "--programs-root",
            str(reports_root.parent / "programs"),
        ],
    )
    assert rollback.exit_code == 0

    baseline = load_trusted_baseline(
        EDITION_NAME,
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )
    assert baseline is not None
    assert baseline.trusted_issue_number == 1
    assert baseline.history[-1].action == "rolled_back"


def test_admin_baseline_records_rollback_drill(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)

    advance_trusted_baseline(
        EDITION_NAME,
        1,
        established_at=datetime(2026, 5, 13, 10, 0, tzinfo=timezone.utc),
        established_by="operator",
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )

    result = runner.invoke(
        app,
        [
            "admin",
            "baseline",
            "--edition",
            EDITION_NAME,
            "--record-rollback-drill",
            "--checkpoint-name",
            "issue_001_20260602T170000Z",
            "--rollback-exit-code",
            "0",
            "--consistency-exit-code",
            "0",
            "--editions-root",
            str(reports_root.parent / "editions"),
            "--programs-root",
            str(reports_root.parent / "programs"),
        ],
    )
    assert result.exit_code == 0

    baseline = load_trusted_baseline(
        EDITION_NAME,
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )
    assert baseline is not None
    assert baseline.history[-1].issue == 1
    assert baseline.history[-1].action == "rollback_drill_passed"
    assert baseline.history[-1].reason == (
        "Rollback drill passed: checkpoint=issue_001_20260602T170000Z; "
        "rollback_exit_code=0; consistency_exit_code=0"
    )


def test_admin_platform_proof_records_program_proof(repo_root: Path, tmp_path: Path, monkeypatch) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    monkeypatch.setenv("VERTEX_AUTHOR", "operator")

    result = runner.invoke(
        app,
        [
            "admin",
            "platform-proof",
            "--program",
            "acme",
            "--proof-id",
            "p4a_clean_machine",
            "--status",
            "passed",
            "--notes",
            "Fresh clone succeeded without repo edits.",
            "--elapsed-minutes",
            "11.5",
            "--no-code-changes",
            "--confirm-exit-code",
            "0",
            "--programs-root",
            str(reports_root.parent / "programs"),
            "--editions-root",
            str(reports_root.parent / "editions"),
        ],
    )

    assert result.exit_code == 0
    records = load_platform_proof_records("acme", programs_root=reports_root.parent / "programs")
    assert len(records) == 1
    assert records[0].proof_id == "p4a_clean_machine"
    assert records[0].status == "passed"
    assert records[0].recorded_by == "operator"
    assert records[0].notes == "Fresh clone succeeded without repo edits."
    assert records[0].elapsed_minutes == 11.5
    assert records[0].no_code_changes is True
    assert records[0].confirm_exit_code == 0


def test_admin_platform_proof_resolves_program_from_edition(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)

    result = runner.invoke(
        app,
        [
            "admin",
            "platform-proof",
            "--edition",
            EDITION_NAME,
            "--proof-id",
            "p6b_ado_only",
            "--status",
            "failed",
            "--archetype",
            "ADO-only",
            "--programs-root",
            str(reports_root.parent / "programs"),
            "--editions-root",
            str(reports_root.parent / "editions"),
        ],
    )

    assert result.exit_code == 0
    records = load_platform_proof_records("acme", programs_root=reports_root.parent / "programs")
    assert len(records) == 1
    assert records[0].proof_id == "p6b_ado_only"
    assert records[0].status == "failed"
    assert records[0].edition == EDITION_NAME
    assert records[0].archetype == "ADO-only"


def test_admin_platform_proof_plan_reports_missing_and_recorded_proofs(repo_root: Path, tmp_path: Path, monkeypatch) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    monkeypatch.setenv("VERTEX_AUTHOR", "operator")

    record_result = runner.invoke(
        app,
        [
            "admin",
            "platform-proof",
            "--program",
            "acme",
            "--proof-id",
            "p4a_clean_machine",
            "--status",
            "passed",
            "--programs-root",
            str(reports_root.parent / "programs"),
            "--editions-root",
            str(reports_root.parent / "editions"),
        ],
    )
    assert record_result.exit_code == 0

    plan_result = runner.invoke(
        app,
        [
            "admin",
            "platform-proof",
            "--program",
            "acme",
            "--plan",
            "--programs-root",
            str(reports_root.parent / "programs"),
            "--editions-root",
            str(reports_root.parent / "editions"),
        ],
    )

    assert plan_result.exit_code == 0
    assert "Platform proof plan for program acme:" in plan_result.stdout
    assert "- p4a_clean_machine | passed | phase=P4a" in plan_result.stdout
    assert "- p4b_ado_only | missing | phase=P4b" in plan_result.stdout
    assert "archetype=ADO-only" in plan_result.stdout


def test_admin_platform_proof_rejects_mismatched_archetype(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)

    result = runner.invoke(
        app,
        [
            "admin",
            "platform-proof",
            "--edition",
            EDITION_NAME,
            "--proof-id",
            "p6b_ado_only",
            "--status",
            "failed",
            "--archetype",
            "ADO + M365",
            "--programs-root",
            str(reports_root.parent / "programs"),
            "--editions-root",
            str(reports_root.parent / "editions"),
        ],
    )

    assert result.exit_code != 0
    assert "requires archetype 'ADO-only'" in (result.stdout + result.stderr)


def test_admin_s7_position_records_deferred_state(repo_root: Path, tmp_path: Path, monkeypatch) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    monkeypatch.setenv("VERTEX_AUTHOR", "operator")

    result = runner.invoke(
        app,
        [
            "admin",
            "s7-position",
            "--position",
            "deferred",
            "--justification",
            "S7b remains outside the V-11 critical path pending explicit PM sign-off.",
            "--programs-root",
            str(reports_root.parent / "programs"),
        ],
    )

    assert result.exit_code == 0
    state = load_platform_s7_state(programs_root=reports_root.parent / "programs")
    assert state is not None
    assert state.position == "deferred"
    assert state.recorded_by == "operator"
    assert state.justification == "S7b remains outside the V-11 critical path pending explicit PM sign-off."


def test_admin_s7_position_refuses_reverting_complete_to_deferred(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)

    complete = runner.invoke(
        app,
        [
            "admin",
            "s7-position",
            "--position",
            "complete",
            "--programs-root",
            str(reports_root.parent / "programs"),
        ],
    )
    assert complete.exit_code == 0

    revert = runner.invoke(
        app,
        [
            "admin",
            "s7-position",
            "--position",
            "deferred",
            "--justification",
            "Trying to undo a completed S7 state.",
            "--programs-root",
            str(reports_root.parent / "programs"),
        ],
    )
    assert revert.exit_code != 0
    state = load_platform_s7_state(programs_root=reports_root.parent / "programs")
    assert state is not None
    assert state.position == "complete"


def _write_confirmed_issue(archive_root: Path, *, issue_number: int) -> None:
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=issue_number,
        snapshot=_build_snapshot(issue_number),
        html_body="<html><body>Rendered</body></html>",
        markdown_body="# Rendered",
        manifest=_build_manifest(issue_number),
        archive_root=archive_root,
    )


def _build_snapshot(issue_number: int) -> Snapshot:
    return Snapshot(
        issue_number=issue_number,
        generated_at=datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc),
        ado_data_as_of=datetime(2026, 5, 5, 8, 45, tzinfo=timezone.utc),
        edition_type=EditionType.DETAILED,
        items=(
            SnapshotItem(
                id=100 + issue_number,
                type="Feature",
                title=f"Deployment readiness {issue_number}",
                state="Active",
                assigned_to="Vertex Maintainer",
                area_path="One\\Adventure\\Acme",
                target_date=date(2026, 6, 30),
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
    )


def _build_manifest(issue_number: int) -> RunManifest:
    return RunManifest(
        manifest_id=f"manifest-{issue_number}",
        issue_number=issue_number,
        edition=EDITION_NAME,
        started_at=datetime(2026, 5, 5, 8, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc),
        config_hash="config",
        snapshot_hash="snapshot",
        html_hash="html",
        md_hash="md",
        ado_calls=1,
        ai_calls=0,
        ai_cost_usd=0.0,
        freshness_summary={"blocks": 0, "warns": 0, "infos": 0},
        qg_results={"QG-4": True, "QG-5": True, "QG-6": True, "QG-8": True},
        git_sha=None,
    )
