"""ADF-W5.6 (Section 10.7): cockpit onboarding walkthrough on first run."""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import src.commands.cockpit as cockpit_module
from src.commands.cockpit import app

runner = CliRunner()


@pytest.fixture()
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr(cockpit_module, "PROGRAMS_ROOT", programs_root)
    return programs_root


def _fake_snapshot():
    from datetime import datetime, timezone

    from src.core.cockpit_models import (
        CockpitSnapshot,
        EconomicsCockpitSummary,
        IntelligenceCockpitSummary,
        ProgramCockpitSummary,
        ReliabilityCockpitSummary,
        SourceCockpitSummary,
        ValueCockpitSummary,
        finalize_cockpit_snapshot,
    )

    now = datetime(2026, 7, 13, tzinfo=timezone.utc)
    snap = CockpitSnapshot(
        schema_version="1", program_id="xpf", edition_id=None, generated_at=now, as_of=now,
        program_summary=ProgramCockpitSummary(
            overall_risk="green", readiness_percent=80, blocker_count=0, top_three_candidates=(), next_action=None
        ),
        source_summary=SourceCockpitSummary(
            required_healthy=5, required_total=5, stale_sources=(), degraded_sources=(),
            manual_sources=(), newest_watermarks={},
        ),
        intelligence_summary=IntelligenceCockpitSummary(
            lineage_coverage=0.5, verification_coverage=0.3, extraction_quality=(), contradiction_count=0
        ),
        economics_summary=EconomicsCockpitSummary(
            frontier_avoidance=0.6, frontier_cost_usd=1.5, cache_hit_rate=0.2, context_tokens_in=100
        ),
        value_summary=ValueCockpitSummary(metrics=(), time_savings_certification=None),
        reliability_summary=ReliabilityCockpitSummary(
            outbox_pending=0, uncertain_remote_state=0, dead_letter_count=0,
            duplicate_preventions=0, audit_coverage=None,
        ),
        findings=(),
        input_hash="",
    )
    return finalize_cockpit_snapshot(snap)


def test_first_run_shows_the_walkthrough(_isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cockpit_module, "build_cockpit_snapshot", lambda *a, **k: _fake_snapshot())
    result = runner.invoke(app, ["show", "--program", "xpf"])
    assert result.exit_code == 0, result.output
    assert "Welcome to the Vertex cockpit" in result.output
    assert "Program vs. platform health" in result.output


def test_second_run_does_not_repeat_the_walkthrough(_isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cockpit_module, "build_cockpit_snapshot", lambda *a, **k: _fake_snapshot())
    runner.invoke(app, ["show", "--program", "xpf"])
    second = runner.invoke(app, ["show", "--program", "xpf"])
    assert second.exit_code == 0, second.output
    assert "Welcome to the Vertex cockpit" not in second.output


def test_no_persist_never_marks_first_run_consumed(_isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cockpit_module, "build_cockpit_snapshot", lambda *a, **k: _fake_snapshot())
    preview = runner.invoke(app, ["show", "--program", "xpf", "--no-persist"])
    assert "Welcome to the Vertex cockpit" not in preview.output  # --no-persist never triggers onboarding

    real_first_run = runner.invoke(app, ["show", "--program", "xpf"])
    assert "Welcome to the Vertex cockpit" in real_first_run.output


def test_json_format_never_includes_the_walkthrough(_isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cockpit_module, "build_cockpit_snapshot", lambda *a, **k: _fake_snapshot())
    result = runner.invoke(app, ["show", "--program", "xpf", "--format", "json"])
    assert result.exit_code == 0, result.output
    assert "Welcome to the Vertex cockpit" not in result.output
    import json
    json.loads(result.output)  # must still be valid, parseable JSON
