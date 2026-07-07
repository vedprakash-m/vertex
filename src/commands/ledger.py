from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import tempfile
from time import monotonic
from typing import Any, cast
import uuid

log = logging.getLogger(__name__)

import typer

from src.core.config_loader import PROGRAMS_ROOT
from src.core.knowledge_claim_store import find_claim_revision_by_id
from src.core.knowledge_store import get_shared_knowledge_root
from src.core.knowledge.vault import load_all_vault_entries, load_shared_vault_verify_status, write_shared_vault_verify_status
from src.core.knowledge.vault_integrity import summarize_knowledge_vault_integrity
from src.core.ledger.evidence_vault import delete_evidence_vault_entry, evidence_vault_entry_status, evidence_vault_paths, load_evidence_vault_entries
from src.core.ledger.redaction import load_redaction_registry, redact_event
from src.core.ledger.event_type_registry import EventDisposition, lookup_event_spec
from src.core.ledger.fact_bridge import append_bridged_assumption_event, append_bridged_commitment_event, append_bridged_decision_event, append_bridged_dependency_event, append_bridged_milestone_event, append_bridged_risk_event, append_bridged_workstream_event, sync_bridged_risk_corroboration
from src.core.rev.privacy import run_local_checks
from src.core.rev.entity_types import EntityType
from src.core.ledger.discovery_candidate_builders import DiscoveryCandidateBuildError, build_lt_deck_artifact_candidates, candidate_from_import_line, fresh_discovery_batch_id
from src.core.ledger.candidate_store import (
    CandidateDecisionRecord,
    CandidateEntityResolution,
    CandidateEvent,
    SKIP_EXPIRY_DAYS,
    active_candidates,
    active_count,
    append_candidate,
    append_triage_decision,
    batch_ids,
    derive_candidate_dedupe_key,
    get_candidate_db_dir,
    load_pending_candidates,
    load_triage_decisions,
    summarize_triage_activity,
    summarize_batch_progress,
)
from src.core.ledger.candidate_sqlite_store import (
    init_candidate_db,
    outbox_enqueue,
    outbox_mark_projected,
    outbox_mark_failed,
    outbox_list_dead_letters,
    OUTBOX_STATUS_DEAD_LETTER,
)
from src.core.ledger.event_index import load_entity_event_ids, load_indexed_events, load_vault_refs, rebuild_event_index
from src.core.ledger.event_log import ConfidenceTier, EventEnvelope, TemporalConfidence, build_event_envelope, canonical_json, compute_dedupe_core_hash, read_events, verify_event_log, write_event, write_events_atomic
from src.core.ledger.event_types import get_event_schema, validate_event_payload
from src.core.ledger.verification_assertions import (
    assertions_for_candidate,
    effective_verification_state,
    is_candidate_verified,
)
from src.core.ledger.program_views import _LOCKABLE_FIELDS, canonical_projection_dump, collapse_orphan_links, collapse_shadow_links, get_current_projection_path, project_events_incremental_to_sqlite, project_events_to_memory, project_events_to_sqlite, project_program_events
from src.core.edition_resolver import load_program
from src.core.quality_gates.editorial import summarize_open_material_conflicts
from src.core.ledger.verify_status import load_ledger_verify_status, write_ledger_verify_status
from src.core.ledger.source_refs import LTDeckRef, OperatorAssertionRef, SourceRef, source_document_key, source_ref_from_dict
from src.core.operator_identity import capture_operator_identity


_BACKFILL_PIPELINE_TIER_BY_NAME = {
    "lt_deck": "tier_a",
    "newsletter": "tier_b",
    "email": "tier_b",
    "kb_extract": "tier_c",
}


app = typer.Typer(help="Manage append-only program ledger state.")
triage_app = typer.Typer(help="Review staged ledger candidates.")
app.add_typer(triage_app, name="triage")

_VERIFY_VAULT_CORRUPTION_EXIT_CODE = 4


@app.command("write")
def write(
    program: str = typer.Option(..., "--program", "-p", help="Program ID to mutate."),
    event_type: str = typer.Option(..., "--event-type", help="Registered ledger event type to write."),
    occurred_at: str = typer.Option(..., "--occurred-at", help="Occurred-at timestamp (ISO-8601)."),
    actor: str = typer.Option(..., "--actor", help="Actor writing the event."),
    payload_json: str = typer.Option(..., "--payload-json", help="JSON object payload for the event."),
    source_ref_json: str = typer.Option(..., "--source-ref-json", help="JSON object representing the SourceRef."),
    confidence: str = typer.Option(ConfidenceTier.OPERATOR_CONFIRMED.value, "--confidence", help="Confidence tier."),
    temporal_confidence: str = typer.Option(TemporalConfidence.EXACT.value, "--temporal-confidence", help="Temporal confidence tier."),
    corroborating_refs_json: str | None = typer.Option(None, "--corroborating-refs-json", help="Optional JSON array of corroborating SourceRefs."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    payload = _parse_json_mapping(payload_json, option_name="--payload-json")
    source_ref = source_ref_from_dict(_parse_json_mapping(source_ref_json, option_name="--source-ref-json"))
    corroborating_refs = _parse_source_ref_list(corroborating_refs_json)
    occurred = _parse_cli_datetime(occurred_at, option_name="--occurred-at")
    if occurred is None:
        raise typer.BadParameter("--occurred-at is required.")
    envelope = build_event_envelope(
        program_id=program,
        event_type=event_type,
        occurred_at=occurred,
        recorded_at=datetime.now(timezone.utc),
        temporal_confidence=TemporalConfidence(temporal_confidence),
        confidence=ConfidenceTier(confidence),
        actor=actor,
        payload=payload,
        source_ref=source_ref,
        corroborating_refs=corroborating_refs,
        dedupe_payload=_dedupe_payload_for(event_type, payload),
    )
    diverted_candidate = _maybe_divert_locked_direct_write(envelope, programs_root=programs_root)
    if diverted_candidate is not None:
        typer.echo(
            f"Locked field conflict; staged candidate {diverted_candidate.candidate_id} in batch {diverted_candidate.batch_id}."
        )
        typer.echo(f"Review with: vertex ledger triage list --program {program} --batch-id {diverted_candidate.batch_id}")
        return
    persisted = _persist_event(envelope, programs_root=programs_root)
    project_program_events(program, programs_root=programs_root)
    typer.echo(f"Wrote {persisted.event_type} -> {persisted.event_id}")


@app.command("correct")
def correct(
    program: str = typer.Option(..., "--program", "-p", help="Program ID to mutate."),
    event_id: str = typer.Option(..., "--event-id", help="Event ID being corrected."),
    actor: str = typer.Option(..., "--actor", help="Operator applying the correction."),
    reason: str = typer.Option(..., "--reason", help="Correction reason."),
    corrected_payload_json: str = typer.Option(..., "--corrected-payload-json", help="JSON object payload or JSON null for tombstone."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    corrected_payload = _parse_json_value(corrected_payload_json, option_name="--corrected-payload-json")
    if corrected_payload is not None and not isinstance(corrected_payload, dict):
        raise typer.BadParameter("--corrected-payload-json must decode to an object or null.")
    now = datetime.now(timezone.utc)
    envelope = build_event_envelope(
        program_id=program,
        event_type="operator.correction.v1",
        occurred_at=now,
        recorded_at=now,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor=actor,
        payload={"corrects_event_id": event_id, "corrected_payload": corrected_payload, "reason": reason},
        source_ref=_operator_assertion(actor, f"ledger correct {event_id}", now),
        dedupe_payload={"corrects_event_id": event_id},
    )
    diverted_candidate = _maybe_divert_locked_direct_write(envelope, programs_root=programs_root)
    if diverted_candidate is not None:
        typer.echo(
            f"Locked field conflict; staged candidate {diverted_candidate.candidate_id} in batch {diverted_candidate.batch_id}."
        )
        typer.echo(f"Review with: vertex ledger triage list --program {program} --batch-id {diverted_candidate.batch_id}")
        return
    persisted = _persist_event(envelope, programs_root=programs_root)
    project_program_events(program, programs_root=programs_root)
    typer.echo(f"Corrected {event_id} -> {persisted.event_id}")


@app.command("lock")
def lock(
    program: str = typer.Option(..., "--program", "-p", help="Program ID to mutate."),
    entity_id: str = typer.Option(..., "--entity-id", help="Entity to lock."),
    field: str = typer.Option(..., "--field", help="Field name to lock."),
    actor: str = typer.Option(..., "--actor", help="Operator applying the lock."),
    locked_value_json: str | None = typer.Option(None, "--locked-value-json", help="Optional JSON value to pin."),
    valid_until: str | None = typer.Option(None, "--valid-until", help="Optional ISO-8601 expiry timestamp."),
    reason: str | None = typer.Option(None, "--reason", help="Optional lock reason."),
    override_session_id: str | None = typer.Option(None, "--override-session-id", help="Optional override session ID."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    now = datetime.now(timezone.utc)
    payload: dict[str, object] = {"entity_id": entity_id, "field": field}
    locked_value = _parse_json_value(locked_value_json, option_name="--locked-value-json") if locked_value_json is not None else None
    if locked_value_json is not None:
        payload["locked_value"] = locked_value
    parsed_valid_until = _parse_cli_datetime(valid_until, option_name="--valid-until")
    if parsed_valid_until is not None:
        payload["valid_until"] = parsed_valid_until.isoformat()
    if reason:
        payload["reason"] = reason
    if override_session_id:
        payload["override_session_id"] = override_session_id
    envelope = build_event_envelope(
        program_id=program,
        event_type="operator.field_lock.v1",
        occurred_at=now,
        recorded_at=now,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor=actor,
        payload=payload,
        source_ref=_operator_assertion(actor, f"ledger lock {entity_id}.{field}", now),
        dedupe_payload={key: payload[key] for key in ("entity_id", "field", "locked_value") if key in payload},
    )
    persisted = _persist_event(envelope, programs_root=programs_root)
    project_program_events(program, programs_root=programs_root)
    typer.echo(f"Locked {entity_id}.{field} -> {persisted.event_id}")


@app.command("unlock")
def unlock(
    program: str = typer.Option(..., "--program", "-p", help="Program ID to mutate."),
    entity_id: str = typer.Option(..., "--entity-id", help="Entity to unlock."),
    field: str = typer.Option(..., "--field", help="Field name to unlock."),
    actor: str = typer.Option(..., "--actor", help="Operator removing the lock."),
    reason: str | None = typer.Option(None, "--reason", help="Optional unlock reason."),
    override_session_id: str | None = typer.Option(None, "--override-session-id", help="Optional override session ID."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    now = datetime.now(timezone.utc)
    payload: dict[str, object] = {"entity_id": entity_id, "field": field}
    if reason:
        payload["reason"] = reason
    if override_session_id:
        payload["override_session_id"] = override_session_id
    envelope = build_event_envelope(
        program_id=program,
        event_type="operator.field_unlock.v1",
        occurred_at=now,
        recorded_at=now,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor=actor,
        payload=payload,
        source_ref=_operator_assertion(actor, f"ledger unlock {entity_id}.{field}", now),
        dedupe_payload={"entity_id": entity_id, "field": field},
    )
    persisted = _persist_event(envelope, programs_root=programs_root)
    project_program_events(program, programs_root=programs_root)
    typer.echo(f"Unlocked {entity_id}.{field} -> {persisted.event_id}")


@app.command("status")
def status(
    program: str = typer.Option(..., "--program", "-p", help="Program ID to inspect."),
    batch_id: str | None = typer.Option(None, "--batch-id", help="Filter active candidates to a single batch."),
    format: str = typer.Option("text", "--format", help="Output format: text or json."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    pending = load_pending_candidates(program, programs_root=programs_root)
    decisions = load_triage_decisions(program, programs_root=programs_root)
    active = active_candidates(program, programs_root=programs_root, batch_id=batch_id)
    active_candidate_summary = _summarize_active_candidates(list(active))
    gap_rows = _load_gap_rows(program, programs_root=programs_root)
    gap_summary = _summarize_unacknowledged_gaps(gap_rows)
    field_lock_rows = _load_field_lock_rows(program, programs_root=programs_root)
    field_lock_summary = _summarize_field_locks(field_lock_rows)
    projection_freshness = _summarize_projection_freshness(program, programs_root=programs_root)
    latest_verify_summary = _summarize_latest_verify(program, programs_root=programs_root)
    open_conflict_summary = _summarize_open_conflicts(program, programs_root=programs_root)
    triage_activity_summary = _summarize_triage_activity(program, programs_root=programs_root)
    event_count_summary = _summarize_event_counts(program, programs_root=programs_root)
    vault_summary = _summarize_vault_status(program, programs_root=programs_root)
    batch_progress = summarize_batch_progress(program, programs_root=programs_root)
    backfill_tier_summary = _summarize_backfill_tiers(batch_progress.records)
    coverage_earliest, coverage_latest = _load_projection_coverage_range(program, programs_root=programs_root)
    payload: dict[str, Any] = {
        "program_id": program,
        "event_count": event_count_summary["event_count"],
        "event_count_by_type": event_count_summary["event_count_by_type"],
        "event_count_by_confidence": event_count_summary["event_count_by_confidence"],
        "pending_count": len(pending),
        "decision_count": len(decisions),
        "active_count": active_count(program, programs_root=programs_root, batch_id=batch_id),
        "evidence_vault_file_count": vault_summary["evidence_vault_file_count"],
        "evidence_vault_total_bytes": vault_summary["evidence_vault_total_bytes"],
        "evidence_vault_last_deep_verify_at": vault_summary["evidence_vault_last_deep_verify_at"],
        "evidence_vault_last_deep_verify_ok": vault_summary["evidence_vault_last_deep_verify_ok"],
        "evidence_vault_last_deep_verify_age_seconds": vault_summary["evidence_vault_last_deep_verify_age_seconds"],
        "knowledge_vault_file_count": vault_summary["knowledge_vault_file_count"],
        "knowledge_vault_total_bytes": vault_summary["knowledge_vault_total_bytes"],
        "knowledge_vault_last_deep_verify_at": vault_summary["knowledge_vault_last_deep_verify_at"],
        "knowledge_vault_last_deep_verify_ok": vault_summary["knowledge_vault_last_deep_verify_ok"],
        "knowledge_vault_last_deep_verify_age_seconds": vault_summary["knowledge_vault_last_deep_verify_age_seconds"],
        "backfill_batches_by_tier": backfill_tier_summary["backfill_batches_by_tier"],
        "pending_candidates_by_pipeline": active_candidate_summary["pending_candidates_by_pipeline"],
        "oldest_active_candidate_staged_at": active_candidate_summary["oldest_active_candidate_staged_at"],
        "oldest_active_candidate_age_seconds": active_candidate_summary["oldest_active_candidate_age_seconds"],
        "projection_current": projection_freshness["projection_current"],
        "projection_watermark": projection_freshness["projection_watermark"],
        "ledger_head": projection_freshness["ledger_head"],
        "chain_head": projection_freshness["ledger_head"],
        "last_verify_at": latest_verify_summary["last_verify_at"],
        "last_verify_ok": latest_verify_summary["last_verify_ok"],
        "last_verify_age_seconds": latest_verify_summary["last_verify_age_seconds"],
        "last_verify_deep": latest_verify_summary["last_verify_deep"],
        "last_verify_checked_event_count": latest_verify_summary["last_verify_checked_event_count"],
        "open_conflict_count": open_conflict_summary["open_conflict_count"],
        "open_conflict_previews": open_conflict_summary["open_conflict_previews"],
        "latest_triage_decision_at": triage_activity_summary["latest_triage_decision_at"],
        "latest_triage_session_actor": triage_activity_summary["latest_triage_session_actor"],
        "latest_triage_session_started_at": triage_activity_summary["latest_triage_session_started_at"],
        "latest_triage_session_ended_at": triage_activity_summary["latest_triage_session_ended_at"],
        "latest_triage_session_decision_count": triage_activity_summary["latest_triage_session_decision_count"],
        "latest_triage_session_duration_seconds": triage_activity_summary["latest_triage_session_duration_seconds"],
        "latest_triage_session_throughput_per_minute": triage_activity_summary["latest_triage_session_throughput_per_minute"],
        "triage_session_gap_minutes": triage_activity_summary["triage_session_gap_minutes"],
        "active_lock_count": field_lock_summary["active_lock_count"],
        "expiring_lock_count": field_lock_summary["expiring_lock_count"],
        "expiring_locks": field_lock_summary["expiring_locks"],
        "gap_count": gap_summary["gap_count"],
        "gaps_by_pipeline": gap_summary["gaps_by_pipeline"],
        "oldest_gap_window_start": gap_summary["oldest_gap_window_start"],
        "oldest_gap_window_end": gap_summary["oldest_gap_window_end"],
        "oldest_gap_pipeline": gap_summary["oldest_gap_pipeline"],
        "oldest_gap_kind": gap_summary["oldest_gap_kind"],
        "coverage_earliest": coverage_earliest,
        "coverage_latest": coverage_latest,
        "batch_count": batch_progress.batch_count,
        "staged_batch_count": batch_progress.staged_batch_count,
        "approved_batch_count": batch_progress.approved_batch_count,
        "quarantined_batch_count": batch_progress.quarantined_batch_count,
        "batch_ids": list(batch_ids(program, programs_root=programs_root)),
        "batches": [
            {
                "batch_id": record.batch_id,
                "status": record.status,
                "total_candidate_count": record.total_candidate_count,
                "active_candidate_count": record.active_candidate_count,
                "approved_count": record.approved_count,
                "rejected_count": record.rejected_count,
                "skipped_count": record.skipped_count,
                "quarantined_candidate_count": record.quarantined_candidate_count,
                "latest_decision_at": None if record.latest_decision_at is None else record.latest_decision_at.isoformat(),
                "pipelines": dict(record.pipelines),
            }
            for record in batch_progress.records
        ],
        "active_candidates": [
            {
                "candidate_id": candidate.candidate_id,
                "event_type": candidate.proposed_event_type,
                "batch_id": candidate.batch_id,
                "extraction_confidence": candidate.extraction_confidence,
            }
            for candidate in active
        ],
    }
    normalized_format = format.strip().lower()
    if normalized_format == "json":
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    if normalized_format != "text":
        raise typer.BadParameter("--format must be 'text' or 'json'.")
    typer.echo(f"LEDGER STATUS {program}")
    typer.echo(
        f"events_total={payload['event_count']} pending={payload['pending_count']} active={payload['active_count']} "
        f"decisions={payload['decision_count']} gaps={payload['gap_count']}"
    )
    event_type_summary = ",".join(
        f"{event_type}={count}" for event_type, count in payload["event_count_by_type"].items()
    ) or "-"
    event_confidence_summary = ",".join(
        f"{confidence}={count}" for confidence, count in payload["event_count_by_confidence"].items()
    ) or "-"
    typer.echo(f"events_by_type={event_type_summary} events_by_confidence={event_confidence_summary}")
    pending_pipeline_summary = ",".join(
        f"{pipeline}={count}" for pipeline, count in payload["pending_candidates_by_pipeline"].items()
    ) or "-"
    typer.echo(
        f"pending_by_pipeline={pending_pipeline_summary} oldest_active_candidate_staged_at={payload['oldest_active_candidate_staged_at']}"
        f" oldest_active_candidate_age_seconds={payload['oldest_active_candidate_age_seconds']}"
    )
    typer.echo(
        f"latest_triage_actor={payload['latest_triage_session_actor'] or '-'}"
        f" latest_triage_decision_at={payload['latest_triage_decision_at']}"
        f" latest_triage_session_decisions={payload['latest_triage_session_decision_count']}"
        f" latest_triage_session_duration_seconds={payload['latest_triage_session_duration_seconds']}"
        f" latest_triage_throughput_per_minute={payload['latest_triage_session_throughput_per_minute']}"
        f" triage_session_gap_minutes={payload['triage_session_gap_minutes']}"
    )
    typer.echo(
        f"evidence_vault_files={payload['evidence_vault_file_count']} evidence_vault_bytes={payload['evidence_vault_total_bytes']}"
        f" evidence_vault_last_deep_verify_ok={payload['evidence_vault_last_deep_verify_ok']}"
        f" evidence_vault_last_deep_verify_at={payload['evidence_vault_last_deep_verify_at']}"
        f" evidence_vault_last_deep_verify_age_seconds={payload['evidence_vault_last_deep_verify_age_seconds']}"
    )
    typer.echo(
        f"knowledge_vault_files={payload['knowledge_vault_file_count']} knowledge_vault_bytes={payload['knowledge_vault_total_bytes']}"
        f" knowledge_vault_last_deep_verify_ok={payload['knowledge_vault_last_deep_verify_ok']}"
        f" knowledge_vault_last_deep_verify_at={payload['knowledge_vault_last_deep_verify_at']}"
        f" knowledge_vault_last_deep_verify_age_seconds={payload['knowledge_vault_last_deep_verify_age_seconds']}"
    )
    expiring_lock_summary = ",".join(payload["expiring_locks"]) or "-"
    typer.echo(
        f"locks_active={payload['active_lock_count']} locks_expiring_within_7d={payload['expiring_lock_count']}"
        f" expiring_locks={expiring_lock_summary}"
    )
    gap_pipeline_summary = ",".join(
        f"{pipeline}={count}" for pipeline, count in payload["gaps_by_pipeline"].items()
    ) or "-"
    typer.echo(
        f"gaps_by_pipeline={gap_pipeline_summary} oldest_gap_pipeline={payload['oldest_gap_pipeline']}"
        f" oldest_gap_kind={payload['oldest_gap_kind']} oldest_gap_window_start={payload['oldest_gap_window_start']}"
        f" oldest_gap_window_end={payload['oldest_gap_window_end']}"
    )
    typer.echo(
        f"coverage_earliest={payload['coverage_earliest']} coverage_latest={payload['coverage_latest']}"
        f" batches_total={payload['batch_count']} batches_staged={payload['staged_batch_count']}"
        f" batches_approved={payload['approved_batch_count']} batches_quarantined={payload['quarantined_batch_count']}"
    )
    backfill_tier_text = ",".join(
        f"{tier}={count}" for tier, count in payload["backfill_batches_by_tier"].items()
    ) or "-"
    typer.echo(f"backfill_batches_by_tier={backfill_tier_text}")
    typer.echo(
        f"projection_status={'current' if payload['projection_current'] else 'stale'}"
        f" projection_watermark={payload['projection_watermark']} ledger_head={payload['ledger_head']}"
    )
    typer.echo(
        f"chain_head={payload['chain_head']} last_verify_ok={payload['last_verify_ok']}"
        f" last_verify_at={payload['last_verify_at']} last_verify_age_seconds={payload['last_verify_age_seconds']}"
        f" last_verify_deep={payload['last_verify_deep']} last_verify_checked_event_count={payload['last_verify_checked_event_count']}"
    )
    conflict_preview_summary = ", ".join(payload["open_conflict_previews"]) or "-"
    typer.echo(
        f"open_conflicts={payload['open_conflict_count']} open_conflict_previews={conflict_preview_summary}"
    )
    if payload["batch_ids"]:
        typer.echo(f"batches={', '.join(payload['batch_ids'])}")
    for batch in payload["batches"]:
        pipeline_summary = ",".join(f"{pipeline}={count}" for pipeline, count in batch["pipelines"].items()) or "-"
        typer.echo(
            f"- batch={batch['batch_id']} status={batch['status']} total={batch['total_candidate_count']}"
            f" active={batch['active_candidate_count']} approved={batch['approved_count']}"
            f" rejected={batch['rejected_count']} skipped={batch['skipped_count']}"
            f" quarantined={batch['quarantined_candidate_count']} pipelines={pipeline_summary}"
        )
    if not active:
        typer.echo("No active candidates.")
        return
    typer.echo("ACTIVE CANDIDATES")
    for candidate in active:
        typer.echo(
            f"- {candidate.candidate_id} {candidate.proposed_event_type} "
            f"batch={candidate.batch_id} extraction_confidence={candidate.extraction_confidence:.3f}"
        )


def _load_projection_coverage_range(program_id: str, *, programs_root: Path) -> tuple[str | None, str | None]:
    row = _load_projection_meta_row(program_id, programs_root=programs_root)
    if row is None:
        return None, None
    return cast(str | None, row.get("coverage_earliest")), cast(str | None, row.get("coverage_latest"))


def _load_projection_meta_row(program_id: str, *, programs_root: Path) -> dict[str, object] | None:
    projection_path = get_current_projection_path(program_id, programs_root=programs_root)
    if not projection_path.exists():
        return None
    projection = canonical_projection_dump(projection_path)
    projection_meta = projection.get("projection_meta", [])
    if not projection_meta:
        return None
    return projection_meta[0]


def _load_field_lock_rows(program_id: str, *, programs_root: Path) -> list[dict[str, object]]:
    projection_path = get_current_projection_path(program_id, programs_root=programs_root)
    if not projection_path.exists():
        project_program_events(program_id, programs_root=programs_root)
    projection = canonical_projection_dump(get_current_projection_path(program_id, programs_root=programs_root))
    return list(projection["field_locks"])


def _summarize_field_locks(rows: list[dict[str, object]]) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    expiring_locks: list[str] = []
    for row in rows:
        valid_until = _parse_optional_utc_datetime(row.get("valid_until"))
        if valid_until is None or valid_until <= now:
            continue
        remaining = valid_until - now
        if remaining <= timedelta(days=7):
            expiring_locks.append(f"{row.get('entity_id')}.{row.get('field')}")
    expiring_locks.sort()
    return {
        "active_lock_count": len(rows),
        "expiring_lock_count": len(expiring_locks),
        "expiring_locks": expiring_locks,
    }


def _summarize_active_candidates(candidates: list[CandidateEvent]) -> dict[str, object]:
    pipelines: dict[str, int] = {}
    oldest_staged_at: datetime | None = None
    now = datetime.now(timezone.utc)

    for candidate in candidates:
        pipelines[candidate.pipeline] = pipelines.get(candidate.pipeline, 0) + 1
        if candidate.staged_at is None:
            continue
        if oldest_staged_at is None or candidate.staged_at < oldest_staged_at:
            oldest_staged_at = candidate.staged_at

    oldest_age_seconds = None
    if oldest_staged_at is not None:
        oldest_age_seconds = max(0, int((now - oldest_staged_at).total_seconds()))

    return {
        "pending_candidates_by_pipeline": dict(sorted(pipelines.items())),
        "oldest_active_candidate_staged_at": None if oldest_staged_at is None else oldest_staged_at.isoformat(),
        "oldest_active_candidate_age_seconds": oldest_age_seconds,
    }


def _summarize_projection_freshness(program_id: str, *, programs_root: Path) -> dict[str, object]:
    events = read_events(program_id, programs_root=programs_root)
    ledger_head = events[-1].event_id if events else None
    projection_meta = _load_projection_meta_row(program_id, programs_root=programs_root)
    projection_watermark = None if projection_meta is None else projection_meta.get("event_watermark")
    projection_current = ledger_head is None or projection_watermark == ledger_head
    return {
        "projection_current": projection_current,
        "projection_watermark": projection_watermark,
        "ledger_head": ledger_head,
    }


def _summarize_latest_verify(program_id: str, *, programs_root: Path) -> dict[str, object]:
    latest_verify = load_ledger_verify_status(program_id, programs_root=programs_root)
    if latest_verify is None:
        return {
            "last_verify_at": None,
            "last_verify_ok": None,
            "last_verify_age_seconds": None,
            "last_verify_deep": None,
            "last_verify_checked_event_count": None,
        }
    age_seconds = max(0, int((datetime.now(timezone.utc) - latest_verify.verified_at).total_seconds()))
    return {
        "last_verify_at": latest_verify.verified_at.isoformat(),
        "last_verify_ok": latest_verify.ok,
        "last_verify_age_seconds": age_seconds,
        "last_verify_deep": latest_verify.deep,
        "last_verify_checked_event_count": latest_verify.checked_event_count,
    }


def _summarize_open_conflicts(program_id: str, *, programs_root: Path) -> dict[str, object]:
    try:
        conflict_summary = summarize_open_material_conflicts(program_id, programs_root=programs_root)
    except Exception:
        return {
            "open_conflict_count": 0,
            "open_conflict_previews": [],
        }
    return {
        "open_conflict_count": conflict_summary["count"],
        "open_conflict_previews": list(cast(list[object], conflict_summary["previews"])),
    }


def _summarize_triage_activity(program_id: str, *, programs_root: Path) -> dict[str, object]:
    activity = summarize_triage_activity(program_id, programs_root=programs_root)
    latest_session = activity.latest_session
    return {
        "latest_triage_decision_at": None if activity.latest_decision_at is None else activity.latest_decision_at.isoformat(),
        "latest_triage_session_actor": None if latest_session is None else latest_session.actor,
        "latest_triage_session_started_at": None if latest_session is None else latest_session.started_at.isoformat(),
        "latest_triage_session_ended_at": None if latest_session is None else latest_session.ended_at.isoformat(),
        "latest_triage_session_decision_count": 0 if latest_session is None else latest_session.decision_count,
        "latest_triage_session_duration_seconds": None if latest_session is None else latest_session.duration_seconds,
        "latest_triage_session_throughput_per_minute": None if latest_session is None else latest_session.throughput_per_minute,
        "triage_session_gap_minutes": activity.session_gap_minutes,
    }


def _summarize_event_counts(program_id: str, *, programs_root: Path) -> dict[str, object]:
    indexed_events = load_indexed_events(program_id, programs_root=programs_root)
    counts_by_type: dict[str, int] = {}
    counts_by_confidence: dict[str, int] = {}
    for event in indexed_events:
        counts_by_type[event.event_type] = counts_by_type.get(event.event_type, 0) + 1
        counts_by_confidence[event.confidence] = counts_by_confidence.get(event.confidence, 0) + 1
    return {
        "event_count": len(indexed_events),
        "event_count_by_type": dict(sorted(counts_by_type.items())),
        "event_count_by_confidence": dict(sorted(counts_by_confidence.items())),
    }


def _summarize_vault_status(program_id: str, *, programs_root: Path) -> dict[str, object]:
    evidence_entries = load_evidence_vault_entries(program_id, programs_root=programs_root)
    evidence_verify = load_ledger_verify_status(program_id, programs_root=programs_root)
    evidence_verify_at = None
    evidence_verify_ok = None
    evidence_verify_age_seconds = None
    if evidence_verify is not None and evidence_verify.deep:
        evidence_verify_at = evidence_verify.verified_at.isoformat()
        evidence_verify_ok = evidence_verify.ok
        evidence_verify_age_seconds = max(0, int((datetime.now(timezone.utc) - evidence_verify.verified_at).total_seconds()))

    knowledge_entries = load_all_vault_entries(programs_root=programs_root)
    knowledge_verify = load_shared_vault_verify_status(programs_root=programs_root)
    knowledge_verify_at = None
    knowledge_verify_ok = None
    knowledge_verify_age_seconds = None
    if knowledge_verify is not None:
        knowledge_verify_at = knowledge_verify.verified_at.isoformat()
        knowledge_verify_ok = knowledge_verify.ok
        knowledge_verify_age_seconds = max(0, int((datetime.now(timezone.utc) - knowledge_verify.verified_at).total_seconds()))

    return {
        "evidence_vault_file_count": len(evidence_entries),
        "evidence_vault_total_bytes": sum(entry.content_path.stat().st_size for entry in evidence_entries),
        "evidence_vault_last_deep_verify_at": evidence_verify_at,
        "evidence_vault_last_deep_verify_ok": evidence_verify_ok,
        "evidence_vault_last_deep_verify_age_seconds": evidence_verify_age_seconds,
        "knowledge_vault_file_count": len(knowledge_entries),
        "knowledge_vault_total_bytes": sum(entry.size_bytes for entry in knowledge_entries),
        "knowledge_vault_last_deep_verify_at": knowledge_verify_at,
        "knowledge_vault_last_deep_verify_ok": knowledge_verify_ok,
        "knowledge_vault_last_deep_verify_age_seconds": knowledge_verify_age_seconds,
    }


def _summarize_backfill_tiers(records: tuple[object, ...]) -> dict[str, object]:
    counts: dict[str, int] = {}
    for record in records:
        tier = _backfill_tier_for_pipelines(getattr(record, "pipelines", {}))
        if tier is None:
            continue
        counts[tier] = counts.get(tier, 0) + 1
    return {
        "backfill_batches_by_tier": dict(sorted(counts.items())),
    }


def _backfill_tier_for_pipelines(pipelines: dict[str, int]) -> str | None:
    present_tiers = {
        _BACKFILL_PIPELINE_TIER_BY_NAME[pipeline]
        for pipeline in pipelines
        if pipeline in _BACKFILL_PIPELINE_TIER_BY_NAME
    }
    if len(present_tiers) != 1:
        return None
    return next(iter(present_tiers))


def _parse_optional_utc_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _summarize_unacknowledged_gaps(rows: list[dict[str, object]]) -> dict[str, object]:
    unacknowledged_rows = [row for row in rows if not row.get("acknowledged")]
    gaps_by_pipeline: dict[str, int] = {}
    oldest_row: dict[str, object] | None = None

    for row in unacknowledged_rows:
        pipeline = str(row.get("pipeline") or "unknown")
        gaps_by_pipeline[pipeline] = gaps_by_pipeline.get(pipeline, 0) + 1
        if oldest_row is None:
            oldest_row = row
            continue
        if _gap_window_sort_key(row) < _gap_window_sort_key(oldest_row):
            oldest_row = row

    return {
        "gap_count": len(unacknowledged_rows),
        "gaps_by_pipeline": dict(sorted(gaps_by_pipeline.items())),
        "oldest_gap_window_start": None if oldest_row is None else oldest_row.get("window_start"),
        "oldest_gap_window_end": None if oldest_row is None else oldest_row.get("window_end"),
        "oldest_gap_pipeline": None if oldest_row is None else oldest_row.get("pipeline"),
        "oldest_gap_kind": None if oldest_row is None else oldest_row.get("gap_kind"),
    }


def _gap_window_sort_key(row: dict[str, object]) -> tuple[str, str, str]:
    window_start = row.get("window_start")
    window_end = row.get("window_end")
    event_id = row.get("event_id")
    return (str(window_start or ""), str(window_end or ""), str(event_id or ""))


@triage_app.command("list")
def triage_list(
    program: str = typer.Option(..., "--program", "-p", help="Program ID to inspect."),
    batch_id: str | None = typer.Option(None, "--batch-id", help="Optional batch filter."),
    format: str = typer.Option("text", "--format", help="Output format: text or json."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    active = active_candidates(program, programs_root=programs_root, batch_id=batch_id)
    payload = {
        "program_id": program,
        "batch_id": batch_id,
        "active_count": len(active),
        "candidates": [
            {
                "candidate_id": candidate.candidate_id,
                "event_type": candidate.proposed_event_type,
                "batch_id": candidate.batch_id,
                "extraction_confidence": candidate.extraction_confidence,
                # EXPLAIN-min (activation.md §6.14.19 / O-21): surface the
                # verbatim source quote + prompt version so the operator can
                # verify a candidate in seconds without opening the EML.
                "extraction_rationale": candidate.extraction_rationale,
                "prompt_version": candidate.prompt_version,
                "source_document_key": candidate.source_document_key,
            }
            for candidate in active
        ],
    }
    normalized_format = format.strip().lower()
    if normalized_format == "json":
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    if normalized_format != "text":
        raise typer.BadParameter("--format must be 'text' or 'json'.")
    typer.echo(f"LEDGER TRIAGE {program}")
    if batch_id:
        typer.echo(f"batch={batch_id}")
    if not active:
        typer.echo("No active candidates.")
        return
    for candidate in active:
        typer.echo(
            f"- {candidate.candidate_id} {candidate.proposed_event_type} "
            f"batch={candidate.batch_id} extraction_confidence={candidate.extraction_confidence:.3f}"
        )
        # EXPLAIN-min: show the source quote that produced this candidate.
        rationale = (candidate.extraction_rationale or "").strip()
        if rationale:
            typer.echo(f"    why: {rationale}")


@triage_app.command("batch-status")
def triage_batch_status(
    program: str = typer.Option(..., "--program", "-p", help="Program ID to inspect."),
    batch_id: str = typer.Option(..., "--batch-id", help="Batch ID to summarize."),
    format: str = typer.Option("text", "--format", help="Output format: text or json."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    payload = _build_batch_status(program, batch_id, programs_root=programs_root)
    normalized_format = format.strip().lower()
    if normalized_format == "json":
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    if normalized_format != "text":
        raise typer.BadParameter("--format must be 'text' or 'json'.")
    typer.echo(f"LEDGER BATCH STATUS {program}")
    typer.echo(f"batch={batch_id} total={payload['total_candidates']} active={payload['active_candidates']}")
    typer.echo(
        f"entity_resolution_rate={payload['entity_resolution_rate']:.3f} "
        f"approved_sample_count={payload['approved_sample_count']} required_sample_count={payload['required_sample_count']}"
    )
    typer.echo(
        f"entity_resolution_gate={payload['entity_resolution_gate']} "
        f"sample_gate={payload['sample_gate']} lock_conflict_gate={payload['lock_conflict_gate']}"
    )


@triage_app.command("batch-approve")
def triage_batch_approve(
    program: str = typer.Option(..., "--program", "-p", help="Program ID to mutate."),
    batch_id: str = typer.Option(..., "--batch-id", help="Batch ID to approve."),
    actor: str = typer.Option(..., "--actor", help="Operator approving the batch."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    batch_status = _build_batch_status(program, batch_id, programs_root=programs_root)
    if not batch_status["entity_resolution_gate"]:
        typer.echo(f"Batch '{batch_id}' failed entity-resolution gate.")
        raise typer.Exit(code=3)
    if not batch_status["sample_gate"]:
        typer.echo(f"Batch '{batch_id}' failed sample gate.")
        raise typer.Exit(code=3)
    if batch_status["lock_conflict_gate"] is False:
        typer.echo(f"Batch '{batch_id}' has unresolved lock conflicts.")
        raise typer.Exit(code=3)
    chain_check = verify_event_log(program, programs_root=programs_root)
    if not chain_check.ok:
        typer.echo(f"Ledger chain integrity check failed before batch approval. Run `vertex ledger verify --program {program}` to diagnose, then repair before re-running batch-approve.")
        raise typer.Exit(code=3)
    candidates = active_candidates(program, programs_root=programs_root, batch_id=batch_id)
    if not candidates:
        typer.echo(f"Batch {batch_id} has no active candidates to approve.")
        return
    approved = 0
    total = len(candidates)
    started_at = monotonic()
    last_report_at = started_at
    for candidate in candidates:
        resulting_event = _write_candidate_event(
            candidate,
            actor=actor,
            programs_root=programs_root,
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        )
        audit_event = _write_candidate_audit_event(
            candidate,
            actor=actor,
            event_type="discovery.candidate_approved.v1",
            payload={
                "candidate_id": candidate.candidate_id,
                "resulting_event_id": resulting_event.event_id,
                "triage_actor": actor,
                "edited": False,
            },
            programs_root=programs_root,
        )
        append_triage_decision(
            CandidateDecisionRecord(
                candidate_id=candidate.candidate_id,
                kind="approved",
                decided_at=datetime.now(timezone.utc),
                triage_actor=actor,
                batch_id=batch_id,
                edited=False,
                resulting_event_id=resulting_event.event_id,
                approval_event_id=audit_event.event_id,
            ),
            program_id=program,
            programs_root=programs_root,
        )
        # S-1: enqueue outbox entry per candidate before final batch projection
        _enqueue_outbox_entry(
            program, candidate.candidate_id, resulting_event.event_id, programs_root=programs_root
        )
        approved += 1
        now = monotonic()
        if approved % 10 == 0 or now - last_report_at >= 5 or approved == total:
            typer.echo(_render_import_progress(approved, total, started_at, now).replace("Import progress", "Batch approval progress"))
            last_report_at = now
    db_dir = get_candidate_db_dir(program, programs_root=programs_root)
    # collect all outbox IDs we just enqueued (stored in db by candidate_id lookup is complex;
    # rely on the per-candidate helper having stored them; project then mark all pending)
    try:
        project_program_events(program, programs_root=programs_root)
        _mark_pending_outbox_projected(db_dir, program_id=program)
    except Exception as exc:
        _mark_pending_outbox_failed(db_dir, program_id=program, reason=str(exc))
        typer.echo(f"[outbox] Batch projection failed: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Approved {approved} candidates from batch {batch_id}.")


@triage_app.command("batch-reject")
def triage_batch_reject(
    program: str = typer.Option(..., "--program", "-p", help="Program ID to mutate."),
    batch_id: str = typer.Option(..., "--batch-id", help="Batch ID to reject."),
    actor: str = typer.Option(..., "--actor", help="Operator rejecting the batch."),
    reason: str | None = typer.Option(None, "--reason", help="Optional rejection reason applied to every candidate."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    """Reject every active candidate in a batch (activation.md §6.14.15 / O-21).

    Batch judgment is the operator's real surface for a returning-from-PTO
    backlog: a filtered bulk-reject keeps the time-motion ROI budget intact
    when ~20% of facts need rejection. Each rejection writes its own audit
    event + triage decision (with telemetry); rejections are terminal, so no
    outbox enqueue or projection is needed.
    """
    candidates = active_candidates(program, programs_root=programs_root, batch_id=batch_id)
    if not candidates:
        typer.echo(f"Batch {batch_id} has no active candidates to reject.")
        return
    rejected = 0
    total = len(candidates)
    for candidate in candidates:
        _write_candidate_audit_event(
            candidate,
            actor=actor,
            event_type="discovery.candidate_rejected.v1",
            payload={
                "candidate_id": candidate.candidate_id,
                "triage_actor": actor,
                "batch_id": batch_id,
                **({"reason": reason} if reason else {}),
            },
            programs_root=programs_root,
        )
        append_triage_decision(
            CandidateDecisionRecord(
                candidate_id=candidate.candidate_id,
                kind="rejected",
                decided_at=datetime.now(timezone.utc),
                triage_actor=actor,
                batch_id=batch_id,
                reason=reason,
            ),
            program_id=program,
            programs_root=programs_root,
            staged_at=candidate.staged_at,
        )
        rejected += 1
    typer.echo(f"Rejected {rejected} of {total} candidates from batch {batch_id}.")


@app.command("quarantine-batch")
def quarantine_batch(
    program: str = typer.Option(..., "--program", "-p", help="Program ID to mutate."),
    batch_id: str = typer.Option(..., "--batch-id", help="Batch ID to quarantine."),
    actor: str = typer.Option(..., "--actor", help="Operator quarantining the batch."),
    reason: str = typer.Option(..., "--reason", help="Reason for quarantining the batch."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    _quarantine_batch(program, batch_id=batch_id, actor=actor, reason=reason, programs_root=programs_root)


def _quarantine_batch(
    program: str,
    *,
    batch_id: str,
    actor: str,
    reason: str,
    programs_root: Path,
) -> None:
    pending_candidates = _batch_candidates(program, batch_id, programs_root=programs_root)
    if not pending_candidates:
        typer.echo(f"Unknown batch '{batch_id}'.")
        raise typer.Exit(code=3)
    decisions = load_triage_decisions(program, programs_root=programs_root)
    if any(decision.batch_id == batch_id and decision.kind == "approved" for decision in decisions):
        typer.echo(
            f"Batch '{batch_id}' already contains approved candidates; use tombstone corrections for post-approval cleanup."
        )
        raise typer.Exit(code=3)
    active_batch_candidates = active_candidates(program, programs_root=programs_root, batch_id=batch_id)
    if not active_batch_candidates:
        typer.echo(f"Batch {batch_id} already has no active candidates.")
        return
    for candidate in active_batch_candidates:
        append_triage_decision(
            CandidateDecisionRecord(
                candidate_id=candidate.candidate_id,
                kind="rejected",
                decided_at=datetime.now(timezone.utc),
                triage_actor=actor,
                batch_id=batch_id,
                reason=f"quarantined: {reason}",
            ),
            program_id=program,
            programs_root=programs_root,
        )
    typer.echo(f"Quarantined {len(active_batch_candidates)} candidates from batch {batch_id}.")


@app.command("history")
def history(
    program: str = typer.Option(..., "--program", "-p", help="Program ID to inspect."),
    entity: str = typer.Option(..., "--entity", help="Entity ID to show timeline for."),
    format: str = typer.Option("text", "--format", help="Output format: text or json."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    event_log = read_events(program, programs_root=programs_root)
    events_by_id = {event.event_id: event for event in event_log}
    indexed_by_id = {record.event_id: record for record in load_indexed_events(program, programs_root=programs_root)}
    projection = project_events_to_memory(program, event_log)
    orphaned_by = collapse_orphan_links(projection.get("event_orphan_links", []))
    shadowed_by = collapse_shadow_links(projection.get("event_shadow_links", []))
    entity_event_ids = load_entity_event_ids(program, programs_root=programs_root).get(entity, ())
    timeline = [events_by_id[event_id] for event_id in entity_event_ids if event_id in events_by_id]
    timeline.sort(key=lambda event: (event.occurred_at, event.event_id))
    payload: dict[str, Any] = {
        "program_id": program,
        "entity_id": entity,
        "events": [
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "occurred_at": event.occurred_at.isoformat(),
                "recorded_at": event.recorded_at.isoformat(),
                "actor": event.actor,
                "confidence": event.confidence.value,
                "temporal_confidence": event.temporal_confidence.value,
                "source_ref_type": event.source_ref.ref_type,
                "source_document_key": source_document_key(event.source_ref),
                "orphaned_by": orphaned_by.get(event.event_id),
                "shadowed_by": shadowed_by.get(event.event_id),
                "superseded_by": indexed_by_id[event.event_id].superseded_by if event.event_id in indexed_by_id else None,
            }
            for event in timeline
        ],
    }
    normalized_format = format.strip().lower()
    if normalized_format == "json":
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    if normalized_format != "text":
        raise typer.BadParameter("--format must be 'text' or 'json'.")
    typer.echo(f"LEDGER HISTORY {entity}")
    if not timeline:
        typer.echo("No events found.")
        return
    for row in payload["events"]:
        orphan_suffix = f" orphaned_by={row['orphaned_by']}" if row["orphaned_by"] else ""
        shadow_suffix = f" shadowed_by={row['shadowed_by']}" if row["shadowed_by"] else ""
        superseded_suffix = f" superseded_by={row['superseded_by']}" if row["superseded_by"] else ""
        typer.echo(
            f"- {row['occurred_at']} {row['event_id']} {row['event_type']} "
            f"actor={row['actor']} confidence={row['confidence']} source={row['source_document_key']}"
            f"{orphan_suffix}{shadow_suffix}{superseded_suffix}"
        )


@app.command("gaps")
def gaps(
    program: str = typer.Option(..., "--program", "-p", help="Program ID to inspect."),
    unacknowledged_only: bool = typer.Option(True, "--unacknowledged-only/--all", help="Show only unacknowledged gaps by default."),
    ack: str | None = typer.Option(None, "--ack", help="Acknowledge a specific gap event ID before listing."),
    actor: str | None = typer.Option(None, "--actor", help="Actor acknowledging the gap when --ack is used."),
    format: str = typer.Option("text", "--format", help="Output format: text or json."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    if ack is not None:
        if actor is None or not actor.strip():
            raise typer.BadParameter("--actor is required with --ack.")
        append_triage_decision(
            CandidateDecisionRecord(
                candidate_id=f"gap:{ack}",
                kind="gap_acknowledged",
                decided_at=datetime.now(timezone.utc),
                triage_actor=actor.strip(),
                gap_event_id=ack,
            ),
            program_id=program,
            programs_root=programs_root,
        )
        project_program_events(program, programs_root=programs_root)
    rows = _load_gap_rows(program, programs_root=programs_root)
    if unacknowledged_only:
        rows = [row for row in rows if not row.get("acknowledged")]
    payload = {"program_id": program, "gaps": rows}
    normalized_format = format.strip().lower()
    if normalized_format == "json":
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    if normalized_format != "text":
        raise typer.BadParameter("--format must be 'text' or 'json'.")
    typer.echo(f"LEDGER GAPS {program}")
    if not rows:
        typer.echo("No gaps found.")
        return
    for row in rows:
        typer.echo(
            f"- {row['event_id']} pipeline={row['pipeline']} gap_kind={row['gap_kind']} "
            f"acknowledged={bool(row['acknowledged'])} detail={row['detail']}"
        )


@app.command("replay")
def replay(
    program: str = typer.Option(..., "--program", "-p", help="Program ID to rebuild."),
    as_of: str | None = typer.Option(None, "--as-of", help="Optional occurred_at cutoff (ISO-8601)."),
    knowledge_as_of: str | None = typer.Option(None, "--knowledge-as-of", help="Optional recorded_at cutoff (ISO-8601)."),
    reindex: bool = typer.Option(False, "--reindex", help="Rebuild the derived event index before projection replay."),
    family: list[str] | None = typer.Option(
        None,
        "--family",
        help=(
            "Selective bridge re-projection: re-run the fact-store bridge only for "
            "events whose fact_family matches. Repeatable (e.g. --family milestone "
            "--family risk). When omitted the full ledger projection is rebuilt."
        ),
    ),
    format: str = typer.Option("text", "--format", help="Output format: text or json."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    parsed_as_of = _parse_cli_datetime(as_of, option_name="--as-of")
    parsed_knowledge_as_of = _parse_cli_datetime(knowledge_as_of, option_name="--knowledge-as-of")

    if family:
        # Selective family bridge replay: read ledger events, re-bridge only
        # the requested fact families.  The ledger projection (SQLite event
        # view) is NOT rebuilt — non-selected families are untouched.
        selected = frozenset(f.strip().lower() for f in family)
        bridge_counts = _replay_bridge_for_families(
            program,
            selected_families=selected,
            as_of=parsed_as_of,
            knowledge_as_of=parsed_knowledge_as_of,
            programs_root=programs_root,
        )
        payload: dict[str, object] = {
            "program_id": program,
            "mode": "selective_bridge_replay",
            "selected_families": sorted(selected),
            "bridge_counts": bridge_counts,
            "total_replayed": sum(bridge_counts.values()),
        }
        normalized_format = format.strip().lower()
        if normalized_format == "json":
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
            return
        if normalized_format != "text":
            raise typer.BadParameter("--format must be 'text' or 'json'.")
        typer.echo(f"Selective bridge replay for {program} (families: {', '.join(sorted(selected))})")
        for fam, count in sorted(bridge_counts.items()):
            typer.echo(f"  {fam}: {count} events re-bridged")
        typer.echo(f"total: {sum(bridge_counts.values())} events re-bridged")
        return

    indexed_count = None
    if reindex:
        indexed_count = rebuild_event_index(program, programs_root=programs_root)
    events = read_events(program, programs_root=programs_root)
    result = project_events_incremental_to_sqlite(
        program,
        events,
        projection_path=get_current_projection_path(program, programs_root=programs_root),
        programs_root=programs_root,
        as_of=parsed_as_of,
        knowledge_as_of=parsed_knowledge_as_of,
    )
    full_payload = {
        "program_id": program,
        "projection_path": str(result.projection_path),
        "event_watermark": result.event_watermark,
        "event_count": result.event_count,
        "coverage_earliest": result.coverage_earliest,
        "coverage_latest": result.coverage_latest,
        "reindexed_event_count": indexed_count,
    }
    normalized_format = format.strip().lower()
    if normalized_format == "json":
        typer.echo(json.dumps(full_payload, indent=2, sort_keys=True))
        return
    if normalized_format != "text":
        raise typer.BadParameter("--format must be 'text' or 'json'.")
    typer.echo(f"Replayed ledger for {program} -> {result.projection_path}")
    typer.echo(f"event_count={result.event_count} event_watermark={result.event_watermark}")
    if indexed_count is not None:
        typer.echo(f"reindexed_event_count={indexed_count}")


@app.command("verify")
def verify(
    program: str = typer.Option(..., "--program", "-p", help="Program ID to inspect."),
    deep: bool = typer.Option(False, "--deep", help="Also compare current projection to a fresh replay dump."),
    format: str = typer.Option("text", "--format", help="Output format: text or json."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    log_verification = verify_event_log(program, programs_root=programs_root)
    events = read_events(program, programs_root=programs_root)
    indexed = load_indexed_events(program, programs_root=programs_root)
    event_ids = {event.event_id for event in events}
    indexed_ids = {record.event_id for record in indexed}
    missing_from_index = tuple(sorted(event_ids - indexed_ids))
    extra_in_index = tuple(sorted(indexed_ids - event_ids))
    deep_projection_match = None
    evidence_vault_issues: list[dict[str, str]] = []
    knowledge_vault_issues: list[dict[str, object]] = []
    if deep:
        current_projection_path = get_current_projection_path(program, programs_root=programs_root)
        if current_projection_path.exists():
            with tempfile.TemporaryDirectory() as temp_dir:
                fresh_path = Path(temp_dir) / "projection.sqlite3"
                project_events_to_sqlite(program, events, projection_path=fresh_path)
                deep_projection_match = canonical_projection_dump(current_projection_path) == canonical_projection_dump(fresh_path)
        else:
            deep_projection_match = False
        for vault_hash, ref_owner_id, ref_owner_type, ref_role in load_vault_refs(program, programs_root=programs_root):
            if ref_owner_type != "event":
                continue
            status = evidence_vault_entry_status(program_id=program, vault_hash=vault_hash, programs_root=programs_root)
            if status == "ok":
                continue
            evidence_vault_issues.append(
                {
                    "vault_hash": vault_hash,
                    "ref_owner_id": ref_owner_id,
                    "ref_role": ref_role,
                    "kind": status,
                }
            )
        knowledge_vault_issues = summarize_knowledge_vault_integrity(programs_root=programs_root).issue_records()
        write_shared_vault_verify_status(
            verified_at=datetime.now(timezone.utc),
            ok=not knowledge_vault_issues,
            issue_records=knowledge_vault_issues,
            programs_root=programs_root,
            program_id=program,
        )
    ok = log_verification.ok and not missing_from_index and not extra_in_index and (deep_projection_match in (None, True)) and not evidence_vault_issues and not knowledge_vault_issues
    verified_at = datetime.now(timezone.utc)
    payload = {
        "program_id": program,
        "ok": ok,
        "checked_event_count": log_verification.checked_event_count,
        "log_issues": list(log_verification.issues),
        "missing_from_index": list(missing_from_index),
        "extra_in_index": list(extra_in_index),
        "deep_projection_match": deep_projection_match,
        "evidence_vault_issues": evidence_vault_issues,
        "knowledge_vault_issues": knowledge_vault_issues,
    }
    write_ledger_verify_status(
        program,
        verified_at=verified_at,
        ok=ok,
        deep=deep,
        checked_event_count=log_verification.checked_event_count,
        programs_root=programs_root,
    )
    exit_code = _verify_exit_code(
        ok=ok,
        evidence_vault_issues=evidence_vault_issues,
        knowledge_vault_issues=knowledge_vault_issues,
    )
    normalized_format = format.strip().lower()
    if normalized_format == "json":
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        raise typer.Exit(code=exit_code)
    if normalized_format != "text":
        raise typer.BadParameter("--format must be 'text' or 'json'.")
    typer.echo(f"LEDGER VERIFY {program}")
    typer.echo(f"ok={ok} checked_event_count={log_verification.checked_event_count}")
    for issue in log_verification.issues:
        typer.echo(f"- log_issue={issue}")
    if missing_from_index:
        typer.echo(f"- missing_from_index={', '.join(missing_from_index)}")
    if extra_in_index:
        typer.echo(f"- extra_in_index={', '.join(extra_in_index)}")
    if deep_projection_match is not None:
        typer.echo(f"- deep_projection_match={deep_projection_match}")
    for vault_issue in evidence_vault_issues:
        typer.echo(f"- evidence_vault_issue kind={vault_issue['kind']} vault_hash={vault_issue['vault_hash']} owner={vault_issue['ref_owner_id']} role={vault_issue['ref_role']}")
    for k_vault_issue in knowledge_vault_issues:
        typer.echo(f"- knowledge_vault_issue kind={k_vault_issue['kind']} count={k_vault_issue['count']}")
    raise typer.Exit(code=exit_code)


def _verify_exit_code(
    *,
    ok: bool,
    evidence_vault_issues: list[dict[str, str]],
    knowledge_vault_issues: list[dict[str, object]],
) -> int:
    if ok:
        return 0
    if _has_vault_corruption_issue(
        evidence_vault_issues=evidence_vault_issues,
        knowledge_vault_issues=knowledge_vault_issues,
    ):
        return _VERIFY_VAULT_CORRUPTION_EXIT_CODE
    return 1


def _has_vault_corruption_issue(
    *,
    evidence_vault_issues: list[dict[str, str]],
    knowledge_vault_issues: list[dict[str, object]],
) -> bool:
    if any(issue.get("kind") in {"missing", "hash_mismatch"} for issue in evidence_vault_issues):
        return True
    return any(issue.get("kind") in {"missing_metadata", "hash_mismatch"} for issue in knowledge_vault_issues)


@app.command("diff")
def diff(
    program: str = typer.Option(..., "--program", "-p", help="Program ID to inspect."),
    from_as_of: str = typer.Option(..., "--from", help="Earlier occurred_at cutoff (ISO-8601)."),
    to_as_of: str = typer.Option(..., "--to", help="Later occurred_at cutoff (ISO-8601)."),
    format: str = typer.Option("text", "--format", help="Output format: text or json."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    parsed_from_as_of = _parse_cli_datetime(from_as_of, option_name="--from")
    parsed_to_as_of = _parse_cli_datetime(to_as_of, option_name="--to")
    events = read_events(program, programs_root=programs_root)
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        before_path = temp_root / "before.sqlite3"
        after_path = temp_root / "after.sqlite3"
        project_events_to_sqlite(program, events, projection_path=before_path, as_of=parsed_from_as_of)
        project_events_to_sqlite(program, events, projection_path=after_path, as_of=parsed_to_as_of)
        before_dump = canonical_projection_dump(before_path)
        after_dump = canonical_projection_dump(after_path)
    payload: dict[str, Any] = {
        "program_id": program,
        "from": parsed_from_as_of.isoformat() if parsed_from_as_of is not None else None,
        "to": parsed_to_as_of.isoformat() if parsed_to_as_of is not None else None,
        "tables": _summarize_projection_diff(before_dump, after_dump),
    }
    normalized_format = format.strip().lower()
    if normalized_format == "json":
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    if normalized_format != "text":
        raise typer.BadParameter("--format must be 'text' or 'json'.")
    typer.echo(f"LEDGER DIFF {program} from={payload['from']} to={payload['to']}")
    for table in payload["tables"]:
        typer.echo(
            f"- {table['table']} before={table['before_count']} after={table['after_count']} changed={table['changed']}"
        )


@app.command("export")
def export(
    program: str = typer.Option(..., "--program", "-p", help="Program ID to export."),
    format: str = typer.Option(..., "--format", help="Export format: jsonl or sqlite."),
    out: Path = typer.Option(..., "--out", help="Destination path."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    normalized_format = format.strip().lower()
    out.parent.mkdir(parents=True, exist_ok=True)
    if normalized_format == "jsonl":
        events = read_events(program, programs_root=programs_root)
        out.write_text("".join(canonical_json(event.to_dict()) + "\n" for event in events), encoding="utf-8")
        typer.echo(f"Exported {len(events)} events to {out}")
        return
    if normalized_format == "sqlite":
        projection_path = get_current_projection_path(program, programs_root=programs_root)
        if not projection_path.exists():
            project_program_events(program, programs_root=programs_root)
            projection_path = get_current_projection_path(program, programs_root=programs_root)
        _sqlite_copy(projection_path, out)
        typer.echo(f"Exported projection sqlite to {out}")
        return
    raise typer.BadParameter("--format must be 'jsonl' or 'sqlite'.")


@app.command("import")
def import_command(
    program: str = typer.Option(..., "--program", "-p", help="Program ID to stage into."),
    source: Path = typer.Option(..., "--source", help="Source JSONL file to import into the candidate queue."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview staged candidates without writing pending.jsonl."),
    sample_limit: int = typer.Option(3, "--sample-limit", min=1, help="How many sample candidates to print in dry-run output."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    if not source.exists():
        raise typer.BadParameter(f"Source file not found: {source}")
    batch_id = _fresh_batch_id()
    candidates: list[CandidateEvent] = []
    raw_lines = source.read_text(encoding="utf-8").splitlines()
    started_at = monotonic()
    last_report_at = started_at
    for index, raw_line in enumerate(raw_lines, start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        candidate = _candidate_from_import_line(stripped, program=program, batch_id=batch_id)
        candidates.append(candidate)
        now = monotonic()
        if len(candidates) % 10 == 0 or now - last_report_at >= 5:
            typer.echo(_render_import_progress(len(candidates), len(raw_lines), started_at, now))
            last_report_at = now
    if not candidates:
        typer.echo("No candidates found in source JSONL.")
        raise typer.Exit(code=3)
    if dry_run:
        typer.echo(f"DRY-RUN: would stage {len(candidates)} candidates in batch {batch_id}.")
        for sample in candidates[:sample_limit]:
            typer.echo(f"- sample {sample.candidate_id} {sample.proposed_event_type} occurred_at={sample.proposed_occurred_at.isoformat()}")
        raise typer.Exit(code=0)
    for candidate in candidates:
        append_candidate(candidate, programs_root=programs_root)
    typer.echo(f"Staged {len(candidates)} candidates in batch {batch_id}.")
    typer.echo(
        f"Review with: vertex ledger triage list --program {program} --batch-id {batch_id}"
    )


@app.command("redact")
def ledger_redact(
    program: str = typer.Option(..., "--program", "-p", help="Program ID."),
    event_id: str = typer.Option(..., "--event-id", help="Event ID to redact."),
    reason: str = typer.Option(..., "--reason", help="Compliance reason for redaction."),
    actor: str = typer.Option(..., "--actor", help="Operator performing the redaction."),
    scrub_field: list[str] | None = typer.Option(None, "--scrub-field", help="Source field to blank in source_ref/corroborating_refs (repeatable)."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    """Redact a single ledger event payload in-place (§10.8 compliance redaction).

    The event envelope is preserved; only the payload is replaced with {redacted: true}.
    Hash-chain continuity is maintained via the .redactions.jsonl registry.
    This is the ONLY physical mutation allowed in the ledger subsystem.
    """
    existing = load_redaction_registry(program, programs_root=programs_root)
    if event_id in existing:
        typer.echo(f"Event {event_id!r} is already redacted.")
        return
    scrub: tuple[str, ...] | None = tuple(scrub_field) if scrub_field else None
    record = redact_event(
        program,
        event_id,
        actor=actor,
        reason=reason,
        scrub_source_fields=scrub,
        programs_root=programs_root,
    )
    if record is None:
        typer.echo(f"Event {event_id!r} not found in program {program!r}.")
        raise typer.Exit(code=3)
    typer.echo(f"Redacted event {record.event_id}; original_hash={record.original_envelope_hash}.")


@app.command("redact-vault")
def ledger_redact_vault(
    program: str = typer.Option(..., "--program", "-p", help="Program ID."),
    vault_hash: str = typer.Option(..., "--vault-hash", help="Evidence vault hash to destroy and cascade-redact."),
    reason: str = typer.Option(..., "--reason", help="Compliance reason for vault redaction."),
    actor: str = typer.Option(..., "--actor", help="Operator performing the redaction."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    """Destroy a ledger evidence vault entry and cascade-redact all referencing events (§10.8).

    Deletes the vault content + metadata files, then redacts every ledger event
    whose source_ref or corroborating_refs cite this vault_hash.
    """
    content_path, _ = evidence_vault_paths(program_id=program, vault_hash=vault_hash, programs_root=programs_root)
    if not content_path.exists():
        typer.echo(f"Vault entry {vault_hash!r} not found for program {program!r}.")
        raise typer.Exit(code=3)

    refs = load_vault_refs(program, programs_root=programs_root)
    event_ids_to_redact = [
        ref_owner_id
        for vh, ref_owner_id, ref_owner_type, _ref_role in refs
        if vh == vault_hash and ref_owner_type == "event"
    ]

    delete_evidence_vault_entry(program_id=program, vault_hash=vault_hash, programs_root=programs_root)

    redacted = 0
    already_done = 0
    for eid in event_ids_to_redact:
        try:
            record = redact_event(
                program,
                eid,
                actor=actor,
                reason=f"vault redacted: {reason}",
                programs_root=programs_root,
            )
            if record is not None:
                redacted += 1
        except ValueError:
            already_done += 1

    summary = f"Deleted vault {vault_hash}; events_redacted={redacted}"
    if already_done:
        summary += f" already_redacted={already_done}"
    typer.echo(summary + ".")


@app.command("backfill")
def backfill(
    program: str = typer.Option(..., "--program", "-p", help="Program ID to stage into."),
    source_dir: Path | None = typer.Option(None, "--source-dir", help="Root directory containing backfill source files."),
    quarantine_batch: str | None = typer.Option(None, "--quarantine-batch", help="Quarantine a previously staged backfill batch instead of staging a new one."),
    actor: str | None = typer.Option(None, "--actor", help="Operator name for --quarantine-batch."),
    reason: str | None = typer.Option(None, "--reason", help="Reason for --quarantine-batch."),
    from_year: int | None = typer.Option(None, "--from", help="Optional starting year for recursive Tier-A enumeration."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview staged candidates without writing pending.jsonl."),
    sample_limit: int = typer.Option(3, "--sample-limit", min=1, help="How many sample candidates to print in dry-run output."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    if quarantine_batch is not None:
        if source_dir is not None:
            raise typer.BadParameter("--source-dir cannot be combined with --quarantine-batch.")
        if from_year is not None:
            raise typer.BadParameter("--from cannot be combined with --quarantine-batch.")
        if dry_run:
            raise typer.BadParameter("--dry-run cannot be combined with --quarantine-batch.")
        if actor is None or reason is None:
            raise typer.BadParameter("--actor and --reason are required with --quarantine-batch.")
        _quarantine_batch(program, batch_id=quarantine_batch, actor=actor, reason=reason, programs_root=programs_root)
        return
    if source_dir is None:
        raise typer.BadParameter("--source-dir is required unless --quarantine-batch is used.")
    if not source_dir.exists() or not source_dir.is_dir():
        raise typer.BadParameter(f"Source directory not found: {source_dir}")
    batch_id = _fresh_batch_id()
    candidates = _build_lt_deck_backfill_candidates(program, source_dir=source_dir, from_year=from_year, batch_id=batch_id)
    if not candidates:
        typer.echo("No dated LT deck files matched the requested backfill window.")
        raise typer.Exit(code=3)
    if dry_run:
        typer.echo(f"DRY-RUN: would stage {len(candidates)} LT deck candidates in batch {batch_id}.")
        for sample in candidates[:sample_limit]:
            typer.echo(f"- sample {sample.candidate_id} {sample.proposed_payload['title']} occurred_at={sample.proposed_occurred_at.isoformat()}")
        raise typer.Exit(code=0)
    for candidate in candidates:
        append_candidate(candidate, programs_root=programs_root)
    typer.echo(f"Staged {len(candidates)} LT deck candidates in batch {batch_id}.")
    typer.echo(f"Review with: vertex ledger triage list --program {program} --batch-id {batch_id}")


@triage_app.command("approve")
def triage_approve(
    program: str = typer.Option(..., "--program", "-p", help="Program ID to mutate."),
    candidate_id: str = typer.Option(..., "--candidate", help="Candidate ID to approve."),
    actor: str = typer.Option(..., "--actor", help="Operator approving the candidate."),
    override_lock: bool = typer.Option(False, "--override-lock", help="Temporarily unlock and relock a single conflicting field for this approval."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    candidate = _require_candidate(program, candidate_id, programs_root=programs_root)
    _ensure_candidate_triageable(program, candidate_id, programs_root=programs_root)
    _enforce_rev_verification_gate(program, candidate_id, programs_root=programs_root)
    lock_conflicts = _candidate_lock_conflict_rows(candidate, programs_root=programs_root)
    if lock_conflicts and not override_lock:
        typer.echo(f"Candidate {candidate.candidate_id} touches a locked field; rerun with --override-lock.")
        raise typer.Exit(code=3)
    resulting_event = (
        _write_candidate_event_with_lock_override(
            candidate,
            actor=actor,
            programs_root=programs_root,
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        )
        if override_lock
        else _write_candidate_event(
            candidate,
            actor=actor,
            programs_root=programs_root,
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        )
    )
    audit_event = _write_candidate_audit_event(
        candidate,
        actor=actor,
        event_type="discovery.candidate_approved.v1",
        payload={
            "candidate_id": candidate.candidate_id,
            "resulting_event_id": resulting_event.event_id,
            "triage_actor": actor,
            "edited": False,
        },
        programs_root=programs_root,
    )
    append_triage_decision(
        CandidateDecisionRecord(
            candidate_id=candidate.candidate_id,
            kind="approved",
            decided_at=datetime.now(timezone.utc),
            triage_actor=actor,
            batch_id=candidate.batch_id,
            edited=False,
            resulting_event_id=resulting_event.event_id,
            approval_event_id=audit_event.event_id,
        ),
        program_id=program,
        programs_root=programs_root,
    )
    # S-1: write outbox entry before projection (durability — if projection crashes, entry survives)
    outbox_id = str(uuid.uuid4())
    db_dir = get_candidate_db_dir(program, programs_root=programs_root)
    init_candidate_db(db_dir)  # ensure projection_outbox exists on pre-S-1 databases
    now_iso = datetime.now(timezone.utc).isoformat()
    outbox_enqueue(
        db_dir,
        outbox_id=outbox_id,
        candidate_id=candidate.candidate_id,
        program_id=program,
        source_event_id=resulting_event.event_id,
        enqueued_at=now_iso,
    )
    try:
        project_program_events(program, programs_root=programs_root)
        outbox_mark_projected(db_dir, outbox_id=outbox_id, projected_at=datetime.now(timezone.utc).isoformat())
    except Exception as exc:
        new_status = outbox_mark_failed(
            db_dir,
            outbox_id=outbox_id,
            attempted_at=datetime.now(timezone.utc).isoformat(),
            failure_reason=str(exc),
        )
        if new_status == OUTBOX_STATUS_DEAD_LETTER:
            typer.echo(f"[outbox] Projection failed after max retries — dead-letter: {outbox_id}. Run `vertex ledger doctor` to diagnose.", err=True)
        else:
            typer.echo(f"[outbox] Projection failed (will retry): {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Approved {candidate.candidate_id} -> {resulting_event.event_id}")


@triage_app.command("edit")
def triage_edit(
    program: str = typer.Option(..., "--program", "-p", help="Program ID to mutate."),
    candidate_id: str = typer.Option(..., "--candidate", help="Candidate ID to edit and approve."),
    actor: str = typer.Option(..., "--actor", help="Operator editing the candidate."),
    payload_json: str = typer.Option(..., "--payload-json", help="Replacement JSON object payload for the resulting event."),
    occurred_at: str | None = typer.Option(None, "--occurred-at", help="Optional replacement occurred-at timestamp (ISO-8601)."),
    reason: str | None = typer.Option(None, "--reason", help="Optional edit rationale."),
    override_lock: bool = typer.Option(False, "--override-lock", help="Temporarily unlock and relock a single conflicting field for this approval."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    candidate = _require_candidate(program, candidate_id, programs_root=programs_root)
    _ensure_candidate_triageable(program, candidate_id, programs_root=programs_root)
    _enforce_rev_verification_gate(program, candidate_id, programs_root=programs_root)
    edited_payload = _parse_json_mapping(payload_json, option_name="--payload-json")
    validate_event_payload(candidate.proposed_event_type, edited_payload)
    edited_occurred_at = _parse_cli_datetime(occurred_at, option_name="--occurred-at") or candidate.proposed_occurred_at
    lock_conflicts = _candidate_lock_conflict_rows(candidate, programs_root=programs_root, payload=edited_payload)
    if lock_conflicts and not override_lock:
        typer.echo(f"Candidate {candidate.candidate_id} touches a locked field; rerun with --override-lock.")
        raise typer.Exit(code=3)
    resulting_event = (
        _write_candidate_event_with_lock_override(
            candidate,
            actor=actor,
            programs_root=programs_root,
            payload=edited_payload,
            occurred_at=edited_occurred_at,
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        )
        if override_lock
        else _write_candidate_event(
            candidate,
            actor=actor,
            programs_root=programs_root,
            payload=edited_payload,
            occurred_at=edited_occurred_at,
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        )
    )
    audit_event = _write_candidate_audit_event(
        candidate,
        actor=actor,
        event_type="discovery.candidate_approved.v1",
        payload={
            "candidate_id": candidate.candidate_id,
            "resulting_event_id": resulting_event.event_id,
            "triage_actor": actor,
            "edited": True,
        },
        programs_root=programs_root,
    )
    append_triage_decision(
        CandidateDecisionRecord(
            candidate_id=candidate.candidate_id,
            kind="approved",
            decided_at=datetime.now(timezone.utc),
            triage_actor=actor,
            batch_id=candidate.batch_id,
            reason=reason,
            edited=True,
            resulting_event_id=resulting_event.event_id,
            approval_event_id=audit_event.event_id,
        ),
        program_id=program,
        programs_root=programs_root,
    )
    project_program_events(program, programs_root=programs_root)
    typer.echo(f"Edited {candidate.candidate_id} -> {resulting_event.event_id}")


@triage_app.command("reject")
def triage_reject(
    program: str = typer.Option(..., "--program", "-p", help="Program ID to mutate."),
    candidate_id: str = typer.Option(..., "--candidate", help="Candidate ID to reject."),
    actor: str = typer.Option(..., "--actor", help="Operator rejecting the candidate."),
    reason: str | None = typer.Option(None, "--reason", help="Optional rejection reason."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    candidate = _require_candidate(program, candidate_id, programs_root=programs_root)
    _ensure_candidate_triageable(program, candidate_id, programs_root=programs_root)
    _write_candidate_audit_event(
        candidate,
        actor=actor,
        event_type="discovery.candidate_rejected.v1",
        payload={
            "candidate_id": candidate.candidate_id,
            "triage_actor": actor,
            **({"reason": reason} if reason else {}),
        },
        programs_root=programs_root,
    )
    append_triage_decision(
        CandidateDecisionRecord(
            candidate_id=candidate.candidate_id,
            kind="rejected",
            decided_at=datetime.now(timezone.utc),
            triage_actor=actor,
            batch_id=candidate.batch_id,
            reason=reason,
        ),
        program_id=program,
        programs_root=programs_root,
    )
    typer.echo(f"Rejected {candidate.candidate_id}")


@triage_app.command("revoke")
def triage_revoke(
    program: str = typer.Option(..., "--program", "-p", help="Program ID to mutate."),
    candidate_id: str = typer.Option(..., "--candidate", help="Approved candidate ID to revoke."),
    actor: str = typer.Option(..., "--actor", help="Operator revoking the approved candidate."),
    reason: str = typer.Option(..., "--reason", help="Revocation reason."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    candidate = _require_candidate(program, candidate_id, programs_root=programs_root)
    latest = _latest_candidate_decisions(program, programs_root=programs_root).get(candidate_id)
    if latest is None or latest.kind != "approved" or not latest.resulting_event_id:
        typer.echo(f"Candidate {candidate_id} does not have an approved event to revoke.")
        raise typer.Exit(code=3)
    target_event = _event_by_id(program, latest.resulting_event_id, programs_root=programs_root)
    if target_event is None:
        typer.echo(f"Approved event {latest.resulting_event_id} was not found.")
        raise typer.Exit(code=3)

    now = datetime.now(timezone.utc)
    revocation = build_event_envelope(
        program_id=program,
        event_type="operator.correction.v1",
        occurred_at=now,
        recorded_at=now,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor=actor,
        payload={
            "corrects_event_id": target_event.event_id,
            "corrected_payload": None,
            "reason": f"triage revoke {candidate.candidate_id}: {reason}",
        },
        source_ref=_operator_assertion(actor, f"ledger triage revoke {candidate.candidate_id}", now),
        dedupe_payload={"corrects_event_id": target_event.event_id},
    )
    revocation_event = _persist_event(revocation, programs_root=programs_root)
    audit_event = _write_candidate_audit_event(
        candidate,
        actor=actor,
        event_type="discovery.candidate_revoked.v1",
        payload={
            "candidate_id": candidate.candidate_id,
            "resulting_event_id": target_event.event_id,
            "revocation_event_id": revocation_event.event_id,
            "triage_actor": actor,
            "reason": reason,
            **({"approval_event_id": latest.approval_event_id} if latest.approval_event_id else {}),
        },
        programs_root=programs_root,
    )
    append_triage_decision(
        CandidateDecisionRecord(
            candidate_id=candidate.candidate_id,
            kind="revoked",
            decided_at=now,
            triage_actor=actor,
            batch_id=candidate.batch_id,
            reason=reason,
            edited=latest.edited,
            resulting_event_id=revocation_event.event_id,
            approval_event_id=audit_event.event_id,
        ),
        program_id=program,
        programs_root=programs_root,
    )
    project_program_events(program, programs_root=programs_root)
    typer.echo(f"Revoked {candidate.candidate_id}: {target_event.event_id} -> {revocation_event.event_id}")


@triage_app.command("skip")
def triage_skip(
    program: str = typer.Option(..., "--program", "-p", help="Program ID to mutate."),
    candidate_id: str = typer.Option(..., "--candidate", help="Candidate ID to skip for now."),
    actor: str = typer.Option(..., "--actor", help="Operator skipping the candidate."),
    reason: str | None = typer.Option(None, "--reason", help="Optional skip reason."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    candidate = _require_candidate(program, candidate_id, programs_root=programs_root)
    _ensure_candidate_triageable(program, candidate_id, programs_root=programs_root)
    append_triage_decision(
        CandidateDecisionRecord(
            candidate_id=candidate.candidate_id,
            kind="skipped",
            decided_at=datetime.now(timezone.utc),
            triage_actor=actor,
            batch_id=candidate.batch_id,
            reason=reason,
        ),
        program_id=program,
        programs_root=programs_root,
    )
    typer.echo(f"Skipped {candidate.candidate_id}")


@triage_app.command("expire-skips")
def triage_expire_skips(
    program: str = typer.Option(..., "--program", "-p", help="Program ID to mutate."),
    actor: str = typer.Option("vertex.ledger.expire_skips", "--actor", help="Actor materializing expired skips."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    expired_candidates = _expired_skipped_candidates(program, programs_root=programs_root)
    if not expired_candidates:
        typer.echo("No expired skipped candidates to materialize.")
        return
    for candidate in expired_candidates:
        append_triage_decision(
            CandidateDecisionRecord(
                candidate_id=candidate.candidate_id,
                kind="rejected",
                decided_at=datetime.now(timezone.utc),
                triage_actor=actor,
                batch_id=candidate.batch_id,
                reason=f"skip expired after {SKIP_EXPIRY_DAYS} days",
            ),
            program_id=program,
            programs_root=programs_root,
        )
    typer.echo(f"Materialized {len(expired_candidates)} expired skipped candidate(s).")


# ---------------------------------------------------------------------------
# S-1: projection_outbox helpers (called from triage approve / batch-approve)
# ---------------------------------------------------------------------------

def _enqueue_outbox_entry(
    program_id: str,
    candidate_id: str,
    source_event_id: str,
    *,
    programs_root: Path,
) -> str:
    """Enqueue a pending outbox row; returns the new outbox_id."""
    db_dir = get_candidate_db_dir(program_id, programs_root=programs_root)
    init_candidate_db(db_dir)  # ensure projection_outbox exists on pre-S-1 databases
    outbox_id = str(uuid.uuid4())
    outbox_enqueue(
        db_dir,
        outbox_id=outbox_id,
        candidate_id=candidate_id,
        program_id=program_id,
        source_event_id=source_event_id,
        enqueued_at=datetime.now(timezone.utc).isoformat(),
    )
    return outbox_id


def _mark_pending_outbox_projected(db_dir: "Path", *, program_id: str) -> None:
    """Mark all pending outbox rows for ``program_id`` as projected (batch-approve fast path)."""
    from src.core.ledger.candidate_sqlite_store import outbox_list_pending, outbox_mark_projected  # noqa: PLC0415
    pending = outbox_list_pending(db_dir, program_id=program_id)
    projected_at = datetime.now(timezone.utc).isoformat()
    for row in pending:
        outbox_mark_projected(db_dir, outbox_id=row["outbox_id"], projected_at=projected_at)


def _mark_pending_outbox_failed(db_dir: "Path", *, program_id: str, reason: str) -> None:
    """Increment attempt counts for all pending outbox rows after a projection failure."""
    from src.core.ledger.candidate_sqlite_store import outbox_list_pending, outbox_mark_failed  # noqa: PLC0415
    pending = outbox_list_pending(db_dir, program_id=program_id)
    attempted_at = datetime.now(timezone.utc).isoformat()
    for row in pending:
        outbox_mark_failed(db_dir, outbox_id=row["outbox_id"], attempted_at=attempted_at, failure_reason=reason)


def _require_candidate(program: str, candidate_id: str, *, programs_root: Path) -> CandidateEvent:
    for candidate in load_pending_candidates(program, programs_root=programs_root):
        if candidate.candidate_id == candidate_id:
            return candidate
    raise typer.BadParameter(f"Unknown candidate '{candidate_id}'.")


def _ensure_candidate_triageable(program: str, candidate_id: str, *, programs_root: Path) -> None:
    latest = _latest_candidate_decisions(program, programs_root=programs_root).get(candidate_id)
    if latest is None:
        return
    if latest.kind == "skipped":
        return
    raise typer.BadParameter(f"Candidate '{candidate_id}' already has final decision '{latest.kind}'.")


def _rev_verification_gate_active(program: str, *, programs_root: Path) -> bool:
    """REV verification gate is active only under the ``rev_verified`` profile (§5.9).

    Under ``legacy_nl`` / ``search_hydrate`` the gate is a no-op so the existing
    triage flow is unchanged (backward compatible). The gate is also inactive
    when the program has no M365/REV config at all.
    """
    loaded = load_program(program, programs_root=programs_root)
    if loaded is None or loaded.m365 is None or loaded.m365.rev is None:
        return False
    rev = loaded.m365.rev
    return bool(getattr(rev, "is_rev_verified", False) and getattr(rev, "verification_gate_enabled", False))


def _enforce_rev_verification_gate(program: str, candidate_id: str, *, programs_root: Path) -> None:
    """FR-PCI-9 — block ``triage approve`` unless the candidate is verified (§5.9).

    Active only under ``rev_verified``. A candidate whose effective verification
    state is not ``human_verified`` / ``source_verified`` is rejected *before*
    ``_write_candidate_event`` + ``project_program_events()`` so no unverified
    event enters the ledger. The effective state + assertion count are echoed to
    the operator so the failure is actionable.
    """
    if not _rev_verification_gate_active(program, programs_root=programs_root):
        return
    if is_candidate_verified(program, candidate_id, programs_root=programs_root):
        return
    assertions = assertions_for_candidate(program, candidate_id, programs_root=programs_root)
    state = effective_verification_state(assertions)
    typer.echo(
        f"Candidate '{candidate_id}' is not verified (effective_state={state}, "
        f"assertions={len(assertions)}). REV verification gate (rev_verified) requires "
        "human_verified or source_verified before approval."
    )
    raise typer.Exit(code=7)


def _latest_candidate_decisions(program: str, *, programs_root: Path) -> dict[str, CandidateDecisionRecord]:
    latest: dict[str, CandidateDecisionRecord] = {}
    for decision in load_triage_decisions(program, programs_root=programs_root):
        latest[decision.candidate_id] = decision
    return latest


def _expired_skipped_candidates(program: str, *, programs_root: Path) -> tuple[CandidateEvent, ...]:
    now = datetime.now(timezone.utc)
    latest_decisions = _latest_candidate_decisions(program, programs_root=programs_root)
    expired: list[CandidateEvent] = []
    for candidate in load_pending_candidates(program, programs_root=programs_root):
        decision = latest_decisions.get(candidate.candidate_id)
        if decision is None or decision.kind != "skipped":
            continue
        if decision.decided_at + timedelta(days=SKIP_EXPIRY_DAYS) > now:
            continue
        expired.append(candidate)
    return tuple(expired)


_LEGACY_CONFIDENCE_MAP: dict[str, ConfidenceTier] = {
    "high": ConfidenceTier.SOURCE_AUTHORITATIVE,
    "medium": ConfidenceTier.AI_EXTRACTED,
    "low": ConfidenceTier.INFERRED,
}


def _coerce_confidence_tier(value: str) -> ConfidenceTier:
    if value in _LEGACY_CONFIDENCE_MAP:
        return _LEGACY_CONFIDENCE_MAP[value]
    return ConfidenceTier(value)


def _write_candidate_event(
    candidate: CandidateEvent,
    *,
    actor: str,
    programs_root: Path,
    payload: dict[str, object] | None = None,
    occurred_at: datetime | None = None,
    confidence: ConfidenceTier | None = None,
) -> EventEnvelope:
    schema = get_event_schema(candidate.proposed_event_type)
    effective_payload = payload if payload is not None else candidate.proposed_payload
    dedupe_payload = {
        field_name: effective_payload[field_name]
        for field_name in schema.dedupe_core_fields
        if field_name in effective_payload
    }
    envelope = build_event_envelope(
        program_id=candidate.program_id,
        event_type=candidate.proposed_event_type,
        occurred_at=occurred_at or candidate.proposed_occurred_at,
        recorded_at=datetime.now(timezone.utc),
        temporal_confidence=TemporalConfidence(candidate.proposed_temporal_confidence),
        confidence=confidence or _coerce_confidence_tier(candidate.proposed_confidence),
        actor=actor,
        payload=effective_payload,
        source_ref=candidate.source_ref,
        corroborating_refs=candidate.corroborating_refs,
        dedupe_payload=dedupe_payload,
    )
    return _persist_event(envelope, programs_root=programs_root)


def _write_candidate_event_with_lock_override(
    candidate: CandidateEvent,
    *,
    actor: str,
    programs_root: Path,
    payload: dict[str, object] | None = None,
    occurred_at: datetime | None = None,
    confidence: ConfidenceTier | None = None,
) -> EventEnvelope:
    effective_payload = payload if payload is not None else candidate.proposed_payload
    lock_conflicts = _candidate_lock_conflict_rows(candidate, programs_root=programs_root, payload=effective_payload)
    if not lock_conflicts:
        return _write_candidate_event(
            candidate,
            actor=actor,
            programs_root=programs_root,
            payload=effective_payload,
            occurred_at=occurred_at,
            confidence=confidence,
        )
    override_values = _override_lock_field_values(candidate, effective_payload, programs_root=programs_root)
    missing_fields = sorted(
        field_name
        for (_entity_id, field_name), _lock_row in lock_conflicts.items()
        if field_name not in override_values
    )
    if missing_fields:
        field_list = ", ".join(missing_fields)
        raise typer.BadParameter(f"--override-lock is not supported for {candidate.proposed_event_type} field(s): {field_list}.")
    now = datetime.now(timezone.utc)
    override_session_id = _fresh_batch_id()
    override_payload = dict(effective_payload)
    override_payload["override_session_id"] = override_session_id
    schema = get_event_schema(candidate.proposed_event_type)
    dedupe_payload = {
        field_name: effective_payload[field_name]
        for field_name in schema.dedupe_core_fields
        if field_name in effective_payload
    }
    resulting_envelope = build_event_envelope(
        program_id=candidate.program_id,
        event_type=candidate.proposed_event_type,
        occurred_at=occurred_at or candidate.proposed_occurred_at,
        recorded_at=now,
        temporal_confidence=TemporalConfidence(candidate.proposed_temporal_confidence),
        confidence=confidence or _coerce_confidence_tier(candidate.proposed_confidence),
        actor=actor,
        payload=override_payload,
        source_ref=candidate.source_ref,
        corroborating_refs=candidate.corroborating_refs,
        dedupe_payload=dedupe_payload,
    )
    ordered_conflicts = sorted(lock_conflicts.items())
    unlock_envelopes = tuple(
        build_event_envelope(
            program_id=candidate.program_id,
            event_type="operator.field_unlock.v1",
            occurred_at=now,
            recorded_at=now,
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
            actor=actor,
            payload={
                "entity_id": entity_id,
                "field": field_name,
                "reason": f"override-lock approve {candidate.candidate_id}",
                "override_session_id": override_session_id,
            },
            source_ref=_operator_assertion(actor, f"ledger override unlock {candidate.candidate_id}", now),
            dedupe_payload={"entity_id": entity_id, "field": field_name, "override_session_id": override_session_id},
        )
        for (entity_id, field_name), _lock_row in ordered_conflicts
    )
    relock_envelopes = []
    for (entity_id, field_name), lock_row in ordered_conflicts:
        relock_payload: dict[str, object] = {
            "entity_id": entity_id,
            "field": field_name,
            "locked_value": override_values[field_name],
            "reason": f"override-lock relock {candidate.candidate_id}",
            "override_session_id": override_session_id,
        }
        valid_until = lock_row.get("valid_until")
        if isinstance(valid_until, str) and valid_until:
            relock_payload["valid_until"] = valid_until
        relock_envelopes.append(
            build_event_envelope(
                program_id=candidate.program_id,
                event_type="operator.field_lock.v1",
                occurred_at=now,
                recorded_at=now,
                temporal_confidence=TemporalConfidence.EXACT,
                confidence=ConfidenceTier.OPERATOR_CONFIRMED,
                actor=actor,
                payload=relock_payload,
                source_ref=_operator_assertion(actor, f"ledger override relock {candidate.candidate_id}", now),
                dedupe_payload={
                    "entity_id": entity_id,
                    "field": field_name,
                    "locked_value": override_values[field_name],
                    "override_session_id": override_session_id,
                },
            )
        )
    persisted = _persist_events(unlock_envelopes + (resulting_envelope,) + tuple(relock_envelopes), programs_root=programs_root)
    return persisted[len(unlock_envelopes)]


def _override_lock_field_values(
    candidate: CandidateEvent,
    payload: dict[str, object],
    *,
    programs_root: Path,
) -> dict[str, object]:
    if candidate.proposed_event_type != "operator.correction.v1":
        return _candidate_field_values(candidate.proposed_event_type, payload)
    corrected_payload = payload.get("corrected_payload")
    if corrected_payload is None:
        raise typer.BadParameter("--override-lock is not supported for tombstone correction candidates.")
    if not isinstance(corrected_payload, dict):
        return {}
    original = _event_by_id(candidate.program_id, str(payload.get("corrects_event_id", "")), programs_root=programs_root)
    if original is None:
        return {}
    return _candidate_field_values(original.event_type, corrected_payload)


def _operator_assertion(actor: str, context: str, now: datetime) -> OperatorAssertionRef:
    """Build an ``OperatorAssertionRef`` carrying attested operator identity.

    activation.md §6.15.2 / AG-17: the approval/edit/revoke is the trust root,
    so it must carry more than the operator-supplied ``actor`` string. We attach
    the captured OS principal + machine + session so a forged ``--actor`` value
    is still attributable to a real principal/host. Headless runs (no OS user)
    produce a thinner but never-synthesized attestation.
    """
    identity = capture_operator_identity(actor)
    return OperatorAssertionRef(
        asserted_by=identity.actor,
        asserted_at=now,
        context=context,
        principal=identity.principal,
        machine=identity.machine,
        session=identity.session,
    )


def _write_candidate_audit_event(
    candidate: CandidateEvent,
    *,
    actor: str,
    event_type: str,
    payload: dict[str, object],
    programs_root: Path,
) -> EventEnvelope:
    now = datetime.now(timezone.utc)
    envelope = build_event_envelope(
        program_id=candidate.program_id,
        event_type=event_type,
        occurred_at=now,
        recorded_at=now,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor=actor,
        payload=dict(payload),
        source_ref=_operator_assertion(actor, f"ledger triage {candidate.candidate_id}", now),
        dedupe_payload=dict(payload),
    )
    return _persist_event(envelope, programs_root=programs_root)


def _persist_event(envelope: EventEnvelope, *, programs_root: Path) -> EventEnvelope:
    persisted = write_event(
        envelope,
        programs_root=programs_root,
        grounded_in_validator=lambda claim_id: _grounded_claim_exists(claim_id, programs_root=programs_root),
    ).envelope
    _maybe_bridge_event_to_fact_store(persisted, programs_root=programs_root)
    return persisted


def _persist_events(envelopes: tuple[EventEnvelope, ...], *, programs_root: Path) -> tuple[EventEnvelope, ...]:
    persisted = write_events_atomic(
        envelopes,
        programs_root=programs_root,
        grounded_in_validator=lambda claim_id: _grounded_claim_exists(claim_id, programs_root=programs_root),
    ).envelopes
    for envelope in persisted:
        _maybe_bridge_event_to_fact_store(envelope, programs_root=programs_root)
    return persisted


def _projection_privacy_gate(envelope: EventEnvelope) -> bool:
    """AG-11 composite privacy gate at the candidate→fact projection chokepoint.

    The hydrators already fail-closed on credentials/PII at *ingest* (Stage 1),
    but AG-11 (§6.14.11) requires the boundary to hold end-to-end: a fact that
    becomes ACCEPTED (the trust root — ``write_authority == human`` /
    ``OPERATOR_CONFIRMED``) must be re-checked before it is written to the
    Plane-1 fact store and rendered into a newsletter citation. This closes the
    gap where an operator ``triage edit`` could inject a raw credential/secret
    into a payload that then projects authoritatively.

    Returns True to proceed with projection, False to block (the event stays
    persisted; the bridge is retried on ``ledger replay`` once the payload is
    corrected — matching the existing appender-failure discipline). Fail-closed:
    a credential hit always blocks; a sensitivity/size denial blocks; an
    unexpected error is logged and **proceeds** so the privacy gate can never
    deadlock the weekly publication path (AG-12 graceful-degradation parity).
    """
    if envelope.confidence != ConfidenceTier.OPERATOR_CONFIRMED:
        return True  # PROPOSED facts are not the trust root; ingest gate owns them
    text = canonical_json(envelope.payload)
    try:
        result = run_local_checks(text, source_type=EntityType.MESSAGE)
    except Exception:  # pragma: no cover — defensive: never deadlock on the gate
        log.error(
            "projection privacy gate: run_local_checks raised for event_id=%s "
            "(program=%s event_type=%r) — proceeding fail-open to preserve AG-12",
            envelope.event_id, envelope.program_id, envelope.event_type, exc_info=True,
        )
        return True
    if result.passed:
        return True
    log.error(
        "projection privacy gate: BLOCKED projection of event_id=%s (program=%s "
        "event_type=%r confidence=%s) — reason=%s; fact not written, will retry on replay",
        envelope.event_id, envelope.program_id, envelope.event_type,
        envelope.confidence.value, result.reason,
    )
    return False


def _maybe_bridge_event_to_fact_store(envelope: EventEnvelope, *, programs_root: Path) -> None:
    if not _ledger_fact_bridge_enabled(program_id=envelope.program_id, programs_root=programs_root):
        return
    db_root = _resolve_bridge_db_root(programs_root)
    spec = lookup_event_spec(envelope.event_type)
    if spec is None:
        log.debug(
            "bridge: unrecognised event type %r — no registry entry, no fact projection applied "
            "(event_id=%s program=%s)",
            envelope.event_type,
            envelope.event_id,
            envelope.program_id,
        )
        return
    if spec.disposition == EventDisposition.PASSTHROUGH:
        return
    if spec.disposition == EventDisposition.KNOWN_UNPROJECTEABLE:
        log.warning(
            "bridge: event type %r received but no fact-store projector implemented yet "
            "(event_id=%s program=%s)",
            envelope.event_type,
            envelope.event_id,
            envelope.program_id,
        )
        return
    # PROJECTABLE — resolve appender by name and invoke
    _BRIDGE_APPENDERS: dict[str, Any] = {
        "append_bridged_risk_event": append_bridged_risk_event,
        "append_bridged_decision_event": append_bridged_decision_event,
        "append_bridged_assumption_event": append_bridged_assumption_event,
        "append_bridged_milestone_event": append_bridged_milestone_event,
        "append_bridged_dependency_event": append_bridged_dependency_event,
        "append_bridged_workstream_event": append_bridged_workstream_event,
        "append_bridged_commitment_event": append_bridged_commitment_event,
    }
    appender = _BRIDGE_APPENDERS.get(spec.bridge_appender_name or "")
    if appender is None:
        log.error(
            "bridge: registry entry for %r declares appender %r but it is not registered "
            "in _BRIDGE_APPENDERS — skipping (event_id=%s)",
            spec.prefix,
            spec.bridge_appender_name,
            envelope.event_id,
        )
        return
    # AG-11 (§6.14.11): composite privacy gate at the projection chokepoint.
    # Blocks ACCEPTED facts whose payload carries a credential/secret from
    # being written to the authoritative fact store (fail-closed on a hit).
    if not _projection_privacy_gate(envelope):
        return
    try:
        if spec.bridge_appender_name == "append_bridged_commitment_event":
            appender(envelope, db_root=db_root, programs_root=programs_root)
        else:
            appender(envelope, db_root=db_root)
        if spec.prefix == "risk.":
            sync_bridged_risk_corroboration(envelope, db_root=db_root, programs_root=programs_root)
    except Exception:
        # Bridge failure must never crash the ledger write path (the event is
        # already persisted).  Log at ERROR so operators see it; the next
        # `vertex ledger replay` will retry the projection.
        log.error(
            "bridge: appender %r raised for event_id=%s (program=%s event_type=%r) — "
            "event is persisted; bridge will be retried on next ledger replay",
            spec.bridge_appender_name,
            envelope.event_id,
            envelope.program_id,
            envelope.event_type,
            exc_info=True,
        )


def _replay_bridge_for_families(
    program_id: str,
    *,
    selected_families: frozenset[str],
    as_of: datetime | None,
    knowledge_as_of: datetime | None,
    programs_root: Path,
) -> dict[str, int]:
    """Re-run the fact-store bridge for a subset of fact families (W2-10).

    Reads all ledger events (subject to ``as_of``/``knowledge_as_of`` filters),
    filters to only events whose registry ``fact_family`` is in
    ``selected_families``, and calls ``_maybe_bridge_event_to_fact_store`` for
    each.  Non-selected families are untouched.

    Returns a ``{fact_family: count}`` dict of events processed per family.
    The bridge idempotency gate (``domain_event_id`` uniqueness) ensures that
    re-running already-projected events is a safe no-op.
    """
    events = read_events(program_id, programs_root=programs_root)
    counts: dict[str, int] = {}
    for envelope in events:
        if as_of is not None and envelope.occurred_at > as_of:
            continue
        if knowledge_as_of is not None and envelope.recorded_at > knowledge_as_of:
            continue
        spec = lookup_event_spec(envelope.event_type)
        if spec is None or spec.fact_family not in selected_families:
            continue
        counts[spec.fact_family] = counts.get(spec.fact_family, 0) + 1
        _maybe_bridge_event_to_fact_store(envelope, programs_root=programs_root)
    return counts


def _ledger_fact_bridge_enabled(
    *,
    program_id: str = "",
    programs_root: Path | None = None,
) -> bool:
    if os.environ.get("VERTEX_LEDGER_FACT_BRIDGE", "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if program_id and programs_root is not None:
        prog = load_program(program_id, programs_root=programs_root)
        if prog is not None and prog.m365 is not None:
            rev = prog.m365.rev
            if rev is not None and rev.fact_bridge_enabled:
                return True
    return False


def _resolve_bridge_db_root(programs_root: Path) -> Path:
    if programs_root.name == "programs":
        return programs_root.parent
    return programs_root


def _grounded_claim_exists(claim_id: str, *, programs_root: Path) -> bool:
    return find_claim_revision_by_id(claim_id, knowledge_root=get_shared_knowledge_root(programs_root)) is not None


def _load_gap_rows(program: str, *, programs_root: Path) -> list[dict[str, object]]:
    projection_path = get_current_projection_path(program, programs_root=programs_root)
    if not projection_path.exists():
        project_program_events(program, programs_root=programs_root)
    projection = canonical_projection_dump(get_current_projection_path(program, programs_root=programs_root))
    return list(projection["gaps"])


def _summarize_projection_diff(
    before_dump: dict[str, list[dict[str, object]]],
    after_dump: dict[str, list[dict[str, object]]],
) -> list[dict[str, object]]:
    tables: list[dict[str, object]] = []
    for table_name in sorted(set(before_dump) | set(after_dump)):
        before_rows = before_dump.get(table_name, [])
        after_rows = after_dump.get(table_name, [])
        changed = before_rows != after_rows
        if not changed:
            continue
        tables.append(
            {
                "table": table_name,
                "before_count": len(before_rows),
                "after_count": len(after_rows),
                "changed": changed,
            }
        )
    return tables


def _sqlite_copy(src: Path, dst: Path) -> None:
    src_connection = sqlite3.connect(src)
    dst_connection = sqlite3.connect(dst)
    try:
        src_connection.backup(dst_connection)
    finally:
        dst_connection.close()
        src_connection.close()


def _parse_cli_datetime(value: str | None, *, option_name: str) -> datetime | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise typer.BadParameter(f"{option_name} must be a valid ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_json_mapping(value: str, *, option_name: str) -> dict[str, object]:
    parsed = _parse_json_value(value, option_name=option_name)
    if not isinstance(parsed, dict):
        raise typer.BadParameter(f"{option_name} must decode to a JSON object.")
    return parsed


def _parse_json_value(value: str | None, *, option_name: str) -> object | None:
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"{option_name} must be valid JSON.") from exc


def _parse_source_ref_list(value: str | None) -> tuple[SourceRef, ...]:
    parsed = _parse_json_value(value, option_name="--corroborating-refs-json")
    if parsed is None:
        return ()
    if not isinstance(parsed, list):
        raise typer.BadParameter("--corroborating-refs-json must decode to a JSON array.")
    refs: list[SourceRef] = []
    for item in parsed:
        if not isinstance(item, dict):
            raise typer.BadParameter("--corroborating-refs-json entries must be JSON objects.")
        refs.append(source_ref_from_dict(item))
    return tuple(refs)


def _dedupe_payload_for(event_type: str, payload: dict[str, object]) -> dict[str, object]:
    schema = get_event_schema(event_type)
    return {field_name: payload[field_name] for field_name in schema.dedupe_core_fields if field_name in payload}


def _maybe_divert_locked_direct_write(envelope: EventEnvelope, *, programs_root: Path) -> CandidateEvent | None:
    locked_fields = _event_locked_fields(envelope.event_type, envelope.payload, program=envelope.program_id, programs_root=programs_root)
    if not locked_fields:
        return None
    candidate = _build_direct_write_candidate(envelope, locked_fields=locked_fields)
    append_candidate(candidate, programs_root=programs_root)
    return candidate


def _build_direct_write_candidate(
    envelope: EventEnvelope,
    *,
    locked_fields: set[tuple[str, str]],
) -> CandidateEvent:
    batch_id = _fresh_batch_id()
    candidate_id = _fresh_batch_id()
    source_document = source_document_key(envelope.source_ref)
    dedupe_core_hash = envelope.dedupe_core_hash or _compute_dedupe_core_hash(envelope.event_type, _dedupe_payload_for(envelope.event_type, envelope.payload))
    entity_resolution = tuple(
        CandidateEntityResolution(
            raw_name=entity_id,
            resolved_entity_id=entity_id,
            match_kind="exact",
            score=1.0,
        )
        for entity_id in sorted({entity_id for entity_id, _field_name in locked_fields})
    )
    return CandidateEvent(
        candidate_id=candidate_id,
        program_id=envelope.program_id,
        proposed_event_type=envelope.event_type,
        proposed_payload=dict(envelope.payload),
        proposed_occurred_at=envelope.occurred_at,
        proposed_temporal_confidence=envelope.temporal_confidence.value,
        proposed_confidence=envelope.confidence.value,
        source_ref=envelope.source_ref,
        pipeline="operator_direct_write",
        extraction_confidence=1.0,
        entity_resolution=entity_resolution,
        dedupe_key=derive_candidate_dedupe_key(source_document, dedupe_core_hash),
        dedupe_core_hash=dedupe_core_hash,
        source_document_key=source_document,
        corroborating_refs=envelope.corroborating_refs,
        batch_id=batch_id,
        staged_at=envelope.recorded_at,
    )


def _event_locked_fields(
    event_type: str,
    payload: dict[str, object],
    *,
    program: str,
    programs_root: Path,
) -> set[tuple[str, str]]:
    locked_pairs = _current_locked_pairs(program, programs_root=programs_root)
    if not locked_pairs:
        return set()
    if event_type == "operator.correction.v1":
        return _correction_locked_fields(payload, program=program, programs_root=programs_root, locked_pairs=locked_pairs)
    candidate_fields = _direct_event_candidate_fields(event_type, payload)
    return {pair for pair in candidate_fields if pair in locked_pairs}


def _correction_locked_fields(
    payload: dict[str, object],
    *,
    program: str,
    programs_root: Path,
    locked_pairs: set[tuple[str, str]],
) -> set[tuple[str, str]]:
    event_id = payload.get("corrects_event_id")
    if not isinstance(event_id, str):
        return set()
    original = _event_by_id(program, event_id, programs_root=programs_root)
    if original is None:
        return set()
    touched_fields = set(_direct_event_candidate_fields(original.event_type, original.payload))
    corrected_payload = payload.get("corrected_payload")
    if isinstance(corrected_payload, dict):
        touched_fields.update(_direct_event_candidate_fields(original.event_type, corrected_payload))
    return {pair for pair in touched_fields if pair in locked_pairs}


def _event_by_id(program: str, event_id: str, *, programs_root: Path) -> EventEnvelope | None:
    for event in read_events(program, programs_root=programs_root):
        if event.event_id == event_id:
            return event
    return None


def _current_locked_pairs(program: str, *, programs_root: Path) -> set[tuple[str, str]]:
    return set(_current_lock_rows(program, programs_root=programs_root).keys())


def _current_lock_rows(program: str, *, programs_root: Path) -> dict[tuple[str, str], dict[str, object]]:
    projection_path = get_current_projection_path(program, programs_root=programs_root)
    if not projection_path.exists():
        project_program_events(program, programs_root=programs_root)
        projection_path = get_current_projection_path(program, programs_root=programs_root)
    if not projection_path.exists():
        return {}
    projection = canonical_projection_dump(get_current_projection_path(program, programs_root=programs_root))
    return {
        (str(row["entity_id"]), str(row["field"])): dict(row)
        for row in projection["field_locks"]
    }


def _direct_event_candidate_fields(event_type: str, payload: dict[str, object]) -> set[tuple[str, str]]:
    family = _candidate_entity_family(event_type)
    if family is None or family not in _LOCKABLE_FIELDS:
        return set()
    _, entity_id_field, allowed_fields = _LOCKABLE_FIELDS[family]
    entity_id = payload.get(entity_id_field)
    if not isinstance(entity_id, str) or not entity_id:
        return set()
    candidate_fields = set(_candidate_payload_fields(event_type, payload))
    return {
        (entity_id, field_name)
        for field_name in allowed_fields
        if field_name in candidate_fields
    }


def _batch_candidates(program: str, batch_id: str, *, programs_root: Path) -> tuple[CandidateEvent, ...]:
    return tuple(candidate for candidate in load_pending_candidates(program, programs_root=programs_root) if candidate.batch_id == batch_id)


def _build_batch_status(program: str, batch_id: str, *, programs_root: Path) -> dict[str, object]:
    candidates = _batch_candidates(program, batch_id, programs_root=programs_root)
    if not candidates:
        raise typer.BadParameter(f"Unknown batch '{batch_id}'.")
    decisions = [decision for decision in load_triage_decisions(program, programs_root=programs_root) if decision.batch_id == batch_id]
    approved_sample_count = sum(1 for decision in decisions if decision.kind == "approved")
    resolution_complete_count = sum(1 for candidate in candidates if _candidate_has_complete_entity_resolution(candidate))
    total_candidates = len(candidates)
    required_sample_count = _required_sample_count(total_candidates)
    entity_resolution_rate = resolution_complete_count / total_candidates if total_candidates else 1.0
    lock_conflicts = _batch_lock_conflicts(candidates, programs_root=programs_root, program=program)
    return {
        "program_id": program,
        "batch_id": batch_id,
        "total_candidates": total_candidates,
        "active_candidates": len(active_candidates(program, programs_root=programs_root, batch_id=batch_id)),
        "decision_counts": {
            "approved": sum(1 for decision in decisions if decision.kind == "approved"),
            "rejected": sum(1 for decision in decisions if decision.kind == "rejected"),
            "skipped": sum(1 for decision in decisions if decision.kind == "skipped"),
        },
        "entity_resolution_rate": entity_resolution_rate,
        "approved_sample_count": approved_sample_count,
        "required_sample_count": required_sample_count,
        "entity_resolution_gate": entity_resolution_rate >= 0.9,
        "sample_gate": approved_sample_count >= required_sample_count,
        "lock_conflict_gate": not lock_conflicts,
        "lock_conflict_gate_evaluated": True,
        "lock_conflict_candidates": sorted(lock_conflicts),
    }


def _candidate_has_complete_entity_resolution(candidate: CandidateEvent) -> bool:
    schema = get_event_schema(candidate.proposed_event_type)
    if not schema.entity_ref_fields:
        return True
    if not candidate.entity_resolution:
        return False
    return all(resolution.resolved_entity_id is not None for resolution in candidate.entity_resolution)


def _required_sample_count(total_candidates: int) -> int:
    if total_candidates <= 0:
        return 0
    return min(total_candidates, max(10, math.ceil(total_candidates * 0.05)))


def _batch_lock_conflicts(candidates: tuple[CandidateEvent, ...], *, programs_root: Path, program: str) -> set[str]:
    locked_pairs = _current_locked_pairs(program, programs_root=programs_root)
    conflicts: set[str] = set()
    for candidate in candidates:
        for entity_id, field_name in _candidate_locked_fields(candidate, programs_root=programs_root):
            if (entity_id, field_name) in locked_pairs:
                conflicts.add(candidate.candidate_id)
                break
    return conflicts


def _candidate_lock_conflict_rows(
    candidate: CandidateEvent,
    *,
    programs_root: Path,
    payload: dict[str, object] | None = None,
) -> dict[tuple[str, str], dict[str, object]]:
    lock_rows = _current_lock_rows(candidate.program_id, programs_root=programs_root)
    if not lock_rows:
        return {}
    return {
        pair: lock_rows[pair]
        for pair in _candidate_locked_fields(candidate, programs_root=programs_root, payload=payload)
        if pair in lock_rows
    }


def _candidate_locked_fields(
    candidate: CandidateEvent,
    *,
    programs_root: Path,
    payload: dict[str, object] | None = None,
) -> set[tuple[str, str]]:
    if candidate.proposed_event_type == "operator.correction.v1":
        return _correction_locked_fields(
            payload if payload is not None else candidate.proposed_payload,
            program=candidate.program_id,
            programs_root=programs_root,
            locked_pairs=_current_locked_pairs(candidate.program_id, programs_root=programs_root),
        )
    family = _candidate_entity_family(candidate.proposed_event_type)
    if family is None or family not in _LOCKABLE_FIELDS:
        return set()
    _, _, allowed_fields = _LOCKABLE_FIELDS[family]
    resolved_entity_ids = {
        resolution.resolved_entity_id
        for resolution in candidate.entity_resolution
        if resolution.resolved_entity_id is not None
    }
    if not resolved_entity_ids:
        return set()
    effective_payload = payload if payload is not None else candidate.proposed_payload
    candidate_fields = set(_candidate_payload_fields(candidate.proposed_event_type, effective_payload))
    return {
        (entity_id, field_name)
        for entity_id in resolved_entity_ids
        for field_name in allowed_fields
        if field_name in candidate_fields
    }


def _candidate_entity_family(event_type: str) -> str | None:
    if event_type.startswith("risk."):
        return "risk"
    if event_type.startswith("milestone."):
        return "milestone"
    if event_type.startswith("deliverable."):
        return "deliverable"
    if event_type.startswith("decision."):
        return "decision"
    if event_type.startswith("assumption."):
        return "assumption"
    if event_type.startswith("dependency."):
        return "dependency"
    if event_type.startswith("commitment."):
        return "commitment"
    if event_type.startswith("kpi.") or event_type == "metric.observed.v1":
        return "kpi"
    if event_type.startswith("incident."):
        return "incident"
    if event_type.startswith("knowledge.article"):
        return "article"
    if event_type == "playbook.created.v1":
        return "playbook"
    return None


def _candidate_payload_fields(event_type: str, payload: dict[str, object]) -> tuple[str, ...]:
    field_values = _candidate_field_values(event_type, payload)
    if field_values:
        return tuple(field_values.keys())
    if event_type == "metric.observed.v1":
        return ()
    return tuple(payload.keys())


def _candidate_field_values(event_type: str, payload: dict[str, object]) -> dict[str, object]:
    mapping = {
        "risk.status_changed.v1": {"status": "new_status", "severity": "severity"},
        "risk.owner_changed.v1": {"owner_person_id": "new_owner_person_id"},
        "milestone.date_revised.v1": {"target_date": "new_target_date"},
        "milestone.status_changed.v1": {"status": "new_status"},
        "milestone.completed.v1": {"completed_on": "completed_on"},
        "decision.revised.v1": {"decision_text": "revision_text"},
        "decision.made.v1": {"title": "title", "decision_text": "decision_text", "forum": "forum"},
        "deliverable.status_changed.v1": {"status": "new_status"},
        "dependency.status_changed.v1": {"status": "new_status"},
        "workstream.status_changed.v1": {"status": "new_status"},
        "commitment.slipped.v1": {"due_date": "new_due_date"},
        "commitment.fulfilled.v1": {"fulfilled_on": "fulfilled_on"},
        "incident.resolved.v1": {"resolved_on": "resolved_on", "mttr_minutes": "mttr_minutes", "root_cause": "root_cause"},
        "knowledge.article_revised.v1": {"location": "location"},
    }
    if event_type in mapping:
        return {
            field_name: payload[payload_field]
            for field_name, payload_field in mapping[event_type].items()
            if payload_field in payload
        }
    if event_type == "metric.observed.v1":
        return {}
    return dict(payload)


def _fresh_batch_id() -> str:
    return fresh_discovery_batch_id()


def _candidate_from_import_line(line: str, *, program: str, batch_id: str) -> CandidateEvent:
    try:
        return candidate_from_import_line(line, program=program, batch_id=batch_id, pipeline="backfill_import")
    except DiscoveryCandidateBuildError as error:
        raise typer.BadParameter(str(error)) from error


def _looks_like_event_envelope(payload: dict[str, object]) -> bool:
    return all(key in payload for key in ("event_id", "event_type", "occurred_at", "recorded_at", "payload", "source_ref"))


def _looks_like_candidate_record(payload: dict[str, object]) -> bool:
    return all(
        key in payload
        for key in (
            "proposed_event_type",
            "proposed_payload",
            "proposed_occurred_at",
            "proposed_temporal_confidence",
            "proposed_confidence",
            "source_ref",
        )
    )


def _candidate_from_event_envelope_payload(payload: dict[str, object], *, program: str, batch_id: str) -> CandidateEvent:
    envelope = EventEnvelope.from_dict(payload)
    event_payload = dict(envelope.payload)
    dedupe_payload = _dedupe_payload_for(envelope.event_type, event_payload)
    dedupe_core_hash = envelope.dedupe_core_hash or _compute_dedupe_core_hash(envelope.event_type, dedupe_payload)
    document_key = source_document_key(envelope.source_ref)
    return CandidateEvent(
        candidate_id=_fresh_batch_id(),
        program_id=program,
        proposed_event_type=envelope.event_type,
        proposed_payload=event_payload,
        proposed_occurred_at=envelope.occurred_at,
        proposed_temporal_confidence=envelope.temporal_confidence.value,
        proposed_confidence=envelope.confidence.value,
        source_ref=envelope.source_ref,
        pipeline="backfill_import",
        extraction_confidence=_import_confidence_score(envelope.confidence.value),
        entity_resolution=_import_entity_resolution(envelope.event_type, event_payload),
        dedupe_key=derive_candidate_dedupe_key(document_key, dedupe_core_hash),
        dedupe_core_hash=dedupe_core_hash,
        source_document_key=document_key,
        corroborating_refs=envelope.corroborating_refs,
        batch_id=batch_id,
    )


def _candidate_from_candidate_payload(payload: dict[str, object], *, program: str, batch_id: str) -> CandidateEvent:
    proposed_payload = dict(_require_mapping(payload, "proposed_payload"))
    proposed_event_type = _require_str(payload, "proposed_event_type")
    dedupe_payload = _dedupe_payload_for(proposed_event_type, proposed_payload)
    dedupe_core_hash = str(payload.get("dedupe_core_hash") or _compute_dedupe_core_hash(proposed_event_type, dedupe_payload))
    source_ref = source_ref_from_dict(_require_mapping(payload, "source_ref"))
    document_key = str(payload.get("source_document_key") or source_document_key(source_ref))
    entity_resolution = tuple(
        CandidateEntityResolution(
            raw_name=str(item.get("raw_name", "")),
            resolved_entity_id=str(item["resolved_entity_id"]) if isinstance(item.get("resolved_entity_id"), str) else None,
            match_kind=str(item.get("match_kind", "imported")),
            score=float(item.get("score", 1.0)),
        )
        for item in _require_list(payload, "entity_resolution", default=[])
        if isinstance(item, dict)
    )
    corroborating_refs = tuple(
        source_ref_from_dict(item)
        for item in _require_list(payload, "corroborating_refs", default=[])
        if isinstance(item, dict)
    )
    return CandidateEvent(
        candidate_id=str(payload.get("candidate_id") or _fresh_batch_id()),
        program_id=program,
        proposed_event_type=proposed_event_type,
        proposed_payload=proposed_payload,
        proposed_occurred_at=_require_datetime(payload, "proposed_occurred_at"),
        proposed_temporal_confidence=_require_str(payload, "proposed_temporal_confidence"),
        proposed_confidence=_require_str(payload, "proposed_confidence"),
        source_ref=source_ref,
        pipeline="backfill_import",
        extraction_confidence=float(cast(float, payload.get("extraction_confidence", 1.0))),
        entity_resolution=entity_resolution,
        dedupe_key=str(payload.get("dedupe_key") or derive_candidate_dedupe_key(document_key, dedupe_core_hash)),
        dedupe_core_hash=dedupe_core_hash,
        source_document_key=document_key,
        corroborating_refs=corroborating_refs,
        batch_id=batch_id,
    )


def _compute_dedupe_core_hash(event_type: str, payload: dict[str, object]) -> str:
    return compute_dedupe_core_hash(event_type, payload)


def _import_confidence_score(confidence: str) -> float:
    return {
        ConfidenceTier.OPERATOR_CONFIRMED.value: 1.0,
        ConfidenceTier.SOURCE_AUTHORITATIVE.value: 0.95,
        ConfidenceTier.AI_EXTRACTED.value: 0.8,
        ConfidenceTier.INFERRED.value: 0.6,
    }.get(confidence, 0.5)


def _import_entity_resolution(event_type: str, payload: dict[str, object]) -> tuple[CandidateEntityResolution, ...]:
    schema = get_event_schema(event_type)
    resolutions: list[CandidateEntityResolution] = []
    for field_name in schema.entity_ref_fields:
        raw_value = payload.get(field_name)
        if isinstance(raw_value, str):
            resolutions.append(CandidateEntityResolution(raw_name=raw_value, resolved_entity_id=raw_value, match_kind="imported", score=1.0))
        elif isinstance(raw_value, list):
            for item in raw_value:
                if isinstance(item, str):
                    resolutions.append(CandidateEntityResolution(raw_name=item, resolved_entity_id=item, match_kind="imported", score=1.0))
        elif isinstance(raw_value, dict) and field_name == "milestone_dates":
            for item in raw_value.keys():
                if isinstance(item, str):
                    resolutions.append(CandidateEntityResolution(raw_name=item, resolved_entity_id=item, match_kind="imported", score=1.0))
    return tuple(resolutions)


def _render_import_progress(processed: int, total: int, started_at: float, now: float) -> str:
    elapsed = max(now - started_at, 0.001)
    rate = processed / elapsed
    remaining = max(total - processed, 0)
    eta_seconds = remaining / rate if rate > 0 else 0.0
    return f"Import progress: {processed}/{total} rows ({rate:.1f}/s, eta {eta_seconds:.1f}s)"


def _require_mapping(payload: dict[str, object], field_name: str) -> dict[str, object]:
    value = payload.get(field_name)
    if not isinstance(value, dict):
        raise typer.BadParameter(f"Imported row field '{field_name}' must be a JSON object.")
    return value


def _require_list(payload: dict[str, object], field_name: str, *, default: list[object] | None = None) -> list[object]:
    value = payload.get(field_name, default if default is not None else None)
    if value is None:
        return [] if default is not None else []
    if not isinstance(value, list):
        raise typer.BadParameter(f"Imported row field '{field_name}' must be a JSON array.")
    return value


def _require_str(payload: dict[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise typer.BadParameter(f"Imported row field '{field_name}' must be a non-empty string.")
    return value


def _require_datetime(payload: dict[str, object], field_name: str) -> datetime:
    value = payload.get(field_name)
    if not isinstance(value, str):
        raise typer.BadParameter(f"Imported row field '{field_name}' must be an ISO-8601 string.")
    parsed = _parse_cli_datetime(value, option_name=field_name)
    if parsed is None:
        raise typer.BadParameter(f"Imported row field '{field_name}' must be an ISO-8601 string.")
    return parsed


def _build_lt_deck_backfill_candidates(
    program: str,
    *,
    source_dir: Path,
    from_year: int | None,
    batch_id: str,
) -> list[CandidateEvent]:
    return list(
        build_lt_deck_artifact_candidates(
            program,
            source_dir=source_dir,
            from_year=from_year,
            batch_id=batch_id,
            pipeline="backfill_import",
        )
    )


def _extract_lt_deck_year(path: Path) -> int | None:
    try:
        return int(path.parent.name)
    except ValueError:
        return None


def _extract_lt_deck_date(path: Path):
    stem = path.stem
    for token in stem.replace('_', ' ').split():
        normalized = token.strip().rstrip('-')
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            parsed = None
        if parsed is not None:
            return parsed.date()
        for pattern in ("%Y%m%d", "%Y%m", "%Y%m-%d"):
            try:
                return datetime.strptime(normalized, pattern).date()
            except ValueError:
                continue
    return None
