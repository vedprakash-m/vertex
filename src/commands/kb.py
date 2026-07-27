from __future__ import annotations

import csv
from dataclasses import replace
from datetime import datetime, timezone
import inspect
from io import StringIO
import json
import os
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import Any

import typer

from src.ai._pipeline import AIPipelineError, process_generated_text
from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai.client import AIClientError
from src.ai.deployment_fallback import FallbackAIClient, LEGACY_DEPLOYMENT_ALIAS_NOTICE, resolve_ai_deployments_for_feature
from src.ai.llm_trace import AITraceContext, use_trace_context
from src.ai.provider import LLMProvider
from src.core.config_loader import PROGRAMS_ROOT
from src.core.exceptions import ConfigError, StateError
from src.core.kb_changelog import build_kb_changelog_report, render_kb_changelog_report
from src.core.kb_updates import KbUpdatePlan, apply_kb_update, parse_deterministic_kb_correction
from src.core.kb_updates import parse_kb_update_operations, prepare_kb_update, read_program_kb_documents
from src.core.kb_updates import supported_kb_paths
from src.core.knowledge_store import get_shared_knowledge_root
from src.core.operator_identity import capture_operator_identity
from src.core.people_legacy_affiliation import LegacyAffiliationEdge, find_alias_edges, find_cross_program_overlaps
from src.core.people_registry_identity import bootstrap_registry_identity, load_registry_config, load_registry_manifest
from src.core.people_registry_governance import (
    adopt_registry_edits,
    govern_person_fields,
)
from src.core.people_registry_corrections import (
    PeopleCorrectionResult,
    bind_person_identifier,
    merge_people,
    merge_people_batch,
    split_person,
    unmerge_people,
)
from src.core.people_registry_lease import force_release_registry_lease, is_registry_lease_expired, read_registry_lease_state
from src.core.people_registry_modes import (
    load_effective_registry_config,
    program_shadow_status,
    rollback_program_mode,
    set_program_mode,
    set_registry_flag,
    set_workspace_write_mode,
)
from src.core.people_registry_backup import BACKUP_SNAPSHOT_MANIFEST_NAME, restore_registry_backup_snapshot
from src.core.people_registry_promotion import (
    program_promotion_status,
    record_program_rollback_restore_drill,
)
from src.core.people_shared_migration import (
    EntityIdBackfillPlan,
    SharedMigrationPlan,
    apply_entity_id_backfill,
    apply_shared_migration,
    bootstrap_shared_factual_files,
    preview_entity_id_backfill,
    preview_shared_migration,
)
from src.core.people_shadow_parity import compute_and_record_shadow_parity_if_in_shadow_mode, compute_shadow_parity
from src.core.people_directory_schema import PersonStatus, person_to_payload, team_to_payload
from src.core.people_lifecycle_transitions import transition_person_lifecycle_status
from src.core.people_entity_schema import entity_to_payload
from src.core.people_membership_schema import membership_to_payload
from src.core.people_query import (
    DEFAULT_STALE_FRESHNESS_DAYS,
    find_person,
    find_team,
    list_conflicts,
    list_stale_people,
    paginate,
    search_people,
    team_members,
)
from src.core.people_registry_storage_class import refresh_registry_storage_status
from src.core.people_registry_writer import (
    RegistryPatchOperation,
    apply_shared_registry_patch,
    shared_registry_is_active,
)
from src.core.identity_provider_refresh import RefreshResult, refresh_people_from_provider
from src.core.ledger.ulid import new_ulid
from src.m365.agency_bridge import AgencyBridge
from src.m365.workiq_ask_support import prose_text_from_payload
from src.core.people_enrichment import (
    EnrichmentCandidateEvent,
    EnrichmentCandidateState,
    build_workiq_question,
    list_pending_enrichment_candidates,
    read_enrichment_events,
    record_enrichment_event,
    resolve_enrichment_due_alert,
    select_enrichment_candidates,
)
from src.core.people_delegation_lifecycle import create_delegation, list_delegations, revoke_delegation
from src.core.people_delegation_schema import Delegation, delegation_to_payload
from src.core.profile_encryption import decrypt_people_profiles_file, encrypt_people_profiles_file, inspect_people_profiles_file


app = typer.Typer(help="Knowledge base diagnostics and history.")
profiles_app = typer.Typer(help="Protect or unwrap sensitive people profile files.")
people_app = typer.Typer(help="specs/people.md Phase 0c: read-only, alias-based legacy cross-program queries.")
teams_app = typer.Typer(help="specs/people.md PPL-W3.1: canonical team query surfaces.")
registry_app = typer.Typer(help="specs/people.md Phase 1: workspace people-registry identity and lifecycle.")
lease_app = typer.Typer(help="specs/people.md PPL-W1.2: workspace-global registry lease inspection/recovery.")
mode_app = typer.Typer(help="specs/people.md PPL-W1.9/PPL-W2B.6: per-program modes, promotion, and kill switches.")
delegate_app = typer.Typer(help="specs/people.md PPL-W5b.2: steward-authorized delegation lifecycle (create/revoke/list).")

_LEGACY_ALIAS_WARNING = "WARNING: alias-based legacy result; identity not verified."

_KB_UPDATE_PROMPT_VERSION = "kb_update_plan.v1"


@app.command("changelog")
def kb_changelog_command(
    program: str = typer.Option(..., "--program", help="Program id."),
    since: str = typer.Option(..., "--since", help="ISO week in YYYY-Www format."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    repo_root = Path(PROGRAMS_ROOT).resolve().parents[0]
    try:
        report = build_kb_changelog_report(
            program_id=program,
            since_week=since,
            repo_root=repo_root,
        )
    except (ConfigError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
    except RuntimeError as error:
        raise typer.BadParameter(f"Unable to read git history: {error}") from error

    if format == "human":
        typer.echo(render_kb_changelog_report(report), nl=False)
    else:
        typer.echo(render_kb_changelog_output(report, format=format), nl=False)
    raise typer.Exit(code=0)


def render_kb_changelog_output(report: Any, *, format: str) -> str:
    payload = _build_kb_changelog_payload(report)
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(("program_id", "since_week", "since_date", "commit_sha", "committed_at", "alias", "change_type", "before", "after"))
        if payload["entries"]:
            for entry in payload["entries"]:  # type: ignore[attr-defined]
                writer.writerow(
                    (
                        payload["program_id"],
                        payload["since_week"],
                        payload["since_date"],
                        entry["commit_sha"],
                        entry["committed_at"],
                        entry["alias"],
                        entry["change_type"],
                        entry["before"],
                        entry["after"],
                    )
                )
        else:
            writer.writerow((payload["program_id"], payload["since_week"], payload["since_date"], None, None, None, None, None, None))
        return buffer.getvalue()
    raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")


def _build_kb_changelog_payload(report: Any) -> dict[str, object]:
    return {
        "entry_count": len(report.entries),
        "entries": [
            {
                "after": entry.after,
                "alias": entry.alias,
                "before": entry.before,
                "change_type": entry.change_type,
                "commit_sha": entry.commit_sha,
                "committed_at": entry.committed_at.isoformat(),
            }
            for entry in report.entries
        ],
        "program_id": report.program_id,
        "since_date": report.since_date.isoformat(),
        "since_week": report.since_week,
    }


@app.command("update")
def kb_update_command(
    correction: str = typer.Argument(..., help="Natural-language KB correction."),
    program: str | None = typer.Option(None, "--program", help="Program id. Inferred when only one program exists."),
    apply: bool = typer.Option(False, "--apply", help="Write the validated KB change."),
    ai: bool = typer.Option(True, "--ai/--no-ai", help="Allow AI planning when deterministic parsing is insufficient."),
) -> None:
    programs_root = Path(PROGRAMS_ROOT)
    resolved_program = _resolve_program_id(program, programs_root)

    plan = parse_deterministic_kb_correction(correction, program_id=resolved_program)
    if plan is None and ai:
        plan = _plan_kb_update_with_ai(
            correction=correction,
            program_id=resolved_program,
            programs_root=programs_root,
        )
    if plan is None:
        raise typer.BadParameter(
            "Unable to interpret the correction. Supported deterministic patterns include setting person title/email/display name, "
            "adding or removing person team_ids, and setting workstream pm/eng/alternate owners. Configure "
            "AZURE_OPENAI_KB_DEPLOYMENT, VERTEX_AI_DEPLOYMENT, or AZURE_OPENAI_DEPLOYMENT to enable AI planning for broader corrections; "
            f"{LEGACY_DEPLOYMENT_ALIAS_NOTICE}"
        )

    shared_factual_operations = tuple(
        operation
        for operation in plan.operations
        if operation.file_path in {"knowledge/people_directory.yaml", "knowledge/teams.yaml"}
    )
    non_factual_operations = tuple(
        operation for operation in plan.operations if operation not in shared_factual_operations
    )
    shared_writer_active = shared_registry_is_active(programs_root)
    if shared_factual_operations and shared_writer_active:
        patch_operations = tuple(
            RegistryPatchOperation(
                relative_path=operation.file_path,
                action=operation.action,
                match_value=operation.match_value,
                fields=operation.fields,
                field_name=operation.field_name,
                value=operation.value,
            )
            for operation in shared_factual_operations
        )
        preview_actor = "<preview>"
        shared_preview = apply_shared_registry_patch(
            operations=patch_operations,
            programs_root=programs_root,
            actor=preview_actor,
            reason=f"vertex kb update: {plan.correction}",
            source="kb_update",
            apply=False,
        )
        generic_preview = (
            prepare_kb_update(
                replace(plan, operations=non_factual_operations),
                programs_root=programs_root,
            )
            if non_factual_operations
            else None
        )
        typer.echo(_render_shared_registry_update_preview(shared_preview), nl=False)
        if generic_preview is not None:
            typer.echo()
            typer.echo(_render_kb_update_preview(generic_preview), nl=False)
        if not apply:
            typer.echo("\nPreview only. Re-run with --apply to write the change.")
            raise typer.Exit(code=0)
        applied_shared = apply_shared_registry_patch(
            operations=patch_operations,
            programs_root=programs_root,
            actor=_resolve_operator_principal("kb-update-shared-factual"),
            reason=f"vertex kb update: {plan.correction}",
            source="kb_update",
            apply=True,
        )
        generic_result = (
            apply_kb_update(generic_preview, programs_root=programs_root)
            if generic_preview is not None
            else None
        )
        generic_count = 0 if generic_result is None else len(generic_result.preview.changes)
        typer.echo(
            f"\nApplied shared registry generation {applied_shared.generation_id} "
            f"({len(applied_shared.affected_paths)} factual file(s)); {generic_count} unrelated KB file(s)."
        )
        raise typer.Exit(code=0)

    try:
        preview = prepare_kb_update(plan, programs_root=programs_root)
    except (ConfigError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error

    typer.echo(_render_kb_update_preview(preview), nl=False)

    if not apply:
        typer.echo("\nPreview only. Re-run with --apply to write the change.")
        raise typer.Exit(code=0)

    result = apply_kb_update(preview, programs_root=programs_root)
    typer.echo(f"\nApplied {len(result.preview.changes)} file(s). Audit log: {result.audit_path}")
    raise typer.Exit(code=0)


def _render_shared_registry_update_preview(result) -> str:
    paths = ", ".join(result.affected_paths) or "no factual files"
    lines = [
        "Shared registry factual update preview",
        f"Files: {paths}",
        f"Field changes: {len(result.changes)}",
    ]
    for entity_id, field, before, after in result.changes:
        lines.append(f"- {entity_id} {field}: {before!r} -> {after!r}")
    for conflict in result.conflicts:
        lines.append(f"- quarantined: {conflict}")
    return "\n".join(lines) + "\n"


def _resolve_program_id(program: str | None, programs_root: Path) -> str:
    if program is not None:
        return program
    candidates = tuple(
        path.name
        for path in sorted(programs_root.iterdir(), key=lambda item: item.name.lower())
        if path.is_dir() and (path / "program.yaml").exists()
    ) if programs_root.exists() else ()
    if len(candidates) == 1:
        return candidates[0]
    raise typer.BadParameter("Provide --program when multiple programs exist.")


def _build_kb_update_client(*, trace_context: AITraceContext | None = None) -> LLMProvider | None:
    if get_ai_mode() == AIMode.DISABLED:
        return None
    deployments = resolve_ai_deployments_for_feature(
        feature_name="default",
        primary_candidates=(),
        backup_candidates=(),
        primary_fallback_envs=("AZURE_OPENAI_KB_DEPLOYMENT", "VERTEX_AI_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT"),
        backup_fallback_envs=("VERTEX_AI_BACKUP_DEPLOYMENT",),
    )
    if not deployments:
        return None
    try:
        # D-20: bind the trace context to the process-level ContextVar so
        # any nested helper that doesn't take an explicit `trace_context=`
        # arg still picks it up. The explicit kwarg below still wins, so
        # this is behavior-preserving.
        with use_trace_context(trace_context):
            return FallbackAIClient(
                deployments=deployments,
                temperature=0.0,
                budget_usd=0.25,
                trace_context=trace_context,
            )
    except (AIClientError, RuntimeError):
        return None


def _plan_kb_update_with_ai(
    *,
    correction: str,
    program_id: str,
    programs_root: Path,
) -> KbUpdatePlan | None:
    client = _build_default_kb_update_client(
        trace_context=_build_kb_update_trace_context(
            program_id=program_id,
            programs_root=programs_root,
        )
    )
    if client is None:
        return None

    documents = read_program_kb_documents(program_id, programs_root=programs_root)
    prompt = _build_kb_update_prompt(correction=correction, program_id=program_id, documents=documents)
    try:
        raw_text = str(
            client.chat(
                "You convert knowledge-base corrections into strict JSON plans. Respond with JSON only.",
                prompt,
                max_tokens=900,
                prompt_version=_KB_UPDATE_PROMPT_VERSION,
            )
        ).strip()
        # D-26: route the raw model output through the shared safety pipeline so a
        # prompt-injected correction cannot smuggle malicious instructions into the
        # knowledge base. We validate (injection detection raises AIPipelineError)
        # but parse the original JSON, since PII-scrubbing the structured envelope
        # would corrupt it and KB content legitimately contains aliases/emails.
        process_generated_text(raw_text)
        payload = json.loads(raw_text)
        operations = parse_kb_update_operations(payload)
        return KbUpdatePlan(
            program_id=program_id,
            correction=correction,
            planner="ai",
            operations=operations,
        )
    except (AIClientError, AIPipelineError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _build_kb_update_trace_context(*, program_id: str, programs_root: Path) -> AITraceContext:
    current_time = datetime.now(timezone.utc)
    return AITraceContext(
        edition=program_id,
        run_id=f"{program_id}:kb:update:{current_time.strftime('%Y%m%dT%H%M%SZ')}",
        caller="src.commands.kb._plan_kb_update_with_ai",
        metadata={
            "program_id": program_id,
            "task_type": "kb_update_plan",
            "run_budget_usd": 0.25,
            # specs/backlog.md WO-4: without this, AI telemetry's `feature`
            # field silently fell back to the caller string above instead of
            # a real ai_policy.yaml feature name, making this call site
            # invisible to any feature-scoped telemetry query.
            "feature": "default",
        },
    )


def _build_default_kb_update_client(*, trace_context: AITraceContext) -> LLMProvider | None:
    if get_ai_mode() == AIMode.DISABLED:
        return None
    if "trace_context" in inspect.signature(_build_kb_update_client).parameters:
        return _build_kb_update_client(trace_context=trace_context)
    return _build_kb_update_client()


def _build_kb_update_prompt(
    *,
    correction: str,
    program_id: str,
    documents: dict[str, dict[str, Any]],
) -> str:
    rendered_documents = json.dumps(documents, indent=2, sort_keys=True)
    supported_paths = ", ".join(supported_kb_paths())
    return (
        f"Program: {program_id}\n"
        f"Correction: {correction}\n"
        f"Supported paths: {supported_paths}\n"
        "Output JSON with this shape only:\n"
        "{\n"
        '  "operations": [\n'
        "    {\n"
        '      "path": "knowledge/people_directory.yaml",\n'
        '      "action": "set_fields" | "add_list_value" | "remove_list_value" | "remove_entry",\n'
        '      "match_value": "entry id/alias",\n'
        '      "fields": {"field": "value"},\n'
        '      "field": "list_field_name",\n'
        '      "value": "list item value"\n'
        "    }\n"
        "  ]\n"
        "}\n"
        "Use set_fields for scalar or whole-list replacements. Use add/remove_list_value only for list membership edits.\n"
        "Do not invent unsupported paths. Do not include markdown, explanations, or comments.\n"
        f"Current documents:\n{rendered_documents}"
    )


def _render_kb_update_preview(preview: Any) -> str:
    lines = [
        f"KB update preview: {preview.program_id}",
        f"Planner: {preview.planner}",
        f"Validation: {preview.validation_summary}",
        "",
        preview.diff,
    ]
    return "\n".join(line for line in lines if line != "")


@profiles_app.command("encrypt")
def kb_profiles_encrypt_command(
    program: str = typer.Option(..., "--program", help="Program id."),
    scope: str = typer.Option("active", "--scope", help="Target active, shared, or program-scoped people_profiles.yaml."),
) -> None:
    target_path = _resolve_profiles_path(program=program, scope=scope)
    try:
        before = inspect_people_profiles_file(target_path)
        after = encrypt_people_profiles_file(target_path)
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error

    relative_path = _render_relative_path(target_path)
    if before.storage == "encrypted":
        typer.echo(
            f"Sensitive profiles are already encrypted at rest in {relative_path} ({after.profile_count} entr{'y' if after.profile_count == 1 else 'ies'})."
        )
    else:
        typer.echo(
            f"Encrypted {after.profile_count} sensitive profile entr{'y' if after.profile_count == 1 else 'ies'} in {relative_path}."
        )
    raise typer.Exit(code=0)


@profiles_app.command("decrypt")
def kb_profiles_decrypt_command(
    program: str = typer.Option(..., "--program", help="Program id."),
    scope: str = typer.Option("active", "--scope", help="Target active, shared, or program-scoped people_profiles.yaml."),
) -> None:
    target_path = _resolve_profiles_path(program=program, scope=scope)
    try:
        before = inspect_people_profiles_file(target_path)
        after = decrypt_people_profiles_file(target_path)
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error

    relative_path = _render_relative_path(target_path)
    if before.storage == "plaintext":
        typer.echo(
            f"Sensitive profiles are already plaintext in {relative_path} ({after.profile_count} entr{'y' if after.profile_count == 1 else 'ies'})."
        )
    else:
        typer.echo(
            f"Decrypted {after.profile_count} sensitive profile entr{'y' if after.profile_count == 1 else 'ies'} in {relative_path}."
        )
    raise typer.Exit(code=0)


def _resolve_profiles_path(*, program: str, scope: str) -> Path:
    programs_root = Path(PROGRAMS_ROOT)
    normalized_scope = scope.strip().lower()
    if normalized_scope not in {"active", "program", "shared"}:
        raise typer.BadParameter("--scope must be one of: active, program, shared.")

    program_path = programs_root / program / "knowledge" / "people_profiles.yaml"
    shared_path = get_shared_knowledge_root(programs_root) / "people_profiles.yaml"
    if normalized_scope == "program":
        return program_path
    if normalized_scope == "shared":
        return shared_path
    return shared_path if shared_path.exists() else program_path


def _render_relative_path(path: Path) -> str:
    repo_root = Path(PROGRAMS_ROOT).resolve().parent
    try:
        return path.resolve().relative_to(repo_root).as_posix()
    except ValueError:
        return str(path)


@people_app.command("programs")
def kb_people_programs_command(
    person: str = typer.Option(..., "--person", help="Alias to look up (exact, casefold-normalized match; no fuzzy matching in this Phase 0c slice)."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """specs/people.md Phase 0c: which programs/workstreams currently reference this alias, and in what relation.
    Alias-only -- no identity resolution, no PII reveal, no writes."""
    edges = find_alias_edges(person, programs_root=PROGRAMS_ROOT)
    if format == "json":
        typer.echo(json.dumps(_people_query_payload(items=[_edge_to_dict(edge) for edge in edges]), indent=2, sort_keys=True))
        raise typer.Exit(code=0)
    typer.echo(_LEGACY_ALIAS_WARNING)
    if not edges:
        typer.echo(f"No legacy accountability references found for alias '{person}'.")
        raise typer.Exit(code=0)
    typer.echo(f"Alias '{person}' referenced by {len(edges)} record(s) across {len({edge.program_id for edge in edges})} program(s):")
    for edge in sorted(edges, key=lambda e: (e.program_id, e.relation_type, e.source_path)):
        workstream_suffix = f" (workstream: {edge.workstream_id})" if edge.workstream_id else ""
        typer.echo(f"  - {edge.program_id}: {edge.relation_type}{workstream_suffix} <- {edge.source_path}")
    raise typer.Exit(code=0)


@people_app.command("overlaps")
def kb_people_overlaps_command(
    program: str | None = typer.Option(None, "--program", help="Scope to aliases that reference this program (still shows their other program appearances)."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """specs/people.md Phase 0c: which aliases currently appear across 2+ programs' accountability fields.
    Alias-only -- no identity resolution, no PII reveal, no writes. Normal intentional overlap is
    query information, not a defect (specs/people.md §8.2)."""
    overlaps = find_cross_program_overlaps(programs_root=PROGRAMS_ROOT, program_id=program)
    if format == "json":
        items = [{"alias": alias, "edges": [_edge_to_dict(edge) for edge in edges]} for alias, edges in overlaps]
        typer.echo(json.dumps(_people_query_payload(items=items), indent=2, sort_keys=True))
        raise typer.Exit(code=0)
    typer.echo(_LEGACY_ALIAS_WARNING)
    if not overlaps:
        scope = f" touching program '{program}'" if program else ""
        typer.echo(f"No cross-program alias overlaps found{scope}.")
        raise typer.Exit(code=0)
    typer.echo(f"{len(overlaps)} alias(es) with cross-program overlap:")
    for alias, edges in overlaps:
        program_ids = sorted({edge.program_id for edge in edges})
        typer.echo(f"  - '{alias}': {', '.join(program_ids)}")
        for edge in sorted(edges, key=lambda e: (e.program_id, e.relation_type, e.source_path)):
            workstream_suffix = f" (workstream: {edge.workstream_id})" if edge.workstream_id else ""
            typer.echo(f"      {edge.program_id}: {edge.relation_type}{workstream_suffix} <- {edge.source_path}")
    raise typer.Exit(code=0)


def _edge_to_dict(edge: LegacyAffiliationEdge) -> dict[str, Any]:
    return {
        "alias": edge.alias,
        "program_id": edge.program_id,
        "relation_type": edge.relation_type,
        "source_path": edge.source_path,
        "workstream_id": edge.workstream_id,
    }


def _people_query_payload(*, items: list[dict[str, Any]]) -> dict[str, Any]:
    # specs/people.md §8.2's versioned query envelope, with the mandatory
    # Phase 0c legacy-confidence caveat (confidence_mode) folded in.
    return {
        "schema_version": "people-query.v1",
        "confidence_mode": "legacy_alias",
        "warning": _LEGACY_ALIAS_WARNING,
        "generation_id": None,  # No compiled PeopleRegistry generation exists until Phase 1.
        "as_of": datetime.now(timezone.utc).isoformat(),
        "items": items,
        "next_cursor": None,  # Phase 0c is unpaginated; real pagination lands with Phase 3's query surface.
    }


@registry_app.command("bootstrap")
def kb_registry_bootstrap_command(
    customer_boundary_id: str | None = typer.Option(
        None, "--customer-boundary-id", help="Customer-controlled identifier (e.g. tenant/org short name). Required with --apply on first bootstrap."
    ),
    from_program: str | None = typer.Option(
        None,
        "--from-program",
        help="Selected program whose local entities/people/teams are previewed into the first shared factual root.",
    ),
    apply: bool = typer.Option(False, "--apply", help="Actually mint and persist the workspace identity. Without this flag, preview only."),
    format: str = typer.Option("human", "--format", help="Output format for --from-program migration preview: human or json."),
) -> None:
    """Create the shared registry root; unlike top-level `vertex bootstrap`, this
    initializes people-registry identity and may migrate one selected program's factual records."""
    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    if from_program is not None:
        try:
            if not apply:
                plan = bootstrap_shared_factual_files(
                    from_program,
                    programs_root=PROGRAMS_ROOT,
                    actor="<preview>",
                    apply=False,
                )
                _echo_shared_migration_plan(plan, format=format, heading="Bootstrap factual preview")
                if format == "human":
                    typer.echo("Preview only. Re-run with --apply to create the registry identity and first shared factual root.")
                raise typer.Exit(code=0)

            identity_result = bootstrap_registry_identity(
                knowledge_root=knowledge_root,
                customer_boundary_id=customer_boundary_id,
                apply=True,
            )
            principal = _resolve_operator_principal("kb-registry-bootstrap-from-program")
            plan = bootstrap_shared_factual_files(
                from_program,
                programs_root=PROGRAMS_ROOT,
                actor=principal,
                apply=True,
            )
        except ConfigError as error:
            raise typer.BadParameter(str(error)) from error
        _echo_shared_migration_plan(
            plan,
            format=format,
            heading=(
                f"Created workspace registry identity: workspace_id={identity_result.identity.workspace_id}"
                if identity_result.created
                else f"Using existing workspace registry identity: workspace_id={identity_result.identity.workspace_id}"
            ),
        )
        if format == "human":
            typer.echo("Applied first shared factual root." + (" Partial success: conflicts were quarantined." if plan.partial_success else ""))
        raise typer.Exit(code=0)

    try:
        result = bootstrap_registry_identity(knowledge_root=knowledge_root, customer_boundary_id=customer_boundary_id, apply=apply)
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error

    if not apply:
        if result.created is False and result.identity.workspace_id.startswith("<not yet minted"):
            typer.echo("Dry run: would create a new workspace registry identity and initial manifest at:")
            typer.echo(f"  {knowledge_root / 'registry.yaml'}")
            typer.echo(f"  {knowledge_root / 'registry_manifest.json'}")
            typer.echo("No factual people/team files are created at this phase. Re-run with --apply to persist.")
            raise typer.Exit(code=0)
        typer.echo(f"A workspace registry identity already exists (workspace_id={result.identity.workspace_id}); nothing to preview.")
        raise typer.Exit(code=0)

    if result.created:
        typer.echo(f"Created workspace registry identity: workspace_id={result.identity.workspace_id}, customer_boundary_id={result.identity.customer_boundary_id}")
        typer.echo(f"  {knowledge_root / 'registry.yaml'}")
        typer.echo(f"  {knowledge_root / 'registry_manifest.json'}")
    else:
        typer.echo(f"Workspace registry identity already exists (workspace_id={result.identity.workspace_id}); left unchanged.")
    raise typer.Exit(code=0)


@registry_app.command(
    "migrate-shared",
    help="Merge program-local factual records into the shared people registry; unlike top-level `vertex migrate`, this is conflict-aware.",
)
def kb_registry_migrate_shared_command(
    program_id: str = typer.Argument(..., help="Program ID whose program-local factual files will be inventoried and merged."),
    apply: bool = typer.Option(False, "--apply", help="Commit the fenced shared-registry transaction. Without this flag, preview only."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """Merge program-local factual records into the shared registry; unlike
    top-level `vertex migrate`, this is a people-registry conflict-aware migration."""
    try:
        if not apply:
            plan = preview_shared_migration(program_id, programs_root=PROGRAMS_ROOT)
            _echo_shared_migration_plan(plan, format=format, heading="Shared migration preview")
            if format == "human":
                typer.echo("Preview only. Re-run with --apply to commit the canonical staged registry transaction.")
            raise typer.Exit(code=0)
        principal = _resolve_operator_principal("kb-registry-migrate-shared")
        plan = apply_shared_migration(program_id, programs_root=PROGRAMS_ROOT, actor=principal)
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error

    _echo_shared_migration_plan(plan, format=format, heading="Applied shared migration")
    if format == "human":
        typer.echo(
            f"Committed transaction {plan.transaction_id}, generation {plan.generation_id}."
            + (" Partial success: conflicts were quarantined." if plan.partial_success else "")
        )
    raise typer.Exit(code=0)


@registry_app.command(
    "backfill-entity-ids",
    help="specs/bklg.md BL-E3: mint canonical entity_ids for people_directory.yaml/teams.yaml records that predate entities.yaml.",
)
def kb_registry_backfill_entity_ids_command(
    apply: bool = typer.Option(False, "--apply", help="Commit the fenced shared-registry transaction. Without this flag, preview only."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """One-time backfill for shared people_directory.yaml/teams.yaml records
    that were populated directly (predating entities.yaml) and so carry no
    canonical entity_id -- the exact "migration gap, not a new identity"
    state people_directory_schema.py's loader already diagnoses. Never
    touches a record that already carries a valid entity_id; idempotent."""
    try:
        if not apply:
            plan = preview_entity_id_backfill(programs_root=PROGRAMS_ROOT)
            _echo_entity_id_backfill_plan(plan, format=format, heading="Entity-id backfill preview")
            if format == "human" and not plan.is_noop:
                typer.echo("Preview only. Re-run with --apply to mint canonical entity_ids for the records above.")
            raise typer.Exit(code=0)
        principal = _resolve_operator_principal("kb-registry-backfill-entity-ids")
        plan = apply_entity_id_backfill(programs_root=PROGRAMS_ROOT, actor=principal)
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error

    _echo_entity_id_backfill_plan(plan, format=format, heading="Applied entity-id backfill")
    if format == "human" and not plan.is_noop:
        typer.echo(f"Committed transaction {plan.transaction_id}, generation {plan.generation_id}.")
    raise typer.Exit(code=0)


@registry_app.command("adopt")
def kb_registry_adopt_command(
    reason: str = typer.Option(..., "--reason", help="Why the manually edited managed registry content is being adopted."),
    on_behalf_of: str | None = typer.Option(None, "--on-behalf-of", help="Optional descriptive operator context; never grants authority."),
    apply: bool = typer.Option(False, "--apply", help="Validate, journal, checkpoint, and commit the adoption. Without this flag, preview only."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """Adopt direct managed-registry YAML edits through the canonical staged writer."""
    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    try:
        actor = _resolve_operator_principal("kb-registry-adopt") if apply else "<preview>"
        result = adopt_registry_edits(
            knowledge_root,
            actor=actor,
            reason=reason,
            on_behalf_of=on_behalf_of,
            apply=apply,
        )
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error
    payload = {
        "generation_id": result.generation_id,
        "transaction_id": result.transaction_id,
        "edits": [
            {
                "path": edit.relative_path,
                "expected_hash": edit.expected_hash,
                "actual_hash": edit.actual_hash,
                "changed_fields": list(edit.changed_fields),
                "critical": edit.critical,
            }
            for edit in result.integrity.edits
        ],
    }
    if format == "json":
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    elif format == "human":
        if not result.integrity.edits:
            typer.echo("No unadopted managed registry edits detected.")
        else:
            typer.echo(f"{'Adopted' if apply else 'Preview: would adopt'} {len(result.integrity.edits)} managed registry file(s):")
            for edit in result.integrity.edits:
                severity = "critical" if edit.critical else "informational"
                typer.echo(f"  - {edit.relative_path} ({severity}): {', '.join(edit.changed_fields)}")
            if apply:
                typer.echo(f"Committed transaction {result.transaction_id}, generation {result.generation_id}.")
            else:
                typer.echo("Preview only. Re-run with --apply to validate, journal, checkpoint, and commit the adoption.")
    else:
        raise typer.BadParameter("--format must be 'human' or 'json'.")
    raise typer.Exit(code=0)


def _parse_review_at(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise typer.BadParameter("--review-at must be an ISO-8601 timestamp.") from error
    if parsed.tzinfo is None:
        raise typer.BadParameter("--review-at must include a UTC offset.")
    return parsed.astimezone(timezone.utc)


def _run_people_governance_command(
    *,
    operation: str,
    person: str,
    fields: tuple[str, ...],
    reason: str,
    on_behalf_of: str | None,
    review_at: datetime | None,
    apply: bool,
) -> None:
    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    try:
        actor = _resolve_operator_principal(f"kb-people-{operation}") if apply else "<preview>"
        result = govern_person_fields(
            knowledge_root,
            operation=operation,
            person_ref=person,
            fields=fields,
            reason=reason,
            actor=actor,
            on_behalf_of=on_behalf_of,
            review_at=review_at,
            apply=apply,
        )
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error
    action = {"pin": "pin", "unpin": "unpin", "attest": "attest"}[operation]
    if apply:
        typer.echo(
            f"Applied {action} for {result.entity_id}: {', '.join(result.fields)}. "
            f"Transaction {result.transaction_id}, generation {result.generation_id}."
        )
    else:
        typer.echo(
            f"Preview: would {action} {result.entity_id}: {', '.join(result.fields)}. "
            "Re-run with --apply to commit the canonical staged registry transaction."
        )
    raise typer.Exit(code=0)


@people_app.command("pin")
def kb_people_pin_command(
    person: str = typer.Option(..., "--person", help="Canonical person ID or uniquely resolving alias."),
    field: str = typer.Option(..., "--field", help="Current person field to pin."),
    reason: str = typer.Option(..., "--reason", help="Why this field must not be overwritten by future observations."),
    review_at: str | None = typer.Option(None, "--review-at", help="Optional ISO-8601 review timestamp with UTC offset."),
    on_behalf_of: str | None = typer.Option(None, "--on-behalf-of", help="Optional descriptive operator context; never grants authority."),
    apply: bool = typer.Option(False, "--apply", help="Commit the pin. Without this flag, preview only."),
) -> None:
    """Pin one explicitly named, currently populated person field."""
    _run_people_governance_command(
        operation="pin",
        person=person,
        fields=(field,),
        reason=reason,
        on_behalf_of=on_behalf_of,
        review_at=_parse_review_at(review_at),
        apply=apply,
    )


@people_app.command("unpin")
def kb_people_unpin_command(
    person: str = typer.Option(..., "--person", help="Canonical person ID or uniquely resolving alias."),
    field: str = typer.Option(..., "--field", help="Currently pinned person field to release."),
    reason: str = typer.Option(..., "--reason", help="Why the existing pin is being removed."),
    on_behalf_of: str | None = typer.Option(None, "--on-behalf-of", help="Optional descriptive operator context; never grants authority."),
    apply: bool = typer.Option(False, "--apply", help="Commit the unpin. Without this flag, preview only."),
) -> None:
    """Remove a pin from one explicitly named person field."""
    _run_people_governance_command(
        operation="unpin",
        person=person,
        fields=(field,),
        reason=reason,
        on_behalf_of=on_behalf_of,
        review_at=None,
        apply=apply,
    )


@people_app.command("attest")
def kb_people_attest_command(
    person: str = typer.Option(..., "--person", help="Canonical person ID or uniquely resolving alias."),
    field: list[str] = typer.Option(..., "--field", help="One or more current person fields to human-attest."),
    reason: str = typer.Option(..., "--reason", help="Evidence/rationale for the human verification."),
    on_behalf_of: str | None = typer.Option(None, "--on-behalf-of", help="Optional descriptive operator context; never grants authority."),
    apply: bool = typer.Option(False, "--apply", help="Commit the attestation. Without this flag, preview only."),
) -> None:
    """Record human verification for explicitly named, currently populated person fields."""
    _run_people_governance_command(
        operation="attest",
        person=person,
        fields=tuple(field),
        reason=reason,
        on_behalf_of=on_behalf_of,
        review_at=None,
        apply=apply,
    )


@people_app.command("lifecycle-set")
def kb_people_lifecycle_set_command(
    person: str = typer.Option(..., "--person", help="Canonical person ID or uniquely resolving alias."),
    status: str = typer.Option(..., "--status", help="New lifecycle status: active | inactive | departed | unknown."),
    reason: str = typer.Option(..., "--reason", help="Required steward review rationale."),
    apply: bool = typer.Option(False, "--apply", help="Commit the reviewed transition. Without this flag, preview only."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """specs/people.md PPL-W6.3a: transition a person's lifecycle status (§7.6: "Departure/inactivation is represented explicitly")."""
    try:
        new_status = PersonStatus(status.strip().lower())
    except ValueError as error:
        raise typer.BadParameter(f"--status must be one of {[s.value for s in PersonStatus]}, got {status!r}.") from error
    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    try:
        result = transition_person_lifecycle_status(
            knowledge_root,
            person_ref=person,
            new_status=new_status,
            reason=reason,
            actor=_resolve_operator_principal("kb-people-lifecycle-set") if apply else "<preview>",
            apply=apply,
        )
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error
    if format == "json":
        typer.echo(json.dumps(
            {
                "entity_id": result.entity_id, "from_status": result.from_status.value, "to_status": result.to_status.value,
                "transaction_id": result.transaction_id, "generation_id": result.generation_id,
            },
            indent=2, sort_keys=True,
        ))
    elif format == "human":
        prefix = "Applied" if apply else "Preview: would apply"
        typer.echo(f"{prefix} lifecycle transition for {result.entity_id}: {result.from_status.value} -> {result.to_status.value}.")
        if not apply:
            typer.echo("Re-run with --apply to commit the canonical staged registry transaction.")
    else:
        raise typer.BadParameter("--format must be 'human' or 'json'.")
    raise typer.Exit(code=0)


def _correction_payload(result: PeopleCorrectionResult) -> dict[str, object]:
    return {
        "operation": result.operation,
        "source_entity_id": result.source_entity_id,
        "target_entity_id": result.target_entity_id,
        "affected_paths": list(result.affected_paths),
        "conflicts": [
            {"kind": conflict.kind, "detail": conflict.detail, "source_path": conflict.source_path}
            for conflict in result.conflicts
        ],
        "authored_references": [
            {"source_path": reference.source_path, "field_path": reference.field_path}
            for reference in result.authored_references
        ],
        "transaction_id": result.transaction_id,
        "generation_id": result.generation_id,
    }


def _echo_correction_result(result: PeopleCorrectionResult, *, apply: bool, format: str) -> None:
    if format == "json":
        typer.echo(json.dumps(_correction_payload(result), indent=2, sort_keys=True))
        return
    if format != "human":
        raise typer.BadParameter("--format must be 'human' or 'json'.")
    action = "Applied" if apply else "Preview: would apply"
    source = f" from {result.source_entity_id}" if result.source_entity_id else ""
    typer.echo(f"{action} {result.operation}{source} to {result.target_entity_id}.")
    typer.echo(f"Affected mutable files: {', '.join(result.affected_paths) or 'none'}.")
    for conflict in result.conflicts:
        location = f" ({conflict.source_path})" if conflict.source_path else ""
        typer.echo(f"Conflict: {conflict.kind}{location}: {conflict.detail}")
    if result.authored_references:
        typer.echo(f"Authored references left as conflicts: {len(result.authored_references)}.")
    if apply:
        typer.echo(f"Committed transaction {result.transaction_id}, generation {result.generation_id}.")
    else:
        typer.echo("Preview only. Re-run with --apply after steward review to commit the canonical staged registry transaction.")


@people_app.command("merge")
def kb_people_merge_command(
    source: str = typer.Option(..., "--from", help="Source canonical person ID or uniquely resolving alias to tombstone."),
    target: str = typer.Option(..., "--into", help="Surviving canonical person ID or uniquely resolving alias."),
    reason: str = typer.Option(..., "--reason", help="Required steward review rationale."),
    apply: bool = typer.Option(False, "--apply", help="Commit the reviewed merge. Without this flag, preview only."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """Merge two reviewed people and redirect current mutable references through the canonical writer."""
    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    try:
        result = merge_people(
            knowledge_root,
            source_ref=source,
            target_ref=target,
            reason=reason,
            actor=_resolve_operator_principal("kb-people-merge") if apply else "<preview>",
            apply=apply,
        )
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error
    _echo_correction_result(result, apply=apply, format=format)
    raise typer.Exit(code=0)


@people_app.command(
    "merge-batch",
    help="specs/bklg.md BL-E3 DIR-01: merge multiple independent duplicate-alias pairs in one commit.",
)
def kb_people_merge_batch_command(
    pair: list[str] = typer.Option(
        ...,
        "--pair",
        help="A 'source_entity_id->target_entity_id' pair to merge (source tombstoned into target). Repeatable. "
        "Uses '->', not ':', as the separator since entity_ids themselves contain a colon (e.g. 'person:01H...').",
    ),
    reason: str = typer.Option(..., "--reason", help="Required steward review rationale, applied to every pair."),
    apply: bool = typer.Option(False, "--apply", help="Commit the reviewed merges. Without this flag, preview only."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """Merge N independent duplicate-alias pairs in a single commit. Needed
    because a single `merge` commit validates the WHOLE registry document,
    not just the pair being merged -- with more than one unrelated
    duplicate present, resolving them one at a time always fails on
    whichever pair hasn't been reached yet. Every pair must reference exact
    canonical entity_ids (not aliases), since aliases here are ambiguous by
    construction."""
    parsed_pairs: list[tuple[str, str]] = []
    for entry in pair:
        if "->" not in entry:
            raise typer.BadParameter(f"--pair {entry!r} must be 'source_entity_id->target_entity_id'.")
        source_id, target_id = entry.split("->", 1)
        parsed_pairs.append((source_id.strip(), target_id.strip()))

    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    try:
        result = merge_people_batch(
            knowledge_root,
            merges=tuple(parsed_pairs),
            reason=reason,
            actor=_resolve_operator_principal("kb-people-merge-batch") if apply else "<preview>",
            apply=apply,
        )
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error
    _echo_correction_result(result, apply=apply, format=format)
    raise typer.Exit(code=0)


@people_app.command("bind")
def kb_people_bind_command(
    person: str = typer.Option(..., "--person", help="Canonical person ID or uniquely resolving alias."),
    provider: str = typer.Option(..., "--provider", help="Identity provider name."),
    subject_id: str = typer.Option(..., "--subject-id", help="Exact provider-issued stable subject ID."),
    reason: str = typer.Option(..., "--reason", help="Required steward review rationale."),
    apply: bool = typer.Option(False, "--apply", help="Commit the reviewed binding. Without this flag, preview only."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """Bind an exact stable provider subject to a reviewed canonical person."""
    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    try:
        result = bind_person_identifier(
            knowledge_root,
            person_ref=person,
            provider=provider,
            subject_id=subject_id,
            reason=reason,
            actor=_resolve_operator_principal("kb-people-bind") if apply else "<preview>",
            apply=apply,
        )
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error
    _echo_correction_result(result, apply=apply, format=format)
    raise typer.Exit(code=0)


@people_app.command("split")
def kb_people_split_command(
    person: str = typer.Option(..., "--person", help="Canonical source person ID or uniquely resolving alias."),
    alias: list[str] = typer.Option(..., "--alias", help="Alias to partition into the newly created person; repeat as needed."),
    retain_alias: list[str] = typer.Option(..., "--retain-alias", help="Alias explicitly retained by the source person; repeat as needed."),
    identifier: list[str] = typer.Option([], "--identifier", help="Provider identifier to partition as provider:subject-id; repeat as needed."),
    retain_identifier: list[str] = typer.Option([], "--retain-identifier", help="Provider identifier retained by source as provider:subject-id; repeat as needed."),
    new_id: str | None = typer.Option(None, "--new-id", help="Optional new opaque person ID; defaults to a minted person:<ULID>."),
    reason: str = typer.Option(..., "--reason", help="Required steward review rationale."),
    apply: bool = typer.Option(False, "--apply", help="Commit the reviewed split. Without this flag, preview only."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """Split explicitly partitioned aliases/provider IDs; ambiguous authored references remain conflicts."""
    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    try:
        result = split_person(
            knowledge_root,
            person_ref=person,
            aliases_for_new_person=tuple(alias),
            aliases_retained_by_source=tuple(retain_alias),
            identifiers_for_new_person=tuple(identifier),
            identifiers_retained_by_source=tuple(retain_identifier),
            new_entity_id=new_id,
            reason=reason,
            actor=_resolve_operator_principal("kb-people-split") if apply else "<preview>",
            apply=apply,
            programs_root=PROGRAMS_ROOT,
        )
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error
    _echo_correction_result(result, apply=apply, format=format)
    raise typer.Exit(code=0)


@people_app.command("unmerge")
def kb_people_unmerge_command(
    source: str = typer.Option(..., "--from", help="Original tombstoned source canonical person ID."),
    reason: str = typer.Option(..., "--reason", help="Required steward review rationale."),
    apply: bool = typer.Option(False, "--apply", help="Commit a safe merge reversal. Without this flag, preview only."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """Reverse a known merge only when its mutable generation remains unchanged."""
    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    try:
        result = unmerge_people(
            knowledge_root,
            source_ref=source,
            reason=reason,
            actor=_resolve_operator_principal("kb-people-unmerge") if apply else "<preview>",
            apply=apply,
        )
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error
    _echo_correction_result(result, apply=apply, format=format)
    raise typer.Exit(code=0)


def _parse_required_iso_timestamp(value: str, *, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise typer.BadParameter(f"--{field_name} must be an ISO-8601 timestamp.") from error
    if parsed.tzinfo is None:
        raise typer.BadParameter(f"--{field_name} must include a UTC offset.")
    return parsed.astimezone(timezone.utc)


def _delegation_payload(delegation: Delegation) -> dict:
    payload = delegation_to_payload(delegation)
    payload["status"] = delegation.status.value
    return payload


@delegate_app.command("create")
def kb_people_delegate_create_command(
    from_person: str = typer.Option(..., "--from", help="Delegating canonical person ID or uniquely resolving alias."),
    to_person: str = typer.Option(..., "--to", help="Delegate canonical person ID or uniquely resolving alias."),
    surface: list[str] = typer.Option(..., "--surface", help="Surface the delegation applies to (e.g. vertex::nudge); repeat as needed."),
    valid_from: str = typer.Option(..., "--valid-from", help="ISO-8601 timestamp (with UTC offset) the delegation becomes active."),
    valid_until: str = typer.Option(..., "--valid-until", help="ISO-8601 timestamp (with UTC offset) the delegation expires."),
    reason: str = typer.Option(..., "--reason", help="Required steward review rationale."),
    program: list[str] = typer.Option([], "--program", help="Program ID this delegation is scoped to; repeat as needed. Empty means all programs."),
    workstream: list[str] = typer.Option([], "--workstream", help="Workstream ID this delegation is scoped to; repeat as needed."),
    apply: bool = typer.Option(False, "--apply", help="Commit the reviewed delegation. Without this flag, preview only."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """specs/people.md PPL-W5b.2: create a steward-authorized delegation, gated by the delegation_enabled kill switch."""
    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    try:
        delegation = create_delegation(
            knowledge_root,
            from_ref=from_person,
            to_ref=to_person,
            surfaces=tuple(surface),
            valid_from=_parse_required_iso_timestamp(valid_from, field_name="valid-from"),
            valid_until=_parse_required_iso_timestamp(valid_until, field_name="valid-until"),
            reason=reason,
            actor=_resolve_operator_principal("kb-people-delegate-create") if apply else "<preview>",
            apply=apply,
            program_ids=tuple(program),
            workstream_ids=tuple(workstream),
        )
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error
    if format == "json":
        typer.echo(json.dumps(_delegation_payload(delegation), indent=2, sort_keys=True))
    elif format == "human":
        prefix = "Applied" if apply else "Preview: would create"
        typer.echo(
            f"{prefix} delegation {delegation.delegation_id}: {delegation.from_person_entity_id} -> "
            f"{delegation.to_person_entity_id} for {', '.join(delegation.surfaces)}."
        )
        if not apply:
            typer.echo("Re-run with --apply to commit the canonical staged registry transaction.")
    else:
        raise typer.BadParameter("--format must be 'human' or 'json'.")
    raise typer.Exit(code=0)


@delegate_app.command("revoke")
def kb_people_delegate_revoke_command(
    delegation_id: str = typer.Option(..., "--delegation-id", help="Delegation ID to revoke."),
    reason: str = typer.Option(..., "--reason", help="Required steward review rationale."),
    apply: bool = typer.Option(False, "--apply", help="Commit the reviewed revocation. Without this flag, preview only."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """specs/people.md PPL-W5b.2: revoke an existing delegation."""
    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    try:
        delegation = revoke_delegation(
            knowledge_root,
            delegation_id=delegation_id,
            reason=reason,
            actor=_resolve_operator_principal("kb-people-delegate-revoke") if apply else "<preview>",
            apply=apply,
        )
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error
    if format == "json":
        typer.echo(json.dumps(_delegation_payload(delegation), indent=2, sort_keys=True))
    elif format == "human":
        prefix = "Applied" if apply else "Preview: would revoke"
        typer.echo(f"{prefix} delegation {delegation.delegation_id}.")
        if not apply:
            typer.echo("Re-run with --apply to commit the canonical staged registry transaction.")
    else:
        raise typer.BadParameter("--format must be 'human' or 'json'.")
    raise typer.Exit(code=0)


@delegate_app.command("list")
def kb_people_delegate_list_command(
    active_only: bool = typer.Option(False, "--active-only", help="Only show currently active, in-window delegations."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """specs/people.md PPL-W5b.2: read back delegations.yaml. Read-only; not gated by the delegation_enabled kill switch."""
    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    delegations = list_delegations(knowledge_root, active_only=active_only)
    if format == "json":
        typer.echo(json.dumps([_delegation_payload(delegation) for delegation in delegations], indent=2, sort_keys=True))
    elif format == "human":
        if not delegations:
            typer.echo("No delegations found.")
        for delegation in delegations:
            typer.echo(
                f"{delegation.delegation_id}: {delegation.from_person_entity_id} -> {delegation.to_person_entity_id} "
                f"[{delegation.status.value}] surfaces={','.join(delegation.surfaces)} "
                f"valid={delegation.valid_from.isoformat()}..{delegation.valid_until.isoformat()}"
            )
    else:
        raise typer.BadParameter("--format must be 'human' or 'json'.")
    raise typer.Exit(code=0)


def _refresh_payload(result: RefreshResult) -> dict:
    def observation_payload(observation) -> dict:
        return {
            "request_id": observation.request_id,
            "entity_id": observation.entity_id,
            "field_name": observation.field_name,
            "confidence": observation.confidence,
            "outcome": observation.outcome,
            "reason": observation.reason,
        }

    def team_diff_payload(diff) -> dict:
        return {
            "team_id": diff.team_id,
            "team_entity_id": diff.team_entity_id,
            "added_person_aliases": list(diff.added_person_aliases),
            "removed_person_aliases": list(diff.removed_person_aliases),
            "unresolved_provider_aliases": list(diff.unresolved_provider_aliases),
            "complete": diff.complete,
        }

    return {
        "provider": result.provider,
        "refresh_run_id": result.refresh_run_id,
        "requested_person_count": result.requested_person_count,
        "requested_team_count": result.requested_team_count,
        "kill_switch_engaged": result.kill_switch_engaged,
        "accepted": [observation_payload(o) for o in result.accepted],
        "quarantined": [observation_payload(o) for o in result.quarantined],
        "rejected": [observation_payload(o) for o in result.rejected],
        "unresolved": [observation_payload(o) for o in result.unresolved],
        "team_membership_diffs": [team_diff_payload(d) for d in result.team_membership_diffs],
        "partial_success": result.partial_success,
        "transaction_id": result.write_result.transaction_id if result.write_result else None,
        "generation_id": result.write_result.generation_id if result.write_result else None,
    }


@people_app.command("refresh")
def kb_people_refresh_command(
    provider: str = typer.Option(..., "--provider", help="Identity provider name from identity_providers.yaml."),
    person: list[str] = typer.Option([], "--person", help="Canonical person ID or uniquely resolving alias to refresh; repeat as needed."),
    team: list[str] = typer.Option([], "--team", help="Canonical team ID or uniquely resolving alias to refresh membership for; repeat as needed."),
    import_file: Path | None = typer.Option(None, "--import-file", help="Operator-exported CSV/JSON directory snapshot (required for a local_directory_export provider)."),
    reason: str = typer.Option(..., "--reason", help="Required review rationale for the refresh."),
    apply: bool = typer.Option(False, "--apply", help="Commit accepted observations, quarantine below-threshold ones, and apply membership diffs. Without this flag, preview only."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """specs/people.md PPL-W4.4/PPL-W4.4b: refresh --person's display_name/title/department/contacts
    and/or --team's membership roster from a configured identity provider, routed through the
    canonical staged writer. A membership snapshot only applies when it is COMPLETE (§6.7)."""
    try:
        result = refresh_people_from_provider(
            programs_root=PROGRAMS_ROOT,
            provider_name=provider,
            person_refs=tuple(person),
            team_refs=tuple(team),
            import_file=import_file,
            actor=_resolve_operator_principal("kb-people-refresh") if apply else "<preview>",
            reason=reason,
            apply=apply,
        )
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error

    if format == "json":
        typer.echo(json.dumps(_refresh_payload(result), indent=2, sort_keys=True))
        raise typer.Exit(code=0)
    if format != "human":
        raise typer.BadParameter("--format must be 'human' or 'json'.")

    if result.kill_switch_engaged:
        typer.echo("Provider refresh is disabled (registry.yaml provider_refresh_enabled or the environment kill switch). No provider was contacted.")
        raise typer.Exit(code=0)
    action = "Applied" if apply else "Preview: would apply"
    typer.echo(
        f"{action} refresh from provider {result.provider!r} ({result.refresh_run_id}) for "
        f"{result.requested_person_count} requested person(s) and {result.requested_team_count} requested team(s)."
    )
    typer.echo(f"Accepted: {len(result.accepted)}. Quarantined: {len(result.quarantined)}. Rejected: {len(result.rejected)}. Unresolved: {len(result.unresolved)}.")
    for observation in result.rejected:
        typer.echo(f"Rejected {observation.field_name!r}: {observation.reason}")
    for diff in result.team_membership_diffs:
        if not diff.complete:
            typer.echo(f"Team {diff.team_id!r}: incomplete provider snapshot; membership left unchanged.")
            continue
        typer.echo(f"Team {diff.team_id!r}: +{len(diff.added_person_aliases)} -{len(diff.removed_person_aliases)} member(s).")
        if diff.unresolved_provider_aliases:
            typer.echo(f"Team {diff.team_id!r}: unresolved provider member(s), not created: {', '.join(diff.unresolved_provider_aliases)}.")
    if apply and result.write_result is not None and result.write_result.transaction_id:
        typer.echo(f"Committed transaction {result.write_result.transaction_id}, generation {result.write_result.generation_id}.")
    elif not apply:
        typer.echo("Preview only. Re-run with --apply to commit accepted fields, quarantine below-threshold observations, and apply membership diffs.")
    raise typer.Exit(code=0)


def _query_generation_id() -> str | None:
    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    manifest = load_registry_manifest(knowledge_root)
    return manifest.generation_id if manifest is not None else None


def _query_envelope_payload(*, items: Sequence[Mapping[str, object]], next_cursor: str | None) -> dict[str, object]:
    return {
        "schema_version": "people-query.v1",
        "generation_id": _query_generation_id(),
        "as_of": datetime.now(timezone.utc).isoformat(),
        "items": items,
        "next_cursor": next_cursor,
    }


@people_app.command("show")
def kb_people_show_command(
    person: str = typer.Option(..., "--person", help="Canonical person ID or an alias/ref that resolves to one (P:<alias>, person:<alias>, bare alias, or person:<ULID>)."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """specs/people.md PPL-W3.1: show one canonical person's directory record, profile, and current memberships."""
    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    result = find_person(person, knowledge_root=knowledge_root)
    if result is None:
        if format == "json":
            typer.echo(json.dumps(_query_envelope_payload(items=[], next_cursor=None), indent=2, sort_keys=True))
            raise typer.Exit(code=0)
        typer.echo(f"No canonical person found for {person!r}.")
        raise typer.Exit(code=1)

    item = {
        "entity": entity_to_payload(result.entity),
        "directory": person_to_payload(result.directory) if result.directory is not None else None,
        "memberships": [membership_to_payload(m) for m in result.memberships],
        "resolved_via": result.resolved_via,
    }
    if format == "json":
        typer.echo(json.dumps(_query_envelope_payload(items=[item], next_cursor=None), indent=2, sort_keys=True))
        raise typer.Exit(code=0)
    typer.echo(f"{result.entity.canonical_name} ({result.entity.entity_id}) -- resolved via {result.resolved_via}")
    if result.directory is not None:
        typer.echo(f"  alias: {result.directory.alias}  status: {result.directory.status.value}  title: {result.directory.title or '-'}")
        for contact in result.directory.contacts:
            typer.echo(f"  contact: {contact.kind.value}={contact.value} ({contact.status.value}, delivery_eligible={contact.delivery_eligible})")
    else:
        typer.echo("  no people_directory.yaml record for this entity.")
    typer.echo(f"  memberships: {len(result.memberships)}")
    for membership in sorted(result.memberships, key=lambda m: m.team_entity_id):
        typer.echo(f"    - team {membership.team_entity_id} role={membership.role or 'unknown'} status={membership.status.value}")
    raise typer.Exit(code=0)


@people_app.command("find")
def kb_people_find_command(
    text: str = typer.Argument(..., help="Text to search for over alias/display_name."),
    limit: int = typer.Option(20, "--limit", help="Maximum number of candidates to return."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """specs/people.md PPL-W3.1/§8.2: bounded, scored candidate lookup -- never an automatic binding."""
    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    candidates = search_people(text, knowledge_root=knowledge_root, limit=limit)
    if format == "json":
        items = [
            {"entity_id": c.entity_id, "alias": c.alias, "display_name": c.display_name, "score": c.score, "match_kind": c.match_kind}
            for c in candidates
        ]
        typer.echo(json.dumps(_query_envelope_payload(items=items, next_cursor=None), indent=2, sort_keys=True))
        raise typer.Exit(code=0)
    if not candidates:
        typer.echo(f"No candidates found for {text!r}.")
        raise typer.Exit(code=0)
    typer.echo(f"{len(candidates)} candidate(s) for {text!r}:")
    for candidate in candidates:
        typer.echo(f"  - {candidate.alias} ({candidate.entity_id}) score={candidate.score:.2f} [{candidate.match_kind}]")
    raise typer.Exit(code=0)


@people_app.command("stale")
def kb_people_stale_command(
    freshness_days: int = typer.Option(DEFAULT_STALE_FRESHNESS_DAYS, "--freshness-days", help="Freshness window in days (default: freshness_policy.yaml's people_registry.stale_after_days, DIR-03)."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """specs/people.md PPL-W3.1: people whose verified fields/contacts are older than the freshness window."""
    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    entries = list_stale_people(knowledge_root=knowledge_root, freshness_days=freshness_days)
    if format == "json":
        items = [
            {"entity_id": e.entity_id, "alias": e.alias, "field_name": e.field_name, "verified_at": e.verified_at.isoformat(), "age_days": e.age_days}
            for e in entries
        ]
        typer.echo(json.dumps(_query_envelope_payload(items=items, next_cursor=None), indent=2, sort_keys=True))
        raise typer.Exit(code=0)
    if not entries:
        typer.echo(f"No stale fields found (freshness window: {freshness_days} day(s)).")
        raise typer.Exit(code=0)
    typer.echo(f"{len(entries)} stale field(s) (freshness window: {freshness_days} day(s)):")
    for entry in entries:
        typer.echo(f"  - {entry.alias} ({entry.entity_id}): {entry.field_name} last verified {entry.age_days}d ago")
    raise typer.Exit(code=0)


@people_app.command("enrich")
def kb_people_enrich_command(
    program: str = typer.Option(..., "--program", help="Program ID whose currently-referenced stakeholders are checked for stale/missing enrichable fields."),
    max_candidates: int = typer.Option(5, "--max-candidates", help="Cap on WorkIQ round-trips this run (each is slow, 36-180s) -- keeps one invocation bounded."),
    freshness_days: int = typer.Option(DEFAULT_STALE_FRESHNESS_DAYS, "--freshness-days", help="Freshness window in days (default: freshness_policy.yaml's people_registry.stale_after_days)."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """specs/bklg.md BL-E3: demand-driven WorkIQ enrichment pass.

    Selects real stakeholders of --program whose title/department/manager
    is stale or was never verified, asks WorkIQ one targeted question per
    field, and records each answer as a PENDING review candidate --
    nothing is ever written to the registry here. Run 'vertex kb people
    enrichment resolve' to review and, if accepted, apply a candidate.
    """
    try:
        resolve_enrichment_due_alert(program_id=program, programs_root=PROGRAMS_ROOT)
    except (OSError, StateError):
        pass
    try:
        selected = select_enrichment_candidates(
            program_id=program, programs_root=PROGRAMS_ROOT, freshness_days=freshness_days,
        )
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error
    if not selected:
        typer.echo(f"No enrichment candidates found for program {program!r} (nothing stale/missing among current stakeholders).")
        raise typer.Exit(code=0)
    selected = selected[:max_candidates]

    bridge = AgencyBridge()
    capabilities = bridge.probe()
    if not capabilities.available and not capabilities.has_workiq_cli:
        typer.echo("Agency CLI is unavailable; cannot run WorkIQ enrichment right now.")
        raise typer.Exit(code=1)
    if not capabilities.has_workiq and not capabilities.has_workiq_cli:
        typer.echo("WorkIQ is not available via Agency CLI right now.")
        raise typer.Exit(code=1)

    now = datetime.now(timezone.utc)
    proposed: list[EnrichmentCandidateEvent] = []
    for person, entry in selected:
        question = build_workiq_question(display_name=person.display_name, alias=person.alias, field_name=entry.field_name)
        payload = bridge.ask_workiq(question)
        answer = prose_text_from_payload(payload)
        if answer is None:
            detail = bridge.last_mcp_error() or "no response"
            typer.echo(f"  - {person.alias}/{entry.field_name}: WorkIQ query failed ({detail}); skipped.")
            continue
        candidate_id = f"enrich-{new_ulid(now)}"
        event = EnrichmentCandidateEvent(
            recorded_at=now, program_id=program, candidate_id=candidate_id, entity_id=person.entity_id,
            alias=person.alias, field_name=entry.field_name, current_value=getattr(person, entry.field_name, None),
            event="proposed", workiq_question=question, workiq_answer=answer,
        )
        record_enrichment_event(event, programs_root=PROGRAMS_ROOT)
        proposed.append(event)

    if format == "json":
        typer.echo(json.dumps([_enrichment_event_payload(e) for e in proposed], indent=2, sort_keys=True))
        raise typer.Exit(code=0)
    typer.echo(f"{len(proposed)} enrichment candidate(s) proposed for program {program!r}:")
    for event in proposed:
        typer.echo(f"  - {event.candidate_id}: {event.alias}/{event.field_name} -> WorkIQ says: {event.workiq_answer!r}")
    if proposed:
        typer.echo("Run 'vertex kb people enrichment resolve --program <id> --candidate-id <id> --decision accept|reject --reason <text>' to review.")
    raise typer.Exit(code=0)


@people_app.command("enrichment-list")
def kb_people_enrichment_list_command(
    program: str = typer.Option(..., "--program", help="Program ID."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """specs/bklg.md BL-E3: list pending WorkIQ enrichment candidates awaiting steward review."""
    pending = list_pending_enrichment_candidates(program, programs_root=PROGRAMS_ROOT)
    if format == "json":
        typer.echo(json.dumps([_enrichment_state_payload(s) for s in pending], indent=2, sort_keys=True))
        raise typer.Exit(code=0)
    if not pending:
        typer.echo(f"No pending enrichment candidates for program {program!r}.")
        raise typer.Exit(code=0)
    typer.echo(f"{len(pending)} pending enrichment candidate(s) for program {program!r}:")
    for state in pending:
        current = state.current_value if state.current_value else "<empty>"
        typer.echo(f"  - {state.candidate_id}: {state.alias}/{state.field_name} (current: {current})")
        typer.echo(f"      WorkIQ Q: {state.workiq_question}")
        typer.echo(f"      WorkIQ A: {state.workiq_answer!r}")
    raise typer.Exit(code=0)


@people_app.command("enrichment-resolve")
def kb_people_enrichment_resolve_command(
    program: str = typer.Option(..., "--program", help="Program ID."),
    candidate_id: str = typer.Option(..., "--candidate-id", help="The pending candidate to resolve."),
    decision: str = typer.Option(..., "--decision", help="'accept' or 'reject'."),
    value: str | None = typer.Option(None, "--value", help="Accepted value to write (defaults to WorkIQ's raw answer if omitted on accept)."),
    reason: str = typer.Option(..., "--reason", help="Required steward review rationale."),
    apply: bool = typer.Option(False, "--apply", help="Commit an accepted candidate through the staged writer. Without this flag, preview only."),
) -> None:
    """specs/bklg.md BL-E3: human-in-the-loop resolution of one WorkIQ enrichment candidate.

    A WorkIQ answer is NEVER auto-applied -- 'accept' requires an explicit
    steward decision (and --apply to actually commit it); 'reject' just
    closes the candidate with no registry write at all.
    """
    if decision not in {"accept", "reject"}:
        raise typer.BadParameter("--decision must be 'accept' or 'reject'.")
    pending = {state.candidate_id: state for state in list_pending_enrichment_candidates(program, programs_root=PROGRAMS_ROOT)}
    state = pending.get(candidate_id)
    if state is None:
        raise typer.BadParameter(f"No pending candidate {candidate_id!r} for program {program!r}.")

    now = datetime.now(timezone.utc)
    actor = _resolve_operator_principal("kb-people-enrichment-resolve") if apply else "<preview>"
    resolved_value = value if value is not None else state.workiq_answer
    applied = False

    if decision == "accept":
        if not apply:
            typer.echo(f"Preview: would accept {candidate_id} and set {state.field_name}={resolved_value!r} for {state.alias}.")
            raise typer.Exit(code=0)
        result = apply_shared_registry_patch(
            operations=(
                RegistryPatchOperation(
                    relative_path="knowledge/people_directory.yaml", action="set_fields",
                    match_value=state.alias, fields=((state.field_name, resolved_value),),
                ),
            ),
            programs_root=PROGRAMS_ROOT, actor=actor,
            reason=f"BL-E3 enrichment candidate {candidate_id} accepted: {reason}",
            source="workiq_enrichment_reviewed", source_ref=candidate_id, apply=True,
        )
        if result.conflicts:
            typer.echo(f"Conflicts, not applied: {result.conflicts}")
            raise typer.Exit(code=1)
        applied = True
    else:
        if not apply:
            typer.echo(f"Preview: would reject {candidate_id} (no registry write).")
            raise typer.Exit(code=0)

    record_enrichment_event(
        EnrichmentCandidateEvent(
            recorded_at=now, program_id=program, candidate_id=candidate_id, entity_id=state.entity_id,
            alias=state.alias, field_name=state.field_name, current_value=state.current_value,
            event="accepted" if decision == "accept" else "rejected",
            reviewed_value=resolved_value if decision == "accept" else None,
            reviewed_by=actor, reviewed_reason=reason, applied=applied,
        ),
        programs_root=PROGRAMS_ROOT,
    )
    typer.echo(f"{'Accepted and applied' if applied else 'Rejected'}: {candidate_id} ({state.alias}/{state.field_name}).")
    raise typer.Exit(code=0)


def _enrichment_event_payload(event: EnrichmentCandidateEvent) -> dict[str, Any]:
    return {
        "candidate_id": event.candidate_id, "entity_id": event.entity_id, "alias": event.alias,
        "field_name": event.field_name, "workiq_question": event.workiq_question, "workiq_answer": event.workiq_answer,
    }


def _enrichment_state_payload(state: EnrichmentCandidateState) -> dict[str, Any]:
    return {
        "candidate_id": state.candidate_id, "entity_id": state.entity_id, "alias": state.alias,
        "field_name": state.field_name, "current_value": state.current_value,
        "workiq_question": state.workiq_question, "workiq_answer": state.workiq_answer,
        "status": state.status, "proposed_at": state.proposed_at.isoformat(),
    }


@people_app.command("conflicts")
def kb_people_conflicts_command(
    status: str | None = typer.Option(None, "--status", help="Filter to 'open' or 'resolved'. Omit for both."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """specs/people.md PPL-W3.1: quarantined identity/source conflicts from people_conflicts.jsonl (§8.3 DIR-12)."""
    if status is not None and status not in {"open", "resolved"}:
        raise typer.BadParameter("--status must be 'open' or 'resolved'.")
    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    entries = list_conflicts(knowledge_root=knowledge_root, status=status)
    if format == "json":
        items = [
            {
                "conflict_id": e.conflict_id, "decision": e.decision, "entity_id": e.entity_id, "reason": e.reason,
                "recorded_at": e.recorded_at.isoformat(), "status": e.status,
            }
            for e in entries
        ]
        typer.echo(json.dumps(_query_envelope_payload(items=items, next_cursor=None), indent=2, sort_keys=True))
        raise typer.Exit(code=0)
    if not entries:
        typer.echo("No conflicts found.")
        raise typer.Exit(code=0)
    typer.echo(f"{len(entries)} conflict(s):")
    for entry in entries:
        typer.echo(f"  - [{entry.status}] {entry.conflict_id} ({entry.decision}) entity={entry.entity_id or '-'}: {entry.reason}")
    raise typer.Exit(code=0)


@teams_app.command("show")
def kb_teams_show_command(
    team: str = typer.Option(..., "--team", help="Canonical team ID or an alias/ref that resolves to one."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """specs/people.md PPL-W3.1: show one canonical team's directory record."""
    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    result = find_team(team, knowledge_root=knowledge_root)
    if result is None:
        if format == "json":
            typer.echo(json.dumps(_query_envelope_payload(items=[], next_cursor=None), indent=2, sort_keys=True))
            raise typer.Exit(code=0)
        typer.echo(f"No canonical team found for {team!r}.")
        raise typer.Exit(code=1)

    item = {
        "entity": entity_to_payload(result.entity),
        "team": team_to_payload(result.team) if result.team is not None else None,
        "resolved_via": result.resolved_via,
    }
    if format == "json":
        typer.echo(json.dumps(_query_envelope_payload(items=[item], next_cursor=None), indent=2, sort_keys=True))
        raise typer.Exit(code=0)
    typer.echo(f"{result.entity.canonical_name} ({result.entity.entity_id}) -- resolved via {result.resolved_via}")
    if result.team is not None:
        typer.echo(f"  kind: {result.team.kind.value}  status: {result.team.status.value}")
        if result.team.legacy_programs:
            typer.echo(f"  legacy_programs: {', '.join(result.team.legacy_programs)}")
    else:
        typer.echo("  no teams.yaml record for this entity.")
    raise typer.Exit(code=0)


@teams_app.command("members")
def kb_teams_members_command(
    team: str = typer.Option(..., "--team", help="Canonical team ID or an alias/ref that resolves to one."),
    as_of: str | None = typer.Option(None, "--as-of", help="ISO timestamp to resolve membership validity as of. Omit for the current hot set."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """specs/people.md PPL-W3.1: current (or --as-of historical) membership roster for one canonical team."""
    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    as_of_dt = datetime.fromisoformat(as_of) if as_of else None
    result = team_members(team, knowledge_root=knowledge_root, as_of=as_of_dt)
    if result is None:
        if format == "json":
            typer.echo(json.dumps(_query_envelope_payload(items=[], next_cursor=None), indent=2, sort_keys=True))
            raise typer.Exit(code=0)
        typer.echo(f"No canonical team found for {team!r}.")
        raise typer.Exit(code=1)

    if format == "json":
        items = [membership_to_payload(m) for m in result.members]
        typer.echo(json.dumps(_query_envelope_payload(items=items, next_cursor=None), indent=2, sort_keys=True))
        raise typer.Exit(code=0)
    typer.echo(f"{result.team.entity.canonical_name} ({result.team.entity.entity_id}): {len(result.members)} member(s)")
    for member in sorted(result.members, key=lambda m: m.person_entity_id):
        typer.echo(f"  - {member.person_entity_id} role={member.role or 'unknown'} status={member.status.value}")
    raise typer.Exit(code=0)


def _shared_migration_plan_payload(plan: SharedMigrationPlan) -> dict[str, object]:
    def summary_payload(summary) -> dict[str, object]:
        return {"preserved": list(summary.preserved), "merged": list(summary.merged), "added": list(summary.added)}

    return {
        "program_id": plan.program_id,
        "entities": summary_payload(plan.entities_summary),
        "people": summary_payload(plan.people_summary),
        "teams": summary_payload(plan.teams_summary),
        "conflicts": [
            {
                "kind": conflict.kind,
                "record_kind": conflict.record_kind,
                "key": conflict.key,
                "existing_entity_id": conflict.existing_entity_id,
                "incoming_entity_id": conflict.incoming_entity_id,
                "detail": conflict.detail,
            }
            for conflict in plan.conflicts
        ],
        "diagnostics": list(plan.diagnostics),
        "partial_success": plan.partial_success,
        "transaction_id": plan.transaction_id,
        "generation_id": plan.generation_id,
    }


def _echo_shared_migration_plan(plan: SharedMigrationPlan, *, format: str, heading: str) -> None:
    if format == "json":
        typer.echo(json.dumps(_shared_migration_plan_payload(plan), indent=2, sort_keys=True))
        return
    if format != "human":
        raise typer.BadParameter("--format must be 'human' or 'json'.")
    typer.echo(f"{heading}: program {plan.program_id!r}")
    for name, summary in (
        ("Entities", plan.entities_summary),
        ("People", plan.people_summary),
        ("Teams", plan.teams_summary),
    ):
        typer.echo(f"{name}: preserved={len(summary.preserved)}, merged={len(summary.merged)}, added={len(summary.added)}")
    typer.echo(f"Conflicts quarantined: {len(plan.conflicts)}")
    for conflict in plan.conflicts:
        typer.echo(f"  - {conflict.record_kind}/{conflict.kind}: {conflict.detail}")
    for diagnostic in plan.diagnostics:
        typer.echo(f"Diagnostic: {diagnostic}")


def _entity_id_backfill_plan_payload(plan: EntityIdBackfillPlan) -> dict[str, Any]:
    return {
        "people_backfilled": list(plan.people_backfilled),
        "teams_backfilled": list(plan.teams_backfilled),
        "new_entity_ids": list(plan.new_entity_ids),
        "diagnostics": list(plan.diagnostics),
        "transaction_id": plan.transaction_id,
        "generation_id": plan.generation_id,
        "is_noop": plan.is_noop,
    }


def _echo_entity_id_backfill_plan(plan: EntityIdBackfillPlan, *, format: str, heading: str) -> None:
    if format == "json":
        typer.echo(json.dumps(_entity_id_backfill_plan_payload(plan), indent=2, sort_keys=True))
        return
    if format != "human":
        raise typer.BadParameter("--format must be 'human' or 'json'.")
    typer.echo(heading)
    typer.echo(f"People backfilled: {len(plan.people_backfilled)} ({', '.join(plan.people_backfilled) or 'none'})")
    typer.echo(f"Teams backfilled: {len(plan.teams_backfilled)} ({', '.join(plan.teams_backfilled) or 'none'})")
    for diagnostic in plan.diagnostics:
        typer.echo(f"Diagnostic: {diagnostic}")


@registry_app.command("status")
def kb_registry_status_command(
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """specs/people.md PPL-W1.1: report the current workspace registry identity and generation, if any."""
    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    config = load_registry_config(knowledge_root)
    manifest = load_registry_manifest(knowledge_root)

    if format == "json":
        payload: dict[str, Any] = {
            "bootstrapped": config is not None,
            "workspace_id": config.workspace_id if config else None,
            "customer_boundary_id": config.customer_boundary_id if config else None,
            "write_mode": config.write_mode if config else None,
            "generation_id": manifest.generation_id if manifest else None,
            "committed_at": manifest.committed_at.isoformat() if manifest else None,
        }
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        raise typer.Exit(code=0)

    if config is None or manifest is None:
        typer.echo("No workspace registry identity yet. Run 'vertex kb registry bootstrap --apply --customer-boundary-id <id>' to create one.")
        raise typer.Exit(code=0)
    typer.echo(f"Workspace ID:        {config.workspace_id}")
    typer.echo(f"Customer boundary:   {config.customer_boundary_id}")
    typer.echo(f"Write mode:          {config.write_mode}")
    typer.echo(f"Generation:          {manifest.generation_id}")
    typer.echo(f"Committed at:        {manifest.committed_at.isoformat()}")
    raise typer.Exit(code=0)


@registry_app.command("storage-status")
def kb_registry_storage_status_command(
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """specs/people.md PPL-W1.3: report the shared knowledge/ root's storage-class qualification
    (local/network/unsupported-sync) and whether it is eligible for write_mode: primary. Always
    recomputes live and re-persists registry_capability_status.yaml -- the same check doctor --kb runs."""
    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    qualification = refresh_registry_storage_status(knowledge_root)

    if format == "json":
        typer.echo(json.dumps(qualification.to_payload(), indent=2, sort_keys=True))
        raise typer.Exit(code=0)

    typer.echo(f"Storage class:         {qualification.storage_class}")
    typer.echo(f"Qualified for primary: {qualification.qualified_for_primary}")
    typer.echo(f"Detail:                {qualification.detail}")
    raise typer.Exit(code=0)


@lease_app.command("show")
def kb_registry_lease_show_command(
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """specs/people.md PPL-W1.2: report the current workspace-global registry lease state, if held."""
    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    handle = read_registry_lease_state(knowledge_root=knowledge_root)

    if format == "json":
        payload: dict[str, Any] | None = (
            None
            if handle is None
            else {
                "owner": handle.owner,
                "fencing_token": handle.fencing_token,
                "acquired_at": handle.acquired_at.isoformat(),
                "expires_at": handle.expires_at.isoformat(),
                "mutation_domain": handle.mutation_domain,
                "expired": is_registry_lease_expired(handle),
            }
        )
        typer.echo(json.dumps({"lease": payload}, indent=2, sort_keys=True))
        raise typer.Exit(code=0)

    if handle is None:
        typer.echo("No registry lease is currently held.")
        raise typer.Exit(code=0)
    expired_suffix = " (EXPIRED)" if is_registry_lease_expired(handle) else ""
    typer.echo(f"Owner:          {handle.owner}")
    typer.echo(f"Fencing token:  {handle.fencing_token}")
    typer.echo(f"Acquired at:    {handle.acquired_at.isoformat()}")
    typer.echo(f"Expires at:     {handle.expires_at.isoformat()}{expired_suffix}")
    raise typer.Exit(code=0)


@lease_app.command("release")
def kb_registry_lease_release_command(
    force: bool = typer.Option(False, "--force", help="Required: force-release a stale registry lease."),
    reason: str | None = typer.Option(None, "--reason", help="Required with --force: why this lease is being force-released."),
) -> None:
    """specs/people.md PPL-W1.2 (§6.7): force-release a stale registry lease.
    Requires an authorized directory-steward principal (registry.yaml's directory_steward_principals);
    increments fencing state (never resets it) and appends an audit record."""
    if not force:
        raise typer.BadParameter("This command only supports forced release: pass --force --reason <text>.")
    if not reason:
        raise typer.BadParameter("--reason is required with --force.")

    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    identity = capture_operator_identity("kb-registry-lease-release")
    if not identity.principal:
        raise typer.BadParameter("Could not resolve an authenticated OS/service principal for this operation.")

    try:
        force_release_registry_lease(authorized_principal=identity.principal, reason=reason, knowledge_root=knowledge_root)
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error

    typer.echo(f"Force-released the registry lease. Authorized principal: {identity.principal}. Reason: {reason}")
    raise typer.Exit(code=0)


def _resolve_operator_principal(command_name: str) -> str:
    identity = capture_operator_identity(command_name)
    if not identity.principal:
        raise typer.BadParameter("Could not resolve an authenticated OS/service principal for this operation.")
    return identity.principal


@mode_app.command("status")
def kb_registry_mode_status_command(
    program: str | None = typer.Option(None, "--program", help="Also report this program's mode and shadow status."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """specs/people.md PPL-W1.9: report the effective registry write_mode/flags,
    with environment kill-switch overrides applied and clearly distinguished from the persisted value."""
    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    effective = load_effective_registry_config(knowledge_root)

    if effective is None:
        if format == "json":
            typer.echo(json.dumps({"bootstrapped": False}, indent=2, sort_keys=True))
        else:
            typer.echo("No workspace registry identity yet. Run 'vertex kb registry bootstrap --apply --customer-boundary-id <id>' to create one.")
        raise typer.Exit(code=0)

    shadow_status = program_shadow_status(knowledge_root, program) if program else None
    promotion_status = program_promotion_status(knowledge_root, program) if program else None

    if format == "json":
        payload: dict[str, Any] = {
            "bootstrapped": True,
            "persisted_write_mode": effective.persisted.write_mode,
            "effective_write_mode": effective.effective_write_mode,
            "force_legacy_active": effective.force_legacy_active,
            "persisted_provider_refresh_enabled": effective.persisted.provider_refresh_enabled,
            "effective_provider_refresh_enabled": effective.effective_provider_refresh_enabled,
            "provider_refresh_disabled_by_env": effective.provider_refresh_disabled_by_env,
            "persisted_audience_scopes_enabled": effective.persisted.audience_scopes_enabled,
            "effective_audience_scopes_enabled": effective.effective_audience_scopes_enabled,
            "audience_expansion_disabled_by_env": effective.audience_expansion_disabled_by_env,
            "shared_writes_enabled": effective.persisted.shared_writes_enabled,
            "program_modes": dict(effective.persisted.program_modes),
        }
        if shadow_status is not None:
            payload["program_shadow_status"] = {
                "program_id": shadow_status.program_id,
                "mode": shadow_status.mode,
                "divergence_tracking_available": shadow_status.divergence_tracking_available,
                "note": shadow_status.note,
            }
        if promotion_status is not None:
            payload["program_promotion_status"] = _program_promotion_status_payload(promotion_status)
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        raise typer.Exit(code=0)

    typer.echo(f"Write mode (persisted):        {effective.persisted.write_mode}")
    typer.echo(f"Write mode (effective):        {effective.effective_write_mode}" + (" [FORCED LEGACY by env]" if effective.force_legacy_active else ""))
    typer.echo(f"Provider refresh (effective):  {effective.effective_provider_refresh_enabled}" + (" [disabled by env]" if effective.provider_refresh_disabled_by_env else ""))
    typer.echo(f"Audience scopes (effective):   {effective.effective_audience_scopes_enabled}" + (" [disabled by env]" if effective.audience_expansion_disabled_by_env else ""))
    typer.echo(f"Shared writes enabled:         {effective.persisted.shared_writes_enabled}")
    typer.echo(f"Program modes:                 {dict(effective.persisted.program_modes) or '{}'}")
    if shadow_status is not None:
        typer.echo(f"Program {shadow_status.program_id!r} shadow status: mode={shadow_status.mode}, divergence_tracking_available={shadow_status.divergence_tracking_available}")
        typer.echo(f"  {shadow_status.note}")
    if promotion_status is not None:
        typer.echo(
            f"Program {promotion_status.program_id!r} promotion: "
            f"{promotion_status.clean_cycles}/{promotion_status.required_clean_cycles} clean cycles, "
            f"ready={promotion_status.ready_to_promote}"
        )
        for reason in promotion_status.blocked_reasons:
            typer.echo(f"  Blocked: {reason}")
    raise typer.Exit(code=0)


@mode_app.command("set-write-mode")
def kb_registry_mode_set_write_mode_command(
    write_mode: str = typer.Argument(..., help="legacy | shadow | primary"),
    apply: bool = typer.Option(False, "--apply", help="Actually persist the change. Without this flag, preview only."),
) -> None:
    """specs/people.md PPL-W1.9: flip the workspace write_mode. Promoting to 'primary'
    is gated by PPL-W1.3's storage-class qualification check."""
    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    if not apply:
        typer.echo(f"Dry run: would set workspace write_mode to {write_mode!r}. Re-run with --apply to persist.")
        raise typer.Exit(code=0)
    principal = _resolve_operator_principal("kb-registry-mode-set-write-mode")
    try:
        updated = set_workspace_write_mode(knowledge_root, write_mode, actor=principal)
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(f"Workspace write_mode set to {updated.write_mode!r}.")
    raise typer.Exit(code=0)


@mode_app.command("set-program-mode")
def kb_registry_mode_set_program_mode_command(
    program_id: str = typer.Argument(..., help="Program ID."),
    mode: str = typer.Argument(..., help="legacy | shadow | primary (primary requires the PPL-W2B.6 gate)."),
    apply: bool = typer.Option(False, "--apply", help="Actually persist the change. Without this flag, preview only."),
) -> None:
    """specs/people.md PPL-W1.9: flip one program's mode independently of every other program's."""
    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    if not apply:
        if mode == "primary":
            status = program_promotion_status(knowledge_root, program_id)
            typer.echo(
                f"Preview: promotion for {program_id!r} is "
                f"{'ready' if status.ready_to_promote else 'blocked'} "
                f"({status.clean_cycles}/{status.required_clean_cycles} clean cycles)."
            )
            for reason in status.blocked_reasons:
                typer.echo(f"  Blocked: {reason}")
            typer.echo("Re-run with --apply only after the gate is ready.")
            raise typer.Exit(code=0)
        typer.echo(f"Dry run: would set program {program_id!r}'s mode to {mode!r}. Re-run with --apply to persist.")
        raise typer.Exit(code=0)
    principal = _resolve_operator_principal("kb-registry-mode-set-program-mode")
    try:
        updated = set_program_mode(knowledge_root, program_id, mode, actor=principal)
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(f"Program {program_id!r} mode set to {updated.program_mode(program_id)!r}.")
    raise typer.Exit(code=0)


def _program_promotion_status_payload(status) -> dict[str, object]:
    return {
        "program_id": status.program_id,
        "mode": status.mode,
        "clean_cycles": status.clean_cycles,
        "required_clean_cycles": status.required_clean_cycles,
        "current_generation_id": status.current_generation_id,
        "rollback_restore_drill_generation_id": status.rollback_restore_drill_generation_id,
        "ready_to_promote": status.ready_to_promote,
        "blocked_reasons": list(status.blocked_reasons),
        "last_failure_reason": status.last_failure_reason,
        "last_cycle_consumers": list(status.last_cycle_consumers),
    }


@mode_app.command("promotion-status")
def kb_registry_mode_promotion_status_command(
    program_id: str = typer.Argument(..., help="Program ID."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """Show persisted five-clean-cycle promotion evidence and live blockers."""

    if format not in {"human", "json"}:
        raise typer.BadParameter("--format must be 'human' or 'json'.")
    status = program_promotion_status(get_shared_knowledge_root(PROGRAMS_ROOT), program_id)
    if format == "json":
        typer.echo(json.dumps(_program_promotion_status_payload(status), indent=2, sort_keys=True))
        raise typer.Exit(code=0)
    typer.echo(f"Program:                  {status.program_id}")
    typer.echo(f"Mode:                     {status.mode}")
    typer.echo(f"Clean cycles:             {status.clean_cycles}/{status.required_clean_cycles}")
    typer.echo(f"Registry generation:      {status.current_generation_id or 'none'}")
    typer.echo(f"Rollback/restore drill:   {status.rollback_restore_drill_generation_id or 'missing'}")
    typer.echo(f"Ready to promote:         {status.ready_to_promote}")
    for reason in status.blocked_reasons:
        typer.echo(f"Blocked: {reason}")
    raise typer.Exit(code=0)


@mode_app.command("promote")
def kb_registry_mode_promote_command(
    program_id: str = typer.Argument(..., help="Program ID in shadow mode."),
    apply: bool = typer.Option(False, "--apply", help="Persist the guarded shadow-to-primary transition."),
) -> None:
    """Promote one shadow program only after its persisted five-cycle gate is ready."""

    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    status = program_promotion_status(knowledge_root, program_id)
    if not apply:
        typer.echo(
            f"Preview: promotion for {program_id!r} is "
            f"{'ready' if status.ready_to_promote else 'blocked'} "
            f"({status.clean_cycles}/{status.required_clean_cycles} clean cycles)."
        )
        for reason in status.blocked_reasons:
            typer.echo(f"  Blocked: {reason}")
        typer.echo("Re-run with --apply to persist only when ready.")
        raise typer.Exit(code=0)
    principal = _resolve_operator_principal("kb-registry-mode-promote")
    try:
        updated = set_program_mode(knowledge_root, program_id, "primary", actor=principal)
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(f"Program {program_id!r} promoted to {updated.program_mode(program_id)!r}.")
    raise typer.Exit(code=0)


@mode_app.command("rollback")
def kb_registry_mode_rollback_command(
    program_id: str = typer.Argument(..., help="Primary-mode program to roll back."),
    target: str = typer.Option("shadow", "--target", help="Rollback target: shadow or legacy."),
    apply: bool = typer.Option(False, "--apply", help="Persist the metadata-only rollback."),
) -> None:
    """Roll one primary program back without rewriting shared customer facts."""

    if target not in {"shadow", "legacy"}:
        raise typer.BadParameter("--target must be 'shadow' or 'legacy'.")
    if not apply:
        typer.echo(
            f"Preview: would roll program {program_id!r} back to {target!r}; "
            "no factual registry data would be rewritten. Re-run with --apply to persist."
        )
        raise typer.Exit(code=0)
    principal = _resolve_operator_principal("kb-registry-mode-rollback")
    try:
        updated = rollback_program_mode(
            get_shared_knowledge_root(PROGRAMS_ROOT),
            program_id,
            target_mode=target,
            actor=principal,
        )
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(f"Program {program_id!r} rolled back to {updated.program_mode(program_id)!r}.")
    raise typer.Exit(code=0)


@mode_app.command("record-rollback-drill")
def kb_registry_mode_record_rollback_drill_command(
    program_id: str = typer.Argument(..., help="Program ID whose promotion evidence receives the drill."),
    snapshot: Path = typer.Option(..., "--snapshot", help="Verified registry backup snapshot directory."),
    restore_to: Path = typer.Option(..., "--restore-to", help="Empty directory used only for the restore drill."),
    apply: bool = typer.Option(False, "--apply", help="Run the restore drill and persist verified evidence."),
) -> None:
    """Run a non-live registry restore drill and record its verified generation."""

    snapshot_manifest = snapshot / BACKUP_SNAPSHOT_MANIFEST_NAME
    if not snapshot_manifest.exists():
        raise typer.BadParameter(f"Registry backup snapshot manifest not found: {snapshot_manifest}")
    if restore_to.exists() and any(restore_to.iterdir()):
        raise typer.BadParameter("--restore-to must be absent or an empty directory; the live registry is never a drill target.")
    if not apply:
        typer.echo(
            f"Preview: would restore {snapshot!s} into {restore_to!s}, verify its generation, "
            f"and record rollback evidence for {program_id!r}. Re-run with --apply to execute."
        )
        raise typer.Exit(code=0)
    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    try:
        restore_result = restore_registry_backup_snapshot(snapshot, restore_to)
        state = record_program_rollback_restore_drill(
            knowledge_root,
            program_id,
            generation_id=restore_result.generation_id,
            restore_verified=restore_result.verified,
        )
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(
        f"Recorded verified rollback/restore drill for program {program_id!r}, "
        f"generation {state.rollback_restore_drill_generation_id!r}."
    )
    raise typer.Exit(code=0)


@mode_app.command("set-flag")
def kb_registry_mode_set_flag_command(
    flag_name: str = typer.Argument(..., help="provider_refresh_enabled | shared_writes_enabled | audience_scopes_enabled"),
    value: bool = typer.Argument(..., help="true or false"),
    apply: bool = typer.Option(False, "--apply", help="Actually persist the change. Without this flag, preview only."),
) -> None:
    """specs/people.md PPL-W1.9: flip one workspace-wide registry flag."""
    knowledge_root = get_shared_knowledge_root(PROGRAMS_ROOT)
    if not apply:
        typer.echo(f"Dry run: would set {flag_name!r} to {value}. Re-run with --apply to persist.")
        raise typer.Exit(code=0)
    principal = _resolve_operator_principal("kb-registry-mode-set-flag")
    try:
        updated = set_registry_flag(knowledge_root, flag_name, value, actor=principal)
    except ConfigError as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(f"{flag_name} set to {getattr(updated, flag_name)}.")
    raise typer.Exit(code=0)


@mode_app.command("shadow-parity")
def kb_registry_mode_shadow_parity_command(
    program_id: str = typer.Argument(..., help="Program ID to compute shadow-mode parity for."),
    record: bool = typer.Option(False, "--record", help="Persist the result to knowledge/.state (only computed when the program's effective mode is shadow or primary)."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """specs/people.md PPL-W2A.7 (§6.6): compile the canonical v2 view in parallel
    with the legacy loader and report parity/divergence for one program.
    Without --record, always computes fresh regardless of the program's mode
    (a diagnostic preview); with --record, only computes/persists when the
    program's effective mode is shadow or primary, matching the real gate."""
    if record:
        result = compute_and_record_shadow_parity_if_in_shadow_mode(program_id, programs_root=PROGRAMS_ROOT)
        if result is None:
            typer.echo(f"Program {program_id!r} is not in shadow/primary mode (or the registry isn't bootstrapped yet) -- nothing recorded.")
            raise typer.Exit(code=0)
    else:
        result = compute_shadow_parity(program_id, programs_root=PROGRAMS_ROOT)

    if format == "json":
        typer.echo(json.dumps(result.to_payload(), indent=2, sort_keys=True))
        raise typer.Exit(code=0)

    typer.echo(f"Program:              {result.program_id}")
    typer.echo(f"Zero divergence:      {result.is_zero_divergence}")
    typer.echo(f"People (legacy/v2):   {result.legacy_person_count}/{result.canonical_person_count}")
    typer.echo(f"Teams (legacy/v2):    {result.legacy_team_count}/{result.canonical_team_count}")
    typer.echo(f"Legacy-field diagnostics: {len(result.diagnostics)}")
    if result.divergences:
        typer.echo("Divergences:")
        for divergence in result.divergences[:20]:
            typer.echo(f"  - {divergence.kind}: {divergence.key}")
    raise typer.Exit(code=0)


app.add_typer(profiles_app, name="profiles")
app.add_typer(people_app, name="people")
app.add_typer(teams_app, name="teams")
registry_app.add_typer(lease_app, name="lease")
registry_app.add_typer(mode_app, name="mode")
app.add_typer(registry_app, name="registry")
people_app.add_typer(delegate_app, name="delegate")
