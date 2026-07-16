"""Unit tests for ``src/core/measurement_store.py`` (ADF-W0.7 measurement kernel)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.core.measurement_store import (
    DEFAULT_MEASUREMENT_MAX_BYTES,
    MEASUREMENT_SCHEMA_VERSION,
    TierDecisionRecord,
    TierDecisionSinkConfig,
    compute_record_checksum,
    make_tier_decision_sink,
    append_measurement,
    read_measurements,
    tier_decision_store_path,
    verify_record_checksum,
)
from src.ai.tiered_router import RouteOutcome, Tier


def _sample_record(program_id: str = "xpf", feature: str = "intent_routing") -> TierDecisionRecord:
    return TierDecisionRecord(
        schema_version=MEASUREMENT_SCHEMA_VERSION,
        program_id=program_id,
        edition_id="xpf_weekly",
        run_id="run-1",
        feature=feature,
        chosen_tier=Tier.DETERMINISTIC.value,
        outcome=RouteOutcome.DETERMINISTIC_HIT.value,
        confidence=0.95,
        frontier_eligible=True,
        frontier_called=False,
        cache_hit=False,
        policy_version="1",
        model_version=None,
        deployment_id=None,
        context_hash=None,
        latency_ms=12.0,
        input_tokens=None,
        output_tokens=None,
        cost_usd=None,
        execution_mode="observe",
        recorded_at="2026-07-11T12:00:00+00:00",
    )


def test_checksum_excludes_checksum_field() -> None:
    payload = {"a": 1, "b": "two"}
    digest = compute_record_checksum(payload)
    payload_with_checksum = {**payload, "record_checksum": digest}
    assert payload_with_checksum["record_checksum"] == digest
    assert verify_record_checksum(payload_with_checksum) is True


def test_checksum_is_order_independent() -> None:
    # Same key set, different insertion order -> same digest (sort_keys=True).
    d1 = compute_record_checksum({"a": 1, "b": 2})
    d2 = compute_record_checksum({"b": 2, "a": 1})
    assert d1 == d2


def test_checksum_tamper_detected() -> None:
    payload = {"a": 1, "record_checksum": compute_record_checksum({"a": 1})}
    assert verify_record_checksum(payload) is True
    tampered = {**payload, "a": 999}
    assert verify_record_checksum(tampered) is False


def test_append_measurement_writes_checksummed_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "tier_decisions.jsonl"
    append_measurement(path, _sample_record())
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["schema_version"] == MEASUREMENT_SCHEMA_VERSION
    assert row["feature"] == "intent_routing"
    assert row["record_checksum"] == compute_record_checksum(
        {k: v for k, v in row.items() if k != "record_checksum"}
    )
    assert verify_record_checksum(row) is True


def test_append_measurements_writes_multiple(tmp_path: Path) -> None:
    path = tmp_path / "tier_decisions.jsonl"
    n = append_measurement.__module__  # noqa: F841 - just use module import path
    from src.core.measurement_store import append_measurements

    count = append_measurements(path, [_sample_record(feature="a"), _sample_record(feature="b")])
    assert count == 2
    rows = read_measurements(path)
    assert len(rows) == 2
    assert {r["feature"] for r in rows} == {"a", "b"}


def test_read_measurements_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_measurements(tmp_path / "absent.jsonl") == ()


def test_records_survive_simulated_restart(tmp_path: Path) -> None:
    path = tmp_path / "tier_decisions.jsonl"
    append_measurement(path, _sample_record(feature="first"))
    # "restart" = new process just appends again
    append_measurement(path, _sample_record(feature="second"))
    rows = read_measurements(path)
    assert [r["feature"] for r in rows] == ["first", "second"]
    assert all(verify_record_checksum(r) for r in rows)


def test_cache_and_cache_hit_round_trip(tmp_path: Path) -> None:
    """Schema round-trips the future cache/cache_hit values (Appendix A.4)."""
    record = TierDecisionRecord(
        schema_version=MEASUREMENT_SCHEMA_VERSION,
        program_id="xpf",
        edition_id=None,
        run_id="run-2",
        feature="risk_proposal",
        chosen_tier=Tier.CACHE.value,
        outcome=RouteOutcome.CACHE_HIT.value,
        confidence=None,
        frontier_eligible=False,
        frontier_called=False,
        cache_hit=True,
        policy_version="1",
        model_version="gpt-4o",
        deployment_id="dep-1",
        context_hash="abc123",
        latency_ms=3.0,
        input_tokens=0,
        output_tokens=0,
        cost_usd=0.0,
        execution_mode="enforce",
        recorded_at="2026-07-11T12:01:00+00:00",
    )
    path = tmp_path / "tier_decisions.jsonl"
    append_measurement(path, record)
    row = read_measurements(path)[0]
    assert row["chosen_tier"] == "cache"
    assert row["outcome"] == "cache_hit"
    assert row["cache_hit"] is True
    assert row["deployment_id"] == "dep-1"


def test_tier_decision_sink_persists_decision(tmp_path: Path) -> None:
    from src.ai.tiered_router import TierDecision, reset_recorded_decisions

    reset_recorded_decisions()
    config = TierDecisionSinkConfig(
        program_id="xpf",
        edition_id="xpf_weekly",
        run_id="run-sink",
        execution_mode="observe",
        policy_version="1",
        programs_root=tmp_path,
    )
    sink = make_tier_decision_sink(config)
    decision = TierDecision(
        feature="intent_routing",
        tier=Tier.DETERMINISTIC,
        outcome=RouteOutcome.DETERMINISTIC_HIT,
        confidence=0.9,
        frontier_called=False,
        trace_id="run-sink",
        recorded_at="2026-07-11T12:05:00+00:00",
    )
    sink(decision)
    rows = read_measurements(tier_decision_store_path("xpf", programs_root=tmp_path))
    assert len(rows) == 1
    assert rows[0]["feature"] == "intent_routing"
    assert rows[0]["chosen_tier"] == "deterministic"
    assert rows[0]["execution_mode"] == "observe"


def test_tier_decision_sink_survives_write_error(tmp_path: Path) -> None:
    """A durable sink must never break routing (existing sink contract)."""
    from src.ai.tiered_router import TierDecision

    config = TierDecisionSinkConfig(
        program_id="xpf",
        edition_id=None,
        run_id="run-err",
        execution_mode="enforce",
        programs_root=tmp_path / "readonly",  # nonexistent parent is fine; force error below
    )
    sink = make_tier_decision_sink(config)
    # Corrupt the store path to force an exception inside the sink.
    sink.store_path = tmp_path / "nested" / "deep" / "tier_decisions.jsonl"  # type: ignore[attr-defined]
    decision = TierDecision(
        feature="x",
        tier=Tier.FRONTIER,
        outcome=RouteOutcome.FRONTIER_CALL,
        confidence=1.0,
        frontier_called=True,
        trace_id=None,
        recorded_at="2026-07-11T12:06:00+00:00",
    )
    # Should not raise even though the path may be unwritable in some envs.
    sink(decision)


def test_default_max_bytes_is_set() -> None:
    assert DEFAULT_MEASUREMENT_MAX_BYTES > 0
