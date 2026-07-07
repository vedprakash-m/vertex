"""REV pipeline orchestrator (Zone A) — FR-PCI-1.

specs/program-context-intelligence.md §5.1/§5.10. Coordinates the REV
capability-port pipeline for one retrieval cycle:

    Plan → Enumerate → Resolve identity → Hydrate → (Shield-scan)
         → Extract → Vault admitted excerpts → Stage candidate
         → Layered verification → (triage) → ACCEPTED

The orchestrator is **Zone-A pure**: it depends only on the Zone-A port
protocols (``src.core.rev.ports``), the stores (candidate / evidence /
verification / run-state), the governor, and the normalizer. The Zone-C
enumerator/hydrator and Zone-B extractor/shields are **injected** by the CLI
command (``src/commands/rev.py``), so this module never imports ``src.m365`` /
``src.ai``. Prompt Shields + the verifier are also injected (the verifier is a
callable so Zone A need not import Zone B's verification module).

Crash-safety (§5.10): every durable transition is checkpointed via the
append-only run-state log before the pipeline moves on; ephemeral stages
(hydrated/scanned/extracted_ephemerally) revert to ``hydration_required`` on
resume. A budget stop is **clean** — already-vaulted excerpts and staged
candidates are preserved; only the in-flight ephemeral stage is abandoned.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, replace as dc_replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src.core.jsonl_utils import read_jsonl_records
from src.core.ledger.candidate_store import (
    PROGRAMS_ROOT,
    CandidateEntityResolution,
    CandidateEvent,
    append_candidate,
    derive_candidate_dedupe_key,
)
from src.core.ledger.rev_evidence import (
    build_metadata_defaults,
    store_admitted_excerpt,
)
from src.core.ledger.source_refs import EmailRef
from src.core.rev.governor import BudgetLimits, Governor, GovernorDecision
from src.core.rev.normalizer import dedupe_core_hash_for
from src.core.rev.provenance_gate import admit as provenance_admit
from src.core.rev.ports import (
    CandidateEnumerator,
    ContentHydrator,
    HydratedContent,
    RevExtractor,
)
from src.core.rev.prompt_shields import (
    VERDICT_UNAVAILABLE,
    ChunkShieldResult,
    PromptShields,
    admit_chunks,
)
from src.core.rev.result import Incomplete, Success, is_success
from src.core.rev.run_state import RunState, advance

log = logging.getLogger(__name__)

PIPELINE_VERSION = "rev.pipeline.v1"

# A verifier callable injected by the CLI (Zone B's run_layered_verification).
# Returns the effective verification state string.
LayeredVerifier = Callable[..., str]


@dataclass(frozen=True, slots=True)
class RevCycleReport:
    program_id: str
    correlation_id: str
    enumerated: int
    hydrated: int
    quarantined: int
    metadata_only: int
    candidates_staged: int
    assertions_written: int
    stop_reason: str
    stop_category: str
    breached_budget: str
    shield_degrade: bool          # True if any chunk ran local-only (visible degrade)
    # Phase 1 fields (set by EmlEnumerator/EmlHydrator/KI fixes):
    llm_fallback_count: int = 0
    low_unique_body_count: int = 0
    winmail_skipped_count: int = 0
    date_parse_failures: int = 0
    # Phase 2 fields (set by REV-G8b, P2-7):
    wall_clock_seconds: float = 0.0
    quarantined_files_count: int = 0   # filesystem files in quarantine/ dir (not quarantined items)
    claimed_at_startup_count: int = 0  # files found in claimed/ at startup (prior crash recovery)
    # REV-G6 gap-fill loop (P2-6): number of context-gap status transitions
    # (open→filling / filling→resolved) driven this cycle.
    gap_transitions: int = 0
    processed_successfully: int = 0
    policy_denied: int = 0
    explicitly_skipped: int = 0
    terminal_failures: int = 0
    cycle_integrity_ok: bool = True
    cycle_status: str = "fully_verified"
    source_unreachable: bool = False
    # §6.14.3 / AG-12 / O-13: True when *any* item degraded from the LLM
    # extractor to the deterministic fallback this cycle (i.e. the cycle is
    # *publication-valid* but **not** *quality-valid*). Per-item degradation —
    # the flag summarizes "≥1 item fell back" so ``is_clean_cycle()`` can treat
    # the whole cycle as non-authority-valid without re-deriving it. Distinct
    # from the ``cycle_status`` string (kept for back-compat) and from
    # ``shield_degrade`` (Prompt Shields) / ``source_unreachable`` (counter-source).
    extraction_degraded: bool = False
    # S-5b: per-family shadow↔primary divergence detector (count of natural_keys
    # that differ between the DB snapshot and the YAML shim snapshot per family).
    family_divergence: dict[str, int] = field(default_factory=dict)
    # S-5c: flip gate results for each family evaluated this cycle
    family_flip_results: dict[str, str] = field(default_factory=dict)  # family → action

    def to_dict(self) -> dict[str, Any]:
        return {
            "program_id": self.program_id,
            "correlation_id": self.correlation_id,
            "enumerated": self.enumerated,
            "hydrated": self.hydrated,
            "quarantined": self.quarantined,
            "metadata_only": self.metadata_only,
            "candidates_staged": self.candidates_staged,
            "assertions_written": self.assertions_written,
            "stop_reason": self.stop_reason,
            "stop_category": self.stop_category,
            "breached_budget": self.breached_budget,
            "shield_degrade": self.shield_degrade,
            "llm_fallback_count": self.llm_fallback_count,
            "low_unique_body_count": self.low_unique_body_count,
            "winmail_skipped_count": self.winmail_skipped_count,
            "date_parse_failures": self.date_parse_failures,
            "wall_clock_seconds": self.wall_clock_seconds,
            "quarantined_files_count": self.quarantined_files_count,
            "claimed_at_startup_count": self.claimed_at_startup_count,
            "gap_transitions": self.gap_transitions,
            "processed_successfully": self.processed_successfully,
            "policy_denied": self.policy_denied,
            "explicitly_skipped": self.explicitly_skipped,
            "terminal_failures": self.terminal_failures,
            "cycle_integrity_ok": self.cycle_integrity_ok,
            "cycle_status": self.cycle_status,
            "source_unreachable": self.source_unreachable,
            "extraction_degraded": self.extraction_degraded,
            "family_divergence": dict(self.family_divergence),
            "family_flip_results": dict(self.family_flip_results),
        }


@dataclass(frozen=True, slots=True)
class RevPipelineDeps:
    """Injected port implementations (built by the CLI in Zone C/B)."""

    enumerator: CandidateEnumerator
    hydrator: ContentHydrator
    shields: PromptShields
    # RevExtractor Protocol defined in Zone A ports.py; Zone B concrete impl satisfies
    # it via duck typing (ExtractedClaim return is Any at the Zone A boundary).
    extractor: RevExtractor
    verifier: LayeredVerifier      # run_layered_verification(...) -> effective_state


def _parse_iso(value: str) -> tuple[datetime, bool]:
    """Parse ISO datetime, returning (result, failed). failed=True logs a KI-6 warning.

    Falls back to RFC 2822 (EML ``Date:`` header format, e.g.
    ``Fri, 1 May 2026 04:49:06 +0000``) before giving up.
    """
    if not value:
        return datetime.now(timezone.utc), False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc), False
    except ValueError:
        pass
    # RFC 2822 fallback — standard EML/Outlook Date header format.
    try:
        import email.utils as _eu
        return _eu.parsedate_to_datetime(value).astimezone(timezone.utc), False
    except Exception:
        pass
    log.warning("REV pipeline: _parse_iso could not parse %r — using UTC now (KI-6)", value)
    return datetime.now(timezone.utc), True


def _candidate_id(source_document_key: str, dedupe_core_hash: str) -> str:
    return derive_candidate_dedupe_key(source_document_key, dedupe_core_hash)


def run_rev_cycle(
    *,
    program_id: str,
    intent: Any,
    deps: RevPipelineDeps,
    profile: Any,
    mailbox_tenant_id: str,
    mailbox_principal: str,
    mailbox_container: str,
    correlation_id: str,
    set_at: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
    budget_limits: BudgetLimits | None = None,
) -> RevCycleReport:
    """Run one REV retrieval cycle (§5.1). Returns a structured report.

    The ``intent`` is a ``RetrievalIntent`` (Zone A). Port implementations are
    injected via ``deps`` so this function stays Zone-A pure. The cycle stops
    cleanly on the first budget breach or provider-limited port result.
    """
    now = set_at or datetime.now(timezone.utc)
    _wall_t0 = time.perf_counter()
    governor = Governor(budget_limits or BudgetLimits())
    stop_reason = ""
    stop_category = "complete"
    breached = ""
    shield_degrade = False

    counts = dict(
        enumerated=0,
        hydrated=0,
        quarantined=0,
        metadata_only=0,
        candidates_staged=0,
        assertions_written=0,
        date_parse_failures=0,
        gap_transitions=0,
        processed_successfully=0,
        policy_denied=0,
        explicitly_skipped=0,
        terminal_failures=0,
    )

    # activation.md §6.14.6 / AG-15 — load the program EntityRegistry once per
    # cycle so every staged candidate's person/owner refs resolve against it.
    # Defensive: an empty/missing registry yields all-UNRESOLVED bindings (the
    # honest "0 resolved" coverage state) rather than failing the cycle.
    entity_registry = _load_entity_registry(program_id, programs_root)

    # 1. Enumerate.
    enum_result = deps.enumerator.enumerate(intent, correlation_id=correlation_id)
    decision = governor.decide_for_port_result(enum_result)
    if not decision.continue_run:
        return _finalize_report(
            program_id=program_id,
            correlation_id=correlation_id,
            counts=counts,
            stop_reason=decision.reason,
            stop_category=decision.category,
            breached=decision.breached_budget,
            shield_degrade=shield_degrade,
            wall_seconds=time.perf_counter() - _wall_t0,
            deps=deps,
            programs_root=programs_root,
        )
    if not isinstance(enum_result, (Success, Incomplete)):
        # Unsupported / Forbidden / RateLimited — skip cleanly (not a run stop).
        return _report(program_id, correlation_id, counts, GovernorDecision(continue_run=True), shield_degrade)
    candidates = enum_result.value if hasattr(enum_result, "value") else ()
    counts["enumerated"] = len(candidates)

    # Priority ordering (§5.10): process highest-relevance items first so the
    # budget is consumed on the most valuable candidates.
    sorted_candidates = sorted(candidates, key=lambda c: c.relevance_score, reverse=True)

    for idx, candidate in enumerate(sorted_candidates):
        # Quiet-lane early exit (§5.10): if all *remaining* candidates score
        # below the relevance threshold, exit without consuming further budget.
        remaining_scores = tuple(c.relevance_score for c in sorted_candidates[idx:])
        quiet_decision = governor.check_quiet_lane(remaining_scores)
        if not quiet_decision.continue_run and quiet_decision.reason.startswith("quiet_lane:all_below"):
            stop_reason = quiet_decision.reason
            stop_category = quiet_decision.category
            breached = quiet_decision.breached_budget
            counts["explicitly_skipped"] += len(sorted_candidates) - idx
            break
        gov = governor.record_search(intent.entity_type.value)
        if not gov.continue_run:
            stop_reason, stop_category, breached = gov.reason, gov.category, gov.breached_budget
            counts["explicitly_skipped"] += len(sorted_candidates) - idx
            break
        cid = candidate.locator.resource_id
        sub_corr = f"{correlation_id}:{cid}"

        # 2. Resolve locator + require hydration.
        try:
            advance(program_id, cid, RunState.ENUMERATED, RunState.LOCATOR_RESOLVED,
                    correlation_id=sub_corr, programs_root=programs_root, set_at=now)
            advance(program_id, cid, RunState.LOCATOR_RESOLVED, RunState.HYDRATION_REQUIRED,
                    correlation_id=sub_corr, programs_root=programs_root, set_at=now)
        except ValueError:
            # Already past these states on a resume — safe to continue.
            pass

        # 3. Hydrate.
        hydrate_result = deps.hydrator.hydrate(candidate, correlation_id=sub_corr)
        h_decision = governor.decide_for_port_result(hydrate_result)
        if not h_decision.continue_run:
            stop_reason, stop_category, breached = h_decision.reason, h_decision.category, h_decision.breached_budget
            counts["explicitly_skipped"] += len(sorted_candidates) - idx
            break
        if not is_success(hydrate_result):
            counts["quarantined"] += 1
            # File finalization: quarantine the claimed file unless the hydrator
            # reported the path was already missing/not found (nothing to move).
            _reason = getattr(hydrate_result, "reason", "") or ""
            if not _reason.startswith(("eml_path_missing", "eml_not_found")):
                _finalize_source_file(deps, candidate, success=False, reason=_reason or "hydration_unsupported")
            try:
                advance(program_id, cid, RunState.HYDRATION_REQUIRED, RunState.QUARANTINED,
                        correlation_id=sub_corr, programs_root=programs_root, set_at=now)
            except ValueError:
                pass
            continue
        hydrated: HydratedContent = hydrate_result.value
        if hydrated.metadata_only:
            counts["metadata_only"] += 1
        else:
            counts["hydrated"] += 1
        gov = governor.record_hydration(
            item_bytes=len(hydrated.canonical_text.encode("utf-8")),
            chunk_count=len(hydrated.chunks),
        )
        if not gov.continue_run:
            stop_reason, stop_category, breached = gov.reason, gov.category, gov.breached_budget
            counts["explicitly_skipped"] += len(sorted_candidates) - idx
            break
        if hydrated.metadata_only:
            # No extractable body — record metadata-only and move on so the item
            # is visible in run-state (doctor --rev-health) without re-hydration.
            # The file is handled (nothing to stage) → finalize to processed/ so it
            # is not re-hydrated on the next cycle.
            _finalize_source_file(deps, candidate, success=True)
            try:
                advance(program_id, cid, RunState.HYDRATION_REQUIRED, RunState.METADATA_ONLY_STAGED,
                        correlation_id=sub_corr, programs_root=programs_root, set_at=now)
            except ValueError:
                pass
            counts["processed_successfully"] += 1
            continue

        # 3b. Provenance gate — forge-EML mitigation (activation.md §6.14.9 /
        # AG-17 / RK-23). Prompt Shields scan content, not provenance, so a
        # forged EML that agrees with stale ADO could be approved on agreement
        # alone. When a per-program sender allowlist is configured, a sender
        # outside it is quarantined before extraction (never silently dropped).
        # No allowlist configured → gate stays open (honest opt-in degradation).
        sender_raw = str(hydrated.route_metadata.get("sender", ""))
        provenance = provenance_admit(
            sender_raw,
            program_id=program_id,
            programs_root=programs_root,
        )
        if not provenance.admitted and provenance.verdict == "denied":
            counts["quarantined"] += 1
            counts["policy_denied"] += 1
            _finalize_source_file(deps, candidate, success=False, reason=f"provenance_denied:{provenance.sender}")
            try:
                advance(program_id, cid, RunState.HYDRATION_REQUIRED, RunState.QUARANTINED,
                        correlation_id=sub_corr, programs_root=programs_root, set_at=now)
            except ValueError:
                pass
            continue

        # 4. Prompt-shield scan (chunk-by-chunk).
        try:
            advance(program_id, cid, RunState.HYDRATION_REQUIRED, RunState.HYDRATED,
                    correlation_id=sub_corr, programs_root=programs_root, set_at=now)
            advance(program_id, cid, RunState.HYDRATED, RunState.SCANNED,
                    correlation_id=sub_corr, programs_root=programs_root, set_at=now)
        except ValueError:
            pass
        shield_result = deps.shields.scan_chunks(
            hydrated.chunks, source_type=intent.entity_type, correlation_id=sub_corr,
        )
        if is_success(shield_result):
            shield_outcomes: tuple[ChunkShieldResult, ...] = shield_result.value
            if any(r.external_verdict == VERDICT_UNAVAILABLE for r in shield_outcomes):
                shield_degrade = True
            admitted_ids, blocked_ids = admit_chunks(shield_outcomes)
        else:
            admitted_ids, blocked_ids = tuple(c.chunk_id for c in hydrated.chunks), ()
            shield_degrade = True
        admitted_chunks = tuple(c for c in hydrated.chunks if c.chunk_id in admitted_ids)
        if not admitted_chunks and not hydrated.metadata_only:
            counts["quarantined"] += 1
            counts["policy_denied"] += 1
            _finalize_source_file(deps, candidate, success=False, reason="shield_blocked_all")
            try:
                advance(program_id, cid, RunState.SCANNED, RunState.QUARANTINED,
                        correlation_id=sub_corr, programs_root=programs_root, set_at=now)
            except ValueError:
                pass
            continue

        # 5. Extract (ephemeral).
        # KI-4 security gate: rebuild canonical_text from admitted chunks only so
        # blocked content can never reach the LLM extractor via canonical_text.
        # Vaulting (step 6) still uses the original hydrated for provenance tracing.
        admitted_canonical = " ".join(c.text for c in admitted_chunks) if admitted_chunks else ""
        admitted_hydrated = dc_replace(hydrated, chunks=admitted_chunks, canonical_text=admitted_canonical)
        try:
            advance(program_id, cid, RunState.SCANNED, RunState.EXTRACTED_EPHEMERALLY,
                    correlation_id=sub_corr, programs_root=programs_root, set_at=now)
        except ValueError:
            pass
        extract_result = deps.extractor.extract(admitted_hydrated, correlation_id=sub_corr)
        if not is_success(extract_result):
            counts["quarantined"] += 1
            _finalize_source_file(deps, candidate, success=False, reason="extract_failed")
            continue
        claims = extract_result.value if hasattr(extract_result, "value") else ()
        if not claims and not admitted_hydrated.metadata_only:
            # Nothing extractable — record metadata-only and move on (no candidate,
            # no vaulted excerpts) so the item is not re-hydrated on the next run.
            # File handled → finalize to processed/.
            counts["metadata_only"] += 1
            _finalize_source_file(deps, candidate, success=True)
            try:
                advance(program_id, cid, RunState.EXTRACTED_EPHEMERALLY, RunState.METADATA_ONLY_STAGED,
                        correlation_id=sub_corr, programs_root=programs_root, set_at=now)
            except ValueError:
                pass
            counts["processed_successfully"] += 1
            continue

        # 6. Vault admitted excerpts → EvidenceRefs.
        evidence_refs = _vault_evidence(
            program_id=program_id,
            hydrated=hydrated,
            claims=claims,
            profile=profile,
            tenant_id=mailbox_tenant_id,
            principal_mailbox=mailbox_principal,
            container=mailbox_container,
            retrieval_timestamp=now,
            programs_root=programs_root,
        )
        try:
            advance(program_id, cid, RunState.EXTRACTED_EPHEMERALLY, RunState.EXCERPTS_VAULTED,
                    correlation_id=sub_corr, programs_root=programs_root, set_at=now)
        except ValueError:
            pass

        # 7. Stage all candidates (one per claim — REV-G1b multi-claim fix).
        staged_list, iso_failures = _stage_candidates(
            program_id=program_id,
            hydrated=hydrated,
            claims=claims,
            evidence_refs=evidence_refs,
            set_at=now,
            programs_root=programs_root,
            entity_registry=entity_registry,
        )
        counts["date_parse_failures"] += iso_failures
        if staged_list:
            counts["candidates_staged"] += len(staged_list)
            try:
                advance(program_id, cid, RunState.EXCERPTS_VAULTED, RunState.CANDIDATE_STAGED,
                        correlation_id=sub_corr, programs_root=programs_root, set_at=now)
            except ValueError:
                pass

            # 8. Layered verification per candidate (injected verifier).
            evidence_vault_hashes = tuple(e.vault_hash for e in evidence_refs)
            verifier_results: dict[str, str] = {}
            for staged in staged_list:
                eff_state = deps.verifier(
                    program_id=program_id,
                    candidate_id=staged.candidate_id,
                    claims=claims,
                    hydrated=hydrated,
                    evidence_refs=evidence_vault_hashes,
                    set_at=now,
                    programs_root=programs_root,
                )
                verifier_results[staged.candidate_id] = eff_state if isinstance(eff_state, str) else ""
                counts["assertions_written"] += 1
            try:
                advance(program_id, cid, RunState.CANDIDATE_STAGED, RunState.CANDIDATE_VERIFIED,
                        correlation_id=sub_corr, programs_root=programs_root, set_at=now)
            except ValueError:
                pass
            # 9. REV-G6 gap-fill loop driver (P2-6): advance matching context
            # gaps open→filling (candidate staged) and filling→resolved
            # (candidate reached a verified state). Best-effort — a failure logs
            # and never breaks the cycle.
            counts["gap_transitions"] += _drive_gap_lifecycle(
                program_id=program_id,
                staged_list=staged_list,
                claims=claims,
                verifier_results=verifier_results,
                programs_root=programs_root,
            )
            # File finalization: append_candidate (the durable fence) succeeded in
            # _stage_candidates → move claimed/ → processed/ so the file is not
            # re-hydrated on the next cycle (3-dir atomicity model).
            _finalize_source_file(deps, candidate, success=True)
            counts["processed_successfully"] += 1
        else:
            counts["terminal_failures"] += 1
            _finalize_source_file(deps, candidate, success=False, reason="candidate_shape_failed")

        gov = governor.check_wall_clock()
        if not gov.continue_run:
            stop_reason, stop_category, breached = gov.reason, gov.category, gov.breached_budget
            counts["explicitly_skipped"] += len(sorted_candidates) - (idx + 1)
            break

    # P2-14 / OA-4: rotate stale/surplus files out of processed/ → processed/archive/.
    # Best-effort housekeeping at cycle end so the 3-dir atomicity model stays
    # bounded (raw .eml purged from the hot path after 90d / 500 files).
    _rotate_processed_best_effort(deps)

    return _finalize_report(
        program_id=program_id,
        correlation_id=correlation_id,
        counts=counts,
        stop_reason=stop_reason,
        stop_category=stop_category,
        breached=breached,
        shield_degrade=shield_degrade,
        wall_seconds=time.perf_counter() - _wall_t0,
        deps=deps,
        programs_root=programs_root,
    )


def _vault_evidence(
    *,
    program_id: str,
    hydrated: HydratedContent,
    claims: tuple[Any, ...],
    profile: Any,
    tenant_id: str,
    principal_mailbox: str,
    container: str,
    retrieval_timestamp: datetime,
    programs_root: Path,
) -> tuple[Any, ...]:
    """Vault each claim's supporting excerpts → EvidenceRefs (§5.7 Stage 2)."""
    if not claims:
        return ()
    refs: list[Any] = []
    seen_spans: set[tuple[int, int]] = set()
    for claim in claims:
        for span in claim.evidence_spans:
            key = (span.start_codepoint, span.end_codepoint)
            if key in seen_spans:
                continue
            seen_spans.add(key)
            meta = build_metadata_defaults(
                tenant_id=tenant_id,
                principal_mailbox=principal_mailbox,
                container=container,
                canonical_item_id=hydrated.identity.resource_id,
                canonical_route_id=hydrated.route_metadata.get("conversation_id"),
                retrieval_timestamp=retrieval_timestamp,
                profile=profile,
                extraction_model=getattr(claim, "extraction_model", ""),
                extraction_schema_version=getattr(claim, "extraction_schema_version", ""),
                content_safety_result="unavailable",
            )
            ref = store_admitted_excerpt(
                program_id=program_id,
                excerpt_text=span.excerpt_text,
                normalized_source_text=hydrated.canonical_text,
                metadata=meta,
                programs_root=programs_root,
            )
            refs.append(ref)
    return tuple(refs)


def _stage_candidates(
    *,
    program_id: str,
    hydrated: HydratedContent,
    claims: tuple[Any, ...],
    evidence_refs: tuple[Any, ...],
    set_at: datetime,
    programs_root: Path,
    entity_registry: Any = None,
) -> tuple[list[CandidateEvent], int]:
    """Build + append one CandidateEvent per claim (all claims staged, not just strongest).

    Returns (staged_list, date_parse_failures_count). Caller accumulates the
    counter into RevCycleReport.date_parse_failures (KI-6).

    Each candidate gets a unique ``candidate_id`` keyed by
    ``(source_document_key, event_type, dedupe_core_hash)`` so two claims of
    the same type in the same message still get distinct candidates (different
    dedupe_core_hash because the full canonical text + event_type seed differs
    per claim — see dedupe_core_hash_for). This closes REV-G1b (P1-0b): the
    prior code staged only the strongest claim, silently discarding the rest.
    """
    if not claims:
        return [], 0
    received_at, iso_failed = _parse_iso(str(hydrated.route_metadata.get("received_at", "")))
    vault_hash = evidence_refs[0].vault_hash if evidence_refs else None
    from src.core.ledger.source_refs import source_document_key
    source_ref = EmailRef(
        subject=str(hydrated.route_metadata.get("subject", "")),
        sent_at=received_at,
        sender=str(hydrated.route_metadata.get("sender", "")),
        message_id=hydrated.identity.resource_id,
        folder=hydrated.identity.container,
        vault_hash=vault_hash,
        # activation.md §6.12 / O-21 — thread-aware dedup: thread the RFC-2822
        # conversation index into the EmailRef so replies in the same thread
        # that re-assert the same fact dedupe at the candidate store.
        thread_id=_optional_thread_id(hydrated.route_metadata.get("conversation_id")),
    )
    doc_key = source_document_key(source_ref)
    batch_id = f"rev:{set_at.strftime('%Y%m%d%H%M%S')}"
    staged: list[CandidateEvent] = []
    for claim in claims:
        shaped = _shape_ledger_event(claim, hydrated, received_at)
        if shaped is None:
            continue  # unrecognised event type — no false-fact staging (W1-3)
        event_type, payload = shaped
        # Seed the dedupe hash with the event_type so two different claim types
        # from the same message each produce a distinct candidate_id.
        dedupe_core = dedupe_core_hash_for(hydrated.canonical_text, event_type)
        candidate_id = _candidate_id(doc_key, dedupe_core)
        # activation.md §6.14.6 / AG-15 — resolve entity refs (person/owner)
        # against the program EntityRegistry before staging. Unresolved refs are
        # recorded as match_kind="unresolved" (the S-6 gate convention) so
        # orphaned/ambiguous facts are visible at triage, never silently projected.
        entity_resolution = _resolve_candidate_entities(
            payload=payload,
            event_type=event_type,
            registry=entity_registry,
        )
        candidate = CandidateEvent(
            candidate_id=candidate_id,
            program_id=program_id,
            proposed_event_type=event_type,
            proposed_payload=payload,
            proposed_occurred_at=received_at,
            proposed_temporal_confidence="exact",
            proposed_confidence="medium",
            source_ref=source_ref,
            pipeline="rev_mail",
            extraction_confidence=claim.extraction_confidence,
            entity_resolution=entity_resolution,
            dedupe_key=candidate_id,
            dedupe_core_hash=dedupe_core,
            source_document_key=doc_key,
            corroborating_refs=(),
            batch_id=batch_id,
            staged_at=set_at,
            schema_version="1",
            evidence_refs=evidence_refs,
            prompt_version=claim.prompt_version or None,
            extraction_rationale=claim.extraction_rationale,
        )
        # append_candidate returns False if the candidate_id already exists
        # (crash-resume idempotency, P1-0c); include in staged list either way
        # so the run-state transition and verifier call still fire on resume.
        append_candidate(candidate, programs_root=programs_root)
        staged.append(candidate)
    return staged, (1 if iso_failed else 0)


# REV claim classification → registered ledger event type (§5.8). The claim's
# ``event_type`` is the materiality classification; the candidate's
# ``proposed_event_type`` is the ledger event the candidate proposes. The shaper
# derives a schema-valid payload from the claim + hydrated metadata.
#
# All 8 MATERIAL_EVENT_TYPES from Zone B extractor are covered here. Previously
# deployment.started / risk.blocking_milestone / ownership.changed were missing
# (P1-0b / REV-G1b fix — those 3 types silently dropped with no ledger projection).
#
# v2.22 (ADR-0006 R2): deployment.completed/rollback/started + incident.severity_changed
# now map to their OWN faithful event types (deployment.*.v1 / incident.severity_changed.v1)
# instead of being shoehorned into milestone.completed / deliverable.status_changed /
# incident.opened. The previous wrong-type mappings produced false-positive candidates
# that were correct extractions but guaranteed-reject labels (wrong type + Phase-2 scope).
# These 4 claim types remain "detected-but-not-v1-authoritative" per S-0g — surfaced
# with their true type so the quality metric measures type-correctness cleanly.
_CLAIM_TO_LEDGER_EVENT: dict[str, str] = {
    "deployment.completed": "deployment.completed.v1",
    "milestone.completed": "milestone.completed.v1",
    "deployment.rollback": "deployment.rollback.v1",
    "deployment.started": "deployment.started.v1",
    "incident.severity_changed": "incident.severity_changed.v1",
    "commitment.date_set": "commitment.made.v1",
    "risk.blocking_milestone": "risk.raised.v1",
    "ownership.changed": "workstream.owner_changed.v1",
}


def _optional_thread_id(raw: object) -> str | None:
    """Normalize a conversation/thread id for thread-aware dedup (§6.12/O-21).

    The hydrator surfaces the RFC-2822 conversation index under
    ``route_metadata["conversation_id"]``. Empty/whitespace values collapse to
    None so non-threaded mail round-trips unchanged. The value is left as-is
    (it is already a stable, normalized key by the time it reaches the
    pipeline) — this helper exists purely to coerce + sanitize.
    """
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    return value or None


def _entity_id(prefix: str, hydrated: HydratedContent, claim: Any) -> str:
    """Deterministic synthetic entity id from the source message + claim."""
    import hashlib

    digest = hashlib.sha256(
        f"{prefix}|{hydrated.identity.resource_id}|{claim.event_type}".encode("utf-8")
    ).hexdigest()[:12]
    return f"{prefix}:{digest}"


# activation.md §6.14.6 / AG-15 — entity-resolution gate. The payload fields that
# carry a human/entity reference we can resolve against the EntityRegistry. These
# are the per-event-type "ref slots"; an empty/unresolved value is recorded as an
# UNRESOLVED binding so the S-6 entity-binding gate (entity_binding_gate.py) can
# measure coverage and the operator sees orphaned facts at triage (never silently
# projected as if resolved).
_ENTITY_REF_PAYLOAD_FIELDS: dict[str, tuple[str, ...]] = {
    "commitment.made.v1": ("owner_person_id",),
    "workstream.owner_changed.v1": ("new_owner_person_id",),
    "milestone.completed.v1": (),
    "deployment.completed.v1": (),
    "deployment.rollback.v1": (),
    "deployment.started.v1": (),
    "incident.severity_changed.v1": (),
    "risk.raised.v1": (),
}


def _load_entity_registry(program_id: str, programs_root: Path) -> Any:
    """Load the program EntityRegistry once per cycle (activation.md §6.14.6).

    Defensive: a program without ``knowledge/entities.yaml`` (the current NOVA
    state) yields an empty registry, so every ref resolves to UNRESOLVED — the
    gate still reports honest "0 resolved" coverage rather than crashing. Returns
    ``None`` if the registry module is unavailable for any reason.
    """
    try:
        from src.core.entity_registry import EntityRegistry
        return EntityRegistry.load(program_id, programs_root=programs_root)
    except Exception:  # pragma: no cover — defensive: registry must never break staging
        log.debug("entity registry unavailable for program %r — refs will be UNRESOLVED", program_id)
        return None


def _resolve_candidate_entities(
    *,
    payload: dict[str, Any],
    event_type: str,
    registry: Any,
) -> tuple[CandidateEntityResolution, ...]:
    """Resolve the entity refs in a candidate payload against the EntityRegistry.

    Implements the AG-15 "binds to the right entity" contract: each person/owner
    ref slot is resolved (exact → casefold → fuzzy) and recorded with its
    ``match_kind`` + ``score``. Unresolved refs are kept as ``match_kind=
    "unresolved"`` with a ``resolved_entity_id`` of ``None`` — the S-6 gate
    convention — so orphaned/ambiguous facts are visible at triage and never
    silently projected as if they bound to a real entity.
    """
    fields = _ENTITY_REF_PAYLOAD_FIELDS.get(event_type, ())
    # The sender is always a candidate person-ref (it authored the message); it
    # is material for commitment/ownership events and informational elsewhere.
    raw_refs: list[str] = []
    for field_name in fields:
        value = str(payload.get(field_name) or "").strip()
        if value and value.lower() not in {"unknown", "none", ""}:
            raw_refs.append(value)
    # Deduplicate while preserving order (sender often equals owner for replies).
    seen: set[str] = set()
    unique_refs: list[str] = []
    for r in raw_refs:
        key = r.lower()
        if key not in seen:
            seen.add(key)
            unique_refs.append(r)

    resolutions: list[CandidateEntityResolution] = []
    for raw in unique_refs:
        resolved = None
        match_kind = "unresolved"
        score = 0.0
        if registry is not None:
            try:
                resolved = registry.resolve(raw, entity_type="person", scope="program")
            except Exception:  # pragma: no cover — defensive
                resolved = None
        if resolved is not None:
            match_kind = "resolved"
            score = 1.0
        resolutions.append(CandidateEntityResolution(
            raw_name=raw,
            resolved_entity_id=getattr(resolved, "entity_id", None) if resolved is not None else None,
            match_kind=match_kind,
            score=score,
        ))
    return tuple(resolutions)



def _shape_ledger_event(
    claim: Any, hydrated: HydratedContent, occurred_at: datetime
) -> tuple[str, dict[str, Any]] | None:
    """Map a REV claim to a registered ledger event type + schema-valid payload.

    Returns ``None`` for unrecognised event types — the caller skips staging
    for that claim.  The old catch-all that silently mapped unknown types to
    ``milestone.completed.v1`` is removed (PS-13 / W1-3 / G-coverage).
    """
    event_type = _CLAIM_TO_LEDGER_EVENT.get(claim.event_type)
    if event_type is None:
        log.warning(
            "REV pipeline: claim event type %r has no registered ledger "
            "projection — skipping candidate to prevent false fact creation "
            "(PS-13 / W1-3)",
            claim.event_type,
        )
        return None
    date_value = str(claim.payload.get("date") or occurred_at.date().isoformat())
    subject = str(hydrated.route_metadata.get("subject", claim.event_type))
    sender = str(hydrated.route_metadata.get("sender", ""))
    excerpt = claim.evidence_spans[0].excerpt_text if claim.evidence_spans else subject
    payload: dict[str, Any]
    if event_type == "milestone.completed.v1":
        payload = {
            "milestone_id": _entity_id("milestone", hydrated, claim),
            "completed_on": date_value,
            "evidence": excerpt,
        }
    elif event_type == "deployment.completed.v1":
        payload = {
            "deployment_id": _entity_id("deployment", hydrated, claim),
            "artifact_name": subject,
            "completed_on": date_value,
        }
    elif event_type == "deployment.rollback.v1":
        payload = {
            "deployment_id": _entity_id("deployment", hydrated, claim),
            "artifact_name": subject,
            "reason": excerpt,
            "rolled_back_on": date_value,
        }
    elif event_type == "deployment.started.v1":
        payload = {
            "deployment_id": _entity_id("deployment", hydrated, claim),
            "artifact_name": subject,
            "started_on": date_value,
        }
    elif event_type == "incident.severity_changed.v1":
        payload = {
            "incident_id": _entity_id("incident", hydrated, claim),
            "new_severity": str(claim.payload.get("severity", "2")),
            "prior_severity": str(claim.payload.get("prior_severity", "")),
            "reason": excerpt,
        }
    elif event_type == "commitment.made.v1":
        payload = {
            "commitment_id": _entity_id("commitment", hydrated, claim),
            "text": excerpt,
            "owner_person_id": sender or "unknown",
            "due_date": date_value,
            "made_in": hydrated.route_metadata.get("conversation_id", ""),
        }
    elif event_type == "risk.raised.v1":
        payload = {
            "risk_id": _entity_id("risk", hydrated, claim),
            "title": str(claim.payload.get("blocker_description", subject)),
            "severity": str(claim.payload.get("severity", "high")),
        }
    elif event_type == "workstream.owner_changed.v1":
        payload = {
            "workstream_id": _entity_id("workstream", hydrated, claim),
            "new_owner_person_id": str(claim.payload.get("new_owner", sender or "unknown")),
        }
    else:
        # All known event types are handled above.  If we reach this branch
        # the mapping table has an entry but no matching if/elif — treat as
        # an internal inconsistency (log and skip).
        log.error(
            "REV pipeline: _CLAIM_TO_LEDGER_EVENT maps %r → %r but "
            "_shape_ledger_event has no branch for it — skipping",
            claim.event_type, event_type,
        )
        return None
    return event_type, payload


def _report(
    program_id: str,
    correlation_id: str,
    counts: dict[str, int],
    decision: GovernorDecision,
    shield_degrade: bool,
) -> RevCycleReport:
    cycle_integrity_ok = _cycle_integrity_ok(counts)
    if not cycle_integrity_ok:
        raise AssertionError(_cycle_integrity_message(counts))
    return RevCycleReport(
        program_id=program_id,
        correlation_id=correlation_id,
        enumerated=counts["enumerated"],
        hydrated=counts["hydrated"],
        quarantined=counts["quarantined"],
        metadata_only=counts["metadata_only"],
        candidates_staged=counts["candidates_staged"],
        assertions_written=counts["assertions_written"],
        stop_reason=decision.reason,
        stop_category=decision.category,
        breached_budget=decision.breached_budget,
        shield_degrade=shield_degrade,
        processed_successfully=counts.get("processed_successfully", 0),
        policy_denied=counts.get("policy_denied", 0),
        explicitly_skipped=counts.get("explicitly_skipped", 0),
        terminal_failures=counts.get("terminal_failures", 0),
        cycle_integrity_ok=cycle_integrity_ok,
        cycle_status=_derive_cycle_status(
            stop_category=decision.category,
            shield_degrade=shield_degrade,
            counts=counts,
            assertions_written=counts["assertions_written"],
            candidates_staged=counts["candidates_staged"],
        ),
        source_unreachable=_source_unreachable_from_stop(
            stop_category=decision.category,
            stop_reason=decision.reason,
            breached_budget=decision.breached_budget,
        ),
    )


def _cycle_integrity_ok(counts: dict[str, int]) -> bool:
    return counts.get("enumerated", 0) == _cycle_accounted_total(counts)


def _cycle_accounted_total(counts: dict[str, int]) -> int:
    quarantined_not_policy = max(0, counts.get("quarantined", 0) - counts.get("policy_denied", 0))
    return (
        counts.get("processed_successfully", 0)
        + quarantined_not_policy
        + counts.get("policy_denied", 0)
        + counts.get("explicitly_skipped", 0)
        + counts.get("terminal_failures", 0)
    )


def _cycle_integrity_message(counts: dict[str, int]) -> str:
    return (
        "REV cycle integrity failed: enumerated="
        f"{counts.get('enumerated', 0)} accounted={_cycle_accounted_total(counts)} "
        f"processed_successfully={counts.get('processed_successfully', 0)} "
        f"quarantined={counts.get('quarantined', 0)} "
        f"policy_denied={counts.get('policy_denied', 0)} "
        f"explicitly_skipped={counts.get('explicitly_skipped', 0)} "
        f"terminal_failures={counts.get('terminal_failures', 0)}"
    )


def _derive_cycle_status(
    *,
    stop_category: str,
    shield_degrade: bool,
    counts: dict[str, int],
    assertions_written: int,
    candidates_staged: int,
) -> str:
    if stop_category == "provider_limited":
        return "provider_limited"
    if shield_degrade or counts.get("policy_denied", 0):
        return "security_degraded"
    if stop_category and stop_category not in {"complete", "fully_verified"}:
        return "extraction_degraded"
    if counts.get("terminal_failures", 0) or counts.get("quarantined", 0) or counts.get("date_parse_failures", 0):
        return "extraction_degraded"
    if candidates_staged and assertions_written >= candidates_staged:
        return "fully_verified"
    return "acquisition_complete"


def _source_unreachable_from_stop(
    *,
    stop_category: str,
    stop_reason: str = "",
    breached_budget: str = "",
) -> bool:
    """Return whether this cycle hit an upstream/source availability stop."""
    return (
        stop_category == "provider_limited"
        or breached_budget in {"rate_limit", "forbidden"}
        or stop_reason.startswith(("rate_limited:", "forbidden:"))
    )


# ---------------------------------------------------------------------------
# File finalization + cycle checkpoint (REV-G2 3-dir atomicity, REV-G8b)
# ---------------------------------------------------------------------------

# Hydration Unsupported reasons that mean "no file to move" — skip finalization.
_NO_FILE_REASONS = ("eml_path_missing", "eml_not_found")


def _rotate_processed_best_effort(deps: RevPipelineDeps) -> None:
    """P2-14: rotate stale/surplus files out of ``processed/`` at cycle end.

    Duck-typed on the enumerator (Zone A pure): if the enumerator exposes a
    ``processed_dir()`` accessor, rotate its contents per the OA-4 retention
    window (90 days / 500 files). Best-effort — never raises into the cycle.
    """
    enum = getattr(deps, "enumerator", None)
    accessor = getattr(enum, "processed_dir", None)
    if accessor is None:
        return
    try:
        processed_dir = accessor()
    except Exception:  # pragma: no cover - defensive
        return
    if not processed_dir:
        return
    try:
        from src.core.rev.inbox_rotation import rotate_processed_dir
        moved = rotate_processed_dir(processed_dir)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("REV pipeline: processed/ rotation failed: %s", exc)
        return
    if moved:
        log.info("REV pipeline: rotated %d stale/surplus file(s) to processed/archive/", moved)


# ---------------------------------------------------------------------------
# REV-G6 gap-fill loop driver (P2-6)
# ---------------------------------------------------------------------------


def _drive_gap_lifecycle(
    *,
    program_id: str,
    staged_list: list[Any],
    claims: tuple[Any, ...],
    verifier_results: dict[str, str],
    programs_root: Path,
) -> int:
    """Advance matching context gaps: open/reopened → filling (candidate staged),
    then filling → resolved (candidate reached a verified state).

    Matching rule: a ``ContextGapRecord.metadata["event_types"]`` list intersects
    the claim's material ``event_type``. Each staged candidate maps 1:1 to a
    claim by index (``_stage_candidates`` builds one candidate per claim, in
    order), so ``staged_list[i]`` corresponds to ``claims[i]``.

    ``mark_resolved`` fires only when the candidate's effective verification state
    is in ``VERIFIED_STATES`` (``source_verified`` for non-material auto-verified
    claims; ``human_verified`` is set at triage, outside the cycle). The
    resolving evidence ref is the candidate's first vaulted excerpt hash.

    Best-effort: any failure logs and returns 0 — the cycle is never broken by
    gap tracking. Returns the number of gap status transitions applied.
    """
    if not staged_list:
        return 0
    try:
        from src.core.ledger.gap_lifecycle import GapLifecycleStore, GapStatus
        from src.core.ledger.verification_assertions import VERIFIED_STATES
    except ImportError:  # defensive — modules are Zone A, always present
        return 0

    try:
        store = GapLifecycleStore.load(program_id, programs_root=programs_root)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("REV-G6: could not load gap store for %s: %s", program_id, exc)
        return 0
    if not store.all_gaps():
        return 0  # no tracked gaps → nothing to drive (common case)

    transitions = 0
    for idx, staged in enumerate(staged_list):
        claim = claims[idx] if idx < len(claims) else None
        if claim is None:
            continue
        claim_event_type = getattr(claim, "event_type", "")
        if not claim_event_type:
            continue
        eff_state = verifier_results.get(getattr(staged, "candidate_id", ""), "")
        # Candidate's resolving evidence ref (first vaulted excerpt hash).
        cand_refs = getattr(staged, "evidence_refs", ()) or ()
        resolve_ref = getattr(cand_refs[0], "vault_hash", None) if cand_refs else None

        for gap in store.all_gaps():
            gap_types = set((gap.metadata.get("event_types") or []))
            if claim_event_type not in gap_types:
                continue
            before = gap.status
            # open/reopened → filling (a candidate now exists for this gap).
            gap.mark_filling(reason=f"rev_candidate_staged:{claim_event_type}")
            if gap.status != before:
                transitions += 1
                before = gap.status
            # filling → resolved when the candidate reached a verified state.
            # Skip if already resolved (avoids a spurious resolved→resolved entry).
            if eff_state in VERIFIED_STATES and resolve_ref and before != GapStatus.RESOLVED.value:
                gap.mark_resolved(
                    evidence_ref=resolve_ref,
                    reason=f"rev_verified:{eff_state}",
                )
                if gap.status != before:
                    transitions += 1

    if transitions:
        try:
            store.save(program_id, programs_root=programs_root)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("REV-G6: could not save gap store for %s: %s", program_id, exc)
            return 0
    return transitions


def _finalize_source_file(deps: RevPipelineDeps, candidate: Any, *, success: bool, reason: str = "") -> None:
    """Move a claimed source file to processed/ (success) or quarantine/ (failure).

    Duck-typed on the enumerator: only ``EmlEnumerator`` exposes
    ``mark_processed`` / ``mark_quarantined`` + an ``eml_path`` in the candidate
    metadata. The mock-fixture enumerator has neither, so this is a no-op there —
    keeping Zone A pure (no ``src.m365`` import) while still driving the 3-dir
    atomicity model for the local-import path.
    """
    eml_path = candidate.partial_metadata.get("eml_path")
    if not eml_path:
        return
    enumerator = deps.enumerator
    if success:
        mark_processed = getattr(enumerator, "mark_processed", None)
        if callable(mark_processed):
            mark_processed(eml_path)
    else:
        mark_quarantined = getattr(enumerator, "mark_quarantined", None)
        if callable(mark_quarantined):
            mark_quarantined(eml_path, reason=reason or "unsupported")


def _count_quarantine_files(enumerator: Any) -> int:
    """Count filesystem files in the enumerator's quarantine/ dir (telemetry)."""
    counter = getattr(enumerator, "count_quarantine_files", None)
    if callable(counter):
        try:
            return int(counter())
        except OSError:
            return 0
    return 0


def _compute_family_divergence(
    program_id: str,
    programs_root: Path,
) -> dict[str, int]:
    """S-5b: per-family shadow↔primary divergence detector.

    Computes the symmetric difference between the DB snapshot (shadow facts
    accepted by REV) and the YAML shim snapshot (primary / legacy context) for
    each authority family.  Returns ``{family: count}`` for every family with
    at least one diverging natural_key.  Returns ``{}`` on any error — this is
    a best-effort diagnostic, never critical-path.

    A non-zero count for a family means REV has accepted or detected facts that
    are not yet present in the YAML context (REV-only additions), or YAML context
    entries that REV has not yet seen (YAML-only entries).
    """
    try:
        from src.core.program_fact_store import (
            ProgramFactStore,
            build_legacy_program_fact_snapshot,
            _FACT_TYPE_TO_FAMILY,
        )
        db_root = programs_root.parent
        db_snapshot = ProgramFactStore(
            program_id, home_root=None, db_root=db_root
        ).snapshot(as_of=None)
        shim_snapshot = build_legacy_program_fact_snapshot(
            program_id, programs_root=programs_root
        )

        db_by_family: dict[str, set[str]] = {}
        for fact in db_snapshot.facts:
            family = _FACT_TYPE_TO_FAMILY.get(fact.fact_type.strip().lower())
            if family:
                db_by_family.setdefault(family, set()).add(fact.natural_key)

        shim_by_family: dict[str, set[str]] = {}
        for fact in shim_snapshot.facts:
            family = _FACT_TYPE_TO_FAMILY.get(fact.fact_type.strip().lower())
            if family:
                shim_by_family.setdefault(family, set()).add(fact.natural_key)

        all_families = sorted(set(db_by_family) | set(shim_by_family))
        divergence: dict[str, int] = {}
        for family in all_families:
            diff = len(
                db_by_family.get(family, set()).symmetric_difference(
                    shim_by_family.get(family, set())
                )
            )
            if diff > 0:
                divergence[family] = diff
        return divergence
    except Exception:  # noqa: BLE001
        return {}


def _run_family_flip_gates(
    program_id: str,
    family_divergence: dict[str, int],
    programs_root: Path,
) -> dict[str, str]:
    """S-5c: run the clean-cycle flip gate for all in-scope authority families.

    Called after divergence is computed. Returns ``{family: action}`` for
    every family evaluated. Best-effort — never raises (returns ``{}`` on error).
    """
    try:
        from src.core.truth_model import load_source_authority_policy
        from src.core.fact_sor_state import (
            AUTHORITY_FAMILIES,
            evaluate_family_flip_gate,
            load_fact_sor_state,
        )

        policy = load_source_authority_policy()
        sor_state = load_fact_sor_state(program_id, programs_root=programs_root)
        if sor_state is None:
            return {}

        now = datetime.now(timezone.utc)
        flip_results: dict[str, str] = {}
        for family in AUTHORITY_FAMILIES:
            family_mode = sor_state.family_modes.get(family, sor_state.mode)
            if family_mode not in ("shadow", "primary"):
                continue
            divergence_count = family_divergence.get(family, 0)
            cfg = policy.sor_flip.for_family(family)
            result = evaluate_family_flip_gate(
                program_id,
                family,
                divergence_count,
                0,  # total_entities: 0 → uses critical_zero strict gate
                sor_flip_config=cfg,
                recorded_at=now,
                recorded_by="rev_pipeline",
                programs_root=programs_root,
            )
            flip_results[family] = result.action
            if result.action != "no_change":
                log.info(
                    "S-5c flip gate: program=%s family=%s action=%s reason=%s",
                    program_id, family, result.action, result.reason,
                )
        return flip_results
    except Exception:  # noqa: BLE001
        return {}


def _run_cross_source_conflict_check(
    program_id: str,
    correlation_id: str,
    programs_root: Path,
) -> dict[str, int]:
    """AG-9 / §6.14.5: run cross-source conflict detection on the production path.

    Thin wrapper — the actual snapshot read + detection + fact-append logic
    lives in ``src.core.ledger.fact_bridge.run_cross_source_conflict_detection``
    (W2-12: REV modules must never import ``ProgramFactStore``/``append_fact``
    directly; only the ledger/bridge layer may write facts).
    """
    from src.core.ledger.fact_bridge import run_cross_source_conflict_detection

    return run_cross_source_conflict_detection(
        program_id, programs_root=programs_root, correlation_id=correlation_id
    )


def _finalize_report(
    *,
    program_id: str,
    correlation_id: str,
    counts: dict[str, int],
    stop_reason: str,
    stop_category: str,
    breached: str,
    shield_degrade: bool,
    wall_seconds: float,
    deps: RevPipelineDeps,
    programs_root: Path,
) -> RevCycleReport:
    """Build the final ``RevCycleReport`` with all Phase 1/2 telemetry fields and
    write the ``last_cycle.json`` + ``cycle_history.jsonl`` checkpoints."""
    cycle_integrity_ok = _cycle_integrity_ok(counts)
    if not cycle_integrity_ok:
        raise AssertionError(_cycle_integrity_message(counts))
    effective_stop_category = stop_category if stop_reason else "complete"
    family_divergence = _compute_family_divergence(program_id, programs_root)
    # AG-9 / §6.14.5: run cross-source conflict detection over the snapshot so
    # Vertex reconciles (flags `disputed` with `as_of`) rather than parroting.
    # Best-effort; never blocks the cycle (§6.14.4 — never deadlocks publication).
    conflict_summary = _run_cross_source_conflict_check(
        program_id, correlation_id, programs_root
    )
    # S-5c: evaluate flip gates only on complete (non-degraded) cycles
    family_flip_results: dict[str, str] = {}
    if not stop_reason and not shield_degrade:
        family_flip_results = _run_family_flip_gates(program_id, family_divergence, programs_root)
    report = RevCycleReport(
        program_id=program_id,
        correlation_id=correlation_id,
        enumerated=counts["enumerated"],
        hydrated=counts["hydrated"],
        quarantined=counts["quarantined"],
        metadata_only=counts["metadata_only"],
        candidates_staged=counts["candidates_staged"],
        assertions_written=counts["assertions_written"],
        stop_reason=stop_reason,
        stop_category=effective_stop_category,
        breached_budget=breached,
        shield_degrade=shield_degrade,
        date_parse_failures=counts["date_parse_failures"],
        llm_fallback_count=int(getattr(deps.extractor, "fallback_count", 0) or 0),
        low_unique_body_count=int(getattr(deps.hydrator, "low_unique_body_count", 0) or 0),
        winmail_skipped_count=int(getattr(deps.hydrator, "winmail_skipped_count", 0) or 0),
        wall_clock_seconds=round(wall_seconds, 3),
        quarantined_files_count=_count_quarantine_files(deps.enumerator),
        claimed_at_startup_count=int(getattr(deps.enumerator, "claimed_at_startup_count", 0) or 0),
        gap_transitions=int(counts.get("gap_transitions", 0)),
        processed_successfully=counts.get("processed_successfully", 0),
        policy_denied=counts.get("policy_denied", 0),
        explicitly_skipped=counts.get("explicitly_skipped", 0),
        terminal_failures=counts.get("terminal_failures", 0),
        cycle_integrity_ok=cycle_integrity_ok,
        cycle_status=_derive_cycle_status(
            stop_category=effective_stop_category,
            shield_degrade=shield_degrade,
            counts=counts,
            assertions_written=counts["assertions_written"],
            candidates_staged=counts["candidates_staged"],
        ),
        source_unreachable=_source_unreachable_from_stop(
            stop_category=effective_stop_category,
            stop_reason=stop_reason,
            breached_budget=breached,
        ),
        # §6.14.3: an LLM fallback *is* per-item extraction degradation — any
        # such fallback makes the cycle publication-valid but not quality-valid.
        extraction_degraded=(
            int(getattr(deps.extractor, "fallback_count", 0) or 0) > 0
            or bool(counts.get("terminal_failures", 0))
            or bool(counts.get("date_parse_failures", 0))
        ),
        family_divergence=family_divergence,
        family_flip_results=family_flip_results,
    )
    _write_cycle_checkpoint(program_id, report, programs_root)
    return report


_LAST_CYCLE_SCHEMA_VERSION = "1.1"
_CYCLE_HISTORY_MAX_ENTRIES = 10


def _write_cycle_checkpoint(program_id: str, report: RevCycleReport, programs_root: Path) -> None:
    """Atomically write ``_rev/last_cycle.json`` + bound ``_rev/cycle_history.jsonl``.

    Best-effort: a checkpoint write failure logs a warning and never raises — the
    cycle report is the authoritative result. ``last_cycle.json`` is the
    single-cycle atomic checkpoint; ``cycle_history.jsonl`` is the bounded
    (≤10 entries) append-only history driving the LLM-fallback trend in
    ``doctor --rev-health`` (REV-G8b).
    """
    rev_dir = programs_root / program_id / "_rev"
    try:
        rev_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("REV pipeline: could not create _rev/ dir: %s", exc)
        return

    last_cycle = {
        "schema_version": _LAST_CYCLE_SCHEMA_VERSION,
        "correlation_id": report.correlation_id,
        "stop_category": report.stop_category,
        "cycle_status": report.cycle_status,
        "cycle_integrity_ok": report.cycle_integrity_ok,
        "candidates_staged": report.candidates_staged,
        "enumerated": report.enumerated,
        "processed_successfully": report.processed_successfully,
        "policy_denied": report.policy_denied,
        "explicitly_skipped": report.explicitly_skipped,
        "terminal_failures": report.terminal_failures,
        "llm_fallback_count": report.llm_fallback_count,
        "shield_degrade": report.shield_degrade,
        "source_unreachable": report.source_unreachable,
        "extraction_degraded": report.extraction_degraded,
        "wall_clock_seconds": report.wall_clock_seconds,
    }
    last_path = rev_dir / "last_cycle.json"
    tmp_path = rev_dir / "last_cycle.json.tmp"
    try:
        tmp_path.write_text(json.dumps(last_cycle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp_path, last_path)
    except OSError as exc:
        log.warning("REV pipeline: could not write last_cycle.json: %s", exc)

    hist_path = rev_dir / "cycle_history.jsonl"
    try:
        existing = list(read_jsonl_records(hist_path))
        existing.append({
            "correlation_id": report.correlation_id,
            "stop_category": report.stop_category,
            "cycle_status": report.cycle_status,
            "cycle_integrity_ok": report.cycle_integrity_ok,
            "candidates_staged": report.candidates_staged,
            "enumerated": report.enumerated,
            "processed_successfully": report.processed_successfully,
            "policy_denied": report.policy_denied,
            "explicitly_skipped": report.explicitly_skipped,
            "terminal_failures": report.terminal_failures,
            "llm_fallback_count": report.llm_fallback_count,
            "source_unreachable": report.source_unreachable,
            "extraction_degraded": report.extraction_degraded,
            "wall_clock_seconds": report.wall_clock_seconds,
        })
        # Bound to the last _CYCLE_HISTORY_MAX_ENTRIES cycles (oldest dropped).
        if len(existing) > _CYCLE_HISTORY_MAX_ENTRIES:
            existing = existing[-_CYCLE_HISTORY_MAX_ENTRIES:]
        tmp_hist = rev_dir / "cycle_history.jsonl.tmp"
        tmp_hist.write_text("".join(json.dumps(r) + "\n" for r in existing), encoding="utf-8")
        os.replace(tmp_hist, hist_path)
    except OSError as exc:
        log.warning("REV pipeline: could not write cycle_history.jsonl: %s", exc)


__all__ = [
    "run_rev_cycle",
    "RevCycleReport",
    "RevPipelineDeps",
    "PIPELINE_VERSION",
]
