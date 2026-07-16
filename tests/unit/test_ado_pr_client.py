from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock
import pytest

from src.core.ado_client import ADOClient
from src.core.ado_pr_client import ADOPRClient, PullRequestSummary


def test_ado_pr_client_list_pull_requests() -> None:
    # 1. Setup mock client
    mock_client = MagicMock(spec=ADOClient)
    mock_client.organization = "msazure"
    mock_client.project = "One"
    
    mock_response = {
        "value": [
            {
                "pullRequestId": 123,
                "title": "Fix BIOS Gen8",
                "status": "active",
                "createdBy": {"displayName": "Alice Testowner"},
                "targetRefName": "refs/heads/main",
                "sourceRefName": "refs/heads/feature/gen8",
                "url": "https://weburl/123",
                "creationDate": "2026-05-24T10:00:00Z",
                "_links": {"web": {"href": "https://weburl/123"}},
            }
        ]
    }
    mock_client._request_json.return_value = mock_response
    
    # 2. Instantiate and call
    pr_client = ADOPRClient(mock_client)
    prs = pr_client.list_pull_requests("repo-adventure", status="active", top=50)
    
    # 3. Verify
    assert len(prs) == 1
    pr = prs[0]
    assert pr.pr_id == 123
    assert pr.title == "Fix BIOS Gen8"
    assert pr.status == "active"
    assert pr.created_by == "Alice Testowner"
    assert pr.target_ref == "refs/heads/main"
    assert pr.source_ref == "refs/heads/feature/gen8"
    assert pr.url == "https://weburl/123"
    assert pr.created_at == datetime(2026, 5, 24, 10, 0, tzinfo=timezone.utc)
    assert pr.merged_at is None
    assert pr.repository_id == "repo-adventure"
    
    # Verify mock client call
    mock_client._request_json.assert_called_once_with(
        "GET",
        "https://dev.azure.com/msazure/One/_apis/git/repositories/repo-adventure/pullrequests?api-version=7.1",
        params={"searchCriteria.status": "active", "$top": "50", "$skip": "0"},
    )

def _pr_client_with_value(value: list[dict]) -> ADOPRClient:
    mock_client = MagicMock(spec=ADOClient)
    mock_client.organization = "msazure"
    mock_client.project = "One"
    mock_client._request_json.return_value = {"value": value}
    return ADOPRClient(mock_client)


def test_list_pull_requests_skips_missing_creation_date_without_fabricating() -> None:
    client = _pr_client_with_value(
        [
            {
                "pullRequestId": 1,
                "title": "No date",
                "status": "active",
                "targetRefName": "refs/heads/main",
                "sourceRefName": "refs/heads/f1",
                # creationDate intentionally absent
            },
            {
                "pullRequestId": 2,
                "title": "Has date",
                "status": "active",
                "targetRefName": "refs/heads/main",
                "sourceRefName": "refs/heads/f2",
                "creationDate": "2026-05-24T10:00:00Z",
            },
        ]
    )
    prs = client.list_pull_requests("repo-x")
    # The dateless PR is dropped (no datetime.now() fabrication); the valid one survives.
    assert [pr.pr_id for pr in prs] == [2]
    assert prs[0].created_at == datetime(2026, 5, 24, 10, 0, tzinfo=timezone.utc)


def test_list_pull_requests_skips_unparseable_creation_date() -> None:
    client = _pr_client_with_value(
        [
            {
                "pullRequestId": 3,
                "title": "Bad date",
                "status": "active",
                "targetRefName": "refs/heads/main",
                "sourceRefName": "refs/heads/f3",
                "creationDate": "not-a-date",
            }
        ]
    )
    assert client.list_pull_requests("repo-x") == ()


def test_list_pull_requests_skips_missing_required_fields_without_keyerror() -> None:
    client = _pr_client_with_value(
        [
            {"title": "No id", "status": "active", "targetRefName": "a", "sourceRefName": "b", "creationDate": "2026-05-24T10:00:00Z"},
            {"pullRequestId": 5, "status": "active", "targetRefName": "a", "sourceRefName": "b", "creationDate": "2026-05-24T10:00:00Z"},
            "not-a-dict",
            {
                "pullRequestId": 6,
                "title": "Good",
                "status": "active",
                "targetRefName": "refs/heads/main",
                "sourceRefName": "refs/heads/f6",
                "creationDate": "2026-05-24T10:00:00Z",
            },
        ]
    )
    prs = client.list_pull_requests("repo-x")
    assert [pr.pr_id for pr in prs] == [6]


def test_list_pull_requests_tolerates_malformed_nested_objects() -> None:
    client = _pr_client_with_value(
        [
            {
                "pullRequestId": 7,
                "title": "Weird nesting",
                "status": "completed",
                "targetRefName": "refs/heads/main",
                "sourceRefName": "refs/heads/f7",
                "creationDate": "2026-05-24T10:00:00Z",
                "closedDate": "not-a-date",
                "createdBy": "not-a-dict",
                "_links": "not-a-dict",
                "url": "https://fallback/7",
            }
        ]
    )
    prs = client.list_pull_requests("repo-x")
    assert len(prs) == 1
    pr = prs[0]
    assert pr.created_by == "Unknown"
    assert pr.url == "https://fallback/7"
    # Unparseable closedDate must not crash and must not invent a merge time.
    assert pr.merged_at is None


def _pr_row(pr_id: int) -> dict:
    return {
        "pullRequestId": pr_id,
        "title": f"PR {pr_id}",
        "status": "active",
        "targetRefName": "refs/heads/main",
        "sourceRefName": f"refs/heads/f{pr_id}",
        "creationDate": "2026-05-24T10:00:00Z",
    }


def test_list_pull_requests_pages_across_multiple_requests() -> None:
    """ADF-W2.1: a full first page (top rows) followed by a short page is
    seen as >1-page and not truncated."""
    mock_client = MagicMock(spec=ADOClient)
    mock_client.organization = "msazure"
    mock_client.project = "One"
    mock_client._request_json.side_effect = [
        {"value": [_pr_row(1), _pr_row(2)]},  # full page (top=2)
        {"value": [_pr_row(3)]},  # short page -> done
    ]

    pr_client = ADOPRClient(mock_client)
    outcomes: list[object] = []
    prs = pr_client.list_pull_requests("repo-x", top=2, on_pagination=outcomes.append)

    assert [pr.pr_id for pr in prs] == [1, 2, 3]
    assert mock_client._request_json.call_count == 2
    second_call_params = mock_client._request_json.call_args_list[1].kwargs["params"]
    assert second_call_params["$skip"] == "2"
    assert len(outcomes) == 1
    assert outcomes[0].total_fetched == 3
    assert outcomes[0].page_count == 2
    assert outcomes[0].is_truncated is False


def test_list_pull_requests_reports_truncation_at_safety_cap() -> None:
    mock_client = MagicMock(spec=ADOClient)
    mock_client.organization = "msazure"
    mock_client.project = "One"
    mock_client._request_json.return_value = {"value": [_pr_row(1), _pr_row(2)]}  # always full

    pr_client = ADOPRClient(mock_client)
    outcomes: list[object] = []
    prs = pr_client.list_pull_requests("repo-x", top=2, max_pages=3, on_pagination=outcomes.append)

    assert len(prs) == 6  # 3 pages x 2 rows, safety-capped (dedup not expected across pages here)
    assert outcomes[0].is_truncated is True
    assert outcomes[0].page_count == 3
