"""ADF-W5.8 remainder (specs/arch-data-fix.md Section 8.2.5): the 5
SAFE-BOUNDED alert categories wired in this pass -- channel budget exceeded,
required-source unhealthy, WorkIQ inline invocation attempted, context
budget exceeded, and duplicate state path. Each of these was an existing
detector with no alert-emission wiring before this pass; these tests verify
the wiring, not the underlying detection logic (already covered by each
module's own pre-existing test suite).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.core.alerts import read_alerts

_NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


# --------------------------------------------------------------------------------------
# channel_budget_exceeded (channel_runtime.py)
# --------------------------------------------------------------------------------------


def test_channel_budget_alert_fires_on_degradation(tmp_path: Path) -> None:
    from src.commands.gather_pipeline.channel_runtime import _emit_channel_budget_alert_best_effort

    _emit_channel_budget_alert_best_effort(
        program_id="xpf", channel="ado", stage="discovery", degrade_reason="timeout", programs_root=tmp_path,
    )
    alerts = read_alerts("xpf", programs_root=tmp_path)
    assert len(alerts) == 1
    assert alerts[0].category == "channel_budget_exceeded"
    assert alerts[0].entity_type == "channel"
    assert alerts[0].entity_id == "ado"
    assert "timeout" in alerts[0].message


def test_channel_budget_alert_is_best_effort_and_does_not_raise(tmp_path: Path) -> None:
    from src.commands.gather_pipeline.channel_runtime import _emit_channel_budget_alert_best_effort

    # A path that cannot be created (invalid on most filesystems) should not raise.
    bad_root = tmp_path / "\x00bad"
    try:
        _emit_channel_budget_alert_best_effort(
            program_id="xpf", channel="ado", stage="hydration", degrade_reason=None, programs_root=bad_root,
        )
    except (OSError, ValueError):
        pytest.fail("channel budget alert emission must be best-effort and never raise")


# --------------------------------------------------------------------------------------
# required_source_unhealthy (confirm.py)
# --------------------------------------------------------------------------------------


def test_source_health_alert_fires_only_for_failed_gates(tmp_path: Path) -> None:
    from src.commands.confirm import _emit_source_health_alerts_best_effort
    from src.core.quality_gates import QualityGateReport
    from src.core.quality_gates.models import GateEvaluation

    report = QualityGateReport(results=(
        GateEvaluation(gate_id="QG-SG-01", passed=False, message="transcript unhealthy", exit_code=1),
        GateEvaluation(gate_id="QG-SG-02", passed=True, message="ok", exit_code=0),
    ))
    _emit_source_health_alerts_best_effort(report, program_id="xpf", edition_name="xpf_weekly", programs_root=tmp_path)

    alerts = read_alerts("xpf", programs_root=tmp_path)
    assert len(alerts) == 1
    assert alerts[0].category == "required_source_unhealthy"
    assert alerts[0].entity_id == "xpf_weekly:QG-SG-01"
    assert alerts[0].message == "transcript unhealthy"


def test_source_health_alert_noop_when_all_gates_pass(tmp_path: Path) -> None:
    from src.commands.confirm import _emit_source_health_alerts_best_effort
    from src.core.quality_gates import QualityGateReport
    from src.core.quality_gates.models import GateEvaluation

    report = QualityGateReport(results=(GateEvaluation(gate_id="QG-SG-01", passed=True, message="ok", exit_code=0),))
    _emit_source_health_alerts_best_effort(report, program_id="xpf", edition_name="xpf_weekly", programs_root=tmp_path)
    assert read_alerts("xpf", programs_root=tmp_path) == ()


# --------------------------------------------------------------------------------------
# workiq_inline_invocation_attempted (report.py)
# --------------------------------------------------------------------------------------


def test_workiq_inline_alert_fires_when_pre_report_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.commands.report as report_module

    resolved = SimpleNamespace(
        program=SimpleNamespace(id="xpf", m365=SimpleNamespace(enabled=True, workiq_enrich_schedule="pre_report"))
    )
    monkeypatch.setattr(report_module, "resolve_edition", lambda edition_name, *, programs_root: resolved)
    monkeypatch.setattr(report_module, "PROGRAMS_ROOT", tmp_path)

    report_module._maybe_auto_run_workiq_enrich(
        edition_name="xpf_weekly", dry_run=False, offline=False, show_progress=False,
    )
    alerts = read_alerts("xpf", programs_root=tmp_path)
    assert len(alerts) == 1
    assert alerts[0].category == "workiq_inline_invocation_attempted"


def test_workiq_inline_alert_does_not_fire_when_m365_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.commands.report as report_module

    resolved = SimpleNamespace(
        program=SimpleNamespace(id="xpf", m365=SimpleNamespace(enabled=False, workiq_enrich_schedule="pre_report"))
    )
    monkeypatch.setattr(report_module, "resolve_edition", lambda edition_name, *, programs_root: resolved)
    monkeypatch.setattr(report_module, "PROGRAMS_ROOT", tmp_path)

    report_module._maybe_auto_run_workiq_enrich(
        edition_name="xpf_weekly", dry_run=False, offline=False, show_progress=False,
    )
    assert read_alerts("xpf", programs_root=tmp_path) == ()


def test_workiq_inline_alert_does_not_fire_when_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.commands.report as report_module

    called = False

    def _fail_if_called(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(report_module, "resolve_edition", _fail_if_called)
    report_module._maybe_auto_run_workiq_enrich(edition_name="xpf_weekly", dry_run=False, offline=True, show_progress=False)
    assert called is False


# --------------------------------------------------------------------------------------
# context_budget_exceeded (context_compiler.py)
# --------------------------------------------------------------------------------------


def test_context_budget_alert_fires_on_rejection(tmp_path: Path) -> None:
    from src.core.context_compiler import (
        ContextCompileRejected,
        ContextCompileRequest,
        DeterministicContextCompiler,
    )

    compiler = DeterministicContextCompiler(programs_root=tmp_path)
    request = ContextCompileRequest(
        program_id="xpf",
        edition_id=None,
        feature="risk_proposal_generator",
        prompt_version="1",
        system_instructions="x" * 10,
        output_schema_text="y" * 10,
        required_evidence=(),
        optional_evidence=(),
        max_input_tokens=1,  # deliberately tiny to force QG-32 rejection
        reserved_output_tokens=0,
        per_source_quotas={},
    )
    with pytest.raises(ContextCompileRejected):
        compiler.compile(request, validation_context=SimpleNamespace())

    alerts = read_alerts("xpf", programs_root=tmp_path)
    assert len(alerts) == 1
    assert alerts[0].category == "context_budget_exceeded"
    assert alerts[0].entity_id == "risk_proposal_generator"


# --------------------------------------------------------------------------------------
# duplicate_state_path (state_authority.py)
# --------------------------------------------------------------------------------------


def test_duplicate_state_path_alert_fires_on_stray_database(tmp_path: Path) -> None:
    from src.core.quality_gates.state_authority import evaluate_state_authority_gate

    programs_root = tmp_path / "programs"
    db_root = tmp_path / "vertex-db"
    stray_path = programs_root / "xpf" / "vertex.sqlite3"
    stray_path.parent.mkdir(parents=True)
    stray_path.write_bytes(b"fake sqlite content")

    result = evaluate_state_authority_gate("xpf", programs_root=programs_root, db_root=db_root)
    assert result.passed is False

    alerts = read_alerts("xpf", programs_root=programs_root)
    assert len(alerts) == 1
    assert alerts[0].category == "duplicate_state_path"
    assert alerts[0].severity == "error"


def test_duplicate_state_path_alert_does_not_fire_when_unambiguous(tmp_path: Path) -> None:
    from src.core.quality_gates.state_authority import evaluate_state_authority_gate

    programs_root = tmp_path / "programs"
    db_root = tmp_path / "vertex-db"
    result = evaluate_state_authority_gate("xpf", programs_root=programs_root, db_root=db_root)
    assert result.passed is True
    assert read_alerts("xpf", programs_root=programs_root) == ()
