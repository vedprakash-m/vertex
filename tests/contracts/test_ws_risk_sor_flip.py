from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src.commands import report_deck as report_deck_module
from src.commands import report_health as report_health_module
from src.core.fact_sor_state import save_fact_sor_state
from src.core.html_renderer import HTMLRenderer, RenderContext
from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope
from src.core.ledger.fact_bridge import append_bridged_risk_event
from src.core.ledger.source_refs import OperatorAssertionRef
from src.core.models import Confidence, DeltaSet, EditionType, FreshnessReport, ProgramContext, ReportData, ReviewSection, ReviewState, ReviewStatus, RiskLevel
from src.core.models_v2 import Dependency, DependencyStatus, DependencyType, Milestone, MilestoneAssessment, MilestoneStatus, RiskCategory, RiskEntry, RiskImpact, RiskProbability, RiskStatus
from src.core.pipeline import StageContext
from src.core.program_fact_store import FactLineage, ProgramFactSnapshot
from src.core.program_reality import FactAssessment, ProgramReality
from src.core.exceptions import ConfigError
from src.core.stages import milestone_stage as milestone_stage_module
from src.core.stages import render_stage as render_stage_module
from src.core.stages import risk_stage as risk_stage_module
from src.core.stages.milestone_stage import MilestoneStage
from src.core.stages.risk_stage import RiskStage
from src.core.truth_levels import TruthLevel
from src.core.view_models import EditionMeta, HealthSummary


def _snapshot(program_id: str, when: datetime) -> ProgramFactSnapshot:
    return ProgramFactSnapshot(program_id=program_id, as_of=when, facts=())


def _seed_program(programs_root: Path, program_id: str) -> None:
    program_dir = programs_root / program_id
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "id": program_id,
                "name": program_id.upper(),
                "storage_backend": "sqlite",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _risk_entry(risk_id: str, *, title: str = "Synthetic risk") -> RiskEntry:
    return RiskEntry(
        id=risk_id,
        program_id="nova",
        title=title,
        description="Synthetic bridged risk fixture.",
        probability=RiskProbability.POSSIBLE,
        impact=RiskImpact.HIGH,
        category=RiskCategory.TECHNICAL,
        owner_alias="owner@example.com",
        mitigation_plan=None,
        mitigation_due_date=None,
        linked_workstream_ids=(),
        linked_work_item_ids=(),
        linked_milestone_ids=(),
        linked_claim_ids=(),
        linked_action_ids=(),
        status=RiskStatus.OPEN,
        identified_date=date(2026, 7, 8),
        identified_in_vertex_issue=None,
        last_reviewed_date=date(2026, 7, 8),
        entity_refs=(f"RISK:{risk_id}",),
    )


def _milestone() -> Milestone:
    return Milestone(
        id="ms-1",
        program_id="nova",
        name="Milestone one",
        target_date=date(2026, 8, 1),
        owner_alias="owner@example.com",
        status=MilestoneStatus.ON_TRACK,
        exit_criteria=(),
        linked_workstream_ids=(),
        linked_work_item_ids=(),
    )


def _dependency() -> Dependency:
    return Dependency(
        id="dep-1",
        from_program_id="nova",
        from_workstream_id=None,
        from_item_id=None,
        from_milestone_id=None,
        to_program_id="nova",
        to_workstream_id=None,
        to_item_id=None,
        to_milestone_id=None,
        dependency_type=DependencyType.BLOCKS,
        risk_if_broken="Synthetic dependency",
        mitigation=None,
        status=DependencyStatus.ACTIVE,
        owner_alias="owner@example.com",
    )


def _assessment(
    record,
    *,
    truth_level: TruthLevel,
    disputed: bool = False,
    stale: bool = False,
    source_document_key: str | None = "email:sha256:fixture",
    approval_event_id: str | None = "evt-approval",
) -> FactAssessment:
    return FactAssessment(
        record=record,
        fact_id=f"fact:{getattr(record, 'id', 'fixture')}",
        truth_level=truth_level,
        disputed=disputed,
        stale=stale,
        provisional_inputs=False,
        evidence=("synthetic fixture",),
        lineage=FactLineage(
            source_document_key=source_document_key,
            approval_event_id=approval_event_id,
            domain_event_id="evt-source",
        ),
    )


def _reality(
    *,
    when: datetime,
    risks: tuple[FactAssessment, ...] = (),
    milestones: tuple[FactAssessment, ...] = (),
    dependencies: tuple[FactAssessment, ...] = (),
) -> ProgramReality:
    return ProgramReality(
        program_id="nova",
        snapshot=_snapshot("nova", when),
        sor_mode="shadow",
        as_of=when,
        _entity_fact_index={},
        _actions=(),
        _risks=risks,
        _decisions=(),
        _dependencies=dependencies,
        _milestones=milestones,
        _assumptions=(),
        _workstreams=(),
        _claims=(),
        _family_sor_modes={"judgment": "shadow", "workitem.state": "shadow"},
    )


def _render_context(*, health: HealthSummary, milestone_rows=()) -> RenderContext:
    when = datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc)
    report = ReportData(
        issue_number=78,
        edition=EditionType.DETAILED,
        generated_at=when,
        ado_data_as_of=when,
        program=ProgramContext(
            program_name="NOVA",
            mission="Synthetic fixture",
            pillars=(),
            workstreams=(),
            glossary={},
            people=(),
        ),
        items=(),
        deltas=DeltaSet(
            issue_number=78,
            previous_issue_number=None,
            new_items=(),
            closed_items=(),
            risk_changes=(),
            eta_changes=(),
            unchanged_count=0,
        ),
        scorecard=(),
        scorecard_deltas=(),
        exec_summary_text="Synthetic render context.",
        workstream_blurbs={},
        freshness=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        hygiene_warnings=(),
        review_status=ReviewStatus(
            issue_number=78,
            sections=(
                ReviewSection(
                    section_id="exec_summary",
                    state=ReviewState.APPROVED,
                    reviewer="reviewer@example.com",
                    note=None,
                    updated_at=when,
                ),
            ),
        ),
        manifest_id="12345678-1234-5678-1234-567812345678",
    )
    return RenderContext(
        title="Synthetic newsletter",
        subtitle="",
        preheader="",
        report=report,
        edition_meta=EditionMeta(
            edition="acme_weekly",
            issue_number=78,
            generated_at=when,
            ado_data_as_of=when,
            manifest_id=report.manifest_id,
            qg_status="PASS",
        ),
        health=health,
        milestone_rows=tuple(milestone_rows),
    )


def test_risk_stage_legacy_mode_preserves_legacy_behavior(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_program(programs_root, "nova")
    save_fact_sor_state(
        "nova",
        mode="legacy",
        family_modes={"judgment": "legacy"},
        recorded_at=datetime(2026, 7, 8, tzinfo=timezone.utc),
        recorded_by="test",
        programs_root=programs_root,
    )
    legacy_risk = _risk_entry("risk-legacy", title="Legacy risk")
    monkeypatch.setattr(risk_stage_module, "_load_current_risks", lambda *_a, **_kw: (legacy_risk,))

    ctx = StageContext(
        edition_name="nova_weekly",
        resolved_v2=SimpleNamespace(paths=SimpleNamespace(program_id="nova")),
        programs_root=programs_root,
        data_as_of=datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc),
        stage_support=SimpleNamespace(
            load_program_reality=lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("reality path must not run"))
        ),
    )

    result = RiskStage().execute(ctx)

    assert result.risks == (legacy_risk,)
    assert result.risk_assessments is None
    assert result.risk_lineage is None
    assert result.risk_warnings == ()


def test_risk_stage_non_legacy_surfaces_fact_assessment_metadata_into_render() -> None:
    when = datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc)
    risk = _risk_entry("risk-reality", title="Reality risk")
    assessment = _assessment(risk, truth_level=TruthLevel.CORROBORATED, disputed=True)
    health = report_health_module._build_health_summary(
        (),
        None,
        risks=(risk,),
        risk_assessments=(assessment,),
        as_of=when,
        edition_type=EditionType.DETAILED,
    )

    rendered = HTMLRenderer("acme_weekly").render(_render_context(health=health))

    assert "◆ CORROBORATED" in rendered
    assert "[DISPUTED ⚠]" in rendered


def test_risk_stage_empty_set_cross_check_warns_when_legacy_has_data(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_program(programs_root, "nova")
    save_fact_sor_state(
        "nova",
        mode="legacy",
        family_modes={"judgment": "shadow"},
        recorded_at=datetime(2026, 7, 8, tzinfo=timezone.utc),
        recorded_by="test",
        programs_root=programs_root,
    )
    monkeypatch.setattr(risk_stage_module, "_load_current_risks", lambda *_a, **_kw: (_risk_entry("risk-legacy"),))

    ctx = StageContext(
        edition_name="nova_weekly",
        resolved_v2=SimpleNamespace(paths=SimpleNamespace(program_id="nova")),
        programs_root=programs_root,
        data_as_of=datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc),
        stage_support=SimpleNamespace(load_program_reality=lambda *_a, **_kw: _reality(when=datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc))),
    )

    result = RiskStage().execute(ctx)

    assert result.risks == ()
    assert result.risk_assessments == ()
    assert len(result.risk_warnings) == 1
    assert "ProgramReality returned 0 risks" in result.risk_warnings[0]


def test_risk_stage_requires_audited_rollback_flag_for_reality_failure(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_program(programs_root, "nova")
    save_fact_sor_state(
        "nova",
        mode="legacy",
        family_modes={"judgment": "primary"},
        recorded_at=datetime(2026, 7, 8, tzinfo=timezone.utc),
        recorded_by="test",
        programs_root=programs_root,
    )

    ctx = StageContext(
        edition_name="nova_weekly",
        resolved_v2=SimpleNamespace(paths=SimpleNamespace(program_id="nova")),
        programs_root=programs_root,
        data_as_of=datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc),
        stage_support=SimpleNamespace(load_program_reality=lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("facade unavailable"))),
    )

    with pytest.raises(ConfigError, match="audited legacy rollback"):
        RiskStage().execute(ctx)


def test_risk_stage_audited_rollback_warns_and_uses_legacy(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_program(programs_root, "nova")
    save_fact_sor_state(
        "nova",
        mode="legacy",
        family_modes={"judgment": "primary"},
        recorded_at=datetime(2026, 7, 8, tzinfo=timezone.utc),
        recorded_by="test",
        programs_root=programs_root,
    )
    monkeypatch.setenv("VERTEX_REPORT_ALLOW_LEGACY_RISK_ROLLBACK", "1")
    legacy_risk = _risk_entry("risk-rollback", title="Rollback risk")
    monkeypatch.setattr(risk_stage_module, "_load_current_risks", lambda *_a, **_kw: (legacy_risk,))

    ctx = StageContext(
        edition_name="nova_weekly",
        resolved_v2=SimpleNamespace(paths=SimpleNamespace(program_id="nova")),
        programs_root=programs_root,
        data_as_of=datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc),
        stage_support=SimpleNamespace(load_program_reality=lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("facade unavailable"))),
    )

    result = RiskStage().execute(ctx)

    assert result.risks == (legacy_risk,)
    assert result.risk_assessments == ()
    assert len(result.risk_warnings) == 1
    assert "degraded to legacy risk source via audited rollback flag" in result.risk_warnings[0]


def test_risk_render_fallback_injects_visible_warning_banner(monkeypatch, tmp_path: Path) -> None:
    when = datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc)
    legacy_risk = _risk_entry("risk-legacy", title="Legacy fallback risk")
    monkeypatch.setattr(render_stage_module, "_load_current_risks", lambda *_a, **_kw: (legacy_risk,))

    def _build_health_summary(*args, **kwargs):
        if kwargs.get("risk_assessments"):
            raise AttributeError("record missing status")
        return report_health_module._build_health_summary(*args, **kwargs)

    ctx = StageContext(
        edition_name="nova_weekly",
        resolved_issue_number=78,
        resolved_v2=SimpleNamespace(program=SimpleNamespace(id="nova")),
        programs_root=tmp_path / "programs",
        data_as_of=when,
        risk_assessments=(
            _assessment(_risk_entry("risk-broken", title="Broken risk"), truth_level=TruthLevel.SOURCE_VALIDATED),
        ),
    )

    health = render_stage_module._build_health_summary_with_risk_fallback(
        ctx,
        SimpleNamespace(build_health_summary=_build_health_summary),
        (),
        None,
        risks=(_risk_entry("risk-broken", title="Broken risk"),),
        risk_assessments=ctx.risk_assessments,
        stale_risk_ids=(),
        program_id="nova",
        programs_root=ctx.programs_root,
        as_of=when,
        edition_type=EditionType.DETAILED,
    )
    rendered = HTMLRenderer("acme_weekly").render(_render_context(health=health))

    assert "stale, unmaintained data source because live risk rendering failed" in rendered
    assert "Legacy fallback risk" in rendered


def test_dependency_reads_follow_workitem_state_reality_gate(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_program(programs_root, "nova")
    save_fact_sor_state(
        "nova",
        mode="legacy",
        family_modes={"workitem.state": "shadow"},
        recorded_at=datetime(2026, 7, 8, tzinfo=timezone.utc),
        recorded_by="test",
        programs_root=programs_root,
    )
    milestone = _milestone()
    dependency = _dependency()
    reality = _reality(
        when=datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc),
        milestones=(_assessment(milestone, truth_level=TruthLevel.SOURCE_VALIDATED),),
        dependencies=(_assessment(dependency, truth_level=TruthLevel.SOURCE_VALIDATED),),
    )
    captured_dependencies: list[tuple[Dependency, ...]] = []
    monkeypatch.setattr(
        milestone_stage_module,
        "_load_current_dependencies",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("legacy dependencies must not run in shadow mode")),
    )
    monkeypatch.setattr(
        milestone_stage_module,
        "assess_milestone_health",
        lambda *_a, **_kw: MilestoneAssessment(
            milestone_id=milestone.id,
            computed_health=MilestoneStatus.ON_TRACK,
            blocked_criteria=(),
            slip_probability=0.0,
            critical_path=False,
            confidence=Confidence.HIGH,
            reasoning="test",
        ),
    )
    monkeypatch.setattr(
        milestone_stage_module,
        "build_critical_path",
        lambda _milestones, dependencies: captured_dependencies.append(tuple(dependencies)) or (),
    )

    ctx = StageContext(
        edition_name="nova_weekly",
        resolved_v2=SimpleNamespace(paths=SimpleNamespace(program_id="nova")),
        programs_root=programs_root,
        data_as_of=datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc),
        stage_support=SimpleNamespace(load_program_reality=lambda *_a, **_kw: reality),
    )

    result = MilestoneStage().execute(ctx)

    assert result.milestones == (milestone,)
    assert captured_dependencies == [(dependency,)]


def test_risk_stage_synthetic_bridged_fact_renders_trust_badge(tmp_path: Path) -> None:
    # Synthetic fixture per PS-11: xpf does not yet have enough real bridged risk
    # volume to prove the shadow/primary read path end-to-end.
    programs_root = tmp_path / "programs"
    _seed_program(programs_root, "nova")
    save_fact_sor_state(
        "nova",
        mode="legacy",
        family_modes={"judgment": "shadow"},
        recorded_at=datetime(2026, 7, 8, tzinfo=timezone.utc),
        recorded_by="test",
        programs_root=programs_root,
    )
    when = datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc)
    event = build_event_envelope(
        program_id="nova",
        event_type="risk.raised.v1",
        occurred_at=when,
        recorded_at=when,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="synthetic-fixture",
        payload={
            "risk_id": "risk:synthetic-bridge-1",
            "title": "Synthetic bridged risk",
            "severity": "high",
            "description": "Synthetic bridged fixture for newsletter badge validation.",
        },
        source_ref=OperatorAssertionRef(asserted_by="synthetic-fixture", asserted_at=when),
    )
    append_bridged_risk_event(event, db_root=programs_root.parent)

    reality = ProgramReality.load("nova", programs_root=programs_root, as_of=when, edition_name="nova_weekly")
    risk_assessments = tuple(reality.risks())
    assert len(risk_assessments) == 1

    health = report_health_module._build_health_summary(
        (),
        None,
        risks=tuple(assessment.record for assessment in risk_assessments),
        risk_assessments=risk_assessments,
        as_of=when,
        edition_type=EditionType.DETAILED,
    )
    rendered = HTMLRenderer("acme_weekly").render(_render_context(health=health))

    assert "Synthetic bridged risk" in rendered
    assert "✔ CONFIRMED" in rendered


def test_milestone_rows_render_badges_from_reality_metadata(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _seed_program(programs_root, "nova")
    milestone = _milestone()
    rows = report_deck_module._build_report_milestone_rows(
        (milestone,),
        (
            MilestoneAssessment(
                milestone_id=milestone.id,
                computed_health=MilestoneStatus.ON_TRACK,
                blocked_criteria=(),
                slip_probability=0.0,
                critical_path=False,
                confidence=Confidence.HIGH,
                reasoning="test",
            ),
        ),
        items=(),
        program_id="nova",
        programs_root=programs_root,
        as_of=datetime(2026, 7, 8, 18, 0, tzinfo=timezone.utc),
        milestone_lineage={
            milestone.id: {
                "source_document_key": "email:sha256:milestone-source",
                "approval_event_id": "evt-milestone",
                "truth_level": TruthLevel.RAW_OBSERVED.value,
                "disputed": "true",
                "stale": "false",
            }
        },
    )

    rendered = HTMLRenderer("acme_weekly").render(
        _render_context(
            health=HealthSummary(
                overall_risk=RiskLevel.UNKNOWN,
                high_count=0,
                medium_count=0,
                low_count=0,
                done_count=0,
                total_count=0,
                delta_direction="unchanged",
                prior_counts=None,
            ),
            milestone_rows=rows,
        )
    )

    assert "○ UNCONFIRMED" in rendered
    assert "[DISPUTED ⚠]" in rendered
    assert "⚠ includes unconfirmed sources" in rendered
