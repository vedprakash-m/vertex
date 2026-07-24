from __future__ import annotations

from dataclasses import dataclass
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
from src.core.kb_updates import SharedKnowledgeTempCache, validate_program_kb
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
from src.core.people_entity_schema import check_dir11_compliance, is_legacy_schema_0_entities_document, load_entities_document
from src.core.people_directory_schema import PersonStatus, load_people_directory, load_teams
from src.core.profile_encryption import inspect_people_profiles_file
from src.core.people_registry_privacy_policy import encryption_rank, load_people_registry_privacy_policy
from src.core.people_membership_schema import read_all_memberships
from src.core.people_registry_directory_checks import (
    find_conflict_accountability_findings,
    find_duplicate_identifiers,
    find_manager_and_team_hierarchy_cycles,
    find_stakeholder_lifecycle_violations,
    find_unresolved_references,
)
from src.core.people_query import DEFAULT_STALE_FRESHNESS_DAYS, list_conflicts, list_stale_people
from src.core.program_context import load_program_stakeholder_aliases
from src.core.people_registry_cache import read_cache_status, rebuild_cache
from src.core.people_shared_migration import check_dir05_shadow_compliance
from src.core.people_registry_identity import load_registry_manifest
from src.core.people_registry_modes import load_effective_registry_config
from src.core.people_shadow_parity import compute_shadow_parity
from src.core.people_change_journal import STREAM_PEOPLE_CHANGES, STREAM_PEOPLE_CONFLICTS, read_journal_records, verify_journal_hash_chain
from src.core.identity_provider_port import load_identity_providers_document
from src.core.audience_scopes import audience_scopes_path_for_program, load_audience_scopes
from src.core.people_registry_storage_class import refresh_registry_storage_status
from src.core.people_registry_governance import inspect_registry_manifest_integrity
from src.core.people_legacy_reference_metrics import evaluate_schema_3_0_horizon, summarize_legacy_reference_log
from src.core.people_registry_transaction import detect_stale_registry_lease, recover_registry_transactions
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
        # specs/people.md PPL-W3.5b: shared across every program in this
        # loop so a shared-registry file (e.g. knowledge/people_directory.yaml)
        # is parsed once per `doctor --kb` run, not once per program -- the
        # redundant-reparse cost PPL-W3.5's own scale benchmark measured as
        # the dominant full-doctor bottleneck at real program counts.
        kb_document_cache: dict[Path, dict] = {}
        # specs/people.md PPL-W3.5c: `document_cache` alone still left
        # `_validate_program_documents`'s temp-dir dump/reparse of the
        # SAME cached-but-deepcopied shared documents running once per
        # program -- cProfile against the real 10,000-person/100-program
        # scale fixture found this, not redundant loader parsing, to be
        # the dominant full-doctor cost. `SharedKnowledgeTempCache` dumps
        # the shared subset once and reuses it across the whole loop; see
        # its own docstring in kb_updates.py for the full profiling
        # evidence and design rationale.
        with SharedKnowledgeTempCache() as shared_knowledge_cache:
            for program_dir in program_dirs:
                try:
                    knowledge = validate_program_kb(
                        program_dir.name, programs_root=programs_root,
                        document_cache=kb_document_cache, shared_knowledge_cache=shared_knowledge_cache,
                    )
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

    checks.append(registry_storage_class_check(programs_root=programs_root))
    checks.append(registry_transaction_recovery_check(programs_root=programs_root))
    checks.append(registry_manifest_integrity_check(programs_root=programs_root))
    checks.append(entities_dir11_check(known_program_ids=known_program_ids, programs_root=programs_root))
    checks.append(registry_dir05_shadow_check(known_program_ids=known_program_ids, programs_root=programs_root))
    checks.append(registry_legacy_reference_check(programs_root=programs_root))
    # Loaded ONCE and shared across DIR-01/02/06 -- see _SharedRegistrySnapshot's
    # own docstring for the redundant-reparse regression this closes (PPL-W3.5).
    shared_registry_snapshot = _load_shared_registry_snapshot(programs_root)
    checks.append(registry_dir01_duplicate_identifiers_check(snapshot=shared_registry_snapshot))
    checks.append(registry_dir02_unresolved_references_check(snapshot=shared_registry_snapshot))
    checks.append(registry_dir03_stale_fields_check(programs_root=programs_root))
    checks.append(registry_dir06_hierarchy_cycles_check(snapshot=shared_registry_snapshot))
    checks.append(registry_dir07_journal_integrity_check(programs_root=programs_root))
    checks.append(registry_dir15_shadow_divergence_check(known_program_ids=known_program_ids, programs_root=programs_root))
    checks.append(registry_dir13_cache_check(programs_root=programs_root, snapshot=shared_registry_snapshot))
    checks.append(registry_dir09a_provider_capability_health_check(programs_root=programs_root))
    checks.append(registry_dir09b_provider_configuration_check(programs_root=programs_root))
    checks.append(registry_dir10_audience_scope_check(known_program_ids=known_program_ids, programs_root=programs_root))
    checks.append(registry_dir08_pii_policy_check(programs_root=programs_root, snapshot=shared_registry_snapshot))
    # Loaded ONCE and shared by DIR-04/DIR-12A/DIR-12B -- see
    # _load_program_stakeholder_aliases's own docstring (PPL-W3.2b).
    program_stakeholder_aliases = _load_program_stakeholder_aliases(known_program_ids=known_program_ids, programs_root=programs_root)
    checks.append(registry_dir04_stakeholder_lifecycle_check(program_stakeholder_aliases=program_stakeholder_aliases, snapshot=shared_registry_snapshot))
    dir12_findings = _load_dir12_findings(program_stakeholder_aliases=program_stakeholder_aliases, snapshot=shared_registry_snapshot, programs_root=programs_root)
    checks.append(registry_dir12a_conflict_with_accountability_check(findings=dir12_findings))
    checks.append(registry_dir12b_conflict_without_accountability_check(findings=dir12_findings))

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


def registry_storage_class_check(*, programs_root: Path) -> DoctorCheck:
    """specs/people.md §6.7 PPL-W1.3: "The storage qualification result is
    persisted in registry capability status and surfaced by doctor."
    Independent of whether the registry has been bootstrapped yet -- this
    describes the shared `knowledge/` root's storage medium itself."""
    knowledge_root = get_shared_knowledge_root(programs_root)
    qualification = refresh_registry_storage_status(knowledge_root)
    status = "ok" if qualification.qualified_for_primary else "warn"
    return DoctorCheck(
        "Registry storage class",
        status,
        f"{qualification.storage_class}: {qualification.detail}",
        metadata={"storage_class": qualification.storage_class, "qualified_for_primary": qualification.qualified_for_primary},
    )


def registry_transaction_recovery_check(*, programs_root: Path) -> DoctorCheck:
    """specs/people.md §6.7 PPL-W1.5: "Startup/doctor recovery handles
    every crash point." `recover_registry_transactions` is idempotent and
    safe to run on every `doctor --kb` invocation -- a fully-consistent
    registry produces only `no_action_needed` outcomes. Also surfaces
    crash point 4 (a stale lease) as a `warn`, since that one requires an
    explicit human `--force --reason` call and is never auto-resolved."""
    knowledge_root = get_shared_knowledge_root(programs_root)
    outcomes = recover_registry_transactions(knowledge_root)
    acted = tuple(outcome for outcome in outcomes if outcome.action != "no_action_needed")
    stale_lease = detect_stale_registry_lease(knowledge_root)

    details: list[str] = []
    status = "ok"
    if acted:
        details.append("; ".join(f"{outcome.transaction_id}: {outcome.action}" for outcome in acted))
        status = "warn"
    if stale_lease is not None:
        details.append(
            f"registry lease held by {stale_lease.owner!r} is past its TTL (expired {stale_lease.expires_at.isoformat()}); "
            "run 'vertex kb registry lease release --force --reason <text>' if the holder is confirmed stuck."
        )
        status = "warn"
    if not details:
        details.append(f"No pending transaction recovery needed ({len(outcomes)} transaction(s) already consistent).")

    return DoctorCheck(
        "Registry transaction recovery",
        status,
        " | ".join(details),
        metadata={"recovered_count": len(acted), "stale_lease_owner": stale_lease.owner if stale_lease else None},
    )


def registry_manifest_integrity_check(*, programs_root: Path) -> DoctorCheck:
    """DIR-14: surface manifest-hash drift without silently trusting it."""
    integrity = inspect_registry_manifest_integrity(get_shared_knowledge_root(programs_root))
    if integrity.is_clean:
        return DoctorCheck(
            "Registry manifest integrity",
            "ok",
            "No unadopted managed registry edits detected.",
            metadata={"generation_id": integrity.generation_id, "edit_count": 0, "critical_edit_count": 0},
        )
    critical = tuple(edit for edit in integrity.edits if edit.critical)
    status = "fail" if critical else "warn"
    code = "DIR-14B" if critical else "DIR-14A"
    paths = ", ".join(edit.relative_path for edit in integrity.edits)
    return DoctorCheck(
        "Registry manifest integrity",
        status,
        f"{code}: {len(integrity.edits)} unadopted managed registry edit(s) in {paths}; "
        "run 'vertex kb registry adopt --reason <text> --apply' after review.",
        metadata={
            "generation_id": integrity.generation_id,
            "edit_count": len(integrity.edits),
            "critical_edit_count": len(critical),
            "edits": [
                {
                    "path": edit.relative_path,
                    "changed_fields": list(edit.changed_fields),
                    "critical": edit.critical,
                }
                for edit in integrity.edits
            ],
        },
    )


def entities_dir11_check(*, known_program_ids: tuple[str, ...], programs_root: Path) -> DoctorCheck:
    """specs/people.md §8.3 DIR-11: "Person/org-team entity is program-
    scoped or overrides an org binding." Only meaningful once a program
    has a schema-2.0 `entities.yaml` (PPL-W2A.1) -- no real production
    file has migrated yet, so today this check reports `ok` with an
    explicit "nothing to check yet" detail rather than silently skipping
    (a distinct, later-actionable state from "checked and clean")."""
    knowledge_root = get_shared_knowledge_root(programs_root)
    org_entities_path = knowledge_root / "entities.yaml"
    if not org_entities_path.exists() or is_legacy_schema_0_entities_document(org_entities_path):
        return DoctorCheck("Entities DIR-11", "ok", "No schema-2.0 org entities.yaml yet; nothing to check.")

    try:
        org_document = load_entities_document(org_entities_path)
    except ConfigError as error:
        return DoctorCheck("Entities DIR-11", "fail", f"Could not load org-scope entities.yaml: {error}")
    if org_document is None:
        return DoctorCheck("Entities DIR-11", "ok", "No schema-2.0 org entities.yaml yet; nothing to check.")

    all_violations: list[str] = []
    checked_program_count = 0
    for program_id in known_program_ids:
        program_entities_path = programs_root / program_id / "knowledge" / "entities.yaml"
        if not program_entities_path.exists() or is_legacy_schema_0_entities_document(program_entities_path):
            continue
        try:
            program_document = load_entities_document(program_entities_path)
        except ConfigError as error:
            all_violations.append(f"{program_id}: could not load entities.yaml: {error}")
            continue
        if program_document is None:
            continue
        checked_program_count += 1
        violations = check_dir11_compliance(org_entities=org_document.entities, program_entities=program_document.entities)
        all_violations.extend(f"{program_id}/{v.entity_id}: {v.reason} ({v.detail})" for v in violations)

    if all_violations:
        return DoctorCheck("Entities DIR-11", "fail", "; ".join(all_violations[:5]), metadata={"violation_count": len(all_violations)})
    return DoctorCheck("Entities DIR-11", "ok", f"{checked_program_count} program(s) checked against org-scope entities.yaml; no DIR-11 violations.")


def registry_dir05_shadow_check(*, known_program_ids: tuple[str, ...], programs_root: Path) -> DoctorCheck:
    """specs/people.md §8.3 DIR-05A/05B: a program-local
    `people_directory.yaml`/`teams.yaml` is entirely SHADOWED (never read
    by any runtime path -- §5's "once a shared file exists, the
    corresponding local file is shadowed") once the corresponding shared
    file exists. `migrate-shared --apply` proactively clears any record it
    successfully migrates (see `_clear_migrated_people_from_program_local`
    in `people_shared_migration.py`), so this check exists for anything
    that path doesn't cover -- e.g. a program never run through
    `migrate-shared`, or files touched by some other writer -- and reports
    DIR-05A (info: shadow debris safe to remove) vs DIR-05B (fail: local
    content that no runtime path will ever read, so an operator editing it
    would see the edit silently discarded)."""
    knowledge_root = get_shared_knowledge_root(programs_root)
    shared_people_path = knowledge_root / "people_directory.yaml"
    shared_teams_path = knowledge_root / "teams.yaml"
    if not shared_people_path.exists() and not shared_teams_path.exists():
        return DoctorCheck("Registry DIR-05", "ok", "No shared people_directory.yaml/teams.yaml yet; nothing to check.", code="DIR-05A")

    shared_people_result = load_people_directory(shared_people_path) if shared_people_path.exists() else None
    shared_teams_result = load_teams(shared_teams_path) if shared_teams_path.exists() else None
    shared_people = shared_people_result.people if shared_people_result else ()
    shared_teams = shared_teams_result.teams if shared_teams_result else ()

    all_equivalent: list[str] = []
    all_divergent: list[str] = []
    checked_program_count = 0
    for program_id in known_program_ids:
        program_people_path = programs_root / program_id / "knowledge" / "people_directory.yaml"
        program_teams_path = programs_root / program_id / "knowledge" / "teams.yaml"
        if not program_people_path.exists() and not program_teams_path.exists():
            continue
        program_people_result = load_people_directory(program_people_path) if program_people_path.exists() else None
        program_teams_result = load_teams(program_teams_path) if program_teams_path.exists() else None
        program_people = program_people_result.people if program_people_result else ()
        program_teams = program_teams_result.teams if program_teams_result else ()
        if not program_people and not program_teams:
            continue
        checked_program_count += 1
        equivalent, divergent = check_dir05_shadow_compliance(
            shared_people=shared_people, shared_teams=shared_teams,
            program_people=program_people, program_teams=program_teams,
        )
        all_equivalent.extend(f"{program_id}/{item}" for item in equivalent)
        all_divergent.extend(f"{program_id}: {item}" for item in divergent)

    if all_divergent:
        return DoctorCheck(
            "Registry DIR-05", "fail", "DIR-05B: " + "; ".join(all_divergent[:5]),
            metadata={"divergent_count": len(all_divergent), "equivalent_count": len(all_equivalent)}, code="DIR-05B",
        )
    if all_equivalent:
        return DoctorCheck(
            "Registry DIR-05", "ok",
            f"DIR-05A: {len(all_equivalent)} shadowed program-local record(s) across {checked_program_count} program(s) "
            "are byte/semantic-equivalent to the shared root; safe to remove.",
            metadata={"equivalent_count": len(all_equivalent)}, code="DIR-05A",
        )
    return DoctorCheck(
        "Registry DIR-05", "ok",
        f"{checked_program_count} program(s) with local people/team files checked; no shadowed records found.",
        code="DIR-05A",
    )


def registry_legacy_reference_check(*, programs_root: Path) -> DoctorCheck:
    """specs/backlog.md WO-6 (BL-J1, schema-3.0 horizon): warn-only,
    never-blocking count of `people find`/`teams show` lookups that
    resolved via the legacy alias-keyed compatibility path
    (`P:<alias>`/`person:<alias>`/bare alias) rather than an
    already-canonical `entity_id`. Also surfaces BL-J1's horizon status
    (operator-ratified 2026-07-22: zero reads across 8 consecutive weeks) --
    informational only, never blocking; reaching `met=True` does not by
    itself trigger the schema-3.0 removal, it only makes the removal
    schedulable per WO-6's step 4."""
    knowledge_root = get_shared_knowledge_root(programs_root)
    summary = summarize_legacy_reference_log(knowledge_root)
    horizon = evaluate_schema_3_0_horizon(knowledge_root)
    horizon_note = f" Schema-3.0 horizon: {horizon.reason} ({'MET' if horizon.met else 'not yet met'})."
    if summary.legacy_reference_count == 0:
        return DoctorCheck(
            "Registry legacy references", "ok",
            f"No legacy alias-keyed people/team lookups recorded.{horizon_note}",
            metadata={"legacy_reference_count": 0, "schema_3_0_horizon_met": horizon.met}, code="DIR-16",
        )
    sample = ", ".join(summary.sample_refs)
    return DoctorCheck(
        "Registry legacy references", "warn",
        f"DIR-16: {summary.legacy_reference_count} people/team lookup(s) resolved via the legacy alias-keyed "
        f"path rather than a canonical entity_id; sample refs: {sample}.{horizon_note}",
        metadata={
            "legacy_reference_count": summary.legacy_reference_count,
            "sample_refs": list(summary.sample_refs),
            "schema_3_0_horizon_met": horizon.met,
        },
        code="DIR-16",
    )


def registry_dir08_pii_policy_check(*, programs_root: Path, snapshot: "_SharedRegistrySnapshot") -> DoctorCheck:
    """specs/backlog.md BL-E1 (DIR-08A/08B): compares the people registry's
    actual encryption/reveal/retention posture against the loaded,
    workspace-global privacy policy (`vertex/policies/privacy_policy.yaml`'s
    `people_registry` section, operator-approved 2026-07-24). FAIL
    (DIR-08B) for any required-tier violation (missing reveal allowlist,
    encryption below the required floor, or a departed person past the
    retention deadline still carrying PII fields). WARN (DIR-08A) for
    meeting the required floor but not the recommended (aspirational) bar.
    """
    knowledge_root = get_shared_knowledge_root(programs_root)
    policy = load_people_registry_privacy_policy(knowledge_root=knowledge_root)

    failures: list[str] = []
    warnings: list[str] = []

    # Registry not adopted at all (no registry.yaml) -- nothing to check,
    # same as DIR-05's "no shared file yet" bypass. A workspace that has
    # never turned on the shared people registry has no privacy posture to
    # be non-compliant with.
    effective_config = load_effective_registry_config(knowledge_root)
    if policy.reveal_requires_principal_allowlist and effective_config is not None:
        principals = effective_config.persisted.pii_reveal_principals
        if not principals:
            failures.append(
                "policy requires a non-empty pii_reveal_principals allowlist, "
                "but none is configured (knowledge/registry.yaml)"
            )

    # people_profiles.yaml not created yet -- nothing to check, same reasoning.
    profiles_path = knowledge_root / "people_profiles.yaml"
    profile_status = inspect_people_profiles_file(profiles_path)
    if profile_status.storage != "missing":
        actual_encryption = "sensitive_only" if profile_status.storage == "encrypted" else "none"
        if encryption_rank(actual_encryption) < encryption_rank(policy.default_encryption):
            failures.append(
                f"actual encryption posture ({actual_encryption!r}) is below the required floor "
                f"({policy.default_encryption!r}); {profiles_path.name} storage={profile_status.storage!r}"
            )
        elif encryption_rank(actual_encryption) < encryption_rank(policy.recommended_encryption):
            warnings.append(
                f"actual encryption posture ({actual_encryption!r}) meets the required floor but is below the "
                f"recommended posture ({policy.recommended_encryption!r}); 'all' is not currently achievable -- "
                "no encryption mechanism exists yet for people_directory.yaml/teams.yaml"
            )

    now = datetime.now(timezone.utc)
    overdue: list[str] = []
    for person in snapshot.people:
        if person.status == PersonStatus.DEPARTED and person.departed_at is not None:
            deadline = person.departed_at + timedelta(days=policy.retention_days)
            has_pii = bool(person.contacts) or person.title is not None or person.department is not None
            if now >= deadline and has_pii:
                overdue.append(person.entity_id)
    if overdue:
        failures.append(
            f"{len(overdue)} departed person(s) past the {policy.retention_days}-day retention deadline "
            f"still have PII fields populated: {', '.join(sorted(overdue)[:5])}"
        )

    if failures:
        return DoctorCheck(
            "Registry DIR-08", "fail",
            f"DIR-08B: {'; '.join(failures)}",
            metadata={"failures": failures, "warnings": warnings}, code="DIR-08B",
        )
    if warnings:
        return DoctorCheck(
            "Registry DIR-08", "warn",
            f"DIR-08A: {'; '.join(warnings)}",
            metadata={"warnings": warnings}, code="DIR-08A",
        )
    return DoctorCheck(
        "Registry DIR-08", "ok",
        "PII retention/encryption/reveal posture meets policy.",
        metadata={}, code="DIR-08A",
    )


@dataclass(frozen=True, slots=True)
class _SharedRegistrySnapshot:
    """Loaded ONCE per `run_kb_doctor` call and shared by every per-
    registry-file DIR-* check, instead of each check independently
    re-parsing the same files. Found via PPL-W3.5's 10,000-person scale
    benchmark profiling: the original `_load_shared_registry_records`
    called `load_entities_document` TWICE within a single expression, and
    was itself called once per check (DIR-01/02/06) -- entities.yaml
    alone was being parsed up to 8 times per `doctor --kb` invocation. At
    real scale (thousands of records), each redundant parse costs real
    seconds; this was a major, self-inflicted contributor to a catastrophic
    full-doctor slowdown this session's own PPL-W3.2 checks introduced,
    not a defect in the underlying loaders themselves."""

    entities_path_exists: bool
    has_schema2_entities: bool
    entities: tuple
    people: tuple
    teams: tuple
    memberships: tuple
    redirects: tuple = ()


def _load_shared_registry_snapshot(programs_root: Path) -> _SharedRegistrySnapshot:
    knowledge_root = get_shared_knowledge_root(programs_root)
    entities_path = knowledge_root / "entities.yaml"
    entities_path_exists = entities_path.exists()
    has_schema2_entities = entities_path_exists and not is_legacy_schema_0_entities_document(entities_path)
    entities: tuple = ()
    redirects: tuple = ()
    if has_schema2_entities:
        document = load_entities_document(entities_path)
        entities = document.entities if document is not None else ()
        redirects = document.redirects if document is not None else ()
    people_result = load_people_directory(knowledge_root / "people_directory.yaml")
    teams_result = load_teams(knowledge_root / "teams.yaml")
    memberships = read_all_memberships(knowledge_root)
    return _SharedRegistrySnapshot(
        entities_path_exists=entities_path_exists,
        has_schema2_entities=has_schema2_entities,
        entities=entities,
        people=people_result.people if people_result is not None else (),
        teams=teams_result.teams if teams_result is not None else (),
        memberships=memberships,
        redirects=redirects,
    )


def registry_dir01_duplicate_identifiers_check(*, snapshot: _SharedRegistrySnapshot) -> DoctorCheck:
    """specs/people.md §8.3 DIR-01: schema/version/unknown-key or normalized
    duplicate canonical ID/alias/team ID. Only meaningful once a shared
    schema-2.0 entities.yaml exists."""
    if not snapshot.has_schema2_entities:
        return DoctorCheck("Registry DIR-01", "ok", "No schema-2.0 shared entities.yaml yet; nothing to check.", code="DIR-01")
    violations = find_duplicate_identifiers(entities=snapshot.entities, people=snapshot.people, teams=snapshot.teams)
    if not violations:
        return DoctorCheck("Registry DIR-01", "ok", "No duplicate canonical ID/alias collisions detected.", code="DIR-01")
    return DoctorCheck(
        "Registry DIR-01", "fail",
        f"DIR-01: {len(violations)} duplicate identifier/alias collision(s): " + "; ".join(v.detail for v in violations[:5]),
        metadata={"violation_count": len(violations)}, code="DIR-01",
    )


def registry_dir02_unresolved_references_check(*, snapshot: _SharedRegistrySnapshot) -> DoctorCheck:
    """specs/people.md §8.3 DIR-02: unresolved person/team/membership reference."""
    if not snapshot.has_schema2_entities:
        return DoctorCheck("Registry DIR-02", "ok", "No schema-2.0 shared entities.yaml yet; nothing to check.", code="DIR-02")
    violations = find_unresolved_references(
        entities=snapshot.entities, people=snapshot.people, teams=snapshot.teams, memberships=snapshot.memberships
    )
    if not violations:
        return DoctorCheck("Registry DIR-02", "ok", "No unresolved person/team/membership references detected.", code="DIR-02")
    return DoctorCheck(
        "Registry DIR-02", "fail",
        f"DIR-02: {len(violations)} unresolved reference(s): " + "; ".join(v.detail for v in violations[:5]),
        metadata={"violation_count": len(violations)}, code="DIR-02",
    )


def registry_dir03_stale_fields_check(*, programs_root: Path) -> DoctorCheck:
    """specs/people.md §8.3 DIR-03: required field/membership older than its
    freshness SLA. v1 placeholder threshold (people_query.py's own
    documented caveat -- a real "configured" SLA needs a new
    people_registry section in freshness_policy.yaml, not built yet)."""
    knowledge_root = get_shared_knowledge_root(programs_root)
    stale = list_stale_people(knowledge_root=knowledge_root)
    if not stale:
        return DoctorCheck(
            "Registry DIR-03", "ok",
            f"No fields older than the v1 placeholder freshness window ({DEFAULT_STALE_FRESHNESS_DAYS}d).",
            code="DIR-03",
        )
    return DoctorCheck(
        "Registry DIR-03", "warn",
        f"DIR-03: {len(stale)} field(s) older than the v1 placeholder freshness window ({DEFAULT_STALE_FRESHNESS_DAYS}d); "
        "run 'vertex kb people stale' for the full list.",
        metadata={"stale_count": len(stale)}, code="DIR-03",
    )


def registry_dir06_hierarchy_cycles_check(*, snapshot: _SharedRegistrySnapshot) -> DoctorCheck:
    """specs/people.md §8.3 DIR-06: manager or team hierarchy cycle."""
    if not snapshot.entities_path_exists:
        return DoctorCheck("Registry DIR-06", "ok", "No shared registry data yet; nothing to check.", code="DIR-06")
    violations = find_manager_and_team_hierarchy_cycles(people=snapshot.people, teams=snapshot.teams)
    if not violations:
        return DoctorCheck("Registry DIR-06", "ok", "No manager/team hierarchy cycles detected.", code="DIR-06")
    return DoctorCheck(
        "Registry DIR-06", "fail",
        f"DIR-06: {len(violations)} hierarchy cycle(s): " + "; ".join(v.detail for v in violations[:5]),
        metadata={"violation_count": len(violations)}, code="DIR-06",
    )


def registry_dir07_journal_integrity_check(*, programs_root: Path) -> DoctorCheck:
    """specs/people.md §8.3 DIR-07: shared writer/journal/checkpoint/hash
    integrity failure. Combines the change/conflict journal hash-chain
    verification (PPL-W1.7) with the already-wired transaction-recovery
    check's own crash-consistency evidence."""
    knowledge_root = get_shared_knowledge_root(programs_root)
    manifest = load_registry_manifest(knowledge_root)
    if manifest is None:
        return DoctorCheck("Registry DIR-07", "ok", "Registry not bootstrapped yet; nothing to check.", code="DIR-07")

    problems: list[str] = []
    for stream in (STREAM_PEOPLE_CHANGES, STREAM_PEOPLE_CONFLICTS):
        records = read_journal_records(knowledge_root, stream)
        if not records:
            continue
        verification = verify_journal_hash_chain(records, workspace_id=manifest.workspace_id, stream=stream)
        if not verification.ok:
            problems.append(f"{stream}: {len(verification.violations)} hash-chain violation(s)")

    if not problems:
        return DoctorCheck("Registry DIR-07", "ok", "Journal hash chains verified intact.", code="DIR-07")
    return DoctorCheck(
        "Registry DIR-07", "fail", "DIR-07: " + "; ".join(problems),
        metadata={"problem_count": len(problems)}, code="DIR-07",
    )


def registry_dir15_shadow_divergence_check(*, known_program_ids: tuple[str, ...], programs_root: Path) -> DoctorCheck:
    """specs/people.md §8.3 DIR-15: legacy alias/team/ledger reference
    resolves differently from its shadow canonical ID. A shadow-parity
    divergence (PPL-W2A.7) IS this exact defect by construction -- reused
    wholesale rather than reimplemented."""
    knowledge_root = get_shared_knowledge_root(programs_root)
    effective = load_effective_registry_config(knowledge_root)
    if effective is None:
        return DoctorCheck("Registry DIR-15", "ok", "Registry not bootstrapped yet; nothing to check.", code="DIR-15")

    checked = 0
    diverging: list[str] = []
    for program_id in known_program_ids:
        mode = effective.effective_program_mode(program_id)
        if mode == "legacy":
            continue
        checked += 1
        record = compute_shadow_parity(program_id, programs_root=programs_root)
        if not record.is_zero_divergence:
            diverging.append(f"{program_id}: {len(record.divergences)} divergence(s)")

    if not diverging:
        return DoctorCheck("Registry DIR-15", "ok", f"{checked} shadow/primary program(s) checked; zero shadow-parity divergence.", code="DIR-15")
    return DoctorCheck(
        "Registry DIR-15", "fail", "DIR-15: " + "; ".join(diverging),
        metadata={"diverging_program_count": len(diverging)}, code="DIR-15",
    )


def registry_dir13_cache_check(*, programs_root: Path, snapshot: _SharedRegistrySnapshot | None = None) -> DoctorCheck:
    """specs/people.md §8.3 DIR-13A/13B: compiled registry cache missing/
    stale/corrupted (PPL-W3.4). Rebuilding a stale/missing/corrupt cache
    is safe and idempotent by construction (`rebuild_cache` only reads
    typed source loaders and atomically replaces the disposable cache
    files -- it never touches source YAML), mirroring
    `registry_transaction_recovery_check`'s own established precedent of
    performing a safe corrective action as part of the check rather than
    only reporting.

    `snapshot` (PPL-W3.5d), when supplied by `run_kb_doctor` (already
    loaded once for DIR-01/02/04/06/12), lets a cache rebuild reuse that
    same entities/people/teams data instead of `rebuild_cache`
    independently re-parsing the same shared registry a THIRD time --
    closing the last of PPL-W3.5c's own three named redundant one-time
    full-registry-parse sites. Safe to pass through unconditionally
    (including when `snapshot.has_schema2_entities` is `False`, in which
    case `snapshot.entities` is `()` by construction): `_build_index_rows`'s
    own fresh-load fallback is now equally safe for a legacy schema-0
    `entities.yaml`, a real pre-existing crash bug (`load_entities_document`
    raising `ConfigError`, uncaught here since it isn't `OSError`/
    `ValueError`) fixed at its root in `people_registry_cache.py` as part
    of this same item -- confirmed to reproduce against the pre-fix code
    before fixing, not assumed."""
    knowledge_root = get_shared_knowledge_root(programs_root)
    if load_registry_manifest(knowledge_root) is None:
        return DoctorCheck("Registry DIR-13", "ok", "Registry not bootstrapped yet; nothing to cache.", code="DIR-13A")

    status = read_cache_status(knowledge_root)
    if status.valid:
        return DoctorCheck("Registry DIR-13", "ok", "Registry cache is present and valid.", code="DIR-13A")

    try:
        if snapshot is not None:
            rebuild_cache(knowledge_root, entities=snapshot.entities, people=snapshot.people, teams=snapshot.teams)
        else:
            rebuild_cache(knowledge_root)
    except (OSError, ValueError) as error:
        return DoctorCheck(
            "Registry DIR-13", "warn",
            f"DIR-13B: cache rebuild failed ({error}); source reads continue within budget.",
            code="DIR-13B",
        )
    return DoctorCheck(
        "Registry DIR-13", "ok", f"DIR-13A: cache was {status.reason}; rebuilt successfully.", code="DIR-13A",
    )


_PROVIDER_STALE_REFRESH_DAYS = 30


def _last_provider_refresh_at(records: tuple[dict, ...], provider_name: str) -> datetime | None:
    """`people_changes.jsonl` records PPL-W4.4's `apply_shared_registry_patch`
    call already writes with `source="provider_refresh"` and
    `source_ref=f"{provider}:{refresh_run_id}"` -- reused directly rather
    than waiting on PPL-W4.7's not-yet-built dedicated telemetry stream."""
    matches = [
        record
        for record in records
        if record.get("source") == "provider_refresh" and str(record.get("source_ref") or "").startswith(f"{provider_name}:")
    ]
    if not matches:
        return None
    latest = max(matches, key=lambda record: int(record["sequence"]))
    return datetime.fromisoformat(str(latest["recorded_at"]))


def registry_dir09a_provider_capability_health_check(*, programs_root: Path) -> DoctorCheck:
    """specs/people.md §8.3 DIR-09A: provider capability / last-successful-
    refresh health (INFO). Deferred at PPL-W3.2 ("no real provider/refresh
    module exists yet") -- unblocked by PPL-W4.1-4.4."""
    knowledge_root = get_shared_knowledge_root(programs_root)
    document = load_identity_providers_document(knowledge_root / "identity_providers.yaml")
    if document is None or not document.providers:
        return DoctorCheck("Registry DIR-09", "ok", "No identity_providers.yaml configured yet; nothing to check.", code="DIR-09A")

    manifest = load_registry_manifest(knowledge_root)
    records = read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES) if manifest is not None else ()
    enabled = [provider for provider in document.providers if provider.enabled]
    if not enabled:
        return DoctorCheck("Registry DIR-09", "ok", "No enabled identity providers configured.", code="DIR-09A")
    summaries = [
        f"{provider.name} (contract {provider.capability_contract_version}): "
        + (f"last refresh {last_refresh.isoformat()}" if (last_refresh := _last_provider_refresh_at(records, provider.name)) else "never refreshed")
        for provider in enabled
    ]
    return DoctorCheck("Registry DIR-09", "ok", "DIR-09A: " + "; ".join(summaries), metadata={"provider_count": len(summaries)}, code="DIR-09A")


def registry_dir09b_provider_configuration_check(*, programs_root: Path) -> DoctorCheck:
    """specs/people.md §8.3 DIR-09B: missing/disabled/stale provider for
    configured use (WARN). "Degraded" (elevated error rate) needs
    PPL-W4.7's per-run telemetry, not yet built -- named, not silently
    covered here. A configured-but-never-refreshed provider is reported
    by DIR-09A only, not flagged WARN here, since a freshly-configured
    provider that hasn't run yet is not itself a fault."""
    knowledge_root = get_shared_knowledge_root(programs_root)
    effective = load_effective_registry_config(knowledge_root)
    if effective is None or not effective.effective_provider_refresh_enabled:
        return DoctorCheck("Registry DIR-09", "ok", "Provider refresh is disabled; nothing to check.", code="DIR-09B")

    document = load_identity_providers_document(knowledge_root / "identity_providers.yaml")
    if document is None or not document.providers:
        return DoctorCheck(
            "Registry DIR-09", "warn",
            "DIR-09B: provider_refresh_enabled is true but identity_providers.yaml has no configured providers.",
            code="DIR-09B",
        )
    enabled_providers = [provider for provider in document.providers if provider.enabled]
    if not enabled_providers:
        return DoctorCheck(
            "Registry DIR-09", "warn",
            "DIR-09B: provider_refresh_enabled is true but every configured provider is disabled.",
            code="DIR-09B",
        )

    manifest = load_registry_manifest(knowledge_root)
    records = read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES) if manifest is not None else ()
    now = datetime.now(timezone.utc)
    stale = [
        f"{provider.name}: last refresh {last_refresh.date().isoformat()}"
        for provider in enabled_providers
        if (last_refresh := _last_provider_refresh_at(records, provider.name)) is not None
        and (now - last_refresh) > timedelta(days=_PROVIDER_STALE_REFRESH_DAYS)
    ]
    if not stale:
        return DoctorCheck("Registry DIR-09", "ok", f"{len(enabled_providers)} enabled provider(s) configured; none stale.", code="DIR-09B")
    return DoctorCheck(
        "Registry DIR-09", "warn",
        f"DIR-09B: {len(stale)} stale provider(s) (no refresh in {_PROVIDER_STALE_REFRESH_DAYS}d): " + "; ".join(stale),
        metadata={"stale_provider_count": len(stale)}, code="DIR-09B",
    )


_MIN_SANE_REQUIRE_VERIFIED_WITHIN_DAYS = 1


def registry_dir10_audience_scope_check(*, known_program_ids: tuple[str, ...], programs_root: Path) -> DoctorCheck:
    """specs/people.md §8.3 DIR-10: audience-scope configuration health
    (deferred at PPL-W3.2 for exactly this reason: "Phase 5a audience
    scopes" -- unblocked by PPL-W5a.1-5a.6). Three distinct problems,
    each independently reportable:

    1. `team_refs`/`include_people`/`exclude_people` unresolvable --
       `load_audience_scopes` (PPL-W5a.1) already raises `ConfigError`
       for this at load time; this check just calls it per program and
       surfaces the failure rather than letting it propagate uncaught.
    2. `require_verified_within_days` configured but not a sane positive
       value (<= 0 is nonsensical -- a threshold that excludes everyone
       or excludes nothing depending on sign is a config mistake, not a
       deliberate choice).
    3. The SAME scope id defined in more than one program -- not an
       error (each program's `audience_scopes.yaml` is independently
       valid), but a real naming-collision risk worth surfacing: an
       edition's `audience_scope_ids: [name]` only resolves within its
       OWN program, so a steward skimming two programs' nudge configs
       could easily assume the same name means the same audience across
       both."""
    load_failures: list[str] = []
    threshold_problems: list[str] = []
    scope_ids_by_program: dict[str, list[str]] = {}
    checked_program_count = 0

    for program_id in known_program_ids:
        if not audience_scopes_path_for_program(program_id, programs_root=programs_root).exists():
            continue
        checked_program_count += 1
        try:
            scopes = load_audience_scopes(program_id=program_id, programs_root=programs_root)
        except ConfigError as error:
            load_failures.append(f"{program_id}: {error}")
            continue
        scope_ids_by_program[program_id] = [scope.id for scope in scopes]
        for scope in scopes:
            if scope.require_verified_within_days is not None and scope.require_verified_within_days < _MIN_SANE_REQUIRE_VERIFIED_WITHIN_DAYS:
                threshold_problems.append(f"{program_id}/{scope.id}: require_verified_within_days={scope.require_verified_within_days}")

    if checked_program_count == 0:
        return DoctorCheck("Registry DIR-10", "ok", "No audience_scopes.yaml configured yet; nothing to check.", code="DIR-10")
    if load_failures:
        return DoctorCheck(
            "Registry DIR-10", "fail",
            f"DIR-10: {len(load_failures)} program(s) with unresolvable audience-scope reference(s): " + "; ".join(load_failures[:5]),
            metadata={"failed_program_count": len(load_failures)}, code="DIR-10",
        )

    owning_programs_by_scope_id: dict[str, list[str]] = {}
    for program_id, scope_ids in scope_ids_by_program.items():
        for scope_id in scope_ids:
            owning_programs_by_scope_id.setdefault(scope_id, []).append(program_id)
    collisions = [f"{scope_id} ({', '.join(sorted(programs))})" for scope_id, programs in owning_programs_by_scope_id.items() if len(programs) > 1]

    problems = threshold_problems + [f"cross-program scope name collision: {collision}" for collision in collisions]
    if not problems:
        return DoctorCheck("Registry DIR-10", "ok", f"{checked_program_count} program(s) with audience_scopes.yaml checked; no issues.", code="DIR-10")
    return DoctorCheck(
        "Registry DIR-10", "warn",
        "DIR-10: " + "; ".join(problems[:5]),
        metadata={"problem_count": len(problems)}, code="DIR-10",
    )


def _load_program_stakeholder_aliases(*, known_program_ids: tuple[str, ...], programs_root: Path) -> dict[str, frozenset[str]]:
    """specs/people.md PPL-W3.2b/PPL-W3.5e: loaded ONCE and shared by
    DIR-04/DIR-12A/DIR-12B, matching `_load_shared_registry_snapshot`'s
    own established performance discipline (PPL-W3.5).

    PPL-W3.5e: originally called the FULL `load_program_context` (13
    Plane-1 file reads plus invariant computation) purely to extract its
    `stakeholder_aliases` field -- profiled at real 100-program scale as
    ~1,800 `yaml.safe_load` calls, the dominant remaining full-doctor
    cost once PPL-W3.5c/W3.5d exhausted the shared-registry-reparse
    lever. Investigated (not assumed) that `stakeholder_aliases` is
    derivable from `program.yaml` alone, before any of the other 12
    files are read -- switched to `program_context.py`'s new, purely
    additive `load_program_stakeholder_aliases` accessor, which reads
    only that one file. Cuts this loop's I/O from ~13 files/program to 1.

    Real, deliberate behavioral difference from before, covered by a
    dedicated test: a program whose OTHER Plane-1 files (e.g. a broken
    `kpis.yaml`) fail to parse used to be silently skipped entirely by
    the old full-load-then-catch-`ConfigError` pattern, even though its
    OWN stakeholder data in `program.yaml` was fine. The narrow accessor
    no longer skips it -- arguably more correct (DIR-04/12A/12B check
    real, parseable stakeholder data regardless of an unrelated file's
    health), but a real observable change from prior behavior."""
    result: dict[str, frozenset[str]] = {}
    for program_id in known_program_ids:
        try:
            aliases = load_program_stakeholder_aliases(program_id, programs_root=programs_root)
        except ConfigError:
            continue
        if aliases:
            result[program_id] = aliases
    return result


def registry_dir04_stakeholder_lifecycle_check(
    *, program_stakeholder_aliases: dict[str, frozenset[str]], snapshot: _SharedRegistrySnapshot,
) -> DoctorCheck:
    """specs/people.md §8.3 DIR-04 (PPL-W3.2b): active stakeholder/RACI/
    owner reference to an inactive/departed/ambiguous person. Reuses
    `program_context.py`'s already-resolved `stakeholder_aliases` set per
    program -- STK-01/02/03 already guarantee any RACI/owner alias not in
    that program's OWN `stakeholder_register` is caught separately, so
    this set is the complete input, not a partial one."""
    if not snapshot.has_schema2_entities:
        return DoctorCheck("Registry DIR-04", "ok", "No schema-2.0 shared entities.yaml yet; nothing to check.", code="DIR-04")
    if not program_stakeholder_aliases:
        return DoctorCheck("Registry DIR-04", "ok", "No program stakeholder_register entries found; nothing to check.", code="DIR-04")
    violations = find_stakeholder_lifecycle_violations(
        program_stakeholder_aliases=program_stakeholder_aliases,
        entities=snapshot.entities, people=snapshot.people, redirects=snapshot.redirects,
    )
    alias_count = sum(len(aliases) for aliases in program_stakeholder_aliases.values())
    if not violations:
        return DoctorCheck(
            "Registry DIR-04", "ok",
            f"{alias_count} stakeholder alias(es) across {len(program_stakeholder_aliases)} program(s) checked; all resolve to active people.",
            code="DIR-04",
        )
    return DoctorCheck(
        "Registry DIR-04", "warn",
        f"DIR-04: {len(violations)} stakeholder reference(s) to an inactive/departed/ambiguous person: " + "; ".join(v.detail for v in violations[:5]),
        metadata={"violation_count": len(violations)}, code="DIR-04",
    )


def _load_dir12_findings(
    *, program_stakeholder_aliases: dict[str, frozenset[str]], snapshot: _SharedRegistrySnapshot, programs_root: Path,
) -> tuple | None:
    """Loaded ONCE in `run_kb_doctor` and shared by DIR-12A/DIR-12B, same
    discipline as `_load_shared_registry_snapshot`/`_load_program_stakeholder_aliases`
    -- the open-conflict journal read and alias-union computation happen
    once, not once per check. Returns `None` when the registry has not
    been bootstrapped yet (both checks report the same "nothing to check"
    state in that case)."""
    knowledge_root = get_shared_knowledge_root(programs_root)
    if load_registry_manifest(knowledge_root) is None:
        return None
    open_conflicts = list_conflicts(knowledge_root=knowledge_root, status="open")
    if not open_conflicts:
        return ()
    all_aliases: frozenset[str] = frozenset()
    for aliases in program_stakeholder_aliases.values():
        all_aliases |= aliases
    return find_conflict_accountability_findings(
        open_conflicts=open_conflicts, all_program_stakeholder_aliases=all_aliases, entities=snapshot.entities,
    )


def registry_dir12a_conflict_with_accountability_check(*, findings: tuple | None) -> DoctorCheck:
    """specs/people.md §8.3 DIR-12A (PPL-W3.2b): open conflict WITH an
    active accountability reference -- more urgent than DIR-12B, since a
    real RACI/owner reference currently depends on this exact person."""
    if findings is None:
        return DoctorCheck("Registry DIR-12A", "ok", "Registry not bootstrapped yet; nothing to check.", code="DIR-12A")
    with_reference = [finding for finding in findings if finding.has_active_accountability_reference]
    if not with_reference:
        return DoctorCheck("Registry DIR-12A", "ok", f"{len(findings)} open conflict(s), none with an active accountability reference.", code="DIR-12A")
    return DoctorCheck(
        "Registry DIR-12A", "warn",
        f"DIR-12A: {len(with_reference)} open conflict(s) WITH an active accountability reference: " + "; ".join(finding.detail for finding in with_reference[:5]),
        metadata={"count": len(with_reference)}, code="DIR-12A",
    )


def registry_dir12b_conflict_without_accountability_check(*, findings: tuple | None) -> DoctorCheck:
    """specs/people.md §8.3 DIR-12B (PPL-W3.2b): open conflict WITHOUT an
    active accountability reference -- still open and worth resolving,
    but nothing currently depends on it for accountability."""
    if findings is None:
        return DoctorCheck("Registry DIR-12B", "ok", "Registry not bootstrapped yet; nothing to check.", code="DIR-12B")
    without_reference = [finding for finding in findings if not finding.has_active_accountability_reference]
    if not without_reference:
        return DoctorCheck("Registry DIR-12B", "ok", f"{len(findings)} open conflict(s), none without an active accountability reference.", code="DIR-12B")
    return DoctorCheck(
        "Registry DIR-12B", "warn",
        f"DIR-12B: {len(without_reference)} open conflict(s) without an active accountability reference: " + "; ".join(finding.detail for finding in without_reference[:5]),
        metadata={"count": len(without_reference)}, code="DIR-12B",
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
