from __future__ import annotations

from src.core.ncfl_apply_policy import (
    NCFL_APPLY_STATES,
    NCFL_APPLY_TERMINAL_STATES,
    is_valid_apply_transition,
    recommended_ncfl_apply_durability_decision,
    valid_next_apply_states,
)


def test_recommended_ncfl_apply_decision_reuses_beta_outbox_with_apply_journal() -> None:
    decision = recommended_ncfl_apply_durability_decision()

    assert decision.strategy == "reuse_beta_outbox_plus_minimal_apply_journal"
    assert decision.uses_beta_outbox is True
    assert decision.requires_apply_journal is True
    assert "YAML/changelog" in decision.journal_scope


def test_apply_state_machine_matches_recoverable_spec() -> None:
    assert NCFL_APPLY_STATES == (
        "proposed",
        "write_started",
        "yaml_written",
        "changelog_written",
        "ledger_written",
        "applied",
        "needs_repair",
    )
    assert NCFL_APPLY_TERMINAL_STATES == frozenset({"applied", "needs_repair"})


def test_apply_state_machine_allows_forward_progress_and_repair_offramps() -> None:
    assert is_valid_apply_transition("proposed", "write_started")
    assert is_valid_apply_transition("write_started", "yaml_written")
    assert is_valid_apply_transition("yaml_written", "changelog_written")
    assert is_valid_apply_transition("changelog_written", "ledger_written")
    assert is_valid_apply_transition("ledger_written", "applied")

    for state in ("proposed", "write_started", "yaml_written", "changelog_written", "ledger_written"):
        assert is_valid_apply_transition(state, "needs_repair")


def test_terminal_apply_states_have_no_next_states() -> None:
    assert valid_next_apply_states("applied") == frozenset()
    assert valid_next_apply_states("needs_repair") == frozenset()
