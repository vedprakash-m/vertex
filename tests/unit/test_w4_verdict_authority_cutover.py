from __future__ import annotations

from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
import yaml

from src.commands import brief as brief_cmd
from src.commands import doctor as doctor_cmd
from src.commands import escalate as escalate_cmd
from src.commands import report as report_cmd
from src.commands import review_full as review_full_cmd
from src.commands.report_health import _build_health_summary
from src.core.archive_store import write_confirmed_issue
from src.core.cockpit_builder import build_cockpit_snapshot
from src.core.exceptions import ConfigError
from src.core.forecast_engine import ETAForecast
from src.core.manifest_writer import hash_jsonable
from src.core.models import (
    Confidence,
    ConfirmedDimension,
    DimensionRisk,
    EditionType,
    RiskLevel,
    RunEvidenceSnapshot,
    RunManifest,
    Snapshot,
    SnapshotItem,
)
from src.core.models_v2 import (
    Milestone,
    MilestoneAssessment,
    MilestoneStatus,
    RiskCategory,
    RiskEntry,
    RiskImpact,
    RiskProbability,
    RiskStatus,
)
from src.core.program_verdict import (
    assert_cross_surface_verdict_parity,
    evaluate_program_verdict,
    load_latest_program_verdict,
    observe_program_verdict,
    resolve_authoritative_program_risk,
    verify_program_verdict_oracle,
)
from src.core.reviewer_renderer import ReviewerRenderer
from tests.support.report_test_setup import (
    disable_kusto_in_report_copy,
    reset_overrides_to_seed_state,
    stage_v2_report_workspace,
)
from tests.support.synthetic_report_source import EDITION_NAME, build_synthetic_report_source
from tests.unit.test_commands_report import _sample_items


NOW = datetime(2026, 11, 10, 18, 0, tzinfo=timezone.utc)


def _snapshot(*, item_risk: RiskLevel = RiskLevel.LOW, dim_risk: RiskLevel = RiskLevel.LOW) -> Snapshot:
    return Snapshot(
        issue_number=1,
        generated_at=NOW,
        ado_data_as_of=NOW,
        edition_type=EditionType.DETAILED,
        items=(SnapshotItem(1, "Task", "Synthetic", "Active", None, "Synthetic", date(2026, 11, 20), item_risk, []),),
        scorecards=(ConfirmedDimension("Synthetic", "Lane-0", dim_risk, None, 1, ""),),
    )


def _capture(snapshot: Snapshot) -> RunEvidenceSnapshot:
    cap = RunEvidenceSnapshot(
        program_id="acme",
        fact_snapshot_id="cap-1",
        fact_content_sha256="a" * 64,
        as_of=NOW,
        valid_at=NOW,
        gather_run_id=None,
        gather_run_hash=None,
        source_scopes=(),
        binding_declarations=(),
        fetched_item_ids=tuple(sorted(item.id for item in snapshot.items)),
        report_snapshot_hash=hash_jsonable(snapshot),
        config_hash="sha256:" + "b" * 64,
        sha256="",
        effective_family_modes=(("judgment", "primary"),),
    )
    payload = asdict(cap)
    payload.pop("sha256")
    return replace(cap, sha256=hash_jsonable(payload))


def _high_risk() -> RiskEntry:
    return RiskEntry(
        id="risk-1",
        program_id="acme",
        title="Synthetic critical dependency blocker",
        description="Synthetic blocker for W4.1 contradiction testing",
        probability=RiskProbability.VERY_LIKELY,
        impact=RiskImpact.HIGH,
        category=RiskCategory.SCHEDULE,
        owner_alias="",
        mitigation_plan=None,
        mitigation_due_date=None,
        linked_workstream_ids=(),
        linked_work_item_ids=(),
        linked_milestone_ids=(),
        linked_claim_ids=(),
        linked_action_ids=(),
        status=RiskStatus.OPEN,
        identified_date=date(2026, 11, 13),
        identified_in_vertex_issue=1,
        last_reviewed_date=None,
        entity_refs=(),
        kind="strategic",
    )


def _write_published_verdict(
    programs_root: Path,
    *,
    program_id: str = "foreign",
    health: RiskLevel = RiskLevel.HIGH,
    issue: int = 7,
) -> None:
    publication = programs_root / program_id / "publications" / "weekly" / f"issue_{issue:03d}"
    publication.mkdir(parents=True)
    (publication / f"issue_{issue:03d}.manifest.json").write_text(
        json.dumps({
            "started_at": NOW.isoformat(),
            "metadata": {
                "program_verdict_observation": {
                    "mode": "observe",
                    "verdict": {
                        "schema_version": "program-verdict.v1",
                        "program_id": program_id,
                        "snapshot_id": f"snapshot-{issue}",
                        "as_of": NOW.isoformat(),
                        "health": health.value,
                        "schedule_risk": health.value,
                        "confidence": "none",
                        "completion_phase": "unknown",
                        "reasons": [{"code": "risk_register_high_floor"}],
                        "evidence_gaps": [{"code": "risk_register_coverage_unqualified"}],
                        "leadership_asks": [],
                        "source_refs": [f"PROGRAM:{program_id}"],
                    },
                },
            },
        }),
        encoding="utf-8",
    )


def _missed_milestone() -> tuple[Milestone, MilestoneAssessment]:
    ms = Milestone(
        id="ms-1",
        program_id="acme",
        name="Synthetic Gate",
        target_date=date(2026, 11, 20),
        owner_alias="op",
        status=MilestoneStatus.MISSED,
        exit_criteria=("All gates passed",),
        linked_workstream_ids=("deployment_readiness",),
        linked_work_item_ids=(),
    )
    ma = MilestoneAssessment(
        milestone_id="ms-1",
        computed_health=MilestoneStatus.MISSED,
        blocked_criteria=(),
        slip_probability=0.9,
        critical_path=True,
        confidence=Confidence.HIGH,
        reasoning="Synthetic miss",
        coverage_gap=False,
    )
    return ms, ma


@pytest.mark.parametrize("adverse_mode", ["strategic_risk", "missed_milestone", "high_forecast", "blocked_item"])
def test_w41_contradiction_fixture_resolves_conservatively_and_passes_oracle(adverse_mode: str) -> None:
    snap = _snapshot(
        item_risk=(RiskLevel.BLOCKED if adverse_mode == "blocked_item" else RiskLevel.LOW),
        dim_risk=RiskLevel.LOW,
    )
    cap = _capture(snap)
    forecasts: tuple[ETAForecast, ...] = ()
    risks: tuple[RiskEntry, ...] = ()
    milestones: tuple[Milestone, ...] = ()
    assessments: tuple[MilestoneAssessment, ...] = ()

    if adverse_mode == "strategic_risk":
        risks = (_high_risk(),)
    elif adverse_mode == "missed_milestone":
        ms, ma = _missed_milestone()
        milestones, assessments = (ms,), (ma,)
    elif adverse_mode == "high_forecast":
        forecasts = (
            ETAForecast(
                work_item_id=1,
                ado_target_date=date(2026, 11, 20),
                predicted_target_date=date(2026, 11, 28),
                confidence=Confidence.HIGH,
                slip_probability=0.85,
                reasoning="High slip probability",
                prior_slips=1,
                p50_date=date(2026, 11, 25),
            ),
        )

    obs = observe_program_verdict(
        cap,
        snap,
        forecasts,
        risks=risks,
        milestones=milestones,
        milestone_assessments=assessments,
    )
    expected_health = RiskLevel.BLOCKED if adverse_mode == "blocked_item" else RiskLevel.HIGH
    assert obs.verdict.health == expected_health
    assert obs.verdict.contradictions, "optimistic scorecard vs adverse signal must emit RealityConflict"
    assert obs.verdict.contradictions[0].family == "verdict.contradiction"
    assert obs.verdict.contradictions[0].resolution == "conservative_max_floor"
    verify_program_verdict_oracle(obs.inputs, obs.verdict)

    with pytest.raises(ConfigError, match="oracle"):
        verify_program_verdict_oracle(obs.inputs, replace(obs.verdict, health=RiskLevel.LOW))
    with pytest.raises(ConfigError, match="oracle"):
        verify_program_verdict_oracle(obs.inputs, replace(obs.verdict, contradictions=()))


def test_unconfirmed_verdict_cannot_become_cross_program_authority(tmp_path: Path) -> None:
    from src.core.program_verdict import load_authoritative_program_risk
    from src.commands.report_scorecards import _load_latest_program_overall_risk as report_risk
    from src.commands.triage import _load_latest_program_overall_risk as triage_risk

    programs_root = tmp_path / "programs"
    _write_published_verdict(programs_root, health=RiskLevel.HIGH)

    assert load_authoritative_program_risk("foreign", programs_root=programs_root) is None
    assert report_risk("foreign", programs_root=programs_root) is None
    assert triage_risk("foreign", programs_root=programs_root) is None


def test_shared_confirmed_verdict_risk_reader_does_not_republish_archive_as_current() -> None:
    from src.core.program_verdict import load_authoritative_program_risk

    # No current publication and no indexed confirmation: retained risk archive history
    # cannot become a live cross-program ProgramVerdict.
    assert load_authoritative_program_risk("missing", programs_root=Path("not-created")) is None


def _seed_confirmed_and_draft_workspace(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    source = build_synthetic_report_source(tmp_path / "src")
    ws = tmp_path / "ws"
    reports_root = stage_v2_report_workspace(source, ws)
    programs_root = ws / "programs"
    archive_root = ws / "archive"
    disable_kusto_in_report_copy(reports_root)
    reset_overrides_to_seed_state(reports_root)
    milestones_path = programs_root / "acme" / "milestones.yaml"
    milestones = yaml.safe_load(milestones_path.read_text(encoding="utf-8"))
    for milestone in milestones["milestones"]:
        milestone["target_date"] = "2099-12-31"
        milestone["linked_work_item_ids"] = []
    milestones_path.write_text(yaml.safe_dump(milestones), encoding="utf-8")

    # Confirm issue 1 with HIGH verdict
    snap1 = _snapshot(item_risk=RiskLevel.HIGH, dim_risk=RiskLevel.LOW)
    cap1 = _capture(snap1)
    obs1 = observe_program_verdict(cap1, snap1, (), risks=(_high_risk(),))
    pub1 = programs_root / "acme" / "publications" / EDITION_NAME / "issue_001"
    pub1.mkdir(parents=True, exist_ok=True)
    (pub1 / "issue_001.manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "edition": EDITION_NAME,
                "issue_number": 1,
                "started_at": NOW.isoformat(),
                "metadata": {"program_verdict_observation": asdict(obs1)},
            },
            default=lambda v: v.isoformat() if hasattr(v, "isoformat") else str(v),
        ),
        encoding="utf-8",
    )
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=snap1,
        html_body="<html><body>Issue 1</body></html>",
        markdown_body="# Issue 1",
        manifest=RunManifest(
            manifest_id="m-1",
            issue_number=1,
            edition=EDITION_NAME,
            started_at=NOW,
            ended_at=NOW,
            config_hash="sha256:" + "b" * 64,
            snapshot_hash="sha256:" + "a" * 64,
            html_hash="sha256:" + "c" * 64,
            md_hash="sha256:" + "d" * 64,
            ado_calls=0,
            ai_calls=0,
            ai_cost_usd=0.0,
            freshness_summary={},
            qg_results={"QG-8": True},
            git_sha=None,
            metadata={"program_verdict_observation": asdict(obs1)},
        ),
        archive_root=archive_root,
    )

    # Create unconfirmed newer draft issue 2 with optimistic LOW verdict
    later = NOW + timedelta(days=7)
    snap2 = replace(_snapshot(item_risk=RiskLevel.LOW, dim_risk=RiskLevel.LOW), issue_number=2, generated_at=later, ado_data_as_of=later)
    cap2 = replace(
        _capture(snap2),
        as_of=later,
        valid_at=later,
        report_snapshot_hash=hash_jsonable(snap2),
        sha256="",
    )
    cap2_p = asdict(cap2)
    cap2_p.pop("sha256")
    cap2 = replace(cap2, sha256=hash_jsonable(cap2_p))
    obs2 = observe_program_verdict(cap2, snap2, ())
    pub2 = programs_root / "acme" / "publications" / EDITION_NAME / "issue_002"
    pub2.mkdir(parents=True, exist_ok=True)
    (pub2 / "issue_002.manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "edition": EDITION_NAME,
                "issue_number": 2,
                "started_at": later.isoformat(),
                "metadata": {"program_verdict_observation": asdict(obs2)},
            },
            default=lambda v: v.isoformat() if hasattr(v, "isoformat") else str(v),
        ),
        encoding="utf-8",
    )
    return ws, programs_root, reports_root, archive_root


def test_w41_confirmed_verdict_never_shadowed_by_unconfirmed_draft(tmp_path: Path) -> None:
    from src.core.program_verdict import load_authoritative_program_risk
    _, programs_root, _, archive_root = _seed_confirmed_and_draft_workspace(tmp_path)

    retained = load_latest_program_verdict(
        "acme", programs_root=programs_root, archive_root=archive_root, confirmed_only=True
    )
    assert retained is not None
    assert retained.health == RiskLevel.HIGH
    assert retained.as_of == NOW
    assert load_authoritative_program_risk(
        "acme", programs_root=programs_root, archive_root=archive_root
    ) == RiskLevel.HIGH
    doctor = doctor_cmd.run_doctor(
        edition_name=EDITION_NAME,
        reports_root=programs_root.parent / "reports",
        programs_root=programs_root,
        archive_root=archive_root,
        ado_probe=lambda _: doctor_cmd.ADOProbeResult(True, "synthetic", 1, None, "Synthetic ADO"),
    )
    check = next(row for row in doctor.checks if row.label == "Retained Verdict")
    assert check.status == "warn"
    assert check.metadata is not None
    assert check.metadata["snapshot_id"] == retained.snapshot_id
    assert check.metadata["health"] == retained.health.value

    # Default call when archive exists also refuses unconfirmed draft shadowing
    default_retained = load_latest_program_verdict("acme", programs_root=programs_root, archive_root=archive_root)
    assert default_retained is not None
    assert default_retained.health == RiskLevel.HIGH


def test_w42_all_five_surfaces_agree_on_published_verdict_without_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws, programs_root, reports_root, archive_root = _seed_confirmed_and_draft_workspace(tmp_path)
    monkeypatch.setenv("VERTEX_DB_PATH", str(ws / "vertex-db"))

    # 1. report health summary raises overall_risk & health_state conservatively for optimistic scorecards
    health = _build_health_summary(
        (DimensionRisk("Synthetic", RiskLevel.LOW, "Lane-0", None),),  # type: ignore[arg-type]
        None,
        risks=(_high_risk(),),
        as_of=NOW,
    )
    assert health.overall_risk == RiskLevel.HIGH
    assert health.health_state == "AT RISK"
    assert health.verdict_contradiction_count >= 1

    # End-to-end report draft compare_report_health matches on optimistic scorecards + adverse item risk
    draft_issue = report_cmd.generate_report_draft(
        EDITION_NAME,
        issue_number=3,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        dry_run=True,
        as_of=NOW + timedelta(days=14),
        work_item_loader=lambda _b, ts: (_sample_items(ts)[:1], 0),
        open_browser=False,
    )
    comparison = draft_issue.manifest.metadata["program_verdict_comparison"]
    assert comparison.risk_values_equal is True

    # 2. cockpit uses confirmed HIGH verdict (ignores newer unconfirmed LOW draft)
    cockpit = build_cockpit_snapshot("acme", programs_root=programs_root, edition_id=EDITION_NAME, now=NOW)
    assert cockpit.program_summary.overall_risk == "red"

    # 3. brief attaches & renders published HIGH verdict
    brief_report = brief_cmd.build_brief("acme", programs_root=programs_root, as_of=NOW)
    assert brief_report.published_verdict is not None
    assert brief_report.published_verdict.health == RiskLevel.HIGH
    rendered_brief = brief_cmd.render_brief(brief_report, programs_root=programs_root)
    assert "Program Verdict" in rendered_brief
    assert "HIGH" in rendered_brief.upper()

    # 4. escalate attaches published HIGH verdict
    rules_path = programs_root / "acme" / "escalation_rules.yaml"
    rules_path.write_text("schema_version: '1.0'\nrules: []\n", encoding="utf-8")
    escalate_artifacts = escalate_cmd.generate_escalations(
        edition_name=EDITION_NAME,
        dry_run=True,
        reports_root=reports_root,
        rules_path=rules_path,
        work_item_loader=lambda _b, ts: (_sample_items(ts)[:1], 0),
    )
    assert escalate_artifacts.published_verdict is not None
    assert escalate_artifacts.published_verdict.health == RiskLevel.HIGH
    escalate_text = escalate_cmd.render_escalation_preview_plaintext(escalate_artifacts)
    assert "Program Verdict:" in escalate_text
    assert "HIGH" in escalate_text.upper()
    escalate_json = json.loads(
        escalate_cmd.render_escalation_output(
            edition_name=EDITION_NAME,
            artifacts=escalate_artifacts,
            dry_run=True,
            channel="email",
            format="json",
        )
    )
    assert escalate_json["published_verdict"] is not None
    assert "HIGH" in escalate_json["published_verdict"].upper()

    # 5. review surfaces published HIGH verdict on ReviewFullContextData and rendered HTML
    review_ctx = review_full_cmd.prepare_review_full_context(
        EDITION_NAME,
        issue_number=3,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
    )
    assert review_ctx.published_verdict is not None
    assert review_ctx.published_verdict.health == RiskLevel.HIGH
    assert review_ctx.reviewer_context.published_verdict_line is not None
    assert "HIGH" in review_ctx.reviewer_context.published_verdict_line.upper()
    review_html = ReviewerRenderer(EDITION_NAME, reports_root=reports_root).render(review_ctx.reviewer_context)
    assert "Program Verdict:" in review_html
    assert "HIGH" in review_html.upper()

    # Cross-surface parity oracle across all 5 surfaces
    retained = load_latest_program_verdict("acme", programs_root=programs_root, archive_root=archive_root)
    assert retained is not None
    assert_cross_surface_verdict_parity(
        published=retained,
        report_risk=health.overall_risk,
        cockpit_overall_risk=cockpit.program_summary.overall_risk,
        brief_verdict=brief_report.published_verdict,
        escalate_verdict=escalate_artifacts.published_verdict,
        review_verdict=review_ctx.published_verdict,
    )


def test_w42_corrupt_verdict_manifest_fails_closed_across_brief_escalate_and_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws, programs_root, reports_root, archive_root = _seed_confirmed_and_draft_workspace(tmp_path)
    monkeypatch.setenv("VERTEX_DB_PATH", str(ws / "vertex-db"))
    report_cmd.generate_report_draft(
        EDITION_NAME,
        issue_number=3,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        dry_run=True,
        as_of=NOW + timedelta(days=14),
        work_item_loader=lambda _b, ts: (_sample_items(ts)[:1], 0),
        open_browser=False,
    )

    retained = load_latest_program_verdict(
        "acme", programs_root=programs_root, archive_root=archive_root, confirmed_only=True,
    )
    assert retained is not None
    manifest_path = retained.manifest_path
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_payload["metadata"]["program_verdict_observation"] = {"corrupt": True}
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")

    doctor = doctor_cmd.run_doctor(
        edition_name=EDITION_NAME, reports_root=reports_root, programs_root=programs_root,
        archive_root=archive_root,
        ado_probe=lambda _: doctor_cmd.ADOProbeResult(True, "synthetic", 1, None, "Synthetic ADO"),
    )
    assert next(row for row in doctor.checks if row.label == "Retained Verdict").status == "fail"
    brief_report = brief_cmd.build_brief("acme", programs_root=programs_root, as_of=NOW)
    assert brief_report.published_verdict is None
    assert any("Published program verdict unavailable" in line for line in brief_report.now_lines)

    rules_path = programs_root / "acme" / "escalation_rules.yaml"
    rules_path.write_text("schema_version: '1.0'\nrules: []\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        escalate_cmd.generate_escalations(
            edition_name=EDITION_NAME,
            dry_run=True,
            reports_root=reports_root,
            rules_path=rules_path,
            work_item_loader=lambda _b, ts: (_sample_items(ts)[:1], 0),
        )

    with pytest.raises(ConfigError):
        review_full_cmd.prepare_review_full_context(
            EDITION_NAME,
            issue_number=3,
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=programs_root,
        )


def test_w41_verdict_oracle_negative_paths_and_parity_conflicts() -> None:
    overdue_snap = Snapshot(
        issue_number=1,
        generated_at=NOW,
        ado_data_as_of=NOW,
        edition_type=EditionType.DETAILED,
        items=(SnapshotItem(1, "Task", "Overdue", "Active", None, "Synthetic", date(2026, 11, 1), RiskLevel.LOW, []),),
        scorecards=(ConfirmedDimension("Synthetic", "Lane-0", RiskLevel.LOW, None, 1, ""),),
    )
    overdue_obs = observe_program_verdict(_capture(overdue_snap), overdue_snap, ())
    assert overdue_obs.verdict.schedule_risk == RiskLevel.MEDIUM

    with pytest.raises(ConfigError, match="identity or clock"):
        verify_program_verdict_oracle(overdue_obs.inputs, replace(overdue_obs.verdict, program_id="other"))
    with pytest.raises(ConfigError, match="overdue item"):
        verify_program_verdict_oracle(overdue_obs.inputs, replace(overdue_obs.verdict, schedule_risk=RiskLevel.LOW))

    ms, ma = _missed_milestone()
    ms_obs = observe_program_verdict(_capture(overdue_snap), overdue_snap, (), milestones=(ms,), milestone_assessments=(ma,))
    with pytest.raises(ConfigError, match="missed milestone"):
        verify_program_verdict_oracle(ms_obs.inputs, replace(ms_obs.verdict, schedule_risk=RiskLevel.MEDIUM))

    at_risk_snap = _snapshot(item_risk=RiskLevel.LOW, dim_risk=RiskLevel.LOW)
    at_risk_ma = replace(ma, computed_health=MilestoneStatus.AT_RISK)
    at_risk_obs = observe_program_verdict(
        _capture(at_risk_snap), at_risk_snap, (), milestones=(ms,), milestone_assessments=(at_risk_ma,)
    )
    with pytest.raises(ConfigError, match="at-risk milestone"):
        verify_program_verdict_oracle(at_risk_obs.inputs, replace(at_risk_obs.verdict, schedule_risk=RiskLevel.LOW))

    published = load_latest_program_verdict_stub(overdue_obs.verdict.snapshot_id, overdue_obs.verdict.health)
    with pytest.raises(ConfigError, match="report_risk"):
        assert_cross_surface_verdict_parity(
            published=published,
            report_risk=RiskLevel.HIGH,
            cockpit_overall_risk="yellow",
            brief_verdict=published,
            escalate_verdict=published,
            review_verdict=published,
        )
    with pytest.raises(ConfigError, match="cockpit"):
        assert_cross_surface_verdict_parity(
            published=published,
            report_risk=RiskLevel.MEDIUM,
            cockpit_overall_risk="green",
            brief_verdict=published,
            escalate_verdict=published,
            review_verdict=published,
        )
    with pytest.raises(ConfigError, match="brief"):
        assert_cross_surface_verdict_parity(
            published=published,
            report_risk=RiskLevel.MEDIUM,
            cockpit_overall_risk="yellow",
            brief_verdict=None,
            escalate_verdict=published,
            review_verdict=published,
        )


def load_latest_program_verdict_stub(snapshot_id: str, health: RiskLevel):
    from src.core.program_verdict import PublishedProgramVerdict

    return PublishedProgramVerdict(
        program_id="acme",
        snapshot_id=snapshot_id,
        as_of=NOW,
        health=health,
        schedule_risk=RiskLevel.MEDIUM,
        confidence=Confidence.HIGH,
        completion_phase="active",
        reason_codes=("target_overdue",),
        evidence_gap_codes=(),
        leadership_ask_count=0,
        source_refs=(),
        manifest_path=Path("issue_001.manifest.json"),
    )


def test_w41_resolve_authoritative_program_risk_covers_forecasts_milestones_and_scorecards() -> None:
    closed_item = replace(_sample_items(NOW)[0], id=1, state="Closed")
    fc_closed = ETAForecast(
        work_item_id=1,
        ado_target_date=date(2026, 11, 1),
        predicted_target_date=date(2026, 11, 3),
        confidence=Confidence.HIGH,
        slip_probability=0.95,
        reasoning="Synthetic high slip",
        prior_slips=1,
        p50_date=date(2026, 11, 2),
    )
    fc_high = replace(fc_closed, work_item_id=2, slip_probability=0.85, p50_date=date(2026, 11, 15))
    fc_med = replace(fc_closed, work_item_id=3, slip_probability=0.55, p50_date=date(2026, 11, 1))
    non_strategic = replace(_high_risk(), id="risk-2", kind="operational")
    unassessed = replace(_high_risk(), id="risk-3", probability=RiskProbability.UNASSESSED)
    ms, ma = _missed_milestone()
    overdue_ms = replace(ms, id="ms-2", target_date=date(2026, 11, 1))
    at_risk_ma = replace(ma, milestone_id="ms-2", computed_health=MilestoneStatus.AT_RISK)
    low_sc = (DimensionRisk("Synthetic", RiskLevel.LOW, "Lane-0", None),)  # type: ignore[arg-type]

    high_res = resolve_authoritative_program_risk(
        scorecards=low_sc,
        items=(closed_item,),
        forecasts=(fc_closed, fc_high),
        risks=(non_strategic, unassessed),
        milestones=(ms,),
        milestone_assessments=(ma,),
        as_of=NOW,
    )
    assert high_res.overall_risk == RiskLevel.HIGH
    assert "miss_probability_high" in high_res.reason_codes
    assert "milestone_missed" in high_res.reason_codes
    assert len(high_res.contradictions) == 1

    med_res = resolve_authoritative_program_risk(
        scorecards=(DimensionRisk("Synthetic", RiskLevel.UNKNOWN, "Lane-0", None),),  # type: ignore[arg-type]
        items=(closed_item,),
        forecasts=(fc_med,),
        milestones=(overdue_ms,),
        milestone_assessments=(at_risk_ma,),
        as_of=NOW,
    )
    assert med_res.overall_risk == RiskLevel.MEDIUM
    assert "miss_probability_medium" in med_res.reason_codes
    assert "forecast_p50_past" in med_res.reason_codes
    assert "milestone_at_risk" in med_res.reason_codes
    assert "milestone_target_overdue" in med_res.reason_codes

    low_res = resolve_authoritative_program_risk(scorecards=low_sc, as_of=NOW)
    assert low_res.overall_risk == RiskLevel.LOW
    unk_res = resolve_authoritative_program_risk(
        scorecards=(DimensionRisk("Synthetic", RiskLevel.UNKNOWN, "Lane-0", None),),  # type: ignore[arg-type]
        as_of=NOW,
    )
    assert unk_res.overall_risk == RiskLevel.UNKNOWN


def test_w41_load_latest_program_verdict_manifest_validation_and_archive_edge_cases(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    archive_root = tmp_path / "archive"
    (programs_root / "acme" / "publications" / EDITION_NAME / "issue_001").mkdir(parents=True)

    with pytest.raises(ConfigError, match="identity"):
        load_latest_program_verdict("../bad", programs_root=programs_root)

    # An unreadable index cannot establish the absence of a confirmation.
    ed_arch = archive_root / EDITION_NAME
    ed_arch.mkdir(parents=True)
    (ed_arch / "index.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="archive index"):
        load_latest_program_verdict("acme", programs_root=programs_root, archive_root=archive_root, confirmed_only=True)

    (ed_arch / "index.json").write_text(json.dumps({"issues": "not-a-list"}), encoding="utf-8")
    with pytest.raises(ConfigError, match="archive index"):
        load_latest_program_verdict("acme", programs_root=programs_root, archive_root=archive_root)

    (ed_arch / "index.json").write_text(
        json.dumps({"issues": [{"kind": "dry_run", "issue_number": 1}, {"kind": "confirmed", "issue_number": "1"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="archive index"):
        load_latest_program_verdict("acme", programs_root=programs_root, archive_root=archive_root)
    (ed_arch / "index.json").write_text(json.dumps({"issues": []}), encoding="utf-8")

    manifest_path = programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.manifest.json"
    # No observation metadata -> None
    manifest_path.write_text(json.dumps({"schema_version": 1, "metadata": {}}), encoding="utf-8")
    assert load_latest_program_verdict("acme", programs_root=programs_root) is None

    for bad_payload, match in (
        ("[]", "invalid"),
        (json.dumps({"started_at": NOW.isoformat(), "metadata": {"program_verdict_observation": {"mode": "observe", "verdict": {"program_id": "other"}}}}), "identity"),
        (json.dumps({"metadata": {"program_verdict_observation": {"mode": "observe", "verdict": {"program_id": "acme"}}}}), "clock is unavailable"),
        (json.dumps({"started_at": "bad-clock", "metadata": {"program_verdict_observation": {"mode": "observe", "verdict": {"program_id": "acme"}}}}), "clock is invalid"),
        (json.dumps({"started_at": "2026-11-10T18:00:00", "metadata": {"program_verdict_observation": {"mode": "observe", "verdict": {"program_id": "acme"}}}}), "timezone-aware"),
    ):
        manifest_path.write_text(bad_payload, encoding="utf-8")
        with pytest.raises(ConfigError, match=match):
            load_latest_program_verdict("acme", programs_root=programs_root)
