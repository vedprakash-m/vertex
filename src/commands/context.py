from __future__ import annotations

import json
from pathlib import Path

import typer

from src.core.config_loader import REPORTS_ROOT
from src.core.edition_resolver import PROGRAMS_ROOT, resolve_edition_paths
from src.core.ncfl_extractor import extract_proposals
from src.core.ncfl_proposal_store import (
    conflicting_pending_proposals,
    load_proposals,
    stage_extracted_proposals,
    update_proposal_status,
)


app = typer.Typer(help="NCFL context proposal extraction and review.")


def _resolve_program_id(edition: str, *, programs_root: Path) -> str:
    resolved = resolve_edition_paths(edition, programs_root=programs_root)
    if resolved is None:
        raise typer.BadParameter(f"Unknown edition {edition!r}.")
    return resolved.program_id


@app.command("extract")
def extract_command(
    edition: str = typer.Option(..., "--edition", help="Edition name, e.g. acme_weekly."),
    issue: int = typer.Option(..., "--issue", min=1, help="Confirmed issue number to extract from."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview extracted proposals without writing."),
    reports_root: Path = typer.Option(REPORTS_ROOT, hidden=True),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, hidden=True),
) -> None:
    program_id = _resolve_program_id(edition, programs_root=programs_root)
    proposals = extract_proposals(
        program_id,
        edition,
        issue,
        programs_root=programs_root,
        reports_root=reports_root,
    )
    if dry_run:
        typer.echo(f"Dry run: extracted {len(proposals)} proposal(s) for {edition} issue {issue:03d}.")
        for proposal in proposals:
            typer.echo(
                f"- {proposal.proposal_id} | {proposal.target_store}.{proposal.target_key}.{proposal.target_field} "
                f"| {proposal.confidence} | {proposal.source_value}"
            )
        return

    staged = stage_extracted_proposals(
        program_id,
        issue,
        proposals,
        programs_root=programs_root,
    )
    pending_count = sum(1 for proposal in staged if proposal.status == "pending")
    typer.echo(
        f"Staged {len(proposals)} extracted proposal(s) for {edition} issue {issue:03d}. "
        f"{pending_count} proposal(s) remain pending."
    )


@app.command("proposals")
def proposals_command(
    edition: str = typer.Option(..., "--edition", help="Edition name, e.g. acme_weekly."),
    issue: int | None = typer.Option(None, "--issue", min=1, help="Optional issue number filter."),
    status: str | None = typer.Option(None, "--status", help="Optional status filter."),
    format: str = typer.Option("human", "--format", help="Output format: human or json."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, hidden=True),
) -> None:
    program_id = _resolve_program_id(edition, programs_root=programs_root)
    status_filter = {status.strip()} if status and status.strip() else None
    proposals = load_proposals(
        program_id,
        issue_number=issue,
        status_filter=status_filter,
        programs_root=programs_root,
    )
    conflicts = conflicting_pending_proposals(program_id, programs_root=programs_root)

    if format == "json":
        typer.echo(
            json.dumps(
                {
                    "edition": edition,
                    "issue_number": issue,
                    "proposal_count": len(proposals),
                    "conflicts": {key: list(entries) for key, entries in conflicts.items()},
                    "proposals": [proposal.to_json() for proposal in proposals],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    if format != "human":
        raise typer.BadParameter("--format must be 'human' or 'json'.")

    typer.echo(f"{len(proposals)} proposal(s) for {edition}.")
    if conflicts:
        typer.echo(f"Cross-issue conflicts: {len(conflicts)}")
    for proposal in proposals:
        typer.echo(
            f"- {proposal.proposal_id} | issue {proposal.issue_number:03d} | {proposal.status} | "
            f"{proposal.target_store}.{proposal.target_key}.{proposal.target_field} | "
            f"{proposal.confidence} | {proposal.source_value}"
        )


@app.command("dismiss")
def dismiss_command(
    edition: str = typer.Option(..., "--edition", help="Edition name, e.g. acme_weekly."),
    proposal_id: str = typer.Option(..., "--proposal-id", help="Proposal identifier to dismiss."),
    reason: str = typer.Option(..., "--reason", help="Why the proposal is being dismissed."),
    actor: str = typer.Option("operator", "--actor", help="Actor recorded in the decision history."),
    issue: int | None = typer.Option(None, "--issue", min=1, help="Optional issue filter to narrow the lookup."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, hidden=True),
) -> None:
    program_id = _resolve_program_id(edition, programs_root=programs_root)
    updated = update_proposal_status(
        program_id,
        proposal_id=proposal_id,
        new_status="dismissed",
        actor=actor,
        issue_number=issue,
        rationale=reason,
        programs_root=programs_root,
    )
    typer.echo(f"Dismissed proposal {updated.proposal_id} for {edition}.")


@app.command("apply")
def apply_command(
    edition: str = typer.Option(..., "--edition", help="Edition name, e.g. acme_weekly."),
    issue: int = typer.Option(..., "--issue", min=1, help="Issue number for the accepted proposals."),
    proposal_id: str = typer.Option(..., "--proposal-id", help="Proposal ID to apply."),
    actor: str = typer.Option("operator", "--actor", help="Who is applying the proposal."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview apply without writing."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, hidden=True),
) -> None:
    """Apply one accepted NCFL proposal to its Plane 1 target store."""
    from src.core.ncfl_apply import apply_proposal
    program_id = _resolve_program_id(edition, programs_root=programs_root)
    proposals = load_proposals(program_id, issue_number=issue, status_filter={"accepted"}, programs_root=programs_root)
    matched = [p for p in proposals if p.proposal_id == proposal_id]
    if not matched:
        typer.echo(f"No accepted proposal {proposal_id!r} found for {edition} issue {issue:03d}.")
        raise typer.Exit(code=1)
    result = apply_proposal(matched[0], actor=actor, programs_root=programs_root, dry_run=dry_run)
    prefix = "[DRY RUN] " if dry_run else ""
    typer.echo(f"{prefix}{result.action}: {result.note}")
    if result.action == "needs_repair":
        raise typer.Exit(code=3)


@app.command("apply-batch")
def apply_batch_command(
    edition: str = typer.Option(..., "--edition", help="Edition name, e.g. acme_weekly."),
    issue: int = typer.Option(..., "--issue", min=1, help="Issue number for the accepted proposals."),
    actor: str = typer.Option("operator", "--actor", help="Who is applying the proposals."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview apply without writing."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, hidden=True),
) -> None:
    """Apply all accepted NCFL proposals for an issue (batch mode)."""
    from src.core.ncfl_apply import apply_proposals_batch
    program_id = _resolve_program_id(edition, programs_root=programs_root)
    proposals = load_proposals(program_id, issue_number=issue, status_filter={"accepted"}, programs_root=programs_root)
    if not proposals:
        typer.echo(f"No accepted proposals found for {edition} issue {issue:03d}.")
        raise typer.Exit(code=0)
    results = apply_proposals_batch(tuple(proposals), actor=actor, programs_root=programs_root, dry_run=dry_run)
    prefix = "[DRY RUN] " if dry_run else ""
    repairs = 0
    for r in results:
        typer.echo(f"{prefix}{r.action}: {r.target_store}.{r.target_key}.{r.target_field} — {r.note}")
        if r.action == "needs_repair":
            repairs += 1
    typer.echo(f"{prefix}Done: {len(results)} proposal(s), {repairs} need repair.")
    if repairs:
        raise typer.Exit(code=3)


@app.command("synthesize")
def synthesize_command(
    edition: str = typer.Option(..., "--edition", help="Edition name, e.g. acme_weekly."),
    issue: int = typer.Option(..., "--issue", min=1, help="Confirmed issue number to synthesize from."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview the proposal without staging it."),
    knowledge_doc: str = typer.Option(
        "nova_program_context.md",
        "--knowledge-doc",
        help="Knowledge-doc filename to target (under programs/<id>/knowledge/).",
    ),
    actor: str = typer.Option("operator", "--actor", help="Actor recorded as the synthesizer."),
    programs_root: Path = typer.Option(PROGRAMS_ROOT, hidden=True),
) -> None:
    """Zone B (Phase 5): synthesize a knowledge-doc proposal from accepted proposals + narrative.

    Reads accepted NCFL proposals for the issue and the published narrative,
    asks the LLM to draft a knowledge-doc patch, enforces the ban-list, and
    stages the result as a ``knowledge_doc`` ``ContextUpdateProposal``. The
    proposal is never auto-applied — review with ``vertex context proposals``
    and apply with ``vertex context apply``.
    """
    program_id = _resolve_program_id(edition, programs_root=programs_root)
    accepted = load_proposals(
        program_id,
        issue_number=issue,
        status_filter={"accepted"},
        programs_root=programs_root,
    )
    if not accepted:
        typer.echo(
            f"No accepted Zone A proposals for {edition} issue {issue:03d}; "
            "Zone B synthesis requires at least one accepted proposal."
        )
        raise typer.Exit(code=2)

    result = _run_context_synthesis(
        program_id=program_id,
        edition=edition,
        issue=issue,
        accepted=accepted,
        knowledge_doc=knowledge_doc,
        programs_root=programs_root,
    )
    if not result.available:
        typer.echo(f"Synthesis unavailable: {result.note}")
        raise typer.Exit(code=2)

    prefix = "[DRY RUN] " if dry_run else ""
    if not dry_run:
        stage_extracted_proposals(
            program_id,
            issue,
            (result.proposal,),
            programs_root=programs_root,
        )
    typer.echo(
        f"{prefix}Synthesized knowledge_doc proposal {result.proposal.proposal_id} "
        f"for {edition} issue {issue:03d} ({result.note})."
    )
    typer.echo(
        f"{prefix}Review with: vertex context proposals --edition {edition} --issue {issue}"
    )
    if dry_run:
        typer.echo(f"{prefix}Draft as_of_date: {result.draft.as_of_date}")
        typer.echo(f"{prefix}Highlights: {len(result.draft.highlights)} | Open risks: {len(result.draft.open_risks)}")


def _run_context_synthesis(
    *,
    program_id: str,
    edition: str,
    issue: int,
    accepted,
    knowledge_doc: str,
    programs_root: Path,
):
    """Build the Zone B client (degrade-safe) and run synthesis."""
    from src.ai.context_synthesizer import (
        ContextSynthesizer,
        enforce_ban_list,
        gather_synthesis_inputs,
    )

    inputs = gather_synthesis_inputs(
        program_id=program_id,
        edition_id=edition,
        issue_number=issue,
        accepted_proposals=tuple(accepted),
        programs_root=programs_root,
        knowledge_doc_name=knowledge_doc,
    )

    client = _build_context_synthesis_client()
    synthesizer = ContextSynthesizer(client=client)
    result = synthesizer.synthesize(inputs)
    if result.available and result.draft is not None and result.proposal is not None:
        # A-NC-7: ban-list enforcement before staging.
        sanitized_draft = enforce_ban_list(
            result.draft, programs_root=programs_root, program_id=program_id
        )
        from dataclasses import replace as dc_replace
        from src.ai.context_synthesizer import SynthesisResult
        proposal = dc_replace(result.proposal, source_value=sanitized_draft.render_markdown())
        return SynthesisResult(proposal=proposal, draft=sanitized_draft, note=result.note)
    return result


def _build_context_synthesis_client():
    """Resolve a frontier LLM client for context synthesis, or a degrade client.

    Mirrors the synthesizer command's deployment-resolution + AIMode guard.
    Returns a client whose ``structured`` call degrades to None when no
    deployment is configured (so the command exits cleanly, code 2).
    """
    from src.ai.ai_mode import AIMode, get_ai_mode
    from src.ai.client import AIClient
    from src.ai.deployment_fallback import resolve_ai_deployments_for_feature

    if get_ai_mode() == AIMode.DISABLED:
        return _DegradeClient(reason="AI is disabled (AIMode.DISABLED)")

    deployments = resolve_ai_deployments_for_feature(
        feature_name="context_synthesizer",
        primary_candidates=(None,),
        backup_candidates=(None,),
        primary_fallback_envs=("VERTEX_AI_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT"),
        backup_fallback_envs=("VERTEX_AI_BACKUP_DEPLOYMENT",),
    )
    if not deployments:
        return _DegradeClient(reason="no Azure OpenAI deployment configured for context_synthesizer")

    from src.core.policy_loader import load_ai_feature_policy
    policy = load_ai_feature_policy("context_synthesizer")
    client_kwargs = {
        "deployment": deployments[0],
        "temperature": policy.temperature,
        "budget_usd": 10.0,
    }
    try:
        return AIClient(**client_kwargs)
    except Exception:  # noqa: BLE001 — degrade, never crash the CLI
        return _DegradeClient(reason="AIClient could not be constructed from configuration")


class _DegradeClient:
    """Null LLM provider: structured() returns None via the tiered router degrade path.

    The ContextSynthesizer treats a None outcome as 'unavailable', so the CLI
    exits cleanly (code 2) instead of crashing when AI is unconfigured.
    """

    def __init__(self, *, reason: str) -> None:
        self.reason = reason

    def chat(self, system, user, *, max_tokens=800, prompt_version=None):  # noqa: ANN001
        raise AssertionError(self.reason)

    def structured(self, system, user, *, parser, max_tokens=800, prompt_version=None):  # noqa: ANN001
        # Raise AIClientError so the synthesizer's degrade path catches it.
        from src.ai.client import AIClientError
        raise AIClientError(self.reason)
