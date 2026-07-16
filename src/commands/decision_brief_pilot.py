"""ADF-W2.9 P5 (specs/arch-data-fix.md v1.51 deep-dive plan): the blind A/B
comparison harness that pilots ContextCompiler/AISchemaGateway (ADF-W2.7/
W2.8) into ``decision_brief_advisor`` -- the "one live low-risk AI-authored
surface" the plan calls for, chosen over exec-summary because its per-item,
structured (verdict/reasoning/suggested_text) output is far more directly
comparable side-by-side than free-form narrative prose.

Deliberately additive: this module never touches ``decision-brief``'s
existing ``--ai``/``--no-ai`` production output. ``compare`` runs both the
current ad hoc-context path (``advise_on_decision_brief``) and the new
ContextCompiler/AISchemaGateway-wired path
(``advise_on_decision_brief_via_context_gateway``) against the *same*
pending brief, shows each item's two outputs blind (labeled "A"/"B", the
baseline/candidate mapping randomized per item and never revealed), and
records the reviewer's judgment via ``blind_ab_comparison.py``.
``summary`` reports the cumulative win/loss/tie tally so far -- the
evidence ADF-W2.9's eventual production-swap decision depends on.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import random

from pathlib import Path
import typer

from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai.decision_brief_advisor import (
    advise_on_decision_brief,
    advise_on_decision_brief_via_context_gateway,
)
from src.ai.deployment_fallback import FallbackAIClient, resolve_ai_deployments_for_feature
from src.ai.provider import LLMProvider
from src.commands.decision_brief import load_pending_decision_brief
from src.core.blind_ab_comparison import (
    ComparisonChoice,
    read_comparisons,
    record_comparison,
    summarize_comparisons,
)
from src.core.config_loader import REPORTS_ROOT
from src.core.decision_brief_engine import DecisionItem
from src.core.edition_resolver import PROGRAMS_ROOT, load_program
from src.core.snapshot_store import ARCHIVE_ROOT

app = typer.Typer(
    help="ADF-W2.9 P5: blind A/B comparison of decision-brief-advisor's "
    "ContextCompiler/AISchemaGateway-wired pilot path against the current baseline."
)

_SURFACE = "decision_brief_advisor"
_DEPLOYMENT_FALLBACK_ENVS = ("VERTEX_AI_DEPLOYMENT", "VERTEX_EXEC_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT")
_VALID_CHOICES = ("a", "b", "tie", "neither")


class ContextGatewayPilotError(Exception):
    """Raised for user/config errors in the blind A/B comparison harness."""


@dataclass(frozen=True, slots=True)
class ComparisonRunResult:
    program_id: str
    issue_number: int
    compared_item_ids: tuple[str, ...]
    skipped_item_ids: tuple[str, ...]


def run_context_gateway_comparison(
    *,
    edition_name: str,
    issue_number: int | None = None,
    seed: int | None = None,
    deployment_override: str | None = None,
    reports_root: Path = REPORTS_ROOT,
    archive_root: Path = ARCHIVE_ROOT,
    programs_root: Path = PROGRAMS_ROOT,
    client_factory: Callable[..., LLMProvider] | None = None,
    prompt_fn: Callable[[str], str] = typer.prompt,
    echo_fn: Callable[[str], None] = typer.echo,
) -> ComparisonRunResult:
    """Runs both advisor paths over the same pending brief and records a
    blind human judgment per comparable item. Requires a live AI client for
    both sides -- unlike ``decision-brief --ai``, there is no "AI disabled,
    silently skip" degrade here, since a comparison with only one side
    populated isn't a comparison."""
    if get_ai_mode() == AIMode.DISABLED:
        raise ContextGatewayPilotError(
            "AI execution is disabled (--no-ai / AIMode.DISABLED); the comparison harness needs a live client for both paths."
        )

    brief, program_id = load_pending_decision_brief(
        edition_name=edition_name,
        issue_number=issue_number,
        reports_root=reports_root,
        archive_root=archive_root,
    )

    program = load_program(program_id, programs_root=programs_root)
    if program is None or program.ai is None or not program.ai.enabled:
        raise ContextGatewayPilotError(f"Program {program_id!r} does not have AI enabled in program.yaml.")

    deployments = resolve_ai_deployments_for_feature(
        feature_name=_SURFACE,
        primary_candidates=(deployment_override,),
        backup_candidates=(),
        primary_fallback_envs=_DEPLOYMENT_FALLBACK_ENVS,
        backup_fallback_envs=(),
    )
    if not deployments:
        raise ContextGatewayPilotError(
            "No AI deployment is configured. Pass --deployment or set "
            "VERTEX_AI_DEPLOYMENT/VERTEX_EXEC_DEPLOYMENT/AZURE_OPENAI_DEPLOYMENT."
        )
    client = FallbackAIClient(
        deployments=deployments,
        temperature=program.ai.temperature or 0.2,
        budget_usd=program.ai.budget_usd_per_run,
        requests_per_minute=program.ai.requests_per_minute,
        client_factory=client_factory,
    )

    baseline_brief = advise_on_decision_brief(client=client, brief=brief, program_id=program_id, programs_root=programs_root)
    candidate_brief = advise_on_decision_brief_via_context_gateway(
        client=client, brief=brief, program_id=program_id, programs_root=programs_root,
    )
    baseline_by_id = {item.section_id: item for item in baseline_brief.items}
    candidate_by_id = {item.section_id: item for item in candidate_brief.items}

    rng = random.Random(seed)
    compared: list[str] = []
    skipped: list[str] = []
    for item in brief.items:
        baseline_text = _format_advice(baseline_by_id.get(item.section_id))
        candidate_text = _format_advice(candidate_by_id.get(item.section_id))
        if baseline_text is None or candidate_text is None:
            skipped.append(item.section_id)
            continue
        compared.append(item.section_id)
        a_is_candidate = rng.random() < 0.5
        option_a = candidate_text if a_is_candidate else baseline_text
        option_b = baseline_text if a_is_candidate else candidate_text
        echo_fn(f"\n=== {item.section_title} ({item.section_id}) ===")
        echo_fn(f"[A]\n{option_a}\n")
        echo_fn(f"[B]\n{option_b}")
        raw_choice = prompt_fn("Which is better? [a/b/tie/neither]").strip().lower()
        if raw_choice == "a":
            choice: ComparisonChoice = "a"
        elif raw_choice == "b":
            choice = "b"
        elif raw_choice == "tie":
            choice = "tie"
        else:
            choice = "neither"
        record_comparison(
            program_id=program_id,
            surface=_SURFACE,
            item_id=item.section_id,
            option_a_text=option_a,
            option_b_text=option_b,
            a_is_candidate=a_is_candidate,
            choice=choice,
            programs_root=programs_root,
        )

    return ComparisonRunResult(
        program_id=program_id,
        issue_number=brief.issue_number,
        compared_item_ids=tuple(compared),
        skipped_item_ids=tuple(skipped),
    )


def _format_advice(item: DecisionItem | None) -> str | None:
    if item is None or item.verdict is None:
        return None
    parts = [f"Verdict: {item.verdict}", f"Reasoning: {item.verdict_reasoning or '(none)'}"]
    if item.suggested_text:
        parts.append(f"Suggested text: {item.suggested_text}")
    return "\n".join(parts)


@app.command("compare")
def compare_command(
    edition: str = typer.Option(..., "--edition", help="Edition name, e.g. acme_weekly."),
    issue: int | None = typer.Option(None, "--issue", help="Issue number. Defaults to the active issue."),
    seed: int | None = typer.Option(None, "--seed", help="Deterministic RNG seed for the A/B label order (mainly for testing)."),
    deployment: str | None = typer.Option(None, "--deployment", help="Override the AI deployment (else resolved from env/program config)."),
) -> None:
    """Blind-compare the current decision-brief-advisor against its
    ContextCompiler/AISchemaGateway-wired pilot path for every pending item,
    one comparison at a time. Never affects ``decision-brief``'s own
    ``--ai`` output."""
    try:
        result = run_context_gateway_comparison(edition_name=edition, issue_number=issue, seed=seed, deployment_override=deployment)
    except ContextGatewayPilotError as error:
        typer.echo(str(error))
        raise typer.Exit(code=2)
    typer.echo(
        f"\nCompared {len(result.compared_item_ids)} item(s) for Issue {result.issue_number:03d}; "
        f"skipped {len(result.skipped_item_ids)} (no advice from one or both paths)."
    )
    raise typer.Exit(code=0)


@app.command("summary")
def summary_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
) -> None:
    """Report the cumulative blind-comparison tally recorded so far for
    ``decision_brief_advisor``'s context-gateway pilot."""
    records = read_comparisons(program, surface=_SURFACE)
    summary = summarize_comparisons(records, surface=_SURFACE)
    typer.echo(f"decision_brief_advisor context-gateway pilot: {summary.total} comparison(s) recorded.")
    if summary.total:
        typer.echo(f"  Candidate (ContextCompiler/AISchemaGateway) wins: {summary.candidate_wins}")
        typer.echo(f"  Baseline (current ad hoc context) wins: {summary.baseline_wins}")
        typer.echo(f"  Ties: {summary.ties}  Neither: {summary.neither}")
        if summary.candidate_win_rate is not None:
            typer.echo(f"  Candidate win rate (excl. ties/neither): {summary.candidate_win_rate:.0%}")
        else:
            typer.echo("  Candidate win rate: n/a (no decisive comparisons yet)")
    raise typer.Exit(code=0)


__all__ = ["app", "ContextGatewayPilotError", "ComparisonRunResult", "run_context_gateway_comparison"]
