from __future__ import annotations

from pathlib import Path

import typer

from src.ai.intent_router import IntentRouter, IntentRouterError, RoutedInvocation, render_invocation
from src.commands.ask_intents import (
    NAMED_INTENTS,
    citation_only_fallback,
    cluster_misses,
    log_miss,
    match_named_intent,
    render_named_intent,
)


def ask_command(
    request: str = typer.Argument(
        ...,
        metavar="REQUEST",
        help="Natural-language request. With --program, answers directly from ProgramReality.",
    ),
    edition: str = typer.Option("", "--edition", help="Default edition for routed command."),
    program: str = typer.Option("", "--program", help="Program id. Enables named-intent answering."),
    cluster_misses_flag: bool = typer.Option(
        False,
        "--cluster-misses",
        help="Cluster unroutable questions from the miss log and propose new intent routes.",
    ),
    miss_log: Path = typer.Option(
        Path("output/ask_misses.jsonl"),
        "--miss-log",
        help="Path to the miss log sidecar (JSONL).",
        hidden=True,
    ),
) -> None:
    if cluster_misses_flag:
        typer.echo(cluster_misses(path=miss_log))
        raise typer.Exit(code=0)

    normalized_program = program.strip()
    if normalized_program:
        try:
            typer.echo(_answer_with_reality(request, program=normalized_program, miss_log=miss_log))
        except typer.BadParameter as error:
            typer.echo(str(error))
            raise typer.Exit(code=1) from error
        raise typer.Exit(code=0)

    # Default: route to a Vertex command (original behaviour)
    try:
        typer.echo(route_request(request, edition=edition))
    except typer.BadParameter as error:
        typer.echo(str(error))
        raise typer.Exit(code=1) from error


def _answer_with_reality(question: str, *, program: str, miss_log: Path) -> str:
    """Try named-intent match first (Tier 0); fall back to citation-only (no frontier)."""
    intent = match_named_intent(question)
    if intent is not None:
        from src.core.program_reality import ProgramReality
        from src.core.program_fact_store import PROGRAMS_ROOT
        reality = ProgramReality.load(program, programs_root=PROGRAMS_ROOT)
        return render_named_intent(intent, reality)

    # Log the miss
    log_miss(question, path=miss_log)

    # Citation-only fallback: load reality and return relevant facts without AI
    try:
        from src.core.program_reality import ProgramReality
        from src.core.program_fact_store import PROGRAMS_ROOT
        reality = ProgramReality.load(program, programs_root=PROGRAMS_ROOT)
        return citation_only_fallback(question, reality)
    except Exception:
        return (
            f"Question not matched to a named intent. Logged to miss log ({miss_log}).\n"
            f"Named intents: {', '.join(NAMED_INTENTS)}\n"
            "Use --cluster-misses to review accumulated unroutable questions."
        )


def route_request(request: str, *, edition: str = "") -> str:
    try:
        invocation = _build_router().route(request, default_edition=edition)
    except IntentRouterError as error:
        raise typer.BadParameter(f"Intent routing failed: {error}") from error

    return _render_ask_output(invocation)


def _build_router() -> IntentRouter:
    try:
        return IntentRouter.from_environment()
    except IntentRouterError as error:
        if str(error).startswith("VERTEX_AI_DEPLOYMENT or AZURE_OPENAI_DEPLOYMENT not set."):
            return IntentRouter()
        raise


def _render_ask_output(invocation: RoutedInvocation) -> str:
    lines = ["Suggested command:", render_invocation(invocation)]
    if invocation.prompt_version is None:
        lines.append("Routing: deterministic")
    else:
        lines.append(f"Routing: ai ({invocation.prompt_version})")
    if invocation.warnings:
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in invocation.warnings)
    return "\n".join(lines)