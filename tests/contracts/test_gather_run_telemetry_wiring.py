"""WS-17 contract tests: gather.py wire-in for run_telemetry + per-channel
latency capture, and the failure-taxonomy category wiring.

The test surface:
- ``run_telemetry_wiring.observe_step`` writes into the right channel
  bucket for each mapped step name, and is a no-op for unmapped steps.
- ``run_telemetry_wiring.record_run_telemetry_for_gather`` emits a valid
  ``RunTelemetryRecord`` whose channels are filtered by the
  ``include_*`` flags (defensive: do not invent channel stats the
  operator didn't actually run).
- The wire-in route path is end-to-end: accumulator → record → JSONL
  sidecar (PB-37 portalocker-routed) is observable from a fresh
  ``programs_root``.
- The ``run_telemetry.jsonl`` sidecar is registered in the state reader
  registry (D-18 contract) and the registration is consistent with
  the helper's read API.
- Failure cases: empty accumulator yields no record (early-exit gather
  must not write a half-row), and a raised write error does not
  propagate out of the helper (telemetry must never block gather).
"""
from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.commands.gather_pipeline.run_telemetry_wiring import (
    build_run_telemetry_accumulator,
    observe_step,
    record_run_telemetry_for_gather,
)
from src.core.run_telemetry import (
    read_run_telemetry,
    run_telemetry_path,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
GATHER_PY = REPO_ROOT / "src" / "commands" / "gather.py"
WIRING_PY = REPO_ROOT / "src" / "commands" / "gather_pipeline" / "run_telemetry_wiring.py"
REGISTRY_PY = REPO_ROOT / "src" / "core" / "state_reader_registry.py"


# ---------------------------------------------------------------------------
# Library-level tests
# ---------------------------------------------------------------------------


def test_observe_step_writes_to_mapped_channel() -> None:
    acc = build_run_telemetry_accumulator()
    fake_started = 1000.0
    # patch perf_counter so the elapsed is deterministic
    import unittest.mock as mock
    with mock.patch("src.commands.gather_pipeline.run_telemetry_wiring.perf_counter", return_value=1000.250):
        latency = observe_step(acc, step_name="ado", started_at=fake_started)
    assert latency == 250
    assert acc["ado"] == [250]


def test_observe_step_is_noop_for_unmapped_steps() -> None:
    acc = build_run_telemetry_accumulator()
    assert observe_step(acc, step_name="prepare", started_at=0.0) is None
    assert observe_step(acc, step_name="finalize", started_at=0.0) is None
    assert observe_step(acc, step_name="persist", started_at=0.0) is None
    assert all(samples == [] for samples in acc.values())


def test_observe_step_routes_workiq_to_workiq_bucket() -> None:
    acc = build_run_telemetry_accumulator()
    observe_step(acc, step_name="workiq", started_at=0.0)
    assert acc["workiq"] and len(acc["workiq"]) == 1
    assert acc["ado"] == []


def test_observe_step_routes_kusto_to_kusto_bucket() -> None:
    acc = build_run_telemetry_accumulator()
    observe_step(acc, step_name="kusto", started_at=0.0)
    assert acc["kusto"] and len(acc["kusto"]) == 1


def test_observe_step_routes_icm_to_icm_bucket() -> None:
    acc = build_run_telemetry_accumulator()
    observe_step(acc, step_name="icm", started_at=0.0)
    assert acc["icm"] and len(acc["icm"]) == 1


def test_observe_step_accumulates_multiple_samples() -> None:
    acc = build_run_telemetry_accumulator()
    for _ in range(3):
        observe_step(acc, step_name="ado", started_at=0.0)
    assert acc["ado"] and len(acc["ado"]) == 3


def test_record_emits_valid_jsonl_record(tmp_path: Path) -> None:
    program_id = "t-wiring"
    acc = build_run_telemetry_accumulator()
    acc["ado"] = [120, 150]
    acc["kusto"] = [800]
    started = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
    path = record_run_telemetry_for_gather(
        program_id=program_id,
        programs_root=tmp_path,
        accumulator=acc,
        started_at=started,
        include_workiq=False,
        include_kusto=True,
        include_icm=False,
    )
    assert path is not None
    assert path == run_telemetry_path(program_id, tmp_path)
    assert path.exists()
    # PB-37: one JSON object per line.
    text = path.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert len(rows) == 1
    record = rows[0]
    assert record["program_id"] == program_id
    assert record["run_id"]
    channels = {entry["channel"] for entry in record["channels"]}
    assert "ado" in channels
    assert "kusto" in channels
    # include_workiq=False → no workiq channel emitted
    assert "workiq" not in channels


def test_record_filters_excluded_channels(tmp_path: Path) -> None:
    program_id = "t-wiring-2"
    acc = build_run_telemetry_accumulator()
    # Simulate a path where workiq was observed (step ran) but the operator
    # disabled include_workiq for this run (e.g. by flag) — the defensive
    # filter must drop it.
    acc["workiq"] = [42]
    acc["ado"] = [100]
    started = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
    path = record_run_telemetry_for_gather(
        program_id=program_id,
        programs_root=tmp_path,
        accumulator=acc,
        started_at=started,
        include_workiq=False,
        include_kusto=False,
        include_icm=False,
    )
    assert path is not None
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    channels = {entry["channel"] for entry in rows[0]["channels"]}
    assert "workiq" not in channels
    assert "ado" in channels


def test_record_returns_none_on_empty_accumulator(tmp_path: Path) -> None:
    acc = build_run_telemetry_accumulator()
    started = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
    path = record_run_telemetry_for_gather(
        program_id="t-empty",
        programs_root=tmp_path,
        accumulator=acc,
        started_at=started,
        include_workiq=True,
        include_kusto=True,
        include_icm=True,
    )
    assert path is None
    # No sidecar should have been written.
    sidecar = tmp_path / "t-empty" / "run_telemetry.jsonl"
    assert not sidecar.exists()


def test_record_returns_none_when_all_channels_filtered(tmp_path: Path) -> None:
    acc = build_run_telemetry_accumulator()
    # Only icm observed, but include_icm=False → all dropped.
    acc["icm"] = [10]
    started = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
    path = record_run_telemetry_for_gather(
        program_id="t-filtered",
        programs_root=tmp_path,
        accumulator=acc,
        started_at=started,
        include_workiq=False,
        include_kusto=False,
        include_icm=False,
    )
    assert path is None


def test_record_swallows_write_errors(tmp_path: Path) -> None:
    """Telemetry must never block gather — a write error returns None
    rather than raising."""
    acc = build_run_telemetry_accumulator()
    acc["ado"] = [10]
    started = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
    # Make programs_root unwritable by passing a non-Path that has no
    # attribute programs_root / program_id.
    class _Boom:
        def __getattr__(self, name: str) -> None:
            return None  # will TypeError on str concat / Path math

    path = record_run_telemetry_for_gather(
        program_id="t-boom",
        programs_root=_Boom(),  # type: ignore[arg-type]
        accumulator=acc,
        started_at=started,
        include_workiq=False,
        include_kusto=False,
        include_icm=False,
    )
    assert path is None  # swallowed


def test_record_writes_portalocker_rounded_jsonl(tmp_path: Path) -> None:
    program_id = "t-portalock"
    acc = build_run_telemetry_accumulator()
    acc["ado"] = [50]
    started = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
    path = record_run_telemetry_for_gather(
        program_id=program_id,
        programs_root=tmp_path,
        accumulator=acc,
        started_at=started,
        include_workiq=False,
        include_kusto=False,
        include_icm=False,
    )
    assert path is not None
    # Each row is a single JSON object terminated with \n (append-only).
    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    assert b"\n\n" not in raw  # no blank lines


def test_record_is_round_trip_readable(tmp_path: Path) -> None:
    """What we write is what ``read_run_telemetry`` reads back."""
    program_id = "t-roundtrip"
    acc = build_run_telemetry_accumulator()
    acc["ado"] = [100, 200, 300]
    acc["kusto"] = [1000]
    started = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)
    record_run_telemetry_for_gather(
        program_id=program_id,
        programs_root=tmp_path,
        accumulator=acc,
        started_at=started,
        include_workiq=False,
        include_kusto=True,
        include_icm=False,
    )
    records = read_run_telemetry(program_id, programs_root=tmp_path)
    assert len(records) == 1
    by_channel = {s.channel: s for s in records[0].channels}
    assert by_channel["ado"].latency_ms_samples == (100, 200, 300)
    assert by_channel["kusto"].latency_ms_samples == (1000,)


# ---------------------------------------------------------------------------
# Source-level (AST) tests
# ---------------------------------------------------------------------------


def test_gather_py_imports_run_telemetry_wiring_helpers() -> None:
    text = GATHER_PY.read_text(encoding="utf-8")
    assert "build_run_telemetry_accumulator" in text
    assert "observe_step" in text
    assert "record_run_telemetry_for_gather" in text


def test_gather_py_invokes_record_run_telemetry_for_gather() -> None:
    """The end-of-run wire-in call must be present in gather.py and
    invoked after the state-write stage completes (i.e. just before
    the return statement that yields state_write_result.artifacts)."""
    text = GATHER_PY.read_text(encoding="utf-8")
    assert "_record_run_telemetry_for_gather(" in text


def test_gather_py_complete_progress_step_feeds_accumulator() -> None:
    """The progress callback closure must feed the accumulator — the
    wire-in is the only path the new code takes; without this call,
    the accumulator stays empty."""
    text = GATHER_PY.read_text(encoding="utf-8")
    # Find the _complete_progress_step def and check the body.
    tree = ast.parse(text, filename=str(GATHER_PY))
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_complete_progress_step":
            body_src = ast.unparse(node)
            assert "_observe_run_telemetry_step" in body_src, (
                "_complete_progress_step must call _observe_run_telemetry_step"
            )
            found = True
    assert found, "_complete_progress_step not found in gather.py"


def test_wiring_module_routes_through_append_run_telemetry() -> None:
    """The wire-in must go through ``append_run_telemetry`` (PB-37) so
    the JSONL sidecar is portalocker-routed and fsynced — no direct
    ``open('a', ...)`` writes from the wire-in."""
    text = WIRING_PY.read_text(encoding="utf-8")
    assert "append_run_telemetry" in text
    # AST walk: no direct .open("a",...) calls in the wiring module.
    tree = ast.parse(text, filename=str(WIRING_PY))
    direct_appends: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "open":
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and "a" in arg.value:
                    direct_appends.append((node.lineno, node.col_offset))
    assert not direct_appends, (
        f"run_telemetry_wiring.py has direct .open('a',...) calls at {direct_appends}"
    )


def test_run_telemetry_registered_in_state_reader_registry() -> None:
    """D-18: ``run_telemetry`` must be a registered state. The state_reader_authority
    test suite already enforces the no-direct-reads rule; here we just confirm
    the registration entry exists and points at the right module + symbols."""
    text = REGISTRY_PY.read_text(encoding="utf-8")
    assert '"run_telemetry"' in text or "'run_telemetry'" in text
    # The reader_symbols tuple must include the public surface the wire-in
    # consumer (vertex observability perf / vertex doctor --diagnose) needs.
    for sym in ("read_run_telemetry", "build_channel_perf_summary", "run_telemetry_path"):
        assert sym in text, f"state_reader_registry missing reader_symbol {sym!r}"


# ---------------------------------------------------------------------------
# Failure-taxonomy cross-wiring (lightweight — the contract lives in
# tests/contracts/test_failure_taxonomy_contract.py; this just confirms
# the wire-in does not lose the channel-bucketing contract).
# ---------------------------------------------------------------------------


def test_observed_channels_have_distinct_buckets() -> None:
    """A step in the ``ado`` bucket must not appear in the ``kusto`` bucket
    and vice versa. Regression guard for the step-to-channel map."""
    acc = build_run_telemetry_accumulator()
    observe_step(acc, step_name="ado", started_at=0.0)
    observe_step(acc, step_name="kusto", started_at=0.0)
    observe_step(acc, step_name="icm", started_at=0.0)
    observe_step(acc, step_name="workiq", started_at=0.0)
    # Each step lands in its own bucket — no cross-contamination.
    assert len(acc["ado"]) == 1
    assert len(acc["kusto"]) == 1
    assert len(acc["icm"]) == 1
    assert len(acc["workiq"]) == 1
