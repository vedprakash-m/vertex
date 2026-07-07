from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Literal

from src.core.models import EnumParserMixin

if TYPE_CHECKING:
    from src.core.source_models import SourceRef


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MetricAggregation(EnumParserMixin, str, Enum):
    LAST = "last"
    AVG = "avg"
    P50 = "p50"
    P95 = "p95"
    P99 = "p99"
    SUM = "sum"
    COUNT = "count"
    COUNT_DISTINCT = "count_distinct"
    MAX = "max"
    MIN = "min"


class MetricQualityState(EnumParserMixin, str, Enum):
    OK = "ok"
    STALE_SOURCE = "stale_source"
    QUERY_FAILED = "query_failed"
    ZERO_ROWS = "zero_rows"
    PARTIAL = "partial"
    LATE_CORRECTED = "late_corrected"
    MANUAL = "manual"
    FALLBACK = "fallback"  # FR-SG-31: primary query failed; secondary fallback query succeeded


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    id: str
    title: str
    unit: str
    aggregation: MetricAggregation
    dimension_columns: tuple[str, ...] = ()
    higher_is_better: bool = True
    comparable_with: tuple[str, ...] = ()
    owning_product_id: str | None = None
    service_tree_id: str | None = None
    slo_target: float | None = None
    slo_direction: Literal["gte", "lte"] | None = None
    sensitivity_label: Literal["public", "internal", "confidential", "secret"] = "internal"
    freshness_tier: Literal["hot", "warm", "cold"] = "warm"
    retention_days: int = 365
    expected_pipeline_lag_minutes: int = 0
    zero_rows_policy: Literal["insufficient_data", "zero_value", "breach"] = "insufficient_data"
    max_rows_per_observation_batch: int = 10000
    owner_alias: str | None = None
    valid_from: datetime = field(default_factory=_utc_now)
    valid_until: datetime | None = None
    policy_version: int = 1


@dataclass(frozen=True, slots=True)
class ObservationWindow:
    days: int
    aggregation: MetricAggregation
    dimensions: tuple[tuple[str, str], ...] = ()
    window_kind: Literal["trailing", "anchored_after"] = "trailing"
    anchor_event_ref: SourceRef | None = None
    minimum_observations: int = 1


@dataclass(frozen=True, slots=True)
class MetricSourceBinding:
    binding_id: str
    metric_id: str
    program_id: str
    source_kind: Literal["kusto", "wiql"]
    priority: int = 0
    cluster: str | None = None
    database: str | None = None
    kql_template: str | None = None
    result_column: str | None = None
    dimension_defaults: tuple[tuple[str, str], ...] = ()
    validated: bool = False
    last_validated_at: datetime | None = None
    last_validated_kql_hash: str | None = None
    owner_alias: str | None = None
    owner_entity_ref: str | None = None  # WI-2.5: canonical entity ID resolved from owner_alias
    binding_version: int = 1
    kql_template_hash: str | None = None
    evidence_url_template: str | None = None
    valid_from: datetime = field(default_factory=_utc_now)
    valid_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class MetricObservation:
    observation_id: str
    program_id: str
    metric_id: str
    dimensions_json: str
    measurement_period_start: datetime
    measurement_period_end: datetime
    observed_at: datetime
    value_num: float | None
    value_text: str | None
    sample_count: int | None
    quality_state: MetricQualityState = MetricQualityState.OK
    source_binding_id: str | None = None
    binding_version: int | None = None
    ingestion_run_id: str | None = None
    corrected_at: datetime | None = None
    corrected_reason: str | None = None
    inserted_at: datetime = field(default_factory=_utc_now)
    is_pinned: bool = False
    pinned_at: datetime | None = None
    pin_reason: str | None = None