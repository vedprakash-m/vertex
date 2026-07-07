from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
from typing import Any

import typer
import yaml

from src.core.ado_client import ADOClient
from src.core.exceptions import AuthError, ConfigError, QueryError
from src.core.incident_journal_store import read_incident_entries
from src.core.journal import PROGRAMS_ROOT
from src.core.kusto_client import build_live_kusto_query_executor
from src.core.kusto_query_loader import load_kpi_queries
from src.core.knowledge_store import load_program_knowledge
from src.core.program_fact_store import load_program_facts, project_dependencies, project_risk_entries
from src.core.readiness_engine import (
    ReadinessConfig,
    ReadinessDimensionConfig,
    ReadinessFetchLoaders,
    ReadinessSnapshot,
    build_readiness_snapshot,
    get_readiness_snapshot_path,
    is_snapshot_stale,
    load_readiness_config,
    load_readiness_snapshot,
    snapshot_age_days,
    write_readiness_snapshot,
)


app = typer.Typer(help="Manage launch readiness snapshots.", invoke_without_command=True)


@app.callback(invoke_without_command=True)
def readiness_command(
    ctx: typer.Context,
    program: str | None = typer.Option(None, "--program", help="Program id, e.g. myprogram."),
    format: str = typer.Option("table", "--format", help="Output format: table or json."),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if program is None or not program.strip():
        raise typer.BadParameter("--program is required.")
    _show_readiness(program.strip(), output_format=format, programs_root=PROGRAMS_ROOT)
    raise typer.Exit(code=0)


@app.command("fetch")
def fetch_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    format: str = typer.Option("table", "--format", help="Output format: table or json."),
) -> None:
    program_id = program.strip()
    normalized_format = _normalize_format(format)
    try:
        snapshot, snapshot_path = fetch_readiness_snapshot(program_id, programs_root=PROGRAMS_ROOT)
    except (AuthError, ConfigError, FileNotFoundError, QueryError) as error:
        typer.echo(str(error))
        raise typer.Exit(code=2) from error

    _echo_snapshot(snapshot, output_format=normalized_format, snapshot_path=snapshot_path, warnings=())
    raise typer.Exit(code=0)


@app.command("show")
def show_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    format: str = typer.Option("table", "--format", help="Output format: table or json."),
) -> None:
    _show_readiness(program.strip(), output_format=format, programs_root=PROGRAMS_ROOT)
    raise typer.Exit(code=0)


def _show_readiness(program_id: str, *, output_format: str, programs_root: Path) -> None:
    normalized_format = _normalize_format(output_format)
    load_result = load_readiness_snapshot(program_id, programs_root=programs_root)
    if load_result.snapshot is None:
        _echo_snapshot(None, output_format=normalized_format, snapshot_path=get_readiness_snapshot_path(program_id, programs_root=programs_root), warnings=load_result.warnings)
        raise typer.Exit(code=2)
    _echo_snapshot(
        load_result.snapshot,
        output_format=normalized_format,
        snapshot_path=get_readiness_snapshot_path(program_id, programs_root=programs_root),
        warnings=load_result.warnings,
    )


def _load_ado_query_rows(program_id: str, dimension: ReadinessDimensionConfig, *, programs_root: Path) -> list[dict[str, Any]]:
    ado_settings = _load_program_ado_settings(program_id, programs_root=programs_root)
    query_id = dimension.source.query_id
    if query_id is None:
        raise ConfigError(f"Readiness dimension '{dimension.id}' requires source.query_id for ADO execution.")
    client = ADOClient(
        organization=ado_settings["organization"],
        project=ado_settings["project"],
        timeout=ado_settings["api_timeout_seconds"],
    )
    query_payload = client.get_saved_query(query_id)
    wiql = query_payload.get("wiql")
    if not isinstance(wiql, str) or not wiql.strip():
        raise ConfigError(f"ADO saved query '{query_id}' for readiness dimension '{dimension.id}' does not expose WIQL.")
    work_item_ids = client.execute_wiql(wiql.strip())
    return client.query_work_items_batch(
        work_item_ids,
        fields=("System.Id", "System.State", "System.Title"),
    )


def _load_kusto_query_rows(program_id: str, dimension: ReadinessDimensionConfig, *, programs_root: Path) -> list[dict[str, Any]]:
    query_id = dimension.source.query_id
    if query_id is None:
        raise ConfigError(f"Readiness dimension '{dimension.id}' requires source.query_id for Kusto execution.")
    queries_by_id = {query.id: query for query in load_kpi_queries(program_id, programs_root=programs_root)}
    query = queries_by_id.get(query_id)
    if query is None:
        raise ConfigError(f"Kusto readiness query '{query_id}' is not wired for program '{program_id}'.")
    execute = build_live_kusto_query_executor()
    return execute(query)


def _alias_exists(program_id: str, alias: str, *, programs_root: Path) -> bool:
    knowledge = load_program_knowledge(program_id, programs_root=programs_root)
    normalized_alias = alias.strip().lower()
    directory_aliases = {person.alias.lower() for person in knowledge.people_directory}
    profile_aliases = {profile.alias.lower() for profile in knowledge.people_profiles}
    return normalized_alias in directory_aliases or normalized_alias in profile_aliases


def _load_incident_entries(
    program_id: str,
    dimension: ReadinessDimensionConfig,
    *,
    as_of,
    programs_root: Path,
):
    if dimension.pass_condition.days is None:
        raise ConfigError(f"Readiness dimension '{dimension.id}' requires pass_condition.days for incident_journal.")
    start = as_of - timedelta(days=dimension.pass_condition.days)
    return read_incident_entries(program_id, start=start, end=as_of, programs_root=programs_root)


def _load_program_ado_settings(program_id: str, *, programs_root: Path) -> dict[str, Any]:
    path = programs_root / program_id / "program.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Program '{program_id}' is missing program.yaml.")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"Invalid YAML in {path}: {error}") from error
    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}.")
    ado = document.get("ado")
    if not isinstance(ado, dict):
        raise ConfigError(f"Program '{program_id}' does not define an ado configuration in {path}.")
    organization = str(ado.get("organization") or "").strip()
    project = str(ado.get("project") or "").strip()
    if not organization or not project:
        raise ConfigError(f"Program '{program_id}' must define ado.organization and ado.project in {path}.")
    api_timeout_seconds = ado.get("api_timeout_seconds")
    timeout = int(api_timeout_seconds) if isinstance(api_timeout_seconds, (int, float, str)) and str(api_timeout_seconds).strip() else 30
    return {
        "organization": organization,
        "project": project,
        "api_timeout_seconds": timeout,
    }


def _normalize_format(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"table", "json"}:
        raise typer.BadParameter("--format must be 'table' or 'json'.")
    return normalized


def fetch_readiness_snapshot(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[ReadinessSnapshot, Path]:
    config = load_readiness_config(program_id, programs_root=programs_root)
    snapshot = build_readiness_snapshot(
        program_id,
        config,
        loaders=ReadinessFetchLoaders(
            load_ado_query_rows=lambda dimension: _load_ado_query_rows(program_id, dimension, programs_root=programs_root),
            load_kusto_query_rows=lambda dimension: _load_kusto_query_rows(program_id, dimension, programs_root=programs_root),
            alias_exists=lambda alias: _alias_exists(program_id, alias, programs_root=programs_root),
            load_dependencies=lambda: project_dependencies(
                load_program_facts(
                    program_id,
                    programs_root=programs_root,
                    fact_types=("dependency.link",),
                )
            ),
            load_risk_entries=lambda: project_risk_entries(
                load_program_facts(
                    program_id,
                    programs_root=programs_root,
                    fact_types=("risk.entry",),
                )
            ),
            load_incident_entries=lambda dimension, as_of: _load_incident_entries(program_id, dimension, as_of=as_of, programs_root=programs_root),
        ),
    )
    snapshot_path = write_readiness_snapshot(program_id, snapshot, programs_root=programs_root)
    return snapshot, snapshot_path


def render_readiness_snapshot_output(
    snapshot: ReadinessSnapshot | None,
    *,
    output_format: str,
    snapshot_path: Path,
    warnings: tuple[str, ...] = (),
) -> str:
    normalized_format = _normalize_format(output_format)
    if normalized_format == "json":
        return json.dumps(
            _snapshot_payload(snapshot, snapshot_path=snapshot_path, warnings=warnings),
            indent=2,
            sort_keys=True,
        )
    return _render_table(snapshot, snapshot_path=snapshot_path, warnings=warnings)


def _echo_snapshot(
    snapshot: ReadinessSnapshot | None,
    *,
    output_format: str,
    snapshot_path: Path,
    warnings: tuple[str, ...],
) -> None:
    typer.echo(
        render_readiness_snapshot_output(
            snapshot,
            output_format=output_format,
            snapshot_path=snapshot_path,
            warnings=warnings,
        )
    )


def _snapshot_payload(
    snapshot: ReadinessSnapshot | None,
    *,
    snapshot_path: Path,
    warnings: tuple[str, ...],
) -> dict[str, Any]:
    if snapshot is None:
        return {
            "snapshot_path": str(snapshot_path),
            "warnings": list(warnings),
            "snapshot": None,
        }
    return {
        "snapshot_path": str(snapshot_path),
        "warnings": list(warnings),
        "snapshot": {
            "program_id": snapshot.program_id,
            "fetched_at": snapshot.fetched_at.isoformat(),
            "snapshot_max_age_days": snapshot.snapshot_max_age_days,
            "content_sha256": snapshot.content_sha256,
            "passed_count": snapshot.passed_count,
            "total_count": snapshot.total_count,
            "is_stale": is_snapshot_stale(snapshot),
            "age_days": snapshot_age_days(snapshot),
            "dimensions": [
                {
                    "id": dimension.id,
                    "name": dimension.name,
                    "gate_id": dimension.gate_id,
                    "passed": dimension.passed,
                    "status": dimension.status,
                    "summary": dimension.summary,
                    "observed_value": dimension.observed_value,
                    "threshold": dimension.threshold,
                    "evidence_refs": list(dimension.evidence_refs),
                    "details": dimension.details or {},
                }
                for dimension in snapshot.dimensions
            ],
        },
    }


def _render_table(
    snapshot: ReadinessSnapshot | None,
    *,
    snapshot_path: Path,
    warnings: tuple[str, ...],
) -> str:
    lines: list[str] = []
    if snapshot is None:
        lines.append(f"Launch Readiness snapshot unavailable at {snapshot_path}")
        lines.extend(f"WARNING: {warning}" for warning in warnings)
        return "\n".join(lines)

    age_days = snapshot_age_days(snapshot)
    stale = is_snapshot_stale(snapshot)
    lines.append(f"Launch Readiness - {snapshot.program_id}")
    lines.append(
        f"Fetched: {snapshot.fetched_at.isoformat()} | Score: {snapshot.passed_count}/{snapshot.total_count} passed | Age: {age_days}d"
        + (f" | STALE>{snapshot.snapshot_max_age_days}d" if stale else "")
    )
    lines.append(f"Snapshot: {snapshot_path}")
    lines.extend(f"WARNING: {warning}" for warning in warnings)
    headers = ("status", "gate", "dimension", "observed", "threshold")
    rows = [
        {
            "status": "PASS" if dimension.passed else "FAIL",
            "gate": dimension.gate_id,
            "dimension": dimension.name,
            "observed": dimension.observed_value,
            "threshold": dimension.threshold,
        }
        for dimension in snapshot.dimensions
    ]
    widths = {
        header: max(len(header), *(len(str(row[header])) for row in rows))
        for header in headers
    }
    lines.append("  ".join(header.ljust(widths[header]) for header in headers))
    lines.append("  ".join("-" * widths[header] for header in headers))
    for row in rows:
        lines.append("  ".join(str(row[header]).ljust(widths[header]) for header in headers))
    for dimension in snapshot.dimensions:
        evidence = ", ".join(dimension.evidence_refs[:3]) if dimension.evidence_refs else "none"
        lines.append(f"- {dimension.gate_id}: {dimension.summary} | evidence: {evidence}")
    return "\n".join(lines)