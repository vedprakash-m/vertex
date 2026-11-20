from __future__ import annotations

from datetime import datetime, timezone
import json
from typer.testing import CliRunner

import cli
from src.core.analytics_store import AutonomyAuditRecord, append_autonomy_audit_record, load_autonomy_audit_records
from src.core.feedback.signal_approval_learner import load_promoted_signal_approval_rules, refresh_signal_approval_rules
from src.core.governed_feedback_lifecycle import (
    EvaluationKind,
    FeedbackKind,
    FeedbackPrivacyClass,
    FeedbackRetentionClass,
    FeedbackSliceResult,
    FeedbackTelemetry,
    FeedbackValue,
    ProposalKind,
    approve_feedback_proposal,
    promote_feedback_proposal,
    propose_feedback_change,
    record_feedback_evaluation,
    record_feedback_observation,
    record_shadow_result,
)


runner = CliRunner()
FROZEN_NOW = datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc)


def test_policy_promote_writes_active_rule_and_autonomy_audit(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_signal_approval_workspace(programs_root)
    monkeypatch.setattr("src.commands.policy.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.policy._utc_now", lambda: FROZEN_NOW)
    monkeypatch.setattr(cli, "_stdout_supports_interactive_catchup", lambda: False)

    result = runner.invoke(
        cli.app,
        [
            "policy",
            "promote",
            "--program",
            "acme",
            "--rule",
            "approval:decision_ask_escalation",
            "--updated-by",
            "operator",
        ],
    )

    assert result.exit_code == 0
    assert "Promoted approval:decision_ask_escalation for acme" in result.stdout
    assert "(decision_ask_escalation, trust=L3)." in result.stdout
    assert "Policy file:" in result.stdout

    promoted_rules = load_promoted_signal_approval_rules("acme", programs_root=programs_root)
    assert [rule.proposal.rule_id for rule in promoted_rules] == ["approval:decision_ask_escalation"]
    assert promoted_rules[0].promoted_by == "operator"
    assert promoted_rules[0].promoted_at == FROZEN_NOW

    audit_records = load_autonomy_audit_records("acme", programs_root=programs_root)
    assert audit_records[-1].action_type == "policy_promoted"
    assert audit_records[-1].policy_rule == "approval:decision_ask_escalation"
    assert audit_records[-1].prior_acceptance_rate == 1.0


def test_policy_promote_dry_run_skips_writes(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_signal_approval_workspace(programs_root)
    monkeypatch.setattr("src.commands.policy.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.policy._utc_now", lambda: FROZEN_NOW)
    monkeypatch.setattr(cli, "_stdout_supports_interactive_catchup", lambda: False)

    result = runner.invoke(
        cli.app,
        [
            "policy",
            "promote",
            "--program",
            "acme",
            "--rule",
            "approval:decision_ask_escalation",
            "--updated-by",
            "operator",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert "Dry-run: would promote approval:decision_ask_escalation for acme" in result.stdout
    assert load_promoted_signal_approval_rules("acme", programs_root=programs_root) == ()

    audit_path = programs_root / "acme" / "journal" / "autonomy_audit.jsonl"
    entries = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert all(entry.get("action_type") != "policy_promoted" for entry in entries)


def test_policy_promote_refuses_ungoverned_signal_rule(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_signal_approval_workspace(programs_root, governed=False)
    monkeypatch.setattr("src.commands.policy.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.policy._utc_now", lambda: FROZEN_NOW)
    monkeypatch.setattr(cli, "_stdout_supports_interactive_catchup", lambda: False)

    result = runner.invoke(
        cli.app,
        [
            "policy",
            "promote",
            "--program",
            "acme",
            "--rule",
            "approval:decision_ask_escalation",
            "--updated-by",
            "operator",
        ],
    )

    assert result.exit_code == 2
    assert load_promoted_signal_approval_rules("acme", programs_root=programs_root) == ()


def _seed_signal_approval_workspace(programs_root: Path, *, governed: bool = True) -> None:
    for index in range(10):
        append_autonomy_audit_record(
            AutonomyAuditRecord(
                program_id="acme",
                action_id=f"escalation-{index}",
                level="l3",
                author_alias="owner",
                subject_alias="priya",
                evidence_refs=(f"decision_ask:ask-{index}",),
                policy_rule="decision_ask_escalation",
                accepted=True,
                applied_at=datetime(2026, 5, 1 + index, 8, 0, tzinfo=timezone.utc),
                action_type="decision_ask_escalation",
                blast_radius="1 draft to 2 recipients",
                rollback_mechanism="Delete draft.",
                prior_acceptance_rate=1.0,
            ),
            programs_root=programs_root,
        )

    for index, accepted in enumerate((True, False, True), start=1):
        append_autonomy_audit_record(
            AutonomyAuditRecord(
                program_id="acme",
                action_id=f"nudge-{index}",
                level="l2",
                author_alias="owner",
                subject_alias="priya",
                evidence_refs=(f"decision_ask:nudge-{index}",),
                policy_rule="decision_ask_nudge",
                accepted=accepted,
                applied_at=datetime(2026, 5, 15 + index, 9, 0, tzinfo=timezone.utc),
                action_type="decision_ask_nudge",
                blast_radius="1 draft to 1 recipient",
                rollback_mechanism="Delete draft.",
                prior_acceptance_rate=0.5 if not accepted else 1.0,
            ),
            programs_root=programs_root,
        )

    refresh_signal_approval_rules(
        "acme",
        as_of=FROZEN_NOW,
        programs_root=programs_root,
    )
    if governed:
        _qualify_signal_approval_rule(programs_root)


def _qualify_signal_approval_rule(programs_root: Path) -> None:
    observation = FeedbackTelemetry(
        observation_id="synthetic-signal-approval-feedback",
        program_id="acme",
        kind=FeedbackKind.ACTION_SUCCESS,
        target_type="policy_rule",
        target_id="approval:decision_ask_escalation",
        evidence_refs=("autonomy_audit:decision_ask_escalation",),
        before_value=FeedbackValue.enum("proposal_staging"),
        after_value=FeedbackValue.enum("batch_approval"),
        reason="synthetic accepted signal population",
        actor_role="policy_reviewer",
        snapshot_ref="synthetic-autonomy-snapshot",
        privacy_class=FeedbackPrivacyClass.RESTRICTED,
        retention_class=FeedbackRetentionClass.GOVERNED,
        approved_by="synthetic-owner",
        approved_at=FROZEN_NOW,
        approval_ref="synthetic-approval",
        observed_at=FROZEN_NOW,
    )
    record_feedback_observation(observation, programs_root=programs_root)
    proposal = propose_feedback_change(
        "acme",
        subject="signal_approval:approval:decision_ask_escalation",
        kind=ProposalKind.THRESHOLD,
        target="signal_approval_rules.decision_ask_escalation.mode",
        proposed_value=FeedbackValue.enum("batch_approval"),
        observation_ids=(observation.observation_id,),
        owner="synthetic-policy-owner",
        evidence_refs=observation.evidence_refs,
        reason="synthetic governed threshold proposal",
        privacy_class=FeedbackPrivacyClass.RESTRICTED,
        retention_class=FeedbackRetentionClass.GOVERNED,
        created_at=FROZEN_NOW,
        programs_root=programs_root,
    )
    for kind in (
        EvaluationKind.PRIVACY,
        EvaluationKind.CONTAMINATION,
        EvaluationKind.DEVELOPMENT,
        EvaluationKind.FROZEN_HOLDOUT,
    ):
        record_feedback_evaluation(
            "acme",
            proposal_id=proposal.proposal_id,
            kind=kind,
            passed=True,
            dataset_hash="synthetic-policy-eval",
            frozen=kind is EvaluationKind.FROZEN_HOLDOUT,
            excluded_from_runtime_examples=kind is EvaluationKind.FROZEN_HOLDOUT,
            slice_results=(
                FeedbackSliceResult(
                    name="synthetic",
                    sample_count=10,
                    passed=True,
                    metric="review_burden",
                    value=0.2,
                ),
            ),
            review_burden_reduction=0.2,
            evaluator="synthetic-evaluator",
            evaluated_at=FROZEN_NOW,
            programs_root=programs_root,
        )
    approve_feedback_proposal(
        "acme",
        proposal_id=proposal.proposal_id,
        approved_by="synthetic-approver",
        actor_role="policy_owner",
        approved_at=FROZEN_NOW,
        expires_at=FROZEN_NOW.replace(year=FROZEN_NOW.year + 1),
        kill_switch="vertex audit pause --action-type decision_ask_escalation",
        rollback_to_version=None,
        rollback_plan="disable the batch approval rule",
        programs_root=programs_root,
    )
    record_shadow_result(
        "acme",
        proposal_id=proposal.proposal_id,
        passed=True,
        dataset_hash="synthetic-policy-shadow",
        slice_results=(
            FeedbackSliceResult(
                name="synthetic-shadow",
                sample_count=5,
                passed=True,
                metric="review_burden",
                value=0.15,
            ),
        ),
        review_burden_reduction=0.15,
        evaluator="synthetic-shadow-evaluator",
        evaluated_at=FROZEN_NOW,
        programs_root=programs_root,
    )
    promote_feedback_proposal(
        "acme",
        proposal_id=proposal.proposal_id,
        promoted_by="synthetic-approver",
        promoted_at=FROZEN_NOW,
        programs_root=programs_root,
    )