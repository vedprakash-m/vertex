from __future__ import annotations

import csv
from dataclasses import asdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import getpass
from io import StringIO
import json
from src.core.jsonl_utils import parse_jsonl_line
from pathlib import Path
import re
from typing import Any, Callable
from uuid import uuid4

import typer
import yaml

from src.commands import gather as gather_command_helpers
from src.ai.edit_learner import (
    EditPattern,
    CalibrationSummary,
    ConfidenceBandSummary,
    PromptVersionConfidenceSummary,
    PromptVersionSummary,
    read_edit_patterns,
    summarize_recent_calibration,
    summarize_recent_confidence_bands,
    summarize_recent_prompt_version_confidence_bands,
    summarize_recent_prompt_versions,
)
from src.core.ado_client import ADOClient
from src.core.ado_proposal import ADOUpdateEntry, ADOUpdateProposal, find_proposal_manifest, read_proposal_manifest
from src.core.analytics_store import AutonomyAuditArchiveArtifacts, AutonomyAuditRecord, append_autonomy_audit_record, archive_autonomy_audit_records, compute_prior_acceptance_rate, load_autonomy_audit_records
from src.core.archive_store import read_archive_index
from src.core.feedback.signal_approval_learner import pause_signal_approval_rules
from src.core.edition_resolver import get_program_output_dir
from src.core.journal import PROGRAMS_ROOT, read_signals
from src.core.models import Confidence
from src.core.models_v2 import SignalReviewDecision, SignalUsageMarker
from src.core.store_factory import build_signal_store_for_program_id, read_signal_review_log_for_program_id
from src.m365.ado_writer import ADORollbackArtifacts, ADOWriter


app = typer.Typer(help="Inspect audit history and autonomy governance state.", invoke_without_command=True)


@dataclass(frozen=True, slots=True)
class AuditEvent:
    timestamp: datetime
    category: str
    edition_id: str | None
    reference: str
    source: str
    summary: str
    trace_run_id: str | None = None
    model: str | None = None
    deployment: str | None = None
    prompt_version: str | None = None
    task_type: str | None = None
    ai_confidence: str | None = None
    author_override_magnitude: float | None = None


@dataclass(frozen=True, slots=True)
class AuditPromptLeaderboardRow:
    task_type: str
    rank: int
    prompt_version: str
    sample_count: int
    average_override_magnitude: float
    calibration_score: float


@dataclass(frozen=True, slots=True)
class AuditModelDeploymentLeaderboardRow:
    task_type: str
    rank: int
    model: str
    deployment: str
    sample_count: int
    average_override_magnitude: float
    calibration_score: float
    average_latency_ms: float | None
    average_cost_usd: float | None


@dataclass(frozen=True, slots=True)
class AuditPromptVersionModelDeploymentLeaderboardRow:
    task_type: str
    rank: int
    prompt_version: str
    model: str
    deployment: str
    sample_count: int
    average_override_magnitude: float
    calibration_score: float
    average_latency_ms: float | None
    average_cost_usd: float | None


@dataclass(frozen=True, slots=True)
class AuditPromptVersionModelLeaderboardRow:
    task_type: str
    rank: int
    prompt_version: str
    model: str
    deployment_count: int
    sample_count: int
    average_override_magnitude: float
    calibration_score: float
    average_latency_ms: float | None
    average_cost_usd: float | None


@dataclass(frozen=True, slots=True)
class AuditModelLeaderboardRow:
    task_type: str
    rank: int
    model: str
    deployment_count: int
    sample_count: int
    average_override_magnitude: float
    calibration_score: float
    average_latency_ms: float | None
    average_cost_usd: float | None


@dataclass(frozen=True, slots=True)
class AuditConfidenceModelDeploymentLeaderboardRow:
    task_type: str
    rank: int
    ai_confidence: str
    model: str
    deployment: str
    sample_count: int
    average_override_magnitude: float
    calibration_score: float
    average_latency_ms: float | None
    average_cost_usd: float | None


@dataclass(frozen=True, slots=True)
class AuditPromptLearningReport:
    window_issues: int
    calibration: tuple[CalibrationSummary, ...]
    confidence_bands: tuple[ConfidenceBandSummary, ...]
    prompt_versions: tuple[PromptVersionSummary, ...]
    prompt_version_confidence_bands: tuple[PromptVersionConfidenceSummary, ...]
    leaderboard: tuple[AuditPromptLeaderboardRow, ...]
    prompt_version_model_leaderboard: tuple[AuditPromptVersionModelLeaderboardRow, ...]
    model_leaderboard: tuple[AuditModelLeaderboardRow, ...]
    model_deployment_leaderboard: tuple[AuditModelDeploymentLeaderboardRow, ...]
    prompt_version_model_deployment_leaderboard: tuple[AuditPromptVersionModelDeploymentLeaderboardRow, ...]
    confidence_model_deployment_leaderboard: tuple[AuditConfidenceModelDeploymentLeaderboardRow, ...]


@dataclass(frozen=True, slots=True)
class _AuditPromptLearningTraceRecord:
    timestamp: datetime
    run_id: str
    edition_id: str
    issue_number: int
    section_id: str
    task_type: str
    prompt_version: str | None
    model: str
    deployment: str
    latency_ms: float | None
    cost_usd: float | None


ProgramLoader = Callable[[str, Path], tuple[object, tuple[object, ...]]]


@app.callback(invoke_without_command=True)
def audit_command(
    ctx: typer.Context,
    program: str | None = typer.Option(None, "--program", help="Program id, e.g. myprogram."),
    from_date: str | None = typer.Option(None, "--from", help="Inclusive start date in YYYY-MM-DD."),
    to_date: str | None = typer.Option(None, "--to", help="Inclusive end date in YYYY-MM-DD."),
    prompt_learning_summary: bool = typer.Option(
        False,
        "--prompt-learning-summary",
        help="Append rolling calibration, prompt-version performance, and joined model/deployment summaries.",
    ),
    window_issues: int = typer.Option(
        10,
        "--window-issues",
        min=1,
        help="Rolling issue window used when building the prompt-learning summary.",
    ),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if program is None or not program.strip():
        raise typer.BadParameter("--program is required.")
    resolved_program = program.strip()
    start_date = _parse_iso_date(from_date, option_name="--from") if from_date is not None else None
    end_date = _parse_iso_date(to_date, option_name="--to") if to_date is not None else None
    events = build_audit_timeline(resolved_program, start_date=start_date, end_date=end_date)
    prompt_learning = (
        build_prompt_learning_report(resolved_program, window_issues=window_issues) if prompt_learning_summary else None
    )

    if format == "human":
        typer.echo(render_audit_timeline(resolved_program, events, prompt_learning=prompt_learning))
        raise typer.Exit(code=0)
    if format == "json":
        payload: dict[str, Any] = {
            "program_id": resolved_program,
            "events": [
                {
                    **asdict(event),
                    "timestamp": event.timestamp.isoformat(),
                }
                for event in events
            ],
        }
        if prompt_learning is not None:
            payload["prompt_learning"] = {
                "window_issues": prompt_learning.window_issues,
                "calibration": [asdict(summary) for summary in prompt_learning.calibration],
                "confidence_bands": [asdict(summary) for summary in prompt_learning.confidence_bands],
                "prompt_versions": [asdict(summary) for summary in prompt_learning.prompt_versions],
                "prompt_version_confidence_bands": [
                    asdict(summary) for summary in prompt_learning.prompt_version_confidence_bands
                ],
                "leaderboard": [asdict(row) for row in prompt_learning.leaderboard],
                "prompt_version_model_leaderboard": [
                    asdict(row) for row in prompt_learning.prompt_version_model_leaderboard
                ],
                "model_leaderboard": [asdict(row) for row in prompt_learning.model_leaderboard],
                "model_deployment_leaderboard": [
                    asdict(row) for row in prompt_learning.model_deployment_leaderboard
                ],
                "prompt_version_model_deployment_leaderboard": [
                    asdict(row) for row in prompt_learning.prompt_version_model_deployment_leaderboard
                ],
                "confidence_model_deployment_leaderboard": [
                    asdict(row) for row in prompt_learning.confidence_model_deployment_leaderboard
                ],
            }
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        raise typer.Exit(code=0)
    if format == "csv":
        typer.echo(render_audit_csv(events, prompt_learning=prompt_learning), nl=False)
        raise typer.Exit(code=0)
    raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")


@app.command("archive")
def audit_archive_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    before: str | None = typer.Option(None, "--before", help="Archive autonomy audit rows before YYYY-MM-DD."),
    retention: bool = typer.Option(False, "--retention", help="Archive autonomy audit rows older than the configured retention window in program.yaml."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    if retention and before is not None:
        raise typer.BadParameter("Choose either --before or --retention, not both.")
    if not retention and before is None:
        raise typer.BadParameter("Provide either --before or --retention.")
    retention_days = _load_audit_retention_days(program, programs_root=PROGRAMS_ROOT) if retention else None
    if retention and retention_days is not None:
        before_date = datetime.now(timezone.utc).date() - timedelta(days=retention_days)
    else:
        assert before is not None  # guarded above: raises if not retention and before is None
        before_date = _parse_iso_date(before, option_name="--before")
    artifacts = archive_autonomy_audit_records(program, before=before_date, programs_root=PROGRAMS_ROOT)
    if format == "human":
        if artifacts.archived_count == 0:
            if retention and retention_days is not None:
                typer.echo(
                    f"No autonomy audit rows eligible under the configured retention window ({retention_days} day(s)) for {program}."
                )
            else:
                typer.echo(f"No autonomy audit rows before {before_date.isoformat()} for {program}.")
            raise typer.Exit(code=0)
        if retention and retention_days is not None:
            typer.echo(
                f"Archived {artifacts.archived_count} autonomy audit row(s) for {program} using configured retention {retention_days} day(s)."
            )
        else:
            typer.echo(f"Archived {artifacts.archived_count} autonomy audit row(s) for {program} before {before_date.isoformat()}.")
        typer.echo(f"Remaining active autonomy audit rows: {artifacts.remaining_count}")
        for path in artifacts.archive_paths:
            typer.echo(f"- {path}")
        raise typer.Exit(code=0)
    typer.echo(render_audit_archive_output(artifacts=artifacts, format=format), nl=False)
    raise typer.Exit(code=0)


@app.command("pause")
def audit_pause_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    action_type: str = typer.Option(..., "--action-type", help="Action type to pause batch approval for, for example vitality_nudge."),
    updated_by: str | None = typer.Option(None, "--updated-by", help="Author alias for the pause audit record."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview the pause without changing policy state or audit history."),
) -> None:
    program_id = program.strip()
    normalized_action_type = action_type.strip().lower()
    if not program_id:
        raise typer.BadParameter("--program is required.")
    if not normalized_action_type:
        raise typer.BadParameter("--action-type is required.")

    actor = _default_actor(updated_by)
    timestamp = _utc_now()
    try:
        artifacts = pause_signal_approval_rules(
            program_id,
            action_type=normalized_action_type,
            as_of=timestamp,
            programs_root=PROGRAMS_ROOT,
            dry_run=dry_run,
        )
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error

    paused_rule_ids = ", ".join(sorted(rule.proposal.rule_id for rule in artifacts.paused_rules))
    if dry_run:
        typer.echo(
            f"Dry-run: would pause batch approval for {normalized_action_type} in {program_id} ({paused_rule_ids})."
        )
        raise typer.Exit(code=0)

    append_autonomy_audit_record(
        AutonomyAuditRecord(
            program_id=program_id,
            action_id=str(uuid4()),
            level="l2",
            author_alias=actor,
            subject_alias=None,
            action_type="policy_paused",
            evidence_refs=tuple(
                [
                    f"action_type:{normalized_action_type}",
                    *[
                        f"signal_approval_rule:{rule.proposal.rule_id}"
                        for rule in sorted(artifacts.paused_rules, key=lambda rule: rule.proposal.rule_id)
                    ],
                ]
            ),
            policy_rule=artifacts.paused_rules[0].proposal.rule_id if len(artifacts.paused_rules) == 1 else None,
            accepted=True,
            applied_at=timestamp,
            blast_radius=(
                f"Local batch approval pause for {normalized_action_type}; "
                f"{len(artifacts.paused_rules)} promoted rule(s) disabled and future auto-apply triggers halted."
            ),
            rollback_mechanism=(
                "Promote the signal approval rule again via vertex policy promote --rule <id> to restore batch approvals."
            ),
            prior_acceptance_rate=compute_prior_acceptance_rate(
                program_id,
                action_type=normalized_action_type,
                programs_root=PROGRAMS_ROOT,
            ),
        ),
        programs_root=PROGRAMS_ROOT,
    )

    typer.echo(f"Paused batch approval for {normalized_action_type} in {program_id}.")
    typer.echo(f"Paused rule(s): {paused_rule_ids}")
    if artifacts.path is not None:
        typer.echo(f"Policy file: {artifacts.path}")


@app.command("rollback")
def audit_rollback_command(
    action: str | None = typer.Option(None, "--action", help="Original autonomy action id to roll back."),
    batch: str | None = typer.Option(None, "--batch", help="Roll back all proposal-backed autonomy actions applied on YYYY-MM-DD. Requires --program."),
    program: str | None = typer.Option(None, "--program", help="Program id when you want to avoid action-id lookup across programs."),
    updated_by: str | None = typer.Option(None, "--updated-by", help="Author alias for the rollback audit record."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview the rollback target without applying any external writes."),
) -> None:
    if (action is None) == (batch is None):
        raise typer.BadParameter("Choose exactly one of --action or --batch.")
    actor = _default_actor(updated_by)
    timestamp = _utc_now()
    if action is not None:
        action_id = action.strip()
        if not action_id:
            raise typer.BadParameter("--action is required.")
        record = _find_autonomy_audit_record(action_id, program_id=program, programs_root=PROGRAMS_ROOT)
        manifest_path, proposal, rollbackable_entries = _load_rollback_target(record, programs_root=PROGRAMS_ROOT)
        if not rollbackable_entries:
            typer.echo(f"No applied proposal entries remain to roll back for action {action_id}.")
            raise typer.Exit(code=0)
        if dry_run:
            typer.echo(
                f"Dry-run: would roll back {len(rollbackable_entries)} applied ADO update(s) from proposal {proposal.id} for action {action_id}."
            )
            for entry in rollbackable_entries:
                typer.echo(f"- WI:{entry.work_item_id} | {entry.action} | {entry.field_or_tag}")
            raise typer.Exit(code=0)

        artifacts = rollback_audit_action(
            action_id,
            program_id=record.program_id,
            rolled_back_at=timestamp,
            programs_root=PROGRAMS_ROOT,
            program_loader=gather_command_helpers._load_program_context,
        )
        _append_rollback_audit_record(
            original_record=record,
            proposal_id=proposal.id,
            artifacts=artifacts,
            author_alias=actor,
            applied_at=timestamp,
            programs_root=PROGRAMS_ROOT,
        )
        typer.echo(
            f"Rolled back action {action_id} for {record.program_id}: {artifacts.rolled_back_count} rolled back, "
            f"{artifacts.skipped_count} skipped, {artifacts.conflict_count} conflict, {artifacts.failed_count} failed."
        )
        typer.echo(f"Manifest: {manifest_path}")
        for result in artifacts.results:
            if result.status in {"conflict", "failed"}:
                typer.echo(f"- WI:{result.work_item_id} | {result.action} | {result.status} | {result.status_reason}")
        raise typer.Exit(code=_rollback_exit_code(artifacts))

    if program is None or not program.strip():
        raise typer.BadParameter("--program is required with --batch.")
    assert batch is not None  # guarded at function entry: exactly one of --action or --batch is provided
    batch_date = _parse_iso_date(batch, option_name="--batch")
    program_id = program.strip()
    records = _find_autonomy_audit_records_for_batch(batch_date, program_id=program_id, programs_root=PROGRAMS_ROOT)
    targets = [
        (record, *_load_rollback_target(record, programs_root=PROGRAMS_ROOT))
        for record in records
    ]
    actionable_targets = [target for target in targets if target[3]]
    if not actionable_targets:
        typer.echo(f"No proposal-backed autonomy actions on {batch_date.isoformat()} remain eligible for rollback in {program_id}.")
        raise typer.Exit(code=0)
    if dry_run:
        typer.echo(
            f"Dry-run: would roll back {len(actionable_targets)} autonomy action(s) applied on {batch_date.isoformat()} in {program_id}."
        )
        for record, _manifest_path, proposal, rollbackable_entries in actionable_targets:
            typer.echo(
                f"- {record.action_id} | proposal {proposal.id} | {len(rollbackable_entries)} applied update(s)"
            )
        raise typer.Exit(code=0)

    batch_artifacts: list[tuple[AutonomyAuditRecord, Path, ADOUpdateProposal, ADORollbackArtifacts]] = []
    for record, manifest_path, proposal, _rollbackable_entries in actionable_targets:
        artifacts = rollback_audit_action(
            record.action_id,
            program_id=record.program_id,
            rolled_back_at=timestamp,
            programs_root=PROGRAMS_ROOT,
            program_loader=gather_command_helpers._load_program_context,
        )
        _append_rollback_audit_record(
            original_record=record,
            proposal_id=proposal.id,
            artifacts=artifacts,
            author_alias=actor,
            applied_at=timestamp,
            programs_root=PROGRAMS_ROOT,
        )
        batch_artifacts.append((record, manifest_path, proposal, artifacts))

    total_rolled_back = sum(artifacts.rolled_back_count for _, _, _, artifacts in batch_artifacts)
    total_skipped = sum(artifacts.skipped_count for _, _, _, artifacts in batch_artifacts)
    total_conflict = sum(artifacts.conflict_count for _, _, _, artifacts in batch_artifacts)
    total_failed = sum(artifacts.failed_count for _, _, _, artifacts in batch_artifacts)
    typer.echo(
        f"Rolled back batch {batch_date.isoformat()} for {program_id}: {len(batch_artifacts)} action(s), "
        f"{total_rolled_back} rolled back, {total_skipped} skipped, {total_conflict} conflict, {total_failed} failed."
    )
    for record, manifest_path, _proposal, artifacts in batch_artifacts:
        typer.echo(
            f"- {record.action_id} | {artifacts.rolled_back_count} rolled back, {artifacts.skipped_count} skipped, "
            f"{artifacts.conflict_count} conflict, {artifacts.failed_count} failed | {manifest_path}"
        )
    raise typer.Exit(code=_rollback_exit_code_from_counts(conflict_count=total_conflict, failed_count=total_failed))


def rollback_audit_action(
    action_id: str,
    *,
    program_id: str,
    rolled_back_at: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
    program_loader: ProgramLoader | None = None,
    client_factory: Callable[[object], ADOClient] | None = None,
) -> ADORollbackArtifacts:
    record = _find_autonomy_audit_record(action_id, program_id=program_id, programs_root=programs_root)
    proposal_ref = _proposal_reference_from_audit_record(record)
    if proposal_ref is None:
        raise typer.BadParameter(
            f"Action '{action_id}' is not backed by an ADO proposal manifest and cannot be rolled back."
        )
    manifest_path = _resolve_audit_proposal_manifest(proposal_ref, programs_root=programs_root)
    program, _ = (program_loader or gather_command_helpers._load_program_context)(record.program_id, programs_root)
    client = (client_factory or _build_ado_client_for_program)(program)
    return ADOWriter(client, programs_root=programs_root).rollback_manifest(
        manifest_path,
        action_id=action_id,
        rolled_back_at=rolled_back_at,
    )


def build_audit_timeline(
    program_id: str,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    programs_root: Path | None = None,
) -> tuple[AuditEvent, ...]:
    resolved_programs_root = programs_root or PROGRAMS_ROOT
    start_ts = datetime.combine(start_date, time.min, tzinfo=timezone.utc) if start_date is not None else None
    end_ts = datetime.combine(end_date, time.max, tzinfo=timezone.utc) if end_date is not None else None

    events: list[AuditEvent] = []
    signal_store = build_signal_store_for_program_id(program_id, programs_root=resolved_programs_root)
    for signal in signal_store.read(program_id, start=start_ts, end=end_ts):
        events.append(
            AuditEvent(
                timestamp=signal.timestamp,
                category="signal",
                edition_id=None,
                reference=signal.id,
                source=signal.source,
                summary=signal.text,
            )
        )

    for review_record in read_signal_review_log_for_program_id(program_id, programs_root=resolved_programs_root):
        if not _within_bounds(review_record.reviewed_at, start_ts, end_ts):
            continue
        events.append(
            AuditEvent(
                timestamp=review_record.reviewed_at,
                category="review",
                edition_id=None,
                reference=review_record.signal_id,
                source=review_record.reviewed_by,
                summary=f"{review_record.decision} review{_review_note_suffix(review_record.note)}",
            )
        )

    for usage_record in signal_store.read_usage_markers(program_id):
        if not _within_bounds(usage_record.used_at, start_ts, end_ts):
            continue
        events.append(
            AuditEvent(
                timestamp=usage_record.used_at,
                category="usage",
                edition_id=usage_record.edition_id,
                reference=usage_record.signal_id,
                source=usage_record.manifest_id,
                summary=f"Used in issue {usage_record.issue_number:03d}",
            )
        )

    patterns = tuple(
        pattern
        for pattern in read_edit_patterns(program_id, programs_root=resolved_programs_root)
        if _within_bounds(pattern.recorded_at, start_ts, end_ts)
    )
    traces = _load_prompt_learning_traces(patterns, programs_root=resolved_programs_root)
    exact_traces_by_key, fallback_traces_by_key = _index_prompt_learning_traces(traces)
    for pattern in patterns:
        matched_trace = _match_prompt_learning_trace_to_pattern(
            pattern,
            exact_traces_by_key=exact_traces_by_key,
            fallback_traces_by_key=fallback_traces_by_key,
        )
        events.append(
            AuditEvent(
                timestamp=pattern.recorded_at,
                category="edit_pattern",
                edition_id=pattern.edition_id,
                reference=pattern.section_id,
                source=pattern.prompt_version or pattern.task_type or "edit_pattern",
                summary=_build_edit_pattern_summary(pattern),
                trace_run_id=pattern.trace_run_id,
                model=matched_trace.model if matched_trace is not None else None,
                deployment=matched_trace.deployment if matched_trace is not None else None,
                prompt_version=pattern.prompt_version or (matched_trace.prompt_version if matched_trace is not None else None),
                task_type=pattern.task_type,
                ai_confidence=pattern.ai_confidence.value if pattern.ai_confidence is not None else None,
                author_override_magnitude=pattern.author_override_magnitude,
            )
        )

    archive_root = resolved_programs_root / program_id / "archive"
    if archive_root.exists():
        for edition_dir in sorted(path for path in archive_root.iterdir() if path.is_dir()):
            edition_name = edition_dir.name
            archive_index = read_archive_index(edition_name, archive_root=archive_root)
            for entry in archive_index.issues:
                if not _within_bounds(entry.generated_at, start_ts, end_ts):
                    continue
                manifest_summary = _build_manifest_summary(entry.manifest_path)
                events.append(
                    AuditEvent(
                        timestamp=entry.generated_at,
                        category="archive",
                        edition_id=edition_name,
                        reference=f"issue_{entry.issue_number:03d}",
                        source=entry.kind,
                        summary=manifest_summary or f"Archived issue {entry.issue_number:03d}",
                    )
                )

    for autonomy_record in load_autonomy_audit_records(program_id, programs_root=resolved_programs_root):
        if not _within_bounds(autonomy_record.applied_at, start_ts, end_ts):
            continue
        events.append(
            AuditEvent(
                timestamp=autonomy_record.applied_at,
                category="autonomy",
                edition_id=None,
                reference=autonomy_record.action_id,
                source=autonomy_record.action_type or autonomy_record.level,
                summary=_build_autonomy_audit_summary(autonomy_record),
            )
        )

    events.sort(key=lambda event: (event.timestamp, event.category, event.edition_id or "", event.reference))
    return tuple(events)


def _build_autonomy_audit_summary(record: AutonomyAuditRecord) -> str:
    status = "approved" if record.accepted else "declined"
    action_label = record.action_type or record.level
    summary = f"{status} {action_label}"
    if record.subject_alias:
        summary += f" for {record.subject_alias}"
    incident_refs = _incident_evidence_refs(record.evidence_refs)
    if incident_refs:
        summary += f" | incident-linked via {', '.join(incident_refs)}"
    elif record.evidence_refs:
        summary += f" | evidence {', '.join(record.evidence_refs[:3])}"
    return summary


def _incident_evidence_refs(evidence_refs: tuple[str, ...]) -> tuple[str, ...]:
    incident_refs: list[str] = []
    for evidence_ref in evidence_refs:
        match = re.fullmatch(r"ICM:(?P<incident_id>\d+)", evidence_ref.strip(), flags=re.IGNORECASE)
        if match is None:
            continue
        incident_ref = f"ICM:{match.group('incident_id')}"
        if incident_ref not in incident_refs:
            incident_refs.append(incident_ref)
    return tuple(incident_refs)


def build_prompt_learning_report(
    program_id: str,
    *,
    window_issues: int = 10,
    programs_root: Path | None = None,
) -> AuditPromptLearningReport:
    resolved_programs_root = programs_root or PROGRAMS_ROOT
    recent_patterns = _patterns_within_issue_window(
        read_edit_patterns(program_id, programs_root=resolved_programs_root),
        window_issues=window_issues,
    )
    calibration = summarize_recent_calibration(
        program_id,
        window_issues=window_issues,
        programs_root=resolved_programs_root,
    )
    confidence_bands = summarize_recent_confidence_bands(
        program_id,
        window_issues=window_issues,
        programs_root=resolved_programs_root,
    )
    prompt_versions = summarize_recent_prompt_versions(
        program_id,
        window_issues=window_issues,
        programs_root=resolved_programs_root,
    )
    prompt_version_confidence_bands = summarize_recent_prompt_version_confidence_bands(
        program_id,
        window_issues=window_issues,
        programs_root=resolved_programs_root,
    )
    traces = _load_prompt_learning_traces(
        recent_patterns,
        programs_root=resolved_programs_root,
    )
    return AuditPromptLearningReport(
        window_issues=window_issues,
        calibration=calibration,
        confidence_bands=confidence_bands,
        prompt_versions=prompt_versions,
        prompt_version_confidence_bands=prompt_version_confidence_bands,
        leaderboard=_build_prompt_learning_leaderboard(prompt_versions),
        prompt_version_model_leaderboard=_build_prompt_version_model_leaderboard(
            recent_patterns,
            traces,
        ),
        model_leaderboard=_build_model_leaderboard(recent_patterns, traces),
        model_deployment_leaderboard=_build_model_deployment_leaderboard(recent_patterns, traces),
        prompt_version_model_deployment_leaderboard=_build_prompt_version_model_deployment_leaderboard(
            recent_patterns,
            traces,
        ),
        confidence_model_deployment_leaderboard=_build_confidence_model_deployment_leaderboard(
            recent_patterns,
            traces,
        ),
    )


def _build_prompt_learning_leaderboard(
    prompt_versions: tuple[PromptVersionSummary, ...],
) -> tuple[AuditPromptLeaderboardRow, ...]:
    rows: list[AuditPromptLeaderboardRow] = []
    current_task_type: str | None = None
    current_rank = 0
    for summary in prompt_versions:
        if summary.task_type != current_task_type:
            current_task_type = summary.task_type
            current_rank = 1
        else:
            current_rank += 1
        rows.append(
            AuditPromptLeaderboardRow(
                task_type=summary.task_type,
                rank=current_rank,
                prompt_version=summary.prompt_version,
                sample_count=summary.sample_count,
                average_override_magnitude=summary.average_override_magnitude,
                calibration_score=summary.calibration_score,
            )
        )
    return tuple(rows)


def _build_prompt_version_model_leaderboard(
    patterns: tuple[EditPattern, ...],
    traces: tuple[_AuditPromptLearningTraceRecord, ...],
) -> tuple[AuditPromptVersionModelLeaderboardRow, ...]:
    grouped: dict[tuple[str, str, str], list[tuple[EditPattern, _AuditPromptLearningTraceRecord]]] = {}
    for pattern, trace in _join_prompt_learning_patterns_to_traces(patterns, traces):
        if pattern.task_type is None:
            continue
        prompt_version = pattern.prompt_version or trace.prompt_version
        if prompt_version is None:
            continue
        grouped.setdefault((pattern.task_type, prompt_version, trace.model), []).append((pattern, trace))

    summaries: list[tuple[str, str, str, int, int, float, float, float | None, float | None]] = []
    for (task_type, prompt_version, model), pairs in grouped.items():
        average_override = round(
            sum(pattern.author_override_magnitude for pattern, _ in pairs if pattern.author_override_magnitude is not None)
            / len(pairs),
            4,
        )
        latencies = [trace.latency_ms for _, trace in pairs if trace.latency_ms is not None]
        costs = [trace.cost_usd for _, trace in pairs if trace.cost_usd is not None]
        summaries.append(
            (
                task_type,
                prompt_version,
                model,
                len({trace.deployment for _, trace in pairs}),
                len(pairs),
                average_override,
                round(max(0.0, 1.0 - average_override), 4),
                round(sum(latencies) / len(latencies), 1) if latencies else None,
                round(sum(costs) / len(costs), 6) if costs else None,
            )
        )

    summaries.sort(
        key=lambda summary: (
            summary[0],
            -summary[6],
            -summary[4],
            summary[8] if summary[8] is not None else float("inf"),
            summary[7] if summary[7] is not None else float("inf"),
            summary[1],
            summary[2],
        )
    )

    rows: list[AuditPromptVersionModelLeaderboardRow] = []
    current_task_type: str | None = None
    current_rank = 0
    for (
        task_type,
        prompt_version,
        model,
        deployment_count,
        sample_count,
        average_override,
        calibration_score,
        average_latency_ms,
        average_cost_usd,
    ) in summaries:
        if task_type != current_task_type:
            current_task_type = task_type
            current_rank = 1
        else:
            current_rank += 1
        rows.append(
            AuditPromptVersionModelLeaderboardRow(
                task_type=task_type,
                rank=current_rank,
                prompt_version=prompt_version,
                model=model,
                deployment_count=deployment_count,
                sample_count=sample_count,
                average_override_magnitude=average_override,
                calibration_score=calibration_score,
                average_latency_ms=average_latency_ms,
                average_cost_usd=average_cost_usd,
            )
        )
    return tuple(rows)


def _build_model_leaderboard(
    patterns: tuple[EditPattern, ...],
    traces: tuple[_AuditPromptLearningTraceRecord, ...],
) -> tuple[AuditModelLeaderboardRow, ...]:
    grouped: dict[tuple[str, str], list[tuple[EditPattern, _AuditPromptLearningTraceRecord]]] = {}
    for pattern, trace in _join_prompt_learning_patterns_to_traces(patterns, traces):
        if pattern.task_type is None:
            continue
        grouped.setdefault((pattern.task_type, trace.model), []).append((pattern, trace))

    summaries: list[tuple[str, str, int, int, float, float, float | None, float | None]] = []
    for (task_type, model), pairs in grouped.items():
        average_override = round(
            sum(pattern.author_override_magnitude for pattern, _ in pairs if pattern.author_override_magnitude is not None)
            / len(pairs),
            4,
        )
        latencies = [trace.latency_ms for _, trace in pairs if trace.latency_ms is not None]
        costs = [trace.cost_usd for _, trace in pairs if trace.cost_usd is not None]
        summaries.append(
            (
                task_type,
                model,
                len({trace.deployment for _, trace in pairs}),
                len(pairs),
                average_override,
                round(max(0.0, 1.0 - average_override), 4),
                round(sum(latencies) / len(latencies), 1) if latencies else None,
                round(sum(costs) / len(costs), 6) if costs else None,
            )
        )

    summaries.sort(
        key=lambda summary: (
            summary[0],
            -summary[5],
            -summary[3],
            summary[7] if summary[7] is not None else float("inf"),
            summary[6] if summary[6] is not None else float("inf"),
            summary[1],
        )
    )

    rows: list[AuditModelLeaderboardRow] = []
    current_task_type: str | None = None
    current_rank = 0
    for (
        task_type,
        model,
        deployment_count,
        sample_count,
        average_override,
        calibration_score,
        average_latency_ms,
        average_cost_usd,
    ) in summaries:
        if task_type != current_task_type:
            current_task_type = task_type
            current_rank = 1
        else:
            current_rank += 1
        rows.append(
            AuditModelLeaderboardRow(
                task_type=task_type,
                rank=current_rank,
                model=model,
                deployment_count=deployment_count,
                sample_count=sample_count,
                average_override_magnitude=average_override,
                calibration_score=calibration_score,
                average_latency_ms=average_latency_ms,
                average_cost_usd=average_cost_usd,
            )
        )
    return tuple(rows)


def _build_model_deployment_leaderboard(
    patterns: tuple[EditPattern, ...],
    traces: tuple[_AuditPromptLearningTraceRecord, ...],
) -> tuple[AuditModelDeploymentLeaderboardRow, ...]:
    grouped: dict[tuple[str, str, str], list[tuple[EditPattern, _AuditPromptLearningTraceRecord]]] = {}
    for pattern, trace in _join_prompt_learning_patterns_to_traces(patterns, traces):
        if pattern.task_type is None:
            continue
        grouped.setdefault((pattern.task_type, trace.model, trace.deployment), []).append((pattern, trace))

    summaries: list[tuple[str, str, str, int, float, float, float | None, float | None]] = []
    for (task_type, model, deployment), pairs in grouped.items():
        average_override = round(
            sum(pattern.author_override_magnitude for pattern, _ in pairs if pattern.author_override_magnitude is not None)
            / len(pairs),
            4,
        )
        latencies = [trace.latency_ms for _, trace in pairs if trace.latency_ms is not None]
        costs = [trace.cost_usd for _, trace in pairs if trace.cost_usd is not None]
        summaries.append(
            (
                task_type,
                model,
                deployment,
                len(pairs),
                average_override,
                round(max(0.0, 1.0 - average_override), 4),
                round(sum(latencies) / len(latencies), 1) if latencies else None,
                round(sum(costs) / len(costs), 6) if costs else None,
            )
        )

    summaries.sort(
        key=lambda summary: (
            summary[0],
            -summary[5],
            -summary[3],
            summary[7] if summary[7] is not None else float("inf"),
            summary[6] if summary[6] is not None else float("inf"),
            summary[2],
            summary[1],
        )
    )

    rows: list[AuditModelDeploymentLeaderboardRow] = []
    current_task_type: str | None = None
    current_rank = 0
    for task_type, model, deployment, sample_count, average_override, calibration_score, average_latency_ms, average_cost_usd in summaries:
        if task_type != current_task_type:
            current_task_type = task_type
            current_rank = 1
        else:
            current_rank += 1
        rows.append(
            AuditModelDeploymentLeaderboardRow(
                task_type=task_type,
                rank=current_rank,
                model=model,
                deployment=deployment,
                sample_count=sample_count,
                average_override_magnitude=average_override,
                calibration_score=calibration_score,
                average_latency_ms=average_latency_ms,
                average_cost_usd=average_cost_usd,
            )
        )
    return tuple(rows)


def _build_prompt_version_model_deployment_leaderboard(
    patterns: tuple[EditPattern, ...],
    traces: tuple[_AuditPromptLearningTraceRecord, ...],
) -> tuple[AuditPromptVersionModelDeploymentLeaderboardRow, ...]:
    grouped: dict[tuple[str, str, str, str], list[tuple[EditPattern, _AuditPromptLearningTraceRecord]]] = {}
    for pattern, trace in _join_prompt_learning_patterns_to_traces(patterns, traces):
        if pattern.task_type is None:
            continue
        prompt_version = pattern.prompt_version or trace.prompt_version
        if prompt_version is None:
            continue
        grouped.setdefault((pattern.task_type, prompt_version, trace.model, trace.deployment), []).append((pattern, trace))

    summaries: list[tuple[str, str, str, str, int, float, float, float | None, float | None]] = []
    for (task_type, prompt_version, model, deployment), pairs in grouped.items():
        average_override = round(
            sum(pattern.author_override_magnitude for pattern, _ in pairs if pattern.author_override_magnitude is not None)
            / len(pairs),
            4,
        )
        latencies = [trace.latency_ms for _, trace in pairs if trace.latency_ms is not None]
        costs = [trace.cost_usd for _, trace in pairs if trace.cost_usd is not None]
        summaries.append(
            (
                task_type,
                prompt_version,
                model,
                deployment,
                len(pairs),
                average_override,
                round(max(0.0, 1.0 - average_override), 4),
                round(sum(latencies) / len(latencies), 1) if latencies else None,
                round(sum(costs) / len(costs), 6) if costs else None,
            )
        )

    summaries.sort(
        key=lambda summary: (
            summary[0],
            -summary[6],
            -summary[4],
            summary[8] if summary[8] is not None else float("inf"),
            summary[7] if summary[7] is not None else float("inf"),
            summary[1],
            summary[3],
            summary[2],
        )
    )

    rows: list[AuditPromptVersionModelDeploymentLeaderboardRow] = []
    current_task_type: str | None = None
    current_rank = 0
    for (
        task_type,
        prompt_version,
        model,
        deployment,
        sample_count,
        average_override,
        calibration_score,
        average_latency_ms,
        average_cost_usd,
    ) in summaries:
        if task_type != current_task_type:
            current_task_type = task_type
            current_rank = 1
        else:
            current_rank += 1
        rows.append(
            AuditPromptVersionModelDeploymentLeaderboardRow(
                task_type=task_type,
                rank=current_rank,
                prompt_version=prompt_version,
                model=model,
                deployment=deployment,
                sample_count=sample_count,
                average_override_magnitude=average_override,
                calibration_score=calibration_score,
                average_latency_ms=average_latency_ms,
                average_cost_usd=average_cost_usd,
            )
        )
    return tuple(rows)


def _build_confidence_model_deployment_leaderboard(
    patterns: tuple[EditPattern, ...],
    traces: tuple[_AuditPromptLearningTraceRecord, ...],
) -> tuple[AuditConfidenceModelDeploymentLeaderboardRow, ...]:
    grouped: dict[tuple[str, str, str, str], list[tuple[EditPattern, _AuditPromptLearningTraceRecord]]] = {}
    for pattern, trace in _join_prompt_learning_patterns_to_traces(patterns, traces):
        if pattern.ai_confidence in (None, Confidence.NONE):
            continue
        if pattern.task_type is None:
            continue
        assert pattern.ai_confidence is not None  # narrowed by guard above
        grouped.setdefault((pattern.task_type, pattern.ai_confidence.value, trace.model, trace.deployment), []).append(
            (pattern, trace)
        )

    summaries: list[tuple[str, str, str, str, int, float, float, float | None, float | None]] = []
    for (task_type, ai_confidence, model, deployment), pairs in grouped.items():
        average_override = round(
            sum(pattern.author_override_magnitude for pattern, _ in pairs if pattern.author_override_magnitude is not None)
            / len(pairs),
            4,
        )
        latencies = [trace.latency_ms for _, trace in pairs if trace.latency_ms is not None]
        costs = [trace.cost_usd for _, trace in pairs if trace.cost_usd is not None]
        summaries.append(
            (
                task_type,
                ai_confidence,
                model,
                deployment,
                len(pairs),
                average_override,
                round(max(0.0, 1.0 - average_override), 4),
                round(sum(latencies) / len(latencies), 1) if latencies else None,
                round(sum(costs) / len(costs), 6) if costs else None,
            )
        )

    summaries.sort(
        key=lambda summary: (
            summary[0],
            _confidence_sort_key(summary[1]),
            -summary[6],
            -summary[4],
            summary[8] if summary[8] is not None else float("inf"),
            summary[7] if summary[7] is not None else float("inf"),
            summary[3],
            summary[2],
        )
    )

    rows: list[AuditConfidenceModelDeploymentLeaderboardRow] = []
    current_group: tuple[str, str] | None = None
    current_rank = 0
    for (
        task_type,
        ai_confidence,
        model,
        deployment,
        sample_count,
        average_override,
        calibration_score,
        average_latency_ms,
        average_cost_usd,
    ) in summaries:
        group = (task_type, ai_confidence)
        if group != current_group:
            current_group = group
            current_rank = 1
        else:
            current_rank += 1
        rows.append(
            AuditConfidenceModelDeploymentLeaderboardRow(
                task_type=task_type,
                rank=current_rank,
                ai_confidence=ai_confidence,
                model=model,
                deployment=deployment,
                sample_count=sample_count,
                average_override_magnitude=average_override,
                calibration_score=calibration_score,
                average_latency_ms=average_latency_ms,
                average_cost_usd=average_cost_usd,
            )
        )
    return tuple(rows)


def _confidence_sort_key(ai_confidence: str) -> tuple[int, str]:
    order = {
        Confidence.HIGH.value: 0,
        Confidence.MEDIUM.value: 1,
        Confidence.LOW.value: 2,
        Confidence.NONE.value: 3,
    }
    return (order.get(ai_confidence, 99), ai_confidence)


def _join_prompt_learning_patterns_to_traces(
    patterns: tuple[EditPattern, ...],
    traces: tuple[_AuditPromptLearningTraceRecord, ...],
) -> tuple[tuple[EditPattern, _AuditPromptLearningTraceRecord], ...]:
    exact_traces_by_key, fallback_traces_by_key = _index_prompt_learning_traces(traces)
    pairs: list[tuple[EditPattern, _AuditPromptLearningTraceRecord]] = []
    for pattern in patterns:
        if pattern.task_type is None or pattern.author_override_magnitude is None:
            continue
        matched_trace = _match_prompt_learning_trace_to_pattern(
            pattern,
            exact_traces_by_key=exact_traces_by_key,
            fallback_traces_by_key=fallback_traces_by_key,
        )
        if matched_trace is None:
            continue
        pairs.append((pattern, matched_trace))
    return tuple(pairs)


def render_audit_timeline(
    program_id: str,
    events: tuple[AuditEvent, ...],
    *,
    prompt_learning: AuditPromptLearningReport | None = None,
) -> str:
    lines = [f"Audit Timeline: {program_id}"]
    if not events:
        lines.append("No audit events found.")
    else:
        for event in events:
            edition_fragment = f" | {event.edition_id}" if event.edition_id is not None else ""
            trace_fragment = f" | trace_run_id={event.trace_run_id}" if event.trace_run_id is not None else ""
            model_fragment = f" | model={event.model}" if event.model is not None else ""
            deployment_fragment = f" | deployment={event.deployment}" if event.deployment is not None else ""
            lines.append(
                f"{event.timestamp.isoformat()} | {event.category}{edition_fragment} | {event.reference} | {event.source}{trace_fragment}{model_fragment}{deployment_fragment} | {event.summary}"
            )

    if prompt_learning is not None:
        lines.append("")
        lines.append(f"Prompt Learning Summary: {program_id} (last {prompt_learning.window_issues} issues)")
        if not any(
            (
                prompt_learning.calibration,
                prompt_learning.confidence_bands,
                prompt_learning.prompt_versions,
                prompt_learning.prompt_version_confidence_bands,
                prompt_learning.leaderboard,
                prompt_learning.prompt_version_model_leaderboard,
                prompt_learning.model_leaderboard,
                prompt_learning.model_deployment_leaderboard,
                prompt_learning.prompt_version_model_deployment_leaderboard,
                prompt_learning.confidence_model_deployment_leaderboard,
            )
        ):
            lines.append("No prompt-learning summary available.")
        else:
            if prompt_learning.calibration:
                lines.append("Calibration")
                for cal_summary in prompt_learning.calibration:
                    lines.append(
                        f"- {cal_summary.task_type} | samples={cal_summary.sample_count} | avg_override={cal_summary.average_override_magnitude:.4f} | score={cal_summary.calibration_score:.4f}"
                    )
            if prompt_learning.confidence_bands:
                lines.append("Confidence Bands")
                for band_summary in prompt_learning.confidence_bands:
                    lines.append(
                        f"- {band_summary.task_type} | {band_summary.ai_confidence} | samples={band_summary.sample_count} | avg_override={band_summary.average_override_magnitude:.4f} | score={band_summary.calibration_score:.4f}"
                    )
            if prompt_learning.prompt_versions:
                lines.append("Prompt Versions")
                for pv_summary in prompt_learning.prompt_versions:
                    lines.append(
                        f"- {pv_summary.task_type} | {pv_summary.prompt_version} | samples={pv_summary.sample_count} | avg_override={pv_summary.average_override_magnitude:.4f} | score={pv_summary.calibration_score:.4f}"
                    )
            if prompt_learning.prompt_version_confidence_bands:
                lines.append("Prompt Version Confidence Bands")
                for pvb_summary in prompt_learning.prompt_version_confidence_bands:
                    lines.append(
                        f"- {pvb_summary.task_type} | {pvb_summary.prompt_version} | {pvb_summary.ai_confidence} | samples={pvb_summary.sample_count} | avg_override={pvb_summary.average_override_magnitude:.4f} | score={pvb_summary.calibration_score:.4f}"
                    )
            if prompt_learning.leaderboard:
                lines.append("Leaderboard")
                for lb_row in prompt_learning.leaderboard:
                    lines.append(
                        f"{lb_row.rank}. {lb_row.task_type} | {lb_row.prompt_version} | samples={lb_row.sample_count} | avg_override={lb_row.average_override_magnitude:.4f} | score={lb_row.calibration_score:.4f}"
                    )
            if prompt_learning.prompt_version_model_leaderboard:
                lines.append("Prompt Version Model Leaderboard")
                for pvm_row in prompt_learning.prompt_version_model_leaderboard:
                    latency_fragment = (
                        f" | avg_latency_ms={pvm_row.average_latency_ms:.1f}"
                        if pvm_row.average_latency_ms is not None
                        else ""
                    )
                    cost_fragment = (
                        f" | avg_cost_usd={pvm_row.average_cost_usd:.6f}"
                        if pvm_row.average_cost_usd is not None
                        else ""
                    )
                    lines.append(
                        f"{pvm_row.rank}. {pvm_row.task_type} | prompt={pvm_row.prompt_version} | model={pvm_row.model} | deployments={pvm_row.deployment_count} | samples={pvm_row.sample_count} | avg_override={pvm_row.average_override_magnitude:.4f} | score={pvm_row.calibration_score:.4f}{latency_fragment}{cost_fragment}"
                    )
            if prompt_learning.model_leaderboard:
                lines.append("Model Leaderboard")
                for ml_row in prompt_learning.model_leaderboard:
                    latency_fragment = (
                        f" | avg_latency_ms={ml_row.average_latency_ms:.1f}"
                        if ml_row.average_latency_ms is not None
                        else ""
                    )
                    cost_fragment = (
                        f" | avg_cost_usd={ml_row.average_cost_usd:.6f}"
                        if ml_row.average_cost_usd is not None
                        else ""
                    )
                    lines.append(
                        f"{ml_row.rank}. {ml_row.task_type} | model={ml_row.model} | deployments={ml_row.deployment_count} | samples={ml_row.sample_count} | avg_override={ml_row.average_override_magnitude:.4f} | score={ml_row.calibration_score:.4f}{latency_fragment}{cost_fragment}"
                    )
            if prompt_learning.model_deployment_leaderboard:
                lines.append("Model/Deployment Leaderboard")
                for mdl_row in prompt_learning.model_deployment_leaderboard:
                    latency_fragment = (
                        f" | avg_latency_ms={mdl_row.average_latency_ms:.1f}"
                        if mdl_row.average_latency_ms is not None
                        else ""
                    )
                    cost_fragment = (
                        f" | avg_cost_usd={mdl_row.average_cost_usd:.6f}"
                        if mdl_row.average_cost_usd is not None
                        else ""
                    )
                    lines.append(
                        f"{mdl_row.rank}. {mdl_row.task_type} | model={mdl_row.model} | deployment={mdl_row.deployment} | samples={mdl_row.sample_count} | avg_override={mdl_row.average_override_magnitude:.4f} | score={mdl_row.calibration_score:.4f}{latency_fragment}{cost_fragment}"
                    )
            if prompt_learning.prompt_version_model_deployment_leaderboard:
                lines.append("Prompt Version Model/Deployment Leaderboard")
                for pvmd_row in prompt_learning.prompt_version_model_deployment_leaderboard:
                    latency_fragment = (
                        f" | avg_latency_ms={pvmd_row.average_latency_ms:.1f}"
                        if pvmd_row.average_latency_ms is not None
                        else ""
                    )
                    cost_fragment = (
                        f" | avg_cost_usd={pvmd_row.average_cost_usd:.6f}"
                        if pvmd_row.average_cost_usd is not None
                        else ""
                    )
                    lines.append(
                        f"{pvmd_row.rank}. {pvmd_row.task_type} | prompt={pvmd_row.prompt_version} | model={pvmd_row.model} | deployment={pvmd_row.deployment} | samples={pvmd_row.sample_count} | avg_override={pvmd_row.average_override_magnitude:.4f} | score={pvmd_row.calibration_score:.4f}{latency_fragment}{cost_fragment}"
                    )
            if prompt_learning.confidence_model_deployment_leaderboard:
                lines.append("Confidence Model/Deployment Leaderboard")
                for cmd_row in prompt_learning.confidence_model_deployment_leaderboard:
                    latency_fragment = (
                        f" | avg_latency_ms={cmd_row.average_latency_ms:.1f}"
                        if cmd_row.average_latency_ms is not None
                        else ""
                    )
                    cost_fragment = (
                        f" | avg_cost_usd={cmd_row.average_cost_usd:.6f}"
                        if cmd_row.average_cost_usd is not None
                        else ""
                    )
                    lines.append(
                        f"{cmd_row.rank}. {cmd_row.task_type} | {cmd_row.ai_confidence} | model={cmd_row.model} | deployment={cmd_row.deployment} | samples={cmd_row.sample_count} | avg_override={cmd_row.average_override_magnitude:.4f} | score={cmd_row.calibration_score:.4f}{latency_fragment}{cost_fragment}"
                    )
    return "\n".join(lines)


def render_audit_csv(
    events: tuple[AuditEvent, ...],
    *,
    prompt_learning: AuditPromptLearningReport | None = None,
) -> str:
    buffer = StringIO()
    writer = csv.writer(buffer)
    if prompt_learning is None:
        writer.writerow(
            (
                "timestamp",
                "category",
                "edition_id",
                "reference",
                "source",
                "trace_run_id",
                "model",
                "deployment",
                "prompt_version",
                "task_type",
                "ai_confidence",
                "author_override_magnitude",
                "summary",
            )
        )
        for event in events:
            writer.writerow(
                (
                    event.timestamp.isoformat(),
                    event.category,
                    event.edition_id or "",
                    event.reference,
                    event.source,
                    event.trace_run_id or "",
                    event.model or "",
                    event.deployment or "",
                    event.prompt_version or "",
                    event.task_type or "",
                    event.ai_confidence or "",
                    f"{event.author_override_magnitude:.4f}" if event.author_override_magnitude is not None else "",
                    event.summary,
                )
            )
        return buffer.getvalue()

    writer.writerow(
        (
            "timestamp",
            "category",
            "edition_id",
            "reference",
            "source",
            "trace_run_id",
            "model",
            "deployment",
            "prompt_version",
            "task_type",
            "ai_confidence",
            "author_override_magnitude",
            "summary",
        )
    )
    for event in events:
        writer.writerow(
            (
                event.timestamp.isoformat(),
                event.category,
                event.edition_id or "",
                event.reference,
                event.source,
                event.trace_run_id or "",
                event.model or "",
                event.deployment or "",
                event.prompt_version or "",
                event.task_type or "",
                event.ai_confidence or "",
                f"{event.author_override_magnitude:.4f}" if event.author_override_magnitude is not None else "",
                event.summary,
            )
        )
    writer.writerow(())
    writer.writerow(
        (
            "record_type",
            "task_type",
            "rank",
            "prompt_version",
            "ai_confidence",
            "window_issues",
            "sample_count",
            "average_override_magnitude",
            "calibration_score",
        )
    )
    for cal_summary in prompt_learning.calibration:
        writer.writerow(
            (
                "calibration_summary",
                cal_summary.task_type,
                "",
                "",
                "",
                prompt_learning.window_issues,
                cal_summary.sample_count,
                f"{cal_summary.average_override_magnitude:.4f}",
                f"{cal_summary.calibration_score:.4f}",
            )
        )
    for band_summary in prompt_learning.confidence_bands:
        writer.writerow(
            (
                "confidence_band_summary",
                band_summary.task_type,
                "",
                "",
                band_summary.ai_confidence,
                prompt_learning.window_issues,
                band_summary.sample_count,
                f"{band_summary.average_override_magnitude:.4f}",
                f"{band_summary.calibration_score:.4f}",
            )
        )
    for pv_summary in prompt_learning.prompt_versions:
        writer.writerow(
            (
                "prompt_version_summary",
                pv_summary.task_type,
                "",
                pv_summary.prompt_version,
                "",
                prompt_learning.window_issues,
                pv_summary.sample_count,
                f"{pv_summary.average_override_magnitude:.4f}",
                f"{pv_summary.calibration_score:.4f}",
            )
        )
    for pvb_summary in prompt_learning.prompt_version_confidence_bands:
        writer.writerow(
            (
                "prompt_version_confidence_summary",
                pvb_summary.task_type,
                "",
                pvb_summary.prompt_version,
                pvb_summary.ai_confidence,
                prompt_learning.window_issues,
                pvb_summary.sample_count,
                f"{pvb_summary.average_override_magnitude:.4f}",
                f"{pvb_summary.calibration_score:.4f}",
            )
        )
    for lb_row in prompt_learning.leaderboard:
        writer.writerow(
            (
                "prompt_version_leaderboard",
                lb_row.task_type,
                lb_row.rank,
                lb_row.prompt_version,
                "",
                prompt_learning.window_issues,
                lb_row.sample_count,
                f"{lb_row.average_override_magnitude:.4f}",
                f"{lb_row.calibration_score:.4f}",
            )
        )
    if prompt_learning.prompt_version_model_leaderboard:
        writer.writerow(())
        writer.writerow(
            (
                "record_type",
                "task_type",
                "rank",
                "prompt_version",
                "model",
                "deployment_count",
                "window_issues",
                "sample_count",
                "average_override_magnitude",
                "calibration_score",
                "average_latency_ms",
                "average_cost_usd",
            )
        )
        for pvm_row in prompt_learning.prompt_version_model_leaderboard:
            writer.writerow(
                (
                    "prompt_version_model_leaderboard",
                    pvm_row.task_type,
                    pvm_row.rank,
                    pvm_row.prompt_version,
                    pvm_row.model,
                    pvm_row.deployment_count,
                    prompt_learning.window_issues,
                    pvm_row.sample_count,
                    f"{pvm_row.average_override_magnitude:.4f}",
                    f"{pvm_row.calibration_score:.4f}",
                    f"{pvm_row.average_latency_ms:.1f}" if pvm_row.average_latency_ms is not None else "",
                    f"{pvm_row.average_cost_usd:.6f}" if pvm_row.average_cost_usd is not None else "",
                )
            )
    if prompt_learning.model_leaderboard:
        writer.writerow(())
        writer.writerow(
            (
                "record_type",
                "task_type",
                "rank",
                "model",
                "deployment_count",
                "window_issues",
                "sample_count",
                "average_override_magnitude",
                "calibration_score",
                "average_latency_ms",
                "average_cost_usd",
            )
        )
        for ml_row in prompt_learning.model_leaderboard:
            writer.writerow(
                (
                    "model_leaderboard",
                    ml_row.task_type,
                    ml_row.rank,
                    ml_row.model,
                    ml_row.deployment_count,
                    prompt_learning.window_issues,
                    ml_row.sample_count,
                    f"{ml_row.average_override_magnitude:.4f}",
                    f"{ml_row.calibration_score:.4f}",
                    f"{ml_row.average_latency_ms:.1f}" if ml_row.average_latency_ms is not None else "",
                    f"{ml_row.average_cost_usd:.6f}" if ml_row.average_cost_usd is not None else "",
                )
            )
    if prompt_learning.model_deployment_leaderboard:
        writer.writerow(())
        writer.writerow(
            (
                "record_type",
                "task_type",
                "rank",
                "model",
                "deployment",
                "window_issues",
                "sample_count",
                "average_override_magnitude",
                "calibration_score",
                "average_latency_ms",
                "average_cost_usd",
            )
        )
        for mdl_row in prompt_learning.model_deployment_leaderboard:
            writer.writerow(
                (
                    "model_deployment_leaderboard",
                    mdl_row.task_type,
                    mdl_row.rank,
                    mdl_row.model,
                    mdl_row.deployment,
                    prompt_learning.window_issues,
                    mdl_row.sample_count,
                    f"{mdl_row.average_override_magnitude:.4f}",
                    f"{mdl_row.calibration_score:.4f}",
                    f"{mdl_row.average_latency_ms:.1f}" if mdl_row.average_latency_ms is not None else "",
                    f"{mdl_row.average_cost_usd:.6f}" if mdl_row.average_cost_usd is not None else "",
                )
            )
    if prompt_learning.prompt_version_model_deployment_leaderboard:
        writer.writerow(())
        writer.writerow(
            (
                "record_type",
                "task_type",
                "rank",
                "prompt_version",
                "model",
                "deployment",
                "window_issues",
                "sample_count",
                "average_override_magnitude",
                "calibration_score",
                "average_latency_ms",
                "average_cost_usd",
            )
        )
        for pvmd_row in prompt_learning.prompt_version_model_deployment_leaderboard:
            writer.writerow(
                (
                    "prompt_version_model_deployment_leaderboard",
                    pvmd_row.task_type,
                    pvmd_row.rank,
                    pvmd_row.prompt_version,
                    pvmd_row.model,
                    pvmd_row.deployment,
                    prompt_learning.window_issues,
                    pvmd_row.sample_count,
                    f"{pvmd_row.average_override_magnitude:.4f}",
                    f"{pvmd_row.calibration_score:.4f}",
                    f"{pvmd_row.average_latency_ms:.1f}" if pvmd_row.average_latency_ms is not None else "",
                    f"{pvmd_row.average_cost_usd:.6f}" if pvmd_row.average_cost_usd is not None else "",
                )
            )
    if prompt_learning.confidence_model_deployment_leaderboard:
        writer.writerow(())
        writer.writerow(
            (
                "record_type",
                "task_type",
                "rank",
                "ai_confidence",
                "model",
                "deployment",
                "window_issues",
                "sample_count",
                "average_override_magnitude",
                "calibration_score",
                "average_latency_ms",
                "average_cost_usd",
            )
        )
        for cmd_row in prompt_learning.confidence_model_deployment_leaderboard:
            writer.writerow(
                (
                    "confidence_model_deployment_leaderboard",
                    cmd_row.task_type,
                    cmd_row.rank,
                    cmd_row.ai_confidence,
                    cmd_row.model,
                    cmd_row.deployment,
                    prompt_learning.window_issues,
                    cmd_row.sample_count,
                    f"{cmd_row.average_override_magnitude:.4f}",
                    f"{cmd_row.calibration_score:.4f}",
                    f"{cmd_row.average_latency_ms:.1f}" if cmd_row.average_latency_ms is not None else "",
                    f"{cmd_row.average_cost_usd:.6f}" if cmd_row.average_cost_usd is not None else "",
                )
            )
    return buffer.getvalue()


def render_audit_archive_output(*, artifacts: AutonomyAuditArchiveArtifacts, format: str) -> str:
    payload: dict[str, Any] = {
        "program_id": artifacts.program_id,
        "before_date": artifacts.before_date.isoformat(),
        "archived_count": artifacts.archived_count,
        "remaining_count": artifacts.remaining_count,
        "archive_paths": [str(path) for path in artifacts.archive_paths],
    }
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(("row_type", "program_id", "before_date", "archived_count", "remaining_count", "path"))
        writer.writerow(("summary", payload["program_id"], payload["before_date"], payload["archived_count"], payload["remaining_count"], ""))
        for path in payload["archive_paths"]:
            writer.writerow(("path", payload["program_id"], payload["before_date"], "", "", path))
        return buffer.getvalue()
    raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")


def _load_audit_retention_days(program_id: str, *, programs_root: Path) -> int:
    program_path = programs_root / program_id / "program.yaml"
    if not program_path.exists():
        return 365
    try:
        document = yaml.safe_load(program_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise typer.BadParameter(f"Program '{program_id}' has invalid program.yaml: {error}") from error
    if not isinstance(document, dict):
        return 365
    audit_config = document.get("audit")
    if not isinstance(audit_config, dict):
        return 365
    retention_days = audit_config.get("retention_days")
    if isinstance(retention_days, int) and retention_days > 0:
        return retention_days
    return 365


def _find_autonomy_audit_record(
    action_id: str,
    *,
    program_id: str | None,
    programs_root: Path,
) -> AutonomyAuditRecord:
    candidate_records: list[AutonomyAuditRecord] = []
    candidate_program_ids = [program_id] if program_id is not None else _discover_program_ids(programs_root)
    for candidate_program_id in candidate_program_ids:
        if candidate_program_id is None:
            continue
        candidate_records.extend(
            record
            for record in load_autonomy_audit_records(candidate_program_id, programs_root=programs_root)
            if record.action_id == action_id
        )
    if not candidate_records:
        raise typer.BadParameter(f"Autonomy action '{action_id}' was not found.")
    if len(candidate_records) > 1:
        programs = ", ".join(sorted({record.program_id for record in candidate_records}))
        raise typer.BadParameter(
            f"Autonomy action '{action_id}' is ambiguous across programs ({programs}). Re-run with --program."
        )
    return candidate_records[0]


def _find_autonomy_audit_records_for_batch(
    batch_date: date,
    *,
    program_id: str,
    programs_root: Path,
) -> tuple[AutonomyAuditRecord, ...]:
    return tuple(
        sorted(
            (
                record
                for record in load_autonomy_audit_records(program_id, programs_root=programs_root)
                if record.accepted
                and _proposal_reference_from_audit_record(record) is not None
                and record.applied_at.astimezone(timezone.utc).date() == batch_date
            ),
            key=lambda record: (record.applied_at, record.action_id),
        )
    )


def _discover_program_ids(programs_root: Path) -> list[str]:
    if not programs_root.exists():
        return []
    return sorted(
        path.name
        for path in programs_root.iterdir()
        if path.is_dir() and (path / "program.yaml").exists()
    )


def _proposal_reference_from_audit_record(record: AutonomyAuditRecord) -> str | None:
    for evidence_ref in record.evidence_refs:
        if evidence_ref.startswith("ado_proposal:"):
            return evidence_ref.split(":", 1)[1].strip() or None
    return None


def _load_rollback_target(record: AutonomyAuditRecord, *, programs_root: Path = PROGRAMS_ROOT) -> tuple[Path, ADOUpdateProposal, tuple[ADOUpdateEntry, ...]]:
    proposal_ref = _proposal_reference_from_audit_record(record)
    if proposal_ref is None:
        raise typer.BadParameter(
            f"Action '{record.action_id}' is not backed by an ADO proposal manifest and cannot be rolled back."
        )
    manifest_path = _resolve_audit_proposal_manifest(proposal_ref, programs_root=programs_root)
    proposal, _ = read_proposal_manifest(manifest_path)
    rollbackable_entries = tuple(entry for entry in proposal.entries if entry.entry_status == "applied")
    return manifest_path, proposal, rollbackable_entries


def _append_rollback_audit_record(
    *,
    original_record: AutonomyAuditRecord,
    proposal_id: str,
    artifacts: ADORollbackArtifacts,
    author_alias: str,
    applied_at: datetime,
    programs_root: Path,
) -> None:
    append_autonomy_audit_record(
        AutonomyAuditRecord(
            program_id=original_record.program_id,
            action_id=str(uuid4()),
            level=original_record.level,
            author_alias=author_alias,
            subject_alias=None,
            action_type="rollback",
            evidence_refs=tuple(
                [
                    f"original_action:{original_record.action_id}",
                    f"ado_proposal:{proposal_id}",
                    *[f"WI:{result.work_item_id}" for result in artifacts.results],
                ]
            ),
            policy_rule=original_record.policy_rule,
            accepted=True,
            applied_at=applied_at,
            blast_radius=(
                f"Rolled back {artifacts.rolled_back_count} ADO update(s) from action {original_record.action_id}; "
                f"{artifacts.skipped_count} skipped, {artifacts.conflict_count} conflict, {artifacts.failed_count} failed."
            ),
            rollback_mechanism=(
                "Reapply the original ADO proposal or generate a new corrected proposal if this rollback must be undone."
            ),
            prior_acceptance_rate=(
                None
                if original_record.action_type is None
                else compute_prior_acceptance_rate(
                    original_record.program_id,
                    action_type=original_record.action_type,
                    programs_root=programs_root,
                )
            ),
        ),
        programs_root=programs_root,
    )


def _resolve_audit_proposal_manifest(proposal_reference: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    try:
        return find_proposal_manifest(proposal_reference, programs_root=programs_root)
    except (FileNotFoundError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error


def _build_ado_client_for_program(program: object) -> ADOClient:
    ado_config = getattr(program, "ado", None)
    program_id = getattr(program, "id", "unknown")
    if ado_config is None:
        raise typer.BadParameter(f"Program '{program_id}' is missing ado configuration.")
    return ADOClient(
        organization=ado_config.organization,
        project=ado_config.project,
        timeout=ado_config.api_timeout_seconds,
    )


def _default_actor(value: str | None) -> str:
    if value is not None and value.strip():
        return value.strip()
    try:
        return getpass.getuser() or "unknown"
    except Exception:
        return "unknown"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _rollback_exit_code(artifacts: ADORollbackArtifacts) -> int:
    return _rollback_exit_code_from_counts(conflict_count=artifacts.conflict_count, failed_count=artifacts.failed_count)


def _rollback_exit_code_from_counts(*, conflict_count: int, failed_count: int) -> int:
    if conflict_count or failed_count:
        return 2
    return 0


def _patterns_within_issue_window(
    patterns: tuple[EditPattern, ...],
    *,
    window_issues: int,
) -> tuple[EditPattern, ...]:
    if window_issues <= 0:
        return ()
    ordered = sorted(patterns, key=lambda pattern: (pattern.issue_number, pattern.recorded_at), reverse=True)
    recent_issue_numbers: list[int] = []
    for pattern in ordered:
        if pattern.issue_number not in recent_issue_numbers:
            recent_issue_numbers.append(pattern.issue_number)
        if len(recent_issue_numbers) >= window_issues:
            break
    allowed_issue_numbers = set(recent_issue_numbers)
    return tuple(pattern for pattern in ordered if pattern.issue_number in allowed_issue_numbers)


def _load_prompt_learning_traces(
    patterns: tuple[EditPattern, ...],
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[_AuditPromptLearningTraceRecord, ...]:
    if not patterns:
        return ()

    exact_keys = {
        _prompt_learning_trace_join_key(
            run_id=pattern.trace_run_id,
            section_id=pattern.section_id,
            task_type=pattern.task_type,
        )
        for pattern in patterns
        if pattern.task_type is not None and pattern.trace_run_id is not None
    }
    fallback_keys = {
        _prompt_learning_join_key(
            edition_id=pattern.edition_id,
            issue_number=pattern.issue_number,
            section_id=pattern.section_id,
            task_type=pattern.task_type,
        )
        for pattern in patterns
        if pattern.task_type is not None and pattern.trace_run_id is None
    }
    if not exact_keys and not fallback_keys:
        return ()

    latest_by_exact_key: dict[tuple[str, str, str], _AuditPromptLearningTraceRecord] = {}
    latest_by_fallback_key: dict[tuple[str, int, str, str], _AuditPromptLearningTraceRecord] = {}
    for edition_id in sorted({pattern.edition_id for pattern in patterns}):
        trace_path = get_program_output_dir(edition_id, programs_root=programs_root) / "ai" / "llm_trace.jsonl"
        if not trace_path.exists():
            continue
        with trace_path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = parse_jsonl_line(line)
                except json.JSONDecodeError:
                    continue
                trace_record = _parse_prompt_learning_trace_record(payload)
                if trace_record is None:
                    continue
                exact_key = _prompt_learning_trace_join_key(
                    run_id=trace_record.run_id,
                    section_id=trace_record.section_id,
                    task_type=trace_record.task_type,
                )
                if exact_key in exact_keys:
                    current = latest_by_exact_key.get(exact_key)
                    if current is None or trace_record.timestamp > current.timestamp:
                        latest_by_exact_key[exact_key] = trace_record
                    continue

                fallback_key = _prompt_learning_join_key(
                    edition_id=trace_record.edition_id,
                    issue_number=trace_record.issue_number,
                    section_id=trace_record.section_id,
                    task_type=trace_record.task_type,
                )
                if fallback_key not in fallback_keys:
                    continue
                current = latest_by_fallback_key.get(fallback_key)
                if current is None or trace_record.timestamp > current.timestamp:
                    latest_by_fallback_key[fallback_key] = trace_record

    exact_traces = tuple(latest_by_exact_key[key] for key in sorted(latest_by_exact_key))
    fallback_traces = tuple(latest_by_fallback_key[key] for key in sorted(latest_by_fallback_key))
    return exact_traces + fallback_traces


def _parse_prompt_learning_trace_record(payload: object) -> _AuditPromptLearningTraceRecord | None:
    if not isinstance(payload, dict):
        return None
    if str(payload.get("error") or "").strip():
        return None

    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return None

    timestamp = _coerce_datetime(payload.get("timestamp"))
    run_id = _coerce_str(payload.get("run_id"))
    edition_id = _coerce_str(payload.get("edition"))
    issue_number = _coerce_int(metadata.get("issue_number"))
    section_id = _coerce_str(metadata.get("section_id"))
    task_type = _coerce_str(metadata.get("task_type"))
    model = _coerce_str(payload.get("model"))
    deployment = _coerce_str(payload.get("deployment")) or model
    if (
        timestamp is None
        or run_id is None
        or edition_id is None
        or issue_number is None
        or section_id is None
        or task_type is None
        or model is None
        or deployment is None
    ):
        return None

    return _AuditPromptLearningTraceRecord(
        timestamp=timestamp,
        run_id=run_id,
        edition_id=edition_id,
        issue_number=issue_number,
        section_id=section_id,
        task_type=task_type,
        prompt_version=_coerce_str(payload.get("prompt_version")),
        model=model,
        deployment=deployment,
        latency_ms=_coerce_float(payload.get("latency_ms")),
        cost_usd=_coerce_float(payload.get("cost_usd")),
    )


def _prompt_learning_join_key(
    *,
    edition_id: str,
    issue_number: int,
    section_id: str,
    task_type: str | None,
) -> tuple[str, int, str, str]:
    return (edition_id, issue_number, section_id, task_type or "")


def _prompt_learning_trace_join_key(
    *,
    run_id: str | None,
    section_id: str,
    task_type: str | None,
) -> tuple[str, str, str]:
    return (run_id or "", section_id, task_type or "")


def _index_prompt_learning_traces(
    traces: tuple[_AuditPromptLearningTraceRecord, ...],
) -> tuple[
    dict[tuple[str, str, str], _AuditPromptLearningTraceRecord],
    dict[tuple[str, int, str, str], _AuditPromptLearningTraceRecord],
]:
    exact_traces_by_key = {
        _prompt_learning_trace_join_key(
            run_id=trace.run_id,
            section_id=trace.section_id,
            task_type=trace.task_type,
        ): trace
        for trace in traces
    }
    fallback_traces_by_key = {
        _prompt_learning_join_key(
            edition_id=trace.edition_id,
            issue_number=trace.issue_number,
            section_id=trace.section_id,
            task_type=trace.task_type,
        ): trace
        for trace in traces
    }
    return exact_traces_by_key, fallback_traces_by_key


def _match_prompt_learning_trace_to_pattern(
    pattern: EditPattern,
    *,
    exact_traces_by_key: dict[tuple[str, str, str], _AuditPromptLearningTraceRecord],
    fallback_traces_by_key: dict[tuple[str, int, str, str], _AuditPromptLearningTraceRecord],
) -> _AuditPromptLearningTraceRecord | None:
    if pattern.task_type is None:
        return None
    if pattern.trace_run_id is not None:
        return exact_traces_by_key.get(
            _prompt_learning_trace_join_key(
                run_id=pattern.trace_run_id,
                section_id=pattern.section_id,
                task_type=pattern.task_type,
            )
        )
    return fallback_traces_by_key.get(
        _prompt_learning_join_key(
            edition_id=pattern.edition_id,
            issue_number=pattern.issue_number,
            section_id=pattern.section_id,
            task_type=pattern.task_type,
        )
    )


def _coerce_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _coerce_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _coerce_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _parse_iso_date(value: str, *, option_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise typer.BadParameter(f"{option_name} must be a YYYY-MM-DD date.") from error


def _within_bounds(value: datetime, start_ts: datetime | None, end_ts: datetime | None) -> bool:
    if start_ts is not None and value < start_ts:
        return False
    if end_ts is not None and value > end_ts:
        return False
    return True


def _build_manifest_summary(manifest_path_value: str | None) -> str | None:
    if not manifest_path_value:
        return None
    manifest_path = Path(manifest_path_value)
    if not manifest_path.exists():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    freshness_raw = payload.get("freshness_summary")
    freshness: dict[str, Any] = freshness_raw if isinstance(freshness_raw, dict) else {}
    qg_raw = payload.get("qg_results")
    qg_results: dict[str, Any] = qg_raw if isinstance(qg_raw, dict) else {}
    passed = sum(1 for value in qg_results.values() if bool(value))
    total = len(qg_results)
    return (
        f"Confirmed publish | QG {passed}/{total} passing | "
        f"freshness b{int(freshness.get('blocks', 0))}/w{int(freshness.get('warns', 0))}/i{int(freshness.get('infos', 0))}"
    )


def _build_edit_pattern_summary(pattern: object) -> str:
    task_type = getattr(pattern, "task_type", None) or "unknown"
    confidence = getattr(getattr(pattern, "ai_confidence", None), "value", None) or "unknown"
    override_magnitude = getattr(pattern, "author_override_magnitude", None)
    override_text = f"{float(override_magnitude):.4f}" if override_magnitude is not None else "unknown"
    change_summary = getattr(pattern, "summary", "") or "Author edit recorded"
    return f"task={task_type} | confidence={confidence} | override={override_text} | {change_summary}"


def _review_note_suffix(note: str | None) -> str:
    if note is None or not note.strip():
        return ""
    return f": {note.strip()}"


# ---------------------------------------------------------------------------
# WS-18: hash-chain + tombstoning subcommands
# ---------------------------------------------------------------------------

@app.command("query")
def audit_query_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    action_type: str | None = typer.Option(None, "--action-type", help="Substring filter on action_type."),
    level: str | None = typer.Option(None, "--level", help="Exact-match filter on level."),
    action_id: str | None = typer.Option(None, "--action-id", help="Exact-match filter on action_id."),
    from_date: str | None = typer.Option(None, "--from", help="Inclusive lower bound (YYYY-MM-DD, UTC)."),
    to_date: str | None = typer.Option(None, "--to", help="Inclusive upper bound (YYYY-MM-DD, UTC)."),
    limit: int | None = typer.Option(None, "--limit", help="Truncate to the first N events."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, csv."),
) -> None:
    """Filter the autonomy-audit JSONL and return matching events + chain status."""
    from src.core.audit_query import build_audit_query

    parsed_from = _parse_iso_date(from_date, option_name="--from") if from_date is not None else None
    parsed_to = _parse_iso_date(to_date, option_name="--to") if to_date is not None else None
    result = build_audit_query(
        program,
        programs_root=PROGRAMS_ROOT,
        action_type=action_type,
        level=level,
        action_id=action_id,
        from_date=parsed_from,
        to_date=parsed_to,
        limit=limit,
    )
    if format == "json":
        typer.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True), nl=False)
        return
    if format == "csv":
        _render_audit_query_csv(result)
        return
    _render_audit_query_human(result, program=program)


@app.command("verify-chain")
def audit_verify_chain_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """Walk the autonomy-audit hash chain and report tampering or success."""
    from src.core.audit_query import verify_autonomy_audit_chain

    result = verify_autonomy_audit_chain(program, programs_root=PROGRAMS_ROOT)
    if format == "json":
        typer.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True), nl=False)
        return
    status = "OK" if result.ok else "BROKEN"
    typer.echo(
        f"[{status}] autonomy_audit chain for {program}: "
        f"{result.total_records} record(s), {result.excised_count} excised, "
        f"head={result.chain_head_hash or '∅'}"
    )
    if not result.ok:
        typer.echo(
            f"  broken at line {result.broken_at_line}: {result.broken_reason}"
        )
        raise typer.Exit(code=1)
    raise typer.Exit(code=0)


@app.command("excise")
def audit_excise_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    line: int = typer.Option(..., "--line", help="1-indexed line number in autonomy_audit.jsonl."),
    excisor: str = typer.Option(..., "--excisor", help="Operator name responsible for the excision."),
    reason: str | None = typer.Option(None, "--reason", help="Why this line is being redacted."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview without rewriting the file."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
) -> None:
    """Redact PII in one autonomy-audit line; the chain validator forgives the line."""
    from src.core.audit_query import excise_pii_from_autonomy_audit

    if dry_run:
        # In dry-run we still walk the chain + report what WOULD happen.
        from src.core.audit_query import verify_autonomy_audit_chain

        chain = verify_autonomy_audit_chain(program, programs_root=PROGRAMS_ROOT)
        if format == "json":
            typer.echo(
                json.dumps(
                    {
                        "dry_run": True,
                        "would_excise": {"line": line, "excisor": excisor, "reason": reason},
                        "chain_status": chain.to_dict(),
                    },
                    indent=2,
                    sort_keys=True,
                ),
                nl=False,
            )
            return
        typer.echo(
            f"DRY-RUN: would excise line {line} of autonomy_audit.jsonl for {program} "
            f"(excisor={excisor}, reason={reason or 'unspecified'})"
        )
        typer.echo(
            f"  chain before: ok={chain.ok} head={chain.chain_head_hash or '∅'} "
            f"records={chain.total_records} excised={chain.excised_count}"
        )
        raise typer.Exit(code=0)

    result = excise_pii_from_autonomy_audit(
        program, line, programs_root=PROGRAMS_ROOT, excisor=excisor, reason=reason
    )
    if format == "json":
        typer.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True), nl=False)
        return
    typer.echo(
        f"EXCISED line {result.line_number} of autonomy_audit.jsonl for {program} "
        f"(excisor={result.excisor}, original_hash={result.original_hash or '∅'})"
    )
    typer.echo(f"  chain still valid: {result.chain_still_valid}")
    raise typer.Exit(code=0)


def _render_audit_query_human(result, *, program: str) -> None:
    if result.total_matched == 0:
        typer.echo(f"No autonomy-audit events matched the filter for {program}.")
        raise typer.Exit(code=0)
    typer.echo(
        f"{result.total_matched} event(s) for {program} "
        f"(chain ok={result.chain_status.ok}, total={result.chain_status.total_records}, "
        f"excised={result.chain_status.excised_count}):"
    )
    for ev in result.events:
        if ev.get("kind") == "excision":
            typer.echo(
                f"  L{ev['line']:>4} EXCISION action_id={ev.get('action_id')} "
                f"excisor={ev.get('excisor')} at={ev.get('excised_at')}"
            )
        else:
            typer.echo(
                f"  L{ev['line']:>4} {ev.get('applied_at')} {ev.get('action_id')} "
                f"type={ev.get('action_type')} level={ev.get('level')} "
                f"accepted={ev.get('accepted')}"
            )
    if not result.chain_status.ok:
        typer.echo(
            f"WARNING: chain is BROKEN at line {result.chain_status.broken_at_line}: "
            f"{result.chain_status.broken_reason}"
        )


def _render_audit_query_csv(result) -> None:
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["line", "kind", "applied_at", "action_id", "action_type", "level", "accepted", "excisor"])
    for ev in result.events:
        writer.writerow([
            ev.get("line"),
            ev.get("kind"),
            ev.get("applied_at") or ev.get("excised_at"),
            ev.get("action_id"),
            ev.get("action_type"),
            ev.get("level"),
            ev.get("accepted"),
            ev.get("excisor"),
        ])
    typer.echo(buf.getvalue(), nl=False)