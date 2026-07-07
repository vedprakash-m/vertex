"""NCFL apply durability decision artifact.

This module defines the recommended recoverable-apply state machine without
performing any Plane 1 writes.  It is a decision-support artifact for the
ADR-0006 S-NC-apply gate (originally specs/consolidated.md, now folded into
the core specs; the consolidated doc is archived at
.archive/specs/consolidated.md, local-only), not approval to enable
``vertex context apply``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


NcflApplyState = Literal[
    "proposed",
    "write_started",
    "yaml_written",
    "changelog_written",
    "ledger_written",
    "applied",
    "needs_repair",
]


NCFL_APPLY_STATES: tuple[NcflApplyState, ...] = (
    "proposed",
    "write_started",
    "yaml_written",
    "changelog_written",
    "ledger_written",
    "applied",
    "needs_repair",
)

NCFL_APPLY_TERMINAL_STATES: frozenset[NcflApplyState] = frozenset({"applied", "needs_repair"})

NCFL_APPLY_TRANSITIONS: dict[NcflApplyState, frozenset[NcflApplyState]] = {
    "proposed": frozenset({"write_started", "needs_repair"}),
    "write_started": frozenset({"yaml_written", "needs_repair"}),
    "yaml_written": frozenset({"changelog_written", "needs_repair"}),
    "changelog_written": frozenset({"ledger_written", "needs_repair"}),
    "ledger_written": frozenset({"applied", "needs_repair"}),
    "applied": frozenset(),
    "needs_repair": frozenset(),
}


@dataclass(frozen=True, slots=True)
class NcflApplyDurabilityDecision:
    strategy: str
    uses_beta_outbox: bool
    requires_apply_journal: bool
    journal_scope: str
    states: tuple[NcflApplyState, ...]
    terminal_states: frozenset[NcflApplyState]


def recommended_ncfl_apply_durability_decision() -> NcflApplyDurabilityDecision:
    return NcflApplyDurabilityDecision(
        strategy="reuse_beta_outbox_plus_minimal_apply_journal",
        uses_beta_outbox=True,
        requires_apply_journal=True,
        journal_scope="YAML/changelog recovery only; ledger idempotency remains beta-outbox-backed",
        states=NCFL_APPLY_STATES,
        terminal_states=NCFL_APPLY_TERMINAL_STATES,
    )


def valid_next_apply_states(state: NcflApplyState) -> frozenset[NcflApplyState]:
    return NCFL_APPLY_TRANSITIONS[state]


def is_valid_apply_transition(from_state: NcflApplyState, to_state: NcflApplyState) -> bool:
    return to_state in valid_next_apply_states(from_state)
