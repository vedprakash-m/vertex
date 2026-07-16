"""ADF-W5.5: `vertex cockpit build/explain/compare` CLI + find_nearest_history_snapshot."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

import src.commands.cockpit as cockpit_module
from src.commands.cockpit import (
    app,
    find_nearest_history_snapshot,
    persist_cockpit_snapshot,
    render_cockpit_comparison,
)
from src.core.cockpit_models import (
    CockpitFinding,
    CockpitSnapshot,
    EconomicsCockpitSummary,
    IntelligenceCockpitSummary,
    ProgramCockpitSummary,
    ReliabilityCockpitSummary,
    SourceCockpitSummary,
    ValueCockpitSummary,
    finalize_cockpit_snapshot,
)

runner = CliRunner()

_T0 = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
_T1 = datetime(2026, 7, 8, 10, 0, 0, tzinfo=timezone.utc)


def _snapshot(*, generated_at: datetime, overall_risk: str = "green", readiness: int = 50,
              findings: tuple[CockpitFinding, ...] = ()) -> CockpitSnapshot:
    snap = CockpitSnapshot(
        schema_version="1", program_id="xpf", edition_id="xpf_weekly",
        generated_at=generated_at, as_of=generated_at,
        program_summary=ProgramCockpitSummary(
            overall_risk=overall_risk, readiness_percent=readiness, blocker_count=0,
            top_three_candidates=(), next_action=None,
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
        findings=findings,
        input_hash="",
    )
    return finalize_cockpit_snapshot(snap)


@pytest.fixture()
def programs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "programs"
    monkeypatch.setattr(cockpit_module, "PROGRAMS_ROOT", root)
    return root


# --- find_nearest_history_snapshot ---


def test_returns_none_when_no_history(programs_root: Path) -> None:
    assert find_nearest_history_snapshot("xpf", _T0, programs_root=programs_root) is None


def test_finds_the_exact_or_nearest_prior_snapshot(programs_root: Path) -> None:
    persist_cockpit_snapshot(_snapshot(generated_at=_T0), programs_root=programs_root)
    persist_cockpit_snapshot(_snapshot(generated_at=_T1, readiness=90), programs_root=programs_root)

    at_between = _T0 + timedelta(days=3)
    found = find_nearest_history_snapshot("xpf", at_between, programs_root=programs_root)
    assert found is not None
    assert found.generated_at == _T0  # nearest AT OR BEFORE, not the later one
    assert found.program_summary.readiness_percent == 50


def test_never_returns_a_snapshot_after_the_requested_time(programs_root: Path) -> None:
    persist_cockpit_snapshot(_snapshot(generated_at=_T1), programs_root=programs_root)
    before_any_history = _T0
    assert find_nearest_history_snapshot("xpf", before_any_history, programs_root=programs_root) is None


# --- cockpit build --as-of ---


def test_build_as_of_renders_the_retained_snapshot(programs_root: Path) -> None:
    persist_cockpit_snapshot(_snapshot(generated_at=_T0), programs_root=programs_root)
    result = runner.invoke(app, ["build", "--program", "xpf", "--as-of", _T0.isoformat()])
    assert result.exit_code == 0, result.output
    html_path = programs_root / "xpf" / "runtime" / "cockpit" / "cockpit.html"
    assert html_path.exists()
    assert "xpf" in html_path.read_text(encoding="utf-8")


def test_build_as_of_with_no_history_exits_nonzero(programs_root: Path) -> None:
    result = runner.invoke(app, ["build", "--program", "xpf", "--as-of", _T0.isoformat()])
    assert result.exit_code == 1


def test_build_rejects_invalid_as_of(programs_root: Path) -> None:
    result = runner.invoke(app, ["build", "--program", "xpf", "--as-of", "not-a-date"])
    assert result.exit_code != 0


# --- cockpit compare ---


def test_compare_renders_a_diff(programs_root: Path) -> None:
    persist_cockpit_snapshot(_snapshot(generated_at=_T0, overall_risk="yellow", readiness=40), programs_root=programs_root)
    persist_cockpit_snapshot(_snapshot(generated_at=_T1, overall_risk="green", readiness=90), programs_root=programs_root)

    result = runner.invoke(app, ["compare", "--program", "xpf", "--from", _T0.isoformat(), "--to", _T1.isoformat()])
    assert result.exit_code == 0, result.output
    assert "yellow -> green" in result.output
    assert "40 -> 90" in result.output


def test_compare_missing_history_exits_nonzero(programs_root: Path) -> None:
    result = runner.invoke(app, ["compare", "--program", "xpf", "--from", _T0.isoformat(), "--to", _T1.isoformat()])
    assert result.exit_code == 1


def test_render_cockpit_comparison_reports_new_and_resolved_findings() -> None:
    earlier = _snapshot(
        generated_at=_T0,
        findings=(CockpitFinding(
            finding_id="f-old", area="program", status="warn", summary="s", detail="d",
            owner=None, next_command=None, evidence_refs=(), observed_at=_T0,
        ),),
    )
    later = _snapshot(
        generated_at=_T1,
        findings=(CockpitFinding(
            finding_id="f-new", area="program", status="warn", summary="s2", detail="d2",
            owner=None, next_command=None, evidence_refs=(), observed_at=_T1,
        ),),
    )
    text = render_cockpit_comparison(earlier, later)
    assert "New findings: f-new" in text
    assert "Resolved/removed findings: f-old" in text


# --- cockpit explain ---


def test_explain_renders_finding_detail(programs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _snapshot(
        generated_at=_T0,
        findings=(CockpitFinding(
            finding_id="f1", area="source", status="blocked", summary="ADO is stale",
            detail="No ADO gather in 10 days.", owner="alex", next_command="vertex gather --program xpf",
            evidence_refs=("sig-1",), observed_at=_T0,
        ),),
    )
    monkeypatch.setattr(cockpit_module, "build_cockpit_snapshot", lambda *a, **k: snapshot)

    result = runner.invoke(app, ["explain", "--program", "xpf", "--finding", "f1"])
    assert result.exit_code == 0, result.output
    assert "ADO is stale" in result.output
    assert "alex" in result.output
    assert "vertex gather --program xpf" in result.output
    assert "sig-1" in result.output


def test_explain_unknown_finding_exits_nonzero(programs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = _snapshot(generated_at=_T0, findings=())
    monkeypatch.setattr(cockpit_module, "build_cockpit_snapshot", lambda *a, **k: snapshot)

    result = runner.invoke(app, ["explain", "--program", "xpf", "--finding", "does-not-exist"])
    assert result.exit_code == 1
