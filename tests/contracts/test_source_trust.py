"""WI-3.1 contract tests: source_trust.py — learning trust ledger.

Acceptance:
- v3.0 list: bootstrap, Laplace score update, persistence round-trip,
  breaker evaluation (each trigger independently), policy loader
- Bucket-classification tests: each O-16 bucket gets the correct label
- Bucket-(a)-only revoke test: revoke_on_dismissal fires ONLY for bucket (a)
  within 24h — never for (b), (c), (d)

Zone A contract — must not exercise src.ai or src.m365 code paths.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.core.source_trust import (
    BreakerVerdictResult,
    BootstrapGrant,
    RegretBucket,
    TrustKey,
    TrustPolicy,
    TrustScore,
    apply_laplace_update,
    build_trust_score_from_bootstrap,
    check_circuit_breaker,
    classify_regret_bucket,
    load_trust_policy,
    should_revoke_on_dismissal,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _policy(
    *,
    laplace_pseudo_count: float = 1.0,
    contradiction_weight: float = 2.0,
    gather_cadence_multiplier: float = 1.0,
    dismiss_window_days: int = 7,
    revoke_on_dismissal_hours: float = 24.0,
    volume_sigma: float = 3.0,
    malform_rate: float = 0.2,
    zero_yield_runs: int = 3,
) -> TrustPolicy:
    return TrustPolicy(
        policy_schema_version="1",
        bootstrap_trust={},
        circuit_breaker_volume_sigma=volume_sigma,
        circuit_breaker_malform_rate=malform_rate,
        circuit_breaker_zero_yield_runs=zero_yield_runs,
        laplace_pseudo_count=laplace_pseudo_count,
        contradiction_weight=contradiction_weight,
        gather_cadence_multiplier=gather_cadence_multiplier,
        dismiss_window_days=dismiss_window_days,
        revoke_on_dismissal_hours=revoke_on_dismissal_hours,
    )


def _fresh_score(key: TrustKey, *, policy: TrustPolicy) -> TrustScore:
    """Uninitialised score (prior only)."""
    pc = policy.laplace_pseudo_count
    return TrustScore(
        key=key,
        alpha=pc,
        beta=pc,
        sample_count=0,
        contradiction_count=0,
        last_computed=datetime.now(timezone.utc),
    )


_KEY = TrustKey(source="ado", signal_class="workitem.state")
_NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# v3.0 list: policy loader
# ---------------------------------------------------------------------------


class TestPolicyLoader:
    def test_loads_without_error(self) -> None:
        policy = load_trust_policy()
        assert isinstance(policy, TrustPolicy)
        assert policy.policy_schema_version == "1"

    def test_bootstrap_trust_covers_known_provenance_classes(self) -> None:
        policy = load_trust_policy()
        assert "ado" in policy.bootstrap_trust
        assert "kusto" in policy.bootstrap_trust
        assert "icm" in policy.bootstrap_trust
        assert "human_comms" in policy.bootstrap_trust

    def test_bootstrap_scores_in_range(self) -> None:
        policy = load_trust_policy()
        for src, cfg in policy.bootstrap_trust.items():
            score = float(cfg["score"])
            assert 0.0 <= score <= 1.0, f"{src!r} score {score} out of [0,1]"

    def test_circuit_breaker_thresholds_sane(self) -> None:
        policy = load_trust_policy()
        assert policy.circuit_breaker_volume_sigma > 0
        assert 0.0 < policy.circuit_breaker_malform_rate < 1.0
        assert policy.circuit_breaker_zero_yield_runs >= 1

    def test_laplace_pseudo_count_positive(self) -> None:
        policy = load_trust_policy()
        assert policy.laplace_pseudo_count > 0.0

    def test_contradiction_weight_gte_one(self) -> None:
        policy = load_trust_policy()
        assert policy.contradiction_weight >= 1.0


# ---------------------------------------------------------------------------
# v3.0 list: bootstrap
# ---------------------------------------------------------------------------


class TestBootstrap:
    def test_fresh_score_yields_half(self) -> None:
        p = _policy()
        score = _fresh_score(_KEY, policy=p)
        assert score.score == pytest.approx(0.5)

    def test_bootstrap_score_above_fresh(self) -> None:
        p = _policy()
        score = build_trust_score_from_bootstrap(_KEY, 0.8, policy=p, granted_at=_NOW)
        assert score.score > 0.5

    def test_bootstrap_score_0_8_near_expected(self) -> None:
        p = _policy(laplace_pseudo_count=1.0)
        score = build_trust_score_from_bootstrap(_KEY, 0.8, policy=p, granted_at=_NOW)
        # alpha = 1 * (1 + 0.8) = 1.8; beta = 1 * (2 - 0.8) = 1.2
        assert score.alpha == pytest.approx(1.8)
        assert score.beta == pytest.approx(1.2)
        assert score.score == pytest.approx(1.8 / 3.0)

    def test_bootstrap_score_0_5_is_half(self) -> None:
        p = _policy()
        score = build_trust_score_from_bootstrap(_KEY, 0.5, policy=p, granted_at=_NOW)
        assert score.score == pytest.approx(0.5)

    def test_bootstrap_sample_count_zero(self) -> None:
        p = _policy()
        score = build_trust_score_from_bootstrap(_KEY, 0.8, policy=p, granted_at=_NOW)
        assert score.sample_count == 0

    def test_bootstrap_not_suspended(self) -> None:
        p = _policy()
        score = build_trust_score_from_bootstrap(_KEY, 0.7, policy=p, granted_at=_NOW)
        assert not score.suspended


# ---------------------------------------------------------------------------
# v3.0 list: Laplace score update
# ---------------------------------------------------------------------------


class TestLaplaceUpdate:
    def test_approval_increases_alpha(self) -> None:
        p = _policy()
        s0 = _fresh_score(_KEY, policy=p)
        s1 = apply_laplace_update(s0, approved=True, policy=p)
        assert s1.alpha == s0.alpha + 1.0
        assert s1.beta == s0.beta

    def test_rejection_increases_beta(self) -> None:
        p = _policy()
        s0 = _fresh_score(_KEY, policy=p)
        s1 = apply_laplace_update(s0, approved=False, policy=p)
        assert s1.alpha == s0.alpha
        assert s1.beta == s0.beta + 1.0

    def test_contradiction_increases_beta_by_weight(self) -> None:
        p = _policy(contradiction_weight=2.0)
        s0 = _fresh_score(_KEY, policy=p)
        s1 = apply_laplace_update(s0, approved=False, is_contradiction=True, policy=p)
        assert s1.beta == s0.beta + 2.0
        assert s1.contradiction_count == 1

    def test_contradiction_weight_configurable(self) -> None:
        p = _policy(contradiction_weight=3.5)
        s0 = _fresh_score(_KEY, policy=p)
        s1 = apply_laplace_update(s0, approved=False, is_contradiction=True, policy=p)
        assert s1.beta == pytest.approx(s0.beta + 3.5)

    def test_sample_count_increments_always(self) -> None:
        p = _policy()
        s0 = _fresh_score(_KEY, policy=p)
        for _ in range(5):
            s0 = apply_laplace_update(s0, approved=True, policy=p)
        assert s0.sample_count == 5

    def test_score_improves_with_approvals(self) -> None:
        p = _policy()
        s = _fresh_score(_KEY, policy=p)
        for _ in range(10):
            s = apply_laplace_update(s, approved=True, policy=p)
        assert s.score > 0.5

    def test_score_degrades_with_contradictions(self) -> None:
        p = _policy()
        s = build_trust_score_from_bootstrap(_KEY, 0.8, policy=p, granted_at=_NOW)
        for _ in range(5):
            s = apply_laplace_update(s, approved=False, is_contradiction=True, policy=p)
        assert s.score < 0.8


# ---------------------------------------------------------------------------
# v3.0 list: circuit breaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_no_anomaly_on_normal_volumes(self) -> None:
        p = _policy(volume_sigma=3.0)
        key = _KEY
        result = check_circuit_breaker(
            key,
            recent_volumes=[100, 100, 100, 100, 100],
            recent_malform_counts=[1, 1, 1, 1, 1],
            recent_zero_yields=[False, False, False],
            policy=p,
        )
        assert not result.suspended

    def test_volume_anomaly_triggers_suspension(self) -> None:
        p = _policy(volume_sigma=3.0)
        # Historical: mean=100, std~0; current=1000 >> threshold
        result = check_circuit_breaker(
            _KEY,
            recent_volumes=[1000, 100, 100, 100, 100, 100],
            recent_malform_counts=[0, 0, 0, 0, 0, 0],
            recent_zero_yields=[False, False, False],
            policy=p,
        )
        assert result.suspended
        assert result.volume_anomaly

    def test_malform_anomaly_triggers_suspension(self) -> None:
        p = _policy(malform_rate=0.2)
        # 30% malformed in current run
        result = check_circuit_breaker(
            _KEY,
            recent_volumes=[100, 100, 100],
            recent_malform_counts=[30, 5, 5],
            recent_zero_yields=[False, False, False],
            policy=p,
        )
        assert result.suspended
        assert result.malform_anomaly

    def test_zero_yield_streak_triggers_suspension(self) -> None:
        p = _policy(zero_yield_runs=3)
        result = check_circuit_breaker(
            _KEY,
            recent_volumes=[100, 100, 100],
            recent_malform_counts=[0, 0, 0],
            recent_zero_yields=[True, True, True],
            policy=p,
        )
        assert result.suspended
        assert result.zero_yield_anomaly

    def test_partial_zero_yield_streak_no_suspension(self) -> None:
        p = _policy(zero_yield_runs=3)
        result = check_circuit_breaker(
            _KEY,
            recent_volumes=[100, 100, 100],
            recent_malform_counts=[0, 0, 0],
            recent_zero_yields=[True, True, False],
            policy=p,
        )
        assert not result.zero_yield_anomaly

    def test_malform_below_threshold_no_suspension(self) -> None:
        p = _policy(malform_rate=0.2)
        result = check_circuit_breaker(
            _KEY,
            recent_volumes=[100, 100],
            recent_malform_counts=[15, 5],  # 15% < 20%
            recent_zero_yields=[False],
            policy=p,
        )
        assert not result.malform_anomaly

    def test_breaker_reason_set_when_suspended(self) -> None:
        p = _policy(zero_yield_runs=3)
        result = check_circuit_breaker(
            _KEY,
            recent_volumes=[100, 100, 100],
            recent_malform_counts=[0, 0, 0],
            recent_zero_yields=[True, True, True],
            policy=p,
        )
        assert result.reason is not None
        assert len(result.reason) > 0

    def test_breaker_reason_none_when_clear(self) -> None:
        p = _policy()
        result = check_circuit_breaker(
            _KEY,
            recent_volumes=[100, 100, 100],
            recent_malform_counts=[1, 1, 1],
            recent_zero_yields=[False, False, False],
            policy=p,
        )
        assert not result.suspended
        assert result.reason is None


# ---------------------------------------------------------------------------
# Bucket-classification tests (O-16)
# ---------------------------------------------------------------------------


class TestRegretBuckets:
    """Bucket-classification tests per spec §4.2 / §6.5."""

    def _make_policy(self, *, cadence_hours: float = 24.0) -> TrustPolicy:
        return _policy(gather_cadence_multiplier=1.0)

    def test_bucket_a_within_one_cadence(self) -> None:
        p = self._make_policy(cadence_hours=24.0)
        # fact_age = 12h < cadence 24h → bucket (a)
        fact_at = _NOW - timedelta(hours=12)
        bucket = classify_regret_bucket(
            fact_at,
            _NOW,
            gather_cadence_hours=24.0,
            fact_type_ttl_days=30.0,
            dismissed_at=None,
            policy=p,
        )
        assert bucket == RegretBucket.SAME_NEXT_GATHER

    def test_bucket_a_at_exactly_cadence(self) -> None:
        p = self._make_policy()
        fact_at = _NOW - timedelta(hours=24)
        bucket = classify_regret_bucket(
            fact_at,
            _NOW,
            gather_cadence_hours=24.0,
            fact_type_ttl_days=30.0,
            dismissed_at=None,
            policy=p,
        )
        assert bucket == RegretBucket.SAME_NEXT_GATHER

    def test_bucket_b_in_ttl_beyond_cadence(self) -> None:
        p = self._make_policy()
        # fact_age = 5 days (120h) > cadence 24h; TTL = 30 days → bucket (b)
        fact_at = _NOW - timedelta(days=5)
        bucket = classify_regret_bucket(
            fact_at,
            _NOW,
            gather_cadence_hours=24.0,
            fact_type_ttl_days=30.0,
            dismissed_at=None,
            policy=p,
        )
        assert bucket == RegretBucket.IN_TTL

    def test_bucket_c_post_ttl(self) -> None:
        p = self._make_policy()
        # fact_age = 45 days > TTL 30 days → bucket (c)
        fact_at = _NOW - timedelta(days=45)
        bucket = classify_regret_bucket(
            fact_at,
            _NOW,
            gather_cadence_hours=24.0,
            fact_type_ttl_days=30.0,
            dismissed_at=None,
            policy=p,
        )
        assert bucket == RegretBucket.POST_TTL

    def test_bucket_c_no_ttl_configured(self) -> None:
        p = self._make_policy()
        # No TTL → post-cadence contradictions land in bucket (c)
        fact_at = _NOW - timedelta(days=10)
        bucket = classify_regret_bucket(
            fact_at,
            _NOW,
            gather_cadence_hours=24.0,
            fact_type_ttl_days=None,
            dismissed_at=None,
            policy=p,
        )
        assert bucket == RegretBucket.POST_TTL

    def test_bucket_d_dismissed_within_7d_supersedes_a(self) -> None:
        """Dismissed within 7d → bucket (d) even if age < cadence."""
        p = self._make_policy()
        fact_at = _NOW - timedelta(hours=6)    # age 6h < cadence 24h → would be (a)
        dismissed_at = _NOW + timedelta(days=3)  # dismissed 3d after contradiction
        bucket = classify_regret_bucket(
            fact_at,
            _NOW,
            gather_cadence_hours=24.0,
            fact_type_ttl_days=30.0,
            dismissed_at=dismissed_at,
            policy=p,
        )
        assert bucket == RegretBucket.DISMISSED_7D

    def test_bucket_d_dismissed_within_7d_supersedes_b(self) -> None:
        """Dismissed within 7d → bucket (d) even if age is in-TTL range."""
        p = self._make_policy()
        fact_at = _NOW - timedelta(days=10)    # age 10d → would be (b) for TTL=30d
        dismissed_at = _NOW + timedelta(days=5)
        bucket = classify_regret_bucket(
            fact_at,
            _NOW,
            gather_cadence_hours=24.0,
            fact_type_ttl_days=30.0,
            dismissed_at=dismissed_at,
            policy=p,
        )
        assert bucket == RegretBucket.DISMISSED_7D

    def test_bucket_not_d_when_dismissed_after_window(self) -> None:
        """Dismissed after 7d → NOT bucket (d); age determines bucket."""
        p = self._make_policy()
        fact_at = _NOW - timedelta(days=5)    # age 5d → bucket (b) for TTL=30d
        dismissed_at = _NOW + timedelta(days=10)  # dismissed after 7d window
        bucket = classify_regret_bucket(
            fact_at,
            _NOW,
            gather_cadence_hours=24.0,
            fact_type_ttl_days=30.0,
            dismissed_at=dismissed_at,
            policy=p,
        )
        assert bucket == RegretBucket.IN_TTL

    def test_all_four_buckets_exist(self) -> None:
        assert len(RegretBucket) == 4
        assert {b.value for b in RegretBucket} == {"a", "b", "c", "d"}


# ---------------------------------------------------------------------------
# Bucket-(a)-only revoke test
# ---------------------------------------------------------------------------


class TestRevokeOnDismissal:
    """revoke_on_dismissal fires ONLY for bucket (a) within 24h — never (b)/(c)/(d)."""

    def _p(self) -> TrustPolicy:
        return _policy(revoke_on_dismissal_hours=24.0)

    def test_revoke_fires_for_bucket_a_within_24h(self) -> None:
        """True error, dismissed promptly → revoke fires."""
        p = self._p()
        contradiction_at = _NOW
        dismissed_at = _NOW + timedelta(hours=12)   # 12h after → within 24h
        assert should_revoke_on_dismissal(
            RegretBucket.SAME_NEXT_GATHER,
            contradiction_at,
            dismissed_at,
            p,
        )

    def test_revoke_does_not_fire_for_bucket_b(self) -> None:
        p = self._p()
        contradiction_at = _NOW
        dismissed_at = _NOW + timedelta(hours=6)
        assert not should_revoke_on_dismissal(
            RegretBucket.IN_TTL,
            contradiction_at,
            dismissed_at,
            p,
        )

    def test_revoke_does_not_fire_for_bucket_c(self) -> None:
        p = self._p()
        contradiction_at = _NOW
        dismissed_at = _NOW + timedelta(hours=6)
        assert not should_revoke_on_dismissal(
            RegretBucket.POST_TTL,
            contradiction_at,
            dismissed_at,
            p,
        )

    def test_revoke_does_not_fire_for_bucket_d(self) -> None:
        p = self._p()
        contradiction_at = _NOW
        dismissed_at = _NOW + timedelta(hours=6)
        assert not should_revoke_on_dismissal(
            RegretBucket.DISMISSED_7D,
            contradiction_at,
            dismissed_at,
            p,
        )

    def test_revoke_does_not_fire_for_bucket_a_beyond_24h(self) -> None:
        """Bucket (a) but dismissed 30h later → outside the 24h revoke window → no revoke."""
        p = self._p()
        contradiction_at = _NOW
        dismissed_at = _NOW + timedelta(hours=30)
        assert not should_revoke_on_dismissal(
            RegretBucket.SAME_NEXT_GATHER,
            contradiction_at,
            dismissed_at,
            p,
        )

    def test_revoke_fires_exactly_at_boundary(self) -> None:
        """Exactly at 24h boundary → fires (≤ not <)."""
        p = self._p()
        contradiction_at = _NOW
        dismissed_at = _NOW + timedelta(hours=24)
        assert should_revoke_on_dismissal(
            RegretBucket.SAME_NEXT_GATHER,
            contradiction_at,
            dismissed_at,
            p,
        )
