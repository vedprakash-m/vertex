from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from src.core.archive_store import read_scorecard_history
from src.core.models import RiskLevel
from src.core.snapshot_store import ARCHIVE_ROOT


@dataclass(frozen=True, slots=True)
class ScorecardTrend:
    current_risk: RiskLevel
    prior_risk: RiskLevel | None
    history: tuple[RiskLevel, ...]
    direction: Literal["improving", "stable", "worsening"]
    consecutive_high_count: int
    annotation: str | None


def load_scorecard_trends(
    edition: str,
    current_dimensions: dict[tuple[str, str], RiskLevel],
    *,
    archive_root: Path = ARCHIVE_ROOT,
    history_window: int = 4,
) -> dict[tuple[str, str], ScorecardTrend]:
    history_by_dimension: dict[tuple[str, str], list[tuple[int, int, RiskLevel]]] = {}
    for sequence, entry in enumerate(read_scorecard_history(edition, archive_root=archive_root)):
        scorecard_name = str(entry.get("scorecard_name") or "").strip()
        dimension_name = str(entry.get("dimension") or "").strip()
        raw_risk = str(entry.get("risk") or "").strip()
        if not scorecard_name or not dimension_name or not raw_risk:
            continue
        raw_issue_number = entry.get("issue_number")
        issue_number = _issue_number(raw_issue_number, sequence)
        history_by_dimension.setdefault((scorecard_name, dimension_name), []).append(
            (issue_number, sequence, RiskLevel.from_string(raw_risk))
        )

    trends: dict[tuple[str, str], ScorecardTrend] = {}
    for key, current_risk in current_dimensions.items():
        ordered_entries = tuple(
            sorted(history_by_dimension.get(key, ()), key=lambda item: (item[0], item[1]))
        )
        full_prior_history = tuple(risk for _, _, risk in ordered_entries)
        prior_risk = full_prior_history[-1] if full_prior_history else None
        visible_history = (*full_prior_history[-max(history_window - 1, 0):], current_risk)
        consecutive_high_count = _consecutive_high_count((*full_prior_history, current_risk))
        direction = _direction(current_risk, prior_risk)
        trends[key] = ScorecardTrend(
            current_risk=current_risk,
            prior_risk=prior_risk,
            history=visible_history,
            direction=direction,
            consecutive_high_count=consecutive_high_count,
            annotation=_annotation(current_risk, prior_risk, consecutive_high_count),
        )
    return trends


def _consecutive_high_count(history: tuple[RiskLevel, ...]) -> int:
    count = 0
    for risk in reversed(history):
        if risk != RiskLevel.HIGH:
            break
        count += 1
    return count


def _direction(
    current_risk: RiskLevel,
    prior_risk: RiskLevel | None,
) -> Literal["improving", "stable", "worsening"]:
    if prior_risk is None:
        return "stable"
    current_rank = _risk_rank(current_risk)
    prior_rank = _risk_rank(prior_risk)
    if current_rank < prior_rank:
        return "improving"
    if current_rank > prior_rank:
        return "worsening"
    return "stable"


def _annotation(
    current_risk: RiskLevel,
    prior_risk: RiskLevel | None,
    consecutive_high_count: int,
) -> str | None:
    if current_risk == RiskLevel.HIGH and consecutive_high_count >= 3:
        return f"High for {consecutive_high_count} consecutive issues."
    if (
        prior_risk is None
        or prior_risk == RiskLevel.UNKNOWN
        or current_risk == RiskLevel.UNKNOWN
        or prior_risk == current_risk
    ):
        return None
    if _risk_rank(current_risk) < _risk_rank(prior_risk):
        return f"Improved from {prior_risk.value.title()} to {current_risk.value.title()}."
    return f"Worsened from {prior_risk.value.title()} to {current_risk.value.title()}."


def _risk_rank(level: RiskLevel) -> int:
    return {
        RiskLevel.UNKNOWN: 0,
        RiskLevel.DONE: 1,
        RiskLevel.LOW: 2,
        RiskLevel.MEDIUM: 3,
        RiskLevel.HIGH: 4,
    }[level]


def _issue_number(raw_issue_number: object, fallback: int) -> int:
    if isinstance(raw_issue_number, int):
        return raw_issue_number
    if isinstance(raw_issue_number, str) and raw_issue_number.strip().isdigit():
        return int(raw_issue_number.strip())
    return fallback