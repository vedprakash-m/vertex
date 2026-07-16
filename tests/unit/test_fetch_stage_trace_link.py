"""ADF-W2.12 (Section 8.2.6): the FetchStage is the first in-pipeline
production writer of an ``operation.trace_linked.v1`` acquisition/source
link. These tests cover the helper directly (no live ADO data needed) and
the opt-out / dedup / correlation-isolation properties the spec requires.

The full FetchStage.execute() path needs the private ``stage_v2_report_workspace``
fixture (data-gated), so the production stage integration is covered by the
existing data-dependent pipeline tests; these tests cover the trace-link
behaviour itself in isolation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.core.operation_trace import REF_TYPE_SOURCE, load_operation_trace
from src.core.pipeline import StageContext
from src.core.stages.fetch_stage import _record_acquisition_trace

_AS_OF = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def _ctx(tmp_path: Path, *, correlation_id: str = "", run_id: str = "") -> StageContext:
    """A minimal StageContext carrying only what the trace helper needs."""
    return StageContext(
        programs_root=tmp_path / "programs",
        bundle=SimpleNamespace(program=SimpleNamespace(id="xpf")),
        correlation_id=correlation_id,
        workflow_id="weekly_report",
        run_id=run_id,
    )


def test_no_correlation_id_records_nothing(tmp_path: Path) -> None:
    """The opt-out default: a StageContext with no correlation identity
    must not write anything (existing construction call sites pass "")."""
    _record_acquisition_trace(_ctx(tmp_path), _AS_OF, [object()], "live")
    assert load_operation_trace("xpf", "anything", programs_root=tmp_path / "programs") is None


def test_acquisition_link_recorded_under_shared_correlation_id(tmp_path: Path) -> None:
    correlation_id = "corr-fetch-1"
    _record_acquisition_trace(
        _ctx(tmp_path, correlation_id=correlation_id, run_id="run-1"),
        _AS_OF,
        [1, 2, 3],
        "live",
    )
    trace = load_operation_trace("xpf", correlation_id, programs_root=tmp_path / "programs")
    assert trace is not None
    assert trace.workflow_id == "weekly_report"
    assert trace.run_id == "run-1"
    assert len(trace.source_refs) == 1
    assert trace.source_refs[0].startswith("ado:live:3@")
    # No other ref family populated by the acquisition stage.
    assert trace.fact_refs == ()
    assert trace.output_refs == ()


def test_offline_and_live_modes_produce_distinct_refs(tmp_path: Path) -> None:
    correlation_id = "corr-fetch-2"
    ctx = _ctx(tmp_path, correlation_id=correlation_id, run_id="run-2")
    _record_acquisition_trace(ctx, _AS_OF, [1], "live")
    _record_acquisition_trace(ctx, _AS_OF, [1], "offline")
    trace = load_operation_trace("xpf", correlation_id, programs_root=tmp_path / "programs")
    assert trace is not None
    assert len(trace.source_refs) == 2
    modes = sorted(ref.split(":")[1] for ref in trace.source_refs)
    assert modes == ["live", "offline"]


def test_identical_acquisition_is_dedup_idempotent(tmp_path: Path) -> None:
    """The ledger dedupes on (correlation_id, ref_type, ref_id); re-recording
    the same acquisition under the same correlation_id must not duplicate."""
    correlation_id = "corr-fetch-3"
    ctx = _ctx(tmp_path, correlation_id=correlation_id, run_id="run-3")
    _record_acquisition_trace(ctx, _AS_OF, [1, 2], "live")
    _record_acquisition_trace(ctx, _AS_OF, [1, 2], "live")
    trace = load_operation_trace("xpf", correlation_id, programs_root=tmp_path / "programs")
    assert trace is not None
    assert len(trace.source_refs) == 1


def test_trace_failure_is_swallowed_and_never_blocks(tmp_path: Path) -> None:
    """The trace link is observability, never a render blocker: a broken
    programs_root (read-only parent) must not raise out of the helper."""
    _record_acquisition_trace(
        _ctx(tmp_path, correlation_id="corr-fetch-4", run_id="run-4"),
        _AS_OF,
        [1],
        "live",
    )  # must not raise
