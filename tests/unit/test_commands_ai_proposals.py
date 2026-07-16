"""Tests for the ADF v1.51 shared AI-proposal review CLI (`vertex ai-proposals`).

Covers list/accept/reject for all five human-reviewable AISchemaGateway
proposal types, verifying each accept path's terminal effect (risk register
update, dependency update, ADO outbox routing, top_3_now publication) plus
the shared error/idempotency guards (already-decided proposal, unknown
type, --dry-run no-op).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from cli import app
from src.core.adoption_telemetry import GoldenWorkflow, read_adoption_events
from src.core.ai_review_proposal_store import load_proposal, stage_proposal
from src.core.proposal_audit import read_proposal_audit
from src.core.proposal_autonomy_ladder import advance_proposal_class_autonomy, promote_proposal_class_explicit
from src.core.dependency_blast_radius import DependencyBlastRadiusProposal
from src.core.dependency_graph import save_dependencies
from src.core.governance_decision_brief import GovernanceDecisionBriefProposal, GovernanceDecisionOption
from src.core.meeting_action import MeetingAction
from src.core.models_v2 import (
    Dependency,
    DependencyStatus,
    DependencyType,
    RiskCategory,
    RiskEntry,
    RiskImpact,
    RiskKind,
    RiskProbability,
    RiskStatus,
)
from src.core.overrides_store import OverridesDocument, load_overrides, save_overrides
from src.core.risk_proposal import RiskProposal
from src.core.risk_register_engine import save_risk_register
from src.core.top_three_candidates import TopThreeCandidateProposal

runner = CliRunner()


def _risk_proposal(**overrides: object) -> RiskProposal:
    defaults: dict[str, object] = dict(
        id="risk-proposal-1",
        program_id="acme",
        candidate_risk_id="risk-candidate-1",
        causal_title="Vendor SDK delay blocks integration milestone",
        why_it_matters="The integration milestone cannot complete without the vendor SDK.",
        probability=RiskProbability.LIKELY,
        impact=RiskImpact.HIGH,
        category=RiskCategory.EXTERNAL,
        mitigation="Escalate to vendor account team.",
        owner_alias="jordanr",
        by_when=date(2026, 8, 1),
        fallback="Build an internal shim if vendor slips past July.",
        evidence_refs=("signal-1",),
        ai_run_id="ai-run-1",
        proposed_at=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return RiskProposal(**defaults)  # type: ignore[arg-type]


def _candidate_risk_entry(**overrides: object) -> RiskEntry:
    defaults: dict[str, object] = dict(
        id="risk-candidate-1",
        program_id="acme",
        title="Vendor SDK delay",
        description="Machine-detected candidate risk.",
        probability=RiskProbability.UNASSESSED,
        impact=RiskImpact.UNASSESSED,
        category=RiskCategory.EXTERNAL,
        owner_alias="unassigned",
        mitigation_plan=None,
        mitigation_due_date=None,
        linked_workstream_ids=(),
        linked_work_item_ids=(),
        linked_milestone_ids=(),
        linked_claim_ids=(),
        linked_action_ids=(),
        status=RiskStatus.OPEN,
        identified_date=date(2026, 7, 1),
        identified_in_vertex_issue=None,
        last_reviewed_date=None,
        entity_refs=(),
        kind=RiskKind.CANDIDATE.value,
    )
    defaults.update(overrides)
    return RiskEntry(**defaults)  # type: ignore[arg-type]


def _meeting_action(**overrides: object) -> MeetingAction:
    defaults: dict[str, object] = dict(
        id="action-1",
        program_id="acme",
        meeting_ref="2026-07-01-standup",
        commitment="Send the updated timeline to stakeholders.",
        owner_alias="jordanr",
        due_date=date(2026, 7, 10),
        linked_work_item_id=1234,
        blocks=(),
        source_span="Action: Send the updated timeline | owner=jordanr | due=2026-07-10 | wi=1234",
        extraction_method="deterministic",
        proposed_at=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return MeetingAction(**defaults)  # type: ignore[arg-type]


def _top_three(**overrides: object) -> TopThreeCandidateProposal:
    defaults: dict[str, object] = dict(
        id="top3-1",
        program_id="acme",
        item_id="risk-candidate-1",
        reason="Highest-severity open risk this cycle.",
        evidence_refs=("signal-1",),
        urgency="high",
        decision_or_action_needed="Escalate vendor timeline.",
        owner_alias="jordanr",
        confidence="high",
        ai_run_id="ai-run-2",
        proposed_at=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return TopThreeCandidateProposal(**defaults)  # type: ignore[arg-type]


def _governance_brief(**overrides: object) -> GovernanceDecisionBriefProposal:
    defaults: dict[str, object] = dict(
        id="brief-1",
        program_id="acme",
        decision_ask_id="ask-1",
        decision="Should we switch vendors?",
        context="Vendor has slipped twice this quarter.",
        options=(GovernanceDecisionOption(label="Switch vendors", tradeoffs="6-week onboarding delay."),),
        recommendation="Switch vendors given repeated slips.",
        consequences_of_delay="Milestone slips a further 4 weeks.",
        owner_alias="jordanr",
        due_date=date(2026, 7, 15),
        evidence_refs=("signal-3",),
        ai_run_id="ai-run-3",
        proposed_at=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return GovernanceDecisionBriefProposal(**defaults)  # type: ignore[arg-type]


def _blast_radius(**overrides: object) -> DependencyBlastRadiusProposal:
    defaults: dict[str, object] = dict(
        id="blast-1",
        program_id="acme",
        dependency_id="dep-1",
        next_proving_event="Vendor integration test on 2026-07-20.",
        blast_radius_narrative="A missed vendor delivery cascades into the GA milestone.",
        evidence_refs=("signal-4",),
        ai_run_id="ai-run-4",
        proposed_at=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return DependencyBlastRadiusProposal(**defaults)  # type: ignore[arg-type]


def _dependency(**overrides: object) -> Dependency:
    defaults: dict[str, object] = dict(
        id="dep-1",
        from_program_id="acme",
        from_workstream_id="ws-1",
        from_item_id=None,
        from_milestone_id=None,
        to_program_id="acme",
        to_workstream_id="ws-2",
        to_item_id=None,
        to_milestone_id=None,
        dependency_type=DependencyType.BLOCKS,
        risk_if_broken="GA milestone slips.",
        mitigation=None,
        status=DependencyStatus.ACTIVE,
        owner_alias="jordanr",
    )
    defaults.update(overrides)
    return Dependency(**defaults)  # type: ignore[arg-type]


def _patch_roots(monkeypatch, tmp_path: Path) -> Path:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.ai_proposals.PROGRAMS_ROOT", programs_root)
    return programs_root


def test_list_reports_no_proposals(monkeypatch, tmp_path: Path) -> None:
    _patch_roots(monkeypatch, tmp_path)
    result = runner.invoke(app, ["ai-proposals", "list", "--program", "acme"])
    assert result.exit_code == 0
    assert "No staged proposals for acme" in result.stdout


def test_list_shows_staged_proposal(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    stage_proposal("acme", "risk", _risk_proposal(), programs_root=programs_root)

    result = runner.invoke(app, ["ai-proposals", "list", "--program", "acme"])
    assert result.exit_code == 0
    assert "risk-proposal-1" in result.stdout
    assert "Vendor SDK delay" in result.stdout


def test_accept_risk_proposal_promotes_candidate_and_records_adoption(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    stage_proposal("acme", "risk", _risk_proposal(), programs_root=programs_root)
    save_risk_register("acme", (_candidate_risk_entry(),), programs_root=programs_root)

    result = runner.invoke(
        app, ["ai-proposals", "accept", "--program", "acme", "--type", "risk", "--id", "risk-proposal-1"]
    )
    assert result.exit_code == 0, result.stdout
    assert "promoted candidate -> strategic" in result.stdout

    approved = load_proposal("acme", "risk", "risk-proposal-1", programs_root=programs_root)
    assert approved is not None
    assert approved.status == "approved"

    events = read_adoption_events("acme", programs_root=programs_root)
    assert any(event.workflow == GoldenWorkflow.RISK_DEPENDENCY_REVIEW for event in events)


def test_reject_risk_proposal_requires_reason(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    stage_proposal("acme", "risk", _risk_proposal(), programs_root=programs_root)

    missing_reason = runner.invoke(
        app, ["ai-proposals", "reject", "--program", "acme", "--type", "risk", "--id", "risk-proposal-1", "--reason", "  "]
    )
    assert missing_reason.exit_code != 0

    result = runner.invoke(
        app,
        [
            "ai-proposals", "reject", "--program", "acme", "--type", "risk",
            "--id", "risk-proposal-1", "--reason", "Duplicate of an existing strategic risk.",
        ],
    )
    assert result.exit_code == 0
    rejected = load_proposal("acme", "risk", "risk-proposal-1", programs_root=programs_root)
    assert rejected is not None
    assert rejected.status == "rejected"
    assert rejected.rejection_reason == "Duplicate of an existing strategic risk."


def test_accept_already_decided_proposal_fails(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    stage_proposal(
        "acme", "risk",
        _risk_proposal(status="approved", decided_at=datetime(2026, 7, 2, tzinfo=timezone.utc)),
        programs_root=programs_root,
    )
    result = runner.invoke(
        app, ["ai-proposals", "accept", "--program", "acme", "--type", "risk", "--id", "risk-proposal-1"]
    )
    assert result.exit_code != 0


def test_accept_dry_run_makes_no_changes(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    stage_proposal("acme", "risk", _risk_proposal(), programs_root=programs_root)
    save_risk_register("acme", (_candidate_risk_entry(),), programs_root=programs_root)

    result = runner.invoke(
        app,
        ["ai-proposals", "accept", "--program", "acme", "--type", "risk", "--id", "risk-proposal-1", "--dry-run"],
    )
    assert result.exit_code == 0
    assert "[dry-run]" in result.stdout

    still_staged = load_proposal("acme", "risk", "risk-proposal-1", programs_root=programs_root)
    assert still_staged is not None
    assert still_staged.status == "staged"


def test_accept_meeting_action_without_org_project_only_approves(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    stage_proposal("acme", "meeting_action", _meeting_action(), programs_root=programs_root)

    result = runner.invoke(
        app, ["ai-proposals", "accept", "--program", "acme", "--type", "meeting_action", "--id", "action-1"]
    )
    assert result.exit_code == 0
    assert "--org and --project" in result.stdout

    approved = load_proposal("acme", "meeting_action", "action-1", programs_root=programs_root)
    assert approved is not None
    assert approved.status == "approved"


def test_accept_meeting_action_with_org_project_routes_to_outbox(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    stage_proposal("acme", "meeting_action", _meeting_action(), programs_root=programs_root)

    result = runner.invoke(
        app,
        [
            "ai-proposals", "accept", "--program", "acme", "--type", "meeting_action", "--id", "action-1",
            "--org", "contoso", "--project", "vertex-proj",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "routed to ADO outbox entry" in result.stdout

    events = read_adoption_events("acme", programs_root=programs_root)
    assert any(event.workflow == GoldenWorkflow.MEETING_TO_ACTION for event in events)


def test_accept_top_three_without_edition_prints_entry_only(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    stage_proposal("acme", "top_three", _top_three(), programs_root=programs_root)

    result = runner.invoke(
        app, ["ai-proposals", "accept", "--program", "acme", "--type", "top_three", "--id", "top3-1"]
    )
    assert result.exit_code == 0
    assert "Pass --edition to publish" in result.stdout

    approved = load_proposal("acme", "top_three", "top3-1", programs_root=programs_root)
    assert approved is not None
    assert approved.status == "approved"


def test_accept_top_three_with_edition_publishes_to_overrides(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    reports_root = programs_root.parent / "reports"
    monkeypatch.setattr("src.commands.ai_proposals.REPORTS_ROOT", reports_root)
    stage_proposal("acme", "top_three", _top_three(), programs_root=programs_root)

    document = OverridesDocument(issue_number=None, top_3_now=(), scorecards=())
    save_overrides("acme_weekly", document, reports_root=reports_root)

    result = runner.invoke(
        app,
        [
            "ai-proposals", "accept", "--program", "acme", "--type", "top_three", "--id", "top3-1",
            "--edition", "acme_weekly",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "published to 'acme_weekly'" in result.stdout

    updated = load_overrides("acme_weekly", reports_root=reports_root)
    assert updated is not None
    assert len(updated.top_3_now) == 1
    assert "Highest-severity open risk this cycle." in updated.top_3_now[0].text


def test_accept_governance_decision_brief_approves_only(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    stage_proposal("acme", "governance_decision_brief", _governance_brief(), programs_root=programs_root)

    result = runner.invoke(
        app,
        ["ai-proposals", "accept", "--program", "acme", "--type", "governance_decision_brief", "--id", "brief-1"],
    )
    assert result.exit_code == 0, result.stdout

    approved = load_proposal("acme", "governance_decision_brief", "brief-1", programs_root=programs_root)
    assert approved is not None
    assert approved.status == "approved"


def test_accept_dependency_blast_radius_updates_dependency(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    stage_proposal("acme", "dependency_blast_radius", _blast_radius(), programs_root=programs_root)
    save_dependencies("acme", (_dependency(),), programs_root=programs_root)

    result = runner.invoke(
        app,
        ["ai-proposals", "accept", "--program", "acme", "--type", "dependency_blast_radius", "--id", "blast-1"],
    )
    assert result.exit_code == 0, result.stdout

    from src.core.dependency_graph import load_dependencies

    updated = load_dependencies("acme", programs_root=programs_root)
    assert updated[0].next_proving_event == "Vendor integration test on 2026-07-20."
    assert updated[0].blast_radius_narrative == "A missed vendor delivery cascades into the GA milestone."


def test_accept_unknown_type_fails(monkeypatch, tmp_path: Path) -> None:
    _patch_roots(monkeypatch, tmp_path)
    result = runner.invoke(
        app, ["ai-proposals", "accept", "--program", "acme", "--type", "not_a_type", "--id", "x"]
    )
    assert result.exit_code != 0


def test_accept_missing_proposal_fails(monkeypatch, tmp_path: Path) -> None:
    _patch_roots(monkeypatch, tmp_path)
    result = runner.invoke(
        app, ["ai-proposals", "accept", "--program", "acme", "--type", "risk", "--id", "does-not-exist"]
    )
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# `generate` (P2, v1.52 deep-dive plan): the on-demand generation trigger.
# The AI generators (`generate_risk_proposal`/`run_meeting_action_extraction_
# pipeline`) already have their own dedicated unit tests -- these tests
# monkeypatch them directly to exercise only this module's own wiring: AI
# mode/config gating, deployment resolution, and staging the result.
# ---------------------------------------------------------------------------


def _write_ai_enabled_program(programs_root: Path, *, enabled: bool = True) -> None:
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        f"""
schema_version: '2.0'
id: acme
name: Acme
ai:
  enabled: {"true" if enabled else "false"}
  budget_usd_per_run: 0.5
  temperature: 0.2
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_generate_risk_stages_proposal(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    _write_ai_enabled_program(programs_root)
    monkeypatch.setattr(
        "src.commands.ai_proposals.resolve_ai_deployments_for_feature", lambda **kwargs: ("fake-deployment",)
    )
    monkeypatch.setattr(
        "src.commands.ai_proposals.generate_risk_proposal",
        lambda request, *, client, programs_root: _risk_proposal(
            id="risk-proposal-generated", candidate_risk_id=request.candidate_risk_id
        ),
    )

    result = runner.invoke(
        app,
        [
            "ai-proposals", "generate", "--program", "acme", "--type", "risk",
            "--candidate-risk-id", "risk-candidate-1", "--title", "Vendor delay",
            "--description", "Multiple signals mention a vendor delay.",
            "--evidence-text", "Vendor X reported a delay.", "--evidence-ref", "sig-1",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "risk-proposal-generated" in result.stdout

    staged = load_proposal("acme", "risk", "risk-proposal-generated", programs_root=programs_root)
    assert staged is not None
    assert staged.status == "staged"


def test_generate_risk_discarded_reports_no_staging(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    _write_ai_enabled_program(programs_root)
    monkeypatch.setattr(
        "src.commands.ai_proposals.resolve_ai_deployments_for_feature", lambda **kwargs: ("fake-deployment",)
    )
    monkeypatch.setattr(
        "src.commands.ai_proposals.generate_risk_proposal", lambda request, *, client, programs_root: None
    )

    result = runner.invoke(
        app,
        [
            "ai-proposals", "generate", "--program", "acme", "--type", "risk",
            "--candidate-risk-id", "risk-candidate-1", "--title", "Vendor delay",
            "--description", "Multiple signals mention a vendor delay.",
            "--evidence-text", "Vendor X reported a delay.",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "discarded or rejected" in result.stdout
    assert load_proposal("acme", "risk", "risk-proposal-generated", programs_root=programs_root) is None


def test_generate_risk_requires_type_specific_fields(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    _write_ai_enabled_program(programs_root)
    monkeypatch.setattr(
        "src.commands.ai_proposals.resolve_ai_deployments_for_feature", lambda **kwargs: ("fake-deployment",)
    )

    result = runner.invoke(app, ["ai-proposals", "generate", "--program", "acme", "--type", "risk"])
    assert result.exit_code != 0


def test_generate_meeting_action_stages_staged_and_rejected_actions(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    _write_ai_enabled_program(programs_root)
    monkeypatch.setattr(
        "src.commands.ai_proposals.resolve_ai_deployments_for_feature", lambda **kwargs: ("fake-deployment",)
    )

    staged_action = MeetingAction(
        id="ma-staged", program_id="acme", meeting_ref="m1", commitment="Follow up with legal",
        owner_alias="priya", due_date=None, linked_work_item_id=1001, blocks=(), source_span="span one",
        extraction_method="llm", status="staged",
    )
    rejected_action = MeetingAction(
        id="ma-rejected", program_id="acme", meeting_ref="m1", commitment="Fabricated commitment",
        owner_alias=None, due_date=None, linked_work_item_id=None, blocks=(), source_span="",
        extraction_method="llm", status="rejected", rejection_reason="source_span is empty.",
    )

    from src.ai.meeting_action_extractor import MeetingActionExtractionResult

    monkeypatch.setattr(
        "src.commands.ai_proposals.run_meeting_action_extraction_pipeline",
        lambda **kwargs: MeetingActionExtractionResult(actions=(staged_action, rejected_action), warnings=()),
    )

    transcript_file = tmp_path / "transcript.txt"
    transcript_file.write_text("We agreed that Priya will follow up with legal.", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "ai-proposals", "generate", "--program", "acme", "--type", "meeting_action",
            "--meeting-ref", "m1", "--transcript-file", str(transcript_file), "--work-item-id", "1001",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "1 staged for review, 1 rejected by validation" in result.stdout

    assert load_proposal("acme", "meeting_action", "ma-staged", programs_root=programs_root) is not None
    stored_rejected = load_proposal("acme", "meeting_action", "ma-rejected", programs_root=programs_root)
    assert stored_rejected is not None
    assert stored_rejected.status == "rejected"


def test_generate_governance_decision_brief_stages_proposal(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    _write_ai_enabled_program(programs_root)
    monkeypatch.setattr(
        "src.commands.ai_proposals.resolve_ai_deployments_for_feature", lambda **kwargs: ("fake-deployment",)
    )
    monkeypatch.setattr(
        "src.commands.ai_proposals.generate_governance_decision_brief",
        lambda request, *, client, programs_root: _governance_brief(
            id="brief-generated", decision_ask_id=request.decision_ask_id
        ),
    )

    result = runner.invoke(
        app,
        [
            "ai-proposals", "generate", "--program", "acme", "--type", "governance_decision_brief",
            "--decision-ask-id", "ask-1", "--decision-text", "Should we switch vendors?",
            "--evidence-text", "Vendor has slipped twice.", "--evidence-ref", "sig-3",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "brief-generated" in result.stdout

    staged = load_proposal("acme", "governance_decision_brief", "brief-generated", programs_root=programs_root)
    assert staged is not None
    assert staged.status == "staged"


def test_generate_governance_decision_brief_requires_type_specific_fields(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    _write_ai_enabled_program(programs_root)
    monkeypatch.setattr(
        "src.commands.ai_proposals.resolve_ai_deployments_for_feature", lambda **kwargs: ("fake-deployment",)
    )
    result = runner.invoke(
        app, ["ai-proposals", "generate", "--program", "acme", "--type", "governance_decision_brief"]
    )
    assert result.exit_code != 0


def test_generate_dependency_blast_radius_stages_proposal(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    _write_ai_enabled_program(programs_root)
    monkeypatch.setattr(
        "src.commands.ai_proposals.resolve_ai_deployments_for_feature", lambda **kwargs: ("fake-deployment",)
    )
    monkeypatch.setattr(
        "src.commands.ai_proposals.generate_dependency_blast_radius_proposal",
        lambda request, *, client, programs_root: _blast_radius(
            id="blast-generated", dependency_id=request.dependency_id
        ),
    )

    result = runner.invoke(
        app,
        [
            "ai-proposals", "generate", "--program", "acme", "--type", "dependency_blast_radius",
            "--dependency-id", "dep-1", "--from-summary", "Upstream API team",
            "--to-summary", "Downstream GA milestone", "--risk-if-broken", "GA slips.",
            "--current-status", "active", "--evidence-text", "Vendor confirmed the date.",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "blast-generated" in result.stdout

    staged = load_proposal("acme", "dependency_blast_radius", "blast-generated", programs_root=programs_root)
    assert staged is not None
    assert staged.status == "staged"


def test_generate_dependency_blast_radius_requires_type_specific_fields(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    _write_ai_enabled_program(programs_root)
    monkeypatch.setattr(
        "src.commands.ai_proposals.resolve_ai_deployments_for_feature", lambda **kwargs: ("fake-deployment",)
    )
    result = runner.invoke(
        app,
        ["ai-proposals", "generate", "--program", "acme", "--type", "dependency_blast_radius",
         "--dependency-id", "dep-1"],
    )
    assert result.exit_code != 0


def _write_candidates_file(tmp_path: Path, items: list[dict[str, object]]) -> Path:
    path = tmp_path / "candidates.json"
    path.write_text(json.dumps({"items": items}), encoding="utf-8")
    return path


def test_generate_top_three_stages_selected_candidates(monkeypatch, tmp_path: Path) -> None:
    # ADF-W4.8: closes the last of the 5 proposal types' on-demand generate
    # gap via --candidates-file, a structured JSON input the flat scalar
    # CLI-flag pattern used by the other 4 types can't responsibly represent.
    programs_root = _patch_roots(monkeypatch, tmp_path)
    _write_ai_enabled_program(programs_root)
    monkeypatch.setattr(
        "src.commands.ai_proposals.resolve_ai_deployments_for_feature", lambda **kwargs: ("fake-deployment",)
    )
    monkeypatch.setattr(
        "src.commands.ai_proposals.generate_top_three_candidates",
        lambda request, *, client, programs_root: (
            _top_three(id="top3-generated-1", item_id=request.items[0].item_id),
            _top_three(id="top3-generated-2", item_id=request.items[1].item_id),
        ),
    )
    candidates_file = _write_candidates_file(
        tmp_path,
        [
            {"category": "risk", "item_id": "risk-candidate-1", "summary": "Vendor SDK delay.", "severity": "high"},
            {"category": "milestone", "item_id": "m1-code-complete", "summary": "Code complete at risk."},
        ],
    )

    result = runner.invoke(
        app,
        [
            "ai-proposals", "generate", "--program", "acme", "--type", "top_three",
            "--candidates-file", str(candidates_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "Selected and staged 2" in result.stdout

    assert load_proposal("acme", "top_three", "top3-generated-1", programs_root=programs_root) is not None
    assert load_proposal("acme", "top_three", "top3-generated-2", programs_root=programs_root) is not None


def test_generate_top_three_parses_candidates_file_fields(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    _write_ai_enabled_program(programs_root)
    monkeypatch.setattr(
        "src.commands.ai_proposals.resolve_ai_deployments_for_feature", lambda **kwargs: ("fake-deployment",)
    )
    captured: dict[str, object] = {}

    def _fake_generate(request, *, client, programs_root):
        captured["items"] = request.items
        return ()

    monkeypatch.setattr("src.commands.ai_proposals.generate_top_three_candidates", _fake_generate)
    candidates_file = _write_candidates_file(
        tmp_path,
        [
            {
                "category": "dependency", "item_id": "dep-1", "summary": "Fabrikam buildout at risk.",
                "severity": "medium", "evidence_refs": ["signal-9"],
            },
        ],
    )

    result = runner.invoke(
        app,
        [
            "ai-proposals", "generate", "--program", "acme", "--type", "top_three",
            "--candidates-file", str(candidates_file),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "discarded or rejected" in result.stdout  # empty tuple -> nothing staged

    items = captured["items"]
    assert len(items) == 1
    assert items[0].category == "dependency"
    assert items[0].item_id == "dep-1"
    assert items[0].summary == "Fabrikam buildout at risk."
    assert items[0].severity == "medium"
    assert items[0].evidence_refs == ("signal-9",)


def test_generate_top_three_requires_candidates_file(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    _write_ai_enabled_program(programs_root)
    monkeypatch.setattr(
        "src.commands.ai_proposals.resolve_ai_deployments_for_feature", lambda **kwargs: ("fake-deployment",)
    )
    result = runner.invoke(app, ["ai-proposals", "generate", "--program", "acme", "--type", "top_three"])
    assert result.exit_code != 0


def test_generate_top_three_rejects_malformed_candidates_file(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    _write_ai_enabled_program(programs_root)
    monkeypatch.setattr(
        "src.commands.ai_proposals.resolve_ai_deployments_for_feature", lambda **kwargs: ("fake-deployment",)
    )
    # Missing the required "summary" field.
    candidates_file = _write_candidates_file(tmp_path, [{"category": "risk", "item_id": "risk-1"}])

    result = runner.invoke(
        app,
        [
            "ai-proposals", "generate", "--program", "acme", "--type", "top_three",
            "--candidates-file", str(candidates_file),
        ],
    )
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# ADF-W5.12 P4 (Section 8.15.2): `review-batch` (sampled/batch review, piloted
# for risk + meeting_action) and `flag-regression` (material-regression
# signal, all five types).
# ---------------------------------------------------------------------------


def _promote_to_l3(programs_root: Path, program_id: str, proposal_type: str, *, sample_rate: float | None = None) -> None:
    """Raises the governance ceiling to l3 for ``proposal_type`` (the
    unconfigured default ceiling is l2 -- see adf_config.py's
    ``_DEFAULT_CEILING_WHEN_UNCONFIGURED``) and then explicitly promotes,
    mirroring what an operator would do via
    `vertex cockpit autonomy-promote --to l3 --sample-rate ...` once
    independent-review evidence justifies it."""
    program_yaml = programs_root / program_id / "program.yaml"
    program_yaml.parent.mkdir(parents=True, exist_ok=True)
    program_yaml.write_text(
        yaml.safe_dump({"arch_data_fix": {"governance": {"autonomy_ceiling": {proposal_type: "l3"}}}}),
        encoding="utf-8",
    )
    promote_proposal_class_explicit(
        program_id, proposal_type, "l3", "test setup: pre-promoted for review-batch coverage",
        programs_root=programs_root, sample_rate=sample_rate,
    )


def test_review_batch_refuses_below_l3(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    stage_proposal("acme", "risk", _risk_proposal(), programs_root=programs_root)

    result = runner.invoke(app, ["ai-proposals", "review-batch", "--program", "acme", "--type", "risk"])
    assert result.exit_code != 0


def test_review_batch_rejects_unsupported_proposal_type(monkeypatch, tmp_path: Path) -> None:
    _patch_roots(monkeypatch, tmp_path)
    result = runner.invoke(app, ["ai-proposals", "review-batch", "--program", "acme", "--type", "top_three"])
    assert result.exit_code != 0


def test_review_batch_materiality_forces_high_impact_into_sample_rest_auto_approved(
    monkeypatch, tmp_path: Path
) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    _promote_to_l3(programs_root, "acme", "risk", sample_rate=0.05)

    critical = _risk_proposal(id="risk-critical", candidate_risk_id="cand-critical", impact=RiskImpact.CRITICAL)
    low_ones = [
        _risk_proposal(id=f"risk-low-{i}", candidate_risk_id=f"cand-low-{i}", impact=RiskImpact.LOW)
        for i in range(3)
    ]
    for proposal in [critical, *low_ones]:
        stage_proposal("acme", "risk", proposal, programs_root=programs_root)

    # Computed sample size at sample_rate=0.05 over 4 staged proposals is 1
    # (max(1, ceil(0.05*4))); the critical one is force-included regardless,
    # so it should be the only one individually reviewed here.
    result = runner.invoke(
        app, ["ai-proposals", "review-batch", "--program", "acme", "--type", "risk", "--seed", "1"], input="y\n",
    )
    assert result.exit_code == 0, result.stdout
    assert "risk-critical" in result.stdout
    assert "1 individually accepted" in result.stdout
    assert "3 auto-approved" in result.stdout

    approved_critical = load_proposal("acme", "risk", "risk-critical", programs_root=programs_root)
    assert approved_critical is not None and approved_critical.status == "approved"
    for i in range(3):
        approved_low = load_proposal("acme", "risk", f"risk-low-{i}", programs_root=programs_root)
        assert approved_low is not None and approved_low.status == "approved"

    audit = read_proposal_audit("acme", programs_root=programs_root)
    reviewed_flags = {record.proposal_id: record.reviewed for record in audit if record.event == "approved"}
    assert reviewed_flags["risk-critical"] is True
    assert reviewed_flags["risk-low-0"] is False
    assert reviewed_flags["risk-low-1"] is False
    assert reviewed_flags["risk-low-2"] is False


def test_review_batch_dry_run_does_not_decide_anything(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    _promote_to_l3(programs_root, "acme", "risk", sample_rate=1.0)
    stage_proposal("acme", "risk", _risk_proposal(), programs_root=programs_root)

    result = runner.invoke(
        app, ["ai-proposals", "review-batch", "--program", "acme", "--type", "risk", "--dry-run"],
    )
    assert result.exit_code == 0, result.stdout
    assert "[dry-run]" in result.stdout

    still_staged = load_proposal("acme", "risk", "risk-proposal-1", programs_root=programs_root)
    assert still_staged is not None and still_staged.status == "staged"


def test_review_batch_no_staged_proposals(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    _promote_to_l3(programs_root, "acme", "risk")

    result = runner.invoke(app, ["ai-proposals", "review-batch", "--program", "acme", "--type", "risk"])
    assert result.exit_code == 0, result.stdout
    assert "No staged risk proposals" in result.stdout


def test_review_batch_reject_path_records_rejection_reason(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    _promote_to_l3(programs_root, "acme", "risk", sample_rate=1.0)
    stage_proposal("acme", "risk", _risk_proposal(), programs_root=programs_root)

    result = runner.invoke(
        app, ["ai-proposals", "review-batch", "--program", "acme", "--type", "risk"], input="n\nnot convincing\n",
    )
    assert result.exit_code == 0, result.stdout
    assert "1 individually rejected" in result.stdout
    rejected = load_proposal("acme", "risk", "risk-proposal-1", programs_root=programs_root)
    assert rejected is not None and rejected.status == "rejected"
    assert rejected.rejection_reason == "not convincing"


def test_flag_regression_records_reversed_event_and_demotes_at_l3(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    stage_proposal("acme", "risk", _risk_proposal(), programs_root=programs_root)
    accept_result = runner.invoke(
        app, ["ai-proposals", "accept", "--program", "acme", "--type", "risk", "--id", "risk-proposal-1"],
    )
    assert accept_result.exit_code == 0, accept_result.stdout

    _promote_to_l3(programs_root, "acme", "risk")

    flag_result = runner.invoke(
        app,
        [
            "ai-proposals", "flag-regression", "--program", "acme", "--type", "risk",
            "--id", "risk-proposal-1", "--reason", "Escalation caused vendor to walk away from the deal.",
        ],
    )
    assert flag_result.exit_code == 0, flag_result.stdout
    assert "Flagged risk proposal 'risk-proposal-1' as a material regression" in flag_result.stdout

    audit = read_proposal_audit("acme", programs_root=programs_root)
    reversed_records = [r for r in audit if r.event == "reversed"]
    assert len(reversed_records) == 1
    assert reversed_records[0].rejection_reason == "Escalation caused vendor to walk away from the deal."

    evaluation = advance_proposal_class_autonomy("acme", "risk", programs_root=programs_root)
    assert evaluation.action == "demoted"
    assert "material regression" in evaluation.reason


def test_flag_regression_rejects_non_approved_proposal(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    stage_proposal("acme", "risk", _risk_proposal(), programs_root=programs_root)

    result = runner.invoke(
        app,
        [
            "ai-proposals", "flag-regression", "--program", "acme", "--type", "risk",
            "--id", "risk-proposal-1", "--reason", "premature",
        ],
    )
    assert result.exit_code != 0


def test_flag_regression_rejects_unknown_id(monkeypatch, tmp_path: Path) -> None:
    _patch_roots(monkeypatch, tmp_path)
    result = runner.invoke(
        app,
        [
            "ai-proposals", "flag-regression", "--program", "acme", "--type", "risk",
            "--id", "does-not-exist", "--reason", "n/a",
        ],
    )
    assert result.exit_code != 0


def test_generate_unknown_type_fails(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    _write_ai_enabled_program(programs_root)
    result = runner.invoke(app, ["ai-proposals", "generate", "--program", "acme", "--type", "top_three"])
    assert result.exit_code != 0


def test_generate_dry_run_makes_no_ai_call(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    _write_ai_enabled_program(programs_root)
    monkeypatch.setattr(
        "src.commands.ai_proposals.resolve_ai_deployments_for_feature", lambda **kwargs: ("fake-deployment",)
    )
    calls = {"count": 0}

    def _boom(*args, **kwargs):
        calls["count"] += 1
        raise AssertionError("generate_risk_proposal should not be called in --dry-run mode")

    monkeypatch.setattr("src.commands.ai_proposals.generate_risk_proposal", _boom)

    result = runner.invoke(
        app,
        [
            "ai-proposals", "generate", "--program", "acme", "--type", "risk", "--dry-run",
            "--candidate-risk-id", "risk-candidate-1", "--title", "Vendor delay",
            "--description", "Multiple signals mention a vendor delay.",
            "--evidence-text", "Vendor X reported a delay.",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "[dry-run]" in result.stdout
    assert calls["count"] == 0


def test_generate_requires_ai_enabled_program(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    _write_ai_enabled_program(programs_root, enabled=False)
    result = runner.invoke(
        app,
        [
            "ai-proposals", "generate", "--program", "acme", "--type", "risk",
            "--candidate-risk-id", "risk-candidate-1", "--title", "Vendor delay",
            "--description", "Multiple signals mention a vendor delay.",
            "--evidence-text", "Vendor X reported a delay.",
        ],
    )
    assert result.exit_code != 0


def test_generate_requires_deployment_configured(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    _write_ai_enabled_program(programs_root)
    for env_name in ("VERTEX_AI_DEPLOYMENT", "VERTEX_EXEC_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT"):
        monkeypatch.delenv(env_name, raising=False)

    result = runner.invoke(
        app,
        [
            "ai-proposals", "generate", "--program", "acme", "--type", "risk",
            "--candidate-risk-id", "risk-candidate-1", "--title", "Vendor delay",
            "--description", "Multiple signals mention a vendor delay.",
            "--evidence-text", "Vendor X reported a delay.",
        ],
    )
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# P3 (ADF-W2.11/W4.8 real-usage measurement): end-to-end verification that the
# full generate -> review -> apply loop, once wired by P0/P1/P2, actually
# produces real proposal_audit/adoption_telemetry data for the two pilot
# types -- not just each stage in isolation.
# ---------------------------------------------------------------------------


def test_generate_then_accept_risk_produces_real_audit_and_adoption_data(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    _write_ai_enabled_program(programs_root)
    save_risk_register("acme", (_candidate_risk_entry(),), programs_root=programs_root)
    monkeypatch.setattr(
        "src.commands.ai_proposals.resolve_ai_deployments_for_feature", lambda **kwargs: ("fake-deployment",)
    )
    monkeypatch.setattr(
        "src.commands.ai_proposals.generate_risk_proposal",
        lambda request, *, client, programs_root: _risk_proposal(
            id="risk-proposal-e2e", candidate_risk_id=request.candidate_risk_id
        ),
    )

    generated = runner.invoke(
        app,
        [
            "ai-proposals", "generate", "--program", "acme", "--type", "risk",
            "--candidate-risk-id", "risk-candidate-1", "--title", "Vendor delay",
            "--description", "Multiple signals mention a vendor delay.",
            "--evidence-text", "Vendor X reported a delay.", "--evidence-ref", "sig-1",
        ],
    )
    assert generated.exit_code == 0, generated.stdout

    accepted = runner.invoke(
        app, ["ai-proposals", "accept", "--program", "acme", "--type", "risk", "--id", "risk-proposal-e2e"]
    )
    assert accepted.exit_code == 0, accepted.stdout

    audit_records = read_proposal_audit("acme", programs_root=programs_root)
    matching = [r for r in audit_records if r.proposal_id == "risk-proposal-e2e"]
    assert matching, "expected a real proposal_audit.jsonl entry for the generated-then-accepted proposal"
    assert matching[0].event == "approved"

    adoption_events = read_adoption_events("acme", programs_root=programs_root)
    assert any(event.workflow == GoldenWorkflow.RISK_DEPENDENCY_REVIEW for event in adoption_events)


# ---------------------------------------------------------------------------
# ADF-W5.11: `run_proposal_review_session`/`ai-proposals review` -- the
# shared typed command service for interactive one-by-one review of all
# five proposal types, called identically by this CLI command and by
# cockpit_tui.py's launch action.
# ---------------------------------------------------------------------------

def _scripted(responses: list[str]):
    iterator = iter(responses)

    def _confirm(message: str) -> bool:
        return next(iterator) == "y"

    def _prompt(message: str) -> str:
        return next(iterator)

    return _confirm, _prompt


def test_review_session_accepts_a_staged_risk_proposal_through_the_real_write_path(monkeypatch, tmp_path: Path) -> None:
    from src.commands.ai_proposals import run_proposal_review_session

    programs_root = _patch_roots(monkeypatch, tmp_path)
    stage_proposal("acme", "risk", _risk_proposal(), programs_root=programs_root)
    save_risk_register("acme", (_candidate_risk_entry(),), programs_root=programs_root)

    confirm_fn, prompt_fn = _scripted(["y"])
    output: list[str] = []
    reviewed_count = run_proposal_review_session(
        "acme", "risk", confirm_fn=confirm_fn, prompt_fn=prompt_fn, echo_fn=output.append, programs_root=programs_root,
    )

    assert reviewed_count == 1
    approved = load_proposal("acme", "risk", "risk-proposal-1", programs_root=programs_root)
    assert approved is not None
    assert approved.status == "approved"
    assert any("promoted candidate" in line for line in output)
    adoption_events = read_adoption_events("acme", programs_root=programs_root)
    assert any(event.workflow == GoldenWorkflow.RISK_DEPENDENCY_REVIEW for event in adoption_events)


def test_review_session_rejects_when_declined_with_a_reason(monkeypatch, tmp_path: Path) -> None:
    from src.commands.ai_proposals import run_proposal_review_session

    programs_root = _patch_roots(monkeypatch, tmp_path)
    stage_proposal("acme", "risk", _risk_proposal(), programs_root=programs_root)

    confirm_fn, prompt_fn = _scripted(["n", "Too speculative."])
    output: list[str] = []
    reviewed_count = run_proposal_review_session(
        "acme", "risk", confirm_fn=confirm_fn, prompt_fn=prompt_fn, echo_fn=output.append, programs_root=programs_root,
    )

    assert reviewed_count == 1
    rejected = load_proposal("acme", "risk", "risk-proposal-1", programs_root=programs_root)
    assert rejected is not None
    assert rejected.status == "rejected"
    assert any("Too speculative" in line for line in output)


def test_review_session_no_staged_proposals_reports_none(monkeypatch, tmp_path: Path) -> None:
    from src.commands.ai_proposals import run_proposal_review_session

    programs_root = _patch_roots(monkeypatch, tmp_path)
    output: list[str] = []
    reviewed_count = run_proposal_review_session(
        "acme", "risk", confirm_fn=lambda m: True, prompt_fn=lambda m: "", echo_fn=output.append, programs_root=programs_root,
    )

    assert reviewed_count == 0
    assert any("No staged" in line for line in output)


def test_review_session_accepts_all_five_proposal_types(monkeypatch, tmp_path: Path) -> None:
    """The remaining 3 of 5 types not already covered by the risk/
    meeting_action tests above -- proves `_accept_dispatch` really routes
    to each type's own accept handler, not just risk."""
    from src.commands.ai_proposals import run_proposal_review_session

    programs_root = _patch_roots(monkeypatch, tmp_path)
    stage_proposal("acme", "top_three", _top_three(), programs_root=programs_root)
    stage_proposal("acme", "governance_decision_brief", _governance_brief(), programs_root=programs_root)
    stage_proposal("acme", "dependency_blast_radius", _blast_radius(), programs_root=programs_root)
    save_dependencies("acme", (_dependency(),), programs_root=programs_root)

    for proposal_type in ("top_three", "governance_decision_brief", "dependency_blast_radius"):
        confirm_fn, prompt_fn = _scripted(["y"])
        reviewed_count = run_proposal_review_session(
            "acme", proposal_type, confirm_fn=confirm_fn, prompt_fn=prompt_fn, echo_fn=lambda m: None, programs_root=programs_root,
        )
        assert reviewed_count == 1
        approved = load_proposal("acme", proposal_type, {
            "top_three": "top3-1", "governance_decision_brief": "brief-1", "dependency_blast_radius": "blast-1",
        }[proposal_type], programs_root=programs_root)
        assert approved is not None
        assert approved.status == "approved"


def test_review_command_cli_accepts_a_staged_meeting_action(monkeypatch, tmp_path: Path) -> None:
    programs_root = _patch_roots(monkeypatch, tmp_path)
    stage_proposal("acme", "meeting_action", _meeting_action(), programs_root=programs_root)

    result = runner.invoke(
        app, ["ai-proposals", "review", "--program", "acme", "--type", "meeting_action"], input="y\n",
    )

    assert result.exit_code == 0, result.stdout
    assert "Reviewed 1 meeting_action proposal(s)" in result.stdout
    approved = load_proposal("acme", "meeting_action", "action-1", programs_root=programs_root)
    assert approved is not None
    assert approved.status == "approved"
