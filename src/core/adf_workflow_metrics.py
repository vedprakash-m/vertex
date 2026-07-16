"""ADF-W2.11/W3.8/W4.8 (specs/arch-data-fix.md): review-latency, cycle-time,
and proposal-volume aggregation over `proposal_audit.py`'s durable trail.

Ratified by ADR-0017 (`governance/decisions/0017-workflow-measurement-instrumentation.md`,
2026-07-13). "Active time" (the time a TPM is actually engaged, excluding
idle time between when a proposal was staged and when a human happened to
look at it) is not measurable from server-side timestamps alone -- this
module computes **review latency** (decided_at - proposed_at) as an honest,
explicitly-labeled proxy, not a claim of true active time. The distinction
is documented, not hidden, per ADR-0017 Decision 2.

Zone A -- no AI or M365 imports.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.proposal_audit import ProposalType, read_proposal_audit


@dataclass(frozen=True, slots=True)
class ReviewLatencySummary:
    proposal_type: ProposalType
    decided_count: int
    approved_count: int
    rejected_count: int
    p50_latency_seconds: float | None
    p90_latency_seconds: float | None
    max_latency_seconds: float | None


@dataclass(frozen=True, slots=True)
class WorkflowMeasurementReport:
    program_id: str
    total_proposal_events: int
    by_type: tuple[ReviewLatencySummary, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "program_id": self.program_id,
            "total_proposal_events": self.total_proposal_events,
            "by_type": [
                {
                    "proposal_type": summary.proposal_type,
                    "decided_count": summary.decided_count,
                    "approved_count": summary.approved_count,
                    "rejected_count": summary.rejected_count,
                    "p50_latency_seconds": summary.p50_latency_seconds,
                    "p90_latency_seconds": summary.p90_latency_seconds,
                    "max_latency_seconds": summary.max_latency_seconds,
                }
                for summary in self.by_type
            ],
        }


_ALL_TYPES: tuple[ProposalType, ...] = (
    "risk",
    "meeting_action",
    "top_three",
    "governance_decision_brief",
    "dependency_blast_radius",
)


def _percentile(sorted_values: list[float], fraction: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    index = fraction * (len(sorted_values) - 1)
    lower = int(index)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = index - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * weight


def compute_workflow_measurement_report(
    program_id: str,
    *,
    since: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> WorkflowMeasurementReport:
    """Reads `proposal_audit.jsonl` and computes review-latency percentiles
    and proposal volume per type. `since` filters to decisions at or after
    that instant (for a "this week" report); omit for all-time.

    Each decision record already carries both its own `at` (decided_at) and
    `proposed_at` (copied from the proposal object at decision time), so
    latency is `record.at - record.proposed_at` directly -- no pairing
    against a separate "proposed" event is needed or attempted.
    """
    all_records = read_proposal_audit(program_id, programs_root=programs_root)
    if since is not None:
        all_records = tuple(record for record in all_records if record.at >= since)

    summaries: list[ReviewLatencySummary] = []
    for proposal_type in _ALL_TYPES:
        decisions = tuple(
            record
            for record in all_records
            if record.proposal_type == proposal_type and record.event in ("approved", "rejected")
        )
        latencies = sorted(
            (record.at - record.proposed_at).total_seconds()
            for record in decisions
            if record.proposed_at is not None
        )
        approved_count = sum(1 for record in decisions if record.event == "approved")
        rejected_count = sum(1 for record in decisions if record.event == "rejected")
        summaries.append(
            ReviewLatencySummary(
                proposal_type=proposal_type,
                decided_count=len(decisions),
                approved_count=approved_count,
                rejected_count=rejected_count,
                p50_latency_seconds=_percentile(latencies, 0.50) if latencies else None,
                p90_latency_seconds=_percentile(latencies, 0.90) if latencies else None,
                max_latency_seconds=max(latencies) if latencies else None,
            )
        )

    return WorkflowMeasurementReport(
        program_id=program_id,
        total_proposal_events=len(all_records),
        by_type=tuple(summaries),
    )


__all__ = [
    "ReviewLatencySummary",
    "WorkflowMeasurementReport",
    "compute_workflow_measurement_report",
]
