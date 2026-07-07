from __future__ import annotations

from datetime import datetime, timezone
import getpass
from pathlib import Path
from uuid import uuid4

import typer

from src.core.analytics_store import AutonomyAuditRecord, append_autonomy_audit_record, compute_prior_acceptance_rate
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.feedback.signal_approval_learner import promote_signal_approval_rule


app = typer.Typer(help="Promote governed policy proposals into active local rules.", invoke_without_command=True)


@app.callback(invoke_without_command=True)
def policy_command(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(code=0)


@app.command("promote")
def promote_policy_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    rule: str = typer.Option(..., "--rule", help="Signal approval rule id to promote, for example approval:decision_ask_escalation."),
    updated_by: str | None = typer.Option(None, "--updated-by", help="Author alias for the policy promotion audit record."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview the promotion without writing the rule or audit record."),
) -> None:
    program_id = program.strip()
    rule_id = rule.strip()
    if not program_id:
        raise typer.BadParameter("--program is required.")
    if not rule_id:
        raise typer.BadParameter("--rule is required.")

    actor = _default_actor(updated_by)
    timestamp = _utc_now()
    try:
        promoted_rule, path = promote_signal_approval_rule(
            program_id,
            rule_id=rule_id,
            promoted_by=actor,
            as_of=timestamp,
            programs_root=PROGRAMS_ROOT,
            dry_run=dry_run,
        )
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error

    if dry_run:
        typer.echo(
            f"Dry-run: would promote {promoted_rule.proposal.rule_id} for {program_id} "
            f"({promoted_rule.proposal.action_type}, trust={promoted_rule.proposal.recommended_level})."
        )
        raise typer.Exit(code=0)

    append_autonomy_audit_record(
        AutonomyAuditRecord(
            program_id=program_id,
            action_id=str(uuid4()),
            level="l2",
            author_alias=actor,
            subject_alias=None,
            action_type="policy_promoted",
            evidence_refs=(
                f"signal_approval_rule:{promoted_rule.proposal.rule_id}",
                f"action_type:{promoted_rule.proposal.action_type}",
            ),
            policy_rule=promoted_rule.proposal.rule_id,
            accepted=True,
            applied_at=timestamp,
            blast_radius=(
                f"Local approval policy activation for {promoted_rule.proposal.action_type}; no external writes."
            ),
            rollback_mechanism=(
                "Remove the rule from signal_approval_rules.yaml rules and rebuild policy state before batch use."
            ),
            prior_acceptance_rate=compute_prior_acceptance_rate(
                program_id,
                action_type=promoted_rule.proposal.action_type,
                programs_root=PROGRAMS_ROOT,
            ),
        ),
        programs_root=PROGRAMS_ROOT,
    )

    typer.echo(
        f"Promoted {promoted_rule.proposal.rule_id} for {program_id} "
        f"({promoted_rule.proposal.action_type}, trust={promoted_rule.proposal.recommended_level})."
    )
    if path is not None:
        typer.echo(f"Policy file: {path}")


def _default_actor(value: str | None) -> str:
    if value is not None and value.strip():
        return value.strip()
    try:
        return getpass.getuser() or "unknown"
    except Exception:
        return "unknown"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)