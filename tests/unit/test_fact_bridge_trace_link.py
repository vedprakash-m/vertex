"""ADF-W2.12: the fact-stage OperationTrace writer in
``src/core/ledger/fact_bridge.py`` -- conflict/corroboration facts written by
``run_cross_source_conflict_detection`` now record a ``stage="fact"``
trace link under the rev cycle's shared ``correlation_id`` (when one is
threaded), the same no-op-when-absent discipline ``fetch_stage.py``'s
acquisition-stage writer already established.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core.ledger.fact_bridge import _append_conflict_fact, _append_corroboration_fact
from src.core.operation_trace import load_operation_trace
from src.core.program_fact_store import ProgramFactStore


def _programs_root(tmp_path: Path) -> Path:
    programs_root = tmp_path / "programs"
    (programs_root / "acme").mkdir(parents=True)
    return programs_root


def test_conflict_fact_records_trace_link_when_correlation_id_present(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    store = ProgramFactStore("acme", db_root=tmp_path)
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    conflict = {
        "entity_id": "MILESTONE:m1", "family": "milestone", "expected_value": "on_track",
        "observed_value": "at_risk", "material": True, "losing_source": "ado", "winning_source": "eml",
    }

    _append_conflict_fact(store, conflict, now, program_id="acme", correlation_id="corr-1", programs_root=programs_root)

    trace = load_operation_trace("acme", "corr-1", programs_root=programs_root)
    assert trace is not None
    assert len(trace.fact_refs) == 1
    assert "fact.conflict:MILESTONE:m1:milestone" in trace.fact_refs[0]


def test_corroboration_fact_records_trace_link_when_correlation_id_present(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    store = ProgramFactStore("acme", db_root=tmp_path)
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    corroboration = {"entity_id": "RISK:r1", "family": "risk", "source_a": "ado", "source_b": "eml"}

    _append_corroboration_fact(store, corroboration, now, program_id="acme", correlation_id="corr-2", programs_root=programs_root)

    trace = load_operation_trace("acme", "corr-2", programs_root=programs_root)
    assert trace is not None
    assert len(trace.fact_refs) == 1
    assert "fact.corroboration:RISK:r1:risk" in trace.fact_refs[0]


def test_no_trace_link_recorded_when_correlation_id_empty(tmp_path: Path) -> None:
    # Pre-ADF-W2.12 default: an empty correlation_id means no run-level
    # correlation identity was threaded, so this stays a pure no-op -- every
    # pre-existing call site (correlation_id="") sees zero behavior change.
    programs_root = _programs_root(tmp_path)
    store = ProgramFactStore("acme", db_root=tmp_path)
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    conflict = {
        "entity_id": "MILESTONE:m1", "family": "milestone", "expected_value": "on_track",
        "observed_value": "at_risk", "material": True, "losing_source": "ado", "winning_source": "eml",
    }

    _append_conflict_fact(store, conflict, now, program_id="acme", correlation_id="", programs_root=programs_root)

    assert load_operation_trace("acme", "", programs_root=programs_root) is None
    # The fact itself is still written -- only the trace link is skipped.
    snapshot = store.snapshot(as_of=now)
    assert len(snapshot.facts) == 1
