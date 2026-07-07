from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.commands.admin_fact_store_migrate import run_migrate_legacy_state
from src.core.action_tracker import append_action
from src.core.assumption_tracker import save_assumptions
from src.core.archive_store import write_skipped_issue
from src.core.claim_tracker import append_claim_entry, append_claim_status_update, append_decision_ask
from src.core.decision_register import save_decisions
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus, Assumption, AssumptionStatus, ClaimEntry, ClaimStatusUpdate, DecisionAsk, DecisionEntry, DecisionStatus
from src.core.program_fact_store import ProgramFactStore
from src.core.trusted_baseline_store import advance_trusted_baseline
from tests.support.report_test_setup import stage_v2_report_workspace


runner = CliRunner()


def test_run_migrate_legacy_state_dry_run_reports_supported_fact_inventory(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    editions_root = reports_root.parent / "editions"
    programs_root = reports_root.parent / "programs"
    archive_root = tmp_path / "archive"
    append_action("acme", _build_action_item(), programs_root=programs_root)
    append_claim_entry(_build_claim_entry(), programs_root=programs_root)
    append_decision_ask(_build_decision_ask(), programs_root=programs_root)
    append_claim_status_update("acme", _build_claim_status_update(), programs_root=programs_root)
    save_assumptions("acme", (_build_assumption(),), programs_root=programs_root)
    save_decisions("acme", (_build_decision_entry(),), programs_root=programs_root)
    advance_trusted_baseline(
        "acme_weekly",
        12,
        established_at=datetime(2026, 5, 30, 8, 0, tzinfo=timezone.utc),
        established_by="alex",
        editions_root=editions_root,
        programs_root=programs_root,
    )
    write_skipped_issue("acme_weekly", 13, "Holiday week", archive_root=archive_root, acquire_lock=False)

    artifacts = run_migrate_legacy_state(
        program_id="acme",
        programs_root=programs_root,
        editions_root=editions_root,
        archive_root=archive_root,
        dry_run=True,
        db_root=tmp_path / "vertex-db",
    )

    assert artifacts.dry_run is True
    assert artifacts.fact_count >= 1
    assert any(path.endswith("actions.jsonl") for path in artifacts.source_inventory)
    assert any(path.endswith("claims.jsonl") for path in artifacts.source_inventory)
    assert any(path.endswith("trusted_baseline.yaml") for path in artifacts.source_inventory)
    assert any(path.endswith("assumptions.yaml") for path in artifacts.source_inventory)
    assert any(path.endswith("decisions.yaml") for path in artifacts.source_inventory)


def test_run_migrate_legacy_state_is_idempotent(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    editions_root = reports_root.parent / "editions"
    programs_root = reports_root.parent / "programs"
    archive_root = tmp_path / "archive"
    reality_db_root = tmp_path / "vertex-db"
    append_action("acme", _build_action_item(), programs_root=programs_root)
    append_claim_entry(_build_claim_entry(), programs_root=programs_root)
    append_decision_ask(_build_decision_ask(), programs_root=programs_root)
    append_claim_status_update("acme", _build_claim_status_update(), programs_root=programs_root)
    save_assumptions("acme", (_build_assumption(),), programs_root=programs_root)
    save_decisions("acme", (_build_decision_entry(),), programs_root=programs_root)
    advance_trusted_baseline(
        "acme_weekly",
        12,
        established_at=datetime(2026, 5, 30, 8, 0, tzinfo=timezone.utc),
        established_by="alex",
        editions_root=editions_root,
        programs_root=programs_root,
    )
    write_skipped_issue("acme_weekly", 13, "Holiday week", archive_root=archive_root, acquire_lock=False)

    first = run_migrate_legacy_state(
        program_id="acme",
        programs_root=programs_root,
        editions_root=editions_root,
        archive_root=archive_root,
        db_root=reality_db_root,
    )
    second = run_migrate_legacy_state(
        program_id="acme",
        programs_root=programs_root,
        editions_root=editions_root,
        archive_root=archive_root,
        db_root=reality_db_root,
    )

    assert first.created_count >= 1
    assert second.created_count == 0
    assert second.noop_count == second.fact_count
    assert ProgramFactStore("acme", db_root=reality_db_root).db_path.exists()


def test_admin_migrate_legacy_state_cli_runs(repo_root: Path, tmp_path: Path, monkeypatch) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    editions_root = reports_root.parent / "editions"
    programs_root = reports_root.parent / "programs"
    archive_root = tmp_path / "archive"
    append_action("acme", _build_action_item(), programs_root=programs_root)
    append_claim_entry(_build_claim_entry(), programs_root=programs_root)
    append_decision_ask(_build_decision_ask(), programs_root=programs_root)
    append_claim_status_update("acme", _build_claim_status_update(), programs_root=programs_root)
    save_assumptions("acme", (_build_assumption(),), programs_root=programs_root)
    save_decisions("acme", (_build_decision_entry(),), programs_root=programs_root)
    advance_trusted_baseline(
        "acme_weekly",
        12,
        established_at=datetime(2026, 5, 30, 8, 0, tzinfo=timezone.utc),
        established_by="alex",
        editions_root=editions_root,
        programs_root=programs_root,
    )
    write_skipped_issue("acme_weekly", 13, "Holiday week", archive_root=archive_root, acquire_lock=False)

    monkeypatch.setattr("src.commands.admin_fact_store_migrate.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.admin_fact_store_migrate.EDITIONS_ROOT", editions_root)
    monkeypatch.setattr("src.commands.admin_fact_store_migrate.ARCHIVE_ROOT", archive_root)

    result = runner.invoke(app, ["admin", "migrate-legacy-state", "--program", "acme", "--dry-run", "--db-root", str(tmp_path / "vertex-db")])

    assert result.exit_code == 0
    assert "storage_backend=file" in result.stdout
    assert "Dry-run: fact store was not modified." in result.stdout


def _build_action_item() -> ActionItem:
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
        created_at=datetime(2026, 5, 29, 9, 0, tzinfo=timezone.utc),
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


def _build_claim_status_update() -> ClaimStatusUpdate:
    return ClaimStatusUpdate(
        claim_id="claim-1",
        new_status="stale",
        updated_at=datetime(2026, 5, 30, 8, 30, tzinfo=timezone.utc),
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