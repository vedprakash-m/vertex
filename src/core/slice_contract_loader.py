from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from src.core.exceptions import ConfigError
from src.core.connector_config import ExternalConnectorConfig


REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTS_ROOT = REPO_ROOT / "reports"
_ALLOWED_ASSIGNMENT_MODES = {"auto", "manual_only"}
_ALLOWED_SOURCE_OF_TRUTH = {"ado_primary", "telemetry_primary", "hybrid", "manual_only"}
_ALLOWED_SCHEMA_VERSIONS = {"1.0", "1.1"}
_ALLOWED_QUERY_MODES = {"full_scope", "activity_delta", "analytics_history"}
# Armada spec D-20: schema 1.1 binding-level filters use a closed field/op set so the
# loader never silently drops an unsupported predicate.
_CLOSED_BINDING_FILTER_OPS: dict[str, set[str]] = {
    "area_path": {"eq", "under", "not_under"},
    "work_item_type": {"eq"},
    "title": {"eq", "contains"},
    "tag": {"contains_words"},
}


@dataclass(frozen=True, slots=True)
class SlicePredicateDefinition:
    field: str
    op: str
    value: str


@dataclass(frozen=True, slots=True)
class SliceFilterDefinition:
    all_of: tuple[SlicePredicateDefinition, ...] = ()
    any_of: tuple[SlicePredicateDefinition, ...] = ()

    def is_empty(self) -> bool:
        return not self.all_of and not self.any_of


@dataclass(frozen=True, slots=True)
class TagExpression:
    all_of: tuple[str, ...] = ()
    any_of: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not self.all_of and not self.any_of


@dataclass(frozen=True, slots=True)
class SliceOwners:
    primary: str
    support_tpm: str | None = None


@dataclass(frozen=True, slots=True)
class SavedQueryBinding:
    """Armada spec D-20 (schema 1.1): one typed saved-query binding.

    Schema 1.0 saved-query GUID strings are synthesized into a ``legacy-NN``
    binding with ``mode="activity_delta"`` for backward-compatible report
    behavior (D-20 migration rule); they are never emitted by writers.
    """

    binding_id: str
    query_id: str
    mode: str  # full_scope | activity_delta | analytics_history
    required: bool = True
    workstream_ids: tuple[str, ...] = ()
    lane_ids: tuple[str, ...] = ()
    filter: SliceFilterDefinition | None = None

    @property
    def is_legacy(self) -> bool:
        return self.binding_id.startswith("legacy-")


@dataclass(frozen=True, slots=True)
class SliceAdoSourceContract:
    saved_queries: tuple[str, ...]
    filters: SliceFilterDefinition | None
    explicit_work_item_ids: tuple[int, ...]
    required_fields: tuple[str, ...]
    tag_expression: TagExpression | None = None
    intentional_filter_only: bool = False
    intentional_filter_only_expires_on: date | None = None
    saved_query_bindings: tuple[SavedQueryBinding, ...] = ()

    def bindings_for_mode(self, mode: str) -> tuple[SavedQueryBinding, ...]:
        return tuple(binding for binding in self.saved_query_bindings if binding.mode == mode)

    @property
    def full_scope_query_ids(self) -> tuple[str, ...]:
        """Query IDs bound with mode=full_scope (D-2): never date-bounded by a consumer."""
        return tuple(dict.fromkeys(binding.query_id for binding in self.bindings_for_mode("full_scope")))


@dataclass(frozen=True, slots=True)
class SliceTelemetryContract:
    query_id: str
    expected_grain: str
    freshness_sla_hours: int
    confidence_threshold: str
    fallback_behavior: str
    cross_check_rules: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SliceDecisionSourceSelector:
    workstream_id: str
    artifact_type: str


@dataclass(frozen=True, slots=True)
class SliceDecisionSource:
    source_id: str
    channels: tuple[str, ...]
    blocked_artifact_selectors: tuple[SliceDecisionSourceSelector, ...] = ()
    blocked_artifact_ids: tuple[str, ...] = ()




@dataclass(frozen=True, slots=True)
class SliceSourceContract:
    ado: SliceAdoSourceContract | None
    telemetry: SliceTelemetryContract | None
    fallback_sources: tuple[str, ...]
    decision_sources: tuple[SliceDecisionSource, ...] = ()


@dataclass(frozen=True, slots=True)
class SliceFreshness:
    warn_days: int
    block_days: int


@dataclass(frozen=True, slots=True)
class SliceDegradation:
    blank_filter_is_error: bool
    missing_target_date: str | None = None
    stale_owner_comment: str | None = None


@dataclass(frozen=True, slots=True)
class SliceContract:
    id: str
    scorecard_name: str
    section: str
    workstream: str
    slice_kind: str
    title: str
    source_of_truth: str
    owners: SliceOwners
    source_contract: SliceSourceContract
    freshness: SliceFreshness
    degradation: SliceDegradation
    remediation_template: str | None
    assignment_mode: str = "auto"
    required: bool = True
    # Armada spec D-1/D-20: stable workstream identifier supplementing the
    # existing human-readable `workstream` display field (schema 1.1+).
    workstream_id: str | None = None

    @property
    def lookup_key(self) -> tuple[str, str]:
        return (self.scorecard_name, self.title)


def load_slice_contract(path: Path) -> tuple[SliceContract, ...]:
    if not path.exists():
        raise ConfigError(f"Missing required file: {path}")
    with path.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}
    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}")
    schema_version = str(document.get("schema_version", "")).strip()
    if schema_version not in _ALLOWED_SCHEMA_VERSIONS:
        raise ConfigError(
            f"Unsupported slice contract schema version {schema_version!r} in {path}. "
            f"Supported versions: {sorted(_ALLOWED_SCHEMA_VERSIONS)}. "
            "Migrate with the schema-1.1 saved-query binding shape (binding_id/query_id/mode)."
        )
    raw_slices = document.get("slices", [])
    if not isinstance(raw_slices, list):
        raise ConfigError(f"slices must be a list in {path}")

    contracts = tuple(_parse_slice_contract(entry, path, schema_version=schema_version) for entry in raw_slices)
    _validate_unique_contracts(contracts, path)
    return contracts


def load_slice_contract_for_edition(
    edition_name: str,
    reports_root: Path = REPORTS_ROOT,
) -> tuple[SliceContract, ...] | None:
    path = reports_root / edition_name / "slice_contracts.yaml"
    if not path.exists():
        return None
    return load_slice_contract(path)


def load_external_connector_configs(path: Path) -> tuple[ExternalConnectorConfig, ...]:
    """Load external_connectors section from a slice_contracts.yaml file (FR-SG-48)."""
    if not path.exists():
        return ()
    with path.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}
    raw_connectors = document.get("external_connectors", [])
    if not isinstance(raw_connectors, list):
        raise ConfigError(f"external_connectors must be a list in {path}")
    return tuple(_parse_external_connector_config(entry, path) for entry in raw_connectors)


def load_external_connector_configs_for_edition(
    edition_name: str,
    reports_root: Path = REPORTS_ROOT,
) -> tuple[ExternalConnectorConfig, ...]:
    path = reports_root / edition_name / "slice_contracts.yaml"
    return load_external_connector_configs(path)


def _parse_external_connector_config(raw: Any, path: Path) -> ExternalConnectorConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"Each external_connectors entry must be a mapping in {path}")
    dep_id = _require_non_empty_string(raw.get("dep_id"), path, "external_connector.dep_id")
    connector_type = _require_non_empty_string(
        raw.get("connector_type"), path, f"{dep_id}.connector_type"
    )
    source_url = _require_non_empty_string(raw.get("source_url"), path, f"{dep_id}.source_url")
    team = _require_non_empty_string(raw.get("team"), path, f"{dep_id}.team")
    raw_gates = raw.get("gates") or []
    gates = tuple(str(g) for g in raw_gates) if isinstance(raw_gates, list) else ()
    auth_token = raw.get("auth_token") or None
    return ExternalConnectorConfig(
        dep_id=dep_id,
        connector_type=connector_type,  # type: ignore[arg-type]
        source_url=source_url,
        team=team,
        gates=gates,
        auth_token=auth_token,
    )


def _parse_slice_contract(raw_slice: Any, path: Path, *, schema_version: str = "1.0") -> SliceContract:
    if not isinstance(raw_slice, dict):
        raise ConfigError(f"Each slice contract must be a mapping in {path}")
    slice_id = _require_non_empty_string(raw_slice.get("id"), path, "slice.id")
    scorecard_name = _require_non_empty_string(raw_slice.get("scorecard_name"), path, f"{slice_id}.scorecard_name")
    section = _require_non_empty_string(raw_slice.get("section"), path, f"{slice_id}.section")
    workstream = _require_non_empty_string(raw_slice.get("workstream"), path, f"{slice_id}.workstream")
    slice_kind = _require_non_empty_string(raw_slice.get("slice_kind"), path, f"{slice_id}.slice_kind")
    title = _require_non_empty_string(raw_slice.get("title"), path, f"{slice_id}.title")
    source_of_truth = str(raw_slice.get("source_of_truth", "ado_primary")).strip().lower()
    if source_of_truth not in _ALLOWED_SOURCE_OF_TRUTH:
        raise ConfigError(f"Unsupported source_of_truth '{source_of_truth}' for slice '{slice_id}' in {path}")
    assignment_mode = str(raw_slice.get("assignment_mode", "auto")).strip().lower()
    if assignment_mode not in _ALLOWED_ASSIGNMENT_MODES:
        raise ConfigError(f"Unsupported assignment_mode '{assignment_mode}' for slice '{slice_id}' in {path}")

    raw_owners = raw_slice.get("owners", {})
    if not isinstance(raw_owners, dict):
        raise ConfigError(f"owners must be a mapping for slice '{slice_id}' in {path}")
    owners = SliceOwners(
        primary=_require_non_empty_string(raw_owners.get("primary"), path, f"{slice_id}.owners.primary"),
        support_tpm=_optional_string(raw_owners.get("support_tpm")),
    )

    raw_source_contract = raw_slice.get("source_contract", {})
    if not isinstance(raw_source_contract, dict):
        raise ConfigError(f"source_contract must be a mapping for slice '{slice_id}' in {path}")
    raw_ado = raw_source_contract.get("ado")
    ado_contract = None
    raw_telemetry = raw_source_contract.get("telemetry")
    telemetry_contract = None
    if raw_telemetry is not None:
        if not isinstance(raw_telemetry, dict):
            raise ConfigError(f"source_contract.telemetry must be a mapping for slice '{slice_id}' in {path}")
        telemetry_contract = SliceTelemetryContract(
            query_id=_require_non_empty_string(raw_telemetry.get("query_id"), path, f"{slice_id}.source_contract.telemetry.query_id"),
            expected_grain=_require_non_empty_string(raw_telemetry.get("expected_grain"), path, f"{slice_id}.source_contract.telemetry.expected_grain"),
            freshness_sla_hours=_require_int(raw_telemetry.get("freshness_sla_hours"), path, f"{slice_id}.source_contract.telemetry.freshness_sla_hours"),
            confidence_threshold=_require_non_empty_string(raw_telemetry.get("confidence_threshold"), path, f"{slice_id}.source_contract.telemetry.confidence_threshold"),
            fallback_behavior=_require_non_empty_string(raw_telemetry.get("fallback_behavior"), path, f"{slice_id}.source_contract.telemetry.fallback_behavior"),
            cross_check_rules=_parse_string_list(raw_telemetry.get("cross_check_rules", []), path, slice_id, "cross_check_rules"),
        )
    if raw_ado is not None:
        if not isinstance(raw_ado, dict):
            raise ConfigError(f"source_contract.ado must be a mapping for slice '{slice_id}' in {path}")
        raw_filters = raw_ado.get("filters")
        filters = _parse_filters(raw_filters, path, slice_id) if raw_filters is not None else None
        raw_tag_expression = raw_ado.get("tag_expression")
        tag_expression = _parse_tag_expression(raw_tag_expression, path, slice_id) if raw_tag_expression is not None else None
        explicit_work_item_ids = _parse_int_list(raw_ado.get("explicit_work_item_ids", []), path, slice_id, "explicit_work_item_ids")
        raw_saved_queries = raw_ado.get("saved_queries", [])
        if not isinstance(raw_saved_queries, list):
            raise ConfigError(f"saved_queries must be a list for slice '{slice_id}' in {path}")
        if schema_version == "1.1":
            saved_query_bindings = tuple(
                _parse_saved_query_binding(entry, path, slice_id, index)
                for index, entry in enumerate(raw_saved_queries, start=1)
            )
            saved_query_ids = tuple(dict.fromkeys(binding.query_id for binding in saved_query_bindings))
        else:
            saved_query_ids = _parse_string_list(raw_saved_queries, path, slice_id, "saved_queries")
            saved_query_bindings = tuple(
                SavedQueryBinding(
                    binding_id=f"legacy-{index:02d}",
                    query_id=query_id,
                    mode="activity_delta",
                    required=True,
                )
                for index, query_id in enumerate(saved_query_ids, start=1)
            )
        ado_contract = SliceAdoSourceContract(
            saved_queries=saved_query_ids,
            saved_query_bindings=saved_query_bindings,
            filters=filters,
            tag_expression=tag_expression,
            explicit_work_item_ids=explicit_work_item_ids,
            required_fields=_parse_string_list(raw_ado.get("required_fields", []), path, slice_id, "required_fields"),
            intentional_filter_only=bool(raw_ado.get("intentional_filter_only", False)),
            intentional_filter_only_expires_on=_parse_optional_date(
                raw_ado.get("intentional_filter_only_expires_on"),
                path,
                f"{slice_id}.source_contract.ado.intentional_filter_only_expires_on",
            ),
        )

    fallback_sources = _parse_string_list(raw_slice.get("fallback_sources", []), path, slice_id, "fallback_sources")
    _ensure_unique_strings(fallback_sources, path, slice_id, "fallback_sources")

    raw_decision_sources = raw_slice.get("decision_sources")
    if raw_source_contract.get("decision_sources") is not None:
        raise ConfigError(
            f"decision_sources must be declared at the slice root for slice '{slice_id}' in {path}"
        )
    if raw_decision_sources is None:
        raw_decision_sources = []

    source_contract = SliceSourceContract(
        ado=ado_contract,
        telemetry=telemetry_contract,
        fallback_sources=fallback_sources,
        decision_sources=_parse_decision_sources(
            raw_decision_sources,
            path,
            slice_id,
            fallback_sources,
        ),
    )
    if source_contract.fallback_sources:
        decision_source_ids = {entry.source_id for entry in source_contract.decision_sources}
        missing_decision_sources = tuple(
            source_id for source_id in source_contract.fallback_sources if source_id not in decision_source_ids
        )
        if missing_decision_sources:
            missing_sources_display = ", ".join(missing_decision_sources)
            raise ConfigError(
                f"Slice '{slice_id}' is missing decision_sources entries for fallback_sources: {missing_sources_display} in {path}"
            )

    raw_freshness = raw_slice.get("freshness", {})
    if not isinstance(raw_freshness, dict):
        raise ConfigError(f"freshness must be a mapping for slice '{slice_id}' in {path}")
    freshness = SliceFreshness(
        warn_days=_require_int(raw_freshness.get("warn_days"), path, f"{slice_id}.freshness.warn_days"),
        block_days=_require_int(raw_freshness.get("block_days"), path, f"{slice_id}.freshness.block_days"),
    )

    raw_degradation = raw_slice.get("degradation", {})
    if not isinstance(raw_degradation, dict):
        raise ConfigError(f"degradation must be a mapping for slice '{slice_id}' in {path}")
    degradation = SliceDegradation(
        blank_filter_is_error=bool(raw_degradation.get("blank_filter_is_error", False)),
        missing_target_date=_optional_string(raw_degradation.get("missing_target_date")),
        stale_owner_comment=_optional_string(raw_degradation.get("stale_owner_comment")),
    )

    raw_remediation = raw_slice.get("remediation", {})
    if raw_remediation is None:
        raw_remediation = {}
    if not isinstance(raw_remediation, dict):
        raise ConfigError(f"remediation must be a mapping for slice '{slice_id}' in {path}")

    return SliceContract(
        id=slice_id,
        scorecard_name=scorecard_name,
        section=section,
        workstream=workstream,
        slice_kind=slice_kind,
        title=title,
            source_of_truth=source_of_truth,
        owners=owners,
        source_contract=source_contract,
        freshness=freshness,
        degradation=degradation,
        remediation_template=_optional_string(raw_remediation.get("ask_template")),
        assignment_mode=assignment_mode,
        required=bool(raw_slice.get("required", True)),
        workstream_id=_optional_string(raw_slice.get("workstream_id")),
    )


def _parse_saved_query_binding(raw: Any, path: Path, slice_id: str, index: int) -> SavedQueryBinding:
    if not isinstance(raw, dict):
        raise ConfigError(
            f"schema 1.1 saved_queries entries must be mappings (binding_id/query_id/mode) for slice '{slice_id}' in {path}"
        )
    binding_id = _require_non_empty_string(
        raw.get("binding_id"), path, f"{slice_id}.source_contract.ado.saved_queries[{index}].binding_id"
    )
    query_id = _require_non_empty_string(
        raw.get("query_id"), path, f"{slice_id}.source_contract.ado.saved_queries[{index}].query_id"
    )
    mode = str(raw.get("mode", "")).strip()
    if mode not in _ALLOWED_QUERY_MODES:
        raise ConfigError(
            f"Unsupported saved query mode '{mode}' for binding '{binding_id}' in slice '{slice_id}' in {path}. "
            f"Allowed modes: {sorted(_ALLOWED_QUERY_MODES)}"
        )
    raw_filter = raw.get("filter")
    binding_filter = _parse_binding_filter(raw_filter, path, slice_id, binding_id) if raw_filter is not None else None
    return SavedQueryBinding(
        binding_id=binding_id,
        query_id=query_id,
        mode=mode,  # type: ignore[arg-type]
        required=bool(raw.get("required", True)),
        workstream_ids=tuple(_parse_string_list(raw.get("workstream_ids", []), path, slice_id, f"saved_queries.{binding_id}.workstream_ids")),
        lane_ids=tuple(_parse_string_list(raw.get("lane_ids", []), path, slice_id, f"saved_queries.{binding_id}.lane_ids")),
        filter=binding_filter,
    )


def _parse_binding_filter(raw_filter: Any, path: Path, slice_id: str, binding_id: str) -> SliceFilterDefinition:
    if not isinstance(raw_filter, dict):
        raise ConfigError(f"saved_queries[{binding_id}].filter must be a mapping for slice '{slice_id}' in {path}")
    all_of = _parse_predicate_list(raw_filter.get("all_of", []), path, slice_id, f"saved_queries.{binding_id}.filter.all_of")
    any_of = _parse_predicate_list(raw_filter.get("any_of", []), path, slice_id, f"saved_queries.{binding_id}.filter.any_of")
    for predicate in (*all_of, *any_of):
        allowed_ops = _CLOSED_BINDING_FILTER_OPS.get(predicate.field)
        if allowed_ops is None:
            raise ConfigError(
                f"Unsupported saved-query binding filter field '{predicate.field}' for binding '{binding_id}' "
                f"in slice '{slice_id}' in {path}. Allowed fields: {sorted(_CLOSED_BINDING_FILTER_OPS)}"
            )
        if predicate.op not in allowed_ops:
            raise ConfigError(
                f"Unsupported operator '{predicate.op}' for filter field '{predicate.field}' on binding "
                f"'{binding_id}' in slice '{slice_id}' in {path}. Allowed operators: {sorted(allowed_ops)}"
            )
    return SliceFilterDefinition(all_of=all_of, any_of=any_of)


def _parse_filters(raw_filters: Any, path: Path, slice_id: str) -> SliceFilterDefinition:
    if not isinstance(raw_filters, dict):
        raise ConfigError(f"source_contract.ado.filters must be a mapping for slice '{slice_id}' in {path}")
    return SliceFilterDefinition(
        all_of=_parse_predicate_list(raw_filters.get("all_of", []), path, slice_id, "all_of"),
        any_of=_parse_predicate_list(raw_filters.get("any_of", []), path, slice_id, "any_of"),
    )


def _parse_decision_sources(
    raw_sources: Any,
    path: Path,
    slice_id: str,
    raw_fallback_sources: Any,
) -> tuple[SliceDecisionSource, ...]:
    if raw_sources is None:
        return ()
    if not isinstance(raw_sources, list):
        raise ConfigError(f"decision_sources must be a list for slice '{slice_id}' in {path}")

    fallback_sources = (
        raw_fallback_sources
        if isinstance(raw_fallback_sources, tuple)
        else _parse_string_list(raw_fallback_sources, path, slice_id, "fallback_sources")
    )
    fallback_source_ids = {value.strip() for value in fallback_sources if value.strip()}

    parsed_sources: list[SliceDecisionSource] = []
    seen_source_ids: set[str] = set()
    for index, raw_source in enumerate(raw_sources, start=1):
        if not isinstance(raw_source, dict):
            raise ConfigError(
                f"decision_sources[{index}] must be a mapping for slice '{slice_id}' in {path}"
            )
        source_id = _require_non_empty_string(
            raw_source.get("source_id"),
            path,
            f"{slice_id}.decision_sources[{index}].source_id",
        )
        if source_id in seen_source_ids:
            raise ConfigError(f"Duplicate decision source '{source_id}' for slice '{slice_id}' in {path}")
        if source_id not in fallback_source_ids:
            raise ConfigError(
                f"Decision source '{source_id}' must also appear in fallback_sources for slice '{slice_id}' in {path}"
            )
        seen_source_ids.add(source_id)
        channels = _parse_string_list(
            raw_source.get("channels", []),
            path,
            slice_id,
            f"decision_sources[{index}].channels",
        )
        if not channels:
            raise ConfigError(
                f"decision_sources[{index}].channels must not be empty for slice '{slice_id}' in {path}"
            )
        _ensure_unique_strings(
            channels,
            path,
            slice_id,
            f"decision_sources[{index}].channels",
        )
        blocked_artifact_ids = _parse_string_list(
            raw_source.get("blocked_artifact_ids", []),
            path,
            slice_id,
            f"decision_sources[{index}].blocked_artifact_ids",
        )
        _ensure_unique_strings(
            blocked_artifact_ids,
            path,
            slice_id,
            f"decision_sources[{index}].blocked_artifact_ids",
        )
        parsed_sources.append(
            SliceDecisionSource(
                source_id=source_id,
                channels=channels,
                blocked_artifact_selectors=_parse_decision_source_selectors(raw_source.get("blocked_artifact_selectors", []), path, slice_id, index),
                blocked_artifact_ids=blocked_artifact_ids,
            )
        )
    return tuple(parsed_sources)


def _parse_decision_source_selectors(
    raw_selectors: Any,
    path: Path,
    slice_id: str,
    source_index: int,
) -> tuple[SliceDecisionSourceSelector, ...]:
    if raw_selectors is None:
        return ()
    if not isinstance(raw_selectors, list):
        raise ConfigError(
            f"decision_sources[{source_index}].blocked_artifact_selectors must be a list for slice '{slice_id}' in {path}"
        )

    selectors: list[SliceDecisionSourceSelector] = []
    seen_selectors: set[tuple[str, str]] = set()
    for selector_index, raw_selector in enumerate(raw_selectors, start=1):
        if not isinstance(raw_selector, dict):
            raise ConfigError(
                f"decision_sources[{source_index}].blocked_artifact_selectors[{selector_index}] must be a mapping for slice '{slice_id}' in {path}"
            )
        selector = SliceDecisionSourceSelector(
            workstream_id=_require_non_empty_string(
                raw_selector.get("workstream_id"),
                path,
                f"{slice_id}.decision_sources[{source_index}].blocked_artifact_selectors[{selector_index}].workstream_id",
            ),
            artifact_type=_require_non_empty_string(
                raw_selector.get("artifact_type"),
                path,
                f"{slice_id}.decision_sources[{source_index}].blocked_artifact_selectors[{selector_index}].artifact_type",
            ),
        )
        selector_key = (selector.workstream_id, selector.artifact_type)
        if selector_key in seen_selectors:
            raise ConfigError(
                f"Duplicate blocked_artifact_selector '{selector.workstream_id}:{selector.artifact_type}' "
                f"for decision source [{source_index}] in slice '{slice_id}' in {path}"
            )
        seen_selectors.add(selector_key)
        selectors.append(selector)
    return tuple(selectors)


def _ensure_unique_strings(values: tuple[str, ...], path: Path, slice_id: str, field_name: str) -> None:
    seen_values: set[str] = set()
    for value in values:
        if value in seen_values:
            raise ConfigError(f"Duplicate value '{value}' in {field_name} for slice '{slice_id}' in {path}")
        seen_values.add(value)


def _parse_tag_expression(raw_expression: Any, path: Path, slice_id: str) -> TagExpression:
    if not isinstance(raw_expression, dict):
        raise ConfigError(f"source_contract.ado.tag_expression must be a mapping for slice '{slice_id}' in {path}")
    expression = TagExpression(
        all_of=_parse_string_list(raw_expression.get("all_of", []), path, slice_id, "tag_expression.all_of"),
        any_of=_parse_string_list(raw_expression.get("any_of", []), path, slice_id, "tag_expression.any_of"),
    )
    if expression.is_empty():
        raise ConfigError(f"source_contract.ado.tag_expression must include all_of or any_of for slice '{slice_id}' in {path}")
    return expression


def _parse_predicate_list(
    raw_predicates: Any,
    path: Path,
    slice_id: str,
    field_name: str,
) -> tuple[SlicePredicateDefinition, ...]:
    if not isinstance(raw_predicates, list):
        raise ConfigError(f"{field_name} must be a list for slice '{slice_id}' in {path}")
    predicates: list[SlicePredicateDefinition] = []
    for index, raw_predicate in enumerate(raw_predicates):
        if not isinstance(raw_predicate, dict):
            raise ConfigError(f"{field_name}[{index}] must be a mapping for slice '{slice_id}' in {path}")
        predicates.append(
            SlicePredicateDefinition(
                field=_require_non_empty_string(raw_predicate.get("field"), path, f"{slice_id}.{field_name}[{index}].field"),
                op=_require_non_empty_string(raw_predicate.get("op"), path, f"{slice_id}.{field_name}[{index}].op"),
                value=_require_non_empty_string(raw_predicate.get("value"), path, f"{slice_id}.{field_name}[{index}].value"),
            )
        )
    return tuple(predicates)


def _parse_string_list(raw_values: Any, path: Path, slice_id: str, field_name: str) -> tuple[str, ...]:
    if not isinstance(raw_values, list):
        raise ConfigError(f"{field_name} must be a list for slice '{slice_id}' in {path}")
    values = []
    for index, raw_value in enumerate(raw_values):
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise ConfigError(f"{field_name}[{index}] must be a non-empty string for slice '{slice_id}' in {path}")
        values.append(raw_value.strip())
    return tuple(values)


def _parse_int_list(raw_values: Any, path: Path, slice_id: str, field_name: str) -> tuple[int, ...]:
    if not isinstance(raw_values, list):
        raise ConfigError(f"{field_name} must be a list for slice '{slice_id}' in {path}")
    values = []
    for index, raw_value in enumerate(raw_values):
        if not isinstance(raw_value, int):
            raise ConfigError(f"{field_name}[{index}] must be an integer for slice '{slice_id}' in {path}")
        values.append(raw_value)
    return tuple(values)


def _validate_unique_contracts(contracts: tuple[SliceContract, ...], path: Path) -> None:
    ids = set()
    lookup_keys = set()
    for contract in contracts:
        if contract.id in ids:
            raise ConfigError(f"Duplicate slice id '{contract.id}' in {path}")
        if contract.lookup_key in lookup_keys:
            scorecard_name, title = contract.lookup_key
            raise ConfigError(f"Duplicate slice contract for '{scorecard_name} / {title}' in {path}")
        ids.add(contract.id)
        lookup_keys.add(contract.lookup_key)


def _require_non_empty_string(raw_value: Any, path: Path, field_name: str) -> str:
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ConfigError(f"{field_name} must be a non-empty string in {path}")
    return raw_value.strip()


def _require_int(raw_value: Any, path: Path, field_name: str) -> int:
    if not isinstance(raw_value, int):
        raise ConfigError(f"{field_name} must be an integer in {path}")
    return raw_value


def _optional_string(raw_value: Any) -> str | None:
    if not isinstance(raw_value, str):
        return None
    value = raw_value.strip()
    return value or None


def _parse_optional_date(raw_value: Any, path: Path, field_name: str) -> date | None:
    if raw_value is None:
        return None
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ConfigError(f"{field_name} must be an ISO date string in {path}")
    try:
        return date.fromisoformat(raw_value.strip())
    except ValueError as error:
        raise ConfigError(f"{field_name} must be an ISO date string in {path}") from error
