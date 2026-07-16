"""ADF v1.51 deep-dive: the shared CLI review surface for the five
AISchemaGateway-pattern proposal types that (per the master finding recorded
in ``specs/arch-data-fix.md``'s v1.51 changelog entry) had no CLI reviewer
at all -- confirmed by a repo-wide grep finding zero callers of
``apply_risk_proposal``, ``approve_meeting_action``, ``to_top3_now_entry``,
``apply_dependency_blast_radius_proposal``, or any approve/reject function
on ``GovernanceDecisionBriefProposal``.

Mirrors ``apply_proposals.py``'s proven accept/reject-by-id CLI shape
rather than inventing a fourth review UX pattern, and reuses each type's
own already-built, already-tested approve/reject/apply terminal functions
(this module only wires them to a human-invokable command; it introduces
no new proposal-mutation logic).

``ProgramSynthesis`` is out of scope here -- it already has its own
content-addressed, QG-29-released persistence and read gate
(``program_synthesis.py``), not a human accept/reject staging flow.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
import json
import math
import os
from pathlib import Path
import random
from typing import Any, Callable, cast

import typer

from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai.deployment_fallback import resolve_ai_deployments_for_feature
from src.ai.deployment_fallback import FallbackAIClient
from src.ai.dependency_blast_radius_generator import generate_dependency_blast_radius_proposal
from src.ai.governance_decision_brief_generator import generate_governance_decision_brief
from src.ai.meeting_action_extractor import run_meeting_action_extraction_pipeline
from src.ai.provider import LLMProvider
from src.ai.risk_proposal_generator import generate_risk_proposal
from src.core.adoption_telemetry import GoldenWorkflow, record_adoption
from src.core.ai_review_proposal_store import (
    REVIEW_PROPOSAL_TYPES,
    ReviewProposal,
    ReviewProposalType,
    load_proposal,
    load_proposals,
    stage_proposal,
)
from src.core.dependency_blast_radius import (
    DependencyBlastRadiusError,
    DependencyBlastRadiusRequest,
    apply_dependency_blast_radius_proposal,
    approve_blast_radius_proposal,
    reject_blast_radius_proposal,
)
from src.core.dependency_graph import load_dependencies, save_dependencies
from src.core.edition_resolver import PROGRAMS_ROOT, load_program
from src.core.governance_decision_brief import (
    GovernanceDecisionBriefError,
    GovernanceDecisionRequest,
    approve_governance_decision_brief,
    reject_governance_decision_brief,
)
from src.core.maturity_engine import load_earned_autonomy_state
from src.core.meeting_action import approve_meeting_action, reject_meeting_action
from src.core.meeting_action_routing import MeetingActionRoutingError, route_meeting_action_to_ado_proposal
from src.core.models import RiskLevel, WorkItem
from src.core.models_v2 import RiskImpact
from src.core.overrides_store import REPORTS_ROOT, load_overrides, save_overrides
from src.core.program_synthesis import ProgramSynthesisRequest, SynthesisInputItem
from src.core.proposal_audit import record_proposal_event
from src.core.risk_proposal import RiskProposalError, RiskProposalRequest, apply_risk_proposal, approve_risk_proposal, reject_risk_proposal
from src.core.risk_register_engine import load_risk_register, save_risk_register
from src.ai.top_three_candidate_generator import generate_top_three_candidates
from src.core.top_three_candidates import (
    TopThreeCandidateError,
    approve_top_three_candidate,
    reject_top_three_candidate,
    to_top3_now_entry,
)

app = typer.Typer(help="Review AI-generated proposals: risk, meeting action, top-three, governance decision brief, dependency blast radius.")

#: P2 pilot scope (v1.52 deep-dive plan): on-demand generation was wired for
#: the two simplest/most mature proposal types first (risk, meeting_action).
#: ADF-W4.8 (v1.59+) extended it to governance_decision_brief and
#: dependency_blast_radius, whose request shapes are equally flat/scalar
#: (same shape as RiskProposalRequest, easily CLI-flag-able). top_three was
#: deferred longer since its request needs a LIST of structured candidate
#: items (category/item_id/summary/severity each), which flat scalar CLI
#: flags can't responsibly represent in one invocation. Closed in v1.64 via
#: `--candidates-file`, a JSON envelope (`{"items": [...]}`) mirroring the
#: `facts import --input` file-reading convention already established in
#: this codebase rather than inventing a new one -- see
#: specs/arch-data-fix.md's ADF-W4.8 changelog entries.
_GENERATABLE_TYPES: tuple[str, ...] = ("risk", "meeting_action", "top_three", "governance_decision_brief", "dependency_blast_radius")

_DEPLOYMENT_FALLBACK_ENVS = ("VERTEX_AI_DEPLOYMENT", "VERTEX_EXEC_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT")


class GenerateProposalError(Exception):
    """Raised for user/config errors in the on-demand generate path."""


@dataclass(frozen=True, slots=True)
class GenerateProposalResult:
    proposal_type: str
    staged_ids: tuple[str, ...]
    rejected_count: int = 0
    message: str = ""


@app.command("list")
def list_proposals_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    type: str | None = typer.Option(None, "--type", help=f"Optional filter, one of: {', '.join(REVIEW_PROPOSAL_TYPES)}."),
    status: str = typer.Option("staged", "--status", help="Filter by status: staged|approved|rejected|all."),
) -> None:
    proposal_type = _parse_type(type) if type is not None else None
    status_filter = None if status.strip().lower() == "all" else {status.strip().lower()}
    proposals = load_proposals(
        program.strip(),
        proposal_type=proposal_type,
        status_filter=status_filter,
        programs_root=PROGRAMS_ROOT,
    )
    if not proposals:
        typer.echo(f"No {status} proposals for {program.strip()}" + (f" (type={proposal_type})." if proposal_type else "."))
        raise typer.Exit(code=0)
    for proposal in proposals:
        typer.echo(_format_proposal(proposal))
    raise typer.Exit(code=0)


@app.command("generate")
def generate_proposal_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    type: str = typer.Option(..., "--type", help=f"One of: {', '.join(_GENERATABLE_TYPES)}."),
    candidate_risk_id: str | None = typer.Option(None, "--candidate-risk-id", help="[risk] Candidate risk id being escalated."),
    title: str | None = typer.Option(None, "--title", help="[risk] Candidate risk title."),
    description: str | None = typer.Option(None, "--description", help="[risk] Candidate risk description."),
    evidence_text: list[str] = typer.Option([], "--evidence-text", help="[risk] Evidence text snippet. Repeat for multiple."),
    evidence_ref: list[str] = typer.Option([], "--evidence-ref", help="[risk] Evidence reference id (e.g. signal id). Repeat for multiple."),
    meeting_ref: str | None = typer.Option(None, "--meeting-ref", help="[meeting_action] Meeting reference id."),
    transcript_file: Path | None = typer.Option(None, "--transcript-file", help="[meeting_action] Path to a plain-text transcript file."),
    work_item_id: list[int] = typer.Option([], "--work-item-id", help="[meeting_action] Allowed work item id the extractor may link actions to. Repeat for multiple."),
    candidates_file: Path | None = typer.Option(
        None, "--candidates-file",
        help='[top_three] JSON file: {"items": [{"category": str, "item_id": str, "summary": str, '
             '"severity": str|null, "evidence_refs": [str, ...]}, ...]}.',
    ),
    decision_ask_id: str | None = typer.Option(None, "--decision-ask-id", help="[governance_decision_brief] Decision ask id being resolved."),
    decision_text: str | None = typer.Option(None, "--decision-text", help="[governance_decision_brief] The open decision ask's text."),
    dependency_id: str | None = typer.Option(None, "--dependency-id", help="[dependency_blast_radius] Dependency id being assessed."),
    from_summary: str | None = typer.Option(None, "--from-summary", help="[dependency_blast_radius] Upstream (from) side summary."),
    to_summary: str | None = typer.Option(None, "--to-summary", help="[dependency_blast_radius] Downstream (to) side summary."),
    risk_if_broken: str | None = typer.Option(None, "--risk-if-broken", help="[dependency_blast_radius] Risk if this dependency breaks."),
    current_status: str | None = typer.Option(None, "--current-status", help="[dependency_blast_radius] Current dependency status."),
    deployment: str | None = typer.Option(None, "--deployment", help="Override Azure OpenAI deployment name; defaults to VERTEX_AI_DEPLOYMENT/VERTEX_EXEC_DEPLOYMENT/AZURE_OPENAI_DEPLOYMENT."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Run the AI generation but do not stage the result for review."),
) -> None:
    transcript_text = None
    if transcript_file is not None:
        try:
            transcript_text = transcript_file.read_text(encoding="utf-8")
        except OSError as error:
            raise typer.BadParameter(f"Could not read --transcript-file {str(transcript_file)!r}: {error}") from error

    candidate_items: tuple[SynthesisInputItem, ...] = ()
    if candidates_file is not None:
        try:
            candidates_raw = json.loads(candidates_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise typer.BadParameter(f"Could not read --candidates-file {str(candidates_file)!r}: {error}") from error
        candidate_items = _parse_candidate_items(candidates_raw)

    try:
        result = generate_ai_review_proposal(
            program_id=program.strip(),
            proposal_type=type.strip().lower(),
            candidate_risk_id=candidate_risk_id,
            candidate_title=title,
            candidate_description=description,
            evidence_texts=tuple(evidence_text),
            evidence_refs=tuple(evidence_ref),
            meeting_ref=meeting_ref,
            transcript_text=transcript_text,
            work_item_ids=tuple(work_item_id),
            candidate_items=candidate_items,
            decision_ask_id=decision_ask_id,
            decision_text=decision_text,
            dependency_id=dependency_id,
            from_summary=from_summary,
            to_summary=to_summary,
            risk_if_broken=risk_if_broken,
            current_status=current_status,
            deployment_override=deployment,
            dry_run=dry_run,
            programs_root=PROGRAMS_ROOT,
        )
    except GenerateProposalError as error:
        raise typer.BadParameter(str(error)) from error

    typer.echo(result.message)
    raise typer.Exit(code=0)


def _parse_candidate_items(raw: Any) -> tuple[SynthesisInputItem, ...]:
    """Parse a `--candidates-file` JSON envelope into `SynthesisInputItem`s,
    mirroring `facts.py::facts_import`'s established file-input convention
    (a named envelope key holding a list of dicts). Unlike `facts import`
    (which skips malformed entries so a bulk import degrades gracefully),
    a malformed candidate item here raises `typer.BadParameter` immediately
    -- silently dropping one of a handful of hand-curated candidates would
    invisibly change what the AI is asked to prioritize among."""
    items_raw = raw.get("items") if isinstance(raw, dict) else None
    if not isinstance(items_raw, list) or not items_raw:
        raise typer.BadParameter('--candidates-file must be a JSON object with a non-empty "items" array.')
    parsed: list[SynthesisInputItem] = []
    for index, entry in enumerate(items_raw):
        if not isinstance(entry, dict):
            raise typer.BadParameter(f"--candidates-file items[{index}] must be a JSON object.")
        try:
            severity = entry.get("severity")
            parsed.append(
                SynthesisInputItem(
                    category=str(entry["category"]),
                    item_id=str(entry["item_id"]),
                    summary=str(entry["summary"]),
                    evidence_refs=tuple(str(ref) for ref in entry.get("evidence_refs") or ()),
                    severity=str(severity) if severity is not None else None,
                )
            )
        except KeyError as error:
            raise typer.BadParameter(f"--candidates-file items[{index}] is missing required field {error}.") from error
    return tuple(parsed)


_FEATURE_NAME_BY_TYPE: dict[str, str] = {
    "risk": "risk_proposal_generator",
    "meeting_action": "meeting_action_extractor",
    "top_three": "top_three_candidate_generator",
    "governance_decision_brief": "governance_decision_brief_generator",
    "dependency_blast_radius": "dependency_blast_radius_generator",
}


def generate_ai_review_proposal(
    *,
    program_id: str,
    proposal_type: str,
    candidate_risk_id: str | None = None,
    candidate_title: str | None = None,
    candidate_description: str | None = None,
    evidence_texts: tuple[str, ...] = (),
    evidence_refs: tuple[str, ...] = (),
    meeting_ref: str | None = None,
    transcript_text: str | None = None,
    work_item_ids: tuple[int, ...] = (),
    candidate_items: tuple[SynthesisInputItem, ...] = (),
    decision_ask_id: str | None = None,
    decision_text: str | None = None,
    dependency_id: str | None = None,
    from_summary: str | None = None,
    to_summary: str | None = None,
    risk_if_broken: str | None = None,
    current_status: str | None = None,
    deployment_override: str | None = None,
    dry_run: bool = False,
    programs_root: Path = PROGRAMS_ROOT,
    client_factory: Callable[..., LLMProvider] | None = None,
) -> GenerateProposalResult:
    """P2 (v1.52 deep-dive plan) + ADF-W4.8 (v1.59+): the on-demand
    generation trigger the user chose over auto-wiring into gather/report --
    an explicit CLI invocation, mirroring ``vertex synthesize``'s
    already-proven standalone-CLI shape, so real AI spend/latency is only
    ever incurred when an operator asks for it. Piloted with the two
    simplest/most mature generators first (``risk_proposal_generator.py``,
    ``meeting_action_extractor.py``); extended to
    ``governance_decision_brief_generator.py``/
    ``dependency_blast_radius_generator.py`` once their request shapes were
    confirmed equally flat/scalar. All four previously had zero CLI
    callers anywhere in the codebase."""
    if proposal_type not in _GENERATABLE_TYPES:
        raise GenerateProposalError(
            f"--type must be one of: {', '.join(_GENERATABLE_TYPES)} for `generate` (got {proposal_type!r}). "
            f"The other proposal types ({', '.join(t for t in REVIEW_PROPOSAL_TYPES if t not in _GENERATABLE_TYPES)}) "
            "are not yet wired to an on-demand generator."
        )
    if get_ai_mode() == AIMode.DISABLED:
        raise GenerateProposalError("AI execution is disabled (--no-ai / AIMode.DISABLED); no proposal can be generated.")

    program = load_program(program_id, programs_root=programs_root)
    if program is None:
        raise GenerateProposalError(f"Program {program_id!r} is missing program.yaml.")
    if program.ai is None or not program.ai.enabled:
        raise GenerateProposalError(f"Program {program_id!r} does not have AI enabled in program.yaml.")

    feature_name = _FEATURE_NAME_BY_TYPE[proposal_type]
    deployments = resolve_ai_deployments_for_feature(
        feature_name=feature_name,
        primary_candidates=(deployment_override,),
        backup_candidates=(),
        primary_fallback_envs=_DEPLOYMENT_FALLBACK_ENVS,
        backup_fallback_envs=(),
    )
    if not deployments:
        raise GenerateProposalError(
            "No AI deployment is configured. Pass --deployment or set VERTEX_AI_DEPLOYMENT/VERTEX_EXEC_DEPLOYMENT/AZURE_OPENAI_DEPLOYMENT."
        )
    client = FallbackAIClient(
        deployments=deployments,
        temperature=program.ai.temperature or 0.2,
        budget_usd=program.ai.budget_usd_per_run,
        requests_per_minute=program.ai.requests_per_minute,
        client_factory=client_factory,
    )

    if proposal_type == "risk":
        return _generate_risk(
            program_id, candidate_risk_id=candidate_risk_id, candidate_title=candidate_title,
            candidate_description=candidate_description, evidence_texts=evidence_texts, evidence_refs=evidence_refs,
            client=client, dry_run=dry_run, programs_root=programs_root,
        )
    if proposal_type == "meeting_action":
        return _generate_meeting_action(
            program_id, meeting_ref=meeting_ref, transcript_text=transcript_text, work_item_ids=work_item_ids,
            client=client, dry_run=dry_run, programs_root=programs_root,
        )
    if proposal_type == "top_three":
        return _generate_top_three(
            program_id, candidate_items=candidate_items, client=client, dry_run=dry_run, programs_root=programs_root,
        )
    if proposal_type == "governance_decision_brief":
        return _generate_governance_decision_brief(
            program_id, decision_ask_id=decision_ask_id, decision_text=decision_text,
            evidence_texts=evidence_texts, evidence_refs=evidence_refs,
            client=client, dry_run=dry_run, programs_root=programs_root,
        )
    return _generate_dependency_blast_radius(
        program_id, dependency_id=dependency_id, from_summary=from_summary, to_summary=to_summary,
        risk_if_broken=risk_if_broken, current_status=current_status,
        evidence_texts=evidence_texts, evidence_refs=evidence_refs,
        client=client, dry_run=dry_run, programs_root=programs_root,
    )


def _generate_risk(
    program_id: str,
    *,
    candidate_risk_id: str | None,
    candidate_title: str | None,
    candidate_description: str | None,
    evidence_texts: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    client: LLMProvider,
    dry_run: bool,
    programs_root: Path,
) -> GenerateProposalResult:
    if not candidate_risk_id or not candidate_title or not candidate_description or not evidence_texts:
        raise GenerateProposalError(
            "--candidate-risk-id, --title, --description, and at least one --evidence-text are required for --type risk."
        )
    request = RiskProposalRequest(
        program_id=program_id,
        candidate_risk_id=candidate_risk_id,
        candidate_title=candidate_title,
        candidate_description=candidate_description,
        evidence_texts=evidence_texts,
        evidence_refs=evidence_refs,
    )
    if dry_run:
        return GenerateProposalResult(
            proposal_type="risk", staged_ids=(),
            message=f"[dry-run] Would generate a risk proposal for candidate {candidate_risk_id!r}. No AI call made, nothing staged.",
        )
    proposal = generate_risk_proposal(request, client=client, programs_root=programs_root)
    if proposal is None:
        return GenerateProposalResult(
            proposal_type="risk", staged_ids=(),
            message=(
                f"AI run for candidate risk {candidate_risk_id!r} was discarded or rejected -- see "
                f"programs/{program_id}/journal/ai_release_audit.jsonl for the reason. Nothing was staged."
            ),
        )
    stage_proposal(program_id, "risk", proposal, programs_root=programs_root)
    return GenerateProposalResult(
        proposal_type="risk", staged_ids=(proposal.id,),
        message=f"Generated and staged risk proposal {proposal.id!r} for review (`vertex ai-proposals list --program {program_id} --type risk`).",
    )


def _generate_governance_decision_brief(
    program_id: str,
    *,
    decision_ask_id: str | None,
    decision_text: str | None,
    evidence_texts: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    client: LLMProvider,
    dry_run: bool,
    programs_root: Path,
) -> GenerateProposalResult:
    if not decision_ask_id or not decision_text:
        raise GenerateProposalError("--decision-ask-id and --decision-text are required for --type governance_decision_brief.")
    request = GovernanceDecisionRequest(
        program_id=program_id,
        decision_ask_id=decision_ask_id,
        decision_text=decision_text,
        evidence_texts=evidence_texts,
        evidence_refs=evidence_refs,
    )
    if dry_run:
        return GenerateProposalResult(
            proposal_type="governance_decision_brief", staged_ids=(),
            message=f"[dry-run] Would generate a governance decision brief for ask {decision_ask_id!r}. No AI call made, nothing staged.",
        )
    proposal = generate_governance_decision_brief(request, client=client, programs_root=programs_root)
    if proposal is None:
        return GenerateProposalResult(
            proposal_type="governance_decision_brief", staged_ids=(),
            message=(
                f"AI run for decision ask {decision_ask_id!r} was discarded or rejected -- see "
                f"programs/{program_id}/journal/ai_release_audit.jsonl for the reason. Nothing was staged."
            ),
        )
    stage_proposal(program_id, "governance_decision_brief", proposal, programs_root=programs_root)
    return GenerateProposalResult(
        proposal_type="governance_decision_brief", staged_ids=(proposal.id,),
        message=f"Generated and staged governance decision brief {proposal.id!r} for review (`vertex ai-proposals list --program {program_id} --type governance_decision_brief`).",
    )


def _generate_dependency_blast_radius(
    program_id: str,
    *,
    dependency_id: str | None,
    from_summary: str | None,
    to_summary: str | None,
    risk_if_broken: str | None,
    current_status: str | None,
    evidence_texts: tuple[str, ...],
    evidence_refs: tuple[str, ...],
    client: LLMProvider,
    dry_run: bool,
    programs_root: Path,
) -> GenerateProposalResult:
    if not dependency_id or not from_summary or not to_summary or not risk_if_broken or not current_status:
        raise GenerateProposalError(
            "--dependency-id, --from-summary, --to-summary, --risk-if-broken, and --current-status "
            "are all required for --type dependency_blast_radius."
        )
    request = DependencyBlastRadiusRequest(
        program_id=program_id,
        dependency_id=dependency_id,
        from_summary=from_summary,
        to_summary=to_summary,
        risk_if_broken=risk_if_broken,
        current_status=current_status,
        evidence_texts=evidence_texts,
        evidence_refs=evidence_refs,
    )
    if dry_run:
        return GenerateProposalResult(
            proposal_type="dependency_blast_radius", staged_ids=(),
            message=f"[dry-run] Would generate a dependency blast-radius proposal for {dependency_id!r}. No AI call made, nothing staged.",
        )
    proposal = generate_dependency_blast_radius_proposal(request, client=client, programs_root=programs_root)
    if proposal is None:
        return GenerateProposalResult(
            proposal_type="dependency_blast_radius", staged_ids=(),
            message=(
                f"AI run for dependency {dependency_id!r} was discarded or rejected -- see "
                f"programs/{program_id}/journal/ai_release_audit.jsonl for the reason. Nothing was staged."
            ),
        )
    stage_proposal(program_id, "dependency_blast_radius", proposal, programs_root=programs_root)
    return GenerateProposalResult(
        proposal_type="dependency_blast_radius", staged_ids=(proposal.id,),
        message=f"Generated and staged dependency blast-radius proposal {proposal.id!r} for review (`vertex ai-proposals list --program {program_id} --type dependency_blast_radius`).",
    )


def _generate_meeting_action(
    program_id: str,
    *,
    meeting_ref: str | None,
    transcript_text: str | None,
    work_item_ids: tuple[int, ...],
    client: LLMProvider,
    dry_run: bool,
    programs_root: Path,
) -> GenerateProposalResult:
    if not meeting_ref or not transcript_text or not transcript_text.strip():
        raise GenerateProposalError("--meeting-ref and a non-empty --transcript-file are required for --type meeting_action.")
    if dry_run:
        return GenerateProposalResult(
            proposal_type="meeting_action", staged_ids=(),
            message=f"[dry-run] Would extract meeting actions for {meeting_ref!r}. No AI call made, nothing staged.",
        )
    # Only `item.id` membership is consulted by the extractor's validator and
    # prompt-builder (src/ai/meeting_action_extractor.py) -- these minimal
    # stand-ins correctly represent "the allowed work item id set" without
    # requiring a live ADO fetch for a single CLI invocation.
    items = tuple(
        WorkItem(
            id=work_item_id, type="", title="", state="", assigned_to=None, assigned_to_email=None,
            area_path="", iteration_path="", target_date=None, risk_level=RiskLevel.UNKNOWN, tags=[], custom_fields={},
        )
        for work_item_id in work_item_ids
    )
    result = run_meeting_action_extraction_pipeline(
        program_id=program_id, meeting_ref=meeting_ref, transcript_text=transcript_text,
        items=items, client=client, programs_root=programs_root,
    )
    staged_ids: list[str] = []
    rejected_count = 0
    for action in result.actions:
        stage_proposal(program_id, "meeting_action", action, programs_root=programs_root)
        if action.status == "rejected":
            rejected_count += 1
        else:
            staged_ids.append(action.id)
    warning_suffix = f" Warnings: {'; '.join(result.warnings)}" if result.warnings else ""
    return GenerateProposalResult(
        proposal_type="meeting_action", staged_ids=tuple(staged_ids), rejected_count=rejected_count,
        message=(
            f"Extracted {len(result.actions)} action(s) for meeting {meeting_ref!r}: "
            f"{len(staged_ids)} staged for review, {rejected_count} rejected by validation." + warning_suffix
        ),
    )


def _generate_top_three(
    program_id: str,
    *,
    candidate_items: tuple[SynthesisInputItem, ...],
    client: LLMProvider,
    dry_run: bool,
    programs_root: Path,
) -> GenerateProposalResult:
    if not candidate_items:
        raise GenerateProposalError("--candidates-file (with at least one item) is required for --type top_three.")
    request = ProgramSynthesisRequest(
        program_id=program_id,
        as_of=datetime.now(timezone.utc),
        items=candidate_items,
    )
    if dry_run:
        return GenerateProposalResult(
            proposal_type="top_three", staged_ids=(),
            message=f"[dry-run] Would select top-three candidates from {len(candidate_items)} item(s). No AI call made, nothing staged.",
        )
    candidates = generate_top_three_candidates(request, client=client, programs_root=programs_root)
    if not candidates:
        return GenerateProposalResult(
            proposal_type="top_three", staged_ids=(),
            message=(
                f"AI run selecting top-three from {len(candidate_items)} candidate(s) was discarded or rejected -- see "
                f"programs/{program_id}/journal/ai_release_audit.jsonl for the reason. Nothing was staged."
            ),
        )
    staged_ids = tuple(candidate.id for candidate in candidates)
    for candidate in candidates:
        stage_proposal(program_id, "top_three", candidate, programs_root=programs_root)
    return GenerateProposalResult(
        proposal_type="top_three", staged_ids=staged_ids,
        message=(
            f"Selected and staged {len(staged_ids)} top-three candidate(s) for review "
            f"(`vertex ai-proposals list --program {program_id} --type top_three`)."
        ),
    )


@app.command("accept")
def accept_proposal_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    type: str = typer.Option(..., "--type", help=f"One of: {', '.join(REVIEW_PROPOSAL_TYPES)}."),
    id: str = typer.Option(..., "--id", help="Proposal id to accept."),
    actor: str | None = typer.Option(None, "--actor", help="Reviewer identity. Defaults to the current OS user."),
    org: str | None = typer.Option(None, "--org", help="ADO organization (meeting_action routing only)."),
    project: str | None = typer.Option(None, "--project", help="ADO project (meeting_action routing only)."),
    area_path: str | None = typer.Option(None, "--area-path", help="Optional ADO area path (meeting_action routing only)."),
    iteration_path: str | None = typer.Option(None, "--iteration-path", help="Optional ADO iteration path (meeting_action routing only)."),
    edition: str | None = typer.Option(None, "--edition", help="Edition to publish into top_3_now (top_three only)."),
    by_date: str | None = typer.Option(None, "--by-date", help="Optional YYYY-MM-DD due date for the published top_3_now entry (top_three only)."),
    ado_link: str = typer.Option("", "--ado-link", help="Optional ADO link for the published top_3_now entry (top_three only)."),
    anchor: str = typer.Option("", "--anchor", help="Optional report anchor for the published top_3_now entry (top_three only)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview the accept without persisting any change."),
) -> None:
    program_id = program.strip()
    proposal_type = _parse_type(type)
    proposal = load_proposal(program_id, proposal_type, id, programs_root=PROGRAMS_ROOT)
    if proposal is None:
        raise typer.BadParameter(f"No {proposal_type} proposal with id {id!r} for program {program_id!r}.")
    if proposal.status != "staged":
        raise typer.BadParameter(f"Proposal {id!r} has status={proposal.status!r}, not 'staged' -- it was already decided.")

    actor_identity = _default_actor(actor)

    if dry_run:
        typer.echo(f"[dry-run] Would accept {proposal_type} proposal {id!r} as {actor_identity!r}. No changes made.")
        raise typer.Exit(code=0)

    if proposal_type == "risk":
        _accept_risk(program_id, proposal, actor_identity=actor_identity, programs_root=PROGRAMS_ROOT)
    elif proposal_type == "meeting_action":
        _accept_meeting_action(
            program_id, proposal, actor_identity=actor_identity,
            org=org, project=project, area_path=area_path, iteration_path=iteration_path,
            programs_root=PROGRAMS_ROOT,
        )
    elif proposal_type == "top_three":
        _accept_top_three(
            program_id, proposal,
            edition=edition, by_date=_parse_optional_date(by_date), ado_link=ado_link, anchor=anchor,
            programs_root=PROGRAMS_ROOT, reports_root=REPORTS_ROOT,
        )
    elif proposal_type == "governance_decision_brief":
        _accept_governance_decision_brief(program_id, proposal, programs_root=PROGRAMS_ROOT)
    elif proposal_type == "dependency_blast_radius":
        _accept_dependency_blast_radius(program_id, proposal, programs_root=PROGRAMS_ROOT)
    raise typer.Exit(code=0)


@app.command("reject")
def reject_proposal_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    type: str = typer.Option(..., "--type", help=f"One of: {', '.join(REVIEW_PROPOSAL_TYPES)}."),
    id: str = typer.Option(..., "--id", help="Proposal id to reject."),
    reason: str = typer.Option(..., "--reason", help="Why this proposal was rejected."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview the reject without persisting any change."),
) -> None:
    program_id = program.strip()
    proposal_type = _parse_type(type)
    proposal = load_proposal(program_id, proposal_type, id, programs_root=PROGRAMS_ROOT)
    if proposal is None:
        raise typer.BadParameter(f"No {proposal_type} proposal with id {id!r} for program {program_id!r}.")
    if proposal.status != "staged":
        raise typer.BadParameter(f"Proposal {id!r} has status={proposal.status!r}, not 'staged' -- it was already decided.")

    reason_text = reason.strip()
    if not reason_text:
        raise typer.BadParameter("--reason is required and cannot be blank.")

    if dry_run:
        typer.echo(f"[dry-run] Would reject {proposal_type} proposal {id!r} ({reason_text!r}). No changes made.")
        raise typer.Exit(code=0)

    rejected = _REJECTORS[proposal_type](proposal, reason=reason_text, programs_root=PROGRAMS_ROOT)
    stage_proposal(program_id, proposal_type, rejected, programs_root=PROGRAMS_ROOT)
    typer.echo(f"Rejected {proposal_type} proposal {id!r}: {reason_text}")
    raise typer.Exit(code=0)


# ---------------------------------------------------------------------------
# Per-type accept handlers. Each: (1) calls the type's own already-tested
# approve_*/apply_* function -- no new mutation logic is introduced here --
# (2) re-stages the resulting proposal, (3) applies the terminal record
# mutation where a safe, unambiguous target exists.
# ---------------------------------------------------------------------------


def _accept_risk(
    program_id: str, proposal, *, actor_identity: str, reviewed: bool = True,
    echo_fn: Callable[[str], None] = typer.echo, programs_root: Path = PROGRAMS_ROOT,
) -> None:
    del actor_identity  # RiskProposal has no reviewer-identity field yet (mirrors approve_meeting_action's own note).
    try:
        approved = approve_risk_proposal(proposal, programs_root=programs_root, reviewed=reviewed)
    except RiskProposalError as error:
        raise typer.BadParameter(str(error)) from error
    stage_proposal(program_id, "risk", approved, programs_root=programs_root)

    risks = load_risk_register(program_id, programs_root)
    candidate = next((entry for entry in risks if entry.id == approved.candidate_risk_id), None)
    if candidate is None:
        echo_fn(
            f"Approved risk proposal {approved.id!r}, but candidate risk {approved.candidate_risk_id!r} "
            "no longer exists in the risk register -- nothing to apply."
        )
        return
    try:
        updated_risk = apply_risk_proposal(candidate, approved)
    except RiskProposalError as error:
        raise typer.BadParameter(str(error)) from error
    updated_risks = tuple(updated_risk if entry.id == candidate.id else entry for entry in risks)
    save_risk_register(program_id, updated_risks, programs_root=programs_root)
    record_adoption(program_id, GoldenWorkflow.RISK_DEPENDENCY_REVIEW, programs_root=programs_root)
    echo_fn(f"Accepted risk proposal {approved.id!r}: risk {updated_risk.id!r} promoted candidate -> strategic.")


def _accept_meeting_action(
    program_id: str,
    proposal,
    *,
    actor_identity: str,
    org: str | None,
    project: str | None,
    area_path: str | None,
    iteration_path: str | None,
    reviewed: bool = True,
    echo_fn: Callable[[str], None] = typer.echo,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    try:
        approved = approve_meeting_action(
            proposal, approved_by=actor_identity, programs_root=programs_root, reviewed=reviewed,
        )
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    stage_proposal(program_id, "meeting_action", approved, programs_root=programs_root)

    if not org or not project:
        echo_fn(
            f"Accepted meeting action {approved.id!r}. Pass --org and --project to route it to an ADO create-task "
            "proposal via the outbox now, or run this command again with --id once you have them."
        )
        return
    try:
        outbox_entry = route_meeting_action_to_ado_proposal(
            approved, org=org, project=project, area_path=area_path, iteration_path=iteration_path,
            programs_root=programs_root,
        )
    except MeetingActionRoutingError as error:
        raise typer.BadParameter(str(error)) from error
    record_adoption(program_id, GoldenWorkflow.MEETING_TO_ACTION, programs_root=programs_root)
    echo_fn(f"Accepted meeting action {approved.id!r}: routed to ADO outbox entry {outbox_entry.outbox_id!r}.")


def _accept_top_three(
    program_id: str,
    proposal,
    *,
    edition: str | None,
    by_date: date | None,
    ado_link: str,
    anchor: str,
    echo_fn: Callable[[str], None] = typer.echo,
    programs_root: Path = PROGRAMS_ROOT,
    reports_root: Path = REPORTS_ROOT,
) -> None:
    try:
        approved = approve_top_three_candidate(proposal, programs_root=programs_root)
    except TopThreeCandidateError as error:
        raise typer.BadParameter(str(error)) from error
    stage_proposal(program_id, "top_three", approved, programs_root=programs_root)

    entry = to_top3_now_entry(approved, by_date=by_date, ado_link=ado_link, anchor=anchor)
    if not edition:
        echo_fn(
            f"Accepted top-three candidate {approved.id!r}. Pass --edition to publish it into that edition's "
            f"top_3_now now, or add it manually via `vertex override`. Proposed entry text: {entry.text!r}"
        )
        return
    document = load_overrides(edition.strip(), reports_root=reports_root)
    if document is None:
        raise typer.BadParameter(
            f"No overrides document found for edition {edition!r} -- run `vertex report --edition {edition}` "
            "at least once before publishing to top_3_now."
        )
    updated_document = replace(document, top_3_now=document.top_3_now + (entry,))
    save_overrides(edition.strip(), updated_document, reports_root=reports_root)
    echo_fn(f"Accepted top-three candidate {approved.id!r}: published to {edition!r}'s top_3_now.")


def _accept_governance_decision_brief(
    program_id: str, proposal, *, echo_fn: Callable[[str], None] = typer.echo, programs_root: Path = PROGRAMS_ROOT,
) -> None:
    try:
        approved = approve_governance_decision_brief(proposal, programs_root=programs_root)
    except GovernanceDecisionBriefError as error:
        raise typer.BadParameter(str(error)) from error
    stage_proposal(program_id, "governance_decision_brief", approved, programs_root=programs_root)
    echo_fn(
        f"Accepted governance decision brief {approved.id!r}. It has no separate published record yet "
        "(Section 8.10.7 consumption path is tracked as remaining ADF-W2.9/P5 work) -- it is now visible "
        "via `vertex ai-proposals list --status approved`."
    )


def _accept_dependency_blast_radius(
    program_id: str, proposal, *, echo_fn: Callable[[str], None] = typer.echo, programs_root: Path = PROGRAMS_ROOT,
) -> None:
    try:
        approved = approve_blast_radius_proposal(proposal, programs_root=programs_root)
    except DependencyBlastRadiusError as error:
        raise typer.BadParameter(str(error)) from error
    stage_proposal(program_id, "dependency_blast_radius", approved, programs_root=programs_root)

    dependencies = load_dependencies(program_id, programs_root)
    dependency = next((item for item in dependencies if item.id == approved.dependency_id), None)
    if dependency is None:
        echo_fn(
            f"Approved blast-radius proposal {approved.id!r}, but dependency {approved.dependency_id!r} "
            "no longer exists -- nothing to apply."
        )
        return
    try:
        updated_dependency = apply_dependency_blast_radius_proposal(dependency, approved)
    except DependencyBlastRadiusError as error:
        raise typer.BadParameter(str(error)) from error
    updated_dependencies = tuple(
        updated_dependency if item.id == dependency.id else item for item in dependencies
    )
    save_dependencies(program_id, updated_dependencies, programs_root=programs_root)
    echo_fn(f"Accepted blast-radius proposal {approved.id!r}: dependency {updated_dependency.id!r} updated.")


# ---------------------------------------------------------------------------
# ADF-W5.12 P4 (Section 8.15.2): sampled/batch review at L3+ autonomy, and
# the "material regression" signal the L3/L4 promotion floor depends on.
# `review-batch` is piloted for the same two types P2's `generate` targets
# first -- risk and meeting_action -- for identical reasons (see
# `_GENERATABLE_TYPES` above); `flag-regression` has no domain-specific
# apply logic and supports all five REVIEW_PROPOSAL_TYPES.
# ---------------------------------------------------------------------------
_SAMPLED_REVIEW_TYPES: tuple[str, ...] = _GENERATABLE_TYPES

_LEVEL_ORDER = ("l0", "l1", "l2", "l3", "l4")
_SAMPLED_REVIEW_MIN_LEVEL = "l3"


class ReviewBatchError(Exception):
    """Raised for user/config errors in the sampled/batch review path."""


@dataclass(frozen=True, slots=True)
class ReviewBatchResult:
    proposal_type: str
    reviewed_ids: tuple[str, ...]
    auto_approved_ids: tuple[str, ...]
    rejected_ids: tuple[str, ...]
    sample_rate: float


def _materiality_forced_ids(proposal_type: str, staged: list) -> set[str]:
    """Section 8.15.2: sampled review must always include materiality-
    weighted and low-confidence/changed-policy/model cases, regardless of
    the computed sample size. For ``risk``, ``RiskImpact.CRITICAL``/``HIGH``
    proposals are always force-included. No equivalent severity/confidence
    field exists on ``MeetingAction`` yet -- an honest scope limit, not an
    invented proxy signal, so that type falls back to pure random sampling."""
    if proposal_type != "risk":
        return set()
    return {p.id for p in staged if getattr(p, "impact", None) in (RiskImpact.CRITICAL, RiskImpact.HIGH)}


def _accept_one(
    program_id: str, proposal_type: str, proposal, *, actor_identity: str,
    org: str | None, project: str | None, area_path: str | None, iteration_path: str | None, reviewed: bool,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    if proposal_type == "risk":
        _accept_risk(program_id, proposal, actor_identity=actor_identity, reviewed=reviewed, programs_root=programs_root)
    else:
        _accept_meeting_action(
            program_id, proposal, actor_identity=actor_identity,
            org=org, project=project, area_path=area_path, iteration_path=iteration_path, reviewed=reviewed,
            programs_root=programs_root,
        )


def _reject_one(program_id: str, proposal_type: ReviewProposalType, proposal, *, reason: str, programs_root: Path = PROGRAMS_ROOT) -> None:
    rejected = (
        reject_risk_proposal(proposal, reason=reason, programs_root=programs_root)
        if proposal_type == "risk"
        else reject_meeting_action(proposal, reason=reason, programs_root=programs_root)
    )
    stage_proposal(program_id, proposal_type, rejected, programs_root=programs_root)


def run_review_batch(
    program_id: str,
    proposal_type: ReviewProposalType,
    *,
    sample_size: int | None = None,
    seed: int | None = None,
    dry_run: bool = False,
    actor: str | None = None,
    org: str | None = None,
    project: str | None = None,
    area_path: str | None = None,
    iteration_path: str | None = None,
    confirm_fn: Callable[[str], bool] = typer.confirm,
    prompt_fn: Callable[[str], str] = typer.prompt,
    echo_fn: Callable[[str], None] = typer.echo,
    programs_root: Path = PROGRAMS_ROOT,
) -> ReviewBatchResult:
    """Section 8.15.2's sampled review: at L3+, a human individually
    reviews only a sample of the currently-staged batch (random +
    materiality-weighted); the rest are auto-approved by extension of that
    sample's trust -- the ``sample_rate`` the autonomy ladder already
    tracked (``proposal_autonomy_ladder.py``) but never consumed until now.
    Below L3, refuses -- full individual review via `list`/`accept`/`reject`
    is the only authorized mode (Section 8.15.1's ladder)."""
    if proposal_type not in _SAMPLED_REVIEW_TYPES:
        raise ReviewBatchError(
            f"Sampled/batch review is only wired for {', '.join(_SAMPLED_REVIEW_TYPES)} today; got {proposal_type!r}."
        )
    if sample_size is not None and sample_size < 0:
        raise ReviewBatchError(f"--sample-size cannot be negative (got {sample_size!r}).")

    state = load_earned_autonomy_state(program_id, programs_root=programs_root)
    class_state = state.proposal_classes.get(proposal_type) if state else None
    level = class_state.level if class_state else "l0"
    if _LEVEL_ORDER.index(level) < _LEVEL_ORDER.index(_SAMPLED_REVIEW_MIN_LEVEL):
        raise ReviewBatchError(
            f"{proposal_type!r} is at autonomy level {level!r}; sampled/batch review requires L3+. "
            "Use `vertex ai-proposals list`/`accept`/`reject` for full individual review, or "
            "`vertex cockpit autonomy-promote` once independent-review evidence justifies L3."
        )
    sample_rate = class_state.sample_rate if class_state else 1.0

    staged = list(load_proposals(program_id, proposal_type=proposal_type, status_filter={"staged"}, programs_root=programs_root))
    if not staged:
        return ReviewBatchResult(
            proposal_type=proposal_type, reviewed_ids=(), auto_approved_ids=(), rejected_ids=(), sample_rate=sample_rate,
        )

    forced_ids = _materiality_forced_ids(proposal_type, staged)
    forced = [p for p in staged if p.id in forced_ids]
    remaining_pool = [p for p in staged if p.id not in forced_ids]
    computed_size = max(1, math.ceil(sample_rate * len(staged)))
    target_size = sample_size if sample_size is not None else computed_size
    target_size = max(0, min(target_size, len(staged)))
    extra_needed = max(0, target_size - len(forced))
    rng = random.Random(seed)
    sampled_extra = rng.sample(remaining_pool, min(extra_needed, len(remaining_pool)))
    sample = forced + sampled_extra
    sample_ids = {p.id for p in sample}
    auto_batch = [p for p in staged if p.id not in sample_ids]

    if dry_run:
        echo_fn(
            f"[dry-run] {proposal_type}: {len(sample)} would be individually reviewed "
            f"(sample_rate={sample_rate:.2f}), {len(auto_batch)} would be auto-approved without individual review."
        )
        return ReviewBatchResult(
            proposal_type=proposal_type, reviewed_ids=tuple(p.id for p in sample),
            auto_approved_ids=tuple(p.id for p in auto_batch), rejected_ids=(), sample_rate=sample_rate,
        )

    actor_identity = _default_actor(actor)
    reviewed_ids: list[str] = []
    rejected_ids: list[str] = []
    for proposal in sample:
        echo_fn(_format_proposal(proposal))
        if confirm_fn(f"Accept {proposal_type} proposal {proposal.id!r}?"):
            _accept_one(
                program_id, proposal_type, proposal, actor_identity=actor_identity,
                org=org, project=project, area_path=area_path, iteration_path=iteration_path, reviewed=True,
                programs_root=programs_root,
            )
            reviewed_ids.append(proposal.id)
        else:
            reason = prompt_fn(f"Rejection reason for {proposal.id!r}")
            _reject_one(program_id, proposal_type, proposal, reason=reason, programs_root=programs_root)
            rejected_ids.append(proposal.id)

    auto_approved_ids: list[str] = []
    for proposal in auto_batch:
        _accept_one(
            program_id, proposal_type, proposal, actor_identity=actor_identity,
            org=org, project=project, area_path=area_path, iteration_path=iteration_path, reviewed=False,
            programs_root=programs_root,
        )
        auto_approved_ids.append(proposal.id)
    if auto_approved_ids:
        echo_fn(
            f"Auto-approved {len(auto_approved_ids)} {proposal_type} proposal(s) without individual review "
            f"(sample_rate={sample_rate:.2f}): {', '.join(auto_approved_ids)}."
        )
    return ReviewBatchResult(
        proposal_type=proposal_type, reviewed_ids=tuple(reviewed_ids),
        auto_approved_ids=tuple(auto_approved_ids), rejected_ids=tuple(rejected_ids), sample_rate=sample_rate,
    )


@app.command("review-batch")
def review_batch_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    type: str = typer.Option(..., "--type", help=f"One of: {', '.join(_SAMPLED_REVIEW_TYPES)} (sampled review is piloted for these two types)."),
    sample_size: int | None = typer.Option(None, "--sample-size", help="Override the computed sample size (operator control/testing)."),
    seed: int | None = typer.Option(None, "--seed", help="Deterministic RNG seed for sample selection (mainly for testing)."),
    actor: str | None = typer.Option(None, "--actor", help="Reviewer identity for the sampled subset. Defaults to the current OS user."),
    org: str | None = typer.Option(None, "--org", help="ADO organization (meeting_action routing only)."),
    project: str | None = typer.Option(None, "--project", help="ADO project (meeting_action routing only)."),
    area_path: str | None = typer.Option(None, "--area-path", help="Optional ADO area path (meeting_action routing only)."),
    iteration_path: str | None = typer.Option(None, "--iteration-path", help="Optional ADO iteration path (meeting_action routing only)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview the sample/auto-approve split without deciding or applying anything."),
) -> None:
    """ADF-W5.12 P4 (Section 8.15.2): sampled/batch review for a proposal
    class at L3+ autonomy. A human individually reviews only a sample of
    the currently-staged batch (random + materiality-weighted); the rest
    are auto-approved by extension of that sample's trust. Requires L3+
    (promote via `vertex cockpit autonomy-promote --to l3 --sample-rate ...`
    first) -- below L3, use `list`/`accept`/`reject` for full review."""
    program_id = program.strip()
    proposal_type = _parse_type(type)
    try:
        result = run_review_batch(
            program_id, proposal_type, sample_size=sample_size, seed=seed, dry_run=dry_run,
            actor=actor, org=org, project=project, area_path=area_path, iteration_path=iteration_path,
            programs_root=PROGRAMS_ROOT,
        )
    except ReviewBatchError as error:
        raise typer.BadParameter(str(error)) from error
    if not result.reviewed_ids and not result.auto_approved_ids and not result.rejected_ids:
        typer.echo(f"No staged {proposal_type} proposals for {program_id!r} to review.")
        raise typer.Exit(code=0)
    if not dry_run:
        typer.echo(
            f"Batch review complete for {proposal_type}: {len(result.reviewed_ids)} individually accepted, "
            f"{len(result.rejected_ids)} individually rejected, {len(result.auto_approved_ids)} auto-approved."
        )
    raise typer.Exit(code=0)


@app.command("flag-regression")
def flag_regression_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    type: str = typer.Option(..., "--type", help=f"One of: {', '.join(REVIEW_PROPOSAL_TYPES)}."),
    id: str = typer.Option(..., "--id", help="Proposal id to flag as a material regression."),
    reason: str = typer.Option(..., "--reason", help="Why this approved proposal's effect turned out to be a material downstream regression."),
) -> None:
    """ADF-W5.12 P4 (Section 8.15.1's 'zero material downstream regressions'
    L3/L4 floor): records that an already-approved proposal (whether
    individually reviewed or auto-approved via `review-batch`'s sampled
    trust extension) turned out to be a material downstream regression --
    the human-facing entry point for the "material regression" signal the
    autonomy ladder's L3/L4 evidence needs and that no code path could ever
    detect on its own (a CLI cannot observe the downstream consequences of
    its own past effects). Feeds `vertex cockpit autonomy-evaluate`'s next
    run: any flagged regression at L3+ immediately demotes the class one
    level (see `proposal_autonomy_ladder.evaluate_promotion`)."""
    program_id = program.strip()
    proposal_type = _parse_type(type)
    proposal = load_proposal(program_id, proposal_type, id, programs_root=PROGRAMS_ROOT)
    if proposal is None:
        raise typer.BadParameter(f"No {proposal_type} proposal with id {id!r} for program {program_id!r}.")
    if proposal.status != "approved":
        raise typer.BadParameter(
            f"Proposal {id!r} has status={proposal.status!r}, not 'approved' -- only an approved (and thus "
            "already-applied) proposal can be flagged as a material regression."
        )
    reason_text = reason.strip()
    if not reason_text:
        raise typer.BadParameter("--reason is required and cannot be blank.")
    record_proposal_event(
        program_id=program_id, proposal_type=proposal_type, proposal_id=id, event="reversed",
        programs_root=PROGRAMS_ROOT, proposed_at=getattr(proposal, "proposed_at", None),
        ai_run_id=getattr(proposal, "ai_run_id", None), rejection_reason=reason_text,
    )
    typer.echo(
        f"Flagged {proposal_type} proposal {id!r} as a material regression: {reason_text}. "
        f"Run `vertex cockpit autonomy-evaluate --program {program_id} --class {proposal_type}` to apply any resulting demotion."
    )
    raise typer.Exit(code=0)


# ---------------------------------------------------------------------------
# ADF-W5.11: the shared typed command service for interactive one-by-one
# review of ALL FIVE proposal types (not just the two `review-batch` pilots
# above) -- the exact same mechanism `risks.py::run_risk_review_session`
# established for risk-register review, applied to AI proposal review.
# Called identically by the `ai-proposals review` CLI command (typer I/O)
# and by `cockpit_tui.py`'s launch action (the loop's own injected I/O), so
# it is structurally impossible for the two paths to diverge. No new
# mutation logic: dispatches to the same `_accept_*`/reject functions
# `accept`/`reject` already use.
# ---------------------------------------------------------------------------
_REJECTORS: dict[ReviewProposalType, Callable[..., ReviewProposal]] = {
    "risk": reject_risk_proposal,
    "meeting_action": reject_meeting_action,
    "top_three": reject_top_three_candidate,
    "governance_decision_brief": reject_governance_decision_brief,
    "dependency_blast_radius": reject_blast_radius_proposal,
}


def _accept_dispatch(
    program_id: str, proposal_type: ReviewProposalType, proposal, *, actor_identity: str, echo_fn: Callable[[str], None],
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    """Dispatches to the type's own accept handler, all five types (unlike
    `_accept_one` above, which is scoped to the two `review-batch` pilot
    types). Optional per-type follow-on fields (ADO routing for
    meeting_action, edition publish for top_three) are intentionally left
    unset here -- each handler already degrades gracefully to an
    informational message when they're absent (see `_accept_meeting_action`/
    `_accept_top_three` above), matching this session's established
    "accept the core mutation now, route/publish separately" shape rather
    than trying to model every follow-on option in a REPL prompt."""
    if proposal_type == "risk":
        _accept_risk(program_id, proposal, actor_identity=actor_identity, echo_fn=echo_fn, programs_root=programs_root)
    elif proposal_type == "meeting_action":
        _accept_meeting_action(
            program_id, proposal, actor_identity=actor_identity,
            org=None, project=None, area_path=None, iteration_path=None, echo_fn=echo_fn, programs_root=programs_root,
        )
    elif proposal_type == "top_three":
        _accept_top_three(
            program_id, proposal, edition=None, by_date=None, ado_link="", anchor="", echo_fn=echo_fn,
            programs_root=programs_root,
        )
    elif proposal_type == "governance_decision_brief":
        _accept_governance_decision_brief(program_id, proposal, echo_fn=echo_fn, programs_root=programs_root)
    else:
        _accept_dependency_blast_radius(program_id, proposal, echo_fn=echo_fn, programs_root=programs_root)


def run_proposal_review_session(
    program_id: str,
    proposal_type: ReviewProposalType,
    *,
    actor: str | None = None,
    confirm_fn: Callable[[str], bool] = typer.confirm,
    prompt_fn: Callable[[str], str] = typer.prompt,
    echo_fn: Callable[[str], None] = typer.echo,
    programs_root: Path = PROGRAMS_ROOT,
) -> int:
    """The interactive one-by-one review loop for a single proposal type:
    preview each staged proposal, confirm accept/reject, and (on reject)
    prompt for a reason -- the same preview/confirm shape
    `run_risk_review_session` established. Returns the number reviewed
    (accepted + rejected)."""
    proposal_type = _parse_type(proposal_type)
    staged = load_proposals(program_id, proposal_type=proposal_type, status_filter={"staged"}, programs_root=programs_root)
    if not staged:
        echo_fn(f"No staged {proposal_type} proposals for {program_id}.")
        return 0

    actor_identity = _default_actor(actor)
    reviewed_count = 0
    for proposal in staged:
        echo_fn(_format_proposal(proposal))
        if confirm_fn(f"Accept {proposal_type} proposal {proposal.id!r}?"):
            _accept_dispatch(
                program_id, proposal_type, proposal, actor_identity=actor_identity, echo_fn=echo_fn,
                programs_root=programs_root,
            )
        else:
            reason = prompt_fn(f"Rejection reason for {proposal.id!r}")
            rejected = _REJECTORS[proposal_type](proposal, reason=reason, programs_root=programs_root)
            stage_proposal(program_id, proposal_type, rejected, programs_root=programs_root)
            echo_fn(f"Rejected {proposal_type} proposal {proposal.id!r}: {reason}")
        reviewed_count += 1
    return reviewed_count


@app.command("review")
def review_proposal_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    type: str = typer.Option(..., "--type", help=f"One of: {', '.join(REVIEW_PROPOSAL_TYPES)}."),
    actor: str | None = typer.Option(None, "--actor", help="Reviewer identity. Defaults to the current OS user."),
) -> None:
    """Interactive one-by-one review of every staged proposal of --type:
    preview, confirm accept/reject, prompt for a rejection reason. For
    per-type follow-on options (ADO routing, edition publish), use
    `ai-proposals accept --id <id> ...` directly after accepting here."""
    proposal_type = _parse_type(type)
    reviewed_count = run_proposal_review_session(
        program.strip(), proposal_type, actor=actor, programs_root=PROGRAMS_ROOT
    )
    if reviewed_count:
        typer.echo(f"Reviewed {reviewed_count} {proposal_type} proposal(s) for {program.strip()}.")
    raise typer.Exit(code=0)


def _format_proposal(proposal) -> str:
    proposal_type = type(proposal).__name__
    summary = getattr(proposal, "causal_title", None) or getattr(proposal, "commitment", None) or getattr(
        proposal, "reason", None
    ) or getattr(proposal, "decision", None) or getattr(proposal, "blast_radius_narrative", None) or ""
    summary = (summary[:80] + "...") if len(summary) > 80 else summary
    return f"[{proposal.status}] {proposal_type} {proposal.id} -- {summary}"


def _parse_type(value: str) -> ReviewProposalType:
    normalized = value.strip().lower()
    if normalized not in REVIEW_PROPOSAL_TYPES:
        raise typer.BadParameter(f"--type must be one of: {', '.join(REVIEW_PROPOSAL_TYPES)} (got {value!r}).")
    return cast(ReviewProposalType, normalized)


def _default_actor(value: str | None) -> str:
    if value is not None and value.strip():
        return value.strip()
    return (os.environ.get("USERNAME") or os.environ.get("USER") or "manual").strip() or "manual"


def _parse_optional_date(value: str | None) -> date | None:
    if value is None or not value.strip():
        return None
    return date.fromisoformat(value.strip())


__all__ = ["app"]
