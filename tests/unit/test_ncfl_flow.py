from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from cli import app
import src.commands.context as context_command
import src.core.ncfl_extractor as extractor_module
from src.core.context_snapshot_store import ContextSnapshot
from src.core.ncfl_models import ContextUpdateProposal
from src.core.ncfl_proposal_store import (
    conflicting_pending_proposals,
    load_proposals,
    stage_extracted_proposals,
    update_proposal_status,
)
from src.core.models import ConfirmedDimension, EditionType, RiskLevel, Snapshot, SnapshotItem
from src.core.models_v2 import Milestone, MilestoneStatus, RiskCategory, RiskEntry, RiskImpact, RiskProbability, RiskStatus
from src.core.overrides_store import DimensionOverride, OverridesDocument, ScorecardOverrides, Top3NowEntry
from src.core.snapshot_store import write_confirmed


runner = CliRunner()


def _proposal(
    proposal_id: str,
    *,
    issue_number: int,
    conflict_key: str,
    source_value: str,
    target_store: str = "risk_register",
    target_key: str = "control-plane",
    target_field: str = "dimension_risk_level",
) -> ContextUpdateProposal:
    return ContextUpdateProposal(
        proposal_id=proposal_id,
        program_id="acme",
        issue_number=issue_number,
        edition_id="acme_weekly",
        source_type="confirmed_overrides",
        extracted_at=datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc),
        extractor_version="1.0.0",
        source_artifact="overrides/issue_001.yaml",
        source_field="scorecards.delivery.control-plane.risk",
        extraction_method="overrides_yaml",
        target_store=target_store,
        target_key=target_key,
        target_field=target_field,
        source_value=source_value,
        current_value="medium",
        current_value_hash="abc",
        confidence="high",
        batch_eligible=True,
        extraction_method_rationale="test",
        conflict_key=conflict_key,
    )


def test_stage_extracted_proposals_supersedes_prior_pending_and_tracks_conflicts(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    first = _proposal("prop-1", issue_number=1, conflict_key="conflict-a", source_value="high")
    second = _proposal("prop-2", issue_number=1, conflict_key="conflict-a", source_value="low")
    third = _proposal("prop-3", issue_number=2, conflict_key="conflict-a", source_value="blocked")

    stage_extracted_proposals("acme", 1, (first,), programs_root=programs_root)
    stage_extracted_proposals("acme", 1, (second,), programs_root=programs_root)
    stage_extracted_proposals("acme", 2, (third,), programs_root=programs_root)

    issue_one = {proposal.proposal_id: proposal for proposal in load_proposals("acme", issue_number=1, programs_root=programs_root)}
    conflicts = conflicting_pending_proposals("acme", programs_root=programs_root)

    assert issue_one["prop-1"].status == "superseded"
    assert issue_one["prop-1"].superseded_by == "prop-2"
    assert issue_one["prop-2"].status == "pending"
    assert len(conflicts["conflict-a"]) == 2


def test_update_proposal_status_marks_dismissed_and_records_history(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposal = _proposal("prop-1", issue_number=1, conflict_key="conflict-a", source_value="high")
    stage_extracted_proposals("acme", 1, (proposal,), programs_root=programs_root)

    updated = update_proposal_status(
        "acme",
        proposal_id="prop-1",
        new_status="dismissed",
        actor="tester",
        rationale="not applicable",
        programs_root=programs_root,
    )

    assert updated.status == "dismissed"
    assert updated.dismissed_by == "tester"
    assert updated.rationale == "not applicable"
    assert updated.decision_history[-1].to_status == "dismissed"


def test_extract_proposals_combines_overrides_snapshot_and_context_diffs(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    reports_root = tmp_path / "reports"

    overrides = OverridesDocument(
        issue_number=1,
        top_3_now=(Top3NowEntry(type="decision", text="Need LT call", owner="", ado_link="", anchor=""),),
        scorecards=(
            ScorecardOverrides(
                name="Delivery",
                dimensions=(DimensionOverride(name="control-plane", risk=RiskLevel.HIGH),),
            ),
        ),
    )
    risk_entry = RiskEntry(
        id="risk-1",
        program_id="acme",
        title="control-plane",
        description="desc",
        probability=RiskProbability.POSSIBLE,
        impact=RiskImpact.MEDIUM,
        category=RiskCategory.SCHEDULE,
        owner_alias="old-owner",
        mitigation_plan=None,
        mitigation_due_date=None,
        linked_workstream_ids=("control-plane",),
        linked_work_item_ids=(),
        linked_milestone_ids=(),
        linked_claim_ids=(),
        linked_action_ids=(),
        status=RiskStatus.OPEN,
        identified_date=date(2026, 6, 1),
        identified_in_vertex_issue=None,
        last_reviewed_date=None,
        entity_refs=(),
        dimension_id="control-plane",
    )
    milestone = Milestone(
        id="m1",
        program_id="acme",
        name="Pilot",
        target_date=date(2026, 7, 1),
        owner_alias="owner",
        status=MilestoneStatus.ON_TRACK,
        exit_criteria=(),
        linked_workstream_ids=(),
        linked_work_item_ids=(101,),
    )
    snapshot = Snapshot(
        issue_number=1,
        generated_at=datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc),
        ado_data_as_of=datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc),
        edition_type=EditionType.DETAILED,
        items=(
            SnapshotItem(
                id=101,
                type="Bug",
                title="Done",
                state="Closed",
                assigned_to=None,
                area_path="Area",
                target_date=None,
                risk_level=RiskLevel.LOW,
                tags=[],
            ),
        ),
        scorecards=(
            ConfirmedDimension(
                scorecard_name="Delivery",
                name="control-plane",
                risk=RiskLevel.BLOCKED,
                prior_risk=None,
                item_count=1,
                ado_query_url="",
            ),
        ),
    )
    context_snapshot = ContextSnapshot(
        schema_version="1.1",
        issue_number=1,
        edition="acme_weekly",
        program_id="acme",
        confirmed_at=datetime(2026, 6, 27, 13, 0, tzinfo=timezone.utc),
        milestones=({"id": "m1", "status": "completed"},),
        risks=({"id": "risk-1", "owner_alias": "new-owner"},),
        workstreams=({"id": "ws1", "current_blocker": "Awaiting review", "dri_email": "new@example.com"},),
        decisions=(),
        plane1_change_count_since_prior=0,
    )

    archive_root = programs_root / "acme" / "archive" / "acme_weekly"
    write_confirmed("acme_weekly", 1, snapshot, archive_root=tmp_path / "archive")
    monkeypatch.setattr(extractor_module, "load_overrides", lambda *args, **kwargs: overrides)
    monkeypatch.setattr(extractor_module, "load_risk_register", lambda *args, **kwargs: (risk_entry,))
    monkeypatch.setattr(extractor_module, "load_decisions", lambda *args, **kwargs: ())
    monkeypatch.setattr(extractor_module, "load_milestones", lambda *args, **kwargs: (milestone,))
    monkeypatch.setattr(extractor_module, "load_context_snapshot", lambda *args, **kwargs: context_snapshot)
    monkeypatch.setattr(
        extractor_module,
        "resolve_edition_paths",
        lambda *args, **kwargs: SimpleNamespace(
            program_dir=programs_root / "acme",
            archive_dir=tmp_path / "archive" / "acme_weekly",
        ),
    )
    monkeypatch.setattr(extractor_module, "load_yaml_mapping", lambda *args, **kwargs: {"workstreams": [{"id": "ws1", "name": "WS1", "status": "active", "dri_email": "old@example.com", "current_blocker": "Old blocker"}]})

    proposals = extractor_module.extract_proposals(
        "acme",
        "acme_weekly",
        1,
        programs_root=programs_root,
        reports_root=reports_root,
    )

    target_fields = {(proposal.target_store, proposal.target_key, proposal.target_field) for proposal in proposals}
    assert ("risk_register", "control-plane", "dimension_risk_level") in target_fields
    assert ("decisions", "need-lt-call", "decision") in target_fields
    assert ("milestones", "m1", "status") in target_fields
    assert ("risk_register", "risk-1", "owner_alias") in target_fields
    assert ("workstreams", "ws1", "current_blocker") in target_fields
    assert ("workstreams", "ws1", "dri_email") in target_fields


def test_context_cli_extract_proposals_and_dismiss(monkeypatch) -> None:
    proposal = _proposal("prop-1", issue_number=1, conflict_key="conflict-a", source_value="high")
    monkeypatch.setattr(
        context_command,
        "resolve_edition_paths",
        lambda *args, **kwargs: SimpleNamespace(program_id="acme"),
    )
    monkeypatch.setattr(context_command, "extract_proposals", lambda *args, **kwargs: (proposal,))
    monkeypatch.setattr(context_command, "stage_extracted_proposals", lambda *args, **kwargs: (proposal,))
    monkeypatch.setattr(context_command, "load_proposals", lambda *args, **kwargs: (proposal,))
    monkeypatch.setattr(context_command, "conflicting_pending_proposals", lambda *args, **kwargs: {})
    monkeypatch.setattr(context_command, "update_proposal_status", lambda *args, **kwargs: proposal)

    extract_result = runner.invoke(app, ["context", "extract", "--edition", "acme_weekly", "--issue", "1"])
    proposals_result = runner.invoke(app, ["context", "proposals", "--edition", "acme_weekly"])
    dismiss_result = runner.invoke(
        app,
        ["context", "dismiss", "--edition", "acme_weekly", "--proposal-id", "prop-1", "--reason", "no longer needed"],
    )

    assert extract_result.exit_code == 0
    assert "Staged 1 extracted proposal" in extract_result.stdout
    assert proposals_result.exit_code == 0
    assert "1 proposal(s) for acme_weekly." in proposals_result.stdout
    assert dismiss_result.exit_code == 0
    assert "Dismissed proposal prop-1" in dismiss_result.stdout


def test_context_cli_blocked_commands_explain_stop_gates() -> None:
    # apply/apply-batch/synthesize now have required args; invoking without args
    # exits non-zero (Typer reports the missing options).
    apply_result = runner.invoke(app, ["context", "apply"])
    batch_result = runner.invoke(app, ["context", "apply-batch"])
    synthesize_result = runner.invoke(app, ["context", "synthesize"])

    # All three exit non-zero when required args are missing
    assert apply_result.exit_code != 0
    assert batch_result.exit_code != 0
    assert synthesize_result.exit_code != 0


def test_context_cli_synthesize_requires_accepted_proposals(monkeypatch) -> None:
    """Phase 5: synthesize exits code 2 with a clear message when no accepted
    Zone A proposals exist (and degrades cleanly when AI is unconfigured)."""
    monkeypatch.setattr(
        context_command,
        "resolve_edition_paths",
        lambda *args, **kwargs: SimpleNamespace(program_id="acme"),
    )
    monkeypatch.setattr(context_command, "load_proposals", lambda *args, **kwargs: ())

    result = runner.invoke(app, ["context", "synthesize", "--edition", "acme_weekly", "--issue", "79"])
    assert result.exit_code == 2
    assert "No accepted Zone A proposals" in result.stdout
