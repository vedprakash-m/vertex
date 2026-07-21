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
    def __init__(self, payload: dict[str, object], *, response_headers: dict[str, str] | None = None) -> None:
        self.payload = payload
        self.response_headers = response_headers or {}
        self.recorded_method: str | None = None
        self.recorded_url: str | None = None
        self.recorded_kwargs: dict[str, object] | None = None
        self._rest_base_url = "https://dev.azure.com/your-org/One/_apis/wit/"

    def _request_json(self, method: str, url: str, **kwargs: object) -> dict[str, object]:
        self.recorded_method = method
        self.recorded_url = url
        self.recorded_kwargs = kwargs
        return self.payload

    def _request_response(self, method: str, url: str, **kwargs: object) -> "_FakeJsonResponse":
        self.recorded_method = method
        self.recorded_url = url
        self.recorded_kwargs = kwargs
        return _FakeJsonResponse(self.payload, headers=self.response_headers)

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


class _FakeJsonResponse:
    status_code = 200
    text = "{}"

    def __init__(self, payload: dict[str, object], *, headers: dict[str, str] | None = None) -> None:
        self._payload = payload
        self.headers = headers or {}

    def json(self) -> dict[str, object]:
        return self._payload


def test_ado_client_surfaces_admin_auth_setup_when_no_credentials(monkeypatch) -> None:
    monkeypatch.setattr("src.core.ado_client.AZURE_IDENTITY_AVAILABLE", False)
    monkeypatch.delenv("ADO_PAT", raising=False)

    with pytest.raises(AuthError, match="vertex admin auth setup"):
        ADOClient("your-org", "One", show_progress=False)


def test_strict_azure_cli_mode_never_falls_back_to_default_or_pat(monkeypatch) -> None:
    class _FailingAzureCliCredential:
        def get_token(self, _scope: str):
            raise RuntimeError("Azure CLI session expired")

    class _UnexpectedDefaultCredential:
        def get_token(self, _scope: str):
            raise AssertionError("strict Azure CLI mode must not instantiate DefaultAzureCredential")

    monkeypatch.setattr("src.core.ado_client.AZURE_IDENTITY_AVAILABLE", True)
    monkeypatch.setattr(
        "src.core.ado_client.AZURE_CREDENTIAL_TYPES",
        (_FailingAzureCliCredential, _UnexpectedDefaultCredential),
    )
    monkeypatch.setenv("VERTEX_ADO_AUTH_MODE", "azure-cli")
    monkeypatch.setenv("ADO_PAT", "must-not-be-used")

    with pytest.raises(AuthError, match="Azure CLI authentication was selected"):
        ADOClient("your-org", "One", show_progress=False)


def test_strict_pat_mode_skips_azure_identity_providers(monkeypatch) -> None:
    class _UnexpectedCredential:
        def __init__(self) -> None:
            raise AssertionError("strict PAT mode must not instantiate an AAD credential")

    monkeypatch.setattr("src.core.ado_client.AZURE_IDENTITY_AVAILABLE", True)
    monkeypatch.setattr("src.core.ado_client.AZURE_CREDENTIAL_TYPES", (_UnexpectedCredential,))
    monkeypatch.setenv("VERTEX_ADO_AUTH_MODE", "pat")
    monkeypatch.setenv("ADO_PAT", "scheduled-secret")

    assert ADOClient("your-org", "One", show_progress=False).auth_method == "pat"


def test_headers_wraps_token_acquisition_failures_as_auth_error() -> None:
    class _FailingCredential:
        def get_token(self, _scope: str):
            raise RuntimeError("azure cli timed out")

    client = object.__new__(ADOClient)
    client._credential = _FailingCredential()
    client.pat_env = "ADO_PAT"

    with pytest.raises(AuthError, match="Failed to acquire Azure DevOps token"):
        client._headers()


def test_headers_cache_azure_cli_access_token_until_near_expiry() -> None:
    class _AccessToken:
        token = "cached-token"
        expires_on = time.time() + 600

    class _CountingCredential:
        def __init__(self) -> None:
            self.calls = 0

        def get_token(self, _scope: str) -> _AccessToken:
            self.calls += 1
            return _AccessToken()

    credential = _CountingCredential()
    client = object.__new__(ADOClient)
    client._credential = credential
    client.pat_env = "ADO_PAT"

    assert client._headers()["Authorization"] == "Bearer cached-token"
    assert client._headers()["Authorization"] == "Bearer cached-token"
    assert credential.calls == 1


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
    assert client.recorded_kwargs == {"params": {"$top": "200", "$skip": "0"}}


def test_list_work_item_revisions_pages_across_multiple_requests() -> None:
    """ADF-W2.1: a fixture with exactly one full page (page_size rows)
    followed by a short page must be seen as >1-page and NOT truncated."""

    class _PagedRevisionsClient(ADOClient):
        def __init__(self) -> None:
            self._rest_base_url = "https://dev.azure.com/your-org/One/_apis/wit/"
            self.requested_skips: list[int] = []

        def _request_json(self, method: str, url: str, **kwargs: object) -> dict[str, object]:
            del method, url
            params = kwargs.get("params") or {}
            skip = int(params.get("$skip", 0))  # type: ignore[union-attr]
            self.requested_skips.append(skip)
            if skip == 0:
                return {"value": [{"rev": i} for i in range(1, 3)]}  # full page (page_size=2)
            return {"value": [{"rev": 3}]}  # short page -> done

    client = _PagedRevisionsClient()
    outcomes: list[object] = []

    rows = client.list_work_item_revisions(101, page_size=2, on_pagination=outcomes.append)

    assert [row["rev"] for row in rows] == [1, 2, 3]
    assert client.requested_skips == [0, 2]
    assert len(outcomes) == 1
    assert outcomes[0].total_fetched == 3
    assert outcomes[0].page_count == 2
    assert outcomes[0].is_truncated is False


def test_list_work_item_revisions_reports_truncation_at_safety_cap() -> None:
    class _AlwaysFullPageClient(ADOClient):
        def __init__(self) -> None:
            self._rest_base_url = "https://dev.azure.com/your-org/One/_apis/wit/"
            self.call_count = 0

        def _request_json(self, method: str, url: str, **kwargs: object) -> dict[str, object]:
            del method, url, kwargs
            self.call_count += 1
            return {"value": [{"rev": self.call_count}, {"rev": self.call_count + 1000}]}  # always a full page

    client = _AlwaysFullPageClient()
    outcomes: list[object] = []

    rows = client.list_work_item_revisions(101, page_size=2, max_pages=3, on_pagination=outcomes.append)

    assert len(rows) == 6  # 3 pages x 2 rows, safety-capped
    assert outcomes[0].is_truncated is True
    assert outcomes[0].page_count == 3


def test_list_work_item_comments_uses_comments_endpoint() -> None:
    client = RecordingRestADOClient(payload={"comments": [{"id": 7}]})

    rows = client.list_work_item_comments(101)

    assert rows == [{"id": 7}]
    assert client.recorded_method == "GET"
    assert client.recorded_url == "https://dev.azure.com/your-org/One/_apis/wit/workItems/101/comments?api-version=7.1-preview.4"
    assert client.recorded_kwargs == {"params": None}


def test_list_work_item_comments_follows_continuation_token_across_pages() -> None:
    class _PagedCommentsClient(ADOClient):
        def __init__(self) -> None:
            self._rest_base_url = "https://dev.azure.com/your-org/One/_apis/wit/"
            self.requested_tokens: list[str | None] = []

        def _request_response(self, method: str, url: str, **kwargs: object) -> _FakeJsonResponse:
            del method, url
            params = kwargs.get("params")
            token = (params or {}).get("continuationToken") if params else None  # type: ignore[union-attr]
            self.requested_tokens.append(token)
            if token is None:
                return _FakeJsonResponse({"comments": [{"id": 1}]}, headers={"x-ms-continuationtoken": "page-2"})
            return _FakeJsonResponse({"comments": [{"id": 2}]}, headers={})

    client = _PagedCommentsClient()
    outcomes: list[object] = []

    rows = client.list_work_item_comments(101, on_pagination=outcomes.append)

    assert [row["id"] for row in rows] == [1, 2]
    assert client.requested_tokens == [None, "page-2"]
    assert outcomes[0].total_fetched == 2
    assert outcomes[0].page_count == 2
    assert outcomes[0].is_truncated is False


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


class _RecordingSnapshotADOClient(ADOClient):
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows
        self.recorded_entity_set: str | None = None
        self.recorded_params: dict[str, str] | None = None

    def query_odata_all(self, entity_set: str, params: dict[str, str]) -> list[dict[str, object]]:
        self.recorded_entity_set = entity_set
        self.recorded_params = params
        return self._rows


def test_query_work_item_snapshot_expands_area_path_and_flattens_result() -> None:
    client = _RecordingSnapshotADOClient(
        rows=[
            {"DateSK": 20260501, "WorkItemId": 101, "Area": {"AreaPath": "One\\XStore\\Armada"}},
        ]
    )

    rows = client.query_work_item_snapshot(
        filter_expression="startswith(Area/AreaPath, 'One\\XStore\\Armada')",
        select_fields=("DateSK", "WorkItemId", "AreaPath"),
    )

    assert client.recorded_entity_set == "WorkItemSnapshot"
    assert client.recorded_params == {
        "$filter": "startswith(Area/AreaPath, 'One\\XStore\\Armada')",
        "$select": "DateSK,WorkItemId",
        "$expand": "Area($select=AreaPath)",
    }
    assert rows == [{"DateSK": 20260501, "WorkItemId": 101, "AreaPath": "One\\XStore\\Armada"}]


def test_query_work_item_snapshot_expands_both_area_and_iteration_path() -> None:
    client = _RecordingSnapshotADOClient(
        rows=[
            {
                "DateSK": 20260501,
                "WorkItemId": 101,
                "Area": {"AreaPath": "One\\XStore\\Armada"},
                "Iteration": {"IterationPath": "One\\Sprint 24"},
            },
        ]
    )

    rows = client.query_work_item_snapshot(
        filter_expression="DateSK ge 20260501",
        select_fields=("DateSK", "WorkItemId", "AreaPath", "IterationPath"),
    )

    assert client.recorded_params == {
        "$filter": "DateSK ge 20260501",
        "$select": "DateSK,WorkItemId",
        "$expand": "Area($select=AreaPath),Iteration($select=IterationPath)",
    }
    assert rows == [
        {
            "DateSK": 20260501,
            "WorkItemId": 101,
            "AreaPath": "One\\XStore\\Armada",
            "IterationPath": "One\\Sprint 24",
        }
    ]


def test_query_work_item_snapshot_flattens_missing_nested_area_to_empty_string() -> None:
    client = _RecordingSnapshotADOClient(rows=[{"DateSK": 20260501, "WorkItemId": 101, "Area": None}])

    rows = client.query_work_item_snapshot(
        filter_expression="DateSK ge 20260501",
        select_fields=("DateSK", "WorkItemId", "AreaPath"),
    )

    assert rows == [{"DateSK": 20260501, "WorkItemId": 101, "AreaPath": ""}]


def test_query_work_item_snapshot_without_area_or_iteration_fields_omits_expand() -> None:
    client = _RecordingSnapshotADOClient(rows=[{"DateSK": 20260501, "WorkItemId": 101}])

    rows = client.query_work_item_snapshot(
        filter_expression="DateSK ge 20260501",
        select_fields=("DateSK", "WorkItemId"),
    )

    assert client.recorded_params == {
        "$filter": "DateSK ge 20260501",
        "$select": "DateSK,WorkItemId",
    }
    assert rows == [{"DateSK": 20260501, "WorkItemId": 101}]


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


def test_execute_wiql_reports_cap_reached_via_on_pagination() -> None:
    client = RecordingRestADOClient(payload={"workItems": [{"id": i} for i in range(1, 4)]})
    outcomes: list[object] = []

    item_ids = client.execute_wiql("Select [System.Id] From WorkItems", top=3, on_pagination=outcomes.append)

    assert len(item_ids) == 3
    assert len(outcomes) == 1
    assert outcomes[0].is_truncated is True  # result count == top: likely capped
    assert outcomes[0].total_fetched == 3


def test_execute_wiql_reports_not_capped_when_under_top() -> None:
    client = RecordingRestADOClient(payload={"workItems": [{"id": 101}]})
    outcomes: list[object] = []

    client.execute_wiql("Select [System.Id] From WorkItems", top=2000, on_pagination=outcomes.append)

    assert outcomes[0].is_truncated is False


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


class _PaginatingPRClient(ADOClient):
    """Returns successive pages of PR ``value`` arrays based on ``$skip`` so
    the multi-page ``list_pull_requests`` loop can be exercised without a live
    ADO connection. ``pages`` is a list of (value-list, count) pairs served in
    order; each call pops the front page."""

    def __init__(self, pages: list[list[dict[str, object]]]) -> None:
        self._pages = list(pages)
        self.organization = "your-org"
        self.project = "One"
        self.recorded_calls: list[dict[str, str]] = []

    def _request_json(self, method: str, url: str, **kwargs: object) -> dict[str, object]:
        params = kwargs.get("params", {})
        self.recorded_calls.append({"method": method, "url": url, **(params if isinstance(params, dict) else {})})
        if not self._pages:
            return {"value": []}
        return {"value": self._pages.pop(0)}


def test_list_pull_requests_pages_across_full_result_set() -> None:
    """ADF-W2.1 (Section 8.4.2): the discover-repos ``list_pull_requests`` now
    pages via ``$top``/``$skip`` across the full result set, matching the
    production ``ADOPRClient.list_pull_requests`` and ``list_work_item_revisions``
    pagination loops."""
    page1 = [{"pullRequestId": i} for i in range(100)]  # full page (== top)
    page2 = [{"pullRequestId": i} for i in range(100, 150)]  # short page -> stops
    client = _PaginatingPRClient(pages=[page1, page2])

    prs = client.list_pull_requests("repo-1", status="active", top=100)

    assert len(prs) == 150
    assert prs[0]["pullRequestId"] == 0
    assert prs[-1]["pullRequestId"] == 149
    # Two requests: first skip=0, second skip=100
    assert len(client.recorded_calls) == 2
    assert client.recorded_calls[0]["$skip"] == "0"
    assert client.recorded_calls[1]["$skip"] == "100"


def test_list_pull_requests_single_page_when_under_top() -> None:
    """A result that fits in one page must not make extra requests."""
    page = [{"pullRequestId": i} for i in range(5)]  # < top=100 -> stops immediately
    client = _PaginatingPRClient(pages=[page])

    prs = client.list_pull_requests("repo-1", status="active", top=100)

    assert len(prs) == 5
    assert len(client.recorded_calls) == 1


def test_list_pull_requests_reports_truncation_via_on_pagination() -> None:
    """When the safety ``max_pages`` cap is hit while the provider still has
    more data, ``on_pagination`` fires with ``is_truncated=True``."""
    # Every page returns a full top=10 -> the loop hits max_pages=3 while data remains
    full_page = [{"pullRequestId": i} for i in range(10)]
    client = _PaginatingPRClient(pages=[list(full_page), list(full_page), list(full_page), list(full_page)])
    outcomes: list[object] = []

    prs = client.list_pull_requests("repo-1", status="active", top=10, max_pages=3, on_pagination=outcomes.append)

    assert len(prs) == 30  # 3 pages * 10
    assert len(client.recorded_calls) == 3  # stopped at max_pages
    assert len(outcomes) == 1
    assert outcomes[0].is_truncated is True
    assert outcomes[0].page_count == 3
    assert outcomes[0].total_fetched == 30


def test_list_pull_requests_reports_not_truncated_on_short_page() -> None:
    """A result that ends naturally (short page) must report is_truncated=False."""
    page = [{"pullRequestId": i} for i in range(3)]  # short page -> stops naturally
    client = _PaginatingPRClient(pages=[page])
    outcomes: list[object] = []

    prs = client.list_pull_requests("repo-1", status="active", top=10, on_pagination=outcomes.append)

    assert len(prs) == 3
    assert outcomes[0].is_truncated is False
    assert outcomes[0].page_count == 1
