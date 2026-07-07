from __future__ import annotations

import csv
from typing import Any, Literal, cast
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import typer

from src.core.feedback.salience_modeler import SalienceEvent, append_salience_event, predict_salience_event_weights
from src.core.ai_proposal_store import load_ai_proposals
from src.core.edition_resolver import PROGRAMS_ROOT, load_program
from src.core.knowledge_store import load_program_knowledge
from src.core.models import Confidence
from src.core.models_v2 import AIProposal, AIProposalStatus, Signal, SignalReviewDecision, SignalThreadLink
from src.core.signal_review import signal_needs_review
from src.core.signal_classification import classify_signal as _classify_signal
from src.core.signal_dedup import is_duplicate_signal
from src.core.signal_ranking import sort_signals_for_ai_context
from src.core.store_factory import build_signal_store, build_signal_store_for_program_id


app = typer.Typer(help="List and review journal signals.", invoke_without_command=True)


@app.callback(invoke_without_command=True)
def signals_command(
    ctx: typer.Context,
    program: str | None = typer.Option(None, "--program", help="Program id, e.g. myprogram."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if program is None or not program.strip():
        raise typer.BadParameter("--program is required.")
    _print_pending_signals(program.strip(), programs_root=PROGRAMS_ROOT, format=format)
    raise typer.Exit(code=0)


@app.command("review")
def review_signals_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    reviewer: str | None = typer.Option(None, "--reviewer", help="Reviewer alias. Defaults to the current OS user."),
) -> None:
    signal_store = build_signal_store_for_program_id(program, programs_root=PROGRAMS_ROOT)
    queue = _pending_signals(program, programs_root=PROGRAMS_ROOT, signal_store=signal_store)
    pending_ai_proposals = _pending_ai_proposals(program, programs_root=PROGRAMS_ROOT)
    if not queue and not pending_ai_proposals:
        typer.echo(f"No pending signals for {program}.")
        raise typer.Exit(code=0)

    resolved_reviewer = (reviewer or os.environ.get("USERNAME") or "unknown").strip() or "unknown"
    reviewed = 0
    for signal in queue:
        typer.echo("")
        typer.echo(
            f"{signal.id} | {signal.timestamp.isoformat()} | {signal.source} | {signal.workstream_id or '-'} | confidence {signal.confidence.value.lower()}"
        )
        typer.echo(signal.text)
        choice = typer.prompt(
            "Decision [a]pprove/[d]ismiss/[f]defer/[s]kip/[q]uit",
            default="s",
        ).strip().lower()
        if choice in {"q", "quit"}:
            break
        if choice in {"", "s", "skip"}:
            continue
        decision = _resolve_decision(choice)
        note: str | None = None
        if decision != "approved":
            note_value = typer.prompt("Note (optional)", default="").strip()
            note = note_value or None
        signal_store.append_review(
            program,
            SignalReviewDecision(
                signal_id=signal.id,
                decision=decision,
                reviewed_at=datetime.now(timezone.utc),
                reviewed_by=resolved_reviewer,
                note=note,
            ),
        )
        if decision in {"approved", "dismissed"} and signal.source == "vertex/catchup" and signal.workstream_id:
            salience_action = "acted" if decision == "approved" else "dismissed"
            weight_before, weight_after = predict_salience_event_weights(
                program,
                workstream_id=signal.workstream_id,
                action=salience_action,
                programs_root=PROGRAMS_ROOT,
            )
            append_salience_event(
                program,
                SalienceEvent(
                    event_id=str(uuid5(NAMESPACE_URL, f"{program}|salience|{signal.id}|{salience_action}|{resolved_reviewer}")),
                    recorded_at=datetime.now(timezone.utc),
                    anomaly_id=signal.id,
                    workstream_id=signal.workstream_id,
                    action=salience_action,
                    work_item_id=_extract_signal_work_item_id(signal),
                    decision_latency_ms=None,
                    weight_before=weight_before,
                    weight_after=weight_after,
                    confirmed_within_30d=None,
                ),
                programs_root=PROGRAMS_ROOT,
            )
        reviewed += 1

    if pending_ai_proposals:
        typer.echo("")
        typer.echo(_format_pending_ai_proposals(program, pending_ai_proposals))
        typer.echo("Review pending AI-proposed risks with `vertex override --edition <edition>`.")

    typer.echo(f"Reviewed {reviewed} signal(s) for {program}.")
    raise typer.Exit(code=0)


@app.command("add")
def add_signal_command(
    text: str = typer.Argument(..., help="Manual signal text."),
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    workstream: str | None = typer.Option(None, "--workstream", help="Optional workstream id for the signal."),
    ref: list[str] | None = typer.Option(None, "--ref", help="Optional entity reference. Repeat for multiple refs."),
) -> None:
    resolved_program = program.strip()
    if not resolved_program:
        raise typer.BadParameter("--program is required.")
    if not (PROGRAMS_ROOT / resolved_program).exists():
        raise typer.BadParameter(f"Program '{resolved_program}' does not exist at {PROGRAMS_ROOT / resolved_program}.")

    normalized_text = " ".join(text.split()).strip()
    if not normalized_text:
        raise typer.BadParameter("Signal text must not be empty.")

    signal_store = build_signal_store_for_program_id(resolved_program, programs_root=PROGRAMS_ROOT)
    current_time = datetime.now(timezone.utc)
    refs = tuple(entry.strip() for entry in (ref or []) if entry.strip())
    signal = Signal(
        id=_build_manual_signal_id(
            program_id=resolved_program,
            workstream_id=workstream.strip() if workstream is not None and workstream.strip() else None,
            refs=refs,
            text=normalized_text,
            timestamp=current_time,
        ),
        timestamp=current_time,
        source="manual",
        program_id=resolved_program,
        workstream_id=workstream.strip() if workstream is not None and workstream.strip() else None,
        entity_refs=refs,
        text=normalized_text,
        raw_ref=None,
        confidence=Confidence.HIGH,
        metadata={"author": _default_reviewer_identity()},
    )
    existing_signals = _current_week_signals(
        resolved_program,
        current_time,
        programs_root=PROGRAMS_ROOT,
        signal_store=signal_store,
    )
    if is_duplicate_signal(signal, existing_signals):
        typer.echo(f"No signal added for {resolved_program}; matching manual signal already exists this week.")
        raise typer.Exit(code=0)

    signal_store.append(_classify_signal(signal))
    signal_store.append_review(
        resolved_program,
        SignalReviewDecision(
            signal_id=signal.id,
            decision="approved",
            reviewed_at=current_time,
            reviewed_by=_default_reviewer_identity(),
            note=None,
        ),
    )
    typer.echo(f"Added manual signal {signal.id} for {resolved_program} and auto-approved it.")
    raise typer.Exit(code=0)


@app.command("link")
def link_signals_command(
    signal: list[str] = typer.Option(..., "--signal", help="Signal id to add to the thread. Repeat for multiple ids."),
    thread: str | None = typer.Option(None, "--thread", help="Optional thread name. Defaults to a deterministic generated id."),
    program: str | None = typer.Option(None, "--program", help="Optional program id. If omitted, Vertex searches all programs."),
) -> None:
    signal_ids = _normalize_signal_ids(signal)
    program_id, matched_signals = _resolve_thread_targets(signal_ids, requested_program=program, programs_root=PROGRAMS_ROOT)
    signal_store = build_signal_store_for_program_id(program_id, programs_root=PROGRAMS_ROOT)
    thread_id = _resolve_thread_id(signal_ids, thread)
    linked_at = datetime.now(timezone.utc)
    linked_by = _default_reviewer_identity()

    for matched_signal in matched_signals:
        signal_store.append_thread(
            program_id,
            SignalThreadLink(
                signal_id=matched_signal.id,
                thread_id=thread_id,
                linked_at=linked_at,
                linked_by=linked_by,
            ),
        )

    typer.echo(f"Linked {len(matched_signals)} signal(s) in {program_id} under thread '{thread_id}'.")
    raise typer.Exit(code=0)


def _print_pending_signals(program_id: str, *, programs_root: Path, format: str) -> None:
    signal_store = build_signal_store_for_program_id(program_id, programs_root=programs_root)
    queue = _pending_signals(program_id, programs_root=programs_root, signal_store=signal_store)
    pending_ai_proposals = _pending_ai_proposals(program_id, programs_root=programs_root)
    review_states = signal_store.read_reviews(program_id)
    pending_signal_rows = [
        asdict(signal)
        | {
            "review_state": (_rs.decision if (_rs := review_states.get(signal.id)) is not None else "unreviewed"),
        }
        for signal in queue
    ]
    pending_ai_rows = [asdict(proposal) for proposal in pending_ai_proposals]

    if format == "json":
        typer.echo(
            json.dumps(
                {
                    "program_id": program_id,
                    "pending_signals": pending_signal_rows,
                    "pending_ai_proposals": pending_ai_rows,
                },
                indent=2,
                sort_keys=True,
                default=_json_default,
            )
        )
        return
    if format == "csv":
        typer.echo(render_pending_signals_csv(pending_signal_rows, pending_ai_rows), nl=False)
        return
    if format != "human":
        raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")

    if not queue and not pending_ai_proposals:
        typer.echo(f"No pending signals for {program_id}.")
        return

    if queue:
        typer.echo(f"PENDING SIGNALS — {program_id} ({len(queue)})")
        for signal in queue:
            state = review_states.get(signal.id)
            label = state.decision if state is not None else "unreviewed"
            typer.echo(
                f"- {signal.id} | {signal.timestamp.isoformat()} | {signal.source} | {signal.workstream_id or '-'} | {label} | confidence {signal.confidence.value.lower()}"
            )
            typer.echo(f"  {signal.text}")
    if pending_ai_proposals:
        if queue:
            typer.echo("")
        typer.echo(_format_pending_ai_proposals(program_id, pending_ai_proposals))


def render_pending_signals_csv(pending_signals: list[dict[str, object]], pending_ai_proposals: list[dict[str, object]]) -> str:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        (
            "entry_type",
            "id",
            "program_id",
            "timestamp",
            "source",
            "workstream_id",
            "review_state",
            "confidence",
            "status",
            "created_at",
            "proposed_risk",
            "text",
            "overall_assessment",
            "entity_refs",
            "evidence_refs",
        )
    )
    for signal in pending_signals:
        writer.writerow(
            (
                "signal",
                signal["id"],
                signal["program_id"],
                _csv_datetime(signal.get("timestamp")),
                signal["source"],
                signal["workstream_id"] or "",
                signal["review_state"],
                signal["confidence"],
                "",
                "",
                "",
                signal["text"],
                "",
                "|".join(str(r) for r in (signal.get("entity_refs") or ())),  # type: ignore[attr-defined]
                "",
            )
        )
    for proposal in pending_ai_proposals:
        synthesis: dict[str, object] = proposal["synthesis"]  # type: ignore[assignment]
        writer.writerow(
            (
                "ai_proposal",
                proposal["id"],
                "",
                "",
                "",
                proposal["workstream_id"],
                "",
                synthesis["confidence"],
                proposal["status"],
                _csv_datetime(proposal.get("created_at")),
                synthesis["proposed_risk"],
                "",
                synthesis["overall_assessment"],
                "",
                "|".join(str(r) for r in cast(list[Any], synthesis.get("evidence_refs") or [])),
            )
        )
    return buffer.getvalue()


def _pending_signals(program_id: str, *, programs_root: Path, signal_store=None) -> tuple[Signal, ...]:
    store = signal_store or build_signal_store_for_program_id(program_id, programs_root=programs_root)
    review_states = store.read_reviews(program_id)
    pending = []
    for signal in store.read(program_id):
        if signal_needs_review(signal, review_states):
            pending.append(signal)
    program = load_program(program_id, programs_root=programs_root)
    knowledge = load_program_knowledge(program_id, programs_root=programs_root)
    return sort_signals_for_ai_context(
        tuple(pending),
        people_directory=knowledge.people_directory,
        source_confidence_order=program.source_confidence_order if program is not None else (),
    )


def _pending_ai_proposals(program_id: str, *, programs_root: Path) -> tuple[AIProposal, ...]:
    return load_ai_proposals(
        program_id,
        status=AIProposalStatus.PENDING,
        programs_root=programs_root,
    )


def _format_pending_ai_proposals(program_id: str, proposals: tuple[AIProposal, ...]) -> str:
    lines = [f"PENDING AI PROPOSALS — {program_id} ({len(proposals)})"]
    for proposal in proposals:
        lines.append(
            f"- {proposal.id} | {proposal.workstream_id} | risk {proposal.synthesis.proposed_risk.value} | confidence {proposal.synthesis.confidence.value}"
        )
        lines.append(f"  {proposal.synthesis.overall_assessment}")
    return "\n".join(lines)


def _normalize_signal_ids(values: list[str]) -> tuple[str, ...]:
    signal_ids = tuple(dict.fromkeys(value.strip() for value in values if value.strip()))
    if len(signal_ids) < 2:
        raise typer.BadParameter("Provide at least two --signal values.")
    return signal_ids


def _resolve_thread_targets(
    signal_ids: tuple[str, ...],
    *,
    requested_program: str | None,
    programs_root: Path,
) -> tuple[str, tuple[Signal, ...]]:
    if requested_program is not None and not requested_program.strip():
        raise typer.BadParameter("--program must not be empty when provided.")

    program_ids = (
        (requested_program.strip(),)
        if requested_program is not None
        else tuple(sorted(path.name for path in programs_root.iterdir() if path.is_dir()))
    )
    if not program_ids:
        raise typer.BadParameter("No programs are available to search for the requested signals.")

    matched_by_program: dict[str, dict[str, Signal]] = {}
    # PB-40: hoist the signal-store construction out of the per-program loop
    # so a Q: drive hit happens once, not once-per-program.
    signal_store = build_signal_store(programs_root=programs_root)
    for program_id in program_ids:
        signal_map = {entry.id: entry for entry in signal_store.read(program_id)}
        matches = {signal_id: signal_map[signal_id] for signal_id in signal_ids if signal_id in signal_map}
        if matches:
            matched_by_program[program_id] = matches

    if not matched_by_program:
        raise typer.BadParameter(f"No signals matched: {', '.join(signal_ids)}")

    complete_programs = {
        program_id: matches
        for program_id, matches in matched_by_program.items()
        if len(matches) == len(signal_ids)
    }
    if len(complete_programs) != 1:
        if requested_program is None and len(complete_programs) > 1:
            raise typer.BadParameter("Signal ids are ambiguous across multiple programs; rerun with --program.")
        matched_programs = next(iter(matched_by_program))
        missing = [signal_id for signal_id in signal_ids if signal_id not in matched_by_program[matched_programs]]
        raise typer.BadParameter(f"Signals must all exist in the same program. Missing: {', '.join(missing)}")

    program_id, matches = next(iter(complete_programs.items()))
    return program_id, tuple(matches[signal_id] for signal_id in signal_ids)


def _resolve_thread_id(signal_ids: tuple[str, ...], thread_name: str | None) -> str:
    if thread_name is not None:
        normalized = " ".join(thread_name.split()).strip()
        if not normalized:
            raise typer.BadParameter("--thread must not be empty when provided.")
        return normalized
    payload = "|".join(sorted(signal_ids))
    return f"thread-{uuid5(NAMESPACE_URL, payload).hex[:12]}"


def _resolve_decision(value: str) -> Literal["approved", "dismissed", "deferred"]:
    mapping = {
        "a": "approved",
        "approve": "approved",
        "approved": "approved",
        "d": "dismissed",
        "dismiss": "dismissed",
        "dismissed": "dismissed",
        "f": "deferred",
        "defer": "deferred",
        "deferred": "deferred",
    }
    resolved = mapping.get(value)
    if resolved is None:
        raise typer.BadParameter(f"Unsupported review decision '{value}'.")
    return cast(Literal["approved", "dismissed", "deferred"], resolved)


def _current_week_signals(
    program_id: str,
    current_time: datetime,
    *,
    programs_root: Path,
    signal_store=None,
) -> tuple[Signal, ...]:
    store = signal_store or build_signal_store_for_program_id(program_id, programs_root=programs_root)
    return tuple(
        signal
        for signal in store.read(program_id)
        if signal.timestamp.isocalendar()[:2] == current_time.isocalendar()[:2]
    )


def _build_manual_signal_id(
    *,
    program_id: str,
    workstream_id: str | None,
    refs: tuple[str, ...],
    text: str,
    timestamp: datetime,
) -> str:
    text_hash = hashlib.sha256(text.lower().encode("utf-8")).hexdigest()
    payload = f"{program_id}|manual|{workstream_id or ''}|{'|'.join(refs)}|{text_hash}|{timestamp.isoformat()}"
    return str(uuid5(NAMESPACE_URL, payload))


def _extract_signal_work_item_id(signal: Signal) -> int | None:
    for reference in signal.entity_refs:
        normalized = str(reference).strip()
        if normalized.upper().startswith("WI:"):
            candidate = normalized.split(":", 1)[1].strip()
            if candidate.isdigit():
                return int(candidate)
    return None


def _default_reviewer_identity() -> str:
    return (os.environ.get("USERNAME") or os.environ.get("USER") or "manual").strip() or "manual"


def _csv_datetime(value: object) -> str:
    if value is None:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _json_default(value: object) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value)!r} is not JSON serializable")