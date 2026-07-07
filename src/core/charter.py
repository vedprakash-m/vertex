from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.core.models import RiskLevel


@dataclass(frozen=True, slots=True)
class DimensionMaxRiskMetric:
    scorecard_name: str
    dimension_name: str
    max_risk: RiskLevel


@dataclass(frozen=True, slots=True)
class ItemCountMaxMetric:
    max_count: int
    states: tuple[str, ...] = ()
    work_item_types: tuple[str, ...] = ()
    area_path_prefixes: tuple[str, ...] = ()
    risk_levels: tuple[RiskLevel, ...] = ()
    tags: tuple[str, ...] = ()


CharterSuccessMetric = DimensionMaxRiskMetric | ItemCountMaxMetric


@dataclass(frozen=True, slots=True)
class CharterSuccessCriterion:
    text: str
    metric: CharterSuccessMetric | None = None
    evaluation_note: str | None = None
    is_structured: bool = False


def normalize_charter_text(value: object) -> str | None:
    if isinstance(value, str):
        normalized = " ".join(value.strip().split())
        return normalized or None
    if isinstance(value, dict):
        for key in ("text", "title"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                normalized = " ".join(candidate.strip().split())
                if normalized:
                    return normalized
    return None


def normalize_charter_values(raw_values: object) -> tuple[str, ...]:
    if not isinstance(raw_values, list):
        return ()
    normalized_values: list[str] = []
    for value in raw_values:
        normalized = normalize_charter_text(value)
        if normalized is not None:
            normalized_values.append(normalized)
    return tuple(normalized_values)


def parse_charter_success_criteria(raw_values: object) -> tuple[CharterSuccessCriterion, ...]:
    if not isinstance(raw_values, list):
        return ()

    parsed: list[CharterSuccessCriterion] = []
    for value in raw_values:
        if isinstance(value, str):
            normalized = normalize_charter_text(value)
            if normalized is not None:
                parsed.append(CharterSuccessCriterion(text=normalized))
            continue
        if not isinstance(value, dict):
            continue

        normalized_text = normalize_charter_text(value)
        if normalized_text is None:
            continue
        metric, evaluation_note = _parse_charter_success_metric(value.get("metric"))
        parsed.append(
            CharterSuccessCriterion(
                text=normalized_text,
                metric=metric,
                evaluation_note=evaluation_note,
                is_structured=True,
            )
        )
    return tuple(parsed)


def _parse_charter_success_metric(raw_metric: object) -> tuple[CharterSuccessMetric | None, str | None]:
    if raw_metric is None:
        return None, "No deterministic archive metric authored."
    if not isinstance(raw_metric, dict):
        return None, "Invalid deterministic archive metric definition."

    kind = _normalized_metric_token(raw_metric.get("kind"))
    if kind is None:
        return None, "Deterministic archive metric is missing kind."
    if kind == "dimension_max_risk":
        return _parse_dimension_max_risk_metric(raw_metric)
    if kind == "item_count_max":
        return _parse_item_count_max_metric(raw_metric)
    return None, f"Unsupported deterministic archive metric kind: {kind}."


def _parse_dimension_max_risk_metric(raw_metric: dict[str, Any]) -> tuple[DimensionMaxRiskMetric | None, str | None]:
    scorecard_name = normalize_charter_text(raw_metric.get("scorecard")) or normalize_charter_text(raw_metric.get("scorecard_name"))
    dimension_name = normalize_charter_text(raw_metric.get("dimension")) or normalize_charter_text(raw_metric.get("dimension_name"))
    max_risk_name = _normalized_metric_token(raw_metric.get("max_risk"))
    if scorecard_name is None or dimension_name is None or max_risk_name is None:
        return None, "dimension_max_risk metrics require scorecard/scorecard_name, dimension/dimension_name, and max_risk."
    try:
        max_risk = RiskLevel.from_string(max_risk_name)
    except ValueError:
        return None, f"Unsupported max_risk value: {max_risk_name}."
    return DimensionMaxRiskMetric(
        scorecard_name=scorecard_name,
        dimension_name=dimension_name,
        max_risk=max_risk,
    ), None


def _parse_item_count_max_metric(raw_metric: dict[str, Any]) -> tuple[ItemCountMaxMetric | None, str | None]:
    raw_max_count = raw_metric.get("max_count")
    if isinstance(raw_max_count, bool) or not isinstance(raw_max_count, int) or raw_max_count < 0:
        return None, "item_count_max metrics require a non-negative integer max_count."
    states = _parse_metric_tokens(raw_metric.get("states"))
    if states is None:
        return None, "item_count_max states must be a list of non-empty strings when provided."
    work_item_types = _parse_metric_tokens(raw_metric.get("work_item_types"))
    if work_item_types is None:
        return None, "item_count_max work_item_types must be a list of non-empty strings when provided."
    area_path_prefixes = _parse_metric_tokens(raw_metric.get("area_path_prefixes"))
    if area_path_prefixes is None:
        return None, "item_count_max area_path_prefixes must be a list of non-empty strings when provided."
    risk_levels = _parse_metric_risk_levels(raw_metric.get("risk_levels"))
    if risk_levels is None:
        return None, "item_count_max risk_levels must be a list of supported risk levels when provided."
    tags = _parse_metric_tokens(raw_metric.get("tags"))
    if tags is None:
        return None, "item_count_max tags must be a list of non-empty strings when provided."
    return ItemCountMaxMetric(
        max_count=raw_max_count,
        states=states,
        work_item_types=work_item_types,
        area_path_prefixes=area_path_prefixes,
        risk_levels=risk_levels,
        tags=tags,
    ), None


def _parse_metric_tokens(raw_values: object) -> tuple[str, ...] | None:
    if raw_values is None:
        return ()
    if not isinstance(raw_values, list):
        return None
    normalized_values: list[str] = []
    for raw_value in raw_values:
        normalized_value = _normalized_metric_token(raw_value)
        if normalized_value is not None:
            normalized_values.append(normalized_value)
    if raw_values and not normalized_values:
        return None
    return tuple(dict.fromkeys(normalized_values))


def _parse_metric_risk_levels(raw_risk_levels: object) -> tuple[RiskLevel, ...] | None:
    normalized_values = _parse_metric_tokens(raw_risk_levels)
    if normalized_values is None:
        return None

    parsed_levels: list[RiskLevel] = []
    for normalized_value in normalized_values:
        try:
            risk_level = RiskLevel.from_string(normalized_value)
        except ValueError:
            return None
        if risk_level not in parsed_levels:
            parsed_levels.append(risk_level)
    return tuple(parsed_levels)


def _normalized_metric_token(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.strip().lower().split())
    return normalized or None