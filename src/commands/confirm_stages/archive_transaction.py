from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from src.core.analytics_store import mark_analytics_dirty, project_confirmed_issue, record_override
from src.core.archive_signing import (
    get_archive_signing_key,
    manifest_signature_sidecar_path,
    sign_manifest_file,
    write_signature_record,
)
from src.core.archive_store import ConfirmedIssueArchivePaths
from src.core.archive_store import find_latest_confirmed_entry, read_archive_index
from src.core.checkpoint_store import create_checkpoint_snapshot
from src.core.measurement_spine import IssueMetrics, write_issue_metrics
from src.core.program_fact_store import load_program_facts, persist_program_fact_snapshot, resolve_fact_sor_mode
from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope, read_events, write_event
from src.core.ledger.program_views import project_program_events
from src.core.ledger.source_refs import NewsletterRef, OperatorAssertionRef
from src.core.projections.snapshot_manager import build_baseline_hardlock_event, write_projection_snapshot
from src.core.section_proposal_store import load_proposals, write_accepted_proposals_archive
from src.core.semantic_index import mark_semantic_index_dirty, update_archive_semantic_index_for_issue
from src.core.signal_classification import SignalClass
from src.core.snapshot_store import ArchiveLock, get_archive_root
from src.core.gather_state_store import load_gather_state
from src.core.maturity_engine import try_advance_earned_autonomy


Artifacts = tuple[Any, ...]
SignalClassFn = Callable[[Any], Any]
SignalMetricFn = Callable[[Any], float | None]
ContextSnapshotWriter = Callable[..., None]
ContinuationPathLoader = Callable[..., Path | None]
ConfirmedEmlBuilder = Callable[..., bytes | None]
ClaimTrackerRecorder = Callable[..., tuple[str, ...]]
ProgramPolicyBoolFn = Callable[[Any], bool]
OptimizationProposalWriter = Callable[..., None]
RiskUpserter = Callable[..., object]
OverridesPathResolver = Callable[..., Path]
SemanticIndexUpdater = Callable[..., object]
DirtyMarker = Callable[..., object]
GatherStateLoader = Callable[..., Any]
SnapshotWriter = Callable[..., Path]
ArchiveWriter = Callable[..., ConfirmedIssueArchivePaths]
LedgerProjectionWriter = Callable[..., Any]
LedgerSnapshotWriter = Callable[..., Any]
LedgerEventReader = Callable[..., tuple[Any, ...]]
LedgerEventWriter = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class ArchiveTransactionResult:
    archive_paths: ConfirmedIssueArchivePaths
    warnings: tuple[str, ...]


def execute_archive_transaction(
    *,
    edition_name: str,
    issue_number: int,
    confirmed_at: datetime,
    warnings: tuple[str, ...],
    archive_root: Path,
    programs_root: Path,
    reports_root: Path,
    approved_signals: tuple[Any, ...],
    artifacts: tuple[Any, ...],
    bundle: Any,
    manifest: Any,
    overrides_document: Any,
    vitality_archive_entry: Any,
    chart_cache_entries: dict[str, dict[str, Any]] | None,
    review_status_path: Path,
    narrative_dir: Path,
    program_id: str | None,
    resolved_v2: Any,
    legacy_regex_extractor: bool,
    extraction_result: Any,
    extraction_mode: str,
    build_confirmed_eml_bytes_fn: ConfirmedEmlBuilder,
    write_confirmed_fn: SnapshotWriter,
    write_confirmed_issue_fn: ArchiveWriter,
    get_overrides_path_fn: OverridesPathResolver,
    load_draft_continuation_contract_path_fn: ContinuationPathLoader,
    write_context_snapshot_for_issue_fn: ContextSnapshotWriter,
    record_confirmed_claims_for_v2_fn: ClaimTrackerRecorder,
    semantic_index_enabled_fn: ProgramPolicyBoolFn,
    update_archive_semantic_index_for_issue_fn: SemanticIndexUpdater,
    mark_semantic_index_dirty_fn: DirtyMarker,
    write_optimization_proposals_fn: OptimizationProposalWriter,
    load_gather_state_fn: GatherStateLoader,
    compute_source_health_pct_fn: SignalMetricFn,
    compute_provenance_confidence_fn: SignalMetricFn,
    get_signal_class_fn: SignalClassFn,
    upsert_risk_from_signal_fn: RiskUpserter,
    confirmed_by: str = "vertex.confirm",
    project_program_events_fn: LedgerProjectionWriter = project_program_events,
    write_projection_snapshot_fn: LedgerSnapshotWriter = write_projection_snapshot,
    read_events_fn: LedgerEventReader = read_events,
    write_event_fn: LedgerEventWriter = write_event,
) -> ArchiveTransactionResult:
    from src.core.models_v2 import SectionRevisionStatus

    create_checkpoint_snapshot(program_id, issue_number, programs_root=programs_root) if program_id is not None else None

    with ArchiveLock(get_archive_root(edition_name, archive_root)):
        staged_snapshot_path = write_confirmed_fn(
            edition=edition_name,
            issue_number=issue_number,
            snapshot=artifacts[2],
            archive_root=archive_root,
            promote=False,
            acquire_lock=False,
        )
        archive_paths = write_confirmed_issue_fn(
            edition=edition_name,
            issue_number=issue_number,
            snapshot=artifacts[2],
            eml_bytes=build_confirmed_eml_bytes_fn(
                bundle,
                issue_number=issue_number,
                as_of=artifacts[6].ado_data_as_of,
                html_body=artifacts[4],
                markdown_body=artifacts[5],
                suggested_subject=str(manifest.metadata.get("suggested_subject") or ""),
                generated_at=confirmed_at,
            ),
            html_body=artifacts[4],
            markdown_body=artifacts[5],
            manifest=manifest,
            overrides_source=get_overrides_path_fn(edition_name, reports_root, issue_number=issue_number),
            review_status_source=(review_status_path if review_status_path.exists() else None),
            narratives_source_dir=(narrative_dir if narrative_dir.exists() else None),
            continuation_contract_source=load_draft_continuation_contract_path_fn(
                edition_name,
                issue_number,
                programs_root=programs_root,
            ),
            vitality_record=vitality_archive_entry,
            chart_cache_entries=chart_cache_entries,
            archive_root=archive_root,
            snapshot_source=staged_snapshot_path,
            snapshot_is_staged=True,
            acquire_lock=False,
        )

        if resolved_v2 is not None:
            write_context_snapshot_for_issue_fn(
                program_id=resolved_v2.program.id,
                edition_id=edition_name,
                issue_number=issue_number,
                confirmed_at=confirmed_at,
                archive_root=archive_root,
                programs_root=programs_root,
                prior_issue_entry=find_latest_confirmed_entry(
                    read_archive_index(edition_name, archive_root),
                    before_issue_number=issue_number,
                ),
            )

        if resolved_v2 is not None and archive_paths.narratives_path is not None:
            accepted_proposals = load_proposals(
                resolved_v2.program.id,
                issue_number,
                programs_root=programs_root,
                status_filter={SectionRevisionStatus.ACCEPTED, SectionRevisionStatus.ACCEPTED_MODIFIED},
            )
            if accepted_proposals:
                write_accepted_proposals_archive(accepted_proposals, archive_paths.narratives_path)
        if resolved_v2 is not None and semantic_index_enabled_fn(resolved_v2.raw_program):
            try:
                update_archive_semantic_index_for_issue_fn(
                    edition_name,
                    issue_number,
                    archive_root=archive_root,
                )
            except Exception as exc:
                mark_semantic_index_dirty_fn(
                    edition_name,
                    f"confirm issue {issue_number:03d}: {exc}",
                    archive_root=archive_root,
                )
                warnings = warnings + (f"Semantic index update skipped: {exc}",)

        # WS-7: sign the manifest after it's been promoted to its final
        # location. Failures are surfaced as warnings (the manifest is
        # already on disk + already in the index); a future WS-7
        # quality-gate will block confirms on missing signatures.
        archive_signing_warnings = _try_sign_archive_manifest(
            manifest_path=archive_paths.manifest_path,
            edition_name=edition_name,
            issue_number=issue_number,
        )
        if archive_signing_warnings:
            warnings = warnings + archive_signing_warnings

        if resolved_v2 is not None:
            try:
                _write_ledger_baseline_artifacts(
                    program_id=resolved_v2.program.id,
                    edition_name=edition_name,
                    issue_number=issue_number,
                    confirmed_at=confirmed_at,
                    confirmed_by=confirmed_by,
                    archive_paths=archive_paths,
                    published_title=str(manifest.metadata.get("suggested_subject") or f"{edition_name} issue {issue_number:03d}"),
                    programs_root=programs_root,
                    project_program_events_fn=project_program_events_fn,
                    write_projection_snapshot_fn=write_projection_snapshot_fn,
                    read_events_fn=read_events_fn,
                    write_event_fn=write_event_fn,
                )
            except Exception as exc:
                warnings = warnings + (f"Ledger baseline hardlock/snapshot skipped: {exc}",)

    try:
        claim_tracking_warnings = record_confirmed_claims_for_v2_fn(
            edition_name=edition_name,
            issue_number=issue_number,
            confirmed_at=confirmed_at,
            reports_root=reports_root,
            items=artifacts[6].items,
            legacy_regex_extractor=legacy_regex_extractor,
            extraction_result=extraction_result,
            extraction_mode=extraction_mode,
            resolve_extraction_if_missing=False,
        )
    except Exception as exc:
        warnings = warnings + (f"Claim tracker skipped: {exc}",)
    else:
        warnings = warnings + claim_tracking_warnings

    if resolved_v2 is not None:
        try:
            project_confirmed_issue(
                program_id=resolved_v2.program.id,
                edition_id=edition_name,
                snapshot=artifacts[2],
                confirmed_at=confirmed_at,
                vitality_entry=vitality_archive_entry,
                programs_root=programs_root,
            )
        except Exception as exc:
            mark_analytics_dirty(
                resolved_v2.program.id,
                reason=f"confirm projection failed: {exc}",
                programs_root=programs_root,
            )
            warnings = warnings + (f"Analytics projection skipped: {exc}",)
        else:
            override_count = 0
            for scorecard in overrides_document.scorecards:
                for dimension in scorecard.dimensions:
                    if dimension.risk is not None:
                        try:
                            record_override(
                                resolved_v2.program.id,
                                dimension=dimension.name,
                                edition_id=edition_name,
                                issue_number=issue_number,
                                original_value=None,
                                override_value=dimension.risk.value,
                                programs_root=programs_root,
                            )
                            override_count += 1
                        except OSError:
                            pass
            try:
                write_optimization_proposals_fn(
                    resolved_v2.program.id,
                    edition_id=edition_name,
                    issue_number=issue_number,
                    programs_root=programs_root,
                )
            except OSError:
                pass
            try:
                try_advance_earned_autonomy(
                    resolved_v2.program.id,
                    edition_id=edition_name,
                    programs_root=programs_root,
                )
            except OSError:
                pass
            try:
                gather_state = load_gather_state_fn(program_id, programs_root=programs_root) if program_id is not None else None
                write_issue_metrics(
                    IssueMetrics(
                        program_id=resolved_v2.program.id,
                        issue_number=issue_number,
                        edition_id=edition_name,
                        computed_at=confirmed_at,
                        override_count=override_count,
                        claim_coverage=None,
                        source_health_pct=compute_source_health_pct_fn(gather_state),
                        provenance_confidence=compute_provenance_confidence_fn(approved_signals),
                        baseline_parity_score=None,
                        manual_rewrite_rate=None,
                    ),
                    programs_root=programs_root,
                )
            except OSError:
                pass
            if program_id is not None:
                for signal in approved_signals:
                    try:
                        if get_signal_class_fn(signal) == SignalClass.RISK:
                            upsert_risk_from_signal_fn(
                                program_id,
                                signal_id=signal.id,
                                signal_text=signal.text or "",
                                signal_entity_refs=tuple(signal.entity_refs or ()),
                                signal_workstream_id=getattr(signal, "workstream_id", None),
                                programs_root=programs_root,
                            )
                    except (OSError, ValueError):
                        pass
            if program_id is not None:
                try:
                    fact_snapshot = load_program_facts(program_id, programs_root=programs_root)
                    persist_program_fact_snapshot(
                        fact_snapshot,
                        recorded_at=confirmed_at,
                        accepted_by="vertex.confirm",
                    )
                except Exception as exc:
                    warnings = warnings + (f"FactStore shadow write skipped: {exc}",)

    return ArchiveTransactionResult(
        archive_paths=archive_paths,
        warnings=warnings,
    )


def _write_ledger_baseline_artifacts(
    *,
    program_id: str,
    edition_name: str,
    issue_number: int,
    confirmed_at: datetime,
    confirmed_by: str,
    archive_paths: ConfirmedIssueArchivePaths,
    published_title: str,
    programs_root: Path,
    project_program_events_fn: LedgerProjectionWriter,
    write_projection_snapshot_fn: LedgerSnapshotWriter,
    read_events_fn: LedgerEventReader,
    write_event_fn: LedgerEventWriter,
) -> None:
    projection_result = project_program_events_fn(program_id, programs_root=programs_root)
    ledger_events = read_events_fn(program_id, programs_root=programs_root)
    snapshot_paths = write_projection_snapshot_fn(
        program_id,
        issue_number,
        projection_result,
        events=ledger_events,
        programs_root=programs_root,
    )
    hardlock_event = build_baseline_hardlock_event(
        program_id,
        issue_number,
        snapshot_paths,
        projection_result,
        source_ref=OperatorAssertionRef(asserted_by=confirmed_by, asserted_at=confirmed_at, context="confirm"),
        actor=confirmed_by,
        recorded_at=confirmed_at,
    )
    write_event_fn(hardlock_event, programs_root=programs_root)
    published_event = build_event_envelope(
        program_id=program_id,
        event_type="artifact.published.v1",
        occurred_at=confirmed_at,
        recorded_at=confirmed_at,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor=confirmed_by,
        payload={
            "artifact_id": f"published_artifact:issue-{issue_number:03d}",
            "artifact_kind": "newsletter",
            "title": published_title,
            "location": str(archive_paths.html_path),
            "period_start": confirmed_at.date().isoformat(),
            "period_end": confirmed_at.date().isoformat(),
        },
        source_ref=NewsletterRef(
            file_path=str(archive_paths.html_path),
            publication_date=confirmed_at.date(),
            issue_number=issue_number,
            section=edition_name,
        ),
    )
    write_event_fn(published_event, programs_root=programs_root)
    project_program_events_fn(program_id, programs_root=programs_root)


def _try_sign_archive_manifest(
    *,
    manifest_path: Path,
    edition_name: str,
    issue_number: int,
) -> tuple[str, ...]:
    """Sign the just-written archive manifest + write a `.sig.json` sidecar.

    Returns an empty tuple on success, a tuple of warnings on any
    expected failure (no key configured, keyring unavailable, I/O error).
    An unexpected (e.g. malformed manifest) failure raises — those are
    genuine integrity problems that the caller should not paper over.

    The behavior matrix is:
    - No signing key configured: warning, sidecar NOT written. This is
      the operator-off-ramp (e.g. dev/CI machines without keyring).
      The future QG (WS-7 step 2) decides whether to *block* confirms
      with no key configured.
    - Keyring / I/O error: warning. Same as above; the manifest itself
      is unaffected.
    - Manifest exists but is not a JSON object: raise `ValueError` —
      this should never happen for a manifest Vertex wrote itself.
    """
    key = get_archive_signing_key()
    if key is None:
        return (
            "Archive signing skipped: no HMAC key configured "
            "(run `vertex admin archive-signing set-key` to enable).",
        )
    try:
        record = sign_manifest_file(
            manifest_path=manifest_path,
            edition=edition_name,
            issue_number=issue_number,
            key=key,
        )
        sidecar_path = manifest_signature_sidecar_path(manifest_path)
        write_signature_record(sidecar_path, record)
    except OSError as exc:
        return (f"Archive signing failed (I/O): {exc}",)
    return ()
