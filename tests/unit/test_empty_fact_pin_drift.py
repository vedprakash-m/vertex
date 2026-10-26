from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.exceptions import ConfigError
from src.core.program_fact_store import FactReviewState, ProgramFactInput, ProgramFactStore
from src.core.quality_gates.fact_drift import evaluate_program_fact_drift_from_draft


def test_accepted_fact_after_empty_pin_blocks_existing_release_gate(tmp_path) -> None:
    store = ProgramFactStore("fixture", db_root=tmp_path)
    pin = store.pin_snapshot(created_at=datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert pin.pinned_revision_count == 0
    assert pin.pinned_recorded_at is None
    accepted = store.append_fact(
        ProgramFactInput(
            fact_type="action", scope="workitem:1", entity_refs=("WI:1",),
            payload={"status": "open"},
        ),
        recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    drift = store.detect_drift(pin.snapshot_id)

    assert tuple(fact.revision_id for fact in drift) == (accepted.revision.revision_id,)
    report = evaluate_program_fact_drift_from_draft(
        draft_state={"program_fact_snapshot": {
            "snapshot_id": pin.snapshot_id, "program_id": "fixture",
        }},
        program_id="fixture", db_root=tmp_path,
    )
    assert report.results[0].gate_id == "QG-SG-20"
    assert report.results[0].passed is False
    assert report.results[0].exit_code == 3


def test_empty_pin_ignores_proposed_only_facts(tmp_path) -> None:
    store = ProgramFactStore("fixture", db_root=tmp_path)
    pin = store.pin_snapshot()
    assert store.detect_drift(pin.snapshot_id) == ()
    store.append_fact(ProgramFactInput(
        fact_type="action", scope="workitem:1", entity_refs=("WI:1",),
        payload={"status": "open"}, review_state=FactReviewState.PROPOSED,
    ))
    assert store.detect_drift(pin.snapshot_id) == ()


def test_unknown_pin_remains_an_explicit_store_and_release_gate_failure(tmp_path) -> None:
    store = ProgramFactStore("fixture", db_root=tmp_path)
    with pytest.raises(KeyError, match="Unknown snapshot pin"):
        store.detect_drift("missing-pin")
    with pytest.raises(ConfigError, match="snapshot is unavailable"):
        evaluate_program_fact_drift_from_draft(
            draft_state={"program_fact_snapshot": {
                "snapshot_id": "missing-pin", "program_id": "fixture",
            }},
            program_id="fixture", db_root=tmp_path,
        )
