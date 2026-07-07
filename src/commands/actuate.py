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
    from src.core.integration_protocol import ActuationResult

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

    policy = load_actuation_policy(program, programs_root=resolved_programs_root)

    if not policy.enabled:
        typer.echo(
            f"[actuate review] Actuation is disabled for {program!r}.\n"
            "  Set actuation.enabled: true in the program or global policy to enable.\n"
            "  Run --dry-run to preview what proposals WOULD be derived."
        )

    # Derive proposals from live reality
    proposals = derive_proposals(reality, policy)

    # Also include persisted action.proposal facts (human-approved ones not yet executed)
    persisted = reality.pending_actuations()

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
    from src.core.actuation_engine import load_actuation_policy

    resolved_programs_root = programs_root or _get_programs_root()

    try:
        reality = ProgramReality.load(program, programs_root=resolved_programs_root)
    except Exception as exc:
        typer.echo(f"[actuate execute] ERROR: could not load reality for {program!r}: {exc}", err=True)
        raise typer.Exit(code=1)

    policy = load_actuation_policy(program, programs_root=resolved_programs_root)

    # Find the proposal
    all_pending = reality.pending_actuations()
    proposal = next((p for p in all_pending if p.proposal_id == proposal_id), None)

    if proposal is None:
        typer.echo(
            f"[actuate execute] Proposal {proposal_id!r} not found in pending actuations for {program!r}.\n"
            "  It may have expired, been executed, or is not yet persisted.",
            err=True,
        )
        raise typer.Exit(code=1)

    # INV-12: human-approval absolute gate
    if not proposal.approved:
        typer.echo(
            f"[actuate execute] INV-12 BLOCKED: proposal {proposal_id!r} is not approved.\n"
            "  Human approval is required before execution.",
            err=True,
        )
        raise typer.Exit(code=2)

    # Check gap_reason — blocked proposals cannot be executed
    if proposal.gap_reason:
        typer.echo(
            f"[actuate execute] BLOCKED: proposal {proposal_id!r} has gap_reason={proposal.gap_reason!r}.\n"
            "  Resolve the blocking condition before executing.",
            err=True,
        )
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


def _dispatch_execution(proposal: object, reality: object, policy: object) -> "ActuationResult":
    """Route proposal to the appropriate adapter and execute.

    WI-7.2: AdoAdapter dispatches state_transition, comment, and
    work_item_create (gap-fix rules only).  AdoAdapter.execute() is live;
    lineage is written via reality's fact writer when available.
    """
    from src.core.integration_protocol import ActuationResult
    from src.core.ado_actuation_adapter import AdoActuationAdapter

    adapter_name = getattr(proposal, "adapter", "")
    operation = getattr(proposal, "operation", "")
    payload = dict(getattr(proposal, "payload", {}))
    payload["entity_ref"] = getattr(proposal, "entity_ref", "")
    payload["rule_id"] = getattr(proposal, "rule_id", "")

    if adapter_name == "ado":
        # Inject live ADO client if available via reality (Zone C path)
        ado_client_fn = None
        try:
            from src.core.program_fact_store import PROGRAMS_ROOT
            ado_client_fn = _resolve_ado_client_fn(reality)
        except Exception:
            pass

        adapter = AdoActuationAdapter(ado_client_fn=ado_client_fn)
        return adapter.execute(operation, payload, dry_run=False)

    return ActuationResult(
        success=False,
        error_message=f"No adapter registered for {adapter_name!r}",
    )


def _resolve_ado_client_fn(reality: object):
    """Attempt to return a callable that produces a live ADO client.

    Returns None if ADO is not configured (safe for dry-run / test scenarios).
    Actuation is CP-7 gated; production ADO client wiring is deferred until
    per-program actuation is enabled by an operator.
    """
    import os
    program_id = getattr(reality, "_program_id", None) or getattr(reality, "program_id", None)
    if not program_id:
        return None
    pat = os.environ.get("ADO_PAT")
    if not pat:
        return None
    try:
        from src.core.edition_resolver import PROGRAMS_ROOT
        from src.core.edition_resolver import load_program
        prog = load_program(program_id, programs_root=PROGRAMS_ROOT)
        ado_cfg = getattr(prog, "ado", None)
        if not ado_cfg:
            return None
        if isinstance(ado_cfg, dict):
            org = ado_cfg.get("organization")
            project = ado_cfg.get("project")
        else:
            org = getattr(ado_cfg, "organization", None)
            project = getattr(ado_cfg, "project", None)
        if not org or not project:
            return None
        from src.core.ado_client import ADOClient
        return lambda: ADOClient(org, project)
    except Exception:
        return None
