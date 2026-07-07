"""WS-5b: AI telemetry sidecar contract tests.

Spec: specs/prod-vis.md §WS-5b acceptance:
  "forced AI failure writes a typed telemetry record; reviewer pane shows
   per-feature cost; budget-exceeded QG fires; SQLite authoritative read."

Tests:
  1. ai_telemetry_path returns the canonical sidecar location
  2. append_ai_telemetry round-trips a record (read back matches)
  3. read_ai_telemetry returns empty tuple when file absent
  4. build_feature_cost_summary aggregates per-feature stats correctly
  5. AiTelemetryStatus.from_exception classifies common exceptions
  6. Registry: ai_telemetry registered with correct owner_module + symbols
  7. QG-WS5B passes when no budget configured (vacuous n/a)
  8. QG-WS5B passes when spend is under budget
  9. QG-WS5B fails (forceable) when spend exceeds budget
  10. QG-WS5B passes when program_id is None (vacuous n/a)
  11. QG-WS5B is in evaluate_phase_1b_gates output (wired)
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.ai_telemetry import (
    AiTelemetryRecord,
    AiTelemetryStatus,
    ai_telemetry_path,
    append_ai_telemetry,
    build_feature_cost_summary,
    read_ai_telemetry,
)
from src.core.quality_gates import evaluate_phase_1b_gates
from src.core.quality_gates.ai_budget import evaluate_ai_budget_gate
from src.core.state_reader_registry import STATE_READER_REGISTRY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_record(
    program_id: str,
    *,
    feature: str = "blurb_generator",
    status: str = AiTelemetryStatus.OK,
    cost_usd: float | None = 0.01,
    latency_ms: float | None = 250.0,
    tokens_in: int | None = 100,
    tokens_out: int | None = 50,
    ts: datetime | None = None,
) -> AiTelemetryRecord:
    return AiTelemetryRecord(
        ts=ts or datetime.now(timezone.utc),
        feature=feature,
        deployment_id="gpt-4o-test",
        status=status,
        program_id=program_id,
        latency_ms=latency_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
    )


# ---------------------------------------------------------------------------
# 1. Path helper
# ---------------------------------------------------------------------------
def test_ai_telemetry_path_canonical():
    with tempfile.TemporaryDirectory() as tmpdir:
        programs_root = Path(tmpdir)
        path = ai_telemetry_path("my_prog", programs_root=programs_root)
    assert path.name == "ai_telemetry.jsonl"
    assert "_state" in path.parts


# ---------------------------------------------------------------------------
# 2. Round-trip
# ---------------------------------------------------------------------------
def test_append_and_read_roundtrip():
    with tempfile.TemporaryDirectory() as tmpdir:
        programs_root = Path(tmpdir)
        record = _make_record("prog1", cost_usd=0.042, latency_ms=312.5)
        append_ai_telemetry(record, programs_root=programs_root)

        records = read_ai_telemetry("prog1", programs_root=programs_root)

    assert len(records) == 1
    r = records[0]
    assert r.feature == "blurb_generator"
    assert r.status == AiTelemetryStatus.OK
    assert r.cost_usd == pytest.approx(0.042, rel=1e-6)
    assert r.latency_ms == pytest.approx(312.5, rel=1e-6)
    assert r.program_id == "prog1"


# ---------------------------------------------------------------------------
# 3. Read when absent
# ---------------------------------------------------------------------------
def test_read_ai_telemetry_absent_returns_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        records = read_ai_telemetry("no_such_prog", programs_root=Path(tmpdir))
    assert records == ()


# ---------------------------------------------------------------------------
# 4. Feature cost summary
# ---------------------------------------------------------------------------
def test_build_feature_cost_summary():
    with tempfile.TemporaryDirectory() as tmpdir:
        programs_root = Path(tmpdir)
        recent = datetime.now(timezone.utc)
        append_ai_telemetry(_make_record("p", feature="claim_extractor", cost_usd=0.01, ts=recent), programs_root=programs_root)
        append_ai_telemetry(_make_record("p", feature="claim_extractor", cost_usd=0.02, ts=recent), programs_root=programs_root)
        append_ai_telemetry(_make_record("p", feature="blurb_generator", cost_usd=0.05, ts=recent), programs_root=programs_root)
        append_ai_telemetry(
            _make_record("p", feature="blurb_generator", status=AiTelemetryStatus.RATE_LIMIT, cost_usd=None, ts=recent),
            programs_root=programs_root,
        )

        summary = build_feature_cost_summary("p", programs_root=programs_root, window_days=1)

    assert "claim_extractor" in summary
    assert "blurb_generator" in summary
    ce = summary["claim_extractor"]
    assert ce.call_count == 2
    assert ce.ok_count == 2
    assert ce.total_cost_usd == pytest.approx(0.03, rel=1e-6)
    bg = summary["blurb_generator"]
    assert bg.call_count == 2
    assert bg.ok_count == 1
    assert bg.error_count == 1
    assert bg.total_cost_usd == pytest.approx(0.05, rel=1e-6)


# ---------------------------------------------------------------------------
# 5. Status classification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("msg,expected", [
    ("BudgetExceeded: spent $1.00 of $0.50", AiTelemetryStatus.BUDGET_EXCEEDED),
    ("HTTP 429 Too Many Requests", AiTelemetryStatus.RATE_LIMIT),
    ("context length exceeded: 4096 tokens", AiTelemetryStatus.CONTEXT_LENGTH),
    ("401 Unauthorized: invalid token", AiTelemetryStatus.AUTH),
    ("Timed out after 30s", AiTelemetryStatus.TIMEOUT),
    ("Something random happened", AiTelemetryStatus.OTHER),
])
def test_status_from_exception(msg, expected):
    exc = RuntimeError(msg)
    assert AiTelemetryStatus.from_exception(exc) == expected


# ---------------------------------------------------------------------------
# 6. Registry entry
# ---------------------------------------------------------------------------
def test_ai_telemetry_registry_entry():
    assert "ai_telemetry" in STATE_READER_REGISTRY
    reg = STATE_READER_REGISTRY["ai_telemetry"]
    assert reg.owner_module == "src.core.ai_telemetry"
    assert "append_ai_telemetry" in reg.reader_symbols
    assert "read_ai_telemetry" in reg.reader_symbols
    assert "build_feature_cost_summary" in reg.reader_symbols
    assert reg.path_pattern == "programs/<program>/_state/ai_telemetry.jsonl"


# ---------------------------------------------------------------------------
# 7. QG-WS5B vacuous pass — no budget
# ---------------------------------------------------------------------------
def test_qg_ws5b_passes_when_no_budget():
    with tempfile.TemporaryDirectory() as tmpdir:
        report = evaluate_ai_budget_gate(
            program_id="prog",
            budget_usd_per_run=0.0,
            programs_root=Path(tmpdir),
        )
    assert report.passed
    results = {r.gate_id: r for r in report.results}
    assert results["QG-WS5B"].passed


# ---------------------------------------------------------------------------
# 8. QG-WS5B passes when under budget
# ---------------------------------------------------------------------------
def test_qg_ws5b_passes_when_under_budget():
    with tempfile.TemporaryDirectory() as tmpdir:
        programs_root = Path(tmpdir)
        append_ai_telemetry(_make_record("p", cost_usd=0.10), programs_root=programs_root)
        report = evaluate_ai_budget_gate(
            program_id="p",
            budget_usd_per_run=1.00,
            programs_root=programs_root,
            window_days=1,
        )
    assert report.passed


# ---------------------------------------------------------------------------
# 9. QG-WS5B fails (forceable) when over budget
# ---------------------------------------------------------------------------
def test_qg_ws5b_fails_forceable_when_over_budget():
    with tempfile.TemporaryDirectory() as tmpdir:
        programs_root = Path(tmpdir)
        for _ in range(5):
            append_ai_telemetry(_make_record("p", cost_usd=0.30), programs_root=programs_root)
        report = evaluate_ai_budget_gate(
            program_id="p",
            budget_usd_per_run=0.50,
            programs_root=programs_root,
            window_days=1,
        )
    assert not report.passed
    result = report.results[0]
    assert result.gate_id == "QG-WS5B"
    assert result.forceable is True
    assert result.exit_code == 3
    assert "exceeded" in result.message.lower()


# ---------------------------------------------------------------------------
# 10. QG-WS5B vacuous pass — program_id is None
# ---------------------------------------------------------------------------
def test_qg_ws5b_passes_when_program_id_none():
    with tempfile.TemporaryDirectory() as tmpdir:
        report = evaluate_ai_budget_gate(
            program_id=None,
            budget_usd_per_run=1.00,
            programs_root=Path(tmpdir),
        )
    assert report.passed


# ---------------------------------------------------------------------------
# 11. QG-WS5B is wired into evaluate_phase_1b_gates
# ---------------------------------------------------------------------------
def test_qg_ws5b_wired_into_phase_1b_gates():
    """QG-WS5B gate_id appears in evaluate_phase_1b_gates output."""
    from src.core.models import FreshnessReport
    from src.core.quality_gates import evaluate_phase_1b_gates

    with tempfile.TemporaryDirectory() as tmpdir:
        programs_root = Path(tmpdir)
        # Seed a telemetry record that exceeds a tiny budget
        append_ai_telemetry(_make_record("p2", cost_usd=1.00), programs_root=programs_root)

        report = evaluate_phase_1b_gates(
            freshness_report=FreshnessReport(issue_number=1, items=(), blocks=0, warns=0, infos=0),
            program_id="p2",
            programs_root=programs_root,
            budget_usd_per_run=0.01,  # tiny budget → should fire
        )

    gate_ids = {r.gate_id for r in report.results}
    assert "QG-WS5B" in gate_ids
    qg27 = next(r for r in report.results if r.gate_id == "QG-WS5B")
    assert not qg27.passed
    assert qg27.forceable
