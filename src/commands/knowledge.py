from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
from time import monotonic
from typing import Any

import typer

from src.ai.discovery.kb_claim_extractor import extract_claim_candidates_from_markdown
from src.ai.discovery.kb_event_extractor import KBEventExtractorError, extract_event_candidates_from_markdown
from src.core.backup import find_backups_referencing_paths
from src.core.entity_registry import EntityRegistry
from src.core.knowledge_candidate_store import active_candidates as active_knowledge_candidates
from src.core.knowledge_candidate_store import append_candidate as append_knowledge_candidate
from src.core.knowledge_candidate_store import SKIP_EXPIRY_DAYS, KnowledgeCandidate, KnowledgeCandidateDecisionRecord, KnowledgeCandidateEntityResolution, append_triage_decision, load_pending_candidates, load_triage_decisions
from src.core.knowledge.predicate_registry import all_predicates, validate_predicate_value
from src.core.knowledge.vault import delete_vault_entry, ingest_knowledge_source, load_all_vault_entries, load_scope_sources, load_vault_entry, source_registry_paths_for_vault_hash
from src.core.knowledge_claim_store import append_claim_revision, claim_ids_referencing_vault_hash, find_claim_redaction_by_id, find_claim_revision_by_id, find_claim_revision_storage_path, load_program_knowledge_scopes, redact_claim_revision, summarize_knowledge_status
from src.core.knowledge_index import ensure_knowledge_index, load_live_vault_hashes
from src.core.knowledge_store import get_shared_knowledge_root
from src.core.ledger.candidate_store import CandidateEvent, append_candidate as append_ledger_candidate
from src.core.ledger.event_log import ConfidenceTier
from src.core.ledger.source_refs import OperatorAssertionRef
from src.core.ledger.ulid import new_ulid
from src.core.program_reality import ProgramReality


app = typer.Typer(help="Knowledge plane authoring and inspection commands.")
triage_app = typer.Typer(help="Review staged knowledge claim candidates.")
PROGRAMS_ROOT = Path(__file__).resolve().parents[2] / "programs"


@app.command("assert")
def assert_claim(
    scope: str = typer.Option(..., "--scope", help="Knowledge scope, e.g. domain:storage-platform."),
    subject: str = typer.Option(..., "--subject", help="Subject entity id."),
    predicate: str = typer.Option(..., "--predicate", help="Registered knowledge predicate."),
    value: str | None = typer.Option(None, "--value", help="String value for the claim."),
    value_json: str | None = typer.Option(None, "--value-json", help="JSON-encoded value. Use 'null' for tombstones."),
    valid_from: str | None = typer.Option(None, "--valid-from", help="Validity start (ISO date or datetime). Defaults to now."),
    valid_until: str | None = typer.Option(None, "--valid-until", help="Validity end (ISO date or datetime)."),
    actor: str = typer.Option(..., "--actor", help="Operator alias writing the claim."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    now = datetime.now(timezone.utc)
    revision = _write_operator_claim(
        scope=scope,
        subject=subject,
        predicate=predicate,
        value=_parse_claim_value(value=value, value_json=value_json),
        valid_from=_parse_cli_datetime(valid_from) or now,
        valid_until=_parse_cli_datetime(valid_until),
        actor=actor,
        context=None,
        programs_root=programs_root,
    )
    typer.echo(f"Wrote claim {revision.claim_id} for {revision.subject} {revision.predicate} in {revision.scope}.")


@app.command("supersede")
def supersede_claim(
    scope: str | None = typer.Option(None, "--scope", help="Knowledge scope, e.g. domain:storage-platform."),
    subject: str | None = typer.Option(None, "--subject", help="Subject entity id."),
    predicate: str | None = typer.Option(None, "--predicate", help="Registered knowledge predicate."),
    claim_id: str | None = typer.Option(None, "--claim-id", help="Existing claim revision ULID to supersede."),
    value: str | None = typer.Option(None, "--value", help="String value for the replacement claim."),
    value_json: str | None = typer.Option(None, "--value-json", help="JSON-encoded value. Use 'null' for tombstones."),
    valid_from: str | None = typer.Option(None, "--valid-from", help="Validity start (ISO date or datetime). Defaults to now."),
    valid_until: str | None = typer.Option(None, "--valid-until", help="Validity end (ISO date or datetime)."),
    reason: str = typer.Option(..., "--reason", help="Operator reason for the supersession."),
    actor: str = typer.Option(..., "--actor", help="Operator alias writing the claim."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    knowledge_root = get_shared_knowledge_root(programs_root)
    resolved_scope, resolved_subject, resolved_predicate = _resolve_supersede_target(
        scope=scope,
        subject=subject,
        predicate=predicate,
        claim_id=claim_id,
        knowledge_root=knowledge_root,
    )
    now = datetime.now(timezone.utc)
    revision = _write_operator_claim(
        scope=resolved_scope,
        subject=resolved_subject,
        predicate=resolved_predicate,
        value=_parse_claim_value(value=value, value_json=value_json),
        valid_from=_parse_cli_datetime(valid_from) or now,
        valid_until=_parse_cli_datetime(valid_until),
        actor=actor,
        context=reason,
        programs_root=programs_root,
        recorded_at=now,
    )
    typer.echo(
        f"Superseded {revision.supersedes or 'prior claim'} with {revision.claim_id} for"
        f" {revision.subject} {revision.predicate} in {revision.scope}."
    )


@app.command("redact")
def redact_claim(
    claim_id: str = typer.Option(..., "--claim-id", help="Existing claim revision ULID to redact."),
    reason: str = typer.Option(..., "--reason", help="Compliance reason for redaction."),
    actor: str = typer.Option(..., "--actor", help="Operator performing the redaction."),
    backup_root: Path | None = typer.Option(None, "--backup-root", help="Optional root directory containing backup snapshots to inspect."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    knowledge_root = get_shared_knowledge_root(programs_root)
    existing = find_claim_redaction_by_id(claim_id, knowledge_root=knowledge_root)
    if existing is not None:
        typer.echo(f"Claim {claim_id} already redacted.")
        return
    storage_path = find_claim_revision_storage_path(claim_id, knowledge_root=knowledge_root)
    try:
        record = redact_claim_revision(
            claim_id,
            knowledge_root=knowledge_root,
            actor=actor,
            reason=reason,
        )
    except ValueError:
        typer.echo(f"Unknown claim id '{claim_id}'.")
        raise typer.Exit(code=3) from None
    typer.echo(f"Redacted claim {record.claim_id} in {record.scope}.")
    _emit_backup_hits(
        backup_root=backup_root,
        relative_paths=_repo_relative_paths(
            (path for path in (storage_path, knowledge_root / ".claim-redactions.jsonl") if path is not None),
            root_path=programs_root.parent,
        ),
    )


@app.command("redact-vault")
def redact_vault(
    vault_hash: str = typer.Option(..., "--vault-hash", help="Knowledge vault hash to destroy and cascade-redact."),
    reason: str = typer.Option(..., "--reason", help="Compliance reason for vault redaction."),
    actor: str = typer.Option(..., "--actor", help="Operator performing the redaction."),
    backup_root: Path | None = typer.Option(None, "--backup-root", help="Optional root directory containing backup snapshots to inspect."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    knowledge_root = get_shared_knowledge_root(programs_root)
    registry_paths = source_registry_paths_for_vault_hash(vault_hash, programs_root=programs_root)
    claim_storage_paths = [
        find_claim_revision_storage_path(claim_id, knowledge_root=knowledge_root)
        for claim_id in claim_ids_referencing_vault_hash(vault_hash, knowledge_root=knowledge_root)
    ]
    try:
        vault_entry = load_vault_entry(vault_hash, programs_root=programs_root)
    except Exception:
        typer.echo(f"Unknown vault hash '{vault_hash}'.")
        raise typer.Exit(code=3) from None
    try:
        delete_vault_entry(vault_hash, programs_root=programs_root)
    except Exception:
        typer.echo(f"Unknown vault hash '{vault_hash}'.")
        raise typer.Exit(code=3) from None
    redacted_claims = 0
    for claim_id in claim_ids_referencing_vault_hash(vault_hash, knowledge_root=knowledge_root):
        redact_claim_revision(
            claim_id,
            knowledge_root=knowledge_root,
            actor=actor,
            reason=f"vault redacted: {reason}",
        )
        redacted_claims += 1
    rejected_candidates = 0
    for candidate in load_pending_candidates(programs_root=programs_root):
        if not _candidate_references_vault_hash(candidate, vault_hash=vault_hash):
            continue
        if not _candidate_is_active(candidate.candidate_id, programs_root=programs_root):
            continue
        append_triage_decision(
            KnowledgeCandidateDecisionRecord(
                candidate_id=candidate.candidate_id,
                kind="rejected",
                decided_at=datetime.now(timezone.utc),
                triage_actor=actor,
                batch_id=candidate.batch_id,
                reason=f"source vault redacted: {reason}",
            ),
            programs_root=programs_root,
        )
        rejected_candidates += 1
    typer.echo(
        f"Redacted vault {vault_hash}; claims_redacted={redacted_claims} active_candidates_rejected={rejected_candidates}."
    )
    _emit_backup_hits(
        backup_root=backup_root,
        relative_paths=_repo_relative_paths(
            (
                path
                for path in (
                    vault_entry.content_path,
                    vault_entry.metadata_path,
                    *registry_paths,
                    *(path for path in claim_storage_paths if path is not None),
                    knowledge_root / ".claim-redactions.jsonl",
                    knowledge_root / "candidates" / "triaged.jsonl",
                )
                if path is not None
            ),
            root_path=programs_root.parent,
        ),
    )


@app.command("gc")
def gc_vault(
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview unreferenced vault entries without deleting them."),
    older_than_days: int = typer.Option(90, "--older-than-days", help="Minimum age in days before an unreferenced vault entry is collectible."),
    format: str = typer.Option("text", "--format", help="Output format: text or json."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    knowledge_root = get_shared_knowledge_root(programs_root)
    rebuilt_index = ensure_knowledge_index(knowledge_root=knowledge_root, programs_root=programs_root)
    live_hashes = set(load_live_vault_hashes(knowledge_root=knowledge_root))
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    candidates = tuple(
        entry
        for entry in load_all_vault_entries(programs_root=programs_root)
        if entry.vault_hash not in live_hashes and entry.ingested_at <= cutoff
    )
    payload = {
        "rebuilt_index": rebuilt_index,
        "older_than_days": older_than_days,
        "candidate_count": len(candidates),
        "candidates": [
            {
                "vault_hash": entry.vault_hash,
                "original_filename": entry.original_filename,
                "ingested_at": entry.ingested_at.isoformat(),
                "size_bytes": entry.size_bytes,
            }
            for entry in candidates
        ],
        "deleted_count": 0,
        "dry_run": dry_run,
    }
    if not dry_run:
        for entry in candidates:
            delete_vault_entry(entry.vault_hash, programs_root=programs_root)
        payload["deleted_count"] = len(candidates)
    normalized_format = format.strip().lower()
    if normalized_format == "json":
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    if normalized_format != "text":
        raise typer.BadParameter("--format must be 'text' or 'json'.")
    typer.echo(
        f"Knowledge GC candidates={payload['candidate_count']} deleted={payload['deleted_count']} rebuilt_index={payload['rebuilt_index']}"
    )
    if dry_run:
        typer.echo("Dry run: no vault entries deleted.")
    for entry in candidates:
        typer.echo(f"- {entry.vault_hash} {entry.original_filename} ingested_at={entry.ingested_at.isoformat()}")


@app.command("ingest")
def ingest_source(
    source: Path = typer.Option(..., "--source", help="Local source document to ingest into the knowledge vault."),
    scope: str = typer.Option(..., "--scope", help="Knowledge scope to register the source under."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    entry = ingest_knowledge_source(source, scope=scope, programs_root=programs_root)
    typer.echo(f"Ingested {source.name} -> {entry.vault_hash} for {scope}.")


@app.command("extract")
def extract_sources(
    scope: str = typer.Option(..., "--scope", help="Knowledge scope to extract from."),
    source: str | None = typer.Option(None, "--source", help="Optional vault hash to extract from."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview candidates without writing pending.jsonl."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    sources = load_scope_sources(scope, programs_root=programs_root)
    if source is not None:
        sources = tuple(item for item in sources if item.vault_hash == source)
    extracted: list[KnowledgeCandidate] = []
    ledger_extracted: list[CandidateEvent] = []
    batch_id: str | None = new_ulid(datetime.now(timezone.utc)) if sources else None
    for source_record in sources:
        vault_entry = load_vault_entry(source_record.vault_hash, programs_root=programs_root)
        if vault_entry.content_path.suffix.lower() == ".md":
            text = vault_entry.content_path.read_text(encoding="utf-8")
        else:
            text = vault_entry.content_path.read_text(encoding="utf-8")
        program_id = _program_id_from_scope(scope)
        if program_id is not None:
            try:
                batch = extract_event_candidates_from_markdown(
                    markdown_text=text,
                    program_id=program_id,
                    vault_hash=source_record.vault_hash,
                    original_filename=source_record.original_filename,
                    origin_path=source_record.origin_path,
                    ingested_at=source_record.ingested_at,
                    batch_id=batch_id or new_ulid(datetime.now(timezone.utc)),
                )
            except KBEventExtractorError as error:
                raise typer.BadParameter(str(error)) from error
            batch_id = batch.batch_id
            ledger_extracted.extend(batch.candidates)
        if _is_decision_log_source(source_record.original_filename) and program_id is None:
            raise typer.BadParameter("decision-log-kb.md extraction requires --scope program:<id>.")
        claim_batch = extract_claim_candidates_from_markdown(
            markdown_text=text,
            scope=scope,
            vault_hash=source_record.vault_hash,
            original_filename=source_record.original_filename,
            origin_path=source_record.origin_path,
            ingested_at=source_record.ingested_at,
            batch_id=batch_id,
        )
        batch_id = claim_batch.batch_id
        extracted.extend(claim_batch.candidates)
        extracted = list(_collapse_extracted_knowledge_candidates(extracted))
        ledger_extracted = list(_collapse_extracted_ledger_candidates(ledger_extracted))
    if dry_run:
        total = len(extracted) + len(ledger_extracted)
        typer.echo(f"Dry run: {total} candidates from {len(sources)} source(s).")
        if extracted:
            sample = extracted[0]
            typer.echo(f"Sample: {sample.proposed_claim.subject} {sample.proposed_claim.predicate}={sample.proposed_claim.value}")
        elif ledger_extracted:
            ledger_sample = ledger_extracted[0]
            typer.echo(f"Sample: {ledger_sample.proposed_event_type} {ledger_sample.proposed_payload}")
        return
    for candidate in extracted:
        append_knowledge_candidate(candidate, programs_root=programs_root)
    for ledger_candidate in ledger_extracted:
        append_ledger_candidate(ledger_candidate, programs_root=programs_root)
    typer.echo(
        f"Staged {len(extracted) + len(ledger_extracted)} candidates from {len(sources)} source(s) batch={batch_id or '-'}."
    )


@app.command("quarantine-batch")
def quarantine_batch(
    batch_id: str = typer.Option(..., "--batch-id", help="Batch ID to quarantine."),
    actor: str = typer.Option(..., "--actor", help="Operator quarantining the batch."),
    reason: str = typer.Option(..., "--reason", help="Reason for quarantining the batch."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    pending_candidates = [candidate for candidate in load_pending_candidates(programs_root=programs_root) if candidate.batch_id == batch_id]
    if not pending_candidates:
        typer.echo(f"Unknown batch '{batch_id}'.")
        raise typer.Exit(code=3)
    decisions = load_triage_decisions(programs_root=programs_root)
    if any(decision.batch_id == batch_id and decision.kind == "approved" for decision in decisions):
        typer.echo(
            f"Batch '{batch_id}' already contains approved candidates; use claim supersession or tombstone revisions for post-approval cleanup."
        )
        raise typer.Exit(code=3)
    active_batch_candidates = active_knowledge_candidates(programs_root=programs_root, batch_id=batch_id)
    if not active_batch_candidates:
        typer.echo(f"Batch {batch_id} already has no active candidates.")
        return
    for candidate in active_batch_candidates:
        append_triage_decision(
            KnowledgeCandidateDecisionRecord(
                candidate_id=candidate.candidate_id,
                kind="rejected",
                decided_at=datetime.now(timezone.utc),
                triage_actor=actor,
                batch_id=batch_id,
                reason=f"quarantined: {reason}",
            ),
            programs_root=programs_root,
        )
    typer.echo(f"Quarantined {len(active_batch_candidates)} candidates from batch {batch_id}.")


@app.command("show")
def show_claims(
    entity: str = typer.Option(..., "--entity", help="Entity id to resolve."),
    program: str = typer.Option(..., "--program", help="Program id used to resolve scope chain."),
    as_of: str | None = typer.Option(None, "--as-of", help="Occurred-time cutoff (ISO date or datetime)."),
    knowledge_as_of: str | None = typer.Option(None, "--knowledge-as-of", help="Knowledge-time cutoff (ISO date or datetime)."),
    format: str = typer.Option("text", "--format", help="Output format: text or json."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    reality = ProgramReality.load(program, programs_root=programs_root)
    context = reality.knowledge_context(
        (entity,),
        as_of=_parse_cli_datetime(as_of),
        knowledge_as_of=_parse_cli_datetime(knowledge_as_of),
    )
    entry = context.entry(entity)
    payload = {
        "program_id": program,
        "scope_chain": list(context.scope_chain or load_program_knowledge_scopes(program, programs_root=programs_root)),
        "entity_id": entity,
        "entry": None
        if entry is None
        else {
            "entity_id": entry.entity_id,
            "projection_coverage": entry.projection_coverage,
            "claims": [
                {
                    "claim_id": claim.claim_id,
                    "scope": claim.scope,
                    "predicate": claim.predicate,
                    "value": claim.value,
                    "tombstoned": claim.tombstoned,
                    "confidence": claim.confidence,
                    "valid_from": claim.valid_from.isoformat(),
                    "valid_until": claim.valid_until.isoformat() if claim.valid_until is not None else None,
                    "recorded_at": claim.recorded_at.isoformat(),
                    "source_document_key": claim.source_document_key,
                    "overridden_claim_ids": list(claim.overridden_claim_ids),
                }
                for claim in entry.claims
            ],
        },
    }
    normalized_format = format.strip().lower()
    if normalized_format == "json":
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    if normalized_format != "text":
        raise typer.BadParameter("--format must be 'text' or 'json'.")
    typer.echo(f"KNOWLEDGE SHOW {entity}")
    if entry is None or not entry.claims:
        typer.echo("No claims found.")
        return
    typer.echo(f"projection_coverage={entry.projection_coverage}")
    for claim in entry.claims:
        value_label = "null" if claim.value is None else str(claim.value)
        tombstone_suffix = " tombstoned=true" if claim.tombstoned else ""
        typer.echo(
            f"- {claim.predicate}={value_label} scope={claim.scope} confidence={claim.confidence}"
            f" source={claim.source_document_key}{tombstone_suffix}"
        )


@app.command("predicates")
def list_predicates(format: str = typer.Option("text", "--format", help="Output format: text or json.")) -> None:
    definitions = all_predicates()
    normalized_format = format.strip().lower()
    if normalized_format == "json":
        typer.echo(json.dumps([definition.__dict__ for definition in definitions], indent=2, sort_keys=True))
        return
    if normalized_format != "text":
        raise typer.BadParameter("--format must be 'text' or 'json'.")
    for definition in definitions:
        typer.echo(f"- {definition.name} ({definition.value_kind})")


@app.command("status")
def status(format: str = typer.Option("text", "--format", help="Output format: text or json."), programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True)) -> None:
    summary = summarize_knowledge_status(knowledge_root=get_shared_knowledge_root(programs_root))
    normalized_format = format.strip().lower()
    if normalized_format == "json":
        typer.echo(json.dumps(summary.to_dict(), indent=2, sort_keys=True))
        return
    if normalized_format != "text":
        raise typer.BadParameter("--format must be 'text' or 'json'.")
    typer.echo("KNOWLEDGE STATUS")
    pipeline_summary = ",".join(f"{pipeline}={count}" for pipeline, count in summary.pending_candidates_by_pipeline.items()) or "-"
    typer.echo(
        f"pending_candidates={summary.pending_candidate_count}"
        f" pending_by_pipeline={pipeline_summary}"
        f" oldest_pending_age_seconds={summary.oldest_pending_candidate_age_seconds}"
        f" pending_missing_created_at={summary.pending_candidates_missing_created_at_count}"
        f" triaged_candidates={summary.triaged_candidate_count}"
    )
    typer.echo(
        f"latest_triage_actor={summary.latest_triage_session_actor or '-'}"
        f" latest_triage_session_decisions={summary.latest_triage_session_decision_count}"
        f" latest_triage_session_duration_seconds={summary.latest_triage_session_duration_seconds}"
        f" latest_triage_throughput_per_minute={summary.latest_triage_session_throughput_per_minute}"
        f" triage_session_gap_minutes={summary.triage_session_gap_minutes}"
    )
    typer.echo(
        f"batches_total={summary.batch_count}"
        f" batches_staged={summary.staged_batch_count}"
        f" batches_approved={summary.approved_batch_count}"
        f" batches_quarantined={summary.quarantined_batch_count}"
    )
    typer.echo(f"registered_predicates={summary.registered_predicate_count}")
    typer.echo(
        f"expired_claims={summary.expired_claim_count}"
        f" expiring_soon_claims={summary.expiring_soon_claim_count}"
        f" warning_window_days={summary.warning_window_days}"
    )
    typer.echo(
        f"active_overrides={summary.active_override_count}"
        f" override_programs={summary.active_override_program_count}"
    )
    typer.echo(
        f"vault_files={summary.vault.file_count} vault_bytes={summary.vault.total_bytes}"
        f" missing_meta={summary.vault.missing_meta_count} hash_mismatches={summary.vault.hash_mismatch_count}"
        f" missing_source_records={summary.vault.missing_source_record_count}"
        f" missing_claim_refs={summary.vault.missing_claim_ref_count}"
        f" missing_candidate_refs={summary.vault.missing_candidate_ref_count}"
        f" last_deep_verify_ok={summary.vault.last_deep_verify_ok}"
        f" last_deep_verify_age_seconds={summary.vault.last_deep_verify_age_seconds}"
    )
    for batch in summary.batches:
        pipeline_summary = ",".join(f"{pipeline}={count}" for pipeline, count in batch["pipelines"].items()) or "-"
        typer.echo(
            f"- batch={batch['batch_id']} status={batch['status']} total={batch['total_candidate_count']}"
            f" active={batch['active_candidate_count']} approved={batch['approved_count']}"
            f" rejected={batch['rejected_count']} skipped={batch['skipped_count']}"
            f" quarantined={batch['quarantined_candidate_count']} pipelines={pipeline_summary}"
        )
    if not summary.scopes:
        typer.echo("No claim scopes found.")
        return
    for scope in summary.scopes:
        latest = scope.latest_recorded_at.isoformat() if scope.latest_recorded_at is not None else "-"
        tier_summary = ",".join(f"{tier}={count}" for tier, count in sorted(scope.active_claims_by_confidence.items()))
        typer.echo(
            f"- {scope.scope} revisions={scope.revision_count} active={scope.active_claim_count}"
            f" tombstoned={scope.tombstoned_claim_count} subjects={scope.subject_count} predicates={scope.predicate_count} latest={latest}"
            f" tiers={tier_summary}"
        )


@triage_app.command("list")
def triage_list(
    scope: str | None = typer.Option(None, "--scope", help="Optional scope filter."),
    batch_id: str | None = typer.Option(None, "--batch-id", help="Optional batch filter."),
    format: str = typer.Option("text", "--format", help="Output format: text or json."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    candidates = active_knowledge_candidates(programs_root=programs_root, scope=scope, batch_id=batch_id)
    normalized_format = format.strip().lower()
    if normalized_format == "json":
        typer.echo(
            json.dumps(
                {"candidates": [_candidate_payload(candidate, programs_root=programs_root) for candidate in candidates]},
                indent=2,
                sort_keys=True,
            )
        )
        return
    if normalized_format != "text":
        raise typer.BadParameter("--format must be 'text' or 'json'.")
    if not candidates:
        typer.echo("No active candidates.")
        return
    for candidate in candidates:
        resolution_summary = _render_candidate_resolution_summary(candidate, programs_root=programs_root)
        typer.echo(
            f"- {candidate.candidate_id} scope={candidate.scope} subject={candidate.proposed_claim.subject}"
            f" predicate={candidate.proposed_claim.predicate} confidence={candidate.proposed_confidence}"
            f" extraction_confidence={candidate.extraction_confidence:.3f} source={candidate.source_document_key}"
            f" corroborating_refs={len(candidate.corroborating_refs)} effective_subject={_effective_candidate_subject(candidate, programs_root=programs_root)}"
            f" entity_resolution={resolution_summary}"
        )


@triage_app.command("approve")
def triage_approve(
    candidate_id: str = typer.Option(..., "--candidate", help="Candidate ID to approve."),
    actor: str = typer.Option(..., "--actor", help="Operator approving the candidate."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    candidate = _require_candidate(candidate_id, programs_root=programs_root)
    _ensure_candidate_triageable(candidate_id, programs_root=programs_root)
    revision = append_claim_revision(
        scope=candidate.scope,
        subject=_effective_candidate_subject(candidate, programs_root=programs_root),
        predicate=candidate.proposed_claim.predicate,
        value=candidate.proposed_claim.value,
        valid_from=candidate.proposed_claim.valid_from,
        valid_until=candidate.proposed_claim.valid_until,
        confidence=ConfidenceTier(candidate.proposed_confidence),
        source_ref=candidate.source_ref,
        knowledge_root=get_shared_knowledge_root(programs_root),
    )
    append_triage_decision(
        KnowledgeCandidateDecisionRecord(
            candidate_id=candidate.candidate_id,
            kind="approved",
            decided_at=datetime.now(timezone.utc),
            triage_actor=actor,
            batch_id=candidate.batch_id,
            edited=False,
            resulting_claim_id=revision.claim_id,
        ),
        programs_root=programs_root,
    )
    typer.echo(f"Approved {candidate.candidate_id} -> {revision.claim_id}")


@triage_app.command("edit")
def triage_edit(
    candidate_id: str = typer.Option(..., "--candidate", help="Candidate ID to edit and approve."),
    actor: str = typer.Option(..., "--actor", help="Operator editing the candidate."),
    subject: str | None = typer.Option(None, "--subject", help="Replacement subject entity id."),
    predicate: str | None = typer.Option(None, "--predicate", help="Replacement predicate."),
    value: str | None = typer.Option(None, "--value", help="Replacement string value."),
    value_json: str | None = typer.Option(None, "--value-json", help="Replacement JSON value. Use 'null' for tombstones."),
    valid_from: str | None = typer.Option(None, "--valid-from", help="Replacement validity start (ISO date or datetime)."),
    valid_until: str | None = typer.Option(None, "--valid-until", help="Replacement validity end (ISO date or datetime)."),
    reason: str | None = typer.Option(None, "--reason", help="Optional edit rationale."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    candidate = _require_candidate(candidate_id, programs_root=programs_root)
    _ensure_candidate_triageable(candidate_id, programs_root=programs_root)
    edited_subject = subject or _effective_candidate_subject(candidate, programs_root=programs_root)
    edited_predicate = predicate or candidate.proposed_claim.predicate
    edited_value = (
        _parse_claim_value(value=value, value_json=value_json)
        if value is not None or value_json is not None
        else candidate.proposed_claim.value
    )
    edited_valid_from = _parse_cli_datetime(valid_from) or candidate.proposed_claim.valid_from
    edited_valid_until = _parse_cli_datetime(valid_until) if valid_until is not None else candidate.proposed_claim.valid_until
    revision = _write_operator_claim(
        scope=candidate.scope,
        subject=edited_subject,
        predicate=edited_predicate,
        value=edited_value,
        valid_from=edited_valid_from,
        valid_until=edited_valid_until,
        actor=actor,
        context=reason or f"edited from candidate {candidate.candidate_id}",
        programs_root=programs_root,
    )
    append_triage_decision(
        KnowledgeCandidateDecisionRecord(
            candidate_id=candidate.candidate_id,
            kind="approved",
            decided_at=datetime.now(timezone.utc),
            triage_actor=actor,
            batch_id=candidate.batch_id,
            reason=reason,
            edited=True,
            resulting_claim_id=revision.claim_id,
        ),
        programs_root=programs_root,
    )
    typer.echo(f"Edited {candidate.candidate_id} -> {revision.claim_id}")


@triage_app.command("batch-approve")
def triage_batch_approve(
    batch_id: str = typer.Option(..., "--batch-id", help="Batch ID to approve."),
    actor: str = typer.Option(..., "--actor", help="Operator approving the batch."),
    min_confidence: float = typer.Option(0.9, "--min-confidence", help="Minimum extraction confidence required for auto-approval."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    candidates = active_knowledge_candidates(programs_root=programs_root, batch_id=batch_id)
    if not candidates:
        typer.echo(f"Batch {batch_id} has no active candidates to approve.")
        return
    batch_status = _build_batch_status(batch_id, programs_root=programs_root, min_confidence=min_confidence)
    if not batch_status["sample_gate"]:
        typer.echo(f"Batch '{batch_id}' failed sample gate.")
        raise typer.Exit(code=3)
    blocked_for_resolution = [
        candidate.candidate_id
        for candidate in candidates
        if any(
            resolution.resolved_entity_id is None
            for resolution in _effective_candidate_entity_resolution(candidate, programs_root=programs_root)
        )
    ]
    if blocked_for_resolution:
        typer.echo(f"Batch '{batch_id}' failed entity-resolution gate.")
        raise typer.Exit(code=3)
    blocked_for_confidence = [candidate.candidate_id for candidate in candidates if candidate.extraction_confidence < min_confidence]
    if blocked_for_confidence:
        typer.echo(f"Batch '{batch_id}' failed confidence gate.")
        raise typer.Exit(code=3)
    approved = 0
    total = len(candidates)
    started_at = monotonic()
    last_report_at = started_at
    for candidate in candidates:
        revision = append_claim_revision(
            scope=candidate.scope,
            subject=_effective_candidate_subject(candidate, programs_root=programs_root),
            predicate=candidate.proposed_claim.predicate,
            value=candidate.proposed_claim.value,
            valid_from=candidate.proposed_claim.valid_from,
            valid_until=candidate.proposed_claim.valid_until,
            confidence=ConfidenceTier(candidate.proposed_confidence),
            source_ref=candidate.source_ref,
            knowledge_root=get_shared_knowledge_root(programs_root),
        )
        append_triage_decision(
            KnowledgeCandidateDecisionRecord(
                candidate_id=candidate.candidate_id,
                kind="approved",
                decided_at=datetime.now(timezone.utc),
                triage_actor=actor,
                batch_id=batch_id,
                edited=False,
                resulting_claim_id=revision.claim_id,
            ),
            programs_root=programs_root,
        )
        approved += 1
        now = monotonic()
        if approved % 10 == 0 or now - last_report_at >= 5 or approved == total:
            typer.echo(_render_batch_approval_progress(approved, total, started_at, now))
            last_report_at = now
    typer.echo(f"Approved {approved} candidates from batch {batch_id}.")


@triage_app.command("batch-status")
def triage_batch_status(
    batch_id: str = typer.Option(..., "--batch-id", help="Batch ID to summarize."),
    min_confidence: float = typer.Option(0.9, "--min-confidence", help="Minimum extraction confidence required for auto-approval."),
    format: str = typer.Option("text", "--format", help="Output format: text or json."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    payload = _build_batch_status(batch_id, programs_root=programs_root, min_confidence=min_confidence)
    normalized_format = format.strip().lower()
    if normalized_format == "json":
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    if normalized_format != "text":
        raise typer.BadParameter("--format must be 'text' or 'json'.")
    typer.echo("KNOWLEDGE BATCH STATUS")
    typer.echo(f"batch={batch_id} total={payload['total_candidates']} active={payload['active_candidates']}")
    typer.echo(
        f"entity_resolution_rate={payload['entity_resolution_rate']:.3f} "
        f"approved_sample_count={payload['approved_sample_count']} required_sample_count={payload['required_sample_count']}"
    )
    typer.echo(
        f"entity_resolution_gate={payload['entity_resolution_gate']} "
        f"sample_gate={payload['sample_gate']} "
        f"confidence_gate={payload['confidence_gate']} min_extraction_confidence={payload['min_extraction_confidence']:.3f}"
    )


def _collapse_extracted_knowledge_candidates(candidates: list[KnowledgeCandidate]) -> tuple[KnowledgeCandidate, ...]:
    grouped: dict[str, list[KnowledgeCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.dedupe_key, []).append(candidate)
    collapsed: list[KnowledgeCandidate] = []
    for dedupe_key in sorted(grouped):
        collapsed.append(_merge_knowledge_candidate_group(grouped[dedupe_key]))
    return tuple(collapsed)


def _merge_knowledge_candidate_group(group: list[KnowledgeCandidate]) -> KnowledgeCandidate:
    primary = group[0]
    corroborating_refs = list(primary.corroborating_refs)
    seen_refs = {_knowledge_source_ref_identity(primary.source_ref)}
    for candidate in group[1:]:
        for ref in (candidate.source_ref, *candidate.corroborating_refs):
            identity = _knowledge_source_ref_identity(ref)
            if identity in seen_refs:
                continue
            corroborating_refs.append(ref)
            seen_refs.add(identity)
    if len(corroborating_refs) == len(primary.corroborating_refs):
        return primary
    return KnowledgeCandidate(
        candidate_id=primary.candidate_id,
        scope=primary.scope,
        proposed_claim=primary.proposed_claim,
        proposed_confidence=primary.proposed_confidence,
        source_ref=primary.source_ref,
        pipeline=primary.pipeline,
        extraction_confidence=primary.extraction_confidence,
        entity_resolution=primary.entity_resolution,
        dedupe_key=primary.dedupe_key,
        source_document_key=primary.source_document_key,
        corroborating_refs=tuple(corroborating_refs),
        batch_id=primary.batch_id,
    )


def _knowledge_source_ref_identity(source_ref: object) -> str:
    return repr(source_ref)


def _collapse_extracted_ledger_candidates(candidates: list[CandidateEvent]) -> tuple[CandidateEvent, ...]:
    grouped: dict[tuple[str, str], list[CandidateEvent]] = {}
    for candidate in candidates:
        grouped.setdefault((candidate.proposed_event_type, candidate.dedupe_core_hash), []).append(candidate)
    collapsed: list[CandidateEvent] = []
    for key in sorted(grouped):
        collapsed.append(_merge_ledger_candidate_group(grouped[key]))
    return tuple(collapsed)


def _merge_ledger_candidate_group(group: list[CandidateEvent]) -> CandidateEvent:
    primary = group[0]
    corroborating_refs = list(primary.corroborating_refs)
    seen_refs = {repr(primary.source_ref)}
    for candidate in group[1:]:
        for ref in (candidate.source_ref, *candidate.corroborating_refs):
            identity = repr(ref)
            if identity in seen_refs:
                continue
            corroborating_refs.append(ref)
            seen_refs.add(identity)
    if len(corroborating_refs) == len(primary.corroborating_refs):
        return primary
    return CandidateEvent(
        candidate_id=primary.candidate_id,
        program_id=primary.program_id,
        proposed_event_type=primary.proposed_event_type,
        proposed_payload=primary.proposed_payload,
        proposed_occurred_at=primary.proposed_occurred_at,
        proposed_temporal_confidence=primary.proposed_temporal_confidence,
        proposed_confidence=primary.proposed_confidence,
        source_ref=primary.source_ref,
        pipeline=primary.pipeline,
        extraction_confidence=primary.extraction_confidence,
        entity_resolution=primary.entity_resolution,
        dedupe_key=primary.dedupe_key,
        dedupe_core_hash=primary.dedupe_core_hash,
        source_document_key=primary.source_document_key,
        corroborating_refs=tuple(corroborating_refs),
        batch_id=primary.batch_id,
        staged_at=primary.staged_at,
    )


def _is_decision_log_source(original_filename: str) -> bool:
    return original_filename.strip().lower() == "decision-log-kb.md"


def _program_id_from_scope(scope: str) -> str | None:
    if not scope.startswith("program:"):
        return None
    program_id = scope.split(":", 1)[1].strip()
    return program_id or None

@triage_app.command("reject")
def triage_reject(
    candidate_id: str = typer.Option(..., "--candidate", help="Candidate ID to reject."),
    actor: str = typer.Option(..., "--actor", help="Operator rejecting the candidate."),
    reason: str | None = typer.Option(None, "--reason", help="Optional rejection reason."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    candidate = _require_candidate(candidate_id, programs_root=programs_root)
    _ensure_candidate_triageable(candidate_id, programs_root=programs_root)
    append_triage_decision(
        KnowledgeCandidateDecisionRecord(
            candidate_id=candidate.candidate_id,
            kind="rejected",
            decided_at=datetime.now(timezone.utc),
            triage_actor=actor,
            batch_id=candidate.batch_id,
            reason=reason,
        ),
        programs_root=programs_root,
    )
    typer.echo(f"Rejected {candidate.candidate_id}")


@triage_app.command("skip")
def triage_skip(
    candidate_id: str = typer.Option(..., "--candidate", help="Candidate ID to skip for now."),
    actor: str = typer.Option(..., "--actor", help="Operator skipping the candidate."),
    reason: str | None = typer.Option(None, "--reason", help="Optional skip reason."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    candidate = _require_candidate(candidate_id, programs_root=programs_root)
    _ensure_candidate_triageable(candidate_id, programs_root=programs_root)
    append_triage_decision(
        KnowledgeCandidateDecisionRecord(
            candidate_id=candidate.candidate_id,
            kind="skipped",
            decided_at=datetime.now(timezone.utc),
            triage_actor=actor,
            batch_id=candidate.batch_id,
            reason=reason,
        ),
        programs_root=programs_root,
    )
    typer.echo(f"Skipped {candidate.candidate_id}")


@triage_app.command("expire-skips")
def triage_expire_skips(
    actor: str = typer.Option("vertex.knowledge.expire_skips", "--actor", help="Actor materializing expired skips."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    expired_candidates = _expired_skipped_candidates(programs_root=programs_root)
    if not expired_candidates:
        typer.echo("No expired skipped candidates to materialize.")
        return
    now = datetime.now(timezone.utc)
    for candidate in expired_candidates:
        append_triage_decision(
            KnowledgeCandidateDecisionRecord(
                candidate_id=candidate.candidate_id,
                kind="rejected",
                decided_at=now,
                triage_actor=actor,
                batch_id=candidate.batch_id,
                reason=f"skip expired after {SKIP_EXPIRY_DAYS} days",
            ),
            programs_root=programs_root,
        )
    typer.echo(f"Materialized {len(expired_candidates)} expired skipped candidate(s).")


def _parse_claim_value(*, value: str | None, value_json: str | None) -> Any:
    if value is not None and value_json is not None:
        raise typer.BadParameter("Provide only one of --value or --value-json.")
    if value_json is not None:
        try:
            return json.loads(value_json)
        except json.JSONDecodeError as error:
            raise typer.BadParameter(f"Invalid --value-json: {error}") from error
    if value is None:
        raise typer.BadParameter("One of --value or --value-json is required.")
    return value


def _write_operator_claim(
    *,
    scope: str,
    subject: str,
    predicate: str,
    value: Any,
    valid_from: datetime,
    valid_until: datetime | None,
    actor: str,
    context: str | None,
    programs_root: Path,
    recorded_at: datetime | None = None,
):
    try:
        validate_predicate_value(predicate, value)
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    now = recorded_at or datetime.now(timezone.utc)
    return append_claim_revision(
        scope=scope,
        subject=subject,
        predicate=predicate,
        value=value,
        valid_from=valid_from,
        valid_until=valid_until,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        source_ref=OperatorAssertionRef(asserted_by=actor, asserted_at=now, context=context),
        knowledge_root=get_shared_knowledge_root(programs_root),
        recorded_at=now,
    )


def _resolve_supersede_target(
    *,
    scope: str | None,
    subject: str | None,
    predicate: str | None,
    claim_id: str | None,
    knowledge_root: Path,
) -> tuple[str, str, str]:
    if claim_id is None:
        if scope is None or subject is None or predicate is None:
            raise typer.BadParameter("Provide --claim-id or all of --scope, --subject, and --predicate.")
        return scope, subject, predicate

    target = find_claim_revision_by_id(claim_id, knowledge_root=knowledge_root)
    if target is None:
        raise typer.BadParameter(f"Unknown claim id: {claim_id}")

    if scope is not None and scope != target.scope:
        raise typer.BadParameter("--scope does not match the referenced claim id.")
    if subject is not None and subject != target.subject:
        raise typer.BadParameter("--subject does not match the referenced claim id.")
    if predicate is not None and predicate != target.predicate:
        raise typer.BadParameter("--predicate does not match the referenced claim id.")
    return target.scope, target.subject, target.predicate


def _require_candidate(candidate_id: str, *, programs_root: Path) -> KnowledgeCandidate:
    for candidate in load_pending_candidates(programs_root=programs_root):
        if candidate.candidate_id == candidate_id:
            return candidate
    raise typer.BadParameter(f"Unknown candidate '{candidate_id}'.")


def _ensure_candidate_triageable(candidate_id: str, *, programs_root: Path) -> None:
    latest = None
    for decision in load_triage_decisions(programs_root=programs_root):
        if decision.candidate_id == candidate_id:
            latest = decision
    if latest is None:
        return
    if latest.kind == "skipped":
        if latest.decided_at + timedelta(days=SKIP_EXPIRY_DAYS) > datetime.now(timezone.utc):
            return
        raise typer.BadParameter(f"Candidate '{candidate_id}' skip has expired.")
    raise typer.BadParameter(f"Candidate '{candidate_id}' already has final decision '{latest.kind}'.")


def _candidate_payload(candidate: KnowledgeCandidate, *, programs_root: Path) -> dict[str, Any]:
    effective_resolution = _effective_candidate_entity_resolution(candidate, programs_root=programs_root)
    return {
        "candidate_id": candidate.candidate_id,
        "scope": candidate.scope,
        "subject": candidate.proposed_claim.subject,
        "effective_subject": _effective_candidate_subject(candidate, programs_root=programs_root),
        "predicate": candidate.proposed_claim.predicate,
        "value": candidate.proposed_claim.value,
        "proposed_confidence": candidate.proposed_confidence,
        "extraction_confidence": candidate.extraction_confidence,
        "source_document_key": candidate.source_document_key,
        "corroborating_ref_count": len(candidate.corroborating_refs),
        "entity_resolution": [
            {
                "raw_name": resolution.raw_name,
                "resolved_entity_id": resolution.resolved_entity_id,
                "match_kind": resolution.match_kind,
                "score": resolution.score,
            }
            for resolution in candidate.entity_resolution
        ],
        "effective_entity_resolution": [
            {
                "raw_name": resolution.raw_name,
                "resolved_entity_id": resolution.resolved_entity_id,
                "match_kind": resolution.match_kind,
                "score": resolution.score,
            }
            for resolution in effective_resolution
        ],
        "batch_id": candidate.batch_id,
    }


def _render_candidate_resolution_summary(candidate: KnowledgeCandidate, *, programs_root: Path) -> str:
    effective_resolution = _effective_candidate_entity_resolution(candidate, programs_root=programs_root)
    if not effective_resolution:
        return "none"
    parts: list[str] = []
    for resolution in effective_resolution:
        target = resolution.resolved_entity_id or "unresolved"
        parts.append(f"{resolution.raw_name}->{target}/{resolution.match_kind}")
    return ",".join(parts)


def _effective_candidate_subject(candidate: KnowledgeCandidate, *, programs_root: Path) -> str:
    effective_resolution = _effective_candidate_entity_resolution(candidate, programs_root=programs_root)
    if len(effective_resolution) == 1 and effective_resolution[0].resolved_entity_id is not None:
        return effective_resolution[0].resolved_entity_id
    return candidate.proposed_claim.subject


def _effective_candidate_entity_resolution(candidate: KnowledgeCandidate, *, programs_root: Path) -> tuple[KnowledgeCandidateEntityResolution, ...]:
    if not candidate.entity_resolution:
        return ()
    registry = _load_entity_registry_for_scope(candidate.scope, programs_root=programs_root)
    effective: list[KnowledgeCandidateEntityResolution] = []
    for resolution in candidate.entity_resolution:
        # ADF-W2.6: resolve_with_binding() so registry drift into a genuine
        # ambiguous near-tie is itself surfaced as a change (a new
        # "ambiguous" match_kind), rather than silently keeping the
        # previously-recorded resolution as if nothing had changed.
        binding = registry.resolve_with_binding(resolution.raw_name)
        if binding.ambiguous:
            effective.append(
                KnowledgeCandidateEntityResolution(
                    raw_name=resolution.raw_name,
                    resolved_entity_id=None,
                    match_kind="ambiguous",
                    score=binding.confidence,
                )
            )
            continue
        resolved = binding.resolved_entity
        if resolved is None:
            effective.append(resolution)
            continue
        if resolved.entity_id == resolution.resolved_entity_id:
            effective.append(resolution)
            continue
        effective.append(
            KnowledgeCandidateEntityResolution(
                raw_name=resolution.raw_name,
                resolved_entity_id=resolved.entity_id,
                match_kind="registry_refresh",
                score=1.0,
            )
        )
    return tuple(effective)


def _load_entity_registry_for_scope(scope: str, *, programs_root: Path) -> EntityRegistry:
    program_id = _program_id_from_scope(scope)
    if program_id is None:
        return EntityRegistry.load("__missing__", programs_root=programs_root, _repo_root=programs_root.parent)
    return EntityRegistry.load(program_id, programs_root=programs_root, _repo_root=programs_root.parent)


def _repo_relative_paths(paths: Any, *, root_path: Path) -> set[str]:
    relative_paths: set[str] = set()
    for path in paths:
        candidate = Path(path)
        try:
            relative_paths.add(candidate.resolve().relative_to(root_path.resolve()).as_posix())
        except ValueError:
            continue
    return relative_paths


def _emit_backup_hits(*, backup_root: Path | None, relative_paths: set[str]) -> None:
    if backup_root is None:
        typer.echo("Backup report skipped; pass --backup-root to inspect existing backup snapshots.")
        return
    hits = find_backups_referencing_paths(backup_root, relative_paths=relative_paths)
    if not hits:
        typer.echo(f"Affected backups: none found under {backup_root.resolve()}.")
        return
    typer.echo("Affected backups:")
    for hit in hits:
        typer.echo(f"- {hit.backup_root}")


def _candidate_references_vault_hash(candidate: KnowledgeCandidate, *, vault_hash: str) -> bool:
    if getattr(candidate.source_ref, "vault_hash", None) == vault_hash:
        return True
    return any(getattr(ref, "vault_hash", None) == vault_hash for ref in candidate.corroborating_refs)


def _candidate_is_active(candidate_id: str, *, programs_root: Path) -> bool:
    return any(candidate.candidate_id == candidate_id for candidate in active_knowledge_candidates(programs_root=programs_root))


def _expired_skipped_candidates(*, programs_root: Path) -> tuple[KnowledgeCandidate, ...]:
    now = datetime.now(timezone.utc)
    latest_decisions: dict[str, KnowledgeCandidateDecisionRecord] = {}
    for decision in load_triage_decisions(programs_root=programs_root):
        latest_decisions[decision.candidate_id] = decision
    expired: list[KnowledgeCandidate] = []
    for candidate in load_pending_candidates(programs_root=programs_root):
        pending_decision = latest_decisions.get(candidate.candidate_id)
        if pending_decision is None or pending_decision.kind != "skipped":
            continue
        if pending_decision.decided_at + timedelta(days=SKIP_EXPIRY_DAYS) > now:
            continue
        expired.append(candidate)
    return tuple(expired)


def _build_batch_status(batch_id: str, *, programs_root: Path, min_confidence: float) -> dict[str, object]:
    candidates = tuple(candidate for candidate in load_pending_candidates(programs_root=programs_root) if candidate.batch_id == batch_id)
    if not candidates:
        raise typer.BadParameter(f"Unknown batch '{batch_id}'.")
    decisions = [decision for decision in load_triage_decisions(programs_root=programs_root) if decision.batch_id == batch_id]
    approved_sample_count = sum(1 for decision in decisions if decision.kind == "approved")
    resolution_complete_count = sum(
        1 for candidate in candidates if _candidate_has_complete_entity_resolution(candidate, programs_root=programs_root)
    )
    total_candidates = len(candidates)
    entity_resolution_rate = resolution_complete_count / total_candidates if total_candidates else 1.0
    active = active_knowledge_candidates(programs_root=programs_root, batch_id=batch_id)
    return {
        "batch_id": batch_id,
        "total_candidates": total_candidates,
        "active_candidates": len(active),
        "decision_counts": {
            "approved": sum(1 for decision in decisions if decision.kind == "approved"),
            "rejected": sum(1 for decision in decisions if decision.kind == "rejected"),
            "skipped": sum(1 for decision in decisions if decision.kind == "skipped"),
        },
        "entity_resolution_rate": entity_resolution_rate,
        "approved_sample_count": approved_sample_count,
        "required_sample_count": _required_sample_count(total_candidates),
        "entity_resolution_gate": entity_resolution_rate >= 0.9,
        "sample_gate": approved_sample_count >= _required_sample_count(total_candidates),
        "confidence_gate": all(candidate.extraction_confidence >= min_confidence for candidate in active),
        "min_extraction_confidence": min((candidate.extraction_confidence for candidate in active), default=1.0),
    }


def _candidate_has_complete_entity_resolution(candidate: KnowledgeCandidate, *, programs_root: Path) -> bool:
    effective_resolution = _effective_candidate_entity_resolution(candidate, programs_root=programs_root)
    if not effective_resolution:
        return False
    return all(resolution.resolved_entity_id is not None for resolution in effective_resolution)


def _required_sample_count(total_candidates: int) -> int:
    if total_candidates <= 0:
        return 0
    return min(total_candidates, max(10, math.ceil(total_candidates * 0.05)))


def _render_batch_approval_progress(processed: int, total: int, started_at: float, now: float) -> str:
    elapsed = max(now - started_at, 0.001)
    rate = processed / elapsed
    remaining = max(total - processed, 0)
    eta_seconds = remaining / rate if rate > 0 else 0.0
    return f"Batch approval progress: {processed}/{total} rows ({rate:.1f}/s, eta {eta_seconds:.1f}s)"


app.add_typer(triage_app, name="triage")


def _parse_cli_datetime(value: str | None) -> datetime | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    if "T" not in normalized:
        normalized = f"{normalized}T00:00:00+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)