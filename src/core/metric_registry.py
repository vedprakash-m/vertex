from __future__ import annotations

from collections.abc import Collection
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Literal, TypeVar, cast

import yaml

from src.core.exceptions import ConfigError
from src.core.metric_models import MetricAggregation, MetricDefinition


REPO_ROOT = Path(__file__).resolve().parents[2]
METRICS_ROOT = REPO_ROOT / "knowledge" / "metrics"
MetricSloDirection = Literal["gte", "lte"]
MetricSensitivityLabel = Literal["public", "internal", "confidential", "secret"]
MetricFreshnessTier = Literal["hot", "warm", "cold"]
MetricZeroRowsPolicy = Literal["insufficient_data", "zero_value", "breach"]
MetricLiteral = TypeVar("MetricLiteral", bound=str)


def load_metric_definition_map(
    *,
    metrics_root: Path = METRICS_ROOT,
    metric_ids: Collection[str] | None = None,
    as_of: datetime | None = None,
) -> dict[str, MetricDefinition]:
    reference_time = as_of or datetime.now(timezone.utc)
    definitions = load_metric_definitions(metrics_root=metrics_root, metric_ids=metric_ids)
    selected: dict[str, MetricDefinition] = {}
    for definition in definitions:
        current = selected.get(definition.id)
        if current is None:
            selected[definition.id] = definition
            continue
        current_active = _is_definition_active(current, reference_time)
        candidate_active = _is_definition_active(definition, reference_time)
        if candidate_active and not current_active:
            selected[definition.id] = definition
            continue
        if candidate_active == current_active and definition.valid_from >= current.valid_from:
            selected[definition.id] = definition
    return selected


def load_metric_definitions(
    *,
    metrics_root: Path = METRICS_ROOT,
    metric_ids: Collection[str] | None = None,
) -> tuple[MetricDefinition, ...]:
    if not metrics_root.exists():
        return ()

    selected_ids = {metric_id.strip() for metric_id in metric_ids or () if metric_id.strip()} or None
    definitions: list[MetricDefinition] = []
    for path in sorted(metrics_root.glob("*.y*ml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        entries = _coerce_metric_entries(document, path)
        for entry in entries:
            definition = _parse_metric_definition(entry, path)
            if selected_ids is None or definition.id in selected_ids:
                definitions.append(definition)
    return tuple(sorted(definitions, key=lambda item: (item.id, item.valid_from, item.policy_version)))


def _coerce_metric_entries(document: Any, path: Path) -> list[dict[str, Any]]:
    if document is None:
        return []
    if isinstance(document, list):
        entries = document
    elif isinstance(document, dict):
        entries = document.get("metrics", [])
    else:
        raise ConfigError(f"Metric registry file must contain a list or metrics: list in {path}")
    if not isinstance(entries, list):
        raise ConfigError(f"Metric registry file must contain a list of metrics in {path}")
    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConfigError(f"Metric registry entries must be objects in {path}")
        normalized.append(entry)
    return normalized


def _parse_metric_definition(entry: dict[str, Any], path: Path) -> MetricDefinition:
    freshness_tier = cast(MetricFreshnessTier, str(entry.get("freshness_tier", "warm")).strip().lower())
    if freshness_tier not in {"hot", "warm", "cold"}:
        raise ConfigError(f"Unsupported freshness_tier '{freshness_tier}' in {path}")
    zero_rows_policy = cast(
        MetricZeroRowsPolicy,
        str(entry.get("zero_rows_policy", "insufficient_data")).strip().lower(),
    )
    if zero_rows_policy not in {"insufficient_data", "zero_value", "breach"}:
        raise ConfigError(f"Unsupported zero_rows_policy '{zero_rows_policy}' in {path}")
    sensitivity_label = cast(
        MetricSensitivityLabel,
        _require_literal(
            entry.get("sensitivity_label"),
            {"public", "internal", "confidential", "secret"},
            path,
            "sensitivity_label",
            default="internal",
        ),
    )

    return MetricDefinition(
        id=_required_string(entry.get("id"), path, "id"),
        title=_required_string(entry.get("title"), path, "title"),
        unit=_required_string(entry.get("unit"), path, "unit"),
        aggregation=MetricAggregation.from_string(str(entry.get("aggregation") or "")),
        dimension_columns=_string_tuple(entry.get("dimension_columns", []), path, "dimension_columns"),
        higher_is_better=bool(entry.get("higher_is_better", True)),
        comparable_with=_string_tuple(entry.get("comparable_with", []), path, "comparable_with"),
        owning_product_id=_optional_string(entry.get("owning_product_id")),
        service_tree_id=_optional_string(entry.get("service_tree_id")),
        slo_target=_optional_float(entry.get("slo_target"), path, "slo_target"),
        slo_direction=_optional_literal(
            entry.get("slo_direction"),
            {"gte", "lte"},
            path,
            "slo_direction",
            default=None,
        ),
        sensitivity_label=sensitivity_label,
        freshness_tier=freshness_tier,
        retention_days=_non_negative_int(entry.get("retention_days", 365), path, "retention_days"),
        expected_pipeline_lag_minutes=_non_negative_int(
            entry.get("expected_pipeline_lag_minutes", 0),
            path,
            "expected_pipeline_lag_minutes",
        ),
        zero_rows_policy=zero_rows_policy,
        max_rows_per_observation_batch=_non_negative_int(
            entry.get("max_rows_per_observation_batch", 10000),
            path,
            "max_rows_per_observation_batch",
        ),
        owner_alias=_optional_string(entry.get("owner_alias")),
        valid_from=_parse_datetime(entry.get("valid_from"), path, "valid_from") or datetime(1970, 1, 1, tzinfo=timezone.utc),
        valid_until=_parse_datetime(entry.get("valid_until"), path, "valid_until"),
        policy_version=_non_negative_int(entry.get("policy_version", 1), path, "policy_version"),
    )


def _is_definition_active(definition: MetricDefinition, as_of: datetime) -> bool:
    if definition.valid_from > as_of:
        return False
    if definition.valid_until is None:
        return True
    return definition.valid_until > as_of


def _required_string(value: Any, path: Path, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Metric registry field '{field_name}' must be a non-empty string in {path}")
    return value.strip()


def _optional_string(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _string_tuple(value: Any, path: Path, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"Metric registry field '{field_name}' must be a list in {path}")
    items: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise ConfigError(f"Metric registry field '{field_name}' entries must be non-empty strings in {path}")
        items.append(entry.strip())
    return tuple(items)


def _non_negative_int(value: Any, path: Path, field_name: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"Metric registry field '{field_name}' must be numeric in {path}")
    if not isinstance(value, int):
        raise ConfigError(f"Metric registry field '{field_name}' must be an integer in {path}")
    if value < 0:
        raise ConfigError(f"Metric registry field '{field_name}' must be non-negative in {path}")
    return value


def _optional_float(value: Any, path: Path, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"Metric registry field '{field_name}' must be numeric in {path}")
    return float(value)


def _optional_literal(
    value: Any,
    allowed: set[MetricLiteral],
    path: Path,
    field_name: str,
    *,
    default: MetricLiteral | None = None,
) -> MetricLiteral | None:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Metric registry field '{field_name}' must be a non-empty string in {path}")
    normalized = cast(MetricLiteral, value.strip().lower())
    if normalized not in allowed:
        raise ConfigError(
            f"Metric registry field '{field_name}' must be one of {sorted(allowed)} in {path}"
        )
    return normalized


def _require_literal(
    value: Any,
    allowed: set[MetricLiteral],
    path: Path,
    field_name: str,
    *,
    default: MetricLiteral,
) -> MetricLiteral:
    normalized = _optional_literal(value, allowed, path, field_name, default=default)
    if normalized is None:
        raise ConfigError(f"Metric registry field '{field_name}' must be set in {path}")
    return normalized


def _parse_datetime(value: Any, path: Path, field_name: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time(0, 0, 0), tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Metric registry field '{field_name}' must be an ISO timestamp in {path}")
    text = value.strip()
    try:
        if len(text) == 10:
            parsed_date = date.fromisoformat(text)
            return datetime.combine(parsed_date, time(0, 0, 0), tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigError(f"Metric registry field '{field_name}' must be an ISO timestamp in {path}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
