from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
from typing import Callable

import typer

from src.core.adoption_telemetry import GoldenWorkflow, record_adoption
from src.core.journal import PROGRAMS_ROOT
from src.core.program_reality import FactAssessment, ProgramReality
from src.core.raid_graph import RaidChainResult, build_raid_chain_index
from src.core.models_v2 import RiskCategory, RiskEntry, RiskImpact, RiskProbability, RiskStatus
from src.core.risk_register_engine import assess_risk_staleness, build_risk_id, compute_risk_score, link_risk_action, record_risk_update, save_risk_register
from src.core.truth_levels import TruthLevel


app = typer.Typer(help="Manage the program risk register.", invoke_without_command=True)


@app.callback(invoke_without_command=True)
def risks_command(
    ctx: typer.Context,
    program: str | None = typer.Option(None, "--program", help="Program id, e.g. myprogram."),
    show_links: bool = typer.Option(False, "--show-links", help="Show RAID causal links for each listed risk."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if program is None or not program.strip():
        raise typer.BadParameter("--program is required.")
    _print_risks(program.strip(), status_filter=None, format=format, show_links=show_links)
    raise typer.Exit(code=0)


@app.command("list")
def list_risks_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    status: str | None = typer.Option(None, "--status", help="Optional status filter."),
    show_links: bool = typer.Option(False, "--show-links", help="Show RAID causal links for each listed risk."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    status_filter = RiskStatus.from_string(status) if status is not None and status.strip() else None
    _print_risks(program.strip(), status_filter=status_filter, format=format, show_links=show_links)
    raise typer.Exit(code=0)


@app.command("add")
def add_risk_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    title: str = typer.Option(..., "--title", help="Risk title."),
    probability: str = typer.Option(..., "--probability", help="Probability: very_likely|likely|possible|unlikely."),
    impact: str = typer.Option(..., "--impact", help="Impact: critical|high|medium|low."),
    description: str | None = typer.Option(None, "--description", help="Optional longer description. Defaults to the title."),
    category: str = typer.Option("technical", "--category", help="Category: technical|schedule|resource|dependency|external."),
    owner: str | None = typer.Option(None, "--owner", help="Owner alias. Defaults to current OS user."),
    mitigation_plan: str | None = typer.Option(None, "--mitigation-plan", help="Optional mitigation plan."),
    mitigation_due_date: str | None = typer.Option(None, "--mitigation-due-date", help="Optional YYYY-MM-DD mitigation due date."),
    workstream: list[str] | None = typer.Option(None, "--workstream", help="Repeat to link workstream ids."),
    work_item: list[int] | None = typer.Option(None, "--work-item", help="Repeat to link ADO work item ids."),
    milestone: list[str] | None = typer.Option(None, "--milestone", help="Repeat to link milestone ids."),
    claim: list[str] | None = typer.Option(None, "--claim", help="Repeat to link claim ids."),
    action: list[str] | None = typer.Option(None, "--action", help="Repeat to link action ids."),
    entity_ref: list[str] | None = typer.Option(None, "--entity-ref", help="Repeat to add entity refs."),
    issue: int | None = typer.Option(None, "--issue", help="Optional Vertex issue number that identified the risk."),
    identified_date: str | None = typer.Option(None, "--identified-date", help="Optional YYYY-MM-DD identified date."),
) -> None:
    program_id = program.strip()
    title_text = title.strip()
    if not title_text:
        raise typer.BadParameter("--title is required.")

    description_text = description.strip() if description is not None and description.strip() else title_text
    owner_alias = _default_actor(owner)
    risk_entry = RiskEntry(
        id=build_risk_id(program_id, title=title_text, description=description_text, owner_alias=owner_alias),
        program_id=program_id,
        title=title_text,
        description=description_text,
        probability=RiskProbability.from_string(probability),
        impact=RiskImpact.from_string(impact),
        category=RiskCategory.from_string(category),
        owner_alias=owner_alias,
        mitigation_plan=mitigation_plan.strip() if mitigation_plan is not None and mitigation_plan.strip() else None,
        mitigation_due_date=_parse_optional_date(mitigation_due_date),
        linked_workstream_ids=tuple(item.strip() for item in workstream or [] if item.strip()),
        linked_work_item_ids=tuple(work_item or []),
        linked_milestone_ids=tuple(item.strip() for item in milestone or [] if item.strip()),
        linked_claim_ids=tuple(item.strip() for item in claim or [] if item.strip()),
        linked_action_ids=tuple(item.strip() for item in action or [] if item.strip()),
        status=RiskStatus.OPEN,
        identified_date=_parse_optional_date(identified_date) or datetime.now(timezone.utc).date(),
        identified_in_vertex_issue=issue,
        last_reviewed_date=None,
        entity_refs=tuple(item.strip() for item in entity_ref or [] if item.strip()),
    )

    entries = list(_load_current_risks(program_id, programs_root=PROGRAMS_ROOT))
    if any(entry.id == risk_entry.id for entry in entries):
        raise typer.BadParameter(
            f"Risk '{risk_entry.title}' already exists as {risk_entry.id}. Use `vertex risks update` or choose a more specific title/description."
        )
    entries.append(risk_entry)
    save_risk_register(program_id, _sort_risks(tuple(entries)), programs_root=PROGRAMS_ROOT)
    typer.echo(f"Added risk {risk_entry.id} to {program_id}.")
    raise typer.Exit(code=0)


@app.command("update")
def update_risk_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    risk_id: str = typer.Option(..., "--id", help="Risk id."),
    status: str | None = typer.Option(None, "--status", help="Optional new status."),
    probability: str | None = typer.Option(None, "--probability", help="Optional new probability."),
    impact: str | None = typer.Option(None, "--impact", help="Optional new impact."),
    category: str | None = typer.Option(None, "--category", help="Optional new category."),
    owner: str | None = typer.Option(None, "--owner", help="Optional new owner alias."),
    description: str | None = typer.Option(None, "--description", help="Optional new description."),
    mitigation_plan: str | None = typer.Option(None, "--mitigation-plan", help="Optional new mitigation plan."),
    mitigation_due_date: str | None = typer.Option(None, "--mitigation-due-date", help="Optional new YYYY-MM-DD mitigation due date."),
    reviewed_date: str | None = typer.Option(None, "--reviewed-date", help="Optional YYYY-MM-DD review date. Defaults to today when any change is applied."),
    note: str | None = typer.Option(None, "--note", help="Optional status-change note."),
    reviewer: str | None = typer.Option(None, "--reviewer", help="Reviewer alias for status changes. Defaults to current OS user."),
) -> None:
    program_id = program.strip()
    entries = list(_load_current_risks(program_id, programs_root=PROGRAMS_ROOT))
    match_index = next((index for index, entry in enumerate(entries) if entry.id == risk_id.strip()), None)
    if match_index is None:
        raise typer.BadParameter(f"Risk '{risk_id}' was not found in {program_id}.")

    current = entries[match_index]
    next_status = RiskStatus.from_string(status) if status is not None and status.strip() else current.status
    next_probability = RiskProbability.from_string(probability) if probability is not None and probability.strip() else current.probability
    next_impact = RiskImpact.from_string(impact) if impact is not None and impact.strip() else current.impact
    next_category = RiskCategory.from_string(category) if category is not None and category.strip() else current.category
    next_owner = owner.strip() if owner is not None and owner.strip() else current.owner_alias
    next_description = description.strip() if description is not None and description.strip() else current.description
    next_mitigation_plan = mitigation_plan.strip() if mitigation_plan is not None and mitigation_plan.strip() else (None if mitigation_plan == "" else current.mitigation_plan)
    next_mitigation_due_date = _parse_optional_date(mitigation_due_date) if mitigation_due_date is not None else current.mitigation_due_date
    review_date = _parse_optional_date(reviewed_date)
    if review_date is None and any(
        candidate != baseline
        for candidate, baseline in (
            (next_status, current.status),
            (next_probability, current.probability),
            (next_impact, current.impact),
            (next_category, current.category),
            (next_owner, current.owner_alias),
            (next_description, current.description),
            (next_mitigation_plan, current.mitigation_plan),
            (next_mitigation_due_date, current.mitigation_due_date),
        )
    ):
        review_date = datetime.now(timezone.utc).date()

    updated = replace(
        current,
        description=next_description,
        probability=next_probability,
        impact=next_impact,
        category=next_category,
        owner_alias=next_owner,
        mitigation_plan=next_mitigation_plan,
        mitigation_due_date=next_mitigation_due_date,
        status=next_status,
        last_reviewed_date=(review_date or current.last_reviewed_date),
    )
    if updated == current:
        typer.echo(f"Risk {current.id} is unchanged.")
        raise typer.Exit(code=0)

    entries[match_index] = updated
    save_risk_register(program_id, _sort_risks(tuple(entries)), programs_root=PROGRAMS_ROOT)
    if updated.status != current.status:
        record_risk_update(
            program_id,
            updated.id,
            current.status.value,
            updated.status.value,
            _default_actor(reviewer),
            note,
            programs_root=PROGRAMS_ROOT,
        )
    typer.echo(f"Updated risk {updated.id} in {program_id}.")
    raise typer.Exit(code=0)


@dataclass(frozen=True, slots=True)
class RiskReviewIO:
    """ADF-W5.11 (Section 10.3a): the injectable I/O seam that makes
    ``run_risk_review_session`` a shared typed command service -- callable
    identically from the ``risks review`` CLI (typer I/O) and ``cockpit
    tui``'s launch action (injected I/O), never a copy of the review logic."""

    confirm: Callable[[str], bool]
    prompt: Callable[[str], str]
    echo: Callable[[str], None]


def run_risk_review_session(
    program_id: str,
    *,
    mark_reviewed: bool,
    review_actor: str,
    io: RiskReviewIO,
    programs_root: Path = PROGRAMS_ROOT,
) -> int:
    """The stale-risk review loop itself, independent of which surface is
    driving it. Returns the number of risks reviewed. Writes through the
    exact same real paths regardless of caller: ``record_risk_update`` for
    each status change, ``save_risk_register`` for the persisted register
    (dual-writes the fact-store projection), and a best-effort
    ``record_adoption`` -- no mutation-path bypass, no shortcut store write."""
    entries = list(_load_current_risks(program_id, programs_root=programs_root))
    stale_entries = [entry for entry in _sort_risks(tuple(entries)) if assess_risk_staleness(entry, datetime.now(timezone.utc).date())]
    if not stale_entries:
        io.echo(f"No stale risks for {program_id}.")
        return 0

    today = datetime.now(timezone.utc).date()
    reviewed_count = 0
    changed = False

    for stale_entry in stale_entries:
        io.echo(f"{stale_entry.id} | {stale_entry.status.value} | score {compute_risk_score(stale_entry)} | {stale_entry.title}")
        should_review = mark_reviewed or io.confirm("Mark reviewed today?")
        if not should_review:
            continue

        next_status = stale_entry.status
        note: str | None = None
        if not mark_reviewed:
            status_override = io.prompt("Status override (blank to keep)").strip()
            if status_override:
                next_status = RiskStatus.from_string(status_override)
            note_value = io.prompt("Review note (blank for none)").strip()
            note = note_value or None

        updated = replace(stale_entry, status=next_status, last_reviewed_date=today)
        entries[entries.index(stale_entry)] = updated
        reviewed_count += 1
        changed = True
        if updated.status != stale_entry.status:
            record_risk_update(
                program_id,
                updated.id,
                stale_entry.status.value,
                updated.status.value,
                review_actor,
                note,
                programs_root=programs_root,
            )

    if changed:
        save_risk_register(program_id, _sort_risks(tuple(entries)), programs_root=programs_root)
        # ADF-W5.14: a completed risk review (at least one stale risk actually
        # reviewed) is the risk_dependency_review golden workflow's adoption moment.
        try:
            record_adoption(program_id, GoldenWorkflow.RISK_DEPENDENCY_REVIEW, programs_root=programs_root)
        except Exception:
            pass
    return reviewed_count


@app.command("review")
def review_risks_command(
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    reviewer: str | None = typer.Option(None, "--reviewer", help="Reviewer alias. Defaults to current OS user."),
    mark_reviewed: bool = typer.Option(False, "--mark-reviewed", help="Mark all stale risks reviewed today without prompting."),
) -> None:
    program_id = program.strip()
    io = RiskReviewIO(
        confirm=lambda message: typer.confirm(message, default=True),
        prompt=lambda message: typer.prompt(message, default="", show_default=False),
        echo=typer.echo,
    )
    reviewed_count = run_risk_review_session(
        program_id,
        mark_reviewed=mark_reviewed,
        review_actor=_default_actor(reviewer),
        io=io,
        programs_root=PROGRAMS_ROOT,
    )
    typer.echo(f"Reviewed {reviewed_count} stale risk{'s' if reviewed_count != 1 else ''} in {program_id}.")
    raise typer.Exit(code=0)


@app.command("link")
def link_risk_command(
    risk_id: str = typer.Argument(..., help="Risk id to update."),
    action_id: str = typer.Argument(..., help="Action id to link."),
    program: str = typer.Option(..., "--program", help="Program id, e.g. myprogram."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview the link without writing the risk register."),
) -> None:
    program_id = program.strip()
    risk_key = risk_id.strip()
    action_key = action_id.strip()
    if not risk_key:
        raise typer.BadParameter("risk_id is required.")
    if not action_key:
        raise typer.BadParameter("action_id is required.")

    _reality = ProgramReality.load(program_id, programs_root=PROGRAMS_ROOT)
    # .record strips are intentional: the link command validates ids and writes
    # to the register via link_risk_action(); FactAssessment metadata is not
    # needed for a write/validation operation.
    risks = tuple(a.record for a in _reality.risks())
    risk_entry = next((entry for entry in risks if entry.id == risk_key), None)
    if risk_entry is None:
        raise typer.BadParameter(f"Risk '{risk_key}' was not found in {program_id}.")

    actions = tuple(a.record for a in _reality.actions())
    action_entry = next((entry for entry in actions if entry.id == action_key), None)
    if action_entry is None:
        raise typer.BadParameter(f"Action '{action_key}' was not found in {program_id}.")
    if action_entry.linked_risk_id is not None and action_entry.linked_risk_id != risk_key:
        raise typer.BadParameter(
            f"Action '{action_key}' is already linked to risk '{action_entry.linked_risk_id}'."
        )
    if action_key in risk_entry.linked_action_ids:
        typer.echo(f"Risk {risk_key} already links action {action_key} in {program_id}.")
        raise typer.Exit(code=0)

    if dry_run:
        typer.echo(f"Would link action {action_key} to risk {risk_key} in {program_id}.")
        raise typer.Exit(code=0)

    link_risk_action(program_id, risk_key, action_key, programs_root=PROGRAMS_ROOT)
    typer.echo(f"Linked action {action_key} to risk {risk_key} in {program_id}.")
    raise typer.Exit(code=0)


def _print_risks(program_id: str, *, status_filter: RiskStatus | None, format: str, show_links: bool) -> None:
    assessments = _load_current_risk_assessments(program_id)
    if status_filter is not None:
        assessments = tuple(a for a in assessments if a.record.status == status_filter)
    today = datetime.now(timezone.utc).date()
    sorted_assessments = _sort_risk_assessments(assessments)
    entries = tuple(a.record for a in sorted_assessments)
    chain_index = build_raid_chain_index(program_id, programs_root=PROGRAMS_ROOT) if show_links else {}
    if format == "json":
        typer.echo(
            json.dumps(
                {
                    "program_id": program_id,
                    "status_filter": status_filter.value if status_filter is not None else None,
                    "risks": [
                        asdict(entry)
                        | {
                            "risk_score": compute_risk_score(entry),
                            "staleness": ("stale" if assess_risk_staleness(entry, today) else "current"),
                            # Track E follow-up: preserve FactAssessment metadata so the
                            # risk board carries the same truth signal as ask/triage/newsletter.
                            "truth_level": assessment.truth_level.value,
                            "disputed": assessment.disputed,
                            "fact_stale": assessment.stale,
                            "evidence": list(assessment.evidence),
                            **(_raid_chain_payload(chain_index.get(entry.id)) if show_links else {}),
                        }
                        for assessment, entry in zip(sorted_assessments, entries, strict=True)
                    ],
                },
                indent=2,
                sort_keys=True,
                default=_json_default,
            )
        )
        return
    if format == "csv":
        typer.echo(
            render_risks_csv(entries, as_of=today, chain_index=chain_index if show_links else None, assessments=sorted_assessments),
            nl=False,
        )
        return
    if format != "human":
        raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")
    if not sorted_assessments:
        typer.echo(f"No risks found for {program_id}.")
        return

    typer.echo(f"RISK REGISTER — {program_id} ({len(sorted_assessments)})")
    for assessment in sorted_assessments:
        entry = assessment.record
        stale_label = "stale" if assess_risk_staleness(entry, today) else "current"
        due_label = entry.mitigation_due_date.isoformat() if entry.mitigation_due_date is not None else "-"
        truth_label = _format_risk_truth_label(assessment)
        typer.echo(
            f"- {entry.id} | {entry.status.value} | score {compute_risk_score(entry)} | {stale_label} | due {due_label} | owner {entry.owner_alias}{truth_label}"
        )
        typer.echo(f"  {entry.title}")
        if show_links:
            chain = chain_index.get(entry.id)
            typer.echo(f"  RAID: {_format_raid_chain_summary(chain)}")
            if chain is not None:
                for warning in chain.warnings:
                    typer.echo(f"  RAID warning: {warning}")


def _sort_risks(entries: tuple[RiskEntry, ...]) -> tuple[RiskEntry, ...]:
    return tuple(
        sorted(
            entries,
            key=lambda entry: (
                0 if entry.status in {RiskStatus.OPEN, RiskStatus.ESCALATED} else 1,
                -compute_risk_score(entry),
                entry.title.lower(),
            ),
        )
    )


def _sort_risk_assessments(assessments: tuple[FactAssessment, ...]) -> tuple[FactAssessment, ...]:
    """Sort FactAssessment-wrapped risks by the same precedence as ``_sort_risks``.

    Disputed risks are promoted to the top of the board (a disputed risk is the
    highest-priority item for a TPM/EM to look at), then the standard
    open/escalated-first → score-desc → title precedence.
    """
    return tuple(
        sorted(
            assessments,
            key=lambda a: (
                0 if a.disputed else 1,
                0 if a.record.status in {RiskStatus.OPEN, RiskStatus.ESCALATED} else 1,
                -compute_risk_score(a.record),
                a.record.title.lower(),
            ),
        )
    )


def _format_risk_truth_label(assessment: FactAssessment) -> str:
    """Compact human-readable truth-signal suffix for the risk board (one line).

    Surfaces ``truth_level`` (as the same glyph vocabulary the newsletter uses)
    plus dispute/staleness/provisional flags, so the risk board reader sees the
    same confidence signal the newsletter's trust badges carry. Returns an empty
    string when the fact is human-confirmed or above with no flags (the common,
    unremarkable case) to avoid noisy output.
    """
    if (
        assessment.truth_level in {TruthLevel.HUMAN_CONFIRMED, TruthLevel.GOVERNANCE_LOCKED}
        and not assessment.disputed
        and not assessment.stale
        and not assessment.provisional_inputs
    ):
        return ""
    glyph = _TRUTH_GLYPH.get(assessment.truth_level, "")
    flags: list[str] = []
    if assessment.disputed:
        flags.append("DISPUTED")
    if assessment.stale:
        flags.append("STALE")
    if assessment.provisional_inputs:
        flags.append("PROVISIONAL")
    flag_text = f" [{'/'.join(flags)}]" if flags else ""
    level_text = f" | {glyph} {assessment.truth_level.value}" if glyph else ""
    return f"{level_text}{flag_text}"


# Glyph vocabulary mirroring the newsletter's trust badges (UX spec §3.6b), so
# the CLI risk board and the newsletter show the same signal for the same fact.
_TRUTH_GLYPH: dict[TruthLevel, str] = {
    TruthLevel.RAW_OBSERVED: "○",
    TruthLevel.SOURCE_VALIDATED: "●",
    TruthLevel.CORROBORATED: "◆",
    TruthLevel.HUMAN_CONFIRMED: "✔",
    TruthLevel.GOVERNANCE_LOCKED: "🔒",
}


def _default_actor(value: str | None) -> str:
    if value is not None and value.strip():
        return value.strip()
    return (os.environ.get("USERNAME") or os.environ.get("USER") or "manual").strip() or "manual"


def _parse_optional_date(value: str | None) -> date | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return date.fromisoformat(stripped)


def render_risks_csv(
    entries: tuple[RiskEntry, ...],
    *,
    as_of: date,
    chain_index: dict[str, RaidChainResult] | None = None,
    assessments: tuple[FactAssessment, ...] | None = None,
) -> str:
    # ``assessments`` (Track E follow-up) optionally carries FactAssessment
    # metadata so the CSV can emit truth_level/disputed/fact_stale columns. When
    # omitted (e.g. callers that only have bare RiskEntry records), those columns
    # are written empty, preserving backward compatibility for any external
    # consumer of this renderer.
    assessment_by_id: dict[str, FactAssessment] = {}
    if assessments is not None:
        assessment_by_id = {a.record.id: a for a in assessments}
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        (
            "id",
            "program_id",
            "title",
            "status",
            "staleness",
            "risk_score",
            "probability",
            "impact",
            "category",
            "owner_alias",
            "mitigation_due_date",
            "identified_date",
            "last_reviewed_date",
            "linked_workstream_ids",
            "linked_work_item_ids",
            "linked_milestone_ids",
            "linked_claim_ids",
            "linked_action_ids",
            "entity_refs",
            "raid_chain",
            "raid_has_mitigating_action",
            "raid_warnings",
            "truth_level",
            "disputed",
            "fact_stale",
        )
    )
    for entry in entries:
        chain = chain_index.get(entry.id) if chain_index is not None else None
        assessment = assessment_by_id.get(entry.id)
        writer.writerow(
            (
                entry.id,
                entry.program_id,
                entry.title,
                entry.status.value,
                "stale" if assess_risk_staleness(entry, as_of) else "current",
                compute_risk_score(entry),
                entry.probability.value,
                entry.impact.value,
                entry.category.value,
                entry.owner_alias,
                _csv_date(entry.mitigation_due_date),
                entry.identified_date.isoformat(),
                _csv_date(entry.last_reviewed_date),
                "|".join(entry.linked_workstream_ids),
                "|".join(str(item_id) for item_id in entry.linked_work_item_ids),
                "|".join(entry.linked_milestone_ids),
                "|".join(entry.linked_claim_ids),
                "|".join(entry.linked_action_ids),
                "|".join(entry.entity_refs),
                _format_raid_chain_csv(chain),
                _format_raid_bool(chain),
                _format_raid_warnings(chain),
                assessment.truth_level.value if assessment is not None else "",
                str(assessment.disputed).lower() if assessment is not None else "",
                str(assessment.stale).lower() if assessment is not None else "",
            )
        )
    return buffer.getvalue()


def _raid_chain_payload(chain: RaidChainResult | None) -> dict[str, object]:
    if chain is None:
        return {
            "raid_chain": (),
            "raid_has_mitigating_action": False,
            "raid_warnings": (),
        }
    return {
        "raid_chain": [asdict(link) for link in chain.links],
        "raid_has_mitigating_action": chain.has_mitigating_action,
        "raid_warnings": list(chain.warnings),
    }


def _format_raid_chain_summary(chain: RaidChainResult | None) -> str:
    if chain is None or not chain.links:
        return "risk only (no linked assumptions, actions, or decisions)."
    path = " -> ".join(f"{link.node_type}:{link.node_id}" for link in chain.links)
    mitigation = "mitigating action present" if chain.has_mitigating_action else "no in_progress/done mitigating action"
    return f"{path} | {mitigation}"


def _format_raid_chain_csv(chain: RaidChainResult | None) -> str:
    if chain is None:
        return ""
    return "|".join(f"{link.node_type}:{link.node_id}:{link.status}:{link.hop}" for link in chain.links)


def _format_raid_bool(chain: RaidChainResult | None) -> str:
    if chain is None:
        return ""
    return "true" if chain.has_mitigating_action else "false"


def _load_current_risks(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> tuple[RiskEntry, ...]:
    # Returns bare ``RiskEntry`` records (FactAssessment metadata stripped) for the
    # write/CRUD path (add/update/review), which manipulates records and persists
    # them back to the legacy risk register. Truth-level metadata is irrelevant to
    # a write/manipulate operation. The read/display path (``_print_risks``) uses
    # ``_load_current_risk_assessments`` instead, which preserves FactAssessment
    # metadata so the risk board can surface truth/dispute/staleness signals
    # (Track E follow-up — the same FactAssessment-preservation fix the newsletter
    # render pipeline got, applied to this user-facing CLI surface).
    return tuple(a.record for a in ProgramReality.load(program_id, programs_root=programs_root).risks())


def _load_current_risk_assessments(program_id: str) -> tuple[FactAssessment, ...]:
    """Load risks with full ``FactAssessment`` metadata for the read/display path.

    Distinct from ``_load_current_risks`` (which strips to ``RiskEntry`` for the
    write path): the ``vertex risks`` list/board is a user-facing surface, and
    the spec's Vision (§1) names "risk board" as one of the projections that
    should carry the same truth guarantees as ``vertex ask``/``vertex triage``.
    Returning ``FactAssessment`` here lets the JSON/human/CSV renderers surface
    ``truth_level``/``disputed``/``stale`` alongside each risk.
    """
    return ProgramReality.load(program_id, programs_root=PROGRAMS_ROOT).risks()


def _format_raid_warnings(chain: RaidChainResult | None) -> str:
    if chain is None:
        return ""
    return "|".join(chain.warnings)


def _csv_date(value: date | None) -> str:
    return value.isoformat() if value is not None else ""


def _json_default(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")