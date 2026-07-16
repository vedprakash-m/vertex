"""ADF-W5.11 (specs/arch-data-fix.md Section 10.3a): ``vertex cockpit tui``
-- the optional interactive terminal cockpit.

Section 10.3a's "must not" list is satisfied by construction: no network
port, no HTTP endpoint, no direct store write, no bypass of confirmation/
outbox/lease/approval/quality-gate/audit paths -- navigation (findings,
explain detail, refresh) reads only, and every mutation launch calls the
SAME real functions the corresponding CLI command uses (``review`` ->
``risks.py::run_risk_review_session``; ``proposals`` ->
``ai_proposals.py::run_proposal_review_session``, both shared typed command
services factored out for exactly this reuse), never a parallel/duplicated
write path.

The loop is injectable (``input_fn``/``output_fn``) rather than calling
``input()``/``typer.echo`` directly, so it is fully unit-testable without a
real TTY -- the same dependency-injection shape this session has used
throughout (``bridge_factory``, ``live_fetch_fn``, ``cache_lookup_fn``, ...).

**Two mutation launches wired** (2026-07-14, ADF-W5.11): ``review`` runs the
stale-risk review session in-loop -- same preview/confirm/status-override/
note prompts as the CLI, same write path (``record_risk_update``/
``save_risk_register``/best-effort ``record_adoption``). ``proposals
<type>`` runs the interactive one-by-one AI-proposal review session for any
of the five AISchemaGateway-pattern proposal types (risk, meeting_action,
top_three, governance_decision_brief, dependency_blast_radius) -- same
preview/confirm/reject-reason prompts and same accept/reject dispatch
``ai-proposals review`` uses. Both refresh the cockpit's read model after
completing, per Section 10.3a's "refresh its read model after a completed
command" bullet. ``program_synthesis`` has no human accept/reject staging
flow to launch (it is release-gated, not human-reviewed -- see
``ai_proposals.py``'s own module docstring), so it is out of scope here by
design, not deferred.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from src.core.cockpit_builder import build_cockpit_snapshot
from src.core.cockpit_models import CockpitFinding, CockpitSnapshot
from src.core.edition_resolver import PROGRAMS_ROOT

InputFn = Callable[[str], str]
OutputFn = Callable[[str], None]

_PROMPT = (
    "Enter a finding number to explain, 'review' to review stale risks, "
    "'proposals <type>' to review AI proposals, 'r' to refresh, or 'q' to quit: "
)
_QUIT_COMMANDS = frozenset({"q", "quit", "exit"})
_REFRESH_COMMANDS = frozenset({"r", "refresh"})
_REVIEW_RISKS_COMMANDS = frozenset({"review", "review-risks"})
_PROPOSAL_REVIEW_COMMAND_PREFIXES = frozenset({"proposals", "review-proposals"})


def _render_summary(snapshot: CockpitSnapshot) -> str:
    program = snapshot.program_summary
    readiness = f"{program.readiness_percent}%" if program.readiness_percent is not None else "not measured yet"
    lines = [
        f"Vertex Cockpit TUI -- {snapshot.program_id} (as of {snapshot.as_of.isoformat()})",
        f"Overall risk: {program.overall_risk.upper()}  Readiness: {readiness}",
        "",
        "Findings:",
    ]
    if not snapshot.findings:
        lines.append("  (none)")
    for index, finding in enumerate(snapshot.findings, start=1):
        lines.append(f"  [{index}] ({finding.status}) {finding.summary}")
    return "\n".join(lines)


def _render_finding_detail(finding: CockpitFinding) -> str:
    from src.commands.cockpit import _render_finding_explanation

    return _render_finding_explanation(finding)


def _launch_risk_review(
    program_id: str,
    *,
    input_fn: InputFn,
    output_fn: OutputFn,
    programs_root: Path,
) -> None:
    """Launch the stale-risk review session through the shared typed
    command service ``risks.py::run_risk_review_session`` -- the exact same
    function ``vertex risks review`` calls, with this loop's own injected
    I/O standing in for typer's. No mutation-path bypass: the write goes
    through ``record_risk_update``/``save_risk_register``/`record_adoption``
    exactly as it would from the CLI."""
    from src.commands.risks import RiskReviewIO, _default_actor, run_risk_review_session

    def _confirm(message: str) -> bool:
        raw = input_fn(f"{message} [Y/n]: ").strip().lower()
        return raw in ("", "y", "yes")

    def _prompt(message: str) -> str:
        return input_fn(f"{message}: ")

    io = RiskReviewIO(confirm=_confirm, prompt=_prompt, echo=output_fn)
    reviewed_count = run_risk_review_session(
        program_id,
        mark_reviewed=False,
        review_actor=_default_actor(None),
        io=io,
        programs_root=programs_root,
    )
    output_fn(f"Reviewed {reviewed_count} stale risk{'s' if reviewed_count != 1 else ''} in {program_id}.")


def _launch_proposal_review(
    program_id: str,
    proposal_type: str | None,
    *,
    input_fn: InputFn,
    output_fn: OutputFn,
    programs_root: Path,
) -> None:
    """Launch the interactive one-by-one AI-proposal review session through
    the shared typed command service
    ``ai_proposals.py::run_proposal_review_session`` -- the exact same
    function ``vertex ai-proposals review`` calls. No mutation-path bypass:
    accept/reject go through the same ``_accept_*``/reject functions the
    CLI's own `accept`/`reject` commands use."""
    from src.core.ai_review_proposal_store import REVIEW_PROPOSAL_TYPES
    from src.commands.ai_proposals import run_proposal_review_session

    if not proposal_type:
        proposal_type = input_fn(f"Proposal type ({'/'.join(REVIEW_PROPOSAL_TYPES)}): ").strip().lower()
    if proposal_type not in REVIEW_PROPOSAL_TYPES:
        output_fn(f"Unrecognized proposal type {proposal_type!r}. Must be one of: {', '.join(REVIEW_PROPOSAL_TYPES)}.")
        return

    def _confirm(message: str) -> bool:
        raw = input_fn(f"{message} [Y/n]: ").strip().lower()
        return raw in ("", "y", "yes")

    def _prompt(message: str) -> str:
        return input_fn(f"{message}: ")

    reviewed_count = run_proposal_review_session(
        program_id, proposal_type, confirm_fn=_confirm, prompt_fn=_prompt, echo_fn=output_fn, programs_root=programs_root,
    )
    if reviewed_count:
        output_fn(f"Reviewed {reviewed_count} {proposal_type} proposal(s) for {program_id}.")


def run_cockpit_tui_loop(
    program_id: str,
    *,
    edition_id: str | None = None,
    input_fn: InputFn,
    output_fn: OutputFn,
    programs_root: Path = PROGRAMS_ROOT,
    max_iterations: int | None = None,
) -> None:
    """The core, testable REPL loop. ``max_iterations`` bounds the loop for
    tests/non-interactive callers; production callers omit it (loop until
    quit)."""
    snapshot = build_cockpit_snapshot(program_id, programs_root=programs_root, edition_id=edition_id)
    output_fn(_render_summary(snapshot))

    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        try:
            raw = input_fn(_PROMPT).strip().lower()
        except (EOFError, KeyboardInterrupt):
            # Piped-input exhaustion or Ctrl+C -- exit the loop cleanly,
            # never crash a read-only navigation surface.
            return

        if raw in _QUIT_COMMANDS:
            return

        if raw in _REFRESH_COMMANDS:
            snapshot = build_cockpit_snapshot(program_id, programs_root=programs_root, edition_id=edition_id)
            output_fn(_render_summary(snapshot))
            continue

        if raw in _REVIEW_RISKS_COMMANDS:
            _launch_risk_review(program_id, input_fn=input_fn, output_fn=output_fn, programs_root=programs_root)
            # Section 10.3a: "refresh its read model after a completed command."
            snapshot = build_cockpit_snapshot(program_id, programs_root=programs_root, edition_id=edition_id)
            output_fn(_render_summary(snapshot))
            continue

        tokens = raw.split()
        if tokens and tokens[0] in _PROPOSAL_REVIEW_COMMAND_PREFIXES:
            requested_type = tokens[1] if len(tokens) > 1 else None
            _launch_proposal_review(
                program_id, requested_type, input_fn=input_fn, output_fn=output_fn, programs_root=programs_root,
            )
            # Section 10.3a: "refresh its read model after a completed command."
            snapshot = build_cockpit_snapshot(program_id, programs_root=programs_root, edition_id=edition_id)
            output_fn(_render_summary(snapshot))
            continue

        try:
            index = int(raw)
        except ValueError:
            output_fn(f"Unrecognized input {raw!r}. Enter a finding number, 'r', or 'q'.")
            continue

        if not (1 <= index <= len(snapshot.findings)):
            output_fn(f"No finding [{index}]. Enter a number between 1 and {len(snapshot.findings)}.")
            continue

        output_fn(_render_finding_detail(snapshot.findings[index - 1]))


__all__ = ["run_cockpit_tui_loop"]
