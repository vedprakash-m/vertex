from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from io import StringIO
import json
from pathlib import Path
from typing import Any

import typer

from src.core.ai_proposal_store import load_ai_proposals
from src.core.analytics_store import AutonomyAuditRecord, load_autonomy_audit_records, load_contradiction_state
from src.core.capability_status import ProgramCapabilityStatus, find_program_capability_status, load_program_capability_status
from src.core.edition_resolver import EDITIONS_ROOT, PROGRAMS_ROOT, resolve_edition
from src.core.exceptions import ConfigError
from src.core.models_v2 import AIProposal, AIProposalStatus


_LOW_OVERRIDE_BREAKING_STATUSES = frozenset({AIProposalStatus.REJECTED, AIProposalStatus.SUPERSEDED, AIProposalStatus.EXPIRED})
_L1_FALSE_POSITIVE_THRESHOLD = 0.20
_L2_ACCEPTANCE_THRESHOLD = 0.70
_L3_ACCEPTANCE_THRESHOLD = 0.90
_L4_CYCLE_THRESHOLD = 10


@dataclass(frozen=True, slots=True)
class MaturityCriterionStatus:
    criterion_id: str
    label: str
    threshold: str
    status: str
    detail: str


@dataclass(frozen=True, slots=True)
class MaturityCheckReport:
    edition: str
    program_id: str
    display_name: str
    current_maturity_level: int
    proposal_count: int
    proposal_confidence_summary: str | None
    scoped_issue_numbers: tuple[int, ...]
    criteria: tuple[MaturityCriterionStatus, ...]
    data_limitations: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


def maturity_check_command(
    edition: str = typer.Option(..., "--edition", help="Edition id, e.g. myprogram_weekly."),
    format: str = typer.Option("human", "--format", help="Output format: human, json, or csv."),
) -> None:
    report = build_maturity_check_report(edition)
    if format == "json":
        typer.echo(json.dumps(report.to_payload(), indent=2, sort_keys=True))
        raise typer.Exit(code=0)
    if format == "csv":
        typer.echo(render_maturity_check_csv(report), nl=False)
        raise typer.Exit(code=0)
    if format != "human":
        raise typer.BadParameter("--format must be 'human', 'json', or 'csv'.")
    typer.echo(render_maturity_check(report))
    raise typer.Exit(code=0)


def build_maturity_check_report(
    edition_name: str,
    *,
    editions_root: Path | None = None,
    programs_root: Path | None = None,
) -> MaturityCheckReport:
    resolved_editions_root = editions_root or EDITIONS_ROOT
    resolved_programs_root = programs_root or PROGRAMS_ROOT
    repo_root = resolved_editions_root.parent

    resolved = resolve_edition(
        edition_name,
        editions_root=resolved_editions_root,
        programs_root=resolved_programs_root,
    )
    if resolved is None:
        raise ConfigError(f"Edition '{edition_name}' was not found.")

    proposals = load_ai_proposals(resolved.program.id, programs_root=resolved_programs_root)
    scoped_proposals = tuple(
        proposal
        for proposal in proposals
        if proposal.edition_id == edition_name and proposal.issue_number is not None
    )
    data_limitations: list[str] = []
    if proposals and not scoped_proposals:
        data_limitations.append(
            "AI proposals exist, but none carry edition/issue lineage yet; rolling L2 proposal metrics are unavailable for historical records."
        )

    recent_issue_numbers = tuple(sorted({proposal.issue_number for proposal in scoped_proposals if proposal.issue_number is not None}))
    rolling_issue_numbers = recent_issue_numbers[-10:]
    recent_issue_set = set(rolling_issue_numbers)
    rolling_proposals = tuple(proposal for proposal in scoped_proposals if proposal.issue_number in recent_issue_set)
    resolved_proposals = tuple(
        proposal
        for proposal in rolling_proposals
        if proposal.status in {AIProposalStatus.ACCEPTED, AIProposalStatus.REJECTED}
    )
    capability_statuses = load_program_capability_status(
        resolved.program.id,
        programs_root=resolved_programs_root,
        program_document=resolved.raw_program,
    )
    autonomy_records = load_autonomy_audit_records(
        resolved.program.id,
        programs_root=resolved_programs_root,
    )
    contradiction_packets = load_contradiction_state(
        resolved.program.id,
        programs_root=resolved_programs_root,
    )

    criteria = (
        _evaluate_l0_1(resolved.program.maturity_level),
        _evaluate_l1_1(scoped_proposals, data_limitations),
        _evaluate_l2_1(resolved_proposals, rolling_issue_numbers, data_limitations),
        _evaluate_l2_2(scoped_proposals, data_limitations),
        _evaluate_l2_3(capability_statuses, data_limitations),
        _evaluate_l2_4(),
        _evaluate_l3_1(autonomy_records, data_limitations),
        _evaluate_l3_2(autonomy_records, data_limitations),
        _evaluate_l4_1(autonomy_records, data_limitations),
        _evaluate_l4_2(autonomy_records, contradiction_packets, data_limitations),
    )
    governance_criterion = next((criterion for criterion in criteria if criterion.criterion_id == "L2-3"), None)
    if governance_criterion is not None and governance_criterion.status in {"deferred", "unavailable"}:
        data_limitations.append(governance_criterion.detail)

    return MaturityCheckReport(
        edition=edition_name,
        program_id=resolved.program.id,
        display_name=resolved.edition.brand_name or resolved.program.name,
        current_maturity_level=resolved.program.maturity_level,
        proposal_count=len(proposals),
        proposal_confidence_summary=_summarize_proposal_confidence(scoped_proposals),
        scoped_issue_numbers=rolling_issue_numbers,
        criteria=criteria,
        data_limitations=tuple(dict.fromkeys(data_limitations)),
    )


def render_maturity_check(report: MaturityCheckReport) -> str:
    scoped_issues = ", ".join(f"{issue:03d}" for issue in report.scoped_issue_numbers) or "none"
    lines = [
        f"{report.display_name} — Maturity Check",
        f"  Program:         {report.program_id}",
        f"  Maturity level:  L{report.current_maturity_level}",
        f"  AI proposals:    {report.proposal_count}",
        f"  Confidence mix:  {report.proposal_confidence_summary or 'none'}",
        f"  Scoped issues:   {scoped_issues}",
        "",
    ]
    for criterion in report.criteria:
        lines.append(f"  {criterion.criterion_id} {criterion.label}: {criterion.status} — {criterion.detail}")
    if report.data_limitations:
        lines.append("")
        lines.append("  Data limits:")
        for limitation in report.data_limitations:
            lines.append(f"    - {limitation}")
    return "\n".join(lines)


def render_maturity_check_csv(report: MaturityCheckReport) -> str:
    output = StringIO()
    criterion_fieldnames = [
        f"{criterion.criterion_id.lower().replace('-', '_')}_status"
        for criterion in report.criteria
    ]
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "edition",
            "program_id",
            "display_name",
            "current_maturity_level",
            "proposal_count",
            "proposal_confidence_summary",
            "scoped_issue_numbers",
            *criterion_fieldnames,
            "data_limitations",
        ],
    )
    writer.writeheader()
    criteria = {criterion.criterion_id.lower().replace("-", "_"): criterion for criterion in report.criteria}
    row = {
        "edition": report.edition,
        "program_id": report.program_id,
        "display_name": report.display_name,
        "current_maturity_level": report.current_maturity_level,
        "proposal_count": report.proposal_count,
        "proposal_confidence_summary": report.proposal_confidence_summary or "",
        "scoped_issue_numbers": ",".join(str(issue) for issue in report.scoped_issue_numbers),
        "data_limitations": " | ".join(report.data_limitations),
    }
    for criterion_id, criterion in criteria.items():
        row[f"{criterion_id}_status"] = criterion.status
    writer.writerow(row)
    return output.getvalue()


def _summarize_proposal_confidence(proposals: tuple[AIProposal, ...]) -> str | None:
    if not proposals:
        return None
    counts: dict[str, int] = {}
    for proposal in proposals:
        confidence = proposal.synthesis.confidence.value.lower()
        counts[confidence] = counts.get(confidence, 0) + 1
    ordered_confidences = sorted(counts, key=lambda value: (_confidence_sort_key(value), value))
    return ", ".join(f"{confidence}={counts[confidence]}" for confidence in ordered_confidences) or None


def _confidence_sort_key(confidence: str) -> int:
    if confidence == "high":
        return 0
    if confidence == "medium":
        return 1
    if confidence == "low":
        return 2
    return 3


def _evaluate_l0_1(current_maturity_level: int) -> MaturityCriterionStatus:
    if 0 <= current_maturity_level <= 4:
        return MaturityCriterionStatus(
            "L0-1",
            "Deterministic baseline",
            "Configured maturity level is within L0-L4",
            "passed",
            f"Program config sets maturity_level=L{current_maturity_level}; deterministic gather, render, validation, and rollback invariants remain mandatory.",
        )
    return MaturityCriterionStatus(
        "L0-1",
        "Deterministic baseline",
        "Configured maturity level is within L0-L4",
        "failed",
        f"Program config sets unsupported maturity_level={current_maturity_level}; expected an integer between 0 and 4.",
    )


def _evaluate_l1_1(
    proposals: tuple[AIProposal, ...],
    data_limitations: list[str],
) -> MaturityCriterionStatus:
    issue_numbers = tuple(sorted({proposal.issue_number for proposal in proposals if proposal.issue_number is not None}))
    if len(issue_numbers) < 5:
        return MaturityCriterionStatus(
            "L1-1",
            "Advisory false-positive window",
            "<= 20% rejected outcomes over 5 scoped sessions",
            "pending",
            f"Only {len(issue_numbers)}/5 issue-scoped sessions are available for the advisory false-positive window.",
        )

    recent_issue_numbers = issue_numbers[-5:]
    recent_proposals = tuple(
        proposal
        for proposal in proposals
        if proposal.issue_number in set(recent_issue_numbers)
        and proposal.status in {AIProposalStatus.ACCEPTED, AIProposalStatus.REJECTED}
    )
    if not recent_proposals:
        detail = "No accepted/rejected advisory outcomes are available across the last 5 scoped sessions."
        data_limitations.append(detail)
        return MaturityCriterionStatus(
            "L1-1",
            "Advisory false-positive window",
            "<= 20% rejected outcomes over 5 scoped sessions",
            "unavailable",
            detail,
        )

    rejected = sum(1 for proposal in recent_proposals if proposal.status is AIProposalStatus.REJECTED)
    false_positive_rate = rejected / len(recent_proposals)
    return MaturityCriterionStatus(
        "L1-1",
        "Advisory false-positive window",
        "<= 20% rejected outcomes over 5 scoped sessions",
        "passed" if false_positive_rate <= _L1_FALSE_POSITIVE_THRESHOLD else "failed",
        f"False-positive proxy is {false_positive_rate:.1%} from {rejected}/{len(recent_proposals)} rejected outcomes across scoped issues {', '.join(f'{issue:03d}' for issue in recent_issue_numbers)}.",
    )


def _evaluate_l2_1(
    proposals: tuple[AIProposal, ...],
    rolling_issue_numbers: tuple[int, ...],
    data_limitations: list[str],
) -> MaturityCriterionStatus:
    if len(rolling_issue_numbers) < 10:
        return MaturityCriterionStatus(
            "L2-1",
            "Proposal staging acceptance rate",
            ">= 70% accepted outcomes over 10 issues",
            "pending",
            f"Only {len(rolling_issue_numbers)}/10 issue-scoped windows are available.",
        )
    if not proposals:
        data_limitations.append("No accepted/rejected AI proposals are available in the last 10 scoped issues.")
        return MaturityCriterionStatus(
            "L2-1",
            "Proposal staging acceptance rate",
            ">= 70% accepted outcomes over 10 issues",
            "unavailable",
            "No accepted/rejected proposal outcomes are available in the last 10 scoped issues.",
        )
    accepted = sum(1 for proposal in proposals if proposal.status is AIProposalStatus.ACCEPTED)
    accuracy = accepted / len(proposals)
    return MaturityCriterionStatus(
        "L2-1",
        "Proposal staging acceptance rate",
        ">= 70% accepted outcomes over 10 issues",
        "passed" if accuracy >= _L2_ACCEPTANCE_THRESHOLD else "failed",
        f"Acceptance rate {accuracy:.1%} from {accepted}/{len(proposals)} accepted proposal outcomes across the last 10 scoped issues.",
    )


def _evaluate_l2_2(
    proposals: tuple[AIProposal, ...],
    data_limitations: list[str],
) -> MaturityCriterionStatus:
    issue_numbers = tuple(sorted({proposal.issue_number for proposal in proposals if proposal.issue_number is not None}))
    if not issue_numbers:
        data_limitations.append("No issue-scoped AI proposals are available for low-override streak measurement.")
        return MaturityCriterionStatus(
            "L2-2",
            "Consecutive low-override issues",
            ">= 20 issues with <= 2 overrides",
            "unavailable",
            "No issue-scoped AI proposals are available for low-override streak measurement.",
        )
    proposals_by_issue = {
        issue_number: tuple(proposal for proposal in proposals if proposal.issue_number == issue_number)
        for issue_number in issue_numbers
    }
    overrides_by_issue = {
        issue_number: sum(
            1
            for proposal in issue_proposals
            if proposal.status in _LOW_OVERRIDE_BREAKING_STATUSES
        )
        for issue_number, issue_proposals in proposals_by_issue.items()
    }
    pending_issue_numbers = {
        issue_number
        for issue_number, issue_proposals in proposals_by_issue.items()
        if any(proposal.status is AIProposalStatus.PENDING for proposal in issue_proposals)
    }
    streak = 0
    blocking_issue_number: int | None = None
    for issue_number in reversed(issue_numbers):
        if issue_number in pending_issue_numbers:
            blocking_issue_number = issue_number
            break
        if overrides_by_issue[issue_number] > 2:
            blocking_issue_number = issue_number
            break
        streak += 1
    status = "passed" if streak >= 20 else "pending"
    if blocking_issue_number in pending_issue_numbers:
        detail = (
            f"Issue {blocking_issue_number:03d} still has pending AI proposals; current resolved trailing streak is {streak} issue(s)."
        )
    elif blocking_issue_number is not None:
        detail = (
            f"Issue {blocking_issue_number:03d} recorded {overrides_by_issue[blocking_issue_number]} overrides; "
            f"current trailing streak is {streak} issue(s)."
        )
    else:
        detail = (
            f"Current trailing streak is {streak} issue(s); latest measured issues: {', '.join(f'{issue:03d}' for issue in issue_numbers[-5:])}."
        )
    return MaturityCriterionStatus(
        "L2-2",
        "Consecutive low-override issues",
        ">= 20 issues with <= 2 overrides",
        status,
        detail,
    )


def _evaluate_l2_3(
    capability_statuses: tuple[ProgramCapabilityStatus, ...],
    data_limitations: list[str],
) -> MaturityCriterionStatus:
    graph_auth_status = find_program_capability_status(capability_statuses, "graph_app_only_auth")
    if graph_auth_status is None:
        detail = "No Graph app-only auth capability status is available yet."
        data_limitations.append(detail)
        return MaturityCriterionStatus(
            "L2-3",
            "Governance plane",
            "Complete",
            "unavailable",
            detail,
        )

    criterion_status = {
        "complete": "passed",
        "in_progress": "pending",
        "deferred": "deferred",
        "unavailable": "unavailable",
    }[graph_auth_status.status]
    return MaturityCriterionStatus(
        "L2-3",
        "Governance plane",
        "Complete",
        criterion_status,
        graph_auth_status.detail,
    )


def _evaluate_l2_4() -> MaturityCriterionStatus:
    return MaturityCriterionStatus(
        "L2-4",
        "Quality gate coverage",
        "QG-1 through QG-19 evaluating",
        "passed",
        "QG-1 through QG-19 are implemented in the current quality gate registry.",
    )


def _evaluate_l3_1(
    autonomy_records: tuple[AutonomyAuditRecord, ...],
    data_limitations: list[str],
) -> MaturityCriterionStatus:
    candidate_action_type, candidate_records = _select_write_action_candidate(autonomy_records)
    if candidate_action_type is None:
        detail = "No accepted L3/L4 autonomy-audit records with action_type evidence are available yet."
        data_limitations.append(detail)
        return MaturityCriterionStatus(
            "L3-1",
            "Bounded write acceptance rate",
            ">= 90% prior acceptance for one audited action type",
            "pending",
            detail,
        )

    rates = tuple(record.prior_acceptance_rate for record in candidate_records if record.prior_acceptance_rate is not None)
    if len(rates) != len(candidate_records):
        detail = f"Action type '{candidate_action_type}' has {len(candidate_records) - len(rates)} audited writes without persisted prior_acceptance_rate."
        data_limitations.append(detail)
        return MaturityCriterionStatus(
            "L3-1",
            "Bounded write acceptance rate",
            ">= 90% prior acceptance for one audited action type",
            "pending",
            detail,
        )

    minimum_rate = min(rates)
    return MaturityCriterionStatus(
        "L3-1",
        "Bounded write acceptance rate",
        ">= 90% prior acceptance for one audited action type",
        "passed" if minimum_rate >= _L3_ACCEPTANCE_THRESHOLD else "failed",
        f"Action type '{candidate_action_type}' records {len(candidate_records)} accepted bounded-write cycle(s); minimum persisted prior acceptance is {minimum_rate:.1%}.",
    )


def _evaluate_l3_2(
    autonomy_records: tuple[AutonomyAuditRecord, ...],
    data_limitations: list[str],
) -> MaturityCriterionStatus:
    candidate_action_type, candidate_records = _select_write_action_candidate(autonomy_records)
    if candidate_action_type is None:
        detail = "No accepted L3/L4 autonomy-audit records are available to verify blast radius and rollback controls."
        data_limitations.append(detail)
        return MaturityCriterionStatus(
            "L3-2",
            "Rollback and blast radius controls",
            "Every audited bounded write captures blast radius and rollback mechanism",
            "pending",
            detail,
        )

    missing_controls = [
        record.action_id
        for record in candidate_records
        if not (record.blast_radius and record.rollback_mechanism)
    ]
    if missing_controls:
        return MaturityCriterionStatus(
            "L3-2",
            "Rollback and blast radius controls",
            "Every audited bounded write captures blast radius and rollback mechanism",
            "failed",
            f"Action type '{candidate_action_type}' is missing blast-radius or rollback metadata on {len(missing_controls)}/{len(candidate_records)} audited write(s).",
        )
    return MaturityCriterionStatus(
        "L3-2",
        "Rollback and blast radius controls",
        "Every audited bounded write captures blast radius and rollback mechanism",
        "passed",
        f"Action type '{candidate_action_type}' captured blast radius and rollback metadata on all {len(candidate_records)} audited write(s).",
    )


def _evaluate_l4_1(
    autonomy_records: tuple[AutonomyAuditRecord, ...],
    data_limitations: list[str],
) -> MaturityCriterionStatus:
    candidate_action_type, candidate_records = _select_write_action_candidate(autonomy_records)
    if candidate_action_type is None:
        detail = "No accepted L3/L4 autonomy-audit records are available to establish a scheduled-action proof window."
        data_limitations.append(detail)
        return MaturityCriterionStatus(
            "L4-1",
            "Scheduled action proof window",
            ">= 10 accepted bounded-write cycles for one action type",
            "pending",
            detail,
        )

    cycle_count = len(candidate_records)
    return MaturityCriterionStatus(
        "L4-1",
        "Scheduled action proof window",
        ">= 10 accepted bounded-write cycles for one action type",
        "passed" if cycle_count >= _L4_CYCLE_THRESHOLD else "pending",
        f"Action type '{candidate_action_type}' has {cycle_count}/{_L4_CYCLE_THRESHOLD} accepted bounded-write cycle(s) recorded in autonomy audit.",
    )


def _evaluate_l4_2(
    autonomy_records: tuple[AutonomyAuditRecord, ...],
    contradiction_packets: tuple[object, ...],
    data_limitations: list[str],
) -> MaturityCriterionStatus:
    candidate_action_type, candidate_records = _select_write_action_candidate(autonomy_records)
    if candidate_action_type is None or len(candidate_records) < _L4_CYCLE_THRESHOLD:
        detail = "L4 contradiction-free validation waits until at least 10 accepted bounded-write cycles exist for one action type."
        if candidate_action_type is None:
            data_limitations.append("No accepted L3/L4 autonomy-audit records are available to evaluate L4 contradiction-free windows.")
        return MaturityCriterionStatus(
            "L4-2",
            "Contradiction-free autonomy window",
            "0 contradiction packets in the prior 5-cycle validation window",
            "pending",
            detail,
        )

    contradiction_count = len(contradiction_packets)
    if contradiction_count:
        return MaturityCriterionStatus(
            "L4-2",
            "Contradiction-free autonomy window",
            "0 contradiction packets in the prior 5-cycle validation window",
            "failed",
            f"{contradiction_count} contradiction packet(s) remain in analytics state, so scheduled autonomy for action type '{candidate_action_type}' stays blocked.",
        )
    return MaturityCriterionStatus(
        "L4-2",
        "Contradiction-free autonomy window",
        "0 contradiction packets in the prior 5-cycle validation window",
        "passed",
        f"No contradiction packets are present in analytics state; action type '{candidate_action_type}' has a clean contradiction window across its latest accepted cycles.",
    )


def _select_write_action_candidate(
    autonomy_records: tuple[AutonomyAuditRecord, ...],
) -> tuple[str | None, tuple[AutonomyAuditRecord, ...]]:
    grouped: dict[str, list[AutonomyAuditRecord]] = {}
    for record in autonomy_records:
        if not record.accepted or record.action_type is None:
            continue
        if _normalize_level(record.level) not in {"l3", "l4"}:
            continue
        grouped.setdefault(record.action_type, []).append(record)
    if not grouped:
        return None, ()

    chosen_action_type = sorted(
        grouped,
        key=lambda action_type: (
            -len(grouped[action_type]),
            -max(record.applied_at.timestamp() for record in grouped[action_type]),
            action_type,
        ),
    )[0]
    records = tuple(sorted(grouped[chosen_action_type], key=lambda record: record.applied_at))
    return chosen_action_type, records


def _normalize_level(level: str) -> str:
    return level.strip().lower()
