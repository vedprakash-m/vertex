from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import getpass
from pathlib import Path
from uuid import uuid4

import typer
import yaml

from src.core.kusto_query_loader import load_kpi_queries
from src.core.hypothesis_models import AssertionOperator, Hypothesis, HypothesisKind, HypothesisStatus, TelemetryAssertion
from src.core.metric_models import MetricAggregation, MetricDefinition, MetricSourceBinding, ObservationWindow
from src.core.metric_registry import METRICS_ROOT, load_metric_definition_map
from src.core.models_v2 import KustoQuery
from src.core.reality_store import RealityStore


def quickstart_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    metric_id: str | None = typer.Option(None, "--metric-id", help="Metric id to monitor. May be omitted when --query-id refers to a KPI catalog entry that declares metric_id or exactly one active assertion_id."),
    binding_id: str | None = typer.Option(None, "--binding-id", help="Metric source binding id to reuse or create. Defaults to a stable id derived from the metric id."),
    operator: str | None = typer.Option(None, "--operator", help="Comparison operator, for example >= or <=. May be omitted when --query-id refers to a KPI catalog entry with exactly one active assertion_id."),
    threshold: float | None = typer.Option(None, "--threshold", help="Threshold value for the quickstart assertion. May be omitted when --query-id refers to a KPI catalog entry with exactly one active assertion_id."),
    baseline_value: float | None = typer.Option(None, "--baseline-value", help="Baseline value required for percent-change assertions when quickstart authors a new assertion."),
    baseline_captured_at: str | None = typer.Option(None, "--baseline-captured-at", help="Optional ISO timestamp for the percent-change baseline observation."),
    cluster: str | None = typer.Option(None, "--cluster", help="Kusto cluster for a new binding."),
    database: str | None = typer.Option(None, "--database", help="Kusto database for a new binding."),
    kql_template: str | None = typer.Option(None, "--kql-template", help="Kusto query template for a new binding."),
    result_column: str | None = typer.Option(None, "--result-column", help="Result column for a new binding."),
    query_id: str | None = typer.Option(None, "--query-id", help="Existing KPI query id whose binding inputs should be reused when creating a new binding."),
    metric_title: str | None = typer.Option(None, "--metric-title", help="Title for a new metric definition when the metric is missing."),
    unit: str | None = typer.Option(None, "--unit", help="Unit for a new metric definition when the metric is missing."),
    aggregation: str = typer.Option("last", "--aggregation", help="Aggregation for a new metric definition when the metric is missing."),
    statement: str | None = typer.Option(None, "--statement", help="Optional PM-readable hypothesis statement override."),
    review_due: str | None = typer.Option(None, "--review-due", help="Optional YYYY-MM-DD review date."),
    description: str = typer.Option("", "--description", help="Optional assertion description override."),
    proposed_by: str | None = typer.Option(None, "--proposed-by", help="Actor alias. Defaults to current OS user."),
    db_root: Path | None = typer.Option(None, hidden=True),
    metrics_root: Path = typer.Option(METRICS_ROOT, hidden=True),
    programs_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    program_id = _require_text(program, "--program")
    query = _load_query_binding_template(program_id, query_id=query_id, programs_root=programs_root)
    actor = _default_actor(proposed_by)
    proposed_at = datetime.now(timezone.utc)

    store = RealityStore(program_id, db_root=db_root)
    store.initialize()
    reusable_assertions = _load_reusable_assertions(store, query=query)
    parsed_operator, resolved_threshold, assertion_to_link = _resolve_assertion_inputs(
        store,
        operator,
        threshold,
        reusable_assertions=reusable_assertions,
    )
    resolved_baseline_value, resolved_baseline_captured_at = _resolve_percent_baseline(
        parsed_operator,
        baseline_value=baseline_value if assertion_to_link is None else assertion_to_link.baseline_value,
        baseline_captured_at=baseline_captured_at if assertion_to_link is None else assertion_to_link.baseline_captured_at,
        explicit_inputs=baseline_value is not None or baseline_captured_at is not None,
        reused_assertion=assertion_to_link,
    )
    normalized_metric_id = _resolve_metric_id(metric_id, query=query, reusable_assertion=assertion_to_link)
    normalized_binding_id = _resolve_binding_id(store, binding_id, metric_id=normalized_metric_id)

    definition = load_metric_definition_map(
        metrics_root=metrics_root,
        metric_ids=(normalized_metric_id,),
        as_of=proposed_at,
    ).get(normalized_metric_id)
    definition_created = False

    binding = store.get_metric_source_binding(normalized_binding_id)
    if binding is not None and binding.program_id == program_id and binding.valid_until is None:
        binding_created = False
    else:
        resolved_cluster = _coalesce_binding_input(cluster, query.cluster if query is not None else None)
        resolved_database = _coalesce_binding_input(database, query.database if query is not None else None)
        resolved_kql_template = _coalesce_binding_input(
            kql_template,
            (query.wiql if query is not None and query.engine == "wiql" else query.kql if query is not None else None),
        )
        resolved_result_column = _coalesce_binding_input(result_column, query.result_column if query is not None else None)

        definition, definition_created = _resolve_or_create_metric_definition(
            definition,
            metrics_root=metrics_root,
            metric_id=normalized_metric_id,
            metric_title=metric_title,
            unit=unit,
            aggregation=aggregation,
            owner_alias=actor,
            created_at=proposed_at,
        )

        binding, binding_created = _resolve_or_create_binding(
            store,
            program_id=program_id,
            metric_id=normalized_metric_id,
            binding_id=normalized_binding_id,
            source_kind=query.engine if query is not None else "kusto",
            cluster=resolved_cluster,
            database=resolved_database,
            kql_template=resolved_kql_template,
            result_column=resolved_result_column,
            owner_alias=actor,
            metric_definition=definition,
            created_at=proposed_at,
        )
    if binding.metric_id != normalized_metric_id:
        raise typer.BadParameter(
            f"Metric source binding {normalized_binding_id} targets metric {binding.metric_id}, not {normalized_metric_id}."
        )

    _ensure_no_active_metric_hypothesis(store, normalized_metric_id)

    hypothesis_statement = _resolve_statement(
        statement,
        metric_id=normalized_metric_id,
        operator=parsed_operator,
        threshold=resolved_threshold,
        definition=definition,
        assertion=assertion_to_link,
    )
    review_due_date = _parse_optional_date(review_due, option_name="--review-due")
    if review_due_date is None and assertion_to_link is not None:
        review_due_date = assertion_to_link.re_evaluate_by

    assertion = assertion_to_link
    if assertion is None:
        assertion = TelemetryAssertion(
            id=str(uuid4()),
            program_id=program_id,
            metric_id=normalized_metric_id,
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=parsed_operator,
            threshold=resolved_threshold,
            baseline_value=resolved_baseline_value,
            baseline_captured_at=resolved_baseline_captured_at,
            description=description.strip() or hypothesis_statement,
            created_by=actor,
        )
        store.upsert_telemetry_assertion(assertion)

    hypothesis = Hypothesis(
        id=str(uuid4()),
        short_id=store.next_hypothesis_short_id(),
        program_id=program_id,
        kind=HypothesisKind.SCALAR_FACT,
        statement=hypothesis_statement,
        expected_value=resolved_threshold,
        as_of_date=proposed_at.date(),
        telemetry_assertion_id=assertion.id,
        proposed_by=actor,
        proposed_at=proposed_at,
        status=HypothesisStatus.PROPOSED,
        review_due=review_due_date,
    )
    store.upsert_hypothesis(hypothesis)
    store.set_hypothesis_state(hypothesis.id, HypothesisStatus.PROPOSED, proposed_at, actor=actor, reason="quickstart")
    store.upsert_telemetry_assertion(replace(assertion, linked_hypothesis_id=hypothesis.id))

    if definition_created:
        typer.echo(f"Quickstart created metric definition {normalized_metric_id} in {metrics_root}.")
    if binding_created:
        typer.echo(
            f"Quickstart created binding {binding.binding_id} for metric {normalized_metric_id}; run `vertex admin metric validate --program {program_id} --binding-id {binding.binding_id}` before live reconcile."
        )
    typer.echo(
        f"Quickstart created proposed hypothesis {hypothesis.short_id} with assertion {assertion.id} for metric {normalized_metric_id}."
    )
    raise typer.Exit(code=0)


def _resolve_or_create_metric_definition(
    definition: MetricDefinition | None,
    *,
    metrics_root: Path,
    metric_id: str,
    metric_title: str | None,
    unit: str | None,
    aggregation: str,
    owner_alias: str,
    created_at: datetime,
) -> tuple[MetricDefinition, bool]:
    if definition is not None:
        return definition, False

    normalized_unit = _optional_text(unit)
    if normalized_unit is None:
        raise typer.BadParameter(
            f"Metric definition {metric_id} was not found in the metric registry. Provide --unit to create it inline."
        )

    parsed_aggregation = MetricAggregation.from_string(_require_text(aggregation, "--aggregation"))
    title_text = _optional_text(metric_title) or _derive_metric_title(metric_id)
    product_id = metric_id.split(".", 1)[0]
    entry: dict[str, object] = {
        "id": metric_id,
        "title": title_text,
        "unit": normalized_unit,
        "aggregation": parsed_aggregation.value,
        "owning_product_id": product_id,
        "owner_alias": owner_alias,
        "valid_from": created_at.isoformat(),
        "policy_version": 1,
    }

    target_path = metrics_root / f"{product_id}.yaml"
    existing_entries = _load_metric_registry_entries(target_path)
    existing_entries.append(entry)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(
        yaml.safe_dump({"metrics": existing_entries}, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )

    return (
        MetricDefinition(
            id=metric_id,
            title=title_text,
            unit=normalized_unit,
            aggregation=parsed_aggregation,
            owning_product_id=product_id,
            owner_alias=owner_alias,
            valid_from=created_at,
        ),
        True,
    )


def _load_metric_registry_entries(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if document is None:
        return []
    if isinstance(document, list):
        entries = document
    elif isinstance(document, dict):
        entries = document.get("metrics", [])
    else:
        raise typer.BadParameter(f"Metric registry file {path} must contain a list or metrics: list.")
    if not isinstance(entries, list):
        raise typer.BadParameter(f"Metric registry file {path} must contain a list of metrics.")
    normalized: list[dict[str, object]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise typer.BadParameter(f"Metric registry file {path} contains a non-object metric entry.")
        normalized.append(dict(entry))
    return normalized


def _resolve_or_create_binding(
    store: RealityStore,
    *,
    program_id: str,
    metric_id: str,
    binding_id: str,
    source_kind: str,
    cluster: str | None,
    database: str | None,
    kql_template: str | None,
    result_column: str | None,
    owner_alias: str,
    metric_definition: MetricDefinition | None,
    created_at: datetime,
) -> tuple[MetricSourceBinding, bool]:
    binding_inputs = {
        "--cluster": cluster,
        "--database": database,
        "--kql-template": kql_template,
        "--result-column": result_column,
    }
    missing_inputs = [name for name, value in binding_inputs.items() if value is None or not value.strip()]
    if missing_inputs:
        raise typer.BadParameter(
            f"Metric source binding {binding_id} was not found for program {program_id}. "
            f"Provide {', '.join(missing_inputs)} to create it inline."
        )
    if metric_definition is None:
        raise typer.BadParameter(
            f"Metric definition {metric_id} was not found in the metric registry. "
            "Author the metric definition first before creating a binding via quickstart."
        )

    created_binding = MetricSourceBinding(
        binding_id=binding_id,
        metric_id=metric_id,
        program_id=program_id,
        source_kind="wiql" if source_kind == "wiql" else "kusto",
        cluster=_require_text(cluster, "--cluster"),
        database=_require_text(database, "--database"),
        kql_template=_require_text(kql_template, "--kql-template"),
        result_column=_require_text(result_column, "--result-column"),
        owner_alias=owner_alias,
        valid_from=created_at,
    )
    store.upsert_metric_source_binding(created_binding)
    return created_binding, True


def _default_actor(value: str | None) -> str:
    if value is not None and value.strip():
        return value.strip()
    return getpass.getuser() or "vertex/quickstart"


def _resolve_binding_id(store: RealityStore, value: str | None, *, metric_id: str) -> str:
    if value is not None and value.strip():
        return value.strip()

    active_bindings = store.list_active_metric_source_bindings(metric_id=metric_id)
    if len(active_bindings) == 1:
        return active_bindings[0].binding_id
    if len(active_bindings) > 1:
        binding_ids = ", ".join(binding.binding_id for binding in active_bindings)
        raise typer.BadParameter(
            f"--binding-id is required because metric {metric_id} has multiple active bindings: {binding_ids}."
        )

    normalized = "".join(character if character.isalnum() else "-" for character in metric_id.lower())
    collapsed = "-".join(segment for segment in normalized.split("-") if segment)
    if not collapsed:
        raise typer.BadParameter("--binding-id must be non-empty")
    return f"{collapsed}-binding"


def _resolve_metric_id(
    value: str | None,
    *,
    query: KustoQuery | None,
    reusable_assertion: TelemetryAssertion | None,
) -> str:
    explicit_metric_id = _optional_text(value)
    query_metric_id = _optional_text(query.metric_id) if query is not None else None
    assertion_metric_id = reusable_assertion.metric_id if reusable_assertion is not None else None

    if explicit_metric_id is not None and query_metric_id is not None and explicit_metric_id != query_metric_id:
        assert query is not None
        raise typer.BadParameter(
            f"KPI query {query.id} declares metric {query_metric_id}, not {explicit_metric_id}."
        )
    if explicit_metric_id is not None and assertion_metric_id is not None and explicit_metric_id != assertion_metric_id:
        assert reusable_assertion is not None
        raise typer.BadParameter(
            f"Catalog-linked assertion {reusable_assertion.id} targets metric {assertion_metric_id}, not {explicit_metric_id}."
        )
    if query_metric_id is not None and assertion_metric_id is not None and query_metric_id != assertion_metric_id:
        assert query is not None
        assert reusable_assertion is not None
        raise typer.BadParameter(
            f"KPI query {query.id} declares metric {query_metric_id}, but linked assertion {reusable_assertion.id} targets {assertion_metric_id}."
        )

    resolved_metric_id = explicit_metric_id or query_metric_id or assertion_metric_id
    if resolved_metric_id is None:
        raise typer.BadParameter(
            "Provide --metric-id or use --query-id with a KPI catalog entry that declares metric_id or exactly one active assertion_id."
        )
    return resolved_metric_id


def _load_query_binding_template(program_id: str, *, query_id: str | None, programs_root: Path | None) -> KustoQuery | None:
    normalized_query_id = _optional_text(query_id)
    if normalized_query_id is None:
        return None

    queries = load_kpi_queries(program_id, programs_root=programs_root) if programs_root is not None else load_kpi_queries(program_id)
    for query in queries:
        if query.id == normalized_query_id:
            return query
    raise typer.BadParameter(f"KPI query {normalized_query_id} was not found for program {program_id}.")


def _coalesce_binding_input(primary: str | None, fallback: str | None) -> str | None:
    return _optional_text(primary) or _optional_text(fallback)


def _load_reusable_assertions(
    store: RealityStore,
    *,
    query: KustoQuery | None,
) -> tuple[TelemetryAssertion, ...]:
    if query is None or not query.assertion_ids:
        return ()

    active_assertions = [
        assertion
        for assertion_id in query.assertion_ids
        if (assertion := store.get_telemetry_assertion(assertion_id)) is not None and assertion.valid_until is None
    ]
    return tuple(active_assertions)


def _resolve_assertion_inputs(
    store: RealityStore,
    operator: str | None,
    threshold: float | None,
    *,
    reusable_assertions: tuple[TelemetryAssertion, ...],
) -> tuple[AssertionOperator, float, TelemetryAssertion | None]:
    has_operator = _optional_text(operator) is not None
    has_threshold = threshold is not None
    if has_operator != has_threshold:
        raise typer.BadParameter("Provide both --operator and --threshold, or omit both to reuse a catalog-linked assertion.")
    if has_operator and has_threshold:
        return _parse_operator(operator or ""), float(threshold), None  # type: ignore[arg-type]
    if len(reusable_assertions) == 1:
        reusable_assertion = reusable_assertions[0]
        _ensure_assertion_available(store, reusable_assertion)
        return reusable_assertion.operator, float(reusable_assertion.threshold), reusable_assertion
    raise typer.BadParameter(
        "Provide both --operator and --threshold, or use --query-id with a KPI catalog entry that declares exactly one active assertion_id."
    )


def _resolve_percent_baseline(
    operator: AssertionOperator,
    *,
    baseline_value: float | None,
    baseline_captured_at: str | datetime | None,
    explicit_inputs: bool,
    reused_assertion: TelemetryAssertion | None,
) -> tuple[float | None, datetime | None]:
    if operator not in {AssertionOperator.PCT_IMPROVEMENT, AssertionOperator.PCT_REGRESSION}:
        if explicit_inputs:
            raise typer.BadParameter(
                "--baseline-value and --baseline-captured-at are only valid for percent-change assertions"
            )
        return None, None
    if baseline_value is None:
        if reused_assertion is not None:
            raise typer.BadParameter(
                f"Reusable assertion {reused_assertion.id} is missing baseline_value for percent-change evaluation."
            )
        raise typer.BadParameter("--baseline-value is required for percent-change assertions")
    return float(baseline_value), _parse_optional_datetime(
        baseline_captured_at,
        option_name="--baseline-captured-at",
    )


def _ensure_assertion_available(store: RealityStore, assertion: TelemetryAssertion) -> None:
    if assertion.linked_hypothesis_id is None:
        return
    linked = store.get_hypothesis(assertion.linked_hypothesis_id)
    if linked is None:
        return
    if linked.status not in {HypothesisStatus.REJECTED, HypothesisStatus.INVALIDATED, HypothesisStatus.SUPERSEDED}:
        raise typer.BadParameter(
            f"Telemetry assertion {assertion.id} is already linked to active hypothesis {linked.short_id}."
        )


def _ensure_no_active_metric_hypothesis(store: RealityStore, metric_id: str) -> None:
    for hypothesis in store.list_active_hypotheses(include_proposed=True):
        if hypothesis.telemetry_assertion_id is None:
            continue
        assertion = store.get_telemetry_assertion(hypothesis.telemetry_assertion_id)
        if assertion is None or assertion.valid_until is not None:
            continue
        if assertion.metric_id == metric_id:
            raise typer.BadParameter(
                f"Metric {metric_id} already has active hypothesis {hypothesis.short_id}. "
                "Use `vertex hypothesis update` or `vertex assertion update` instead."
            )


def _parse_operator(value: str) -> AssertionOperator:
    text = _require_text(value, "--operator")
    try:
        return AssertionOperator.from_string(text)
    except ValueError as exc:
        raise typer.BadParameter(f"Unsupported --operator value: {value}") from exc


def _parse_optional_date(value: str | None, *, option_name: str) -> date | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        raise typer.BadParameter(f"{option_name} must be non-empty when provided")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise typer.BadParameter(f"{option_name} must be an ISO date (YYYY-MM-DD)") from exc


def _parse_optional_datetime(value: str | datetime | None, *, option_name: str) -> datetime | None:
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


def _require_text(value: str | None, option_name: str) -> str:
    if value is None or not value.strip():
        raise typer.BadParameter(f"{option_name} must be non-empty")
    return value.strip()


def _optional_text(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value.strip()


def _derive_metric_title(metric_id: str) -> str:
    leaf = metric_id.split(".")[-1]
    return leaf.replace("_", " ").replace("-", " ").title()


def _resolve_statement(
    statement: str | None,
    *,
    metric_id: str,
    operator: AssertionOperator,
    threshold: float,
    definition: MetricDefinition | None,
    assertion: TelemetryAssertion | None,
) -> str:
    if statement is not None and statement.strip():
        return statement.strip()
    if assertion is not None and assertion.description.strip():
        return assertion.description.strip()
    subject = definition.title.strip() if definition is not None and definition.title.strip() else metric_id
    return f"{subject} should {_render_operator_phrase(operator, threshold)}."


def _render_operator_phrase(operator: AssertionOperator, threshold: float) -> str:
    if operator is AssertionOperator.GTE:
        return f"stay at or above {threshold:g}"
    if operator is AssertionOperator.LTE:
        return f"stay at or below {threshold:g}"
    if operator is AssertionOperator.EQ:
        return f"equal {threshold:g}"
    if operator is AssertionOperator.NEQ:
        return f"differ from {threshold:g}"
    if operator is AssertionOperator.PCT_IMPROVEMENT:
        return f"improve by at least {threshold:g}%"
    if operator is AssertionOperator.FORECAST_GTE:
        return f"project to at least {threshold:g} over the next window"
    if operator is AssertionOperator.FORECAST_LTE:
        return f"project to at most {threshold:g} over the next window"
    if operator is AssertionOperator.BURN_RATE_GTE:
        return f"burn down by at least {threshold:g} per window"
    if operator is AssertionOperator.BURN_RATE_LTE:
        return f"burn down by no more than {threshold:g} per window"
    return f"regress by no more than {threshold:g}%"