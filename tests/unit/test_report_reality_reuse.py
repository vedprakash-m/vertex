from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.core.exceptions import ConfigError
from src.core.fact_sor_state import save_fact_sor_state
from src.core.models import Confidence
from src.core.models_v2 import Milestone, MilestoneAssessment, MilestoneStatus
from src.core.pipeline import StageContext
from src.core.program_fact_store import ProgramFactInput, ProgramFactSnapshot, ProgramFactStore
from src.core.program_reality import ProgramReality
from src.core.stages import action_stage, milestone_stage, risk_stage, sor_gated_load
from tests.unit.test_config_loader import _write_demo_v2_layout


@pytest.fixture
def report_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    when = datetime(2026, 10, 25, tzinfo=timezone.utc)
    risk = SimpleNamespace(id="risk-1")
    assessment = SimpleNamespace(
        record=risk,
        lineage=SimpleNamespace(source_document_key="synthetic:source", approval_event_id="approval-1"),
        truth_level=SimpleNamespace(value="corroborated"), disputed=True, stale=False,
    )
    reality = SimpleNamespace(
        milestones=Mock(return_value=()), dependencies=Mock(return_value=()),
        risks=Mock(return_value=(assessment,)),
    )
    loader = Mock(return_value=reality)
    mode = {"default": "primary", "families": {}}
    monkeypatch.setattr(sor_gated_load, "resolve_fact_sor_mode", lambda **_: mode["default"])
    monkeypatch.setattr(
        sor_gated_load, "load_fact_sor_state",
        lambda *_, **__: SimpleNamespace(family_modes=mode["families"]),
    )
    family_mode = lambda _, family, **__: mode["families"].get(family, mode["default"])
    monkeypatch.setattr(sor_gated_load, "resolve_family_sor_mode", family_mode)
    monkeypatch.setattr(milestone_stage, "resolve_family_sor_mode", family_mode)
    monkeypatch.setattr(risk_stage, "assess_risk_staleness", lambda *_: True)
    monkeypatch.setattr(risk_stage, "_load_current_risks", Mock(return_value=(risk,)))
    monkeypatch.setattr(milestone_stage, "_load_current_milestones", Mock(return_value=()))
    monkeypatch.setattr(milestone_stage, "_load_current_dependencies", Mock(return_value=()))
    ctx = StageContext(
        edition_name="synthetic_weekly", programs_root=tmp_path / "programs",
        archive_root=tmp_path / "archive", data_as_of=when, started_at=when,
        resolved_v2=SimpleNamespace(paths=SimpleNamespace(program_id="synthetic")),
        fact_snapshot=ProgramFactSnapshot("synthetic", when, ()),
        stage_support=SimpleNamespace(load_program_reality=loader),
    )
    return ctx, loader, reality, mode


@pytest.mark.parametrize("injected", [False, True])
def test_ordinary_stages_reduce_actual_loader_calls_and_preserve_risk_parity(
    report_context, monkeypatch: pytest.MonkeyPatch, injected: bool,
) -> None:
    ctx, loader, reality, _ = report_context
    if not injected:
        monkeypatch.setattr(ProgramReality, "load", loader)
        ctx = replace(ctx, stage_support=None)
    independent_risk = risk_stage.RiskStage().execute(ctx)
    milestone_ctx = milestone_stage.MilestoneStage().execute(ctx)
    assert loader.call_count == 2
    loader.reset_mock()

    result = risk_stage.RiskStage().execute(milestone_ctx)

    assert loader.call_count == 0
    for field in ("risks", "risk_assessments", "risk_warnings", "risk_lineage", "stale_risk_ids"):
        assert getattr(result, field) == getattr(independent_risk, field)
    loader.reset_mock()
    fresh_result = risk_stage.RiskStage().execute(milestone_stage.MilestoneStage().execute(ctx))
    assert loader.call_count == 1
    assert fresh_result.risk_assessments == tuple(reality.risks())
    assert fresh_result.milestone_assessments == ()


@pytest.mark.parametrize("change", [
    "programs_root", "archive_root", "edition_name", "data_as_of", "started_at",
    "fact_snapshot", "fact_snapshot_pin", "stage_support", "run_id",
])
def test_changed_scope_does_not_reuse(report_context, change: str) -> None:
    ctx, loader, _, _ = report_context
    retained = milestone_stage.MilestoneStage().execute(ctx)
    if change.endswith("_root"):
        value = getattr(ctx, change) / "other"
    elif change in {"data_as_of", "started_at"}:
        value = getattr(ctx, change) + timedelta(seconds=1)
    elif change == "fact_snapshot":
        value = replace(ctx.fact_snapshot)
    elif change == "fact_snapshot_pin":
        value = SimpleNamespace(snapshot_id="new-capture")
    elif change == "stage_support":
        value = SimpleNamespace(load_program_reality=Mock(return_value=loader.return_value))
    else:
        value = "other"
    changed = replace(retained, **{change: value})
    risk_stage.RiskStage().execute(changed)
    assert loader.call_count == (1 if change == "stage_support" else 2)
    if change == "stage_support":
        assert value.load_program_reality.call_count == 1


@pytest.mark.parametrize("default,families,expected_calls", [
    ("legacy", {}, 0),
    ("shadow", {}, 2),
    ("primary", {"unrelated": "shadow"}, 2),
    ("legacy", {"workitem.state": "primary", "judgment": "primary"}, 2),
])
def test_non_primary_authority_retains_existing_loading(
    report_context, default: str, families: dict[str, str], expected_calls: int,
) -> None:
    ctx, loader, _, mode = report_context
    mode.update(default=default, families=families)
    risk_stage.RiskStage().execute(milestone_stage.MilestoneStage().execute(ctx))
    assert loader.call_count == expected_calls


def test_changed_authoritative_eligibility_does_not_reuse(report_context) -> None:
    ctx, loader, _, mode = report_context
    retained = milestone_stage.MilestoneStage().execute(ctx)
    mode["families"] = {"unrelated": "shadow"}
    risk_stage.RiskStage().execute(retained)
    assert loader.call_count == 2
    assert "fact_snapshot" not in loader.call_args.kwargs


def test_no_retained_snapshot_does_not_reuse(report_context) -> None:
    ctx, loader, _, _ = report_context
    ctx = replace(ctx, fact_snapshot=None)
    risk_stage.RiskStage().execute(milestone_stage.MilestoneStage().execute(ctx))
    assert loader.call_count == 2


def test_unretained_direct_readers_preserve_default_inputs(
    report_context, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, loader, _, _ = report_context
    monkeypatch.setattr(ProgramReality, "load", loader)
    ctx = replace(ctx, fact_snapshot=None, archive_root=None, stage_support=None)
    risk_stage.RiskStage().execute(milestone_stage.MilestoneStage().execute(ctx))
    assert loader.call_count == 2
    assert loader.call_args_list[0] == loader.call_args_list[1]
    assert "archive_root" not in loader.call_args.kwargs
    assert "fact_snapshot" not in loader.call_args.kwargs


@pytest.mark.parametrize("error", [ConfigError("synthetic unavailable"), RuntimeError("synthetic unavailable")])
def test_failed_milestone_load_does_not_retain(
    report_context, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    ctx, loader, reality, _ = report_context
    monkeypatch.setenv("VERTEX_REPORT_ALLOW_LEGACY_MILESTONE_ROLLBACK", "1")
    loader.side_effect = [error, reality]
    milestone_ctx = milestone_stage.MilestoneStage().execute(ctx)
    assert milestone_ctx.milestone_warnings
    result = risk_stage.RiskStage().execute(milestone_ctx)
    assert loader.call_count == 2
    assert result.risks
    assert not result.risk_warnings


def test_failed_milestone_projection_does_not_retain(
    report_context, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, loader, reality, _ = report_context
    monkeypatch.setenv("VERTEX_REPORT_ALLOW_LEGACY_MILESTONE_ROLLBACK", "1")
    reality.dependencies.side_effect = RuntimeError("projection failed")
    milestone_ctx = milestone_stage.MilestoneStage().execute(ctx)
    assert milestone_ctx.milestone_warnings
    risk_stage.RiskStage().execute(milestone_ctx)
    assert loader.call_count == 2


def test_failed_milestone_load_requires_audited_rollback(
    report_context, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, loader, _, _ = report_context
    monkeypatch.delenv("VERTEX_REPORT_ALLOW_LEGACY_MILESTONE_ROLLBACK", raising=False)
    loader.side_effect = RuntimeError("loader unavailable")
    with pytest.raises(ConfigError, match="audited legacy rollback"):
        milestone_stage.MilestoneStage().execute(ctx)


@pytest.mark.parametrize("error", [ConfigError("risk unavailable"), RuntimeError("risk unavailable")])
def test_reused_risk_projection_preserves_failure_behavior(
    report_context, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    ctx, loader, reality, _ = report_context
    monkeypatch.setenv("VERTEX_REPORT_ALLOW_LEGACY_RISK_ROLLBACK", "1")
    retained = milestone_stage.MilestoneStage().execute(ctx)
    reality.risks.side_effect = error
    independent = risk_stage.RiskStage().execute(ctx)
    loader.reset_mock()
    reused = risk_stage.RiskStage().execute(retained)
    assert loader.call_count == 0
    for field in ("risks", "risk_assessments", "risk_lineage", "risk_warnings", "stale_risk_ids"):
        assert getattr(reused, field) == getattr(independent, field)


def test_reused_risk_projection_failure_requires_audited_rollback(
    report_context, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, loader, reality, _ = report_context
    monkeypatch.delenv("VERTEX_REPORT_ALLOW_LEGACY_RISK_ROLLBACK", raising=False)
    retained = milestone_stage.MilestoneStage().execute(ctx)
    reality.risks.side_effect = RuntimeError("projection failed")
    with pytest.raises(ConfigError, match="audited legacy rollback"):
        risk_stage.RiskStage().execute(retained)
    assert loader.call_count == 1


def test_empty_risk_cross_check_warning_survives_reuse(report_context) -> None:
    ctx, loader, reality, _ = report_context
    reality.risks.return_value = ()
    result = risk_stage.RiskStage().execute(milestone_stage.MilestoneStage().execute(ctx))
    assert loader.call_count == 1
    assert "cross-check" in result.risk_warnings[0]


def test_empty_risk_cross_check_without_legacy_data_stays_quiet(
    report_context, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, loader, reality, _ = report_context
    reality.risks.return_value = ()
    monkeypatch.setattr(risk_stage, "_load_current_risks", Mock(return_value=()))
    result = risk_stage.RiskStage().execute(milestone_stage.MilestoneStage().execute(ctx))
    assert loader.call_count == 1
    assert result.risk_warnings == ()


def test_fresh_contexts_never_share_retained_reality(report_context) -> None:
    ctx, loader, _, _ = report_context
    for _ in range(2):
        risk_stage.RiskStage().execute(milestone_stage.MilestoneStage().execute(replace(ctx)))
    assert loader.call_count == 2


def test_nonempty_milestone_health_and_lineage_survive_reuse(
    report_context, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, loader, reality, _ = report_context
    milestone = Milestone(
        id="milestone-1", program_id="synthetic", name="Synthetic milestone",
        target_date=ctx.data_as_of.date(), owner_alias="synthetic-owner",
        status=MilestoneStatus.AT_RISK, exit_criteria=(), linked_workstream_ids=(),
        linked_work_item_ids=(1,),
    )
    fact_assessment = SimpleNamespace(
        record=milestone, lineage=reality.risks.return_value[0].lineage,
        truth_level=SimpleNamespace(value="corroborated"), disputed=True, stale=True,
    )
    reality.milestones.return_value = (fact_assessment,)
    health = MilestoneAssessment(
        milestone_id=milestone.id, computed_health=MilestoneStatus.AT_RISK,
        blocked_criteria=(), slip_probability=0.5, critical_path=False,
        confidence=Confidence.HIGH, reasoning="synthetic assessment",
    )
    trajectory_store = Mock()
    monkeypatch.setattr(milestone_stage, "build_trajectory_store_for_program_id", Mock(return_value=trajectory_store))
    assessor = Mock(return_value=health)
    monkeypatch.setattr(milestone_stage, "assess_milestone_health", assessor)
    monkeypatch.setattr(milestone_stage, "build_critical_path", Mock(return_value=(milestone,)))
    monkeypatch.setattr(milestone_stage, "_emit_coverage_gap_findings", Mock())
    result = risk_stage.RiskStage().execute(milestone_stage.MilestoneStage().execute(ctx))
    assert loader.call_count == 1
    assert result.milestones == (milestone,)
    assert result.milestone_assessments == (replace(health, critical_path=True),)
    assert result.milestone_lineage[milestone.id] == {
        "source_document_key": "synthetic:source", "approval_event_id": "approval-1",
        "truth_level": "corroborated", "disputed": "true", "stale": "true",
    }
    assessor.assert_called_once_with(milestone, ctx.items, {1: trajectory_store.read.return_value}, ctx.data_as_of)
    assert result.milestone_warnings == ()


def test_retrying_failed_milestone_does_not_leave_previous_retained_reality(
    report_context,
) -> None:
    ctx, loader, reality, _ = report_context
    retained = milestone_stage.MilestoneStage().execute(ctx)
    loader.side_effect = [ConfigError("retry failed"), reality]
    retried = milestone_stage.MilestoneStage().execute(replace(retained, milestone_assessments=None))
    assert retried.report_reality is None
    assert retried.report_reality_scope is None
    risk_stage.RiskStage().execute(retried)
    assert loader.call_count == 3


@pytest.mark.parametrize("missing", [False, True])
def test_reuse_never_bypasses_snapshot_guard(report_context, missing: bool) -> None:
    ctx, loader, _, _ = report_context
    retained = milestone_stage.MilestoneStage().execute(ctx)
    changed = replace(
        retained, bundle=object(),
        fact_snapshot=None if missing else replace(ctx.fact_snapshot, program_id="other"),
    )
    with pytest.raises(ConfigError, match="Missing fact snapshot|program does not match"):
        risk_stage.RiskStage().execute(changed)
    assert loader.call_count == 1


@pytest.mark.parametrize("has_risk", [False, True])
def test_real_program_reality_loader_reduces_two_calls_to_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_risk: bool,
) -> None:
    _, programs_root = _write_demo_v2_layout(tmp_path)
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "db"))
    monkeypatch.setenv("VERTEX_FACT_SOR", "primary")
    when = datetime(2026, 10, 25, tzinfo=timezone.utc)
    save_fact_sor_state(
        "demo", mode="primary", recorded_at=when, recorded_by="synthetic-test",
        programs_root=programs_root,
    )
    store = ProgramFactStore("demo", db_root=tmp_path / "db")
    if has_risk:
        store.append_fact(ProgramFactInput(
            fact_type="risk.entry", entity_refs=("RISK:captured-risk",),
            payload={
                "id": "captured-risk", "program_id": "demo", "title": "Captured risk",
                "description": "Synthetic retained risk", "probability": "likely",
                "impact": "high", "category": "schedule", "owner_alias": "demo",
                "status": "open", "identified_date": "2026-10-24",
            },
            source_document_key="synthetic:captured-risk",
            approval_event_id="synthetic-risk-approval",
        ), recorded_at=when - timedelta(hours=1))
    pin = store.pin_snapshot(created_at=when)
    ctx = StageContext(
        edition_name="demo_weekly", programs_root=programs_root,
        archive_root=tmp_path / "archive", data_as_of=when, started_at=when,
        resolved_v2=SimpleNamespace(paths=SimpleNamespace(program_id="demo")),
        fact_snapshot=store.load_pinned_snapshot(pin.snapshot_id), fact_snapshot_pin=pin,
    )
    loader = Mock(wraps=ProgramReality.load)
    monkeypatch.setattr(ProgramReality, "load", loader)
    milestones = milestone_stage.MilestoneStage().execute(ctx)
    baseline = risk_stage.RiskStage().execute(replace(milestones, report_reality=None, report_reality_scope=None))
    assert loader.call_count == 2
    loader.reset_mock()
    reused = risk_stage.RiskStage().execute(milestone_stage.MilestoneStage().execute(ctx))
    assert loader.call_count == 1
    for field in ("milestones", "milestone_assessments", "milestone_lineage", "milestone_warnings",
                  "risks", "risk_assessments", "risk_lineage", "risk_warnings"):
        assert getattr(reused, field) == getattr(baseline, field)
    assert [risk.title for risk in reused.risks] == (["Captured risk"] if has_risk else [])
    if has_risk:
        assert reused.risk_lineage["captured-risk"]["source_document_key"] == "synthetic:captured-risk"
    actions = action_stage.ActionStage().execute(reused)
    assert actions.actions == ()
    assert loader.call_count == 1
