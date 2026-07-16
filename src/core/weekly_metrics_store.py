"""ADF-W5.13 (specs/arch-data-fix.md Section 9.7): weekly telemetry
aggregate store, writer, and query path.

Section 9.7, verbatim: "Weekly aggregate measurements are schema-versioned
records at ``programs/<id>/runtime/metrics/weekly/<measurement-family>.jsonl``.
`ADF-W5.13` owns the rollup writer and query path. Before it lands, metrics
needing a 13-month window are explicitly unavailable; no command
extrapolates a long-term trend from raw retained rows."

This module is a generic rollup engine (one family-agnostic aggregator,
not N bespoke per-family writers) so it can roll up any JSONL-backed raw
measurement family (tier decisions, AI telemetry, run/channel telemetry)
that shares the same shape: one JSON object per line, an ISO-8601
timestamp field, and zero or more numeric fields worth averaging.

Zone A -- no AI or M365 imports.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.jsonl_utils import append_jsonl_line, read_jsonl_records

WEEKLY_METRICS_SCHEMA_VERSION = "1"

#: Section 9.7's own retention target for weekly aggregates.
DEFAULT_QUERY_WINDOW_WEEKS = 57  # ~13 months


@dataclass(frozen=True, slots=True)
class WeeklyAggregateRecord:
    schema_version: str
    program_id: str
    measurement_family: str
    iso_year: int
    iso_week: int
    week_start: date
    week_end: date
    record_count: int
    computed_at: datetime
    metrics: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "program_id": self.program_id,
            "measurement_family": self.measurement_family,
            "iso_year": self.iso_year,
            "iso_week": self.iso_week,
            "week_start": self.week_start.isoformat(),
            "week_end": self.week_end.isoformat(),
            "record_count": self.record_count,
            "computed_at": self.computed_at.astimezone(timezone.utc).isoformat(),
            "metrics": self.metrics,
        }

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "WeeklyAggregateRecord":
        return WeeklyAggregateRecord(
            schema_version=str(payload["schema_version"]),
            program_id=str(payload["program_id"]),
            measurement_family=str(payload["measurement_family"]),
            iso_year=int(payload["iso_year"]),
            iso_week=int(payload["iso_week"]),
            week_start=date.fromisoformat(payload["week_start"]),
            week_end=date.fromisoformat(payload["week_end"]),
            record_count=int(payload["record_count"]),
            computed_at=datetime.fromisoformat(payload["computed_at"]),
            metrics={str(k): float(v) for k, v in (payload.get("metrics") or {}).items()},
        )


def _weekly_metrics_path(program_id: str, measurement_family: str, *, programs_root: Path) -> Path:
    return programs_root / program_id / "runtime" / "metrics" / "weekly" / f"{measurement_family}.jsonl"


def _iso_week_bounds(iso_year: int, iso_week: int) -> tuple[date, date]:
    week_start = date.fromisocalendar(iso_year, iso_week, 1)  # Monday
    week_end = week_start + timedelta(days=6)  # Sunday
    return week_start, week_end


def compute_weekly_rollup(
    raw_records: list[dict[str, Any]],
    *,
    program_id: str,
    measurement_family: str,
    iso_year: int,
    iso_week: int,
    timestamp_field: str,
    numeric_fields: tuple[str, ...] = (),
    now: datetime | None = None,
) -> WeeklyAggregateRecord | None:
    """Filters ``raw_records`` to those whose ``timestamp_field`` falls in
    the given ISO week, and computes count + mean of each requested numeric
    field. Returns ``None`` (not a zero-record placeholder) when nothing
    falls in that week -- an absent week is a real fact, not a zero."""
    week_start, week_end = _iso_week_bounds(iso_year, iso_week)
    in_week: list[dict[str, Any]] = []
    for record in raw_records:
        raw_ts = record.get(timestamp_field)
        if not isinstance(raw_ts, str):
            continue
        try:
            parsed = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        record_date = parsed.date()
        if week_start <= record_date <= week_end:
            in_week.append(record)

    if not in_week:
        return None

    metrics: dict[str, float] = {}
    for field in numeric_fields:
        values = [float(r[field]) for r in in_week if isinstance(r.get(field), (int, float))]
        if values:
            metrics[f"{field}_mean"] = sum(values) / len(values)
            metrics[f"{field}_max"] = max(values)
            metrics[f"{field}_count"] = float(len(values))

    return WeeklyAggregateRecord(
        schema_version=WEEKLY_METRICS_SCHEMA_VERSION,
        program_id=program_id,
        measurement_family=measurement_family,
        iso_year=iso_year,
        iso_week=iso_week,
        week_start=week_start,
        week_end=week_end,
        record_count=len(in_week),
        computed_at=now or datetime.now(timezone.utc),
        metrics=metrics,
    )


def rollup_jsonl_family_for_week(
    source_path: Path,
    *,
    program_id: str,
    measurement_family: str,
    iso_year: int,
    iso_week: int,
    timestamp_field: str,
    numeric_fields: tuple[str, ...] = (),
    programs_root: Path = PROGRAMS_ROOT,
    now: datetime | None = None,
) -> WeeklyAggregateRecord | None:
    """Reads ``source_path`` (a raw JSONL measurement file), computes the
    rollup for one ISO week, and appends it to the family's weekly
    aggregate store. Returns the appended record, or ``None`` if the
    source has no rows in that week (nothing is appended)."""
    if not source_path.exists():
        return None
    raw_records = list(read_jsonl_records(source_path))
    record = compute_weekly_rollup(
        raw_records,
        program_id=program_id,
        measurement_family=measurement_family,
        iso_year=iso_year,
        iso_week=iso_week,
        timestamp_field=timestamp_field,
        numeric_fields=numeric_fields,
        now=now,
    )
    if record is None:
        return None
    output_path = _weekly_metrics_path(program_id, measurement_family, programs_root=programs_root)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    import json

    line = json.dumps(record.to_dict(), sort_keys=True) + "\n"
    append_jsonl_line(output_path, line)
    return record


def query_weekly_aggregates(
    program_id: str,
    measurement_family: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    since_weeks: int = DEFAULT_QUERY_WINDOW_WEEKS,
    now: datetime | None = None,
) -> tuple[WeeklyAggregateRecord, ...]:
    """The query path: the last ``since_weeks`` of weekly aggregates for
    one family, oldest first. Section 9.7's "13-month window" default."""
    path = _weekly_metrics_path(program_id, measurement_family, programs_root=programs_root)
    if not path.exists():
        return ()
    resolved_now = now or datetime.now(timezone.utc)
    cutoff = resolved_now.date() - timedelta(weeks=since_weeks)
    records = [WeeklyAggregateRecord.from_dict(raw) for raw in read_jsonl_records(path)]
    in_window = [record for record in records if record.week_end >= cutoff]
    return tuple(sorted(in_window, key=lambda record: (record.iso_year, record.iso_week)))


__all__ = [
    "DEFAULT_QUERY_WINDOW_WEEKS",
    "WEEKLY_METRICS_SCHEMA_VERSION",
    "WeeklyAggregateRecord",
    "compute_weekly_rollup",
    "query_weekly_aggregates",
    "rollup_jsonl_family_for_week",
]
