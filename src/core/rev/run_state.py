"""REV per-candidate run-state machine (Zone A).

specs/program-context-intelligence.md §5.10. Each candidate advances through a
deterministic state machine:

    enumerated → locator_resolved → hydration_required
        → hydrated* → scanned* → extracted_ephemerally*
        → excerpts_vaulted → candidate_staged → candidate_verified → accepted

Stages marked ``*`` are **ephemeral** — they live only in the in-process run.
On crash/resume, any candidate whose persisted state is ephemeral **reverts to
``hydration_required``** so the next run re-hydrates from the durable locator
rather than trusting transient content. Durable stages (``excerpts_vaulted``
onward) are checkpointed and survive a crash — vaulted excerpts and staged
candidates are preserved (QG-DM-4 retention-by-reference; no silent loss).

The transition log is append-only (one JSONL line per transition); the
**current state** is *derived* (last transition for a candidate wins), so the
projection is replay-deterministic (QG-DM-2) and a corrupt tail line never
flips earlier durable state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from src.core.jsonl_utils import append_jsonl_line, read_jsonl_records, validate_jsonl_row
from src.core.ledger.candidate_store import PROGRAMS_ROOT, get_candidate_dir


class RunState(str, Enum):
    ENUMERATED = "enumerated"
    LOCATOR_RESOLVED = "locator_resolved"
    HYDRATION_REQUIRED = "hydration_required"
    HYDRATED = "hydrated"                       # ephemeral
    SCANNED = "scanned"                         # ephemeral
    EXTRACTED_EPHEMERALLY = "extracted_ephemerally"  # ephemeral
    EXCERPTS_VAULTED = "excerpts_vaulted"
    CANDIDATE_STAGED = "candidate_staged"
    CANDIDATE_VERIFIED = "candidate_verified"
    ACCEPTED = "accepted"
    # Terminal non-acceptance states (durable):
    REJECTED = "rejected"
    QUARANTINED = "quarantined"
    METADATA_ONLY_STAGED = "metadata_only_staged"


EPHEMERAL_STATES = frozenset({RunState.HYDRATED, RunState.SCANNED, RunState.EXTRACTED_EPHEMERALLY})
DURABLE_STATES = frozenset({
    RunState.ENUMERATED, RunState.LOCATOR_RESOLVED, RunState.HYDRATION_REQUIRED,
    RunState.EXCERPTS_VAULTED, RunState.CANDIDATE_STAGED, RunState.CANDIDATE_VERIFIED,
    RunState.ACCEPTED, RunState.REJECTED, RunState.QUARANTINED, RunState.METADATA_ONLY_STAGED,
})
TERMINAL_STATES = frozenset({RunState.ACCEPTED, RunState.REJECTED, RunState.QUARANTINED})

# Allowed forward edges (§5.10). Crash-revert (ephemeral → hydration_required)
# is handled outside this table by ``crash_revert``.
_ALLOWED_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.ENUMERATED: frozenset({RunState.LOCATOR_RESOLVED, RunState.QUARANTINED, RunState.METADATA_ONLY_STAGED}),
    RunState.LOCATOR_RESOLVED: frozenset({RunState.HYDRATION_REQUIRED, RunState.QUARANTINED, RunState.METADATA_ONLY_STAGED}),
    RunState.HYDRATION_REQUIRED: frozenset({RunState.HYDRATED, RunState.METADATA_ONLY_STAGED, RunState.QUARANTINED}),
    RunState.HYDRATED: frozenset({RunState.SCANNED, RunState.HYDRATION_REQUIRED, RunState.QUARANTINED}),
    RunState.SCANNED: frozenset({RunState.EXTRACTED_EPHEMERALLY, RunState.HYDRATION_REQUIRED, RunState.QUARANTINED}),
    RunState.EXTRACTED_EPHEMERALLY: frozenset({RunState.EXCERPTS_VAULTED, RunState.HYDRATION_REQUIRED, RunState.QUARANTINED, RunState.METADATA_ONLY_STAGED}),
    RunState.EXCERPTS_VAULTED: frozenset({RunState.CANDIDATE_STAGED, RunState.QUARANTINED}),
    RunState.CANDIDATE_STAGED: frozenset({RunState.CANDIDATE_VERIFIED, RunState.REJECTED, RunState.QUARANTINED, RunState.METADATA_ONLY_STAGED}),
    RunState.CANDIDATE_VERIFIED: frozenset({RunState.ACCEPTED, RunState.REJECTED}),
    RunState.ACCEPTED: frozenset(),
    RunState.REJECTED: frozenset(),
    RunState.QUARANTINED: frozenset(),
    RunState.METADATA_ONLY_STAGED: frozenset({RunState.REJECTED, RunState.CANDIDATE_STAGED, RunState.QUARANTINED}),
}


def is_valid_transition(from_state: RunState, to_state: RunState) -> bool:
    return to_state in _ALLOWED_TRANSITIONS.get(from_state, frozenset())


def is_ephemeral(state: RunState) -> bool:
    return state in EPHEMERAL_STATES


def is_terminal(state: RunState) -> bool:
    return state in TERMINAL_STATES


@dataclass(frozen=True, slots=True)
class RunStateRecord:
    candidate_id: str
    state: str
    recorded_at: datetime
    ephemeral: bool
    correlation_id: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "state": self.state,
            "recorded_at": self.recorded_at.isoformat(),
            "ephemeral": self.ephemeral,
            "correlation_id": self.correlation_id,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RunStateRecord":
        return cls(
            candidate_id=str(payload["candidate_id"]),
            state=str(payload["state"]),
            recorded_at=datetime.fromisoformat(str(payload["recorded_at"])).astimezone(timezone.utc),
            ephemeral=bool(payload.get("ephemeral", False)),
            correlation_id=str(payload.get("correlation_id", "")),
            note=str(payload.get("note", "")),
        )


RUN_STATES_MAX_BYTES = 10 * 1024 * 1024


def _run_states_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return get_candidate_dir(program_id, programs_root=programs_root) / "rev_run_states.jsonl"


def append_run_state(
    record: RunStateRecord,
    *,
    program_id: str,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    """Append one transition to the per-program run-state log (append-only)."""
    line = json.dumps(record.to_dict(), sort_keys=True) + "\n"
    append_jsonl_line(
        _run_states_path(program_id, programs_root=programs_root),
        line,
        max_bytes=RUN_STATES_MAX_BYTES,
    )


def load_run_states(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[RunStateRecord, ...]:
    path = _run_states_path(program_id, programs_root=programs_root)
    if not path.exists():
        return ()
    rows = read_jsonl_records(path)
    out: list[RunStateRecord] = []
    for row in rows:
        validate_jsonl_row(row, ("candidate_id", "state", "recorded_at"), field_name="run_state")
        out.append(RunStateRecord.from_dict(row))
    return tuple(out)


def current_state_by_candidate(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    apply_crash_revert: bool = True,
) -> dict[str, RunStateRecord]:
    """Derive each candidate's current state (last transition wins).

    When ``apply_crash_revert`` is True, any candidate whose current state is
    ephemeral is reverted to ``hydration_required`` (§5.10 crash-safety): the
    durable locator is trusted, the transient content is not.
    """
    latest: dict[str, RunStateRecord] = {}
    for record in load_run_states(program_id, programs_root=programs_root):
        latest[record.candidate_id] = record
    if not apply_crash_revert:
        return latest
    reverted: dict[str, RunStateRecord] = {}
    for candidate_id, record in latest.items():
        try:
            state = RunState(record.state)
        except ValueError:
            reverted[candidate_id] = record
            continue
        if is_ephemeral(state):
            reverted[candidate_id] = crash_revert(record)
        else:
            reverted[candidate_id] = record
    return reverted


def crash_revert(record: RunStateRecord) -> RunStateRecord:
    """Revert an ephemeral-state record to ``hydration_required`` (§5.10)."""
    return RunStateRecord(
        candidate_id=record.candidate_id,
        state=RunState.HYDRATION_REQUIRED.value,
        recorded_at=record.recorded_at,
        ephemeral=False,
        correlation_id=record.correlation_id,
        note=f"crash_revert_from:{record.state}",
    )


def advance(
    program_id: str,
    candidate_id: str,
    from_state: RunState,
    to_state: RunState,
    *,
    correlation_id: str = "",
    note: str = "",
    set_at: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> RunStateRecord:
    """Validate + append a forward transition; raises on invalid edge."""
    if is_terminal(from_state):
        raise ValueError(f"cannot advance from terminal state {from_state.value}")
    if not is_valid_transition(from_state, to_state):
        raise ValueError(f"invalid transition {from_state.value}->{to_state.value}")
    record = RunStateRecord(
        candidate_id=candidate_id,
        state=to_state.value,
        recorded_at=set_at or datetime.now(timezone.utc),
        ephemeral=is_ephemeral(to_state),
        correlation_id=correlation_id,
        note=note,
    )
    append_run_state(record, program_id=program_id, programs_root=programs_root)
    return record


def state_distribution(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> dict[str, int]:
    """Per-state candidate counts (FR-PCI-12 / doctor --rev-health)."""
    current = current_state_by_candidate(program_id, programs_root=programs_root)
    dist: dict[str, int] = {}
    for record in current.values():
        dist[record.state] = dist.get(record.state, 0) + 1
    return dict(sorted(dist.items()))


__all__ = [
    "RunState",
    "RunStateRecord",
    "EPHEMERAL_STATES",
    "DURABLE_STATES",
    "TERMINAL_STATES",
    "is_valid_transition",
    "is_ephemeral",
    "is_terminal",
    "append_run_state",
    "load_run_states",
    "current_state_by_candidate",
    "crash_revert",
    "advance",
    "state_distribution",
]