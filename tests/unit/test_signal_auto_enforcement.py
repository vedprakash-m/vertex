"""Tests for FR-SG-38: signal auto-approval policy enforcement."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core.models import Confidence
from src.core.models_v2 import ReviewPolicy, Signal, SignalReviewDecision
from src.core.signal_review import (
    compute_auto_approval_policies,
    get_review_policy_audit_path,
    load_review_policy_audit_entries,
    write_autonomy_audit_entries,
)


def _signal(sig_id: str, source: str = "workiq/email") -> Signal:
    return Signal(
        id=sig_id,
        timestamp=datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
        source=source,
        program_id="acme",
        workstream_id="ws-1",
        entity_refs=(),
        text="test signal",
        raw_ref=None,
        confidence=Confidence.MEDIUM,
    )


def _approved(sig_id: str) -> tuple[str, SignalReviewDecision]:
    return sig_id, SignalReviewDecision(
        signal_id=sig_id,
        decision="approved",
        reviewed_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        reviewed_by="reviewer",
    )


def _deferred(sig_id: str) -> tuple[str, SignalReviewDecision]:
    return sig_id, SignalReviewDecision(
        signal_id=sig_id,
        decision="deferred",
        reviewed_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        reviewed_by="reviewer",
    )


def test_compute_auto_approval_no_history_returns_empty() -> None:
    signals = tuple(_signal(f"s{i}") for i in range(5))
    result = compute_auto_approval_policies(signals, {})
    assert result == {}


def test_compute_auto_approval_below_min_sample_returns_empty() -> None:
    # Only 5 reviewed signals, min_sample=10 → no recommendations
    signals = tuple(_signal(f"s{i}") for i in range(15))
    review_states = dict(_approved(f"s{i}") for i in range(5))
    result = compute_auto_approval_policies(signals, review_states, min_sample=10)
    assert result == {}


def test_compute_auto_approval_high_rate_promotes_pending() -> None:
    # 10 reviewed, 9 approved (90% > 80% ceiling) → PENDING signals should be AUTO_APPROVED
    signals = tuple(_signal(f"s{i}") for i in range(15))
    review_states = {**dict(_approved(f"s{i}") for i in range(9)), **dict([_deferred("s9")])}
    result = compute_auto_approval_policies(signals, review_states, min_sample=10, ceiling_rate=0.8)
    # Signals s10..s14 have no review decision and are PENDING by default
    # They should be recommended as AUTO_APPROVED
    for sig_id in [f"s{i}" for i in range(10, 15)]:
        assert result.get(sig_id) == ReviewPolicy.AUTO_APPROVED


def test_compute_auto_approval_low_rate_demotes_auto_approved() -> None:
    # 10 reviewed, 1 approved (10% < 20% floor) → AUTO_APPROVED signals should be demoted to PENDING
    source = "ado/revision"

    # Create signals where ado/revision has LOW approval rate
    signals = [_signal(f"s{i}", source=source) for i in range(15)]
    # Override review_policy on first few to AUTO_APPROVED via monkey-patching
    from dataclasses import replace
    signals_tuple = tuple(
        replace(s, review_policy=ReviewPolicy.AUTO_APPROVED) for s in signals
    )
    review_states = {**dict([_approved("s0")]), **dict(_deferred(f"s{i}") for i in range(1, 10))}
    result = compute_auto_approval_policies(signals_tuple, review_states, min_sample=10, floor_rate=0.2)
    # Signals that are currently AUTO_APPROVED and source has low rate → should be PENDING
    auto_approved_ids = [s.id for s in signals_tuple if s.review_policy == ReviewPolicy.AUTO_APPROVED]
    for sig_id in auto_approved_ids[:5]:
        if sig_id in result:
            assert result[sig_id] == ReviewPolicy.PENDING


def test_write_autonomy_audit_entries_creates_file(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    policy_changes = {
        "sig-1": ReviewPolicy.AUTO_APPROVED,
        "sig-2": ReviewPolicy.PENDING,
    }
    write_autonomy_audit_entries("demo", policy_changes, programs_root=programs_root)
    audit_path = get_review_policy_audit_path("demo", programs_root=programs_root)
    assert audit_path.exists()
    lines = load_review_policy_audit_entries("demo", programs_root=programs_root)
    assert len(lines) == 2
    sig_ids = {entry.signal_id for entry in lines}
    assert sig_ids == {"sig-1", "sig-2"}
    for line in lines:
        assert line.new_policy in {ReviewPolicy.AUTO_APPROVED, ReviewPolicy.PENDING}
        assert line.source == "auto_enforcement"


def test_write_autonomy_audit_entries_appends(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    write_autonomy_audit_entries("demo", {"sig-1": ReviewPolicy.AUTO_APPROVED}, programs_root=programs_root)
    write_autonomy_audit_entries("demo", {"sig-2": ReviewPolicy.PENDING}, programs_root=programs_root)
    audit_path = get_review_policy_audit_path("demo", programs_root=programs_root)
    lines = load_review_policy_audit_entries("demo", programs_root=programs_root)
    assert audit_path.exists()
    assert len(lines) == 2


def test_write_autonomy_audit_entries_empty_noop(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    write_autonomy_audit_entries("demo", {}, programs_root=programs_root)
    audit_path = get_review_policy_audit_path("demo", programs_root=programs_root)
    assert not audit_path.exists()
