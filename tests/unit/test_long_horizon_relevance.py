"""RELO-W4.10: Tech Spec Sec 8.4.4 long-horizon relevance selection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.core.long_horizon_relevance import (
    DEFAULT_MAX_ITEMS,
    PriorOutcomeRecord,
    RelevanceReason,
    select_prior_outcomes,
)

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _rec(
    record_id: str,
    entity_id: str,
    *,
    day: int = 0,
    linked: tuple[str, ...] = (),
    as_of_day: int | None = None,
) -> PriorOutcomeRecord:
    return PriorOutcomeRecord(
        record_id=record_id,
        entity_id=entity_id,
        kind="milestone",
        as_of=_T0 + timedelta(days=day if as_of_day is None else as_of_day),
        valid_at=_T0 + timedelta(days=day),
        outcome_summary=f"outcome {record_id}",
        evidence_refs=(f"ado:{record_id}",),
        linked_entity_ids=linked,
    )


def _ids(selection) -> list[str]:
    return [item.record.record_id for item in selection.selected]


def test_same_entity_ranks_before_linked_before_recency():
    records = [
        _rec("r-recent", "other", day=30),
        _rec("r-linked", "dep", day=20, linked=("ws-1",)),
        _rec("r-same", "ws-1", day=1),
    ]
    selection = select_prior_outcomes(records, {"ws-1"})
    assert _ids(selection) == ["r-same", "r-linked", "r-recent"]
    assert [item.reason for item in selection.selected] == [
        RelevanceReason.SAME_ENTITY,
        RelevanceReason.LINKED_ENTITY,
        RelevanceReason.RECENCY,
    ]


def test_within_tier_newer_valid_at_first_and_ties_break_by_record_id():
    records = [
        _rec("b", "ws-1", day=5),
        _rec("a", "ws-1", day=5),
        _rec("c", "ws-1", day=9),
        _rec("d", "ws-1", day=1),
    ]
    assert _ids(select_prior_outcomes(records, {"ws-1"})) == ["c", "a", "b", "d"]


def test_recency_uses_valid_at_not_as_of():
    records = [
        _rec("late-recorded", "x", day=1, as_of_day=50),
        _rec("late-valid", "y", day=10, as_of_day=10),
    ]
    assert _ids(select_prior_outcomes(records, set())) == ["late-valid", "late-recorded"]


def test_bounded_by_max_items_and_reports_omitted_count():
    records = [_rec(f"r{i:02d}", "ws-1", day=i) for i in range(15)]
    selection = select_prior_outcomes(records, {"ws-1"})
    assert len(selection.selected) == DEFAULT_MAX_ITEMS == 10
    assert selection.candidate_count == 15
    assert selection.omitted_count == 5
    assert _ids(selection)[0] == "r14"

    small = select_prior_outcomes(records, {"ws-1"}, max_items=2)
    assert _ids(small) == ["r14", "r13"]
    assert small.omitted_count == 13


def test_matched_entity_ids_are_sorted_and_specific_to_reason():
    records = [
        _rec("same", "ws-1", day=1, linked=("ws-2",)),
        _rec("linked", "dep", day=2, linked=("ws-2", "zz", "ws-1")),
        _rec("none", "other", day=3),
    ]
    by_id = {
        item.record.record_id: item
        for item in select_prior_outcomes(records, {"ws-2", "ws-1"}).selected
    }
    assert by_id["same"].matched_entity_ids == ("ws-1",)
    assert by_id["linked"].matched_entity_ids == ("ws-1", "ws-2")
    assert by_id["none"].matched_entity_ids == ()


def test_input_order_does_not_change_selection():
    records = [
        _rec("a", "ws-1", day=3),
        _rec("b", "dep", day=3, linked=("ws-1",)),
        _rec("c", "other", day=3),
        _rec("d", "ws-1", day=3),
    ]
    forward = select_prior_outcomes(records, ["ws-1"], max_items=3)
    backward = select_prior_outcomes(list(reversed(records)), ["ws-1"], max_items=3)
    assert forward == backward
    assert _ids(forward) == ["a", "d", "b"]


def test_knowledge_cutoff_excludes_records_recorded_after_as_of():
    records = [
        _rec("known", "ws-1", day=1, as_of_day=2),
        _rec("future", "ws-1", day=1, as_of_day=20),
    ]
    selection = select_prior_outcomes(
        records, {"ws-1"}, as_of=_T0 + timedelta(days=10)
    )
    assert _ids(selection) == ["known"]
    assert selection.candidate_count == 1
    assert selection.omitted_count == 0


def test_empty_inputs_return_empty_selection():
    selection = select_prior_outcomes([], {"ws-1"})
    assert selection.selected == ()
    assert selection.candidate_count == 0
    assert selection.omitted_count == 0


@pytest.mark.parametrize("max_items", [0, -1])
def test_rejects_non_positive_max_items(max_items):
    with pytest.raises(ValueError, match="max_items"):
        select_prior_outcomes([_rec("a", "ws-1")], {"ws-1"}, max_items=max_items)


def test_rejects_duplicate_record_ids():
    with pytest.raises(ValueError, match="duplicate record_id"):
        select_prior_outcomes([_rec("a", "ws-1"), _rec("a", "ws-2")], set())


def test_rejects_naive_datetimes_and_blank_identity():
    with pytest.raises(ValueError, match="timezone-aware"):
        PriorOutcomeRecord(
            record_id="a",
            entity_id="ws-1",
            kind="risk",
            as_of=datetime(2026, 1, 1),
            valid_at=_T0,
            outcome_summary="s",
            evidence_refs=(),
        )
    with pytest.raises(ValueError, match="entity_id"):
        _rec("a", " ")
    with pytest.raises(ValueError, match="timezone-aware"):
        select_prior_outcomes([], set(), as_of=datetime(2026, 1, 1))
