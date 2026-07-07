from __future__ import annotations

import json
from types import SimpleNamespace

from typer.testing import CliRunner

from cli import app


runner = CliRunner()


def test_probe_ado_accepts_canonical_parent_area(monkeypatch) -> None:
    client = _MatchingAreaClient(
        items=[
            {"WorkItemId": "101", "WorkItemType": "Feature"},
            {"WorkItemId": "102", "WorkItemType": "Feature"},
            {"WorkItemId": "103", "WorkItemType": "Risk"},
        ]
    )
    monkeypatch.setattr("src.commands.probe_ado.load_report_config", _report_config)
    monkeypatch.setattr("src.commands.probe_ado.ADOClient", lambda **_: client)

    result = runner.invoke(app, ["probe-ado", "--area", "One\\Adventure\\Acme", "--since", "14d"])

    assert result.exit_code == 0
    assert "Feature\t2" in result.stdout
    assert "Risk\t1" in result.stdout
    assert "Total\t3" in result.stdout
    assert client.probed_ids == [101, 102, 103]
    assert "startswith(Area/AreaPath, 'One\\Adventure\\Acme')" in client.filter_expression


def test_probe_ado_suggests_when_area_scope_missing(monkeypatch) -> None:
    client = _MissingAreaClient()
    monkeypatch.setattr("src.commands.probe_ado.load_report_config", _report_config)
    monkeypatch.setattr("src.commands.probe_ado.ADOClient", lambda **_: client)

    result = runner.invoke(app, ["probe-ado", "--area", "One\\Adventure\\Acme", "--since", "14d"])

    assert result.exit_code == 2
    assert "Result\tNo exact analytics area-path match found" in result.stdout
    assert "Diagnosis\tNo analytics descendants found under requested prefix" in result.stdout
    assert "Suggestions" in result.stdout
    assert "One\\Adventure\\Acme\\Deployment" in result.stdout


def test_probe_ado_supports_json_and_csv(monkeypatch) -> None:
    client = _MatchingAreaClient(
        items=[
            {"WorkItemId": "101", "WorkItemType": "Feature"},
            {"WorkItemId": "102", "WorkItemType": "Feature"},
            {"WorkItemId": "103", "WorkItemType": "Risk"},
        ]
    )
    monkeypatch.setattr("src.commands.probe_ado.load_report_config", _report_config)
    monkeypatch.setattr("src.commands.probe_ado.ADOClient", lambda **_: client)

    json_result = runner.invoke(app, ["probe-ado", "--area", "One\\Adventure\\Acme", "--since", "14d", "--format", "json", "--edition", "acme_weekly"])

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["area"] == "One\\Adventure\\Acme"
    assert payload["auth_method"] == "azure_cli"
    assert payload["total"] == 3
    assert payload["work_item_type_counts"][0] == {"count": 2, "work_item_type": "Feature"}

    csv_result = runner.invoke(app, ["probe-ado", "--area", "One\\Adventure\\Acme", "--since", "14d", "--format", "csv", "--edition", "acme_weekly"])

    assert csv_result.exit_code == 0
    lines = csv_result.stdout.strip().splitlines()
    assert lines[0] == "entry_type,auth_method,edition,area,since,work_item_type,count,detail"
    assert any(line == "summary,azure_cli,acme_weekly,One\\Adventure\\Acme,14d,,3,ok" for line in lines[1:])
    assert any(line == "work_item_type,azure_cli,acme_weekly,One\\Adventure\\Acme,14d,Feature,2," for line in lines[1:])


def test_probe_ado_missing_scope_supports_json(monkeypatch) -> None:
    client = _MissingAreaClient()
    monkeypatch.setattr("src.commands.probe_ado.load_report_config", _report_config)
    monkeypatch.setattr("src.commands.probe_ado.ADOClient", lambda **_: client)

    result = runner.invoke(app, ["probe-ado", "--area", "One\\Adventure\\Acme", "--since", "14d", "--format", "json"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["result"] == "No exact analytics area-path match found"
    assert payload["diagnosis"] == "No analytics descendants found under requested prefix"
    assert payload["suggestions"][0] == "One\\Adventure\\Acme\\Deployment"


def _report_config(_: str) -> SimpleNamespace:
    return SimpleNamespace(
        ado=SimpleNamespace(
            organization="your-org",
            project="One",
            api_timeout_seconds=30,
            work_item_types=["Feature", "Risk", "Scenario", "Key Result"],
            excluded_states=["Removed", "Cut"],
        )
    )


class _MatchingAreaClient:
    def __init__(self, items: list[dict[str, str]]) -> None:
        self.auth_method = "azure_cli"
        self._items = items
        self.filter_expression = ""
        self.probed_ids: list[int] = []

    def find_area_scope_matches(self, area_path: str) -> tuple[str, ...]:
        if area_path == "One\\Adventure\\Acme":
            return ("One\\Adventure\\Acme\\Deployment",)
        return tuple()

    def suggest_area_paths(self, area_path: str) -> tuple[str, ...]:
        return ("One\\Adventure\\Acme\\Deployment",)

    def query_work_items(self, filter_expression: str) -> list[dict[str, str]]:
        self.filter_expression = filter_expression
        return self._items

    def probe_rest_batch(self, work_item_ids: list[int]) -> dict[str, object]:
        self.probed_ids = work_item_ids
        return {}


class _MissingAreaClient:
    auth_method = "azure_cli"

    def find_area_scope_matches(self, area_path: str) -> tuple[str, ...]:
        return tuple()

    def suggest_area_paths(self, area_path: str) -> tuple[str, ...]:
        return (
            "One\\Adventure\\Acme\\Deployment",
            "One\\Adventure\\Acme\\Networking",
        )
