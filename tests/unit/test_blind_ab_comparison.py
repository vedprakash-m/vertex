"""ADF-W2.9 P5: tests for the blind A/B comparison recording sidecar."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

import src.core.blind_ab_comparison as blind_ab_comparison
import src.core.jsonl_utils as jsonl_utils
from src.core.blind_ab_comparison import (
    ComparisonChoice,
    blind_ab_comparison_path,
    read_comparisons,
    recommend_swap_decision,
    record_comparison,
    summarize_comparisons,
)
from src.core.exceptions import StateError


def test_record_and_read_comparison_round_trips(tmp_path: Path) -> None:
    record_comparison(
        program_id="samplealpha",
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

    path = blind_ab_comparison_path("samplealpha", programs_root=tmp_path)
    assert path.exists()

    records = read_comparisons("samplealpha", programs_root=tmp_path)
    assert len(records) == 1
    record = records[0]
    assert record.program_id == "samplealpha"
    assert record.surface == "decision_brief"
    assert record.item_id == "risks"
    assert record.option_a_text == "Baseline output."
    assert record.option_b_text == "Candidate output."
    assert record.a_is_candidate is False
    assert record.choice == "b"
    assert record.notes == "candidate was clearer"
    assert record.recorded_at == datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)


def test_record_comparison_retains_newest_same_second_journals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz == timezone.utc
            return cls(2026, 1, 1, tzinfo=timezone.utc)

    monkeypatch.setattr(jsonl_utils, "datetime", FixedClock)
    monkeypatch.setattr(blind_ab_comparison, "_MAX_BYTES", 1)
    for item in range(8):
        record_comparison(
            program_id="samplealpha", surface="s", item_id=str(item),
            option_a_text="A", option_b_text="B", a_is_candidate=True, choice="a",
            programs_root=tmp_path,
        )

    path = blind_ab_comparison_path("samplealpha", programs_root=tmp_path)
    rotated = jsonl_utils.list_rotated_jsonl_paths(path.parent / "rotated", stem=path.stem)
    assert len(rotated) == jsonl_utils.DEFAULT_MAX_ROTATED_FILES
    assert [json.loads(p.read_text(encoding="utf-8"))["item_id"] for p in (*rotated, path)] == [
        str(item) for item in range(2, 8)
    ]
    assert jsonl_utils.jsonl_checksum_matches(path, path.with_suffix(".sha256")) is True


@pytest.mark.parametrize("existing", [False, True], ids=["absent", "retained"])
@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("choice", None),
        ("choice", ""),
        ("choice", "A"),
        ("choice", "private-invalid-choice"),
        ("choice", True),
        ("choice", 0),
        ("choice", []),
        ("choice", {}),
        ("a_is_candidate", None),
        ("a_is_candidate", 0),
        ("a_is_candidate", 1),
        ("a_is_candidate", "private-invalid-candidate"),
        ("a_is_candidate", []),
        ("a_is_candidate", {}),
        ("critical_error", None),
        ("critical_error", 0),
        ("critical_error", 1),
        ("critical_error", "private-invalid-critical"),
        ("critical_error", []),
        ("critical_error", {}),
    ],
)
def test_record_comparison_rejects_invalid_judgments_without_writing(
    tmp_path: Path, existing: bool, field: str, invalid: object,
) -> None:
    path = blind_ab_comparison_path("samplealpha", programs_root=tmp_path)
    if existing:
        record_comparison(
            program_id="samplealpha", surface="s", item_id="previous",
            option_a_text="A", option_b_text="B", a_is_candidate=True, choice="a",
            programs_root=tmp_path, critical_error=False,
        )
        original = path.read_bytes()

    with pytest.raises(ValueError) as exc_info:
        record_comparison(
            program_id="samplealpha", surface="s", item_id="new",
            option_a_text="A", option_b_text="B", programs_root=tmp_path,
            choice=cast(ComparisonChoice, invalid if field == "choice" else "a"),
            a_is_candidate=cast(bool, invalid if field == "a_is_candidate" else True),
            critical_error=cast(bool, invalid if field == "critical_error" else False),
        )
    assert str(exc_info.value) == "Invalid blind-comparison judgment."
    if existing:
        assert path.read_bytes() == original
    else:
        assert not path.parent.exists()


@pytest.mark.parametrize("choice", ["a", "b", "tie", "neither"])
@pytest.mark.parametrize("a_is_candidate", [False, True])
@pytest.mark.parametrize("critical_error", [False, True])
def test_record_comparison_preserves_valid_judgments(
    tmp_path: Path, choice: ComparisonChoice, a_is_candidate: bool, critical_error: bool,
) -> None:
    record_comparison(
        program_id="samplealpha", surface="s", item_id="valid",
        option_a_text="A", option_b_text="B", a_is_candidate=a_is_candidate,
        choice=choice, programs_root=tmp_path, critical_error=critical_error,
    )
    record, = read_comparisons("samplealpha", programs_root=tmp_path)
    assert (record.choice, record.a_is_candidate, record.critical_error) == (
        choice, a_is_candidate, critical_error,
    )
    summary = summarize_comparisons((record,), surface="s")
    assert summary.critical_errors == int(critical_error)
    assert summary.ties == int(choice == "tie")
    assert summary.neither == int(choice == "neither")
    if choice in ("a", "b"):
        assert summary.candidate_wins == int((choice == "a") == a_is_candidate)
        assert summary.baseline_wins == 1 - summary.candidate_wins
    else:
        assert summary.candidate_wins == summary.baseline_wins == 0


def test_read_comparisons_returns_empty_tuple_when_no_file_exists(tmp_path: Path) -> None:
    assert read_comparisons("samplealpha", programs_root=tmp_path) == ()


@pytest.mark.parametrize("bad_row", [b'{"private-marker":\n', b'["private-marker"]\n', b"\xff\n"])
def test_read_comparisons_refuses_invalid_journal_without_repair(
    tmp_path: Path, bad_row: bytes,
) -> None:
    record_comparison(
        program_id="samplealpha", surface="s", item_id="previous",
        option_a_text="A", option_b_text="B", a_is_candidate=True, choice="a",
        programs_root=tmp_path,
    )
    path = blind_ab_comparison_path("samplealpha", programs_root=tmp_path)
    path.write_bytes(path.read_bytes() + bad_row)
    original = path.read_bytes()
    checksum = path.with_suffix(".sha256").read_bytes()

    with pytest.raises(StateError) as error:
        read_comparisons("samplealpha", surface="s", programs_root=tmp_path)
    assert "private-marker" not in str(error.value)
    assert path.read_bytes() == original
    assert path.with_suffix(".sha256").read_bytes() == checksum
    assert not (path.parent / "quarantine").exists()


@pytest.mark.parametrize("foreign_identity", [None, "private-other-program"])
@pytest.mark.parametrize("surface", ["s", "another-surface"])
def test_read_comparisons_refuses_mismatched_program_before_filtering(
    tmp_path: Path, foreign_identity: str | None, surface: str,
) -> None:
    record_comparison(
        program_id="samplealpha", surface="s", item_id="previous",
        option_a_text="A", option_b_text="B", a_is_candidate=True, choice="a",
        programs_root=tmp_path,
    )
    path = blind_ab_comparison_path("samplealpha", programs_root=tmp_path)
    row = json.loads(path.read_text(encoding="utf-8"))
    row["surface"] = surface
    if foreign_identity is None:
        del row["program_id"]
    else:
        row["program_id"] = foreign_identity
    path.write_bytes(path.read_bytes() + (json.dumps(row) + "\n").encode())
    original = path.read_bytes()
    checksum = path.with_suffix(".sha256").read_bytes()

    with pytest.raises(StateError) as error:
        read_comparisons("samplealpha", surface="s", programs_root=tmp_path)
    assert "private-other-program" not in str(error.value)
    assert path.read_bytes() == original
    assert path.with_suffix(".sha256").read_bytes() == checksum
    assert not (path.parent / "quarantine").exists()


@pytest.mark.parametrize("invalid_field", ["recorded_at", "choice"])
def test_read_comparisons_refuses_invalid_retained_record_without_leaking_value(
    tmp_path: Path, invalid_field: str,
) -> None:
    record_comparison(
        program_id="samplealpha", surface="s", item_id="previous",
        option_a_text="A", option_b_text="B", a_is_candidate=True, choice="a",
        programs_root=tmp_path,
    )
    path = blind_ab_comparison_path("samplealpha", programs_root=tmp_path)
    row = json.loads(path.read_text(encoding="utf-8"))
    if invalid_field == "recorded_at":
        row["recorded_at"] = "private-invalid-timestamp"
    else:
        del row["choice"]
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    original = path.read_bytes()
    checksum = path.with_suffix(".sha256").read_bytes()

    with pytest.raises(StateError) as error:
        read_comparisons("samplealpha", surface="s", programs_root=tmp_path)
    assert "private-invalid-timestamp" not in str(error.value)
    assert path.read_bytes() == original
    assert path.with_suffix(".sha256").read_bytes() == checksum
    assert not (path.parent / "quarantine").exists()


def test_read_comparisons_refuses_io_failure_without_leaking_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    record_comparison(
        program_id="samplealpha", surface="s", item_id="previous",
        option_a_text="A", option_b_text="B", a_is_candidate=True, choice="a",
        programs_root=tmp_path,
    )
    path = blind_ab_comparison_path("samplealpha", programs_root=tmp_path)
    original = path.read_bytes()
    checksum = path.with_suffix(".sha256").read_bytes()
    monkeypatch.setattr(
        "src.core.blind_ab_comparison.read_jsonl_records",
        Mock(side_effect=PermissionError("private-path")),
    )

    with pytest.raises(StateError) as error:
        read_comparisons("samplealpha", programs_root=tmp_path)
    assert "private-path" not in str(error.value)
    assert error.value.__suppress_context__
    assert path.read_bytes() == original
    assert path.with_suffix(".sha256").read_bytes() == checksum
    assert not (path.parent / "quarantine").exists()


def test_read_comparisons_preserves_empty_journal(tmp_path: Path) -> None:
    path = blind_ab_comparison_path("samplealpha", programs_root=tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    assert read_comparisons("samplealpha", programs_root=tmp_path) == ()
    assert path.read_bytes() == b""


def test_read_comparisons_filters_by_surface(tmp_path: Path) -> None:
    record_comparison(
        program_id="samplealpha",
        surface="decision_brief",
        item_id="a1",
        option_a_text="A",
        option_b_text="B",
        a_is_candidate=True,
        choice="a",
        programs_root=tmp_path,
    )
    record_comparison(
        program_id="samplealpha",
        surface="exec_summary",
        item_id="a2",
        option_a_text="A",
        option_b_text="B",
        a_is_candidate=True,
        choice="b",
        programs_root=tmp_path,
    )

    decision_brief_only = read_comparisons("samplealpha", surface="decision_brief", programs_root=tmp_path)
    assert len(decision_brief_only) == 1
    assert decision_brief_only[0].item_id == "a1"

    all_records = read_comparisons("samplealpha", programs_root=tmp_path)
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
            program_id="samplealpha",
            surface="decision_brief",
            item_id=item_id,
            option_a_text="A",
            option_b_text="B",
            a_is_candidate=a_is_candidate,
            choice=choice,
            programs_root=tmp_path,
        )

    records = read_comparisons("samplealpha", programs_root=tmp_path)
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
            program_id="samplealpha",
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
        program_id="samplealpha",
        surface="decision_brief",
        item_id="risks",
        option_a_text="A",
        option_b_text="B",
        a_is_candidate=True,
        choice="a",
        programs_root=tmp_path,
        critical_error=True,
    )
    records = read_comparisons("samplealpha", programs_root=tmp_path)
    assert records[0].critical_error is True


def test_read_comparisons_defaults_critical_error_false_for_legacy_records(tmp_path: Path) -> None:
    # Records written before this field existed have no "critical_error" key
    # at all -- confirm they load as False, not crash or become None.
    path = blind_ab_comparison_path("samplealpha", programs_root=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"program_id": "samplealpha", "surface": "decision_brief", "item_id": "legacy", '
        '"option_a_text": "A", "option_b_text": "B", "a_is_candidate": true, '
        '"choice": "a", "recorded_at": "2026-06-01T00:00:00+00:00", "notes": null}\n',
        encoding="utf-8",
    )
    records = read_comparisons("samplealpha", programs_root=tmp_path)
    assert records[0].critical_error is False


def test_summarize_comparisons_counts_critical_errors_and_lower_bound(tmp_path: Path) -> None:
    for item_id, a_is_candidate, choice in [("i1", True, "a"), ("i2", True, "a"), ("i3", True, "b")]:
        record_comparison(
            program_id="samplealpha", surface="s", item_id=item_id, option_a_text="A", option_b_text="B",
            a_is_candidate=a_is_candidate, choice=choice, programs_root=tmp_path,
            critical_error=(item_id == "i2"),
        )
    records = read_comparisons("samplealpha", programs_root=tmp_path)
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
            program_id="samplealpha", surface="s", item_id=f"i{i}", option_a_text="A", option_b_text="B",
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
            program_id="samplealpha", surface="s", item_id=f"i{i}", option_a_text="A", option_b_text="B",
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
            program_id="samplealpha", surface="s", item_id=f"i{i}", option_a_text="A", option_b_text="B",
            a_is_candidate=True, choice=("a" if i < 38 else "b"), recorded_at=datetime.now(timezone.utc),
        )
        for i in range(40)
    )
    summary = summarize_comparisons(records, surface="s")
    assert recommend_swap_decision(summary) == "swap"
