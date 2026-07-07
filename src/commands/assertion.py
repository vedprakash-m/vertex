from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Literal
from uuid import uuid4

import typer
import yaml

from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.hypothesis_models import AssertionEvaluation, AssertionOperator, CompositeAssertion, CompositeAssertionOperator, TelemetryAssertion
from src.core.kusto_query_loader import load_kpi_queries
from src.core.metric_registry import METRICS_ROOT, load_metric_definition_map
from src.core.metric_models import MetricAggregation, ObservationWindow
from src.core.models_v2 import KustoQuery
from src.core.reality_store import RealityStore


app = typer.Typer(help="Author telemetry assertions for L1 reality evaluation.")
composite_app = typer.Typer(help="Author composite assertions over existing telemetry assertions.")
app.add_typer(composite_app, name="composite")


@app.command("list")
def list_assertions_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    format: Literal["text", "json"] = typer.Option("text", "--format", help="Output format."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    normalized_program = _require_text(program, "--program")
    store = RealityStore(normalized_program, db_root=db_root)
    store.initialize()
    assertions = store.list_active_telemetry_assertions()

    if format == "json":
        typer.echo(json.dumps([_serialize_assertion(item) for item in assertions], ensure_ascii=True, indent=2))
        raise typer.Exit(code=0)

    if not assertions:
        typer.echo(f"No active assertions for {normalized_program}.")
        raise typer.Exit(code=0)

    for assertion in assertions:
        typer.echo(
            " | ".join(
                (
                    assertion.id,
                    assertion.metric_id,
                    assertion.operator.value,
                    _format_assertion_threshold(assertion),
                    f"policy_version={assertion.policy_version}",
                )
            )
        )
    raise typer.Exit(code=0)


@app.command("history")
def history_assertions_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    id: str | None = typer.Option(None, "--id", help="Assertion id to anchor the history lookup."),
    metric_id: str | None = typer.Option(None, "--metric-id", help="Metric id to inspect across assertion versions."),
    format: Literal["text", "json"] = typer.Option("text", "--format", help="Output format."),
    include_evaluations: bool = typer.Option(
        True,
        "--include-evaluations/--no-include-evaluations",
        help="Include linked assertion evaluation rows.",
    ),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    normalized_program = _require_text(program, "--program")
    if (id is None and metric_id is None) or (id is not None and metric_id is not None):
        raise typer.BadParameter("Provide exactly one of --id or --metric-id.")

    store = RealityStore(normalized_program, db_root=db_root)
    store.initialize()

    anchor_assertion: TelemetryAssertion | None = None
    if id is not None:
        anchor_assertion = _require_assertion(store, normalized_program, _require_text(id, "--id"))
        normalized_metric_id = anchor_assertion.metric_id
    else:
        normalized_metric_id = _require_text(metric_id, "--metric-id")

    assertions = store.list_telemetry_assertions(metric_id=normalized_metric_id, include_archived=True)
    if anchor_assertion is not None:
        assertions = tuple(
            item for item in assertions if _same_assertion_history_family(item, anchor_assertion)
        )

    if not assertions:
        typer.echo(f"No assertion history for {normalized_program}:{normalized_metric_id}.")
        raise typer.Exit(code=0)

    evaluations = (
        store.list_assertion_evaluations(assertion_ids=tuple(item.id for item in assertions))
        if include_evaluations
        else ()
    )
    evaluation_lookup = _group_evaluations_by_assertion_id(evaluations)
    history_entries = _build_assertion_history_entries(
        assertions,
        evaluation_lookup,
        include_evaluations=include_evaluations,
    )

    if format == "json":
        typer.echo(
            json.dumps(
                {
                    "program_id": normalized_program,
                    "metric_id": normalized_metric_id,
                    "scope_assertion_id": anchor_assertion.id if anchor_assertion is not None else None,
                    "assertions": history_entries,
                },
                ensure_ascii=True,
                indent=2,
            )
        )
        raise typer.Exit(code=0)

    for entry in history_entries:
        typer.echo(
            " | ".join(
                (
                    str(entry["id"]),
                    str(entry["metric_id"]),
                    str(entry["status"]),
                    f"policy_version={entry['policy_version']}",
                    f"operator={entry['operator']}",
                    f"threshold={_format_serialized_threshold(entry)}",
                    f"valid_from={entry['valid_from']}",
                    f"valid_until={entry['valid_until'] or '-'}",
                    f"evaluations={entry['evaluation_count']}",
                )
            )
        )
        if include_evaluations and entry["latest_evaluated_at"] is not None:
            typer.echo(f"  latest_evaluated_at={entry['latest_evaluated_at']}")
    raise typer.Exit(code=0)


@app.command("add")
def add_assertion_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    assertion_id: str | None = typer.Option(None, "--id", "--assertion-id", help="Optional explicit assertion id."),
    query_id: str | None = typer.Option(None, "--query-id", help="Optional KPI query id whose catalog-linked metric_id/assertion_ids should be reused."),
    metric_id: str | None = typer.Option(None, "--metric-id", help="Metric id bound to this assertion."),
    operator: str | None = typer.Option(None, "--operator", help="Comparison operator, for example >= or <=."),
    threshold: float | None = typer.Option(None, "--threshold", help="Threshold value to compare against."),
    threshold_upper: float | None = typer.Option(None, "--threshold-upper", help="Upper threshold for between assertions."),
    baseline_value: float | None = typer.Option(None, "--baseline-value", help="Baseline value required for percent-change assertions."),
    baseline_captured_at: str | None = typer.Option(None, "--baseline-captured-at", help="Optional ISO timestamp for the baseline observation."),
    window_days: int = typer.Option(7, "--window-days", min=1, help="Trailing observation window in days."),
    tolerance_rel: float = typer.Option(0.10, "--tolerance-rel", min=0.0, help="Relative tolerance for delta magnitude."),
    tolerance_abs: float | None = typer.Option(None, "--tolerance-abs", help="Optional absolute tolerance."),
    sustain_min_observations: int = typer.Option(3, "--sustain-min-observations", min=1, help="Consecutive violations required before challenge emission."),
    cooldown_hours: int = typer.Option(24, "--cooldown-hours", min=0, help="Cooldown after dismissal or resolution."),
    severity_override: str | None = typer.Option(None, "--severity-override", help="Optional severity override: info, warn, alert."),
    description: str = typer.Option("", "--description", help="Optional human-readable description."),
    created_by: str | None = typer.Option(None, "--created-by", help="Optional actor recorded on the assertion."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, hidden=True),
    metrics_root: Path = typer.Option(METRICS_ROOT, hidden=True),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    normalized_program = _require_text(program, "--program")
    query = _load_kpi_query(normalized_program, query_id=query_id, programs_root=programs_root)
    normalized_assertion_id = _resolve_assertion_id(assertion_id, query=query)
    normalized_metric_id = _resolve_metric_id(metric_id, query=query)
    metric_definition = load_metric_definition_map(
        metrics_root=metrics_root,
        metric_ids=(normalized_metric_id,),
        as_of=datetime.now(timezone.utc),
    ).get(normalized_metric_id)
    resolved_operator_text, resolved_threshold_input = _resolve_assertion_threshold_inputs(
        operator,
        threshold,
        metric_id=normalized_metric_id,
        metric_definition=metric_definition,
    )
    parsed_operator = _parse_operator(resolved_operator_text)
    parsed_severity = _parse_optional_severity(severity_override)
    resolved_threshold, resolved_threshold_upper = _resolve_threshold_bounds(
        operator=parsed_operator,
        threshold=resolved_threshold_input,
        threshold_upper=threshold_upper,
    )
    resolved_baseline_value, resolved_baseline_captured_at = _resolve_percent_baseline(
        operator=parsed_operator,
        baseline_value=baseline_value,
        baseline_captured_at=baseline_captured_at,
        explicit_inputs=baseline_value is not None or baseline_captured_at is not None,
    )

    store = RealityStore(normalized_program, db_root=db_root)
    store.initialize()

    assertion = TelemetryAssertion(
        id=normalized_assertion_id or str(uuid4()),
        program_id=normalized_program,
        metric_id=normalized_metric_id,
        window=ObservationWindow(days=window_days, aggregation=MetricAggregation.LAST),
        operator=parsed_operator,
        threshold=resolved_threshold,
        tolerance_rel=float(tolerance_rel),
        tolerance_abs=float(tolerance_abs) if tolerance_abs is not None else None,
        sustain_min_observations=int(sustain_min_observations),
        cooldown_hours=int(cooldown_hours),
        severity_override=parsed_severity,
        description=description.strip(),
        created_by=created_by.strip() if isinstance(created_by, str) and created_by.strip() else "vertex/assertion",
        threshold_upper=resolved_threshold_upper,
        baseline_value=resolved_baseline_value,
        baseline_captured_at=resolved_baseline_captured_at,
    )
    store.upsert_telemetry_assertion(assertion)
    typer.echo(f"Created assertion {assertion.id} for {assertion.metric_id}")
    raise typer.Exit(code=0)


@app.command("update")
def update_assertion_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    id: str = typer.Option(..., "--id", help="Existing assertion id."),
    operator: str | None = typer.Option(None, "--operator", help="Updated comparison operator, for example >= or <=."),
    threshold: float | None = typer.Option(None, "--threshold", help="Updated threshold value."),
    threshold_upper: float | None = typer.Option(None, "--threshold-upper", help="Updated upper threshold for between assertions."),
    baseline_value: float | None = typer.Option(None, "--baseline-value", help="Updated baseline value for percent-change assertions."),
    baseline_captured_at: str | None = typer.Option(None, "--baseline-captured-at", help="Updated ISO timestamp for the baseline observation."),
    clear_baseline: bool = typer.Option(False, "--clear-baseline", help="Clear stored baseline fields before applying other updates."),
    window_days: int | None = typer.Option(None, "--window-days", min=1, help="Updated trailing observation window in days."),
    tolerance_rel: float | None = typer.Option(None, "--tolerance-rel", min=0.0, help="Updated relative tolerance."),
    tolerance_abs: float | None = typer.Option(None, "--tolerance-abs", help="Updated absolute tolerance."),
    sustain_min_observations: int | None = typer.Option(None, "--sustain-min-observations", min=1, help="Updated sustained-violation count."),
    cooldown_hours: int | None = typer.Option(None, "--cooldown-hours", min=0, help="Updated cooldown after dismissal or resolution."),
    severity_override: str | None = typer.Option(None, "--severity-override", help="Updated severity override: info, warn, alert. Pass an empty string to clear."),
    description: str | None = typer.Option(None, "--description", help="Updated human-readable description. Pass an empty string to clear."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    normalized_program = _require_text(program, "--program")
    assertion_id = _require_text(id, "--id")
    store = RealityStore(normalized_program, db_root=db_root)
    store.initialize()

    current = _require_assertion(store, normalized_program, assertion_id)
    next_operator = _parse_operator(operator) if operator is not None else current.operator
    next_threshold = float(threshold) if threshold is not None else current.threshold
    next_threshold_upper = current.threshold_upper
    if threshold_upper is not None:
        next_threshold_upper = float(threshold_upper)
    next_threshold, next_threshold_upper = _resolve_threshold_bounds(
        operator=next_operator,
        threshold=next_threshold,
        threshold_upper=next_threshold_upper,
        threshold_upper_explicit=threshold_upper is not None,
        allow_auto_clear=True,
    )
    next_baseline_value = current.baseline_value
    next_baseline_captured_at = current.baseline_captured_at
    if clear_baseline:
        next_baseline_value = None
        next_baseline_captured_at = None
    if baseline_value is not None:
        next_baseline_value = float(baseline_value)
    if baseline_captured_at is not None:
        if next_baseline_value is None:
            raise typer.BadParameter("--baseline-captured-at requires a stored or updated --baseline-value")
        next_baseline_captured_at = _resolve_optional_datetime(
            baseline_captured_at,
            option_name="--baseline-captured-at",
        )
    next_baseline_value, next_baseline_captured_at = _resolve_percent_baseline(
        operator=next_operator,
        baseline_value=next_baseline_value,
        baseline_captured_at=next_baseline_captured_at,
        explicit_inputs=clear_baseline or baseline_value is not None or baseline_captured_at is not None,
    )
    next_window_days = int(window_days) if window_days is not None else current.window.days
    next_tolerance_rel = float(tolerance_rel) if tolerance_rel is not None else current.tolerance_rel
    next_tolerance_abs = float(tolerance_abs) if tolerance_abs is not None else current.tolerance_abs
    next_sustain_min_observations = int(sustain_min_observations) if sustain_min_observations is not None else current.sustain_min_observations
    next_cooldown_hours = int(cooldown_hours) if cooldown_hours is not None else current.cooldown_hours
    next_severity = _parse_optional_severity(severity_override) if severity_override is not None else current.severity_override
    next_description = description.strip() if description is not None else current.description

    if (
        next_operator == current.operator
        and next_threshold == current.threshold
        and next_threshold_upper == current.threshold_upper
        and next_baseline_value == current.baseline_value
        and next_baseline_captured_at == current.baseline_captured_at
        and next_window_days == current.window.days
        and next_tolerance_rel == current.tolerance_rel
        and next_tolerance_abs == current.tolerance_abs
        and next_sustain_min_observations == current.sustain_min_observations
        and next_cooldown_hours == current.cooldown_hours
        and next_severity == current.severity_override
        and next_description == current.description
    ):
        raise typer.BadParameter("No assertion changes were provided.")

    updated = replace(
        current,
        id=str(uuid4()),
        window=replace(current.window, days=next_window_days),
        operator=next_operator,
        threshold=next_threshold,
        threshold_upper=next_threshold_upper,
        baseline_value=next_baseline_value,
        baseline_captured_at=next_baseline_captured_at,
        tolerance_rel=next_tolerance_rel,
        tolerance_abs=next_tolerance_abs,
        sustain_min_observations=next_sustain_min_observations,
        cooldown_hours=next_cooldown_hours,
        severity_override=next_severity,
        description=next_description,
        policy_version=current.policy_version + 1,
        valid_from=datetime.now(timezone.utc),
        valid_until=None,
    )
    archived = replace(current, valid_until=updated.valid_from)
    store.upsert_telemetry_assertion(archived)
    store.upsert_telemetry_assertion(updated)

    if current.linked_hypothesis_id is not None:
        hypothesis = store.get_hypothesis(current.linked_hypothesis_id)
        if hypothesis is not None and hypothesis.telemetry_assertion_id == current.id:
            store.upsert_hypothesis(replace(hypothesis, telemetry_assertion_id=updated.id))

    typer.echo(
        f"Updated assertion {current.id}; new active assertion is {updated.id}; policy version is now {updated.policy_version}"
    )
    raise typer.Exit(code=0)


@app.command("add-evidence-url")
def add_assertion_evidence_url_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    binding_id: str = typer.Option(..., "--binding-id", help="Metric source binding id."),
    template: str = typer.Option(..., "--template", help="Evidence URL template."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    normalized_program = _require_text(program, "--program")
    normalized_binding_id = _require_text(binding_id, "--binding-id")
    normalized_template = _require_text(template, "--template")
    _validate_evidence_url_template(normalized_template)

    store = RealityStore(normalized_program, db_root=db_root)
    store.initialize()
    binding = store.get_metric_source_binding(normalized_binding_id)
    if binding is None or binding.program_id != normalized_program:
        raise typer.BadParameter(
            f"Metric source binding {normalized_binding_id} was not found for program {normalized_program}."
        )

    store.upsert_metric_source_binding(replace(binding, evidence_url_template=normalized_template))
    typer.echo(f"Updated evidence URL template for binding {normalized_binding_id}")
    raise typer.Exit(code=0)


@app.command("export")
def export_assertions_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    include_history: bool = typer.Option(False, "--include-history", help="Include archived versions and linked evaluation history."),
    db_root: Path | None = typer.Option(None, hidden=True),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, hidden=True),
) -> None:
    normalized_program = _require_text(program, "--program")
    store = RealityStore(normalized_program, db_root=db_root)
    store.initialize()
    assertions = store.list_active_telemetry_assertions()

    export_path = programs_root / normalized_program / "reality" / "assertions.snapshot.yaml"
    export_path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "version": "1.0",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "assertions": [_serialize_assertion(item) for item in assertions],
    }
    if include_history:
        historical_assertions = store.list_telemetry_assertions(include_archived=True)
        history_evaluations = store.list_assertion_evaluations(
            assertion_ids=tuple(item.id for item in historical_assertions)
        )
        document["assertion_history"] = _build_assertion_history_entries(
            historical_assertions,
            _group_evaluations_by_assertion_id(history_evaluations),
            include_evaluations=True,
        )
    body = yaml.safe_dump(document, sort_keys=False, allow_unicode=False)
    export_path.write_text(
        "# READ-ONLY -- generated by `vertex assertion export`\n"
        "# Edits to this file are ignored. Author via: vertex assertion add/update/add-evidence-url\n"
        + body,
        encoding="utf-8",
    )
    typer.echo(str(export_path))
    raise typer.Exit(code=0)


@composite_app.command("list")
def list_composite_assertions_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    format: Literal["text", "json"] = typer.Option("text", "--format", help="Output format."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    normalized_program = _require_text(program, "--program")
    store = RealityStore(normalized_program, db_root=db_root)
    store.initialize()
    assertions = store.list_active_composite_assertions()

    if format == "json":
        typer.echo(json.dumps([_serialize_composite_assertion(item) for item in assertions], ensure_ascii=True, indent=2))
        raise typer.Exit(code=0)

    if not assertions:
        typer.echo(f"No active composite assertions for {normalized_program}.")
        raise typer.Exit(code=0)

    for assertion in assertions:
        typer.echo(
            " | ".join(
                (
                    assertion.id,
                    assertion.operator.value,
                    ",".join(assertion.child_assertion_ids),
                    f"policy_version={assertion.policy_version}",
                )
            )
        )
    raise typer.Exit(code=0)


@composite_app.command("add")
def add_composite_assertion_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    composite_id: str | None = typer.Option(None, "--id", "--composite-id", help="Optional explicit composite assertion id."),
    operator: str = typer.Option(..., "--operator", help="Composite operator: and or or."),
    child_assertion_id: list[str] = typer.Option(..., "--child-assertion-id", help="Repeatable child assertion id (2-4 required)."),
    description: str = typer.Option("", "--description", help="Optional human-readable description."),
    created_by: str | None = typer.Option(None, "--created-by", help="Optional actor recorded on the composite assertion."),
    db_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    normalized_program = _require_text(program, "--program")
    resolved_id = _optional_text(composite_id) or str(uuid4())
    resolved_operator = _parse_composite_operator(operator)
    resolved_children = _resolve_child_assertion_ids(child_assertion_id)

    store = RealityStore(normalized_program, db_root=db_root)
    store.initialize()

    for assertion_id in resolved_children:
        _require_assertion(store, normalized_program, assertion_id)

    assertion = CompositeAssertion(
        id=resolved_id,
        program_id=normalized_program,
        operator=resolved_operator,
        child_assertion_ids=resolved_children,
        description=description.strip(),
        created_by=created_by.strip() if isinstance(created_by, str) and created_by.strip() else "vertex/assertion",
    )
    store.upsert_composite_assertion(assertion)
    typer.echo(f"Created composite assertion {assertion.id}")
    raise typer.Exit(code=0)


def _parse_operator(value: str) -> AssertionOperator:
    text = _require_text(value, "--operator")
    try:
        return AssertionOperator.from_string(text)
    except ValueError as exc:
        raise typer.BadParameter(f"Unsupported --operator value: {value}") from exc


def _parse_optional_severity(value: str | None) -> "Literal['info', 'warn', 'alert'] | None":
    from typing import Literal, cast

    if value is None or not value.strip():
        return None
    text = value.strip().lower()
    if text not in {"info", "warn", "alert"}:
        raise typer.BadParameter("--severity-override must be one of: info, warn, alert")
    return cast("Literal['info', 'warn', 'alert']", text)


def _parse_composite_operator(value: str) -> CompositeAssertionOperator:
    text = _require_text(value, "--operator")
    try:
        return CompositeAssertionOperator.from_string(text)
    except ValueError as exc:
        raise typer.BadParameter(f"Unsupported composite --operator value: {value}") from exc


def _require_assertion(store: RealityStore, program_id: str, assertion_id: str) -> TelemetryAssertion:
    assertion = store.get_telemetry_assertion(assertion_id)
    if assertion is None or assertion.program_id != program_id:
        raise typer.BadParameter(f"Assertion {assertion_id} was not found for program {program_id}.")
    return assertion


def _require_text(value: str | None, option_name: str) -> str:
    if value is None or not value.strip():
        raise typer.BadParameter(f"{option_name} must be non-empty")
    return value.strip()


def _optional_text(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value.strip()


def _resolve_child_assertion_ids(values: list[str]) -> tuple[str, ...]:
    resolved: list[str] = []
    seen: set[str] = set()
    for raw_value in values:
        assertion_id = _require_text(raw_value, "--child-assertion-id")
        if assertion_id in seen:
            continue
        seen.add(assertion_id)
        resolved.append(assertion_id)
    if len(resolved) < 2 or len(resolved) > 4:
        raise typer.BadParameter("Composite assertions require 2 to 4 distinct --child-assertion-id values")
    return tuple(resolved)


def _load_kpi_query(program_id: str, *, query_id: str | None, programs_root: Path) -> KustoQuery | None:
    normalized_query_id = _optional_text(query_id)
    if normalized_query_id is None:
        return None
    for query in load_kpi_queries(program_id, programs_root=programs_root):
        if query.id == normalized_query_id:
            return query
    raise typer.BadParameter(f"KPI query {normalized_query_id} was not found for program {program_id}.")


def _resolve_metric_id(metric_id: str | None, *, query: KustoQuery | None) -> str:
    explicit_metric_id = _optional_text(metric_id)
    query_metric_id = _optional_text(query.metric_id) if query is not None else None
    if explicit_metric_id is not None and query_metric_id is not None and explicit_metric_id != query_metric_id:
        raise typer.BadParameter(
            f"--metric-id {explicit_metric_id} does not match KPI query metric_id {query_metric_id}."
        )
    resolved_metric_id = explicit_metric_id or query_metric_id
    if resolved_metric_id is None:
        raise typer.BadParameter("Provide --metric-id or use --query-id with a KPI catalog entry that declares metric_id.")
    return resolved_metric_id


def _resolve_assertion_id(assertion_id: str | None, *, query: KustoQuery | None) -> str | None:
    explicit_assertion_id = _optional_text(assertion_id)
    query_assertion_ids = query.assertion_ids if query is not None else ()
    if explicit_assertion_id is not None and len(query_assertion_ids) == 1 and explicit_assertion_id != query_assertion_ids[0]:
        raise typer.BadParameter(
            f"--id {explicit_assertion_id} does not match KPI query assertion_id {query_assertion_ids[0]}."
        )
    if explicit_assertion_id is not None:
        return explicit_assertion_id
    if len(query_assertion_ids) == 1:
        return query_assertion_ids[0]
    return None


def _resolve_assertion_threshold_inputs(
    operator: str | None,
    threshold: float | None,
    *,
    metric_id: str,
    metric_definition: object | None,
) -> tuple[str, float]:
    explicit_operator = _optional_text(operator)
    has_threshold = threshold is not None
    if explicit_operator is not None and has_threshold:
        return explicit_operator, float(threshold)  # type: ignore[arg-type]
    if explicit_operator is None and not has_threshold and metric_definition is not None:
        slo_direction = getattr(metric_definition, "slo_direction", None)
        slo_target = getattr(metric_definition, "slo_target", None)
        if isinstance(slo_direction, str) and slo_target is not None:
            return (">=" if slo_direction == "gte" else "<="), float(slo_target)
    raise typer.BadParameter(
        f"Provide both --operator and --threshold, or use a metric definition for {metric_id} that declares slo_direction and slo_target."
    )


def _serialize_assertion(assertion: TelemetryAssertion) -> dict[str, object]:
    return {
        "id": assertion.id,
        "program_id": assertion.program_id,
        "metric_id": assertion.metric_id,
        "operator": assertion.operator.value,
        "threshold": assertion.threshold,
        "threshold_upper": assertion.threshold_upper,
        "baseline_value": assertion.baseline_value,
        "baseline_captured_at": assertion.baseline_captured_at.isoformat() if assertion.baseline_captured_at is not None else None,
        "tolerance_rel": assertion.tolerance_rel,
        "tolerance_abs": assertion.tolerance_abs,
        "sustain_min_observations": assertion.sustain_min_observations,
        "cooldown_hours": assertion.cooldown_hours,
        "severity_override": assertion.severity_override,
        "description": assertion.description,
        "linked_hypothesis_id": assertion.linked_hypothesis_id,
        "policy_version": assertion.policy_version,
        "valid_from": assertion.valid_from.isoformat(),
        "valid_until": assertion.valid_until.isoformat() if assertion.valid_until is not None else None,
        "created_by": assertion.created_by,
    }


def _serialize_composite_assertion(assertion: CompositeAssertion) -> dict[str, object]:
    return {
        "id": assertion.id,
        "program_id": assertion.program_id,
        "operator": assertion.operator.value,
        "child_assertion_ids": list(assertion.child_assertion_ids),
        "description": assertion.description,
        "linked_hypothesis_id": assertion.linked_hypothesis_id,
        "policy_version": assertion.policy_version,
        "valid_from": assertion.valid_from.isoformat(),
        "valid_until": assertion.valid_until.isoformat() if assertion.valid_until is not None else None,
        "created_by": assertion.created_by,
    }


def _format_assertion_threshold(assertion: TelemetryAssertion) -> str:
    if assertion.operator == AssertionOperator.BETWEEN and assertion.threshold_upper is not None:
        return f"{assertion.threshold:g}..{assertion.threshold_upper:g}"
    return f"{assertion.threshold:g}"


def _format_serialized_threshold(entry: dict[str, object]) -> str:
    threshold = float(entry["threshold"])  # type: ignore[arg-type]
    threshold_upper = entry.get("threshold_upper")
    if entry.get("operator") == AssertionOperator.BETWEEN.value and threshold_upper is not None:
        return f"{threshold:g}..{float(threshold_upper):g}"  # type: ignore[arg-type]
    return f"{threshold:g}"


def _resolve_threshold_bounds(
    *,
    operator: AssertionOperator,
    threshold: float,
    threshold_upper: float | None,
    threshold_upper_explicit: bool = False,
    allow_auto_clear: bool = False,
) -> tuple[float, float | None]:
    lower_bound = float(threshold)
    upper_bound = float(threshold_upper) if threshold_upper is not None else None
    if operator == AssertionOperator.BETWEEN:
        if upper_bound is None:
            raise typer.BadParameter("--threshold-upper is required when --operator is between")
        if upper_bound < lower_bound:
            raise typer.BadParameter("--threshold-upper must be greater than or equal to --threshold")
        return lower_bound, upper_bound
    if threshold_upper_explicit:
        raise typer.BadParameter("--threshold-upper is only valid when --operator is between")
    if allow_auto_clear:
        return lower_bound, None
    return lower_bound, upper_bound


def _resolve_percent_baseline(
    *,
    operator: AssertionOperator,
    baseline_value: float | None,
    baseline_captured_at: str | datetime | None,
    explicit_inputs: bool,
) -> tuple[float | None, datetime | None]:
    if operator not in {AssertionOperator.PCT_IMPROVEMENT, AssertionOperator.PCT_REGRESSION}:
        if explicit_inputs:
            raise typer.BadParameter(
                "--baseline-value, --baseline-captured-at, and --clear-baseline are only valid for percent-change assertions"
            )
        return None, None
    if baseline_value is None:
        raise typer.BadParameter("--baseline-value is required for percent-change assertions")
    return float(baseline_value), _resolve_optional_datetime(
        baseline_captured_at,
        option_name="--baseline-captured-at",
    )


def _resolve_optional_datetime(value: str | datetime | None, *, option_name: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise typer.BadParameter(f"{option_name} must be a valid ISO datetime") from exc
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _serialize_assertion_evaluation(evaluation: AssertionEvaluation) -> dict[str, object]:
    return {
        "id": evaluation.id,
        "program_id": evaluation.program_id,
        "hypothesis_id": evaluation.hypothesis_id,
        "assertion_id": evaluation.assertion_id,
        "observation_id": evaluation.observation_id,
        "evaluated_at": evaluation.evaluated_at.isoformat(),
        "violated": evaluation.violated,
        "value_num": evaluation.value_num,
        "expected_value": evaluation.expected_value,
        "quality_state": evaluation.quality_state.value if evaluation.quality_state is not None else None,
        "note": evaluation.note,
    }


def _build_assertion_history_entries(
    assertions: tuple[TelemetryAssertion, ...],
    evaluation_lookup: dict[str, tuple[AssertionEvaluation, ...]],
    *,
    include_evaluations: bool,
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for assertion in assertions:
        evaluations = evaluation_lookup.get(assertion.id, ())
        entry = {
            **_serialize_assertion(assertion),
            "status": "active" if assertion.valid_until is None else "archived",
            "evaluation_count": len(evaluations),
            "latest_evaluated_at": evaluations[-1].evaluated_at.isoformat() if evaluations else None,
        }
        if include_evaluations:
            entry["evaluations"] = [_serialize_assertion_evaluation(item) for item in evaluations]
        entries.append(entry)
    return entries


def _group_evaluations_by_assertion_id(
    evaluations: tuple[AssertionEvaluation, ...],
) -> dict[str, tuple[AssertionEvaluation, ...]]:
    grouped: dict[str, list[AssertionEvaluation]] = {}
    for evaluation in evaluations:
        if evaluation.assertion_id is None:
            continue
        grouped.setdefault(evaluation.assertion_id, []).append(evaluation)
    return {assertion_id: tuple(items) for assertion_id, items in grouped.items()}


def _same_assertion_history_family(candidate: TelemetryAssertion, anchor: TelemetryAssertion) -> bool:
    return (
        candidate.metric_id == anchor.metric_id
        and (candidate.linked_hypothesis_id or "") == (anchor.linked_hypothesis_id or "")
        and (candidate.linked_claim_id or "") == (anchor.linked_claim_id or "")
        and (candidate.linked_assumption_id or "") == (anchor.linked_assumption_id or "")
    )


_EVIDENCE_URL_TOKEN_PATTERN = re.compile(r"\{([^{}]+)\}")
_ALLOWED_EVIDENCE_URL_TOKENS = {
    "metric_id",
    "program_id",
    "cluster",
    "database",
    "observed_value",
    "expected_value",
    "detected_at_iso",
    "binding_id",
}


def _validate_evidence_url_template(template: str) -> None:
    unknown_tokens = sorted(
        {
            match.group(1)
            for match in _EVIDENCE_URL_TOKEN_PATTERN.finditer(template)
            if match.group(1) not in _ALLOWED_EVIDENCE_URL_TOKENS
        }
    )
    if unknown_tokens:
        raise typer.BadParameter(
            "Unsupported evidence-url token(s): " + ", ".join(unknown_tokens)
        )