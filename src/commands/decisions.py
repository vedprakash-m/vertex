from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

import typer

from src.commands.escalate import apply_decision_ask_escalation, plan_decision_ask_escalation, render_escalation_preview_plaintext
from src.commands.notify import build_decision_ask_nudge_preview, render_notify_preview_plaintext, write_decision_ask_nudge_emls
from src.core.ado_proposal import load_confirmed_issue_snapshot
from src.core.analytics_store import AutonomyAuditRecord, append_autonomy_audit_record, compute_prior_acceptance_rate
from src.core.ask_lifecycle import evaluate_decision_ask_lifecycle
from src.core.claim_tracker import PROGRAMS_ROOT, append_claim_status_update, load_open_decision_asks, locate_tracked_entry, touch_decision_ask
from src.core.decision_register import assess_proposed_decision_staleness, save_decisions, sort_decisions
from src.core.edition_resolver import resolve_edition
from src.core.exceptions import ConfigError, StateError
from src.core.incident_learning_synthesizer import IncidentRefPattern, build_incident_ref_patterns, normalize_incident_ref
from src.core.incident_journal_store import read_incident_entries
from src.core.models import NotifyPreview
from src.core.models_v2 import ClaimStatusUpdate, DecisionAsk, DecisionEntry, DecisionStatus, IncidentEntry
from src.core.program_fact_store import load_program_facts, project_decision_entries
from src.core.workstream_path_resolver import resolve_workstream_id_loose_longest as _resolve_workstream_id


from src.core.overrides_store import (
    GovernanceState,
    OverridesDocument,
    load_overrides,
    save_overrides,
    get_overrides_path,
)


governance_app = typer.Typer(help="Show and edit governance state (DFD, escalation) in issue overrides.")


@governance_app.command("show")
def governance_show(
    edition: str = typer.Option(..., "--edition", help="Edition name."),
    issue: int | None = typer.Option(None, "--issue", help="Issue number. Defaults to current draft."),
    reports_root: Path | None = typer.Option(None, "--reports-root", help="Override reports root path."),
) -> None:
    """Show governance state from an issue's overrides."""
    resolved_reports_root = reports_root or REPO_ROOT / "reports"
    overrides = load_overrides(edition, reports_root=resolved_reports_root, issue_number=issue)
    if overrides is None:
        typer.echo(f"No overrides found for {edition}" + (f" issue {issue:03d}" if issue else ""))
        raise typer.Exit(code=0)

    gov = overrides.governance
    typer.echo(f"GOVERNANCE STATE — {edition}" + (f" Issue {issue:03d}" if issue else ""))
    typer.echo(f"  DFD Date: {gov.dfd_date.isoformat() if gov.dfd_date else '(not set)'}")
    if gov.dfd_history:
        typer.echo(f"  DFD History: {', '.join(d.isoformat() for d in gov.dfd_history)}")
    typer.echo(f"  Escalation Active: {gov.escalation_active}")
    if gov.escalation_workstreams:
        typer.echo(f"  Escalation Workstreams: {', '.join(gov.escalation_workstreams)}")
    typer.echo(f"  LT Commitment: {gov.lt_commitment or '(none)'}")
    typer.echo(f"  LT Commitment Date: {gov.lt_commitment_date.isoformat() if gov.lt_commitment_date else '(none)'}")

    if overrides.decisions:
        typer.echo(f"\n  Decisions ({len(overrides.decisions)}):")
        for decision in overrides.decisions:
            typer.echo(f"    - [{decision.id}] {decision.type} | {decision.status} | {decision.statement}")

    raise typer.Exit(code=0)


@governance_app.command("edit")
def governance_edit(
    edition: str = typer.Option(..., "--edition", help="Edition name."),
    issue: int = typer.Option(..., "--issue", help="Issue number."),
    dfd_date: str | None = typer.Option(None, "--dfd-date", help="Set DFD date (YYYY-MM-DD)."),
    escalation_active: bool | None = typer.Option(None, "--escalation-active/--no-escalation-active", help="Set escalation active state."),
    lt_commitment: str | None = typer.Option(None, "--lt-commitment", help="Set LT commitment text."),
    lt_commitment_date: str | None = typer.Option(None, "--lt-commitment-date", help="Set LT commitment date (YYYY-MM-DD)."),
    reports_root: Path | None = typer.Option(None, "--reports-root", help="Override reports root path."),
) -> None:
    """Edit governance state in an issue's overrides YAML."""
    resolved_reports_root = reports_root or REPO_ROOT / "reports"
    overrides = load_overrides(edition, reports_root=resolved_reports_root, issue_number=issue)
    if overrides is None:
        typer.secho(f"No overrides found for {edition} issue {issue:03d}. Create overrides first.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    import dataclasses
    gov = overrides.governance
    updated_parts: list[str] = []

    if dfd_date is not None:
        try:
            new_date = date.fromisoformat(dfd_date.strip())
        except ValueError:
            raise typer.BadParameter("--dfd-date must be YYYY-MM-DD.")
        current_history = list(gov.dfd_history)
        if gov.dfd_date is not None and new_date != gov.dfd_date:
            current_history.append(gov.dfd_date)
        gov = dataclasses.replace(gov, dfd_date=new_date, dfd_history=tuple(current_history))
        updated_parts.append(f"dfd_date={new_date.isoformat()}")

    if escalation_active is not None:
        gov = dataclasses.replace(gov, escalation_active=escalation_active)
        updated_parts.append(f"escalation_active={escalation_active}")

    if lt_commitment is not None:
        gov = dataclasses.replace(gov, lt_commitment=lt_commitment.strip() or None)
        updated_parts.append("lt_commitment set")

    if lt_commitment_date is not None:
        try:
            lt_date = date.fromisoformat(lt_commitment_date.strip())
        except ValueError:
            raise typer.BadParameter("--lt-commitment-date must be YYYY-MM-DD.")
        gov = dataclasses.replace(gov, lt_commitment_date=lt_date)
        updated_parts.append(f"lt_commitment_date={lt_date.isoformat()}")

    if not updated_parts:
        typer.secho("No changes specified. Use --dfd-date, --escalation-active, --lt-commitment, or --lt-commitment-date.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=0)

    updated_overrides = dataclasses.replace(overrides, governance=gov)
    save_overrides(edition, updated_overrides, reports_root=resolved_reports_root)

    typer.secho(f"Updated governance for {edition} issue {issue:03d}: {', '.join(updated_parts)}", fg=typer.colors.GREEN)
    raise typer.Exit(code=0)


REPO_ROOT = Path(__file__).resolve().parents[2]

app = typer.Typer(help="Manage the program decision register.", invoke_without_command=True)
# Register governance subcommand after app is defined
app.add_typer(governance_app, name="governance", invoke_without_command=True)


@dataclass(frozen=True, slots=True)
class DecisionAskNudgePlan:
    ask: DecisionAsk
    preview: NotifyPreview
    context_note: str | None = None


@app.callback(invoke_without_command=True)
def decisions_command(
    ctx: typer.Context,
    program: str | None = typer.Option(None, "--program", help="Program id, e.g. myprogram."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if program is None or not program.strip():
        raise typer.BadParameter("--program is required.")
    _print_decisions(program.strip(), status_filter=None, format=format)
    raise typer.Exit(code=0)


@app.command("list")
def list_decisions_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    status: str | None = typer.Option(None, "--status", help="Optional status filter."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    status_filter = DecisionStatus.from_string(status) if status is not None and status.strip() else None
    _print_decisions(program.strip(), status_filter=status_filter, format=format)
    raise typer.Exit(code=0)


@app.command("aging")
def aging_decisions_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    min_age_days: int = typer.Option(14, "--min-age-days", help="Minimum inactive age to include in the decision debt report."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
    apply: bool = typer.Option(False, "--apply", help="Write follow-up drafts for due decision asks in the aging report."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview the follow-up drafts that --apply would write without creating files."),
) -> None:
    if min_age_days < 0:
        raise typer.BadParameter("--min-age-days must be zero or greater.")
    if dry_run and not apply:
        raise typer.BadParameter("--dry-run requires --apply.")
    if apply and format != "human":
        raise typer.BadParameter("--apply currently supports only '--format human'.")
    if apply:
        _apply_decision_aging(program.strip(), min_age_days=min_age_days, dry_run=dry_run)
        raise typer.Exit(code=0)
    _print_decision_aging(program.strip(), min_age_days=min_age_days, format=format)
    raise typer.Exit(code=0)


@app.command("nudge")
def nudge_decision_ask_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    decision_ask_id: str = typer.Option(..., "--id", help="Decision ask id."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview the nudge draft without writing an EML file."),
) -> None:
    program_id = program.strip()
    try:
        plan = plan_decision_ask_nudge(
            program_id=program_id,
            decision_ask_id=decision_ask_id.strip(),
            programs_root=PROGRAMS_ROOT,
        )
    except StateError as error:
        typer.echo(str(error))
        raise typer.Exit(code=2)

    typer.echo(render_notify_preview_plaintext((plan.preview,)))
    if dry_run:
        typer.echo("Dry run: no nudge draft written.")
        raise typer.Exit(code=0)

    if not typer.confirm(f"Write decision-ask nudge draft EML(s) for {plan.ask.id}?", default=True):
        _record_decision_ask_nudge_declined_audit(
            plan.ask,
            author_alias=_default_actor(None),
            declined_at=datetime.now(timezone.utc),
            recipient_count=len(plan.preview.to),
            programs_root=PROGRAMS_ROOT,
            context_note=plan.context_note,
        )
        raise typer.Exit(code=1)

    eml_paths = apply_decision_ask_nudge(
        plan,
        programs_root=PROGRAMS_ROOT,
        generated_at=datetime.now(timezone.utc),
    )
    for eml_path in eml_paths:
        typer.echo(f"EML: {eml_path}")
    typer.echo(f"Wrote {len(eml_paths)} decision-ask nudge draft EML(s). Send manually via Outlook.")
    raise typer.Exit(code=0)


def plan_decision_ask_nudge(
    *,
    program_id: str,
    decision_ask_id: str,
    programs_root: Path = PROGRAMS_ROOT,
    context_note: str | None = None,
) -> DecisionAskNudgePlan:
    located = locate_tracked_entry(decision_ask_id.strip(), program_id=program_id, programs_root=programs_root)
    if located is None or not isinstance(located.entry, DecisionAsk):
        raise typer.BadParameter(f"Decision ask '{decision_ask_id}' was not found in {program_id}.")
    if located.effective_status != "open":
        raise typer.BadParameter(f"Decision ask '{decision_ask_id}' is not open in {program_id}.")
    preview = build_decision_ask_nudge_preview(
        ask=located.entry,
        programs_root=programs_root,
        context_note=context_note,
    )
    return DecisionAskNudgePlan(ask=located.entry, preview=preview, context_note=context_note)


def apply_decision_ask_nudge(
    plan: DecisionAskNudgePlan,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    generated_at: datetime | None = None,
    updated_by: str | None = None,
) -> tuple[Path, ...]:
    current_time = generated_at or datetime.now(timezone.utc)
    author_alias = _default_actor(updated_by)
    eml_paths = write_decision_ask_nudge_emls(
        ask=plan.ask,
        previews=(plan.preview,),
        programs_root=programs_root,
        generated_at=current_time,
    )
    touch_decision_ask(
        program_id=plan.ask.program_id,
        decision_ask_id=plan.ask.id,
        updated_at=current_time,
        updated_by=author_alias,
        note="Decision ask touched by nudge draft.",
        programs_root=programs_root,
    )
    append_autonomy_audit_record(
        AutonomyAuditRecord(
            program_id=plan.ask.program_id,
            action_id=str(uuid4()),
            level="l2",
            author_alias=author_alias,
            subject_alias=plan.ask.owner_alias,
            action_type="decision_ask_nudge",
            evidence_refs=_decision_ask_audit_refs(plan.ask, context_note=plan.context_note),
            policy_rule="decision_ask_nudge",
            accepted=True,
            applied_at=current_time,
            blast_radius=f"{len(eml_paths)} local EML draft(s) for {len(plan.preview.to)} recipient(s)",
            rollback_mechanism="Delete the draft EML and defer or resolve the ask before sending.",
            prior_acceptance_rate=compute_prior_acceptance_rate(
                plan.ask.program_id,
                action_type="decision_ask_nudge",
                programs_root=programs_root,
            ),
        ),
        programs_root=programs_root,
    )
    return eml_paths


def _record_decision_ask_nudge_declined_audit(
    ask: DecisionAsk,
    *,
    author_alias: str,
    declined_at: datetime,
    recipient_count: int,
    programs_root: Path,
    context_note: str | None = None,
) -> None:
    append_autonomy_audit_record(
        AutonomyAuditRecord(
            program_id=ask.program_id,
            action_id=str(uuid4()),
            level="l2",
            author_alias=author_alias,
            subject_alias=ask.owner_alias,
            action_type="decision_ask_nudge",
            evidence_refs=_decision_ask_audit_refs(ask, context_note=context_note),
            policy_rule="decision_ask_nudge",
            accepted=False,
            applied_at=declined_at,
            blast_radius=(
                f"Decision-ask nudge draft declined before any local EML writes; "
                f"{recipient_count} intended recipient(s)."
            ),
            rollback_mechanism="No rollback needed; nudge draft was not written.",
            prior_acceptance_rate=compute_prior_acceptance_rate(
                ask.program_id,
                action_type="decision_ask_nudge",
                programs_root=programs_root,
            ),
        ),
        programs_root=programs_root,
    )


def _decision_ask_audit_refs(ask: DecisionAsk, *, context_note: str | None = None) -> tuple[str, ...]:
    refs = list(ask.entity_refs)
    refs.extend(f"workstream:{workstream_id}" for workstream_id in _decision_ask_workstream_ids(ask))
    refs.extend(_incident_audit_refs_from_context(context_note))
    refs.append(f"decision_ask:{ask.id}")
    return tuple(dict.fromkeys(refs))


def _decision_ask_workstream_ids(ask: DecisionAsk) -> tuple[str, ...]:
    try:
        resolved = resolve_edition(ask.edition_id, programs_root=PROGRAMS_ROOT)
        snapshot = load_confirmed_issue_snapshot(ask.edition_id, ask.issue_number)
    except (ConfigError, FileNotFoundError, ValueError):
        return ()
    if resolved is None:
        return ()

    item_lookup = {item.id: item for item in snapshot.items}
    workstream_ids: list[str] = []
    for ref in ask.entity_refs:
        work_item_id = _work_item_id_from_ref(ref)
        if work_item_id is None:
            continue
        item = item_lookup.get(work_item_id)
        if item is None:
            continue
        workstream_id = _resolve_workstream_id(item.area_path, resolved.workstreams)
        if workstream_id is not None:
            workstream_ids.append(workstream_id)
    return tuple(dict.fromkeys(workstream_ids))


def _work_item_id_from_ref(ref: str) -> int | None:
    if not ref.upper().startswith("WI:"):
        return None
    value = ref.split(":", 1)[1]
    if not value.isdigit():
        return None
    return int(value)


def _incident_audit_refs_from_context(context_note: str | None) -> tuple[str, ...]:
    if context_note is None or not context_note.strip():
        return ()
    incident_ids = tuple(dict.fromkeys(match.group(1) for match in re.finditer(r"\bIcM\s+(\d+)\b", context_note, flags=re.IGNORECASE)))
    return tuple(f"ICM:{incident_id}" for incident_id in incident_ids)


@app.command("add")
def add_decision_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    title: str = typer.Option(..., "--title", help="Decision title."),
    context: str = typer.Option(..., "--context", help="Decision context or problem statement."),
    decision: str = typer.Option(..., "--decision", help="Decision outcome or proposed choice."),
    status: str = typer.Option("decided", "--status", help="Status: proposed|decided|superseded|reverted."),
    rationale: str | None = typer.Option(None, "--rationale", help="Optional rationale."),
    alternative: list[str] | None = typer.Option(None, "--alternative", help="Repeat to add alternatives considered."),
    decided_by: str | None = typer.Option(None, "--decided-by", help="Decision owner alias. Defaults to current OS user."),
    decision_date: str | None = typer.Option(None, "--decision-date", help="Optional YYYY-MM-DD decision date. Defaults to today."),
    review_by: str | None = typer.Option(None, "--review-by", help="Optional YYYY-MM-DD review date for this decision."),
    linked_claim: str | None = typer.Option(None, "--linked-claim", help="Optional linked decision-ask id."),
    linked_risk: str | None = typer.Option(None, "--linked-risk", help="Optional linked risk id."),
    linked_action: list[str] | None = typer.Option(None, "--linked-action", help="Repeat to link action ids."),
    workstream: str | None = typer.Option(None, "--workstream", help="Optional workstream id."),
    entity_ref: list[str] | None = typer.Option(None, "--entity-ref", help="Repeat to add entity refs."),
) -> None:
    program_id = program.strip()
    linked_claim_id, linked_claim_status = _resolve_linked_decision_ask(program_id, linked_claim)
    entry = DecisionEntry(
        id=str(uuid4()),
        program_id=program_id,
        title=title.strip(),
        context=context.strip(),
        decision=decision.strip(),
        rationale=(rationale.strip() if rationale is not None and rationale.strip() else None),
        alternatives_considered=tuple(item.strip() for item in alternative or [] if item.strip()),
        decided_by=_default_actor(decided_by),
        decision_date=_parse_optional_date(decision_date) or datetime.now(timezone.utc).date(),
        status=DecisionStatus.from_string(status),
        superseded_by=None,
        linked_claim_id=linked_claim_id,
        linked_risk_id=(linked_risk.strip() if linked_risk is not None and linked_risk.strip() else None),
        linked_action_ids=tuple(item.strip() for item in linked_action or [] if item.strip()),
        workstream_id=(workstream.strip() if workstream is not None and workstream.strip() else None),
        entity_refs=tuple(item.strip() for item in entity_ref or [] if item.strip()),
        review_by=_parse_optional_date(review_by),
    )
    if not entry.title:
        raise typer.BadParameter("--title is required.")
    if not entry.context:
        raise typer.BadParameter("--context is required.")
    if not entry.decision:
        raise typer.BadParameter("--decision is required.")

    entries = list(_load_current_decisions(program_id))
    entries.append(entry)
    save_decisions(program_id, _sort_decisions(tuple(entries)), programs_root=PROGRAMS_ROOT)
    _resolve_linked_claim_if_needed(program_id, entry, linked_claim_status=linked_claim_status, reviewer=entry.decided_by)  # type: ignore[arg-type]
    typer.echo(f"Added decision {entry.id} to {program_id}.")
    raise typer.Exit(code=0)


@app.command("resolve")
def resolve_decision_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    decision_id: str = typer.Option(..., "--id", help="Decision id."),
    decision: str | None = typer.Option(None, "--decision", help="Optional updated decision text."),
    rationale: str | None = typer.Option(None, "--rationale", help="Optional updated rationale."),
    decided_by: str | None = typer.Option(None, "--decided-by", help="Decision owner alias. Defaults to current OS user."),
    decision_date: str | None = typer.Option(None, "--decision-date", help="Optional YYYY-MM-DD decision date. Defaults to today."),
    review_by: str | None = typer.Option(None, "--review-by", help="Optional YYYY-MM-DD review date for this decision."),
) -> None:
    program_id = program.strip()
    entries = list(_load_current_decisions(program_id))
    match_index = next((index for index, entry in enumerate(entries) if entry.id == decision_id.strip()), None)
    if match_index is None:
        raise typer.BadParameter(f"Decision '{decision_id}' was not found in {program_id}.")

    current = entries[match_index]
    updated = replace(
        current,
        decision=(decision.strip() if decision is not None and decision.strip() else current.decision),
        rationale=(rationale.strip() if rationale is not None and rationale.strip() else current.rationale),
        decided_by=_default_actor(decided_by) if decided_by is not None else current.decided_by,
        decision_date=_parse_optional_date(decision_date) or datetime.now(timezone.utc).date(),
        status=DecisionStatus.DECIDED,
        review_by=_parse_optional_date(review_by) if review_by is not None else current.review_by,
    )
    if updated == current:
        typer.echo(f"Decision {current.id} is unchanged.")
        raise typer.Exit(code=0)

    entries[match_index] = updated
    save_decisions(program_id, _sort_decisions(tuple(entries)), programs_root=PROGRAMS_ROOT)
    linked_claim_status = _resolve_linked_claim_status(program_id, updated.linked_claim_id)
    _resolve_linked_claim_if_needed(program_id, updated, linked_claim_status=linked_claim_status, reviewer=updated.decided_by)  # type: ignore[arg-type]
    typer.echo(f"Resolved decision {updated.id} in {program_id}.")
    raise typer.Exit(code=0)


@app.command("supersede")
def supersede_decision_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    decision_id: str = typer.Option(..., "--id", help="Decision id."),
    superseded_by: str = typer.Option(..., "--superseded-by", help="Replacement decision id."),
) -> None:
    program_id = program.strip()
    entries = list(_load_current_decisions(program_id))
    match_index = next((index for index, entry in enumerate(entries) if entry.id == decision_id.strip()), None)
    if match_index is None:
        raise typer.BadParameter(f"Decision '{decision_id}' was not found in {program_id}.")
    if not any(entry.id == superseded_by.strip() for entry in entries):
        raise typer.BadParameter(f"Decision '{superseded_by}' was not found in {program_id}.")

    current = entries[match_index]
    updated = replace(current, status=DecisionStatus.SUPERSEDED, superseded_by=superseded_by.strip())
    entries[match_index] = updated
    save_decisions(program_id, _sort_decisions(tuple(entries)), programs_root=PROGRAMS_ROOT)
    typer.echo(f"Superseded decision {updated.id} in {program_id}.")
    raise typer.Exit(code=0)


def _print_decisions(program_id: str, *, status_filter: DecisionStatus | None, format: str) -> None:
    entries = _load_current_decisions(program_id)
    if status_filter is not None:
        entries = tuple(entry for entry in entries if entry.status == status_filter)
    today = datetime.now(timezone.utc).date()
    sorted_entries = _sort_decisions(entries)
    if format == "json":
        typer.echo(
            json.dumps(
                {
                    "program_id": program_id,
                    "status_filter": status_filter.value if status_filter is not None else None,
                    "decisions": [
                        asdict(entry)
                        | {
                            "staleness": ("stale" if assess_proposed_decision_staleness(entry, today) else "current"),
                        }
                        for entry in sorted_entries
                    ],
                },
                indent=2,
                sort_keys=True,
                default=_json_default,
            )
        )
        return
    if format == "csv":
        typer.echo(render_decisions_csv(sorted_entries, as_of=today), nl=False)
        return
    if format != "human":
        raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")
    if not sorted_entries:
        typer.echo(f"No decisions found for {program_id}.")
        return

    typer.echo(f"DECISION REGISTER - {program_id} ({len(sorted_entries)})")
    for entry in sorted_entries:
        stale_label = "stale" if assess_proposed_decision_staleness(entry, today) else "current"
        typer.echo(
            f"- {entry.id} | {entry.status.value} | {stale_label} | {entry.decision_date.isoformat()} | {entry.title}"  # type: ignore[union-attr]
        )
        typer.echo(f"  {entry.decision}")


def _print_decision_aging(program_id: str, *, min_age_days: int, format: str) -> None:
    rows = _build_decision_aging_rows(program_id, as_of=datetime.now(timezone.utc).date(), min_age_days=min_age_days)
    if format == "json":
        typer.echo(
            json.dumps(
                {
                    "program_id": program_id,
                    "min_age_days": min_age_days,
                    "decision_debt": rows,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    if format == "csv":
        typer.echo(render_decision_aging_csv(rows), nl=False)
        return
    if format != "human":
        raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")
    if not rows:
        typer.echo(f"No open decision asks at or above {min_age_days} day(s) for {program_id}.")
        return

    typer.echo(f"DECISION DEBT - {program_id} ({len(rows)})")
    for row in rows:
        typer.echo(
            f"- {row['id']} | {row['lifecycle_stage']} | {row['age_days']} day(s) open | issue #{row['issue_number']:03d} | owner {row['owner_alias']}"
        )
        typer.echo(f"  {row['text']}")
        if row["incident_summary"]:
            typer.echo(f"  Incident-linked: {row['incident_summary']}")
        if row["command"]:
            typer.echo(f"  Approve: {row['command']}")


def _apply_decision_aging(program_id: str, *, min_age_days: int, dry_run: bool) -> None:
    rows = _build_decision_aging_rows(program_id, as_of=datetime.now(timezone.utc).date(), min_age_days=min_age_days)
    _render_decision_aging_rows(program_id, rows, min_age_days=min_age_days)
    actionable_rows = tuple(
        row
        for row in rows
        if str(row.get("lifecycle_stage")) in {"nudge", "escalate"}
    )
    if not actionable_rows:
        typer.echo(f"No decision-ask nudge or escalation follow-ups at or above {min_age_days} day(s) for {program_id}.")
        return

    if not dry_run and not typer.confirm(
        f"Write {len(actionable_rows)} decision-ask follow-up draft(s) for {program_id}?",
        default=True,
    ):
        raise typer.Exit(code=1)

    for line in _materialize_decision_ask_followups(program_id, actionable_rows=actionable_rows, dry_run=dry_run):
        typer.echo(line)
    if dry_run:
        typer.echo(f"Dry run: would write {len(actionable_rows)} decision-ask follow-up draft(s).")
        return
    typer.echo(f"Wrote {len(actionable_rows)} decision-ask follow-up draft(s).")


def _render_decision_aging_rows(program_id: str, rows: list[dict[str, object]], *, min_age_days: int) -> None:
    if not rows:
        typer.echo(f"No open decision asks at or above {min_age_days} day(s) for {program_id}.")
        return

    typer.echo(f"DECISION DEBT - {program_id} ({len(rows)})")
    for row in rows:
        typer.echo(
            f"- {row['id']} | {row['lifecycle_stage']} | {row['age_days']} day(s) open | issue #{row['issue_number']:03d} | owner {row['owner_alias']}"
        )
        typer.echo(f"  {row['text']}")
        if row["incident_summary"]:
            typer.echo(f"  Incident-linked: {row['incident_summary']}")
        if row["command"]:
            typer.echo(f"  Approve: {row['command']}")


def _materialize_decision_ask_followups(
    program_id: str,
    *,
    actionable_rows: tuple[dict[str, object], ...],
    dry_run: bool,
) -> tuple[str, ...]:
    current_time = datetime.now(timezone.utc)
    output_lines: list[str] = []
    for row in actionable_rows:
        decision_ask_id = str(row["id"])
        stage = str(row["lifecycle_stage"])
        incident_summary = row.get("incident_summary")
        context_note = str(incident_summary) if isinstance(incident_summary, str) and incident_summary.strip() else None
        if stage == "nudge":
            nudge_plan = plan_decision_ask_nudge(
                program_id=program_id,
                decision_ask_id=decision_ask_id,
                programs_root=PROGRAMS_ROOT,
                context_note=context_note,
            )
            if dry_run:
                output_lines.append(render_notify_preview_plaintext((nudge_plan.preview,)))
                output_lines.append(f"Dry run: would write a decision-ask nudge draft for {decision_ask_id}.")
                continue
            eml_paths = apply_decision_ask_nudge(
                nudge_plan,
                programs_root=PROGRAMS_ROOT,
                generated_at=current_time,
            )
            output_lines.extend(f"EML: {path}" for path in eml_paths)
            continue

        if stage == "escalate":
            escalation_plan = plan_decision_ask_escalation(
                edition_name=str(row["edition_id"]),
                decision_ask_id=decision_ask_id,
            )
            if dry_run:
                output_lines.append(render_escalation_preview_plaintext(escalation_plan.artifacts))
                output_lines.append(f"Dry run: would write an escalation draft for {decision_ask_id}.")
                continue
            artifacts = apply_decision_ask_escalation(
                escalation_plan,
                generated_at=current_time,
            )
            output_lines.extend(f"EML: {path}" for path in artifacts.eml_paths)
    return tuple(output_lines)


def _build_decision_aging_rows(program_id: str, *, as_of: date, min_age_days: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    as_of_datetime = datetime.combine(as_of, datetime.min.time(), tzinfo=timezone.utc)
    incident_patterns = _build_incident_patterns(
        read_incident_entries(
            program_id,
            start=as_of_datetime - timedelta(days=14),
            end=as_of_datetime,
            programs_root=PROGRAMS_ROOT,
        )
    )
    for ask in load_open_decision_asks(program_id, programs_root=PROGRAMS_ROOT):
        proposal = evaluate_decision_ask_lifecycle(
            ask,
            as_of=as_of_datetime,
        )
        if proposal is None or proposal.inactive_days < min_age_days:
            continue
        related_incident_patterns = _related_incident_patterns_for_ask(ask, incident_patterns)
        incident_refs = []
        if related_incident_patterns:
            incident_refs = [incident_ref for pattern in related_incident_patterns for incident_ref in pattern.incident_refs]
        rows.append(
            {
                "age_days": proposal.age_days,
                "ask_date": ask.ask_date.isoformat(),
                "command": proposal.command,
                "edition_id": ask.edition_id,
                "entity_refs": list(ask.entity_refs),
                "id": ask.id,
                "inactive_days": proposal.inactive_days,
                "incident_refs": list(dict.fromkeys(incident_refs)),
                "incident_summary": (_render_incident_pattern_evidence(related_incident_patterns[0]) if related_incident_patterns else None),
                "issue_number": ask.issue_number,
                "lifecycle_stage": proposal.stage.value,
                "owner_alias": ask.owner_alias,
                "program_id": ask.program_id,
                "text": ask.text,
            }
        )
    rows.sort(
        key=lambda row: (
            -int(row["inactive_days"]),
            -int(row["age_days"]),
            int(row["issue_number"]),
            str(row["id"]),
        )
    )
    return rows


def _load_current_decisions(program_id: str) -> tuple[DecisionEntry, ...]:
    return project_decision_entries(
        load_program_facts(program_id, programs_root=PROGRAMS_ROOT)
    )


def _sort_decisions(entries: tuple[DecisionEntry, ...]) -> tuple[DecisionEntry, ...]:
    return sort_decisions(entries)


def _resolve_linked_decision_ask(program_id: str, linked_claim: str | None) -> tuple[str | None, str | None]:
    if linked_claim is None or not linked_claim.strip():
        return None, None
    located = locate_tracked_entry(linked_claim.strip(), program_id=program_id, programs_root=PROGRAMS_ROOT)
    if located is None or not isinstance(located.entry, DecisionAsk):
        raise typer.BadParameter(f"Linked decision ask '{linked_claim}' was not found in {program_id}.")
    return located.entry.id, located.effective_status


def _resolve_linked_claim_status(program_id: str, linked_claim_id: str | None) -> str | None:
    if linked_claim_id is None:
        return None
    located = locate_tracked_entry(linked_claim_id, program_id=program_id, programs_root=PROGRAMS_ROOT)
    if located is None or not isinstance(located.entry, DecisionAsk):
        return None
    return located.effective_status


def _resolve_linked_claim_if_needed(
    program_id: str,
    entry: DecisionEntry,
    *,
    linked_claim_status: str | None,
    reviewer: str,
) -> None:
    if entry.linked_claim_id is None or entry.status is DecisionStatus.PROPOSED:
        return
    if linked_claim_status == "resolved":
        return
    append_claim_status_update(
        program_id,
        ClaimStatusUpdate(
            claim_id=entry.linked_claim_id,
            new_status="resolved",
            updated_at=datetime.now(timezone.utc),
            updated_by=reviewer,
            note=f"Resolved by decision {entry.id}.",
        ),
        programs_root=PROGRAMS_ROOT,
    )


def _parse_optional_date(value: str | None) -> date | None:
    if value is None or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError as error:
        raise typer.BadParameter("Dates must use YYYY-MM-DD.") from error


def _default_actor(actor: str | None) -> str:
    if actor is not None and actor.strip():
        return actor.strip()
    return (os.environ.get("USERNAME") or os.environ.get("USER") or "manual").strip() or "manual"


def render_decisions_csv(entries: tuple[DecisionEntry, ...], *, as_of: date) -> str:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        (
            "id",
            "program_id",
            "title",
            "status",
            "staleness",
            "decision_date",
            "decided_by",
            "workstream_id",
            "linked_claim_id",
            "linked_risk_id",
            "linked_action_ids",
            "superseded_by",
            "entity_refs",
            "decision",
            "rationale",
            "alternatives_considered",
            "context",
        )
    )
    for entry in entries:
        writer.writerow(
            (
                entry.id,
                entry.program_id,
                entry.title,
                entry.status.value,
                "stale" if assess_proposed_decision_staleness(entry, as_of) else "current",
                entry.decision_date.isoformat(),  # type: ignore[union-attr]
                entry.decided_by,
                entry.workstream_id or "",
                entry.linked_claim_id or "",
                entry.linked_risk_id or "",
                "|".join(entry.linked_action_ids),
                entry.superseded_by or "",
                "|".join(entry.entity_refs),
                entry.decision,
                entry.rationale or "",
                "|".join(entry.alternatives_considered),
                entry.context,
            )
        )
    return buffer.getvalue()


def render_decision_aging_csv(rows: list[dict[str, Any]]) -> str:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(("id", "program_id", "edition_id", "issue_number", "age_days", "inactive_days", "lifecycle_stage", "ask_date", "owner_alias", "entity_refs", "incident_refs", "incident_summary", "command", "text"))
    for row in rows:
        writer.writerow(
            (
                row["id"],
                row["program_id"],
                row["edition_id"],
                row["issue_number"],
                row["age_days"],
                row["inactive_days"],
                row["lifecycle_stage"],
                row["ask_date"],
                row["owner_alias"],
                "|".join(str(ref) for ref in row["entity_refs"]),
                "|".join(str(ref) for ref in row["incident_refs"]),
                row["incident_summary"] or "",
                row["command"],
                row["text"],
            )
        )
    return buffer.getvalue()


def _build_incident_patterns(entries: tuple[IncidentEntry, ...]) -> tuple[IncidentRefPattern, ...]:
    return build_incident_ref_patterns(entries)


def _related_incident_patterns_for_ask(
    ask: DecisionAsk,
    patterns: tuple[IncidentRefPattern, ...],
) -> tuple[IncidentRefPattern, ...]:
    if not ask.entity_refs:
        return ()
    ask_refs = {normalize_incident_ref(ref) for ref in ask.entity_refs if normalize_incident_ref(ref)}
    if not ask_refs:
        return ()
    return tuple(pattern for pattern in patterns if pattern.ref in ask_refs)


def _render_incident_pattern_evidence(pattern: IncidentRefPattern) -> str:
    incident_refs = ", ".join(str(ref) for ref in pattern.incident_refs)
    if pattern.entry_count == 1:
        return f"{pattern.ref}: {pattern.summary_text}. Source: {incident_refs}. ({pattern.confidence.value.lower()} confidence)"
    return (
        f"{pattern.ref}: repeated across {pattern.entry_count} incident learnings. {pattern.summary_text}. "
        f"Source: {incident_refs}. ({pattern.confidence.value.lower()} confidence)"
    )


def _json_default(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


# ---------------------------------------------------------------------------
# WI-3.11: vertex decisions link-outcome (§6.2.8)
# ---------------------------------------------------------------------------

@app.command("link-outcome")
def decisions_link_outcome_command(
    program: str = typer.Option(..., "--program", help="Program id."),
    decision_id: str = typer.Option(..., "--decision-id", help="Natural key or fact_id of the decision.entry to link."),
    assumption: str = typer.Option(..., "--assumption", help="Natural key of the assumption.entry stating the testable premise."),
    db_root: Path | None = typer.Option(None, hidden=True),
    programs_root: Path | None = typer.Option(None, hidden=True),
) -> None:
    """Link a decision to a testable assumption premise (§6.2.8).

    Sets `expected_outcome_refs` on the decision.entry fact so that
    DECISION_OUTCOME_DRIFT attention fires when the assumption becomes
    disputed or stale.

    Note: numeric/metric assumptions are evaluated via the §6.2.3 metric
    digest. Free-text assumptions drift only via human dispute in triage.
    """
    from src.core.program_fact_store import (
        ProgramFactStore,
        ProgramFactInput,
        FactPrecedence,
        FactReviewState,
        FactLifecycleState,
        load_program_facts,
    )

    normalized_program = program.strip()
    if not normalized_program:
        raise typer.BadParameter("--program must be non-empty")
    normalized_decision_id = decision_id.strip()
    normalized_assumption = assumption.strip()
    if not normalized_decision_id:
        raise typer.BadParameter("--decision-id must be non-empty")
    if not normalized_assumption:
        raise typer.BadParameter("--assumption must be non-empty")

    db_root_resolved = db_root or (programs_root.parent if programs_root else None)
    snapshot = load_program_facts(normalized_program, programs_root=programs_root, db_root=db_root_resolved)

    # Find the target decision.entry fact
    target_fact = None
    for fact in snapshot.facts:
        if fact.fact_type != "decision.entry":
            continue
        if fact.fact_id == normalized_decision_id or fact.natural_key == normalized_decision_id:
            target_fact = fact
            break

    if target_fact is None:
        typer.secho(
            f"No decision.entry found with id or natural_key '{normalized_decision_id}'.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    # Check assumption exists
    assumption_exists = any(
        f.natural_key == normalized_assumption and f.fact_type == "assumption.entry"
        for f in snapshot.facts
    )
    if not assumption_exists:
        typer.secho(
            f"No assumption.entry found with natural_key '{normalized_assumption}'. "
            f"The link will be stored but DECISION_OUTCOME_DRIFT won't fire until "
            f"the assumption is created. (Free-text assumptions drift only via human dispute in triage.)",
            fg=typer.colors.YELLOW,
        )

    # Update the payload
    new_payload = dict(target_fact.payload)
    existing_refs = list(new_payload.get("expected_outcome_refs") or [])
    if normalized_assumption not in existing_refs:
        existing_refs.append(normalized_assumption)
    new_payload["expected_outcome_refs"] = existing_refs

    # Write via append_fact (idempotent update)
    store = ProgramFactStore(normalized_program, db_root=db_root_resolved)
    result = store.append_fact(
        ProgramFactInput(
            fact_type="decision.entry",
            entity_refs=target_fact.entity_refs,
            payload=new_payload,
            scope=target_fact.scope,
            source_signal_ids=target_fact.source_signal_ids,
            confidence=target_fact.confidence,
            precedence=FactPrecedence.ACTIVE_PM_JUDGMENT,
            review_state=FactReviewState.ACCEPTED,
            lifecycle_state=FactLifecycleState.ACTIVE,
            natural_key=target_fact.natural_key,
            created_by="vertex/decisions_link_outcome",
        )
    )
    typer.echo(
        f"Linked decision '{normalized_decision_id}' → assumption '{normalized_assumption}' "
        f"(action={result.action}). "
        f"expected_outcome_refs now: {existing_refs}"
    )
    raise typer.Exit(code=0)