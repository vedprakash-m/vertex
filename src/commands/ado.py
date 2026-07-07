from __future__ import annotations

from collections.abc import Callable
from typing import cast
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import getpass
from io import StringIO
import json
from pathlib import Path
import re
from uuid import uuid4

import typer
import yaml

from src.commands import gather as gather_command_helpers
from src.commands import report as report_command_helpers
from src.commands.vitality import generate_vitality_report
from src.core.ado_client import ADOClient
from src.core.ado_proposal import ADOFieldMappingConfig, ADOFieldProposalValue, build_action_item_proposal, build_comment_proposal, build_field_proposal, build_vitality_nudge_proposal, build_vitality_tag_proposal, find_proposal_manifest, load_ado_field_mapping_config, load_confirmed_issue_snapshot, read_proposal_manifest, write_proposal_manifest
from src.core.ado_reconcile import ADOReconcileReport, build_ado_reconcile_report, render_ado_reconcile_report
from src.core.ado_status import _area_path_matches
from src.core.ado_status import ADOStatusReport, GatherStatus, build_ado_status_report, render_ado_status_report
from src.core.analytics_store import AutonomyAuditRecord, append_autonomy_audit_record, compute_prior_acceptance_rate
from src.core.coverage_gap import build_coverage_gaps
from src.core.claim_tracker import load_open_claims
from src.core.config_loader import ScorecardSettings, discover_report_editions, load_bundle_with_mode
from src.core.edition_resolver import EDITIONS_ROOT, PROGRAMS_ROOT, ResolvedEdition, resolve_edition
from src.core.exceptions import QueryError
from src.core.feedback.signal_approval_learner import PromotedSignalApprovalRule, load_promoted_signal_approval_rules
from src.core.models import ScorecardEvidencePacket, WorkItem
from src.core.overrides_store import load_latest_program_overrides
from src.core.models_v2 import Program, Scorecard, Signal, Workstream
from src.core.query_builder import ODataFilter
from src.core.scorecard_engine import build_scorecard
from src.core.signal_review import signal_is_approved_for_evidence
from src.core.snapshot_store import ARCHIVE_ROOT
from src.core.store_factory import build_signal_store_for_program_id, build_trajectory_store_for_program_id
from src.core.vitality_reporting import vitality_settings_from_program
from src.m365.ado_writer import ADOApplyArtifacts, ADOWriter


ProgramLoader = Callable[[str, Path], tuple[Program, tuple[Workstream, ...]]]
StatusItemLoader = Callable[[ADOClient | None, Program, datetime], tuple[tuple[WorkItem, ...], int]]
AreaScopeLoader = Callable[[str], tuple[str, ...]]

_GATHER_SOURCES = {
    "ado/odata",
    "ado/revision",
    "workiq/email",
    "workiq/teams",
    "workiq/transcript",
    "kusto",
    "icm",
    "vertex/freshness",
}


@dataclass(frozen=True, slots=True)
class ADOStatusArtifacts:
    report: ADOStatusReport
    exit_code: int
    ado_calls: int


@dataclass(frozen=True, slots=True)
class ADOProposalArtifacts:
    proposal_id: str
    edition_id: str
    manifest_path: Path | None
    entry_count: int
    ado_calls: int


@dataclass(frozen=True, slots=True)
class ADOReconcileArtifacts:
    report: ADOReconcileReport
    ado_calls: int


@dataclass(frozen=True, slots=True)
class ADORepositoryCandidate:
    workstream_id: str
    repository_id: str
    repository_name: str
    score: int
    matched_terms: tuple[str, ...]
    active_pr_count: int | None = None


app = typer.Typer(help="ADO diagnostics and update workflows.")


@app.command("status")
def ado_status_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    artifacts = generate_ado_status(program)
    if format == "human":
        typer.echo(render_ado_status_report(artifacts.report))
    else:
        typer.echo(render_ado_status_output(artifacts, format=format), nl=False)
    raise typer.Exit(code=artifacts.exit_code)


@app.command("propose")
def ado_propose_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    proposal_type: str = typer.Option(..., "--type", help="Proposal type. Supports comment, field, vitality_nudge, and vitality_tag."),
    edition: str | None = typer.Option(None, "--edition", help="Edition id that owns the confirmed issue."),
    issue: int | None = typer.Option(None, "--issue", help="Confirmed issue number to cite in the proposal."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview the proposal without writing a manifest file."),
) -> None:
    artifacts = generate_ado_proposal(
        program_id=program,
        proposal_type=proposal_type,
        edition_id=edition,
        issue_number=issue,
        dry_run=dry_run,
        programs_root=PROGRAMS_ROOT,
        editions_root=EDITIONS_ROOT,
        archive_root=ARCHIVE_ROOT,
    )
    typer.echo(
        f"Proposal {artifacts.proposal_id} | edition: {artifacts.edition_id} | entries: {artifacts.entry_count} | ado calls: {artifacts.ado_calls}"
    )
    if artifacts.manifest_path is None:
        typer.echo("Dry-run: proposal manifest not written.")
    else:
        typer.echo(f"Manifest: {artifacts.manifest_path}")
    raise typer.Exit(code=0)


@app.command("apply")
def ado_apply_command(
    proposal: str = typer.Option(..., "--proposal", help="Proposal id or manifest path."),
    yes: bool = typer.Option(False, "--yes", help="Apply without interactive confirmation."),
) -> None:
    manifest_path = _resolve_proposal_manifest(proposal, programs_root=PROGRAMS_ROOT)
    loaded_proposal, _ = read_proposal_manifest(manifest_path)
    batch_rule = _batch_approval_rule_for_action_type(
        loaded_proposal.program_id,
        action_type=loaded_proposal.update_type,
        programs_root=PROGRAMS_ROOT,
    )
    pending_entries = sum(1 for entry in loaded_proposal.entries if entry.entry_status in {"pending", "failed"})
    if pending_entries and not yes and batch_rule is None:
        if not typer.confirm(f"Apply {pending_entries} update(s) to ADO from proposal {loaded_proposal.id}?", default=False):
            _record_ado_apply_declined_audit(
                loaded_proposal,
                author_alias=_default_actor(None),
                approval_rule=_matching_promoted_signal_approval_rule(
                    loaded_proposal.program_id,
                    action_type=loaded_proposal.update_type,
                    programs_root=PROGRAMS_ROOT,
                ),
                declined_at=datetime.now(timezone.utc),
                programs_root=PROGRAMS_ROOT,
            )
            raise typer.Exit(code=1)
    if batch_rule is not None:
        typer.echo(
            f"Using promoted batch approval rule {batch_rule.proposal.rule_id} "
            f"for {loaded_proposal.update_type}."
        )

    artifacts = apply_ado_proposal(
        proposal,
        programs_root=PROGRAMS_ROOT,
        author_alias=_default_actor(None),
    )
    typer.echo(
        f"Applied proposal {artifacts.proposal.id}: {artifacts.applied_count} applied, {artifacts.skipped_count} skipped, {artifacts.conflict_count} conflict, {artifacts.failed_count} failed."
    )
    typer.echo(f"Manifest: {artifacts.manifest_path}")
    raise typer.Exit(code=_apply_exit_code(artifacts))


@app.command("reconcile")
def ado_reconcile_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    artifacts = generate_ado_reconcile(program)
    if format == "human":
        typer.echo(render_ado_reconcile_report(artifacts.report))
    else:
        typer.echo(render_ado_reconcile_output(artifacts, format=format), nl=False)
    raise typer.Exit(code=0)


@app.command("discover-repos")
def ado_discover_repos_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    workstream: str | None = typer.Option(None, "--workstream", help="Optional workstream id to scope repository discovery."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    candidates = discover_ado_repository_candidates(program_id=program, workstream_id=workstream)
    if format == "human":
        if not candidates:
            typer.echo(f"No ADO repository candidates found for {program}.")
            raise typer.Exit(code=1)
        current_workstream = None
        for candidate in candidates:
            if current_workstream != candidate.workstream_id:
                current_workstream = candidate.workstream_id
                typer.echo(f"[{current_workstream}]")
            pr_suffix = "" if candidate.active_pr_count is None else f" | active_prs={candidate.active_pr_count}"
            typer.echo(
                f"- {candidate.repository_name} | {candidate.repository_id} | score={candidate.score} | terms={', '.join(candidate.matched_terms)}{pr_suffix}"
            )
    else:
        typer.echo(render_ado_repository_candidates_output(candidates, format=format), nl=False)
    raise typer.Exit(code=0)


@app.command("set-repos")
def ado_set_repos_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    workstream: str = typer.Option(..., "--workstream", help="Workstream id to update."),
    repository_id: list[str] = typer.Option(None, "--repository-id", help="Repository id to attach. Repeat for multiple repos."),
    repository_name: list[str] = typer.Option(None, "--repository-name", help="Exact repository name to resolve and attach. Repeat for multiple repos."),
    clear: bool = typer.Option(False, "--clear", help="Clear all configured ado_repository_ids for the target workstream."),
) -> None:
    if clear and (repository_id or repository_name):
        raise typer.BadParameter("--clear cannot be combined with --repository-id or --repository-name.")
    if not clear and not repository_id and not repository_name:
        raise typer.BadParameter("Provide at least one --repository-id or --repository-name, or use --clear.")

    updated_ids = set_ado_repository_ids(
        program_id=program,
        workstream_id=workstream,
        repository_ids=tuple(repository_id or ()),
        repository_names=tuple(repository_name or ()),
        clear=clear,
    )
    if clear:
        typer.echo(f"Cleared ado_repository_ids for {program}/{workstream}.")
    else:
        typer.echo(f"Updated ado_repository_ids for {program}/{workstream}: {', '.join(updated_ids)}")
    raise typer.Exit(code=0)


def render_ado_status_output(artifacts: ADOStatusArtifacts, *, format: str) -> str:
    payload = _build_ado_status_payload(artifacts)
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            (
                "entry_type",
                "program_id",
                "ado_calls",
                "workstream_id",
                "ref_id",
                "title",
                "state",
                "area_path",
                "assigned_to",
                "detail",
            )
        )
        writer.writerow(
            (
                "summary",
                payload["program_id"],
                payload["ado_calls"],
                None,
                None,
                None,
                None,
                None,
                None,
                json.dumps(payload["counts"], sort_keys=True),
            )
        )
        for row in payload["area_coverage"]:  # type: ignore[attr-defined]
            writer.writerow(
                (
                    "area_coverage",
                    payload["program_id"],
                    payload["ado_calls"],
                    row["workstream_id"],
                    None,
                    row["workstream_name"],
                    None,
                    row["area_path"],
                    None,
                    json.dumps(row["analytics_matches"]),
                )
            )
        for area_path in payload["unmapped_area_paths"]:  # type: ignore[attr-defined]
            writer.writerow(("unmapped_area", payload["program_id"], payload["ado_calls"], None, None, None, None, area_path, None, None))
        for item in payload["orphaned_items"]:  # type: ignore[attr-defined]
            writer.writerow(
                (
                    "orphaned_item",
                    payload["program_id"],
                    payload["ado_calls"],
                    None,
                    item["work_item_id"],
                    item["title"],
                    item["state"],
                    item["area_path"],
                    item["assigned_to"],
                    None,
                )
            )
        for gap in payload["coverage_gaps"]:  # type: ignore[attr-defined]
            writer.writerow(
                (
                    "coverage_gap",
                    payload["program_id"],
                    payload["ado_calls"],
                    None,
                    gap["work_item_id"],
                    gap["title"],
                    gap["state"],
                    None,
                    gap["assigned_to"],
                    json.dumps({"confidence": gap["confidence"]}, sort_keys=True),
                )
            )
        last_gather = payload["last_gather"]
        if last_gather is not None:
            writer.writerow(
                (
                    "last_gather",
                    payload["program_id"],
                    payload["ado_calls"],
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    json.dumps(last_gather, sort_keys=True),
                )
            )
        return buffer.getvalue()
    raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")


def render_ado_reconcile_output(artifacts: ADOReconcileArtifacts, *, format: str) -> str:
    payload = _build_ado_reconcile_payload(artifacts)
    if format == "json":
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if format == "csv":
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(("program_id", "ado_calls", "override_issue_number", "kind", "work_item_id", "context", "vertex_value", "ado_value", "note"))
        if payload["discrepancies"]:
            for entry in payload["discrepancies"]:  # type: ignore[attr-defined]
                writer.writerow(
                    (
                        payload["program_id"],
                        payload["ado_calls"],
                        payload["override_issue_number"],
                        entry["kind"],
                        entry["work_item_id"],
                        entry["context"],
                        entry["vertex_value"],
                        entry["ado_value"],
                        entry["note"],
                    )
                )
        else:
            writer.writerow((payload["program_id"], payload["ado_calls"], payload["override_issue_number"], None, None, None, None, None, None))
        return buffer.getvalue()
    raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")


def discover_ado_repository_candidates(
    program_id: str,
    *,
    workstream_id: str | None = None,
    programs_root: Path | None = None,
    program_loader: ProgramLoader | None = None,
    client_factory: Callable[[Program], ADOClient] | None = None,
) -> tuple[ADORepositoryCandidate, ...]:
    resolved_programs_root = programs_root or PROGRAMS_ROOT
    loaded_program, workstreams = (program_loader or gather_command_helpers._load_program_context)(program_id, resolved_programs_root)
    if loaded_program.ado is None:
        raise typer.BadParameter(f"Program '{program_id}' is missing ado configuration.")
    scoped_workstreams = tuple(
        ws for ws in workstreams if workstream_id is None or ws.id == workstream_id
    )
    if not scoped_workstreams:
        raise typer.BadParameter(f"No workstreams matched '{workstream_id}' for program '{program_id}'.")

    client = (client_factory or _build_ado_client)(loaded_program)
    repositories = client.list_repositories()
    candidates: list[ADORepositoryCandidate] = []
    for ws in scoped_workstreams:
        terms = _ado_repository_search_terms(ws)
        preliminary_candidates: list[ADORepositoryCandidate] = []
        for repository in repositories:
            repository_name = str(repository.get("name") or "").strip()
            if not repository_name:
                continue
            matched_terms = _matched_repository_terms(repository_name, terms)
            if not matched_terms:
                continue
            repository_id = str(repository.get("id") or "").strip()
            if not repository_id:
                continue
            preliminary_candidates.append(
                ADORepositoryCandidate(
                    workstream_id=ws.id,
                    repository_id=repository_id,
                    repository_name=repository_name,
                    score=_repository_candidate_score(repository_name, matched_terms, None),
                    matched_terms=matched_terms,
                    active_pr_count=None,
                )
            )
        preliminary_candidates.sort(
            key=lambda candidate: (-candidate.score, candidate.repository_name.lower(), candidate.repository_id)
        )
        workstream_candidates: list[ADORepositoryCandidate] = []
        for candidate in preliminary_candidates[:8]:
            active_pr_count: int | None = None
            try:
                active_pr_count = len(client.list_pull_requests(candidate.repository_id, status="active", top=20))
            except QueryError:
                active_pr_count = None
            workstream_candidates.append(
                ADORepositoryCandidate(
                    workstream_id=candidate.workstream_id,
                    repository_id=candidate.repository_id,
                    repository_name=candidate.repository_name,
                    score=_repository_candidate_score(candidate.repository_name, candidate.matched_terms, active_pr_count),
                    matched_terms=candidate.matched_terms,
                    active_pr_count=active_pr_count,
                )
            )
        workstream_candidates.sort(
            key=lambda candidate: (-candidate.score, candidate.repository_name.lower(), candidate.repository_id)
        )
        candidates.extend(workstream_candidates)
    return tuple(candidates)


def render_ado_repository_candidates_output(candidates: tuple[ADORepositoryCandidate, ...], *, format: str) -> str:
    rows = [
        {
            "workstream_id": candidate.workstream_id,
            "repository_name": candidate.repository_name,
            "repository_id": candidate.repository_id,
            "score": candidate.score,
            "matched_terms": list(candidate.matched_terms),
            "active_pr_count": candidate.active_pr_count,
        }
        for candidate in candidates
    ]
    if format == "json":
        return json.dumps(rows, indent=2, sort_keys=True)
    if format == "csv":
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerow(("workstream_id", "repository_name", "repository_id", "score", "matched_terms", "active_pr_count"))
        for row in rows:
            writer.writerow(
                (
                    row["workstream_id"],
                    row["repository_name"],
                    row["repository_id"],
                    row["score"],
                    "|".join(row["matched_terms"]),  # type: ignore[arg-type]
                    row["active_pr_count"] if row["active_pr_count"] is not None else "",
                )
            )
        return buffer.getvalue()
    raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")


def set_ado_repository_ids(
    program_id: str,
    *,
    workstream_id: str,
    repository_ids: tuple[str, ...] = (),
    repository_names: tuple[str, ...] = (),
    clear: bool = False,
    programs_root: Path | None = None,
    program_loader: ProgramLoader | None = None,
    client_factory: Callable[[Program], ADOClient] | None = None,
) -> tuple[str, ...]:
    resolved_programs_root = programs_root or PROGRAMS_ROOT
    loaded_program, workstreams = (program_loader or gather_command_helpers._load_program_context)(program_id, resolved_programs_root)
    if loaded_program.ado is None:
        raise typer.BadParameter(f"Program '{program_id}' is missing ado configuration.")
    if not any(workstream.id == workstream_id for workstream in workstreams):
        raise typer.BadParameter(f"Workstream '{workstream_id}' not found for program '{program_id}'.")

    resolved_ids: list[str] = []
    if not clear:
        resolved_ids.extend(repository_id for repository_id in repository_ids if repository_id.strip())
        if repository_names:
            client = (client_factory or _build_ado_client)(loaded_program)
            repositories = client.list_repositories()
            name_to_id = {
                str(repository.get("name") or "").strip().lower(): str(repository.get("id") or "").strip()
                for repository in repositories
                if str(repository.get("name") or "").strip() and str(repository.get("id") or "").strip()
            }
            for repository_name in repository_names:
                normalized_name = repository_name.strip().lower()
                resolved_id = name_to_id.get(normalized_name)
                if resolved_id is None:
                    raise typer.BadParameter(
                        f"Repository name '{repository_name}' could not be resolved in ADO inventory for program '{program_id}'."
                    )
                resolved_ids.append(resolved_id)

    final_ids = tuple(dict.fromkeys(resolved_ids))

    # Delegate to the canonical workstreams document writer so the file
    # round-trips through ``save_workstreams_document`` — atomic temp-file
    # write + ``.bak`` snapshot + ``_sync_workstream_facts`` projection (the
    # ``ado_repository_ids`` change is reflected in the ``workstream.entry``
    # fact payload at precedence ``ACTIVE_PM_JUDGMENT``).  See spec §11.3
    # Phase 7 D-24 P2 program-literals de-coupling for the broader context.
    from src.core.workstream_documents import save_workstreams_document  # noqa: PLC0415

    workstreams_path = resolved_programs_root / program_id / "workstreams.yaml"
    document = yaml.safe_load(workstreams_path.read_text(encoding="utf-8")) or {}
    raw_workstreams = document.get("workstreams")
    if not isinstance(raw_workstreams, list):
        raise typer.BadParameter(f"Invalid workstreams.yaml for program '{program_id}'.")
    updated = False
    for entry in raw_workstreams:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("id") or "").strip() != workstream_id:
            continue
        entry["ado_repository_ids"] = list(final_ids)
        updated = True
        break
    if not updated:
        raise typer.BadParameter(f"Workstream '{workstream_id}' not found in workstreams.yaml for program '{program_id}'.")

    save_workstreams_document(program_id, document, programs_root=resolved_programs_root)
    return final_ids


def _ado_repository_search_terms(workstream: Workstream) -> tuple[str, ...]:
    seed_terms = [workstream.id, workstream.name, *workstream.aliases]
    if workstream.signal_sources is not None:
        seed_terms.extend(workstream.signal_sources.workiq_keywords)
    normalized: list[str] = []
    for value in seed_terms:
        for token in re.split(r"[^a-z0-9]+", str(value).lower()):
            stripped = token.strip()
            if len(stripped) < 3:
                continue
            normalized.append(stripped)

    # Program-specific repository alias lists can be injected here via
    # workstream.aliases from your edition YAML (no hardcoded program names).

    deduped = tuple(dict.fromkeys(normalized))
    return deduped


def _matched_repository_terms(repository_name: str, terms: tuple[str, ...]) -> tuple[str, ...]:
    normalized_name = repository_name.lower()
    matched = [term for term in terms if term and term in normalized_name]
    return tuple(dict.fromkeys(matched))


def _repository_candidate_score(repository_name: str, matched_terms: tuple[str, ...], active_pr_count: int | None) -> int:
    normalized_name = repository_name.lower()
    score = sum(len(term) for term in matched_terms)
    if normalized_name.startswith("storage-"):
        score += 5
    if normalized_name.startswith("networking-"):
        score += 3
    if active_pr_count:
        score += min(active_pr_count, 10)
    return score


def _build_ado_status_payload(artifacts: ADOStatusArtifacts) -> dict[str, object]:
    report = artifacts.report
    return {
        "ado_calls": artifacts.ado_calls,
        "area_coverage": [
            {
                "active_item_count": row.active_item_count,
                "analytics_matches": list(row.analytics_matches),
                "area_path": row.area_path,
                "workstream_id": row.workstream_id,
                "workstream_name": row.workstream_name,
            }
            for row in report.area_coverage
        ],
        "counts": {
            "area_coverage": len(report.area_coverage),
            "coverage_gaps": len(report.coverage_gaps),
            "orphaned_items": len(report.orphaned_items),
            "total_active_items": report.total_active_items,
            "unmapped_area_paths": len(report.unmapped_area_paths),
        },
        "coverage_gaps": [
            {
                "assigned_to": gap.assigned_to,
                "confidence": gap.confidence.value,
                "state": gap.state,
                "title": gap.title,
                "work_item_id": gap.work_item_id,
            }
            for gap in report.coverage_gaps
        ],
        "date_window_days": report.date_window_days,
        "exit_code": artifacts.exit_code,
        "last_gather": None
        if report.last_gather is None
        else {
            "captured_at": report.last_gather.captured_at.isoformat(),
            "signal_count": report.last_gather.signal_count,
            "trajectory_updates": report.last_gather.trajectory_updates,
        },
        "organization": report.organization,
        "orphaned_items": [
            {
                "area_path": item.area_path,
                "assigned_to": item.assigned_to,
                "state": item.state,
                "title": item.title,
                "work_item_id": item.work_item_id,
            }
            for item in report.orphaned_items
        ],
        "program_id": report.program_id,
        "project": report.project,
        "saved_query_count": report.saved_query_count,
        "unmapped_area_paths": list(report.unmapped_area_paths),
    }


def _build_ado_reconcile_payload(artifacts: ADOReconcileArtifacts) -> dict[str, object]:
    report = artifacts.report
    return {
        "ado_calls": artifacts.ado_calls,
        "discrepancies": [
            {
                "ado_value": entry.ado_value,
                "context": entry.context,
                "kind": entry.kind,
                "note": entry.note,
                "vertex_value": entry.vertex_value,
                "work_item_id": entry.work_item_id,
            }
            for entry in report.discrepancies
        ],
        "override_issue_number": report.override_issue_number,
        "program_id": report.program_id,
    }


def _empty_scope_loader(_: str) -> tuple[str, ...]:
    return ()


def generate_ado_status(
    program_id: str,
    *,
    as_of: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
    program_loader: ProgramLoader | None = None,
    item_loader: StatusItemLoader | None = None,
    area_scope_loader: AreaScopeLoader | None = None,
) -> ADOStatusArtifacts:
    current_time = as_of or datetime.now(timezone.utc)
    program, workstreams = (program_loader or gather_command_helpers._load_program_context)(program_id, programs_root)
    if program.ado is None:
        raise typer.BadParameter(f"Program '{program_id}' is missing ado configuration.")

    client: ADOClient | None = None
    if item_loader is None or area_scope_loader is None:
        client = ADOClient(
            organization=program.ado.organization,
            project=program.ado.project,
            timeout=program.ado.api_timeout_seconds,
        )

    items, ado_calls = (item_loader or _load_active_program_items)(client, program, current_time)
    approved_signals = _load_approved_signals(
        program_id,
        as_of=current_time,
        window_days=program.ado.date_window_days,
        programs_root=programs_root,
    )
    narratives = _load_latest_narratives(program_id, programs_root=programs_root)
    report = build_ado_status_report(
        program=program,
        workstreams=workstreams,
        items=items,
        approved_signals=approved_signals,
        narratives=narratives,
        as_of=current_time,
        area_scope_loader=area_scope_loader or (client.find_area_scope_matches if client is not None else _empty_scope_loader),
        last_gather=_load_last_gather(program_id, programs_root=programs_root),
    )
    return ADOStatusArtifacts(report=report, exit_code=0, ado_calls=ado_calls)


def generate_ado_proposal(
    program_id: str,
    *,
    proposal_type: str,
    edition_id: str | None,
    issue_number: int | None,
    as_of: datetime | None = None,
    programs_root: Path | None = None,
    editions_root: Path | None = None,
    archive_root: Path | None = None,
    client_factory: Callable[[Program], ADOClient] | None = None,
    dry_run: bool = False,
) -> ADOProposalArtifacts:
    resolved_programs_root = programs_root or PROGRAMS_ROOT
    resolved_editions_root = editions_root or EDITIONS_ROOT
    resolved_archive_root = archive_root or ARCHIVE_ROOT
    normalized_type = proposal_type.strip().lower()
    current_time = as_of or datetime.now(timezone.utc)
    if normalized_type == "comment":
        if issue_number is None:
            raise typer.BadParameter("--issue is required for --type comment.")
        resolved = _resolve_comment_edition(
            program_id,
            edition_id=edition_id,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
        if resolved.program.ado is None:
            raise typer.BadParameter(f"Program '{program_id}' is missing ado configuration.")
        snapshot = load_confirmed_issue_snapshot(resolved.paths.edition_id, issue_number, archive_root=resolved_archive_root)
        client = (client_factory or _build_ado_client)(resolved.program)
        current_rows = client.query_work_items_batch(
            [item.id for item in snapshot.items],
            fields=("System.Id", "System.Rev"),
        )
        proposal = build_comment_proposal(
            program_id=program_id,
            edition_id=resolved.paths.edition_id,
            snapshot=snapshot,
            current_work_item_rows=current_rows,
            created_at=current_time,
            ttl_hours=resolved.program.ado.proposal_ttl_hours,
        )
        total_ado_calls = 1
    elif normalized_type == "field":
        resolved = _resolve_program_edition(
            program_id,
            edition_id=edition_id,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
        if resolved.program.ado is None:
            raise typer.BadParameter(f"Program '{program_id}' is missing ado configuration.")
        try:
            mapping_config = load_ado_field_mapping_config(program_id, programs_root=resolved_programs_root)
        except (FileNotFoundError, ValueError) as error:
            raise typer.BadParameter(str(error)) from error

        client = (client_factory or _build_ado_client)(resolved.program)
        items, item_ado_calls = _load_active_program_items(client, resolved.program, current_time)
        requested_fields = tuple(sorted({"System.Id", "System.Rev", *(mapping.ado_field for mapping in mapping_config.mappings)}))
        current_rows = client.query_work_items_batch([item.id for item in items], fields=requested_fields)
        overrides_document = load_latest_program_overrides(program_id, programs_root=resolved_programs_root)
        proposal = build_field_proposal(
            program_id=program_id,
            edition_id=resolved.paths.edition_id,
            issue_number=overrides_document.issue_number if overrides_document is not None else None,
            mapping_config=mapping_config,
            current_work_item_rows=current_rows,
            field_values_by_item=_build_field_proposal_values(
                items=items,
                workstreams=resolved.workstreams,
                scorecards=resolved.scorecards,
                overrides_document=overrides_document,
                mapping_config=mapping_config,
            ),
            created_at=current_time,
        )
        total_ado_calls = item_ado_calls + (1 if items else 0)
    elif normalized_type in {"vitality_nudge", "vitality_tag"}:
        resolved = _resolve_program_edition(
            program_id,
            edition_id=edition_id,
            editions_root=resolved_editions_root,
            programs_root=resolved_programs_root,
        )
        if resolved.program.ado is None:
            raise typer.BadParameter(f"Program '{program_id}' is missing ado configuration.")
        settings = vitality_settings_from_program(resolved.raw_program)
        if normalized_type == "vitality_nudge" and not settings.ado_nudge_comments:
            raise typer.BadParameter(
                f"Vitality ADO nudge comments are disabled for program '{program_id}'."
            )
        if normalized_type == "vitality_tag" and not settings.ado_tags:
            raise typer.BadParameter(
                f"Vitality ADO tags are disabled for program '{program_id}'."
            )
        vitality_artifacts = generate_vitality_report(
            program_id,
            as_of=current_time,
            programs_root=resolved_programs_root,
        )
        client = (client_factory or _build_ado_client)(resolved.program)
        current_rows = client.query_work_items_batch(
            [item.id for item in vitality_artifacts.items],
            fields=("System.Id", "System.Rev"),
        )
        current_issue_number = _current_issue_number(program_id, programs_root=resolved_programs_root)
        if normalized_type == "vitality_nudge":
            recent_nudge_item_ids = _recent_ado_update_item_ids(
                program_id,
                update_type="vitality_nudge",
                as_of=current_time,
                lookback_days=settings.nudge_cooldown_days,
                programs_root=resolved_programs_root,
            )
            proposal = build_vitality_nudge_proposal(
                program_id=program_id,
                edition_id=resolved.paths.edition_id,
                issue_number=current_issue_number,
                items=vitality_artifacts.items,
                scores=vitality_artifacts.scored_items,
                current_work_item_rows=current_rows,
                created_at=current_time,
                ttl_hours=resolved.program.ado.proposal_ttl_hours,
                composite_threshold=settings.nudge_composite_threshold,
                stale_days=settings.nudge_stale_days,
                recent_nudge_item_ids=recent_nudge_item_ids,
            )
        else:
            coverage_gaps = build_coverage_gaps(
                vitality_artifacts.items,
                approved_signals=_load_approved_signals(
                    program_id,
                    as_of=current_time,
                    window_days=resolved.program.ado.date_window_days,
                    programs_root=resolved_programs_root,
                ),
                narratives=_load_latest_narratives(program_id, programs_root=resolved_programs_root),
                as_of=current_time,
                min_age_days=resolved.program.ado.date_window_days,
            )
            proposal = build_vitality_tag_proposal(
                program_id=program_id,
                edition_id=resolved.paths.edition_id,
                issue_number=current_issue_number,
                items=vitality_artifacts.items,
                scores=vitality_artifacts.scored_items,
                current_work_item_rows=current_rows,
                coverage_gaps=coverage_gaps,
                created_at=current_time,
                ttl_hours=resolved.program.ado.proposal_ttl_hours,
                tag_name=settings.vitality_tag_name,
                consecutive_gap_threshold=settings.tag_consecutive_gaps,
                gap_window_days=resolved.program.ado.date_window_days,
            )
        total_ado_calls = vitality_artifacts.ado_calls + (1 if vitality_artifacts.items else 0)
    else:
        raise typer.BadParameter("Supported --type values are comment, field, vitality_nudge, and vitality_tag.")

    manifest_path = None if dry_run else write_proposal_manifest(proposal, programs_root=resolved_programs_root)
    return ADOProposalArtifacts(
        proposal_id=proposal.id,
        edition_id=resolved.paths.edition_id,
        manifest_path=manifest_path,
        entry_count=len(proposal.entries),
        ado_calls=total_ado_calls,
    )


def apply_ado_proposal(
    proposal_reference: str,
    *,
    applied_at: datetime | None = None,
    author_alias: str | None = None,
    programs_root: Path | None = None,
    program_loader: ProgramLoader | None = None,
    client_factory: Callable[[Program], ADOClient] | None = None,
) -> ADOApplyArtifacts:
    resolved_programs_root = programs_root or PROGRAMS_ROOT
    manifest_path = _resolve_proposal_manifest(proposal_reference, programs_root=resolved_programs_root)
    proposal, _ = read_proposal_manifest(manifest_path)
    program, _ = (program_loader or gather_command_helpers._load_program_context)(proposal.program_id, resolved_programs_root)
    if program.ado is None:
        raise typer.BadParameter(f"Program '{proposal.program_id}' is missing ado configuration.")
    client = (client_factory or _build_ado_client)(program)
    artifacts = ADOWriter(client, programs_root=resolved_programs_root).apply_manifest(manifest_path, applied_at=applied_at)
    if _should_record_ado_apply_audit(artifacts):
        _record_ado_apply_audit(
            artifacts,
            author_alias=_default_actor(author_alias),
            programs_root=resolved_programs_root,
        )
    return artifacts


def generate_ado_reconcile(
    program_id: str,
    *,
    as_of: datetime | None = None,
    programs_root: Path | None = None,
    editions_root: Path | None = None,
    program_loader: ProgramLoader | None = None,
    item_loader: StatusItemLoader | None = None,
    scorecard_loader: Callable[[str, Path, Path], tuple[ScorecardSettings, ...]] | None = None,
) -> ADOReconcileArtifacts:
    resolved_programs_root = programs_root or PROGRAMS_ROOT
    resolved_editions_root = editions_root or EDITIONS_ROOT
    current_time = as_of or datetime.now(timezone.utc)
    program, workstreams = (program_loader or gather_command_helpers._load_program_context)(program_id, resolved_programs_root)
    if program.ado is None:
        raise typer.BadParameter(f"Program '{program_id}' is missing ado configuration.")
    client: ADOClient | None = None
    if item_loader is None:
        client = _build_ado_client(program)
    items, ado_calls = (item_loader or _load_active_program_items)(client, program, current_time)
    report = build_ado_reconcile_report(
        program_id=program_id,
        items=items,
        workstreams=workstreams,
        scorecards=(scorecard_loader or _load_reconcile_scorecards)(program_id, resolved_editions_root, resolved_programs_root),
        overrides_document=load_latest_program_overrides(program_id, programs_root=resolved_programs_root),
        open_claims=load_open_claims(program_id, programs_root=resolved_programs_root),
    )
    return ADOReconcileArtifacts(report=report, ado_calls=ado_calls)


def _load_active_program_items(
    client: ADOClient | None,
    program: Program,
    as_of: datetime,
) -> tuple[tuple[WorkItem, ...], int]:
    if client is None or program.ado is None:
        return (), 0

    rows = client.query_all(
        filter_expression=(
            ODataFilter()
            .in_area_paths(program.ado.area_paths)
            .in_work_item_types(program.ado.work_item_types)
            .not_in_states(program.ado.excluded_states)
            .build()
        ),
        select_fields=(
            "WorkItemId",
            "WorkItemType",
            "Title",
            "State",
            "ChangedDate",
        ),
        top=report_command_helpers.DEFAULT_ADO_TOP,
    )
    ids = [int(row.get("WorkItemId") or row.get("id") or 0) for row in rows if int(row.get("WorkItemId") or row.get("id") or 0) > 0]
    batch_rows = client.query_work_items_batch(ids, report_command_helpers._BATCH_FIELDS)
    batch_by_id = {int(row.get("id") or row.get("fields", {}).get("System.Id") or 0): row for row in batch_rows}
    return (
        tuple(
            report_command_helpers._work_item_from_sources(
                row,
                batch_by_id.get(int(row.get("WorkItemId") or row.get("id") or 0), {}),
                as_of,
            )
            for row in rows
        ),
        1 + (1 if ids else 0),
    )


def _load_approved_signals(
    program_id: str,
    *,
    as_of: datetime,
    window_days: int,
    programs_root: Path,
) -> tuple[Signal, ...]:
    window_start = as_of - __import__("datetime", fromlist=["timedelta"]).timedelta(days=window_days)
    signal_store = build_signal_store_for_program_id(program_id, programs_root=programs_root)
    review_states = signal_store.read_reviews(program_id)
    return tuple(
        signal
        for signal in signal_store.read(program_id, start=window_start, end=as_of)
        if signal_is_approved_for_evidence(signal, review_states)
    )


def _load_latest_narratives(program_id: str, *, programs_root: Path) -> dict[str, str]:
    narratives_root = programs_root / program_id / "narratives"
    if not narratives_root.exists():
        return {}
    issue_dirs = sorted(
        (path for path in narratives_root.glob("issue_*") if path.is_dir()),
        key=lambda entry: entry.name.lower(),
    )
    if not issue_dirs:
        return {}
    latest_dir = issue_dirs[-1]
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(latest_dir.glob("*.md"), key=lambda entry: entry.name.lower())
    }


def _load_last_gather(program_id: str, *, programs_root: Path) -> GatherStatus | None:
    signal_store = build_signal_store_for_program_id(program_id, programs_root=programs_root)
    gather_signals = tuple(
        signal
        for signal in signal_store.read(program_id)
        if signal.source in _GATHER_SOURCES
    )
    if not gather_signals:
        return None

    captured_at = max(signal.timestamp for signal in gather_signals)
    signal_count = sum(1 for signal in gather_signals if signal.timestamp == captured_at)
    trajectory_updates = _count_latest_trajectory_updates(
        program_id,
        captured_at=captured_at,
        programs_root=programs_root,
    )

    return GatherStatus(
        captured_at=captured_at,
        signal_count=signal_count,
        trajectory_updates=trajectory_updates,
    )


def _resolve_comment_edition(
    program_id: str,
    *,
    edition_id: str | None,
    editions_root: Path,
    programs_root: Path,
) -> ResolvedEdition:
    if edition_id is not None:
        resolved = resolve_edition(edition_id, editions_root=editions_root, programs_root=programs_root)
        if resolved is None:
            raise typer.BadParameter(f"Edition '{edition_id}' was not found.")
        if resolved.paths.program_id != program_id:
            raise typer.BadParameter(
                f"Edition '{edition_id}' belongs to program '{resolved.paths.program_id}', not '{program_id}'."
            )
        return resolved

    matches = [
        resolved
        for edition_name in discover_report_editions(editions_root=editions_root, programs_root=programs_root)
        for resolved in [resolve_edition(edition_name, editions_root=editions_root, programs_root=programs_root)]
        if resolved is not None and resolved.paths.program_id == program_id
    ]
    if not matches:
        raise typer.BadParameter(f"No editions were found for program '{program_id}'.")
    if len(matches) > 1:
        choices = ", ".join(sorted(match.paths.edition_id for match in matches))
        raise typer.BadParameter(f"Program '{program_id}' has multiple editions. Provide --edition. Choices: {choices}")
    return matches[0]


def _resolve_program_edition(
    program_id: str,
    *,
    edition_id: str | None,
    editions_root: Path,
    programs_root: Path,
) -> ResolvedEdition:
    if edition_id is not None:
        resolved = resolve_edition(edition_id, editions_root=editions_root, programs_root=programs_root)
        if resolved is None:
            raise typer.BadParameter(f"Edition '{edition_id}' was not found.")
        if resolved.paths.program_id != program_id:
            raise typer.BadParameter(
                f"Edition '{edition_id}' belongs to program '{resolved.paths.program_id}', not '{program_id}'."
            )
        return resolved
    return _resolve_primary_program_edition(program_id, editions_root=editions_root, programs_root=programs_root)


def _resolve_primary_program_edition(
    program_id: str,
    *,
    editions_root: Path,
    programs_root: Path,
) -> ResolvedEdition:
    matches = _matching_program_editions(program_id, editions_root=editions_root, programs_root=programs_root)
    if not matches:
        raise typer.BadParameter(f"No editions were found for program '{program_id}'.")
    return sorted(
        matches,
        key=lambda resolved: (
            0 if resolved.edition.type in {"detailed", "focused", "narrative", "condensed"} else 1,
            0 if resolved.edition.cadence == "weekly" else 1,
            -sum(len(scorecard.dimensions) for scorecard in resolved.scorecards),
            resolved.paths.edition_id,
        ),
    )[0]


def _matching_program_editions(
    program_id: str,
    *,
    editions_root: Path,
    programs_root: Path,
) -> list[ResolvedEdition]:
    return [
        resolved
        for edition_name in discover_report_editions(editions_root=editions_root, programs_root=programs_root)
        for resolved in [resolve_edition(edition_name, editions_root=editions_root, programs_root=programs_root)]
        if resolved is not None and resolved.paths.program_id == program_id
    ]


def _load_reconcile_scorecards(
    program_id: str,
    editions_root: Path,
    programs_root: Path,
) -> tuple[ScorecardSettings, ...]:
    selected = _resolve_primary_program_edition(program_id, editions_root=editions_root, programs_root=programs_root)
    return load_bundle_with_mode(
        selected.paths.edition_id,
        editions_root=editions_root,
        programs_root=programs_root,
    ).bundle.config.scorecards


def _current_issue_number(program_id: str, *, programs_root: Path) -> int | None:
    overrides = load_latest_program_overrides(program_id, programs_root=programs_root)
    return overrides.issue_number if overrides is not None else None


def _build_field_proposal_values(
    *,
    items: tuple[WorkItem, ...],
    workstreams: tuple[Workstream, ...],
    scorecards: tuple[Scorecard, ...],
    overrides_document,
    mapping_config: ADOFieldMappingConfig,
) -> dict[int, dict[str, ADOFieldProposalValue]]:
    mapped_vertex_fields = {mapping.vertex_field for mapping in mapping_config.mappings}
    risk_values = (
        _build_override_risk_values_by_item(items, scorecards, overrides_document)
        if "risk_level" in mapped_vertex_fields
        else {}
    )
    proposal_values: dict[int, dict[str, ADOFieldProposalValue]] = {}

    for item in items:
        item_values: dict[str, ADOFieldProposalValue] = {}
        for mapping in mapping_config.mappings:
            if mapping.vertex_field == "risk_level":
                override_risk = risk_values.get(item.id)
                if override_risk is not None:
                    item_values[mapping.vertex_field] = ADOFieldProposalValue(
                        value=override_risk,
                        reason="Sync risk_level from the latest Vertex override.",
                    )
                else:
                    item_values[mapping.vertex_field] = ADOFieldProposalValue(
                        value=item.risk_level.value,
                        reason="Sync risk_level from the current Vertex item state.",
                    )
                continue

            if mapping.vertex_field == "workstream_id":
                workstream_id = _workstream_id_for_item(item, workstreams)
                if workstream_id is None:
                    continue
                item_values[mapping.vertex_field] = ADOFieldProposalValue(
                    value=workstream_id,
                    reason="Sync workstream_id from Vertex area-path mapping.",
                )
                continue

            raise typer.BadParameter(
                f"Unsupported vertex_field '{mapping.vertex_field}' in ado_field_map.yaml. Supported fields: risk_level, workstream_id."
            )

        if item_values:
            proposal_values[item.id] = item_values

    return proposal_values


def _build_override_risk_values_by_item(
    items: tuple[WorkItem, ...],
    scorecards: tuple[Scorecard, ...],
    overrides_document,
) -> dict[int, str]:
    if overrides_document is None:
        return {}

    packet_map: dict[tuple[str, str], ScorecardEvidencePacket] = {}
    for scorecard in scorecards:
        for packet in build_scorecard(items, scorecard.dimensions, prev_confirmed=None, scorecard_name=scorecard.name):  # type: ignore[arg-type]
            packet_map[(scorecard.name, packet.dimension_name)] = packet

    risk_values: dict[int, str] = {}
    for scorecard in overrides_document.scorecards:
        for dimension in scorecard.dimensions:
            if dimension.risk is None:
                continue
            dim_packet = packet_map.get((scorecard.name, dimension.name))
            if dim_packet is None:
                continue
            for work_item_id in dim_packet.item_ids:
                risk_values[work_item_id] = dimension.risk.value
    return risk_values


def _workstream_id_for_item(item: WorkItem, workstreams: tuple[Workstream, ...]) -> str | None:
    for workstream in workstreams:
        if any(_area_path_matches(item.area_path, area_path) for area_path in workstream.area_paths):
            return workstream.id
    return None


def _recent_ado_update_item_ids(
    program_id: str,
    *,
    update_type: str,
    as_of: datetime,
    lookback_days: int,
    programs_root: Path,
) -> set[int]:
    start = as_of - timedelta(days=lookback_days)
    item_ids: set[int] = set()
    signal_store = build_signal_store_for_program_id(program_id, programs_root=programs_root)
    for signal in signal_store.read(program_id, start=start, end=as_of):
        if signal.source != "vertex/ado_update" or not isinstance(signal.metadata, dict):
            continue
        if str(signal.metadata.get("update_type") or "") != update_type:
            continue
        work_item_id = signal.metadata.get("work_item_id")
        if isinstance(work_item_id, int):
            item_ids.add(work_item_id)
            continue
        coerced = _coerce_int(work_item_id)
        if coerced is not None:
            item_ids.add(coerced)
    return item_ids


def _coerce_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None


def _count_latest_trajectory_updates(
    program_id: str,
    *,
    captured_at: datetime,
    programs_root: Path,
) -> int:
    trajectory_store = build_trajectory_store_for_program_id(program_id, programs_root=programs_root)
    captured_date = captured_at.date()
    return sum(
        1
        for work_item_id in trajectory_store.list_work_item_ids(program_id)
        if trajectory_store.read(
            program_id,
            work_item_id,
            start=captured_date,
            end=captured_date,
        )
    )


def _build_ado_client(program: Program) -> ADOClient:
    if program.ado is None:
        raise typer.BadParameter(f"Program '{program.id}' is missing ado configuration.")
    return ADOClient(
        organization=program.ado.organization,
        project=program.ado.project,
        timeout=program.ado.api_timeout_seconds,
    )


def _resolve_proposal_manifest(proposal_reference: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    try:
        return find_proposal_manifest(proposal_reference, programs_root=programs_root)
    except (FileNotFoundError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error


def _matching_promoted_signal_approval_rule(
    program_id: str,
    *,
    action_type: str,
    programs_root: Path,
) -> PromotedSignalApprovalRule | None:
    normalized_action_type = action_type.strip().lower()
    if not normalized_action_type:
        return None
    for rule in load_promoted_signal_approval_rules(program_id, programs_root=programs_root):
        if rule.proposal.action_type.strip().lower() == normalized_action_type:
            return rule
    return None


def _batch_approval_rule_for_action_type(
    program_id: str,
    *,
    action_type: str,
    programs_root: Path,
) -> PromotedSignalApprovalRule | None:
    rule = _matching_promoted_signal_approval_rule(
        program_id,
        action_type=action_type,
        programs_root=programs_root,
    )
    if rule is None or rule.proposal.recommended_mode != "batch_approval":
        return None
    return rule


def _should_record_ado_apply_audit(artifacts: ADOApplyArtifacts) -> bool:
    if artifacts.conflict_count or artifacts.failed_count:
        return artifacts.applied_count > 0 or artifacts.skipped_count > 0
    return artifacts.applied_count > 0 or artifacts.skipped_count > 0


def _record_ado_apply_audit(
    artifacts: ADOApplyArtifacts,
    *,
    author_alias: str,
    programs_root: Path,
) -> None:
    proposal = artifacts.proposal
    approval_rule = _matching_promoted_signal_approval_rule(
        proposal.program_id,
        action_type=proposal.update_type,
        programs_root=programs_root,
    )
    append_autonomy_audit_record(
        AutonomyAuditRecord(
            program_id=proposal.program_id,
            action_id=str(uuid4()),
            level=_ado_apply_audit_level(approval_rule),
            author_alias=author_alias,
            subject_alias=None,
            action_type=proposal.update_type,
            evidence_refs=_ado_proposal_evidence_refs(proposal),
            policy_rule=None if approval_rule is None else approval_rule.proposal.rule_id,
            accepted=True,
            applied_at=datetime.now(timezone.utc),
            blast_radius=(
                f"ADO {proposal.update_type} apply touched {artifacts.applied_count} work item(s); "
                f"{artifacts.skipped_count} skipped, {artifacts.conflict_count} conflict, {artifacts.failed_count} failed."
            ),
            rollback_mechanism=_ado_apply_rollback_mechanism(proposal.update_type),
            prior_acceptance_rate=compute_prior_acceptance_rate(
                proposal.program_id,
                action_type=proposal.update_type,
                programs_root=programs_root,
            ),
        ),
        programs_root=programs_root,
    )


def _record_ado_apply_declined_audit(
    proposal,
    *,
    author_alias: str,
    approval_rule: PromotedSignalApprovalRule | None,
    declined_at: datetime,
    programs_root: Path,
) -> None:
    append_autonomy_audit_record(
        AutonomyAuditRecord(
            program_id=proposal.program_id,
            action_id=str(uuid4()),
            level="l2",
            author_alias=author_alias,
            subject_alias=None,
            action_type=proposal.update_type,
            evidence_refs=_ado_proposal_evidence_refs(proposal),
            policy_rule=None if approval_rule is None else approval_rule.proposal.rule_id,
            accepted=False,
            applied_at=declined_at,
            blast_radius=(
                f"ADO {proposal.update_type} apply declined before any external writes; "
                f"{len(proposal.entries)} proposal entr{'y' if len(proposal.entries) == 1 else 'ies'} remained pending."
            ),
            rollback_mechanism="No rollback needed; proposal was not applied.",
            prior_acceptance_rate=compute_prior_acceptance_rate(
                proposal.program_id,
                action_type=proposal.update_type,
                programs_root=programs_root,
            ),
        ),
        programs_root=programs_root,
    )


def _ado_proposal_evidence_refs(proposal) -> tuple[str, ...]:
    refs = [f"ado_proposal:{proposal.id}"]
    if proposal.edition_id is not None:
        refs.append(f"edition:{proposal.edition_id}")
    if proposal.issue_number is not None:
        refs.append(f"confirmed_issue:{proposal.issue_number}")
    refs.extend(f"WI:{entry.work_item_id}" for entry in proposal.entries)
    return tuple(dict.fromkeys(refs))


def _ado_apply_rollback_mechanism(update_type: str) -> str:
    if update_type == "comment":
        return "Post a follow-up correction comment in ADO; comments are append-only."
    if update_type in {"field", "vitality_tag"}:
        return "Generate and apply a compensating ADO proposal to restore the prior field or tag values."
    if update_type == "vitality_nudge":
        return "Post a follow-up correction comment or regenerate a compensating vitality proposal before further nudges."
    return "Generate and apply a compensating ADO proposal after review."


def _ado_apply_audit_level(rule: PromotedSignalApprovalRule | None) -> str:
    if rule is not None and rule.proposal.recommended_mode == "batch_approval":
        return "l3"
    return "l2"


def _default_actor(value: str | None) -> str:
    if value is not None and value.strip():
        return value.strip()
    try:
        return getpass.getuser() or "unknown"
    except Exception:
        return "unknown"


def _apply_exit_code(artifacts: ADOApplyArtifacts) -> int:
    if artifacts.conflict_count or artifacts.failed_count:
        return 2
    return 0