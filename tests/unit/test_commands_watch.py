from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner
import typer

from cli import app
from src.commands import watch
from src.core.exceptions import ConfigError
from src.core.journal import read_review_log, read_signals
from src.core.models import Confidence, Revision, RiskLevel, WorkItem
from src.core.models_v2 import ADOConfig, KustoConfig, Program, Signal, Workstream
from src.core.sqlite_stores import SQLiteSignalStore, SQLiteTrajectoryStore
from src.core.trajectory import read_trajectory
from src.m365.agency_bridge import AgencyCapabilities


runner = CliRunner()


def test_watch_program_once_appends_incremental_ado_signals(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Acme",
        maturity_level=2,
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    since = datetime(2026, 5, 11, 10, 0, tzinfo=timezone.utc)
    as_of = datetime(2026, 5, 11, 10, 1, tzinfo=timezone.utc)
    item = WorkItem(
        id=1234,
        type="Feature",
        title="Ramp checkpoint",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\FY26\\Q4",
        target_date=date(2026, 5, 17),
        risk_level=RiskLevel.MEDIUM,
        tags=("acme",),
        custom_fields={},
        revisions=[
            Revision(
                work_item_id=1234,
                rev_number=7,
                changed_by="priya@example.com",
                changed_by_email="priya@example.com",
                changed_date=datetime(2026, 5, 11, 10, 0, 30, tzinfo=timezone.utc),
                fields_changed={"System.State": ("Proposed", "Active")},
            ),
        ],
        comments=[],
        fetched_at=as_of,
    )

    result = watch.watch_program_once(
        "acme",
        since=since,
        as_of=as_of,
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, since: ((item,), 4),
        program_context=(program, workstreams),
    )

    signals = read_signals("acme", programs_root=programs_root)
    reviews = read_review_log("acme", programs_root=programs_root)
    trajectory = read_trajectory("acme", 1234, programs_root=programs_root)

    assert result.scanned_items == 1
    assert result.discovered_signals == 1
    assert result.new_signals == 1
    assert result.auto_reviews_written == 1
    assert result.trajectory_updates == 1
    assert result.ado_calls == 4
    assert len(signals) == 1
    assert len(reviews) == 1
    assert len(trajectory) == 1

    second = watch.watch_program_once(
        "acme",
        since=since,
        as_of=as_of,
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, since: ((item,), 4),
        program_context=(program, workstreams),
    )

    assert second.new_signals == 0
    assert second.auto_reviews_written == 0
    assert second.trajectory_updates == 0


def test_watch_program_requires_l2_maturity(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Acme",
        maturity_level=1,
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )

    with pytest.raises(ConfigError, match="requires maturity_level >= 2"):
        watch.watch_program_once(
            "acme",
            since=datetime(2026, 5, 11, 10, 0, tzinfo=timezone.utc),
            as_of=datetime(2026, 5, 11, 10, 1, tzinfo=timezone.utc),
            programs_root=programs_root,
            loader=lambda program, workstreams, as_of, since: ((), 0),
            program_context=(program, workstreams),
        )


def test_watch_program_once_uses_sqlite_stores_when_program_configured(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Acme",
        maturity_level=2,
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        storage_backend="sqlite",
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    since = datetime(2026, 5, 11, 10, 0, tzinfo=timezone.utc)
    as_of = datetime(2026, 5, 11, 10, 1, tzinfo=timezone.utc)
    item = WorkItem(
        id=1234,
        type="Feature",
        title="Ramp checkpoint",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\FY26\\Q4",
        target_date=date(2026, 5, 17),
        risk_level=RiskLevel.MEDIUM,
        tags=("acme",),
        custom_fields={},
        revisions=[
            Revision(
                work_item_id=1234,
                rev_number=7,
                changed_by="priya@example.com",
                changed_by_email="priya@example.com",
                changed_date=datetime(2026, 5, 11, 10, 0, 30, tzinfo=timezone.utc),
                fields_changed={"System.State": ("Proposed", "Active")},
            ),
        ],
        comments=[],
        fetched_at=as_of,
    )

    result = watch.watch_program_once(
        "acme",
        since=since,
        as_of=as_of,
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, since: ((item,), 4),
        program_context=(program, workstreams),
    )

    signal_store = SQLiteSignalStore(programs_root=programs_root)
    trajectory_store = SQLiteTrajectoryStore(programs_root=programs_root)

    assert result.new_signals == 1
    assert result.auto_reviews_written == 1
    assert result.trajectory_updates == 1
    assert len(signal_store.read("acme")) == 1
    assert len(signal_store.read_reviews("acme")) == 1
    assert len(trajectory_store.read("acme", 1234)) == 1
    assert read_signals("acme", programs_root=programs_root) == ()
    assert read_trajectory("acme", 1234, programs_root=programs_root) == ()

    second = watch.watch_program_once(
        "acme",
        since=since,
        as_of=as_of,
        programs_root=programs_root,
        loader=lambda program, workstreams, as_of, since: ((item,), 4),
        program_context=(program, workstreams),
    )

    assert second.new_signals == 0
    assert second.auto_reviews_written == 0
    assert second.trajectory_updates == 0


def test_watch_program_fails_fast_when_workiq_source_is_not_configured(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Acme",
        maturity_level=2,
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    loader_calls: list[str] = []

    monkeypatch.setattr(watch.gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))

    def _unexpected_full_loader(program, workstreams, as_of, since):
        loader_calls.append("full")
        return (), 0

    with pytest.raises(typer.BadParameter, match="source 'workiq' requires enabled m365.workiq_queries"):
        watch.watch_program(
            "acme",
            interval_seconds=60,
            sources=(watch.WatchSource.WORKIQ,),
            programs_root=programs_root,
            full_loader=_unexpected_full_loader,
            max_cycles=1,
        )

    assert loader_calls == []


def test_watch_program_fails_fast_when_kusto_source_has_no_queries(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Acme",
        maturity_level=2,
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        kusto=KustoConfig(enabled=True),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )

    monkeypatch.setattr(watch.gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(watch.gather, "_load_kusto_queries", lambda program_id, program, programs_root: ())

    with pytest.raises(typer.BadParameter, match="source 'kusto' requires at least one applicable non-IcM query"):
        watch.watch_program(
            "acme",
            interval_seconds=60,
            sources=(watch.WatchSource.KUSTO,),
            programs_root=programs_root,
            max_cycles=1,
        )


def test_watch_program_fails_fast_when_icm_source_has_no_available_path(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Acme",
        maturity_level=2,
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )

    class _UnavailableAgencyBridge:
        def probe(self) -> AgencyCapabilities:
            return AgencyCapabilities(available=False)

    monkeypatch.setattr(watch.gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(watch, "AgencyBridge", _UnavailableAgencyBridge)

    with pytest.raises(typer.BadParameter, match="source 'icm' requires Agency IcM access or program.kusto.enabled with an applicable IcM query"):
        watch.watch_program(
            "acme",
            interval_seconds=60,
            sources=(watch.WatchSource.ICM,),
            programs_root=programs_root,
            max_cycles=1,
        )


def test_watch_cli_is_registered(monkeypatch, tmp_path: Path) -> None:
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        watch,
        "watch_program",
        lambda program_id, interval_seconds, sources, cadence, emit: observed.update(
            {
                "program_id": program_id,
                "interval_seconds": interval_seconds,
                "cadence": cadence,
                "sources": sources,
            }
        )
        or watch.WatchRunSummary(
            program_id=program_id,
            interval_seconds=interval_seconds,
            cycles=1,
            total_new_signals=2,
            total_auto_reviews_written=2,
            total_trajectory_updates=1,
            total_ado_calls=5,
            last_polled_at=datetime(2026, 5, 11, 10, 1, tzinfo=timezone.utc),
        ),
    )

    result = runner.invoke(app, ["watch", "--program", "acme", "--interval", "60"])

    assert result.exit_code == 0
    assert observed == {
        "program_id": "acme",
        "interval_seconds": 60,
        "cadence": watch.WatchCadence.INTRADAY,
        "sources": (),
    }
    assert "Watch stopped for acme after 1 poll(s): 2 new signal(s), 2 auto-review(s), 1 trajectory update(s), 5 ADO call(s)." in result.stdout


def test_watch_cli_accepts_multiple_sources(monkeypatch) -> None:
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        watch,
        "watch_program",
        lambda program_id, interval_seconds, sources, cadence, emit: observed.update({"sources": sources, "cadence": cadence})
        or watch.WatchRunSummary(
            program_id=program_id,
            interval_seconds=interval_seconds,
            cycles=1,
            total_new_signals=0,
            total_auto_reviews_written=0,
            total_trajectory_updates=0,
            total_ado_calls=0,
            last_polled_at=datetime(2026, 5, 11, 10, 1, tzinfo=timezone.utc),
        ),
    )

    result = runner.invoke(app, ["watch", "--program", "acme", "--source", "workiq,kusto", "--source", "icm"])

    assert result.exit_code == 0
    assert observed["cadence"] == watch.WatchCadence.INTRADAY
    assert observed["sources"] == (
        watch.WatchSource.WORKIQ,
        watch.WatchSource.KUSTO,
        watch.WatchSource.ICM,
    )


def test_watch_cli_passes_daily_cadence(monkeypatch) -> None:
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        watch,
        "watch_program",
        lambda program_id, interval_seconds, sources, cadence, emit: observed.update(
            {
                "program_id": program_id,
                "interval_seconds": interval_seconds,
                "sources": sources,
                "cadence": cadence,
            }
        )
        or watch.WatchRunSummary(
            program_id=program_id,
            interval_seconds=interval_seconds,
            cycles=1,
            total_new_signals=0,
            total_auto_reviews_written=0,
            total_trajectory_updates=0,
            total_ado_calls=0,
            last_polled_at=datetime(2026, 5, 11, 10, 1, tzinfo=timezone.utc),
        ),
    )

    result = runner.invoke(app, ["watch", "--program", "acme", "--cadence", "daily"])

    assert result.exit_code == 0
    assert observed["cadence"] is watch.WatchCadence.DAILY
    assert observed["sources"] == ()


def test_watch_program_defaults_intraday_sources_to_ado_and_icm_when_icm_ready(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Acme",
        maturity_level=2,
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
        kusto=KustoConfig(enabled=True),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    observed: dict[str, object] = {}

    monkeypatch.setattr(watch.gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(watch, "_load_watch_kusto_queries", lambda program_id, program, programs_root: ((object(),), None))
    monkeypatch.setattr(watch.gather, "_is_icm_query", lambda query: True)
    monkeypatch.setattr(
        watch,
        "watch_program_once",
        lambda program_id, **kwargs: observed.update({"sources": kwargs["sources"], "cadence": kwargs["cadence"]})
        or watch.WatchPollResult(
            program_id=program_id,
            since=datetime(2026, 5, 11, 10, 0, tzinfo=timezone.utc),
            polled_at=datetime(2026, 5, 11, 10, 1, tzinfo=timezone.utc),
            scanned_items=0,
            discovered_signals=0,
            new_signals=0,
            auto_reviews_written=0,
            trajectory_updates=0,
            ado_calls=0,
        ),
    )

    summary = watch.watch_program(
        "acme",
        interval_seconds=60,
        programs_root=programs_root,
        max_cycles=1,
    )

    assert summary.cycles == 1
    assert observed["cadence"] is watch.WatchCadence.INTRADAY
    assert observed["sources"] == (watch.WatchSource.ADO, watch.WatchSource.ICM)


def test_watch_program_defaults_intraday_sources_to_ado_only_when_icm_not_ready(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Acme",
        maturity_level=2,
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    observed: dict[str, object] = {}

    monkeypatch.setattr(watch.gather, "_load_program_context", lambda program_id, programs_root: (program, workstreams))
    monkeypatch.setattr(
        watch,
        "watch_program_once",
        lambda program_id, **kwargs: observed.update({"sources": kwargs["sources"], "cadence": kwargs["cadence"]})
        or watch.WatchPollResult(
            program_id=program_id,
            since=datetime(2026, 5, 11, 10, 0, tzinfo=timezone.utc),
            polled_at=datetime(2026, 5, 11, 10, 1, tzinfo=timezone.utc),
            scanned_items=0,
            discovered_signals=0,
            new_signals=0,
            auto_reviews_written=0,
            trajectory_updates=0,
            ado_calls=0,
        ),
    )

    summary = watch.watch_program(
        "acme",
        interval_seconds=60,
        programs_root=programs_root,
        max_cycles=1,
    )

    assert summary.cycles == 1
    assert observed["cadence"] is watch.WatchCadence.INTRADAY
    assert observed["sources"] == (watch.WatchSource.ADO,)


def test_watch_program_once_daily_cadence_uses_freshness_and_dependency_signals(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Acme",
        maturity_level=2,
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    as_of = datetime(2026, 5, 11, 10, 1, tzinfo=timezone.utc)
    item = WorkItem(
        id=1234,
        type="Feature",
        title="Ramp checkpoint",
        state="Active",
        assigned_to="Priya",
        assigned_to_email="priya@example.com",
        area_path="One\\Adventure\\Acme",
        iteration_path="One\\FY26\\Q4",
        target_date=date(2026, 5, 17),
        risk_level=RiskLevel.MEDIUM,
        tags=("acme",),
        custom_fields={},
        revisions=(),
        comments=[],
        fetched_at=as_of,
    )
    called: dict[str, object] = {}

    monkeypatch.setattr(
        watch.gather,
        "_build_ado_revision_signals",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("revision path should not run for daily cadence")),
    )
    monkeypatch.setattr(watch.gather, "_load_freshness_thresholds", lambda program_id, programs_root: (14, 30))
    def _fake_build_freshness_signals(items, **kwargs):
        del kwargs
        called["freshness_items"] = tuple(items)
        return (
            Signal(
                id="fresh-1",
                timestamp=as_of,
                source="vertex/freshness",
                program_id="acme",
                workstream_id="acme",
                entity_refs=("WI:1234",),
                text="Freshness warning",
                raw_ref="wi:1234:freshness:FR-21:2026-05-11",
                confidence=Confidence.HIGH,
            ),
        )

    def _fake_build_dependency_signals(dependency_items, **kwargs):
        del kwargs
        called["dependency_item_count"] = sum(len(group.items) for group in dependency_items)
        return (
            Signal(
                id="dep-1",
                timestamp=as_of,
                source="ado/dependency",
                program_id="acme",
                workstream_id="acme",
                entity_refs=("WI:2234",),
                text="Dependency stale",
                raw_ref="dependency:OneDeploy stager:2234:FR-21:2026-05-11",
                confidence=Confidence.HIGH,
            ),
        )

    monkeypatch.setattr(watch.gather, "_build_freshness_signals", _fake_build_freshness_signals)
    monkeypatch.setattr(watch.gather, "_build_dependency_signals", _fake_build_dependency_signals)

    dependency_group = watch.gather._DependencyQueryItems(
        workstream_id="acme",
        label="OneDeploy stager",
        resolution_path="cross_org_onedeploy",
        items=(replace(item, id=2234, title="Dependency item"),),
    )

    result = watch.watch_program_once(
        "acme",
        since=datetime(2026, 5, 11, 9, 0, tzinfo=timezone.utc),
        as_of=as_of,
        cadence=watch.WatchCadence.DAILY,
        programs_root=programs_root,
        freshness_loader=lambda program, workstreams, as_of: ((item,), 2),
        dependency_loader=lambda program, workstreams, as_of: ((dependency_group,), 3),
        program_context=(program, workstreams),
    )

    signals = read_signals("acme", programs_root=programs_root)

    assert result.discovered_signals == 2
    assert result.new_signals == 2
    assert result.ado_calls == 5
    assert result.trajectory_updates == 1
    assert called["freshness_items"] == (item,)
    assert called["dependency_item_count"] == 1
    assert {signal.source for signal in signals} == {"vertex/freshness", "ado/dependency"}


def test_watch_program_once_dedupes_workiq_signals_across_polls(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program = Program(
        schema_version="2.0",
        id="acme",
        name="Acme",
        maturity_level=2,
        ado=ADOConfig(
            organization="your-org",
            project="One",
            area_paths=("One\\Adventure\\Acme",),
            work_item_types=("Feature",),
            excluded_states=("Removed",),
            date_window_days=14,
            api_timeout_seconds=30,
        ),
    )
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            area_paths=("One\\Adventure\\Acme",),
            dri_email="maintainer@example.com",
        ),
    )
    since = datetime(2026, 5, 11, 10, 0, tzinfo=timezone.utc)
    as_of = datetime(2026, 5, 11, 10, 1, tzinfo=timezone.utc)
    signal = Signal(
        id="workiq-1",
        timestamp=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
        source="workiq/email",
        program_id="acme",
        workstream_id=None,
        entity_refs=(),
        text="WorkIQ Email: deployment packet follow-up",
        raw_ref="workiq:email:message-1",
        confidence=Confidence.MEDIUM,
        metadata=None,
        thread_id="thread-1",
    )
    full_loader_calls: list[datetime | None] = []

    monkeypatch.setattr(
        watch.gather,
        "_build_workiq_signals",
        lambda **_: (signal,),
    )

    first = watch.watch_program_once(
        "acme",
        since=since,
        as_of=as_of,
        sources=(watch.WatchSource.WORKIQ,),
        programs_root=programs_root,
        full_loader=lambda program, workstreams, as_of, since: (full_loader_calls.append(since) or (), 2),
        program_context=(program, workstreams),
    )

    second = watch.watch_program_once(
        "acme",
        since=since,
        as_of=as_of,
        sources=(watch.WatchSource.WORKIQ,),
        programs_root=programs_root,
        full_loader=lambda program, workstreams, as_of, since: (full_loader_calls.append(since) or (), 2),
        program_context=(program, workstreams),
    )

    signals = read_signals("acme", programs_root=programs_root)

    assert first.scanned_items == 0
    assert first.discovered_signals == 1
    assert first.new_signals == 1
    assert first.trajectory_updates == 0
    assert first.ado_calls == 2
    assert second.discovered_signals == 1
    assert second.new_signals == 0
    assert second.auto_reviews_written == 0
    assert second.trajectory_updates == 0
    assert full_loader_calls == [None, None]
    assert len(signals) == 1