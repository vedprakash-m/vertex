"""ADF-W2.9 P5: tests for the blind A/B comparison recording sidecar."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.blind_ab_comparison import (
    blind_ab_comparison_path,
    read_comparisons,
    recommend_swap_decision,
    record_comparison,
    summarize_comparisons,
)


def test_record_and_read_comparison_round_trips(tmp_path: Path) -> None:
    record_comparison(
        program_id="xpf",
        surface="decision_brief",
        item_id="risks",
        option_a_text="Baseline output.",
        option_b_text="Candidate output.",
        a_is_candidate=False,
        choice="b",
        programs_root=tmp_path,
        recorded_at=datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc),
        notes="candidate was clearer",
    )

    path = blind_ab_comparison_path("xpf", programs_root=tmp_path)
    assert path.exists()

    records = read_comparisons("xpf", programs_root=tmp_path)
    assert len(records) == 1
    record = records[0]
    assert record.program_id == "xpf"
    assert record.surface == "decision_brief"
    assert record.item_id == "risks"
    assert record.option_a_text == "Baseline output."
    assert record.option_b_text == "Candidate output."
    assert record.a_is_candidate is False
    assert record.choice == "b"
    assert record.notes == "candidate was clearer"
    assert record.recorded_at == datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)


def test_read_comparisons_returns_empty_tuple_when_no_file_exists(tmp_path: Path) -> None:
    assert read_comparisons("xpf", programs_root=tmp_path) == ()


def test_read_comparisons_filters_by_surface(tmp_path: Path) -> None:
    record_comparison(
        program_id="xpf",
        surface="decision_brief",
        item_id="a1",
        option_a_text="A",
        option_b_text="B",
        a_is_candidate=True,
        choice="a",
        programs_root=tmp_path,
    )
    record_comparison(
        program_id="xpf",
        surface="exec_summary",
        item_id="a2",
        option_a_text="A",
        option_b_text="B",
        a_is_candidate=True,
        choice="b",
        programs_root=tmp_path,
    )

    decision_brief_only = read_comparisons("xpf", surface="decision_brief", programs_root=tmp_path)
    assert len(decision_brief_only) == 1
    assert decision_brief_only[0].item_id == "a1"

    all_records = read_comparisons("xpf", programs_root=tmp_path)
    assert len(all_records) == 2


def test_summarize_comparisons_counts_candidate_and_baseline_wins_correctly(tmp_path: Path) -> None:
    for item_id, a_is_candidate, choice in [
        ("i1", True, "a"),  # candidate win (a is candidate, a chosen)
        ("i2", False, "b"),  # candidate win (b is candidate, b chosen)
        ("i3", True, "b"),  # baseline win (a is candidate, b chosen -> baseline)
        ("i4", False, "a"),  # baseline win (b is candidate, a chosen -> baseline)
        ("i5", True, "tie"),
        ("i6", False, "neither"),
    ]:
        record_comparison(
            program_id="xpf",
            surface="decision_brief",
            item_id=item_id,
            option_a_text="A",
            option_b_text="B",
            a_is_candidate=a_is_candidate,
            choice=choice,
            programs_root=tmp_path,
        )

    records = read_comparisons("xpf", programs_root=tmp_path)
    summary = summarize_comparisons(records, surface="decision_brief")

    assert summary.surface == "decision_brief"
    assert summary.total == 6
    assert summary.candidate_wins == 2
    assert summary.baseline_wins == 2
    assert summary.ties == 1
    assert summary.neither == 1
    assert summary.candidate_win_rate == 0.5


def test_summarize_comparisons_win_rate_is_none_without_decisive_comparisons() -> None:
    from src.core.blind_ab_comparison import ComparisonRecord

    records = (
        ComparisonRecord(
            program_id="xpf",
            surface="decision_brief",
            item_id="i1",
            option_a_text="A",
            option_b_text="B",
            a_is_candidate=True,
            choice="tie",
            recorded_at=datetime.now(timezone.utc),
        ),
    )
    summary = summarize_comparisons(records, surface="decision_brief")
    assert summary.total == 1
    assert summary.candidate_win_rate is None
    assert summary.candidate_win_rate_lower_bound is None


def test_record_comparison_round_trips_critical_error_flag(tmp_path: Path) -> None:
    record_comparison(
        program_id="xpf",
        surface="decision_brief",
        item_id="risks",
        option_a_text="A",
        option_b_text="B",
        a_is_candidate=True,
        choice="a",
        programs_root=tmp_path,
        critical_error=True,
    )
    records = read_comparisons("xpf", programs_root=tmp_path)
    assert records[0].critical_error is True


def test_read_comparisons_defaults_critical_error_false_for_legacy_records(tmp_path: Path) -> None:
    # Records written before this field existed have no "critical_error" key
    # at all -- confirm they load as False, not crash or become None.
    path = blind_ab_comparison_path("xpf", programs_root=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"program_id": "xpf", "surface": "decision_brief", "item_id": "legacy", '
        '"option_a_text": "A", "option_b_text": "B", "a_is_candidate": true, '
        '"choice": "a", "recorded_at": "2026-06-01T00:00:00+00:00", "notes": null}\n',
        encoding="utf-8",
    )
    records = read_comparisons("xpf", programs_root=tmp_path)
    assert records[0].critical_error is False


def test_summarize_comparisons_counts_critical_errors_and_lower_bound(tmp_path: Path) -> None:
    for item_id, a_is_candidate, choice in [("i1", True, "a"), ("i2", True, "a"), ("i3", True, "b")]:
        record_comparison(
            program_id="xpf", surface="s", item_id=item_id, option_a_text="A", option_b_text="B",
            a_is_candidate=a_is_candidate, choice=choice, programs_root=tmp_path,
            critical_error=(item_id == "i2"),
        )
    records = read_comparisons("xpf", programs_root=tmp_path)
    summary = summarize_comparisons(records, surface="s")
    assert summary.critical_errors == 1
    assert summary.candidate_win_rate == pytest.approx(2 / 3)
    # Wilson lower bound must be strictly below the raw point estimate for a small sample.
    assert summary.candidate_win_rate_lower_bound is not None
    assert summary.candidate_win_rate_lower_bound < summary.candidate_win_rate


def test_recommend_swap_decision_holds_on_any_critical_error() -> None:
    from src.core.blind_ab_comparison import ComparisonRecord

    records = tuple(
        ComparisonRecord(
            program_id="xpf", surface="s", item_id=f"i{i}", option_a_text="A", option_b_text="B",
            a_is_candidate=True, choice="a", recorded_at=datetime.now(timezone.utc),
            critical_error=(i == 0),
        )
        for i in range(40)
    )
    summary = summarize_comparisons(records, surface="s")
    assert recommend_swap_decision(summary) == "hold"


def test_recommend_swap_decision_insufficient_data_below_min_observations() -> None:
    from src.core.blind_ab_comparison import ComparisonRecord

    records = tuple(
        ComparisonRecord(
            program_id="xpf", surface="s", item_id=f"i{i}", option_a_text="A", option_b_text="B",
            a_is_candidate=True, choice="a", recorded_at=datetime.now(timezone.utc),
        )
        for i in range(5)
    )
    summary = summarize_comparisons(records, surface="s")
    assert recommend_swap_decision(summary, min_observations=30) == "insufficient_data"


def test_recommend_swap_decision_swaps_on_strong_clean_signal() -> None:
    from src.core.blind_ab_comparison import ComparisonRecord

    # 38/40 candidate wins, no critical errors -- comfortably clears both
    # the min-observations and lower-bound-threshold defaults.
    records = tuple(
        ComparisonRecord(
            program_id="xpf", surface="s", item_id=f"i{i}", option_a_text="A", option_b_text="B",
            a_is_candidate=True, choice=("a" if i < 38 else "b"), recorded_at=datetime.now(timezone.utc),
        )
        for i in range(40)
    )
    summary = summarize_comparisons(records, surface="s")
    assert recommend_swap_decision(summary) == "swap"
