from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import typer

from src.commands.doctor import run_doctor
from src.core.action_tracker import append_action
from src.core.assumption_tracker import save_assumptions
from src.core.archive_store import write_skipped_issue
from src.core.claim_tracker import append_claim_entry, append_claim_status_update, append_decision_ask
from src.core.decision_register import save_decisions
from src.core.snapshot_store import get_archive_root
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus, Assumption, AssumptionStatus, ClaimEntry, ClaimStatusUpdate, DecisionAsk, DecisionEntry, DecisionStatus
from src.core.program_fact_store import load_program_facts, persist_program_fact_snapshot
from src.core.trusted_baseline_store import advance_trusted_baseline
from src.core.workstream_association_store import WorkstreamAssociationRecord, append_workstream_association_records
from tests.support.report_test_setup import stage_v2_report_workspace


EDITION_NAME = "acme_weekly"


def test_flip_parity_reports_ok_when_fact_store_matches_legacy(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    editions_root = reports_root.parent / "editions"
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    reality_db_root = tmp_path / "vertex-db"
    generated_at = datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc)

    _write_confirmed_archive_entries(archive_root, generated_ats=(generated_at,))
    append_action("acme", _build_action_item(created_at=generated_at - timedelta(days=1)), programs_root=programs_root)
    append_claim_entry(_build_claim_entry(), programs_root=programs_root)
    append_decision_ask(_build_decision_ask(), programs_root=programs_root)
    append_claim_status_update("acme", _build_claim_status_update(updated_at=generated_at - timedelta(hours=1)), programs_root=programs_root)
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
    append_workstream_association_records(
        "acme",
        (
            WorkstreamAssociationRecord(
                recorded_at=generated_at - timedelta(minutes=15),
                edition=EDITION_NAME,
                issue_number=1,
                workstream_id="deployment",
                source_type="review",
                work_item_id=12345,
                section_id="top-risks",
            ),
        ),
        programs_root=programs_root,
    )
    _write_skipped_archive_entry(
        archive_root,
        issue_number=4,
        generated_at=generated_at - timedelta(minutes=30),
        reason="Holiday week",
    )
    persist_program_fact_snapshot(
        load_program_facts("acme", programs_root=programs_root, editions_root=editions_root, archive_root=archive_root),
        recorded_at=generated_at - timedelta(hours=1),
        db_root=reality_db_root,
    )

    report = run_doctor(
        edition_name=EDITION_NAME,
        flip_parity=True,
        issue_number=1,
        editions_root=reports_root.parent / "editions",
        programs_root=programs_root,
        archive_root=archive_root,
        reality_db_root=reality_db_root,
    )

    checks = {check.label: check for check in report.checks}
    assert checks["Flip Parity Anchor"].status == "info"
    assert checks["Flip Parity"].status == "ok"
    assert checks["Flip Parity"].metadata is not None
    assert checks["Flip Parity"].metadata["mismatched_families"] == ()
    assert any(result["family"] == "claims" and result["matches"] for result in checks["Flip Parity"].metadata["family_results"])
    assert any(result["family"] == "claim_status_updates" and result["matches"] for result in checks["Flip Parity"].metadata["family_results"])
    assert any(result["family"] == "decision_asks" and result["matches"] for result in checks["Flip Parity"].metadata["family_results"])
    assert any(result["family"] == "workstream_associations" and result["matches"] for result in checks["Flip Parity"].metadata["family_results"])
    assert any(result["family"] == "baseline_trust_events" and result["matches"] for result in checks["Flip Parity"].metadata["family_results"])
    assert any(result["family"] == "skip_issues" and result["matches"] for result in checks["Flip Parity"].metadata["family_results"])
    assert any(result["family"] == "assumptions" and result["matches"] for result in checks["Flip Parity"].metadata["family_results"])
    assert any(result["family"] == "decisions" and result["matches"] for result in checks["Flip Parity"].metadata["family_results"])


def test_flip_parity_reports_fail_when_legacy_and_fact_store_diverge(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    generated_at = datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc)

    _write_confirmed_archive_entries(archive_root, generated_ats=(generated_at,))
    append_action("acme", _build_action_item(created_at=generated_at - timedelta(days=1)), programs_root=programs_root)

    report = run_doctor(
        edition_name=EDITION_NAME,
        flip_parity=True,
        issue_number=1,
        editions_root=reports_root.parent / "editions",
        programs_root=programs_root,
        archive_root=archive_root,
        reality_db_root=tmp_path / "vertex-db",
    )

    checks = {check.label: check for check in report.checks}
    assert checks["Flip Parity"].status == "fail"
    assert "actions(legacy=1, fact_store=0)" in checks["Flip Parity"].detail
    assert checks["Flip Parity"].metadata is not None
    assert "actions" in checks["Flip Parity"].metadata["mismatched_families"]


def test_flip_parity_requires_issue_number() -> None:
    with pytest.raises(typer.BadParameter, match="--flip-parity requires --issue"):
        run_doctor(flip_parity=True)


def test_flip_parity_fails_when_issue_is_missing(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"

    _write_confirmed_archive_entries(
        archive_root,
        generated_ats=(datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc),),
    )

    report = run_doctor(
        edition_name=EDITION_NAME,
        flip_parity=True,
        issue_number=2,
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
        archive_root=archive_root,
        reality_db_root=tmp_path / "vertex-db",
    )

    check = report.checks[0]
    assert check.label == "Flip Parity"
    assert check.status == "fail"
    assert "issue=2" in check.detail


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
    (edition_root / "index.json").write_text(__import__("json").dumps(payload, indent=2), encoding="utf-8")


def _write_skipped_archive_entry(
    archive_root: Path,
    *,
    issue_number: int,
    generated_at: datetime,
    reason: str,
) -> None:
    edition_root = get_archive_root(EDITION_NAME, archive_root=archive_root)
    edition_root.mkdir(parents=True, exist_ok=True)
    index_path = edition_root / "index.json"
    payload = __import__("json").loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {
        "schema_version": "1.0",
        "edition": EDITION_NAME,
        "issues": [],
    }
    payload.setdefault("issues", []).append(
        {
            "issue_number": issue_number,
            "generated_at": generated_at.isoformat(),
            "kind": "skipped",
            "reason": reason,
        }
    )
    payload["issues"] = sorted(payload["issues"], key=lambda entry: entry["issue_number"])
    index_path.write_text(__import__("json").dumps(payload, indent=2), encoding="utf-8")