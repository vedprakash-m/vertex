"""Typed, governed feedback telemetry and offline proposal lifecycle (RELO-D19)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import portalocker

from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.jsonl_utils import append_jsonl_line, read_jsonl_records


_FEEDBACK_MAX_BYTES = 10 * 1024 * 1024
_SCHEMA_VERSION = "1.0"
_HASH_RE = frozenset("0123456789abcdef")
_REQUIRED_EVALUATIONS = frozenset({"privacy", "contamination", "development", "frozen_holdout"})


class FeedbackKind(str, Enum):
    GENERATED_TO_FINAL_DIFF = "generated_to_final_diff"
    VERDICT_OVERRIDE = "verdict_override"
    RISK_OVERRIDE = "risk_override"
    BINDING_CORRECTION = "binding_correction"
    OWNER_CORRECTION = "owner_correction"
    DATE_CORRECTION = "date_correction"
    CATEGORY_CORRECTION = "category_correction"
    SEVERITY_CORRECTION = "severity_correction"
    DEPENDENCY_CORRECTION = "dependency_correction"
    AUDIENCE_CORRECTION = "audience_correction"
    ROUTER_ABSTENTION = "router_abstention"
    ROUTER_FALSE_ROUTE = "router_false_route"
    BLOCKED_GATE_FALSE_POSITIVE = "blocked_gate_false_positive"
    BLOCKED_GATE_FALSE_NEGATIVE = "blocked_gate_false_negative"
    ACTION_SUCCESS = "action_success"
    ACTION_FAILURE = "action_failure"
    ACTION_REVERSAL = "action_reversal"


class FeedbackPrivacyClass(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


class FeedbackRetentionClass(str, Enum):
    ISSUE_CYCLE = "issue_cycle"
    SHORT = "short"
    GOVERNED = "governed"


class ProposalKind(str, Enum):
    RULE = "rule"
    PROMPT = "prompt"
    EXAMPLE = "example"
    INDEX = "index"
    THRESHOLD = "threshold"


class ProposalStatus(str, Enum):
    PROPOSED = "proposed"
    EVALUATING = "evaluating"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    SHADOW = "shadow"
    ACTIVE = "active"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    ROLLED_BACK = "rolled_back"


class EvaluationKind(str, Enum):
    PRIVACY = "privacy"
    CONTAMINATION = "contamination"
    DEVELOPMENT = "development"
    FROZEN_HOLDOUT = "frozen_holdout"
    SHADOW = "shadow"


@dataclass(frozen=True, slots=True)
class FeedbackValue:
    value_type: str
    value: str | int | float | bool | None
    length: int | None = None

    def __post_init__(self) -> None:
        if self.value_type == "sha256":
            text = self.value
            if (
                not isinstance(text, str)
                or len(text) != 64
                or any(character not in _HASH_RE for character in text)
                or self.length is None
                or self.length < 0
            ):
                raise ValueError("sha256 feedback values require a SHA-256 digest and non-negative length.")
        elif self.value_type == "excerpt":
            if not isinstance(self.value, str) or not self.value.strip() or len(self.value) > 240:
                raise ValueError("excerpt feedback values must contain 1-240 characters.")
        elif self.value_type == "enum":
            if not isinstance(self.value, str) or not self.value.strip():
                raise ValueError("enum feedback values must contain a non-empty string.")
        elif self.value_type == "integer":
            if isinstance(self.value, bool) or not isinstance(self.value, int):
                raise ValueError("integer feedback values must contain an integer.")
        elif self.value_type == "number":
            if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
                raise ValueError("number feedback values must contain a number.")
        elif self.value_type == "boolean":
            if not isinstance(self.value, bool):
                raise ValueError("boolean feedback values must contain a boolean.")
        elif self.value_type != "redacted":
            raise ValueError(f"Unknown feedback value_type {self.value_type!r}.")
        if self.value_type == "number" and isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("number feedback values must be finite.")
        if self.value_type == "redacted" and self.value is not None:
            raise ValueError("redacted feedback values cannot contain a value.")

    @classmethod
    def text_hash(cls, text: str) -> FeedbackValue:
        payload = text.encode("utf-8")
        return cls("sha256", hashlib.sha256(payload).hexdigest(), len(payload))

    @classmethod
    def excerpt(cls, text: str, *, privacy_class: FeedbackPrivacyClass) -> FeedbackValue:
        if privacy_class is FeedbackPrivacyClass.RESTRICTED:
            raise ValueError("restricted feedback may not store raw excerpts.")
        return cls("excerpt", text)

    @classmethod
    def enum(cls, value: str) -> FeedbackValue:
        return cls("enum", value)

    @classmethod
    def integer(cls, value: int) -> FeedbackValue:
        return cls("integer", value)

    @classmethod
    def boolean(cls, value: bool) -> FeedbackValue:
        return cls("boolean", value)

    @classmethod
    def redacted(cls) -> FeedbackValue:
        return cls("redacted", None)


@dataclass(frozen=True, slots=True)
class FeedbackTelemetry:
    observation_id: str
    program_id: str
    kind: FeedbackKind
    target_type: str
    target_id: str
    evidence_refs: tuple[str, ...]
    before_value: FeedbackValue
    after_value: FeedbackValue
    reason: str
    actor_role: str
    snapshot_ref: str
    privacy_class: FeedbackPrivacyClass
    retention_class: FeedbackRetentionClass
    approved_by: str
    approved_at: datetime
    approval_ref: str
    observed_at: datetime
    schema_version: str = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        required_text = {
            "observation_id": self.observation_id,
            "program_id": self.program_id,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "reason": self.reason,
            "actor_role": self.actor_role,
            "snapshot_ref": self.snapshot_ref,
            "approved_by": self.approved_by,
            "approval_ref": self.approval_ref,
        }
        if any(not value.strip() for value in required_text.values()):
            raise ValueError("Feedback telemetry text fields must be non-empty.")
        if not self.evidence_refs or any(not ref.strip() for ref in self.evidence_refs):
            raise ValueError("Feedback telemetry requires non-empty authorized evidence references.")
        if len(self.reason) > 500:
            raise ValueError("Feedback telemetry reason must not exceed 500 characters.")
        if self.privacy_class is FeedbackPrivacyClass.RESTRICTED and any(
            value.value_type == "excerpt" for value in (self.before_value, self.after_value)
        ):
            raise ValueError("restricted feedback may not store raw excerpts.")
        if self.observed_at.tzinfo is None or self.approved_at.tzinfo is None:
            raise ValueError("Feedback timestamps must be timezone-aware.")


@dataclass(frozen=True, slots=True)
class FeedbackSliceResult:
    name: str
    sample_count: int
    passed: bool
    metric: str
    value: float

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.metric.strip():
            raise ValueError("Evaluation slice name and metric are required.")
        if isinstance(self.sample_count, bool) or self.sample_count <= 0:
            raise ValueError("Evaluation slice sample_count must be positive.")
        if not math.isfinite(self.value):
            raise ValueError("Evaluation slice metric value must be finite.")


@dataclass(frozen=True, slots=True)
class FeedbackEvaluation:
    kind: EvaluationKind
    passed: bool
    dataset_hash: str | None
    frozen: bool
    excluded_from_runtime_examples: bool
    slice_results: tuple[FeedbackSliceResult, ...]
    review_burden_reduction: float | None
    evaluator: str
    evaluated_at: datetime

    def __post_init__(self) -> None:
        if not self.evaluator.strip() or self.evaluated_at.tzinfo is None:
            raise ValueError("Evaluation requires an evaluator and timezone-aware time.")
        if not self.slice_results:
            raise ValueError("Evaluation requires at least one slice result.")
        if self.passed and not all(result.passed for result in self.slice_results):
            raise ValueError("A passing evaluation cannot contain a failed slice result.")
        if self.kind in {EvaluationKind.DEVELOPMENT, EvaluationKind.FROZEN_HOLDOUT, EvaluationKind.SHADOW}:
            if not self.dataset_hash or not self.dataset_hash.strip():
                raise ValueError(f"{self.kind.value} evaluation requires a dataset hash.")
        if self.kind is EvaluationKind.FROZEN_HOLDOUT:
            if not self.frozen:
                raise ValueError("Frozen-holdout evaluation must use a frozen dataset.")
            if not self.excluded_from_runtime_examples:
                raise ValueError("Frozen-holdout examples must be excluded from runtime examples.")
            if self.review_burden_reduction is None or self.review_burden_reduction <= 0:
                raise ValueError("Frozen-holdout evaluation must show positive review-burden reduction.")


@dataclass(frozen=True, slots=True)
class FeedbackApproval:
    approved_by: str
    actor_role: str
    approved_at: datetime
    expires_at: datetime
    kill_switch: str
    rollback_to_version: int | None
    rollback_plan: str
    proposal_digest: str

    def __post_init__(self) -> None:
        if any(not value.strip() for value in (
            self.approved_by, self.actor_role, self.kill_switch, self.rollback_plan, self.proposal_digest,
        )):
            raise ValueError("Approval fields must be non-empty.")
        if self.approved_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("Approval timestamps must be timezone-aware.")
        if self.expires_at <= self.approved_at:
            raise ValueError("Approval expiry must follow approval.")


@dataclass(frozen=True, slots=True)
class GovernedFeedbackProposal:
    proposal_id: str
    program_id: str
    subject: str
    version: int
    parent_proposal_id: str | None
    kind: ProposalKind
    target: str
    proposed_value: FeedbackValue
    observation_ids: tuple[str, ...]
    owner: str
    evidence_refs: tuple[str, ...]
    reason: str
    privacy_class: FeedbackPrivacyClass
    retention_class: FeedbackRetentionClass
    status: ProposalStatus
    created_at: datetime
    updated_at: datetime
    evaluations: tuple[FeedbackEvaluation, ...] = ()
    approval: FeedbackApproval | None = None
    transition_actor: str | None = None
    transition_reason: str | None = None
    schema_version: str = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or self.version <= 0:
            raise ValueError("Feedback proposal version must be positive.")
        if any(not value.strip() for value in (
            self.proposal_id,
            self.program_id,
            self.subject,
            self.target,
            self.owner,
            self.reason,
        )):
            raise ValueError("Feedback proposal identity, target, owner and reason are required.")
        if not self.observation_ids or any(not item.strip() for item in self.observation_ids):
            raise ValueError("Feedback proposal requires observation references.")
        if not self.evidence_refs or any(not item.strip() for item in self.evidence_refs):
            raise ValueError("Feedback proposal requires evidence references.")
        if self.privacy_class is FeedbackPrivacyClass.RESTRICTED and self.proposed_value.value_type == "excerpt":
            raise ValueError("Restricted feedback proposals cannot contain raw excerpts.")

    def payload_digest(self) -> str:
        payload = {
            "proposal_id": self.proposal_id,
            "program_id": self.program_id,
            "subject": self.subject,
            "version": self.version,
            "parent_proposal_id": self.parent_proposal_id,
            "kind": self.kind.value,
            "target": self.target,
            "proposed_value": asdict(self.proposed_value),
            "observation_ids": list(self.observation_ids),
            "owner": self.owner,
            "evidence_refs": list(self.evidence_refs),
            "reason": self.reason,
            "privacy_class": self.privacy_class.value,
            "retention_class": self.retention_class.value,
            "created_at": _utc(self.created_at).isoformat(),
            "evaluations": [
                _evaluation_to_record(evaluation)
                for evaluation in self.evaluations
                if evaluation.kind is not EvaluationKind.SHADOW
            ],
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def get_feedback_observations_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "_feedback" / "feedback_observations.jsonl"


def get_feedback_proposals_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "_feedback" / "feedback_proposals.jsonl"


def record_feedback_observation(
    observation: FeedbackTelemetry,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    append_jsonl_line(
        get_feedback_observations_path(observation.program_id, programs_root=programs_root),
        json.dumps(_observation_to_record(observation), sort_keys=True) + "\n",
        max_bytes=_FEEDBACK_MAX_BYTES,
    )
    return get_feedback_observations_path(observation.program_id, programs_root=programs_root)


def load_feedback_observations(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[FeedbackTelemetry, ...]:
    observations = tuple(
        _observation_from_record(record)
        for record in read_jsonl_records(
            get_feedback_observations_path(program_id, programs_root=programs_root),
        )
    )
    if any(observation.program_id != program_id for observation in observations):
        raise ValueError("Feedback observation store contains a foreign program.")
    return tuple(sorted(observations, key=lambda observation: (observation.observed_at, observation.observation_id)))


def propose_feedback_change(
    program_id: str,
    *,
    subject: str,
    kind: ProposalKind,
    target: str,
    proposed_value: FeedbackValue,
    observation_ids: tuple[str, ...],
    owner: str,
    evidence_refs: tuple[str, ...],
    reason: str,
    privacy_class: FeedbackPrivacyClass,
    retention_class: FeedbackRetentionClass,
    created_at: datetime,
    programs_root: Path = PROGRAMS_ROOT,
) -> GovernedFeedbackProposal:
    observations = {item.observation_id for item in load_feedback_observations(program_id, programs_root=programs_root)}
    if not observation_ids or any(observation_id not in observations for observation_id in observation_ids):
        raise ValueError("Feedback proposal references unknown observations.")
    existing = _latest_for_subject(program_id, subject, programs_root=programs_root)
    if existing is not None and existing.status in {
        ProposalStatus.PROPOSED,
        ProposalStatus.EVALUATING,
        ProposalStatus.AWAITING_APPROVAL,
        ProposalStatus.APPROVED,
        ProposalStatus.SHADOW,
    } and _pending_payload_matches(
        existing=existing,
        program_id=program_id,
        subject=subject,
        kind=kind,
        target=target,
        proposed_value=proposed_value,
        owner=owner,
        privacy_class=privacy_class,
        retention_class=retention_class,
    ):
        return existing
    version = max(
        (
            proposal.version
            for proposal in load_feedback_proposals(program_id, programs_root=programs_root)
            if proposal.subject == subject
        ),
        default=0,
    ) + 1
    proposal = GovernedFeedbackProposal(
        proposal_id=str(uuid4()),
        program_id=program_id,
        subject=subject,
        version=version,
        parent_proposal_id=(existing.proposal_id if existing is not None else None),
        kind=kind,
        target=target,
        proposed_value=proposed_value,
        observation_ids=observation_ids,
        owner=owner,
        evidence_refs=evidence_refs,
        reason=reason,
        privacy_class=privacy_class,
        retention_class=retention_class,
        status=ProposalStatus.PROPOSED,
        created_at=_require_utc(created_at),
        updated_at=_require_utc(created_at),
    )
    _append_proposals(program_id, (proposal,), programs_root=programs_root)
    return proposal


def modify_feedback_proposal(
    program_id: str,
    *,
    proposal_id: str,
    proposed_value: FeedbackValue,
    modified_by: str,
    reason: str,
    modified_at: datetime,
    programs_root: Path = PROGRAMS_ROOT,
) -> GovernedFeedbackProposal:
    prior = _require_proposal(program_id, proposal_id, programs_root=programs_root)
    if prior.status is ProposalStatus.ROLLED_BACK:
        raise ValueError("Rolled-back proposals cannot be modified.")
    version = max(
        proposal.version
        for proposal in load_feedback_proposals(program_id, programs_root=programs_root)
        if proposal.subject == prior.subject
    ) + 1
    revised = replace(
        prior,
        proposal_id=str(uuid4()),
        version=version,
        parent_proposal_id=prior.proposal_id,
        proposed_value=proposed_value,
        status=ProposalStatus.PROPOSED,
        updated_at=_require_utc(modified_at),
        evaluations=(),
        approval=None,
        transition_actor=modified_by,
        transition_reason=reason,
    )
    appended = [revised]
    if prior.status is not ProposalStatus.ACTIVE:
        appended.insert(
            0,
            replace(
                prior,
                status=ProposalStatus.SUPERSEDED,
                updated_at=_require_utc(modified_at),
                transition_actor=modified_by,
                transition_reason=f"Superseded by modified proposal {revised.proposal_id}.",
            ),
        )
    _append_proposals(program_id, tuple(appended), programs_root=programs_root)
    return revised


def record_feedback_evaluation(
    program_id: str,
    *,
    proposal_id: str,
    kind: EvaluationKind,
    passed: bool,
    dataset_hash: str | None,
    frozen: bool,
    excluded_from_runtime_examples: bool,
    slice_results: tuple[FeedbackSliceResult, ...],
    review_burden_reduction: float | None,
    evaluator: str,
    evaluated_at: datetime,
    programs_root: Path = PROGRAMS_ROOT,
) -> GovernedFeedbackProposal:
    proposal = _require_proposal(program_id, proposal_id, programs_root=programs_root)
    if proposal.status not in {
        ProposalStatus.PROPOSED,
        ProposalStatus.EVALUATING,
        ProposalStatus.AWAITING_APPROVAL,
    }:
        raise ValueError(f"Cannot evaluate proposal in status {proposal.status.value!r}.")
    if kind is EvaluationKind.SHADOW:
        raise ValueError("Use record_shadow_result after human approval.")
    evaluation = FeedbackEvaluation(
        kind=kind,
        passed=passed,
        dataset_hash=dataset_hash,
        frozen=frozen,
        excluded_from_runtime_examples=excluded_from_runtime_examples,
        slice_results=slice_results,
        review_burden_reduction=review_burden_reduction,
        evaluator=evaluator,
        evaluated_at=_require_utc(evaluated_at),
    )
    evaluations = _replace_evaluation(proposal.evaluations, evaluation)
    status = (
        ProposalStatus.AWAITING_APPROVAL
        if _required_evaluations_pass(evaluations)
        else ProposalStatus.EVALUATING
    )
    updated = replace(
        proposal,
        status=status,
        evaluations=evaluations,
        updated_at=evaluation.evaluated_at,
        transition_actor=evaluator,
        transition_reason=f"{kind.value} evaluation: {'passed' if passed else 'failed'}",
    )
    _append_proposals(program_id, (updated,), programs_root=programs_root)
    return updated


def approve_feedback_proposal(
    program_id: str,
    *,
    proposal_id: str,
    approved_by: str,
    actor_role: str,
    approved_at: datetime,
    expires_at: datetime,
    kill_switch: str,
    rollback_to_version: int | None,
    rollback_plan: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> GovernedFeedbackProposal:
    proposal = _require_proposal(program_id, proposal_id, programs_root=programs_root)
    if proposal.status is not ProposalStatus.AWAITING_APPROVAL:
        raise ValueError("Approval requires a fully evaluated proposal awaiting approval.")
    if not _required_evaluations_pass(proposal.evaluations):
        raise ValueError("Approval requires passing privacy, contamination, development, and frozen-holdout evaluations.")
    if rollback_to_version is not None:
        if rollback_to_version >= proposal.version:
            raise ValueError("Rollback target must be an earlier proposal version.")
        _require_version(program_id, proposal.subject, rollback_to_version, programs_root=programs_root)
    approval = FeedbackApproval(
        approved_by=approved_by,
        actor_role=actor_role,
        approved_at=_require_utc(approved_at),
        expires_at=_require_utc(expires_at),
        kill_switch=kill_switch,
        rollback_to_version=rollback_to_version,
        rollback_plan=rollback_plan,
        proposal_digest=proposal.payload_digest(),
    )
    updated = replace(
        proposal,
        status=ProposalStatus.APPROVED,
        approval=approval,
        updated_at=approval.approved_at,
        transition_actor=approved_by,
        transition_reason="human approval",
    )
    _append_proposals(program_id, (updated,), programs_root=programs_root)
    return updated


def record_shadow_result(
    program_id: str,
    *,
    proposal_id: str,
    passed: bool,
    dataset_hash: str,
    slice_results: tuple[FeedbackSliceResult, ...],
    review_burden_reduction: float,
    evaluator: str,
    evaluated_at: datetime,
    programs_root: Path = PROGRAMS_ROOT,
) -> GovernedFeedbackProposal:
    proposal = _require_proposal(program_id, proposal_id, programs_root=programs_root)
    if proposal.status is not ProposalStatus.APPROVED or proposal.approval is None:
        raise ValueError("Shadow evaluation requires digest-bound human approval.")
    evaluation = FeedbackEvaluation(
        kind=EvaluationKind.SHADOW,
        passed=passed,
        dataset_hash=dataset_hash,
        frozen=True,
        excluded_from_runtime_examples=True,
        slice_results=slice_results,
        review_burden_reduction=review_burden_reduction,
        evaluator=evaluator,
        evaluated_at=_require_utc(evaluated_at),
    )
    updated = replace(
        proposal,
        status=ProposalStatus.SHADOW,
        evaluations=_replace_evaluation(proposal.evaluations, evaluation),
        updated_at=evaluation.evaluated_at,
        transition_actor=evaluator,
        transition_reason=f"shadow evaluation: {'passed' if passed else 'failed'}",
    )
    _append_proposals(program_id, (updated,), programs_root=programs_root)
    return updated


def promote_feedback_proposal(
    program_id: str,
    *,
    proposal_id: str,
    promoted_by: str,
    promoted_at: datetime,
    programs_root: Path = PROGRAMS_ROOT,
) -> GovernedFeedbackProposal:
    proposal = _require_proposal(program_id, proposal_id, programs_root=programs_root)
    if proposal.status is not ProposalStatus.SHADOW:
        raise ValueError("Promotion requires a proposal in shadow status.")
    _require_approval(proposal, as_of=_require_utc(promoted_at))
    if not any(
        evaluation.kind is EvaluationKind.SHADOW
        and evaluation.passed
        and evaluation.review_burden_reduction is not None
        and evaluation.review_burden_reduction > 0
        for evaluation in proposal.evaluations
    ):
        raise ValueError("Promotion requires passing shadow evidence with positive review-burden reduction.")
    appended: list[GovernedFeedbackProposal] = []
    for active in load_active_feedback_proposals(program_id, programs_root=programs_root, as_of=_require_utc(promoted_at)):
        if active.subject == proposal.subject and active.proposal_id != proposal.proposal_id:
            appended.append(
                replace(
                    active,
                    status=ProposalStatus.SUPERSEDED,
                    updated_at=_require_utc(promoted_at),
                    transition_actor=promoted_by,
                    transition_reason=f"Superseded by {proposal.proposal_id}.",
                )
            )
    promoted = replace(
        proposal,
        status=ProposalStatus.ACTIVE,
        updated_at=_require_utc(promoted_at),
        transition_actor=promoted_by,
        transition_reason="promoted after governance and shadow approval",
    )
    appended.append(promoted)
    _append_proposals(program_id, tuple(appended), programs_root=programs_root)
    return promoted


def rollback_feedback_proposal(
    program_id: str,
    *,
    proposal_id: str,
    rolled_back_by: str,
    reason: str,
    rolled_back_at: datetime,
    programs_root: Path = PROGRAMS_ROOT,
) -> GovernedFeedbackProposal:
    proposal = _require_proposal(program_id, proposal_id, programs_root=programs_root)
    if proposal.status is not ProposalStatus.ACTIVE or proposal.approval is None:
        raise ValueError("Only active governed proposals can be rolled back.")
    appended = [
        replace(
            proposal,
            status=ProposalStatus.ROLLED_BACK,
            updated_at=_require_utc(rolled_back_at),
            transition_actor=rolled_back_by,
            transition_reason=reason,
        )
    ]
    rollback_version = proposal.approval.rollback_to_version
    if rollback_version is not None:
        prior = _require_version(program_id, proposal.subject, rollback_version, programs_root=programs_root)
        if prior.status is not ProposalStatus.SUPERSEDED:
            raise ValueError("Rollback target must be a superseded governed version.")
        _require_approval(prior, as_of=_require_utc(rolled_back_at))
        appended.append(
            replace(
                prior,
                status=ProposalStatus.ACTIVE,
                updated_at=_require_utc(rolled_back_at),
                transition_actor=rolled_back_by,
                transition_reason=f"Restored by rollback of {proposal.proposal_id}.",
            )
        )
    _append_proposals(program_id, tuple(appended), programs_root=programs_root)
    return appended[0]


def reject_feedback_proposal(
    program_id: str,
    *,
    proposal_id: str,
    rejected_by: str,
    reason: str,
    rejected_at: datetime,
    programs_root: Path = PROGRAMS_ROOT,
) -> GovernedFeedbackProposal:
    proposal = _require_proposal(program_id, proposal_id, programs_root=programs_root)
    if proposal.status is ProposalStatus.ACTIVE:
        raise ValueError("Active proposals must be rolled back, not rejected.")
    return _transition(
        program_id,
        proposal,
        status=ProposalStatus.REJECTED,
        actor=rejected_by,
        reason=reason,
        at=_require_utc(rejected_at),
        programs_root=programs_root,
    )


def expire_feedback_proposal(
    program_id: str,
    *,
    proposal_id: str,
    expired_by: str,
    reason: str,
    expired_at: datetime,
    programs_root: Path = PROGRAMS_ROOT,
) -> GovernedFeedbackProposal:
    proposal = _require_proposal(program_id, proposal_id, programs_root=programs_root)
    return _transition(
        program_id,
        proposal,
        status=ProposalStatus.EXPIRED,
        actor=expired_by,
        reason=reason,
        at=_require_utc(expired_at),
        programs_root=programs_root,
    )


def reassign_feedback_proposal(
    program_id: str,
    *,
    proposal_id: str,
    owner: str,
    reassigned_by: str,
    reason: str,
    reassigned_at: datetime,
    programs_root: Path = PROGRAMS_ROOT,
) -> GovernedFeedbackProposal:
    proposal = _require_proposal(program_id, proposal_id, programs_root=programs_root)
    if not owner.strip():
        raise ValueError("Feedback proposal owner is required.")
    if proposal.status in {ProposalStatus.ACTIVE, ProposalStatus.ROLLED_BACK, ProposalStatus.SUPERSEDED}:
        raise ValueError(f"Cannot reassign proposal in status {proposal.status.value!r}.")
    updated = replace(
        proposal,
        owner=owner,
        status=(
            ProposalStatus.AWAITING_APPROVAL
            if _required_evaluations_pass(proposal.evaluations)
            else ProposalStatus.PROPOSED
        ),
        approval=None,
        updated_at=_require_utc(reassigned_at),
        transition_actor=reassigned_by,
        transition_reason=reason,
    )
    _append_proposals(program_id, (updated,), programs_root=programs_root)
    return updated


def load_feedback_proposals(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[GovernedFeedbackProposal, ...]:
    latest: dict[str, GovernedFeedbackProposal] = {}
    for record in read_jsonl_records(get_feedback_proposals_path(program_id, programs_root=programs_root)):
        proposal = _proposal_from_record(record)
        if proposal.program_id != program_id:
            raise ValueError("Feedback proposal store contains a foreign program.")
        latest[proposal.proposal_id] = proposal
    return tuple(sorted(latest.values(), key=lambda proposal: (proposal.subject, proposal.version, proposal.proposal_id)))


def load_feedback_proposal(
    program_id: str,
    proposal_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> GovernedFeedbackProposal:
    return _require_proposal(program_id, proposal_id, programs_root=programs_root)


def load_active_feedback_proposals(
    program_id: str,
    *,
    subject: str | None = None,
    as_of: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[GovernedFeedbackProposal, ...]:
    resolved_as_of = _require_utc(as_of or datetime.now(timezone.utc))
    active: list[GovernedFeedbackProposal] = []
    for proposal in load_feedback_proposals(program_id, programs_root=programs_root):
        if proposal.status is not ProposalStatus.ACTIVE or (
            subject is not None and proposal.subject != subject
        ):
            continue
        if proposal.approval is None:
            raise ValueError("Active feedback proposal is missing human approval.")
        if proposal.approval.expires_at <= resolved_as_of:
            continue
        _require_approval(proposal, as_of=resolved_as_of)
        active.append(proposal)
    return tuple(active)


def _transition(
    program_id: str,
    proposal: GovernedFeedbackProposal,
    *,
    status: ProposalStatus,
    actor: str,
    reason: str,
    at: datetime,
    programs_root: Path,
) -> GovernedFeedbackProposal:
    updated = replace(
        proposal,
        status=status,
        updated_at=at,
        transition_actor=actor,
        transition_reason=reason,
    )
    _append_proposals(program_id, (updated,), programs_root=programs_root)
    return updated


def _replace_evaluation(
    evaluations: tuple[FeedbackEvaluation, ...],
    evaluation: FeedbackEvaluation,
) -> tuple[FeedbackEvaluation, ...]:
    return tuple(item for item in evaluations if item.kind is not evaluation.kind) + (evaluation,)


def _required_evaluations_pass(evaluations: tuple[FeedbackEvaluation, ...]) -> bool:
    latest = {evaluation.kind.value: evaluation for evaluation in evaluations}
    return all(kind in latest and latest[kind].passed for kind in _REQUIRED_EVALUATIONS)


def _require_approval(proposal: GovernedFeedbackProposal, *, as_of: datetime) -> FeedbackApproval:
    approval = proposal.approval
    if approval is None:
        raise ValueError("Promotion requires human approval.")
    if approval.proposal_digest != proposal.payload_digest():
        raise ValueError("Approval is not bound to this proposal version.")
    if approval.expires_at <= as_of:
        raise ValueError("Governed proposal approval has expired.")
    return approval


def _latest_for_subject(
    program_id: str,
    subject: str,
    *,
    programs_root: Path,
) -> GovernedFeedbackProposal | None:
    candidates = [
        proposal
        for proposal in load_feedback_proposals(program_id, programs_root=programs_root)
        if proposal.subject == subject
    ]
    return max(candidates, key=lambda proposal: (proposal.version, proposal.proposal_id)) if candidates else None


def _require_proposal(
    program_id: str,
    proposal_id: str,
    *,
    programs_root: Path,
) -> GovernedFeedbackProposal:
    proposal = next(
        (
            item
            for item in load_feedback_proposals(program_id, programs_root=programs_root)
            if item.proposal_id == proposal_id
        ),
        None,
    )
    if proposal is None:
        raise ValueError(f"Unknown governed feedback proposal {proposal_id!r}.")
    return proposal


def _require_version(
    program_id: str,
    subject: str,
    version: int,
    *,
    programs_root: Path,
) -> GovernedFeedbackProposal:
    proposal = next(
        (
            item
            for item in load_feedback_proposals(program_id, programs_root=programs_root)
            if item.subject == subject and item.version == version
        ),
        None,
    )
    if proposal is None:
        raise ValueError(f"Unknown rollback version {version} for subject {subject!r}.")
    return proposal


def _append_proposals(
    program_id: str,
    proposals: tuple[GovernedFeedbackProposal, ...],
    *,
    programs_root: Path,
) -> None:
    if not proposals:
        return
    path = get_feedback_proposals_path(program_id, programs_root=programs_root)
    lock_path = path.parent / ".governed_feedback.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with portalocker.Lock(lock_path, mode="a+", timeout=5) as lock:
        for proposal in proposals:
            if proposal.program_id != program_id:
                raise ValueError("Refusing to append a foreign-program feedback proposal.")
            append_jsonl_line(
                path,
                json.dumps(_proposal_to_record(proposal), sort_keys=True) + "\n",
                max_bytes=_FEEDBACK_MAX_BYTES,
            )
        lock.flush()
        os.fsync(lock.fileno())


def _pending_payload_matches(
    *,
    existing: GovernedFeedbackProposal,
    program_id: str,
    subject: str,
    kind: ProposalKind,
    target: str,
    proposed_value: FeedbackValue,
    owner: str,
    privacy_class: FeedbackPrivacyClass,
    retention_class: FeedbackRetentionClass,
) -> bool:
    return (
        existing.program_id == program_id
        and existing.subject == subject
        and existing.kind is kind
        and existing.target == target
        and existing.proposed_value == proposed_value
        and existing.owner == owner
        and existing.privacy_class is privacy_class
        and existing.retention_class is retention_class
    )


def _observation_to_record(observation: FeedbackTelemetry) -> dict[str, Any]:
    return {
        "schema_version": observation.schema_version,
        "observation_id": observation.observation_id,
        "program_id": observation.program_id,
        "kind": observation.kind.value,
        "target_type": observation.target_type,
        "target_id": observation.target_id,
        "evidence_refs": list(observation.evidence_refs),
        "before_value": asdict(observation.before_value),
        "after_value": asdict(observation.after_value),
        "reason": observation.reason,
        "actor_role": observation.actor_role,
        "snapshot_ref": observation.snapshot_ref,
        "privacy_class": observation.privacy_class.value,
        "retention_class": observation.retention_class.value,
        "approved_by": observation.approved_by,
        "approved_at": _utc(observation.approved_at).isoformat(),
        "approval_ref": observation.approval_ref,
        "observed_at": _utc(observation.observed_at).isoformat(),
    }


def _observation_from_record(record: dict[str, Any]) -> FeedbackTelemetry:
    return FeedbackTelemetry(
        schema_version=str(record.get("schema_version", _SCHEMA_VERSION)),
        observation_id=str(record["observation_id"]),
        program_id=str(record["program_id"]),
        kind=FeedbackKind(str(record["kind"])),
        target_type=str(record["target_type"]),
        target_id=str(record["target_id"]),
        evidence_refs=tuple(str(item) for item in record["evidence_refs"]),
        before_value=_value_from_record(record["before_value"]),
        after_value=_value_from_record(record["after_value"]),
        reason=str(record["reason"]),
        actor_role=str(record["actor_role"]),
        snapshot_ref=str(record["snapshot_ref"]),
        privacy_class=FeedbackPrivacyClass(str(record["privacy_class"])),
        retention_class=FeedbackRetentionClass(str(record["retention_class"])),
        approved_by=str(record["approved_by"]),
        approved_at=_parse_datetime(str(record["approved_at"])),
        approval_ref=str(record["approval_ref"]),
        observed_at=_parse_datetime(str(record["observed_at"])),
    )


def _proposal_to_record(proposal: GovernedFeedbackProposal) -> dict[str, Any]:
    return {
        "schema_version": proposal.schema_version,
        "proposal_id": proposal.proposal_id,
        "program_id": proposal.program_id,
        "subject": proposal.subject,
        "version": proposal.version,
        "parent_proposal_id": proposal.parent_proposal_id,
        "kind": proposal.kind.value,
        "target": proposal.target,
        "proposed_value": asdict(proposal.proposed_value),
        "observation_ids": list(proposal.observation_ids),
        "owner": proposal.owner,
        "evidence_refs": list(proposal.evidence_refs),
        "reason": proposal.reason,
        "privacy_class": proposal.privacy_class.value,
        "retention_class": proposal.retention_class.value,
        "status": proposal.status.value,
        "created_at": _utc(proposal.created_at).isoformat(),
        "updated_at": _utc(proposal.updated_at).isoformat(),
        "evaluations": [_evaluation_to_record(item) for item in proposal.evaluations],
        "approval": (
            {
                **asdict(proposal.approval),
                "approved_at": _utc(proposal.approval.approved_at).isoformat(),
                "expires_at": _utc(proposal.approval.expires_at).isoformat(),
            }
            if proposal.approval is not None
            else None
        ),
        "transition_actor": proposal.transition_actor,
        "transition_reason": proposal.transition_reason,
    }


def _proposal_from_record(record: dict[str, Any]) -> GovernedFeedbackProposal:
    approval_record = record.get("approval")
    approval = (
        FeedbackApproval(
            approved_by=str(approval_record["approved_by"]),
            actor_role=str(approval_record["actor_role"]),
            approved_at=_parse_datetime(str(approval_record["approved_at"])),
            expires_at=_parse_datetime(str(approval_record["expires_at"])),
            kill_switch=str(approval_record["kill_switch"]),
            rollback_to_version=approval_record.get("rollback_to_version"),
            rollback_plan=str(approval_record["rollback_plan"]),
            proposal_digest=str(approval_record["proposal_digest"]),
        )
        if isinstance(approval_record, dict)
        else None
    )
    return GovernedFeedbackProposal(
        schema_version=str(record.get("schema_version", _SCHEMA_VERSION)),
        proposal_id=str(record["proposal_id"]),
        program_id=str(record["program_id"]),
        subject=str(record["subject"]),
        version=int(record["version"]),
        parent_proposal_id=record.get("parent_proposal_id"),
        kind=ProposalKind(str(record["kind"])),
        target=str(record["target"]),
        proposed_value=_value_from_record(record["proposed_value"]),
        observation_ids=tuple(str(item) for item in record["observation_ids"]),
        owner=str(record["owner"]),
        evidence_refs=tuple(str(item) for item in record["evidence_refs"]),
        reason=str(record["reason"]),
        privacy_class=FeedbackPrivacyClass(str(record["privacy_class"])),
        retention_class=FeedbackRetentionClass(str(record["retention_class"])),
        status=ProposalStatus(str(record["status"])),
        created_at=_parse_datetime(str(record["created_at"])),
        updated_at=_parse_datetime(str(record["updated_at"])),
        evaluations=tuple(_evaluation_from_record(item) for item in record.get("evaluations", ())),
        approval=approval,
        transition_actor=record.get("transition_actor"),
        transition_reason=record.get("transition_reason"),
    )


def _evaluation_to_record(evaluation: FeedbackEvaluation) -> dict[str, Any]:
    return {
        "kind": evaluation.kind.value,
        "passed": evaluation.passed,
        "dataset_hash": evaluation.dataset_hash,
        "frozen": evaluation.frozen,
        "excluded_from_runtime_examples": evaluation.excluded_from_runtime_examples,
        "slice_results": [asdict(item) for item in evaluation.slice_results],
        "review_burden_reduction": evaluation.review_burden_reduction,
        "evaluator": evaluation.evaluator,
        "evaluated_at": _utc(evaluation.evaluated_at).isoformat(),
    }


def _evaluation_from_record(record: dict[str, Any]) -> FeedbackEvaluation:
    return FeedbackEvaluation(
        kind=EvaluationKind(str(record["kind"])),
        passed=bool(record["passed"]),
        dataset_hash=record.get("dataset_hash"),
        frozen=bool(record["frozen"]),
        excluded_from_runtime_examples=bool(record["excluded_from_runtime_examples"]),
        slice_results=tuple(FeedbackSliceResult(**item) for item in record["slice_results"]),
        review_burden_reduction=record.get("review_burden_reduction"),
        evaluator=str(record["evaluator"]),
        evaluated_at=_parse_datetime(str(record["evaluated_at"])),
    )


def _value_from_record(record: dict[str, Any]) -> FeedbackValue:
    return FeedbackValue(
        value_type=str(record["value_type"]),
        value=record.get("value"),
        length=record.get("length"),
    )


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Governed feedback timestamps must be timezone-aware.")
    return value.astimezone(timezone.utc)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _require_utc(parsed)
