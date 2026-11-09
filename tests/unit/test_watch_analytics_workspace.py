from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.commands import gather, watch
from src.commands.watch_scan import watch_program_once
from src.core.catchup_runner import run_catchup
from src.core.catchup_scan import WatchSource
from src.core.exceptions import ConfigError, QueryError
from src.core.journal import read_signals
from src.core.models_v2 import ADOConfig, Program, Workstream


_NOW = datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)


def _write_queries(root: Path, query_id: str | None, *, shared: bool) -> None:
    directory = root.parent / "knowledge" if shared else root / "synthetic" / "knowledge"
    directory.mkdir(parents=True)
    if query_id is None:
        return
    (directory / "golden_queries.yaml").write_text(
        "schema_version: '1.0'\n"
        "queries:\n"
        f"  - id: {query_id}\n"
        "    engine: wiql\n"
        f"    wiql: SELECT [System.Id] FROM WorkItems WHERE [System.Tags] = '{query_id}'\n"
        "    validated: true\n",
        encoding="utf-8",
    )


@pytest.fixture
def scenario(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    selected = tmp_path / "selected" / "programs"
    default = tmp_path / "default" / "programs"
    _write_queries(default, "default_query", shared=False)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "db"))
    program = Program(
        schema_version="2.0",
        id="synthetic",
        name="Synthetic",
        maturity_level=2,
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
    calls: list[str] = []
    roots: list[Path] = []

    class FakeADOClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def query_work_item_snapshot(self, **_kwargs: object) -> list[dict[str, Any]]:
            return []

        def execute_wiql(self, wiql: str) -> list[int]:
            calls.append(wiql)
            return []

    real_load = gather.load_program_knowledge

    def load_knowledge(program_id: str, programs_root: Path) -> Any:
        roots.append(programs_root)
        return real_load(program_id, programs_root=programs_root)

    assert gather._load_analytics_signals.__kwdefaults__ is not None
    monkeypatch.setitem(gather._load_analytics_signals.__kwdefaults__, "programs_root", default)
    monkeypatch.setattr(gather, "ADOClient", FakeADOClient)
    monkeypatch.setattr(gather, "load_program_knowledge", load_knowledge)
    monkeypatch.setattr(gather, "_load_program_context", lambda *_args, **_kwargs: (program, workstreams))
    return SimpleNamespace(
        selected=selected, default=default, program=program, workstreams=workstreams, calls=calls, roots=roots
    )


def _poll(scenario: SimpleNamespace, route: str) -> tuple[int, int]:
    if route == "scan":
        result = watch_program_once(
            "synthetic",
            since=_NOW - timedelta(minutes=1),
            as_of=_NOW,
            sources=(WatchSource.ANALYTICS,),
            programs_root=scenario.selected,
            program_context=(scenario.program, scenario.workstreams),
            auto_approve_signals=False,
        )
        return result.ado_calls, result.new_signals
    elif route == "watch":
        summary = watch.watch_program(
            "synthetic",
            interval_seconds=60,
            sources=(WatchSource.ANALYTICS,),
            programs_root=scenario.selected,
            now_provider=lambda: _NOW,
            max_cycles=1,
        )
        assert summary.cycles == 1
        return summary.total_ado_calls, summary.total_new_signals
    else:
        result = run_catchup(
            "synthetic",
            sources=(WatchSource.ANALYTICS,),
            programs_root=scenario.selected,
            scan_func=watch_program_once,
            as_of=_NOW,
        )
        assert result.result.polled_at == _NOW
        return result.result.ado_calls, result.result.new_signals


@pytest.mark.parametrize("route", ["scan", "watch", "catchup"])
@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("selected_present", [False, True])
def test_analytics_poll_uses_only_selected_workspace_queries(
    scenario: SimpleNamespace, route: str, shared: bool, selected_present: bool
) -> None:
    _write_queries(scenario.selected, "selected_query" if selected_present else None, shared=shared)
    default_file = scenario.default / "synthetic" / "knowledge" / "golden_queries.yaml"
    default_bytes = default_file.read_bytes()
    default_files = {path.relative_to(scenario.default) for path in scenario.default.rglob("*") if path.is_file()}

    ado_calls, new_signals = _poll(scenario, route)

    assert scenario.roots == [scenario.selected]
    assert len(scenario.calls) == int(selected_present)
    assert all("selected_query" in query for query in scenario.calls)
    assert (ado_calls, new_signals) == (1 + int(selected_present), int(selected_present))
    signals = tuple(read_signals("synthetic", programs_root=scenario.selected))
    assert [signal.metadata["query_id"] for signal in signals] == (["selected_query"] if selected_present else [])
    assert default_file.read_bytes() == default_bytes
    assert tuple(read_signals("synthetic", programs_root=scenario.default)) == ()
    assert {path.relative_to(scenario.default) for path in scenario.default.rglob("*") if path.is_file()} == default_files


def test_analytics_poll_preserves_selected_workspace_shared_knowledge_precedence(
    scenario: SimpleNamespace
) -> None:
    _write_queries(scenario.selected, "local_query", shared=False)
    _write_queries(scenario.selected, "shared_query", shared=True)

    ado_calls, new_signals = _poll(scenario, "scan")

    assert scenario.roots == [scenario.selected]
    assert len(scenario.calls) == 1 and "shared_query" in scenario.calls[0]
    assert (ado_calls, new_signals) == (2, 1)
    assert [signal.metadata["query_id"] for signal in read_signals("synthetic", programs_root=scenario.selected)] == [
        "shared_query"
    ]


def test_analytics_poll_preserves_selected_workspace_local_fallback(scenario: SimpleNamespace) -> None:
    _write_queries(scenario.selected, "local_query", shared=False)
    (scenario.selected.parent / "knowledge").mkdir(parents=True)

    ado_calls, new_signals = _poll(scenario, "scan")

    assert scenario.roots == [scenario.selected]
    assert len(scenario.calls) == 1 and "local_query" in scenario.calls[0]
    assert (ado_calls, new_signals) == (2, 1)


def test_invalid_selected_knowledge_does_not_fall_back_to_default(scenario: SimpleNamespace) -> None:
    directory = scenario.selected / "synthetic" / "knowledge"
    directory.mkdir(parents=True)
    (directory / "golden_queries.yaml").write_text("- invalid-top-level\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="Expected mapping"):
        _poll(scenario, "scan")

    assert scenario.roots == [scenario.selected]
    assert scenario.calls == []
    assert tuple(read_signals("synthetic", programs_root=scenario.selected)) == ()


@pytest.mark.parametrize("route", ["scan", "watch", "catchup"])
def test_analytics_query_failure_keeps_existing_error_behavior(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    _write_queries(scenario.selected, "selected_query", shared=False)

    class FailedADOClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def query_work_item_snapshot(self, **_kwargs: object) -> list[dict[str, Any]]:
            return []

        def execute_wiql(self, wiql: str) -> list[int]:
            assert "selected_query" in wiql
            raise QueryError("synthetic query failure")

    monkeypatch.setattr(gather, "ADOClient", FailedADOClient)
    with pytest.raises(QueryError, match="synthetic query failure"):
        _poll(scenario, route)

    assert scenario.roots == [scenario.selected]
    assert tuple(read_signals("synthetic", programs_root=scenario.selected)) == ()


def test_analytics_poll_keeps_implicit_default_workspace(
    scenario: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert watch_program_once.__kwdefaults__ is not None
    monkeypatch.setitem(watch_program_once.__kwdefaults__, "programs_root", scenario.default)
    result = watch_program_once(
        "synthetic",
        since=_NOW - timedelta(minutes=1),
        as_of=_NOW,
        sources=(WatchSource.ANALYTICS,),
        program_context=(scenario.program, scenario.workstreams),
        auto_approve_signals=False,
    )

    assert scenario.roots == [scenario.default]
    assert len(scenario.calls) == 1 and "default_query" in scenario.calls[0]
    assert result.new_signals == 1 and result.ado_calls == 2
