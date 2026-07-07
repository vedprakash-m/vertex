"""
Unit tests for src/core/context_gap_store.py — §21 context gap feedback.

Zone A only. Tests use tmp_path programs root (no live filesystem state).
Covers: append_context_gap (ADR-001 atomic write), load_context_gaps,
        rank_context_gaps (deduplication, ordering), and round-trip serialization.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.context_gap_store import (
    ContextGapRecord,
    append_context_gap,
    load_context_gaps,
    rank_context_gaps,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _programs_root(tmp_path: Path) -> Path:
    root = tmp_path / "programs"
    root.mkdir(parents=True, exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# append_context_gap
# ---------------------------------------------------------------------------

def test_append_creates_feedback_dir(tmp_path: Path) -> None:
    pr = _programs_root(tmp_path)
    append_context_gap(
        feature="test_feature",
        program="acme",
        field="deep_context.why",
        message="Missing why field",
        programs_root=pr,
    )
    feedback_dir = pr / "acme" / "_feedback"
    assert feedback_dir.exists(), "Expected _feedback/ directory to be created"


def test_append_creates_context_gaps_jsonl(tmp_path: Path) -> None:
    pr = _programs_root(tmp_path)
    append_context_gap(
        feature="test_feature",
        program="acme",
        field="deep_context.why",
        message="test msg",
        programs_root=pr,
    )
    gaps_file = pr / "acme" / "_feedback" / "context_gaps.jsonl"
    assert gaps_file.exists()
    assert gaps_file.stat().st_size > 0


def test_append_returns_path(tmp_path: Path) -> None:
    pr = _programs_root(tmp_path)
    path = append_context_gap(
        feature="test_feature",
        program="acme",
        field="kpis.validated",
        message="test",
        programs_root=pr,
    )
    assert path.exists()
    assert path.suffix == ".jsonl"


def test_append_multiple_records(tmp_path: Path) -> None:
    pr = _programs_root(tmp_path)
    for i in range(5):
        append_context_gap(
            feature="gather",
            program="acme",
            lane=f"ws{i}",
            field="deep_context.why",
            message=f"gap {i}",
            programs_root=pr,
        )
    gaps = load_context_gaps("acme", programs_root=pr)
    assert len(gaps) == 5


# ---------------------------------------------------------------------------
# load_context_gaps
# ---------------------------------------------------------------------------

def test_load_returns_empty_list_for_missing_program(tmp_path: Path) -> None:
    pr = _programs_root(tmp_path)
    gaps = load_context_gaps("nonexistent_program", programs_root=pr)
    assert gaps == []


def test_load_round_trip_fields(tmp_path: Path) -> None:
    pr = _programs_root(tmp_path)
    append_context_gap(
        feature="doctor --context",
        program="acme",
        lane="ws.alpha",
        field="roles.primary_owner.email",
        severity="quality_degraded",
        message="No email for primary_owner",
        impact_estimate="high",
        programs_root=pr,
    )
    gaps = load_context_gaps("acme", programs_root=pr)
    assert len(gaps) == 1
    g = gaps[0]
    assert g.feature == "doctor --context"
    assert g.lane == "ws.alpha"
    assert g.field == "roles.primary_owner.email"
    assert g.severity == "quality_degraded"
    assert g.impact_estimate == "high"
    assert isinstance(g.ts, datetime)


def test_load_preserves_order(tmp_path: Path) -> None:
    pr = _programs_root(tmp_path)
    for i in range(3):
        append_context_gap(
            feature="gather",
            program="acme",
            field=f"field_{i}",
            message=f"msg {i}",
            programs_root=pr,
        )
    gaps = load_context_gaps("acme", programs_root=pr)
    assert [g.field for g in gaps] == ["field_0", "field_1", "field_2"]


def test_load_rejects_naive_ts(tmp_path: Path) -> None:
    pr = _programs_root(tmp_path)
    gaps_file = pr / "acme" / "_feedback" / "context_gaps.jsonl"
    gaps_file.parent.mkdir(parents=True, exist_ok=True)
    gaps_file.write_text(
        '{"ts":"2026-05-10T08:00:00","feature":"gather","program":"acme","lane":"ws1","field":"deep_context.why","severity":"quality_degraded","message":"missing why","impact_estimate":"medium"}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="ts must include timezone information"):
        load_context_gaps("acme", programs_root=pr)


# ---------------------------------------------------------------------------
# rank_context_gaps
# ---------------------------------------------------------------------------

def test_rank_deduplicates_by_feature_field_lane(tmp_path: Path) -> None:
    pr = _programs_root(tmp_path)
    # Append the same gap three times (deduplicate=False to test ranking aggregation)
    for _ in range(3):
        append_context_gap(
            feature="gather",
            program="acme",
            lane="ws1",
            field="deep_context.why",
            message="repeated gap",
            programs_root=pr,
            deduplicate=False,
        )
    gaps = load_context_gaps("acme", programs_root=pr)
    ranked = rank_context_gaps(gaps)
    # Should deduplicate to 1 unique (feature, field, lane) combination
    assert len(ranked) == 1
    assert ranked[0].count == 3


def test_rank_orders_by_impact_high_first(tmp_path: Path) -> None:
    pr = _programs_root(tmp_path)
    append_context_gap(
        feature="nudge", program="acme", field="f1",
        message="low impact", impact_estimate="low", programs_root=pr,
    )
    append_context_gap(
        feature="gather", program="acme", field="f2",
        message="high impact", impact_estimate="high", programs_root=pr,
    )
    gaps = load_context_gaps("acme", programs_root=pr)
    ranked = rank_context_gaps(gaps)
    assert ranked[0].impact_estimate == "high"


def test_rank_empty_returns_empty(tmp_path: Path) -> None:
    result = rank_context_gaps([])
    assert result == []


# ---------------------------------------------------------------------------
# ContextGapRecord serialization
# ---------------------------------------------------------------------------

def test_context_gap_record_to_from_json_round_trip() -> None:
    now = datetime.now(timezone.utc)
    record = ContextGapRecord(
        ts=now,
        feature="gather",
        program="acme",
        lane="ws1",
        field="kpis.validated",
        severity="quality_degraded",
        message="KPI query skipped",
        impact_estimate="medium",
    )
    d = record.to_json()
    restored = ContextGapRecord.from_json(d)
    assert restored.feature == record.feature
    assert restored.program == record.program
    assert restored.lane == record.lane
    assert restored.field == record.field
    assert restored.severity == record.severity
