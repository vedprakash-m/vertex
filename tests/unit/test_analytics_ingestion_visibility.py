from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.commands import gather
from src.commands.gather_pipeline.source_ingestion_stage import record_optional_source_ingestion_runs
from src.core.exceptions import QueryError
from src.core.gather_run_manifest import resolve_latest_committed_manifest
from src.core.gather_state_store import load_gather_state
from src.core.models import Confidence
from src.core.models_v2 import ADOConfig, IntegrationError, Program, Signal, Workstream
from src.core.reality_store import RealityStore


_NOW = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
_PRIVATE_ERROR = "synthetic-private-analytics-provider-error"
_SAFE_ERROR = "Analytics gather failed; see gather diagnostics."


@pytest.mark.parametrize("mode", ["failed", "empty", "available"])
def test_actual_gather_analytics_ingestion(
    mode: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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

    class FakeADOClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def query_work_item_snapshot(self, **_kwargs: object) -> list[dict[str, object]]:
            if mode == "failed":
                raise QueryError(_PRIVATE_ERROR)
            if mode == "empty":
                return []
            return [{
                "DateSK": 20260510,
                "WorkItemId": 101,
                "AreaPath": "Example\\Area",
                "State": "Active",
                "WorkItemType": "Feature",
                "Title": "Synthetic item",
            }]

    monkeypatch.setattr(gather, "ADOClient", FakeADOClient)
    monkeypatch.setattr(gather, "_load_ado_wiql_queries", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(gather, "_load_program_context", lambda *_args, **_kwargs: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda *_args, **_kwargs: (14, 30))
    artifacts = gather.gather_program(
        "synthetic",
        programs_root=root,
        as_of=_NOW,
        loader=lambda *_args, **_kwargs: ((), 0),
        freshness_loader=lambda *_args, **_kwargs: ((), 0),
        include_analytics=True,
    )
    manifest = resolve_latest_committed_manifest("synthetic", programs_root=root)
    state = load_gather_state("synthetic", programs_root=root)
    assert manifest is not None and state is not None
    outcome = next(entry for entry in manifest.channel_outcomes if entry.channel == "analytics")
    store = RealityStore("synthetic", db_root=db_root)
    with sqlite3.connect(store.db_path) as connection:
        ingestion = connection.execute(
            "SELECT status, signals_written, error_message FROM reality_ingestion_runs WHERE source_ref = ?",
            ("ado/analytics",),
        ).fetchone()
    failed = mode == "failed"
    assert artifacts.integration_error_count == int(failed)
    assert state.integration_errors == int(failed)
    assert outcome.degraded is failed
    assert gather._semantic_gather_exit_code(artifacts) == (2 if failed else 0)
    assert ingestion == ("failed" if failed else "success", int(mode == "available"), _SAFE_ERROR if failed else None)
    assert _PRIVATE_ERROR not in repr(ingestion)


class _Recorder:
    def __init__(self) -> None:
        self.runs = []

    def initialize(self) -> None:
        pass

    def record_ingestion_run(self, run) -> None:
        self.runs.append(run)


def _signal(source: str) -> Signal:
    return Signal(
        id=f"synthetic-{source}",
        timestamp=_NOW,
        source=source,
        program_id="synthetic",
        workstream_id="ws",
        entity_refs=(),
        text="Synthetic",
        raw_ref="synthetic",
        confidence=Confidence.HIGH,
    )


@pytest.mark.parametrize(
    ("source", "expected_error"),
    [("analytics", _SAFE_ERROR), ("  AnAlYtIcS  ", _SAFE_ERROR), ("ado/analytics", _PRIVATE_ERROR)],
)
def test_analytics_ingestion_matches_channel_and_direct_source_errors(source: str, expected_error: str) -> None:
    recorder = _Recorder()
    record_optional_source_ingestion_runs(
        "synthetic",
        as_of=_NOW,
        include_workiq=False,
        include_analytics=True,
        include_sprints=True,
        include_pipelines=False,
        include_icm=False,
        signals=(_signal("ado/analytics"), _signal("ado/wiql"), _signal("ado/sprint")),
        integration_error_details=(
            IntegrationError(source=source, stage="gather", retryable=True, message=_PRIVATE_ERROR),
        ),
        store=recorder,
    )
    analytics = next(run for run in recorder.runs if run.source_ref == "ado/analytics")
    sprint = next(run for run in recorder.runs if run.source_ref == "ado/sprint")
    assert (analytics.status, analytics.signals_written, analytics.error_message) == ("failed", 2, expected_error)
    assert (sprint.status, sprint.signals_written, sprint.error_message) == ("success", 1, None)


def test_other_source_error_does_not_fail_analytics_ingestion() -> None:
    recorder = _Recorder()
    record_optional_source_ingestion_runs(
        "synthetic",
        as_of=_NOW,
        include_workiq=False,
        include_analytics=True,
        include_sprints=True,
        include_pipelines=False,
        include_icm=True,
        signals=(_signal("ado/analytics"), _signal("ado/sprint"), _signal("icm")),
        integration_error_details=(
            IntegrationError(source="sprints", stage="capacity", retryable=True, message="Sprint failure"),
        ),
        store=recorder,
    )
    analytics = next(run for run in recorder.runs if run.source_ref == "ado/analytics")
    sprint = next(run for run in recorder.runs if run.source_ref == "ado/sprint")
    icm = next(run for run in recorder.runs if run.source_ref == "icm")
    assert (analytics.status, analytics.signals_written, analytics.error_message) == ("success", 1, None)
    assert (sprint.status, sprint.signals_written, sprint.error_message) == ("failed", 1, "Sprint failure")
    assert (icm.status, icm.signals_written, icm.error_message) == ("success", 1, None)


def test_disabled_analytics_does_not_add_an_ingestion_run() -> None:
    recorder = _Recorder()
    record_optional_source_ingestion_runs(
        "synthetic",
        as_of=_NOW,
        include_workiq=False,
        include_analytics=False,
        include_sprints=False,
        include_pipelines=False,
        include_icm=False,
        signals=(),
        integration_error_details=(
            IntegrationError(source="analytics", stage="gather", retryable=True, message=_PRIVATE_ERROR),
        ),
        store=recorder,
    )
    assert all(run.source_ref != "ado/analytics" for run in recorder.runs)
