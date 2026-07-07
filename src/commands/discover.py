from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import typer

from src.ai.client import AIClientError
from src.ai.deployment_fallback import FallbackAIClient, resolve_ai_deployments
from src.ai.discovery._mime_text import MIMETextError, parse_eml_message
from src.ai.discovery.email_extractor import EmailExtractorError, extract_email_candidates
from src.ai.discovery.lt_deck_extractor import LTDeckExtractorError, extract_lt_deck_candidates_from_pptx, extract_lt_deck_date_from_path, get_lt_deck_prose_text
from src.ai.discovery.newsletter_extractor import NewsletterExtractorError, extract_newsletter_candidates, extract_newsletter_issue_number, extract_newsletter_publication_date, normalize_newsletter_text
from src.ai.discovery.prose_event_extractor import extract_prose_event_candidates
from src.ai.discovery.sharepoint_doc_extractor import SharePointDocExtractorError, extract_sharepoint_doc_candidates
from src.core.ledger.source_refs import LTDeckRef, NewsletterRef
from src.core.config_loader import PROGRAMS_ROOT
from src.core.ledger.candidate_store import CandidateEvent, append_candidate
from src.core.ledger.discovery_candidate_builders import DiscoveryCandidateBuildError, build_lt_deck_artifact_candidates, candidate_from_import_line, fresh_discovery_batch_id
from src.core.ledger.discovery_run_recorder import DiscoveryRunResult, GapDetail, record_discovery_run
from src.core.ledger.event_log import EventEnvelope
from src.m365.discovery.outlook_pipeline import OutlookPipelineError, run_outlook_pipeline
from src.m365.discovery.sharepoint_pipeline import SharePointPipelineError, run_sharepoint_pipeline
from src.m365.discovery.teams_pipeline import TeamsPipelineError, run_teams_pipeline
from src.m365.discovery.workiq_pipeline import WorkIQPipelineError, run_workiq_pipeline


app = typer.Typer(help="Discovery pipeline orchestration commands.")


@dataclass(frozen=True, slots=True)
class SourcePipelineExecution:
    result: DiscoveryRunResult
    staged_candidates: tuple[CandidateEvent, ...]
    written_events: tuple[EventEnvelope, ...]
    dry_run: bool


@dataclass(frozen=True, slots=True)
class NewsletterSourceLoad:
    candidates: tuple[CandidateEvent, ...]
    gaps: tuple[GapDetail, ...]


@dataclass(frozen=True, slots=True)
class ProseSourceLoad:
    candidates: tuple[CandidateEvent, ...]
    gaps: tuple[GapDetail, ...]


@app.command("candidates")
def discover_candidates_command(
    program: str = typer.Option(..., "--program", help="Program ID receiving the discovery run."),
    source: str | None = typer.Option(None, "--source", help="Discovery source to run. Supported: backfill_import, lt_deck, newsletter, email, sharepoint_doc, sharepoint, workiq, teams, outlook, prose_extract."),
    pipeline: str | None = typer.Option(None, "--pipeline", help="Discovery pipeline name when not using --result-json."),
    batch_id: str | None = typer.Option(None, "--batch-id", help="Candidate batch id when not using --result-json."),
    candidate_count: int = typer.Option(0, "--candidate-count", min=0, help="Number of candidates staged by the discovery run."),
    gap_json: list[str] = typer.Option(None, "--gap-json", help="JSON object describing one gap detail; may be repeated."),
    heartbeat: bool = typer.Option(True, "--heartbeat/--no-heartbeat", help="Whether the pipeline produced a heartbeat even if no gaps/candidates were found."),
    result_json: str | None = typer.Option(None, "--result-json", help="Full DiscoveryRunResult JSON payload; cannot be combined with inline fields."),
    input_jsonl: Path | None = typer.Option(None, "--input-jsonl", help="JSONL import source used with --source backfill_import."),
    source_dir: Path | None = typer.Option(None, "--source-dir", help="Source directory used with source-backed discovery runs."),
    from_year: int | None = typer.Option(None, "--from", help="Optional starting year used with source-backed discovery runs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview the selected source pipeline without staging candidates or recording governance events."),
    record: bool = typer.Option(False, "--record", help="Persist the resulting governance events to the ledger instead of previewing only."),
    actor: str = typer.Option("discover_candidates", "--actor", help="Actor recorded on governance events when --record is used."),
    recorded_at: str | None = typer.Option(None, "--recorded-at", help="Optional recorded-at override (ISO-8601)."),
    format: str = typer.Option("text", "--format", help="Output format: text or json."),
    wave: int = typer.Option(1, "--wave", help="Extraction wave for --source prose_extract (1=decision/risk/milestone/metric; 2=phase/scope/workstream; 3=commitment/assumption/dependency/incident; 4=knowledge/sku_generation/kpi)."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, "--programs-root", hidden=True),
) -> None:
    if format not in {"text", "json"}:
        raise typer.BadParameter("--format must be one of: text, json.")

    effective_recorded_at = _parse_cli_datetime(recorded_at) if recorded_at is not None else None
    if source is not None:
        execution = _run_source_pipeline(
            program,
            source=source,
            input_jsonl=input_jsonl,
            source_dir=source_dir,
            from_year=from_year,
            dry_run=dry_run,
            actor=actor,
            recorded_at=effective_recorded_at,
            wave=wave,
            programs_root=programs_root,
        )
        payload = _result_payload(
            program=program,
            result=execution.result,
            recorded=bool(execution.written_events),
            written=execution.written_events,
            dry_run=execution.dry_run,
            staged_count=len(execution.staged_candidates),
        )
        if format == "json":
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
            return
        _emit_text_payload(payload)
        return

    if input_jsonl is not None or source_dir is not None or from_year is not None or dry_run:
        raise typer.BadParameter("--input-jsonl, --source-dir, --from, and --dry-run require --source.")

    result = _resolve_result(
        result_json=result_json,
        pipeline=pipeline,
        batch_id=batch_id,
        candidate_count=candidate_count,
        gap_json=tuple(gap_json or ()),
        heartbeat=heartbeat,
    )
    written = (
        record_discovery_run(
            program,
            result,
            actor=actor,
            recorded_at=effective_recorded_at,
            programs_root=programs_root,
        )
        if record
        else ()
    )
    payload = _result_payload(
        program=program,
        result=result,
        recorded=record,
        written=written,
        dry_run=False,
        staged_count=result.candidates_written,
    )
    if format == "json":
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    _emit_text_payload(payload)


def _run_source_pipeline(
    program: str,
    *,
    source: str,
    input_jsonl: Path | None,
    source_dir: Path | None,
    from_year: int | None,
    dry_run: bool,
    actor: str,
    recorded_at: datetime | None,
    wave: int = 1,
    programs_root: Path,
) -> SourcePipelineExecution:
    selected_source = source.strip().lower()
    batch_id = fresh_discovery_batch_id()
    if selected_source == "backfill_import":
        if source_dir is not None or from_year is not None:
            raise typer.BadParameter("--source backfill_import only supports --input-jsonl.")
        if input_jsonl is None:
            raise typer.BadParameter("--input-jsonl is required with --source backfill_import.")
        if not input_jsonl.exists() or not input_jsonl.is_file():
            raise typer.BadParameter(f"Import source file not found: {input_jsonl}")
        candidates = _load_import_candidates(input_jsonl, program=program, batch_id=batch_id, pipeline=selected_source)
        result = _source_result(
            pipeline=selected_source,
            batch_id=batch_id,
            candidates_written=len(candidates),
            zero_yield_detail=f"Import source {input_jsonl.name} produced no candidate rows.",
        )
    elif selected_source == "lt_deck":
        if input_jsonl is not None:
            raise typer.BadParameter("--source lt_deck does not support --input-jsonl.")
        if source_dir is None:
            raise typer.BadParameter("--source-dir is required with --source lt_deck.")
        if not source_dir.exists() or not source_dir.is_dir():
            raise typer.BadParameter(f"Source directory not found: {source_dir}")
        candidates = _load_lt_deck_candidates(source_dir, program=program, from_year=from_year, batch_id=batch_id, pipeline=selected_source)
        result = _source_result(
            pipeline=selected_source,
            batch_id=batch_id,
            candidates_written=len(candidates),
            zero_yield_detail="No structured LT deck marker candidates matched the requested discovery window.",
        )
    elif selected_source == "newsletter":
        if input_jsonl is not None:
            raise typer.BadParameter("--source newsletter does not support --input-jsonl.")
        if source_dir is None:
            raise typer.BadParameter("--source-dir is required with --source newsletter.")
        if not source_dir.exists() or not source_dir.is_dir():
            raise typer.BadParameter(f"Source directory not found: {source_dir}")
        newsletter_load = _load_newsletter_candidates(source_dir, program=program, from_year=from_year, batch_id=batch_id, pipeline=selected_source)
        candidates = newsletter_load.candidates
        result = _source_result(
            pipeline=selected_source,
            batch_id=batch_id,
            candidates_written=len(candidates),
            zero_yield_detail="No structured newsletter marker candidates matched the requested discovery window.",
            gaps=newsletter_load.gaps,
        )
    elif selected_source == "email":
        if input_jsonl is not None:
            raise typer.BadParameter("--source email does not support --input-jsonl.")
        if source_dir is None:
            raise typer.BadParameter("--source-dir is required with --source email.")
        if not source_dir.exists() or not source_dir.is_dir():
            raise typer.BadParameter(f"Source directory not found: {source_dir}")
        candidates = _load_email_candidates(source_dir, program=program, from_year=from_year, batch_id=batch_id, pipeline=selected_source, programs_root=programs_root)
        result = _source_result(
            pipeline=selected_source,
            batch_id=batch_id,
            candidates_written=len(candidates),
            zero_yield_detail="No structured email marker candidates matched the requested discovery window.",
        )
    elif selected_source == "sharepoint_doc":
        if input_jsonl is not None:
            raise typer.BadParameter("--source sharepoint_doc does not support --input-jsonl.")
        if source_dir is None:
            raise typer.BadParameter("--source-dir is required with --source sharepoint_doc.")
        if not source_dir.exists() or not source_dir.is_dir():
            raise typer.BadParameter(f"Source directory not found: {source_dir}")
        candidates = _load_sharepoint_doc_candidates(
            source_dir,
            program=program,
            from_year=from_year,
            batch_id=batch_id,
            pipeline=selected_source,
            programs_root=programs_root,
        )
        result = _source_result(
            pipeline=selected_source,
            batch_id=batch_id,
            candidates_written=len(candidates),
            zero_yield_detail="No structured SharePoint document marker candidates matched the requested discovery window.",
        )
    elif selected_source == "sharepoint":
        if input_jsonl is not None:
            raise typer.BadParameter("--source sharepoint does not support --input-jsonl.")
        if source_dir is not None:
            raise typer.BadParameter("--source sharepoint does not support --source-dir.")
        try:
            sharepoint_load = run_sharepoint_pipeline(
                program_id=program,
                batch_id=batch_id,
                pipeline=selected_source,
                from_year=from_year,
                programs_root=programs_root,
            )
        except SharePointPipelineError as error:
            raise typer.BadParameter(str(error)) from error
        candidates = sharepoint_load.candidates
        result = _source_result(
            pipeline=selected_source,
            batch_id=batch_id,
            candidates_written=len(candidates),
            zero_yield_detail="Configured SharePoint discovery documents returned no structured discovery markers.",
            gaps=sharepoint_load.gaps,
            append_zero_yield=not sharepoint_load.gaps,
        )
    elif selected_source == "workiq":
        if input_jsonl is not None:
            raise typer.BadParameter("--source workiq does not support --input-jsonl.")
        if source_dir is not None:
            raise typer.BadParameter("--source workiq does not support --source-dir.")
        try:
            workiq_load = run_workiq_pipeline(
                program_id=program,
                batch_id=batch_id,
                pipeline=selected_source,
                from_year=from_year,
                programs_root=programs_root,
            )
        except WorkIQPipelineError as error:
            raise typer.BadParameter(str(error)) from error
        candidates = workiq_load.candidates
        result = _source_result(
            pipeline=selected_source,
            batch_id=batch_id,
            candidates_written=len(candidates),
            zero_yield_detail="Configured WorkIQ queries returned no structured discovery markers.",
            gaps=workiq_load.gaps,
            append_zero_yield=not workiq_load.gaps,
        )
    elif selected_source == "teams":
        if input_jsonl is not None:
            raise typer.BadParameter("--source teams does not support --input-jsonl.")
        if source_dir is not None:
            raise typer.BadParameter("--source teams does not support --source-dir.")
        try:
            teams_load = run_teams_pipeline(
                program_id=program,
                batch_id=batch_id,
                pipeline=selected_source,
                from_year=from_year,
                programs_root=programs_root,
            )
        except TeamsPipelineError as error:
            raise typer.BadParameter(str(error)) from error
        candidates = teams_load.candidates
        result = _source_result(
            pipeline=selected_source,
            batch_id=batch_id,
            candidates_written=len(candidates),
            zero_yield_detail="Configured Teams discovery queries returned no structured discovery markers.",
            gaps=teams_load.gaps,
            append_zero_yield=not teams_load.gaps,
        )
    elif selected_source == "outlook":
        if input_jsonl is not None:
            raise typer.BadParameter("--source outlook does not support --input-jsonl.")
        if source_dir is not None:
            raise typer.BadParameter("--source outlook does not support --source-dir.")
        try:
            outlook_load = run_outlook_pipeline(
                program_id=program,
                batch_id=batch_id,
                pipeline=selected_source,
                from_year=from_year,
                programs_root=programs_root,
            )
        except OutlookPipelineError as error:
            raise typer.BadParameter(str(error)) from error
        candidates = outlook_load.candidates
        result = _source_result(
            pipeline=selected_source,
            batch_id=batch_id,
            candidates_written=len(candidates),
            zero_yield_detail="Configured Outlook discovery queries returned no structured discovery markers.",
            gaps=outlook_load.gaps,
            append_zero_yield=not outlook_load.gaps,
        )
    elif selected_source == "prose_extract":
        if input_jsonl is not None:
            raise typer.BadParameter("--source prose_extract does not support --input-jsonl.")
        if source_dir is None:
            raise typer.BadParameter("--source-dir is required with --source prose_extract.")
        if not source_dir.exists() or not source_dir.is_dir():
            raise typer.BadParameter(f"Source directory not found: {source_dir}")
        prose_load = _load_prose_candidates(source_dir, program=program, from_year=from_year, batch_id=batch_id, pipeline=selected_source, wave=wave)
        candidates = prose_load.candidates
        result = _source_result(
            pipeline=selected_source,
            batch_id=batch_id,
            candidates_written=len(candidates),
            zero_yield_detail="Prose extraction yielded no AI-extracted event candidates (AI may be disabled or unavailable).",
            gaps=prose_load.gaps,
        )
    else:
        raise typer.BadParameter("--source must be one of: backfill_import, lt_deck, newsletter, email, sharepoint_doc, sharepoint, workiq, teams, outlook, prose_extract.")

    if dry_run:
        return SourcePipelineExecution(result=result, staged_candidates=candidates, written_events=(), dry_run=True)

    for candidate in candidates:
        append_candidate(candidate, programs_root=programs_root)
    written = record_discovery_run(
        program,
        result,
        actor=actor,
        recorded_at=recorded_at,
        programs_root=programs_root,
    )
    return SourcePipelineExecution(result=result, staged_candidates=candidates, written_events=written, dry_run=False)


def _load_import_candidates(source_path: Path, *, program: str, batch_id: str, pipeline: str) -> tuple[CandidateEvent, ...]:
    candidates: list[CandidateEvent] = []
    for index, raw_line in enumerate(source_path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            candidates.append(candidate_from_import_line(stripped, program=program, batch_id=batch_id, pipeline=pipeline))
        except DiscoveryCandidateBuildError as error:
            raise typer.BadParameter(f"Import row {index}: {error}") from error
    return tuple(candidates)


def _load_lt_deck_candidates(
    source_dir: Path,
    *,
    program: str,
    from_year: int | None,
    batch_id: str,
    pipeline: str,
) -> tuple[CandidateEvent, ...]:
    candidates: list[CandidateEvent] = []
    for path in sorted(source_dir.rglob("*.pptx"), key=lambda item: item.as_posix().lower()):
        if path.parent == source_dir:
            continue
        year = _extract_lt_deck_year(path)
        if year is None:
            continue
        if from_year is not None and year < from_year:
            continue
        relative_path = path.relative_to(source_dir).as_posix()
        try:
            batch = extract_lt_deck_candidates_from_pptx(
                program_id=program,
                source_path=path,
                relative_path=relative_path,
                batch_id=batch_id,
                pipeline=pipeline,
            )
        except LTDeckExtractorError as error:
            raise typer.BadParameter(str(error)) from error
        candidates.extend(batch.candidates)
    return tuple(candidates)


def _load_newsletter_candidates(
    source_dir: Path,
    *,
    program: str,
    from_year: int | None,
    batch_id: str,
    pipeline: str,
) -> NewsletterSourceLoad:
    candidates: list[CandidateEvent] = []
    issue_numbers: list[int] = []
    for path in sorted(_iter_newsletter_files(source_dir), key=lambda item: item.as_posix().lower()):
        relative_path = path.relative_to(source_dir).as_posix()
        if from_year is not None and not _newsletter_matches_from_year(path, from_year):
            continue
        issue_number = extract_newsletter_issue_number(path)
        if issue_number is not None:
            issue_numbers.append(issue_number)
        try:
            batch = extract_newsletter_candidates(
                program_id=program,
                source_path=path,
                relative_path=relative_path,
                batch_id=batch_id,
                pipeline=pipeline,
            )
        except NewsletterExtractorError as error:
            raise typer.BadParameter(str(error)) from error
        candidates.extend(batch.candidates)
    return NewsletterSourceLoad(
        candidates=tuple(candidates),
        gaps=_newsletter_sequence_gaps(sorted(set(issue_numbers))),
    )


def _load_prose_candidates(
    source_dir: Path,
    *,
    program: str,
    from_year: int | None,
    batch_id: str,
    pipeline: str,
    wave: int = 1,
) -> ProseSourceLoad:
    client = _build_prose_extract_client()
    candidates: list[CandidateEvent] = []

    # Process newsletter files (EML/HTML/PDF)
    for path in sorted(_iter_newsletter_files(source_dir), key=lambda item: item.as_posix().lower()):
        if from_year is not None and not _newsletter_matches_from_year(path, from_year):
            continue
        publication_date = extract_newsletter_publication_date(path)
        default_occurred_at = (
            datetime.combine(publication_date, datetime.min.time(), tzinfo=timezone.utc)
            if publication_date is not None
            else datetime.now(timezone.utc)
        )
        relative_path = path.relative_to(source_dir).as_posix()
        issue_number = extract_newsletter_issue_number(path)
        newsletter_ref = NewsletterRef(
            file_path=relative_path,
            publication_date=publication_date or default_occurred_at.date(),
            issue_number=issue_number,
        )
        try:
            prose_text = normalize_newsletter_text(path)
        except (NewsletterExtractorError, Exception):
            continue
        batch = extract_prose_event_candidates(
            prose_text=prose_text,
            program_id=program,
            source_ref=newsletter_ref,
            batch_id=batch_id,
            default_occurred_at=default_occurred_at,
            pipeline=pipeline,
            wave=wave,
            client=client,
        )
        candidates.extend(batch.candidates)

    # Process LT deck files (PPTX)
    for path in sorted(source_dir.rglob("*.pptx"), key=lambda item: item.as_posix().lower()):
        if path.parent == source_dir:
            continue
        year = _extract_lt_deck_year(path)
        if year is None:
            continue
        if from_year is not None and year < from_year:
            continue
        deck_date = extract_lt_deck_date_from_path(path)
        if deck_date is None:
            continue
        default_occurred_at = datetime.combine(deck_date, datetime.min.time(), tzinfo=timezone.utc)
        relative_path = path.relative_to(source_dir).as_posix()
        deck_ref = LTDeckRef(file_path=relative_path, deck_date=deck_date)
        try:
            prose_text = get_lt_deck_prose_text(path)
        except Exception:
            continue
        if not prose_text.strip():
            continue
        batch = extract_prose_event_candidates(
            prose_text=prose_text,
            program_id=program,
            source_ref=deck_ref,
            batch_id=batch_id,
            default_occurred_at=default_occurred_at,
            pipeline=pipeline,
            wave=wave,
            client=client,
        )
        candidates.extend(batch.candidates)

    return ProseSourceLoad(candidates=tuple(candidates), gaps=())


def _build_prose_extract_client() -> "FallbackAIClient | None":
    deployments = resolve_ai_deployments(
        primary_candidates=(None,),
        backup_candidates=(None,),
        primary_fallback_envs=("VERTEX_AI_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT"),
        backup_fallback_envs=("VERTEX_AI_BACKUP_DEPLOYMENT",),
    )
    if not deployments:
        return None
    try:
        return FallbackAIClient(deployments=deployments, temperature=0.0, budget_usd=2.0)
    except (AIClientError, RuntimeError):
        return None


def _load_email_candidates(
    source_dir: Path,
    *,
    program: str,
    from_year: int | None,
    batch_id: str,
    pipeline: str,
    programs_root: Path,
) -> tuple[CandidateEvent, ...]:
    candidates_by_week: dict[tuple[int, int], list[CandidateEvent]] = {}
    for path in sorted(source_dir.rglob("*.eml"), key=lambda item: item.as_posix().lower()):
        try:
            if from_year is not None and not _email_matches_from_year(path, from_year):
                continue
            batch = extract_email_candidates(
                program_id=program,
                source_path=path,
                batch_id=batch_id,
                pipeline=pipeline,
                programs_root=programs_root,
            )
        except (EmailExtractorError, MIMETextError) as error:
            raise typer.BadParameter(str(error)) from error
        for candidate in batch.candidates:
            week_key = candidate.proposed_occurred_at.isocalendar()[:2]
            candidates_by_week.setdefault(week_key, []).append(candidate)
    collapsed: list[CandidateEvent] = []
    for week_key in sorted(candidates_by_week):
        collapsed.extend(_collapse_weekly_email_candidates(candidates_by_week[week_key]))
    return tuple(collapsed)


def _load_sharepoint_doc_candidates(
    source_dir: Path,
    *,
    program: str,
    from_year: int | None,
    batch_id: str,
    pipeline: str,
    programs_root: Path,
) -> tuple[CandidateEvent, ...]:
    candidates: list[CandidateEvent] = []
    for path in sorted(_iter_sharepoint_doc_files(source_dir), key=lambda item: item.as_posix().lower()):
        modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if from_year is not None and modified_at.year < from_year:
            continue
        relative = path.relative_to(source_dir)
        site, doc_path = _sharepoint_site_and_doc_path(relative, source_dir=source_dir)
        try:
            batch = extract_sharepoint_doc_candidates(
                program_id=program,
                source_path=path,
                relative_path=doc_path,
                site=site,
                batch_id=batch_id,
                pipeline=pipeline,
                programs_root=programs_root,
            )
        except SharePointDocExtractorError as error:
            raise typer.BadParameter(str(error)) from error
        candidates.extend(batch.candidates)
    return tuple(candidates)


def _collapse_weekly_email_candidates(candidates: list[CandidateEvent]) -> tuple[CandidateEvent, ...]:
    if not candidates:
        return ()
    grouped: dict[tuple[str, str], list[CandidateEvent]] = {}
    for candidate in sorted(candidates, key=_email_candidate_sort_key):
        grouped.setdefault((candidate.proposed_event_type, candidate.dedupe_core_hash), []).append(candidate)
    merged: list[CandidateEvent] = []
    for key in sorted(grouped):
        merged.append(_merge_email_candidate_group(grouped[key]))
    return tuple(merged)


def _merge_email_candidate_group(group: list[CandidateEvent]) -> CandidateEvent:
    primary = group[0]
    corroborating_refs = list(primary.corroborating_refs)
    seen_refs = {_source_ref_identity(primary.source_ref)}
    for candidate in group[1:]:
        for ref in (candidate.source_ref, *candidate.corroborating_refs):
            identity = _source_ref_identity(ref)
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


def _email_candidate_sort_key(candidate: CandidateEvent) -> tuple[datetime, str, str]:
    message_id = getattr(candidate.source_ref, "message_id", None) or ""
    return (candidate.proposed_occurred_at, message_id, candidate.candidate_id)


def _source_ref_identity(source_ref: object) -> str:
    return repr(source_ref)


def _iter_sharepoint_doc_files(source_dir: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for pattern in ("*.md", "*.txt"):
        files.extend(source_dir.rglob(pattern))
    return tuple(files)


def _sharepoint_site_and_doc_path(relative_path: Path, *, source_dir: Path) -> tuple[str, str]:
    parts = relative_path.parts
    if len(parts) >= 2:
        return parts[0], "/".join(parts[1:])
    return source_dir.name, relative_path.as_posix()


def _email_matches_from_year(path: Path, from_year: int) -> bool:
    sent_at = parse_eml_message(path).sent_at
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    return sent_at.astimezone(timezone.utc).year >= from_year


def _extract_lt_deck_year(path: Path) -> int | None:
    try:
        return int(path.parent.name)
    except ValueError:
        return None


def _iter_newsletter_files(source_dir: Path) -> tuple[Path, ...]:
    primary_files: list[Path] = []
    for pattern in ("*.html", "*.htm", "*.eml"):
        primary_files.extend(source_dir.rglob(pattern))
    identities = {_newsletter_file_identity(path) for path in primary_files}
    pdf_fallbacks = [
        path
        for path in source_dir.rglob("*.pdf")
        if _newsletter_file_identity(path) not in identities
    ]
    return tuple(primary_files + pdf_fallbacks)


def _newsletter_file_identity(path: Path) -> str:
    issue_number = extract_newsletter_issue_number(path)
    publication_date = extract_newsletter_publication_date(path)
    if issue_number is not None and publication_date is not None:
        return f"issue:{issue_number}:date:{publication_date.isoformat()}"
    if issue_number is not None:
        return f"issue:{issue_number}"
    if publication_date is not None:
        return f"date:{publication_date.isoformat()}"
    return f"path:{path.stem.strip().lower()}"


def _newsletter_matches_from_year(path: Path, from_year: int) -> bool:
    publication_date = extract_newsletter_publication_date(path)
    return publication_date is not None and publication_date.year >= from_year


def _newsletter_sequence_gaps(issue_numbers: list[int]) -> tuple[GapDetail, ...]:
    if len(issue_numbers) < 2:
        return ()
    missing: list[int] = []
    for prior, current in zip(issue_numbers, issue_numbers[1:], strict=False):
        if current <= prior + 1:
            continue
        missing.extend(range(prior + 1, current))
    if not missing:
        return ()
    formatted = ", ".join(str(issue_number) for issue_number in missing)
    return (
        GapDetail(
            gap_kind="missed_window",
            window_start=None,
            window_end=None,
            detail=f"Newsletter corpus is missing issue numbers within the observed range: {formatted}.",
        ),
    )


def _source_result(
    *,
    pipeline: str,
    batch_id: str,
    candidates_written: int,
    zero_yield_detail: str,
    gaps: tuple[GapDetail, ...] = (),
    append_zero_yield: bool = True,
) -> DiscoveryRunResult:
    result_gaps = list(gaps)
    if candidates_written == 0 and append_zero_yield:
        result_gaps.append(GapDetail(gap_kind="zero_yield", window_start=None, window_end=None, detail=zero_yield_detail))
    return DiscoveryRunResult(
        pipeline=pipeline,
        batch_id=batch_id,
        candidates_written=candidates_written,
        gaps=tuple(result_gaps),
        heartbeat=False,
    )


def _result_payload(
    *,
    program: str,
    result: DiscoveryRunResult,
    recorded: bool,
    written: tuple[EventEnvelope, ...],
    dry_run: bool,
    staged_count: int,
) -> dict[str, Any]:
    payload = {
        "program_id": program,
        "pipeline": result.pipeline,
        "batch_id": result.batch_id,
        "candidate_count": result.candidates_written,
        "heartbeat": result.heartbeat,
        "gap_count": len(result.gaps),
        "gaps": [_gap_to_payload(gap) for gap in result.gaps],
        "dry_run": dry_run,
        "staged_count": staged_count,
        "recorded": recorded,
        "written_event_ids": [event.event_id for event in written],
        "written_event_types": [event.event_type for event in written],
    }
    return payload


def _emit_text_payload(payload: dict[str, Any]) -> None:
    typer.echo(
        f"pipeline={payload['pipeline']} batch_id={payload['batch_id']} candidates={payload['candidate_count']} "
        f"gaps={payload['gap_count']} heartbeat={payload['heartbeat']}"
    )
    for gap in payload["gaps"]:
        typer.echo(f"gap[{gap['gap_kind']}] {gap['detail']}")
    if payload["dry_run"]:
        typer.echo("Preview only; no candidates staged and no ledger events written.")
        return
    if payload["recorded"]:
        typer.echo(f"Recorded {len(payload['written_event_ids'])} governance event(s).")
        for event_type, event_id in zip(payload["written_event_types"], payload["written_event_ids"], strict=False):
            typer.echo(f"  {event_type} -> {event_id}")
    else:
        typer.echo("Preview only; no ledger events written.")


def _resolve_result(
    *,
    result_json: str | None,
    pipeline: str | None,
    batch_id: str | None,
    candidate_count: int,
    gap_json: tuple[str, ...],
    heartbeat: bool,
) -> DiscoveryRunResult:
    if result_json is not None:
        if any(value is not None for value in (pipeline, batch_id)) or candidate_count != 0 or gap_json or heartbeat is not True:
            raise typer.BadParameter("--result-json cannot be combined with inline discovery-run fields.")
        return _parse_result_json(result_json)
    if pipeline is None or not pipeline.strip():
        raise typer.BadParameter("--pipeline is required when --result-json is not provided.")
    if batch_id is None or not batch_id.strip():
        raise typer.BadParameter("--batch-id is required when --result-json is not provided.")
    return DiscoveryRunResult(
        pipeline=pipeline.strip(),
        batch_id=batch_id.strip(),
        candidates_written=candidate_count,
        gaps=tuple(_parse_gap_json(entry) for entry in gap_json),
        heartbeat=heartbeat,
    )


def _parse_result_json(raw: str) -> DiscoveryRunResult:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise typer.BadParameter(f"--result-json must decode to a JSON object: {error}") from error
    if not isinstance(payload, dict):
        raise typer.BadParameter("--result-json must decode to a JSON object.")
    pipeline = payload.get("pipeline")
    batch_id = payload.get("batch_id")
    candidates_written = payload.get("candidates_written")
    heartbeat = payload.get("heartbeat")
    gaps = payload.get("gaps", [])
    if not isinstance(pipeline, str) or not pipeline.strip():
        raise typer.BadParameter("result.pipeline must be a non-empty string.")
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise typer.BadParameter("result.batch_id must be a non-empty string.")
    if not isinstance(candidates_written, int) or isinstance(candidates_written, bool) or candidates_written < 0:
        raise typer.BadParameter("result.candidates_written must be a non-negative integer.")
    if not isinstance(heartbeat, bool):
        raise typer.BadParameter("result.heartbeat must be a boolean.")
    if not isinstance(gaps, list):
        raise typer.BadParameter("result.gaps must be a list.")
    return DiscoveryRunResult(
        pipeline=pipeline.strip(),
        batch_id=batch_id.strip(),
        candidates_written=candidates_written,
        gaps=tuple(_gap_from_payload(item) for item in gaps),
        heartbeat=heartbeat,
    )


def _parse_gap_json(raw: str) -> GapDetail:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise typer.BadParameter(f"--gap-json must decode to a JSON object: {error}") from error
    return _gap_from_payload(payload)


def _gap_from_payload(payload: Any) -> GapDetail:
    if not isinstance(payload, dict):
        raise typer.BadParameter("gap payload must be a JSON object.")
    gap_kind = payload.get("gap_kind")
    detail = payload.get("detail")
    if not isinstance(gap_kind, str) or not gap_kind.strip():
        raise typer.BadParameter("gap_kind must be a non-empty string.")
    if not isinstance(detail, str) or not detail.strip():
        raise typer.BadParameter("detail must be a non-empty string.")
    return GapDetail(
        gap_kind=gap_kind.strip(),
        window_start=_parse_optional_datetime(payload.get("window_start"), field_name="window_start"),
        window_end=_parse_optional_datetime(payload.get("window_end"), field_name="window_end"),
        detail=detail.strip(),
    )


def _gap_to_payload(gap: GapDetail) -> dict[str, Any]:
    return {
        "gap_kind": gap.gap_kind,
        "detail": gap.detail,
        "window_start": gap.window_start.isoformat() if gap.window_start is not None else None,
        "window_end": gap.window_end.isoformat() if gap.window_end is not None else None,
    }


def _parse_cli_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_optional_datetime(value: Any, *, field_name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise typer.BadParameter(f"{field_name} must be an ISO-8601 string when present.")
    try:
        return _parse_cli_datetime(value)
    except ValueError as error:
        raise typer.BadParameter(f"{field_name} must be a valid ISO-8601 timestamp.") from error
