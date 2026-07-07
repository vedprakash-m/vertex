from __future__ import annotations


TERMINAL_WORK_ITEM_STATES: frozenset[str] = frozenset(
    {
        "closed",
        "done",
        "resolved",
        "completed",
        "removed",
        "cut",
    }
)


def _title_cased_terminal_states() -> tuple[str, ...]:
    # Returns Title-Case forms for ADO WIQL query construction.
    return tuple(state.title() for state in TERMINAL_WORK_ITEM_STATES)


TERMINAL_WORK_ITEM_STATES_ADO: tuple[str, ...] = _title_cased_terminal_states()
