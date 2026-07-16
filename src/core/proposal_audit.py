"""ADF-W2.11/W3.8/W4.8 (specs/arch-data-fix.md): a generic, append-only
audit trail for the proposal-shaped types built this session (RiskProposal,
MeetingAction, TopThreeCandidateProposal, GovernanceDecisionBriefProposal,
DependencyBlastRadiusProposal).

Ratified by ADR-0017 (`governance/decisions/0017-workflow-measurement-instrumentation.md`,
2026-07-13). None of those five types were durably persisted anywhere --
each lives only in memory for the duration of one CLI invocation, so
without this module there is nothing a later "weekly measurement" report
could query. This is a new, small, generic sidecar (one file, one schema,
one discriminator field) rather than five bespoke stores or a shoehorned
reuse of `ai_proposal_store.py` (which is tightly coupled to a different,
older `WorkstreamSynthesis` payload shape -- see ADR-0017's alternatives).

Recording is opt-in and additive: `record_proposal_event` is called by each
type's `approve_*`/`reject_*` helper only when a caller passes
`programs_root` -- when omitted (the default), the call is a pure no-op and
every pre-existing test/call site is unaffected. This mirrors the session's
established "additive, backward-compatible, zero behavior change for
existing callers" convention (e.g. `Dependency.evidence_tier`).

Zone A -- no AI or M365 imports.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Literal

from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.jsonl_utils import append_jsonl_line, read_jsonl_records

ProposalType = Literal[
    "risk",
    "meeting_action",
    "top_three",
    "governance_decision_brief",
    "dependency_blast_radius",
]
ProposalEvent = Literal["proposed", "approved", "rejected", "reversed"]

_MAX_BYTES = 10 * 1024 * 1024  # matches journal/ai_proposals.jsonl's rotation cap


def proposal_audit_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "journal" / "proposal_audit.jsonl"


@dataclass(frozen=True, slots=True)
class ProposalAuditRecord:
    program_id: str
    proposal_type: ProposalType
    proposal_id: str
    event: ProposalEvent
    at: datetime
    # The proposal's own `proposed_at` (its construction-time stamp), carried
    # on every decision record so review latency (`at - proposed_at`) is
    # computable from this one record -- no pairing/joining against a
    # separate "proposed" event required. `None` only if a caller genuinely
    # doesn't have it (should not happen for the five wired proposal types).
    proposed_at: datetime | None = None
    ai_run_id: str | None = None
    # Also reused (not renamed, to avoid a second near-identical field) as
    # the free-text reason on a "reversed" event (ADF-W5.12 P4's
    # `flag-regression` command) -- both are "why a decision didn't stand,"
    # just at different times (at decision time vs. after the fact).
    rejection_reason: str | None = None
    # ADF-W5.12 P4 (Section 8.15.1/8.15.2): False marks a decision that was
    # never individually eyeballed by a human -- it was auto-approved as
    # part of a sampled-review batch's implicit extension of trust from the
    # reviewed sample to the rest of the batch. Default True preserves every
    # pre-existing caller's behavior (individually-reviewed decisions).
    reviewed: bool = True


def record_proposal_event(
    *,
    program_id: str,
    proposal_type: ProposalType,
    proposal_id: str,
    event: ProposalEvent,
    programs_root: Path | None,
    at: datetime | None = None,
    proposed_at: datetime | None = None,
    ai_run_id: str | None = None,
    rejection_reason: str | None = None,
    reviewed: bool = True,
) -> None:
    """Append one audit-trail record. A no-op when `programs_root` is
    `None` -- callers must opt in explicitly (see module docstring)."""
    if programs_root is None:
        return
    record = ProposalAuditRecord(
        program_id=program_id,
        proposal_type=proposal_type,
        proposal_id=proposal_id,
        event=event,
        at=at or datetime.now(timezone.utc),
        proposed_at=proposed_at,
        ai_run_id=ai_run_id,
        rejection_reason=rejection_reason,
        reviewed=reviewed,
    )
    line = json.dumps(_to_jsonable(record), sort_keys=True) + "\n"
    append_jsonl_line(proposal_audit_path(program_id, programs_root=programs_root), line, max_bytes=_MAX_BYTES)


def _to_jsonable(record: ProposalAuditRecord) -> dict[str, object]:
    return {
        "program_id": record.program_id,
        "proposal_type": record.proposal_type,
        "proposal_id": record.proposal_id,
        "event": record.event,
        "at": record.at.isoformat(),
        "proposed_at": record.proposed_at.isoformat() if record.proposed_at is not None else None,
        "ai_run_id": record.ai_run_id,
        "rejection_reason": record.rejection_reason,
        "reviewed": record.reviewed,
    }


def read_proposal_audit(
    program_id: str, *, programs_root: Path = PROGRAMS_ROOT
) -> tuple[ProposalAuditRecord, ...]:
    path = proposal_audit_path(program_id, programs_root=programs_root)
    if not path.exists():
        return ()
    records = []
    for raw in read_jsonl_records(path):
        records.append(
            ProposalAuditRecord(
                program_id=raw["program_id"],
                proposal_type=raw["proposal_type"],
                proposal_id=raw["proposal_id"],
                event=raw["event"],
                at=datetime.fromisoformat(raw["at"]),
                proposed_at=datetime.fromisoformat(raw["proposed_at"]) if raw.get("proposed_at") else None,
                ai_run_id=raw.get("ai_run_id"),
                rejection_reason=raw.get("rejection_reason"),
                reviewed=bool(raw.get("reviewed", True)),
            )
        )
    return tuple(records)


__all__ = [
    "ProposalAuditRecord",
    "ProposalEvent",
    "ProposalType",
    "proposal_audit_path",
    "read_proposal_audit",
    "record_proposal_event",
]
