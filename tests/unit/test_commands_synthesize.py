from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path

import click
import pytest
from src.ai.ai_mode import AIMode, set_ai_mode
from typer.testing import CliRunner

from cli import app
from src.ai.synthesizer import SynthesizedProposalDraft, SynthesizerError
from src.commands import synthesize
from src.core.action_tracker import append_action
from src.core.analytics_store import replace_contradiction_state
from src.core.ai_proposal_store import append_ai_proposal, build_ai_proposal_id, load_ai_proposals
from src.core.exceptions import ConfigError
from src.core.journal import append_review_decision, append_signal
from src.core.models import Confidence, RiskLevel
from src.core.models_v2 import (
    AIConfig,
    AIProposal,
    AIProposalStatus,
    ActionItem,
    ActionSourceType,
    ActionStatus,
    Contradiction,
    ContradictionPacket,
    DataSourceType,
    Program,
    ResolvedContradiction,
    RiskCategory,
    RiskEntry,
    RiskImpact,
    RiskProbability,
    RiskStatus,
    Signal,
    SignalReviewDecision,
    TrajectoryPoint,
    Workstream,
    WorkstreamSynthesis,
)
from src.core.risk_register_engine import save_risk_register
from src.core.sqlite_stores import SQLiteSignalStore, SQLiteTrajectoryStore
from src.core.trajectory import append_trajectory_point


runner = CliRunner()


def test_synthesize_workstream_raises_disabled_error_before_any_ai_or_store_work(monkeypatch) -> None:
    monkeypatch.setattr(
        synthesize,
        "_resolve_program_id",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("_resolve_program_id should not be called")),
    )
    monkeypatch.setattr(
        synthesize,
        "append_ai_proposal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("append_ai_proposal should not be called")),
    )
    monkeypatch.setattr(
        synthesize,
        "supersede_pending_ai_proposals",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("supersede_pending_ai_proposals should not be called")),
    )

    set_ai_mode(AIMode.DISABLED)
    try:
        with pytest.raises(
            synthesize.SynthesisDisabledError,
            match=r"AI synthesis is disabled by --no-ai / AIMode\.DISABLED; no proposal generated for workstream 'networking'\.",
        ):
            synthesize.synthesize_workstream(workstream_id="networking", program_id="acme")
    finally:
        set_ai_mode(AIMode.ACTIVE)


def test_synthesize_command_exits_cleanly_when_ai_disabled(capsys: pytest.CaptureFixture[str]) -> None:
    set_ai_mode(AIMode.DISABLED)
    try:
        with pytest.raises(click.exceptions.Exit) as excinfo:
            synthesize.synthesize_command(workstream="networking", program="acme")
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert excinfo.value.exit_code == 0
    assert "AI synthesis is disabled by --no-ai / AIMode.DISABLED; no proposal generated for workstream 'networking'." in capsys.readouterr().out


def test_load_open_risks_uses_fact_projection_without_unrelated_action_loader(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_program_files(programs_root, tmp_path / "programs" / "acme" / "editions")
    save_risk_register(
        "acme",
        (
            RiskEntry(
                id="risk-linked-workstream",
                program_id="acme",
                title="Networking still blocked",
                description="Critical networking dependency remains open.",
                probability=RiskProbability.LIKELY,
                impact=RiskImpact.HIGH,
                category=RiskCategory.DEPENDENCY,
                owner_alias="operator",
                mitigation_plan="Escalate daily.",
                mitigation_due_date=date(2026, 5, 18),
                linked_workstream_ids=("networking",),
                linked_work_item_ids=(),
                linked_milestone_ids=(),
                linked_claim_ids=(),
                linked_action_ids=(),
                status=RiskStatus.OPEN,
                identified_date=date(2026, 5, 10),
                identified_in_vertex_issue=78,
                last_reviewed_date=date(2026, 5, 11),
                entity_refs=("WI:1234",),
            ),
            RiskEntry(
                id="risk-linked-work-item",
                program_id="acme",
                title="Servicing validation slipped",
                description="Validation work item is late.",
                probability=RiskProbability.POSSIBLE,
                impact=RiskImpact.MEDIUM,
                category=RiskCategory.SCHEDULE,
                owner_alias="operator",
                mitigation_plan="Recover schedule.",
                mitigation_due_date=date(2026, 5, 20),
                linked_workstream_ids=(),
                linked_work_item_ids=(1234,),
                linked_milestone_ids=(),
                linked_claim_ids=(),
                linked_action_ids=(),
                status=RiskStatus.ESCALATED,
                identified_date=date(2026, 5, 12),
                identified_in_vertex_issue=78,
                last_reviewed_date=date(2026, 5, 13),
                entity_refs=("WI:1234",),
            ),
            RiskEntry(
                id="risk-filtered-out",
                program_id="acme",
                title="Already mitigated",
                description="This should not be included.",
                probability=RiskProbability.UNLIKELY,
                impact=RiskImpact.LOW,
                category=RiskCategory.TECHNICAL,
                owner_alias="operator",
                mitigation_plan=None,
                mitigation_due_date=None,
                linked_workstream_ids=("networking",),
                linked_work_item_ids=(),
                linked_milestone_ids=(),
                linked_claim_ids=(),
                linked_action_ids=(),
                status=RiskStatus.MITIGATED,
                identified_date=date(2026, 5, 1),
                identified_in_vertex_issue=77,
                last_reviewed_date=date(2026, 5, 2),
                entity_refs=("WI:9999",),
            ),
        ),
        programs_root=programs_root,
    )

    def _boom(*args, **kwargs):
        raise ConfigError("actions broken")

    monkeypatch.setattr("src.core.action_tracker.load_actions", _boom)

    risks = synthesize._load_open_risks(
        "acme",
        workstream_id="networking",
        work_item_ids=(1234,),
        programs_root=programs_root,
    )

    assert {risk.id for risk in risks} == {"risk-linked-workstream", "risk-linked-work-item"}


def test_load_open_actions_uses_fact_projection_without_unrelated_risk_loader(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_program_files(programs_root, tmp_path / "programs" / "acme" / "editions")
    append_action(
        "acme",
        ActionItem(
            id="action-linked-workstream",
            program_id="acme",
            text="Drive networking close plan",
            owner_alias="operator",
            due_date=date(2026, 5, 18),
            status=ActionStatus.OPEN,
            source_signal_id="sig-1",
            source_type=ActionSourceType.SIGNAL,
            linked_work_item_ids=(),
            linked_claim_id=None,
            linked_risk_id=None,
            workstream_id="networking",
            created_at=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
            resolved_at=None,
            resolution_note=None,
        ),
        programs_root=programs_root,
    )
    append_action(
        "acme",
        ActionItem(
            id="action-linked-work-item",
            program_id="acme",
            text="Close servicing bug",
            owner_alias="operator",
            due_date=date(2026, 5, 19),
            status=ActionStatus.IN_PROGRESS,
            source_signal_id="sig-2",
            source_type=ActionSourceType.SIGNAL,
            linked_work_item_ids=(1234,),
            linked_claim_id=None,
            linked_risk_id=None,
            workstream_id=None,
            created_at=datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc),
            resolved_at=None,
            resolution_note=None,
        ),
        programs_root=programs_root,
    )
    append_action(
        "acme",
        ActionItem(
            id="action-filtered-out",
            program_id="acme",
            text="Completed work",
            owner_alias="operator",
            due_date=date(2026, 5, 15),
            status=ActionStatus.DONE,
            source_signal_id="sig-3",
            source_type=ActionSourceType.SIGNAL,
            linked_work_item_ids=(1234,),
            linked_claim_id=None,
            linked_risk_id=None,
            workstream_id="networking",
            created_at=datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc),
            resolved_at=datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc),
            resolution_note="Done",
        ),
        programs_root=programs_root,
    )

    def _boom(*args, **kwargs):
        raise ConfigError("risks broken")

    monkeypatch.setattr("src.core.program_fact_store.load_risk_register", _boom)

    actions = synthesize._load_open_actions(
        "acme",
        workstream_id="networking",
        work_item_ids=(1234,),
        programs_root=programs_root,
    )

    assert {action.id for action in actions} == {"action-linked-workstream", "action-linked-work-item"}


def test_resolve_program_id_from_workstream_reads_workstreams_from_program_facts(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "alpha").mkdir(parents=True)
    (programs_root / "beta").mkdir(parents=True)
    captured: list[str] = []

    def _load_current_workstreams(program_id: str, *, programs_root: Path):
        captured.append(program_id)
        if program_id == "beta":
            return (Workstream(id="networking", name="Networking", area_paths=("One\\Beta",)),)
        return ()

    monkeypatch.setattr(synthesize, "load_current_workstreams", _load_current_workstreams)

    resolved = synthesize._resolve_program_id(
        requested_program_id=None,
        edition_id=None,
        workstream_id="networking",
        programs_root=programs_root,
        editions_root=tmp_path / "programs" / "acme" / "editions",
    )

    assert resolved == "beta"
    assert captured == ["alpha", "beta"]


def test_load_program_context_reads_workstream_from_program_facts(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    editions_root = tmp_path / "programs" / "acme" / "editions"
    _write_program_files(programs_root, editions_root)
    captured: dict[str, object] = {}
    workstream = Workstream(id="networking", name="Networking", area_paths=("One\\Networking",))

    def _load_current_workstreams(program_id: str, *, programs_root: Path):
        captured["program_id"] = program_id
        captured["programs_root"] = programs_root
        return (workstream,)

    monkeypatch.setattr(synthesize, "load_current_workstreams", _load_current_workstreams)

    program, loaded_workstream = synthesize._load_program_context(
        "acme",
        workstream_id="networking",
        programs_root=programs_root,
    )

    assert program.id == "acme"
    assert loaded_workstream == workstream
    assert captured == {
        "program_id": "acme",
        "programs_root": programs_root,
    }


def test_synthesize_workstream_writes_pending_proposal_and_supersedes_prior_pending(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    editions_root = tmp_path / "programs" / "acme" / "editions"
    _write_program_files(programs_root, editions_root)
    fixed_now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)

    append_signal(_approved_signal("sig-1"), programs_root=programs_root, partition_at=fixed_now)
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="sig-1",
            decision="approved",
            reviewed_at=fixed_now,
            reviewed_by="operator",
        ),
        programs_root=programs_root,
    )
    append_trajectory_point(
        "acme",
        1234,
        TrajectoryPoint(
            date=date(2026, 5, 1),
            state="Active",
            assigned_to="operator",
            target_date=date(2026, 5, 12),
            risk_level=RiskLevel.MEDIUM,
            area_path="One\\Adventure\\Acme\\Networking",
        ),
        programs_root=programs_root,
    )
    append_trajectory_point(
        "acme",
        1234,
        TrajectoryPoint(
            date=date(2026, 5, 8),
            state="Active",
            assigned_to="operator",
            target_date=date(2026, 5, 17),
            risk_level=RiskLevel.HIGH,
            area_path="One\\Adventure\\Acme\\Networking",
        ),
        programs_root=programs_root,
    )

    monkeypatch.setattr(synthesize, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(synthesize, "EDITIONS_ROOT", editions_root)
    monkeypatch.setattr(synthesize, "_now_utc", lambda: fixed_now)
    monkeypatch.setattr(
        synthesize,
        "_build_synthesizer",
        lambda program: _FakeSynthesizer(
            SynthesizedProposalDraft(
                synthesis=WorkstreamSynthesis(
                    workstream_id="networking",
                    overall_assessment="Networking remains the blocking lane.",
                    proposed_risk=RiskLevel.HIGH,
                    confidence=Confidence.HIGH,
                    key_findings=("Target date slipped twice.",),
                    evidence_refs=("sig-1",),
                    open_questions=("Who owns the final sign-off?",),
                    recommended_actions=("Close the servicing checkpoint.",),
                ),
                prompt_version="synthesizer.v1",
            )
        ),
    )

    first = synthesize.synthesize_workstream(
        workstream_id="networking",
        program_id="acme",
        programs_root=programs_root,
        editions_root=editions_root,
    )
    second = synthesize.synthesize_workstream(
        workstream_id="networking",
        program_id="acme",
        programs_root=programs_root,
        editions_root=editions_root,
    )

    proposals = load_ai_proposals("acme", programs_root=programs_root)
    proposals_by_id = {proposal.id: proposal for proposal in proposals}

    assert first.superseded_count == 0
    assert second.superseded_count == 1
    assert proposals_by_id[first.proposal.id].status is AIProposalStatus.SUPERSEDED
    assert proposals_by_id[second.proposal.id].status is AIProposalStatus.PENDING


def test_synthesize_workstream_expires_stale_pending_proposals_via_ttl(monkeypatch, tmp_path: Path) -> None:
    """D-30: a synthesis run garbage-collects pending proposals older than the
    TTL, even on workstreams it is not synthesizing (so the GC is independent of
    same-workstream supersession)."""
    programs_root = tmp_path / "programs"
    editions_root = tmp_path / "programs" / "acme" / "editions"
    _write_program_files(programs_root, editions_root)
    fixed_now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)

    # Seed a pending proposal on a DIFFERENT workstream, created 20 days ago
    # (older than the 14d TTL). Supersession (scoped to "networking") cannot
    # touch it; only the TTL GC can.
    stale_created_at = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    stale = AIProposal(
        id=build_ai_proposal_id("acme", workstream_id="storage", created_at=stale_created_at),
        workstream_id="storage",
        synthesis=WorkstreamSynthesis(
            workstream_id="storage",
            overall_assessment="Old pending synthesis awaiting review.",
            proposed_risk=RiskLevel.MEDIUM,
            confidence=Confidence.MEDIUM,
            key_findings=("Stale finding.",),
            evidence_refs=("sig-old",),
            open_questions=(),
            recommended_actions=(),
        ),
        status=AIProposalStatus.PENDING,
        created_at=stale_created_at,
        resolved_at=None,
        resolved_by=None,
        edition_id=None,
        issue_number=None,
    )
    append_ai_proposal("acme", stale, programs_root=programs_root)

    append_signal(_approved_signal("sig-1"), programs_root=programs_root, partition_at=fixed_now)
    append_review_decision(
        "acme",
        SignalReviewDecision(signal_id="sig-1", decision="approved", reviewed_at=fixed_now, reviewed_by="operator"),
        programs_root=programs_root,
    )
    append_trajectory_point(
        "acme",
        1234,
        TrajectoryPoint(
            date=date(2026, 5, 8),
            state="Active",
            assigned_to="operator",
            target_date=date(2026, 5, 17),
            risk_level=RiskLevel.HIGH,
            area_path="One\\Adventure\\Acme\\Networking",
        ),
        programs_root=programs_root,
    )

    monkeypatch.setattr(synthesize, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(synthesize, "EDITIONS_ROOT", editions_root)
    monkeypatch.setattr(synthesize, "_now_utc", lambda: fixed_now)
    monkeypatch.setattr(
        synthesize,
        "_build_synthesizer",
        lambda program: _FakeSynthesizer(
            SynthesizedProposalDraft(
                synthesis=WorkstreamSynthesis(
                    workstream_id="networking",
                    overall_assessment="Networking remains the blocking lane.",
                    proposed_risk=RiskLevel.HIGH,
                    confidence=Confidence.HIGH,
                    key_findings=("Target date slipped twice.",),
                    evidence_refs=("sig-1",),
                    open_questions=(),
                    recommended_actions=(),
                ),
                prompt_version="synthesizer.v1",
            )
        ),
    )

    synthesize.synthesize_workstream(
        workstream_id="networking",
        program_id="acme",
        programs_root=programs_root,
        editions_root=editions_root,
    )

    proposals_by_id = {p.id: p for p in load_ai_proposals("acme", programs_root=programs_root)}
    assert proposals_by_id[stale.id].status is AIProposalStatus.EXPIRED
    assert proposals_by_id[stale.id].resolved_by == "system:ttl"


def test_synthesize_cli_invokes_command(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    editions_root = tmp_path / "programs" / "acme" / "editions"
    _write_program_files(programs_root, editions_root)
    fixed_now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)

    append_signal(_approved_signal("sig-1"), programs_root=programs_root, partition_at=fixed_now)
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="sig-1",
            decision="approved",
            reviewed_at=fixed_now,
            reviewed_by="operator",
        ),
        programs_root=programs_root,
    )
    append_trajectory_point(
        "acme",
        1234,
        TrajectoryPoint(
            date=date(2026, 5, 8),
            state="Active",
            assigned_to="operator",
            target_date=date(2026, 5, 17),
            risk_level=RiskLevel.HIGH,
            area_path="One\\Adventure\\Acme\\Networking",
        ),
        programs_root=programs_root,
    )

    monkeypatch.setattr(synthesize, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(synthesize, "EDITIONS_ROOT", editions_root)
    monkeypatch.setattr(synthesize, "_now_utc", lambda: fixed_now)
    monkeypatch.setattr(
        synthesize,
        "_build_synthesizer",
        lambda program: _FakeSynthesizer(
            SynthesizedProposalDraft(
                synthesis=WorkstreamSynthesis(
                    workstream_id="networking",
                    overall_assessment="Networking remains the blocking lane.",
                    proposed_risk=RiskLevel.HIGH,
                    confidence=Confidence.HIGH,
                    key_findings=("Target date slipped twice.",),
                    evidence_refs=("sig-1",),
                    open_questions=("Who owns the final sign-off?",),
                    recommended_actions=("Close the servicing checkpoint.",),
                ),
                prompt_version="synthesizer.v1",
            )
        ),
    )

    result = runner.invoke(app, ["synthesize", "--program", "acme", "--workstream", "networking"])

    assert result.exit_code == 0
    assert "Synthesis proposal stored for acme/networking." in result.stdout


def test_synthesize_workstream_stamps_edition_issue_lineage(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    editions_root = tmp_path / "programs" / "acme" / "editions"
    expected_output_root = programs_root / "acme" / "publications" / "acme_weekly"
    _write_program_files(programs_root, editions_root)
    expected_output_root.mkdir(parents=True)
    (expected_output_root / "issue_007").mkdir()
    (expected_output_root / "issue_007" / "issue_007.manifest.json").write_text("{}", encoding="utf-8")
    fixed_now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)

    append_signal(_approved_signal("sig-1"), programs_root=programs_root, partition_at=fixed_now)
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="sig-1",
            decision="approved",
            reviewed_at=fixed_now,
            reviewed_by="operator",
        ),
        programs_root=programs_root,
    )
    append_trajectory_point(
        "acme",
        1234,
        TrajectoryPoint(
            date=date(2026, 5, 8),
            state="Active",
            assigned_to="operator",
            target_date=date(2026, 5, 17),
            risk_level=RiskLevel.HIGH,
            area_path="One\\Adventure\\Acme\\Networking",
        ),
        programs_root=programs_root,
    )

    monkeypatch.setattr(synthesize, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(synthesize, "EDITIONS_ROOT", editions_root)
    monkeypatch.setattr(synthesize, "_now_utc", lambda: fixed_now)
    monkeypatch.setattr(
        synthesize,
        "_build_synthesizer",
        lambda program: _FakeSynthesizer(
            SynthesizedProposalDraft(
                synthesis=WorkstreamSynthesis(
                    workstream_id="networking",
                    overall_assessment="Networking remains the blocking lane.",
                    proposed_risk=RiskLevel.HIGH,
                    confidence=Confidence.HIGH,
                    key_findings=("Target date slipped twice.",),
                    evidence_refs=("sig-1",),
                    open_questions=("Who owns the final sign-off?",),
                    recommended_actions=("Close the servicing checkpoint.",),
                ),
                prompt_version="synthesizer.v1",
            )
        ),
    )

    result = synthesize.synthesize_workstream(
        workstream_id="networking",
        edition_id="acme_weekly",
        programs_root=programs_root,
        editions_root=editions_root,
    )

    assert result.proposal.edition_id == "acme_weekly"
    assert result.proposal.issue_number == 7


def test_synthesize_cli_supports_json_and_csv(monkeypatch, tmp_path: Path) -> None:
    proposal_path = tmp_path / "ai_proposals.jsonl"
    fixed_now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        synthesize,
        "synthesize_workstream",
        lambda **kwargs: synthesize.SynthesizeResult(
            program_id="acme",
            workstream_id="networking",
            proposal=AIProposal(
                id="proposal-123",
                workstream_id="networking",
                synthesis=WorkstreamSynthesis(
                    workstream_id="networking",
                    overall_assessment="Networking remains the blocking lane.",
                    proposed_risk=RiskLevel.HIGH,
                    confidence=Confidence.MEDIUM,
                    key_findings=("Target date slipped twice.",),
                    evidence_refs=("sig-1",),
                    open_questions=("Who owns the final sign-off?",),
                    recommended_actions=("Close the servicing checkpoint.",),
                ),
                status=AIProposalStatus.PENDING,
                created_at=fixed_now,
                resolved_at=None,
                resolved_by=None,
            ),
            proposal_path=proposal_path,
            prompt_version="synthesizer.v1",
            superseded_count=2,
            invalid_evidence_refs=("sig-stale", "sig-missing"),
            flagged_for_review=True,
            signal_count=6,
            drift_pattern_count=2,
            risk_count=1,
            action_count=3,
        ),
    )

    json_result = runner.invoke(app, ["synthesize", "--program", "acme", "--workstream", "networking", "--format", "json"])

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["program_id"] == "acme"
    assert payload["workstream_id"] == "networking"
    assert payload["proposal_id"] == "proposal-123"
    assert payload["proposed_risk"] == "high"
    assert payload["confidence"] == "medium"
    assert payload["flagged_for_review"] is True
    assert payload["invalid_evidence_refs"] == ["sig-stale", "sig-missing"]
    assert payload["proposal_path"] == str(proposal_path)

    csv_result = runner.invoke(app, ["synthesize", "--program", "acme", "--workstream", "networking", "--format", "csv"])

    assert csv_result.exit_code == 0
    lines = csv_result.stdout.strip().splitlines()
    assert lines[0] == "program_id,workstream_id,proposal_id,proposed_risk,confidence,prompt_version,signal_count,drift_pattern_count,risk_count,action_count,superseded_count,proposal_path,invalid_evidence_refs,flagged_for_review"
    assert lines[1] == f"acme,networking,proposal-123,high,medium,synthesizer.v1,6,2,1,3,2,{proposal_path},sig-stale;sig-missing,True"


def test_build_synthesizer_falls_back_to_backup_deployment_when_primary_generation_fails(monkeypatch) -> None:
    attempts: list[str] = []
    trace_contexts: list[object] = []
    draft = SynthesizedProposalDraft(
        synthesis=WorkstreamSynthesis(
            workstream_id="networking",
            overall_assessment="Networking remains the blocking lane.",
            proposed_risk=RiskLevel.HIGH,
            confidence=Confidence.HIGH,
            key_findings=("Target date slipped twice.",),
            evidence_refs=("sig-1",),
            open_questions=("Who owns the final sign-off?",),
            recommended_actions=("Close the servicing checkpoint.",),
        ),
        prompt_version="synthesizer.v1",
    )

    class _FakeAIClient:
        def __init__(self, *, deployment: str, temperature: float, budget_usd: float, trace_context=None) -> None:
            del temperature, budget_usd
            self.deployment = deployment
            attempts.append(f"client:{deployment}")
            trace_contexts.append(trace_context)

    class _FailingSynthesizer:
        def generate(self, *, program, workstream, signals, drift_patterns, open_risks=(), open_actions=(), contradictions=()):
            del program, workstream, signals, drift_patterns, open_risks, open_actions, contradictions
            raise SynthesizerError("primary deployment failed")

    class _SuccessfulSynthesizer:
        def generate(self, *, program, workstream, signals, drift_patterns, open_risks=(), open_actions=(), contradictions=()):
            del program, workstream, signals, drift_patterns, open_risks, open_actions, contradictions
            return draft

    def _build_from_client(client):
        attempts.append(f"builder:{client.deployment}")
        if client.deployment == "primary-deployment":
            return _FailingSynthesizer()
        return _SuccessfulSynthesizer()

    monkeypatch.setattr(synthesize, "AIClient", _FakeAIClient)
    monkeypatch.setattr(synthesize, "build_synthesizer_from_client", _build_from_client)

    synthesizer = synthesize._build_synthesizer(
        Program(
            schema_version="2.0",
            id="acme",
            name="Acme",
            ai=AIConfig(
                enabled=True,
                budget_usd_per_run=0.5,
                exec_summary_deployment="primary-deployment",
                exec_summary_backup_deployment="backup-deployment",
                temperature=0.2,
            ),
        )
    )

    result = synthesizer.generate(
        program=Program(schema_version="2.0", id="acme", name="Acme"),
        workstream=Workstream(id="networking", name="Networking"),
        signals=(_approved_signal("sig-1"),),
        drift_patterns=(),
    )

    assert result == draft
    assert attempts == [
        "client:primary-deployment",
        "builder:primary-deployment",
        "client:backup-deployment",
        "builder:backup-deployment",
    ]
    assert trace_contexts == [None, None]


def test_build_synthesizer_surfaces_vertex_first_missing_deployment_guidance(monkeypatch) -> None:
    monkeypatch.delenv("VERTEX_EXEC_DEPLOYMENT", raising=False)
    monkeypatch.delenv("VERTEX_AI_DEPLOYMENT", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_DEPLOYMENT", raising=False)
    monkeypatch.delenv("VERTEX_EXEC_BACKUP_DEPLOYMENT", raising=False)
    monkeypatch.delenv("VERTEX_AI_BACKUP_DEPLOYMENT", raising=False)
    with pytest.raises(
        SynthesizerError,
        match="VERTEX_EXEC_DEPLOYMENT, VERTEX_AI_DEPLOYMENT, or AZURE_OPENAI_DEPLOYMENT",
    ):
        synthesize._build_synthesizer(
            Program(
                schema_version="2.0",
                id="acme",
                name="Acme",
                ai=AIConfig(
                    enabled=True,
                    budget_usd_per_run=0.5,
                    temperature=0.2,
                ),
            )
        )


def test_build_synthesizer_passes_trace_context_to_ai_clients(monkeypatch) -> None:
    seen_trace_contexts: list[object] = []

    class _FakeAIClient:
        def __init__(self, *, deployment: str, temperature: float, budget_usd: float, trace_context=None) -> None:
            del deployment, temperature, budget_usd
            seen_trace_contexts.append(trace_context)

    class _SuccessfulSynthesizer:
        def generate(self, *, program, workstream, signals, drift_patterns, open_risks=(), open_actions=(), contradictions=()):
            del program, workstream, signals, drift_patterns, open_risks, open_actions, contradictions
            return SynthesizedProposalDraft(
                synthesis=WorkstreamSynthesis(
                    workstream_id="networking",
                    overall_assessment="Networking remains the blocking lane.",
                    proposed_risk=RiskLevel.HIGH,
                    confidence=Confidence.HIGH,
                    key_findings=("Target date slipped twice.",),
                    evidence_refs=("sig-1",),
                    open_questions=("Who owns the final sign-off?",),
                    recommended_actions=("Close the servicing checkpoint.",),
                ),
                prompt_version="synthesizer.v1",
            )

    monkeypatch.setattr(synthesize, "AIClient", _FakeAIClient)
    monkeypatch.setattr(synthesize, "build_synthesizer_from_client", lambda client: _SuccessfulSynthesizer())

    trace_context = synthesize._build_synthesis_trace_context(
        program_id="acme",
        edition_id="acme_weekly",
        workstream_id="networking",
        current_time=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        budget_usd=0.5,
    )
    synthesizer = synthesize._build_synthesizer(
        Program(
            schema_version="2.0",
            id="acme",
            name="Acme",
            ai=AIConfig(
                enabled=True,
                budget_usd_per_run=0.5,
                exec_summary_deployment="primary-deployment",
                temperature=0.2,
            ),
        ),
        trace_context=trace_context,
    )

    result = synthesizer.generate(
        program=Program(schema_version="2.0", id="acme", name="Acme"),
        workstream=Workstream(id="networking", name="Networking"),
        signals=(_approved_signal("sig-1"),),
        drift_patterns=(),
    )

    assert result is not None
    assert len(seen_trace_contexts) == 1
    assert seen_trace_contexts[0] is trace_context
    assert trace_context.run_id == "acme_weekly:synthesize:networking:20260510T120000Z"
    assert trace_context.metadata["run_budget_usd"] == 0.5
    assert trace_context.metadata["task_type"] == "workstream_synthesis"


def test_build_default_synthesizer_raises_when_invocation_ai_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        synthesize,
        "_build_synthesizer",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("_build_synthesizer should not be called")),
    )
    trace_context = synthesize._build_synthesis_trace_context(
        program_id="acme",
        edition_id="acme_weekly",
        workstream_id="networking",
        current_time=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        budget_usd=0.5,
    )

    set_ai_mode(AIMode.DISABLED)
    try:
        with pytest.raises(synthesize.SynthesisDisabledError, match=r"AI synthesis is disabled by --no-ai / AIMode\.DISABLED\."):
            synthesize._build_default_synthesizer(
                program=Program(
                    schema_version="2.0",
                    id="acme",
                    name="Acme",
                    ai=AIConfig(
                        enabled=True,
                        budget_usd_per_run=0.5,
                        exec_summary_deployment="primary-deployment",
                        temperature=0.2,
                    ),
                ),
                trace_context=trace_context,
            )
    finally:
        set_ai_mode(AIMode.ACTIVE)


class _FakeSynthesizer:
    def __init__(self, draft: SynthesizedProposalDraft) -> None:
        self._draft = draft
        self.received_signals: tuple[Signal, ...] = ()
        self.received_drift_patterns: tuple[DriftPattern, ...] = ()
        self.received_contradictions: tuple[ContradictionPacket, ...] = ()

    def generate(self, *, program, workstream, signals, drift_patterns, open_risks=(), open_actions=(), contradictions=()):
        self.received_signals = tuple(signals)
        self.received_drift_patterns = tuple(drift_patterns)
        self.received_contradictions = tuple(contradictions)
        del program, workstream, signals, drift_patterns, open_risks, open_actions, contradictions
        return self._draft


def test_synthesize_workstream_reads_sqlite_backed_signals_and_trajectories(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    editions_root = tmp_path / "programs" / "acme" / "editions"
    _write_program_files(
        programs_root,
        editions_root,
        program_extra="storage_backend: sqlite",
    )
    fixed_now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    signal_store = SQLiteSignalStore(programs_root=programs_root)
    trajectory_store = SQLiteTrajectoryStore(programs_root=programs_root)
    signal_store.append(_approved_signal("sig-1"))
    signal_store.append_review(
        "acme",
        SignalReviewDecision(
            signal_id="sig-1",
            decision="approved",
            reviewed_at=fixed_now,
            reviewed_by="operator",
        ),
    )
    trajectory_store.append(
        "acme",
        1234,
        TrajectoryPoint(
            date=date(2026, 4, 28),
            state="Active",
            assigned_to="operator",
            target_date=date(2026, 5, 10),
            risk_level=RiskLevel.MEDIUM,
            area_path="One\\Adventure\\Acme\\Networking",
        ),
    )
    trajectory_store.append(
        "acme",
        1234,
        TrajectoryPoint(
            date=date(2026, 5, 1),
            state="Active",
            assigned_to="operator",
            target_date=date(2026, 5, 12),
            risk_level=RiskLevel.MEDIUM,
            area_path="One\\Adventure\\Acme\\Networking",
        ),
    )
    trajectory_store.append(
        "acme",
        1234,
        TrajectoryPoint(
            date=date(2026, 5, 8),
            state="Active",
            assigned_to="operator",
            target_date=date(2026, 5, 17),
            risk_level=RiskLevel.HIGH,
            area_path="One\\Adventure\\Acme\\Networking",
        ),
    )

    fake_synthesizer = _FakeSynthesizer(
        SynthesizedProposalDraft(
            synthesis=WorkstreamSynthesis(
                workstream_id="networking",
                overall_assessment="Networking remains the blocking lane.",
                proposed_risk=RiskLevel.HIGH,
                confidence=Confidence.HIGH,
                key_findings=("Target date slipped twice.",),
                evidence_refs=("sig-1",),
                open_questions=("Who owns the final sign-off?",),
                recommended_actions=("Close the servicing checkpoint.",),
            ),
            prompt_version="synthesizer.v1",
        )
    )

    monkeypatch.setattr(synthesize, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(synthesize, "EDITIONS_ROOT", editions_root)
    monkeypatch.setattr(synthesize, "_now_utc", lambda: fixed_now)
    monkeypatch.setattr(synthesize, "_build_synthesizer", lambda program: fake_synthesizer)

    result = synthesize.synthesize_workstream(
        workstream_id="networking",
        program_id="acme",
        programs_root=programs_root,
        editions_root=editions_root,
    )

    assert [signal.id for signal in fake_synthesizer.received_signals] == ["sig-1"]
    assert len(fake_synthesizer.received_drift_patterns) == 1
    assert result.signal_count == 1
    assert result.drift_pattern_count == 1


def test_synthesize_workstream_passes_cached_contradictions_for_workstream_items(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    editions_root = tmp_path / "programs" / "acme" / "editions"
    _write_program_files(programs_root, editions_root)
    fixed_now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)

    append_signal(_approved_signal("sig-1"), programs_root=programs_root, partition_at=fixed_now)
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="sig-1",
            decision="approved",
            reviewed_at=fixed_now,
            reviewed_by="operator",
        ),
        programs_root=programs_root,
    )
    append_trajectory_point(
        "acme",
        1234,
        TrajectoryPoint(
            date=date(2026, 5, 8),
            state="Active",
            assigned_to="operator",
            target_date=date(2026, 5, 17),
            risk_level=RiskLevel.HIGH,
            area_path="One\\Adventure\\Acme\\Networking",
        ),
        programs_root=programs_root,
    )
    replace_contradiction_state(
        "acme",
        (
            ContradictionPacket(
                work_item_id=1234,
                workstream_id="networking",
                contradictions=(
                    Contradiction(
                        field="target_date",
                        source_a="journal",
                        source_b="ado",
                        summary="Claim date disagrees with ADO target date.",
                        confidence=Confidence.HIGH,
                        evidence_refs=("sig-1",),
                    ),
                ),
                confidence=Confidence.HIGH,
                recommended_resolution=ResolvedContradiction(
                    winning_source=DataSourceType.WORKIQ,
                    confidence=Confidence.HIGH,
                    rationale="Prefer the external signal while the DRI remains optimistic.",
                    evidence_refs=("sig-1",),
                ),
                generated_at=fixed_now,
            ),
            ContradictionPacket(
                work_item_id=9999,
                workstream_id="repair",
                contradictions=(
                    Contradiction(
                        field="target_date",
                        source_a="journal",
                        source_b="ado",
                        summary="Unrelated work item contradiction.",
                        confidence=Confidence.MEDIUM,
                        evidence_refs=("sig-x",),
                    ),
                ),
                confidence=Confidence.MEDIUM,
                recommended_resolution=None,
                generated_at=fixed_now,
            ),
        ),
        programs_root=programs_root,
    )

    fake_synthesizer = _FakeSynthesizer(
        SynthesizedProposalDraft(
            synthesis=WorkstreamSynthesis(
                workstream_id="networking",
                overall_assessment="Networking remains the blocking lane.",
                proposed_risk=RiskLevel.HIGH,
                confidence=Confidence.HIGH,
                key_findings=("Target date slipped twice.",),
                evidence_refs=("sig-1",),
                open_questions=("Who owns the final sign-off?",),
                recommended_actions=("Close the servicing checkpoint.",),
            ),
            prompt_version="synthesizer.v1",
        )
    )

    monkeypatch.setattr(synthesize, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(synthesize, "EDITIONS_ROOT", editions_root)
    monkeypatch.setattr(synthesize, "_now_utc", lambda: fixed_now)
    monkeypatch.setattr(synthesize, "_build_synthesizer", lambda program: fake_synthesizer)

    synthesize.synthesize_workstream(
        workstream_id="networking",
        program_id="acme",
        programs_root=programs_root,
        editions_root=editions_root,
    )

    assert [packet.work_item_id for packet in fake_synthesizer.received_contradictions] == [1234]


def test_synthesize_workstream_orders_signals_by_source_confidence_and_workiq_relevance(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    editions_root = tmp_path / "programs" / "acme" / "editions"
    _write_program_files(programs_root, editions_root)
    _write_people_directory(
        tmp_path / "knowledge",
        """
people:
  - alias: gm
    title: General Manager
  - alias: alex
    title: Senior Software Engineer
""".strip()
        + "\n",
    )
    fixed_now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)

    for signal in (
        Signal(
            id="sig-workiq-low",
            timestamp=datetime(2026, 5, 10, 11, 30, tzinfo=timezone.utc),
            source="workiq/email",
            program_id="acme",
            workstream_id="networking",
            entity_refs=("WI:1234",),
            text="Alex asked for a status refresh.",
            raw_ref=None,
            confidence=Confidence.MEDIUM,
            metadata={"sender_alias": "alex", "thread_id": "thread-low"},
            thread_id="thread-low",
        ),
        Signal(
            id="sig-workiq-high-older",
            timestamp=datetime(2026, 5, 10, 10, 30, tzinfo=timezone.utc),
            source="workiq/email",
            program_id="acme",
            workstream_id="networking",
            entity_refs=("WI:1234",),
            text="GM asked for blocker confirmation.",
            raw_ref=None,
            confidence=Confidence.MEDIUM,
            metadata={"sender_alias": "gm", "thread_id": "thread-high"},
            thread_id="thread-high",
        ),
        Signal(
            id="sig-ado",
            timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
            source="ado/revision",
            program_id="acme",
            workstream_id="networking",
            entity_refs=("WI:1234",),
            text="Servicing validation moved to 2026-05-17.",
            raw_ref=None,
            confidence=Confidence.HIGH,
        ),
        Signal(
            id="sig-workiq-high-newer",
            timestamp=datetime(2026, 5, 10, 10, 45, tzinfo=timezone.utc),
            source="workiq/email",
            program_id="acme",
            workstream_id="networking",
            entity_refs=("WI:1234",),
            text="GM reiterated the blocker in the same thread.",
            raw_ref=None,
            confidence=Confidence.MEDIUM,
            metadata={"sender_alias": "gm", "thread_id": "thread-high"},
            thread_id="thread-high",
        ),
    ):
        append_signal(signal, programs_root=programs_root, partition_at=fixed_now)
        append_review_decision(
            "acme",
            SignalReviewDecision(
                signal_id=signal.id,
                decision="approved",
                reviewed_at=fixed_now,
                reviewed_by="operator",
            ),
            programs_root=programs_root,
        )

    append_trajectory_point(
        "acme",
        1234,
        TrajectoryPoint(
            date=date(2026, 5, 1),
            state="Active",
            assigned_to="operator",
            target_date=date(2026, 5, 12),
            risk_level=RiskLevel.MEDIUM,
            area_path="One\\Adventure\\Acme\\Networking",
        ),
        programs_root=programs_root,
    )
    append_trajectory_point(
        "acme",
        1234,
        TrajectoryPoint(
            date=date(2026, 5, 8),
            state="Active",
            assigned_to="operator",
            target_date=date(2026, 5, 17),
            risk_level=RiskLevel.HIGH,
            area_path="One\\Adventure\\Acme\\Networking",
        ),
        programs_root=programs_root,
    )

    fake_synthesizer = _FakeSynthesizer(
        SynthesizedProposalDraft(
            synthesis=WorkstreamSynthesis(
                workstream_id="networking",
                overall_assessment="Networking remains the blocking lane.",
                proposed_risk=RiskLevel.HIGH,
                confidence=Confidence.HIGH,
                key_findings=("Target date slipped twice.",),
                evidence_refs=("sig-ado",),
                open_questions=("Who owns the final sign-off?",),
                recommended_actions=("Close the servicing checkpoint.",),
            ),
            prompt_version="synthesizer.v1",
        )
    )

    monkeypatch.setattr(synthesize, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(synthesize, "EDITIONS_ROOT", editions_root)
    monkeypatch.setattr(synthesize, "_now_utc", lambda: fixed_now)
    monkeypatch.setattr(synthesize, "_build_synthesizer", lambda program: fake_synthesizer)

    synthesize.synthesize_workstream(
        workstream_id="networking",
        program_id="acme",
        programs_root=programs_root,
        editions_root=editions_root,
    )

    assert [signal.id for signal in fake_synthesizer.received_signals] == [
        "sig-ado",
        "sig-workiq-high-newer",
        "sig-workiq-high-older",
        "sig-workiq-low",
    ]


def test_synthesize_workstream_honors_program_source_confidence_order(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    editions_root = tmp_path / "programs" / "acme" / "editions"
    _write_program_files(
        programs_root,
        editions_root,
        program_extra="""
source_confidence_order:
  - workiq
  - ado
""".strip(),
    )
    _write_people_directory(
        tmp_path / "knowledge",
        """
people:
  - alias: gm
    title: General Manager
""".strip()
        + "\n",
    )
    fixed_now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)

    for signal in (
        Signal(
            id="sig-workiq",
            timestamp=datetime(2026, 5, 10, 10, 30, tzinfo=timezone.utc),
            source="workiq/email",
            program_id="acme",
            workstream_id="networking",
            entity_refs=("WI:1234",),
            text="GM asked for blocker confirmation.",
            raw_ref=None,
            confidence=Confidence.MEDIUM,
            metadata={"sender_alias": "gm", "thread_id": "thread-high"},
            thread_id="thread-high",
        ),
        Signal(
            id="sig-ado",
            timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
            source="ado/revision",
            program_id="acme",
            workstream_id="networking",
            entity_refs=("WI:1234",),
            text="Servicing validation moved to 2026-05-17.",
            raw_ref=None,
            confidence=Confidence.HIGH,
        ),
    ):
        append_signal(signal, programs_root=programs_root, partition_at=fixed_now)
        append_review_decision(
            "acme",
            SignalReviewDecision(
                signal_id=signal.id,
                decision="approved",
                reviewed_at=fixed_now,
                reviewed_by="operator",
            ),
            programs_root=programs_root,
        )

    append_trajectory_point(
        "acme",
        1234,
        TrajectoryPoint(
            date=date(2026, 5, 8),
            state="Active",
            assigned_to="operator",
            target_date=date(2026, 5, 17),
            risk_level=RiskLevel.HIGH,
            area_path="One\\Adventure\\Acme\\Networking",
        ),
        programs_root=programs_root,
    )

    fake_synthesizer = _FakeSynthesizer(
        SynthesizedProposalDraft(
            synthesis=WorkstreamSynthesis(
                workstream_id="networking",
                overall_assessment="Networking remains the blocking lane.",
                proposed_risk=RiskLevel.HIGH,
                confidence=Confidence.HIGH,
                key_findings=("Target date slipped twice.",),
                evidence_refs=("sig-ado",),
                open_questions=("Who owns the final sign-off?",),
                recommended_actions=("Close the servicing checkpoint.",),
            ),
            prompt_version="synthesizer.v1",
        )
    )

    monkeypatch.setattr(synthesize, "PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(synthesize, "EDITIONS_ROOT", editions_root)
    monkeypatch.setattr(synthesize, "_now_utc", lambda: fixed_now)
    monkeypatch.setattr(synthesize, "_build_synthesizer", lambda program: fake_synthesizer)

    synthesize.synthesize_workstream(
        workstream_id="networking",
        program_id="acme",
        programs_root=programs_root,
        editions_root=editions_root,
    )

    assert [signal.id for signal in fake_synthesizer.received_signals] == ["sig-workiq", "sig-ado"]


def _approved_signal(signal_id: str) -> Signal:
    return Signal(
        id=signal_id,
        timestamp=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
        source="manual",
        program_id="acme",
        workstream_id="networking",
        entity_refs=("WI:1234",),
        text="Servicing validation moved to 2026-05-17.",
        raw_ref=None,
        confidence=Confidence.HIGH,
    )


def _write_program_files(programs_root: Path, editions_root: Path, *, program_extra: str = "") -> None:
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    editions_root.mkdir(parents=True)

    program_extra_block = f"\n{program_extra.strip()}" if program_extra.strip() else ""

    (program_dir / "program.yaml").write_text(
        (
            """
schema_version: '2.0'
id: acme
name: Acme
ai:
  enabled: false
  budget_usd_per_run: 0.5
  blurb_deployment: fake-deployment
  exec_summary_deployment: fake-deployment
  temperature: 0.2
""".strip()
            + program_extra_block
            + "\n"
        ),
        encoding="utf-8",
    )
    (program_dir / "workstreams.yaml").write_text(
        """
workstreams:
  - id: networking
    name: Networking
    area_paths:
      - One\\Adventure\\Acme\\Networking
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (program_dir / "scorecards.yaml").write_text("scorecards: []\n", encoding="utf-8")
    (editions_root / "acme_weekly.yaml").write_text(
        """
schema_version: '2.0'
id: acme_weekly
program_id: acme
name: Acme Weekly
type: detailed
altitude: newsletter
cadence: weekly
""".strip()
        + "\n",
        encoding="utf-8",
    )


def _write_people_directory(knowledge_root: Path, content: str) -> None:
    knowledge_root.mkdir(parents=True, exist_ok=True)
    (knowledge_root / "people_directory.yaml").write_text(content, encoding="utf-8")
