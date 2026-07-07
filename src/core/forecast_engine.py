from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from random import Random

from src.core.archive_store import get_dimension_history
from src.core.models import Confidence, DeltaKind, DeltaSet, EnumParserMixin, WorkItem
from src.core.models_v2 import TrajectoryPoint
from src.core.trajectory_analyzer import DriftPattern, count_eta_slips
from src.core.view_models import WorkstreamData
from src.core.work_item_states import TERMINAL_WORK_ITEM_STATES


_MIN_HISTORY_ENTRIES = 4
_DEFAULT_SLIP_THRESHOLD_DAYS = 5


@dataclass(frozen=True, slots=True)
class ForecastAssessment:
    section_id: str
    title: str
    confidence: Confidence
    current_eta: date
    predicted_eta: date
    slip_days: int
    formula: str
    source_item_ids: tuple[int, ...]

    @property
    def summary(self) -> str:
        return (
            f"Forecast: Current velocity suggests {self.title} may slip to "
            f"{self.predicted_eta.strftime('%b %d')} (current ETA {self.current_eta.strftime('%b %d')})."
        )

    @property
    def published_summary(self) -> str | None:
        if self.confidence == Confidence.LOW:
            return None
        return f"{self.summary} Forecast based on ADO velocity — confidence: {self.confidence.value}."

    @property
    def reviewer_summary(self) -> str:
        if self.confidence == Confidence.LOW:
            return f"Candidate forecast: {self.summary[10:]} Low confidence — not published."
        return self.summary


class ForecastMethod(EnumParserMixin, str, Enum):
    TRAJECTORY_HISTORY = "trajectory_history"
    MONTE_CARLO = "monte_carlo"
    HEURISTIC = "heuristic"


@dataclass(frozen=True, slots=True)
class ETAForecast:
    work_item_id: int
    ado_target_date: date | None
    predicted_target_date: date | None
    confidence: Confidence
    slip_probability: float
    reasoning: str
    prior_slips: int
    p50_date: date | None = None
    p80_date: date | None = None
    p95_date: date | None = None
    forecast_method: ForecastMethod = ForecastMethod.HEURISTIC

    @property
    def annotation(self) -> str | None:
        if self.ado_target_date is None or self.slip_probability <= 0.5:
            return None
        slip_label = "1 prior slip" if self.prior_slips == 1 else f"{self.prior_slips} prior slips"
        probability = round(self.slip_probability * 100)
        return f"{self.confidence.value} confidence — {slip_label}, {probability}% miss probability"

    @property
    def percentile_annotation(self) -> str | None:
        if self.p50_date is None or self.p80_date is None or self.p95_date is None:
            return None
        if self.ado_target_date is None:
            return None
        return (
            f"forecast p50 {self.p50_date.strftime('%b %d')}, "
            f"p80 {self.p80_date.strftime('%b %d')}, "
            f"p95 {self.p95_date.strftime('%b %d')}"
        )

    @property
    def display_annotation(self) -> str | None:
        parts: list[str] = []
        if self.annotation is not None:
            parts.append(self.annotation)
        if self.percentile_annotation is not None:
            parts.append(self.percentile_annotation)
        if not parts:
            return None
        return " | ".join(parts)


def forecast_etas(
    trajectories: dict[int, tuple[TrajectoryPoint, ...]],
    drift_patterns: tuple[DriftPattern, ...],
    *,
    calibration_adjustments: Mapping[int, float] | None = None,
    window_days: int = 90,
    as_of: date | None = None,
) -> dict[int, ETAForecast]:
    reference_date = as_of or datetime.now().date()
    pattern_map_lists: dict[int, list[DriftPattern]] = {}
    for pattern in drift_patterns:
        pattern_map_lists.setdefault(pattern.work_item_id, []).append(pattern)
    pattern_map: dict[int, tuple[DriftPattern, ...]] = {
        wid: tuple(pats) for wid, pats in pattern_map_lists.items()
    }

    forecasts: dict[int, ETAForecast] = {}
    for work_item_id, points in trajectories.items():
        ordered = tuple(sorted(points, key=lambda point: point.date))
        if not ordered:
            continue
        current = ordered[-1]
        if current.target_date is None:
            continue

        prior_slips = count_eta_slips(ordered, window_days=window_days, as_of=reference_date)
        base_probability = _base_slip_probability(current.target_date, reference_date)
        pattern_penalty = _pattern_penalty(pattern_map.get(work_item_id, ()))
        slip_probability, confidence = _trajectory_probability(prior_slips, base_probability, pattern_penalty)
        calibration_adjustment = 0.0 if calibration_adjustments is None else calibration_adjustments.get(work_item_id, 0.0)
        slip_probability = _apply_calibration_adjustment(slip_probability, calibration_adjustment)
        predicted_slip_days = _predicted_slip_days(prior_slips, slip_probability)
        predicted_target_date = current.target_date + timedelta(days=predicted_slip_days)
        forecast_method, p50_date, p80_date, p95_date = _forecast_percentiles(
            work_item_id=work_item_id,
            ordered_points=ordered,
            current_target_date=current.target_date,
            reference_date=reference_date,
            prior_slips=prior_slips,
            base_probability=base_probability,
            slip_probability=slip_probability,
            predicted_slip_days=predicted_slip_days,
        )
        slip_label = "1 prior slip" if prior_slips == 1 else f"{prior_slips} prior slips"
        forecasts[work_item_id] = ETAForecast(
            work_item_id=work_item_id,
            ado_target_date=current.target_date,
            predicted_target_date=predicted_target_date,
            confidence=confidence,
            slip_probability=slip_probability,
            reasoning=(
                f"{slip_label} in {window_days} days -> {round(slip_probability * 100)}% miss probability"
                if calibration_adjustment == 0.0
                else f"{slip_label} in {window_days} days -> {round(slip_probability * 100)}% miss probability (calibrated {calibration_adjustment:+.2f})"
            ),
            prior_slips=prior_slips,
            p50_date=p50_date,
            p80_date=p80_date,
            p95_date=p95_date,
            forecast_method=forecast_method,
        )
    return forecasts


def build_forecast_assessment(
    *,
    enabled: bool,
    edition_name: str,
    as_of: datetime,
    workstreams: tuple[WorkstreamData, ...],
    deltas: DeltaSet,
    archive_root: Path,
    slip_threshold_days: int = _DEFAULT_SLIP_THRESHOLD_DAYS,
) -> ForecastAssessment | None:
    if not enabled:
        return None

    candidates: list[ForecastAssessment] = []
    delta_lookup = _group_deltas_by_item(deltas)
    for workstream in workstreams:
        active_items = tuple(
            item
            for item in workstream.items
            if item.target_date is not None and item.state.strip().lower() not in TERMINAL_WORK_ITEM_STATES
        )
        if not active_items:
            continue
        history = get_dimension_history(edition_name, workstream.title, archive_root=archive_root, last_n=6)
        if len(history) < _MIN_HISTORY_ENTRIES:
            continue

        open_count = len(active_items)
        current_eta = max(item.target_date for item in active_items if item.target_date is not None)
        eta_churn = sum(
            1
            for item in active_items
            for delta in delta_lookup.get(item.id, ())
            if delta.kind == DeltaKind.ETA_CHANGED
        )
        closure_velocity = sum(
            1
            for item in active_items
            for delta in delta_lookup.get(item.id, ())
            if delta.kind == DeltaKind.CLOSED
        )
        remaining_days = sum((item.target_date - as_of.date()).days for item in active_items if item.target_date is not None) / open_count
        unblocked_ratio = max(0.0, (open_count - workstream.blocked_count) / open_count)
        slip_days = (
            (workstream.blocked_count * 2)
            + (eta_churn * 2)
            + (3 if closure_velocity == 0 and open_count >= 2 else 0)
            + (3 if closure_velocity == 0 and remaining_days <= 7 else 0)
            + (2 if unblocked_ratio < 0.6 else 0)
            + (2 if remaining_days <= 7 else 1 if remaining_days <= 14 else 0)
        )
        if slip_days < slip_threshold_days:
            continue

        confidence = _forecast_confidence(
            history_count=len(history),
            closure_velocity=closure_velocity,
            eta_churn=eta_churn,
            unblocked_ratio=unblocked_ratio,
        )
        candidates.append(
            ForecastAssessment(
                section_id=workstream.section_id,
                title=workstream.title,
                confidence=confidence,
                current_eta=current_eta,
                predicted_eta=current_eta + timedelta(days=slip_days),
                slip_days=slip_days,
                formula=(
                    f"slip={slip_days}d from remaining_days={remaining_days:.1f}, closure_velocity={closure_velocity}, "
                    f"eta_churn={eta_churn}, unblocked_ratio={unblocked_ratio:.0%}"
                ),
                source_item_ids=tuple(item.id for item in active_items[:3]),
            )
        )

    if not candidates:
        return None
    return max(candidates, key=lambda candidate: (candidate.slip_days, candidate.confidence.value))


def _forecast_confidence(
    *,
    history_count: int,
    closure_velocity: int,
    eta_churn: int,
    unblocked_ratio: float,
) -> Confidence:
    if history_count >= 6 and closure_velocity > 0 and eta_churn <= 1 and unblocked_ratio >= 0.75:
        return Confidence.HIGH
    if history_count >= _MIN_HISTORY_ENTRIES:
        return Confidence.MEDIUM
    return Confidence.LOW


def _group_deltas_by_item(deltas: DeltaSet) -> dict[int, tuple]:
    grouped: dict[int, list] = {}
    for delta in [*deltas.risk_changes, *deltas.new_items, *deltas.eta_changes, *deltas.closed_items]:
        grouped.setdefault(delta.work_item_id, []).append(delta)
    return {work_item_id: tuple(entries) for work_item_id, entries in grouped.items()}


def _base_slip_probability(target_date: date, as_of: date) -> float:
    days_to_target = (target_date - as_of).days
    if days_to_target <= 0:
        return 0.9
    if days_to_target <= 7:
        return 0.5
    if days_to_target <= 14:
        return 0.35
    if days_to_target <= 30:
        return 0.25
    return 0.15


def _pattern_penalty(patterns: tuple[DriftPattern, ...]) -> float:
    penalty = 0.0
    for pattern in patterns:
        if pattern.pattern == "stale":
            penalty += 0.1
        elif pattern.pattern in {"chronic_reassign", "state_oscillation"}:
            penalty += 0.05
    return min(0.2, penalty)


def _trajectory_probability(
    prior_slips: int,
    base_probability: float,
    pattern_penalty: float,
) -> tuple[float, Confidence]:
    if prior_slips <= 0:
        return (min(1.0, base_probability + pattern_penalty), Confidence.HIGH)
    if prior_slips == 1:
        return (min(1.0, base_probability + 0.2 + pattern_penalty), Confidence.MEDIUM)
    if prior_slips == 2:
        return (min(1.0, base_probability + 0.4 + pattern_penalty), Confidence.LOW)
    return (max(0.8, min(1.0, base_probability + pattern_penalty)), Confidence.LOW)


def _predicted_slip_days(prior_slips: int, slip_probability: float) -> int:
    if prior_slips <= 0:
        return 0
    return max(prior_slips * 2, round(slip_probability * 7))


def _apply_calibration_adjustment(slip_probability: float, adjustment: float) -> float:
    return min(0.95, max(0.0, round(slip_probability + adjustment, 2)))


def _forecast_percentiles(
    *,
    work_item_id: int,
    ordered_points: tuple[TrajectoryPoint, ...],
    current_target_date: date,
    reference_date: date,
    prior_slips: int,
    base_probability: float,
    slip_probability: float,
    predicted_slip_days: int,
) -> tuple[ForecastMethod, date | None, date | None, date | None]:
    target_points = tuple(point for point in ordered_points if point.target_date is not None)
    if len(target_points) <= 2:
        return (
            ForecastMethod.HEURISTIC,
            *_heuristic_percentiles(
                current_target_date=current_target_date,
                point_count=len(target_points),
                prior_slips=prior_slips,
                slip_probability=slip_probability,
                predicted_slip_days=predicted_slip_days,
            ),
        )
    if len(target_points) == 3:
        return (
            ForecastMethod.TRAJECTORY_HISTORY,
            *_trajectory_history_percentiles(
                target_points=target_points,
                current_target_date=current_target_date,
                reference_date=reference_date,
                predicted_slip_days=predicted_slip_days,
            ),
        )
    return (
        ForecastMethod.MONTE_CARLO,
        *_monte_carlo_percentiles(
            work_item_id=work_item_id,
            target_points=target_points,
            current_target_date=current_target_date,
            base_probability=base_probability,
            slip_probability=slip_probability,
            predicted_slip_days=predicted_slip_days,
        ),
    )


def _heuristic_percentiles(
    *,
    current_target_date: date,
    point_count: int,
    prior_slips: int,
    slip_probability: float,
    predicted_slip_days: int,
) -> tuple[date, date, date]:
    if point_count <= 1:
        return _percentile_dates(current_target_date, 0, max(14, predicted_slip_days), max(30, predicted_slip_days + 16))

    p50_days = max(0, round(prior_slips * 0.7))
    uncertainty_days = max(7, round(max(slip_probability, 0.25) * 14))
    p80_days = max(p50_days, predicted_slip_days, p50_days + uncertainty_days)
    p95_days = max(p80_days + uncertainty_days, p80_days + 7)
    return _percentile_dates(current_target_date, p50_days, p80_days, p95_days)


def _trajectory_history_percentiles(
    *,
    target_points: tuple[TrajectoryPoint, ...],
    current_target_date: date,
    reference_date: date,
    predicted_slip_days: int,
) -> tuple[date, date, date]:
    positive_shifts = _positive_target_shifts(target_points[-3:])
    if not positive_shifts:
        return _percentile_dates(current_target_date, 0, 0, 0)

    recent_points = target_points[-3:]
    recent_first_target = recent_points[0].target_date
    recent_last_target = recent_points[-1].target_date
    if recent_first_target is None or recent_last_target is None:
        return _percentile_dates(current_target_date, 0, 0, 0)
    history_span_days = max(1, (recent_points[-1].date - recent_points[0].date).days)
    remaining_days = max(1, (current_target_date - reference_date).days)
    total_shift_days = max(0, (recent_last_target - recent_first_target).days)
    weighted_shift_days = _weighted_average(positive_shifts)
    slip_velocity = total_shift_days / history_span_days
    projected_days = max(
        round(weighted_shift_days),
        round(slip_velocity * min(remaining_days, 14)),
    )
    p50_days = max(0, projected_days)
    p80_days = max(p50_days, predicted_slip_days, p50_days + max(2, round(weighted_shift_days * 0.5)))
    p95_days = max(p80_days, p80_days + max(3, max(positive_shifts)))
    return _percentile_dates(current_target_date, p50_days, p80_days, p95_days)


def _monte_carlo_percentiles(
    *,
    work_item_id: int,
    target_points: tuple[TrajectoryPoint, ...],
    current_target_date: date,
    base_probability: float,
    slip_probability: float,
    predicted_slip_days: int,
) -> tuple[date, date, date]:
    positive_shifts = _positive_target_shifts(target_points)
    if not positive_shifts:
        return _percentile_dates(current_target_date, 0, 0, 0)

    rng = Random(_forecast_seed(work_item_id, target_points))
    event_probability = min(
        0.85,
        max(base_probability, slip_probability * 0.75, len(positive_shifts) / max(1, len(target_points) - 1)),
    )
    simulated_slips: list[int] = []
    for _ in range(1000):
        total_slip_days = 0
        step_probability = event_probability
        for _ in range(3):
            if rng.random() >= step_probability:
                step_probability *= 0.4
                continue
            total_slip_days += positive_shifts[rng.randrange(len(positive_shifts))]
            step_probability *= 0.6
        simulated_slips.append(total_slip_days)

    simulated_slips.sort()
    p50_days = _percentile_value(simulated_slips, 0.50)
    p80_days = max(_percentile_value(simulated_slips, 0.80), predicted_slip_days)
    p95_days = max(_percentile_value(simulated_slips, 0.95), p80_days)
    return _percentile_dates(current_target_date, p50_days, p80_days, p95_days)


def _positive_target_shifts(points: tuple[TrajectoryPoint, ...]) -> tuple[int, ...]:
    shifts: list[int] = []
    prior_target: date | None = None
    for point in points:
        if point.target_date is None:
            continue
        if prior_target is not None:
            shift_days = (point.target_date - prior_target).days
            if shift_days > 0:
                shifts.append(shift_days)
        prior_target = point.target_date
    return tuple(shifts)


def _weighted_average(values: tuple[int, ...]) -> float:
    if not values:
        return 0.0
    weights = tuple(range(1, len(values) + 1))
    return sum(value * weight for value, weight in zip(values, weights)) / sum(weights)


def _percentile_value(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    index = int(round((len(values) - 1) * percentile))
    index = max(0, min(len(values) - 1, index))
    return values[index]


def _percentile_dates(
    current_target_date: date,
    p50_days: int,
    p80_days: int,
    p95_days: int,
) -> tuple[date, date, date]:
    normalized_p50 = max(0, p50_days)
    normalized_p80 = max(normalized_p50, p80_days)
    normalized_p95 = max(normalized_p80, p95_days)
    return (
        current_target_date + timedelta(days=normalized_p50),
        current_target_date + timedelta(days=normalized_p80),
        current_target_date + timedelta(days=normalized_p95),
    )


def _forecast_seed(work_item_id: int, target_points: tuple[TrajectoryPoint, ...]) -> int:
    seed = work_item_id & 0xFFFFFFFF
    for index, point in enumerate(target_points, start=1):
        if point.target_date is None:
            continue
        seed = (
            (seed * 1103515245)
            + (point.date.toordinal() * 97)
            + (point.target_date.toordinal() * 53)
            + index
        ) & 0xFFFFFFFF
    return seed