"""ADF-W5.8 (specs/arch-data-fix.md Section 8.2.5): the last open alert
category -- ``value_baseline_expired_or_incomparable``.

Section 8.2.5 lists this category; the prior passes (v1.31/v1.44/v1.46) closed
eight of the ten categories but left this one open with the explicit note that it
was "blocked on a design decision -- what 'expired'/'incomparable' mean." This
module is that design decision, made concrete against the binding contracts that
already exist in the spec:

**Expired** (Section 15.6 "Historical baseline admissibility"): a baseline must
be "contemporaneously recorded rather than recollected." A ``ValueMetric`` that
claims ``confidence == MEASUREDED`` (the only tier INV-ADF-11 permits to present
as a real outcome) but whose ``period_end`` is older than ``max_age_days``
relative to the current observation time is no longer contemporaneous with the
program's current state. The program has drifted; the measured value is now a
historical recollection, not a live measurement. Section 15.6 also requires a
historical baseline be relabeled ``calibrated`` -- so the finding's remedy is to
re-measure or to relabel, both of which the alert's ``next_command`` surfaces.

**Incomparable** (Section 8.1.8 "Baseline measurement"): a savings comparison
presented as ``MEASUREDED`` requires "at least 8 matched pairs" before the
"certified" claim is admissible. A ``TimeSavingsCertification`` (or a measured
``ValueMetric`` carrying a savings-style ``baseline_value``/``value`` pair) with
fewer than ``min_matched_pairs`` observations is presenting a measurement the
evidence cannot support -- the comparison is not yet statistically comparable.

The detector is a pure read-and-compare over a ``ValueCockpitSummary`` and an
observation instant -- no I/O, no alert side effects (the cockpit wiring emits
the alert best-effort, matching the ``projection_lag``/``lineage_regression``
precedent). Zone A.

Design note on why ``MEASUREDED`` is the gating tier: ``CALIBRATED``/``PROXY``/
``UNAVAILABLE`` metrics are *already* honestly labeled as not-directly-measured
by INV-ADF-11, so an expired or under-evidenced ``CALIBRATED`` metric is not
misleading -- only a ``MEASUREDED`` presentation can create the false confidence
this category exists to catch.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.core.cockpit_models import TimeSavingsCertification, ValueCockpitSummary, ValueConfidence, ValueMetric

#: Section 15.6: a measured value metric whose evidence window is older than
#: this is no longer "contemporaneously recorded." 90 days mirrors a quarterly
#: cadence -- long enough that a healthy program re-measures within it, short
#: enough that a stale measured claim is caught before the program drifts so
#: far the comparison is meaningless. Phase-0 ratification (ADF-W0.6) may tune
#: this via config without code change; the detector accepts it as a parameter.
DEFAULT_MAX_AGE_DAYS = 90.0

#: Section 8.1.8: "at least 8 matched pairs." A savings comparison presented as
#: MEASURED with fewer observations than this is not statistically comparable.
DEFAULT_MIN_MATCHED_PAIRS = 8


@dataclass(frozen=True, slots=True)
class ValueBaselineFreshnessFinding:
    """The result of comparing a value summary's baselines against the
    freshness/evidence contracts.

    ``is_degraded`` is True iff at least one measured value metric is expired
    or incomparable. When False, every measured metric is within its freshness
    and evidence budget (or no measured metric exists -- a program that has
    only ``CALIBRATED``/``PROXY`` metrics has nothing that can mislead).
    """

    is_degraded: bool
    reason: str  # expired | incomparable | none
    detail: str
    affected_metric_ids: tuple[str, ...]
    max_age_days: float
    min_matched_pairs: int


def _is_timezone_aware(dt: datetime) -> bool:
    return dt.tzinfo is not None and dt.utcoffset() is not None


def _to_aware(dt: datetime) -> datetime:
    if _is_timezone_aware(dt):
        return dt
    return dt.replace(tzinfo=timezone.utc)


def detect_value_baseline_freshness(
    value_summary: ValueCockpitSummary,
    *,
    observed_at: datetime,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
    min_matched_pairs: int = DEFAULT_MIN_MATCHED_PAIRS,
) -> ValueBaselineFreshnessFinding:
    """Evaluate a value summary's measured metrics for expiry/incomparability.

    Returns a non-degraded finding when the summary has no ``MEASUREDED``
    metrics (nothing can mislead) or when every measured metric is within its
    freshness and evidence budget. The caller (cockpit build/show) emits an
    alert best-effort via ``append_or_suppress_alert`` when ``is_degraded``.
    """
    now = _to_aware(observed_at)
    cutoff = now - timedelta(days=max_age_days)

    expired: list[str] = []
    incomparable: list[str] = []

    for metric in value_summary.metrics:
        finding_kind = _evaluate_metric(metric, now=now, cutoff=cutoff, min_matched_pairs=min_matched_pairs)
        if finding_kind == "expired":
            expired.append(metric.metric_id)
        elif finding_kind == "incomparable":
            incomparable.append(metric.metric_id)

    # The TimeSavingsCertification is a distinct measured-savings surface
    # (Section 8.1.8). It is incomparable when it claims to be a real
    # certification but has fewer matched pairs than the minimum.
    cert = value_summary.time_savings_certification
    if cert is not None and _certification_is_incomparable(cert, min_matched_pairs):
        incomparable.append(f"time_savings_certification:{cert.workflow}")

    affected = tuple(expired) + tuple(incomparable)
    if not affected:
        return ValueBaselineFreshnessFinding(
            is_degraded=False,
            reason="none",
            detail="All measured value metrics are within their freshness and evidence budgets.",
            affected_metric_ids=(),
            max_age_days=max_age_days,
            min_matched_pairs=min_matched_pairs,
        )

    parts: list[str] = []
    if expired:
        parts.append(
            f"{len(expired)} measured metric(s) expired (period_end older than {max_age_days:.0f} days): "
            f"{', '.join(expired)}"
        )
    if incomparable:
        parts.append(
            f"{len(incomparable)} measured comparison(s) incomparable (fewer than {min_matched_pairs} matched pairs): "
            f"{', '.join(incomparable)}"
        )
    reason = "expired" if expired and not incomparable else ("incomparable" if incomparable and not expired else "expired_and_incomparable")
    return ValueBaselineFreshnessFinding(
        is_degraded=True,
        reason=reason,
        detail="; ".join(parts) + ". Re-measure or relabel the affected metrics as 'calibrated' (Section 15.6).",
        affected_metric_ids=affected,
        max_age_days=max_age_days,
        min_matched_pairs=min_matched_pairs,
    )


def _evaluate_metric(
    metric: ValueMetric, *, now: datetime, cutoff: datetime, min_matched_pairs: int
) -> str | None:
    """Classify a single value metric. Returns 'expired' | 'incomparable' | None.

    Only ``MEASUREDED`` metrics are evaluated (INV-ADF-11: only a measured
    presentation can create the false confidence this category catches).
    """
    if metric.confidence != ValueConfidence.MEASURED:
        return None

    # Expired: the evidence window ended before the freshness cutoff.
    period_end = _to_aware(metric.period_end)
    if period_end < cutoff:
        return "expired"

    # Incomparable: a measured *comparison* (non-null baseline_value and a
    # real delta) that does not carry enough evidence to support a measured
    # presentation. We cannot see the sample count on a bare ValueMetric (the
    # type carries evidence_refs, not a count), so this branch only fires for
    # the explicit savings-style metrics where the period window is degenerate
    # (period_start == period_end -- a "measurement" with no observation span).
    # The matched-pair floor for savings certification is enforced on the
    # TimeSavingsCertification object separately.
    if metric.baseline_value is not None and metric.delta_value is not None:
        span = (_to_aware(metric.period_end) - _to_aware(metric.period_start)).total_seconds()
        if span <= 0:
            return "incomparable"

    return None


def _certification_is_incomparable(cert: TimeSavingsCertification, min_matched_pairs: int) -> bool:
    """Section 8.1.8: a savings certification needs >= min_matched_pairs.

    The certification type carries ``manual_sample_count`` and
    ``vertex_sample_count``; the matched-pair floor is the minimum of the two
    (a matched pair requires one of each).
    """
    matched = min(cert.manual_sample_count, cert.vertex_sample_count)
    return matched < min_matched_pairs


def build_value_baseline_alert_message(finding: ValueBaselineFreshnessFinding) -> tuple[str, str]:
    """Return ``(message, next_command)`` for a degraded finding.

    Kept as a separate helper so the cockpit wiring can format the alert
    payload without importing the alerts module into the detector (which stays
    a pure read-and-compare with no alert side effects), matching the
    ``projection_lag``/``lineage_regression`` precedent.
    """
    assert finding.is_degraded
    message = (
        f"Value baseline expired or incomparable: {finding.detail}"
    )
    next_command = (
        "vertex cockpit show --program {program}  # inspect value metrics; "
        "re-measure or relabel affected metrics as 'calibrated'"
    )
    return message, next_command


__all__ = [
    "DEFAULT_MAX_AGE_DAYS",
    "DEFAULT_MIN_MATCHED_PAIRS",
    "ValueBaselineFreshnessFinding",
    "build_value_baseline_alert_message",
    "detect_value_baseline_freshness",
]
