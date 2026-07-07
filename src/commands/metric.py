from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

import typer

from src.commands import gather as gather_helpers
from src.core.ado_client import ADOClient
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.exceptions import ConfigError, QueryError
from src.core.hypothesis_models import AssertionOperator, TelemetryAssertion
from src.core.kusto_client import KustoColumn
from src.core.kusto_query_loader import load_kpi_queries
from src.core.metric_binding_validator import (
    MetricBindingProbe,
    build_live_metric_binding_probe,
    validate_metric_source_binding,
)
from src.core.metric_models import MetricAggregation, MetricDefinition, MetricObservation, MetricSourceBinding, ObservationWindow
from src.core.models_v2 import KustoQuery, Program, Workstream
from src.core.metric_registry import METRICS_ROOT, load_metric_definition_map
from src.core.reality_store import RealityStore


app = typer.Typer(help="Metric binding operator commands.")

# Backward-compatible seam used by focused tests.
_live_metric_binding_probe = build_live_metric_binding_probe


def _resolve_runtime_db_root(
    db_root: Path | None,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path | None:
    if db_root is not None:
        return db_root
    if os.environ.get("VERTEX_DB_PATH"):
        return None
    return programs_root.parent / "vertex-db"


@app.command("bind")
def bind_metric_source_command(
    program: str = typer.Option(..., "--program", help="Program identifier."),
    query_id: str = typer.Option(..., "--query-id", help="Existing KPI query id to bind."),
    binding_id: str | None = typer.Option(None, "--binding-id", help="Optional explicit binding id."),
    owner_alias: str | None = typer.Option(None, "--owner-alias", help="Optional owner alias for the binding."),
    db_root: Path | None = typer.Option(None, "--db-root", help="Override the SQLite root for tests or local runs."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, hidden=True),
) -> None:
    program_id = _require_text(program, "--program")
    normalized_query_id = _require_text(query_id, "--query-id")
    query = _load_kpi_query(program_id, query_id=normalized_query_id, programs_root=programs_root)
    metric_id = _require_metric_id(query)
    normalized_binding_id = _optional_text(binding_id) or _default_binding_id(program_id, normalized_query_id)

    store = RealityStore(program_id, db_root=_resolve_runtime_db_root(db_root, programs_root=programs_root))
    store.initialize()

    binding = MetricSourceBinding(
        binding_id=normalized_binding_id,
        metric_id=metric_id,
        program_id=program_id,
        source_kind="wiql" if query.engine == "wiql" else "kusto",
        cluster=query.cluster or None,
        database=query.database or None,
        kql_template=(query.wiql if query.engine == "wiql" else query.kql) or None,
        result_column=query.result_column,
        owner_alias=_optional_text(owner_alias),
    )
    store.upsert_metric_source_binding(binding)
    typer.echo(f"Bound query {normalized_query_id} to metric {metric_id} as {normalized_binding_id}.")
    raise typer.Exit(code=0)


@app.command("provision")
def provision_metric_rollout_command(
    program: str = typer.Option(..., "--program", help="Program identifier."),
    query_id: str | None = typer.Option(None, "--query-id", help="Existing KPI query id to provision."),
    all_eligible: bool = typer.Option(False, "--all-eligible", help="Provision every KPI catalog entry with enough metadata to create assertion and binding records."),
    binding_id: str | None = typer.Option(None, "--binding-id", help="Optional explicit binding id."),
    assertion_id: str | None = typer.Option(None, "--assertion-id", help="Optional explicit assertion id."),
    owner_alias: str | None = typer.Option(None, "--owner-alias", help="Optional owner alias for the binding."),
    created_by: str | None = typer.Option(None, "--created-by", help="Optional actor recorded on the assertion."),
    window_days: int = typer.Option(7, "--window-days", min=1, help="Trailing observation window in days."),
    tolerance_rel: float = typer.Option(0.10, "--tolerance-rel", min=0.0, help="Relative tolerance for delta magnitude."),
    tolerance_abs: float | None = typer.Option(None, "--tolerance-abs", help="Optional absolute tolerance."),
    sustain_min_observations: int = typer.Option(3, "--sustain-min-observations", min=1, help="Consecutive violations required before challenge emission."),
    cooldown_hours: int = typer.Option(24, "--cooldown-hours", min=0, help="Cooldown after dismissal or resolution."),
    severity_override: str | None = typer.Option(None, "--severity-override", help="Optional severity override: info, warn, alert."),
    description: str = typer.Option("", "--description", help="Optional human-readable description."),
    db_root: Path | None = typer.Option(None, "--db-root", help="Override the SQLite root for tests or local runs."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, hidden=True),
    metrics_root: Path = typer.Option(METRICS_ROOT, hidden=True),
) -> None:
    program_id = _require_text(program, "--program")
    store = RealityStore(program_id, db_root=_resolve_runtime_db_root(db_root, programs_root=programs_root))
    store.initialize()
    if (_optional_text(query_id) is None) == (not all_eligible):
        raise typer.BadParameter("Provide exactly one of --query-id or --all-eligible.")

    queries = (
        (_load_kpi_query(program_id, query_id=_require_text(query_id, "--query-id"), programs_root=programs_root),)
        if not all_eligible
        else tuple(load_kpi_queries(program_id, programs_root=programs_root))
    )
    metric_ids = tuple(
        metric_id
        for query in queries
        if (metric_id := _optional_text(query.metric_id)) is not None
    )
    metric_definitions = load_metric_definition_map(
        metrics_root=metrics_root,
        metric_ids=metric_ids or None,
        as_of=datetime.now(timezone.utc),
    )

    provisioned = 0
    skipped = 0
    for query in queries:
        try:
            outcome = _provision_metric_rollout_for_query(
                store,
                program_id=program_id,
                query=query,
                binding_id=binding_id,
                assertion_id=assertion_id,
                owner_alias=owner_alias,
                created_by=created_by,
                window_days=window_days,
                tolerance_rel=tolerance_rel,
                tolerance_abs=tolerance_abs,
                sustain_min_observations=sustain_min_observations,
                cooldown_hours=cooldown_hours,
                severity_override=severity_override,
                description=description,
                metric_definition=metric_definitions.get(_optional_text(query.metric_id) or ""),
            )
        except typer.BadParameter as error:
            if not all_eligible:
                raise
            skipped += 1
            typer.echo(f"Skipped query {query.id}: {error}")
            continue

        provisioned += 1
        typer.echo(
            f"Provisioned query {query.id}: binding {outcome.binding_id} ({'created' if outcome.binding_created else 'reused'}), assertion {outcome.assertion_id} ({'created' if outcome.assertion_created else 'reused'})."
        )

    if all_eligible:
        typer.echo(f"Bulk provision summary: {provisioned} provisioned, {skipped} skipped.")
    raise typer.Exit(code=0)


class _ProvisionOutcome:
    def __init__(self, *, binding_id: str, binding_created: bool, assertion_id: str, assertion_created: bool) -> None:
        self.binding_id = binding_id
        self.binding_created = binding_created
        self.assertion_id = assertion_id
        self.assertion_created = assertion_created


class _MetricRolloutStatus:
    def __init__(
        self,
        *,
        query_id: str,
        metric_id: str | None,
        eligible: bool,
        eligible_reason: str | None,
        binding_count: int,
        assertion_count: int,
    ) -> None:
        self.query_id = query_id
        self.metric_id = metric_id
        self.eligible = eligible
        self.eligible_reason = eligible_reason
        self.binding_count = binding_count
        self.assertion_count = assertion_count

    @property
    def ready(self) -> bool:
        return self.eligible and self.binding_count > 0 and self.assertion_count > 0


def _provision_metric_rollout_for_query(
    store: RealityStore,
    *,
    program_id: str,
    query: KustoQuery,
    binding_id: str | None,
    assertion_id: str | None,
    owner_alias: str | None,
    created_by: str | None,
    window_days: int,
    tolerance_rel: float,
    tolerance_abs: float | None,
    sustain_min_observations: int,
    cooldown_hours: int,
    severity_override: str | None,
    description: str,
    metric_definition: MetricDefinition | None,
) -> _ProvisionOutcome:
    metric_id = _require_metric_id(query)
    normalized_binding_id = _resolve_binding_id(
        store,
        binding_id,
        program_id=program_id,
        query_id=query.id,
        metric_id=metric_id,
    )
    normalized_assertion_id = _resolve_assertion_id(
        store,
        assertion_id,
        program_id=program_id,
        query=query,
        metric_id=metric_id,
    )
    operator_text, threshold = _resolve_assertion_defaults(metric_id, metric_definition)
    parsed_operator = _parse_assertion_operator(operator_text)
    parsed_severity = _parse_optional_severity(severity_override)

    existing_binding = store.get_metric_source_binding(normalized_binding_id)
    binding_created = False
    if existing_binding is None or existing_binding.program_id != program_id or existing_binding.valid_until is not None:
        existing_binding = MetricSourceBinding(
            binding_id=normalized_binding_id,
            metric_id=metric_id,
            program_id=program_id,
            source_kind="wiql" if query.engine == "wiql" else "kusto",
            cluster=query.cluster or None,
            database=query.database or None,
            kql_template=(query.wiql if query.engine == "wiql" else query.kql) or None,
            result_column=query.result_column,
            owner_alias=_optional_text(owner_alias),
        )
        store.upsert_metric_source_binding(existing_binding)
        binding_created = True
    elif existing_binding.metric_id != metric_id:
        raise typer.BadParameter(
            f"Metric source binding {normalized_binding_id} targets metric {existing_binding.metric_id}, not {metric_id}."
        )

    existing_assertion = store.get_telemetry_assertion(normalized_assertion_id)
    assertion_created = False
    if existing_assertion is None or existing_assertion.valid_until is not None:
        existing_assertion = TelemetryAssertion(
            id=normalized_assertion_id,
            program_id=program_id,
            metric_id=metric_id,
            window=ObservationWindow(days=window_days, aggregation=MetricAggregation.LAST),
            operator=parsed_operator,
            threshold=threshold,
            tolerance_rel=float(tolerance_rel),
            tolerance_abs=float(tolerance_abs) if tolerance_abs is not None else None,
            sustain_min_observations=int(sustain_min_observations),
            cooldown_hours=int(cooldown_hours),
            severity_override=parsed_severity,
            description=description.strip(),
            created_by=_optional_text(created_by) or "vertex/admin-metric-provision",
        )
        store.upsert_telemetry_assertion(existing_assertion)
        assertion_created = True
    elif existing_assertion.metric_id != metric_id:
        raise typer.BadParameter(
            f"Assertion {normalized_assertion_id} targets metric {existing_assertion.metric_id}, not {metric_id}."
        )

    return _ProvisionOutcome(
        binding_id=normalized_binding_id,
        binding_created=binding_created,
        assertion_id=normalized_assertion_id,
        assertion_created=assertion_created,
    )


@app.command("status")
def metric_rollout_status_command(
    program: str = typer.Option(..., "--program", help="Program identifier."),
    query_id: str | None = typer.Option(None, "--query-id", help="Existing KPI query id to inspect."),
    all_eligible: bool = typer.Option(False, "--all-eligible", help="Inspect rollout readiness for every KPI query eligible for deterministic provisioning."),
    format: str = typer.Option("text", "--format", help="Output format: text or json."),
    db_root: Path | None = typer.Option(None, "--db-root", help="Override the SQLite root for tests or local runs."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, hidden=True),
    metrics_root: Path = typer.Option(METRICS_ROOT, hidden=True),
) -> None:
    program_id = _require_text(program, "--program")
    output_format = _normalize_format(format)
    if (_optional_text(query_id) is None) == (not all_eligible):
        raise typer.BadParameter("Provide exactly one of --query-id or --all-eligible.")

    queries = (
        (_load_kpi_query(program_id, query_id=_require_text(query_id, "--query-id"), programs_root=programs_root),)
        if not all_eligible
        else tuple(load_kpi_queries(program_id, programs_root=programs_root))
    )
    metric_ids = tuple(metric_id for query in queries if (metric_id := _optional_text(query.metric_id)) is not None)
    metric_definitions = load_metric_definition_map(
        metrics_root=metrics_root,
        metric_ids=metric_ids or None,
        as_of=datetime.now(timezone.utc),
    )
    store = RealityStore(program_id, db_root=_resolve_runtime_db_root(db_root, programs_root=programs_root))
    store.initialize()
    statuses = tuple(_build_metric_rollout_status(store, query, metric_definitions) for query in queries)
    if all_eligible:
        statuses = tuple(status for status in statuses if status.eligible)

    if output_format == "json":
        typer.echo(json.dumps([_serialize_metric_rollout_status(status) for status in statuses], indent=2))
        raise typer.Exit(code=0)

    if not statuses:
        typer.echo(f"No rollout status entries found for {program_id}.")
        raise typer.Exit(code=0)

    for status in statuses:
        typer.echo(
            " | ".join(
                (
                    status.query_id,
                    f"metric={status.metric_id or '-'}",
                    f"eligible={'yes' if status.eligible else 'no'}",
                    f"binding_count={status.binding_count}",
                    f"assertion_count={status.assertion_count}",
                    f"ready={'yes' if status.ready else 'no'}",
                    f"reason={status.eligible_reason or '-'}",
                )
            )
        )
    raise typer.Exit(code=0)


def _build_metric_rollout_status(
    store: RealityStore,
    query: KustoQuery,
    metric_definitions: dict[str, MetricDefinition],
) -> _MetricRolloutStatus:
    metric_id = _optional_text(query.metric_id)
    eligible, eligible_reason = _query_rollout_eligibility(query, metric_definitions)
    if metric_id is None:
        return _MetricRolloutStatus(
            query_id=query.id,
            metric_id=None,
            eligible=eligible,
            eligible_reason=eligible_reason,
            binding_count=0,
            assertion_count=0,
        )
    return _MetricRolloutStatus(
        query_id=query.id,
        metric_id=metric_id,
        eligible=eligible,
        eligible_reason=eligible_reason,
        binding_count=len(store.list_active_metric_source_bindings(metric_id=metric_id)),
        assertion_count=len(store.list_telemetry_assertions(metric_id=metric_id)),
    )


def _query_rollout_eligibility(
    query: KustoQuery,
    metric_definitions: dict[str, MetricDefinition],
) -> tuple[bool, str | None]:
    metric_id = _optional_text(query.metric_id)
    if metric_id is None:
        return False, "missing metric_id"
    definition = metric_definitions.get(metric_id)
    if definition is None:
        return False, "missing metric definition"
    if definition.slo_target is None or definition.slo_direction is None:
        return False, "missing slo_target/slo_direction"
    return True, None


@app.command("list")
def list_metric_definitions_command(
    product: str | None = typer.Option(None, "--product", help="Filter to a product id or metric-id prefix."),
    format: str = typer.Option("text", "--format", help="Output format: text or json."),
) -> None:
    output_format = _normalize_format(format)
    definitions = tuple(
        definition
        for definition in sorted(
            load_metric_definition_map(metrics_root=METRICS_ROOT, as_of=datetime.now(timezone.utc)).values(),
            key=lambda item: item.id,
        )
        if _matches_product(definition, product)
    )
    if output_format == "json":
        typer.echo(json.dumps([_serialize_metric_definition(definition) for definition in definitions], indent=2))
        raise typer.Exit(code=0)

    if not definitions:
        if product is None or not product.strip():
            typer.echo("No metric definitions found.")
        else:
            typer.echo(f"No metric definitions found for product {product.strip()}.")
        raise typer.Exit(code=0)

    typer.echo(_render_metric_definition_list(definitions))
    raise typer.Exit(code=0)


@app.command("history")
def metric_history_command(
    metric: str = typer.Option(..., "--metric", help="Metric identifier."),
    program: str = typer.Option(..., "--program", help="Program identifier."),
    format: str = typer.Option("text", "--format", help="Output format: text or json."),
    db_root: Path | None = typer.Option(None, "--db-root", help="Override the SQLite root for tests or local runs."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, hidden=True),
) -> None:
    output_format = _normalize_format(format)
    store = RealityStore(program.strip(), db_root=_resolve_runtime_db_root(db_root, programs_root=programs_root))
    store.initialize()
    observations = store.list_metric_observations(metric.strip())
    if output_format == "json":
        payload = {
            "program_id": program.strip(),
            "metric_id": metric.strip(),
            "observation_count": len(observations),
            "observations": [_serialize_metric_observation(observation) for observation in observations],
        }
        typer.echo(json.dumps(payload, indent=2))
        raise typer.Exit(code=0)

    typer.echo(_render_metric_history(program.strip(), metric.strip(), observations))
    raise typer.Exit(code=0)

@app.command("validate")
def validate_metric_bindings_command(
    program: str = typer.Option(..., "--program", help="Program identifier."),
    binding_id: str | None = typer.Option(None, "--binding-id", help="Specific binding id to validate."),
    all_bindings: bool = typer.Option(False, "--all", help="Validate all active bindings for the program."),
    db_root: Path | None = typer.Option(None, "--db-root", help="Override the SQLite root for tests or local runs."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, hidden=True),
) -> None:
    if (binding_id is None) == (not all_bindings):
        raise typer.BadParameter("Provide exactly one of --binding-id or --all.")

    store = RealityStore(program.strip(), db_root=_resolve_runtime_db_root(db_root, programs_root=programs_root))
    store.initialize()

    now = datetime.now(timezone.utc)
    metric_definitions = load_metric_definition_map(metrics_root=METRICS_ROOT, as_of=now)
    probe = _build_program_metric_binding_probe(program.strip(), programs_root=programs_root)

    if all_bindings:
        bindings = store.list_active_metric_source_bindings()
        if not bindings:
            typer.echo(f"No active metric bindings found for program {program.strip()}.")
            raise typer.Exit(code=0)
    else:
        binding = store.get_metric_source_binding(_require_text(binding_id, "--binding-id"))
        if binding is None or binding.program_id != program.strip() or binding.valid_until is not None:
            raise typer.BadParameter(f"Unknown active metric binding: {binding_id}")
        bindings = (binding,)

    failures: list[str] = []
    successes = 0
    for binding in bindings:
        try:
            validated_binding = validate_metric_source_binding(
                binding,
                metric_definitions=metric_definitions,
                probe=probe,
                validated_at=now,
            )
        except (ConfigError, QueryError, ValueError) as error:
            store.upsert_metric_source_binding(replace(binding, validated=False))
            failures.append(f"{binding.binding_id}: {error}")
            continue

        store.upsert_metric_source_binding(validated_binding)
        successes += 1
        typer.echo(f"Validated binding {binding.binding_id} for metric {binding.metric_id}.")

    if failures:
        for failure in failures:
            typer.echo(failure, err=True)
        raise typer.Exit(code=2)

    if all_bindings:
        typer.echo(f"Validated {successes} binding(s) for program {program.strip()}.")
    raise typer.Exit(code=0)


def _require_text(value: str | None, option_name: str) -> str:
    if value is None or not value.strip():
        raise typer.BadParameter(f"{option_name} is required.")
    return value.strip()


def _optional_text(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value.strip()


def _load_kpi_query(program_id: str, *, query_id: str, programs_root: Path) -> KustoQuery:
    for query in load_kpi_queries(program_id, programs_root=programs_root):
        if query.id == query_id:
            return query
    raise typer.BadParameter(f"KPI query {query_id} was not found for program {program_id}.")


def _require_metric_id(query: KustoQuery) -> str:
    metric_id = _optional_text(query.metric_id)
    if metric_id is None:
        raise typer.BadParameter(f"KPI query {query.id} has no metric_id; cannot create a source binding.")
    return metric_id


def _resolve_assertion_id(
    store: RealityStore,
    assertion_id: str | None,
    *,
    program_id: str,
    query: KustoQuery,
    metric_id: str,
) -> str:
    normalized = _optional_text(assertion_id)
    if normalized is not None:
        return normalized
    active_assertions = store.list_telemetry_assertions(metric_id=metric_id)
    if len(active_assertions) == 1:
        return active_assertions[0].id
    if len(active_assertions) > 1:
        assertion_ids = ", ".join(assertion.id for assertion in active_assertions)
        raise typer.BadParameter(
            f"--assertion-id is required because metric {metric_id} has multiple active assertions: {assertion_ids}."
        )
    query_assertion_ids = tuple(
        item.strip() for item in (query.assertion_ids or ()) if isinstance(item, str) and item.strip()
    )
    if len(query_assertion_ids) == 1:
        return query_assertion_ids[0]
    return str(uuid5(NAMESPACE_URL, f"{program_id}|telemetry_assertion|{query.id}"))


def _resolve_binding_id(
    store: RealityStore,
    value: str | None,
    *,
    program_id: str,
    query_id: str,
    metric_id: str,
) -> str:
    normalized = _optional_text(value)
    if normalized is not None:
        return normalized
    active_bindings = store.list_active_metric_source_bindings(metric_id=metric_id)
    if len(active_bindings) == 1:
        return active_bindings[0].binding_id
    if len(active_bindings) > 1:
        binding_ids = ", ".join(binding.binding_id for binding in active_bindings)
        raise typer.BadParameter(
            f"--binding-id is required because metric {metric_id} has multiple active bindings: {binding_ids}."
        )
    return _default_binding_id(program_id, query_id)


def _resolve_assertion_defaults(metric_id: str, definition: MetricDefinition | None) -> tuple[str, float]:
    if definition is None or definition.slo_target is None or definition.slo_direction is None:
        raise typer.BadParameter(
            f"Metric definition {metric_id} must declare slo_direction and slo_target before admin metric provision can create its assertion."
        )
    if definition.slo_direction == "gte":
        return ">=", float(definition.slo_target)
    if definition.slo_direction == "lte":
        return "<=", float(definition.slo_target)
    raise typer.BadParameter(
        f"Metric definition {metric_id} has unsupported slo_direction {definition.slo_direction!r}; expected 'gte' or 'lte'."
    )


def _parse_assertion_operator(value: str) -> AssertionOperator:
    normalized = value.strip().lower()
    if normalized in {">=", "gte"}:
        return AssertionOperator.GTE
    if normalized in {"<=", "lte"}:
        return AssertionOperator.LTE
    raise typer.BadParameter(f"Unsupported assertion operator: {value}")


def _parse_optional_severity(value: str | None) -> "Literal['info', 'warn', 'alert'] | None":
    from typing import cast

    normalized = _optional_text(value)
    if normalized is None:
        return None
    severity = normalized.lower()
    if severity not in {"info", "warn", "alert"}:
        raise typer.BadParameter("--severity-override must be one of: info, warn, alert.")
    return cast("Literal['info', 'warn', 'alert']", severity)


def _default_binding_id(program_id: str, query_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"{program_id}|metric_binding|{query_id}"))


def _build_program_metric_binding_probe(program_id: str, *, programs_root: Path) -> MetricBindingProbe:
    kusto_probe = _live_metric_binding_probe()
    program: Program | None = None
    workstreams: tuple[Workstream, ...] | None = None
    ado_client: ADOClient | None = None
    current_iteration_path_by_team: dict[str | None, str | None] = {}

    def _ensure_wiql_context() -> tuple[Program, tuple[Workstream, ...], ADOClient]:
        nonlocal program, workstreams, ado_client
        if ado_client is not None:
            assert program is not None
            assert workstreams is not None
            return program, workstreams, ado_client
        program, workstreams = gather_helpers._load_program_context(program_id, programs_root)
        if program.ado is None:
            raise typer.BadParameter(f"Program {program_id} does not have ADO configured.")
        ado_client = ADOClient(
            program.ado.organization,
            program.ado.project,
            timeout=program.ado.api_timeout_seconds,
        )
        assert workstreams is not None
        return program, workstreams, ado_client

    def probe(binding: MetricSourceBinding) -> tuple[list[dict[str, object]], tuple[KustoColumn, ...]]:
        if binding.source_kind != "wiql":
            return kusto_probe(binding)
        resolved_program, resolved_workstreams, resolved_ado_client = _ensure_wiql_context()
        query = KustoQuery(
            id=binding.binding_id,
            cluster=binding.cluster or "",
            database=binding.database or "",
            kql="",
            section=binding.metric_id,
            render_as="metric_highlight",
            confidence="medium",
            result_column=binding.result_column,
            engine="wiql",
            wiql=binding.kql_template,
        )
        resolved_wiql, _ = gather_helpers._resolve_wiql_query_text(
            query,
            program=resolved_program,
            workstreams=resolved_workstreams,
            client=resolved_ado_client,
            current_iteration_path_by_team=current_iteration_path_by_team,
        )
        if not resolved_wiql:
            raise QueryError(f"WIQL binding {binding.binding_id} has no WIQL text.")
        work_item_ids = tuple(resolved_ado_client.execute_wiql(resolved_wiql))
        result_column = binding.result_column or "Count"
        return [{result_column: len(work_item_ids)}], (KustoColumn(result_column, "long"),)

    return probe


def _normalize_format(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"text", "json"}:
        raise typer.BadParameter("--format must be one of: text, json.")
    return normalized


def _serialize_metric_rollout_status(status: _MetricRolloutStatus) -> dict[str, object]:
    return {
        "query_id": status.query_id,
        "metric_id": status.metric_id,
        "eligible": status.eligible,
        "eligible_reason": status.eligible_reason,
        "binding_count": status.binding_count,
        "assertion_count": status.assertion_count,
        "ready": status.ready,
    }


def _matches_product(definition: MetricDefinition, product: str | None) -> bool:
    if product is None or not product.strip():
        return True
    needle = product.strip().lower()
    owning_product = (definition.owning_product_id or "").lower()
    metric_prefix = definition.id.split(".", 1)[0].lower()
    return owning_product == needle or metric_prefix == needle


def _serialize_metric_definition(definition: MetricDefinition) -> dict[str, object]:
    return {
        "metric_id": definition.id,
        "title": definition.title,
        "unit": definition.unit,
        "aggregation": definition.aggregation.value,
        "dimension_columns": list(definition.dimension_columns),
        "owning_product_id": definition.owning_product_id,
        "freshness_tier": definition.freshness_tier,
        "retention_days": definition.retention_days,
        "policy_version": definition.policy_version,
        "valid_from": definition.valid_from.isoformat(),
        "valid_until": definition.valid_until.isoformat() if definition.valid_until is not None else None,
    }


def _render_metric_definition_list(definitions: tuple[MetricDefinition, ...]) -> str:
    lines = ["Metric Definitions", "------------------"]
    for definition in definitions:
        product = definition.owning_product_id or definition.id.split(".", 1)[0]
        dimensions = ", ".join(definition.dimension_columns) if definition.dimension_columns else "-"
        lines.append(
            f"{definition.id} | {definition.title} | agg={definition.aggregation.value} | tier={definition.freshness_tier} | product={product} | dims={dimensions}"
        )
    return "\n".join(lines)


def _serialize_metric_observation(observation: MetricObservation) -> dict[str, object]:
    return {
        "observation_id": observation.observation_id,
        "program_id": observation.program_id,
        "metric_id": observation.metric_id,
        "dimensions_json": observation.dimensions_json,
        "measurement_period_start": observation.measurement_period_start.isoformat(),
        "measurement_period_end": observation.measurement_period_end.isoformat(),
        "observed_at": observation.observed_at.isoformat(),
        "value_num": observation.value_num,
        "value_text": observation.value_text,
        "sample_count": observation.sample_count,
        "quality_state": observation.quality_state.value,
        "source_binding_id": observation.source_binding_id,
        "binding_version": observation.binding_version,
        "ingestion_run_id": observation.ingestion_run_id,
        "corrected_at": observation.corrected_at.isoformat() if observation.corrected_at is not None else None,
        "corrected_reason": observation.corrected_reason,
        "inserted_at": observation.inserted_at.isoformat(),
        "is_corrected": observation.corrected_at is not None,
    }


def _render_metric_history(program_id: str, metric_id: str, observations: tuple[MetricObservation, ...]) -> str:
    header = f"Metric History - {metric_id} ({program_id})"
    lines = [header, "-" * len(header)]
    if not observations:
        lines.append("No observations found.")
        return "\n".join(lines)

    lines.append(f"Observations: {len(observations)}")
    for observation in observations:
        value = observation.value_num if observation.value_num is not None else observation.value_text
        corrected_marker = " [corrected]" if observation.corrected_at is not None else ""
        binding = observation.source_binding_id or "manual"
        lines.append(
            " | ".join(
                (
                    observation.measurement_period_end.isoformat(),
                    f"value={value}",
                    f"quality={observation.quality_state.value}{corrected_marker}",
                    f"binding={binding}",
                )
            )
        )
    return "\n".join(lines)
