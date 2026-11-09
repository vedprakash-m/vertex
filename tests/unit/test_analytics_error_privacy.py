from __future__ import annotations

import logging
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from cli import app
from src.commands import gather
from src.commands.gather_pipeline import ado_snapshot_stage
from src.core.exceptions import QueryError, QueryTimeoutError
from src.core.gather_run_manifest import resolve_latest_committed_manifest
from src.core.gather_state_store import load_gather_state
from src.core.journal import append_signal, read_signals
from src.core.models_v2 import ADOConfig, Program, Workstream
from src.core.reality_store import RealityStore
from src.core.signal_dedup import build_deterministic_signal_id


_NOW = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
_PRIVATE = "SYNTHETICPRIVATEPROVIDERDETAIL"


@pytest.fixture
def scenario(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    root = tmp_path / "programs"
    db_root = tmp_path / "db"
    monkeypatch.setenv("VERTEX_DB_PATH", str(db_root))
    program = Program(
        schema_version="2.0",
        id="synthetic",
        name="Synthetic",
        ado=ADOConfig(
            organization="example",
            project="Example",
            area_paths=("Example\\Area",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
        ),
    )
    workstreams = (Workstream(id="ws", name="Example", area_paths=("Example\\Area",)),)
    behavior: dict[str, Exception | None] = {"exception": QueryError(_PRIVATE)}

    class FakeADOClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def query_work_item_snapshot(self, **_kwargs: object) -> list[dict[str, Any]]:
            error = behavior["exception"]
            if error is not None:
                raise error
            return []

    monkeypatch.setattr(gather, "ADOClient", FakeADOClient)
    monkeypatch.setattr(gather, "_load_ado_wiql_queries", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(gather, "_load_program_context", lambda *_args, **_kwargs: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda *_args, **_kwargs: (14, 30))
    return SimpleNamespace(root=root, db_root=db_root, program=program, workstreams=workstreams, behavior=behavior)


def _gather(scenario: SimpleNamespace, *, as_of: datetime = _NOW, **kwargs: object) -> Any:
    return gather.gather_program(
        "synthetic",
        programs_root=scenario.root,
        as_of=as_of,
        loader=lambda *_args, **_kwargs: ((), 0),
        freshness_loader=lambda *_args, **_kwargs: ((), 0),
        include_analytics=True,
        **kwargs,
    )


@pytest.mark.parametrize("error_type", [QueryError, QueryTimeoutError, TimeoutError, typer.BadParameter])
def test_actual_gather_error_surfaces_omit_provider_text(
    scenario: SimpleNamespace, caplog: pytest.LogCaptureFixture, error_type: type[Exception]
) -> None:
    scenario.behavior["exception"] = error_type(_PRIVATE)
    with caplog.at_level(logging.WARNING):
        artifacts = _gather(scenario)
    state = load_gather_state("synthetic", programs_root=scenario.root)
    manifest = resolve_latest_committed_manifest("synthetic", programs_root=scenario.root)
    signals = tuple(read_signals("synthetic", programs_root=scenario.root))
    assert state is not None and manifest is not None
    surfaces = {
        "gather result": repr(artifacts.integration_errors),
        "gather state": repr(state.integration_error_details),
        "manifest outcomes": repr(manifest.channel_outcomes),
        "manifest failed refs": repr(manifest.failed_refs),
        "persisted signals": repr(signals),
        "logs": caplog.text,
    }
    assert [name for name, value in surfaces.items() if _PRIVATE in value] == []
    assert artifacts.integration_error_count == 1
    detail = artifacts.integration_errors[0]
    assert (detail.source, detail.stage, detail.retryable) == ("analytics", "gather", True)
    assert detail.message and detail.operator_action
    assert state.integration_error_details == (detail,)
    assert next(item for item in manifest.channel_outcomes if item.channel == "analytics").degraded
    assert any(
        signal.source == "system" and (signal.metadata or {}).get("integration_source") == "analytics"
        for signal in signals
    )
    assert gather._semantic_gather_exit_code(artifacts) == 2


def test_injected_analytics_loader_uses_same_safe_diagnostic(scenario: SimpleNamespace) -> None:
    def broken_loader(_program: object, _workstreams: object, _as_of: object) -> tuple[tuple[()], int]:
        raise QueryError(_PRIVATE)

    artifacts = _gather(scenario, analytics_signal_loader=broken_loader)
    assert _PRIVATE not in repr(artifacts.integration_errors)
    assert artifacts.integration_errors[0].operator_action


def test_safe_error_signal_identity_is_stable_for_the_same_gather_time(scenario: SimpleNamespace) -> None:
    first = _gather(scenario)
    initial_signals = tuple(read_signals("synthetic", programs_root=scenario.root))
    scenario.behavior["exception"] = QueryError("another synthetic provider error")
    repeated = _gather(scenario)
    repeated_signals = tuple(read_signals("synthetic", programs_root=scenario.root))
    repeated_state = load_gather_state("synthetic", programs_root=scenario.root)
    repeated_manifest = resolve_latest_committed_manifest("synthetic", programs_root=scenario.root)
    store = RealityStore("synthetic", db_root=scenario.db_root)
    with sqlite3.connect(store.db_path) as connection:
        ingestion = connection.execute(
            "SELECT status, error_message FROM reality_ingestion_runs WHERE source_ref = ?",
            ("ado/analytics",),
        ).fetchall()
    later = _gather(scenario, as_of=_NOW + timedelta(seconds=1))
    later_signals = tuple(read_signals("synthetic", programs_root=scenario.root))

    assert len(initial_signals) == 1
    assert initial_signals[0].id == build_deterministic_signal_id(initial_signals[0])
    assert first.new_signals == 1 and repeated.new_signals == 0 and later.new_signals == 1
    assert len(repeated_signals) == 1 and len(later_signals) == 2
    assert all(result.integration_error_count == 1 and gather._semantic_gather_exit_code(result) == 2
               for result in (first, repeated, later))
    assert repeated_state is not None and repeated_state.integration_errors == 1
    assert repeated_manifest is not None
    assert next(item for item in repeated_manifest.channel_outcomes if item.channel == "analytics").degraded
    assert ingestion and all(row == ("failed", "Analytics gather failed; see gather diagnostics.") for row in ingestion)
    initial_ids = {signal.id for signal in initial_signals}
    repeated_ids = {signal.id for signal in repeated_signals}
    later_ids = {signal.id for signal in later_signals}
    assert len(initial_ids) == 1 and repeated_ids == initial_ids
    assert initial_ids < later_ids and len(later_ids) == 2


def test_safe_error_signal_deduplicates_legacy_preclassification_id(scenario: SimpleNamespace) -> None:
    signal = gather._build_integration_error_signal(
        program_id="synthetic", source="analytics", error="ADO Analytics gather failed", as_of=_NOW
    )
    legacy = replace(signal, id=build_deterministic_signal_id(signal))
    path = append_signal(legacy, programs_root=scenario.root, partition_at=_NOW)
    original = path.read_bytes()

    artifacts = _gather(scenario)

    assert artifacts.new_signals == 0
    assert path.read_bytes() == original
    stored = tuple(read_signals("synthetic", programs_root=scenario.root))
    assert len(stored) == 1 and stored[0].id == legacy.id


def test_unexpected_analytics_failure_still_propagates(scenario: SimpleNamespace) -> None:
    scenario.behavior["exception"] = RuntimeError("unexpected failure")
    with pytest.raises(RuntimeError, match="unexpected failure"):
        _gather(scenario)


def test_actual_cli_reports_failure_without_provider_text(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gather, "PROGRAMS_ROOT", scenario.root)
    monkeypatch.setattr(gather, "_surface_alert_banner", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", lambda *_args, **_kwargs: ((), (), 0))
    result = CliRunner().invoke(app, ["gather", "--program", "synthetic", "--analytics"])
    assert result.exit_code == 2, result.output
    assert "analytics/gather" in result.output
    assert "Next:" in result.output
    assert _PRIVATE not in result.output


@pytest.mark.parametrize(
    ("mode", "expected_calls"),
    [("ordinary", 1), ("unknown_field", 1), ("retry_limit", 4)],
)
def test_snapshot_query_failures_log_no_provider_text(
    scenario: SimpleNamespace, caplog: pytest.LogCaptureFixture, mode: str, expected_calls: int
) -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []

    class FakeClient:
        def query_work_item_snapshot(
            self, *, filter_expression: str, select_fields: tuple[str, ...]
        ) -> list[dict[str, Any]]:
            calls.append((filter_expression, select_fields))
            if mode == "ordinary":
                raise QueryError(_PRIVATE)
            field = (
                _PRIVATE if mode == "unknown_field"
                else ("IsLastRevisionOfDay", "FieldA", "FieldB", "FieldC")[len(calls) - 1]
            )
            raise QueryError(f"VS403522: The property '{field}' is not available; {_PRIVATE}")

    with caplog.at_level(logging.WARNING), pytest.raises(QueryError, match=_PRIVATE):
        ado_snapshot_stage.load_analytics_signals(
            scenario.program,
            scenario.workstreams,
            _NOW,
            programs_root=scenario.root,
            ado_client_factory=lambda **_kwargs: FakeClient(),
            date_to_sk_fn=lambda value: int(value.strftime("%Y%m%d")),
            analytics_snapshot_fields=("DateSK", "WorkItemId", "FieldA", "FieldB", "FieldC"),
            build_analytics_signals_fn=lambda **_kwargs: (),
            load_wiql_golden_query_signals_fn=lambda *_args, **_kwargs: ((), 0),
            expected_max_age_hours=48,
        )
    assert len(calls) == expected_calls
    assert _PRIVATE not in caplog.text
    assert scenario.program.id not in caplog.text


def test_retry_still_removes_unavailable_field_without_logging_its_name(
    scenario: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    selections: list[tuple[str, ...]] = []
    captured_rows: list[Any] = []

    class FakeClient:
        def query_work_item_snapshot(
            self, *, filter_expression: str, select_fields: tuple[str, ...]
        ) -> list[dict[str, Any]]:
            selections.append(select_fields)
            if _PRIVATE in select_fields:
                raise QueryError(f"VS403522: The property '{_PRIVATE}' is not available")
            return [{"WorkItemId": 101}]

    with caplog.at_level(logging.WARNING):
        signals, calls = ado_snapshot_stage.load_analytics_signals(
            scenario.program,
            scenario.workstreams,
            _NOW,
            programs_root=scenario.root,
            ado_client_factory=lambda **_kwargs: FakeClient(),
            date_to_sk_fn=lambda value: int(value.strftime("%Y%m%d")),
            analytics_snapshot_fields=("DateSK", "WorkItemId", _PRIVATE),
            build_analytics_signals_fn=lambda **kwargs: captured_rows.append(kwargs["rows"]) or (),
            load_wiql_golden_query_signals_fn=lambda *_args, **_kwargs: ((), 3),
            expected_max_age_hours=48,
        )
    assert selections == [("DateSK", "WorkItemId", _PRIVATE), ("DateSK", "WorkItemId")]
    assert captured_rows == [[{"WorkItemId": 101}]]
    assert signals == () and calls == 5
    assert _PRIVATE not in caplog.text
    assert scenario.program.id not in caplog.text
