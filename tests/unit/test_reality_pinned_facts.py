from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from src.core import program_reality
from src.core.exceptions import ConfigError
from src.core.fact_sor_state import save_fact_sor_state
from src.core.ledger.candidate_store import CandidateDecisionRecord
from src.core.models import EditionType
from src.core.pipeline import StageContext
from src.core.program_fact_store import ProgramFactInput, ProgramFactSnapshot, ProgramFactStore
from src.core.program_reality import ProgramReality
from src.core.stages import milestone_stage, risk_stage
from src.core.stages.milestone_stage import MilestoneStage
from src.core.stages.resolution_stage import ResolutionStage
from src.core.stages.risk_stage import RiskStage
from src.core.truth_levels import TruthLevel
from tests.unit.test_pipeline_stages import _stage_pipeline_workspace


NOW = datetime(2026, 5, 5, 18, tzinfo=timezone.utc)


def _risk(title: str = "Captured risk") -> ProgramFactInput:
    return ProgramFactInput(
        fact_type="risk.entry", entity_refs=("RISK:r1",),
        payload={
            "id": "r1", "program_id": "acme", "title": title, "description": "Synthetic risk",
            "probability": "likely", "impact": "high", "category": "schedule",
            "owner_alias": "demo", "status": "open", "identified_date": "2026-05-01",
        },
        write_authority="system", domain_event_id="domain-r1", source_document_key="synthetic:r1",
        source_signal_ids=("pending-signal",),
    )


def _milestone(name: str = "Captured milestone") -> ProgramFactInput:
    return ProgramFactInput(
        fact_type="milestone.entry", entity_refs=("MILESTONE:m1",),
        payload={
            "id": "m1", "program_id": "acme", "name": name, "target_date": "2026-05-18",
            "owner_alias": "demo", "status": "on_track", "exit_criteria": ["Synthetic exit"],
        },
        source_document_key="synthetic:m1", approval_event_id="approved-m1",
    )


def _dependency(status: str = "active") -> ProgramFactInput:
    return ProgramFactInput(
        fact_type="dependency.link", entity_refs=("DEPENDENCY:d1",),
        payload={
            "id": "d1", "from_program_id": "acme", "to_program_id": "acme",
            "from_milestone_id": "m1", "to_milestone_id": "m1",
            "dependency_type": "blocks", "risk_if_broken": "Synthetic delay", "status": status,
        },
    )


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("VERTEX_DB_PATH", raising=False)
    monkeypatch.delenv("VERTEX_FACT_SOR", raising=False)
    reports_root = _stage_pipeline_workspace(tmp_path)
    programs_root = tmp_path / "programs"
    save_fact_sor_state("acme", mode="primary", recorded_at=NOW, programs_root=programs_root)
    return programs_root, reports_root, ProgramFactStore("acme", db_root=tmp_path)


def _resolved(workspace) -> StageContext:
    programs_root, reports_root, _store = workspace
    return ResolutionStage().execute(StageContext(
        edition_name="acme_weekly", programs_root=programs_root, reports_root=reports_root,
        archive_root=programs_root.parent / "archive", data_as_of=NOW,
    ))


def _supporting_facts(store: ProgramFactStore) -> None:
    risk = store.append_fact(_risk(), recorded_at=NOW - timedelta(days=60)).revision
    for fact in (
        ProgramFactInput("fact.corroboration", ("corroboration",), {
            "entity_id": "RISK:r1", "family": "judgment",
        }),
        ProgramFactInput("fact.conflict", ("conflict",), {
            "target_natural_key": risk.natural_key, "resolved": False,
        }),
        ProgramFactInput("signal.observation", ("signal",), {"source": "synthetic"}),
    ):
        store.append_fact(fact, recorded_at=NOW - timedelta(days=1))


@pytest.mark.parametrize("injected", [False, True])
def test_resolution_risk_and_milestone_use_retained_members_without_fact_reload(workspace, monkeypatch, injected):
    _programs_root, _reports_root, store = workspace
    _supporting_facts(store)
    store.append_fact(_milestone(), recorded_at=NOW)
    store.append_fact(_dependency(), recorded_at=NOW)
    ctx = _resolved(workspace)
    assert ctx.fact_snapshot is not None
    original_risk = next(f for f in ctx.fact_snapshot.facts if f.fact_type == "risk.entry")
    store.append_fact(_risk("Backdated replacement"), recorded_at=NOW - timedelta(days=1))
    store.append_fact(_milestone("Replacement milestone"), recorded_at=NOW + timedelta(seconds=1))
    store.append_fact(_dependency("resolved"), recorded_at=NOW + timedelta(seconds=1))
    if injected:
        from src.commands.report import _load_program_reality

        ctx = replace(ctx, stage_support=SimpleNamespace(load_program_reality=_load_program_reality))
    decisions = (CandidateDecisionRecord(
        candidate_id="candidate-r1", kind="approved", decided_at=NOW, triage_actor="demo",
        resulting_event_id="domain-r1", approval_event_id="approved-r1",
    ),)
    monkeypatch.setattr(program_reality, "load_triage_decisions", Mock(return_value=decisions))
    critical_path = Mock(return_value=())
    monkeypatch.setattr(milestone_stage, "build_critical_path", critical_path)
    with (
        patch.object(program_reality, "load_program_facts", side_effect=AssertionError("current facts")),
        patch.object(ProgramFactStore, "snapshot", side_effect=AssertionError("current snapshot")),
    ):
        risk_result = RiskStage().execute(ctx)
        milestone_result = MilestoneStage().execute(ctx)
    assert [risk.title for risk in risk_result.risks] == ["Captured risk"]
    assessment, = risk_result.risk_assessments
    assert assessment.fact_id == original_risk.fact_id
    assert assessment.truth_level == TruthLevel.CORROBORATED
    assert assessment.disputed is True
    assert assessment.stale is True
    assert assessment.evidence == ("pending-signal", "RISK:r1")
    assert risk_result.risk_lineage == {
        "r1": {"source_document_key": "synthetic:r1", "approval_event_id": "approved-r1"},
    }
    assert [milestone.name for milestone in milestone_result.milestones] == ["Captured milestone"]
    assert milestone_result.milestone_lineage["m1"]["approval_event_id"] == "approved-m1"
    assert critical_path.call_args.args[1][0].status.value == "active"
    assert not risk_result.risk_warnings
    assert not milestone_result.milestone_warnings


@pytest.mark.parametrize("stage", [RiskStage, MilestoneStage])
def test_resolved_primary_missing_capture_fails_before_rollback(workspace, monkeypatch, stage):
    ctx = replace(_resolved(workspace), fact_snapshot=None, fact_snapshot_pin=None)
    monkeypatch.setenv("VERTEX_REPORT_ALLOW_LEGACY_RISK_ROLLBACK", "1")
    monkeypatch.setenv("VERTEX_REPORT_ALLOW_LEGACY_MILESTONE_ROLLBACK", "1")
    with patch.object(program_reality, "load_program_facts", side_effect=AssertionError("current facts")):
        with pytest.raises(ConfigError, match="snapshot"):
            stage().execute(ctx)


@pytest.mark.parametrize("stage", [RiskStage, MilestoneStage])
def test_wrong_program_capture_rejected_before_rollback(workspace, stage):
    ctx = replace(_resolved(workspace), fact_snapshot=ProgramFactSnapshot("other", NOW, ()))
    with pytest.raises(ConfigError, match="program"):
        stage().execute(ctx)


@pytest.mark.parametrize("stage", [RiskStage, MilestoneStage])
def test_empty_capture_does_not_reload_replacement_facts(workspace, stage):
    _programs_root, _reports_root, store = workspace
    ctx = _resolved(workspace)
    store.append_fact(_risk(), recorded_at=NOW)
    store.append_fact(_milestone(), recorded_at=NOW)
    with (
        patch.object(program_reality, "load_program_facts", side_effect=AssertionError("current facts")),
        patch.object(risk_stage, "_load_current_risks", return_value=()) as cross_check,
    ):
        result = stage().execute(ctx)
    assert (result.risks if stage is RiskStage else result.milestones) == ()
    assert cross_check.call_count == (1 if stage is RiskStage else 0)


def test_reality_optional_capture_preserves_assessments_and_supporting_facts(workspace):
    programs_root, _reports_root, store = workspace
    _supporting_facts(store)
    ctx = _resolved(workspace)
    assert ctx.fact_snapshot is not None
    signal = next(f for f in ctx.fact_snapshot.facts if f.fact_type == "signal.observation")
    risk = next(f for f in ctx.fact_snapshot.facts if f.fact_type == "risk.entry")
    snapshot = replace(ctx.fact_snapshot, facts=tuple(
        replace(f, review_state="candidate") if f is signal
        else replace(f, source_signal_ids=(signal.fact_id,)) if f is risk else f
        for f in ctx.fact_snapshot.facts
    ))
    with patch.object(program_reality, "load_program_facts", side_effect=AssertionError("current facts")):
        reality = ProgramReality.load(
            "acme", programs_root=programs_root, as_of=NOW, fact_snapshot=snapshot,
        )
    assessment, = reality.risks()
    assert assessment.truth_level == TruthLevel.CORROBORATED
    assert assessment.disputed is True
    assert assessment.provisional_inputs is True
    assert assessment.stale is True
    assert assessment.lineage.source_document_key == "synthetic:r1"
    assert reality._snapshot is snapshot


def test_reality_rejects_wrong_program_capture(workspace):
    programs_root, _reports_root, _store = workspace
    with pytest.raises(ConfigError, match="program"):
        ProgramReality.load(
            "acme", programs_root=programs_root, fact_snapshot=ProgramFactSnapshot("other", NOW, ()),
        )


@pytest.mark.parametrize("retained", [False, True])
@pytest.mark.parametrize("family", ["risk", "milestone"])
def test_family_and_entity_index_share_enriched_approval(workspace, monkeypatch, retained, family):
    programs_root, _, store = workspace
    fact = _risk() if family == "risk" else replace(
        _milestone(), domain_event_id="domain-m1", approval_event_id=None,
    )
    # Exact refs make the existing entity-index join observable independently of bridge aliases.
    fact = replace(fact, entity_refs=(fact.payload["id"],))
    original = store.append_fact(fact, recorded_at=NOW).revision
    snapshot = store.snapshot(as_of=NOW)
    decision = CandidateDecisionRecord(
        candidate_id="candidate", kind="approved", decided_at=NOW, triage_actor="demo",
        resulting_event_id=fact.domain_event_id, approval_event_id="enriched-approval",
    )
    monkeypatch.setattr(program_reality, "load_triage_decisions", Mock(return_value=(decision,)))
    with patch.object(program_reality, "load_program_facts", return_value=snapshot) as loader:
        reality = ProgramReality.load(
            "acme", programs_root=programs_root, as_of=NOW,
            **({"fact_snapshot": snapshot} if retained else {}),
        )
    indexed, = reality._entity_fact_index[original.fact_id]
    assessed, = reality.risks() if family == "risk" else reality.milestones()
    assert indexed.lineage.approval_event_id == "enriched-approval"
    assert assessed.lineage.approval_event_id == "enriched-approval"
    assert assessed is indexed
    assert assessed.fact_id == original.fact_id
    assert assessed.truth_level == indexed.truth_level
    assert loader.call_count == (0 if retained else 1)


@pytest.mark.parametrize("mode,families", [
    ("legacy", {}), ("shadow", {}), ("primary", {"judgment": "shadow"}),
    ("primary", {"workitem.state": "legacy"}), ("shadow", {"judgment": "primary"}),
])
def test_reality_ineligible_modes_keep_existing_fact_loader(workspace, mode, families):
    programs_root, _reports_root, store = workspace
    save_fact_sor_state("acme", mode=mode, recorded_at=NOW, family_modes=families, programs_root=programs_root)
    current = store.snapshot()
    with patch.object(program_reality, "load_program_facts", return_value=current) as loader:
        reality = ProgramReality.load(
            "acme", programs_root=programs_root, fact_snapshot=ProgramFactSnapshot("acme", NOW, ()),
        )
    loader.assert_called_once()
    assert reality._snapshot is current


@pytest.mark.parametrize("stage", [RiskStage, MilestoneStage])
@pytest.mark.parametrize("mode,families", [
    ("legacy", {}), ("shadow", {}), ("primary", {"judgment": "legacy"}),
    ("primary", {"workitem.state": "shadow"}),
])
def test_stages_ineligible_modes_preserve_existing_authored_path(workspace, stage, mode, families):
    programs_root, _reports_root, _store = workspace
    ctx = replace(_resolved(workspace), fact_snapshot=None, fact_snapshot_pin=None)
    save_fact_sor_state("acme", mode=mode, recorded_at=NOW, family_modes=families, programs_root=programs_root)
    with patch.object(risk_stage, "_load_current_risks", wraps=risk_stage._load_current_risks) as legacy:
        result = stage().execute(ctx)
    if stage is MilestoneStage and (mode == "legacy" or families.get("workitem.state") == "legacy"):
        assert result.milestones
    elif stage is RiskStage and (mode == "legacy" or families.get("judgment") == "legacy"):
        legacy.assert_called_once()


def test_standalone_reality_without_capture_keeps_current_loader(workspace):
    programs_root, _reports_root, store = workspace
    with patch.object(program_reality, "load_program_facts", return_value=store.snapshot()) as loader:
        ProgramReality.load("acme", programs_root=programs_root)
    loader.assert_called_once()


@pytest.mark.parametrize("stage", [RiskStage, MilestoneStage])
def test_standalone_and_skipped_stages_need_no_capture(workspace, stage):
    ctx = replace(_resolved(workspace), fact_snapshot=None, fact_snapshot_pin=None, bundle=None)
    with patch.object(program_reality, "load_program_facts", return_value=ProgramFactSnapshot("acme", NOW, ())):
        result = stage().execute(ctx)
    assert (result.risks if stage is RiskStage else result.milestones) == ()
    skipped = stage().execute(replace(ctx, resolved_edition_type=EditionType.LOOKBACK))
    assert (skipped.risks if stage is RiskStage else skipped.milestones) == ()
    assert stage().execute(StageContext(edition_name="standalone")).data_as_of is None


@pytest.mark.parametrize("mode", ["legacy", "shadow"])
def test_action_mixed_family_keeps_authored_loader(workspace, mode):
    from src.core.stages import action_stage

    programs_root, _, _ = workspace
    ctx = _resolved(workspace)
    save_fact_sor_state(
        "acme", mode="primary", family_modes={"workitem.state": mode},
        recorded_at=NOW, programs_root=programs_root,
    )
    authored = (SimpleNamespace(id="authored", due_date=None, status="open"),)
    with patch.object(action_stage, "_load_current_actions", return_value=authored) as loader:
        result = action_stage.ActionStage().execute(ctx)
    loader.assert_called_once()
    assert result.actions == authored


@pytest.mark.parametrize("stage", [RiskStage, MilestoneStage])
def test_prepopulated_stages_do_not_require_capture(workspace, stage):
    ctx = replace(_resolved(workspace), fact_snapshot=None, fact_snapshot_pin=None)
    ctx = replace(ctx, risks=()) if stage is RiskStage else replace(ctx, milestone_assessments=())
    assert stage().execute(ctx) is ctx


@pytest.mark.parametrize("stage", [RiskStage, MilestoneStage])
def test_reality_config_error_retains_existing_skip_warning(workspace, monkeypatch, stage):
    ctx = _resolved(workspace)
    monkeypatch.setattr(ProgramReality, "load", Mock(side_effect=ConfigError("synthetic config")))
    result = stage().execute(ctx)
    warnings = result.risk_warnings if stage is RiskStage else result.milestone_warnings
    assert "skipped" in warnings[0].lower()
    assert "synthetic config" in warnings[0]


@pytest.mark.parametrize("stage", [RiskStage, MilestoneStage])
def test_capture_projection_failure_keeps_audited_rollback(workspace, monkeypatch, stage):
    ctx = _resolved(workspace)
    monkeypatch.setattr(ProgramReality, "load", Mock(side_effect=RuntimeError("synthetic failure")))
    flag = (
        "VERTEX_REPORT_ALLOW_LEGACY_RISK_ROLLBACK" if stage is RiskStage
        else "VERTEX_REPORT_ALLOW_LEGACY_MILESTONE_ROLLBACK"
    )
    monkeypatch.delenv(flag, raising=False)
    with pytest.raises(ConfigError, match="audited legacy rollback"):
        stage().execute(ctx)
    monkeypatch.setenv(flag, "1")
    result = stage().execute(ctx)
    warnings = result.risk_warnings if stage is RiskStage else result.milestone_warnings
    assert "degraded to legacy" in warnings[0]


@pytest.mark.parametrize("stage", [RiskStage, MilestoneStage])
def test_direct_standalone_loader_without_archive_or_capture(workspace, stage):
    from src.commands.report import _load_program_reality

    programs_root, _, _ = workspace
    ctx = replace(
        _resolved(workspace), fact_snapshot=None, fact_snapshot_pin=None, bundle=None,
        archive_root=None, edition_name="",
    )
    with patch.object(program_reality, "load_program_facts", return_value=ProgramFactSnapshot("acme", NOW, ())):
        result = stage().execute(ctx)
        reality = _load_program_reality("acme", programs_root=programs_root, as_of=NOW)
    assert reality.risks() == ()
    assert (result.risks if stage is RiskStage else result.milestones) == ()


def test_primary_family_overrides_accept_complete_capture(workspace):
    programs_root, _, _ = workspace
    save_fact_sor_state(
        "acme", mode="primary", family_modes={"judgment": "primary", "workitem.state": "primary"},
        recorded_at=NOW, programs_root=programs_root,
    )
    ctx = _resolved(workspace)
    with (
        patch.object(program_reality, "load_program_facts", side_effect=AssertionError("current facts")),
        patch.object(risk_stage, "_load_current_risks", return_value=()),
    ):
        assert RiskStage().execute(ctx).risks == ()
        assert MilestoneStage().execute(ctx).milestones == ()
