"""ADF-W4.6 (Section 8.10.3): qualitative slip bands + calibrated rendering.

Section 8.10.3 requires slip output to use ``Low``/``Elevated``/``High`` bands
with named drivers *unless* statistically calibrated. When a forecast has
sufficient calibrated evidence, render a confidence interval/date band, the
probability of meeting the authored date, top drivers, calibration quality,
and the evidence window. When calibration is insufficient, render the
qualitative band and drivers only -- never an uncalibrated precise probability.

This module maps an existing :class:`~src.core.forecast_engine.ETAForecast`
plus a calibration-availability flag into the spec's two-mode rendering
contract. It does not modify the frozen ``ETAForecast`` (that would break its
many existing tests); instead it produces an additive
:class:`SlipBandAssessment` that downstream renderers consume.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from src.core.forecast_engine import ETAForecast, ForecastMethod
from src.core.models import Confidence


class SlipBand(str, Enum):
    """Section 8.10.3 qualitative slip band."""

    LOW = "Low"
    ELEVATED = "Elevated"
    HIGH = "High"


@dataclass(frozen=True, slots=True)
class SlipBandAssessment:
    """One milestone's slip assessment in the spec's two-mode rendering.

    ``calibrated`` is True only when a real calibration adjustment was applied
    to the forecast (``calibration_adjustments`` was non-None and non-zero for
    the item). When ``calibrated`` is False, ``slip_probability`` carries the
    raw heuristic value but renderers MUST show ``band`` + ``drivers`` only,
    never the precise probability (Section 8.10.3's prohibition).
    """

    work_item_id: int
    band: SlipBand
    calibrated: bool
    slip_probability: float | None
    drivers: tuple[str, ...]
    p50_date: date | None
    p80_date: date | None
    p95_date: date | None
    evidence_window_days: int | None
    calibration_quality: str | None  # "calibrated" | "heuristic" | "trajectory_only"

    @property
    def render_summary(self) -> str:
        """The spec-compliant one-line rendering for this assessment."""
        parts: list[str] = [f"{self.band.value} slip risk"]
        if self.drivers:
            parts.append("drivers: " + ", ".join(self.drivers))
        if self.calibrated and self.slip_probability is not None:
            parts.append(f"~{round(self.slip_probability * 100)}% miss probability (calibrated)")
            if self.p50_date is not None and self.p95_date is not None:
                parts.append(
                    f"forecast p50 {self.p50_date.strftime('%b %d')} – p95 {self.p95_date.strftime('%b %d')}"
                )
        return " | ".join(parts)


#: Slip-probability band thresholds (applied to the raw slip_probability).
#: Low: <0.33, Elevated: 0.33–0.66, High: >0.66.
_BAND_THRESHOLDS = (0.33, 0.66)


def classify_slip_band(slip_probability: float) -> SlipBand:
    """Map a raw slip probability to a qualitative band."""
    if slip_probability < _BAND_THRESHOLDS[0]:
        return SlipBand.LOW
    if slip_probability <= _BAND_THRESHOLDS[1]:
        return SlipBand.ELEVATED
    return SlipBand.HIGH


def _extract_drivers(forecast: ETAForecast) -> tuple[str, ...]:
    """Derive human-readable named drivers from a forecast's signals."""
    drivers: list[str] = []
    if forecast.prior_slips >= 2:
        drivers.append(f"{forecast.prior_slips} prior slips")
    elif forecast.prior_slips == 1:
        drivers.append("1 prior slip")
    if forecast.confidence in (Confidence.LOW, Confidence.NONE):
        drivers.append("low-confidence trajectory")
    if forecast.forecast_method is ForecastMethod.HEURISTIC:
        drivers.append("heuristic (no trajectory history)")
    return tuple(drivers) or ("on-track trajectory",)


def assess_slip_band(
    forecast: ETAForecast,
    *,
    calibrated: bool,
    evidence_window_days: int | None = None,
) -> SlipBandAssessment:
    """Map an :class:`ETAForecast` into the spec's two-mode rendering contract.

    ``calibrated`` is the caller's determination of whether a real calibration
    adjustment was applied (from ``calibration_engine``). When False, the
    returned assessment carries the raw probability for diagnostics but its
    ``render_summary`` deliberately omits it.
    """
    band = classify_slip_band(forecast.slip_probability)
    drivers = _extract_drivers(forecast)
    if calibrated:
        calibration_quality = "calibrated"
    elif forecast.forecast_method is ForecastMethod.TRAJECTORY_HISTORY:
        calibration_quality = "trajectory_only"
    else:
        calibration_quality = "heuristic"
    return SlipBandAssessment(
        work_item_id=forecast.work_item_id,
        band=band,
        calibrated=calibrated,
        slip_probability=forecast.slip_probability,
        drivers=drivers,
        p50_date=forecast.p50_date,
        p80_date=forecast.p80_date,
        p95_date=forecast.p95_date,
        evidence_window_days=evidence_window_days,
        calibration_quality=calibration_quality,
    )


def assess_forecasts(
    forecasts: dict[int, ETAForecast],
    *,
    calibrated_ids: frozenset[int] | None = None,
    evidence_window_days: int | None = None,
) -> dict[int, SlipBandAssessment]:
    """Assess a batch of forecasts. ``calibrated_ids`` marks which have real calibration."""
    calibrated_set = calibrated_ids or frozenset()
    return {
        wid: assess_slip_band(
            forecast,
            calibrated=wid in calibrated_set,
            evidence_window_days=evidence_window_days,
        )
        for wid, forecast in forecasts.items()
    }


__all__ = [
    "SlipBand",
    "SlipBandAssessment",
    "assess_forecasts",
    "assess_slip_band",
    "classify_slip_band",
]
