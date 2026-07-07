from __future__ import annotations

import io
import time

import pytest

from src.core.ado_client import ADOClient
from src.core.exceptions import AuthError, QueryTimeoutError


class RecordingADOClient(ADOClient):
    def __init__(self) -> None:
        self.recorded_entity_set: str | None = None
        self.recorded_params: dict[str, str] | None = None

    def query_odata_all(self, entity_set: str, params: dict[str, str]) -> list[dict[str, str]]:
        self.recorded_entity_set = entity_set
        self.recorded_params = params
        return [{"WorkItemId": "1", "WorkItemType": "Feature"}]


class RecordingRestADOClient(ADOClient):
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.recorded_method: str | None = None
        self.recorded_url: str | None = None
        self.recorded_kwargs: dict[str, object] | None = None
        self._rest_base_url = "https://dev.azure.com/your-org/One/_apis/wit/"

    def _request_json(self, method: str, url: str, **kwargs: object) -> dict[str, object]:
        self.recorded_method = method
        self.recorded_url = url
        self.recorded_kwargs = kwargs
        return self.payload

    def _request_with_progress(self, method: str, url: str, **kwargs: object) -> _FakeNoContentResponse:
        self.recorded_method = method
        self.recorded_url = url
        self.recorded_kwargs = kwargs
        return _FakeNoContentResponse()


class ProgressADOClient(ADOClient):
    def __init__(self, *, delay_seconds: float, timeout_seconds: float, progress_stream: io.StringIO) -> None:
        self.timeout = timeout_seconds
        self.show_progress = True
        self.slow_warning_seconds = 0
        self.progress_poll_seconds = 0.01
        self.progress_stream = progress_stream
        self._session = _SlowSession(delay_seconds)

    def _headers(self) -> dict[str, str]:
        return {}


class _SlowSession:
    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds

    def request(self, *args, **kwargs):
        time.sleep(self.delay_seconds)
        return _FakeResponse()


class _FakeResponse:
    status_code = 200
    text = ""

    def json(self) -> dict[str, object]:
        return {"value": []}


class _FakeNoContentResponse:
    status_code = 204
    text = ""


def test_ado_client_surfaces_admin_auth_setup_when_no_credentials(monkeypatch) -> None:
    monkeypatch.setattr("src.core.ado_client.AZURE_IDENTITY_AVAILABLE", False)
    monkeypatch.delenv("ADO_PAT", raising=False)

    with pytest.raises(AuthError, match="vertex admin auth setup"):
        ADOClient("your-org", "One", show_progress=False)


def test_headers_wraps_token_acquisition_failures_as_auth_error() -> None:
    class _FailingCredential:
        def get_token(self, _scope: str):
            raise RuntimeError("azure cli timed out")

    client = object.__new__(ADOClient)
    client._credential = _FailingCredential()
    client.pat_env = "ADO_PAT"

    with pytest.raises(AuthError, match="Failed to acquire Azure DevOps token"):
        client._headers()


def test_query_all_uses_work_items_surface() -> None:
    client = RecordingADOClient()

    items = client.query_all(
        filter_expression="WorkItemType eq 'Feature'",
        select_fields=("WorkItemId", "Title"),
        top=200,
    )

    assert items == [{"WorkItemId": "1", "WorkItemType": "Feature"}]
    assert client.recorded_entity_set == "WorkItems"
    assert client.recorded_params == {
        "$filter": "WorkItemType eq 'Feature'",
        "$select": "WorkItemId,Title",
        "$expand": "Area",
        "$count": "true",
        "$top": "200",
    }


def test_query_work_items_batch_uses_rest_batch_surface() -> None:
    client = RecordingRestADOClient(payload={"value": [{"id": 101}]})

    rows = client.query_work_items_batch(
        work_item_ids=[101, 102],
        fields=("System.Id", "System.Title"),
    )

    assert rows == [{"id": 101}]
    assert client.recorded_method == "POST"
    assert client.recorded_url == "https://dev.azure.com/your-org/One/_apis/wit/workitemsbatch?api-version=7.1"
    assert client.recorded_kwargs == {
        "json": {
            "ids": [101, 102],
            "fields": ["System.Id", "System.Title"],
        }
    }


def test_list_work_item_revisions_uses_revisions_endpoint() -> None:
    client = RecordingRestADOClient(payload={"value": [{"rev": 3}]})

    rows = client.list_work_item_revisions(101)

    assert rows == [{"rev": 3}]
    assert client.recorded_method == "GET"
    assert client.recorded_url == "https://dev.azure.com/your-org/One/_apis/wit/workItems/101/revisions?api-version=7.1"
    assert client.recorded_kwargs == {}


def test_list_work_item_comments_uses_comments_endpoint() -> None:
    client = RecordingRestADOClient(payload={"comments": [{"id": 7}]})

    rows = client.list_work_item_comments(101)

    assert rows == [{"id": 7}]
    assert client.recorded_method == "GET"
    assert client.recorded_url == "https://dev.azure.com/your-org/One/_apis/wit/workItems/101/comments?api-version=7.1-preview.4"
    assert client.recorded_kwargs == {}


def test_get_work_item_relations_expands_relations() -> None:
    client = RecordingRestADOClient(payload={"value": [{"id": 101, "relations": []}]})

    rows = client.get_work_item_relations([101, 202])

    assert rows == [{"id": 101, "relations": []}]
    assert client.recorded_method == "GET"
    assert client.recorded_url == "https://dev.azure.com/your-org/One/_apis/wit/workitems?ids=101,202&api-version=7.1"
    assert client.recorded_kwargs == {"params": {"$expand": "relations"}}


def test_query_work_item_snapshot_history_uses_work_item_snapshot_surface() -> None:
    client = RecordingADOClient()

    rows = client.query_work_item_snapshot_history(
        [101, 202],
        select_fields=("DateValue", "WorkItemId", "TargetDate"),
        start_date="2026-05-01",
    )

    assert rows == [{"WorkItemId": "1", "WorkItemType": "Feature"}]
    assert client.recorded_entity_set == "WorkItemSnapshot"
    assert client.recorded_params == {
        "$filter": "WorkItemId in (101,202) and DateValue ge 2026-05-01",
        "$select": "DateValue,WorkItemId,TargetDate",
    }


def test_get_saved_query_expands_wiql() -> None:
    client = RecordingRestADOClient(payload={"id": "query-1", "wiql": "Select [System.Id] From WorkItems"})

    payload = client.get_saved_query("query-1")

    assert payload == {"id": "query-1", "wiql": "Select [System.Id] From WorkItems"}
    assert client.recorded_method == "GET"
    assert client.recorded_url == "https://dev.azure.com/your-org/One/_apis/wit/queries/query-1?$expand=wiql&api-version=7.1"
    assert client.recorded_kwargs == {}


def test_get_saved_query_quotes_folder_path() -> None:
    client = RecordingRestADOClient(payload={"id": "query-1"})

    client.get_saved_query("Shared Queries/Vertex/Acme")

    assert client.recorded_method == "GET"
    assert client.recorded_url == "https://dev.azure.com/your-org/One/_apis/wit/queries/Shared%20Queries/Vertex/Acme?$expand=wiql&api-version=7.1"
    assert client.recorded_kwargs == {}


def test_create_saved_query_uses_parent_path_endpoint() -> None:
    client = RecordingRestADOClient(payload={"id": "query-1"})

    payload = client.create_saved_query(
        "Shared Queries/Vertex/Acme",
        name="acme.scenarios_stg_signoff",
        wiql="Select [System.Id] From WorkItems",
    )

    assert payload == {"id": "query-1"}
    assert client.recorded_method == "POST"
    assert client.recorded_url == "https://dev.azure.com/your-org/One/_apis/wit/queries/Shared%20Queries/Vertex/Acme?api-version=7.1"
    assert client.recorded_kwargs == {
        "json": {
            "name": "acme.scenarios_stg_signoff",
            "isFolder": False,
            "wiql": "Select [System.Id] From WorkItems",
        }
    }


def test_create_saved_query_can_validate_wiql_only() -> None:
    client = RecordingRestADOClient(payload={"id": "query-1"})

    result = client.create_saved_query(
        "Shared Queries/Vertex/Acme",
        name="acme.scenarios_stg_signoff",
        wiql="Select [System.Id] From WorkItems",
        validate_wiql_only=True,
    )

    assert result == {}
    assert client.recorded_method == "POST"
    assert client.recorded_url == "https://dev.azure.com/your-org/One/_apis/wit/queries/Shared%20Queries/Vertex/Acme?api-version=7.1&validateWiqlOnly=true"


def test_execute_wiql_uses_wiql_endpoint() -> None:
    client = RecordingRestADOClient(payload={"workItems": [{"id": 101}, {"id": 202}]})

    item_ids = client.execute_wiql("Select [System.Id] From WorkItems")

    assert item_ids == [101, 202]
    assert client.recorded_method == "POST"
    assert client.recorded_url == "https://dev.azure.com/your-org/One/_apis/wit/wiql?api-version=7.1&$top=2000"
    assert client.recorded_kwargs == {"json": {"query": "Select [System.Id] From WorkItems"}}


def test_execute_wiql_honors_explicit_top_cap() -> None:
    client = RecordingRestADOClient(payload={"workItems": [{"id": 101}]})

    item_ids = client.execute_wiql("Select [System.Id] From WorkItems", top=10000)

    assert item_ids == [101]
    assert client.recorded_method == "POST"
    assert client.recorded_url == "https://dev.azure.com/your-org/One/_apis/wit/wiql?api-version=7.1&$top=10000"
    assert client.recorded_kwargs == {"json": {"query": "Select [System.Id] From WorkItems"}}


def test_list_team_iterations_uses_teamsettings_iterations_endpoint() -> None:
    client = RecordingRestADOClient(payload={"values": [{"id": "iteration-1"}]})
    client.organization = "your-org"
    client.project = "One"

    iterations = client.list_team_iterations(timeframe="current")

    assert iterations == [{"id": "iteration-1"}]
    assert client.recorded_method == "GET"
    assert client.recorded_url == "https://dev.azure.com/your-org/One/_apis/work/teamsettings/iterations?api-version=7.1"
    assert client.recorded_kwargs == {"params": {"$timeframe": "current"}}


def test_list_iteration_capacities_uses_iteration_capacity_endpoint() -> None:
    client = RecordingRestADOClient(payload={"value": [{"teamMember": {"displayName": "Chuck"}}]})
    client.organization = "your-org"
    client.project = "One"

    capacities = client.list_iteration_capacities("iteration-1")

    assert capacities == [{"teamMember": {"displayName": "Chuck"}}]
    assert client.recorded_method == "GET"
    assert client.recorded_url == "https://dev.azure.com/your-org/One/_apis/work/teamsettings/iterations/iteration-1/capacities?api-version=6.0"
    assert client.recorded_kwargs == {}


def test_list_team_iterations_uses_team_specific_endpoint_when_team_provided() -> None:
    client = RecordingRestADOClient(payload={"values": [{"id": "iteration-1"}]})
    client.organization = "your-org"
    client.project = "One"

    iterations = client.list_team_iterations(timeframe="current", team="Northwind Team")

    assert iterations == [{"id": "iteration-1"}]
    assert client.recorded_method == "GET"
    assert client.recorded_url == "https://dev.azure.com/your-org/One/Northwind%20Team/_apis/work/teamsettings/iterations?api-version=7.1"
    assert client.recorded_kwargs == {"params": {"$timeframe": "current"}}


def test_list_iteration_capacities_uses_team_specific_endpoint_when_team_provided() -> None:
    client = RecordingRestADOClient(payload={"value": [{"teamMember": {"displayName": "Chuck"}}]})
    client.organization = "your-org"
    client.project = "One"

    capacities = client.list_iteration_capacities("iteration-1", team="Northwind Team")

    assert capacities == [{"teamMember": {"displayName": "Chuck"}}]
    assert client.recorded_method == "GET"
    assert client.recorded_url == "https://dev.azure.com/your-org/One/Northwind%20Team/_apis/work/teamsettings/iterations/iteration-1/capacities?api-version=6.0"
    assert client.recorded_kwargs == {}


def test_request_json_emits_slow_warning() -> None:
    progress_stream = io.StringIO()
    client = ProgressADOClient(delay_seconds=0.03, timeout_seconds=1.0, progress_stream=progress_stream)

    payload = client._request_json("GET", "https://example.test")

    assert payload == {"value": []}
    assert "ADO slow (15s elapsed). Still waiting" in progress_stream.getvalue()


def test_request_json_raises_timeout_error() -> None:
    progress_stream = io.StringIO()
    client = ProgressADOClient(delay_seconds=0.2, timeout_seconds=0.05, progress_stream=progress_stream)

    with pytest.raises(QueryTimeoutError, match="ADO fetch timed out after 0.05s"):
        client._request_json("GET", "https://example.test")