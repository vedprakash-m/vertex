from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
from typing import Callable

import typer

from src.commands import freshness as freshness_helpers
from src.commands import gather as gather_helpers
from src.core.ado_client import ADOClient
from src.core.channel_registry_store import ChannelRegistryStore
from src.core.program_paths import resolve_channel_registry_path_for_read
from src.core.leakage_detector import detect_leakage, load_approved_workiq_signals
from src.core.models import WorkItem
from src.core.models_v2 import Program, VitalityAggregate, VitalityScore, Workstream
from src.core.knowledge_store import load_program_knowledge
from src.core.store_factory import build_trajectory_store_for_program_id
from src.core.vitality_reporting import effective_vitality_exempt_aliases, vitality_settings_from_program
from src.core.vitality_scorer import aggregate_vitality, score_vitality, summarize_vitality
from src.core.yaml_utils import load_yaml_mapping
from src.core.edition_resolver import PROGRAMS_ROOT


VitalityLoader = Callable[[Program, tuple[Workstream, ...], datetime], tuple[tuple[WorkItem, ...], int]]

_BATCH_FIELDS = freshness_helpers._BATCH_FIELDS


@dataclass(frozen=True, slots=True)
class VitalityArtifacts:
    program_id: str
    items: tuple[WorkItem, ...]
    scored_items: tuple[VitalityScore, ...]
    owner_aggregates: tuple[VitalityAggregate, ...]
    workstream_aggregates: tuple[VitalityAggregate, ...]
    ado_calls: int


def vitality_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    owner: str | None = typer.Option(None, "--owner", help="Filter to a single owner alias."),
    workstream: str | None = typer.Option(None, "--workstream", help="Filter to a single workstream id."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    artifacts = generate_vitality_report(program, owner_alias=owner, workstream_id=workstream)
    typer.echo(_render_vitality_report(artifacts, format=format))
    raise typer.Exit(code=0)


def generate_vitality_report(
    program_id: str,
    *,
    as_of: datetime | None = None,
    programs_root: Path | None = None,
    owner_alias: str | None = None,
    workstream_id: str | None = None,
    loader: VitalityLoader | None = None,
) -> VitalityArtifacts:
    resolved_programs_root = programs_root or PROGRAMS_ROOT
    program, workstreams = gather_helpers._load_program_context(program_id, resolved_programs_root)
    program_path = resolved_programs_root / program_id / "program.yaml"
    settings = vitality_settings_from_program(load_yaml_mapping(program_path, required=False, default={}))
    knowledge = load_program_knowledge(program_id, programs_root=resolved_programs_root)
    exempt_aliases = effective_vitality_exempt_aliases(settings, knowledge.people_directory)
    current_time = as_of or datetime.now(timezone.utc)
    items, ado_calls = (loader or _load_vitality_items)(program, workstreams, current_time)
    eligible_items = tuple(
        item
        for item in items
        if _owner_alias(item) not in exempt_aliases
    )
    trajectory_store = build_trajectory_store_for_program_id(
        program_id,
        programs_root=resolved_programs_root,
    )
    leakage = detect_leakage(
        eligible_items,
        load_approved_workiq_signals(
            program_id,
            as_of=current_time,
            programs_root=resolved_programs_root,
        ),
        trajectory_loader=lambda work_item_id: trajectory_store.read(
            program_id,
            work_item_id,
        ),
    )
    scores = score_vitality(
        eligible_items,
        as_of=current_time,
        workstream_resolver=lambda item: gather_helpers._resolve_workstream_id(item.area_path, workstreams),
        leakage=leakage,
        leakage_signal_threshold=settings.sparse_workiq_threshold,
    )
    if owner_alias is not None:
        normalized_owner = owner_alias.strip().lower()
        scores = tuple(score for score in scores if score.owner_alias == normalized_owner)
    if workstream_id is not None:
        normalized_workstream = workstream_id.strip()
        scores = tuple(score for score in scores if score.workstream_id == normalized_workstream)
    return VitalityArtifacts(
        program_id=program_id,
        items=eligible_items,
        scored_items=scores,
        owner_aggregates=aggregate_vitality(scores, scope_type="owner", leakage_signal_threshold=settings.sparse_workiq_threshold),
        workstream_aggregates=aggregate_vitality(scores, scope_type="workstream", leakage_signal_threshold=settings.sparse_workiq_threshold),
        ado_calls=ado_calls,
    )


def _load_vitality_items(
    program: Program,
    workstreams: tuple[Workstream, ...],
    as_of: datetime,
) -> tuple[tuple[WorkItem, ...], int]:
    del workstreams
    if program.ado is None:
        return (), 0
    client = ADOClient(
        organization=program.ado.organization,
        project=program.ado.project,
        timeout=program.ado.api_timeout_seconds,
    )
    _registry_path = resolve_channel_registry_path_for_read(program.id, programs_root=PROGRAMS_ROOT)
    if not _registry_path.exists():
        return (), 0
    _store = ChannelRegistryStore(_registry_path, program.id, ensure_schema=False)
    ids = [int(r.ref_id) for r in _store.pullable_registrations("ado") if r.ref_id.isdigit()]
    if not ids:
        return (), 0
    batch_rows = client.query_work_items_batch(ids, _BATCH_FIELDS)
    batch_by_id = {int(row.get("id") or row.get("fields", {}).get("System.Id") or 0): row for row in batch_rows}
    items: list[WorkItem] = []
    ado_calls = 1
    for work_item_id in ids:
        comment_rows = client.list_work_item_comments(work_item_id)
        revision_rows = client.list_work_item_revisions(work_item_id)
        ado_calls += 2
        items.append(
            freshness_helpers._work_item_from_sources(
                raw={},
                batch_row=batch_by_id.get(work_item_id, {}),
                comment_rows=comment_rows,
                revision_rows=revision_rows,
                fetched_at=as_of,
            )
        )
    return tuple(items), ado_calls


def _render_vitality_report(artifacts: VitalityArtifacts, *, format: str = "human") -> str:
    summary = summarize_vitality(artifacts.scored_items)
    payload = {
        "program_id": artifacts.program_id,
        "ado_calls": artifacts.ado_calls,
        "summary": asdict(summary),
        "owner_aggregates": [asdict(aggregate) for aggregate in artifacts.owner_aggregates],
        "workstream_aggregates": [asdict(aggregate) for aggregate in artifacts.workstream_aggregates],
        "item_scores": [asdict(score) for score in artifacts.scored_items],
    }

    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        return render_vitality_csv(payload)
    if format != "human":
        raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")

    lines = [
        f"ADO Vitality: {artifacts.program_id}",
        f"Scored items: {len(artifacts.scored_items)}",
        f"Items updated this week: {summary.updated_this_week}/{summary.total_items} ({summary.updated_this_week_percentage}%)",
        f"Average freshness age: {summary.freshness_average_days:.1f} days",
        f"Owners with stale items: {', '.join(summary.stale_owner_aliases) if summary.stale_owner_aliases else 'none'}",
        "",
        "OWNER AGGREGATES:",
    ]
    if artifacts.owner_aggregates:
        for aggregate in artifacts.owner_aggregates:
            lines.append(
                f"- {aggregate.scope_id}: composite {aggregate.composite_score}% | {aggregate.fresh_items}/{aggregate.total_items} fresh | avg richness {aggregate.avg_richness:.1f}"
            )
    else:
        lines.append("- none")
    lines.extend(("", "ITEM SCORES:"))
    if artifacts.scored_items:
        for score in artifacts.scored_items:
            missing = ", ".join(score.richness_missing) if score.richness_missing else "none"
            workstream_label = score.workstream_id or "-"
            owner_label = score.owner_alias or "-"
            lines.append(
                f"- WI:{score.work_item_id} | owner={owner_label} | ws={workstream_label} | freshness={score.freshness_grade} ({score.freshness_days}d) | richness={score.richness_score} | composite={score.composite_score} | missing={missing}"
            )
    else:
        lines.append("- none")
    return "\n".join(lines)


def render_vitality_csv(payload: dict[str, object]) -> str:
    buffer = StringIO()
    writer = csv.writer(buffer)
    columns = (
        "entry_type",
        "program_id",
        "scope_id",
        "scope_type",
        "work_item_id",
        "owner_alias",
        "workstream_id",
        "freshness_days",
        "freshness_grade",
        "richness_score",
        "richness_missing",
        "leakage_events",
        "workiq_signal_count",
        "composite_score",
        "suggested_update",
        "total_items",
        "updated_this_week",
        "updated_this_week_percentage",
        "freshness_average_days",
        "stale_owner_aliases",
        "fresh_items",
        "avg_richness",
        "total_leakage",
        "leakage_ratio",
        "trend",
        "ado_calls",
    )
    writer.writerow(columns)

    summary = dict(payload["summary"]) if isinstance(payload["summary"], dict) else {}
    writer.writerow(
        [
            "summary",
            payload["program_id"],
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            summary.get("total_items", ""),
            summary.get("updated_this_week", ""),
            summary.get("updated_this_week_percentage", ""),
            summary.get("freshness_average_days", ""),
            ";".join(summary.get("stale_owner_aliases", [])),
            "",
            "",
            "",
            "",
            "",
            payload["ado_calls"],
        ]
    )

    owner_aggregates = payload["owner_aggregates"]
    for aggregate in owner_aggregates if isinstance(owner_aggregates, list) else []:
        writer.writerow(
            [
                "owner_aggregate",
                payload["program_id"],
                aggregate.get("scope_id", ""),
                aggregate.get("scope_type", ""),
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                aggregate.get("workiq_signal_count", ""),
                aggregate.get("composite_score", ""),
                "",
                aggregate.get("total_items", ""),
                "",
                "",
                "",
                "",
                aggregate.get("fresh_items", ""),
                aggregate.get("avg_richness", ""),
                aggregate.get("total_leakage", ""),
                aggregate.get("leakage_ratio", ""),
                aggregate.get("trend", ""),
                "",
            ]
        )

    workstream_aggregates = payload["workstream_aggregates"]
    for aggregate in workstream_aggregates if isinstance(workstream_aggregates, list) else []:
        writer.writerow(
            [
                "workstream_aggregate",
                payload["program_id"],
                aggregate.get("scope_id", ""),
                aggregate.get("scope_type", ""),
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                aggregate.get("workiq_signal_count", ""),
                aggregate.get("composite_score", ""),
                "",
                aggregate.get("total_items", ""),
                "",
                "",
                "",
                "",
                aggregate.get("fresh_items", ""),
                aggregate.get("avg_richness", ""),
                aggregate.get("total_leakage", ""),
                aggregate.get("leakage_ratio", ""),
                aggregate.get("trend", ""),
                "",
            ]
        )

    item_scores = payload["item_scores"]
    for score in item_scores if isinstance(item_scores, list) else []:
        writer.writerow(
            [
                "item_score",
                payload["program_id"],
                "",
                "",
                score.get("work_item_id", ""),
                score.get("owner_alias", ""),
                score.get("workstream_id", ""),
                score.get("freshness_days", ""),
                score.get("freshness_grade", ""),
                score.get("richness_score", ""),
                ";".join(score.get("richness_missing", [])),
                score.get("leakage_events", ""),
                score.get("workiq_signal_count", ""),
                score.get("composite_score", ""),
                score.get("suggested_update", ""),
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
            ]
        )
    return buffer.getvalue()


def _owner_alias(item: WorkItem) -> str | None:
    owner_value = item.assigned_to_email or item.assigned_to
    if owner_value is None:
        return None
    alias = owner_value.strip().lower()
    if not alias:
        return None
    if "@" in alias:
        alias = alias.split("@", 1)[0]
    return alias or None