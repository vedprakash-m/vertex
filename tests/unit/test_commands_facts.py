from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from src.commands.facts import app, run_facts_parity_check
from src.core.action_tracker import append_action
from src.core.assumption_tracker import save_assumptions
from src.core.archive_store import write_skipped_issue
from src.core.claim_tracker import append_claim_entry, append_claim_status_update, append_decision_ask
from src.core.decision_register import save_decisions
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus, Assumption, AssumptionStatus, ClaimEntry, ClaimStatusUpdate, DecisionAsk, DecisionEntry, DecisionStatus
from src.core.program_fact_store import load_program_facts, persist_program_fact_snapshot
from src.core.trusted_baseline_store import advance_trusted_baseline
from src.core.workstream_association_store import (
    WorkstreamAssociationRecord,
    append_workstream_association_records,
)
from tests.support.report_test_setup import stage_v2_report_workspace


runner = CliRunner()


def test_run_facts_parity_check_passes_when_supported_families_match(repo_root: Path, tmp_path: Path) -> None:
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
    append_workstream_association_records(
        "acme",
        (_build_workstream_association_record(),),
        programs_root=programs_root,
    )
    advance_trusted_baseline(
        "acme_weekly",
        12,
        established_at=datetime(2026, 5, 30, 8, 0, tzinfo=timezone.utc),
        established_by="alex",
        editions_root=editions_root,
        programs_root=programs_root,
    )
    write_skipped_issue("acme_weekly", 13, "Holiday week", archive_root=archive_root, acquire_lock=False)
    persist_program_fact_snapshot(
        load_program_facts("acme", programs_root=programs_root, editions_root=editions_root, archive_root=archive_root),
        recorded_at=datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc),
        db_root=reality_db_root,
    )

    assessment = run_facts_parity_check(
        program_id="acme",
        programs_root=programs_root,
        editions_root=editions_root,
        archive_root=archive_root,
        db_root=reality_db_root,
    )

    assert assessment.passed is True
    assert assessment.parity_ratio == 1.0
    assert assessment.zero_tolerance_failures == ()
    assert next(result for result in assessment.family_results if result.family == "claims").matches is True
    assert next(result for result in assessment.family_results if result.family == "claim_status_updates").matches is True
    assert next(result for result in assessment.family_results if result.family == "decision_asks").matches is True
    assert next(result for result in assessment.family_results if result.family == "baseline_trust_events").matches is True
    assert next(result for result in assessment.family_results if result.family == "skip_issues").matches is True
    assert next(result for result in assessment.family_results if result.family == "assumptions").matches is True
    assert next(result for result in assessment.family_results if result.family == "decisions").matches is True
    assert next(result for result in assessment.family_results if result.family == "workstream_associations").matches is True


def test_run_facts_parity_check_includes_workstream_associations_in_supported_families(
    repo_root: Path, tmp_path: Path
) -> None:
    """Spec §22 Step 6: the parity-check assessment always includes the
    ``workstream_associations`` family even when no records exist (a
    zero-count match is a valid match — the spec asks the operator to see
    the family reported so any drift is visible in the dual-read window).
    """
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    editions_root = reports_root.parent / "editions"
    archive_root = tmp_path / "archive"
    reality_db_root = tmp_path / "vertex-db"

    assessment = run_facts_parity_check(
        program_id="acme",
        programs_root=programs_root,
        editions_root=editions_root,
        archive_root=archive_root,
        db_root=reality_db_root,
    )

    families = {result.family for result in assessment.family_results}
    assert "workstream_associations" in families
    workstream_assoc_result = next(
        result for result in assessment.family_results if result.family == "workstream_associations"
    )
    assert workstream_assoc_result.matches is True
    assert workstream_assoc_result.legacy_count == 0
    assert workstream_assoc_result.fact_store_count == 0


def test_run_facts_parity_check_flags_action_mismatch_as_zero_tolerance(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    editions_root = reports_root.parent / "editions"
    programs_root = reports_root.parent / "programs"
    archive_root = tmp_path / "archive"

    append_action("acme", _build_action_item(), programs_root=programs_root)
    append_claim_entry(_build_claim_entry(), programs_root=programs_root)
    append_decision_ask(_build_decision_ask(), programs_root=programs_root)
    append_claim_status_update(
        "acme",
        _build_claim_status_update(claim_id="ask-1", new_status="resolved"),
        programs_root=programs_root,
    )
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

    assessment = run_facts_parity_check(
        program_id="acme",
        programs_root=programs_root,
        editions_root=editions_root,
        archive_root=archive_root,
        db_root=tmp_path / "vertex-db",
    )

    assert assessment.passed is False
    assert "actions" in assessment.zero_tolerance_failures
    assert next(result for result in assessment.family_results if result.family == "claims").matches is False
    assert next(result for result in assessment.family_results if result.family == "claim_status_updates").matches is False
    assert next(result for result in assessment.family_results if result.family == "decision_asks").matches is True
    assert next(result for result in assessment.family_results if result.family == "baseline_trust_events").matches is False
    assert next(result for result in assessment.family_results if result.family == "skip_issues").matches is False
    assert next(result for result in assessment.family_results if result.family == "assumptions").matches is False
    assert next(result for result in assessment.family_results if result.family == "decisions").matches is False


def test_facts_parity_check_cli_returns_failure_for_mismatch(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    editions_root = reports_root.parent / "editions"
    archive_root = tmp_path / "archive"
    append_action("acme", _build_action_item(), programs_root=programs_root)

    monkeypatch.setattr("src.commands.facts.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.facts.EDITIONS_ROOT", editions_root)
    monkeypatch.setattr("src.commands.facts.ARCHIVE_ROOT", archive_root)

    result = runner.invoke(app, ["parity-check", "--program", "acme"])

    assert result.exit_code == 1
    assert "mismatches=assumptions" in result.stdout
    assert "zero_tolerance_failures=none" in result.stdout


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


def _build_claim_status_update(
    *,
    claim_id: str = "claim-1",
    new_status: str = "stale",
) -> ClaimStatusUpdate:
    return ClaimStatusUpdate(
        claim_id=claim_id,
        new_status=new_status,  # type: ignore[arg-type]
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


# ---------------------------------------------------------------------------
# dual-read shadow window + snapshot pinning (spec §22 Steps 4 + 8)
# ---------------------------------------------------------------------------


def test_facts_dual_read_log_writes_jsonl_per_cycle_and_returns_zero_when_passed(
    repo_root: Path, tmp_path: Path
) -> None:
    """Spec §22: dual-read shadow window logs one JSONL record per cycle to
    programs/<prog>/fact_store_parity_log.jsonl.  When the parity check
    passes for all cycles, the CLI exits 0."""
    import json

    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    archive_root = tmp_path / "archive"
    reality_db_root = tmp_path / "vertex-db"
    editions_root = reports_root.parent / "editions"
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
    persist_program_fact_snapshot(
        load_program_facts(
            "acme",
            programs_root=programs_root,
            editions_root=editions_root,
            archive_root=archive_root,
        ),
        recorded_at=datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc),
        db_root=reality_db_root,
    )

    result = runner.invoke(
        app,
        [
            "dual-read-log",
            "--program",
            "acme",
            "--cycles",
            "2",
            "--db-root",
            str(reality_db_root),
            "--editions-root",
            str(editions_root),
            "--archive-root",
            str(archive_root),
            "--programs-root",
            str(programs_root),
        ],
    )

    log_path = programs_root / "acme" / "fact_store_parity_log.jsonl"
    assert result.exit_code == 0
    assert log_path.exists()
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    assert len(records) == 2
    assert all(record["program_id"] == "acme" for record in records)
    assert [record["cycle_index"] for record in records] == [1, 2]
    assert all(record["passed"] is True for record in records)
    assert all(record["zero_tolerance_failures"] == [] for record in records)


def test_facts_dual_read_log_writes_quarantine_records_when_families_mismatch(
    repo_root: Path, tmp_path: Path
) -> None:
    """Spec §22: mismatched families are appended to a sibling
    fact_store_quarantine.jsonl so the operator has a per-cycle audit trail
    of which families drifted during the dual-read window."""
    import json

    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    archive_root = tmp_path / "archive"
    reality_db_root = tmp_path / "vertex-db"
    editions_root = reports_root.parent / "editions"
    append_action("acme", _build_action_item(), programs_root=programs_root)
    append_claim_entry(_build_claim_entry(), programs_root=programs_root)
    save_assumptions("acme", (_build_assumption(),), programs_root=programs_root)
    save_decisions("acme", (_build_decision_entry(),), programs_root=programs_root)
    persist_program_fact_snapshot(
        load_program_facts(
            "acme",
            programs_root=programs_root,
            editions_root=editions_root,
            archive_root=archive_root,
        ),
        recorded_at=datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc),
        db_root=reality_db_root,
    )
    # Inject a legacy-only action after the fact-store snapshot is captured,
    # so the parity check sees a mismatch in the actions family.
    append_action(
        "acme",
        _build_action_item(id="action-2", text="Late-added by editor"),
        programs_root=programs_root,
    )

    result = runner.invoke(
        app,
        [
            "dual-read-log",
            "--program",
            "acme",
            "--cycles",
            "1",
            "--db-root",
            str(reality_db_root),
            "--editions-root",
            str(editions_root),
            "--archive-root",
            str(archive_root),
            "--programs-root",
            str(programs_root),
        ],
    )

    quarantine_path = programs_root / "acme" / "fact_store_quarantine.jsonl"
    log_path = programs_root / "acme" / "fact_store_parity_log.jsonl"
    assert result.exit_code == 1
    assert log_path.exists()
    assert quarantine_path.exists()
    log_records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    assert log_records[0]["mismatched_families"] == ["actions"]
    assert log_records[0]["passed"] is False
    quarantine_records = [
        json.loads(line) for line in quarantine_path.read_text(encoding="utf-8").splitlines() if line
    ]
    assert len(quarantine_records) == 1
    assert quarantine_records[0]["program_id"] == "acme"
    assert quarantine_records[0]["cycle_index"] == 1
    mismatched = quarantine_records[0]["mismatched_families"]
    assert any(entry["family"] == "actions" for entry in mismatched)


def test_facts_pin_snapshot_records_pin_and_detect_drift_surfaces_post_pin_writes(
    repo_root: Path, tmp_path: Path
) -> None:
    """Spec §22 Step 8: pin-snapshot creates a pin row; detect-drift
    surfaces fact revisions recorded after the pin."""
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    archive_root = tmp_path / "archive"
    reality_db_root = tmp_path / "vertex-db"
    editions_root = reports_root.parent / "editions"
    append_action("acme", _build_action_item(), programs_root=programs_root)
    append_claim_entry(_build_claim_entry(), programs_root=programs_root)
    save_assumptions("acme", (_build_assumption(),), programs_root=programs_root)
    save_decisions("acme", (_build_decision_entry(),), programs_root=programs_root)
    persist_program_fact_snapshot(
        load_program_facts(
            "acme",
            programs_root=programs_root,
            editions_root=editions_root,
            archive_root=archive_root,
        ),
        recorded_at=datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc),
        db_root=reality_db_root,
    )

    pin_result = runner.invoke(
        app,
        [
            "pin-snapshot",
            "--program",
            "acme",
            "--issue-number",
            "78",
            "--db-root",
            str(reality_db_root),
        ],
    )
    assert pin_result.exit_code == 0
    assert "Pinned fact snapshot for acme @ issue #78" in pin_result.stdout
    pin_id = pin_result.stdout.strip().split("→ ")[-1]
    assert pin_id.startswith("pfs_")

    # Post-pin fact write: append a new action and persist a fresh snapshot.
    append_action(
        "acme",
        _build_action_item(id="action-postpin", text="Added after pin"),
        programs_root=programs_root,
    )
    persist_program_fact_snapshot(
        load_program_facts(
            "acme",
            programs_root=programs_root,
            editions_root=editions_root,
            archive_root=archive_root,
        ),
        recorded_at=datetime(2026, 5, 30, 10, 0, tzinfo=timezone.utc),
        db_root=reality_db_root,
    )

    drift_result = runner.invoke(
        app,
        [
            "detect-drift",
            "--program",
            "acme",
            "--snapshot-id",
            pin_id,
            "--db-root",
            str(reality_db_root),
        ],
    )
    # Exit 2 is the documented "drift detected" code.
    assert drift_result.exit_code == 2
    assert f"Drift since {pin_id}: 1 revision(s)" in drift_result.stdout


def _build_action_item(*, id: str = "action-1", text: str = "Follow up on launch gate") -> ActionItem:
    return ActionItem(
        id=id,
        program_id="acme",
        text=text,
        owner_alias="alex",
        due_date=date(2026, 6, 2),
        status=ActionStatus.OPEN,
        source_signal_id="signal-1",
        source_type=ActionSourceType.MANUAL,
        linked_work_item_ids=(),
        linked_claim_id=None,
        linked_risk_id=None,
        workstream_id=None,
        created_at=datetime(2026, 5, 30, 8, 0, tzinfo=timezone.utc),
        resolved_at=None,
        resolution_note=None,
    )


def _build_workstream_association_record() -> WorkstreamAssociationRecord:
    return WorkstreamAssociationRecord(
        recorded_at=datetime(2026, 5, 30, 8, 30, tzinfo=timezone.utc),
        edition="acme_weekly",
        issue_number=12,
        workstream_id="deployment",
        source_type="review",
        source_slice_id=None,
        section_id="ws:deployment",
        work_item_id=12345,
        note="mapped during confirm",
    )