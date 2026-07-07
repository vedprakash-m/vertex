from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.commands import inspect as inspect_command
from src.commands.inspect import inspect_kusto_state


runner = CliRunner()


def test_inspect_kusto_state_returns_wired_queries_with_runtime_state(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
schema_version: "1.0"
kpis:
  - id: acme-deployment-p50-p90
    workstream_ids: [acme]
    cluster: https://xdeployment.kusto.windows.net
    database: Deployment
    kql: PFDeployments | take 1
    section: Deployment Velocity
    render_as: metric_highlight
    confidence: medium
    refresh_on_gather: true
    validated: true
""".strip(),
        encoding="utf-8",
    )
    (program_dir / "gather_state.json").write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "queries": {
                    "acme-deployment-p50-p90": {
                        "last_succeeded_at": "2026-05-17T14:02:14Z",
                        "last_cycle_succeeded": True,
                        "row_count": 1,
                        "duration_ms": 2873,
                        "last_error": None,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    rows = inspect_kusto_state("acme", programs_root=programs_root)

    assert rows == [
        {
            "query_id": "acme-deployment-p50-p90",
            "validated": True,
            "last_cycle_succeeded": True,
            "last_succeeded_at": "2026-05-17T14:02:14Z",
            "row_count": 1,
            "duration_ms": 2873,
            "last_error": None,
        }
    ]


def test_inspect_kusto_state_filters_by_query_and_since(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
schema_version: "1.0"
kpis:
  - id: recent-query
    workstream_ids: [acme]
    cluster: https://adventure.kusto.windows.net
    database: adventure
    kql: Recent | take 1
    section: Fleet Health
    render_as: metric_highlight
    confidence: medium
    refresh_on_gather: true
  - id: old-query
    workstream_ids: [acme]
    cluster: https://adventure.kusto.windows.net
    database: adventure
    kql: Old | take 1
    section: Fleet Health
    render_as: metric_highlight
    confidence: medium
    refresh_on_gather: true
""".strip(),
        encoding="utf-8",
    )
    (program_dir / "gather_state.json").write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "queries": {
                    "recent-query": {"last_succeeded_at": "2026-05-17T14:02:14Z"},
                    "old-query": {"last_succeeded_at": "2026-05-01T14:02:14Z"},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.commands.inspect.datetime", _FrozenDateTime)

    filtered = inspect_kusto_state("acme", query_id="recent-query", since="7d", programs_root=programs_root)

    assert [row["query_id"] for row in filtered] == ["recent-query"]


def test_inspect_kusto_cli_returns_json_output(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
schema_version: "1.0"
kpis:
  - id: acme-buildout-slo
    workstream_ids: [acme]
    cluster: https://azcis.kusto.windows.net
    database: azcispub
    kql: BuildoutInfo | take 1
    section: Buildout
    render_as: metric_highlight
    confidence: medium
    refresh_on_gather: true
    validated: true
""".strip(),
        encoding="utf-8",
    )
    (program_dir / "gather_state.json").write_text(
        json.dumps({"schema_version": "2.0", "queries": {"acme-buildout-slo": {"row_count": 1}}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(inspect_command, "PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["inspect", "kusto", "--program", "acme", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["program_id"] == "acme"
    assert payload["queries"][0]["query_id"] == "acme-buildout-slo"


def test_inspect_kusto_cli_returns_code_2_when_query_missing(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text('schema_version: "1.0"\nkpis: []\n', encoding="utf-8")
    (program_dir / "gather_state.json").write_text(json.dumps({"schema_version": "2.0", "queries": {}}), encoding="utf-8")

    monkeypatch.setattr(inspect_command, "PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["inspect", "kusto", "--program", "acme", "--query", "missing-query"])

    assert result.exit_code == 2
    assert "No matching wired Kusto queries found" in result.stdout


def test_inspect_kusto_cli_returns_code_3_when_state_missing(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text('schema_version: "1.0"\nkpis: []\n', encoding="utf-8")

    monkeypatch.setattr(inspect_command, "PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["inspect", "kusto", "--program", "acme"])

    assert result.exit_code == 3
    assert "Gather state file is missing" in result.stdout


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz: timezone | None = None) -> datetime:
        current = datetime(2026, 5, 17, 15, 0, tzinfo=timezone.utc)
        if tz is None:
            return current.replace(tzinfo=None)
        return current.astimezone(tz)