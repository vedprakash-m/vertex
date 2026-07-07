from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

import typer

from src.ai.cost_guard import load_latest_run_state
from src.commands.decisions import apply_decision_ask_nudge, plan_decision_ask_nudge
from src.commands.escalate import apply_decision_ask_escalation, plan_decision_ask_escalation, render_escalation_preview_plaintext
from src.commands.notify import render_notify_preview_plaintext
from src.commands.readiness import fetch_readiness_snapshot, render_readiness_snapshot_output
from src.core.brief_intervention_store import BriefInterventionResolution, BriefInterventionStatus, append_brief_intervention_resolution, load_brief_intervention_resolutions
from src.core.ask_lifecycle import build_decision_ask_lifecycle_proposals, evaluate_decision_ask_lifecycle
from src.core.analytics_store import load_contradiction_state
from src.core.catchup_state_store import load_catchup_state
from src.core.claim_tracker import assess_claim_entries, load_open_claims, load_open_decision_asks
from src.core.edition_resolver import PROGRAMS_ROOT, get_program_output_dir, get_program_output_root
from src.core.engms_content import summarize_engms_page
from src.core.exceptions import AuthError, ConfigError, QueryError, StateError
from src.core.feedback.calibration_router import load_forecast_calibration_modifier
from src.core.incident_learning_synthesizer import IncidentClassPattern, IncidentRefPattern, build_incident_class_patterns, build_incident_ref_patterns, normalize_incident_learning_summary
from src.core.incident_journal_store import read_incident_entries
from src.core.feedback.salience_modeler import load_author_salience
from src.core.intervention_ranker import InterventionProposal, rank_brief_interventions
from src.core.knowledge_store import load_program_knowledge, select_engms_pages
from src.core.models import Confidence
from src.core.models_v2 import ContradictionPacket, IncidentEntry


@dataclass(frozen=True, slots=True)
class BriefLine:
    priority: int
    text: str


@dataclass(frozen=True, slots=True)
class BriefReport:
    program_id: str
    generated_at: datetime
    now_lines: tuple[str, ...]
    watch_lines: tuple[str, ...]
    staged_lines: tuple[str, ...]
    reference_lines: tuple[str, ...] = ()
    staged_interventions: tuple[InterventionProposal, ...] = ()


def brief_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    today: bool = typer.Option(False, "--today", help="Use the current session-scoped local state surfaces."),
    approve: str | None = typer.Option(None, "--approve", help="Approve a staged intervention id from the brief output."),
    dismiss: str | None = typer.Option(None, "--dismiss", help="Dismiss a staged intervention id from the brief output."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Render to stdout without writing the brief artifact."),
) -> None:
    del today
    if approve is not None and dismiss is not None:
        raise typer.BadParameter("Use only one of --approve or --dismiss per invocation.")

    program_id = program.strip()
    report = build_brief(program_id, programs_root=PROGRAMS_ROOT)
    if approve is not None or dismiss is not None:
        requested_id = (approve or dismiss or "").strip()
        proposal = _find_staged_intervention(report, requested_id)
        if proposal is None:
            raise typer.BadParameter(f"Staged intervention '{requested_id}' is not active in the current brief.")
        status = BriefInterventionStatus.APPROVED if approve is not None else BriefInterventionStatus.DISMISSED
        applied_outputs: tuple[str, ...] = ()
        if approve is not None:
            try:
                applied_outputs = _apply_supported_intervention(
                    proposal,
                    programs_root=PROGRAMS_ROOT,

                    dry_run=dry_run,
                )
            except (AuthError, ConfigError, FileNotFoundError, QueryError, StateError) as error:
                typer.echo(str(error))
                raise typer.Exit(code=2)
        elif dry_run:
            typer.echo(f"Dry run: would mark staged intervention {requested_id} as {status.value}.")
            typer.echo(f"Apply command: {proposal.command}")
            raise typer.Exit(code=0)

        path = append_brief_intervention_resolution(
            program_id,
            proposal_id=proposal.proposal_id,
            title=proposal.title,
            command=proposal.command,
            source_hash=proposal.source_hash,
            status=status,
            programs_root=PROGRAMS_ROOT,
        )
        typer.echo(f"Recorded {status.value} for staged intervention {requested_id}.")
        typer.echo(f"State: {path}")
        for line in applied_outputs:
            typer.echo(line)
        typer.echo(f"Apply command: {proposal.command}")
        report = build_brief(program_id, programs_root=PROGRAMS_ROOT)

    rendered = render_brief(report)
    typer.echo(rendered)
    if not dry_run:
        path = write_brief_artifact(report, rendered, programs_root=PROGRAMS_ROOT)
        typer.echo(f"Saved brief: {path}")
    raise typer.Exit(code=0)


def build_brief(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,

    as_of: datetime | None = None,
) -> BriefReport:
    generated_at = _ensure_utc(as_of or _utc_now())
    catchup_state = load_catchup_state(program_id, programs_root=programs_root)
    claim_assessments = assess_claim_entries(
        load_open_claims(program_id, programs_root=programs_root),
        items=(),
        as_of=generated_at,
    )
    decision_asks = load_open_decision_asks(program_id, programs_root=programs_root)
    salience = load_author_salience(program_id, programs_root=programs_root)
    calibration_modifier = load_forecast_calibration_modifier(program_id, programs_root=programs_root)
    contradiction_packets = load_contradiction_state(program_id, programs_root=programs_root)
    incident_entries = read_incident_entries(
        program_id,
        start=generated_at - timedelta(days=14),
        end=generated_at,
        programs_root=programs_root,
    )
    weight_by_workstream = {
        entry.workstream_id: entry.attention_weight
        for entry in (salience.workstreams if salience is not None else ())
    }
    active_workstream_ids = tuple(
        sorted(
            {
                claim.workstream_id
                for claim in load_open_claims(program_id, programs_root=programs_root)
                if claim.workstream_id
            }
            | {
                entry.workstream_id
                for entry in incident_entries
                if entry.workstream_id
            }
        )
    )
    knowledge = load_program_knowledge(program_id, programs_root=programs_root)
    reference_lines = tuple(
        f"- {page.title} | {page.url} | {summarize_engms_page(page)}"
        for page in select_engms_pages(knowledge, program_id=program_id, workstream_ids=active_workstream_ids)[:5]
    )

    now_items: list[BriefLine] = []
    watch_items: list[BriefLine] = []

    now_items.extend(_build_cost_guard_brief_lines(program_id, programs_root=programs_root))

    if catchup_state is not None and catchup_state.last_result is not None:
        result = catchup_state.last_result
        catchup_text = (
            f"Catchup since {result.since.strftime('%Y-%m-%d %H:%M')}Z: "
            f"{result.new_signals} new signal(s), {result.discovered_signals} discovered, {result.scanned_items} item(s) scanned."
        )
        target = now_items if result.new_signals > 0 else watch_items
        target.append(BriefLine(priority=1000 + result.new_signals, text=catchup_text))
        if result.total_changed_items is not None and result.total_changed_items > result.scanned_items:
            now_items.append(
                BriefLine(
                    priority=995,
                    text=(
                        f"Catchup truncated after {result.scanned_items} of {result.total_changed_items} changed item(s). "
                        "Run vertex gather for full refresh."
                    ),
                )
            )
        for index, summary in enumerate(result.new_signal_summaries):
            priority = 990 - index
            if index < len(result.catchup_events):
                event = result.catchup_events[index]
                priority += {"alert": 20, "warn": 10}.get(event.severity, 0)
            target.append(
                BriefLine(
                    priority=priority,
                    text=f"Catchup detail: {summary}",
                )
            )

    for assessment in claim_assessments:
        claim = assessment.claim
        if claim.due_date is None:
            continue
        days_until_due = (claim.due_date - generated_at.date()).days
        salience_weight = weight_by_workstream.get(claim.workstream_id or "", 0.2)
        claim_text = f"Claim {claim.id} ({claim.workstream_id or '-'}) due {claim.due_date.isoformat()}: {claim.text}"
        if assessment.effective_status in {"stale", "contradicted"} or days_until_due <= 7:
            now_items.append(
                BriefLine(
                    priority=_priority_for_claim(days_until_due, salience_weight),
                    text=claim_text,
                )
            )
        elif days_until_due <= 14:
            watch_items.append(
                BriefLine(
                    priority=_priority_for_claim(days_until_due, salience_weight),
                    text=claim_text,
                )
            )

    for proposal in build_decision_ask_lifecycle_proposals(decision_asks, as_of=generated_at):
        ask_text = _render_decision_ask_brief_line(proposal)
        if proposal.stage.value == "watch":
            watch_items.append(BriefLine(priority=400, text=ask_text))
        else:
            now_items.append(BriefLine(priority=700, text=ask_text))

    if calibration_modifier is not None:
        for workstream_id, slip_modifier in sorted(
            calibration_modifier.workstream_modifiers.items(),
            key=lambda item: (-item[1], item[0]),
        ):
            attention_weight = weight_by_workstream.get(workstream_id, 0.2)
            if slip_modifier <= 0.15 or attention_weight >= 0.4:
                continue
            now_items.append(
                BriefLine(
                    priority=_priority_for_slip_bias_audit(slip_modifier),
                    text=f"Low attention + known slip bias on {workstream_id}.",
                )
            )

    for packet in contradiction_packets:
        if not packet.contradictions:
            continue
        target = now_items if packet.confidence is Confidence.HIGH or packet.recommended_resolution is not None else watch_items
        target.append(
            BriefLine(
                priority=_priority_for_contradiction(packet),
                text=_render_contradiction_line(packet),
            )
        )

    for target_name, line in _build_incident_learning_brief_lines(
        incident_entries,
        weight_by_workstream=weight_by_workstream,
    ):
        if target_name == "now":
            now_items.append(line)
        else:
            watch_items.append(line)

    intervention_resolutions = load_brief_intervention_resolutions(program_id, programs_root=programs_root)
    staged_interventions = tuple(
        proposal
        for proposal in rank_brief_interventions(
            program_id,
            claim_assessments=claim_assessments,
            decision_asks=decision_asks,
            contradiction_packets=contradiction_packets,
            salience_weights=weight_by_workstream,
            as_of=generated_at,
            programs_root=programs_root,
            incident_entries=incident_entries,
        )
        if not _is_resolved_intervention(proposal, intervention_resolutions)
    )
    staged_lines = tuple(
        _render_staged_intervention_line(program_id, proposal)
        for proposal in staged_interventions
    )

    now_lines = tuple(line.text for line in sorted(now_items, key=lambda item: (-item.priority, item.text)))
    watch_lines = tuple(line.text for line in sorted(watch_items, key=lambda item: (-item.priority, item.text)))
    return BriefReport(
        program_id=program_id,
        generated_at=generated_at,
        now_lines=now_lines,
        watch_lines=watch_lines,
        staged_lines=staged_lines,
        reference_lines=reference_lines,
        staged_interventions=staged_interventions,
    )


def _build_cost_guard_brief_lines(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> tuple[BriefLine, ...]:
    program_output_dir = get_program_output_root(program_id, programs_root=programs_root)
    if not program_output_dir.exists():
        return ()

    lines: list[BriefLine] = []
    for edition_dir in sorted(program_output_dir.iterdir(), key=lambda path: path.name):
        if not edition_dir.is_dir():
            continue
        try:
            state = load_latest_run_state(edition_dir.name, programs_root=programs_root)
        except StateError as error:
            lines.append(
                BriefLine(
                    priority=960,
                    text=f"AI cost guard state invalid for {edition_dir.name}: {error}",
                )
            )
            continue
        if state is None or state.within_budget:
            continue
        lines.append(
            BriefLine(
                priority=980,
                text=(
                    f"AI cost ceiling exceeded for {edition_dir.name}: ${state.spent_usd:.3f} / ${state.budget_usd:.2f} "
                    f"across {state.ai_calls} AI call(s) (run {state.run_id})."
                ),
            )
        )
    return tuple(lines)


def _edition_matches_program(program_id: str, edition_name: str) -> bool:
    return edition_name == program_id or edition_name.startswith(f"{program_id}_")


def render_brief(report: BriefReport) -> str:
    lines = [
        f"Morning Brief - {report.program_id}",
        f"Generated: {report.generated_at.isoformat()}",
        "",
        "Now",
        "---",
    ]
    if report.now_lines:
        lines.extend(f"- {line}" for line in report.now_lines)
    else:
        lines.append("- None")
    lines.extend(["", "Watch", "-----"])
    if report.watch_lines:
        lines.extend(f"- {line}" for line in report.watch_lines)
    else:
        lines.append("- None")
    lines.extend(["", "Staged", "------"])
    if report.staged_lines:
        lines.extend(f"- {line}" for line in report.staged_lines)
    else:
        lines.append("- None")
    if report.reference_lines:
        lines.extend(("", "Reference Docs", "--------------"))
        lines.extend(report.reference_lines)
    return "\n".join(lines)


def write_brief_artifact(report: BriefReport, rendered: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    output_dir = get_program_output_root(report.program_id, programs_root=programs_root) / "briefs"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"brief_{report.generated_at.date().isoformat()}.txt"
    path.write_text(rendered + "\n", encoding="utf-8")
    return path


def _priority_for_claim(days_until_due: int, salience_weight: float) -> int:
    urgency = 500 if days_until_due < 0 else 250 if days_until_due <= 7 else 100
    return urgency + int(salience_weight * 100)


def _priority_for_slip_bias_audit(slip_modifier: float) -> int:
    return 800 + int(round(slip_modifier * 100))


def _priority_for_contradiction(packet: ContradictionPacket) -> int:
    base_priority = {
        Confidence.HIGH: 750,
        Confidence.MEDIUM: 450,
        Confidence.LOW: 250,
        Confidence.NONE: 100,
    }[packet.confidence]
    if packet.recommended_resolution is not None:
        return base_priority + 100
    return base_priority


def _render_contradiction_line(packet: ContradictionPacket) -> str:
    primary = packet.contradictions[0]
    workstream_label = f" ({packet.workstream_id})" if packet.workstream_id else ""
    summary = primary.summary.strip()
    if len(packet.contradictions) > 1:
        summary = f"{summary} (+{len(packet.contradictions) - 1} more)"
    line = f"Contradiction WI:{packet.work_item_id}{workstream_label}: {summary}"
    if packet.recommended_resolution is None:
        return line
    separator = " " if line.endswith((".", "!", "?")) else ". "
    return (
        f"{line}{separator}Prefer {packet.recommended_resolution.winning_source.value} "
        f"({packet.recommended_resolution.confidence.value})."
    )


def _render_staged_intervention_line(program_id: str, proposal: InterventionProposal) -> str:
    return (
        f"Id: {proposal.proposal_id} | {proposal.title} | Evidence: {proposal.evidence_summary} "
        f"| Action: {proposal.proposed_action} | Apply: {proposal.command} "
        f"| Approve: vertex brief --program {program_id} --approve {proposal.proposal_id} "
        f"| Dismiss: vertex brief --program {program_id} --dismiss {proposal.proposal_id} "
        f"| Rollback: {proposal.rollback}"
    )


def _render_decision_ask_brief_line(proposal) -> str:
    ask = proposal.ask
    if proposal.stage.value == "watch":
        return f"Decision ask {ask.id} is in watch at {proposal.age_days} day(s) open: {ask.text}"
    if proposal.stage.value == "nudge":
        return f"Decision ask {ask.id} is ready for nudge after {proposal.inactive_days} day(s) inactive: {ask.text}"
    if proposal.is_expired and ask.expiry_date is not None:
        return f"Decision ask {ask.id} is ready for escalation after expiring on {ask.expiry_date.isoformat()}: {ask.text}"
    return f"Decision ask {ask.id} is ready for escalation after {proposal.inactive_days} day(s) inactive: {ask.text}"


def _build_incident_learning_brief_lines(
    entries: tuple[IncidentEntry, ...],
    *,
    weight_by_workstream: dict[str, float],
) -> tuple[tuple[str, BriefLine], ...]:
    if not entries:
        return ()

    lines: list[tuple[str, BriefLine]] = []
    covered_signal_ids: set[str] = set()
    for pattern in build_incident_class_patterns(entries):
        covered_signal_ids.update(pattern.signal_ids)
        workstream_weight = 0.2 if not pattern.workstream_ids else max(weight_by_workstream.get(workstream_id, 0.2) for workstream_id in pattern.workstream_ids)
        target_name = "now" if _incident_class_pattern_belongs_in_now(pattern) else "watch"
        lines.append(
            (
                target_name,
                BriefLine(
                    priority=_priority_for_incident_class_pattern(pattern, workstream_weight),
                    text=_render_incident_class_pattern_line(pattern),
                ),
            )
        )

    for ref_pattern in build_incident_ref_patterns(entries):
        covered_signal_ids.update(ref_pattern.signal_ids)
        workstream_weight = 0.2 if ref_pattern.workstream_id is None else weight_by_workstream.get(ref_pattern.workstream_id, 0.2)
        target_name = "now" if _incident_pattern_belongs_in_now(ref_pattern) else "watch"
        lines.append(
            (
                target_name,
                BriefLine(
                    priority=_priority_for_incident_pattern(ref_pattern, workstream_weight),
                    text=_render_incident_pattern_line(ref_pattern),
                ),
            )
        )

    for entry in entries:
        if entry.signal_id in covered_signal_ids:
            continue
        workstream_weight = 0.2 if entry.workstream_id is None else weight_by_workstream.get(entry.workstream_id, 0.2)
        target_name = "now" if _incident_entry_belongs_in_now(entry) else "watch"
        lines.append(
            (
                target_name,
                BriefLine(
                    priority=_priority_for_incident_entry(entry, workstream_weight),
                    text=_render_incident_entry_line(entry),
                ),
            )
        )
    return tuple(lines)


def _incident_pattern_belongs_in_now(pattern: IncidentRefPattern) -> bool:
    return (
        pattern.entry_count > 1
        or (pattern.max_severity is not None and pattern.max_severity <= 2)
        or pattern.confidence is Confidence.HIGH
    )


def _incident_class_pattern_belongs_in_now(pattern: IncidentClassPattern) -> bool:
    return (
        pattern.entry_count > 2
        or (pattern.max_severity is not None and pattern.max_severity <= 2)
        or pattern.confidence is Confidence.HIGH
    )


def _incident_entry_belongs_in_now(entry: IncidentEntry) -> bool:
    return (entry.severity is not None and entry.severity <= 2) or entry.confidence is Confidence.HIGH


def _priority_for_incident_pattern(pattern: IncidentRefPattern, workstream_weight: float) -> int:
    priority = 730 + int(round(max(0.0, min(workstream_weight, 1.0)) * 10))
    if pattern.entry_count > 1:
        priority += 20
    if pattern.max_severity is not None and pattern.max_severity <= 2:
        priority += 20
    if pattern.confidence is Confidence.HIGH:
        priority += 10
    return priority


def _priority_for_incident_class_pattern(pattern: IncidentClassPattern, workstream_weight: float) -> int:
    priority = 760 + int(round(max(0.0, min(workstream_weight, 1.0)) * 10))
    if pattern.entry_count > 2:
        priority += 20
    if pattern.max_severity is not None and pattern.max_severity <= 2:
        priority += 20
    if pattern.confidence is Confidence.HIGH:
        priority += 10
    return priority


def _priority_for_incident_entry(entry: IncidentEntry, workstream_weight: float) -> int:
    priority = 500 + int(round(max(0.0, min(workstream_weight, 1.0)) * 10))
    if entry.severity is not None and entry.severity <= 2:
        priority += 20
    if entry.confidence is Confidence.HIGH:
        priority += 10
    return priority


def _render_incident_pattern_line(pattern: IncidentRefPattern) -> str:
    workstream_label = f" ({pattern.workstream_id})" if pattern.workstream_id else ""
    incident_refs = ", ".join(pattern.incident_refs)
    recurrence = " recurred" if pattern.entry_count > 1 else ""
    return (
        f"Incident learning {pattern.ref}{workstream_label}:{recurrence} {pattern.summary_text}. "
        f"Source: {incident_refs}. {_incident_confidence_suffix(pattern.confidence)}"
    ).replace(": recurred", ": Recurred")


def _render_incident_class_pattern_line(pattern: IncidentClassPattern) -> str:
    workstream_label = f" ({', '.join(pattern.workstream_ids)})" if pattern.workstream_ids else ""
    incident_refs = ", ".join(pattern.incident_refs)
    linked_refs = f" Refs: {', '.join(pattern.linked_refs)}." if pattern.linked_refs else ""
    return (
        f"Incident class {pattern.class_label}{workstream_label}: Recurred across {pattern.entry_count} incident learnings. "
        f"{pattern.summary_text}. Source: {incident_refs}.{linked_refs} {_incident_confidence_suffix(pattern.confidence)}"
    )


def _render_incident_entry_line(entry: IncidentEntry) -> str:
    workstream_label = f" ({entry.workstream_id})" if entry.workstream_id else ""
    summary = normalize_incident_learning_summary(entry.belief_change_summary)
    return f"Incident learning IcM {entry.incident_id}{workstream_label}: {summary}. {_incident_confidence_suffix(entry.confidence)}"


def _incident_confidence_suffix(confidence: Confidence) -> str:
    return f"({confidence.value.lower()} confidence)"


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _apply_supported_intervention(
    proposal: InterventionProposal,
    *,
    programs_root: Path,
    dry_run: bool,
) -> tuple[str, ...]:
    decision_ask_id = _decision_ask_nudge_id_from_command(proposal.command)
    if decision_ask_id is not None:
        context_note = _decision_ask_context_note_from_proposal(proposal)
        plan = plan_decision_ask_nudge(
            program_id=_program_id_from_decision_ask_nudge_command(proposal.command),
            decision_ask_id=decision_ask_id,
            programs_root=programs_root,
            context_note=context_note,
        )
        if dry_run:
            return (
                render_notify_preview_plaintext((plan.preview,)),
                f"Dry run: would mark staged intervention {proposal.proposal_id} as approved and write a decision-ask nudge draft.",
            )

        eml_paths = apply_decision_ask_nudge(
            plan,
            programs_root=programs_root,
            generated_at=_utc_now(),
        )
        return tuple(f"EML: {path}" for path in eml_paths)

    escalation_target = _decision_ask_escalation_from_command(proposal.command)
    if escalation_target is not None:
        edition_name, escalation_decision_ask_id = escalation_target
        escalation_plan = plan_decision_ask_escalation(
            edition_name=edition_name,
            decision_ask_id=escalation_decision_ask_id,
        )
        if dry_run:
            return (
                render_escalation_preview_plaintext(escalation_plan.artifacts),
                f"Dry run: would mark staged intervention {proposal.proposal_id} as approved and write an escalation draft.",
            )

        artifacts = apply_decision_ask_escalation(
            escalation_plan,
            generated_at=_utc_now(),
        )
        return tuple(f"EML: {path}" for path in artifacts.eml_paths)

    readiness_program_id = _readiness_program_id_from_command(proposal.command)
    if readiness_program_id is not None:
        if dry_run:
            return (
                f"Dry run: would mark staged intervention {proposal.proposal_id} as approved and refresh launch readiness.",
                f"Apply command: {proposal.command}",
            )

        snapshot, snapshot_path = fetch_readiness_snapshot(readiness_program_id, programs_root=programs_root)
        return (
            render_readiness_snapshot_output(
                snapshot,
                output_format="table",
                snapshot_path=snapshot_path,
                warnings=(),
            ),
        )

    if dry_run:
        return (
            f"Dry run: would mark staged intervention {proposal.proposal_id} as approved.",
            f"Apply command: {proposal.command}",
        )
    return ()


def _decision_ask_nudge_id_from_command(command: str) -> str | None:
    match = re.fullmatch(r"vertex decisions nudge --program \S+ --id (?P<decision_ask_id>\S+) --dry-run", command.strip())
    if match is None:
        return None
    return match.group("decision_ask_id")


def _program_id_from_decision_ask_nudge_command(command: str) -> str:
    match = re.fullmatch(r"vertex decisions nudge --program (?P<program_id>\S+) --id \S+ --dry-run", command.strip())
    if match is None:
        raise ValueError(f"Unsupported intervention command {command!r}.")
    return match.group("program_id")


def _decision_ask_escalation_from_command(command: str) -> tuple[str, str] | None:
    match = re.fullmatch(
        r"vertex escalate --edition (?P<edition_name>\S+) --decision-ask (?P<decision_ask_id>\S+) --dry-run",
        command.strip(),
    )
    if match is None:
        return None
    return match.group("edition_name"), match.group("decision_ask_id")


def _readiness_program_id_from_command(command: str) -> str | None:
    match = re.fullmatch(r"vertex readiness fetch --program (?P<program_id>\S+)", command.strip())
    if match is None:
        return None
    return match.group("program_id")


def _decision_ask_context_note_from_proposal(proposal: InterventionProposal) -> str | None:
    marker = "Recent incident learning:"
    if marker not in proposal.evidence_summary:
        return None
    _, _, note = proposal.evidence_summary.partition(marker)
    normalized = note.strip()
    return normalized or None


def _find_staged_intervention(report: BriefReport, proposal_id: str) -> InterventionProposal | None:
    return next((proposal for proposal in report.staged_interventions if proposal.proposal_id == proposal_id), None)


def _is_resolved_intervention(
    proposal: InterventionProposal,
    resolutions: dict[str, BriefInterventionResolution],
) -> bool:
    resolution = resolutions.get(proposal.proposal_id)
    if resolution is None:
        return False
    if resolution.source_hash != proposal.source_hash:
        return False
    return resolution.status in {BriefInterventionStatus.APPROVED, BriefInterventionStatus.DISMISSED}