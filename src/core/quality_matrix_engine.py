from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from src.core.config_loader import KustoQuerySettings, ReportBundle, ScorecardDimensionSettings
from src.core.kusto_rendering import TelemetryObservation
from src.core.models import Comment, Revision, WorkItem
from src.core.scorecard_engine import assign_dimension_items
from src.core.slice_contract_loader import SliceAdoSourceContract, SliceContract


@dataclass(frozen=True, slots=True)
class SliceQualityCheck:
    name: str
    outcome: str
    detail: str


@dataclass(frozen=True, slots=True)
class ContinuityAssessment:
    baseline_available: bool
    previous_issue_number: int | None
    state: str
    statement: str


@dataclass(frozen=True, slots=True)
class SectionQualityRecord:
    section_id: str
    quality_state: str
    status: str
    slice_ids: tuple[str, ...]
    issues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SliceTelemetryRecord:
    query_id: str
    cluster: str
    database: str
    expected_grain: str
    freshness_sla_hours: int
    confidence_threshold: str
    fallback_behavior: str
    cross_check_rules: tuple[str, ...]
    validates_slice: bool
    status: str
    query_success: bool | None
    last_successful_fetch_at: datetime | None
    confidence: str | None
    contradiction: str | None


@dataclass(frozen=True, slots=True)
class SliceQualityRecord:
    slice_id: str
    scorecard_name: str
    title: str
    section: str
    workstream: str
    primary_owner: str
    support_tpm: str | None
    source_of_truth: str
    assignment_mode: str
    quality_state: str
    status: str
    newsletter_surface: str
    assigned_item_count: int
    assigned_item_ids: tuple[int, ...]
    saved_queries: tuple[str, ...]
    ado_query_url: str
    required_fields: tuple[str, ...]
    missing_fields: dict[str, tuple[int, ...]]
    stale_item_ids: tuple[int, ...]
    failing_conditions: tuple[str, ...]
    issues: tuple[str, ...]
    checks: tuple[SliceQualityCheck, ...]
    telemetry: SliceTelemetryRecord | None
    remediation_template: str | None


@dataclass(frozen=True, slots=True)
class QualityMatrix:
    schema_version: str
    edition: str
    issue_number: int
    generated_at: datetime
    continuity: ContinuityAssessment
    summary: dict[str, int]
    sections: tuple[SectionQualityRecord, ...]
    slices: tuple[SliceQualityRecord, ...]


@dataclass(frozen=True, slots=True)
class SliceContractValidationSummary:
    slice_count: int
    warning_count: int
    failure_count: int
    warnings: tuple[str, ...]
    failures: tuple[str, ...]


_QUALITY_TO_STATUS = {
    "healthy": "green",
    "degraded": "yellow",
    "stale": "yellow",
    "under_specified": "red",
    "manual_only": "manual_only",
}
_SECTION_RANK = {"healthy": 0, "manual_only": 1, "degraded": 2, "stale": 3, "under_specified": 4}


def build_quality_matrix(
    *,
    bundle: ReportBundle,
    issue_number: int,
    generated_at: datetime,
    current_items: tuple[WorkItem, ...],
    previous_issue_number: int | None,
    telemetry_observations: tuple[TelemetryObservation, ...] | None = None,
    ado_query_base_url: str = "https://dev.azure.com/query",
    kpi_queries: tuple[Any, ...] | None = None,
) -> QualityMatrix:
    slice_contracts = bundle.slice_contracts or ()
    dimension_lookup = _dimension_lookup(bundle)
    kusto_query_lookup: dict[str, Any] = {query.id: query for query in bundle.config.kusto.queries}
    if kpi_queries is not None:
        for q in kpi_queries:
            if q.id not in kusto_query_lookup:
                kusto_query_lookup[q.id] = q
    telemetry_lookup = (
        {observation.query_id: observation for observation in telemetry_observations}
        if telemetry_observations is not None
        else None
    )
    slice_rows = tuple(
        _evaluate_slice(
            contract=contract,
            dimension=dimension_lookup.get(contract.lookup_key),
            items=current_items,
            generated_at=generated_at,
            kusto_query_lookup=kusto_query_lookup,
            telemetry_lookup=telemetry_lookup,
            ado_query_base_url=ado_query_base_url,
        )
        for contract in slice_contracts
    )
    summary = {
        key: int(value)
        for key, value in Counter(row.quality_state for row in slice_rows).items()
    }
    for quality_state in ("healthy", "degraded", "stale", "under_specified", "manual_only"):
        summary.setdefault(quality_state, 0)
    return QualityMatrix(
        schema_version="1.0",
        edition=bundle.config.edition.name,
        issue_number=issue_number,
        generated_at=generated_at,
        continuity=_build_continuity(previous_issue_number),
        summary=summary,
        sections=_build_section_rollups(slice_rows),
        slices=slice_rows,
    )


def render_quality_matrix_markdown(matrix: QualityMatrix) -> str:
    lines = [
        f"# Quality Matrix — {matrix.edition} Issue {matrix.issue_number:03d}",
        "",
        f"Generated: {matrix.generated_at.isoformat()}",
        f"Continuation: {matrix.continuity.statement}",
        "",
        "## Summary",
        f"- Healthy: {matrix.summary['healthy']}",
        f"- Degraded: {matrix.summary['degraded']}",
        f"- Stale: {matrix.summary['stale']}",
        f"- Under-specified: {matrix.summary['under_specified']}",
        f"- Manual-only: {matrix.summary['manual_only']}",
        "",
        "## Section Quality",
        "",
        "| Section | State | Status | Notes |",
        "| --- | --- | --- | --- |",
    ]
    for section in matrix.sections:
        notes = "; ".join(section.issues) if section.issues else "No issues detected."
        lines.append(f"| {section.section_id} | {section.quality_state} | {section.status} | {notes} |")

    lines.extend(
        [
            "",
            "## Slice Quality",
            "",
            "| Slice | Source | Telemetry | State | Status | Owner | Items | Notes |",
            "| --- | --- | --- | --- | --- | --- | ---: | --- |",
        ]
    )
    for slice_row in matrix.slices:
        notes = "; ".join(slice_row.issues) if slice_row.issues else "No issues detected."
        telemetry_status = slice_row.telemetry.status if slice_row.telemetry is not None else "n/a"
        lines.append(
            "| "
            f"{slice_row.slice_id} ({slice_row.title}) | {slice_row.source_of_truth} | {telemetry_status} | "
            f"{slice_row.quality_state} | {slice_row.status} | {slice_row.primary_owner} | {slice_row.assigned_item_count} | {notes} |"
        )

    return "\n".join(lines) + "\n"


def validate_slice_contracts(slice_contracts: tuple[SliceContract, ...] | None) -> SliceContractValidationSummary:
    if not slice_contracts:
        return SliceContractValidationSummary(
            slice_count=0,
            warning_count=0,
            failure_count=1,
            warnings=(),
            failures=("slice_contracts.yaml is missing or empty.",),
        )

    warnings: list[str] = []
    failures: list[str] = []
    for contract in slice_contracts:
        failures.extend(_validate_decision_source_contract(contract))
        ado_contract = contract.source_contract.ado
        if contract.freshness.warn_days > contract.freshness.block_days:
            failures.append(
                f"{contract.id}: freshness.warn_days exceeds freshness.block_days."
            )
        if contract.assignment_mode == "manual_only":
            if ado_contract is None or not ado_contract.explicit_work_item_ids:
                warnings.append(
                    f"{contract.id}: manual_only slice has no explicit_work_item_ids yet."
                )
            continue
        if contract.source_of_truth in {"telemetry_primary", "hybrid"}:
            telemetry_contract = contract.source_contract.telemetry
            if telemetry_contract is None:
                failures.append(f"{contract.id}: {contract.source_of_truth} slice is missing source_contract.telemetry.")
            elif not telemetry_contract.cross_check_rules:
                warnings.append(f"{contract.id}: telemetry-backed slice has no cross_check_rules.")
        if ado_contract is None:
            failures.append(f"{contract.id}: auto slice is missing source_contract.ado.")
            continue
        if _ado_contract_is_blank(ado_contract):
            failures.append(f"{contract.id}: auto slice has no filters or explicit work item ids.")
        elif _ado_contract_is_filter_only(ado_contract):
            if ado_contract.intentional_filter_only:
                expires_on = ado_contract.intentional_filter_only_expires_on
                if expires_on is None:
                    warnings.append(
                        f"{contract.id}: filter-only waiver is missing intentional_filter_only_expires_on."
                    )
                elif expires_on < date.today():
                    warnings.append(
                        f"{contract.id}: filter-only waiver expired on {expires_on.isoformat()}; add saved_queries or explicit_work_item_ids, or renew the waiver."
                    )
                # else: waiver is current — suppress warning
            else:
                warnings.append(
                    f"{contract.id}: ADO contract is filter-only; add saved_queries or explicit_work_item_ids, or keep this warning until the external Analytics source contract is corrected."
                )
        if not ado_contract.required_fields:
            warnings.append(f"{contract.id}: required_fields is empty.")
        if contract.remediation_template is None:
            warnings.append(f"{contract.id}: remediation.ask_template is missing.")

    return SliceContractValidationSummary(
        slice_count=len(slice_contracts),
        warning_count=len(warnings),
        failure_count=len(failures),
        warnings=tuple(warnings),
        failures=tuple(failures),
    )


def _validate_decision_source_contract(contract: SliceContract) -> list[str]:
    failures: list[str] = []
    fallback_sources = contract.source_contract.fallback_sources
    decision_sources = contract.source_contract.decision_sources

    blank_fallback_sources = tuple(source_id for source_id in fallback_sources if not source_id.strip())
    if blank_fallback_sources:
        failures.append(f"{contract.id}: fallback_sources contains blank source IDs.")

    normalized_fallback_sources = tuple(source_id for source_id in fallback_sources if source_id.strip())

    if len(set(fallback_sources)) != len(fallback_sources):
        failures.append(f"{contract.id}: fallback_sources contains duplicates.")

    decision_source_ids = tuple(entry.source_id for entry in decision_sources)
    non_blank_decision_source_ids = tuple(source_id for source_id in decision_source_ids if source_id.strip())
    if len(set(decision_source_ids)) != len(decision_source_ids):
        failures.append(f"{contract.id}: decision_sources contains duplicate source_id values.")

    fallback_source_ids = set(normalized_fallback_sources)
    decision_source_id_set = set(non_blank_decision_source_ids)
    if fallback_source_ids:
        missing_source_ids = tuple(source_id for source_id in normalized_fallback_sources if source_id not in decision_source_id_set)
        if missing_source_ids:
            failures.append(
                f"{contract.id}: decision_sources is missing fallback_sources bindings for {', '.join(missing_source_ids)}."
            )
    extra_source_ids = tuple(source_id for source_id in non_blank_decision_source_ids if source_id not in fallback_source_ids)
    if extra_source_ids:
        failures.append(
            f"{contract.id}: decision_sources contains source_ids outside fallback_sources: {', '.join(extra_source_ids)}."
        )

    for decision_source in decision_sources:
        if not decision_source.source_id.strip():
            failures.append(f"{contract.id}: decision_sources contains a blank source_id.")

        if not decision_source.channels:
            failures.append(
                f"{contract.id}: decision source '{decision_source.source_id}' must declare at least one channel."
            )
        elif any(not channel.strip() for channel in decision_source.channels):
            failures.append(
                f"{contract.id}: decision source '{decision_source.source_id}' contains blank channel values."
            )
        elif len(set(decision_source.channels)) != len(decision_source.channels):
            failures.append(
                f"{contract.id}: decision source '{decision_source.source_id}' contains duplicate channels."
            )

        if any(not artifact_id.strip() for artifact_id in decision_source.blocked_artifact_ids):
            failures.append(
                f"{contract.id}: decision source '{decision_source.source_id}' contains blank blocked_artifact_ids."
            )
        if len(set(decision_source.blocked_artifact_ids)) != len(decision_source.blocked_artifact_ids):
            failures.append(
                f"{contract.id}: decision source '{decision_source.source_id}' contains duplicate blocked_artifact_ids."
            )

        blank_selectors = tuple(
            selector
            for selector in decision_source.blocked_artifact_selectors
            if not selector.workstream_id.strip() or not selector.artifact_type.strip()
        )
        if blank_selectors:
            failures.append(
                f"{contract.id}: decision source '{decision_source.source_id}' contains blank blocked_artifact_selectors."
            )

        selector_keys = tuple(
            (selector.workstream_id, selector.artifact_type)
            for selector in decision_source.blocked_artifact_selectors
            if selector.workstream_id.strip() and selector.artifact_type.strip()
        )
        if len(set(selector_keys)) != len(selector_keys):
            failures.append(
                f"{contract.id}: decision source '{decision_source.source_id}' contains duplicate blocked_artifact_selectors."
            )

    return failures


def _build_continuity(previous_issue_number: int | None) -> ContinuityAssessment:
    if previous_issue_number is None:
        return ContinuityAssessment(
            baseline_available=False,
            previous_issue_number=None,
            state="baseline_missing",
            statement="No usable continuity baseline is available; delta language is suppressed.",
        )
    return ContinuityAssessment(
        baseline_available=True,
        previous_issue_number=previous_issue_number,
        state="continuation_ready",
        statement=f"Comparing against confirmed issue {previous_issue_number:03d}.",
    )


def _dimension_lookup(bundle: ReportBundle) -> dict[tuple[str, str], ScorecardDimensionSettings]:
    lookup: dict[tuple[str, str], ScorecardDimensionSettings] = {}
    for scorecard in bundle.config.scorecards:
        for dimension in scorecard.dimensions:
            lookup[(scorecard.name, dimension.name)] = dimension
    return lookup


def _evaluate_slice(
    *,
    contract: SliceContract,
    dimension: ScorecardDimensionSettings | None,
    items: tuple[WorkItem, ...],
    generated_at: datetime,
    kusto_query_lookup: dict[str, KustoQuerySettings],
    telemetry_lookup: dict[str, TelemetryObservation] | None,
    ado_query_base_url: str,
) -> SliceQualityRecord:
    checks: list[SliceQualityCheck] = []
    issues: list[str] = []
    failing_conditions: list[str] = []
    assigned_items: tuple[WorkItem, ...] = ()
    ado_query_url = ""

    if dimension is None:
        checks.append(SliceQualityCheck("dimension", "fail", "Config dimension is missing for this slice."))
        issues.append("Config dimension is missing for this slice contract.")
        failing_conditions.append("dimension_missing")
        return _build_slice_record(
            contract=contract,
            quality_state="under_specified",
            checks=tuple(checks),
            issues=tuple(issues),
            failing_conditions=tuple(failing_conditions),
            assigned_items=assigned_items,
            ado_query_url=ado_query_url,
            missing_fields={},
            stale_item_ids=(),
            telemetry=None,
        )

    checks.append(SliceQualityCheck("source_contract", "pass", "Slice contract loaded and mapped to a scorecard dimension."))
    try:
        assignment = assign_dimension_items(
            items,
            dimension,
            slice_contract=contract,
            ado_query_base_url=ado_query_base_url,
        )
        assigned_items = assignment.items
        ado_query_url = assignment.ado_query_url
        checks.append(
            SliceQualityCheck(
                "assignment",
                "pass" if assigned_items or contract.assignment_mode == "manual_only" else "warn",
                (
                    f"Matched {len(assigned_items)} item(s)."
                    if assigned_items
                    else "No items matched the current slice contract."
                ),
            )
        )
    except ValueError as error:
        checks.append(SliceQualityCheck("assignment", "fail", str(error)))
        issues.append(str(error))
        failing_conditions.append("assignment_contract_invalid")
        return _build_slice_record(
            contract=contract,
            quality_state="under_specified",
            checks=tuple(checks),
            issues=tuple(issues),
            failing_conditions=tuple(failing_conditions),
            assigned_items=assigned_items,
            ado_query_url=ado_query_url,
            missing_fields={},
            stale_item_ids=(),
            telemetry=None,
        )

    missing_fields = _collect_missing_fields(contract, assigned_items)
    if missing_fields:
        checks.append(
            SliceQualityCheck(
                "required_fields",
                "fail",
                _format_missing_field_detail(missing_fields),
            )
        )
        issues.append(_format_missing_field_issue(missing_fields))
        for field_name in missing_fields:
            failing_conditions.append(f"missing_{field_name}")
    else:
        checks.append(SliceQualityCheck("required_fields", "pass", "All required fields are present on assigned items."))

    stale_warn_ids, stale_block_ids = _stale_item_ids(contract, assigned_items, generated_at)
    if stale_block_ids:
        checks.append(
            SliceQualityCheck(
                "freshness",
                "fail",
                f"Items {', '.join(str(item_id) for item_id in stale_block_ids)} exceed the slice block SLA.",
            )
        )
        issues.append(
            f"Items {', '.join(str(item_id) for item_id in stale_block_ids)} exceed the slice block freshness SLA."
        )
        failing_conditions.append("stale_input_block")
    elif stale_warn_ids:
        checks.append(
            SliceQualityCheck(
                "freshness",
                "warn",
                f"Items {', '.join(str(item_id) for item_id in stale_warn_ids)} exceed the slice warn SLA.",
            )
        )
        issues.append(
            f"Items {', '.join(str(item_id) for item_id in stale_warn_ids)} exceed the slice warn freshness SLA."
        )
        failing_conditions.append("stale_input_warn")
    else:
        checks.append(SliceQualityCheck("freshness", "pass", "Assigned items are within the slice freshness SLA."))

    checks.append(
        SliceQualityCheck(
            "owner",
            "pass",
            f"Primary owner: {contract.owners.primary}" + (
                f"; support TPM: {contract.owners.support_tpm}" if contract.owners.support_tpm else ""
            ),
        )
    )
    telemetry, telemetry_check, telemetry_issues, telemetry_conditions = _evaluate_telemetry(
        contract=contract,
        assigned_items=assigned_items,
        kusto_query_lookup=kusto_query_lookup,
        telemetry_lookup=telemetry_lookup,
    )
    checks.append(telemetry_check)
    issues.extend(telemetry_issues)
    failing_conditions.extend(telemetry_conditions)

    if contract.assignment_mode == "manual_only" or contract.source_of_truth == "manual_only":
        quality_state = "manual_only"
    elif missing_fields:
        quality_state = "under_specified"
    elif stale_block_ids or stale_warn_ids:
        quality_state = "stale"
    elif not assigned_items:
        issues.append("No items matched the current slice contract; verify the filter or record that the slice is clear.")
        failing_conditions.append("assignment_empty")
        quality_state = "degraded"
    else:
        quality_state = "healthy"
    quality_state = _apply_telemetry_quality_state(contract, quality_state, telemetry)

    return _build_slice_record(
        contract=contract,
        quality_state=quality_state,
        checks=tuple(checks),
        issues=tuple(dict.fromkeys(issues)),
        failing_conditions=tuple(dict.fromkeys(failing_conditions)),
        assigned_items=assigned_items,
        ado_query_url=ado_query_url,
        missing_fields=missing_fields,
        stale_item_ids=tuple(sorted(set(stale_warn_ids + stale_block_ids))),
        telemetry=telemetry,
    )


def _build_slice_record(
    *,
    contract: SliceContract,
    quality_state: str,
    checks: tuple[SliceQualityCheck, ...],
    issues: tuple[str, ...],
    failing_conditions: tuple[str, ...],
    assigned_items: tuple[WorkItem, ...],
    ado_query_url: str,
    missing_fields: dict[str, tuple[int, ...]],
    stale_item_ids: tuple[int, ...],
    telemetry: SliceTelemetryRecord | None,
) -> SliceQualityRecord:
    ado_contract = contract.source_contract.ado
    return SliceQualityRecord(
        slice_id=contract.id,
        scorecard_name=contract.scorecard_name,
        title=contract.title,
        section=contract.section,
        workstream=contract.workstream,
        primary_owner=contract.owners.primary,
        support_tpm=contract.owners.support_tpm,
        source_of_truth=contract.source_of_truth,
        assignment_mode=contract.assignment_mode,
        quality_state=quality_state,
        status=_QUALITY_TO_STATUS[quality_state] if quality_state != "stale" or not stale_item_ids else _stale_status(missing_fields, stale_item_ids, checks),
        newsletter_surface=f"{contract.scorecard_name} / {contract.title}",
        assigned_item_count=len(assigned_items),
        assigned_item_ids=tuple(sorted(item.id for item in assigned_items)),
        saved_queries=ado_contract.saved_queries if ado_contract is not None else (),
        ado_query_url=ado_query_url,
        required_fields=ado_contract.required_fields if ado_contract is not None else (),
        missing_fields=missing_fields,
        stale_item_ids=stale_item_ids,
        failing_conditions=failing_conditions,
        issues=issues,
        checks=checks,
        telemetry=telemetry,
        remediation_template=contract.remediation_template,
    )


def _evaluate_telemetry(
    *,
    contract: SliceContract,
    assigned_items: tuple[WorkItem, ...],
    kusto_query_lookup: dict[str, KustoQuerySettings],
    telemetry_lookup: dict[str, TelemetryObservation] | None,
) -> tuple[SliceTelemetryRecord | None, SliceQualityCheck, tuple[str, ...], tuple[str, ...]]:
    telemetry_contract = contract.source_contract.telemetry
    if telemetry_contract is None:
        return None, SliceQualityCheck("telemetry", "skip", "No telemetry contract for this slice."), (), ()

    query = kusto_query_lookup.get(telemetry_contract.query_id)
    if query is None:
        detail = f"Telemetry query {telemetry_contract.query_id} is missing from the edition config."
        return (
            SliceTelemetryRecord(
                query_id=telemetry_contract.query_id,
                cluster="",
                database="",
                expected_grain=telemetry_contract.expected_grain,
                freshness_sla_hours=telemetry_contract.freshness_sla_hours,
                confidence_threshold=telemetry_contract.confidence_threshold,
                fallback_behavior=telemetry_contract.fallback_behavior,
                cross_check_rules=telemetry_contract.cross_check_rules,
                validates_slice=False,
                status="absent",
                query_success=None,
                last_successful_fetch_at=None,
                confidence=None,
                contradiction=detail,
            ),
            SliceQualityCheck("telemetry", "fail", detail),
            (detail,),
            ("telemetry_query_missing",),
        )

    if telemetry_lookup is None:
        return (
            SliceTelemetryRecord(
                query_id=query.id,
                cluster=query.cluster,
                database=query.database,
                expected_grain=telemetry_contract.expected_grain,
                freshness_sla_hours=telemetry_contract.freshness_sla_hours,
                confidence_threshold=telemetry_contract.confidence_threshold,
                fallback_behavior=telemetry_contract.fallback_behavior,
                cross_check_rules=telemetry_contract.cross_check_rules,
                validates_slice=query.kusto_section_validates_slice,
                status="not_evaluated",
                query_success=None,
                last_successful_fetch_at=None,
                confidence=query.confidence,
                contradiction=None,
            ),
            SliceQualityCheck("telemetry", "skip", "Telemetry was not evaluated by this command."),
            (),
            (),
        )

    observation = telemetry_lookup.get(query.id)
    if observation is None:
        detail = f"Telemetry query {query.id} was not executed for slice {contract.id}."
        outcome = "warn" if contract.source_of_truth == "hybrid" else "fail"
        return (
            _telemetry_record(
                contract=telemetry_contract,
                query=query,
                status="absent",
                query_success=None,
                last_successful_fetch_at=None,
                confidence=query.confidence,
                contradiction=None,
            ),
            SliceQualityCheck("telemetry", outcome, detail),
            (detail,),
            ("telemetry_absent",),
        )

    if observation.execution_state == "degraded":
        detail = observation.message or f"Telemetry query {query.id} degraded during execution."
        outcome = "warn" if contract.source_of_truth == "hybrid" else "fail"
        return (
            _telemetry_record(
                contract=telemetry_contract,
                query=query,
                status="degraded",
                query_success=False,
                last_successful_fetch_at=observation.last_successful_fetch_at,
                confidence=observation.confidence,
                contradiction=None,
            ),
            SliceQualityCheck("telemetry", outcome, detail),
            (detail,),
            ("telemetry_degraded",),
        )

    if observation.execution_state == "empty":
        detail = f"Telemetry query {query.id} returned no rows for this slice."
        outcome = "warn" if contract.source_of_truth == "hybrid" else "fail"
        return (
            _telemetry_record(
                contract=telemetry_contract,
                query=query,
                status="absent",
                query_success=True,
                last_successful_fetch_at=observation.last_successful_fetch_at,
                confidence=observation.confidence,
                contradiction=None,
            ),
            SliceQualityCheck("telemetry", outcome, detail),
            (detail,),
            ("telemetry_absent",),
        )

    contradiction = _detect_telemetry_contradiction(
        cross_check_rules=telemetry_contract.cross_check_rules,
        assigned_items=assigned_items,
    )
    confidence_rank = _confidence_rank(observation.confidence)
    threshold_rank = _confidence_rank(telemetry_contract.confidence_threshold)
    if confidence_rank < threshold_rank:
        detail = (
            f"Telemetry query {query.id} is configured at {observation.confidence} confidence, "
            f"below the slice threshold of {telemetry_contract.confidence_threshold}."
        )
        return (
            _telemetry_record(
                contract=telemetry_contract,
                query=query,
                status="degraded",
                query_success=True,
                last_successful_fetch_at=observation.last_successful_fetch_at,
                confidence=observation.confidence,
                contradiction=None,
            ),
            SliceQualityCheck("telemetry", "warn", detail),
            (detail,),
            ("telemetry_confidence_below_threshold",),
        )
    if contradiction is not None:
        return (
            _telemetry_record(
                contract=telemetry_contract,
                query=query,
                status="degraded",
                query_success=True,
                last_successful_fetch_at=observation.last_successful_fetch_at,
                confidence=observation.confidence,
                contradiction=contradiction,
            ),
            SliceQualityCheck("telemetry", "warn", contradiction),
            (contradiction,),
            ("telemetry_contradiction",),
        )

    return (
        _telemetry_record(
            contract=telemetry_contract,
            query=query,
            status="supporting",
            query_success=True,
            last_successful_fetch_at=observation.last_successful_fetch_at,
            confidence=observation.confidence,
            contradiction=None,
        ),
        SliceQualityCheck("telemetry", "pass", f"Telemetry query {query.id} is supporting this slice."),
        (),
        (),
    )


def _telemetry_record(
    *,
    contract: Any,
    query: KustoQuerySettings,
    status: str,
    query_success: bool | None,
    last_successful_fetch_at: datetime | None,
    confidence: str | None,
    contradiction: str | None,
) -> SliceTelemetryRecord:
    return SliceTelemetryRecord(
        query_id=query.id,
        cluster=query.cluster,
        database=query.database,
        expected_grain=contract.expected_grain,
        freshness_sla_hours=contract.freshness_sla_hours,
        confidence_threshold=contract.confidence_threshold,
        fallback_behavior=contract.fallback_behavior,
        cross_check_rules=contract.cross_check_rules,
        validates_slice=query.kusto_section_validates_slice,
        status=status,
        query_success=query_success,
        last_successful_fetch_at=last_successful_fetch_at,
        confidence=confidence,
        contradiction=contradiction,
    )


def _apply_telemetry_quality_state(
    contract: SliceContract,
    quality_state: str,
    telemetry: SliceTelemetryRecord | None,
) -> str:
    if telemetry is None or telemetry.status in {"supporting", "not_evaluated"}:
        return quality_state
    if contract.source_of_truth == "telemetry_primary" and quality_state not in {"under_specified", "stale", "manual_only"}:
        return "under_specified"
    if contract.source_of_truth == "hybrid" and quality_state == "healthy":
        return "degraded"
    return quality_state


def _detect_telemetry_contradiction(
    *,
    cross_check_rules: tuple[str, ...],
    assigned_items: tuple[WorkItem, ...],
) -> str | None:
    for rule in cross_check_rules:
        if rule == "ado_assignment_non_empty" and not assigned_items:
            return "Telemetry succeeded but the linked ADO slice has no assigned work items."
    return None


def _confidence_rank(value: str | None) -> int:
    ranks = {"high": 3, "medium": 2, "low": 1}
    if value is None:
        return 0
    return ranks.get(value.strip().lower(), 0)


def _stale_status(
    missing_fields: dict[str, tuple[int, ...]],
    stale_item_ids: tuple[int, ...],
    checks: tuple[SliceQualityCheck, ...],
) -> str:
    del missing_fields, stale_item_ids
    freshness_check = next((check for check in checks if check.name == "freshness"), None)
    if freshness_check is not None and freshness_check.outcome == "fail":
        return "red"
    return "yellow"


def _build_section_rollups(slice_rows: tuple[SliceQualityRecord, ...]) -> tuple[SectionQualityRecord, ...]:
    grouped: dict[str, list[SliceQualityRecord]] = defaultdict(list)
    for row in slice_rows:
        grouped[row.section].append(row)

    sections: list[SectionQualityRecord] = []
    for section_id in sorted(grouped):
        rows = grouped[section_id]
        quality_state = max((row.quality_state for row in rows), key=lambda state: _SECTION_RANK[state])
        issues: list[str] = []
        for row in rows:
            if row.issues:
                issues.append(f"{row.title}: {row.issues[0]}")
        sections.append(
            SectionQualityRecord(
                section_id=section_id,
                quality_state=quality_state,
                status=_QUALITY_TO_STATUS[quality_state],
                slice_ids=tuple(row.slice_id for row in rows),
                issues=tuple(issues),
            )
        )
    return tuple(sections)


def _collect_missing_fields(
    contract: SliceContract,
    assigned_items: tuple[WorkItem, ...],
) -> dict[str, tuple[int, ...]]:
    ado_contract = contract.source_contract.ado
    if ado_contract is None or not assigned_items:
        return {}

    missing: dict[str, list[int]] = defaultdict(list)
    for item in assigned_items:
        for field_name in ado_contract.required_fields:
            if _is_missing_required_field(item, field_name):
                missing[field_name].append(item.id)
    return {field_name: tuple(item_ids) for field_name, item_ids in sorted(missing.items())}


def _is_missing_required_field(item: WorkItem, field_name: str) -> bool:
    normalized = field_name.strip().lower()
    if normalized == "state":
        return not item.state.strip()
    if normalized == "assigned_to":
        return not (item.assigned_to or "").strip()
    if normalized == "target_date":
        return item.target_date is None
    if normalized == "changed_date":
        return _last_relevant_activity(item) is None
    if normalized == "title":
        return not item.title.strip()
    if normalized == "type":
        return not item.type.strip()
    if normalized == "area_path":
        return not item.area_path.strip()
    return False


def _stale_item_ids(
    contract: SliceContract,
    assigned_items: tuple[WorkItem, ...],
    generated_at: datetime,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    warn_ids: list[int] = []
    block_ids: list[int] = []
    for item in assigned_items:
        last_activity = _last_relevant_activity(item)
        if last_activity is None:
            continue
        stale_days = (generated_at - last_activity).days
        if stale_days >= contract.freshness.block_days:
            block_ids.append(item.id)
            continue
        if stale_days >= contract.freshness.warn_days:
            warn_ids.append(item.id)
    return (tuple(sorted(warn_ids)), tuple(sorted(block_ids)))


def _last_relevant_activity(item: WorkItem) -> datetime | None:
    candidates: list[datetime] = []
    candidates.extend(_normalize_datetime(revision.changed_date) for revision in item.revisions)
    candidates.extend(_normalize_datetime(comment.created_date) for comment in item.comments)
    changed_date = _datetime_from_custom_fields(item.custom_fields, ("changed_date", "changed_at", "System.ChangedDate"))
    if changed_date is not None:
        candidates.append(changed_date)
    return max(candidates) if candidates else None


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _datetime_from_custom_fields(payload: dict[str, object], candidate_keys: tuple[str, ...]) -> datetime | None:
    for key in candidate_keys:
        raw_value = payload.get(key)
        parsed = _parse_datetime(raw_value)
        if parsed is not None:
            return parsed
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _normalize_datetime(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _normalize_datetime(parsed)


def _format_missing_field_detail(missing_fields: dict[str, tuple[int, ...]]) -> str:
    return "; ".join(
        f"{field_name}: {', '.join(str(item_id) for item_id in item_ids)}"
        for field_name, item_ids in missing_fields.items()
    )


def _format_missing_field_issue(missing_fields: dict[str, tuple[int, ...]]) -> str:
    field_list = ", ".join(sorted(missing_fields))
    return f"Required fields are missing on assigned items ({field_list})."


def _ado_contract_is_blank(ado_contract: SliceAdoSourceContract) -> bool:
    return (ado_contract.filters is None or ado_contract.filters.is_empty()) and not ado_contract.explicit_work_item_ids


def _ado_contract_is_filter_only(ado_contract: SliceAdoSourceContract) -> bool:
    return (
        ado_contract.filters is not None
        and not ado_contract.filters.is_empty()
        and not ado_contract.saved_queries
        and not ado_contract.explicit_work_item_ids
    )