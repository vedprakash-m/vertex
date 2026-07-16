"""ADF-W5.14: src/core/adoption_telemetry.py."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core.adoption_telemetry import (
    GoldenWorkflow,
    NonAdoptionReason,
    compute_adoption_rate,
    pseudonymize_operator,
    read_adoption_events,
    record_adoption,
    record_non_adoption,
)

_NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)  # ISO week 2026-W29


def test_record_adoption_writes_an_event_with_correct_cadence_period(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    record_adoption("xpf", GoldenWorkflow.COCKPIT_SHOW, now=_NOW, programs_root=programs_root)
    events = read_adoption_events("xpf", programs_root=programs_root)
    assert len(events) == 1
    assert events[0].adopted is True
    assert events[0].cadence_period == "2026-W29"
    assert events[0].workflow == GoldenWorkflow.COCKPIT_SHOW


def test_operator_ref_is_pseudonymized_never_stored_raw(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    record_adoption(
        "xpf", GoldenWorkflow.COCKPIT_SHOW, operator_ref="alice@example.com", now=_NOW, programs_root=programs_root
    )
    events = read_adoption_events("xpf", programs_root=programs_root)
    assert events[0].operator_ref is not None
    assert "alice" not in events[0].operator_ref
    assert events[0].operator_ref == pseudonymize_operator("alice@example.com")


def test_pseudonymize_operator_is_deterministic_and_case_insensitive() -> None:
    assert pseudonymize_operator("Alice") == pseudonymize_operator("alice")
    assert pseudonymize_operator("alice") != pseudonymize_operator("bob")
    assert pseudonymize_operator("alice").startswith("sha256:")


def test_record_non_adoption_captures_a_closed_vocabulary_reason(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    record_non_adoption(
        "xpf", GoldenWorkflow.WEEKLY_REPORT, NonAdoptionReason.TOOL_ISSUE, now=_NOW, programs_root=programs_root
    )
    events = read_adoption_events("xpf", programs_root=programs_root)
    assert len(events) == 1
    assert events[0].adopted is False
    assert events[0].reason == NonAdoptionReason.TOOL_ISSUE


def test_read_adoption_events_empty_when_no_file(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    assert read_adoption_events("xpf", programs_root=programs_root) == ()


def test_compute_adoption_rate_with_no_events_returns_none_rate(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    summary = compute_adoption_rate("xpf", programs_root=programs_root, now=_NOW)
    assert summary.adoption_rate is None
    assert summary.adopted_count == 0
    assert summary.non_adopted_count == 0


def test_compute_adoption_rate_mixes_adopted_and_non_adopted(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    record_adoption("xpf", GoldenWorkflow.COCKPIT_SHOW, now=_NOW, programs_root=programs_root)
    record_adoption("xpf", GoldenWorkflow.COCKPIT_SHOW, now=_NOW, programs_root=programs_root)
    record_non_adoption(
        "xpf", GoldenWorkflow.COCKPIT_SHOW, NonAdoptionReason.UNAWARE, now=_NOW, programs_root=programs_root
    )
    summary = compute_adoption_rate("xpf", workflow=GoldenWorkflow.COCKPIT_SHOW, programs_root=programs_root, now=_NOW)
    assert summary.adopted_count == 2
    assert summary.non_adopted_count == 1
    assert summary.adoption_rate == 2 / 3
    assert summary.reason_breakdown == {"unaware": 1}


def test_compute_adoption_rate_filters_by_workflow(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    record_adoption("xpf", GoldenWorkflow.COCKPIT_SHOW, now=_NOW, programs_root=programs_root)
    record_adoption("xpf", GoldenWorkflow.WEEKLY_REPORT, now=_NOW, programs_root=programs_root)
    summary = compute_adoption_rate("xpf", workflow=GoldenWorkflow.WEEKLY_REPORT, programs_root=programs_root, now=_NOW)
    assert summary.adopted_count == 1


def test_compute_adoption_rate_respects_since_weeks_window(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    record_adoption("xpf", GoldenWorkflow.COCKPIT_SHOW, now=old, programs_root=programs_root)
    summary = compute_adoption_rate("xpf", programs_root=programs_root, since_weeks=4, now=_NOW)
    assert summary.adopted_count == 0


def test_events_round_trip_through_json(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    record_adoption(
        "xpf", GoldenWorkflow.MEETING_TO_ACTION, operator_ref="bob", now=_NOW, programs_root=programs_root
    )
    record_non_adoption(
        "xpf", GoldenWorkflow.RISK_DEPENDENCY_REVIEW, NonAdoptionReason.OTHER, now=_NOW, programs_root=programs_root
    )
    events = read_adoption_events("xpf", programs_root=programs_root)
    assert len(events) == 2
    assert {e.workflow for e in events} == {GoldenWorkflow.MEETING_TO_ACTION, GoldenWorkflow.RISK_DEPENDENCY_REVIEW}
