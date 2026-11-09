from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from cli import app
from src.commands import gather
from src.commands.gather_pipeline.source_ingestion_stage import record_optional_source_ingestion_runs
from src.commands.watch_scan import watch_program_once
from src.core.catchup_scan import WatchSource
from src.core.exceptions import QueryError
from src.core.gather_run_manifest import (
    ChannelOutcomeEntry,
    RequiredScopeStatus,
    resolve_latest_committed_manifest,
    resolve_latest_full_committed_manifest,
)
from src.core.gather_state_store import load_gather_state
from src.core.integration_types import DiscoveryQueryResult
from src.core.journal import read_signals
from src.core.models import RiskLevel, WorkItem
from src.core.models_v2 import ADOConfig, IntegrationError, Program, Workstream
from src.core.reality_store import RealityStore


_NOW = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)
_PRIVATE_ERROR = "synthetic-private-provider-error"


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
            area_paths=("Example\\Alpha", "Example\\Beta"),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
        ),
    )
    workstreams = (
        Workstream(id="alpha", name="Alpha", area_paths=("Example\\Alpha",), ado_team="Team Alpha"),
        Workstream(id="beta", name="Beta", area_paths=("Example\\Beta",), ado_team="Team Beta"),
    )
    items = tuple(
        WorkItem(
            id=index,
            type="Feature",
            title=f"Synthetic item {index}",
            state="Active",
            assigned_to=None,
            assigned_to_email=None,
            area_path=f"Example\\{name}",
            iteration_path="Example\\Sprint",
            target_date=None,
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={},
            fetched_at=_NOW,
        )
        for index, name in ((101, "Alpha"), (102, "Beta"))
    )
    modes = {"Team Alpha": "failed", "Team Beta": "available", None: "failed"}

    class FakeADOClient:
        def __init__(self, **_: object) -> None:
            pass

        def list_team_iterations(self, timeframe: str | None = None, team: str | None = None) -> list[dict[str, object]]:
            assert timeframe == "current"
            assert team in modes
            return [{
                "id": "iteration-1",
                "name": "Sprint",
                "path": "Example\\Sprint",
                "attributes": {
                    "startDate": "2026-05-04T00:00:00Z",
                    "finishDate": "2026-05-15T00:00:00Z",
                    "timeFrame": "current",
                },
            }]

        def list_iteration_capacities(self, iteration_id: str, team: str | None = None) -> list[dict[str, object]]:
            assert iteration_id == "iteration-1"
            mode = modes[team]
            if mode == "failed":
                raise QueryError(_PRIVATE_ERROR)
            if mode == "unexpected":
                raise RuntimeError("unexpected defect")
            if mode == "empty":
                return []
            return [{"activities": [{"capacityPerDay": 0 if mode == "zero" else 6}]}]

        def query_work_item_snapshot(self, **_: object) -> list[dict[str, object]]:
            return []

    monkeypatch.setattr(gather, "ADOClient", FakeADOClient)
    monkeypatch.setattr(gather, "_load_program_context", lambda *_args, **_kwargs: (program, workstreams))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda *_args, **_kwargs: (14, 30))
    return SimpleNamespace(root=root, db_root=db_root, program=program, workstreams=workstreams, items=items, modes=modes)


def _gather(scenario: SimpleNamespace, **kwargs: object):
    return gather.gather_program(
        "synthetic",
        programs_root=scenario.root,
        as_of=_NOW,
        loader=lambda *_args, **_kwargs: (scenario.items, 0),
        freshness_loader=lambda *_args, **_kwargs: (scenario.items, 0),
        include_sprints=True,
        **kwargs,
    )


def _observed(scenario: SimpleNamespace):
    manifest = resolve_latest_committed_manifest("synthetic", programs_root=scenario.root)
    state = load_gather_state("synthetic", programs_root=scenario.root)
    assert manifest is not None
    assert state is not None
    signals = {
        signal.workstream_id: signal
        for signal in read_signals("synthetic", programs_root=scenario.root)
        if signal.source == "ado/sprint"
    }
    store = RealityStore("synthetic", db_root=scenario.db_root)
    with sqlite3.connect(store.db_path) as connection:
        ingestion = connection.execute(
            "SELECT status, signals_written, error_message FROM reality_ingestion_runs WHERE source_ref = ?",
            ("ado/sprint",),
        ).fetchone()
    outcome = next(entry for entry in manifest.channel_outcomes if entry.channel == "sprints")
    return manifest, state, signals, ingestion, outcome


@pytest.mark.parametrize("beta_mode", ["available", "failed"])
def test_gather_capacity_failure_degrades_manifest_state_and_ingestion(
    scenario: SimpleNamespace, caplog: pytest.LogCaptureFixture, beta_mode: str
) -> None:
    scenario.modes["Team Beta"] = beta_mode
    with caplog.at_level(logging.WARNING):
        artifacts = _gather(scenario)
    manifest, state, signals, ingestion, outcome = _observed(scenario)

    assert artifacts.ado_calls == 5
    assert len(artifacts.integration_errors) == 1
    error = artifacts.integration_errors[0]
    assert (error.source, error.stage, error.retryable) == ("sprints", "capacity", True)
    assert "capacity" in error.message.lower()
    assert error.operator_action
    assert state.integration_errors == 1
    assert state.integration_error_details == (error,)
    assert outcome.degraded is True
    assert outcome.degrade_reason == error.message
    assert [(ref.ref_kind, ref.ref_id, ref.reason) for ref in manifest.failed_refs] == [
        ("sprints", "capacity", error.message)
    ]
    assert ingestion == ("failed", 2, error.message)
    assert gather._semantic_gather_exit_code(artifacts) == 2
    assert set(signals) == {"alpha", "beta"}
    assert signals["alpha"].metadata is not None
    assert signals["beta"].metadata is not None
    assert signals["alpha"].metadata["total_capacity_per_day"] is None
    assert signals["beta"].metadata["total_capacity_per_day"] == (6.0 if beta_mode == "available" else None)
    warnings = "\n".join(record.getMessage() for record in caplog.records if record.name.endswith("ado_snapshot_stage"))
    for private_value in (_PRIVATE_ERROR, "Team Alpha", "Team Beta", "iteration-1"):
        assert private_value not in warnings
        assert private_value not in repr(error)
        assert private_value not in repr(manifest.failed_refs)
        assert private_value not in repr(ingestion)


@pytest.mark.parametrize(
    ("alpha_mode", "beta_mode", "alpha_capacity", "beta_capacity"),
    [("empty", "available", None, 6.0), ("zero", "available", 0.0, 6.0), ("available", "empty", 6.0, None)],
)
def test_successful_empty_zero_and_available_capacity_remain_healthy(
    scenario: SimpleNamespace, alpha_mode: str, beta_mode: str, alpha_capacity: float | None, beta_capacity: float | None
) -> None:
    scenario.modes["Team Alpha"] = alpha_mode
    scenario.modes["Team Beta"] = beta_mode
    artifacts = _gather(scenario)
    _, state, signals, ingestion, outcome = _observed(scenario)

    assert artifacts.ado_calls == 5
    assert artifacts.integration_errors == ()
    assert state.integration_errors == 0
    assert outcome.degraded is False
    assert ingestion == ("success", 2, None)
    assert gather._semantic_gather_exit_code(artifacts) == 0
    assert signals["alpha"].metadata is not None
    assert signals["beta"].metadata is not None
    assert signals["alpha"].metadata["total_capacity_per_day"] == alpha_capacity
    assert signals["beta"].metadata["total_capacity_per_day"] == beta_capacity


def test_default_team_capacity_failure_is_reported_without_identifiers(
    scenario: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    errors: list[IntegrationError] = []
    with caplog.at_level(logging.WARNING):
        signals, calls = gather._load_sprint_signals(
            scenario.program,
            (replace(scenario.workstreams[0], ado_team=None),),
            (scenario.items[0],),
            _NOW,
            integration_error_sink=errors,
        )
    assert calls == 3
    assert len(signals) == 1
    assert len(errors) == 1
    assert errors[0].source == "sprints"
    assert errors[0].stage == "capacity"
    assert _PRIVATE_ERROR not in caplog.text
    assert "iteration-1" not in caplog.text


def test_failed_capacity_without_sprint_items_is_still_degraded(scenario: SimpleNamespace) -> None:
    scenario.items = ()
    artifacts = _gather(scenario)
    _, state, signals, ingestion, outcome = _observed(scenario)

    assert artifacts.integration_error_count == 1
    assert state.integration_errors == 1
    assert signals == {}
    assert ingestion is not None and ingestion[:2] == ("failed", 0)
    assert outcome.degraded is True


@pytest.mark.parametrize("alpha_mode", ["failed", "empty"])
@pytest.mark.parametrize("required_status", ["FULL", "PARTIAL"])
def test_optional_capacity_outcome_does_not_change_required_ado_scope(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, alpha_mode: str, required_status: str
) -> None:
    scenario.modes["Team Alpha"] = alpha_mode
    membership_ids = ("101", "102")
    query = DiscoveryQueryResult(
        query_id="synthetic-query",
        scope_id="synthetic-scope",
        wiql_hash="a" * 64,
        captured_at=_NOW,
        raw_count=2,
        membership_ids=membership_ids,
        membership_hash=hashlib.sha256("\n".join(membership_ids).encode()).hexdigest(),
        cap_reached=required_status == "PARTIAL",
        completeness_state=required_status,
    )
    refs = tuple(
        SimpleNamespace(registration=SimpleNamespace(ref_id=item_id, ref_kind="work_item"))
        for item_id in membership_ids
    )

    def fake_uil(*_args, discovery_result_sink, channel_outcome_sink, **_kwargs):
        discovery_result_sink.append(SimpleNamespace(query_results=(query,), discovered_refs=refs))
        channel_outcome_sink.append(ChannelOutcomeEntry("ado", False, None, 0.0))
        return scenario.items, scenario.items, 0

    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(gather, "_load_ado_items_via_uil", fake_uil)
    artifacts = gather.gather_program("synthetic", programs_root=scenario.root, as_of=_NOW, include_sprints=True)
    manifest = resolve_latest_committed_manifest("synthetic", programs_root=scenario.root)
    full_manifest = resolve_latest_full_committed_manifest("synthetic", programs_root=scenario.root)

    assert manifest is not None and manifest.required_scope_status is RequiredScopeStatus(required_status)
    if required_status == "FULL":
        assert full_manifest is not None and full_manifest.run_id == manifest.run_id
    else:
        assert full_manifest is None
    expected_exit = 3 if required_status == "PARTIAL" else (2 if alpha_mode == "failed" else 0)
    assert gather._semantic_gather_exit_code(artifacts) == expected_exit
    assert next(entry for entry in manifest.channel_outcomes if entry.channel == "sprints").degraded is (
        alpha_mode == "failed"
    )


def test_actual_gather_cli_discloses_safe_capacity_failure(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gather, "PROGRAMS_ROOT", scenario.root)
    monkeypatch.setattr(gather, "_surface_alert_banner", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gather, "_resolve_uil_channel_binding_for_gather", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        gather, "_load_ado_items_via_uil", lambda *_args, **_kwargs: (scenario.items, scenario.items, 0)
    )
    result = CliRunner().invoke(app, ["gather", "--program", "synthetic", "--sprints"])

    assert result.exit_code == 2, result.output
    assert "sprints/capacity" in result.output
    assert "Next:" in result.output
    for private_value in (_PRIVATE_ERROR, "Team Alpha", "Team Beta", "iteration-1"):
        assert private_value not in result.output
    _, _, signals, ingestion, outcome = _observed(scenario)
    assert set(signals) == {"alpha", "beta"}
    assert ingestion is not None and ingestion[0] == "failed"
    assert outcome.degraded is True


def test_watch_keeps_sprint_signals_and_warns_without_provider_details(
    scenario: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        result = watch_program_once(
            "synthetic",
            since=_NOW - timedelta(days=1),
            as_of=_NOW,
            sources=(WatchSource.SPRINTS,),
            programs_root=scenario.root,
            program_context=(replace(scenario.program, maturity_level=2), scenario.workstreams),
            full_loader=lambda *_args: (scenario.items, 0),
            auto_approve_signals=False,
        )
    assert result.discovered_signals == 2
    assert result.ado_calls == 5
    assert "Sprint capacity unavailable" in caplog.text
    for private_value in (_PRIVATE_ERROR, "Team Alpha", "Team Beta", "iteration-1"):
        assert private_value not in caplog.text
    assert {signal.workstream_id for signal in read_signals("synthetic", programs_root=scenario.root)} == {
        "alpha", "beta"
    }


@pytest.mark.parametrize("caller", ["gather", "watch"])
def test_snapshot_fallback_warning_omits_program_and_provider_text(
    scenario: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    caller: str,
) -> None:
    scenario.modes["Team Alpha"] = "available"
    scenario.modes["Team Beta"] = "available"
    provider_text = "private-snapshot-provider-value"
    snapshot_calls: list[dict[str, object]] = []
    fallback_calls: list[tuple[str, dict[str, str]]] = []

    def reject_snapshot(_client: object, **kwargs: object) -> list[dict[str, object]]:
        snapshot_calls.append(kwargs)
        raise QueryError(provider_text)

    def fallback_snapshot(
        _client: object, entity_set: str, params: dict[str, str]
    ) -> list[dict[str, object]]:
        fallback_calls.append((entity_set, params))
        return [
            {"WorkItemId": 101, "DateSK": 20260508, "State": "Active", "Revision": 1},
            {"WorkItemId": 102, "DateSK": 20260508, "State": "Done", "Revision": 1},
        ]

    monkeypatch.setattr(gather.ADOClient, "query_work_item_snapshot", reject_snapshot)
    monkeypatch.setattr(gather.ADOClient, "query_odata_all", fallback_snapshot, raising=False)
    with caplog.at_level(logging.WARNING):
        if caller == "gather":
            artifacts = _gather(scenario)
            assert artifacts.ado_calls == 6
            assert artifacts.integration_errors == ()
            _, state, signals, ingestion, outcome = _observed(scenario)
            assert state.integration_errors == 0
            assert set(signals) == {"alpha", "beta"}
            assert ingestion == ("success", 2, None)
            assert outcome.degraded is False
            assert gather._semantic_gather_exit_code(artifacts) == 0
        else:
            result = watch_program_once(
                "synthetic",
                since=_NOW - timedelta(days=1),
                as_of=_NOW,
                sources=(WatchSource.SPRINTS,),
                programs_root=scenario.root,
                program_context=(replace(scenario.program, maturity_level=2), scenario.workstreams),
                full_loader=lambda *_args: (scenario.items, 0),
                auto_approve_signals=False,
            )
            assert result.discovered_signals == 2
            assert result.ado_calls == 6
            signals = {
                signal.workstream_id: signal
                for signal in read_signals("synthetic", programs_root=scenario.root)
                if signal.source == "ado/sprint"
            }
            assert set(signals) == {"alpha", "beta"}

    assert signals["alpha"].metadata is not None
    assert signals["beta"].metadata is not None
    assert signals["alpha"].metadata["open_history"] == {"2026-05-08": 1}
    assert signals["beta"].metadata["completed_history"] == {"2026-05-08": 1}
    assert len(snapshot_calls) == 1
    assert snapshot_calls[0]["select_fields"] == gather._SPRINT_SNAPSHOT_FIELDS
    assert len(fallback_calls) == 1
    entity_set, params = fallback_calls[0]
    assert entity_set == "WorkItemSnapshot"
    assert params["$expand"] == "Iteration"
    assert "WorkItemId eq 101" in params["$filter"]
    assert "WorkItemId eq 102" in params["$filter"]
    assert "AreaPath" not in params["$select"]
    assert "IterationPath" not in params["$select"]
    warnings = [
        record for record in caplog.records
        if record.name.endswith("ado_snapshot_stage") and "fallback" in record.getMessage().lower()
    ]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert provider_text not in warnings[0].getMessage()
    assert scenario.program.id not in warnings[0].getMessage()
    assert warnings[0].args == ()
    assert warnings[0].exc_info is None


def test_successful_snapshot_query_does_not_log_fallback(
    scenario: SimpleNamespace, caplog: pytest.LogCaptureFixture
) -> None:
    scenario.modes["Team Alpha"] = "available"
    scenario.modes["Team Beta"] = "available"
    with caplog.at_level(logging.WARNING):
        signals, calls = gather._load_sprint_signals(
            scenario.program, scenario.workstreams, scenario.items, _NOW
        )
    assert calls == 5
    assert {signal.workstream_id for signal in signals} == {"alpha", "beta"}
    assert not any("fallback" in record.getMessage().lower() for record in caplog.records)


def test_snapshot_fallback_query_error_still_propagates(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    scenario.modes["Team Alpha"] = "available"
    scenario.modes["Team Beta"] = "available"
    failure = QueryError("synthetic item-scoped failure")
    fallback_calls: list[str] = []

    def reject_snapshot(_client: object, **_kwargs: object) -> list[dict[str, object]]:
        raise QueryError("private-snapshot-provider-value")

    def reject_fallback(_client: object, entity_set: str, _params: dict[str, str]) -> list[dict[str, object]]:
        fallback_calls.append(entity_set)
        raise failure

    monkeypatch.setattr(gather.ADOClient, "query_work_item_snapshot", reject_snapshot)
    monkeypatch.setattr(gather.ADOClient, "query_odata_all", reject_fallback, raising=False)
    with caplog.at_level(logging.WARNING), pytest.raises(QueryError) as caught:
        gather._load_sprint_signals(scenario.program, scenario.workstreams, scenario.items, _NOW)
    assert caught.value is failure
    assert fallback_calls == ["WorkItemSnapshot"]
    assert "private-snapshot-provider-value" not in caplog.text
    assert scenario.program.id not in caplog.text


def test_unexpected_snapshot_exception_does_not_trigger_fallback(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    scenario.modes["Team Alpha"] = "available"
    scenario.modes["Team Beta"] = "available"
    failure = RuntimeError("unexpected snapshot defect")

    def reject_snapshot(_client: object, **_kwargs: object) -> list[dict[str, object]]:
        raise failure

    def reject_unexpected_fallback(_client: object, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
        pytest.fail("Unexpected exceptions must not trigger the fallback.")

    monkeypatch.setattr(gather.ADOClient, "query_work_item_snapshot", reject_snapshot)
    monkeypatch.setattr(gather.ADOClient, "query_odata_all", reject_unexpected_fallback, raising=False)
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError) as caught:
        gather._load_sprint_signals(scenario.program, scenario.workstreams, scenario.items, _NOW)
    assert caught.value is failure
    assert not any("fallback" in record.getMessage().lower() for record in caplog.records)


def test_snapshot_fallback_with_no_items_does_not_query_odata(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    scenario.modes["Team Alpha"] = "available"
    scenario.modes["Team Beta"] = "available"

    def reject_snapshot(_client: object, **_kwargs: object) -> list[dict[str, object]]:
        raise QueryError("private-snapshot-provider-value")

    def reject_empty_fallback(_client: object, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
        pytest.fail("No item-scoped request should be made for an empty item set.")

    monkeypatch.setattr(gather.ADOClient, "query_work_item_snapshot", reject_snapshot)
    monkeypatch.setattr(gather.ADOClient, "query_odata_all", reject_empty_fallback, raising=False)
    with caplog.at_level(logging.WARNING):
        signals, calls = gather._load_sprint_signals(scenario.program, scenario.workstreams, (), _NOW)
    assert signals == ()
    assert calls == 5
    assert "private-snapshot-provider-value" not in caplog.text
    assert scenario.program.id not in caplog.text


def test_injected_sprint_loader_keeps_existing_signature(scenario: SimpleNamespace) -> None:
    received: list[tuple[object, object]] = []

    def injected(program, workstreams, items, as_of, query_state_sink=None, previous_query_states=None):
        received.append((query_state_sink, previous_query_states))
        return (), 0

    artifacts = _gather(scenario, sprint_signal_loader=injected)
    assert len(received) == 1
    assert artifacts.integration_errors == ()
    assert gather._semantic_gather_exit_code(artifacts) == 0


def test_unexpected_capacity_exception_is_not_swallowed(scenario: SimpleNamespace) -> None:
    scenario.modes["Team Alpha"] = "unexpected"
    with pytest.raises(RuntimeError, match="unexpected defect"):
        _gather(scenario)


@pytest.mark.parametrize("source", ["sprints", "ado/sprint"])
def test_ingestion_accepts_channel_and_source_error_identity(source: str) -> None:
    class Recorder:
        def __init__(self) -> None:
            self.runs = []

        def initialize(self) -> None:
            pass

        def record_ingestion_run(self, run) -> None:
            self.runs.append(run)

    recorder = Recorder()
    record_optional_source_ingestion_runs(
        "synthetic",
        as_of=_NOW,
        include_workiq=False,
        include_analytics=False,
        include_sprints=True,
        include_pipelines=False,
        include_icm=False,
        signals=(),
        integration_error_details=(IntegrationError(source=source, stage="capacity", retryable=True, message="failed"),),
        store=recorder,
    )
    sprint = next(run for run in recorder.runs if run.source_ref == "ado/sprint")
    assert sprint.status == "failed"
    assert sprint.error_message == "failed"
