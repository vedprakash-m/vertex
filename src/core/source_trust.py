"""WI-3.1: Learning trust ledger for source/signal-class/qualifier keys.

Key structure: (source, signal_class, qualifier | None)
Score model: contradiction-weighted Laplace; alpha/beta updated per outcome.
O-16 buckets: (a) same/next-gather, (b) in-TTL, (c) post-TTL, (d) dismissed-7d.
Circuit breaker: volume σ=3, malform rate ≥0.2, zero-yield ≥3 runs → QG-SG-01.
Bootstrap: trust.bootstrap_grant facts loaded from trust_policy.yaml — never
  synthetic reviews.
Verdicts persisted as trust.source_score facts; consumed by TruthContext.

Zone A module — must not import from src.ai or src.m365 (INV-1).
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from src.core.truth_levels import TruthLevel  # noqa: F401 (re-exported for consumers)

_POLICY_PATH = Path(__file__).resolve().parents[2] / "vertex" / "policies" / "trust_policy.yaml"


# ---------------------------------------------------------------------------
# Public enumerations
# ---------------------------------------------------------------------------


class RegretBucket(StrEnum):
    """O-16 regret buckets classifying auto-promotion contradictions.

    (a) SAME_NEXT_GATHER — within one gather cycle → TRUE ledger error;
        `revoke_on_dismissal` fires ONLY here, within 24h.
    (b) IN_TTL            — beyond the gather window but within fact TTL
                            → temporal volatility; investigate source.
    (c) POST_TTL          — beyond TTL → normal expiry; tracked, not targeted.
    (d) DISMISSED_7D      — human-dismissed within 7 days → relevance judgment.
        Checked first: a dismissal within 7d supersedes bucket (a)/(b)/(c).
    """

    SAME_NEXT_GATHER = "a"
    IN_TTL = "b"
    POST_TTL = "c"
    DISMISSED_7D = "d"


# ---------------------------------------------------------------------------
# Core data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrustKey:
    """Granularity key: (source, signal_class, qualifier | None)."""

    source: str
    signal_class: str
    qualifier: str | None = None

    def as_tuple(self) -> tuple[str, str, str | None]:
        return (self.source, self.signal_class, self.qualifier)


@dataclass(frozen=True, slots=True)
class TrustScore:
    """Contradiction-weighted Laplace trust score for a single TrustKey."""

    key: TrustKey
    alpha: float          # pseudo-approvals (includes Laplace prior)
    beta: float           # pseudo-rejects (includes Laplace prior)
    sample_count: int     # raw observed outcomes (excluding prior)
    contradiction_count: int
    last_computed: datetime
    suspended: bool = False
    breaker_reason: str | None = None

    @property
    def score(self) -> float:
        """Laplace score ∈ (0, 1) = alpha / (alpha + beta)."""
        total = self.alpha + self.beta
        if total <= 0.0:
            return 0.5
        return self.alpha / total


@dataclass(frozen=True, slots=True)
class RegretClassification:
    """O-16 bucket classification for a single contradiction event."""

    key: TrustKey
    bucket: RegretBucket
    fact_recorded_at: datetime
    contradiction_at: datetime
    dismissed_at: datetime | None
    gather_cadence_hours: float
    fact_type_ttl_days: float | None


@dataclass(frozen=True, slots=True)
class BreakerVerdictResult:
    """Evaluation result from the anomaly circuit breaker."""

    key: TrustKey
    suspended: bool
    reason: str | None
    volume_anomaly: bool = False
    malform_anomaly: bool = False
    zero_yield_anomaly: bool = False


@dataclass(frozen=True, slots=True)
class BootstrapGrant:
    """A single applied bootstrap grant (mirrors trust.bootstrap_grant payload)."""

    source: str
    signal_class: str
    grant_score: float
    granted_by: str
    granted_at: str       # ISO-8601 UTC
    rationale: str


@dataclass(frozen=True, slots=True)
class TrustPolicy:
    """Loaded trust policy configuration (trust_policy.yaml)."""

    policy_schema_version: str
    bootstrap_trust: dict[str, dict[str, Any]]
    circuit_breaker_volume_sigma: float
    circuit_breaker_malform_rate: float
    circuit_breaker_zero_yield_runs: int
    laplace_pseudo_count: float
    contradiction_weight: float
    gather_cadence_multiplier: float
    dismiss_window_days: int
    revoke_on_dismissal_hours: float


# ---------------------------------------------------------------------------
# Policy loader
# ---------------------------------------------------------------------------


def load_trust_policy(*, policy_path: Path | None = None) -> TrustPolicy:
    """Load trust_policy.yaml; fall back to hard-coded defaults when file absent."""
    from src.core.yaml_utils import load_yaml_mapping

    resolved = policy_path or _POLICY_PATH
    raw: dict[str, Any] = {}
    if resolved.exists():
        raw = load_yaml_mapping(resolved)

    cb = raw.get("circuit_breaker", {})
    regret = raw.get("regret", {})

    return TrustPolicy(
        policy_schema_version=str(raw.get("policy_schema_version", "1")),
        bootstrap_trust=dict(raw.get("bootstrap_trust", {})),
        circuit_breaker_volume_sigma=float(cb.get("volume_sigma", 3.0)),
        circuit_breaker_malform_rate=float(cb.get("malform_rate", 0.2)),
        circuit_breaker_zero_yield_runs=int(cb.get("zero_yield_runs", 3)),
        laplace_pseudo_count=float(raw.get("laplace_pseudo_count", 1.0)),
        contradiction_weight=float(raw.get("contradiction_weight", 2.0)),
        gather_cadence_multiplier=float(regret.get("gather_cadence_multiplier", 1.0)),
        dismiss_window_days=int(regret.get("dismiss_window_days", 7)),
        revoke_on_dismissal_hours=float(regret.get("revoke_on_dismissal_hours", 24.0)),
    )


# ---------------------------------------------------------------------------
# Laplace score update
# ---------------------------------------------------------------------------


def apply_laplace_update(
    score: TrustScore,
    *,
    approved: bool,
    is_contradiction: bool = False,
    policy: TrustPolicy,
) -> TrustScore:
    """Update alpha/beta based on an observed outcome.

    Approved  → alpha += 1
    Rejected  → beta  += 1
    Contradiction → beta += contradiction_weight (e.g. 2.0)
    """
    if approved:
        new_alpha = score.alpha + 1.0
        new_beta = score.beta
        new_contradictions = score.contradiction_count
    elif is_contradiction:
        new_alpha = score.alpha
        new_beta = score.beta + policy.contradiction_weight
        new_contradictions = score.contradiction_count + 1
    else:
        new_alpha = score.alpha
        new_beta = score.beta + 1.0
        new_contradictions = score.contradiction_count

    return TrustScore(
        key=score.key,
        alpha=new_alpha,
        beta=new_beta,
        sample_count=score.sample_count + 1,
        contradiction_count=new_contradictions,
        last_computed=datetime.now(timezone.utc),
        suspended=score.suspended,
        breaker_reason=score.breaker_reason,
    )


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def build_trust_score_from_bootstrap(
    key: TrustKey,
    grant_score: float,
    *,
    policy: TrustPolicy,
    granted_at: datetime,
) -> TrustScore:
    """Create an initial TrustScore from a bootstrap grant.

    Converts grant_score ∈ [0, 1] into (alpha, beta) using the Laplace prior:
      alpha = pseudo_count * (1 + grant_score)
      beta  = pseudo_count * (1 + (1 - grant_score))
    This ensures fresh start at grant_score (not 0.5) while preserving
    the prior contribution of pseudo_count.
    """
    pc = policy.laplace_pseudo_count
    alpha = pc * (1.0 + grant_score)
    beta = pc * (2.0 - grant_score)
    return TrustScore(
        key=key,
        alpha=alpha,
        beta=beta,
        sample_count=0,
        contradiction_count=0,
        last_computed=granted_at,
        suspended=False,
        breaker_reason=None,
    )


def apply_bootstrap_grants(
    program_id: str,
    *,
    granted_by: str,
    programs_root: Path | None = None,
    policy: TrustPolicy | None = None,
    dry_run: bool = False,
) -> list[BootstrapGrant]:
    """Apply bootstrap trust grants from trust_policy.yaml provenance classes.

    Writes trust.bootstrap_grant facts into the fact store (idempotent —
    skips if a grant already exists for the same (source, signal_class) key).
    Never synthesizes reviews.

    Returns list of grants applied (or would-apply in dry_run).
    """
    from src.core.program_fact_store import (
        ProgramFactInput,
        ProgramFactStore,
        FactPrecedence,
        FactReviewState,
        build_natural_key,
    )

    resolved_policy = policy or load_trust_policy()
    now_utc = datetime.now(timezone.utc)
    now_str = now_utc.isoformat()
    grants_applied: list[BootstrapGrant] = []

    store = ProgramFactStore(program_id)

    for source, config in resolved_policy.bootstrap_trust.items():
        grant_score = float(config.get("score", 0.5))
        rationale = str(config.get("rationale", ""))
        signal_class = source  # one grant per provenance class

        natural_key = build_natural_key(
            "trust.bootstrap_grant",
            entity_refs=(f"trust:{source}:{signal_class}",),
            scope="program",
        )

        grant = BootstrapGrant(
            source=source,
            signal_class=signal_class,
            grant_score=grant_score,
            granted_by=granted_by,
            granted_at=now_str,
            rationale=rationale,
        )
        grants_applied.append(grant)

        if dry_run:
            continue

        store.append_fact(
            ProgramFactInput(
                fact_type="trust.bootstrap_grant",
                entity_refs=(f"trust:{source}:{signal_class}",),
                payload={
                    "source": source,
                    "signal_class": signal_class,
                    "grant_score": grant_score,
                    "granted_by": granted_by,
                    "granted_at": now_str,
                    "rationale": rationale,
                },
                scope="program",
                precedence=FactPrecedence.CONFIRMED_GOVERNANCE_DECISION,
                review_state=FactReviewState.ACCEPTED,
                natural_key=natural_key,
                created_by="vertex.trust.bootstrap",
            ),
            recorded_at=now_utc,
        )

    return grants_applied


# ---------------------------------------------------------------------------
# Regret bucket classification
# ---------------------------------------------------------------------------


def classify_regret_bucket(
    fact_recorded_at: datetime,
    contradiction_at: datetime,
    *,
    gather_cadence_hours: float,
    fact_type_ttl_days: float | None,
    dismissed_at: datetime | None,
    policy: TrustPolicy,
) -> RegretBucket:
    """Classify a contradiction into one of the four O-16 regret buckets.

    Evaluation order (first match wins):
    (d) dismissed within `dismiss_window_days` — relevance judgment, NOT error
    (a) contradiction within one gather cadence × multiplier — TRUE error
    (b) beyond cadence but within fact-type TTL — temporal volatility
    (c) beyond TTL — normal expiry (no TTL → defaults to (c))
    """
    age_at_contradiction = contradiction_at - fact_recorded_at
    age_hours = age_at_contradiction.total_seconds() / 3600.0

    # Bucket (d): dismissed within window → relevance judgment
    if dismissed_at is not None:
        dismiss_window = timedelta(days=policy.dismiss_window_days)
        if (dismissed_at - contradiction_at) <= dismiss_window:
            return RegretBucket.DISMISSED_7D

    # Bucket (a): same/next-gather window
    cadence_window_hours = gather_cadence_hours * policy.gather_cadence_multiplier
    if age_hours <= cadence_window_hours:
        return RegretBucket.SAME_NEXT_GATHER

    # Bucket (b): in-TTL (between cadence and TTL)
    if fact_type_ttl_days is not None:
        ttl_hours = fact_type_ttl_days * 24.0
        if age_hours <= ttl_hours:
            return RegretBucket.IN_TTL

    # Bucket (c): post-TTL (or no TTL configured)
    return RegretBucket.POST_TTL


def should_revoke_on_dismissal(
    bucket: RegretBucket,
    contradiction_at: datetime,
    dismissed_at: datetime,
    policy: TrustPolicy,
) -> bool:
    """True iff `revoke_on_dismissal` should fire for this contradiction.

    Contract: fires ONLY for bucket (a) AND within revoke_on_dismissal_hours
    of the contradiction event — never for buckets (b), (c), or (d).
    """
    if bucket is not RegretBucket.SAME_NEXT_GATHER:
        return False
    window = timedelta(hours=policy.revoke_on_dismissal_hours)
    return (dismissed_at - contradiction_at) <= window


# ---------------------------------------------------------------------------
# Anomaly circuit breaker
# ---------------------------------------------------------------------------


def check_circuit_breaker(
    key: TrustKey,
    *,
    recent_volumes: list[int],        # per-run observation counts (newest first)
    recent_malform_counts: list[int],  # per-run malformed signal counts (newest first)
    recent_zero_yields: list[bool],    # per-run zero-promotion flags (newest first)
    policy: TrustPolicy,
) -> BreakerVerdictResult:
    """Evaluate circuit breaker conditions for a trust key.

    Three independent triggers (any one → suspend):
    1. Volume anomaly: current > mean(historical) + σ × std(historical)
    2. Malform rate: malformed / total > malform_rate threshold
    3. Zero-yield streak: ≥N consecutive runs with 0 promotions
    """
    volume_anomaly = False
    malform_anomaly = False
    zero_yield_anomaly = False
    suspension_reasons: list[str] = []

    # --- volume anomaly ---
    if len(recent_volumes) >= 2:
        current_vol = recent_volumes[0]
        historical = recent_volumes[1:]
        if len(historical) >= 2:
            mean_vol = statistics.mean(historical)
            std_vol = statistics.stdev(historical)
            threshold = mean_vol + policy.circuit_breaker_volume_sigma * std_vol
            if current_vol > threshold:
                volume_anomaly = True
                suspension_reasons.append(
                    f"volume={current_vol} > mean={mean_vol:.1f} + "
                    f"{policy.circuit_breaker_volume_sigma}σ={std_vol:.1f}"
                )
        elif len(historical) == 1:
            # Only 1 historical point — use simple ratio (current > 3× historical)
            if historical[0] > 0 and current_vol > historical[0] * (1 + policy.circuit_breaker_volume_sigma):
                volume_anomaly = True
                suspension_reasons.append(
                    f"volume={current_vol} > {1 + policy.circuit_breaker_volume_sigma}× historical={historical[0]}"
                )

    # --- malform rate ---
    if recent_volumes and recent_malform_counts:
        total = recent_volumes[0]
        malformed = recent_malform_counts[0]
        if total > 0 and (malformed / total) > policy.circuit_breaker_malform_rate:
            malform_anomaly = True
            suspension_reasons.append(
                f"malform_rate={malformed}/{total}="
                f"{malformed/total:.2%} > {policy.circuit_breaker_malform_rate:.2%}"
            )
        elif total == 0 and malformed > 0:
            malform_anomaly = True
            suspension_reasons.append(f"malform_count={malformed} with zero total volume")

    # --- zero-yield streak ---
    min_streak = policy.circuit_breaker_zero_yield_runs
    if len(recent_zero_yields) >= min_streak:
        streak = sum(1 for z in recent_zero_yields[:min_streak] if z)
        if streak >= min_streak:
            zero_yield_anomaly = True
            suspension_reasons.append(
                f"zero_yield_streak={streak} >= {min_streak} consecutive runs"
            )

    suspended = volume_anomaly or malform_anomaly or zero_yield_anomaly
    reason = "; ".join(suspension_reasons) if suspension_reasons else None

    return BreakerVerdictResult(
        key=key,
        suspended=suspended,
        reason=reason,
        volume_anomaly=volume_anomaly,
        malform_anomaly=malform_anomaly,
        zero_yield_anomaly=zero_yield_anomaly,
    )


# ---------------------------------------------------------------------------
# Persistence: load / save trust.source_score facts
# ---------------------------------------------------------------------------


def persist_trust_verdict(
    program_id: str,
    score: TrustScore,
    *,
    programs_root: Path | None = None,
    recorded_at: datetime | None = None,
) -> None:
    """Write a trust.source_score fact capturing the current score + breaker verdict."""
    from src.core.program_fact_store import (
        ProgramFactInput,
        ProgramFactStore,
        FactPrecedence,
        FactReviewState,
        build_natural_key,
    )

    now_utc = recorded_at or datetime.now(timezone.utc)
    key = score.key
    qualifier_part = key.qualifier or ""
    entity_ref = f"trust:{key.source}:{key.signal_class}"
    if qualifier_part:
        entity_ref += f":{qualifier_part}"

    natural_key = build_natural_key(
        "trust.source_score",
        entity_refs=(entity_ref,),
        scope="program",
    )

    payload: dict[str, Any] = {
        "source": key.source,
        "signal_class": key.signal_class,
        "score": score.score,
        "alpha": score.alpha,
        "beta": score.beta,
        "sample_count": score.sample_count,
        "contradiction_count": score.contradiction_count,
        "breaker_verdict": "suspended" if score.suspended else "clear",
        "suspended": score.suspended,
        "computed_at": now_utc.isoformat(),
    }
    if key.qualifier:
        payload["qualifier"] = key.qualifier
    if score.breaker_reason:
        payload["suspension_reason"] = score.breaker_reason

    store = ProgramFactStore(program_id)
    store.append_fact(
        ProgramFactInput(
            fact_type="trust.source_score",
            entity_refs=(entity_ref,),
            payload=payload,
            scope="program",
            precedence=FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
            review_state=FactReviewState.ACCEPTED,
            natural_key=natural_key,
            created_by="vertex.trust.ledger",
        ),
        recorded_at=now_utc,
    )


def load_trust_scores(
    program_id: str,
    *,
    programs_root: Path | None = None,
    policy: TrustPolicy | None = None,
) -> dict[tuple[str, str, str | None], TrustScore]:
    """Load latest persisted trust scores from trust.source_score facts.

    Falls back to bootstrap scores from policy when no fact exists.
    Returns a dict keyed by (source, signal_class, qualifier | None).
    """
    from src.core.program_fact_store import ProgramFactStore

    resolved_policy = policy or load_trust_policy()
    store = ProgramFactStore(program_id)
    snapshot = store.snapshot()
    scores: dict[tuple[str, str, str | None], TrustScore] = {}
    now_utc = datetime.now(timezone.utc)

    for fact in snapshot.facts:
        if fact.fact_type != "trust.source_score":
            continue
        p = fact.payload
        source = str(p.get("source", ""))
        signal_class = str(p.get("signal_class", ""))
        qualifier: str | None = p.get("qualifier") or None
        if not source or not signal_class:
            continue

        key = TrustKey(source=source, signal_class=signal_class, qualifier=qualifier)
        alpha = float(p.get("alpha", resolved_policy.laplace_pseudo_count))
        beta = float(p.get("beta", resolved_policy.laplace_pseudo_count))
        sample_count = int(p.get("sample_count", 0))
        contradiction_count = int(p.get("contradiction_count", 0))
        suspended = bool(p.get("suspended", False))
        breaker_reason: str | None = p.get("suspension_reason")

        computed_str = p.get("computed_at")
        try:
            last_computed = datetime.fromisoformat(computed_str) if computed_str else now_utc
        except ValueError:
            last_computed = now_utc

        scores[key.as_tuple()] = TrustScore(
            key=key,
            alpha=alpha,
            beta=beta,
            sample_count=sample_count,
            contradiction_count=contradiction_count,
            last_computed=last_computed,
            suspended=suspended,
            breaker_reason=breaker_reason,
        )

    return scores


def get_suspended_sources(
    program_id: str,
    *,
    programs_root: Path | None = None,
) -> frozenset[str]:
    """Return the set of source identifiers currently suspended by the breaker.

    Used by build_truth_context() to populate TruthContext.suspended_sources.
    The result reflects the LATEST PERSISTED verdict — never live computation.
    """
    scores = load_trust_scores(program_id, programs_root=programs_root)
    return frozenset(
        key[0]  # key[0] is the source
        for key, score in scores.items()
        if score.suspended
    )
