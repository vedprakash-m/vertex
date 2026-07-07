from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Literal

from src.core.hypothesis_models import (
    DigestDelta,
    ChallengeSeverity,
    MetricFreshnessEntry,
    RealityChallenge,
    RealityDigestModel,
    StaleHypothesisEntry,
    SuppressionSummary,
)


def build_digest_model(
    *,
    program_id: str,
    as_of: datetime,
    confirmed_count: int,
    challenged_count: int,
    stale_count: int,
    proposed_count: int,
    recovered_count: int,
    source_freshness: tuple[MetricFreshnessEntry, ...],
    open_challenges: tuple[RealityChallenge, ...],
    stale_entries: tuple[StaleHypothesisEntry, ...] = (),
    suppressed_during_maintenance: tuple[SuppressionSummary, ...] = (),
    delta_since_last_digest: DigestDelta | None = None,
    policy_version: int = 1,
) -> RealityDigestModel:
    return RealityDigestModel(
        program_id=program_id,
        as_of=as_of,
        health=_infer_health(
            confirmed_count=confirmed_count,
            challenged_count=challenged_count,
            stale_count=stale_count,
            proposed_count=proposed_count,
            open_challenges=open_challenges,
        ),
        confirmed_count=confirmed_count,
        challenged_count=challenged_count,
        stale_count=stale_count,
        proposed_count=proposed_count,
        recovered_count=recovered_count,
        source_freshness=tuple(sorted(source_freshness, key=lambda entry: entry.metric_id)),
        open_challenges=tuple(sorted(open_challenges, key=_challenge_sort_key)),
        stale_entries=tuple(sorted(stale_entries, key=lambda entry: entry.hypothesis_id)),
        recovered_entries=(),
        suppressed_during_maintenance=tuple(suppressed_during_maintenance),
        delta_since_last_digest=delta_since_last_digest,
        cache_built_at=datetime.now(timezone.utc),
        policy_version=policy_version,
    )


def serialize_digest_model(model: RealityDigestModel) -> str:
    payload = {
        "program_id": model.program_id,
        "as_of": model.as_of.isoformat(),
        "health": model.health,
        "confirmed_count": model.confirmed_count,
        "challenged_count": model.challenged_count,
        "stale_count": model.stale_count,
        "proposed_count": model.proposed_count,
        "recovered_count": model.recovered_count,
        "source_freshness": [
            {
                "metric_id": entry.metric_id,
                "last_observed_at": entry.last_observed_at.isoformat() if entry.last_observed_at else None,
                "quality_state": entry.quality_state.value,
                "hours_since_last_observation": entry.hours_since_last_observation,
            }
            for entry in model.source_freshness
        ],
        "open_challenges": [
            {
                "id": challenge.id,
                "hypothesis_id": challenge.hypothesis_id,
                "challenge_kind": challenge.challenge_kind.value,
                "severity": challenge.severity.value,
                "current_state": challenge.current_state.value,
                "detected_at": challenge.detected_at.isoformat(),
                "ado_current_target": challenge.ado_current_target,
                "note": challenge.note,
            }
            for challenge in model.open_challenges
        ],
        "stale_entries": [
            {
                "hypothesis_id": entry.hypothesis_id,
                "confirmed_at": entry.confirmed_at.isoformat(),
                "days_since_confirmation": entry.days_since_confirmation,
                "last_observation_at": entry.last_observation_at.isoformat() if entry.last_observation_at else None,
                "staleness_reason": entry.staleness_reason,
            }
            for entry in model.stale_entries
        ],
        "recovered_entries": [],
        "suppressed_during_maintenance": [
            {
                "maintenance_window_id": entry.maintenance_window_id,
                "title": entry.title,
                "suppressed_count": entry.suppressed_count,
                "starts_at": entry.starts_at.isoformat(),
                "ends_at": entry.ends_at.isoformat(),
            }
            for entry in model.suppressed_during_maintenance
        ],
        "delta_since_last_digest": None
        if model.delta_since_last_digest is None
        else {
            "since": model.delta_since_last_digest.since.isoformat(),
            "to": model.delta_since_last_digest.to.isoformat(),
            "challenges_opened": model.delta_since_last_digest.challenges_opened,
            "challenges_resolved": model.delta_since_last_digest.challenges_resolved,
            "challenges_dismissed": model.delta_since_last_digest.challenges_dismissed,
            "challenges_snoozed": model.delta_since_last_digest.challenges_snoozed,
            "hypotheses_proposed": model.delta_since_last_digest.hypotheses_proposed,
            "hypotheses_confirmed": model.delta_since_last_digest.hypotheses_confirmed,
            "hypotheses_recovered": model.delta_since_last_digest.hypotheses_recovered,
            "hypotheses_superseded": model.delta_since_last_digest.hypotheses_superseded,
        },
        "cache_built_at": model.cache_built_at.isoformat(),
        "policy_version": model.policy_version,
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def compute_digest_sha256(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _infer_health(
    *,
    confirmed_count: int,
    challenged_count: int,
    stale_count: int,
    proposed_count: int,
    open_challenges: tuple[RealityChallenge, ...],
) -> Literal["green", "amber", "red", "uninitialized"]:
    if confirmed_count == 0 and challenged_count == 0 and stale_count == 0 and proposed_count == 0:
        return "uninitialized"
    if any(challenge.severity == ChallengeSeverity.ALERT for challenge in open_challenges) or challenged_count > 0:
        return "red"
    if stale_count > 0 or open_challenges or proposed_count > 0:
        return "amber"
    return "green"


def _challenge_sort_key(challenge: RealityChallenge) -> tuple[int, float]:
    severity_rank = {"alert": 0, "warn": 1, "info": 2}
    detected_at_rank = -challenge.detected_at.timestamp()
    return (severity_rank.get(challenge.severity.value, 99), detected_at_rank)
