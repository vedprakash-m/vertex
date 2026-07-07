from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, cast

import typer

from src.commands.integration_candidate import _candidate_payload, _parse_candidate_status, _parse_source_ref_kind
from src.commands.integration_core import _bootstrap_discovery_state_impl, _load_program_impl, _store_impl
from src.commands.integration_discovery import (
    _candidate_store,
    _channel_config,
    _channel_exists,
    _discover_channel_bindings,
    _discover_channel_configs,
    _load_workstreams,
    _run_discovery,
)
from src.commands.integration_format import _display_title, _emit_delta_items, _format_optional_datetime, _print_table
from src.commands.integration_intent_select import _next_source_action, _resolve_selected_intent
from src.commands.integration_intent_status import (
    _intent_match_confidence,
    _intent_match_confidence_with_conn,
    _recompute_intent_status_with_conn,
    _update_intent_status_if_needed_with_conn,
)
from src.commands.integration_lifecycle import (
    _accept_candidate_for_intent_impl,
    _clear_candidate_rejection,
    _mutate_intent_lifecycle_impl,
    _reassign_candidate,
    _reject_candidate_impl,
    _restore_intent_lifecycle_impl,
)
from src.commands.integration_registry import _refresh_gather_state_discovery_health, _registry_channels
from src.commands.integration_seed_plan import (
    SeedPlanEntry,
    _collect_seed_plan_entries,
    _render_seed_plan,
    _seed_plan_entry_payload,
    _seed_plan_lookup_hints,
    _seed_plan_ref_kind_group,
    _seed_plan_ref_kind_priority,
)
from src.commands.integration_support import _backup_path, _registry_path, _resolve_backup, _sqlite_copy
from src.core.discovery_service import channel_for_source_ref_kind
from src.core.channel_registry_store import ChannelRegistryStore, ShrinkageGuardError, compute_registry_delta, normalize_discovery_result_provider_instance
from src.core.discovery_intent import SourceCandidate, SourceCandidateStatus, SourceIntent, SourceIntentStatus, SourceRefKind, build_source_candidate_id
from src.core.integration_types import ChannelRegistration, DiscoveredRef, DiscoveryCompleteness, DiscoveryResult, RegistrationBinding, RegistrationStatus, ScopeStatus, ScopeStatusKind
from src.core.config_loader import PROGRAMS_ROOT
from src.core.m365_registry_store import load_m365_registry
from src.core.program_paths import resolve_m365_registry_path_for_read
from src.core.models_v2 import Program
from src.core.integration_types import RunContext
from src.core.source_intent_audit import append_intent_decision_log, intent_decision_payload
from src.core.source_candidate_store import SourceCandidateStore, candidate_evidence_json
from src.core.yaml_utils import load_yaml_mapping


app = typer.Typer(help="Inspect and manage the unified integration registry.")




@app.command("show")
def show_integration_registry(
    program: str = typer.Option(..., "--program", "-p", help="Program id."),
    channel: str | None = typer.Option(None, "--channel", help="Filter to one channel."),
    provider_instance: str | None = typer.Option(None, "--provider-instance", help="Filter to one provider instance."),
    reveal_titles: bool = typer.Option(False, "--reveal-titles", help="Show stored plaintext titles."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", help="Programs root."),
) -> None:
    db_path = _registry_path(program, programs_root)
    if not db_path.exists():
        typer.echo(f"No integration registry entries found for {program}.")
        return
    store = ChannelRegistryStore(db_path, program)
    channels = (channel,) if channel else _registry_channels(store)
    if not channels:
        typer.echo(f"No integration registry entries found for {program}.")
        return

    # Load channel configs to check per-channel ref_title_visible setting.
    try:
        raw_program = load_yaml_mapping(programs_root / program / "program.yaml")
        raw_channels: dict = raw_program.get("channels") or {}
    except (FileNotFoundError, Exception):
        raw_channels = {}

    rows: list[tuple[str, str, str, str, str, str, str, str, str, str, str, str]] = []
    for channel_name in channels:
        ch_cfg = raw_channels.get(channel_name) or {}
        title_visible = bool(ch_cfg.get("ref_title_visible", False)) if isinstance(ch_cfg, dict) else False
        show_title = reveal_titles or title_visible
        registrations = store.all_registrations(channel_name, provider_instance_id=provider_instance)
        for registration in registrations:
            workstreams = ",".join(registration.workstream_ids) if registration.workstream_ids else "unassigned"
            signal_yield = "/".join(str(value) for value in registration.signal_yield_last_3)
            rows.append(
                (
                    registration.channel,
                    registration.provider_instance_id,
                    workstreams,
                    registration.ref_kind,
                    registration.ref_id,
                    _display_title(registration.ref_title, reveal=show_title),
                    registration.status.value,
                    f"{registration.confidence:.2f}",
                    "yes" if registration.pm_confirmed else "no",
                    "yes" if registration.promoted else "no",
                    signal_yield,
                    registration.last_seen_at.date().isoformat(),
                )
            )
    if not rows:
        typer.echo(f"No integration registry entries found for {program}.")
        return
    _print_table(
        (
            "channel",
            "provider_instance",
            "workstream",
            "ref_kind",
            "ref_id",
            "ref_title",
            "status",
            "confidence",
            "pm_confirmed",
            "promoted",
            "signal_yield",
            "last_seen",
        ),
        rows,
    )


@app.command("candidates")
def list_source_candidates(
    program: str = typer.Option(..., "--program", "-p", help="Program id."),
    status: str | None = typer.Option(None, "--status", help="Filter by candidate status."),
    workstream: str | None = typer.Option(None, "--workstream", help="Filter by workstream id."),
    source_type: str | None = typer.Option(None, "--source-type", help="Filter by source type / ref kind."),
    requires_decision: bool = typer.Option(False, "--requires-decision", help="Show only candidates that still need a PM decision."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead of a human table."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", help="Programs root."),
) -> None:
    candidate_store = _candidate_store(program, programs_root)
    _bootstrap_discovery_state(program, programs_root=programs_root, candidate_store=candidate_store)
    parsed_status = _parse_candidate_status(status) if status is not None else None
    parsed_ref_kind = _parse_source_ref_kind(source_type) if source_type is not None else None
    candidates = candidate_store.list_candidates(
        status=parsed_status,
        workstream_id=workstream,
        ref_kind=parsed_ref_kind,
        requires_decision=requires_decision,
    )
    if json_output:
        payload = [
            _candidate_payload(candidate_store, candidate)
            for candidate in candidates
        ]
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    if not candidates:
        typer.echo(f"No source candidates found for {program}.")
        return
    rows: list[tuple[str, ...]] = []
    for candidate in candidates:
        matches = candidate_store.get_intent_matches(candidate.candidate_id)
        intent_labels = []
        for match in matches:
            intent = candidate_store.get_intent(match.intent_id)
            if intent is None:
                continue
            intent_labels.append(f"{intent.workstream_id}:{intent.display_name}")
        rows.append(
            (
                candidate.candidate_id[:8],
                candidate.status.value,
                candidate.ref_kind.value,
                candidate.channel,
                candidate.ref_id,
                candidate.display_name or "",
                f"{candidate.confidence:.2f}",
                candidate.source_provider,
                ", ".join(intent_labels) or "unmatched",
            )
        )
    _print_table(
        ("candidate", "status", "kind", "channel", "ref_id", "display_name", "confidence", "provider", "intents"),
        rows,
    )


@app.command("seed-id")
def seed_source_candidate_id(
    program: str = typer.Option(..., "--program", "-p", help="Program id."),
    intent_id: str = typer.Option(..., "--intent-id", help="Target source intent id."),
    ref_id: str = typer.Option(..., "--ref-id", help="Durable source identifier to seed."),
    pm_alias: str = typer.Option(..., "--pm-alias", help="PM/operator alias authorising the seed."),
    reason: str | None = typer.Option(None, "--reason", help="Optional rationale for the seeded binding."),
    provider_instance: str = typer.Option("default", "--provider-instance", help="Provider instance id to bind."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", help="Programs root."),
) -> None:
    candidate_store = _candidate_store(program, programs_root)
    _bootstrap_discovery_state(program, programs_root=programs_root, candidate_store=candidate_store)
    intent = candidate_store.get_intent(intent_id)
    if intent is None:
        raise typer.BadParameter(f"Unknown source intent '{intent_id}'.")
    program_config = _load_program(program, programs_root)
    channel = _channel_for_ref_kind(intent.ref_kind)
    if not _channel_exists(program_config, channel, programs_root=programs_root):
        raise typer.BadParameter(
            f"Program '{program}' does not have an enabled '{channel}' integration channel for {intent.ref_kind.value}."
        )
    current_time = datetime.now(timezone.utc)
    candidate = SourceCandidate(
        candidate_id=build_source_candidate_id(
            program_id=program,
            channel=channel,
            provider_instance_id=provider_instance,
            ref_kind=intent.ref_kind,
            ref_id=ref_id,
        ),
        program_id=program,
        channel=channel,
        provider_instance_id=provider_instance,
        ref_id=ref_id,
        ref_kind=intent.ref_kind,
        display_name=intent.display_name,
        confidence=1.0,
        source_provider="manual_seed",
        status=SourceCandidateStatus.ACCEPTED,
        evidence_json=candidate_evidence_json(
            {
                "decision_reason": reason,
                "intent_id": intent.intent_id,
                "manual_seed": True,
                "matched_terms": [intent.display_name],
                "observed_signal_count": 0,
            }
        ),
        first_discovered_at=current_time,
        last_seen_at=current_time,
        decided_at=current_time,
        decided_by=pm_alias,
        decision_reason=reason,
        old_status=None,
        decision_version=1,
    )
    updated_candidate, updated_intent, _ = _accept_candidate_for_intent(
        program=program,
        programs_root=programs_root,
        candidate_store=candidate_store,
        intent=intent,
        candidate=candidate,
        pm_alias=pm_alias,
        reason=reason,
        match_confidence=1.0,
        existing_candidate=False,
    )
    _append_intent_decision_log(
        program,
        programs_root=programs_root,
        payload={
            "ts": current_time.isoformat(),
            "intent_id": updated_intent.intent_id,
            "workstream_id": updated_intent.workstream_id,
            "action": "seed_id",
            "pm_alias": pm_alias,
            "ref_id": updated_candidate.ref_id,
            "ref_kind": updated_intent.ref_kind.value,
            "candidate_id": updated_candidate.candidate_id,
            "old_status": intent.status.value,
            "new_status": updated_intent.status.value,
            "reason": reason,
        },
    )
    typer.echo(
        f"Seeded {updated_intent.ref_kind.value} intent {updated_intent.intent_id} with {ref_id} on channel '{channel}'."
    )


@app.command("seed-plan")
def plan_source_id_seeding(
    program: str = typer.Option(..., "--program", "-p", help="Program id."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON instead of a human checklist."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", help="Programs root."),
) -> None:
    candidate_store = _candidate_store(program, programs_root)
    _bootstrap_discovery_state(program, programs_root=programs_root, candidate_store=candidate_store)
    entries = _collect_seed_plan_entries(program, candidate_store=candidate_store)
    if json_output:
        typer.echo(json.dumps([_seed_plan_entry_payload(entry) for entry in entries], indent=2, sort_keys=True))
        return
    typer.echo(_render_seed_plan(entries, program=program))


@app.command("candidate-accept")
def accept_source_candidate(
    candidate_id: str = typer.Argument(..., help="Candidate id to accept."),
    program: str = typer.Option(..., "--program", "-p", help="Program id."),
    pm_alias: str = typer.Option(..., "--pm-alias", help="PM/operator alias authorising the decision."),
    intent_id: str | None = typer.Option(None, "--intent-id", help="Explicit intent id when a candidate matches multiple intents."),
    reason: str | None = typer.Option(None, "--reason", help="Optional rationale for the accepted binding."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", help="Programs root."),
) -> None:
    candidate_store = _candidate_store(program, programs_root)
    _bootstrap_discovery_state(program, programs_root=programs_root, candidate_store=candidate_store)
    candidate = candidate_store.get_candidate(candidate_id)
    if candidate is None:
        raise typer.BadParameter(f"Unknown source candidate '{candidate_id}'.")
    if candidate.status == SourceCandidateStatus.ACCEPTED:
        raise typer.BadParameter(f"Candidate '{candidate_id}' is already accepted.")
    selected_intent, stale_intents = _resolve_selected_intent(
        candidate_store,
        candidate,
        intent_id=intent_id,
    )
    if selected_intent.status in {
        SourceIntentStatus.SUPPRESSED,
        SourceIntentStatus.RETIRED,
        SourceIntentStatus.SUPERSEDED,
    }:
        raise typer.BadParameter(
            f"Intent '{selected_intent.intent_id}' is {selected_intent.status.value}; clear or reopen it before accepting a candidate."
        )
    updated_candidate, updated_intent, stale_updates = _accept_candidate_for_intent(
        program=program,
        programs_root=programs_root,
        candidate_store=candidate_store,
        intent=selected_intent,
        candidate=candidate,
        pm_alias=pm_alias,
        reason=reason,
        match_confidence=_intent_match_confidence(candidate_store, candidate.candidate_id, selected_intent.intent_id),
        existing_candidate=True,
        unlinked_intent_ids=tuple(intent.intent_id for intent in stale_intents),
    )
    _append_intent_decision_log(
        program,
        programs_root=programs_root,
        payload=_intent_decision_payload(
            ts=updated_candidate.decided_at or datetime.now(timezone.utc),
            intent=selected_intent,
            action="candidate_accept_resolved_intent",
            pm_alias=pm_alias,
            old_status=selected_intent.status.value,
            new_status=updated_intent.status.value,
            reason=reason,
            candidate_id=updated_candidate.candidate_id,
            ref_id=updated_candidate.ref_id,
        ),
    )
    for old_intent, new_intent in stale_updates:
        if old_intent.status == new_intent.status:
            continue
        _append_intent_decision_log(
            program,
            programs_root=programs_root,
            payload=_intent_decision_payload(
                ts=updated_candidate.decided_at or datetime.now(timezone.utc),
                intent=old_intent,
                action="candidate_accept_unlinked_intent",
                pm_alias=pm_alias,
                old_status=old_intent.status.value,
                new_status=new_intent.status.value,
                reason=reason,
                candidate_id=updated_candidate.candidate_id,
                ref_id=updated_candidate.ref_id,
            ),
        )
    typer.echo(
        f"Accepted candidate {updated_candidate.candidate_id[:8]} for intent {updated_intent.intent_id} and activated {updated_candidate.ref_kind.value} {updated_candidate.ref_id}."
    )


@app.command("candidate-reject")
def reject_source_candidate(
    candidate_id: str = typer.Argument(..., help="Candidate id to reject."),
    program: str = typer.Option(..., "--program", "-p", help="Program id."),
    pm_alias: str = typer.Option(..., "--pm-alias", help="PM/operator alias authorising the decision."),
    reason: str = typer.Option(..., "--reason", help="Why this candidate should be rejected."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", help="Programs root."),
) -> None:
    candidate_store = _candidate_store(program, programs_root)
    _bootstrap_discovery_state(program, programs_root=programs_root, candidate_store=candidate_store)
    candidate = candidate_store.get_candidate(candidate_id)
    if candidate is None:
        raise typer.BadParameter(f"Unknown source candidate '{candidate_id}'.")
    updated_candidate, intent_updates, registration_suppressed = _reject_candidate(
        program=program,
        programs_root=programs_root,
        candidate_store=candidate_store,
        candidate=candidate,
        pm_alias=pm_alias,
        reason=reason,
    )
    for old_intent, new_intent in intent_updates:
        if old_intent.status == new_intent.status:
            continue
        _append_intent_decision_log(
            program,
            programs_root=programs_root,
            payload=_intent_decision_payload(
                ts=updated_candidate.decided_at or datetime.now(timezone.utc),
                intent=old_intent,
                action="candidate_reject_reverted_intent",
                pm_alias=pm_alias,
                old_status=old_intent.status.value,
                new_status=new_intent.status.value,
                reason=reason,
                candidate_id=updated_candidate.candidate_id,
                ref_id=updated_candidate.ref_id,
            ),
        )
    status_note = "suppressed existing UIL binding" if registration_suppressed else "no UIL binding to suppress"
    typer.echo(f"Rejected candidate {updated_candidate.candidate_id[:8]} ({status_note}).")


@app.command("candidate-clear-rejection")
def clear_candidate_rejection(
    candidate_id: str = typer.Argument(..., help="Candidate id whose rejection should be cleared."),
    program: str = typer.Option(..., "--program", "-p", help="Program id."),
    pm_alias: str = typer.Option(..., "--pm-alias", help="PM/operator alias authorising the decision."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", help="Programs root."),
) -> None:
    candidate_store = _candidate_store(program, programs_root)
    _bootstrap_discovery_state(program, programs_root=programs_root, candidate_store=candidate_store)
    candidate = candidate_store.get_candidate(candidate_id)
    if candidate is None:
        raise typer.BadParameter(f"Unknown source candidate '{candidate_id}'.")
    if candidate.status != SourceCandidateStatus.REJECTED:
        raise typer.BadParameter(f"Candidate '{candidate_id}' is not rejected.")
    updated_candidate, intent_updates = _clear_candidate_rejection(
        program=program,
        candidate_store=candidate_store,
        candidate=candidate,
        pm_alias=pm_alias,
    )
    for old_intent, new_intent in intent_updates:
        if old_intent.status == new_intent.status:
            continue
        _append_intent_decision_log(
            program,
            programs_root=programs_root,
            payload=_intent_decision_payload(
                ts=updated_candidate.decided_at or datetime.now(timezone.utc),
                intent=old_intent,
                action="candidate_clear_rejection_recomputed_intent",
                pm_alias=pm_alias,
                old_status=old_intent.status.value,
                new_status=new_intent.status.value,
                reason=None,
                candidate_id=updated_candidate.candidate_id,
                ref_id=updated_candidate.ref_id,
            ),
        )
    typer.echo(f"Cleared rejection for candidate {updated_candidate.candidate_id[:8]}; candidate is pending review again.")


@app.command("candidate-reassign")
def reassign_source_candidate(
    candidate_id: str = typer.Argument(..., help="Candidate id whose workstream mapping should change."),
    program: str = typer.Option(..., "--program", "-p", help="Program id."),
    workstream: str = typer.Option(..., "--workstream", help="Target workstream id."),
    pm_alias: str = typer.Option(..., "--pm-alias", help="PM/operator alias authorising the reassignment."),
    from_intent_id: str | None = typer.Option(None, "--from-intent-id", help="Explicit current intent id when a candidate matches multiple intents."),
    reason: str | None = typer.Option(None, "--reason", help="Optional rationale for the reassignment."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", help="Programs root."),
) -> None:
    candidate_store = _candidate_store(program, programs_root)
    _bootstrap_discovery_state(program, programs_root=programs_root, candidate_store=candidate_store)
    candidate = candidate_store.get_candidate(candidate_id)
    if candidate is None:
        raise typer.BadParameter(f"Unknown source candidate '{candidate_id}'.")
    if candidate.status == SourceCandidateStatus.ACCEPTED:
        raise typer.BadParameter("Accepted candidates cannot be reassigned directly; reject them first, then accept the correct intent.")
    if candidate.status != SourceCandidateStatus.PENDING:
        raise typer.BadParameter("Only pending candidates can be reassigned.")
    (old_intent_before, old_intent_after), (new_intent_before, new_intent_after) = _reassign_candidate(
        program=program,
        candidate_store=candidate_store,
        candidate=candidate,
        workstream_id=workstream,
        pm_alias=pm_alias,
        reason=reason,
        from_intent_id=from_intent_id,
    )
    if old_intent_before.status != old_intent_after.status:
        _append_intent_decision_log(
            program,
            programs_root=programs_root,
            payload=_intent_decision_payload(
                ts=datetime.now(timezone.utc),
                intent=old_intent_before,
                action="candidate_reassign_unlinked_intent",
                pm_alias=pm_alias,
                old_status=old_intent_before.status.value,
                new_status=old_intent_after.status.value,
                reason=reason,
                candidate_id=candidate.candidate_id,
                ref_id=candidate.ref_id,
            ),
        )
    if new_intent_before.status != new_intent_after.status:
        _append_intent_decision_log(
            program,
            programs_root=programs_root,
            payload=_intent_decision_payload(
                ts=datetime.now(timezone.utc),
                intent=new_intent_before,
                action="candidate_reassign_linked_intent",
                pm_alias=pm_alias,
                old_status=new_intent_before.status.value,
                new_status=new_intent_after.status.value,
                reason=reason,
                candidate_id=candidate.candidate_id,
                ref_id=candidate.ref_id,
            ),
        )
    typer.echo(f"Reassigned candidate {candidate.candidate_id[:8]} to workstream {new_intent_after.workstream_id}.")


@app.command("intent-suppress")
def suppress_source_intent(
    program: str = typer.Option(..., "--program", "-p", help="Program id."),
    workstream: str = typer.Option(..., "--workstream", help="Workstream id."),
    kind: str = typer.Option(..., "--kind", help="Source kind."),
    name: str = typer.Option(..., "--name", help="Intent display name."),
    pm_alias: str = typer.Option(..., "--pm-alias", help="PM/operator alias authorising the decision."),
    reason: str = typer.Option(..., "--reason", help="Why this source should be suppressed."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", help="Programs root."),
) -> None:
    _mutate_intent_lifecycle(
        program=program,
        programs_root=programs_root,
        workstream_id=workstream,
        ref_kind=_parse_source_ref_kind(kind),
        display_name=name,
        pm_alias=pm_alias,
        reason=reason,
        target_status=SourceIntentStatus.SUPPRESSED,
    )
    typer.echo(f"Suppressed source intent '{name}' for workstream {workstream}.")


@app.command("intent-retire")
def retire_source_intent(
    program: str = typer.Option(..., "--program", "-p", help="Program id."),
    workstream: str = typer.Option(..., "--workstream", help="Workstream id."),
    kind: str = typer.Option(..., "--kind", help="Source kind."),
    name: str = typer.Option(..., "--name", help="Intent display name."),
    pm_alias: str = typer.Option(..., "--pm-alias", help="PM/operator alias authorising the decision."),
    reason: str = typer.Option(..., "--reason", help="Why this source should be retired."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", help="Programs root."),
) -> None:
    _mutate_intent_lifecycle(
        program=program,
        programs_root=programs_root,
        workstream_id=workstream,
        ref_kind=_parse_source_ref_kind(kind),
        display_name=name,
        pm_alias=pm_alias,
        reason=reason,
        target_status=SourceIntentStatus.RETIRED,
    )
    typer.echo(f"Retired source intent '{name}' for workstream {workstream}.")


@app.command("intent-clear-suppression")
def clear_source_intent_suppression(
    program: str = typer.Option(..., "--program", "-p", help="Program id."),
    workstream: str = typer.Option(..., "--workstream", help="Workstream id."),
    kind: str = typer.Option(..., "--kind", help="Source kind."),
    name: str = typer.Option(..., "--name", help="Intent display name."),
    pm_alias: str = typer.Option(..., "--pm-alias", help="PM/operator alias authorising the decision."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", help="Programs root."),
) -> None:
    _restore_intent_lifecycle(
        program=program,
        programs_root=programs_root,
        workstream_id=workstream,
        ref_kind=_parse_source_ref_kind(kind),
        display_name=name,
        pm_alias=pm_alias,
        expected_status=SourceIntentStatus.SUPPRESSED,
        action="intent_clear_suppression",
    )
    typer.echo(f"Cleared suppression for source intent '{name}' in workstream {workstream}.")


@app.command("intent-reopen")
def reopen_source_intent(
    program: str = typer.Option(..., "--program", "-p", help="Program id."),
    workstream: str = typer.Option(..., "--workstream", help="Workstream id."),
    kind: str = typer.Option(..., "--kind", help="Source kind."),
    name: str = typer.Option(..., "--name", help="Intent display name."),
    pm_alias: str = typer.Option(..., "--pm-alias", help="PM/operator alias authorising the decision."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", help="Programs root."),
) -> None:
    _restore_intent_lifecycle(
        program=program,
        programs_root=programs_root,
        workstream_id=workstream,
        ref_kind=_parse_source_ref_kind(kind),
        display_name=name,
        pm_alias=pm_alias,
        expected_status=SourceIntentStatus.RETIRED,
        action="intent_reopen",
    )
    typer.echo(f"Reopened source intent '{name}' in workstream {workstream}.")


@app.command("explain-source")
def explain_source(
    program: str = typer.Option(..., "--program", "-p", help="Program id."),
    intent_id: str | None = typer.Option(None, "--intent-id", help="Explain one source intent."),
    ref_id: str | None = typer.Option(None, "--ref-id", help="Explain the candidate/source carrying this durable ref id."),
    ref_kind: str | None = typer.Option(None, "--ref-kind", help="Ref kind used with --ref-id."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", help="Programs root."),
) -> None:
    if (intent_id is None) == (ref_id is None):
        raise typer.BadParameter("Pass exactly one of --intent-id or --ref-id.")
    candidate_store = _candidate_store(program, programs_root)
    _bootstrap_discovery_state(program, programs_root=programs_root, candidate_store=candidate_store)

    intent: SourceIntent | None = None
    candidate: SourceCandidate | None = None
    if intent_id is not None:
        intent = candidate_store.get_intent(intent_id)
        if intent is None:
            raise typer.BadParameter(f"Unknown source intent '{intent_id}'.")
    else:
        if ref_kind is None:
            raise typer.BadParameter("--ref-kind is required with --ref-id.")
        parsed_ref_kind = _parse_source_ref_kind(ref_kind)
        candidate = candidate_store.get_candidate_by_ref(ref_id=ref_id or "", ref_kind=parsed_ref_kind)
        if candidate is None:
            raise typer.BadParameter(f"No source candidate found for ref_kind={parsed_ref_kind.value} ref_id={ref_id}.")
        matches = candidate_store.get_intent_matches(candidate.candidate_id)
        if matches:
            intent = candidate_store.get_intent(matches[0].intent_id)

    if candidate is None and intent is not None:
        candidates = candidate_store.list_candidates_for_intent(intent.intent_id)
    elif candidate is not None:
        candidates = (candidate,)
    else:
        candidates = ()
    attempts = candidate_store.get_attempts(intent.intent_id) if intent is not None else ()
    derived_status = candidate_store.derive_intent_state(intent.intent_id, as_of=datetime.now(timezone.utc)) if intent is not None else "unknown"

    typer.echo("Intent")
    typer.echo("------")
    if intent is None:
        typer.echo("No matched intent.")
    else:
        typer.echo(f"id: {intent.intent_id}")
        typer.echo(f"workstream: {intent.workstream_id}")
        typer.echo(f"kind: {intent.ref_kind.value}")
        typer.echo(f"display_name: {intent.display_name}")
        typer.echo(f"stored_status: {intent.status.value}")
        typer.echo(f"derived_status: {derived_status}")
        typer.echo(f"updated_by: {intent.updated_by or ''}")
        typer.echo(f"updated_at: {intent.updated_at.isoformat()}")
        typer.echo(f"decision_version: {intent.decision_version}")

    typer.echo("")
    typer.echo("Candidates")
    typer.echo("----------")
    if not candidates:
        typer.echo("No candidates recorded.")
    else:
        candidate_rows: list[tuple[str, ...]] = []
        for item in candidates:
            candidate_rows.append(
                (
                    item.candidate_id[:8],
                    item.status.value,
                    item.ref_id,
                    item.display_name or "",
                    f"{item.confidence:.2f}",
                    item.source_provider,
                    item.last_seen_at.isoformat(),
                )
            )
        _print_table(("candidate", "status", "ref_id", "display_name", "confidence", "provider", "last_seen"), candidate_rows)

    typer.echo("")
    typer.echo("Attempts")
    typer.echo("--------")
    if not attempts:
        typer.echo("No discovery attempts recorded.")
    else:
        attempt_rows: list[tuple[str, ...]] = []
        for attempt in attempts:
            attempt_rows.append(
                (
                    attempt.outcome.value,
                    attempt.source_provider,
                    str(attempt.result_count),
                    attempt.attempted_at.isoformat(),
                    attempt.expires_at.isoformat() if attempt.expires_at is not None else "",
                    attempt.reason or "",
                )
            )
        _print_table(("outcome", "provider", "results", "attempted_at", "expires_at", "reason"), attempt_rows)

    typer.echo("")
    typer.echo("Next action")
    typer.echo("-----------")
    typer.echo(_next_source_action(intent=intent, derived_status=derived_status, candidates=candidates, attempts=attempts))


@app.command("diff")
def show_integration_diff(
    program: str = typer.Option(..., "--program", "-p", help="Program id."),
    channel: str = typer.Option(..., "--channel", help="Channel to inspect."),
    provider_instance: str | None = typer.Option(None, "--provider-instance", help="Filter to one provider instance."),
    history: int = typer.Option(1, "--history", min=1, max=20, help="Number of deltas to show."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", help="Programs root."),
) -> None:
    db_path = _registry_path(program, programs_root)
    if not db_path.exists():
        typer.echo(f"No integration deltas found for {program}/{channel}.")
        return
    deltas = ChannelRegistryStore(db_path, program).recent_deltas(channel, limit=history, provider_instance_id=provider_instance)
    if not deltas:
        typer.echo(f"No integration deltas found for {program}/{channel}.")
        return
    for delta in deltas:
        instance_label = f" instance={provider_instance}" if provider_instance is not None else ""
        typer.echo(f"{delta.computed_at.isoformat()} {channel}{instance_label}: {delta.summary} shrinkage={delta.shrinkage_pct:.0%}")
        _emit_delta_items("added", delta.added)
        _emit_delta_items("removed", delta.removed)
        _emit_delta_items("updated", delta.updated)


@app.command("retire")
def retire_integration_registration(
    program: str = typer.Option(..., "--program", "-p", help="Program id."),
    channel: str = typer.Option(..., "--channel", help="Channel name."),
    ref_id: str = typer.Option(..., "--ref-id", help="External reference id."),
    ref_kind: str = typer.Option("work_item", "--ref-kind", help="Reference kind."),
    provider_instance: str | None = typer.Option(None, "--provider-instance", help="Target one provider instance."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", help="Programs root."),
) -> None:
    _store(program, programs_root).retire(channel, ref_id, ref_kind, provider_instance_id=provider_instance)
    instance_label = f" [instance={provider_instance}]" if provider_instance is not None else ""
    typer.echo(f"Retired {channel}:{ref_kind}:{ref_id}{instance_label}.")


@app.command("suppress")
def suppress_integration_registration(
    program: str = typer.Option(..., "--program", "-p", help="Program id."),
    channel: str = typer.Option(..., "--channel", help="Channel name."),
    ref_id: str = typer.Option(..., "--ref-id", help="External reference id."),
    ref_kind: str = typer.Option("work_item", "--ref-kind", help="Reference kind."),
    provider_instance: str | None = typer.Option(None, "--provider-instance", help="Target one provider instance."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", help="Programs root."),
) -> None:
    _store(program, programs_root).suppress(channel, ref_id, ref_kind, provider_instance_id=provider_instance)
    instance_label = f" [instance={provider_instance}]" if provider_instance is not None else ""
    typer.echo(f"Suppressed {channel}:{ref_kind}:{ref_id}{instance_label}.")


@app.command("confirm")
def confirm_integration_registration(
    program: str = typer.Option(..., "--program", "-p", help="Program id."),
    channel: str = typer.Option(..., "--channel", help="Channel name."),
    ref_id: str = typer.Option(..., "--ref-id", help="External reference id."),
    ref_kind: str = typer.Option("work_item", "--ref-kind", help="Reference kind."),
    provider_instance: str | None = typer.Option(None, "--provider-instance", help="Target one provider instance."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", help="Programs root."),
) -> None:
    _store(program, programs_root).confirm(channel, ref_id, ref_kind, provider_instance_id=provider_instance)
    instance_label = f" [instance={provider_instance}]" if provider_instance is not None else ""
    typer.echo(f"Confirmed {channel}:{ref_kind}:{ref_id}{instance_label}.")


@app.command("promote")
def promote_integration_registration(
    program: str = typer.Option(..., "--program", "-p", help="Program id."),
    channel: str = typer.Option(..., "--channel", help="Channel name."),
    ref_id: str = typer.Option(..., "--ref-id", help="External reference id."),
    ref_kind: str = typer.Option("work_item", "--ref-kind", help="Reference kind."),
    provider_instance: str | None = typer.Option(None, "--provider-instance", help="Target one provider instance."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", help="Programs root."),
) -> None:
    _store(program, programs_root).promote(channel, ref_id, ref_kind, provider_instance_id=provider_instance)
    instance_label = f" [instance={provider_instance}]" if provider_instance is not None else ""
    typer.echo(f"Promoted {channel}:{ref_kind}:{ref_id}{instance_label}.")


@app.command("signal-yield")
def update_integration_signal_yield(
    program: str = typer.Option(..., "--program", "-p", help="Program id."),
    channel: str = typer.Option(..., "--channel", help="Channel name."),
    ref_id: str = typer.Option(..., "--ref-id", help="External reference id."),
    ref_kind: str = typer.Option("work_item", "--ref-kind", help="Reference kind."),
    count: int = typer.Option(..., "--count", min=0, help="Newest signal-yield count to record."),
    provider_instance: str | None = typer.Option(None, "--provider-instance", help="Target one provider instance."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", help="Programs root."),
) -> None:
    _store(program, programs_root).update_signal_yield(
        channel,
        ref_id,
        ref_kind,
        count,
        provider_instance_id=provider_instance,
    )
    instance_label = f" [instance={provider_instance}]" if provider_instance is not None else ""
    typer.echo(f"Recorded signal yield {count} for {channel}:{ref_kind}:{ref_id}{instance_label}.")


@app.command("reassign")
def reassign_integration_workstream(
    program: str = typer.Option(..., "--program", "-p", help="Program id."),
    channel: str = typer.Option(..., "--channel", help="Channel name."),
    ref_id: str = typer.Option(..., "--ref-id", help="External reference id."),
    ref_kind: str = typer.Option("work_item", "--ref-kind", help="Reference kind."),
    workstream: str = typer.Option(..., "--workstream", help="New workstream id."),
    old_workstream: str | None = typer.Option(None, "--old-workstream", help="Only reassign bindings from this workstream."),
    provider_instance: str | None = typer.Option(None, "--provider-instance", help="Target one provider instance."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", help="Programs root."),
) -> None:
    """Reassign workstream attribution for a UIL channel registration."""
    migrated = _store(program, programs_root).reassign_workstream(
        channel,
        ref_id,
        ref_kind,
        workstream,
        old_workstream_id=old_workstream,
        provider_instance_id=provider_instance,
    )
    if migrated == 0:
        typer.echo(f"No bindings updated for {channel}:{ref_kind}:{ref_id} (already correct or not found).")
    else:
        typer.echo(f"Reassigned {migrated} binding(s) for {channel}:{ref_kind}:{ref_id} -> {workstream}.")


@app.command("ref-id")
def reassign_ref_id(
    program: str = typer.Option(..., "--program", "-p", help="Program id."),
    channel: str = typer.Option(..., "--channel", help="Channel name."),
    old_ref_id: str = typer.Option(..., "--old-ref-id", help="Current external reference id."),
    new_ref_id: str = typer.Option(..., "--new-ref-id", help="New external reference id (e.g. new Teams thread id)."),
    ref_kind: str = typer.Option("teams_message", "--ref-kind", help="Reference kind (default: teams_message)."),
    pm_alias: str = typer.Option(..., "--pm", help="PM alias authorising this change."),
    reason: str | None = typer.Option(None, "--reason", help="Optional reason for the ref-id change."),
    provider_instance: str | None = typer.Option(None, "--provider-instance", help="Target one provider instance."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", help="Programs root."),
) -> None:
    """Migrate a UIL registration to a new ref_id (e.g. after a Teams thread rotation).

    This is the UIL equivalent of 'vertex registry set-id'.  Use when a channel artifact
    changes its identity (e.g. a Teams meeting series moves to a new thread).  All bindings
    and governance state are carried over to the new ref_id in a single atomic transaction.
    Raises an error if the old ref_id does not exist or the new ref_id is already registered.
    """
    from src.core.channel_registry_store import RegistryMetadataError

    store = _store(program, programs_root)
    try:
        migrated = store.reassign_ref_id(
            channel,
            old_ref_id,
            new_ref_id,
            ref_kind,
            pm_alias=pm_alias,
            reason=reason,
            provider_instance_id=provider_instance,
        )
    except RegistryMetadataError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(1) from exc
    if migrated == 0:
        typer.echo("No change: old_ref_id and new_ref_id are identical.")
    else:
        typer.echo(
            f"Migrated {channel}:{ref_kind}:{old_ref_id} -> {new_ref_id}  "
            f"({migrated - 1} binding(s) carried over)."
        )


@app.command("discover")
def discover_integration_registry(
    program: str = typer.Option(..., "--program", "-p", help="Program id."),
    channel: str | None = typer.Option(None, "--channel", help="Channel to discover."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", help="Programs root."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute without writes."),
    force: bool = typer.Option(False, "--force", help="Run even when discovery is fresh."),
    accept_shrinkage: bool = typer.Option(False, "--accept-shrinkage", help="Accept guarded shrinkage."),
) -> None:
    program_config = _load_program(program, programs_root)
    registry_path = _registry_path(program, programs_root)
    store = _store(program, programs_root, ensure_schema=not dry_run) if (not dry_run or registry_path.exists()) else None
    selected_configs = _discover_channel_configs(program_config, channel, programs_root=programs_root)
    if not selected_configs:
        if channel is not None and _channel_exists(program_config, channel, programs_root=programs_root):
            typer.echo(f"UIL channel '{channel}' is configured but disabled for {program}.")
        else:
            typer.echo(f"No enabled UIL channels configured for {program}.")
        return
    selected_bindings = _discover_channel_bindings(program_config, selected_configs, programs_root=programs_root)
    run_ctx = RunContext(dry_run=dry_run, force_discovery=force, accept_shrinkage=accept_shrinkage)

    backed_up = False
    ran_any = False
    for binding in selected_bindings:
        channel_config = binding.config
        selected_channel = channel_config.channel
        provider_instance_id = str((channel_config.extra or {}).get("instance_id") or "default")
        if not force and store is not None and not store.is_discovery_stale(
            selected_channel,
            channel_config.discovery_threshold_hours,
            provider_instance_id=provider_instance_id,
        ):
            typer.echo(f"{selected_channel.upper()} discovery is fresh; use --force to run anyway.")
            continue
        result = _run_discovery(
            program=program,
            binding=binding,
            store=store,
            provider_instance_id=provider_instance_id,
            run_ctx=run_ctx,
        )
        ran_any = True
        if dry_run:
            previous_refs = (
                store.load_discovered_refs(selected_channel, provider_instance_id=provider_instance_id)
                if store is not None
                else ()
            )
            delta = compute_registry_delta(previous_refs, result)
            typer.echo(f"{selected_channel.upper()} discovery dry-run: {delta.summary}")
            continue
        if store is None:
            store = _store(program, programs_root)
        preview_delta = compute_registry_delta(
            store.load_discovered_refs(selected_channel, provider_instance_id=provider_instance_id),
            result,
        )
        if (
            accept_shrinkage
            and not backed_up
            and registry_path.exists()
            and preview_delta.is_shrinkage_guarded()
        ):
            backup_path = _backup_path(program, programs_root, prefix="channel_registry-pre-shrinkage")
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            _sqlite_copy(registry_path, backup_path)
            typer.echo(f"Pre-shrinkage backup: {backup_path}")
            backed_up = True
        try:
            delta = store.apply_discovery_result(
                result,
                ttl_days=channel_config.ttl_days,
                accept_shrinkage=accept_shrinkage,
            )
        except ShrinkageGuardError as error:
            for scope_id, status in result.scope_statuses.items():
                store.record_scope_status(
                    selected_channel,
                    scope_id,
                    status,
                    provider_instance_id=provider_instance_id,
                    recorded_at=result.computed_at,
                )
            typer.echo(
                f"{selected_channel.upper()} discovery shrinkage guard: {error.computed_delta.summary} shrinkage={error.shrinkage_pct:.0%}"
            )
            typer.echo("Re-run with --accept-shrinkage to apply the delta.")
            raise typer.Exit(code=2) from error
        _refresh_gather_state_discovery_health(
            program,
            programs_root=programs_root,
            channel=selected_channel,
            provider_instance_id=provider_instance_id,
            store=store,
        )
        typer.echo(f"{selected_channel.upper()} discovery: {delta.summary}")
    if not ran_any:
        typer.echo("All configured UIL channels are fresh.")


@app.command("migrate")
def migrate_integration_registry(
    program: str = typer.Option(..., "--program", "-p", help="Program id."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would be migrated without writing."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", help="Programs root."),
) -> None:
    from datetime import date, time

    if not dry_run:
        _store(program, programs_root).ensure_schema()

    m365_path = resolve_m365_registry_path_for_read(program, programs_root=programs_root)
    if not m365_path.exists():
        typer.echo(f"No m365_registry.yaml found for {program}. Schema initialized only.")
        if not dry_run:
            typer.echo(f"Integration registry schema is current for {program}.")
        return

    registry = load_m365_registry(program, programs_root)
    now = datetime.now(timezone.utc)

    def _to_dt(d: date | str | None) -> datetime:
        if d is None:
            return now
        if isinstance(d, datetime):
            return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d
        if isinstance(d, date):
            return datetime.combine(d, time.min, tzinfo=timezone.utc)
        try:
            return datetime.fromisoformat(str(d)).replace(tzinfo=timezone.utc)
        except ValueError:
            return now

    def _do_feedback_migration(store: "ChannelRegistryStore") -> None:  # type: ignore[name-defined]
        """Migrate routing feedback events from JSONL file into the registry store."""
        from src.core.m365_registry_store import get_m365_routing_feedback_path
        feedback_path = get_m365_routing_feedback_path(program, programs_root)
        if not feedback_path.exists():
            return
        import json as _json
        feedback_migrated = 0
        with open(feedback_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                artifact_id = ev.get("artifact_id", "")
                artifact = next((item for item in registry.artifacts if item.artifact_id == artifact_id), None)
                series_id = ev.get("series_id") or (artifact_id if "meeting" in ev.get("action", "") else None)
                thread_id = ev.get("thread_id")
                if artifact is not None and artifact.artifact_type == "email_thread":
                    channel = "email"
                    ref_id = thread_id or artifact_id
                    ref_kind = "email_thread"
                else:
                    channel = "teams"
                    ref_id = series_id or thread_id or artifact_id
                    ref_kind = "meeting_series" if series_id else ("teams_chat" if thread_id else "unknown")
                try:
                    created_at_str = ev.get("ts") or ev.get("created_at") or ""
                    created_at = datetime.fromisoformat(str(created_at_str).rstrip("Z")).replace(tzinfo=timezone.utc) if created_at_str else now
                except ValueError:
                    created_at = now
                topics_raw = ev.get("topics")
                detail_json = _json.dumps({"topics": topics_raw}) if topics_raw else None
                store.write_feedback_event(
                    channel,
                    ref_id,
                    ref_kind,
                    action=str(ev.get("action", "unknown")),
                    pm_alias=str(ev.get("pm_alias", "")),
                    reason=ev.get("reason"),
                    workstream_id=ev.get("workstream_id"),
                    prior_workstream_id=ev.get("prior_workstream_id"),
                    series_id=series_id,
                    thread_id=thread_id,
                    new_artifact_id=ev.get("new_artifact_id"),
                    detail_json=detail_json,
                    created_at=created_at,
                )
                feedback_migrated += 1
        if feedback_migrated:
            typer.echo(f"Migrated {feedback_migrated} routing feedback events.")

    if not registry.artifacts:
        typer.echo(f"m365_registry.yaml for {program} has no artifacts. Schema initialized only.")
        if not dry_run:
            store = _store(program, programs_root)
            _do_feedback_migration(store)
            typer.echo(f"Integration registry schema is current for {program}.")
        return

    discovered_refs_by_channel: dict[str, list[DiscoveredRef]] = {"teams": [], "email": []}
    for artifact in registry.artifacts:
        if artifact.artifact_type == "meeting_series":
            channel = "teams"
            ref_id = artifact.series_id or artifact.artifact_id
            ref_kind = "meeting_series"
        elif artifact.artifact_type == "email_thread":
            channel = "email"
            ref_id = artifact.thread_id or artifact.artifact_id
            ref_kind = "email_thread"
        else:
            channel = "teams"
            ref_id = artifact.thread_id or artifact.artifact_id
            ref_kind = "teams_chat"

        metadata: dict[str, str | int | float | bool | None] = {}
        if artifact.series_id:
            metadata["series_id"] = artifact.series_id
        if artifact.thread_id:
            metadata["thread_id"] = artifact.thread_id
        if artifact.topics:
            metadata["topics"] = ",".join(artifact.topics)
        if artifact.routing_reasoning:
            metadata["routing_reasoning"] = artifact.routing_reasoning
        if artifact.high_confidence_streak:
            metadata["high_confidence_streak"] = artifact.high_confidence_streak
        if artifact.legacy_artifact_ids:
            metadata["legacy_artifact_ids"] = ",".join(artifact.legacy_artifact_ids)

        registration = ChannelRegistration(
            channel="teams",
            program_id=program,
            provider_instance_id="default",
            ref_id=ref_id,
            ref_kind=ref_kind,
            status=RegistrationStatus.ACTIVE,
            first_discovered_at=_to_dt(artifact.first_seen),
            last_seen_at=_to_dt(artifact.last_seen),
            confidence=artifact.confidence,
            confidence_source=artifact.confidence_source or "m365_migration",
            pm_confirmed=artifact.pm_confirmed,
            promoted=artifact.promoted_to_workstreams_yaml,
            signal_yield_last_3=artifact.signal_yield_last_3,
            ref_title=artifact.display_name,
            metadata=metadata or None,
        )
        binding = RegistrationBinding(
            workstream_id=artifact.inferred_workstream,
            scope_id="default",
            source_type="m365_migration",
            confidence=artifact.confidence,
            confidence_source=artifact.confidence_source or "m365_migration",
            pm_confirmed=artifact.pm_confirmed,
            promoted=artifact.promoted_to_workstreams_yaml,
            status=RegistrationStatus.ACTIVE,
            signal_yield_last_3=artifact.signal_yield_last_3,
        )
        discovered_refs_by_channel.setdefault(channel, []).append(
            DiscoveredRef(
                registration=replace(registration, channel=channel, ref_id=ref_id, ref_kind=ref_kind),
                bindings=(binding,),
            )
        )

    total_refs = sum(len(refs) for refs in discovered_refs_by_channel.values())
    typer.echo(f"Migrating {total_refs} M365 artifacts -> UIL channels for {program}.")
    if dry_run:
        for channel, refs in discovered_refs_by_channel.items():
            for ref in refs:
                r = ref.registration
                b = ref.bindings[0]
                typer.echo(
                    f"  {channel} {r.ref_kind}:{r.ref_id}  ws={b.workstream_id}  conf={r.confidence:.2f}  pm={r.pm_confirmed}  yield={r.signal_yield_last_3}"
                )
        return

    store = _store(program, programs_root)
    summaries: list[str] = []
    for channel, refs in discovered_refs_by_channel.items():
        if not refs:
            continue
        delta = store.apply_discovery_result(
            DiscoveryResult(
                channel=channel,
                program_id=program,
                discovered_refs=tuple(refs),
                completeness=DiscoveryCompleteness.FULL,
                scope_statuses={
                    "m365_migration": ScopeStatus(
                        scope_id="m365_migration",
                        status=ScopeStatusKind.SUCCESS,
                        completeness=DiscoveryCompleteness.FULL,
                        item_count=len(refs),
                    )
                },
                scope_state_updates={},
                errors=(),
                computed_at=now,
                provider_instance_id="default",
            ),
            accept_shrinkage=True,
        )
        summaries.append(f"{channel}: {delta.summary}")
    typer.echo(f"Migration complete: {'; '.join(summaries)}")

    _do_feedback_migration(store)
    typer.echo(f"Integration registry schema is current for {program}.")



@app.command("schema-migrate")
def schema_migrate_registry(
    program: str = typer.Option(..., "--program", "-p", help="Program id."),
    force: bool = typer.Option(False, "--force", help="Accept schema re-initialization (data-destructive). Creates a backup first."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", help="Programs root."),
) -> None:
    """Handle non-additive schema migrations after a code upgrade.

    For additive changes (new columns), schema is auto-migrated on connection.
    This command is needed only when SchemaVersionError is raised (unknown schema
    version), indicating a non-additive structural change.

    Creates a timestamped backup before any destructive migration.
    """
    from src.core.channel_registry_store import ChannelRegistryStore, SchemaVersionError, SCHEMA_VERSION
    db_path = _registry_path(program, programs_root)
    if not db_path.exists():
        typer.echo(f"No registry found for {program}. Nothing to migrate.")
        return
    # Open without auto-schema-init so we can inspect and handle the version ourselves.
    store = ChannelRegistryStore(db_path, program, ensure_schema=False)
    try:
        store.ensure_schema()
        typer.echo(f"Registry for {program} is already at schema version {SCHEMA_VERSION}. Nothing to do.")
        return
    except SchemaVersionError as exc:
        typer.echo(f"Schema version mismatch: {exc}", err=True)
    if not force:
        typer.echo(
            "Use --force to accept re-initialization. A timestamped backup will be created first.",
            err=True,
        )
        raise typer.Exit(code=1)
    # Create backup before destructive migration.
    backup_path = _backup_path(program, programs_root)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    _sqlite_copy(db_path, backup_path)
    typer.echo(f"Backup created: {backup_path.name}")
    # Re-initialize schema (safe because backup exists).
    db_path.unlink()
    fresh_store = ChannelRegistryStore(db_path, program)
    fresh_store.ensure_schema()
    typer.echo(f"Registry for {program} re-initialized at schema version {SCHEMA_VERSION}.")
    typer.echo("Re-run `vertex integration discover` to repopulate the registry from discovery providers.")


@app.command("backup")
def backup_integration_registry(
    program: str = typer.Option(..., "--program", "-p", help="Program id."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", help="Programs root."),
) -> None:
    source = _registry_path(program, programs_root)
    if not source.exists():
        typer.echo(f"No integration registry exists for {program}.")
        raise typer.Exit(code=1)
    backup_path = _backup_path(program, programs_root)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    _sqlite_copy(source, backup_path)
    typer.echo(f"Backup: {backup_path}")


@app.command("restore")
def restore_integration_registry(
    program: str = typer.Option(..., "--program", "-p", help="Program id."),
    backup: str | None = typer.Option(None, "--backup", help="Backup file name or timestamp."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", help="Programs root."),
) -> None:
    backup_dir = programs_root / program / "registry_backups"
    if backup is None:
        backups = sorted(backup_dir.glob("channel_registry-*.sqlite3"))
        if not backups:
            typer.echo(f"No integration registry backups found for {program}.")
            return
        for path in backups:
            typer.echo(path.name)
        return
    source = _resolve_backup(backup_dir, backup)
    if source is None:
        typer.echo(f"Backup '{backup}' not found for {program}.")
        raise typer.Exit(code=1)
    target = _registry_path(program, programs_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        safety = _backup_path(program, programs_root, prefix="channel_registry-pre-restore")
        safety.parent.mkdir(parents=True, exist_ok=True)
        _sqlite_copy(target, safety)
    _sqlite_copy(source, target)
    typer.echo(f"Restored: {source.name}")


@app.command("prune")
def prune_integration_registry(
    program: str = typer.Option(..., "--program", "-p", help="Program id."),
    channel: str = typer.Option(..., "--channel", "-c", help="Channel to prune (e.g. teams, ado)."),
    older_than_days: int = typer.Option(90, "--older-than-days", help="Delete RETIRED/SUPPRESSED registrations older than this many days."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report how many rows would be pruned without deleting."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", help="Programs root."),
) -> None:
    """Delete RETIRED and SUPPRESSED registrations older than the retention window."""
    registry = _registry_path(program, programs_root)
    if not registry.exists():
        typer.echo(f"No integration registry for {program}.")
        raise typer.Exit(code=0)
    if dry_run:
        typer.echo(f"[dry-run] Would prune RETIRED/SUPPRESSED {channel} registrations older than {older_than_days} days.")
        raise typer.Exit(code=0)
    store = _store(program, programs_root, ensure_schema=False)
    pruned = store.prune_retired(channel, older_than_days=older_than_days)
    typer.echo(f"Pruned {pruned} {channel} registration(s) older than {older_than_days} days for {program}.")


def _store(program: str, programs_root: Path, *, ensure_schema: bool = True) -> ChannelRegistryStore:
    return _store_impl(program, programs_root, ensure_schema=ensure_schema)


def _load_program(program: str, programs_root: Path):
    return _load_program_impl(program, programs_root)


def _bootstrap_discovery_state(
    program: str,
    *,
    programs_root: Path,
    candidate_store: SourceCandidateStore,
) -> None:
    _bootstrap_discovery_state_impl(
        program,
        programs_root=programs_root,
        candidate_store=candidate_store,
    )


def _channel_for_ref_kind(ref_kind: SourceRefKind) -> str:
    return channel_for_source_ref_kind(ref_kind)


def _append_intent_decision_log(
    program: str,
    *,
    programs_root: Path,
    payload: dict[str, Any],
) -> None:
    append_intent_decision_log(program, programs_root=programs_root, payload=payload)


def _intent_decision_payload(
    *,
    ts: datetime,
    intent: SourceIntent,
    action: str,
    pm_alias: str,
    old_status: str,
    new_status: str,
    reason: str | None,
    candidate_id: str | None = None,
    ref_id: str | None = None,
) -> dict[str, Any]:
    return intent_decision_payload(
        ts=ts,
        intent=intent,
        action=action,
        actor_alias=pm_alias,
        old_status=old_status,
        new_status=new_status,
        reason=reason,
        candidate_id=candidate_id,
        ref_id=ref_id,
    )
















def _accept_candidate_for_intent(
    *,
    program: str,
    programs_root: Path,
    candidate_store: SourceCandidateStore,
    intent: SourceIntent,
    candidate: SourceCandidate,
    pm_alias: str,
    reason: str | None,
    match_confidence: float,
    existing_candidate: bool,
    unlinked_intent_ids: tuple[str, ...] = (),
) -> tuple[SourceCandidate, SourceIntent, tuple[tuple[SourceIntent, SourceIntent], ...]]:
    return _accept_candidate_for_intent_impl(
        program=program,
        programs_root=programs_root,
        candidate_store=candidate_store,
        intent=intent,
        candidate=candidate,
        pm_alias=pm_alias,
        reason=reason,
        match_confidence=match_confidence,
        existing_candidate=existing_candidate,
        load_program=_load_program,
        store_factory=_store,
        unlinked_intent_ids=unlinked_intent_ids,
    )


def _reject_candidate(
    *,
    program: str,
    programs_root: Path,
    candidate_store: SourceCandidateStore,
    candidate: SourceCandidate,
    pm_alias: str,
    reason: str,
) -> tuple[SourceCandidate, tuple[tuple[SourceIntent, SourceIntent], ...], bool]:
    return _reject_candidate_impl(
        program=program,
        programs_root=programs_root,
        candidate_store=candidate_store,
        candidate=candidate,
        pm_alias=pm_alias,
        reason=reason,
        store_factory=_store,
    )


def _mutate_intent_lifecycle(
    *,
    program: str,
    programs_root: Path,
    workstream_id: str,
    ref_kind: SourceRefKind,
    display_name: str,
    pm_alias: str,
    reason: str,
    target_status: SourceIntentStatus,
) -> None:
    _mutate_intent_lifecycle_impl(
        program=program,
        programs_root=programs_root,
        workstream_id=workstream_id,
        ref_kind=ref_kind,
        display_name=display_name,
        pm_alias=pm_alias,
        reason=reason,
        target_status=target_status,
        store_factory=_store,
        bootstrap_discovery_state=_bootstrap_discovery_state,
    )


def _restore_intent_lifecycle(
    *,
    program: str,
    programs_root: Path,
    workstream_id: str,
    ref_kind: SourceRefKind,
    display_name: str,
    pm_alias: str,
    expected_status: SourceIntentStatus,
    action: str,
) -> None:
    _restore_intent_lifecycle_impl(
        program=program,
        programs_root=programs_root,
        workstream_id=workstream_id,
        ref_kind=ref_kind,
        display_name=display_name,
        pm_alias=pm_alias,
        expected_status=expected_status,
        action=action,
        load_program=_load_program,
        store_factory=_store,
        bootstrap_discovery_state=_bootstrap_discovery_state,
    )


