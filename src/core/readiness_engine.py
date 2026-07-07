from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml

from src.core.exceptions import ConfigError
from src.core.incident_journal_store import read_incident_entries
from src.core.journal import PROGRAMS_ROOT
from src.core.models import RiskLevel
from src.core.models_v2 import Dependency, DependencyScheduleStatus, DependencyStatus, IncidentEntry, RiskEntry, RiskStatus
from src.core.program_fact_store import load_program_facts, project_dependencies, project_risk_entries
from src.core.program_paths import get_readiness_snapshot_path, resolve_readiness_snapshot_path_for_read
from src.core.risk_register_engine import compute_risk_score


DEFAULT_READINESS_DIMENSIONS: dict[str, tuple[str, str]] = {
    "slo_definition_complete": ("SLO definition complete", "QG-RD1"),
    "dependency_health": ("Dependency health", "QG-RD2"),
    "observability_coverage": ("Observability coverage", "QG-RD3"),
    "rollback_plan": ("Rollback plan", "QG-RD4"),
    "capacity_validation": ("Capacity validation", "QG-RD5"),
    "incident_response_owner": ("Incident response owner", "QG-RD6"),
    "support_handoff_complete": ("Support handoff complete", "QG-RD7"),
    "dora_change_fail_rate": ("DORA change fail rate", "QG-RD8"),
}
DEFAULT_DIMENSION_ORDER = tuple(DEFAULT_READINESS_DIMENSIONS)
_STATE_PASS_CONDITION_KINDS = {"all_work_items_in_states", "any_work_item_in_states"}
_NUMERIC_OPERATORS = {">", ">=", "<", "<=", "==", "!="}
_HIGH_RISK_DEPENDENCY_SCHEDULES = {
    DependencyScheduleStatus.AT_RISK,
    DependencyScheduleStatus.SLIPPED,
    DependencyScheduleStatus.BLOCKED,
}
_ACTIVE_DEPENDENCY_STATUSES = {DependencyStatus.ACTIVE, DependencyStatus.BROKEN}
_ACTIVE_RISK_STATUSES = {RiskStatus.OPEN, RiskStatus.ESCALATED}
_RISK_LEVEL_RANK = {
    RiskLevel.DONE: 0,
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
    RiskLevel.UNKNOWN: 4,
}


@dataclass(frozen=True, slots=True)
class ReadinessSourceConfig:
    type: str
    query_id: str | None = None
    alias: str | None = None
    attested_at: date | None = None
    attested_by: str | None = None
    notes: str | None = None
    workstream_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReadinessPassCondition:
    kind: str
    allowed_states: tuple[str, ...] = ()
    operator: str | None = None
    threshold: float | None = None
    days: int | None = None
    result_column: str | None = None
    risk_level: str | None = None


@dataclass(frozen=True, slots=True)
class ReadinessDimensionConfig:
    id: str
    name: str
    gate_id: str
    source: ReadinessSourceConfig
    pass_condition: ReadinessPassCondition


@dataclass(frozen=True, slots=True)
class ReadinessConfig:
    program_id: str
    snapshot_max_age_days: int
    dimensions: tuple[ReadinessDimensionConfig, ...]


@dataclass(frozen=True, slots=True)
class ReadinessDimensionResult:
    id: str
    name: str
    gate_id: str
    source_type: str
    passed: bool
    status: str
    summary: str
    observed_value: str
    threshold: str
    evidence_refs: tuple[str, ...] = ()
    details: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ReadinessSnapshot:
    program_id: str
    fetched_at: datetime
    snapshot_max_age_days: int
    content_sha256: str
    dimensions: tuple[ReadinessDimensionResult, ...]

    @property
    def passed_count(self) -> int:
        return sum(1 for dimension in self.dimensions if dimension.passed)

    @property
    def total_count(self) -> int:
        return len(self.dimensions)


@dataclass(frozen=True, slots=True)
class ReadinessSnapshotLoadResult:
    snapshot: ReadinessSnapshot | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReadinessFetchLoaders:
    load_ado_query_rows: Callable[[ReadinessDimensionConfig], list[dict[str, Any]]] | None = None
    load_kusto_query_rows: Callable[[ReadinessDimensionConfig], list[dict[str, Any]]] | None = None
    alias_exists: Callable[[str], bool] | None = None
    load_dependencies: Callable[[], tuple[Dependency, ...]] | None = None
    load_risk_entries: Callable[[], tuple[RiskEntry, ...]] | None = None
    load_incident_entries: Callable[[ReadinessDimensionConfig, datetime], tuple[IncidentEntry, ...]] | None = None


def get_readiness_config_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "readiness.yaml"


def load_readiness_config(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> ReadinessConfig:
    path = get_readiness_config_path(program_id, programs_root=programs_root)
    if not path.exists():
        raise FileNotFoundError(f"Readiness config is missing for program '{program_id}'.")

    document = _load_yaml_mapping(path)
    schema_version = _require_schema_version(document, path)
    if schema_version.split(".", 1)[0] != "1":
        raise ConfigError(f"Unsupported readiness schema_version {schema_version!r} in {path}.")

    snapshot_max_age_days = _optional_int(document.get("snapshot_max_age_days")) or 7
    raw_dimensions = document.get("dimensions")
    if raw_dimensions is None:
        raw_dimensions = {}
    if not isinstance(raw_dimensions, dict):
        raise ConfigError(f"Expected 'dimensions' mapping in {path}.")

    raw_custom_dimensions = document.get("custom_dimensions")
    if raw_custom_dimensions is None:
        raw_custom_dimensions = {}
    if not isinstance(raw_custom_dimensions, dict):
        raise ConfigError(f"Expected 'custom_dimensions' mapping in {path}.")
    if not raw_dimensions and not raw_custom_dimensions:
        raise ConfigError(f"Expected at least one readiness dimension in {path}.")

    ordered_entries = _ordered_dimension_entries(raw_dimensions, raw_custom_dimensions)
    dimensions = tuple(
        _parse_dimension_config(dimension_id, payload, path=path, is_custom=(dimension_id in raw_custom_dimensions))
        for dimension_id, payload in ordered_entries
    )
    return ReadinessConfig(
        program_id=program_id,
        snapshot_max_age_days=snapshot_max_age_days,
        dimensions=dimensions,
    )


def build_readiness_snapshot(
    program_id: str,
    config: ReadinessConfig,
    *,
    loaders: ReadinessFetchLoaders,
    fetched_at: datetime | None = None,
) -> ReadinessSnapshot:
    resolved_fetched_at = _coerce_utc_datetime(fetched_at or datetime.now(timezone.utc))
    dependency_loader = loaders.load_dependencies or (lambda: _load_current_dependencies(program_id))
    risk_loader = loaders.load_risk_entries or (lambda: _load_current_risk_entries(program_id))
    incident_loader = loaders.load_incident_entries or (
        lambda dimension, as_of: _load_incident_entries_for_dimension(
            program_id,
            dimension,
            as_of=as_of,
            programs_root=PROGRAMS_ROOT,
        )
    )
    cached_dependencies: tuple[Dependency, ...] | None = None
    cached_risk_entries: tuple[RiskEntry, ...] | None = None
    results: list[ReadinessDimensionResult] = []

    for dimension in config.dimensions:
        if dimension.source.type == "ado_query":
            if loaders.load_ado_query_rows is None:
                raise ConfigError(f"No ADO query loader is configured for readiness dimension '{dimension.id}'.")
            results.append(_evaluate_ado_query_dimension(dimension, loaders.load_ado_query_rows(dimension)))
            continue
        if dimension.source.type == "kusto_query":
            if loaders.load_kusto_query_rows is None:
                raise ConfigError(f"No Kusto query loader is configured for readiness dimension '{dimension.id}'.")
            results.append(_evaluate_kusto_query_dimension(dimension, loaders.load_kusto_query_rows(dimension)))
            continue
        if dimension.source.type == "manual_attestation":
            results.append(_evaluate_manual_attestation_dimension(dimension, as_of=resolved_fetched_at.date()))
            continue
        if dimension.source.type == "people_directory":
            if loaders.alias_exists is None:
                raise ConfigError(f"No people-directory loader is configured for readiness dimension '{dimension.id}'.")
            alias = dimension.source.alias
            if alias is None:
                raise ConfigError(f"Readiness dimension '{dimension.id}' requires source.alias.")
            results.append(_evaluate_people_directory_dimension(dimension, alias_exists=loaders.alias_exists(alias)))
            continue
        if dimension.source.type == "dependency_health":
            if cached_dependencies is None:
                cached_dependencies = dependency_loader()
            results.append(_evaluate_dependency_health_dimension(dimension, dependencies=cached_dependencies))
            continue
        if dimension.source.type == "workstream_risk":
            if cached_risk_entries is None:
                cached_risk_entries = risk_loader()
            results.append(_evaluate_workstream_risk_dimension(dimension, risk_entries=cached_risk_entries))
            continue
        if dimension.source.type == "incident_journal":
            results.append(
                _evaluate_incident_journal_dimension(
                    dimension,
                    incident_entries=incident_loader(dimension, resolved_fetched_at),
                )
            )
            continue
        raise ConfigError(f"Unsupported readiness source type '{dimension.source.type}' for '{dimension.id}'.")

    dimensions_payload = _dimensions_payload(tuple(results))
    return ReadinessSnapshot(
        program_id=program_id,
        fetched_at=resolved_fetched_at,
        snapshot_max_age_days=config.snapshot_max_age_days,
        content_sha256=_compute_dimensions_hash(dimensions_payload),
        dimensions=tuple(results),
    )


def _load_current_dependencies(program_id: str) -> tuple[Dependency, ...]:
    return project_dependencies(
        load_program_facts(
            program_id,
            programs_root=PROGRAMS_ROOT,
            fact_types=("dependency.link",),
        )
    )


def _load_current_risk_entries(program_id: str) -> tuple[RiskEntry, ...]:
    return project_risk_entries(
        load_program_facts(
            program_id,
            programs_root=PROGRAMS_ROOT,
            fact_types=("risk.entry",),
        )
    )


def write_readiness_snapshot(
    program_id: str,
    snapshot: ReadinessSnapshot,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    path = get_readiness_snapshot_path(program_id, programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "program_id": snapshot.program_id,
        "fetched_at": snapshot.fetched_at.isoformat(),
        "snapshot_max_age_days": snapshot.snapshot_max_age_days,
        "content_sha256": snapshot.content_sha256,
        "dimensions": _dimensions_payload(snapshot.dimensions),
    }
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    os.replace(temp_path, path)
    return path


def load_readiness_snapshot(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> ReadinessSnapshotLoadResult:
    path = resolve_readiness_snapshot_path_for_read(program_id, programs_root=programs_root)
    if not path.exists():
        return ReadinessSnapshotLoadResult(
            snapshot=None,
            warnings=(f"Readiness snapshot is missing for program '{program_id}'. Run `vertex readiness fetch --program {program_id}`.",),
        )

    document = _load_yaml_mapping(path)
    schema_version = _require_schema_version(document, path)
    if schema_version.split(".", 1)[0] != "1":
        return ReadinessSnapshotLoadResult(
            snapshot=None,
            warnings=(f"Readiness snapshot schema_version {schema_version!r} is unsupported for program '{program_id}'.",),
        )

    raw_dimensions = document.get("dimensions")
    if not isinstance(raw_dimensions, dict):
        return ReadinessSnapshotLoadResult(
            snapshot=None,
            warnings=(f"Readiness snapshot for program '{program_id}' is unreadable: dimensions payload is missing.",),
        )

    stored_hash = _optional_str(document.get("content_sha256"))
    if stored_hash is None:
        return ReadinessSnapshotLoadResult(
            snapshot=None,
            warnings=(f"Readiness snapshot for program '{program_id}' is missing content_sha256.",),
        )

    calculated_hash = _compute_dimensions_hash(raw_dimensions)
    if calculated_hash != stored_hash:
        return ReadinessSnapshotLoadResult(
            snapshot=None,
            warnings=(
                f"Readiness snapshot hash mismatch for program '{program_id}'. The snapshot may have been tampered with; rerun `vertex readiness fetch --program {program_id}`.",
            ),
        )

    dimensions = tuple(
        _dimension_result_from_payload(dimension_id, payload)
        for dimension_id, payload in _ordered_snapshot_dimensions(raw_dimensions)
    )
    return ReadinessSnapshotLoadResult(
        snapshot=ReadinessSnapshot(
            program_id=_optional_str(document.get("program_id")) or program_id,
            fetched_at=_parse_datetime(document.get("fetched_at"), field_name="fetched_at", path=path),
            snapshot_max_age_days=_optional_int(document.get("snapshot_max_age_days")) or 7,
            content_sha256=stored_hash,
            dimensions=dimensions,
        ),
        warnings=(),
    )


def snapshot_age_days(snapshot: ReadinessSnapshot, *, as_of: datetime | None = None) -> int:
    resolved_as_of = _coerce_utc_datetime(as_of or datetime.now(timezone.utc))
    return max(0, (resolved_as_of - snapshot.fetched_at).days)


def is_snapshot_stale(
    snapshot: ReadinessSnapshot,
    *,
    as_of: datetime | None = None,
    max_age_days: int | None = None,
) -> bool:
    threshold = snapshot.snapshot_max_age_days if max_age_days is None else max_age_days
    return snapshot_age_days(snapshot, as_of=as_of) > threshold


def _ordered_dimension_entries(
    raw_dimensions: dict[str, Any],
    raw_custom_dimensions: dict[str, Any],
) -> tuple[tuple[str, Any], ...]:
    ordered: list[tuple[str, Any]] = []
    seen: set[str] = set()
    for dimension_id in DEFAULT_DIMENSION_ORDER:
        if dimension_id in raw_dimensions:
            ordered.append((dimension_id, raw_dimensions[dimension_id]))
            seen.add(dimension_id)
    for dimension_id, payload in raw_dimensions.items():
        if dimension_id in seen:
            continue
        ordered.append((dimension_id, payload))
        seen.add(dimension_id)
    for dimension_id, payload in raw_custom_dimensions.items():
        if dimension_id in seen:
            raise ConfigError(f"Duplicate readiness dimension '{dimension_id}' in readiness config.")
        ordered.append((dimension_id, payload))
    return tuple(ordered)


def _ordered_snapshot_dimensions(raw_dimensions: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    ordered: list[tuple[str, Any]] = []
    seen: set[str] = set()
    for dimension_id in DEFAULT_DIMENSION_ORDER:
        if dimension_id in raw_dimensions:
            ordered.append((dimension_id, raw_dimensions[dimension_id]))
            seen.add(dimension_id)
    for dimension_id, payload in raw_dimensions.items():
        if dimension_id in seen:
            continue
        ordered.append((dimension_id, payload))
    return tuple(ordered)


def _parse_dimension_config(
    dimension_id: str,
    raw_dimension: Any,
    *,
    path: Path,
    is_custom: bool,
) -> ReadinessDimensionConfig:
    if not isinstance(raw_dimension, dict):
        raise ConfigError(f"Readiness dimension '{dimension_id}' in {path} must be a mapping.")
    default_name, default_gate_id = DEFAULT_READINESS_DIMENSIONS.get(
        dimension_id,
        (_humanize_dimension_id(dimension_id), f"QG-RD-{dimension_id}"),
    )
    gate_id = _optional_str(raw_dimension.get("gate_id")) or default_gate_id
    if is_custom and not gate_id.startswith("QG-RD-"):
        gate_id = f"QG-RD-{dimension_id}"
    return ReadinessDimensionConfig(
        id=dimension_id,
        name=_optional_str(raw_dimension.get("name")) or default_name,
        gate_id=gate_id,
        source=_parse_source_config(dimension_id, raw_dimension.get("source"), path=path),
        pass_condition=_parse_pass_condition(dimension_id, raw_dimension.get("pass_condition"), path=path),
    )


def _parse_source_config(dimension_id: str, raw_source: Any, *, path: Path) -> ReadinessSourceConfig:
    if not isinstance(raw_source, dict):
        raise ConfigError(f"Readiness dimension '{dimension_id}' in {path} must define a source mapping.")
    source_type = _optional_str(raw_source.get("type"))
    if source_type is None:
        raise ConfigError(f"Readiness dimension '{dimension_id}' in {path} is missing source.type.")
    return ReadinessSourceConfig(
        type=source_type,
        query_id=_optional_str(raw_source.get("query_id")),
        alias=_optional_str(raw_source.get("alias")),
        attested_at=_parse_optional_date(raw_source.get("attested_at"), field_name=f"{dimension_id}.source.attested_at", path=path),
        attested_by=_optional_str(raw_source.get("attested_by")),
        notes=_optional_str(raw_source.get("notes")),
        workstream_id=_optional_str(raw_source.get("workstream_id")),
    )


def _parse_pass_condition(dimension_id: str, raw_condition: Any, *, path: Path) -> ReadinessPassCondition:
    if not isinstance(raw_condition, dict):
        raise ConfigError(f"Readiness dimension '{dimension_id}' in {path} must define a pass_condition mapping.")
    kind = _optional_str(raw_condition.get("kind"))
    if kind is None:
        raise ConfigError(f"Readiness dimension '{dimension_id}' in {path} is missing pass_condition.kind.")
    operator = _optional_str(raw_condition.get("operator"))
    if operator is not None and operator not in _NUMERIC_OPERATORS:
        raise ConfigError(f"Readiness dimension '{dimension_id}' in {path} uses unsupported operator '{operator}'.")
    risk_level = _optional_str(raw_condition.get("risk_level"))
    if risk_level is not None:
        try:
            RiskLevel.from_string(risk_level)
        except ValueError as error:
            raise ConfigError(f"Readiness dimension '{dimension_id}' in {path} uses unsupported risk_level '{risk_level}'.") from error
    return ReadinessPassCondition(
        kind=kind,
        allowed_states=_string_tuple(raw_condition.get("allowed_states")),
        operator=operator,
        threshold=_optional_float(raw_condition.get("threshold")),
        days=_optional_int(raw_condition.get("days")),
        result_column=_optional_str(raw_condition.get("result_column")),
        risk_level=risk_level,
    )


def _evaluate_ado_query_dimension(dimension: ReadinessDimensionConfig, rows: list[dict[str, Any]]) -> ReadinessDimensionResult:
    if dimension.pass_condition.kind not in _STATE_PASS_CONDITION_KINDS:
        raise ConfigError(f"Readiness dimension '{dimension.id}' uses unsupported ADO pass condition '{dimension.pass_condition.kind}'.")
    if not dimension.pass_condition.allowed_states:
        raise ConfigError(f"Readiness dimension '{dimension.id}' requires pass_condition.allowed_states.")
    allowed_states = {state.strip().lower() for state in dimension.pass_condition.allowed_states if state.strip()}
    observed_states = [_extract_work_item_state(row) for row in rows]
    matching_count = sum(1 for state in observed_states if state in allowed_states)
    total_count = len(rows)
    if total_count == 0:
        passed = False
        summary = "No work items were returned by the saved query."
    elif dimension.pass_condition.kind == "all_work_items_in_states":
        passed = matching_count == total_count
        summary = f"{matching_count}/{total_count} queried work items are in the required terminal states."
    else:
        passed = matching_count > 0
        summary = f"{matching_count}/{total_count} queried work items are in the required handoff states."
    return ReadinessDimensionResult(
        id=dimension.id,
        name=dimension.name,
        gate_id=dimension.gate_id,
        source_type=dimension.source.type,
        passed=passed,
        status="green" if passed else "red",
        summary=summary,
        observed_value=f"{matching_count}/{total_count} matching",
        threshold=f"states in {{{', '.join(dimension.pass_condition.allowed_states)}}}",
        evidence_refs=tuple(_work_item_ref(row) for row in rows[:5]),
        details={
            "query_id": dimension.source.query_id,
            "matching_count": matching_count,
            "total_count": total_count,
        },
    )


def _evaluate_kusto_query_dimension(dimension: ReadinessDimensionConfig, rows: list[dict[str, Any]]) -> ReadinessDimensionResult:
    if dimension.pass_condition.kind != "numeric_threshold":
        raise ConfigError(f"Readiness dimension '{dimension.id}' uses unsupported Kusto pass condition '{dimension.pass_condition.kind}'.")
    if dimension.pass_condition.operator is None or dimension.pass_condition.threshold is None:
        raise ConfigError(f"Readiness dimension '{dimension.id}' requires numeric threshold operator and threshold.")
    value, column_name = _extract_numeric_result(rows, preferred_column=dimension.pass_condition.result_column)
    if value is None:
        return ReadinessDimensionResult(
            id=dimension.id,
            name=dimension.name,
            gate_id=dimension.gate_id,
            source_type=dimension.source.type,
            passed=False,
            status="red",
            summary="The Kusto probe did not return a numeric value for evaluation.",
            observed_value="unavailable",
            threshold=f"{dimension.pass_condition.operator} {dimension.pass_condition.threshold:g}",
            evidence_refs=(f"kusto:{dimension.source.query_id}",),
            details={"query_id": dimension.source.query_id},
        )
    passed = _compare_numeric(value, dimension.pass_condition.operator, dimension.pass_condition.threshold)
    return ReadinessDimensionResult(
        id=dimension.id,
        name=dimension.name,
        gate_id=dimension.gate_id,
        source_type=dimension.source.type,
        passed=passed,
        status="green" if passed else "red",
        summary=f"Kusto probe returned {value:g} for {column_name}.",
        observed_value=f"{value:g}",
        threshold=f"{column_name} {dimension.pass_condition.operator} {dimension.pass_condition.threshold:g}",
        evidence_refs=(f"kusto:{dimension.source.query_id}",),
        details={
            "query_id": dimension.source.query_id,
            "result_column": column_name,
            "observed": value,
        },
    )


def _evaluate_manual_attestation_dimension(dimension: ReadinessDimensionConfig, *, as_of: date) -> ReadinessDimensionResult:
    if dimension.pass_condition.kind != "attested_within_days" or dimension.pass_condition.days is None:
        raise ConfigError(f"Readiness dimension '{dimension.id}' uses unsupported manual-attestation pass condition '{dimension.pass_condition.kind}'.")
    attested_at = dimension.source.attested_at
    if attested_at is None:
        return ReadinessDimensionResult(
            id=dimension.id,
            name=dimension.name,
            gate_id=dimension.gate_id,
            source_type=dimension.source.type,
            passed=False,
            status="red",
            summary="No attestation date is recorded for this readiness dimension.",
            observed_value="missing",
            threshold=f"attested within {dimension.pass_condition.days} days",
            details={"attested_by": dimension.source.attested_by, "notes": dimension.source.notes},
        )
    age_days = max(0, (as_of - attested_at).days)
    passed = age_days <= dimension.pass_condition.days
    actor = dimension.source.attested_by or "unknown"
    return ReadinessDimensionResult(
        id=dimension.id,
        name=dimension.name,
        gate_id=dimension.gate_id,
        source_type=dimension.source.type,
        passed=passed,
        status="green" if passed else "red",
        summary=f"{dimension.name} attestation is {age_days} day(s) old, last signed by {actor}.",
        observed_value=f"{age_days}d old",
        threshold=f"attested within {dimension.pass_condition.days} days",
        evidence_refs=(f"attestation:{actor}:{attested_at.isoformat()}",),
        details={
            "attested_at": attested_at.isoformat(),
            "attested_by": dimension.source.attested_by,
            "notes": dimension.source.notes,
        },
    )


def _evaluate_people_directory_dimension(dimension: ReadinessDimensionConfig, *, alias_exists: bool) -> ReadinessDimensionResult:
    if dimension.pass_condition.kind != "alias_exists":
        raise ConfigError(f"Readiness dimension '{dimension.id}' uses unsupported people-directory pass condition '{dimension.pass_condition.kind}'.")
    alias = dimension.source.alias or "unknown"
    return ReadinessDimensionResult(
        id=dimension.id,
        name=dimension.name,
        gate_id=dimension.gate_id,
        source_type=dimension.source.type,
        passed=alias_exists,
        status="green" if alias_exists else "red",
        summary=(f"Alias '{alias}' exists in the people directory." if alias_exists else f"Alias '{alias}' is missing from the people directory."),
        observed_value=alias,
        threshold="alias present in people directory",
        evidence_refs=(f"people:{alias}",),
        details={"alias": alias, "found": alias_exists},
    )


def _evaluate_dependency_health_dimension(dimension: ReadinessDimensionConfig, *, dependencies: tuple[Dependency, ...]) -> ReadinessDimensionResult:
    if dimension.pass_condition.kind != "no_high_risk_first_hop":
        raise ConfigError(f"Readiness dimension '{dimension.id}' uses unsupported dependency-health pass condition '{dimension.pass_condition.kind}'.")
    failing_dependencies = [
        dependency
        for dependency in dependencies
        if dependency.status in _ACTIVE_DEPENDENCY_STATUSES
        and (dependency.status == DependencyStatus.BROKEN or dependency.schedule_status in _HIGH_RISK_DEPENDENCY_SCHEDULES)
    ]
    cross_org_failing_dependencies = [
        dependency
        for dependency in failing_dependencies
        if (dependency.resolution_path or "").startswith("cross_org") or dependency.from_program_id != dependency.to_program_id
    ]
    passed = not failing_dependencies
    if passed:
        summary = "No first-hop dependencies are currently at risk."
    elif cross_org_failing_dependencies:
        summary = (
            f"{len(failing_dependencies)} first-hop dependenc{'y is' if len(failing_dependencies) == 1 else 'ies are'} currently at risk, "
            f"including {len(cross_org_failing_dependencies)} cross-org dependenc{'y' if len(cross_org_failing_dependencies) == 1 else 'ies'}."
        )
    else:
        summary = f"{len(failing_dependencies)} first-hop dependenc{'y is' if len(failing_dependencies) == 1 else 'ies are'} currently at risk."
    return ReadinessDimensionResult(
        id=dimension.id,
        name=dimension.name,
        gate_id=dimension.gate_id,
        source_type=dimension.source.type,
        passed=passed,
        status="green" if passed else "red",
        summary=summary,
        observed_value=(
            f"{len(failing_dependencies)} at-risk ({len(cross_org_failing_dependencies)} cross-org)"
            if cross_org_failing_dependencies
            else f"{len(failing_dependencies)} at-risk"
        ),
        threshold="no active first-hop dependency at risk",
        evidence_refs=tuple(f"dependency:{dependency.id}" for dependency in failing_dependencies[:5]),
        details={
            "failing_dependency_ids": [dependency.id for dependency in failing_dependencies],
            "cross_org_failing_dependency_ids": [dependency.id for dependency in cross_org_failing_dependencies],
        },
    )


def _evaluate_workstream_risk_dimension(
    dimension: ReadinessDimensionConfig,
    *,
    risk_entries: tuple[RiskEntry, ...],
) -> ReadinessDimensionResult:
    if dimension.pass_condition.kind != "max_risk_level":
        raise ConfigError(f"Readiness dimension '{dimension.id}' uses unsupported workstream-risk pass condition '{dimension.pass_condition.kind}'.")
    workstream_id = dimension.source.workstream_id
    if workstream_id is None:
        raise ConfigError(f"Readiness dimension '{dimension.id}' requires source.workstream_id.")
    threshold_name = dimension.pass_condition.risk_level
    if threshold_name is None:
        raise ConfigError(f"Readiness dimension '{dimension.id}' requires pass_condition.risk_level.")
    threshold_level = RiskLevel.from_string(threshold_name)
    scoped_risks = tuple(
        entry
        for entry in risk_entries
        if entry.status in _ACTIVE_RISK_STATUSES and workstream_id in entry.linked_workstream_ids
    )
    observed_level = _highest_workstream_risk_level(scoped_risks)
    passed = _risk_level_rank(observed_level) <= _risk_level_rank(threshold_level)
    if scoped_risks:
        summary = (
            f"Highest active risk for workstream '{workstream_id}' is {observed_level.value} across "
            f"{len(scoped_risks)} linked risk register entr{'y' if len(scoped_risks) == 1 else 'ies'}."
        )
    else:
        summary = f"No active risk register entries are linked to workstream '{workstream_id}'."
    return ReadinessDimensionResult(
        id=dimension.id,
        name=dimension.name,
        gate_id=dimension.gate_id,
        source_type=dimension.source.type,
        passed=passed,
        status="green" if passed else "red",
        summary=summary,
        observed_value=observed_level.value,
        threshold=f"risk <= {threshold_level.value}",
        evidence_refs=tuple(f"risk:{entry.id}" for entry in scoped_risks[:5]),
        details={
            "workstream_id": workstream_id,
            "risk_level": observed_level.value,
            "threshold_risk_level": threshold_level.value,
            "risk_ids": [entry.id for entry in scoped_risks],
        },
    )


def _evaluate_incident_journal_dimension(
    dimension: ReadinessDimensionConfig,
    *,
    incident_entries: tuple[IncidentEntry, ...],
) -> ReadinessDimensionResult:
    if dimension.pass_condition.kind != "max_recent_incidents":
        raise ConfigError(
            f"Readiness dimension '{dimension.id}' uses unsupported incident-journal pass condition '{dimension.pass_condition.kind}'."
        )
    if dimension.pass_condition.threshold is None or dimension.pass_condition.days is None:
        raise ConfigError(
            f"Readiness dimension '{dimension.id}' requires pass_condition.threshold and pass_condition.days."
        )

    threshold = max(0, int(dimension.pass_condition.threshold))
    workstream_id = dimension.source.workstream_id
    attributed_entries = tuple(
        entry
        for entry in incident_entries
        if entry.ado_entity_refs and (workstream_id is None or entry.workstream_id == workstream_id)
    )
    passed = len(attributed_entries) <= threshold
    if workstream_id is None:
        scope_label = f"the last {dimension.pass_condition.days} day(s)"
    else:
        scope_label = f"workstream '{workstream_id}' in the last {dimension.pass_condition.days} day(s)"
    if attributed_entries:
        summary = (
            f"{len(attributed_entries)} attribution-backed incident learning"
            f"{'s' if len(attributed_entries) != 1 else ''} recorded for {scope_label}."
        )
    else:
        summary = f"No attribution-backed incident learnings were recorded for {scope_label}."
    return ReadinessDimensionResult(
        id=dimension.id,
        name=dimension.name,
        gate_id=dimension.gate_id,
        source_type=dimension.source.type,
        passed=passed,
        status="green" if passed else "red",
        summary=summary,
        observed_value=str(len(attributed_entries)),
        threshold=f"incident count <= {threshold} within {dimension.pass_condition.days}d",
        evidence_refs=tuple(f"IcM:{entry.incident_id}" for entry in attributed_entries[:5]),
        details={
            "days": dimension.pass_condition.days,
            "threshold": threshold,
            "workstream_id": workstream_id,
            "incident_ids": [entry.incident_id for entry in attributed_entries],
        },
    )


def _load_incident_entries_for_dimension(
    program_id: str,
    dimension: ReadinessDimensionConfig,
    *,
    as_of: datetime,
    programs_root: Path,
) -> tuple[IncidentEntry, ...]:
    if dimension.pass_condition.days is None:
        raise ConfigError(f"Readiness dimension '{dimension.id}' requires pass_condition.days.")
    start = as_of - timedelta(days=dimension.pass_condition.days)
    return read_incident_entries(program_id, start=start, end=as_of, programs_root=programs_root)


def _dimensions_payload(dimensions: tuple[ReadinessDimensionResult, ...]) -> dict[str, dict[str, Any]]:
    return {
        dimension.id: {
            "name": dimension.name,
            "gate_id": dimension.gate_id,
            "source_type": dimension.source_type,
            "passed": dimension.passed,
            "status": dimension.status,
            "summary": dimension.summary,
            "observed_value": dimension.observed_value,
            "threshold": dimension.threshold,
            "evidence_refs": list(dimension.evidence_refs),
            "details": dimension.details or {},
        }
        for dimension in dimensions
    }


def _dimension_result_from_payload(dimension_id: str, payload: Any) -> ReadinessDimensionResult:
    if not isinstance(payload, dict):
        raise ConfigError(f"Readiness dimension '{dimension_id}' snapshot payload must be a mapping.")
    return ReadinessDimensionResult(
        id=dimension_id,
        name=_optional_str(payload.get("name")) or _humanize_dimension_id(dimension_id),
        gate_id=_optional_str(payload.get("gate_id")) or f"QG-RD-{dimension_id}",
        source_type=_optional_str(payload.get("source_type")) or "unknown",
        passed=bool(payload.get("passed", False)),
        status=_optional_str(payload.get("status")) or ("green" if payload.get("passed") else "red"),
        summary=_optional_str(payload.get("summary")) or "",
        observed_value=_optional_str(payload.get("observed_value")) or "",
        threshold=_optional_str(payload.get("threshold")) or "",
        evidence_refs=_string_tuple(payload.get("evidence_refs")),
        details=payload.get("details") if isinstance(payload.get("details"), dict) else {},
    )


def _compute_dimensions_hash(dimensions_payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(dimensions_payload, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()


def _extract_numeric_result(rows: list[dict[str, Any]], *, preferred_column: str | None) -> tuple[float | None, str]:
    if not rows:
        return None, preferred_column or "value"
    first_row = rows[0]
    if preferred_column is not None:
        return _optional_float(first_row.get(preferred_column)), preferred_column
    for key, value in first_row.items():
        numeric_value = _optional_float(value)
        if numeric_value is not None:
            return numeric_value, str(key)
    return None, preferred_column or "value"


def _compare_numeric(value: float, operator: str, threshold: float) -> bool:
    if operator == ">":
        return value > threshold
    if operator == ">=":
        return value >= threshold
    if operator == "<":
        return value < threshold
    if operator == "<=":
        return value <= threshold
    if operator == "==":
        return value == threshold
    if operator == "!=":
        return value != threshold
    raise ConfigError(f"Unsupported readiness operator '{operator}'.")


def _highest_workstream_risk_level(entries: tuple[RiskEntry, ...]) -> RiskLevel:
    if not entries:
        return RiskLevel.LOW
    return max((_risk_level_for_entry(entry) for entry in entries), key=_risk_level_rank)


def _risk_level_for_entry(entry: RiskEntry) -> RiskLevel:
    if entry.status == RiskStatus.ESCALATED:
        return RiskLevel.HIGH
    score = compute_risk_score(entry)
    if score >= 9:
        return RiskLevel.HIGH
    if score >= 4:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def _risk_level_rank(level: RiskLevel) -> int:
    return _RISK_LEVEL_RANK[level]


def _extract_work_item_state(row: dict[str, Any]) -> str:
    fields = row.get("fields")
    if isinstance(fields, dict):
        state = fields.get("System.State")
        if isinstance(state, str):
            return state.strip().lower()
    state = row.get("System.State")
    if isinstance(state, str):
        return state.strip().lower()
    return ""


def _work_item_ref(row: dict[str, Any]) -> str:
    fields = row.get("fields")
    if isinstance(fields, dict):
        work_item_id = fields.get("System.Id")
        if work_item_id is not None:
            return f"WI:{work_item_id}"
    work_item_id = row.get("id") or row.get("System.Id")
    return f"WI:{work_item_id}" if work_item_id is not None else "WI:unknown"


def _coerce_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: Any, *, field_name: str, path: Path) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Missing {field_name} in {path}.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ConfigError(f"Invalid {field_name} in {path}: {value!r}") from error
    return _coerce_utc_datetime(parsed)


def _parse_optional_date(value: Any, *, field_name: str, path: Path) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ConfigError(f"Invalid {field_name} in {path}: expected ISO date string.")
    text = value.strip()
    if not text:
        return None
    if "T" in text:
        text = text.split("T", 1)[0]
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise ConfigError(f"Invalid {field_name} in {path}: {value!r}") from error


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    # Different return shape from yaml_utils.load_yaml_mapping — migration deferred.
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"Invalid YAML in {path}: {error}") from error
    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}.")
    return document


def _require_schema_version(document: dict[str, Any], path: Path) -> str:
    schema_version = _optional_str(document.get("schema_version"))
    if schema_version is None:
        raise ConfigError(f"schema_version is required in {path}.")
    return schema_version


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(entry).strip() for entry in value if str(entry).strip())


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace("%", "")
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _optional_str(value: Any) -> str | None:
    if not isinstance(value, (str, int, float)):
        return None
    text = str(value).strip()
    return text or None


def _humanize_dimension_id(dimension_id: str) -> str:
    return dimension_id.replace("_", " ").replace("-", " ").strip().title()
