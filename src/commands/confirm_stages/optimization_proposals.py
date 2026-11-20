"""FR-SG-37 offline proposals routed through RELO-D19 governance."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path

from src.core.analytics_store import get_override_streaks
from src.core.governed_feedback_lifecycle import (
    FeedbackKind,
    FeedbackPrivacyClass,
    FeedbackRetentionClass,
    FeedbackTelemetry,
    FeedbackValue,
    ProposalKind,
    propose_feedback_change,
    record_feedback_observation,
)


def write_optimization_proposals(
    program_id: str,
    *,
    edition_id: str,
    issue_number: int,
    approved_by: str,
    programs_root: Path,
) -> None:
    """Stage derivation proposals without mutating policy or model state."""
    streaks = get_override_streaks(program_id, min_streak=3, programs_root=programs_root)
    if not streaks:
        return
    observed_at = datetime.now(timezone.utc)
    for streak in streaks:
        snapshot_ref = f"confirmed_issue:{edition_id}:{issue_number:03d}"
        observation_seed = (
            f"{program_id}:{edition_id}:{issue_number}:{streak.dimension}:{streak.override_value}"
        )
        observation_id = f"override-streak:{hashlib.sha256(observation_seed.encode('utf-8')).hexdigest()}"
        record_feedback_observation(
            FeedbackTelemetry(
                observation_id=observation_id,
                program_id=program_id,
                kind=FeedbackKind.RISK_OVERRIDE,
                target_type="scorecard_dimension",
                target_id=streak.dimension,
                evidence_refs=(f"override_streak:{streak.dimension}", snapshot_ref),
                before_value=(
                    FeedbackValue.enum(streak.original_value)
                    if streak.original_value is not None
                    else FeedbackValue.redacted()
                ),
                after_value=FeedbackValue.enum(streak.override_value),
                reason=f"confirmed override repeated {streak.streak_count} times",
                actor_role="confirming_operator",
                snapshot_ref=snapshot_ref,
                privacy_class=FeedbackPrivacyClass.RESTRICTED,
                retention_class=FeedbackRetentionClass.ISSUE_CYCLE,
                approved_by=approved_by,
                approved_at=observed_at,
                approval_ref=snapshot_ref,
                observed_at=observed_at,
            ),
            programs_root=programs_root,
        )
        propose_feedback_change(
            program_id,
            subject=f"derivation-adjustment:{streak.dimension}:{streak.override_value}",
            kind=ProposalKind.THRESHOLD,
            target=f"scorecard_dimensions.{streak.dimension}.default_risk",
            proposed_value=FeedbackValue.enum(streak.override_value),
            observation_ids=(observation_id,),
            owner="program-policy-owner",
            evidence_refs=(f"override_streak:{streak.dimension}", snapshot_ref),
            reason=f"offline proposal after {streak.streak_count} confirmed overrides",
            privacy_class=FeedbackPrivacyClass.RESTRICTED,
            retention_class=FeedbackRetentionClass.ISSUE_CYCLE,
            created_at=observed_at,
            programs_root=programs_root,
        )
