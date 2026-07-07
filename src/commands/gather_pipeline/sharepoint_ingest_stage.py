"""SharePoint ingest stage: CandidateEvent[] → PENDING Signal[] in the gather journal.

SP1-2/SP1-3/SP4-2 — wires run_sharepoint_pipeline() into gather_program() and manages
gather_state.json doc_states for change detection.

Architecture:
- This stage lives in Zone A (src/commands/gather_pipeline/) and coordinates with
  the Zone B pipeline (src/m365/discovery/sharepoint_pipeline.py).
- The stage does NOT read or write gather_state.json directly — callers thread state in.
- Local .pptx backfill (SP4-2) is checked first; WorkIQ fallback is secondary.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from src.core.ledger.candidate_store import CandidateEvent
from src.core.models_v2 import Confidence, ReviewPolicy, Signal
from src.core.signal_classification import classify_signal
from src.core.signal_dedup import dedupe_signals_with_audit

_LOG = logging.getLogger(__name__)

# ── Types ─────────────────────────────────────────────────────────────────────


@runtime_checkable
class SharePointPipelineRunner(Protocol):
    """DI protocol for the SharePoint pipeline runner (SP1-2).

    Enables unit tests to inject a stub without mocking the module.
    The real implementation is run_sharepoint_pipeline from sharepoint_pipeline.py.
    """

    def __call__(
        self,
        *,
        program_id: str,
        batch_id: str,
        pipeline: str,
        programs_root: Path,
        doc_states: dict[str, dict[str, Any]] | None,
        force_refresh: bool,
        as_of: datetime | None,
    ) -> Any:  # returns SharePointPipelineBatch
        ...


@dataclass(frozen=True, slots=True)
class SharePointIngestResult:
    """Result of a single SharePoint ingest stage run."""
    signals_created: int
    docs_processed: int
    gaps: tuple[Any, ...]  # GapDetail tuple — typed Any to avoid circular import
    updated_doc_states: dict[str, dict[str, Any]]  # merged back into gather_state.json


# ── Public function ───────────────────────────────────────────────────────────


def ingest_sharepoint_candidates(
    candidates: tuple[CandidateEvent, ...],
    *,
    program_id: str,
    existing_signals: tuple[Signal, ...],
    signal_store: Any,  # SignalStore protocol — caller builds via build_program_signal_store()
) -> int:
    """Convert CandidateEvent[] from sharepoint_pipeline into PENDING journal signals.

    Returns count of new signals written.

    INVARIANT: All written signals have review_policy=ReviewPolicy.PENDING regardless
    of confidence level. SharePoint signals are NEVER auto-approved.
    """
    candidate_signals: list[Signal] = []
    for candidate in candidates:
        # Derive text from proposed_payload — CandidateEvent has no event_summary field
        payload = candidate.proposed_payload or {}
        text_raw = (
            payload.get("summary")
            or payload.get("description")
            or payload.get("title")
            or candidate.proposed_event_type
        )
        text = str(text_raw)[:255]

        # proposed_confidence is a lowercase str: "source_authoritative" | "ai_extracted"
        confidence = (
            Confidence.HIGH
            if candidate.proposed_confidence.lower() in ("source_authoritative", "high")
            else Confidence.MEDIUM
        )

        # Use staged_at if present (after pipeline run); fall back to proposed_occurred_at
        timestamp = candidate.staged_at or candidate.proposed_occurred_at

        # Metadata: include slide_number from LTDeckRef when available
        source_ref = candidate.source_ref
        slide_number = getattr(source_ref, "slide_number", None)
        doc_id = _extract_doc_id_from_ref(source_ref)

        signal = Signal(
            id=candidate.candidate_id,
            timestamp=timestamp,
            source=candidate.pipeline,          # "sharepoint" or "lt_deck"
            program_id=program_id,
            workstream_id=program_id,           # umbrella lane — SP-3 routing assigns per-workstream
            entity_refs=(),                     # no ADO entity refs for SharePoint signals
            text=text,
            raw_ref=_extract_vault_hash(source_ref),
            confidence=confidence,
            review_policy=ReviewPolicy.PENDING,
            metadata={
                "source_subtype": candidate.pipeline,
                "doc_id": doc_id,
                "slide_number": slide_number,
                "doc_title": getattr(source_ref, "slide_title", None),
                "doc_url": getattr(source_ref, "doc_path", None),
            },
        )
        candidate_signals.append(signal)

    # Deduplication: dedupe_signals_with_audit returns only net-new signals
    dedup_result = dedupe_signals_with_audit(tuple(candidate_signals), existing_signals=existing_signals)
    for signal in dedup_result.signals:
        signal_store.append(classify_signal(signal))
    return len(dedup_result.signals)


def run_sharepoint_ingest_stage(
    *,
    program_id: str,
    programs_root: Path,
    existing_signals: tuple[Signal, ...],
    signal_store: Any,
    prior_doc_states: dict[str, dict[str, Any]],
    batch_id: str,
    include_lt_deck: bool = False,
    force_refresh: bool = False,
    max_docs_per_run: int = 5,
    as_of: datetime | None = None,
    pipeline_runner: SharePointPipelineRunner | None = None,
) -> SharePointIngestResult:
    """Full SharePoint ingest stage: run pipeline, convert candidates, track state.

    Checks for local .pptx backfill files first (SP4-2); WorkIQ is the fallback.
    State injection pattern: caller passes in prior_doc_states and gets back
    updated_doc_states to merge into gather_state.json — this stage never touches disk.

    Args:
        prior_doc_states: From gather_state.json m365_discovery["sharepoint"]["doc_states"].
        pipeline_runner: Injectable pipeline runner for testability (SP1-2 DI pattern).
                         Defaults to run_sharepoint_pipeline from sharepoint_pipeline.py.
        max_docs_per_run: Prevents >2500s serial gather time (5 docs × 300s ceiling).
    """
    resolved_as_of = as_of or datetime.now(timezone.utc)

    # Lazy import to avoid circular dependency at module load
    runner = pipeline_runner
    if runner is None:
        from src.m365.discovery.sharepoint_pipeline import run_sharepoint_pipeline
        runner = run_sharepoint_pipeline  # type: ignore[assignment]

    # SP4-2: Check local backfill .pptx before WorkIQ
    backfill_candidates = _load_backfill_candidates(
        program_id=program_id,
        programs_root=programs_root,
        prior_doc_states=prior_doc_states,
        batch_id=batch_id,
        force_refresh=force_refresh,
        as_of=resolved_as_of,
    )

    # Run WorkIQ pipeline for docs not covered by backfill
    pipeline_result = runner(
        program_id=program_id,
        batch_id=batch_id,
        pipeline="lt_deck" if include_lt_deck else "sharepoint",
        programs_root=programs_root,
        doc_states=prior_doc_states,
        force_refresh=force_refresh,
        as_of=resolved_as_of,
    )

    # Merge backfill + pipeline candidates (backfill takes precedence via dedup)
    all_candidates = backfill_candidates + pipeline_result.candidates
    # Respect max_docs_per_run cap on total candidates ingested per run
    all_candidates = all_candidates[:max_docs_per_run * 30]  # ~30 candidates/doc ceiling

    signals_created = ingest_sharepoint_candidates(
        all_candidates,
        program_id=program_id,
        existing_signals=existing_signals,
        signal_store=signal_store,
    )

    # Build updated doc_states for the caller to write back into gather_state.json
    updated_doc_states = dict(prior_doc_states)
    for candidate in all_candidates:
        doc_id = _extract_doc_id_from_ref(candidate.source_ref)
        if doc_id:
            content = _content_for_hash(candidate)
            updated_doc_states[doc_id] = {
                "last_extracted": resolved_as_of.isoformat().replace("+00:00", "Z"),
                "last_hash": f"sha256:{hashlib.sha256(content.encode()).hexdigest()[:16]}",
                "signals_created": updated_doc_states.get(doc_id, {}).get("signals_created", 0) + 1,
            }

    docs_processed = len({_extract_doc_id_from_ref(c.source_ref) for c in all_candidates if _extract_doc_id_from_ref(c.source_ref)})

    return SharePointIngestResult(
        signals_created=signals_created,
        docs_processed=docs_processed,
        gaps=pipeline_result.gaps,
        updated_doc_states=updated_doc_states,
    )


# ── Backfill helpers (SP4-2) ──────────────────────────────────────────────────


def _load_backfill_candidates(
    *,
    program_id: str,
    programs_root: Path,
    prior_doc_states: dict[str, dict[str, Any]],
    batch_id: str,
    force_refresh: bool,
    as_of: datetime,
) -> tuple[CandidateEvent, ...]:
    """SP4-2: Check local .pptx backfill before calling WorkIQ.

    Looks in programs/<prog>/backfill/sharepoint/<doc-id>/ for .pptx files.
    If a .pptx is found and newer than last_extracted (or force_refresh), extracts candidates.
    Returns empty tuple if no backfill found or not changed.
    """
    backfill_root = programs_root / program_id / "backfill" / "sharepoint"
    if not backfill_root.exists():
        return ()

    try:
        from src.ai.discovery.lt_deck_extractor import (
            LTDeckExtractorError,
            extract_lt_deck_candidates_from_pptx,
        )
    except ImportError:
        _LOG.warning("lt_deck_extractor not available; skipping backfill")
        return ()

    all_candidates: list[CandidateEvent] = []
    for doc_dir in sorted(backfill_root.iterdir()):
        if not doc_dir.is_dir():
            continue
        doc_id = doc_dir.name
        pptx_files = sorted(doc_dir.glob("*.pptx"))
        if not pptx_files:
            continue
        pptx_path = pptx_files[-1]  # most recent alphabetically

        # SP4-2/SP4-4: Check if backfill is newer than last_extracted
        if not force_refresh:
            state = prior_doc_states.get(doc_id, {})
            last_extracted_str = state.get("last_extracted")
            if last_extracted_str:
                try:
                    last_extracted = datetime.fromisoformat(last_extracted_str.replace("Z", "+00:00"))
                    file_mtime = datetime.fromtimestamp(pptx_path.stat().st_mtime, tz=timezone.utc)
                    if file_mtime <= last_extracted:
                        continue  # unchanged — skip
                except (ValueError, OSError):
                    pass  # on error, re-extract

        try:
            batch = extract_lt_deck_candidates_from_pptx(
                program_id=program_id,
                source_path=pptx_path,
                relative_path=pptx_path.name,
                batch_id=batch_id,
                pipeline="lt_deck",
            )
            all_candidates.extend(batch.candidates)
            _LOG.info(
                "Loaded %d candidates from backfill %s",
                len(batch.candidates),
                pptx_path,
            )
        except LTDeckExtractorError as error:
            _LOG.warning("Backfill extraction failed for %s: %s", pptx_path, error)

    return tuple(all_candidates)


# ── Private helpers ───────────────────────────────────────────────────────────


def _extract_vault_hash(source_ref: Any) -> str | None:
    """Extract vault_hash from a CandidateEvent source_ref.
    Typed as Any to satisfy strict mypy — actual types are SharePointDocRef | LTDeckRef.
    Both have vault_hash: str | None (verified against source_refs.py).
    """
    return getattr(source_ref, "vault_hash", None)


def _extract_doc_id_from_ref(source_ref: Any) -> str | None:
    """Extract engms_pages.yaml doc id from a CandidateEvent source_ref.
    SharePointDocRef has doc_path; LTDeckRef has file_path (local path, not an id).
    Neither field is guaranteed — return None if unavailable.
    """
    return getattr(source_ref, "doc_path", None) or getattr(source_ref, "file_path", None)


def _content_for_hash(candidate: CandidateEvent) -> str:
    """Build a stable string for content hashing (change detection)."""
    payload = candidate.proposed_payload or {}
    return f"{candidate.proposed_event_type}:{payload.get('summary', '') or payload.get('description', '')}"
