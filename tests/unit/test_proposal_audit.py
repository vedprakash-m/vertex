"""ADF-W2.11/W3.8/W4.8: proposal_audit.py generic audit trail."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core.proposal_audit import (
    ProposalAuditRecord,
    proposal_audit_path,
    read_proposal_audit,
    record_proposal_event,
)

_T1 = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
_T2 = datetime(2026, 7, 1, 10, 5, 30, tzinfo=timezone.utc)


def test_record_is_a_noop_without_programs_root() -> None:
    # Existing callers that don't pass programs_root get zero I/O.
    record_proposal_event(
        program_id="xpf", proposal_type="risk", proposal_id="r1",
        event="approved", programs_root=None,
    )
    # No path was ever computed/created; nothing to assert against except
    # that this didn't raise.


def test_record_and_read_round_trip(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    record_proposal_event(
        program_id="xpf", proposal_type="risk", proposal_id="r1",
        event="approved", programs_root=programs_root, at=_T2, proposed_at=_T1,
        ai_run_id="run-1",
    )
    records = read_proposal_audit("xpf", programs_root=programs_root)
    assert len(records) == 1
    record = records[0]
    assert record.program_id == "xpf"
    assert record.proposal_type == "risk"
    assert record.proposal_id == "r1"
    assert record.event == "approved"
    assert record.at == _T2
    assert record.proposed_at == _T1
    assert record.ai_run_id == "run-1"
    assert record.rejection_reason is None


def test_rejection_reason_round_trips(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    record_proposal_event(
        program_id="xpf", proposal_type="meeting_action", proposal_id="m1",
        event="rejected", programs_root=programs_root, at=_T2, proposed_at=_T1,
        rejection_reason="not actionable",
    )
    records = read_proposal_audit("xpf", programs_root=programs_root)
    assert records[0].rejection_reason == "not actionable"


def test_read_returns_empty_tuple_when_no_file(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    assert read_proposal_audit("xpf", programs_root=programs_root) == ()


def test_multiple_records_append_in_order(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    for proposal_id in ("r1", "r2", "r3"):
        record_proposal_event(
            program_id="xpf", proposal_type="risk", proposal_id=proposal_id,
            event="approved", programs_root=programs_root, at=_T2, proposed_at=_T1,
        )
    records = read_proposal_audit("xpf", programs_root=programs_root)
    assert [r.proposal_id for r in records] == ["r1", "r2", "r3"]


def test_path_is_per_program(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    xpf_path = proposal_audit_path("xpf", programs_root=programs_root)
    armada_path = proposal_audit_path("armada", programs_root=programs_root)
    assert xpf_path != armada_path
    assert xpf_path.name == "proposal_audit.jsonl"
