from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from typing import Any, Callable, cast

import yaml

from src.core.backup import _hash_file
from src.commands.doctor_checks.models import DoctorCheck, DoctorReport, ProgramPeopleReferenceInfo
from src.core.ado_client import ADOClient
from src.core.edition_resolver import ResolvedEdition, resolve_edition
from src.core.exceptions import AuthError, ConfigError, QueryError, QueryTimeoutError
from src.core.knowledge.predicate_registry import count as predicate_count
from src.core.kb_updates import validate_program_kb
from src.core.knowledge_candidate_store import active_candidates as active_knowledge_candidates
from src.core.knowledge_claim_store import load_all_claim_revisions, load_program_knowledge_claims, load_program_knowledge_scopes, resolve_knowledge_context, summarize_knowledge_status, summarize_latest_claim_freshness, summarize_active_knowledge_overrides, summarize_stale_operator_assertions
from src.core.knowledge.vault_integrity import summarize_knowledge_vault_integrity
from src.core.knowledge.vault import load_all_vault_entries, load_scope_sources, load_shared_vault_verify_status
from src.core.ledger.evidence_vault import evidence_vault_entry_status, load_evidence_vault_entries
from src.core.ledger.event_index import load_vault_refs
from src.core.ledger.event_log import read_events
from src.core.ledger.source_refs import OperatorAssertionRef
from src.core.program_reality import ProgramReality
from src.core.knowledge_store import (
    KnowledgeStore,
    detect_people_directory_drift,
    find_unknown_team_program_references,
    get_shared_knowledge_root,
)
from src.core.store_factory import build_trajectory_store_for_program_id


_KB_DRIFT_MIN_ACTIVE_ASSIGNMENTS = 5
_KB_REFERENCE_EXCLUDED_FILENAMES = frozenset({"trusted_baseline.yaml"})
_KB_PROGRAM_REFERENCE_EXCLUDED_KEYS = frozenset({"people", "author_defaults", "distribution_defaults"})


def run_kb_doctor(
    *,
    editions_root: Path,
    programs_root: Path,
    check_origins: bool = False,
    ado_client_factory: Callable[..., ADOClient] = ADOClient,
) -> DoctorReport:
    checks: list[DoctorCheck] = []
    program_dirs = tuple(
        path for path in sorted(programs_root.iterdir(), key=lambda item: item.name.lower())
        if path.is_dir() and (path / "program.yaml").exists()
    ) if programs_root.exists() else ()
    known_program_ids = tuple(path.name for path in program_dirs)

    if not known_program_ids:
        checks.append(DoctorCheck("Knowledge", "fail", "No programs with program.yaml were found under programs/."))
    else:
        knowledge_failures: list[str] = []
        knowledge_by_program: dict[str, KnowledgeStore] = {}
        total_people = 0
        total_queries = 0
        total_engms_pages = 0
        for program_dir in program_dirs:
            try:
                knowledge = validate_program_kb(program_dir.name, programs_root=programs_root)
                knowledge_by_program[program_dir.name] = knowledge
                total_people += len(knowledge.people_directory)
                total_queries += len(knowledge.golden_queries)
                total_engms_pages += len(knowledge.engms_pages)
            except (ConfigError, OSError, ValueError) as error:
                knowledge_failures.append(f"{program_dir.name}: {error}")
        if knowledge_failures:
            detail = "; ".join(knowledge_failures[:2])
            checks.append(DoctorCheck("Knowledge", "fail", detail))
        else:
            checks.append(
                DoctorCheck(
                    "Knowledge",
                    "ok",
                    f"{len(program_dirs)} program knowledge stores loaded ({total_people} people, {total_queries} queries, {total_engms_pages} eng.ms pages).",
                )
            )
            checks.append(knowledge_vault_integrity_check(programs_root=programs_root))
            evidence_vault_check = program_evidence_vault_parity_check(known_program_ids=known_program_ids, programs_root=programs_root)
            if evidence_vault_check is not None:
                checks.append(evidence_vault_check)
            checks.append(knowledge_predicate_registry_check())
            checks.append(knowledge_candidate_latency_check(programs_root=programs_root))
            checks.append(knowledge_claim_freshness_check(known_program_ids=known_program_ids, programs_root=programs_root))
            checks.append(knowledge_operator_assertion_ttl_check(known_program_ids=known_program_ids, programs_root=programs_root))
            checks.append(knowledge_override_check(known_program_ids=known_program_ids, programs_root=programs_root))
            checks.append(knowledge_grounding_check(known_program_ids=known_program_ids, programs_root=programs_root))
            checks.append(knowledge_projection_coverage_check(known_program_ids=known_program_ids, programs_root=programs_root))
            if check_origins:
                checks.append(knowledge_origin_staleness_check(known_program_ids=known_program_ids, programs_root=programs_root))
            shared_program_scope_check = shared_knowledge_scope_check(
                known_program_ids=known_program_ids,
                knowledge_by_program=knowledge_by_program,
                programs_root=programs_root,
            )
            if shared_program_scope_check is not None:
                checks.append(shared_program_scope_check)
            checks.append(kb_drift_check(knowledge_by_program=knowledge_by_program, programs_root=programs_root))

    edition_paths = tuple(sorted(editions_root.glob("*.yaml"), key=lambda item: item.name.lower())) if editions_root.exists() else ()
    # Fallback to the programs tree when the legacy flat editions_root is empty
    # (editions now live under programs/<id>/editions/).
    if not edition_paths:
        edition_paths = tuple(
            sorted(
                list(programs_root.glob("*/editions/*.yaml")) + list(programs_root.glob("_templates/*/editions/*.yaml")),
                key=lambda item: item.name.lower(),
            )
        ) if programs_root.exists() else ()
    resolved_editions = []
    if not edition_paths:
        checks.append(DoctorCheck("Editions", "fail", "No edition declarations were found under editions/."))
    else:
        edition_failures: list[str] = []
        for edition_path in edition_paths:
            try:
                resolved_editions.append(
                    resolve_edition(edition_path.stem, editions_root=editions_root, programs_root=programs_root)
                )
            except (ConfigError, OSError, ValueError) as error:
                edition_failures.append(f"{edition_path.stem}: {error}")
        if edition_failures:
            detail = "; ".join(edition_failures[:2])
            checks.append(DoctorCheck("Editions", "fail", detail))
        else:
            checks.append(DoctorCheck("Editions", "ok", f"{len(edition_paths)} editions resolved successfully."))

    checks.append(saved_query_health_check(resolved_editions, ado_client_factory=ado_client_factory))

    return DoctorReport(edition="knowledge-base", checks=tuple(checks))


def knowledge_vault_integrity_check(*, programs_root: Path) -> DoctorCheck:
    integrity = summarize_knowledge_vault_integrity(programs_root=programs_root)
    verify_status = load_shared_vault_verify_status(programs_root=programs_root)
    verify_age_seconds = None
    if verify_status is not None:
        verify_age_seconds = max(0, int((datetime.now(timezone.utc) - verify_status.verified_at).total_seconds()))
    file_count = integrity.file_count
    missing_meta_count = integrity.missing_meta_count
    hash_mismatch_count = integrity.hash_mismatch_count
    missing_source_record_count = integrity.missing_source_record_count
    missing_claim_ref_count = integrity.missing_claim_ref_count
    missing_candidate_ref_count = integrity.missing_candidate_ref_count
    metadata = {
        "file_count": file_count,
        "missing_meta_count": missing_meta_count,
        "hash_mismatch_count": hash_mismatch_count,
        "missing_source_record_count": missing_source_record_count,
        "missing_claim_ref_count": missing_claim_ref_count,
        "missing_candidate_ref_count": missing_candidate_ref_count,
        "last_deep_verify_at": None if verify_status is None else verify_status.verified_at.isoformat(),
        "last_deep_verify_ok": None if verify_status is None else verify_status.ok,
        "last_deep_verify_age_seconds": verify_age_seconds,
    }
    if file_count == 0 and missing_source_record_count == 0 and missing_claim_ref_count == 0 and missing_candidate_ref_count == 0:
        return DoctorCheck(
            "Knowledge Vault",
            "ok",
            "No shared knowledge vault entries found.",
            metadata=metadata,
        )
    if missing_meta_count or hash_mismatch_count or missing_source_record_count or missing_claim_ref_count or missing_candidate_ref_count:
        detail_parts: list[str] = []
        if missing_meta_count:
            detail_parts.append(f"missing metadata for {missing_meta_count} vault file(s)")
        if hash_mismatch_count:
            detail_parts.append(f"content hash mismatch for {hash_mismatch_count} vault file(s)")
        if missing_source_record_count:
            detail_parts.append(f"missing vault entries referenced by {missing_source_record_count} source registry record(s)")
        if missing_claim_ref_count:
            detail_parts.append(f"missing vault entries referenced by {missing_claim_ref_count} claim(s)")
        if missing_candidate_ref_count:
            detail_parts.append(f"missing vault entries referenced by {missing_candidate_ref_count} active candidate(s)")
        return DoctorCheck(
            "Knowledge Vault",
            "fail",
            f"Shared knowledge vault integrity failed: {'; '.join(detail_parts)}.",
            metadata=metadata,
        )
    return DoctorCheck(
        "Knowledge Vault",
        "ok",
        f"{file_count} shared knowledge vault file(s) verified.",
        metadata=metadata,
    )


def knowledge_override_check(*, known_program_ids: tuple[str, ...], programs_root: Path) -> DoctorCheck:
    summary = summarize_active_knowledge_overrides(programs_root=programs_root)
    override_records = [record for record in summary.records if record.program_id in known_program_ids]
    override_summaries: list[dict[str, Any]] = []
    overrides_by_program: dict[str, list[dict[str, Any]]] = {}
    for record in override_records:
        overrides_by_program.setdefault(record.program_id, []).append(
            {
                "entity_id": record.entity_id,
                "predicate": record.predicate,
                "claim_id": record.claim_id,
                "overridden_claim_ids": list(record.overridden_claim_ids),
            }
        )
    for program_id, overrides in overrides_by_program.items():
        override_summaries.append(
            {
                "program_id": program_id,
                "override_count": len(overrides),
                "overrides": overrides,
            }
        )

    if not override_summaries:
        return DoctorCheck("Knowledge Overrides", "ok", "No active program-scope knowledge overrides detected.")

    override_programs = len(override_summaries)
    override_count = sum(item["override_count"] for item in override_summaries)
    sample = override_summaries[0]
    sample_override = sample["overrides"][0]
    return DoctorCheck(
        "Knowledge Overrides",
        "warn",
        (
            f"{override_count} active knowledge override(s) across {override_programs} program(s); "
            f"{sample['program_id']} overrides {sample_override['entity_id']}/{sample_override['predicate']}."
        ),
        metadata={
            "override_program_count": override_programs,
            "override_count": override_count,
            "programs": override_summaries,
        },
    )


def knowledge_predicate_registry_check() -> DoctorCheck:
    registered = predicate_count()
    metadata = {"predicate_count": registered, "threshold": 100}
    if registered > 100:
        return DoctorCheck(
            "Knowledge Predicates",
            "warn",
            f"Predicate count {registered} exceeds the review threshold 100; review OSD-6 before approving more claim candidates.",
            metadata=metadata,
        )
    return DoctorCheck(
        "Knowledge Predicates",
        "ok",
        f"Predicate count {registered} within the review threshold 100.",
        metadata=metadata,
    )


def knowledge_candidate_latency_check(*, programs_root: Path) -> DoctorCheck:
    summary = summarize_knowledge_status(knowledge_root=get_shared_knowledge_root(programs_root))
    pending_count = summary.pending_candidate_count
    oldest_age_seconds = summary.oldest_pending_candidate_age_seconds
    age_threshold_days = 14
    age_threshold_seconds = age_threshold_days * 24 * 60 * 60
    count_threshold = 100
    pipeline_summary = ", ".join(
        f"{pipeline}={count}"
        for pipeline, count in sorted(summary.pending_candidates_by_pipeline.items())
    ) or "none"
    metadata = {
        "pending_candidate_count": pending_count,
        "pending_candidates_by_pipeline": dict(summary.pending_candidates_by_pipeline),
        "oldest_pending_candidate_created_at": None if summary.oldest_pending_candidate_created_at is None else summary.oldest_pending_candidate_created_at.isoformat(),
        "oldest_pending_candidate_age_seconds": oldest_age_seconds,
        "pending_candidates_missing_created_at_count": summary.pending_candidates_missing_created_at_count,
        "triaged_candidate_count": summary.triaged_candidate_count,
        "latest_triage_decision_at": None if summary.latest_triage_decision_at is None else summary.latest_triage_decision_at.isoformat(),
        "latest_triage_session_actor": summary.latest_triage_session_actor,
        "latest_triage_session_started_at": None if summary.latest_triage_session_started_at is None else summary.latest_triage_session_started_at.isoformat(),
        "latest_triage_session_ended_at": None if summary.latest_triage_session_ended_at is None else summary.latest_triage_session_ended_at.isoformat(),
        "latest_triage_session_decision_count": summary.latest_triage_session_decision_count,
        "latest_triage_session_duration_seconds": summary.latest_triage_session_duration_seconds,
        "latest_triage_session_throughput_per_minute": summary.latest_triage_session_throughput_per_minute,
        "triage_session_gap_minutes": summary.triage_session_gap_minutes,
        "batch_count": summary.batch_count,
        "staged_batch_count": summary.staged_batch_count,
        "approved_batch_count": summary.approved_batch_count,
        "quarantined_batch_count": summary.quarantined_batch_count,
        "batches": [dict(batch) for batch in summary.batches[:20]],
        "count_threshold": count_threshold,
        "age_threshold_days": age_threshold_days,
    }
    if pending_count == 0:
        return DoctorCheck(
            "Knowledge Candidates",
            "ok",
            "No active pending knowledge candidates.",
            metadata=metadata,
        )

    detail_parts: list[str] = []
    if pending_count > count_threshold:
        detail_parts.append(f"{pending_count} active pending knowledge candidate(s) exceed the threshold {count_threshold}")
    if oldest_age_seconds is not None and oldest_age_seconds > age_threshold_seconds:
        oldest_age_days = oldest_age_seconds // (24 * 60 * 60)
        detail_parts.append(f"oldest active pending knowledge candidate is {oldest_age_days} day(s) old")
    if detail_parts:
        return DoctorCheck(
            "Knowledge Candidates",
            "warn",
            f"Knowledge candidate queue requires review: {'; '.join(detail_parts)}; pipelines {pipeline_summary}.",
            metadata=metadata,
        )
    return DoctorCheck(
        "Knowledge Candidates",
        "ok",
        f"{pending_count} active pending knowledge candidate(s) within queue thresholds; pipelines {pipeline_summary}.",
        metadata=metadata,
    )


def knowledge_claim_freshness_check(*, known_program_ids: tuple[str, ...], programs_root: Path) -> DoctorCheck:
    expired_claims: list[dict[str, Any]] = []
    expiring_claims: list[dict[str, Any]] = []
    warning_window_days = 30
    for program_id in known_program_ids:
        revisions = load_program_knowledge_claims(program_id, programs_root=programs_root)
        freshness = summarize_latest_claim_freshness(revisions, warning_window_days=warning_window_days)
        for revision in freshness.expired:
            record = {
                "program_id": program_id,
                "claim_id": revision.claim_id,
                "scope": revision.scope,
                "subject": revision.subject,
                "predicate": revision.predicate,
                "valid_until": revision.valid_until.isoformat() if revision.valid_until is not None else "",
            }
            expired_claims.append(record)
        for revision in freshness.expiring_soon:
            record = {
                "program_id": program_id,
                "claim_id": revision.claim_id,
                "scope": revision.scope,
                "subject": revision.subject,
                "predicate": revision.predicate,
                "valid_until": revision.valid_until.isoformat() if revision.valid_until is not None else "",
            }
            expiring_claims.append(record)

    metadata = {
        "expired_count": len(expired_claims),
        "expiring_soon_count": len(expiring_claims),
        "expired_claims": expired_claims[:20],
        "expiring_soon_claims": expiring_claims[:20],
        "warning_window_days": warning_window_days,
    }
    if expired_claims or expiring_claims:
        detail_parts: list[str] = []
        if expired_claims:
            detail_parts.append(f"{len(expired_claims)} latest claim revision(s) already expired")
        if expiring_claims:
            detail_parts.append(f"{len(expiring_claims)} latest claim revision(s) expire within {warning_window_days} days")
        return DoctorCheck(
            "Knowledge Freshness",
            "warn",
            f"Knowledge claim freshness requires review: {'; '.join(detail_parts)}.",
            metadata=metadata,
        )
    return DoctorCheck(
        "Knowledge Freshness",
        "ok",
        "No latest knowledge claim revisions are expired or nearing expiry.",
        metadata=metadata,
    )


def knowledge_operator_assertion_ttl_check(*, known_program_ids: tuple[str, ...], programs_root: Path) -> DoctorCheck:
    stale_records: list[dict[str, Any]] = []
    age_threshold_days = 180
    now = datetime.now(timezone.utc)
    for program_id in known_program_ids:
        revisions = load_program_knowledge_claims(program_id, programs_root=programs_root)
        summary = summarize_stale_operator_assertions(revisions, now=now, age_threshold_days=age_threshold_days)
        for revision in summary.stale_without_ttl:
            asserted_at = cast(OperatorAssertionRef, revision.source_ref).asserted_at
            stale_records.append(
                {
                    "program_id": program_id,
                    "claim_id": revision.claim_id,
                    "scope": revision.scope,
                    "subject": revision.subject,
                    "predicate": revision.predicate,
                    "asserted_at": asserted_at.isoformat(),
                    "stale_age_days": max(0, int((now - asserted_at).total_seconds() // (24 * 60 * 60))),
                }
            )

    metadata = {
        "stale_without_ttl_count": len(stale_records),
        "stale_without_ttl": stale_records[:20],
        "age_threshold_days": age_threshold_days,
    }
    if stale_records:
        sample = stale_records[0]
        return DoctorCheck(
            "Knowledge Operator Assertions",
            "warn",
            (
                f"{len(stale_records)} latest operator assertion claim(s) exceed {age_threshold_days} days without TTL; "
                f"sample {sample['program_id']} {sample['subject']}/{sample['predicate']} is {sample['stale_age_days']} day(s) old."
            ),
            metadata=metadata,
        )
    return DoctorCheck(
        "Knowledge Operator Assertions",
        "ok",
        "No latest operator assertion claims exceed the TTL review threshold.",
        metadata=metadata,
    )


def knowledge_grounding_check(*, known_program_ids: tuple[str, ...], programs_root: Path) -> DoctorCheck:
    superseded_claim_ids = {
        revision.supersedes
        for revision in load_all_claim_revisions(knowledge_root=get_shared_knowledge_root(programs_root))
        if isinstance(revision.supersedes, str) and revision.supersedes
    }
    if not superseded_claim_ids:
        return DoctorCheck("Knowledge Grounding", "ok", "No superseded grounded claim references detected.")

    superseded_groundings: list[dict[str, Any]] = []
    for program_id in known_program_ids:
        for event in read_events(program_id, programs_root=programs_root):
            grounded_in = event.payload.get("grounded_in")
            if not isinstance(grounded_in, list):
                continue
            stale_refs = [claim_id for claim_id in grounded_in if isinstance(claim_id, str) and claim_id in superseded_claim_ids]
            if not stale_refs:
                continue
            superseded_groundings.append(
                {
                    "program_id": program_id,
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "claim_ids": stale_refs,
                }
            )

    metadata = {
        "superseded_event_count": len(superseded_groundings),
        "superseded_groundings": superseded_groundings[:20],
    }
    if superseded_groundings:
        return DoctorCheck(
            "Knowledge Grounding",
            "warn",
            f"{len(superseded_groundings)} event(s) still reference superseded grounded claim revision(s).",
            metadata=metadata,
        )
    return DoctorCheck(
        "Knowledge Grounding",
        "ok",
        "No superseded grounded claim references detected.",
        metadata=metadata,
    )


def knowledge_projection_coverage_check(*, known_program_ids: tuple[str, ...], programs_root: Path) -> DoctorCheck:
    coverage_summaries: list[dict[str, Any]] = []
    total_entities = 0
    total_stub = 0
    total_absent = 0
    for program_id in known_program_ids:
        revisions = load_program_knowledge_claims(program_id, programs_root=programs_root)
        entity_ids = tuple(sorted({revision.subject for revision in revisions}))
        if not entity_ids:
            continue
        context = ProgramReality.load(program_id, programs_root=programs_root).knowledge_context(entity_ids)
        entries = [entry for entry in context.entries if entry.claims]
        if not entries:
            continue
        present = sum(1 for entry in entries if entry.projection_coverage == "present")
        stub = sum(1 for entry in entries if entry.projection_coverage == "stub")
        absent = sum(1 for entry in entries if entry.projection_coverage == "absent")
        total_entities += len(entries)
        total_stub += stub
        total_absent += absent
        if not stub and not absent:
            continue
        uncovered_entities = [entry.entity_id for entry in entries if entry.projection_coverage != "present"]
        coverage_summaries.append(
            {
                "program_id": program_id,
                "entity_count": len(entries),
                "present_count": present,
                "stub_count": stub,
                "absent_count": absent,
                "uncovered_entities": uncovered_entities[:10],
            }
        )

    metadata = {
        "entity_count": total_entities,
        "stub_count": total_stub,
        "absent_count": total_absent,
        "programs": coverage_summaries,
    }
    if not total_entities:
        return DoctorCheck(
            "Knowledge Coverage",
            "ok",
            "No active knowledge entities resolved for projection coverage.",
            metadata=metadata,
        )
    if coverage_summaries:
        sample = coverage_summaries[0]
        return DoctorCheck(
            "Knowledge Coverage",
            "warn",
            (
                f"{total_stub + total_absent} active knowledge entit(ies) across {len(coverage_summaries)} program(s) "
                f"resolve outside current ledger history; {sample['program_id']} has {sample['stub_count']} stub and {sample['absent_count']} absent entity entries."
            ),
            metadata=metadata,
        )
    return DoctorCheck(
        "Knowledge Coverage",
        "ok",
        f"All {total_entities} active knowledge entit(ies) map to current program projection coverage.",
        metadata=metadata,
    )


def knowledge_origin_staleness_check(*, known_program_ids: tuple[str, ...], programs_root: Path) -> DoctorCheck:
    source_records = _load_program_scope_sources(known_program_ids=known_program_ids, programs_root=programs_root)
    if not source_records:
        return DoctorCheck("Knowledge Origins", "ok", "No knowledge origin files found for staleness verification.")

    changed_records: list[dict[str, Any]] = []
    missing_records: list[dict[str, Any]] = []
    verified_count = 0
    for source in source_records:
        origin_path = Path(source.origin_path) if source.origin_path else None
        if origin_path is None or not origin_path.exists() or not origin_path.is_file():
            missing_records.append({
                "scope": source.scope,
                "vault_hash": source.vault_hash,
                "origin_path": source.origin_path,
            })
            continue
        actual_hash = _hash_file(origin_path)
        if actual_hash != source.vault_hash:
            changed_records.append(
                {
                    "scope": source.scope,
                    "vault_hash": source.vault_hash,
                    "origin_path": source.origin_path,
                    "current_hash": actual_hash,
                }
            )
            continue
        verified_count += 1

    metadata = {
        "checked_source_count": len(source_records),
        "verified_count": verified_count,
        "changed_count": len(changed_records),
        "missing_count": len(missing_records),
        "changed_sources": changed_records[:10],
        "missing_sources": missing_records[:10],
    }
    if changed_records or missing_records:
        detail_parts: list[str] = []
        if changed_records:
            detail_parts.append(f"{len(changed_records)} origin file(s) changed since ingest")
        if missing_records:
            detail_parts.append(f"{len(missing_records)} origin path(s) unavailable for verification")
        return DoctorCheck(
            "Knowledge Origins",
            "warn",
            f"Knowledge origin staleness detected: {'; '.join(detail_parts)}.",
            metadata=metadata,
        )
    return DoctorCheck(
        "Knowledge Origins",
        "ok",
        f"{verified_count} knowledge origin file(s) match the stored vault content.",
        metadata=metadata,
    )


def _load_program_scope_sources(*, known_program_ids: tuple[str, ...], programs_root: Path) -> tuple[Any, ...]:
    scope_names: set[str] = set()
    for program_id in known_program_ids:
        scope_names.update(load_program_knowledge_scopes(program_id, programs_root=programs_root))
    records_by_hash: dict[tuple[str, str], Any] = {}
    for scope in sorted(scope_names):
        for source in load_scope_sources(scope, programs_root=programs_root):
            records_by_hash[(source.scope, source.vault_hash)] = source
    return tuple(records_by_hash.values())


def program_evidence_vault_parity_check(*, known_program_ids: tuple[str, ...], programs_root: Path) -> DoctorCheck | None:
    missing_refs: list[dict[str, Any]] = []
    hash_mismatch_refs: list[dict[str, Any]] = []
    orphaned_entries: list[dict[str, Any]] = []
    total_refs = 0
    referenced_hashes_by_program: dict[str, set[str]] = {program_id: set() for program_id in known_program_ids}
    for program_id in known_program_ids:
        for vault_hash, ref_owner_id, ref_owner_type, ref_role in load_vault_refs(program_id, programs_root=programs_root):
            if ref_owner_type != "event":
                continue
            total_refs += 1
            referenced_hashes_by_program.setdefault(program_id, set()).add(vault_hash)
            status = evidence_vault_entry_status(program_id=program_id, vault_hash=vault_hash, programs_root=programs_root)
            if status == "ok":
                continue
            record = {
                "program_id": program_id,
                "vault_hash": vault_hash,
                "ref_owner_id": ref_owner_id,
                "ref_role": ref_role,
            }
            if status == "hash_mismatch":
                hash_mismatch_refs.append(record)
            else:
                missing_refs.append(record)

    for program_id in known_program_ids:
        referenced_hashes = referenced_hashes_by_program.get(program_id, set())
        for entry in load_evidence_vault_entries(program_id, programs_root=programs_root):
            if entry.vault_hash in referenced_hashes:
                continue
            orphaned_entries.append(
                {
                    "program_id": entry.program_id,
                    "vault_hash": entry.vault_hash,
                    "content_path": str(entry.content_path),
                    "metadata_path": str(entry.metadata_path),
                }
            )

    if total_refs == 0 and not orphaned_entries:
        return None

    metadata = {
        "indexed_event_vault_ref_count": total_refs,
        "missing_event_vault_ref_count": len(missing_refs),
        "hash_mismatch_event_vault_ref_count": len(hash_mismatch_refs),
        "orphaned_entry_count": len(orphaned_entries),
        "missing_refs": missing_refs[:20],
        "hash_mismatch_refs": hash_mismatch_refs[:20],
        "orphaned_entries": orphaned_entries[:20],
    }
    if missing_refs or hash_mismatch_refs:
        detail_parts: list[str] = []
        if missing_refs:
            detail_parts.append(f"{len(missing_refs)} indexed event vault reference(s) have no matching evidence file")
        if hash_mismatch_refs:
            detail_parts.append(f"{len(hash_mismatch_refs)} indexed event vault reference(s) failed content hash recheck")
        if orphaned_entries:
            detail_parts.append(f"{len(orphaned_entries)} orphaned evidence entrie(s) found")
        return DoctorCheck(
            "Program Evidence Vault",
            "fail",
            f"Program evidence vault parity failed: {'; '.join(detail_parts)}.",
            metadata=metadata,
        )
    if orphaned_entries:
        return DoctorCheck(
            "Program Evidence Vault",
            "warn",
            f"Program evidence vault contains {len(orphaned_entries)} orphaned evidence entrie(s) with no indexed event reference.",
            metadata=metadata,
        )
    return DoctorCheck(
        "Program Evidence Vault",
        "ok",
        f"{total_refs} indexed event vault reference(s) matched program evidence vault files.",
        metadata=metadata,
    )


def shared_knowledge_scope_check(
    *,
    known_program_ids: tuple[str, ...],
    knowledge_by_program: dict[str, KnowledgeStore],
    programs_root: Path,
) -> DoctorCheck | None:
    shared_root = get_shared_knowledge_root(programs_root)
    if not shared_root.exists() or not knowledge_by_program:
        return None

    first_knowledge = next(iter(knowledge_by_program.values()))
    unknown_program_references = find_unknown_team_program_references(
        first_knowledge,
        known_program_ids=known_program_ids,
    )
    if not unknown_program_references:
        return None

    detail = "; ".join(unknown_program_references[:2])
    if len(unknown_program_references) > 2:
        detail = f"{detail}; +{len(unknown_program_references) - 2} more"
    return DoctorCheck(
        "Knowledge Scope",
        "warn",
        f"Shared knowledge references companion programs not staged in this workspace. {detail}",
    )


def kb_drift_check(*, knowledge_by_program: dict[str, KnowledgeStore], programs_root: Path) -> DoctorCheck:
    cutoff_date = datetime.now(timezone.utc).date() - timedelta(days=90)
    saw_any_gather = False
    saw_recent_gather = False
    knowledge_only_unreferenced: set[str] = set()
    knowledge_only_referenced: set[str] = set()
    ado_only: set[str] = set()

    for program_id, knowledge in knowledge_by_program.items():
        recent_identities, recent_repeat_identities, has_any_gather = load_recent_ado_assignees(
            program_id=program_id,
            cutoff_date=cutoff_date,
            programs_root=programs_root,
        )
        saw_any_gather = saw_any_gather or has_any_gather
        if not recent_identities:
            continue
        saw_recent_gather = True
        drift = detect_people_directory_drift(knowledge, recent_identities)
        referenced_aliases = load_program_people_reference_aliases(
            program_id=program_id,
            knowledge=knowledge,
            programs_root=programs_root,
        )
        for alias in drift.knowledge_only_aliases:
            scoped_alias = f"{program_id}/{alias}"
            if alias in referenced_aliases:
                knowledge_only_referenced.add(scoped_alias)
            else:
                knowledge_only_unreferenced.add(scoped_alias)
        repeat_drift = detect_people_directory_drift(knowledge, recent_repeat_identities)
        ado_only.update(f"{program_id}/{alias}" for alias in repeat_drift.ado_only_aliases)

    if not saw_any_gather:
        return DoctorCheck("KB Drift", "ok", "No gathered ADO assignees recorded yet; people drift check skipped.")
    if not saw_recent_gather:
        return DoctorCheck("KB Drift", "warn", "No ADO gather activity within the last 90 days; people drift check skipped.")
    if not knowledge_only_unreferenced and not knowledge_only_referenced and not ado_only:
        return DoctorCheck("KB Drift", "ok", "No people-directory drift detected against recent ADO assignees.")

    retained_summary = collect_retained_reference_summary(
        knowledge_only_referenced,
        knowledge_by_program=knowledge_by_program,
        programs_root=programs_root,
    )
    detail_parts: list[str] = []
    if knowledge_only_unreferenced:
        label = "person" if len(knowledge_only_unreferenced) == 1 else "people"
        detail_parts.append(
            f"{len(knowledge_only_unreferenced)} {label} in knowledge not seen in ADO for 90+ days and unreferenced by current program YAML ({summarize_aliases(knowledge_only_unreferenced)})"
        )
    if knowledge_only_referenced:
        label = "person" if len(knowledge_only_referenced) == 1 else "people"
        detail_parts.append(
            f"{len(knowledge_only_referenced)} retained {label} in knowledge not seen in ADO for 90+ days but still referenced by current program YAML [{summarize_retained_reference_kinds(retained_summary)}; top files: {summarize_retained_reference_files(retained_summary)}] ({summarize_aliases(knowledge_only_referenced)})"
        )
    if ado_only:
        label = "assignee" if len(ado_only) == 1 else "assignees"
        detail_parts.append(
            f"{len(ado_only)} recent repeat ADO {label} (>= {_KB_DRIFT_MIN_ACTIVE_ASSIGNMENTS} active items) missing from people_directory.yaml ({summarize_aliases(ado_only)})"
        )
    metadata: dict[str, Any] = {
        "missing_in_recent_ado": sorted(knowledge_only_unreferenced | knowledge_only_referenced),
        "missing_in_recent_ado_unreferenced": sorted(knowledge_only_unreferenced),
        "missing_in_recent_ado_referenced": sorted(knowledge_only_referenced),
        "missing_from_people_directory": sorted(ado_only),
    }
    if knowledge_only_referenced:
        metadata["retained_reference_kinds"] = retained_summary["kind_counts"]
        metadata["retained_reference_files"] = retained_summary["files"]
    return DoctorCheck("KB Drift", "warn", "; ".join(detail_parts), metadata=metadata)


def load_program_people_reference_aliases(*, program_id: str, knowledge: KnowledgeStore, programs_root: Path) -> set[str]:
    return set(
        load_program_people_references(
            program_id=program_id,
            knowledge=knowledge,
            programs_root=programs_root,
        )
    )


def load_program_people_references(
    *,
    program_id: str,
    knowledge: KnowledgeStore,
    programs_root: Path,
) -> dict[str, ProgramPeopleReferenceInfo]:
    program_dir = programs_root / program_id
    if not program_dir.exists():
        return {}

    alias_index: dict[str, set[str]] = {}
    email_index: dict[str, set[str]] = {}
    display_name_index: dict[str, set[str]] = {}
    for person in getattr(knowledge, "people_directory", ()):
        normalized_alias = normalize_kb_alias(getattr(person, "alias", None))
        if normalized_alias is None:
            continue
        alias_index.setdefault(normalized_alias, set()).add(normalized_alias)

        email = str(getattr(person, "email", "") or "").strip().lower()
        if email:
            email_index.setdefault(email, set()).add(normalized_alias)

        normalized_display_name = normalize_person_reference(getattr(person, "display_name", None))
        if normalized_display_name is not None:
            display_name_index.setdefault(normalized_display_name, set()).add(normalized_alias)

    mutable_references: dict[str, dict[str, set[str]]] = {}
    for yaml_path in sorted(program_dir.glob("*.yaml"), key=lambda item: item.as_posix().lower()):
        if yaml_path.name in _KB_REFERENCE_EXCLUDED_FILENAMES:
            continue
        try:
            document = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if yaml_path.name == "program.yaml":
            document = prune_program_reference_doc(document)
        for scalar_path, raw_value in iter_yaml_scalar_strings_with_paths(document):
            normalized_alias = normalize_kb_alias(raw_value)
            if normalized_alias is not None:
                for alias in alias_index.get(normalized_alias, ()):
                    entry = mutable_references.setdefault(alias, {"kinds": set(), "files": set(), "locations": set()})
                    entry["kinds"].add("alias")
                    entry["files"].add(yaml_path.name)
                    entry["locations"].add(f"{yaml_path.name}:{scalar_path}")

            email = raw_value.strip().lower()
            if email:
                for alias in email_index.get(email, ()):
                    entry = mutable_references.setdefault(alias, {"kinds": set(), "files": set(), "locations": set()})
                    entry["kinds"].add("email")
                    entry["files"].add(yaml_path.name)
                    entry["locations"].add(f"{yaml_path.name}:{scalar_path}")

            normalized_display_name = normalize_person_reference(raw_value)
            if normalized_display_name is not None:
                for alias in display_name_index.get(normalized_display_name, ()):
                    entry = mutable_references.setdefault(alias, {"kinds": set(), "files": set(), "locations": set()})
                    entry["kinds"].add("display_name")
                    entry["files"].add(yaml_path.name)
                    entry["locations"].add(f"{yaml_path.name}:{scalar_path}")
    return {
        alias: ProgramPeopleReferenceInfo(
            kinds=frozenset(values["kinds"]),
            files=frozenset(values["files"]),
            locations=frozenset(values["locations"]),
        )
        for alias, values in mutable_references.items()
    }


def prune_program_reference_doc(document: Any) -> Any:
    if not isinstance(document, dict):
        return document
    return {
        key: value
        for key, value in document.items()
        if key not in _KB_PROGRAM_REFERENCE_EXCLUDED_KEYS
    }


def collect_retained_reference_summary(
    retained_aliases: set[str],
    *,
    knowledge_by_program: dict[str, KnowledgeStore],
    programs_root: Path,
) -> dict[str, Any]:
    counts = {"alias": 0, "display_name": 0, "email": 0}
    file_to_aliases: dict[str, set[str]] = {}
    file_to_locations: dict[str, set[str]] = {}
    reference_cache: dict[str, dict[str, ProgramPeopleReferenceInfo]] = {}
    for scoped_alias in retained_aliases:
        program_id, _, alias = scoped_alias.partition("/")
        knowledge = knowledge_by_program.get(program_id)
        if knowledge is None:
            continue
        program_references = reference_cache.setdefault(
            program_id,
            load_program_people_references(
                program_id=program_id,
                knowledge=knowledge,
                programs_root=programs_root,
            ),
        )
        reference_info = program_references.get(alias, ProgramPeopleReferenceInfo())
        for kind in reference_info.kinds:
            if kind in counts:
                counts[kind] += 1
        for file_name in reference_info.files:
            file_to_aliases.setdefault(file_name, set()).add(scoped_alias)
        for location in reference_info.locations:
            file_name, _, reference_path = location.partition(":")
            if file_name and reference_path:
                file_to_locations.setdefault(file_name, set()).add(reference_path)

    ordered_files = sorted(file_to_aliases.items(), key=lambda item: (-len(item[1]), item[0]))
    return {
        "kind_counts": {kind: count for kind, count in counts.items() if count},
        "files": [
            {
                "aliases": sorted(aliases),
                "path": file_name,
                "reference_locations": sorted(file_to_locations.get(file_name, set())),
                "retained_alias_count": len(aliases),
            }
            for file_name, aliases in ordered_files
        ],
    }


def summarize_retained_reference_kinds(summary: dict[str, Any]) -> str:
    counts: dict[str, int] = summary.get("kind_counts", {})
    parts: list[str] = []
    alias_count: int = counts.get("alias", 0)
    display_name_count: int = counts.get("display_name", 0)
    email_count: int = counts.get("email", 0)
    if alias_count:
        parts.append(f"alias refs: {alias_count}")
    if display_name_count:
        parts.append(f"display-name refs: {display_name_count}")
    if email_count:
        parts.append(f"email refs: {email_count}")
    return ", ".join(parts) if parts else "reference kind unavailable"


def summarize_retained_reference_files(summary: dict[str, Any]) -> str:
    files: list[dict[str, Any]] = summary.get("files", [])
    if not files:
        return "reference files unavailable"
    preview = [f"{entry['path']} x{entry['retained_alias_count']}" for entry in files[:3]]
    if len(files) > 3:
        preview.append(f"+{len(files) - 3} more")
    return ", ".join(preview)


def iter_yaml_scalar_strings_with_paths(value: Any) -> tuple[tuple[str, str], ...]:
    collected: list[tuple[str, str]] = []

    def visit(node: Any, *, path: str) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                child_path = f"{path}.{key}" if path else str(key)
                visit(child, path=child_path)
            return
        if isinstance(node, list):
            for index, child in enumerate(node):
                child_path = f"{path}[{index}]" if path else f"[{index}]"
                visit(child, path=child_path)
            return
        if isinstance(node, str):
            stripped = node.strip()
            if stripped:
                collected.append((path or "$", stripped))

    visit(value, path="")
    return tuple(collected)


def normalize_kb_alias(value: str | None) -> str | None:
    if value is None:
        return None
    alias = value.strip().lower()
    if not alias:
        return None
    if "@" in alias:
        alias = alias.split("@", 1)[0]
    alias = re.sub(r"[^a-z0-9._-]", "", alias)
    return alias or None


def normalize_person_reference(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    return re.sub(r"[^a-z0-9]+", "", normalized) or None


def load_recent_ado_assignees(*, program_id: str, cutoff_date, programs_root: Path) -> tuple[tuple[str, ...], tuple[str, ...], bool]:
    trajectory_store = build_trajectory_store_for_program_id(program_id, programs_root=programs_root)
    work_item_ids = trajectory_store.list_work_item_ids(program_id)
    if not work_item_ids:
        return (), (), False

    identity_counts: dict[str, int] = {}
    saw_any = False
    for work_item_id in work_item_ids:
        points = trajectory_store.read(program_id, work_item_id)
        if not points:
            continue
        latest_point = points[-1]
        saw_any = True
        if latest_point.date < cutoff_date or latest_point.assigned_to is None:
            continue
        identity = latest_point.assigned_to
        identity_counts[identity] = identity_counts.get(identity, 0) + 1
    identities = tuple(sorted(identity_counts))
    repeat_identities = tuple(
        sorted(
            identity
            for identity, count in identity_counts.items()
            if count >= _KB_DRIFT_MIN_ACTIVE_ASSIGNMENTS
        )
    )
    return identities, repeat_identities, saw_any


def summarize_aliases(values: set[str]) -> str:
    ordered = sorted(values)
    if len(ordered) <= 3:
        return ", ".join(ordered)
    return ", ".join([*ordered[:2], f"+{len(ordered) - 2} more"])


def saved_query_health_check(
    resolved_editions: list[ResolvedEdition | None],
    *,
    ado_client_factory: Callable[..., ADOClient] = ADOClient,
) -> DoctorCheck:
    unique_queries: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for resolved in resolved_editions:
        if resolved is None:
            continue
        program = resolved.program
        if program.ado is None:
            continue
        for workstream in resolved.workstreams:
            for query_id in workstream.ado_saved_query_ids:
                key = (program.ado.organization, program.ado.project, query_id)
                if key in seen:
                    continue
                seen.add(key)
                unique_queries.append((program.id, workstream.id, program.ado.organization, query_id))

    if not unique_queries:
        return DoctorCheck("Saved Queries", "ok", "No ado_saved_query_ids declared.")

    clients: dict[tuple[str, str], ADOClient] = {}
    failures: list[str] = []
    checked = 0
    for program_id, workstream_id, organization, query_id in unique_queries:
        resolved = next(entry for entry in resolved_editions if entry is not None and entry.program.id == program_id)
        project = resolved.program.ado.project if resolved.program.ado is not None else ""
        client_key = (organization, project)
        try:
            client = clients.get(client_key)
            if client is None:
                client = ado_client_factory(
                    organization=organization,
                    project=project,
                    timeout=resolved.program.ado.api_timeout_seconds if resolved.program.ado is not None else 30,
                    show_progress=False,
                )
                clients[client_key] = client
            client.get_saved_query(query_id)
            checked += 1
        except (AuthError, QueryError, QueryTimeoutError) as error:
            failures.append(f"{program_id}/{workstream_id}:{query_id} ({error})")

    if failures:
        detail = "; ".join(failures[:2])
        return DoctorCheck("Saved Queries", "warn", f"{len(failures)} broken saved query GUID(s): {detail}")
    return DoctorCheck("Saved Queries", "ok", f"{checked} ADO saved query GUID(s) resolved.")
