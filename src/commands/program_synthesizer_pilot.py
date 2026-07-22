"""ADF-W2.9 (specs/arch-data-fix.md): the blind A/B comparison harness that
pilots ContextCompiler/AISchemaGateway (ADF-W2.7/W2.8) into
``program_synthesizer`` -- mirrors ``decision_brief_pilot.py``'s proven shape
exactly (same imports, same ``compare``/``summary`` command pair, same
``blind_ab_comparison.py`` recording), the second of the two live pilots
ADF-W2.9's own spec text names.

``program_synthesizer`` had a working ContextCompiler-wired candidate path
(``generate_program_synthesis_via_context_gateway``, v1.61) but -- unlike
``decision_brief_advisor`` -- no CLI comparison command at all, so it could
never accrue the real usage/comparison evidence its eventual production-swap
decision depends on. This closes that gap.

Deliberately additive: never touches ``program_synthesizer``'s own
production callers (``top_three_candidate_generator.py`` and any future
newsletter/brief/LT-deck consumer all keep calling ``generate_program_
synthesis``, the ad-hoc-context baseline, unchanged). ``compare`` runs both
paths against the *same* assembled candidate-item pool for one program, shows
the two resulting syntheses blind (labeled "A"/"B", randomized per
invocation), and records the reviewer's judgment. ``summary`` reports the
cumulative tally.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import random

from pathlib import Path
import typer

from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai.deployment_fallback import FallbackAIClient, resolve_ai_deployments_for_feature
from src.ai.program_synthesizer import (
    generate_program_synthesis,
    generate_program_synthesis_via_context_gateway,
)
from src.ai.provider import LLMProvider
from src.core.blind_ab_comparison import (
    ComparisonChoice,
    read_comparisons,
    recommend_swap_decision,
    record_comparison,
    summarize_comparisons,
)
from src.core.edition_resolver import PROGRAMS_ROOT, load_program
from src.core.program_synthesis import ProgramSynthesis, assemble_program_synthesis_request

app = typer.Typer(
    help="ADF-W2.9: blind A/B comparison of program_synthesizer's "
    "ContextCompiler/AISchemaGateway-wired pilot path against the current baseline."
)

_SURFACE = "program_synthesizer"
_DEPLOYMENT_FALLBACK_ENVS = ("VERTEX_AI_DEPLOYMENT", "VERTEX_EXEC_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT")
_VALID_CHOICES = ("a", "b", "tie", "neither")


class ProgramSynthesizerPilotError(Exception):
    """Raised for user/config errors in the blind A/B comparison harness."""


@dataclass(frozen=True, slots=True)
class ComparisonRunResult:
    program_id: str
    compared: bool
    skip_reason: str | None = None


def run_context_gateway_comparison(
    *,
    program_id: str,
    as_of: datetime | None = None,
    seed: int | None = None,
    deployment_override: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
    client_factory: Callable[..., LLMProvider] | None = None,
    prompt_fn: Callable[[str], str] = typer.prompt,
    echo_fn: Callable[[str], None] = typer.echo,
) -> ComparisonRunResult:
    """Runs both synthesis paths over the same assembled candidate-item pool
    and records one blind human judgment. Requires a live AI client for both
    sides -- a comparison with only one side populated isn't a comparison."""
    if get_ai_mode() == AIMode.DISABLED:
        raise ProgramSynthesizerPilotError(
            "AI execution is disabled (--no-ai / AIMode.DISABLED); the comparison harness needs a live client for both paths."
        )

    program = load_program(program_id, programs_root=programs_root)
    if program is None or program.ai is None or not program.ai.enabled:
        raise ProgramSynthesizerPilotError(f"Program {program_id!r} does not have AI enabled in program.yaml.")

    request = assemble_program_synthesis_request(
        program_id, programs_root=programs_root, as_of=as_of or datetime.now(timezone.utc),
    )
    if not request.items:
        return ComparisonRunResult(program_id=program_id, compared=False, skip_reason="no candidate items assembled for this program right now.")

    deployments = resolve_ai_deployments_for_feature(
        feature_name=_SURFACE,
        primary_candidates=(deployment_override,),
        backup_candidates=(),
        primary_fallback_envs=_DEPLOYMENT_FALLBACK_ENVS,
        backup_fallback_envs=(),
    )
    if not deployments:
        raise ProgramSynthesizerPilotError(
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

    baseline_outcome = generate_program_synthesis(request, client=client, programs_root=programs_root)
    candidate_outcome = generate_program_synthesis_via_context_gateway(request, client=client, programs_root=programs_root)
    baseline_text = _format_synthesis(baseline_outcome.synthesis) if baseline_outcome.released else None
    candidate_text = _format_synthesis(candidate_outcome.synthesis) if candidate_outcome.released else None
    if baseline_text is None or candidate_text is None:
        return ComparisonRunResult(
            program_id=program_id, compared=False,
            skip_reason="one or both paths did not release a synthesis this run (discarded/rejected -- see ai_release_audit.jsonl).",
        )

    rng = random.Random(seed)
    a_is_candidate = rng.random() < 0.5
    option_a = candidate_text if a_is_candidate else baseline_text
    option_b = baseline_text if a_is_candidate else candidate_text
    echo_fn(f"\n=== Program synthesis: {program_id} ===")
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
    # BL-D3's pre-registered rubric: a critical error (hallucinated/
    # unsupported claim) is recorded independent of the win/loss/tie
    # choice above -- it disqualifies a swap regardless of win rate.
    critical_raw = prompt_fn("Did either option contain a critical error (hallucinated/unsupported claim)? [y/N]").strip().lower()
    critical_error = critical_raw in ("y", "yes")
    record_comparison(
        program_id=program_id,
        surface=_SURFACE,
        item_id=program_id,
        option_a_text=option_a,
        option_b_text=option_b,
        a_is_candidate=a_is_candidate,
        choice=choice,
        programs_root=programs_root,
        critical_error=critical_error,
    )
    return ComparisonRunResult(program_id=program_id, compared=True)


def _format_synthesis(synthesis: ProgramSynthesis | None) -> str | None:
    if synthesis is None:
        return None
    parts = [f"Through-line: {synthesis.through_line}"]
    if synthesis.long_poles:
        parts.append("Long poles:\n" + "\n".join(f"  - {pole}" for pole in synthesis.long_poles))
    if synthesis.facts:
        parts.append("Facts:\n" + "\n".join(f"  - {fact}" for fact in synthesis.facts))
    if synthesis.inferences:
        parts.append("Inferences:\n" + "\n".join(f"  - {inference}" for inference in synthesis.inferences))
    if synthesis.recommendations:
        parts.append(
            "Recommendations:\n"
            + "\n".join(f"  - {rec.text} (evidence: {', '.join(rec.evidence_refs)})" for rec in synthesis.recommendations)
        )
    return "\n".join(parts)


@app.command("compare")
def compare_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    seed: int | None = typer.Option(None, "--seed", help="Deterministic RNG seed for the A/B label order (mainly for testing)."),
    deployment: str | None = typer.Option(None, "--deployment", help="Override the AI deployment (else resolved from env/program config)."),
) -> None:
    """Blind-compare program_synthesizer's ContextCompiler/AISchemaGateway-
    wired pilot path against its current ad-hoc-context baseline for one
    program. Never affects any production caller of ``generate_program_
    synthesis``."""
    try:
        result = run_context_gateway_comparison(program_id=program.strip(), seed=seed, deployment_override=deployment)
    except ProgramSynthesizerPilotError as error:
        typer.echo(str(error))
        raise typer.Exit(code=2)
    if not result.compared:
        typer.echo(f"Skipped {result.program_id}: {result.skip_reason}")
        raise typer.Exit(code=0)
    typer.echo(f"\nRecorded one comparison for {result.program_id}.")
    raise typer.Exit(code=0)


@app.command("summary")
def summary_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
) -> None:
    """Report the cumulative blind-comparison tally recorded so far for
    ``program_synthesizer``'s context-gateway pilot."""
    records = read_comparisons(program, surface=_SURFACE)
    summary = summarize_comparisons(records, surface=_SURFACE)
    typer.echo(f"program_synthesizer context-gateway pilot: {summary.total} comparison(s) recorded.")
    if summary.total:
        typer.echo(f"  Candidate (ContextCompiler/AISchemaGateway) wins: {summary.candidate_wins}")
        typer.echo(f"  Baseline (current ad hoc context) wins: {summary.baseline_wins}")
        typer.echo(f"  Ties: {summary.ties}  Neither: {summary.neither}")
        if summary.candidate_win_rate is not None:
            typer.echo(
                f"  Candidate win rate (excl. ties/neither): {summary.candidate_win_rate:.0%} "
                f"(95% lower bound: {summary.candidate_win_rate_lower_bound:.0%})"
            )
        else:
            typer.echo("  Candidate win rate: n/a (no decisive comparisons yet)")
        typer.echo(f"  Critical errors: {summary.critical_errors}")
        typer.echo(f"  BL-D3 decision rule recommends: {recommend_swap_decision(summary).upper()}")
    raise typer.Exit(code=0)


__all__ = ["app", "ProgramSynthesizerPilotError", "ComparisonRunResult", "run_context_gateway_comparison"]
