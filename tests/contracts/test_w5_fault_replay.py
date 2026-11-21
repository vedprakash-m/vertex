from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.actuation_fault_harness import (
    FAULT_SCENARIOS,
    replay_fault_scenario,
)


@pytest.mark.parametrize("scenario", FAULT_SCENARIOS, ids=lambda item: item.value)
def test_fault_replay_is_deterministic_and_contains_provider_effects(
    tmp_path: Path,
    scenario,
) -> None:
    first = replay_fault_scenario(tmp_path / "first", scenario)
    second = replay_fault_scenario(tmp_path / "second", scenario)

    assert first == second
    assert first.duplicate_provider_effects == 0
    assert first.unqualified_provider_callbacks == 0
    assert first.final_status in first.expected_statuses
    assert first.receipt_statuses
    assert first.alerts >= first.minimum_alerts


def test_fault_matrix_covers_every_required_lifecycle_boundary(tmp_path: Path) -> None:
    scenarios = {scenario.value for scenario in FAULT_SCENARIOS}
    assert scenarios == {
        "before_lease",
        "after_lease",
        "definite_failure",
        "response_loss_confirmed",
        "response_loss_absent",
        "response_loss_conflict",
        "before_success_receipt",
        "after_succeeded",
        "after_audited",
        "duplicate_semantic_effect",
        "compensation_required",
    }
    replayed = {replay_fault_scenario(tmp_path / item.value, item) for item in FAULT_SCENARIOS}
    assert len(replayed) == len(FAULT_SCENARIOS)
