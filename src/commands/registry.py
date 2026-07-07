from __future__ import annotations

import csv
from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
from typing import Any

import typer

from src.core.exceptions import ConfigError
from src.core.gather_state_store import load_gather_state, write_gather_state
from src.core.journal import PROGRAMS_ROOT
from src.core.program_paths import resolve_channel_registry_path_for_read
from src.core.m365_discovery_support import (
    RegistryIdCandidate,
    build_workstream_match_aliases as _build_workstream_match_aliases,
    normalize_match_text as _normalize_match_text,
)
from src.core.m365_identifiers import normalize_meeting_id, normalize_thread_id
from src.core.m365_registry_store import M365RoutingFeedbackEvent, apply_m365_routing_feedback, load_m365_registry, promote_m365_registry_artifact, rename_m365_registry_artifact
from src.core.program_fact_store import load_program_facts, project_workstreams
from src.m365.agency_bridge import AgencyBridge, AgencyCapabilities
from src.m365.discovery_diagnostics import describe_discovery_unavailable_reason
from src.m365.registry_id_discovery import (
    discover_email_thread_candidates as _discover_email_thread_candidates,
    discover_meeting_id_candidates as _discover_meeting_id_candidates,
    discover_thread_id_candidates as _discover_thread_id_candidates,
)


app = typer.Typer(help="Inspect M365 registry state.")


def _uil_deprecation_warning(command: str, equivalent: str) -> None:
    """Emit a deprecation warning to stderr when a registry command routes to UIL."""
    typer.echo(
        f"[deprecation] 'vertex registry {command}' will be replaced by '{equivalent}' "
        "in a future release. Switch to 'vertex integration' commands to manage the UIL registry directly.",
        err=True,
    )


@app.command("list")
def list_registry(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
    source: str = typer.Option("auto", "--source", help="Data source: auto (UIL when available, else yaml), yaml, or uil."),
) -> None:
    uil_path = resolve_channel_registry_path_for_read(program, programs_root=PROGRAMS_ROOT)
    use_uil = source == "uil" or (source == "auto" and uil_path.exists())
    if use_uil:
        _uil_deprecation_warning("list", "vertex integration show --program " + program)
        rows = _list_registry_from_uil(program, uil_path)
        if rows or source == "uil":
            _emit_output(
                rows,
                format=format,
                columns=("ref_id", "ref_kind", "display_name", "workstream_id", "confidence", "confidence_source", "pm_confirmed", "promoted", "status"),
            )
            if not rows:
                typer.echo(f"No UIL registrations found for {program}.")
                raise typer.Exit(code=1)
            return

    registry = load_m365_registry(program, PROGRAMS_ROOT)
    if not registry.artifacts:
        typer.echo(f"No M365 registry artifacts found for {program}.")
        raise typer.Exit(code=1)

    rows = [
        {
            "artifact_id": artifact.artifact_id,
            "artifact_type": artifact.artifact_type,
            "display_name": artifact.display_name or "",
            "workstream_id": artifact.inferred_workstream,
            "confidence": artifact.confidence,
            "confidence_source": artifact.confidence_source,
            "pm_confirmed": artifact.pm_confirmed,
            "promoted": artifact.promoted_to_workstreams_yaml,
            "topics": artifact.topics,
        }
        for artifact in registry.artifacts
    ]
    _emit_output(
        rows,
        format=format,
        columns=(
            "artifact_id",
            "artifact_type",
            "display_name",
            "workstream_id",
            "confidence",
            "confidence_source",
            "pm_confirmed",
            "promoted",
            "topics",
        ),
    )


def _list_registry_from_uil(program: str, uil_path: Path) -> list[dict[str, object]]:
    if not uil_path.exists():
        return []
    from src.core.channel_registry_store import ChannelRegistryStore

    store = ChannelRegistryStore(uil_path, program, ensure_schema=False)
    rows: list[dict[str, object]] = []
    for channel in store.registered_channels():
        for reg in store.all_registrations(channel):
            workstream_id = reg.workstream_ids[0] if reg.workstream_ids else "unassigned"
            rows.append(
                {
                    "ref_id": reg.ref_id,
                    "ref_kind": reg.ref_kind,
                    "display_name": reg.ref_title or "",
                    "workstream_id": workstream_id,
                    "confidence": reg.confidence,
                    "confidence_source": reg.confidence_source,
                    "pm_confirmed": reg.pm_confirmed,
                    "promoted": reg.promoted,
                    "status": reg.status.value,
                }
            )
    return rows


@app.command("confirm")
def confirm_registry_artifact(
    artifact_id: str,
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    pm_alias: str = typer.Option(..., "--pm-alias", help="PM alias recording this action."),
    workstream_id: str | None = typer.Option(None, "--workstream-id", help="Optional explicit workstream assignment."),
    topics: str | None = typer.Option(None, "--topics", help="Comma-separated topic tags to attach."),
    reason: str | None = typer.Option(None, "--reason", help="Optional rationale for the confirmation."),
) -> None:
    event = M365RoutingFeedbackEvent(
        ts=datetime.now(timezone.utc),
        artifact_id=artifact_id,
        action="confirm",
        pm_alias=pm_alias,
        workstream_id=workstream_id,
        topics=_parse_topics_option(topics),
        reason=reason,
    )
    apply_m365_routing_feedback(program, event=event, programs_root=PROGRAMS_ROOT)
    registry = load_m365_registry(program, PROGRAMS_ROOT)
    artifact = next((a for a in registry.artifacts if a.artifact_id == artifact_id), None)
    if artifact is not None:
        _uil_deprecation_warning(
            "confirm",
            f"vertex integration confirm --program {program} --channel {_preferred_uil_channel_for_artifact(artifact)} --ref-id {artifact_id}",
        )
        _try_uil_confirm(program, artifact, pm_alias=pm_alias, reason=reason)
    typer.echo(f"Confirmed {artifact_id} for {program}.")


@app.command("reject")
def reject_registry_artifact(
    artifact_id: str,
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    pm_alias: str = typer.Option(..., "--pm-alias", help="PM alias recording this action."),
    reason: str | None = typer.Option(None, "--reason", help="Optional rationale for the rejection."),
) -> None:
    event = M365RoutingFeedbackEvent(
        ts=datetime.now(timezone.utc),
        artifact_id=artifact_id,
        action="reject",
        pm_alias=pm_alias,
        reason=reason,
    )
    apply_m365_routing_feedback(program, event=event, programs_root=PROGRAMS_ROOT)
    registry = load_m365_registry(program, PROGRAMS_ROOT)
    artifact = next((a for a in registry.artifacts if a.artifact_id == artifact_id), None)
    if artifact is not None:
        _uil_deprecation_warning(
            "reject",
            f"vertex integration suppress --program {program} --channel {_preferred_uil_channel_for_artifact(artifact)} --ref-id {artifact_id}",
        )
        _try_uil_suppress(program, artifact, pm_alias=pm_alias, reason=reason)
    typer.echo(f"Rejected {artifact_id} for {program}.")


@app.command("reassign")
def reassign_registry_artifact(
    artifact_id: str,
    workstream_id: str = typer.Option(..., "--workstream-id", help="Workstream id to assign."),
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    pm_alias: str = typer.Option(..., "--pm-alias", help="PM alias recording this action."),
    topics: str | None = typer.Option(None, "--topics", help="Comma-separated topic tags to attach."),
    reason: str | None = typer.Option(None, "--reason", help="Optional rationale for the reassignment."),
) -> None:
    event = M365RoutingFeedbackEvent(
        ts=datetime.now(timezone.utc),
        artifact_id=artifact_id,
        action="reassign",
        pm_alias=pm_alias,
        workstream_id=workstream_id,
        topics=_parse_topics_option(topics),
        reason=reason,
    )
    apply_m365_routing_feedback(program, event=event, programs_root=PROGRAMS_ROOT)
    registry = load_m365_registry(program, PROGRAMS_ROOT)
    artifact = next((entry for entry in registry.artifacts if entry.artifact_id == artifact_id), None)
    if artifact is not None:
        _uil_deprecation_warning(
            "reassign",
            f"vertex integration reassign --program {program} --channel {_preferred_uil_channel_for_artifact(artifact)} --ref-id {artifact_id} --workstream {workstream_id}",
        )
        _try_uil_reassign(program, artifact, workstream_id, pm_alias=pm_alias, reason=reason)
    typer.echo(f"Reassigned {artifact_id} to {workstream_id} for {program}.")


@app.command("set-id")
def set_registry_artifact_id(
    artifact_id: str,
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    pm_alias: str = typer.Option(..., "--pm-alias", help="PM alias recording this action."),
    series_id: str | None = typer.Option(None, "--series-id", help="Meeting series id to attach."),
    thread_id: str | None = typer.Option(None, "--thread-id", help="Thread id to attach."),
    reason: str | None = typer.Option(None, "--reason", help="Optional rationale for the update."),
) -> None:
    _uil_deprecation_warning(
        "set-id",
        f"vertex integration ref-id --program {program} --channel teams --old-ref-id <old> --new-ref-id <new> --pm {pm_alias}",
    )
    if bool(series_id) == bool(thread_id):
        raise typer.BadParameter("Provide exactly one of --series-id or --thread-id.")

    registry = load_m365_registry(program, PROGRAMS_ROOT)
    artifact = next((entry for entry in registry.artifacts if entry.artifact_id == artifact_id), None)
    if artifact is None:
        raise typer.BadParameter(f"Unknown M365 registry artifact '{artifact_id}' for program '{program}'.")

    if series_id is not None and artifact.artifact_type != "meeting_series":
        raise typer.BadParameter("--series-id is only valid for meeting_series artifacts.")
    if thread_id is not None and artifact.artifact_type == "meeting_series":
        raise typer.BadParameter("--thread-id is not valid for meeting_series artifacts.")

    normalized_series_id = normalize_meeting_id(series_id) if series_id is not None else None
    normalized_thread_id = normalize_thread_id(thread_id) if thread_id is not None else None

    previous_ref_id = _preferred_ref_id_for_artifact(artifact)
    event = M365RoutingFeedbackEvent(
        ts=datetime.now(timezone.utc),
        artifact_id=artifact_id,
        action="set_series_id" if series_id is not None else "set_thread_id",
        pm_alias=pm_alias,
        reason=reason,
        series_id=normalized_series_id,
        thread_id=normalized_thread_id,
    )
    apply_m365_routing_feedback(program, event=event, programs_root=PROGRAMS_ROOT)
    _try_uil_reassign_ref_id(
        program,
        artifact,
        previous_ref_id=previous_ref_id,
        new_ref_id=normalized_series_id or normalized_thread_id,
        pm_alias=pm_alias,
        reason=reason,
    )
    if series_id is not None:
        typer.echo(f"Attached series_id to {artifact_id} for {program}.")
        return
    typer.echo(f"Attached thread_id to {artifact_id} for {program}.")


@app.command("discover-ids")
def discover_registry_ids(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    apply: bool = typer.Option(False, "--apply", help="Apply only unique exact-match candidates directly to the registry."),
    limit: int = typer.Option(10, "--limit", min=1, max=25, help="Max candidates to inspect per artifact."),
    pm_alias: str = typer.Option("vertex", "--pm-alias", help="PM alias recorded when --apply writes discovered ids."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    normalized_format = format.strip().lower()
    if normalized_format not in {"human", "json"}:
        raise typer.BadParameter("--format must be 'human' or 'json'.")
    registry = load_m365_registry(program, PROGRAMS_ROOT)
    workstreams = project_workstreams(
        load_program_facts(program, programs_root=PROGRAMS_ROOT, fact_types=("workstream.entry",))
    )
    bridge = AgencyBridge()
    agency_caps = bridge.probe()
    missing_artifacts = [
        artifact
        for artifact in registry.artifacts
        if (artifact.artifact_type == "meeting_series" and artifact.series_id is None)
        or (artifact.artifact_type != "meeting_series" and artifact.thread_id is None)
    ]
    if not missing_artifacts:
        typer.echo(f"No registry artifacts are missing ids for {program}.")
        raise typer.Exit(code=0)

    # Per-program match aliases from workstreams.yaml (core stays program-agnostic).
    match_aliases = _build_workstream_match_aliases(workstreams)
    applied_count = 0
    results: list[dict[str, Any]] = []
    for artifact in missing_artifacts:
        if normalized_format == "human":
            typer.echo(f"{artifact.artifact_id} | {artifact.display_name or artifact.artifact_id}")
        if artifact.artifact_type == "meeting_series":
            candidates = _discover_meeting_id_candidates(
                artifact.display_name or artifact.artifact_id,
                limit=limit,
                topics=artifact.topics,
                owner_aliases=_workstream_owner_aliases(artifact.inferred_workstream, workstreams=workstreams),
                bridge=bridge,
                match_aliases=match_aliases,
            )
            action_name = "set_series_id"
        elif artifact.artifact_type == "email_thread":
            candidates = _discover_email_thread_candidates(
                artifact.display_name or artifact.artifact_id,
                limit=limit,
                topics=artifact.topics,
                owner_aliases=_workstream_owner_aliases(artifact.inferred_workstream, workstreams=workstreams),
                bridge=bridge,
                match_aliases=match_aliases,
            )
            action_name = "set_thread_id"
        else:
            candidates = _discover_thread_id_candidates(
                artifact.display_name or artifact.artifact_id,
                limit=limit,
                topics=artifact.topics,
                bridge=bridge,
                match_aliases=match_aliases,
            )
            action_name = "set_thread_id"

        if not candidates:
            last_error_getter = getattr(bridge, "last_mcp_error", None)
            runtime_error = last_error_getter() if callable(last_error_getter) else None
            reason = describe_discovery_unavailable_reason(
                artifact_type=artifact.artifact_type,
                agency_available=agency_caps.available,
                has_workiq=agency_caps.has_workiq,
                workiq_cli_available=agency_caps.has_workiq_cli,
                available_tools={tool.strip() for tool in agency_caps.server_tools.get("workiq", ()) if tool.strip()},
                runtime_error=runtime_error,
            )
            results.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "display_name": artifact.display_name or artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "status": "no_candidates",
                    "reason": reason,
                    "candidates": [],
                }
            )
            if normalized_format == "human":
                typer.echo(f"  no candidates found{f' ({reason})' if reason else ''}")
            continue

        exact_candidates = [candidate for candidate in candidates if candidate.exact_match]
        if apply and len(exact_candidates) == 1:
            candidate = exact_candidates[0]
            previous_ref_id = _preferred_ref_id_for_artifact(artifact)
            event = M365RoutingFeedbackEvent(
                ts=datetime.now(timezone.utc),
                artifact_id=artifact.artifact_id,
                action=action_name,
                pm_alias=pm_alias,
                reason="Auto-discovered from WorkIQ candidate lookup.",
                series_id=candidate.discovered_id if action_name == "set_series_id" else None,
                thread_id=candidate.discovered_id if action_name == "set_thread_id" else None,
            )
            apply_m365_routing_feedback(program, event=event, programs_root=PROGRAMS_ROOT)
            _try_uil_reassign_ref_id(
                program,
                artifact,
                previous_ref_id=previous_ref_id,
                new_ref_id=candidate.discovered_id,
                pm_alias=pm_alias,
                reason="Auto-discovered from WorkIQ candidate lookup.",
            )
            applied_count += 1
            results.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "display_name": artifact.display_name or artifact.artifact_id,
                    "artifact_type": artifact.artifact_type,
                    "status": "applied",
                    "applied_id": candidate.discovered_id,
                    "reason": None,
                    "candidates": [_registry_candidate_payload(candidate)],
                }
            )
            if normalized_format == "human":
                typer.echo(f"  applied: {candidate.discovered_id}")
            continue

        results.append(
            {
                "artifact_id": artifact.artifact_id,
                "display_name": artifact.display_name or artifact.artifact_id,
                "artifact_type": artifact.artifact_type,
                "status": "candidates_found",
                "reason": None,
                "candidates": [_registry_candidate_payload(candidate) for candidate in candidates],
            }
        )
        if normalized_format != "human":
            continue
        for candidate in candidates:
            marker = "exact" if candidate.exact_match else "candidate"
            if candidate.source_url:
                typer.echo(f"  {marker}: {candidate.discovered_id} | {candidate.label} | {candidate.source_url}")
            else:
                typer.echo(f"  {marker}: {candidate.discovered_id} | {candidate.label}")
    if normalized_format == "json":
        _record_discover_ids_attempt(
            program=program,
            bridge=bridge,
            results=results,
            programs_root=PROGRAMS_ROOT,
        )
        typer.echo(
            json.dumps(
                {
                    "program": program,
                    "apply": apply,
                    "applied_count": applied_count,
                    "results": results,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    _record_discover_ids_attempt(
        program=program,
        bridge=bridge,
        results=results,
        programs_root=PROGRAMS_ROOT,
    )
    if apply:
        typer.echo(f"Applied {applied_count} discovered id(s).")


def _registry_candidate_payload(candidate: RegistryIdCandidate) -> dict[str, Any]:
    return {
        "discovered_id": candidate.discovered_id,
        "label": candidate.label,
        "source_url": candidate.source_url,
        "exact_match": candidate.exact_match,
    }


def _workstream_owner_aliases(
    workstream_id: str | None,
    *,
    workstreams: tuple[Any, ...],
) -> tuple[str, ...]:
    if workstream_id is None:
        return ()
    workstream = next((item for item in workstreams if item.id == workstream_id), None)
    if workstream is None:
        return ()
    owners = (
        workstream.pm_owner,
        workstream.eng_owner,
        workstream.accountable_owner,
        workstream.alternate_owner,
        workstream.dri_email,
        workstream.accountable_email,
        *workstream.responsible_owners,
        *workstream.consulted_owners,
        *workstream.informed_owners,
        *workstream.aliases,
    )
    ordered: list[str] = []
    seen: set[str] = set()
    for owner in owners:
        normalized = _normalize_match_text(owner)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(str(owner))
    return tuple(ordered)


def _discover_ids_unavailable_reason(
    *,
    artifact_type: str,
    agency_caps: AgencyCapabilities,
    runtime_error: str | None = None,
) -> str:
    reason = describe_discovery_unavailable_reason(
        artifact_type=artifact_type,
        agency_available=agency_caps.available,
        has_workiq=agency_caps.has_workiq,
        workiq_cli_available=agency_caps.has_workiq_cli,
        available_tools={tool.strip() for tool in agency_caps.server_tools.get("workiq", ()) if tool.strip()},
        runtime_error=runtime_error,
    )
    return f" ({reason})" if reason else ""


@app.command("promote")
def promote_registry_artifact(
    artifact_id: str,
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    pm_alias: str | None = typer.Option(None, "--pm-alias", help="PM alias recording confidence-based promotion."),
    reason: str | None = typer.Option(None, "--reason", help="Optional rationale for confidence-based promotion."),
) -> None:
    try:
        promote_m365_registry_artifact(
            program,
            artifact_id=artifact_id,
            programs_root=PROGRAMS_ROOT,
            pm_alias=pm_alias,
            reason=reason,
        )
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error
    registry = load_m365_registry(program, PROGRAMS_ROOT)
    artifact = next((a for a in registry.artifacts if a.artifact_id == artifact_id), None)
    if artifact is not None:
        _uil_deprecation_warning(
            "promote",
            f"vertex integration promote --program {program} --channel {_preferred_uil_channel_for_artifact(artifact)} --ref-id {artifact_id}",
        )
        _try_uil_promote(program, artifact, pm_alias=pm_alias or "vertex", reason=reason)
    typer.echo(f"Promoted {artifact_id} into workstreams.yaml for {program}.")


@app.command("rename")
def rename_registry_artifact(
    artifact_id: str,
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    display_name: str = typer.Option(..., "--display-name", help="Stable display name used to derive thread:named:<slug>."),
    pm_alias: str = typer.Option(..., "--pm-alias", help="PM alias recording the rename."),
    reason: str | None = typer.Option(None, "--reason", help="Optional rationale for the rename."),
) -> None:
    prior_registry = load_m365_registry(program, PROGRAMS_ROOT)
    prior_artifact = next((artifact for artifact in prior_registry.artifacts if artifact.artifact_id == artifact_id), None)
    try:
        registry = rename_m365_registry_artifact(
            program,
            artifact_id=artifact_id,
            display_name=display_name,
            programs_root=PROGRAMS_ROOT,
            pm_alias=pm_alias,
            reason=reason,
        )
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error
    renamed = next(
        artifact
        for artifact in registry.artifacts
        if artifact.display_name == display_name and artifact.artifact_id.startswith("thread:named:")
    )
    if prior_artifact is not None:
        _try_uil_rename(
            program,
            prior_artifact,
            pm_alias=pm_alias,
            reason=reason,
            new_artifact_id=renamed.artifact_id,
        )
    typer.echo(f"Renamed {artifact_id} to {renamed.artifact_id} for {program}.")


def _emit_output(rows: list[dict[str, object]], *, format: str, columns: tuple[str, ...]) -> None:
    if format == "json":
        typer.echo(json.dumps(rows, indent=2, sort_keys=True, default=_json_default))
        return
    if format == "csv":
        typer.echo(_render_csv(rows, columns=columns), nl=False)
        return
    if format != "human":
        raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")
    for row in rows:
        typer.echo("\t".join(_human_cell(row[column]) for column in columns))


def _render_csv(rows: list[dict[str, object]], *, columns: tuple[str, ...]) -> str:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_cell(row[column]) for column in columns])
    return buffer.getvalue()


def _human_cell(value: object) -> str:
    if isinstance(value, tuple):
        return ", ".join(str(item) for item in value)
    return str(value)


def _csv_cell(value: object) -> str:
    if isinstance(value, tuple):
        return "|".join(str(item) for item in value)
    return str(value)


def _json_default(value: object) -> str:
    if hasattr(value, "value"):
        return str(value.value)
    raise TypeError(f"Object of type {type(value)!r} is not JSON serializable")


def _parse_topics_option(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(topic.strip() for topic in value.split(",") if topic.strip())


def _preferred_uil_channel_for_artifact(artifact: object) -> str:
    return "email" if getattr(artifact, "artifact_type", None) == "email_thread" else "teams"


def _preferred_ref_id_for_artifact(artifact: object) -> str | None:
    if getattr(artifact, "artifact_type", None) == "meeting_series":
        return getattr(artifact, "series_id", None) or getattr(artifact, "artifact_id", None)
    return getattr(artifact, "thread_id", None) or getattr(artifact, "artifact_id", None)


def _candidate_uil_refs_for_artifact(artifact: object) -> tuple[tuple[str, str, str], ...]:
    artifact_id = getattr(artifact, "artifact_id", None)
    artifact_type = getattr(artifact, "artifact_type", None)
    series_id = getattr(artifact, "series_id", None)
    thread_id = getattr(artifact, "thread_id", None)
    candidates: list[tuple[str, str, str]] = []

    def _append(channel: str, ref_id: str | None, ref_kind: str) -> None:
        if not ref_id:
            return
        candidate = (channel, ref_id, ref_kind)
        if candidate not in candidates:
            candidates.append(candidate)

    if artifact_type == "meeting_series":
        _append("teams", series_id or artifact_id, "meeting_series")
    elif artifact_type == "email_thread":
        _append("email", thread_id, "email_thread")
        _append("email", artifact_id, "email_thread")
        _append("teams", thread_id, "teams_chat")
        _append("teams", thread_id, "email_thread")
        _append("teams", artifact_id, "email_thread")
    else:
        _append("teams", thread_id, "teams_chat")
        _append("teams", artifact_id, "teams_channel")
        _append("teams", artifact_id, "teams_chat")
    return tuple(candidates)


def _load_uil_store(program: str):
    uil_path = resolve_channel_registry_path_for_read(program, programs_root=PROGRAMS_ROOT)
    if not uil_path.exists():
        return None
    from src.core.channel_registry_store import ChannelRegistryStore

    return ChannelRegistryStore(uil_path, program, ensure_schema=False)


def _resolve_uil_registration(program: str, artifact: object):
    store = _load_uil_store(program)
    if store is None:
        return None, None
    registrations_by_key = {
        (registration.channel, registration.ref_id, registration.ref_kind): registration
        for channel in store.registered_channels()
        for registration in store.all_registrations(channel)
    }
    for candidate in _candidate_uil_refs_for_artifact(artifact):
        registration = registrations_by_key.get(candidate)
        if registration is not None:
            return store, registration
    return store, None


def _record_discover_ids_attempt(
    *,
    program: str,
    bridge: AgencyBridge,
    results: list[dict[str, Any]],
    programs_root: Path,
) -> None:
    existing = load_gather_state(program, programs_root=programs_root)
    now = datetime.now(timezone.utc)
    m365_discovery = dict(existing.m365_discovery) if existing is not None else {}
    last_error_getter = getattr(bridge, "last_mcp_error", None)
    runtime_error = last_error_getter() if callable(last_error_getter) else None
    m365_discovery["active"] = True
    m365_discovery["first_discovery_completed_at"] = (
        str(m365_discovery.get("first_discovery_completed_at") or "").strip() or now.isoformat()
    )
    m365_discovery["discovery_last_error"] = runtime_error
    m365_discovery["query_plan_count"] = len(results)

    write_gather_state(
        program,
        gathered_at=existing.gathered_at if existing is not None else now,
        scanned_items=existing.scanned_items if existing is not None else 0,
        discovered_signals=existing.discovered_signals if existing is not None else 0,
        new_signals=existing.new_signals if existing is not None else 0,
        pending_review=existing.pending_review if existing is not None else 0,
        trajectory_updates=existing.trajectory_updates if existing is not None else 0,
        auto_reviews_written=existing.auto_reviews_written if existing is not None else 0,
        ado_calls=existing.ado_calls if existing is not None else 0,
        archived_journal_files=existing.archived_journal_files if existing is not None else 0,
        background_proposals=existing.background_proposals if existing is not None else 0,
        integration_errors=existing.integration_errors if existing is not None else 0,
        integration_error_details=existing.integration_error_details if existing is not None else (),
        gather_flags=existing.gather_flags if existing is not None else {},
        channels=existing.channels if existing is not None else {},
        m365_discovery=m365_discovery,
        previous_gathered_at=existing.previous_gathered_at if existing is not None else None,
        previous_query_states=existing.previous_query_states if existing is not None else {},
        previous_channels=existing.previous_channels if existing is not None else {},
        previous_m365_discovery=existing.previous_m365_discovery if existing is not None else {},
        query_states=existing.query_states if existing is not None else {},
        programs_root=programs_root,
    )


def _try_uil_confirm(program: str, artifact: object, *, pm_alias: str, reason: str | None = None) -> None:
    store, registration = _resolve_uil_registration(program, artifact)
    if store is None or registration is None:
        return
    store.confirm(channel=registration.channel, ref_id=registration.ref_id, ref_kind=registration.ref_kind)
    store.write_feedback_event(
        registration.channel,
        registration.ref_id,
        registration.ref_kind,
        action="confirm",
        pm_alias=pm_alias,
        reason=reason,
        workstream_id=registration.workstream_ids[0] if registration.workstream_ids else None,
        series_id=getattr(artifact, "series_id", None),
        thread_id=getattr(artifact, "thread_id", None),
    )


def _try_uil_suppress(program: str, artifact: object, *, pm_alias: str, reason: str | None = None) -> None:
    store, registration = _resolve_uil_registration(program, artifact)
    if store is None or registration is None:
        return
    store.suppress(channel=registration.channel, ref_id=registration.ref_id, ref_kind=registration.ref_kind)
    store.write_feedback_event(
        registration.channel,
        registration.ref_id,
        registration.ref_kind,
        action="reject",
        pm_alias=pm_alias,
        reason=reason,
        workstream_id=registration.workstream_ids[0] if registration.workstream_ids else None,
        series_id=getattr(artifact, "series_id", None),
        thread_id=getattr(artifact, "thread_id", None),
    )


def _try_uil_promote(program: str, artifact: object, *, pm_alias: str, reason: str | None = None) -> None:
    store, registration = _resolve_uil_registration(program, artifact)
    if store is None or registration is None:
        return
    store.promote(channel=registration.channel, ref_id=registration.ref_id, ref_kind=registration.ref_kind)
    store.write_feedback_event(
        registration.channel,
        registration.ref_id,
        registration.ref_kind,
        action="promote",
        pm_alias=pm_alias,
        reason=reason,
        workstream_id=registration.workstream_ids[0] if registration.workstream_ids else None,
        series_id=getattr(artifact, "series_id", None),
        thread_id=getattr(artifact, "thread_id", None),
    )


def _try_uil_reassign(
    program: str,
    artifact: object,
    new_workstream_id: str,
    *,
    pm_alias: str,
    reason: str | None = None,
) -> None:
    store, registration = _resolve_uil_registration(program, artifact)
    if store is None or registration is None:
        return
    prior_workstream_id = registration.workstream_ids[0] if registration.workstream_ids else None
    store.reassign_workstream(
        channel=registration.channel,
        ref_id=registration.ref_id,
        ref_kind=registration.ref_kind,
        new_workstream_id=new_workstream_id,
    )
    store.write_feedback_event(
        registration.channel,
        registration.ref_id,
        registration.ref_kind,
        action="reassign",
        pm_alias=pm_alias,
        reason=reason,
        workstream_id=new_workstream_id,
        prior_workstream_id=prior_workstream_id,
        series_id=getattr(artifact, "series_id", None),
        thread_id=getattr(artifact, "thread_id", None),
    )


def _try_uil_reassign_ref_id(
    program: str,
    artifact: object,
    *,
    previous_ref_id: str | None,
    new_ref_id: str | None,
    pm_alias: str,
    reason: str | None = None,
) -> None:
    if not new_ref_id:
        return
    store, registration = _resolve_uil_registration(program, artifact)
    if store is None or registration is None:
        return
    old_ref_id = previous_ref_id or registration.ref_id
    if old_ref_id == new_ref_id:
        return
    store.reassign_ref_id(
        registration.channel,
        old_ref_id,
        new_ref_id,
        registration.ref_kind,
        pm_alias=pm_alias,
        reason=reason,
        provider_instance_id=registration.provider_instance_id,
    )


def _try_uil_rename(
    program: str,
    artifact: object,
    *,
    pm_alias: str,
    reason: str | None = None,
    new_artifact_id: str,
) -> None:
    store, registration = _resolve_uil_registration(program, artifact)
    if store is None or registration is None:
        return
    store.write_feedback_event(
        registration.channel,
        registration.ref_id,
        registration.ref_kind,
        action="rename_artifact",
        pm_alias=pm_alias,
        reason=reason,
        workstream_id=registration.workstream_ids[0] if registration.workstream_ids else None,
        series_id=getattr(artifact, "series_id", None),
        thread_id=getattr(artifact, "thread_id", None),
        new_artifact_id=new_artifact_id,
    )
