from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from src.core.governed_feedback_lifecycle import (
    EvaluationKind,
    FeedbackApproval,
    FeedbackEvaluation,
    FeedbackKind,
    FeedbackPrivacyClass,
    FeedbackRetentionClass,
    FeedbackSliceResult,
    FeedbackTelemetry,
    FeedbackValue,
    ProposalKind,
    ProposalStatus,
    approve_feedback_proposal,
    expire_feedback_proposal,
    load_active_feedback_proposals,
    load_feedback_observations,
    load_feedback_proposal,
    modify_feedback_proposal,
    promote_feedback_proposal,
    propose_feedback_change,
    record_feedback_evaluation,
    record_feedback_observation,
    record_shadow_result,
    reassign_feedback_proposal,
    reject_feedback_proposal,
    rollback_feedback_proposal,
)


NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def _observation(*, observation_id: str = "observation-1") -> FeedbackTelemetry:
    return FeedbackTelemetry(
        observation_id=observation_id,
        program_id="synthetic-program",
        kind=FeedbackKind.GENERATED_TO_FINAL_DIFF,
        target_type="narrative_section",
        target_id="issue:7:exec_summary",
        evidence_refs=("snapshot:synthetic-program:issue:007",),
        before_value=FeedbackValue.text_hash("synthetic generated text"),
        after_value=FeedbackValue.text_hash("synthetic confirmed text"),
        reason="synthetic confirmed correction",
        actor_role="confirming_operator",
        snapshot_ref="snapshot:synthetic-program:issue:007",
        privacy_class=FeedbackPrivacyClass.RESTRICTED,
        retention_class=FeedbackRetentionClass.ISSUE_CYCLE,
        approved_by="synthetic-operator",
        approved_at=NOW,
        approval_ref="confirm:synthetic-program:issue:007",
        observed_at=NOW,
    )


def _propose(programs_root: Path, *, value: FeedbackValue, subject: str = "editorial:exec-summary") -> str:
    record_feedback_observation(_observation(), programs_root=programs_root)
    proposal = propose_feedback_change(
        "synthetic-program",
        subject=subject,
        kind=ProposalKind.RULE,
        target="editorial_rules.banned_openings",
        proposed_value=value,
        observation_ids=("observation-1",),
        owner="synthetic-policy-owner",
        evidence_refs=("snapshot:synthetic-program:issue:007",),
        reason="offline synthetic proposal",
        privacy_class=FeedbackPrivacyClass.RESTRICTED,
        retention_class=FeedbackRetentionClass.ISSUE_CYCLE,
        created_at=NOW,
        programs_root=programs_root,
    )
    return proposal.proposal_id


def _evaluate(
    proposal_id: str,
    *,
    programs_root: Path,
) -> None:
    for kind, passed in (
        (EvaluationKind.PRIVACY, True),
        (EvaluationKind.CONTAMINATION, True),
        (EvaluationKind.DEVELOPMENT, True),
        (EvaluationKind.FROZEN_HOLDOUT, True),
    ):
        record_feedback_evaluation(
            "synthetic-program",
            proposal_id=proposal_id,
            kind=kind,
            passed=passed,
            dataset_hash=(
                "holdout-synthetic-v1"
                if kind in {EvaluationKind.FROZEN_HOLDOUT, EvaluationKind.DEVELOPMENT}
                else None
            ),
            frozen=(kind is EvaluationKind.FROZEN_HOLDOUT),
            excluded_from_runtime_examples=(kind is EvaluationKind.FROZEN_HOLDOUT),
            slice_results=(FeedbackSliceResult(name="synthetic", sample_count=20, passed=True, metric="review_burden", value=0.15),),
            review_burden_reduction=0.15,
            evaluator="synthetic-evaluator",
            evaluated_at=NOW,
            programs_root=programs_root,
        )


def _approve_and_promote(
    proposal_id: str,
    *,
    programs_root: Path,
    rollback_version: int | None = None,
) -> None:
    _evaluate(proposal_id, programs_root=programs_root)
    approve_feedback_proposal(
        "synthetic-program",
        proposal_id=proposal_id,
        approved_by="synthetic-approver",
        actor_role="release_owner",
        approved_at=NOW,
        expires_at=NOW + timedelta(days=30),
        kill_switch="kill:synth",
        rollback_to_version=rollback_version,
        rollback_plan="restore the prior approved version or disable the proposal",
        programs_root=programs_root,
    )
    record_shadow_result(
        "synthetic-program",
        proposal_id=proposal_id,
        passed=True,
        dataset_hash="shadow-synthetic-v1",
        slice_results=(FeedbackSliceResult(name="synthetic-shadow", sample_count=10, passed=True, metric="review_burden", value=0.1),),
        review_burden_reduction=0.1,
        evaluator="synthetic-shadow-evaluator",
        evaluated_at=NOW,
        programs_root=programs_root,
    )
    promote_feedback_proposal(
        "synthetic-program",
        proposal_id=proposal_id,
        promoted_by="synthetic-approver",
        promoted_at=NOW,
        programs_root=programs_root,
    )


def test_restricted_feedback_records_only_hashes_and_lengths(tmp_path: Path) -> None:
    record_feedback_observation(_observation(), programs_root=tmp_path)

    observations = load_feedback_observations("synthetic-program", programs_root=tmp_path)

    assert len(observations) == 1
    assert observations[0].before_value.value_type == "sha256"
    assert observations[0].before_value.length == len("synthetic generated text")
    assert "synthetic generated text" not in (
        tmp_path / "synthetic-program" / "_feedback" / "feedback_observations.jsonl"
    ).read_text(encoding="utf-8")


def test_restricted_feedback_refuses_raw_excerpt(tmp_path: Path) -> None:
    raw = FeedbackValue.excerpt("synthetic sensitive phrase", privacy_class=FeedbackPrivacyClass.INTERNAL)

    with pytest.raises(ValueError, match="restricted"):
        record_feedback_observation(
            FeedbackTelemetry(
                observation_id="observation-raw",
                program_id="synthetic-program",
                kind=FeedbackKind.RISK_OVERRIDE,
                target_type="risk",
                target_id="risk:synthetic",
                evidence_refs=("risk:synthetic",),
                before_value=raw,
                after_value=FeedbackValue.text_hash("medium"),
                reason="synthetic risk correction",
                actor_role="risk_reviewer",
                snapshot_ref="snapshot:synthetic:7",
                privacy_class=FeedbackPrivacyClass.RESTRICTED,
                retention_class=FeedbackRetentionClass.ISSUE_CYCLE,
                approved_by="synthetic-operator",
                approved_at=NOW,
                approval_ref="review:synthetic:7",
                observed_at=NOW,
            ),
            programs_root=tmp_path,
        )


def test_lifecycle_requires_all_governance_gates_and_rolls_back(tmp_path: Path) -> None:
    proposal_id = _propose(tmp_path, value=FeedbackValue.enum("replacement"))

    with pytest.raises(ValueError, match="shadow status"):
        promote_feedback_proposal(
            "synthetic-program",
            proposal_id=proposal_id,
            promoted_by="synthetic-approver",
            promoted_at=NOW,
            programs_root=tmp_path,
        )

    _approve_and_promote(proposal_id, programs_root=tmp_path)

    active = load_active_feedback_proposals("synthetic-program", programs_root=tmp_path, as_of=NOW)
    assert [item.proposal_id for item in active] == [proposal_id]
    rolled_back = rollback_feedback_proposal(
        "synthetic-program",
        proposal_id=proposal_id,
        rolled_back_by="synthetic-approver",
        reason="synthetic regression",
        rolled_back_at=NOW,
        programs_root=tmp_path,
    )
    assert rolled_back.status is ProposalStatus.ROLLED_BACK
    assert load_active_feedback_proposals("synthetic-program", programs_root=tmp_path, as_of=NOW) == ()


def test_new_version_promotes_and_rollback_restores_prior_version(tmp_path: Path) -> None:
    first_id = _propose(tmp_path, value=FeedbackValue.enum("v1"))
    _approve_and_promote(first_id, programs_root=tmp_path)
    second = modify_feedback_proposal(
        "synthetic-program",
        proposal_id=first_id,
        proposed_value=FeedbackValue.enum("v2"),
        reason="synthetic revision",
        modified_by="synthetic-owner",
        modified_at=NOW,
        programs_root=tmp_path,
    )
    _approve_and_promote(second.proposal_id, programs_root=tmp_path, rollback_version=1)

    rollback_feedback_proposal(
        "synthetic-program",
        proposal_id=second.proposal_id,
        rolled_back_by="synthetic-approver",
        reason="synthetic shadow regression",
        rolled_back_at=NOW,
        programs_root=tmp_path,
    )

    active = load_active_feedback_proposals("synthetic-program", programs_root=tmp_path, as_of=NOW)
    assert [(item.proposal_id, item.version) for item in active] == [(first_id, 1)]


def test_frozen_holdout_must_be_excluded_from_runtime_examples(tmp_path: Path) -> None:
    proposal_id = _propose(tmp_path, value=FeedbackValue.enum("replacement"))

    with pytest.raises(ValueError, match="runtime examples"):
        record_feedback_evaluation(
            "synthetic-program",
            proposal_id=proposal_id,
            kind=EvaluationKind.FROZEN_HOLDOUT,
            passed=True,
            dataset_hash="holdout-synthetic-v1",
            frozen=True,
            excluded_from_runtime_examples=False,
            slice_results=(FeedbackSliceResult(name="synthetic", sample_count=20, passed=True, metric="accuracy", value=0.9),),
            review_burden_reduction=0.15,
            evaluator="synthetic-evaluator",
            evaluated_at=NOW,
            programs_root=tmp_path,
        )


def test_modify_reassign_reject_and_expire_are_explicit_versioned_actions(tmp_path: Path) -> None:
    original_id = _propose(tmp_path, value=FeedbackValue.enum("v1"))
    reassigned = reassign_feedback_proposal(
        "synthetic-program",
        proposal_id=original_id,
        owner="synthetic-new-owner",
        reassigned_by="synthetic-approver",
        reassigned_at=NOW,
        reason="coverage",
        programs_root=tmp_path,
    )
    rejected = reject_feedback_proposal(
        "synthetic-program",
        proposal_id=reassigned.proposal_id,
        rejected_by="synthetic-approver",
        reason="insufficient evidence",
        rejected_at=NOW,
        programs_root=tmp_path,
    )
    revised = modify_feedback_proposal(
        "synthetic-program",
        proposal_id=original_id,
        proposed_value=FeedbackValue.enum("v2"),
        reason="reviewed change",
        modified_by="synthetic-owner",
        modified_at=NOW,
        programs_root=tmp_path,
    )
    expired = expire_feedback_proposal(
        "synthetic-program",
        proposal_id=revised.proposal_id,
        expired_by="synthetic-approver",
        reason="review deadline passed",
        expired_at=NOW,
        programs_root=tmp_path,
    )

    assert rejected.status is ProposalStatus.REJECTED
    assert reassigned.owner == "synthetic-new-owner"
    assert revised.version == 2
    assert revised.parent_proposal_id == original_id
    assert expired.status is ProposalStatus.EXPIRED
    assert load_feedback_proposal("synthetic-program", original_id, programs_root=tmp_path).status is ProposalStatus.SUPERSEDED


@pytest.mark.parametrize(
    "factory",
    (
        lambda: FeedbackValue("sha256", "bad", 2),
        lambda: FeedbackValue("excerpt", ""),
        lambda: FeedbackValue("excerpt", "x" * 241),
        lambda: FeedbackValue("enum", ""),
        lambda: FeedbackValue("integer", True),
        lambda: FeedbackValue("number", float("nan")),
        lambda: FeedbackValue("boolean", 1),
        lambda: FeedbackValue("unknown", "x"),
        lambda: FeedbackValue("redacted", "x"),
        lambda: FeedbackValue.excerpt("restricted", privacy_class=FeedbackPrivacyClass.RESTRICTED),
    ),
)
def test_feedback_values_refuse_invalid_or_sensitive_payloads(factory) -> None:
    with pytest.raises(ValueError):
        factory()


def test_feedback_value_supported_types_are_stable() -> None:
    assert FeedbackValue.integer(3).value == 3
    assert FeedbackValue.boolean(True).value is True
    assert FeedbackValue.redacted().value is None
    assert FeedbackValue.excerpt(
        "short synthetic excerpt",
        privacy_class=FeedbackPrivacyClass.INTERNAL,
    ).value_type == "excerpt"


@pytest.mark.parametrize(
    "mutation",
    (
        {"program_id": ""},
        {"evidence_refs": ()},
        {"reason": "x" * 501},
        {"before_value": FeedbackValue.excerpt("raw", privacy_class=FeedbackPrivacyClass.INTERNAL)},
        {"approved_at": NOW.replace(tzinfo=None)},
        {"observed_at": NOW.replace(tzinfo=None)},
    ),
)
def test_telemetry_rejects_missing_provenance_or_privacy_violations(mutation) -> None:
    with pytest.raises(ValueError):
        replace(_observation(), **mutation)


@pytest.mark.parametrize(
    "factory",
    (
        lambda: FeedbackSliceResult(name="", sample_count=1, passed=True, metric="x", value=0.1),
        lambda: FeedbackSliceResult(name="x", sample_count=0, passed=True, metric="x", value=0.1),
        lambda: FeedbackSliceResult(name="x", sample_count=1, passed=True, metric="", value=0.1),
        lambda: FeedbackSliceResult(name="x", sample_count=1, passed=True, metric="x", value=float("inf")),
        lambda: FeedbackEvaluation(
            kind=EvaluationKind.PRIVACY,
            passed=True,
            dataset_hash=None,
            frozen=False,
            excluded_from_runtime_examples=False,
            slice_results=(),
            review_burden_reduction=None,
            evaluator="synthetic",
            evaluated_at=NOW,
        ),
        lambda: FeedbackEvaluation(
            kind=EvaluationKind.PRIVACY,
            passed=True,
            dataset_hash=None,
            frozen=False,
            excluded_from_runtime_examples=False,
            slice_results=(FeedbackSliceResult("x", 1, False, "x", 0.1),),
            review_burden_reduction=0.1,
            evaluator="synthetic",
            evaluated_at=NOW,
        ),
        lambda: FeedbackEvaluation(
            kind=EvaluationKind.DEVELOPMENT,
            passed=True,
            dataset_hash=None,
            frozen=False,
            excluded_from_runtime_examples=False,
            slice_results=(FeedbackSliceResult("x", 1, True, "x", 0.1),),
            review_burden_reduction=0.1,
            evaluator="synthetic",
            evaluated_at=NOW,
        ),
        lambda: FeedbackEvaluation(
            kind=EvaluationKind.FROZEN_HOLDOUT,
            passed=True,
            dataset_hash="holdout",
            frozen=False,
            excluded_from_runtime_examples=True,
            slice_results=(FeedbackSliceResult("x", 1, True, "x", 0.1),),
            review_burden_reduction=0.1,
            evaluator="synthetic",
            evaluated_at=NOW,
        ),
        lambda: FeedbackEvaluation(
            kind=EvaluationKind.FROZEN_HOLDOUT,
            passed=True,
            dataset_hash="holdout",
            frozen=True,
            excluded_from_runtime_examples=True,
            slice_results=(FeedbackSliceResult("x", 1, True, "x", 0.1),),
            review_burden_reduction=0.0,
            evaluator="synthetic",
            evaluated_at=NOW,
        ),
    ),
)
def test_evaluation_models_reject_invalid_evidence(factory) -> None:
    with pytest.raises(ValueError):
        factory()


@pytest.mark.parametrize(
    "mutation",
    (
        {"approved_by": ""},
        {"approved_at": NOW.replace(tzinfo=None)},
        {"expires_at": NOW},
    ),
)
def test_approval_rejects_missing_identity_or_validity(mutation) -> None:
    approval = FeedbackApproval(
        approved_by="synthetic",
        actor_role="owner",
        approved_at=NOW,
        expires_at=NOW + timedelta(days=1),
        kill_switch="kill:synthetic",
        rollback_to_version=None,
        rollback_plan="disable",
        proposal_digest="digest",
    )
    with pytest.raises(ValueError):
        replace(approval, **mutation)


def test_proposal_rejects_invalid_identity_and_restricted_excerpt(tmp_path: Path) -> None:
    proposal_id = _propose(tmp_path, value=FeedbackValue.enum("v1"))
    proposal = load_feedback_proposal("synthetic-program", proposal_id, programs_root=tmp_path)
    with pytest.raises(ValueError, match="version"):
        replace(proposal, version=0)
    with pytest.raises(ValueError, match="required"):
        replace(proposal, owner="")
    with pytest.raises(ValueError, match="observation references"):
        replace(proposal, observation_ids=())
    with pytest.raises(ValueError, match="evidence"):
        replace(proposal, evidence_refs=())
    with pytest.raises(ValueError, match="raw excerpts"):
        replace(
            proposal,
            proposed_value=FeedbackValue.excerpt(
                "bounded",
                privacy_class=FeedbackPrivacyClass.INTERNAL,
            ),
        )


def test_proposal_staging_is_idempotent_and_requires_known_observations(tmp_path: Path) -> None:
    proposal_id = _propose(tmp_path, value=FeedbackValue.enum("v1"))
    repeated = propose_feedback_change(
        "synthetic-program",
        subject="editorial:exec-summary",
        kind=ProposalKind.RULE,
        target="editorial_rules.banned_openings",
        proposed_value=FeedbackValue.enum("v1"),
        observation_ids=("observation-1",),
        owner="synthetic-policy-owner",
        evidence_refs=("another:reference",),
        reason="same offline payload",
        privacy_class=FeedbackPrivacyClass.RESTRICTED,
        retention_class=FeedbackRetentionClass.ISSUE_CYCLE,
        created_at=NOW + timedelta(minutes=1),
        programs_root=tmp_path,
    )
    assert repeated.proposal_id == proposal_id
    with pytest.raises(ValueError, match="unknown observations"):
        propose_feedback_change(
            "synthetic-program",
            subject="unknown",
            kind=ProposalKind.RULE,
            target="target",
            proposed_value=FeedbackValue.enum("v1"),
            observation_ids=("missing",),
            owner="owner",
            evidence_refs=("evidence",),
            reason="reason",
            privacy_class=FeedbackPrivacyClass.INTERNAL,
            retention_class=FeedbackRetentionClass.GOVERNED,
            created_at=NOW,
            programs_root=tmp_path,
        )


def test_lifecycle_refuses_unguarded_transitions_and_shadow_inputs(tmp_path: Path) -> None:
    proposal_id = _propose(tmp_path, value=FeedbackValue.enum("v1"))
    with pytest.raises(ValueError, match="fully evaluated"):
        approve_feedback_proposal(
            "synthetic-program",
            proposal_id=proposal_id,
            approved_by="owner",
            actor_role="owner",
            approved_at=NOW,
            expires_at=NOW + timedelta(days=1),
            kill_switch="kill",
            rollback_to_version=None,
            rollback_plan="disable",
            programs_root=tmp_path,
        )
    with pytest.raises(ValueError, match="record_shadow_result"):
        record_feedback_evaluation(
            "synthetic-program",
            proposal_id=proposal_id,
            kind=EvaluationKind.SHADOW,
            passed=True,
            dataset_hash="shadow",
            frozen=True,
            excluded_from_runtime_examples=True,
            slice_results=(FeedbackSliceResult("x", 1, True, "x", 0.1),),
            review_burden_reduction=0.1,
            evaluator="evaluator",
            evaluated_at=NOW,
            programs_root=tmp_path,
        )
    with pytest.raises(ValueError, match="digest-bound human approval"):
        record_shadow_result(
            "synthetic-program",
            proposal_id=proposal_id,
            passed=True,
            dataset_hash="shadow",
            slice_results=(FeedbackSliceResult("x", 1, True, "x", 0.1),),
            review_burden_reduction=0.1,
            evaluator="evaluator",
            evaluated_at=NOW,
            programs_root=tmp_path,
        )
    _evaluate(proposal_id, programs_root=tmp_path)
    approve_feedback_proposal(
        "synthetic-program",
        proposal_id=proposal_id,
        approved_by="owner",
        actor_role="owner",
        approved_at=NOW,
        expires_at=NOW + timedelta(days=1),
        kill_switch="kill",
        rollback_to_version=None,
        rollback_plan="disable",
        programs_root=tmp_path,
    )
    with pytest.raises(ValueError, match="shadow status"):
        promote_feedback_proposal(
            "synthetic-program",
            proposal_id=proposal_id,
            promoted_by="owner",
            promoted_at=NOW,
            programs_root=tmp_path,
        )
    with pytest.raises(ValueError, match="Cannot evaluate"):
        record_feedback_evaluation(
            "synthetic-program",
            proposal_id=proposal_id,
            kind=EvaluationKind.PRIVACY,
            passed=True,
            dataset_hash=None,
            frozen=False,
            excluded_from_runtime_examples=False,
            slice_results=(FeedbackSliceResult("x", 1, True, "x", 0.1),),
            review_burden_reduction=0.1,
            evaluator="evaluator",
            evaluated_at=NOW,
            programs_root=tmp_path,
        )
    record_shadow_result(
        "synthetic-program",
        proposal_id=proposal_id,
        passed=False,
        dataset_hash="shadow-failed",
        slice_results=(FeedbackSliceResult("x", 1, False, "x", 0.0),),
        review_burden_reduction=0.0,
        evaluator="evaluator",
        evaluated_at=NOW,
        programs_root=tmp_path,
    )
    with pytest.raises(ValueError, match="passing shadow evidence"):
        promote_feedback_proposal(
            "synthetic-program",
            proposal_id=proposal_id,
            promoted_by="owner",
            promoted_at=NOW,
            programs_root=tmp_path,
        )


def test_active_expiry_tamper_and_terminal_transitions_are_fail_closed(tmp_path: Path) -> None:
    proposal_id = _propose(tmp_path, value=FeedbackValue.enum("v1"))
    _approve_and_promote(proposal_id, programs_root=tmp_path)
    with pytest.raises(ValueError, match="rolled back"):
        reject_feedback_proposal(
            "synthetic-program",
            proposal_id=proposal_id,
            rejected_by="owner",
            reason="reject",
            rejected_at=NOW,
            programs_root=tmp_path,
        )
    with pytest.raises(ValueError, match="Cannot reassign"):
        reassign_feedback_proposal(
            "synthetic-program",
            proposal_id=proposal_id,
            owner="new",
            reassigned_by="owner",
            reason="reassign",
            reassigned_at=NOW,
            programs_root=tmp_path,
        )
    assert load_active_feedback_proposals(
        "synthetic-program",
        as_of=NOW + timedelta(days=31),
        programs_root=tmp_path,
    ) == ()
    path = tmp_path / "synthetic-program" / "_feedback" / "feedback_proposals.jsonl"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    records[-1]["reason"] = "tampered"
    path.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not bound"):
        load_active_feedback_proposals(
            "synthetic-program",
            as_of=NOW,
            programs_root=tmp_path,
        )


def test_terminal_proposals_reject_modification_and_unknown_ids(tmp_path: Path) -> None:
    proposal_id = _propose(tmp_path, value=FeedbackValue.enum("v1"))
    _approve_and_promote(proposal_id, programs_root=tmp_path)
    rollback_feedback_proposal(
        "synthetic-program",
        proposal_id=proposal_id,
        rolled_back_by="owner",
        reason="regression",
        rolled_back_at=NOW,
        programs_root=tmp_path,
    )
    with pytest.raises(ValueError, match="Rolled-back"):
        modify_feedback_proposal(
            "synthetic-program",
            proposal_id=proposal_id,
            proposed_value=FeedbackValue.enum("v2"),
            reason="change",
            modified_by="owner",
            modified_at=NOW,
            programs_root=tmp_path,
        )
    with pytest.raises(ValueError, match="Unknown governed"):
        load_feedback_proposal("synthetic-program", "missing", programs_root=tmp_path)
    second_id = _propose(
        tmp_path,
        value=FeedbackValue.enum("v2"),
        subject="editorial:second",
    )
    _evaluate(second_id, programs_root=tmp_path)
    with pytest.raises(ValueError, match="earlier proposal version"):
        approve_feedback_proposal(
            "synthetic-program",
            proposal_id=second_id,
            approved_by="owner",
            actor_role="owner",
            approved_at=NOW,
            expires_at=NOW + timedelta(days=1),
            kill_switch="kill",
            rollback_to_version=1,
            rollback_plan="restore",
            programs_root=tmp_path,
        )
    with pytest.raises(ValueError, match="rollback version"):
        approve_feedback_proposal(
            "synthetic-program",
            proposal_id=second_id,
            approved_by="owner",
            actor_role="owner",
            approved_at=NOW,
            expires_at=NOW + timedelta(days=1),
            kill_switch="kill",
            rollback_to_version=0,
            rollback_plan="restore",
            programs_root=tmp_path,
        )


def test_reassignment_invalidates_approval_and_validates_owner(tmp_path: Path) -> None:
    proposal_id = _propose(tmp_path, value=FeedbackValue.enum("v1"))
    _evaluate(proposal_id, programs_root=tmp_path)
    approved = approve_feedback_proposal(
        "synthetic-program",
        proposal_id=proposal_id,
        approved_by="owner",
        actor_role="owner",
        approved_at=NOW,
        expires_at=NOW + timedelta(days=1),
        kill_switch="kill",
        rollback_to_version=None,
        rollback_plan="disable",
        programs_root=tmp_path,
    )
    with pytest.raises(ValueError, match="owner is required"):
        reassign_feedback_proposal(
            "synthetic-program",
            proposal_id=proposal_id,
            owner="",
            reassigned_by="owner",
            reason="invalid",
            reassigned_at=NOW,
            programs_root=tmp_path,
        )
    reassigned = reassign_feedback_proposal(
        "synthetic-program",
        proposal_id=proposal_id,
        owner="new-owner",
        reassigned_by="owner",
        reason="coverage",
        reassigned_at=NOW,
        programs_root=tmp_path,
    )
    assert approved.approval is not None
    assert reassigned.approval is None
    assert reassigned.status is ProposalStatus.AWAITING_APPROVAL


def test_store_rejects_foreign_program_records(tmp_path: Path) -> None:
    record_feedback_observation(_observation(), programs_root=tmp_path)
    observation_path = tmp_path / "synthetic-program" / "_feedback" / "feedback_observations.jsonl"
    record = json.loads(observation_path.read_text(encoding="utf-8"))
    record["program_id"] = "foreign"
    observation_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="foreign program"):
        load_feedback_observations("synthetic-program", programs_root=tmp_path)
