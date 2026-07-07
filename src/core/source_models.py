from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Literal

from src.core.models import EnumParserMixin

if TYPE_CHECKING:
    from src.core.models_v2 import AssumptionStatus


class SourceKind(EnumParserMixin, str, Enum):
    ADO_WORK_ITEM = "ado_wi"
    ADO_REVISION = "ado_rev"
    KUSTO_OBS = "kusto_obs"
    KPI_QUERY = "kpi_query"
    CLAIM = "claim"
    ASSUMPTION = "assumption"
    MILESTONE = "milestone"
    DOCUMENT = "document"
    SIGNAL = "signal"
    PM_CONFIRM = "pm_confirm"
    INCIDENT = "incident"
    GITHUB_PR = "github_pr"
    GITHUB_RELEASE = "github_release"


@dataclass(frozen=True, slots=True)
class SourceRef:
    kind: SourceKind
    ref: str
    captured_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class IngestionRun:
    id: str
    program_id: str
    source_kind: str
    source_ref: str
    binding_id: str | None
    started_at: datetime
    heartbeat_at: datetime | None
    completed_at: datetime | None
    status: Literal["running", "success", "partial", "failed"]
    expected_rows: int | None = None
    metrics_observed: int = 0
    signals_written: int = 0
    query_hash: str | None = None
    captured_window: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class MetricBindingHealth:
    program_id: str
    binding_id: str
    metric_id: str
    last_success_at: datetime | None
    last_attempt_at: datetime | None
    last_successful_observation_at: datetime | None
    last_failure_at: datetime | None
    consecutive_failures: int = 0
    last_error_class: str | None = None
    last_validation_error: str | None = None
    is_degraded: bool = False
    degraded_since: datetime | None = None
    watermark: datetime | None = None


@dataclass(frozen=True, slots=True)
class MaintenanceWindow:
    id: str
    program_id: str
    title: str
    starts_at: datetime
    ends_at: datetime
    scope_kind: Literal["program", "metric", "binding", "workstream"]
    scope_value: str
    suppress_kinds: tuple[
        Literal["threshold_breach", "staleness", "data_loss", "source_degraded"],
        ...,
    ] = ("threshold_breach", "staleness", "data_loss", "source_degraded")
    created_by: str = ""
    created_at: datetime | None = None
    reference: str | None = None


@dataclass(frozen=True, slots=True)
class AssumptionEvent:
    id: str
    assumption_id: str
    program_id: str
    event_type: Literal["created", "confirmed", "invalidated", "re_confirmed"]
    from_status: AssumptionStatus | None
    to_status: AssumptionStatus
    occurred_at: datetime
    actor: str
    note: str | None = None
    validating_signal_ids: tuple[str, ...] = ()
    validating_observation_ids: tuple[str, ...] = ()
    record_type: Literal["assumption_event"] = "assumption_event"