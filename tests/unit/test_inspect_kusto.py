"""B7 — tests for `python cli.py inspect kusto` command.

Covers: --query filter, --format table, --format json, exit codes 0/2/3.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from cli import app


runner = CliRunner()


def _write_gather_state(programs_root: Path, program_id: str, queries: dict) -> None:
    program_dir = programs_root / program_id
    program_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "schema_version": "2.0",
        "integration_errors": 0,
        "queries": queries,
    }
    (program_dir / "gather_state.json").write_text(
        json.dumps(state, indent=2), encoding="utf-8"
    )


def _write_kpis_yaml(programs_root: Path, program_id: str, kpi_ids: list[str]) -> None:
    program_dir = programs_root / program_id
    program_dir.mkdir(parents=True, exist_ok=True)
    lines = ["schema_version: '1.0'\nkpis:\n"]
    for kid in kpi_ids:
        lines.append(
            f"  - id: {kid}\n"
            f"    cluster: https://adventure.kusto.windows.net\n"
            f"    database: adventure\n"
            f"    kql: 'T | take 1'\n"
            f"    section: Test\n"
            f"    render_as: metric_highlight\n"
            f"    confidence: medium\n"
            f"    refresh_on_gather: true\n"
            f"    validated: false\n"
            f"    expected_cardinality: zero_ok\n"
        )
    (program_dir / "kpis.yaml").write_text("".join(lines), encoding="utf-8")


def test_inspect_kusto_table_format_exits_0(tmp_path: Path, monkeypatch) -> None:
    programs_root = tmp_path / "programs"
    ts = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc).isoformat()
    _write_kpis_yaml(programs_root, "acme", ["q-one", "q-two"])
    _write_gather_state(
        programs_root,
        "acme",
        {
            "q-one": {"last_succeeded_at": ts, "last_cycle_succeeded": True, "row_count": 1, "duration_ms": 500, "last_error": None},
            "q-two": {"last_succeeded_at": None, "last_cycle_succeeded": False, "row_count": 0, "duration_ms": 100, "last_error": "oops"},
        },
    )
    monkeypatch.setattr("src.commands.inspect.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["inspect", "kusto", "--program", "acme", "--format", "table"])

    assert result.exit_code == 0
    assert "q-one" in result.output
    assert "q-two" in result.output


def test_inspect_kusto_json_format_exits_0(tmp_path: Path, monkeypatch) -> None:
    programs_root = tmp_path / "programs"
    ts = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc).isoformat()
    _write_kpis_yaml(programs_root, "acme", ["q-one"])
    _write_gather_state(programs_root, "acme", {
        "q-one": {"last_succeeded_at": ts, "last_cycle_succeeded": True, "row_count": 1, "duration_ms": 200, "last_error": None},
    })
    monkeypatch.setattr("src.commands.inspect.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["inspect", "kusto", "--program", "acme", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["program_id"] == "acme"
    assert len(payload["queries"]) == 1
    assert payload["queries"][0]["query_id"] == "q-one"


def test_inspect_kusto_query_filter_returns_single_row(tmp_path: Path, monkeypatch) -> None:
    programs_root = tmp_path / "programs"
    ts = datetime(2026, 5, 18, 12, 0, 0, tzinfo=timezone.utc).isoformat()
    _write_kpis_yaml(programs_root, "acme", ["q-one", "q-two"])
    _write_gather_state(programs_root, "acme", {
        "q-one": {"last_succeeded_at": ts, "last_cycle_succeeded": True, "row_count": 1, "duration_ms": 200, "last_error": None},
        "q-two": {"last_succeeded_at": ts, "last_cycle_succeeded": True, "row_count": 2, "duration_ms": 100, "last_error": None},
    })
    monkeypatch.setattr("src.commands.inspect.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["inspect", "kusto", "--program", "acme", "--query", "q-one", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert len(payload["queries"]) == 1
    assert payload["queries"][0]["query_id"] == "q-one"


def test_inspect_kusto_exits_2_when_no_matching_queries(tmp_path: Path, monkeypatch) -> None:
    programs_root = tmp_path / "programs"
    _write_kpis_yaml(programs_root, "acme", ["q-one"])
    _write_gather_state(programs_root, "acme", {})
    monkeypatch.setattr("src.commands.inspect.PROGRAMS_ROOT", programs_root)

    # --query filter for a nonexistent id → 0 rows → exit 2
    result = runner.invoke(app, ["inspect", "kusto", "--program", "acme", "--query", "nonexistent"])

    assert result.exit_code == 2


def test_inspect_kusto_exits_3_when_gather_state_missing(tmp_path: Path, monkeypatch) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "acme").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("src.commands.inspect.PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["inspect", "kusto", "--program", "acme"])

    assert result.exit_code == 3
