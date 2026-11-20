"""WI-7.2: vertex actuate — governed actuation surface (§6.11).

Subcommands:
  vertex actuate review  --program <prog>  [--dry-run]
  vertex actuate execute --program <prog>  --proposal-id <id>

INV-12: execute is unreachable unless the proposal is human-approved
(approved=True on the action.proposal fact).  No auto-execute tier exists.

Kill switch: actuation.enabled in actuation_rules.yaml (global or per-program).
CP-7: operator reviews dry-run payloads before enabling any rule.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from src.core.actuation_engine import ActuationPolicy
    from src.core.integration_protocol import ActuationResult
    from src.core.program_reality import ActuationProposal

app = typer.Typer(help="Governed actuation — review proposals and execute approved ones.")


def _get_programs_root() -> Path:
    from src.core.program_fact_store import PROGRAMS_ROOT
    return PROGRAMS_ROOT


@app.command("review")
def actuate_review(
    program: str = typer.Option(..., "--program", "-p", help="Program ID to review proposals for."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be executed without writing."),
    programs_root: Optional[Path] = typer.Option(None, hidden=True),
) -> None:
    """Show pending actuation proposals for a program.

    Proposals are derived from actuation_engine.derive_proposals() against the
    current ProgramReality. Expired, executed, and terminally-failed proposals
    are excluded.
    """
    from src.core.program_reality import ProgramReality
    from src.core.actuation_engine import load_actuation_policy, derive_proposals

    resolved_programs_root = programs_root or _get_programs_root()

    try:
        reality = ProgramReality.load(program, programs_root=resolved_programs_root)
    except Exception as exc:
        typer.echo(f"[actuate review] ERROR: could not load reality for {program!r}: {exc}", err=True)
        raise typer.Exit(code=1)

    try:
        policy = load_actuation_policy(program, programs_root=resolved_programs_root)
    except ValueError:
        typer.echo("[actuate review] BLOCKED: invalid actuation policy; correct it before review.", err=True)
        raise typer.Exit(code=2)

    if not policy.enabled:
        typer.echo(
            f"[actuate review] Actuation is disabled for {program!r}.\n"
            "  Set actuation.enabled: true in the program or global policy to enable.\n"
            "  Run --dry-run to preview what proposals WOULD be derived."
        )

    # Derive proposals from live reality
    proposals = derive_proposals(reality, policy)

    # Also include persisted action.proposal facts (human-approved ones not yet executed)
    from src.core.exceptions import ConfigError
    try:
        persisted = reality.pending_actuations()
    except ConfigError:
        typer.echo("[actuate review] BLOCKED: invalid retained actuation proposal; correct it before review.", err=True)
        raise typer.Exit(code=2)

    if not proposals and not persisted:
        typer.echo(f"[actuate review] No pending proposals for {program!r}.")
        return

    all_proposals = list(proposals) + [p for p in persisted if not any(
        q.proposal_id == p.proposal_id for q in proposals
    )]

    typer.echo(f"[actuate review] {len(all_proposals)} pending proposal(s) for {program!r}:")
    for i, prop in enumerate(all_proposals, 1):
        status = "APPROVED" if prop.approved else "pending approval"
        gap = f" [BLOCKED: {prop.gap_reason}]" if prop.gap_reason else ""
        dry = " [dry-run only]" if dry_run else ""
        typer.echo(
            f"  {i}. [{status}]{gap}{dry}\n"
            f"     id:        {prop.proposal_id}\n"
            f"     rule:      {prop.rule_id}\n"
            f"     adapter:   {prop.adapter}\n"
            f"     operation: {prop.operation}\n"
            f"     entity:    {prop.entity_ref}\n"
            f"     payload:   {json.dumps(prop.payload, default=str)}"
        )


@app.command("execute")
def actuate_execute(
    program: str = typer.Option(..., "--program", "-p", help="Program ID."),
    proposal_id: str = typer.Option(..., "--proposal-id", help="Proposal ID to execute."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and render; no live writes."),
    programs_root: Optional[Path] = typer.Option(None, hidden=True),
) -> None:
    """Execute an approved actuation proposal.

    INV-12: This command will refuse to execute if the proposal is not marked
    approved. Human approval (setting approved=True on the action.proposal fact)
    is the only path to execution.

    CP-7: Operators MUST review dry-run payloads of all enabled rules before
    live execution. Run with --dry-run first.
    """
    from src.core.program_reality import ProgramReality
    from src.core.actuation_engine import actuation_execution_block_reason, load_actuation_policy
    from src.core.exceptions import ConfigError

    resolved_programs_root = programs_root or _get_programs_root()

    try:
        reality = ProgramReality.load(program, programs_root=resolved_programs_root)
    except Exception as exc:
        typer.echo(f"[actuate execute] ERROR: could not load reality for {program!r}: {exc}", err=True)
        raise typer.Exit(code=1)

    try:
        policy = load_actuation_policy(program, programs_root=resolved_programs_root)
    except ValueError:
        typer.echo("[actuate execute] BLOCKED: invalid actuation policy; correct it before review.", err=True)
        raise typer.Exit(code=2)

    # Find the proposal
    try:
        all_pending = reality.pending_actuations()
    except ConfigError:
        typer.echo("[actuate execute] BLOCKED: invalid retained actuation proposal; correct it before review.", err=True)
        raise typer.Exit(code=2)
    proposal = next((p for p in all_pending if p.proposal_id == proposal_id), None)

    if proposal is None:
        typer.echo(
            f"[actuate execute] Proposal {proposal_id!r} not found in pending actuations for {program!r}.\n"
            "  It may have expired, been executed, or is not yet persisted.",
            err=True,
        )
        raise typer.Exit(code=1)

    block_reason = actuation_execution_block_reason(proposal, policy)
    if block_reason:
        typer.echo(f"[actuate execute] BLOCKED: {block_reason}", err=True)
        raise typer.Exit(code=2)

    typer.echo(
        f"[actuate execute] {'DRY-RUN: ' if dry_run else ''}Executing proposal {proposal_id!r}...\n"
        f"  rule:      {proposal.rule_id}\n"
        f"  adapter:   {proposal.adapter}\n"
        f"  operation: {proposal.operation}\n"
        f"  entity:    {proposal.entity_ref}\n"
        f"  payload:   {json.dumps(proposal.payload, default=str)}"
    )

    if dry_run:
        typer.echo("[actuate execute] DRY-RUN complete — no writes performed.")
        return

    # Dispatch to the appropriate adapter
    result = _dispatch_execution(proposal, reality, policy)

    if result.success:
        typer.echo(
            f"[actuate execute] SUCCESS: {proposal.operation} on {proposal.entity_ref!r}.\n"
            f"  external_ref: {result.external_ref}"
        )
    else:
        typer.echo(
            f"[actuate execute] FAILED: {result.error_message}",
            err=True,
        )
        raise typer.Exit(code=3)


def _dispatch_execution(
    proposal: "ActuationProposal", reality: object, policy: "ActuationPolicy",
) -> "ActuationResult":
    """Refuse every retained direct actuation path.

    RELO-W5.6 makes the outbox the sole live execution boundary.  The retained
    proposal model remains useful for review and dry-run, but it is not itself
    an operation-qualified dispatcher.
    """
    from src.core.integration_protocol import ActuationResult

    return ActuationResult(
        success=False,
        error_message=(
            "Live actuation dispatch is blocked; execute qualified effects through "
            "the durable outbox boundary. Run `vertex actuate execute --dry-run` "
            "to inspect the retained proposal."
        ),
    )
