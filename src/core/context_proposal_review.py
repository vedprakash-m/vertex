"""Read-only, consistent presentation rows for pending NCFL context proposals.

The proposal store remains the system of record. This module deliberately does
not decide, apply, or rewrite proposals; it gives triage, cockpit, and
reviewer HTML one operator-facing representation of the same review queue.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from src.core.context_gap_store import RankedGap, load_context_gaps, rank_context_gaps
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.ncfl_proposal_store import conflicting_pending_proposals, load_proposals, reply_origin_label
from src.core.ncfl_store_policy import is_ncfl_apply_writable_target_store


@dataclass(frozen=True, slots=True)
class ContextProposalReviewRow:
    """All information required to review one pending context revision."""

    proposal_id: str
    edition_id: str
    issue_number: int
    target: str
    proposed_value: str
    current_value_hash: str | None
    evidence: str
    conflict_state: str
    next_command: str

    @property
    def current_hash_label(self) -> str:
        return self.current_value_hash or "new record (no current-value hash)"


def load_pending_context_proposal_rows(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[ContextProposalReviewRow, ...]:
    """Return pending proposals with durable conflict and action context."""
    conflicts = conflicting_pending_proposals(program_id, programs_root=programs_root)
    proposals = load_proposals(
        program_id,
        status_filter={"pending"},
        programs_root=programs_root,
    )
    rows: list[ContextProposalReviewRow] = []
    for proposal in proposals:
        target = f"{proposal.target_store}.{proposal.target_key}.{proposal.target_field}"
        conflict_ids = tuple(
            str(entry["proposal_id"])
            for entry in conflicts.get(proposal.conflict_key, ())
            if entry.get("proposal_id") != proposal.proposal_id and entry.get("proposal_id") is not None
        )
        conflict_state = (
            f"conflicts with {', '.join(conflict_ids)}"
            if conflict_ids
            else "no cross-issue conflict"
        )
        if is_ncfl_apply_writable_target_store(proposal.target_store):
            next_command = (
                f"vertex context proposals --edition {proposal.edition_id} --issue {proposal.issue_number}"
            )
        else:
            next_command = (
                f"vertex context manual-diff --edition {proposal.edition_id} --issue {proposal.issue_number} "
                f"--proposal-id {proposal.proposal_id}"
            )
        rows.append(
            ContextProposalReviewRow(
                proposal_id=proposal.proposal_id,
                edition_id=proposal.edition_id,
                issue_number=proposal.issue_number,
                target=target,
                proposed_value=proposal.source_value,
                current_value_hash=proposal.current_value_hash,
                evidence=(
                    f"{proposal.source_artifact}:{proposal.source_field}"
                    + (
                        f" | {proposal.extraction_method_rationale}"
                        f" | {reply_origin_label(proposal, programs_root=programs_root)}"
                        if proposal.source_type == "context_gap_reply" else ""
                    )
                ),
                conflict_state=conflict_state,
                next_command=next_command,
            )
        )
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class ContextMaintenanceReview:
    proposals: tuple[ContextProposalReviewRow, ...]
    gaps: tuple[RankedGap, ...]
    gap_diagnostic: str | None
    evaluated_at: datetime
    proposal_diagnostic: str | None = None


def load_context_maintenance_review(
    program_id: str, *, programs_root: Path, as_of: datetime,
) -> ContextMaintenanceReview:
    proposal_diagnostic = None
    try:
        proposals = load_pending_context_proposal_rows(program_id, programs_root=programs_root)
    except (OSError, ValueError):
        proposals = ()
        proposal_diagnostic = "Context proposal history unavailable; inspect and repair it before retrying."
    diagnostic = None
    gaps: tuple[RankedGap, ...] = ()
    try:
        records = load_context_gaps(program_id, programs_root=programs_root)
    except (OSError, ValueError):
        diagnostic = "Context gap history unavailable; inspect and repair it before retrying."
    else:
        if any(record.program != program_id for record in records):
            diagnostic = "Context gap history unavailable: observations contain a foreign program."
        else:
            try:
                gaps = tuple(rank_context_gaps(records, programs_root=programs_root))
            except (OSError, ValueError, yaml.YAMLError):
                diagnostic = (
                    "Context gap ranking unavailable; inspect the selected workstream_registry.yaml "
                    "and recorded gap targets before retrying."
                )
    return ContextMaintenanceReview(proposals, gaps, diagnostic, as_of.astimezone(timezone.utc), proposal_diagnostic)


__all__ = [
    "ContextProposalReviewRow", "load_pending_context_proposal_rows",
    "ContextMaintenanceReview", "load_context_maintenance_review",
]
