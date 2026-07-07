from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.ai.client import AIClient
from src.core.kusto_client import KustoClient
from tests.support.ado_cassettes import load_cassette_work_items
from tests.support.external_cassettes import load_aoai_cassette_response, load_external_cassette_payload, load_kusto_cassette_rows_and_schema
from tests.support.report_test_setup import disable_kusto_in_report_copy, stage_v2_report_workspace
from src.commands.report import generate_report_draft


FROZEN_NOW = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)


@pytest.mark.integration
def test_ado_cassette_replays_report_generation(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=FROZEN_NOW,
        work_item_loader=lambda bundle, timestamp: load_cassette_work_items("cold_start", timestamp),
        open_browser=False,
    )

    payload = load_external_cassette_payload("cold_start")
    assert len(artifacts.report.items) == len(payload["work_items"])
    assert artifacts.html_path is not None and artifacts.html_path.exists()
    assert artifacts.manifest.ado_calls == 1


@pytest.mark.integration
def test_kusto_cassette_replays_execute_with_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    rows, schema = load_kusto_cassette_rows_and_schema("kusto_velocity")

    class _CassetteClient:
        def __init__(self, connection: tuple[str, object]) -> None:
            self.connection = connection

        def execute(self, database: str, kql: str, properties=None):
            assert database == "xdataanalytics"
            assert "VelocityMetrics" in kql
            return SimpleNamespace(primary_results=[_CassetteTable(schema, rows)])

    class _CassetteKcsb:
        @staticmethod
        def with_azure_token_credential(cluster: str, credential: object) -> tuple[str, object]:
            return (cluster, credential)

    class _CassetteRequestProperties:
        def __init__(self) -> None:
            self.options: dict[str, object] = {}

        def set_option(self, key: str, value: object) -> None:
            self.options[key] = value

    class _CassetteCredential:
        pass

    client = KustoClient()
    monkeypatch.setattr(
        client,
        "_get_sdk_types",
        lambda: (_CassetteClient, _CassetteKcsb, _CassetteRequestProperties, _CassetteCredential),
    )

    actual_rows, actual_schema = client.execute_with_schema(
        "https://adventure.kusto.windows.net",
        "xdataanalytics",
        "VelocityMetrics | take 2",
    )

    assert actual_rows == rows
    assert actual_schema == schema


@pytest.mark.integration
def test_aoai_cassette_replays_structured_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    cassette_response = load_aoai_cassette_response("aoai_structured_summary")

    class _CassetteAzureOpenAI:
        last_request_kwargs: dict[str, object] | None = None

        def __init__(self, **kwargs) -> None:
            self.chat = SimpleNamespace(completions=self)

        def create(self, **kwargs):
            type(self).last_request_kwargs = kwargs
            return cassette_response

    monkeypatch.setattr(
        AIClient,
        "_get_sdk_types",
        lambda self: (_CassetteAzureOpenAI, Exception, Exception),
    )

    client = AIClient("structured-model", 0.2, 0.5)
    result = client.structured(
        "system",
        "user",
        parser=lambda payload: (payload["summary"], payload["risk"]),
        prompt_version="cassette.v1",
    )

    assert result == ("Deployment velocity is stable and the fleet pilot remains on track.", "medium")
    assert _CassetteAzureOpenAI.last_request_kwargs is not None
    assert _CassetteAzureOpenAI.last_request_kwargs["response_format"] == {"type": "json_object"}
    assert client.usage_stats.total_tokens == 240


class _CassetteTable:
    def __init__(self, columns, rows) -> None:
        self.columns = [SimpleNamespace(column_name=column.name, column_type=column.type_name) for column in columns]
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)
