"""ADF-W5.13 remainder: `vertex admin metrics-rollup` CLI."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

import src.commands.admin_metrics_rollup as rollup_module
from src.commands.admin_metrics_rollup import admin_metrics_rollup_command
from src.core.weekly_metrics_store import query_weekly_aggregates

runner = CliRunner()
app = typer.Typer()
app.command()(admin_metrics_rollup_command)


@pytest.fixture(autouse=True)
def _programs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "programs"
    monkeypatch.setattr(rollup_module, "PROGRAMS_ROOT", root)
    return root


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_rollup_single_family_explicit_week(_programs_root: Path) -> None:
    week_start = date.fromisocalendar(2026, 28, 3)
    ts = datetime.combine(week_start, datetime.min.time(), tzinfo=timezone.utc).isoformat()
    path = _programs_root / "xpf" / "runtime" / "tier_decisions.jsonl"
    _write_jsonl(path, [{"recorded_at": ts, "latency_ms": 100.0}, {"recorded_at": ts, "latency_ms": 200.0}])

    result = runner.invoke(app, ["--program", "xpf", "--family", "tier_decisions", "--iso-week", "2026-W28"])
    assert result.exit_code == 0, result.output
    assert "rolled up 2 row(s)" in result.output

    records = query_weekly_aggregates("xpf", "tier_decisions", programs_root=_programs_root)
    assert len(records) == 1
    assert records[0].metrics["latency_ms_mean"] == 150.0


def test_rollup_all_families_defaults_reports_no_rows(_programs_root: Path) -> None:
    result = runner.invoke(app, ["--program", "xpf", "--iso-week", "2026-W28"])
    assert result.exit_code == 0, result.output
    assert "tier_decisions: no rows" in result.output
    assert "ai_telemetry: no rows" in result.output
    assert "run_telemetry: no rows" in result.output


def test_rollup_rejects_unknown_family(_programs_root: Path) -> None:
    result = runner.invoke(app, ["--program", "xpf", "--family", "not_a_family"])
    assert result.exit_code != 0


def test_rollup_rejects_malformed_iso_week(_programs_root: Path) -> None:
    result = runner.invoke(app, ["--program", "xpf", "--iso-week", "not-a-week"])
    assert result.exit_code != 0


def test_rollup_defaults_to_current_iso_week(_programs_root: Path) -> None:
    now = datetime.now(timezone.utc)
    iso_year, iso_week, _ = now.date().isocalendar()
    path = _programs_root / "xpf" / "_state" / "ai_telemetry.jsonl"
    _write_jsonl(path, [{"ts": now.isoformat(), "latency_ms": 50.0}])

    result = runner.invoke(app, ["--program", "xpf", "--family", "ai_telemetry"])
    assert result.exit_code == 0, result.output
    assert "rolled up 1 row(s)" in result.output

    records = query_weekly_aggregates("xpf", "ai_telemetry", programs_root=_programs_root)
    assert len(records) == 1
    assert records[0].iso_year == iso_year
    assert records[0].iso_week == iso_week
