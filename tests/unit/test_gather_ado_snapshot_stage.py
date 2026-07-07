"""Direct coverage for the extracted ADO snapshot gather stage."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast
import pytest

from src.commands.gather_pipeline import ado_snapshot_stage
from src.core.exceptions import QueryError
from src.core.models import Confidence, WorkItem
from src.core.models_v2 import Signal


def test_build_analytics_snapshot_filter_escapes_values() -> None:
    ado = SimpleNamespace(
        area_paths=("One\\Retail\\Bob's",),
        work_item_types=("Feature", "User's Story"),
        excluded_states=("Removed", "Won't Fix"),
    )

    filter_expression = ado_snapshot_stage.build_analytics_snapshot_filter(
        ado,
        start_date_sk=20260501,
        end_date_sk=20260512,
    )

    assert "startswith(Area/AreaPath, 'One\\Retail\\Bob''s')" in filter_expression
    assert "WorkItemType eq 'User''s Story'" in filter_expression
    assert "State eq 'Won''t Fix'" in filter_expression
    assert "IsLastRevisionOfDay eq true" in filter_expression


def test_build_analytics_snapshot_filter_without_last_revision_of_day() -> None:
    ado = SimpleNamespace(
        area_paths=("One\\Adventure\\Acme",),
        work_item_types=("Feature",),
        excluded_states=("Removed",),
    )

    filter_expression = ado_snapshot_stage.build_analytics_snapshot_filter(
        ado,
        start_date_sk=20260501,
        end_date_sk=20260512,
        include_last_revision_of_day=False,
    )

    assert "IsLastRevisionOfDay" not in filter_expression
    assert "DateSK ge 20260501" in filter_expression
    assert "DateSK le 20260512" in filter_expression
    assert "WorkItemType eq 'Feature'" in filter_expression


def test_load_analytics_signals_retries_without_last_revision_on_vs403522() -> None:
    ado = SimpleNamespace(
        area_paths=("One\\Adventure\\Acme",),
        work_item_types=("Feature",),
        excluded_states=("Removed",),
        api_timeout_seconds=30,
        organization="contoso",
        project="One",
        date_window_days=14,
    )

    class _ProgAdo:
        area_paths = ("One\\Adventure\\Acme",)
        work_item_types = ("Feature",)
        excluded_states = ("Removed",)
        api_timeout_seconds = 30
        organization = "contoso"
        project = "One"
        date_window_days = 14

    class _Prog:
        id = "acme"
        ado = _ProgAdo()

    call_filters: list[str] = []

    class _FakeClient:
        def query_work_item_snapshot(self, *, filter_expression: str, select_fields: Any) -> list[dict[str, Any]]:
            call_filters.append(filter_expression)
            if "IsLastRevisionOfDay" in filter_expression:
                raise QueryError("VS403522: The property 'IsLastRevisionOfDay' is not available")
            return []

    def _fake_client_factory(**kwargs: Any) -> _FakeClient:
        return _FakeClient()

    ado_snapshot_stage.load_analytics_signals(
        cast(Any, _Prog()),
        workstreams=(),
        as_of=datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc),
        programs_root=__import__("pathlib").Path("."),
        ado_client_factory=_fake_client_factory,
        date_to_sk_fn=lambda d: int(d.strftime("%Y%m%d")),
        analytics_snapshot_fields=("DateSK", "WorkItemId"),
        build_analytics_signals_fn=lambda **kw: (),
        load_wiql_golden_query_signals_fn=lambda *a, **kw: ((), 0),
        expected_max_age_hours=48,
    )

    assert len(call_filters) == 2
    assert "IsLastRevisionOfDay" in call_filters[0]
    assert "IsLastRevisionOfDay" not in call_filters[1]


def test_load_analytics_signals_retries_across_multiple_unavailable_fields() -> None:
    """Deterministically strips both IsLastRevisionOfDay AND a named unavailable
    select field across successive attempts, rather than only handling one."""

    class _ProgAdo:
        area_paths = ("One\\Adventure\\Acme",)
        work_item_types = ("Feature",)
        excluded_states = ("Removed",)
        api_timeout_seconds = 30
        organization = "contoso"
        project = "One"
        date_window_days = 14

    class _Prog:
        id = "acme"
        ado = _ProgAdo()

    calls: list[tuple[str, tuple[str, ...]]] = []

    class _FakeClient:
        def query_work_item_snapshot(self, *, filter_expression: str, select_fields: tuple[str, ...]) -> list[dict[str, Any]]:
            calls.append((filter_expression, select_fields))
            if "IsLastRevisionOfDay" in filter_expression:
                raise QueryError("VS403522: The property 'IsLastRevisionOfDay' is not available")
            if "CycleTimeDays" in select_fields:
                raise QueryError("VS403522: The property 'CycleTimeDays' is not available")
            return [{"DateSK": 20260512, "WorkItemId": 101}]

    def _fake_client_factory(**kwargs: Any) -> _FakeClient:
        return _FakeClient()

    captured_rows: list[Any] = []

    ado_snapshot_stage.load_analytics_signals(
        cast(Any, _Prog()),
        workstreams=(),
        as_of=datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc),
        programs_root=__import__("pathlib").Path("."),
        ado_client_factory=_fake_client_factory,
        date_to_sk_fn=lambda d: int(d.strftime("%Y%m%d")),
        analytics_snapshot_fields=("DateSK", "WorkItemId", "CycleTimeDays"),
        build_analytics_signals_fn=lambda **kw: captured_rows.append(kw["rows"]) or (),
        load_wiql_golden_query_signals_fn=lambda *a, **kw: ((), 0),
        expected_max_age_hours=48,
    )

    assert len(calls) == 3
    assert "IsLastRevisionOfDay" in calls[0][0] and calls[0][1] == ("DateSK", "WorkItemId", "CycleTimeDays")
    assert "IsLastRevisionOfDay" not in calls[1][0] and calls[1][1] == ("DateSK", "WorkItemId", "CycleTimeDays")
    assert "IsLastRevisionOfDay" not in calls[2][0] and calls[2][1] == ("DateSK", "WorkItemId")
    assert captured_rows == [[{"DateSK": 20260512, "WorkItemId": 101}]]


def test_load_analytics_signals_reraises_when_vs403522_field_already_removed() -> None:
    """If the same unavailable field keeps recurring (no further field to strip),
    the retry loop must give up instead of looping forever."""

    class _ProgAdo:
        area_paths = ("One\\Adventure\\Acme",)
        work_item_types = ("Feature",)
        excluded_states = ("Removed",)
        api_timeout_seconds = 30
        organization = "contoso"
        project = "One"
        date_window_days = 14

    class _Prog:
        id = "acme"
        ado = _ProgAdo()

    call_count = 0

    class _FakeClient:
        def query_work_item_snapshot(self, *, filter_expression: str, select_fields: tuple[str, ...]) -> list[dict[str, Any]]:
            nonlocal call_count
            call_count += 1
            raise QueryError("VS403522: The property 'SomeGhostField' is not available")

    def _fake_client_factory(**kwargs: Any) -> _FakeClient:
        return _FakeClient()

    with pytest.raises(QueryError):
        ado_snapshot_stage.load_analytics_signals(
            cast(Any, _Prog()),
            workstreams=(),
            as_of=datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc),
            programs_root=__import__("pathlib").Path("."),
            ado_client_factory=_fake_client_factory,
            date_to_sk_fn=lambda d: int(d.strftime("%Y%m%d")),
            analytics_snapshot_fields=("DateSK", "WorkItemId"),
            build_analytics_signals_fn=lambda **kw: (),
            load_wiql_golden_query_signals_fn=lambda *a, **kw: ((), 0),
            expected_max_age_hours=48,
        )

    # "SomeGhostField" is never in select_fields and never IsLastRevisionOfDay,
    # so the loop can't make progress and must abort on the very first retry
    # attempt rather than exhausting all _MAX_VS403522_RETRIES.
    assert call_count == 1


def test_query_snapshot_rows_by_item_ids_prefers_latest_revision_and_hydrates_paths() -> None:
    class _FakeADOClient:
        def query_odata_all(self, entity_set: str, params: dict[str, str]) -> list[dict[str, object]]:
            assert entity_set == "WorkItemSnapshot"
            assert params["$expand"] == "Iteration"
            return [
                {
                    "DateSK": 20260512,
                    "WorkItemId": 101,
                    "Revision": 2,
                    "State": "Active",
                    "Iteration": {"IterationPath": "One\\Sprint 24"},
                },
                {
                    "DateSK": 20260512,
                    "WorkItemId": 101,
                    "Revision": 3,
                    "State": "Closed",
                    "Iteration": {"IterationPath": "One\\Sprint 24"},
                },
            ]

    rows, ado_calls = ado_snapshot_stage.query_snapshot_rows_by_item_ids(
        cast(Any, _FakeADOClient()),
        (
            cast(
                WorkItem,
                SimpleNamespace(id=101, area_path="One\\Retail", iteration_path="One\\Sprint 24"),
            ),
        ),
        ado=SimpleNamespace(work_item_types=("Feature",), excluded_states=("Removed",)),
        start_date_sk=20260501,
        end_date_sk=20260512,
        select_fields=("DateSK", "WorkItemId", "AreaPath", "IterationPath", "State"),
        expand_fields=("Iteration",),
        snapshot_item_filter_batch_size=50,
    )

    assert ado_calls == 1
    assert rows == [
        {
            "DateSK": 20260512,
            "WorkItemId": 101,
            "Revision": 3,
            "State": "Closed",
            "Iteration": {"IterationPath": "One\\Sprint 24"},
            "AreaPath": "One\\Retail",
            "IterationPath": "One\\Sprint 24",
        }
    ]


def test_record_ado_sprint_query_state_tracks_history_and_freshness() -> None:
    query_state_sink: dict[str, dict[str, object]] = {}
    signal = Signal(
        id="sig-1",
        timestamp=datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WS:deployment_readiness",),
        text="Sprint 24: 3 committed, 1 open",
        raw_ref="ado_sprint:sig-1",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_id": "iteration-24",
            "iteration_name": "Sprint 24",
            "iteration_path": "One\\Sprint 24",
            "open_item_count": 1,
            "committed_item_count": 3,
            "completion_pct": 66.7,
            "open_history": {"2026-05-11": 2, "2026-05-12": 1},
            "completed_history": {"2026-05-11": 1, "2026-05-12": 2},
            "pace_status": "on_track",
            "projection_status": "finish",
        },
    )

    ado_snapshot_stage.record_ado_sprint_query_state(
        query_state_sink,
        signal,
        as_of=datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc),
        previous_state={"value_last_4": [1.0, 1.0, 1.0]},
        expected_max_age_hours=48,
    )

    state = query_state_sink["ado-sprint:deployment_readiness:iteration-24"]
    assert state["value_last_4"] == [1.0, 1.0, 1.0, 1.0]
    assert state["value_frozen_warning"] is True
    assert state["data_freshness_ok"] is True
    assert state["pace_status"] == "on_track"
