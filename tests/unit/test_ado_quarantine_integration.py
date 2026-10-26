from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.commands import gather
from src.commands.gather_pipeline import ado_wiql_stage, channel_runtime
from src.core.ado_client import ADOClient
from src.core.ado_discovery import expand_with_linked_items
from src.core.gather_channel_support import append_integration_error_once
from src.core.integration_types import (
    ADOHydrationOutput, ChannelBinding, ChannelConfig, HydrationResult,
    IntegrationError, RunContext,
)
from src.core.models_v2 import ADOConfig, IntegrationError as GatherError, KustoQuery, Program


AS_OF = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)


def _program() -> Program:
    return Program(
        schema_version="2.0", id="fixture", name="Fixture",
        ado=ADOConfig(
            organization="fixture", project="Project", area_paths=("Project\\Scope",),
            work_item_types=("Feature",), excluded_states=("Removed",), date_window_days=14,
        ),
    )


def _query() -> KustoQuery:
    return KustoQuery(
        id="fixture-query", cluster="", database="", kql="", section="Section A",
        render_as="table", confidence="high", engine="wiql",
        wiql="SELECT [System.Id] FROM WorkItems",
    )


class _GraphClient(ADOClient):
    def __init__(self, *, with_known: bool = True) -> None:
        self.with_known = with_known

    def execute_wiql(self, wiql: str, top: int | None = None) -> list[int]:
        return [101]

    def query_work_item_snapshot(self, **kwargs: object) -> list[dict[str, object]]:
        return []

    def get_work_item_relations(self, work_item_ids: list[int]) -> list[dict[str, object]]:
        relations = [{
            "rel": "Custom.Future-Forward",
            "url": "https://dev.azure.com/fixture/Project/_apis/wit/workItems/103",
        }]
        if self.with_known:
            relations.append({
                "rel": "System.LinkTypes.Dependency-Forward",
                "url": "https://dev.azure.com/fixture/Project/_apis/wit/workItems/102",
            })
        return [{"id": item_id, "relations": relations} for item_id in work_item_ids]


@pytest.mark.parametrize("with_known", [False, True])
@pytest.mark.parametrize("with_sink", [False, True])
def test_wiql_graph_surfaces_quarantine_without_losing_valid_membership(
    with_known: bool, with_sink: bool,
) -> None:
    errors: list[GatherError] = []
    signals, _ = ado_wiql_stage.load_wiql_golden_query_signals(
        _program(), (), AS_OF, queries=(_query(),),
        ado_client_factory=lambda **kwargs: _GraphClient(with_known=with_known),
        normalize_ado_team_name_fn=lambda value: value,
        expand_with_linked_items_fn=expand_with_linked_items,
        integration_error_sink=errors if with_sink else None,
    )
    assert signals[0].metadata["work_item_ids"] == ("101",)
    graph = signals[1]
    assert graph.source == "ado/graph"
    assert graph.entity_refs == (("WI:102",) if with_known else ())
    assert graph.metadata["is_degraded"] is True
    assert graph.metadata["quarantined_relation_count"] == 1
    assert graph.metadata["relation_errors_truncated"] is False
    assert "quarantined" in graph.text
    detail = graph.metadata["relation_errors"][0]
    assert detail["retryable"] is False
    assert detail["ref_id"] == "101"
    assert "Custom.Future-Forward" in detail["message"]
    assert len(errors) == int(with_sink)
    if with_sink:
        assert isinstance(errors[0], GatherError)
        assert errors[0].retryable is False
        assert errors[0].operator_action == detail["operator_action"]


def test_actual_analytics_wrappers_forward_quarantine_to_existing_error_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gather, "ADOClient", lambda **kwargs: _GraphClient())
    monkeypatch.setattr(gather, "_load_ado_wiql_queries", Mock(return_value=(_query(),)))
    errors: list[GatherError] = []
    signals, _ = gather._load_analytics_signals(
        _program(), (), AS_OF, programs_root=tmp_path, integration_error_sink=errors,
    )
    assert signals[-1].source == "ado/graph"
    assert signals[-1].entity_refs == ("WI:102",)
    assert len(errors) == 1
    assert errors[0].stage == "relations"
    assert errors[0].retryable is False
    assert "quarantined" in errors[0].message
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("count", [25, 26])
def test_graph_quarantine_display_cap_is_explicit_and_preserves_full_error_sink(count: int) -> None:
    class _ManyUnknownClient(_GraphClient):
        def get_work_item_relations(self, work_item_ids: list[int]) -> list[dict[str, object]]:
            return [{
                "id": item_id,
                "relations": [{
                    "rel": f"Custom.Future{index}",
                    "url": f"https://dev.azure.com/fixture/Project/_apis/wit/workItems/{index + 200}",
                } for index in range(count)],
            } for item_id in work_item_ids]

    errors: list[GatherError] = []
    signals, _ = ado_wiql_stage.load_wiql_golden_query_signals(
        _program(), (), AS_OF, queries=(_query(),),
        ado_client_factory=lambda **kwargs: _ManyUnknownClient(),
        normalize_ado_team_name_fn=lambda value: value,
        expand_with_linked_items_fn=expand_with_linked_items,
        integration_error_sink=errors,
    )
    graph = signals[1]
    assert graph.entity_refs == ()
    assert graph.metadata["quarantined_relation_count"] == count
    assert len(graph.metadata["relation_errors"]) == min(count, 25)
    assert graph.metadata["relation_errors_truncated"] is (count > 25)
    assert len(errors) == count


def test_gather_collector_wires_its_owned_integration_error_list() -> None:
    tree = ast.parse(Path(gather.__file__).read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_load_analytics_signals"
    ]
    assert len(calls) == 1
    assert any(
        keyword.arg == "integration_error_sink"
        and isinstance(keyword.value, ast.Name)
        and keyword.value.id == "integration_error_details"
        for keyword in calls[0].keywords
    )


def test_typed_hydration_quarantine_degrades_channel_and_preserves_operator_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    detail = IntegrationError(
        source="ado", stage="relations", message="Unknown relation quarantined",
        retryable=False, operator_action="Review provider relation metadata.",
        ref_id="101", ref_kind="work_item",
    )
    hydration = HydrationResult(
        channel="ado", resources=ADOHydrationOutput(work_items=()), api_call_count=1,
        errors=(detail,), hydrated_ref_ids=(), failed_ref_ids=(),
    )
    discovery = Mock()
    binding = ChannelBinding(
        config=ChannelConfig(channel="ado", enabled=True, discovery_threshold_hours=24, ttl_days=30),
        discovery_provider=discovery, hydration_provider=Mock(hydrate=Mock(return_value=hydration)),
        signal_extractor=Mock(), discovery_config=object(), hydration_config=object(),
    )
    store = Mock()
    store.is_discovery_stale.return_value = False
    store.pullable_registrations.return_value = ()
    store.get_workstream_map.return_value = {}
    monkeypatch.setattr(channel_runtime, "load_arch_data_fix", Mock(return_value=None))
    errors: list[GatherError] = []
    outcomes = []
    result, _ = channel_runtime.run_channel(
        binding, store, program_id="fixture", since=AS_OF, verified_at=AS_OF,
        run_ctx=RunContext(dry_run=True), integration_error_sink=errors,
        channel_outcome_sink=outcomes, programs_root=tmp_path,
    )
    assert result is not None
    assert result.errors == (detail,)
    assert outcomes[0].degraded is True
    assert errors[0].retryable is False
    assert errors[0].operator_action == detail.operator_action
    discovery.discover.assert_not_called()
    store.mark_verified.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_existing_error_deduplication_preserves_explicit_quarantine_guidance() -> None:
    errors: list[GatherError] = []
    for expected in (True, False):
        assert append_integration_error_once(
            errors, source="ado", stage="relations", error="Unknown relation quarantined",
            retryable=False, operator_action="Review provider relation metadata.",
        ) is expected
    assert len(errors) == 1
    assert errors[0].retryable is False
    assert errors[0].operator_action == "Review provider relation metadata."
