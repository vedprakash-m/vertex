from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.commands.admin_fact_store_flip import run_fact_store_flip_preview
from src.core.checkpoint_store import list_checkpoints
from src.core.fact_sor_state import load_fact_sor_state, save_fact_sor_state
from src.core.action_tracker import append_action
from src.core.assumption_tracker import save_assumptions
from src.core.claim_tracker import append_claim_entry, append_claim_status_update, append_decision_ask
from src.core.decision_register import save_decisions
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus, Assumption, AssumptionStatus, ClaimEntry, ClaimStatusUpdate, DecisionAsk, DecisionEntry, DecisionStatus
from src.core.program_fact_store import load_program_facts, persist_program_fact_snapshot
from src.core.snapshot_store import get_archive_root
from src.core.trusted_baseline_store import advance_trusted_baseline
from tests.support.report_test_setup import stage_v2_report_workspace


runner = CliRunner()
EDITION_NAME = "acme_weekly"


def test_run_fact_store_flip_preview_reports_green_window_ready_for_execution(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    editions_root = reports_root.parent / "editions"
    programs_root = reports_root.parent / "programs"
    archive_root = tmp_path / "archive"
    reality_db_root = tmp_path / "vertex-db"
    generated_ats = (
        datetime(2026, 5, 28, 9, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 29, 9, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc),
    )

    _write_confirmed_archive_entries(archive_root, generated_ats=generated_ats)
    append_action("acme", _build_action_item(created_at=generated_ats[0] - timedelta(days=1)), programs_root=programs_root)
    append_claim_entry(_build_claim_entry(), programs_root=programs_root)
    append_claim_status_update("acme", _build_claim_status_update(updated_at=generated_ats[0] - timedelta(hours=2)), programs_root=programs_root)
    append_decision_ask(_build_decision_ask(), programs_root=programs_root)
    save_assumptions("acme", (_build_assumption(),), programs_root=programs_root)
    save_decisions("acme", (_build_decision_entry(),), programs_root=programs_root)
    advance_trusted_baseline(
        "acme_weekly",
        12,
        established_at=generated_ats[0] - timedelta(hours=2),
        established_by="alex",
        editions_root=editions_root,
        programs_root=programs_root,
    )
    persist_program_fact_snapshot(
        load_program_facts("acme", programs_root=programs_root, editions_root=editions_root, archive_root=archive_root),
        recorded_at=generated_ats[0] - timedelta(hours=1),
        db_root=reality_db_root,
    )

    artifacts = run_fact_store_flip_preview(
        program_id="acme",
        edition_name=EDITION_NAME,
        editions_root=reports_root.parent / "editions",
        programs_root=programs_root,
        archive_root=archive_root,
        reality_db_root=reality_db_root,
    )

    assert artifacts.consecutive_parity_passes == 3
    assert artifacts.ready_for_execution is True
    assert artifacts.shadow_write_retention == "enabled"
    assert artifacts.current_storage_authority == "legacy"
    assert artifacts.parity_window[0].issue_number == 3
    assert artifacts.supported_families == (
        "actions",
        "claims",
        "claim_status_updates",
        "decision_asks",
        "assumptions",
        "decisions",
        "risks",
        "dependencies",
        "milestones",
        "workstreams",
        "workstream_associations",
        "baseline_trust_events",
        "skip_issues",
    )
    assert artifacts.pending_families == ()
    assert not any(blocker.startswith("unsupported families remain:") for blocker in artifacts.blockers)
    assert artifacts.blockers == ()


def test_admin_fact_store_flip_cli_execute_persists_shadow_mode_and_checkpoint(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    editions_root = reports_root.parent / "editions"
    programs_root = reports_root.parent / "programs"
    archive_root = tmp_path / "archive"
    reality_db_root = tmp_path / "vertex-db"
    _write_confirmed_archive_entries(
        archive_root,
        generated_ats=(
            datetime(2026, 5, 28, 9, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 29, 9, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc),
        ),
    )
    generated_at = datetime(2026, 5, 28, 9, 0, tzinfo=timezone.utc)
    append_action("acme", _build_action_item(created_at=generated_at - timedelta(days=1)), programs_root=programs_root)
    append_claim_entry(_build_claim_entry(), programs_root=programs_root)
    append_claim_status_update(
        "acme",
        _build_claim_status_update(updated_at=generated_at - timedelta(hours=2)),
        programs_root=programs_root,
    )
    append_decision_ask(_build_decision_ask(), programs_root=programs_root)
    save_assumptions("acme", (_build_assumption(),), programs_root=programs_root)
    save_decisions("acme", (_build_decision_entry(),), programs_root=programs_root)
    advance_trusted_baseline(
        "acme_weekly",
        12,
        established_at=generated_at - timedelta(hours=2),
        established_by="alex",
        editions_root=editions_root,
        programs_root=programs_root,
    )
    persist_program_fact_snapshot(
        load_program_facts("acme", programs_root=programs_root, editions_root=editions_root, archive_root=archive_root),
        recorded_at=generated_at - timedelta(hours=1),
        db_root=reality_db_root,
    )
    result = runner.invoke(
        app,
        [
            "admin",
            "fact-store-flip",
            "--program",
            "acme",
            "--to",
            EDITION_NAME,
            "--execute",
            "--editions-root",
            str(editions_root),
            "--programs-root",
            str(programs_root),
            "--archive-root",
            str(archive_root),
            "--db-root",
            str(reality_db_root),
        ],
    )

    state = load_fact_sor_state("acme", programs_root=programs_root)
    checkpoints = list_checkpoints("acme", programs_root=programs_root)

    assert result.exit_code == 0
    assert "Fact-store flip preview for acme/acme_weekly" in result.stdout
    assert "Family coverage: supported=actions,claims,claim_status_updates,decision_asks,assumptions,decisions,risks,dependencies,milestones,workstreams,workstream_associations,baseline_trust_events,skip_issues | pending=none" in result.stdout
    assert "Execution complete: sor_mode=shadow; checkpoint=" in result.stdout
    assert state is not None
    assert state.mode == "shadow"
    assert len(checkpoints) == 1
    assert checkpoints[0].name.startswith("issue_003_")


def test_admin_fact_store_flip_cli_commit_promotes_shadow_to_primary(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    editions_root = reports_root.parent / "editions"
    programs_root = reports_root.parent / "programs"
    archive_root = tmp_path / "archive"
    reality_db_root = tmp_path / "vertex-db"
    generated_ats = (
        datetime(2026, 5, 28, 9, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 29, 9, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc),
    )

    _write_confirmed_archive_entries(archive_root, generated_ats=generated_ats)
    append_action("acme", _build_action_item(created_at=generated_ats[0] - timedelta(days=1)), programs_root=programs_root)
    append_claim_entry(_build_claim_entry(), programs_root=programs_root)
    append_claim_status_update("acme", _build_claim_status_update(updated_at=generated_ats[0] - timedelta(hours=2)), programs_root=programs_root)
    append_decision_ask(_build_decision_ask(), programs_root=programs_root)
    save_assumptions("acme", (_build_assumption(),), programs_root=programs_root)
    save_decisions("acme", (_build_decision_entry(),), programs_root=programs_root)
    advance_trusted_baseline(
        "acme_weekly",
        12,
        established_at=generated_ats[0] - timedelta(hours=2),
        established_by="alex",
        editions_root=editions_root,
        programs_root=programs_root,
    )
    persist_program_fact_snapshot(
        load_program_facts("acme", programs_root=programs_root, editions_root=editions_root, archive_root=archive_root),
        recorded_at=generated_ats[0] - timedelta(hours=1),
        db_root=reality_db_root,
    )
    save_fact_sor_state(
        "acme",
        mode="shadow",
        recorded_at=datetime(2026, 6, 7, 11, 0, tzinfo=timezone.utc),
        recorded_by="operator",
        programs_root=programs_root,
    )
    result = runner.invoke(
        app,
        [
            "admin",
            "fact-store-flip",
            "--program",
            "acme",
            "--to",
            EDITION_NAME,
            "--commit",
            "--editions-root",
            str(editions_root),
            "--programs-root",
            str(programs_root),
            "--archive-root",
            str(archive_root),
            "--db-root",
            str(reality_db_root),
        ],
    )

    state = load_fact_sor_state("acme", programs_root=programs_root)

    assert result.exit_code == 0
    assert "Commit complete: sor_mode=primary" in result.stdout
    assert state is not None
    assert state.mode == "primary"


def test_admin_fact_store_flip_cli_commit_requires_prior_execute(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    archive_root = tmp_path / "archive"
    _write_confirmed_archive_entries(
        archive_root,
        generated_ats=(datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc),),
    )
    result = runner.invoke(
        app,
        [
            "admin",
            "fact-store-flip",
            "--program",
            "acme",
            "--to",
            EDITION_NAME,
            "--commit",
            "--editions-root",
            str(reports_root.parent / "editions"),
            "--programs-root",
            str(programs_root),
            "--archive-root",
            str(archive_root),
            "--db-root",
            str(tmp_path / "vertex-db"),
        ],
    )

    assert result.exit_code == 1
    assert "must be executed to shadow mode before commit" in str(result.exception)


def test_run_fact_store_flip_preview_surfaces_failed_issue_mismatch_details(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    archive_root = tmp_path / "archive"
    generated_ats = (
        datetime(2026, 5, 28, 9, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 29, 9, 0, tzinfo=timezone.utc),
        datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc),
    )

    _write_confirmed_archive_entries(archive_root, generated_ats=generated_ats)
    append_action("acme", _build_action_item(created_at=generated_ats[0] - timedelta(days=1)), programs_root=programs_root)

    artifacts = run_fact_store_flip_preview(
        program_id="acme",
        edition_name=EDITION_NAME,
        editions_root=reports_root.parent / "editions",
        programs_root=programs_root,
        archive_root=archive_root,
        reality_db_root=tmp_path / "vertex-db",
    )

    assert artifacts.consecutive_parity_passes == 0
    assert artifacts.parity_window[0].issue_number == 3
    assert "actions" in artifacts.parity_window[0].mismatched_families
    assert any(blocker.startswith("parity mismatch at issue #3: ") for blocker in artifacts.blockers)


def _build_action_item(*, created_at: datetime) -> ActionItem:
    return ActionItem(
        id="action-1",
        program_id="acme",
        text="Follow up on launch gate",
        owner_alias="alex",
        due_date=date(2026, 6, 2),
        status=ActionStatus.OPEN,
        source_signal_id="signal-1",
        source_type=ActionSourceType.MANUAL,
        linked_work_item_ids=(),
        linked_claim_id=None,
        linked_risk_id=None,
        workstream_id=None,
        created_at=created_at,
        resolved_at=None,
        resolution_note=None,
    )


def _build_claim_entry() -> ClaimEntry:
    return ClaimEntry(
        id="claim-1",
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=12,
        workstream_id=None,
        text="Launch gate depends on partner readiness.",
        entity_refs=("WI:123",),
        claim_date=date(2026, 5, 29),
        owner_alias="alex",
        due_date=date(2026, 6, 4),
    )


def _build_decision_ask() -> DecisionAsk:
    return DecisionAsk(
        id="ask-1",
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=12,
        text="Need LT decision on launch sequencing.",
        entity_refs=("WI:124",),
        ask_date=date(2026, 5, 29),
        owner_alias="alex",
    )


def _build_decision_entry() -> DecisionEntry:
    return DecisionEntry(
        id="decision-1",
        program_id="acme",
        title="Approve launch exception",
        context="Partner dependency remains open.",
        decision="Proceed with mitigation plan.",
        rationale="Leadership accepted the temporary risk.",
        alternatives_considered=("Delay by one week",),
        decided_by="lt",
        decision_date=date(2026, 6, 3),
        status=DecisionStatus.DECIDED,
        superseded_by=None,
        linked_claim_id=None,
        linked_risk_id=None,
        linked_action_ids=(),
        workstream_id=None,
        entity_refs=(),
        review_by=date(2026, 6, 17),
        linked_milestone_ids=(),
        last_reviewed_date=date(2026, 6, 4),
    )


def _build_claim_status_update(*, updated_at: datetime) -> ClaimStatusUpdate:
    return ClaimStatusUpdate(
        claim_id="claim-1",
        new_status="stale",
        updated_at=updated_at,
        updated_by="alex",
        note="Waiting on refreshed evidence.",
    )


def _build_assumption() -> Assumption:
    return Assumption(
        id="assumption-1",
        program_id="acme",
        text="Partner schema lands before launch cutoff.",
        validation_method="Review partner schedule",
        validation_due=date(2026, 6, 8),
        status=AssumptionStatus.UNVALIDATED,
        category="schedule",
        linked_risk_id=None,
        linked_workstream_ids=(),
        linked_milestone_id=None,
        owner_alias="operator",
        identified_date=date(2026, 6, 1),
        entity_refs=(),
        resolved_date=None,
        linked_milestone_ids=(),
        last_reviewed_date=date(2026, 6, 5),
    )


def _write_confirmed_archive_entries(archive_root: Path, *, generated_ats: tuple[datetime, ...]) -> None:
    edition_root = get_archive_root(EDITION_NAME, archive_root=archive_root)
    edition_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "edition": EDITION_NAME,
        "issues": [
            {
                "issue_number": issue_number,
                "generated_at": generated_at.isoformat(),
                "kind": "confirmed",
            }
            for issue_number, generated_at in enumerate(generated_ats, start=1)
        ],
    }
    (edition_root / "index.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
