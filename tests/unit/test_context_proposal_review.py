from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.commands.review_full import _build_context_revision_rows
from src.commands.triage import TriageArtifacts, _build_triage_payload, render_triage_output
from src.core.context_proposal_review import ContextProposalReviewRow, load_pending_context_proposal_rows
from src.core.cockpit_builder import build_cockpit_snapshot
from src.core.ncfl_models import ContextUpdateProposal
from src.core.ncfl_proposal_store import stage_extracted_proposals
from src.core.triage import ReadinessAssessment, TriageReport, render_triage_report


def _proposal(
    proposal_id: str,
    *,
    issue_number: int,
    target_store: str,
    conflict_key: str,
) -> ContextUpdateProposal:
    return ContextUpdateProposal(
        proposal_id=proposal_id, program_id="armada", issue_number=issue_number, edition_id="armada_weekly",
        source_type="confirmed_overrides", extracted_at=datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc),
        extractor_version="1.0.0", source_artifact="overrides/issue_001.yaml", source_field="scorecards.delivery.risk",
        extraction_method="overrides_yaml", target_store=target_store, target_key="delivery", target_field="risk_level",
        source_value="high", current_value="medium", current_value_hash="current-hash", confidence="high",
        batch_eligible=True, extraction_method_rationale="test fixture", conflict_key=conflict_key,
    )


def test_pending_rows_include_review_information_and_manual_only_command(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    writable = _proposal("proposal-writable", issue_number=7, target_store="risk_register", conflict_key="risk:delivery")
    registry = _proposal("proposal-registry", issue_number=8, target_store="workstream_registry", conflict_key="registry:delivery")
    conflicting_registry = _proposal("proposal-registry-conflict", issue_number=9, target_store="workstream_registry", conflict_key="registry:delivery")
    stage_extracted_proposals("armada", 7, (writable,), programs_root=programs_root)
    stage_extracted_proposals("armada", 8, (registry,), programs_root=programs_root)
    stage_extracted_proposals("armada", 9, (conflicting_registry,), programs_root=programs_root)

    rows = {row.proposal_id: row for row in load_pending_context_proposal_rows("armada", programs_root=programs_root)}

    assert rows["proposal-writable"].target == "risk_register.delivery.risk_level"
    assert rows["proposal-writable"].current_hash_label == "current-hash"
    assert rows["proposal-writable"].evidence == "overrides/issue_001.yaml:scorecards.delivery.risk"
    assert rows["proposal-writable"].conflict_state == "no cross-issue conflict"
    assert rows["proposal-writable"].next_command == "vertex context proposals --edition armada_weekly --issue 7"
    assert "proposal-registry-conflict" in rows["proposal-registry"].conflict_state
    assert rows["proposal-registry"].next_command == (
        "vertex context manual-diff --edition armada_weekly --issue 8 --proposal-id proposal-registry"
    )


def test_cockpit_surfaces_each_pending_context_revision(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposal = _proposal("proposal-1", issue_number=7, target_store="risk_register", conflict_key="risk:delivery")
    stage_extracted_proposals("armada", 7, (proposal,), programs_root=programs_root)

    snapshot = build_cockpit_snapshot("armada", programs_root=programs_root, now=datetime(2026, 7, 21, tzinfo=timezone.utc))

    finding = next(item for item in snapshot.findings if item.finding_id == "trust.context_proposal.proposal-1")
    assert finding.status == "warn"
    assert "current-hash" in finding.detail
    assert finding.next_command == "vertex context proposals --edition armada_weekly --issue 7"


def test_triage_rendering_includes_context_revision_review_data() -> None:
    row = ContextProposalReviewRow(
        proposal_id="proposal-1", edition_id="armada_weekly", issue_number=78,
        target="workstream_registry.delivery.owner", proposed_value="new-owner", current_value_hash="abc",
        evidence="source.yaml:owner", conflict_state="no cross-issue conflict",
        next_command="vertex context manual-diff --edition armada_weekly --issue 78 --proposal-id proposal-1",
    )
    report = TriageReport(
        edition_name="armada_weekly", issue_number=78, program_id="armada",
        readiness=ReadinessAssessment(100, 100, 1, 1, 0, 0, 0, 0, 1, 1, 1, 1),
        blockers=(), needs_attention=(), milestones=(), risks=(), actions=(), decisions=(), assumptions=(),
        cross_program_cascades=(), active_issues=(), coverage_gaps=(), ready=(), coverage_gap_window_days=14,
        context_proposals=(row,),
    )

    rendered = render_triage_report(report)

    assert "CONTEXT REVISIONS:" in rendered
    assert "workstream_registry.delivery.owner" in rendered
    assert "vertex context manual-diff" in rendered


def test_reviewer_context_rows_keep_manual_registry_boundary(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    proposal = _proposal("proposal-registry", issue_number=8, target_store="workstream_registry", conflict_key="registry:delivery")
    stage_extracted_proposals("armada", 8, (proposal,), programs_root=programs_root)

    rows = _build_context_revision_rows("armada", programs_root=programs_root)

    assert len(rows) == 1
    assert rows[0].title == "proposal-registry · workstream_registry.delivery.risk_level"
    assert "current-hash" in (rows[0].detail or "")
    assert "vertex context manual-diff" in rows[0].summary


def test_triage_json_and_csv_keep_context_revision_fields() -> None:
    row = ContextProposalReviewRow(
        proposal_id="proposal-1", edition_id="armada_weekly", issue_number=78,
        target="risk_register.delivery.risk_level", proposed_value="high", current_value_hash="abc",
        evidence="source.yaml:risk", conflict_state="no cross-issue conflict",
        next_command="vertex context proposals --edition armada_weekly --issue 78",
    )
    report = TriageReport(
        edition_name="armada_weekly", issue_number=78, program_id="armada",
        readiness=ReadinessAssessment(100, 100, 1, 1, 0, 0, 0, 0, 1, 1, 1, 1),
        blockers=(), needs_attention=(), milestones=(), risks=(), actions=(), decisions=(), assumptions=(),
        cross_program_cascades=(), active_issues=(), coverage_gaps=(), ready=(), coverage_gap_window_days=14,
        context_proposals=(row,),
    )
    artifacts = TriageArtifacts(report=report, exit_code=0)

    payload = _build_triage_payload(artifacts)
    csv_output = render_triage_output(artifacts, format="csv")

    assert payload["counts"]["context_revisions"] == 1
    assert payload["context_revisions"][0]["next_command"] == row.next_command
    assert "context_revision" in csv_output
    assert "current_value_hash" in csv_output
