"""ADF-W2.11/W3.8/W4.8: adf_workflow_metrics.py review-latency aggregation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.adf_workflow_metrics import compute_workflow_measurement_report
from src.core.proposal_audit import record_proposal_event

_BASE = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)


def _seed(programs_root: Path, *, program_id: str = "xpf") -> None:
    # r1: proposed at BASE, approved 5 minutes later.
    record_proposal_event(
        program_id=program_id, proposal_type="risk", proposal_id="r1",
        event="approved", programs_root=programs_root,
        at=_BASE + timedelta(minutes=5), proposed_at=_BASE,
    )
    # r2: proposed at BASE, rejected 15 minutes later.
    record_proposal_event(
        program_id=program_id, proposal_type="risk", proposal_id="r2",
        event="rejected", programs_root=programs_root,
        at=_BASE + timedelta(minutes=15), proposed_at=_BASE,
        rejection_reason="stale",
    )
    # One meeting_action, approved 2 minutes later.
    record_proposal_event(
        program_id=program_id, proposal_type="meeting_action", proposal_id="m1",
        event="approved", programs_root=programs_root,
        at=_BASE + timedelta(minutes=2), proposed_at=_BASE,
    )


def test_report_covers_every_known_type_even_with_no_data(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    report = compute_workflow_measurement_report("xpf", programs_root=programs_root)
    assert report.total_proposal_events == 0
    types = {summary.proposal_type for summary in report.by_type}
    assert types == {"risk", "meeting_action", "top_three", "governance_decision_brief", "dependency_blast_radius"}
    for summary in report.by_type:
        assert summary.decided_count == 0
        assert summary.p50_latency_seconds is None


def test_risk_counts_and_latency(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed(programs_root)
    report = compute_workflow_measurement_report("xpf", programs_root=programs_root)
    risk_summary = next(s for s in report.by_type if s.proposal_type == "risk")
    assert risk_summary.decided_count == 2
    assert risk_summary.approved_count == 1
    assert risk_summary.rejected_count == 1
    assert risk_summary.max_latency_seconds == 900.0  # 15 minutes
    assert risk_summary.p50_latency_seconds is not None


def test_meeting_action_counts(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed(programs_root)
    report = compute_workflow_measurement_report("xpf", programs_root=programs_root)
    meeting_summary = next(s for s in report.by_type if s.proposal_type == "meeting_action")
    assert meeting_summary.decided_count == 1
    assert meeting_summary.p50_latency_seconds == 120.0  # 2 minutes


def test_since_filter_excludes_earlier_decisions(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed(programs_root)
    # Everything decided before BASE + 10 minutes should be excluded.
    report = compute_workflow_measurement_report(
        "xpf", since=_BASE + timedelta(minutes=10), programs_root=programs_root
    )
    risk_summary = next(s for s in report.by_type if s.proposal_type == "risk")
    # Only r2 (decided at +15min) survives the filter.
    assert risk_summary.decided_count == 1
    assert risk_summary.rejected_count == 1
    assert risk_summary.approved_count == 0


def test_programs_are_isolated(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed(programs_root, program_id="xpf")
    armada_report = compute_workflow_measurement_report("armada", programs_root=programs_root)
    assert armada_report.total_proposal_events == 0


def test_to_dict_is_json_serializable(tmp_path: Path) -> None:
    import json

    programs_root = tmp_path / "programs"
    _seed(programs_root)
    report = compute_workflow_measurement_report("xpf", programs_root=programs_root)
    json.dumps(report.to_dict())  # must not raise
