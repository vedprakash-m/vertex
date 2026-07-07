from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil

import yaml

from typer.testing import CliRunner

from cli import app
from src.ai.cost_guard import CostGuard
from src.commands import triage
from src.commands.report_cascade import _format_dependency_cascade
from src.core.analytics_store import replace_contradiction_state
from src.core.cascade_detector import DependencyCascade
from src.core.incident_journal_store import append_incident_entry
from src.core.coverage_gap import CoverageGap
from src.core.gather_state_store import write_gather_state
from src.core.issue_projection import IssueProjection
from src.core.action_tracker import append_action
import src.core.archive_store as archive_store
from src.core.claim_tracker import ClaimAssessment
from src.core.claim_tracker import append_claim_entry, append_decision_ask
from src.core.journal import append_review_decision, append_signal
from src.core.models import Comment, Confidence, EditionType, Revision, RiskLevel, RunManifest, Snapshot, WorkItem
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus, ClaimEntry, Contradiction, ContradictionPacket, DataSourceType, DecisionAsk, IncidentEntry, IntegrationError, ResolvedContradiction, Signal, SignalReviewDecision, TrajectoryPoint
from src.core.sqlite_stores import SQLiteSignalStore, SQLiteTrajectoryStore
from src.core.trajectory import backfill_trajectory_points
from src.core.triage import ReadinessAssessment, StaleNarrativeFinding, TriageReport
from src.core.vitality_scorer import VitalitySummary
from tests.support.report_test_setup import _normalize_program_org, get_source_root


runner = CliRunner()


def test_triage_cli_supports_json_and_csv(monkeypatch) -> None:
    monkeypatch.setattr(
        triage,
        "generate_triage_report",
        lambda edition_name: triage.TriageArtifacts(
            report=TriageReport(
                edition_name=edition_name,
                issue_number=78,
                program_id="acme",
                readiness=ReadinessAssessment(
                    score=84,
                    quality_gate_pass_rate=90,
                    quality_gate_passed=9,
                    quality_gate_total=10,
                    unreviewed_signal_count=1,
                    missing_narrative_count=1,
                    missing_override_count=0,
                    coverage_gap_count=1,
                    written_narrative_count=4,
                    total_narrative_count=5,
                    set_override_count=5,
                    total_override_count=5,
                ),
                blockers=("QG-1: Freshness block",),
                needs_attention=("1 unreviewed signal",),
                contradictions=("WI:1001 (deployment_readiness) - WorkIQ and ADO disagree on the target date.",),
                decision_debt=("nudge | 16 day(s) open | Issue #078 ask-1 | owner lt | refs WI:1101 | Need LT decision on rollout gate. | Approve: vertex decisions nudge --program acme --id ask-1 --dry-run",),
                milestones=("M1 at risk",),
                risks=("Open risk register entry",),
                actions=("Action overdue",),
                decisions=("1 proposed decision aging",),
                assumptions=("1 assumption overdue",),
                telemetry=("Latest approved telemetry: analytics, 5 scope",),
                scorecard_composition=("1 proposed addition vs trusted issue #77",),
                section_roster=("1 missing prior section",),
                cross_program_cascades=("Outbound dependency risk",),
                active_issues=(
                    IssueProjection(
                        work_item_id=1001,
                        source_type="ado_blocked",
                        severity="block",
                        summary='WI:1001 "Blocked shiproom item" — blocked in ADO',
                        owner_alias="maintainer",
                        workstream_id="deployment_readiness",
                        ado_url="https://example/1001",
                        linked_entity_ids=("claim-1",),
                        confidence=Confidence.HIGH,
                    ),
                ),
                coverage_gaps=(
                    CoverageGap(
                        work_item_id=1002,
                        title="Missing narrative",
                        state="Active",
                        assigned_to="owner@example.com",
                        confidence=Confidence.HIGH,
                    ),
                ),
                ready=("4/5 workstream narratives written",),
                coverage_gap_window_days=14,
                correlated_items=(
                    triage.CorrelatedTriageItem(
                        work_item_id=1001,
                        work_item_title="Blocked shiproom item",
                        work_item_state="Active",
                        details=("Approved signal indicates blocker",),
                        confidence=Confidence.HIGH,
                    ),
                ),
                vitality_enabled=True,
                vitality_summary=VitalitySummary(
                    total_items=4,
                    updated_this_week=2,
                    updated_this_week_percentage=50,
                    freshness_average_days=6.5,
                    stale_owner_aliases=("maintainer",),
                ),
                stale_narratives=(
                    StaleNarrativeFinding(
                        section_id="deployment_velocity",
                        section_title="Deployment Velocity",
                        narrative_path="narratives/issue_078/ws_deployment_velocity.md",
                        narrative_last_modified=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
                        work_item_id=1003,
                        work_item_title="Deployment velocity milestone",
                        eta_changed_on=date(2026, 5, 8),
                        confidence=Confidence.HIGH,
                    ),
                ),
                stale_claims=(
                    ClaimAssessment(
                        claim=ClaimEntry(
                            id="claim-1",
                            program_id="acme",
                            edition_id="acme_weekly",
                            issue_number=77,
                            workstream_id="deployment_readiness",
                            text="WI:1001 rollout expected by May 01",
                            entity_refs=("WI:1001",),
                            claim_date=date(2026, 4, 28),
                            owner_alias="owner",
                            due_date=date(2026, 5, 1),
                        ),
                        effective_status="stale",
                        reason="Claim due 2026-05-01 has passed.",
                        confidence=Confidence.HIGH,
                    ),
                ),
                open_decision_ask_count=1,
            ),
            exit_code=3,
            gather_integration_details=(
                {
                    "source": "workiq",
                    "stage": "gather",
                    "retryable": True,
                    "message": "workiq unavailable",
                    "operator_action": "Verify Agency CLI WorkIQ support before retrying gather.",
                },
            ),
        ),
    )

    json_result = runner.invoke(app, ["triage", "--edition", "acme_weekly", "--format", "json"])

    assert json_result.exit_code == 3
    payload = json.loads(json_result.stdout)
    assert payload["edition_name"] == "acme_weekly"
    assert payload["program_id"] == "acme"
    assert payload["exit_code"] == 3
    assert payload["counts"]["active_issues"] == 1
    assert payload["counts"]["contradictions"] == 1
    assert payload["counts"]["decision_debt"] == 1
    assert payload["counts"]["stale_claims"] == 1
    assert payload["counts"]["stale_narratives"] == 1
    assert payload["counts"]["telemetry"] == 1
    assert payload["counts"]["scorecard_composition"] == 1
    assert payload["counts"]["section_roster"] == 1
    assert payload["active_issues"][0]["work_item_id"] == 1001
    assert payload["active_issues"][0]["confidence"] == "high"
    assert payload["contradictions"] == ["WI:1001 (deployment_readiness) - WorkIQ and ADO disagree on the target date."]
    assert payload["decision_debt"] == ["nudge | 16 day(s) open | Issue #078 ask-1 | owner lt | refs WI:1101 | Need LT decision on rollout gate. | Approve: vertex decisions nudge --program acme --id ask-1 --dry-run"]
    assert payload["telemetry"] == ["Latest approved telemetry: analytics, 5 scope"]
    assert payload["scorecard_composition"] == ["1 proposed addition vs trusted issue #77"]
    assert payload["section_roster"] == ["1 missing prior section"]
    assert payload["readiness"]["score"] == 84
    assert payload["correlated_items"][0]["confidence"] == "high"
    assert payload["coverage_gaps"][0]["confidence"] == "high"
    assert payload["stale_claims"][0]["confidence"] == "high"
    assert payload["stale_narratives"][0]["confidence"] == "high"
    assert payload["gather_integration_details"] == [
        {
            "source": "workiq",
            "stage": "gather",
            "retryable": True,
            "message": "workiq unavailable",
            "operator_action": "Verify Agency CLI WorkIQ support before retrying gather.",
        }
    ]

    csv_result = runner.invoke(app, ["triage", "--edition", "acme_weekly", "--format", "csv"])

    assert csv_result.exit_code == 3
    lines = csv_result.stdout.strip().splitlines()
    assert lines[0] == "entry_type,edition_name,issue_number,program_id,status,ref_id,title,owner_alias,workstream_id,detail"
    assert any(line.startswith('summary,acme_weekly,78,acme,3,,"Draft readiness: 84%') for line in lines[1:])
    assert any(
        line.startswith('telemetry,acme_weekly,78,acme,,,"Latest approved telemetry: analytics, 5 scope"')
        for line in lines[1:]
    )
    assert any("contradiction,acme_weekly,78,acme,,,WI:1001 (deployment_readiness) - WorkIQ and ADO disagree on the target date." in line for line in lines[1:])
    assert any("decision_debt,acme_weekly,78,acme,,,nudge | 16 day(s) open | Issue #078 ask-1 | owner lt | refs WI:1101 | Need LT decision on rollout gate. | Approve: vertex decisions nudge --program acme --id ask-1 --dry-run" in line for line in lines[1:])
    assert any("active_issue,acme_weekly,78,acme,block,1001" in line for line in lines[1:])
    assert any(
        line.startswith("correlated_item,acme_weekly,78,acme,Active,1001,Blocked shiproom item,,,")
        and '""high""' in line
        for line in lines[1:]
    )
    assert any(
        line.startswith("coverage_gap,acme_weekly,78,acme,Active,1002,Missing narrative,owner@example.com,,")
        and '""high""' in line
        for line in lines[1:]
    )
    assert any(
        line.startswith("stale_narrative,acme_weekly,78,acme,,deployment_velocity,Deployment Velocity,,,")
        and "high confidence" in line
        for line in lines[1:]
    )
    assert any(
        line.startswith("stale_claim,acme_weekly,78,acme,stale,claim-1,WI:1001 rollout expected by May 01,owner,deployment_readiness,")
        and "high confidence" in line
        for line in lines[1:]
    )
    assert any("integration_detail,acme_weekly,78,acme,retryable,workiq,workiq unavailable,,gather," in line for line in lines[1:])


def test_generate_triage_report_surfaces_cached_contradictions(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)

    replace_contradiction_state(
        "acme",
        (
            ContradictionPacket(
                work_item_id=1001,
                workstream_id="deployment_readiness",
                contradictions=(
                    Contradiction(
                        field="target_date",
                        source_a="ado/target_date",
                        source_b="workiq/signal",
                        summary="workiq/email implies a later landing date than ADO",
                        confidence=Confidence.HIGH,
                        evidence_refs=("WI:1001", "sig-1"),
                    ),
                ),
                confidence=Confidence.HIGH,
                recommended_resolution=ResolvedContradiction(
                    winning_source=DataSourceType.WORKIQ,
                    confidence=Confidence.HIGH,
                    rationale="Recent external signals are more reliable than the stale ADO target.",
                    evidence_refs=("sig-1",),
                ),
                generated_at=as_of,
            ),
        ),
        programs_root=programs_root,
    )

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    rendered = triage.render_triage_report(artifacts.report)
    payload = json.loads(triage.render_triage_output(artifacts, format="json"))

    assert any("active contradiction" in line for line in artifacts.report.needs_attention)
    assert not any("aged decision ask" in line for line in artifacts.report.needs_attention)
    assert artifacts.report.contradictions == (
        "WI:1001 (deployment_readiness) - workiq/email implies a later landing date than ADO. Prefer workiq (high)",
    )
    assert artifacts.report.decision_debt == ()


def test_generate_triage_report_surfaces_decision_debt_section(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    as_of = datetime(2026, 5, 26, 18, 0, tzinfo=timezone.utc)

    append_decision_ask(
        DecisionAsk(
            id="ask-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=78,
            text="Need LT decision on rollout gate.",
            entity_refs=("WI:1101",),
            ask_date=date(2026, 5, 10),
            owner_alias="lt",
        ),
        programs_root=programs_root,
    )

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        work_item_loader=lambda bundle, timestamp: (_sample_issue_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    rendered = triage.render_triage_report(artifacts.report)

    assert artifacts.report.decision_debt == (
        "nudge | 16 day(s) open | Issue #078 ask-1 | owner lt | refs WI:1101 | Need LT decision on rollout gate. | Approve: vertex decisions nudge --program acme --id ask-1 --dry-run",
    )
    assert "DECISION DEBT:" in rendered
    assert "nudge | 16 day(s) open | Issue #078 ask-1 | owner lt | refs WI:1101 | Need LT decision on rollout gate. | Approve: vertex decisions nudge --program acme --id ask-1 --dry-run" in rendered


def test_generate_triage_report_surfaces_incident_learning_section(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    as_of = datetime(2026, 5, 26, 18, 0, tzinfo=timezone.utc)

    append_incident_entry(
        IncidentEntry(
            schema_version="1.0",
            program_id="acme",
            incident_id="4101",
            signal_id="sig-icm-1",
            observed_at=datetime(2026, 5, 20, 16, 0, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 5, 20, 16, 5, tzinfo=timezone.utc),
            belief_change_summary="IcM 4101: WI:1001 rollout validation regressed under failover.",
            workstream_id="deployment_readiness",
            owning_team="Acme",
            severity=2,
            source_path="icm://4101",
            query_id="query-1",
            linked_work_item_ids=(1001,),
            ado_entity_refs=("WI:1001",),
            raw_ref="raw-1",
            confidence=Confidence.HIGH,
        ),
        programs_root=programs_root,
    )
    append_incident_entry(
        IncidentEntry(
            schema_version="1.0",
            program_id="acme",
            incident_id="4102",
            signal_id="sig-icm-2",
            observed_at=datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 5, 24, 12, 10, tzinfo=timezone.utc),
            belief_change_summary="IcM 4102: WI:1001 rollout validation regressed under failover again.",
            workstream_id="deployment_readiness",
            owning_team="Acme",
            severity=3,
            source_path="icm://4102",
            query_id="query-2",
            linked_work_item_ids=(1001,),
            ado_entity_refs=("WI:1001",),
            raw_ref="raw-2",
            confidence=Confidence.MEDIUM,
        ),
        programs_root=programs_root,
    )
    append_incident_entry(
        IncidentEntry(
            schema_version="1.0",
            program_id="acme",
            incident_id="4103",
            signal_id="sig-icm-3",
            observed_at=datetime(2026, 5, 25, 9, 0, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 5, 25, 9, 5, tzinfo=timezone.utc),
            belief_change_summary="Cache warmup assumptions were incomplete.",
            workstream_id="deployment_readiness",
            owning_team="Acme",
            severity=3,
            source_path="icm://4103",
            query_id="query-3",
            linked_work_item_ids=(),
            ado_entity_refs=(),
            raw_ref="raw-3",
            confidence=Confidence.MEDIUM,
        ),
        programs_root=programs_root,
    )

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        work_item_loader=lambda bundle, timestamp: (_sample_issue_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    rendered = triage.render_triage_report(artifacts.report)
    payload = json.loads(triage.render_triage_output(artifacts, format="json"))
    csv_output = triage.render_triage_output(artifacts, format="csv")

    assert any("recent incident learning" in line for line in artifacts.report.needs_attention)
    assert artifacts.report.incident_learnings[0].confidence.value == "high"
    assert artifacts.report.incident_learnings[1].confidence.value == "medium"
    assert payload["counts"]["incident_learnings"] == 2
    assert payload["incident_learnings"] == [
        {
            "confidence": "high",
            "summary": "Incident learning WI:1001 (deployment_readiness): Recurred across 2 incident learnings. WI:1001 rollout validation regressed under failover; WI:1001 rollout validation regressed under failover again. Source: IcM 4101, IcM 4102.",
            "summary_with_confidence": "Incident learning WI:1001 (deployment_readiness): Recurred across 2 incident learnings. WI:1001 rollout validation regressed under failover; WI:1001 rollout validation regressed under failover again. Source: IcM 4101, IcM 4102. (high confidence)",
        },
        {
            "confidence": "medium",
            "summary": "Incident learning IcM 4103 (deployment_readiness): Cache warmup assumptions were incomplete.",
            "summary_with_confidence": "Incident learning IcM 4103 (deployment_readiness): Cache warmup assumptions were incomplete. (medium confidence)",
        },
    ]
    assert "INCIDENT LEARNINGS:" in rendered
    assert "Incident learning WI:1001 (deployment_readiness): Recurred across 2 incident learnings." in rendered
    assert "high confidence" in rendered
    assert "incident_learning,acme_weekly,78,acme,high" in csv_output
    assert "medium confidence" in csv_output


def test_generate_triage_report_surfaces_scorecard_composition_from_continuation_contract(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)

    contract_dir = programs_root / "acme" / "publications" / "acme_weekly"
    contract_dir.mkdir(parents=True, exist_ok=True)
    issue_dir = contract_dir / "issue_078"
    issue_dir.mkdir(exist_ok=True)
    (issue_dir / "issue_078.continuation_contract.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "edition": "acme_weekly",
                "issue_number": 78,
                "prior_trusted_issue": 77,
                "first_inherited_at": as_of.isoformat(),
                "last_refreshed_at": as_of.isoformat(),
                "scorecard_composition": {
                    "frozen_from_issue": 77,
                    "inherited_dimensions": [["Acme Adventure/XIO 100% Ramp Readiness", "Deployment Velocity"]],
                    "proposed_additions": [["Contoso Pilot Readiness", "Buildout"]],
                    "proposed_removals": [["Acme Adventure/XIO 100% Ramp Readiness", "LSO"]],
                    "removed_by_override": [["Acme Adventure/XIO 100% Ramp Readiness", "Networking"]],
                },
                "section_roster": {
                    "inherited_sections": [],
                    "seeded_from_prior": False,
                    "sections_missing_evidence": [],
                    "added_sections": ["new_section"],
                    "removed_sections": ["networking"],
                },
                "narrative_seeding": {
                    "seeded": False,
                    "source_issue": 77,
                    "source_path": "archive",
                    "files_seeded": [],
                    "source_hashes": {},
                },
                "overrides_seeding": {
                    "seeded": False,
                    "source_issue": 77,
                    "fields_carried": [],
                    "fields_cleared": [],
                },
                "evidence_quality": {
                    "sections_with_ado_coverage": 0,
                    "sections_with_query_only": 0,
                    "sections_with_connector_only": 0,
                    "sections_manual_only": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    rendered = triage.render_triage_report(artifacts.report)

    assert "SCORECARD COMPOSITION:" in rendered
    assert "SECTION ROSTER:" in rendered
    assert any("proposed addition(s) vs trusted issue #77" in line for line in artifacts.report.scorecard_composition)
    assert any("Add: Contoso Pilot Readiness :: Buildout" in line for line in artifacts.report.scorecard_composition)
    assert any("Missing current evidence: Acme Adventure/XIO 100% Ramp Readiness :: LSO" in line for line in artifacts.report.scorecard_composition)
    assert any("Removed by override: Acme Adventure/XIO 100% Ramp Readiness :: Networking" in line for line in artifacts.report.scorecard_composition)
    assert any("section addition(s) vs trusted issue #77" in line for line in artifacts.report.section_roster)
    assert any("Add section: new_section" in line for line in artifacts.report.section_roster)
    assert any("Missing prior section: networking" in line for line in artifacts.report.section_roster)


def test_generate_triage_report_uses_trusted_baseline_for_previous_snapshot(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    reports_root, archive_root, _ = _seed_v2_triage_layout(repo_root, tmp_path)
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)

    baseline_call: dict[str, int | None] = {}
    snapshot_call: dict[str, int | None] = {}
    original_load_previous_snapshot = triage.report_command_helpers._load_previous_snapshot

    def _fake_load_trusted_baseline_issue(*args, **kwargs):
        del args
        baseline_call["before_issue_number"] = kwargs.get("before_issue_number")
        return 77

    def _capturing_load_previous_snapshot(*args, **kwargs):
        snapshot_call["trusted_issue_number"] = kwargs.get("trusted_issue_number")
        return original_load_previous_snapshot(*args, **kwargs)

    monkeypatch.setattr(triage, "load_trusted_baseline_issue", _fake_load_trusted_baseline_issue)
    monkeypatch.setattr(triage.report_command_helpers, "_load_previous_snapshot", _capturing_load_previous_snapshot)

    triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    assert baseline_call["before_issue_number"] == 78
    assert snapshot_call["trusted_issue_number"] == 77


def test_generate_triage_report_surfaces_gather_integration_diagnostics(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)

    write_gather_state(
        "acme",
        gathered_at=as_of,
        scanned_items=4,
        discovered_signals=2,
        new_signals=1,
        pending_review=1,
        trajectory_updates=0,
        auto_reviews_written=0,
        ado_calls=3,
        archived_journal_files=0,
        background_proposals=0,
        integration_errors=1,
        integration_error_details=(
            IntegrationError(
                source="kusto",
                stage="gather",
                retryable=True,
                message="kusto unavailable",
                operator_action="Run 'vertex admin auth setup' and verify Kusto cluster access before retrying gather.",
            ),
        ),
        programs_root=programs_root,
    )

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    rendered = triage.render_triage_report(artifacts.report)
    payload = json.loads(triage.render_triage_output(artifacts, format="json"))

    assert any("Latest gather recorded 1 optional integration failure" in line for line in artifacts.report.integration_diagnostics)
    assert any("kusto/gather: kusto unavailable" in line for line in artifacts.report.integration_diagnostics)
    assert any("integration diagnostic" in line for line in artifacts.report.needs_attention)
    assert "INTEGRATION DIAGNOSTICS:" in rendered
    assert "Run 'vertex admin auth setup' and verify Kusto cluster access before retrying gather." in rendered
    assert payload["counts"]["integration_diagnostics"] >= 2
    assert any("kusto/gather: kusto unavailable" in line for line in payload["integration_diagnostics"])
    assert payload["gather_integration_details"] == [
        {
            "source": "kusto",
            "stage": "gather",
            "retryable": True,
            "message": "kusto unavailable",
            "operator_action": "Run 'vertex admin auth setup' and verify Kusto cluster access before retrying gather.",
        }
    ]

    csv_rows = triage.render_triage_output(artifacts, format="csv").splitlines()
    assert any(row.startswith("integration_detail,acme_weekly,78,acme,retryable,kusto,kusto unavailable,,gather,") for row in csv_rows[1:])


def test_generate_triage_report_surfaces_ai_cost_guard_breach(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    CostGuard(
        edition="acme_weekly",
        run_id="triage-run-001",
        budget_usd=0.5,
        programs_root=programs_root,
    ).record_actual(0.6)

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    rendered = triage.render_triage_report(artifacts.report)
    payload = json.loads(triage.render_triage_output(artifacts, format="json"))

    assert any(
        "AI cost ceiling exceeded for acme_weekly: $0.600 / $0.50 across 1 AI call(s) (run triage-run-001)." in line
        for line in artifacts.report.integration_diagnostics
    )
    assert any("integration diagnostic" in line for line in artifacts.report.needs_attention)
    assert "INTEGRATION DIAGNOSTICS:" in rendered
    assert any(
        "AI cost ceiling exceeded for acme_weekly: $0.600 / $0.50 across 1 AI call(s) (run triage-run-001)." in line
        for line in payload["integration_diagnostics"]
    )


def test_echo_console_safe_replaces_unencodable_characters(monkeypatch) -> None:
    echoed: list[str] = []
    stdout = type("Stdout", (), {"encoding": "cp1252"})()

    def _fake_echo(text: str, nl: bool = True) -> None:
        echoed.append(text)
        if len(echoed) == 1:
            raise UnicodeEncodeError("cp1252", text, 6, 7, "character maps to <undefined>")

    monkeypatch.setattr(triage.sys, "stdout", stdout)
    monkeypatch.setattr(triage.typer, "echo", _fake_echo)

    triage._echo_console_safe("alpha ✅ beta")

    assert echoed == ["alpha ✅ beta", "alpha ? beta"]


def test_generate_triage_report_surfaces_latest_approved_telemetry_summary(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)

    for signal in (
        Signal(
            id="telemetry-analytics",
            timestamp=datetime(2026, 5, 10, 16, 0, tzinfo=timezone.utc),
            source="ado/analytics",
            program_id="acme",
            workstream_id="acme",
            entity_refs=("WI:1001",),
            text="Analytics snapshot for triage telemetry.",
            raw_ref="ado-analytics:telemetry-analytics",
            confidence=Confidence.HIGH,
            metadata={
                "snapshot_item_count": 5,
                "completed_item_count": 2,
                "scope_delta_count": 2,
                "open_delta_count": -1,
                "average_cycle_time_days": 5.0,
                "average_lead_time_days": 8.0,
            },
            thread_id=None,
        ),
        Signal(
            id="telemetry-sprint",
            timestamp=datetime(2026, 5, 10, 16, 15, tzinfo=timezone.utc),
            source="ado/sprint",
            program_id="acme",
            workstream_id="acme",
            entity_refs=("WI:1001",),
            text="Sprint snapshot for triage telemetry.",
            raw_ref="ado-sprint:telemetry-sprint",
            confidence=Confidence.HIGH,
            metadata={
                "iteration_name": "Sprint 24",
                "completion_pct": 50,
                "open_item_count": 1,
                "team_member_count": 3,
                "total_capacity_per_day": 24.0,
            },
            thread_id=None,
        ),
    ):
        append_signal(signal, programs_root=programs_root, partition_at=as_of)
        append_review_decision(
            "acme",
            SignalReviewDecision(
                signal_id=signal.id,
                decision="approved",
                reviewed_at=as_of,
                reviewed_by="system",
                note=None,
            ),
            programs_root=programs_root,
        )

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    rendered = triage.render_triage_report(artifacts.report)

    assert artifacts.report.telemetry == (
        "Latest approved telemetry: analytics, 5 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d; sprint, Sprint 24, 50% complete, 1 open, team cap 24.0h/day across 3 members",
    )
    assert "TELEMETRY:" in rendered
    assert "Latest approved telemetry: analytics, 5 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d; sprint, Sprint 24, 50% complete, 1 open, team cap 24.0h/day across 3 members" in rendered


def test_generate_triage_report_aggregates_unreviewed_signals_and_coverage_gaps(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    narratives_078 = programs_root / "acme" / "narratives" / "issue_078"
    if narratives_078.exists():
        shutil.rmtree(narratives_078)
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)

    append_signal(
        Signal(
            id="approved-signal",
            timestamp=as_of,
            source="ado/revision",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:1001",),
            text="Approved",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
            thread_id=None,
        ),
        programs_root=programs_root,
        partition_at=as_of,
    )
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="approved-signal",
            decision="approved",
            reviewed_at=as_of,
            reviewed_by="system",
            note=None,
        ),
        programs_root=programs_root,
    )
    append_signal(
        Signal(
            id="pending-signal",
            timestamp=as_of,
            source="workiq/email",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:1002",),
            text="Needs review",
            raw_ref=None,
            confidence=Confidence.MEDIUM,
            metadata=None,
            thread_id=None,
        ),
        programs_root=programs_root,
        partition_at=as_of,
    )

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    assert artifacts.report.readiness.unreviewed_signal_count == 1
    assert artifacts.report.readiness.coverage_gap_count == 1
    assert artifacts.report.readiness.missing_narrative_count > 0
    assert any("unreviewed signal" in line for line in artifacts.report.needs_attention)
    assert artifacts.report.vitality_summary is not None
    assert artifacts.report.vitality_summary.updated_this_week_percentage == 50


def test_generate_triage_report_surfaces_risk_register(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    _write_triage_risks(programs_root)

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    rendered = triage.render_triage_report(artifacts.report)

    assert any("Firmware sign-off remains open" in line for line in artifacts.report.risks)
    assert any("risk register" in line.lower() for line in artifacts.report.needs_attention)
    assert "RISK REGISTER:" in rendered


def test_generate_triage_report_includes_raid_chain_for_risk_register(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    _write_triage_risks(programs_root)

    risk_register_path = programs_root / "acme" / "risk_register.yaml"
    risk_register_payload = yaml.safe_load(risk_register_path.read_text(encoding="utf-8"))
    risk_register_payload["risks"][0]["linked_action_ids"] = ["acme-action-raid"]
    risk_register_path.write_text(yaml.safe_dump(risk_register_payload, sort_keys=False), encoding="utf-8")

    append_action(
        "acme",
        ActionItem(
            id="acme-action-raid",
            program_id="acme",
            text="Escalate the firmware sign-off review.",
            owner_alias="owner",
            due_date=date(2026, 5, 11),
            status=ActionStatus.IN_PROGRESS,
            source_signal_id=None,
            source_type=ActionSourceType.MANUAL,
            linked_work_item_ids=(1001,),
            linked_claim_id=None,
            linked_risk_id="acme-risk-1",
            workstream_id="deployment_readiness",
            created_at=datetime(2026, 5, 8, 9, 0, tzinfo=timezone.utc),
            resolved_at=None,
            resolution_note=None,
        ),
        programs_root=programs_root,
    )

    (programs_root / "acme" / "decisions.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "decisions": [
                    {
                        "id": "acme-decision-raid",
                        "program_id": "acme",
                        "title": "Escalation path for sign-off",
                        "context": "Firmware sign-off remains open near pilot readiness.",
                        "decision": "Escalate daily until sign-off lands.",
                        "rationale": None,
                        "alternatives_considered": [],
                        "decided_by": "owner",
                        "decision_date": "2026-05-09",
                        "status": "proposed",
                        "superseded_by": None,
                        "linked_claim_id": None,
                        "linked_risk_id": None,
                        "linked_action_ids": ["acme-action-raid"],
                        "workstream_id": "deployment_readiness",
                        "entity_refs": ["WI:1001"],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    (programs_root / "acme" / "assumptions.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "assumptions": [
                    {
                        "id": "acme-assumption-raid",
                        "program_id": "acme",
                        "text": "Firmware sign-off can still land before pilot start.",
                        "validation_method": "Check the firmware review notes.",
                        "validation_due": "2026-05-11",
                        "status": "unvalidated",
                        "linked_risk_id": "acme-risk-1",
                        "linked_milestone_id": None,
                        "owner_alias": "owner",
                        "identified_date": "2026-05-01",
                        "entity_refs": ["WI:1001"],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    rendered = triage.render_triage_report(artifacts.report)

    assert any("RAID risk:acme-risk-1[open]" in line for line in artifacts.report.risks)
    assert "assumption:acme-assumption-raid[unvalidated]" in rendered
    assert "action:acme-action-raid[in_progress]" in rendered
    assert "decision:acme-decision-raid[proposed]" in rendered


def test_generate_triage_report_surfaces_actions(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    _write_triage_actions(programs_root)

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    rendered = triage.render_triage_report(artifacts.report)

    assert any("Follow up with the firmware team" in line for line in artifacts.report.actions)
    assert any("action" in line.lower() for line in artifacts.report.needs_attention)
    assert "ACTIONS:" in rendered


def test_generate_triage_report_surfaces_cross_program_cascades(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)

    append_signal(
        Signal(
            id="cascade-signal",
            timestamp=as_of,
            source="ado/revision",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("m3-code-complete",),
            text="m3-code-complete slipped after the latest ADO update.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
            thread_id=None,
        ),
        programs_root=programs_root,
        partition_at=as_of,
    )
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="cascade-signal",
            decision="approved",
            reviewed_at=as_of,
            reviewed_by="system",
            note=None,
        ),
        programs_root=programs_root,
    )

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    rendered = triage.render_triage_report(artifacts.report)

    assert any("m3-code-complete can impact fabrikam:buildouts" in line for line in artifacts.report.cross_program_cascades)
    assert any("cross-program dependency cascade warning" in line.lower() for line in artifacts.report.needs_attention)
    assert "CROSS-PROGRAM CASCADES:" in rendered


def test_generate_triage_report_flags_low_risk_cross_program_understatement(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    _seed_triage_low_override(programs_root, issue_number=78, dimension_name="Deployment Velocity")
    _write_triage_armada_high_dependency(programs_root)

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    rendered = triage.render_triage_report(artifacts.report)

    assert any("LOW risk may be understated - cross-org dependency pressure: Deployment Velocity depends on fabrikam:buildouts" in line for line in artifacts.report.cross_program_cascades)
    assert any("fabrikam's latest confirmed issue is HIGH" in line for line in artifacts.report.cross_program_cascades)
    assert any("cross-program dependency cascade warning" in line.lower() for line in artifacts.report.needs_attention)
    assert "LOW risk may be understated - cross-org dependency pressure: Deployment Velocity depends on fabrikam:buildouts" in rendered


def test_format_dependency_cascade_prefixes_cross_org_classification() -> None:
    cascade = DependencyCascade(
        source_item="acme",
        target_item="fabrikam:buildouts",
        impact="Fabrikam buildouts can block the Acme deployment review.",
        resolution_path="cross_org_compute_pf",
        trigger_kind="signal",
        trigger_detail="Dependency moved.",
        work_item_id=1234,
        target_sections=(),
        target_workstream_ids=("fabrikam:buildouts",),
    )

    assert _format_dependency_cascade(cascade).startswith("[Cross-org] acme can impact fabrikam:buildouts")


def test_format_dependency_cascade_leaves_intra_storage_unprefixed() -> None:
    cascade = DependencyCascade(
        source_item="acme",
        target_item="dd_on_pf",
        impact="DD pilot sequencing can slip.",
        resolution_path="intra_storage",
        trigger_kind="drift",
        trigger_detail="Target drifted.",
        work_item_id=1234,
        target_sections=(),
        target_workstream_ids=("dd_on_pf",),
    )

    assert _format_dependency_cascade(cascade).startswith("acme can impact dd_on_pf")


def test_generate_triage_report_surfaces_pending_dependency_proposals(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    _write_triage_dependency_proposals(programs_root)

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    rendered = triage.render_triage_report(artifacts.report)
    payload = json.loads(triage.render_triage_output(artifacts, format="json"))
    csv_output = triage.render_triage_output(artifacts, format="csv")

    assert any("pending dependency proposal" in line.lower() for line in artifacts.report.needs_attention)
    assert artifacts.report.dependency_proposals[0].confidence.value == "medium"
    assert "DEPENDENCY PROPOSALS:" in rendered
    assert "medium confidence" in rendered
    assert payload["counts"]["dependency_proposals"] == 1
    assert payload["dependency_proposals"] == [
        {
            "accept_command": "vertex dependencies accept --program acme --id dep-proposal-1",
            "confidence": "medium",
            "detection_method": "comment_language",
            "from_item_id": 1001,
            "from_workstream_id": "deployment_readiness",
            "id": "dep-proposal-1",
            "occurrence_count": 2,
            "status": "proposed",
            "summary": "dep-proposal-1 | deployment_readiness:1001 -> platform_readiness:1002 | comment_language | 2 signal(s) | medium confidence | Accept: vertex dependencies accept --program acme --id dep-proposal-1",
            "to_item_id": 1002,
            "to_workstream_id": "platform_readiness",
        }
    ]
    assert "dependency_proposal,acme_weekly,78,acme,proposed,dep-proposal-1" in csv_output
    assert '""confidence"": ""medium""' in csv_output


def test_generate_triage_report_flags_action_resolution_candidates(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    _write_triage_actions(programs_root)
    backfill_trajectory_points(
        "acme",
        1001,
        (
            TrajectoryPoint(
                date=date(2026, 5, 9),
                state="Resolved",
                assigned_to="owner@example.com",
                target_date=None,
                risk_level=None,
                area_path="One\\Acme",
            ),
        ),
        programs_root=programs_root,
    )

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    correlated = next(item for item in artifacts.report.correlated_items if item.work_item_id == 1001)

    assert any("candidate for resolution" in line for line in artifacts.report.actions)
    assert any("candidate for resolution after linked ADO update" in line for line in artifacts.report.needs_attention)
    assert any("candidate for resolution" in detail for detail in correlated.details)


def test_generate_triage_report_surfaces_decisions(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    as_of = datetime(2026, 5, 20, 18, 0, tzinfo=timezone.utc)
    _write_triage_decisions(programs_root)

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    rendered = triage.render_triage_report(artifacts.report)

    assert any("Choose rollout path" in line for line in artifacts.report.decisions)
    assert any("proposed decision" in line.lower() for line in artifacts.report.needs_attention)
    assert "DECISIONS:" in rendered


def test_generate_triage_report_adds_raci_hint_for_chronic_high_dimension(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    _set_triage_workstream_accountable(programs_root, workstream_id="acme", accountable="priya")
    _seed_triage_high_override(programs_root, issue_number=78, dimension_name="Deployment Velocity")
    _seed_triage_chronic_high_history(programs_root, edition_name="acme_weekly", dimension_name="Deployment Velocity")

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        work_item_loader=lambda bundle, timestamp: (_sample_chronic_high_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    assert any("Chronic High dimension Deployment Velocity" in line for line in artifacts.report.needs_attention)
    assert any("Escalate to: priya" in line for line in artifacts.report.needs_attention)


def test_generate_triage_report_skips_raci_hint_when_absent(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    _clear_triage_workstream_accountable(programs_root, workstream_id="acme")
    _seed_triage_high_override(programs_root, issue_number=78, dimension_name="Deployment Velocity")
    _seed_triage_chronic_high_history(programs_root, edition_name="acme_weekly", dimension_name="Deployment Velocity")

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        work_item_loader=lambda bundle, timestamp: (_sample_chronic_high_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    assert not any("Chronic High dimension Deployment Velocity" in line for line in artifacts.report.needs_attention)
    assert not any("Escalate to:" in line for line in artifacts.report.needs_attention)


def test_generate_triage_report_surfaces_assumptions(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    as_of = datetime(2026, 5, 20, 18, 0, tzinfo=timezone.utc)
    _write_triage_assumptions(programs_root)

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    rendered = triage.render_triage_report(artifacts.report)

    assert any("Kusto team ships schema by Q3." in line for line in artifacts.report.assumptions)
    assert any("overdue for validation" in line.lower() for line in artifacts.report.needs_attention)
    assert "ASSUMPTIONS:" in rendered


def test_generate_triage_report_flags_nudged_item_without_response(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    as_of = datetime(2026, 5, 20, 18, 0, tzinfo=timezone.utc)
    (programs_root / "acme" / "nudge_state.json").write_text(
        json.dumps({"1001": datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc).isoformat()}, indent=2),
        encoding="utf-8",
    )

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    assert any(
        "WI:1001 has no ADO response 48h after the last nudge" in line
        for line in artifacts.report.needs_attention
    )


def test_generate_triage_report_skips_nudged_item_after_response(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    as_of = datetime(2026, 5, 20, 18, 0, tzinfo=timezone.utc)
    (programs_root / "acme" / "nudge_state.json").write_text(
        json.dumps({"1001": datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc).isoformat()}, indent=2),
        encoding="utf-8",
    )

    items = list(_sample_items(as_of))
    items[0].comments.append(
        Comment(
            work_item_id=1001,
            comment_id=3,
            created_by="Priya Mehta",
            created_by_email="priya@example.com",
            created_date=datetime(2026, 5, 18, 9, 0, tzinfo=timezone.utc),
            text="Updated rollout status and next steps.",
        )
    )

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        work_item_loader=lambda bundle, timestamp: (tuple(items), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    assert not any(
        "WI:1001 has no ADO response 48h after the last nudge" in line
        for line in artifacts.report.needs_attention
    )


def test_generate_triage_report_surfaces_milestone_health(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    as_of = datetime(2026, 5, 20, 18, 0, tzinfo=timezone.utc)
    completed_item = replace(
        _sample_items(as_of)[0],
        id=1003,
        title="Pilot validation complete",
        state="Resolved",
        target_date=date(2026, 5, 12),
        risk_level=RiskLevel.LOW,
    )
    items = _sample_items(as_of) + (completed_item,)
    milestone_path = programs_root / "acme" / "milestones.yaml"
    milestone_path.write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "milestones:",
                "  - id: m3-code-complete",
                "    name: M3 - Code Complete",
                "    target_date: 2026-05-18",
                "    owner_alias: maintainer",
                "    status: on_track",
                "    exit_criteria:",
                "      - Code complete",
                "      - Validation complete",
                "    linked_workstream_ids:",
                "      - acme",
                "    linked_work_item_ids:",
                "      - 1001",
                "  - id: m4-pilot-rollout-validation",
                "    name: M4 - Pilot Rollout Validation",
                "    target_date: 2026-05-17",
                "    owner_alias: maintainer",
                "    status: on_track",
                "    exit_criteria:",
                "      - Pilot rollout validated",
                "    linked_workstream_ids:",
                "      - acme",
                "    linked_work_item_ids:",
                "      - 1003",
            )
        ),
        encoding="utf-8",
    )
    archive_store.write_confirmed_issue(
        edition="acme_weekly",
        issue_number=77,
        snapshot=Snapshot(
            issue_number=77,
            generated_at=datetime(2026, 5, 16, 18, 0, tzinfo=timezone.utc),
            ado_data_as_of=datetime(2026, 5, 16, 18, 0, tzinfo=timezone.utc),
            edition_type=EditionType.DETAILED,
            items=(),
            scorecards=(),
        ),
        html_body="<html><body>Issue 077</body></html>",
        markdown_body="# Issue 077",
        manifest=RunManifest(
            manifest_id="manifest-77",
            issue_number=77,
            edition="acme_weekly",
            started_at=datetime(2026, 5, 16, 18, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 5, 16, 18, 0, tzinfo=timezone.utc),
            config_hash="config",
            snapshot_hash="snapshot",
            html_hash="html",
            md_hash="md",
            ado_calls=1,
            ai_calls=0,
            ai_cost_usd=0.0,
            freshness_summary={"blocks": 0, "warns": 0, "infos": 0},
            qg_results={"QG-4": True, "QG-5": True, "QG-6": True, "QG-8": True},
            git_sha=None,
            metadata={
                "milestone_assessments": [
                    {
                        "milestone_id": "m3-code-complete",
                        "target_date": "2026-05-15",
                    },
                    {
                        "milestone_id": "m4-pilot-rollout-validation",
                        "completion_date": "2026-05-16",
                    },
                ]
            },
        ),
        archive_root=archive_root,
    )
    backfill_trajectory_points(
        "acme",
        1003,
        (
            TrajectoryPoint(
                date=date(2026, 5, 18),
                state="Resolved",
                assigned_to="owner@example.com",
                target_date=date(2026, 5, 12),
                risk_level=RiskLevel.LOW,
                area_path="One\\Adventure\\Acme\\Deployment",
            ),
        ),
        programs_root=programs_root,
    )

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        work_item_loader=lambda bundle, timestamp: (items, 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    rendered = triage.render_triage_report(artifacts.report)

    assert artifacts.report.milestones
    assert any("milestone at risk or missed" in line for line in artifacts.report.needs_attention)
    assert "MILESTONE HEALTH:" in rendered
    assert "M3 - Code Complete" in rendered
    assert "Tracking 2026-06-01 (14 days late vs target)" in rendered
    assert "target history 2026-05-15 -> 2026-05-18" in rendered
    assert "M4 - Pilot Rollout Validation" in rendered
    assert "Completed 2026-05-18 (1 day late vs target)" in rendered
    assert "completion history 2026-05-16 -> 2026-05-18" in rendered


def test_triage_cli_prints_readiness_summary(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(triage, "REPORTS_ROOT", reports_root)
    monkeypatch.setattr(triage, "ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr(triage.report_command_helpers, "_load_live_work_items", lambda bundle, timestamp: (_sample_items(timestamp), 0))
    monkeypatch.setattr(triage.vitality_command_helpers, "_load_vitality_items", lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0))

    append_signal(
        Signal(
            id="pending-signal",
            timestamp=as_of,
            source="workiq/email",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:1002",),
            text="Needs review",
            raw_ref=None,
            confidence=Confidence.MEDIUM,
            metadata=None,
            thread_id=None,
        ),
        programs_root=programs_root,
        partition_at=as_of,
    )

    result = runner.invoke(app, ["triage", "--edition", "acme_weekly"])

    assert result.exit_code in {2, 3}
    assert "Triage: acme_weekly" in result.stdout
    assert "Draft readiness:" in result.stdout
    assert "NEEDS ATTENTION:" in result.stdout
    assert "ADO VITALITY:" in result.stdout
    assert "1/2 items updated this week (50%)" in result.stdout


def test_generate_triage_report_hides_vitality_when_surface_disabled(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    _set_v2_program_vitality_surface(programs_root, surface="triage", enabled=False)

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    rendered = triage.render_triage_report(artifacts.report)

    assert artifacts.report.vitality_enabled is False
    assert artifacts.report.vitality_summary is None
    assert "ADO VITALITY:" not in rendered


def test_generate_triage_report_flags_stale_claims(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)

    append_claim_entry(
        ClaimEntry(
            id="claim-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            workstream_id="deployment_readiness",
            text="WI:1001 UD chunking fix expected by May 01",
            entity_refs=("WI:1001",),
            claim_date=date(2026, 4, 28),
            owner_alias="owner",
            due_date=date(2026, 5, 1),
        ),
        programs_root=programs_root,
    )

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    assert len(artifacts.report.stale_claims) == 1
    assert artifacts.report.stale_claims[0].confidence is Confidence.HIGH
    assert any("Stale claim from issue #77" in line and "high confidence" in line for line in artifacts.report.needs_attention)


def test_generate_triage_report_flags_stale_narratives(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    # Clear workspace risk overrides so derived risk (MEDIUM) is used for dimension risk computation
    overrides_file = programs_root / "acme" / "overrides" / "issue_078.yaml"
    if overrides_file.exists():
        overrides_file.unlink()
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    stale_narrative_items = (
        WorkItem(
            id=900001,
            type="Feature",
            title="Deployment velocity telemetry stabilization",
            state="Active",
            assigned_to="Vertex Maintainer",
            assigned_to_email="maintainer@example.com",
            area_path="One\\Adventure\\Acme\\Deployment",
            iteration_path="FY26\\Sprint 20",
            target_date=date(2026, 5, 10),
            risk_level=RiskLevel.MEDIUM,
            tags=["Safety", "RAMPP1"],
            custom_fields={},
            revisions=[
                Revision(
                    work_item_id=900001,
                    rev_number=7,
                    changed_by="Vertex Maintainer",
                    changed_by_email="maintainer@example.com",
                    changed_date=as_of,
                    fields_changed={"State": ("Proposed", "Active")},
                )
            ],
            comments=[],
            fetched_at=as_of,
        ),
    )
    narrative_path = programs_root / "acme" / "narratives" / "issue_078" / "chapter_deployment_readiness.md"
    narrative_path.parent.mkdir(parents=True, exist_ok=True)
    narrative_path.write_text("Deployment narrative remains current.\n", encoding="utf-8")
    stale_timestamp = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc).timestamp()
    os.utime(narrative_path, (stale_timestamp, stale_timestamp))
    backfill_trajectory_points(
        "acme",
        900001,
        (
            TrajectoryPoint(
                date=date(2026, 5, 1),
                state="Active",
                assigned_to="Vertex Maintainer",
                target_date=date(2026, 5, 10),
                risk_level=RiskLevel.MEDIUM,
                area_path="One\\Adventure\\Acme\\Deployment",
            ),
            TrajectoryPoint(
                date=date(2026, 5, 8),
                state="Active",
                assigned_to="Vertex Maintainer",
                target_date=date(2026, 5, 17),
                risk_level=RiskLevel.MEDIUM,
                area_path="One\\Adventure\\Acme\\Deployment",
            ),
        ),
        programs_root=programs_root,
    )

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        work_item_loader=lambda bundle, timestamp: (stale_narrative_items, 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    assert len(artifacts.report.stale_narratives) == 1
    assert artifacts.report.stale_narratives[0].confidence is Confidence.HIGH
    assert any(
        "chapter_deployment_readiness.md last edited May 1, but WI:900001 ETA changed May 8 (high confidence)" in line
        for line in artifacts.report.needs_attention
    )


def test_generate_triage_report_groups_correlated_items_by_work_item(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)

    append_signal(
        Signal(
            id="approved-signal",
            timestamp=as_of,
            source="ado/revision",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:1001",),
            text="Approved deployment update",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
            thread_id=None,
        ),
        programs_root=programs_root,
        partition_at=as_of,
    )
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="approved-signal",
            decision="approved",
            reviewed_at=as_of,
            reviewed_by="system",
            note=None,
        ),
        programs_root=programs_root,
    )
    append_signal(
        Signal(
            id="pending-signal",
            timestamp=as_of,
            source="workiq/email",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:1001",),
            text="Pending firmware confirmation",
            raw_ref=None,
            confidence=Confidence.MEDIUM,
            metadata=None,
            thread_id=None,
        ),
        programs_root=programs_root,
        partition_at=as_of,
    )
    append_claim_entry(
        ClaimEntry(
            id="claim-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            workstream_id="deployment_readiness",
            text="WI:1001 firmware sign-off expected by May 12",
            entity_refs=("WI:1001",),
            claim_date=date(2026, 5, 5),
            owner_alias="owner",
            due_date=date(2026, 5, 12),
        ),
        programs_root=programs_root,
    )
    _write_triage_risks(programs_root)
    _write_triage_actions(programs_root)

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    rendered = triage.render_triage_report(artifacts.report)

    correlated = next(item for item in artifacts.report.correlated_items if item.work_item_id == 1001)
    assert correlated.confidence is Confidence.HIGH
    assert any(detail.startswith("Signal (approved): ado/revision | Approved deployment update") for detail in correlated.details)
    assert any(detail.startswith("Signal (needs review): workiq/email | Pending firmware confirmation") for detail in correlated.details)
    assert any(detail.startswith("Claim: issue #77 | due 2026-05-12 | WI:1001 firmware sign-off expected by May 12") for detail in correlated.details)
    assert any(detail.startswith("Risk: OPEN | score ") for detail in correlated.details)
    assert any(detail.startswith("Action: OPEN | due 2026-05-09 | overdue | Follow up with the firmware team") for detail in correlated.details)
    assert "CORRELATED ITEMS:" in rendered
    assert 'WI:1001 "Covered item" (Active; high confidence)' in rendered
    assert "    - Risk: OPEN | score" in rendered


def test_generate_triage_report_reads_signals_and_trajectories_from_sqlite_backend(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    program_path = programs_root / "acme" / "program.yaml"
    program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(program_document, dict)
    program_document["storage_backend"] = "sqlite"
    program_path.write_text(yaml.safe_dump(program_document, sort_keys=False), encoding="utf-8")
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)

    signal_store = SQLiteSignalStore(programs_root=programs_root)
    trajectory_store = SQLiteTrajectoryStore(programs_root=programs_root)

    approved_signal = Signal(
        id="approved-signal",
        timestamp=as_of,
        source="ado/revision",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WI:1001",),
        text="Approved deployment update",
        raw_ref=None,
        confidence=Confidence.HIGH,
        metadata=None,
        thread_id=None,
    )
    pending_signal = Signal(
        id="pending-signal",
        timestamp=as_of,
        source="workiq/email",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=("WI:1001",),
        text="Pending firmware confirmation",
        raw_ref=None,
        confidence=Confidence.MEDIUM,
        metadata=None,
        thread_id=None,
    )
    signal_store.append(approved_signal)
    signal_store.append(pending_signal)
    signal_store.append_review(
        "acme",
        SignalReviewDecision(
            signal_id="approved-signal",
            decision="approved",
            reviewed_at=as_of,
            reviewed_by="system",
            note=None,
        ),
    )
    append_claim_entry(
        ClaimEntry(
            id="claim-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            workstream_id="deployment_readiness",
            text="WI:1001 firmware sign-off expected by May 12",
            entity_refs=("WI:1001",),
            claim_date=date(2026, 5, 5),
            owner_alias="owner",
            due_date=date(2026, 5, 12),
        ),
        programs_root=programs_root,
    )
    _write_triage_risks(programs_root)
    _write_triage_actions(programs_root)
    trajectory_store.append(
        "acme",
        1001,
        TrajectoryPoint(
            date=date(2026, 5, 9),
            state="Resolved",
            assigned_to="owner@example.com",
            target_date=None,
            risk_level=None,
            area_path="One\\Acme",
        ),
    )

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    correlated = next(item for item in artifacts.report.correlated_items if item.work_item_id == 1001)

    assert correlated.confidence is Confidence.HIGH

    assert any(detail.startswith("Signal (approved): ado/revision | Approved deployment update") for detail in correlated.details)
    assert any(detail.startswith("Signal (needs review): workiq/email | Pending firmware confirmation") for detail in correlated.details)
    assert any("candidate for resolution" in detail for detail in correlated.details)
    assert any("candidate for resolution" in line for line in artifacts.report.actions)
    assert any("candidate for resolution after linked ADO update" in line for line in artifacts.report.needs_attention)


def test_generate_triage_report_orders_correlated_signals_by_source_confidence_and_workiq_relevance(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    _write_people_directory(
        tmp_path / "knowledge",
        """
people:
  - alias: gm
    title: General Manager
  - alias: alex
    title: Senior Software Engineer
""".strip()
        + "\n",
    )
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)

    signals_to_seed = (
        Signal(
            id="approved-workiq-low",
            timestamp=datetime(2026, 5, 10, 17, 30, tzinfo=timezone.utc),
            source="workiq/email",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:1001",),
            text="Alex asked for a status refresh.",
            raw_ref=None,
            confidence=Confidence.MEDIUM,
            metadata={"sender_alias": "alex", "thread_id": "thread-low"},
            thread_id="thread-low",
        ),
        Signal(
            id="approved-workiq-high-older",
            timestamp=datetime(2026, 5, 10, 16, 30, tzinfo=timezone.utc),
            source="workiq/email",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:1001",),
            text="GM asked for blocker confirmation.",
            raw_ref=None,
            confidence=Confidence.MEDIUM,
            metadata={"sender_alias": "gm", "thread_id": "thread-high"},
            thread_id="thread-high",
        ),
        Signal(
            id="approved-ado",
            timestamp=datetime(2026, 5, 10, 15, 0, tzinfo=timezone.utc),
            source="ado/revision",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:1001",),
            text="ADO shows the target-date slip.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
            thread_id=None,
        ),
        Signal(
            id="approved-workiq-high-newer",
            timestamp=datetime(2026, 5, 10, 16, 45, tzinfo=timezone.utc),
            source="workiq/email",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:1001",),
            text="GM reiterated the blocker in the same thread.",
            raw_ref=None,
            confidence=Confidence.MEDIUM,
            metadata={"sender_alias": "gm", "thread_id": "thread-high"},
            thread_id="thread-high",
        ),
    )
    for signal in signals_to_seed:
        append_signal(signal, programs_root=programs_root, partition_at=as_of)
        append_review_decision(
            "acme",
            SignalReviewDecision(
                signal_id=signal.id,
                decision="approved",
                reviewed_at=as_of,
                reviewed_by="system",
                note=None,
            ),
            programs_root=programs_root,
        )

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    correlated = next(item for item in artifacts.report.correlated_items if item.work_item_id == 1001)
    signal_details = tuple(detail for detail in correlated.details if detail.startswith("Signal (approved):"))

    assert correlated.confidence is Confidence.HIGH

    assert signal_details == (
        "Signal (approved): ado/revision | ADO shows the target-date slip.",
        "Signal (approved): workiq/email | GM reiterated the blocker in the same thread.",
        "Signal (approved): workiq/email | GM asked for blocker confirmation.",
        "Signal (approved): workiq/email | Alex asked for a status refresh.",
    )


def test_generate_triage_report_prioritizes_top_level_unreviewed_signals_by_program_source_confidence_order(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    program_path = programs_root / "acme" / "program.yaml"
    program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(program_document, dict)
    program_document["source_confidence_order"] = ["workiq", "ado"]
    program_path.write_text(yaml.safe_dump(program_document, sort_keys=False), encoding="utf-8")
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)

    append_signal(
        Signal(
            id="pending-ado",
            timestamp=datetime(2026, 5, 10, 17, 0, tzinfo=timezone.utc),
            source="ado/revision",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:1001",),
            text="ADO shows the target-date slip.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata=None,
            thread_id=None,
        ),
        programs_root=programs_root,
        partition_at=as_of,
    )
    append_signal(
        Signal(
            id="pending-workiq",
            timestamp=datetime(2026, 5, 10, 17, 30, tzinfo=timezone.utc),
            source="workiq/email",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:1001",),
            text="GM asked for blocker confirmation.",
            raw_ref=None,
            confidence=Confidence.MEDIUM,
            metadata={"sender_alias": "gm", "thread_id": "thread-high"},
            thread_id="thread-high",
        ),
        programs_root=programs_root,
        partition_at=as_of,
    )

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    priority_lines = tuple(line for line in artifacts.report.needs_attention if line.startswith("Priority review:"))

    assert priority_lines == (
        "Priority review: workiq/email | WI:1001 | medium confidence | GM asked for blocker confirmation.",
        "Priority review: ado/revision | WI:1001 | high confidence | ADO shows the target-date slip.",
    )


def test_generate_triage_report_surfaces_active_issues(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, programs_root = _seed_v2_triage_layout(repo_root, tmp_path)
    as_of = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)

    append_signal(
        Signal(
            id="icm-signal",
            timestamp=as_of,
            source="icm/incident",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("ICM:12345",),
            text="IcM 12345: Sev2 incident active for rollout readiness.",
            raw_ref="icm:12345",
            confidence=Confidence.HIGH,
            metadata={"severity": 2},
            thread_id=None,
        ),
        programs_root=programs_root,
        partition_at=as_of,
    )
    append_decision_ask(
        DecisionAsk(
            id="ask-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=78,
            text="Need LT decision on rollout gate.",
            entity_refs=("WI:1101",),
            ask_date=date(2026, 5, 10),
            owner_alias="lt",
        ),
        programs_root=programs_root,
    )
    append_action(
        "acme",
        ActionItem(
            id="action-1",
            program_id="acme",
            text="Confirm blocked dependency mitigation",
            owner_alias="owner",
            due_date=date(2026, 5, 9),
            status=ActionStatus.OPEN,
            source_signal_id=None,
            source_type=ActionSourceType.MANUAL,
            linked_work_item_ids=(1101,),
            linked_claim_id=None,
            linked_risk_id="risk-1",
            workstream_id="deployment_readiness",
            created_at=datetime(2026, 5, 8, 9, 0, tzinfo=timezone.utc),
            resolved_at=None,
            resolution_note=None,
        ),
        programs_root=programs_root,
    )

    artifacts = triage.generate_triage_report(
        "acme_weekly",
        issue_number=78,
        as_of=as_of,
        reports_root=reports_root,
        archive_root=archive_root,
        work_item_loader=lambda bundle, timestamp: (_sample_issue_items(timestamp), 0),
        vitality_loader=lambda program, workstreams, timestamp: (_sample_vitality_items(timestamp), 0),
    )

    rendered = triage.render_triage_report(artifacts.report)

    assert any(entry.source_type == "ado_blocked" for entry in artifacts.report.active_issues)
    assert any(entry.source_type == "decision_ask" for entry in artifacts.report.active_issues)
    assert any(entry.source_type == "overdue_action" for entry in artifacts.report.active_issues)
    assert any(entry.source_type == "icm_incident" for entry in artifacts.report.active_issues)
    assert "ACTIVE ISSUES:" in rendered
    assert "BLOCK | ado blocked | high confidence | WI:1101 \"Blocked rollout item\" blocked in ADO (Blocked)" in rendered
    assert "ado https://dev.azure.com/your-org/One/_workitems/edit/1101" in rendered
    assert "WARN | decision ask | high confidence | Issue #078 ask: Need LT decision on rollout gate. (owner lt)" in rendered


def _sample_issue_items(as_of: datetime) -> tuple[WorkItem, ...]:
    return (
        WorkItem(
            id=1101,
            type="Feature",
            title="Blocked rollout item",
            state="Blocked",
            assigned_to="owner@example.com",
            assigned_to_email="owner@example.com",
            area_path="One\\Adventure\\Acme\\Deployment",
            iteration_path="Sprint 1",
            target_date=date(2026, 6, 1),
            risk_level=RiskLevel.HIGH,
            tags=["blocked"],
            custom_fields={"changed_date": as_of.isoformat()},
            revisions=[],
            comments=[],
            fetched_at=as_of,
        ),
    )


def _sample_chronic_high_items(as_of: datetime) -> tuple[WorkItem, ...]:
    return (
        WorkItem(
            id=1201,
            type="Feature",
            title="Chronic high rollout risk",
            state="Active",
            assigned_to="owner@example.com",
            assigned_to_email="owner@example.com",
            area_path="One\\Adventure\\Acme\\Deployment",
            iteration_path="Sprint 1",
            target_date=date(2026, 6, 1),
            risk_level=RiskLevel.HIGH,
            tags=[],
            custom_fields={"changed_date": as_of.isoformat()},
            revisions=[],
            comments=[],
            fetched_at=as_of,
        ),
    )


def _seed_v2_triage_layout(repo_root: Path, tmp_path: Path) -> tuple[Path, Path, Path]:
    src = get_source_root(repo_root)
    if not (src / "editions").exists() or not (src / "programs" / "acme").exists():
        import pytest
        pytest.skip("Requires local editions and programs/acme data")
    reports_root = tmp_path / "reports"
    archive_root = tmp_path / "archive"
    editions_root = tmp_path / "editions"
    programs_root = tmp_path / "programs"
    # Read from local C: cache (already slim — no trajectories/archive/journal).
    # C: -> C: copytree is fast; avoids repeated 72 MB Q: drive copies per test.
    shutil.copytree(src / "reports" / "schemas", reports_root / "schemas")
    shutil.copytree(src / "editions", editions_root)
    shutil.copytree(src / "programs" / "acme", programs_root / "acme")
    journal_dir = programs_root / "acme" / "journal"
    if journal_dir.exists():
        shutil.rmtree(journal_dir)
    journal_dir.mkdir(parents=True)
    _normalize_program_org(programs_root / "acme")
    return reports_root, archive_root, programs_root


def _write_people_directory(knowledge_root: Path, content: str) -> None:
    knowledge_root.mkdir(parents=True, exist_ok=True)
    (knowledge_root / "people_directory.yaml").write_text(content, encoding="utf-8")


def _set_v2_program_vitality_surface(programs_root: Path, *, surface: str, enabled: bool) -> None:
    program_path = programs_root / "acme" / "program.yaml"
    document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    vitality_block = document.setdefault("vitality", {})
    assert isinstance(vitality_block, dict)
    surfaces = vitality_block.setdefault("surfaces", {})
    assert isinstance(surfaces, dict)
    surfaces[surface] = enabled
    program_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _set_triage_workstream_accountable(programs_root: Path, *, workstream_id: str, accountable: str) -> None:
    workstreams_path = programs_root / "acme" / "workstreams.yaml"
    document = yaml.safe_load(workstreams_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    workstreams = document.get("workstreams")
    assert isinstance(workstreams, list)
    for entry in workstreams:
        if not isinstance(entry, dict) or entry.get("id") != workstream_id:
            continue
        entry["raci"] = {
            "accountable": accountable,
            "responsible": ["owner"],
            "consulted": [],
            "informed": [],
        }
        break
    workstreams_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _clear_triage_workstream_accountable(programs_root: Path, *, workstream_id: str) -> None:
    workstreams_path = programs_root / "acme" / "workstreams.yaml"
    document = yaml.safe_load(workstreams_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    workstreams = document.get("workstreams")
    assert isinstance(workstreams, list)
    for entry in workstreams:
        if not isinstance(entry, dict) or entry.get("id") != workstream_id:
            continue
        raci = entry.get("raci")
        if isinstance(raci, dict):
            raci.pop("accountable", None)
        break
    workstreams_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _seed_triage_chronic_high_history(programs_root: Path, *, edition_name: str, dimension_name: str) -> None:
    edition_root = programs_root / "acme" / "archive" / edition_name
    edition_root.mkdir(parents=True, exist_ok=True)
    (edition_root / "scorecards.json").write_text(
        json.dumps(
            {
                "entries": [
                    {"issue_number": 75, "dimension": dimension_name, "risk": "high"},
                    {"issue_number": 76, "dimension": dimension_name, "risk": "high"},
                    {"issue_number": 77, "dimension": dimension_name, "risk": "high"},
                ]
            }
        ),
        encoding="utf-8",
    )


def _seed_triage_high_override(programs_root: Path, *, issue_number: int, dimension_name: str) -> None:
    overrides_dir = programs_root / "acme" / "overrides"
    overrides_dir.mkdir(parents=True, exist_ok=True)
    (overrides_dir / f"issue_{issue_number:03d}.yaml").write_text(
        yaml.safe_dump(
            {
                "issue_number": issue_number,
                "scorecards": {
                    "Acme Adventure/XIO 100% Ramp Readiness": {
                        dimension_name: {
                            "risk": "high",
                        }
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _seed_triage_low_override(programs_root: Path, *, issue_number: int, dimension_name: str) -> None:
    overrides_dir = programs_root / "acme" / "overrides"
    overrides_dir.mkdir(parents=True, exist_ok=True)
    (overrides_dir / f"issue_{issue_number:03d}.yaml").write_text(
        yaml.safe_dump(
            {
                "issue_number": issue_number,
                "scorecards": {
                    "Acme Adventure/XIO 100% Ramp Readiness": {
                        dimension_name: {
                            "risk": "low",
                        }
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_triage_armada_high_dependency(programs_root: Path) -> None:
    (programs_root / "acme" / "dependencies.yaml").write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "dependencies:",
                "  - id: acme-deployment-to-fabrikam-buildouts",
                "    from_workstream_id: acme",
                "    to_workstream_id: fabrikam:buildouts",
                "    resolution_path: cross_org_compute_pf",
                "    dependency_type: blocks",
                "    risk_if_broken: Fabrikam buildouts can block the Acme deployment review.",
                "    status: active",
                "    owner_alias: acme-owner",
            )
        ),
        encoding="utf-8",
    )

    armada_dir = programs_root / "fabrikam"
    armada_dir.mkdir(parents=True, exist_ok=True)
    (armada_dir / "program.yaml").write_text(
        "\n".join(
            (
                'schema_version: "2.0"',
                "id: fabrikam",
                "name: Fabrikam",
            )
        ),
        encoding="utf-8",
    )
    archive_store.write_confirmed_issue(
        edition="fabrikam_weekly",
        issue_number=12,
        snapshot=Snapshot(
            issue_number=12,
            generated_at=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
            ado_data_as_of=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
            edition_type=EditionType.DETAILED,
            items=(),
            scorecards=(),
        ),
        html_body="<html><body>Fabrikam Issue 012</body></html>",
        markdown_body="# Fabrikam Issue 012",
        manifest=RunManifest(
            manifest_id="fabrikam-manifest-12",
            issue_number=12,
            edition="fabrikam_weekly",
            started_at=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
            ended_at=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
            config_hash="config",
            snapshot_hash="snapshot",
            html_hash="html",
            md_hash="md",
            ado_calls=1,
            ai_calls=0,
            ai_cost_usd=0.0,
            freshness_summary={"blocks": 0, "warns": 0, "infos": 0},
            qg_results={"QG-4": True, "QG-5": True, "QG-6": True, "QG-8": True},
            git_sha=None,
        ),
        archive_root=armada_dir / "archive",
    )
    ((armada_dir / "archive" / "fabrikam_weekly" / "scorecards.json")).write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "issue_number": 12,
                        "scorecard_name": "Fabrikam Cross-Team Readiness",
                        "dimension": "Buildouts",
                        "risk": "high",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _sample_items(as_of: datetime) -> tuple[WorkItem, ...]:
    return (
        WorkItem(
            id=1001,
            type="Feature",
            title="Covered item",
            state="Active",
            assigned_to="owner@example.com",
            assigned_to_email="owner@example.com",
            area_path="One\\Adventure\\Acme\\Deployment",
            iteration_path="Sprint 1",
            target_date=date(2026, 6, 1),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={"changed_date": "2026-05-01T00:00:00+00:00"},
            revisions=[],
            comments=[],
            fetched_at=as_of,
        ),
        WorkItem(
            id=1002,
            type="Feature",
            title="Uncovered item",
            state="Active",
            assigned_to="owner@example.com",
            assigned_to_email="owner@example.com",
            area_path="One\\Adventure\\Acme\\Deployment",
            iteration_path="Sprint 1",
            target_date=date(2026, 6, 10),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={"changed_date": "2026-05-01T00:00:00+00:00"},
            revisions=[],
            comments=[],
            fetched_at=as_of,
        ),
    )


def _sample_vitality_items(as_of: datetime) -> tuple[WorkItem, ...]:
    return (
        WorkItem(
            id=2001,
            type="Feature",
            title="Fresh item",
            state="Active",
            assigned_to="owner@example.com",
            assigned_to_email="owner@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="Sprint 1",
            target_date=date(2026, 6, 1),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={"changed_date": (as_of - timedelta(days=2)).isoformat(), "description": "Follow up with owner by 2026-05-20 on the current blocker."},
            revisions=[],
            comments=[],
            fetched_at=as_of,
        ),
        WorkItem(
            id=2002,
            type="Feature",
            title="Stale item",
            state="Active",
            assigned_to="owner@example.com",
            assigned_to_email="owner@example.com",
            area_path="One\\Adventure\\Acme",
            iteration_path="Sprint 1",
            target_date=date(2026, 6, 10),
            risk_level=RiskLevel.MEDIUM,
            tags=[],
            custom_fields={"changed_date": (as_of - timedelta(days=18)).isoformat(), "description": "short"},
            revisions=[],
            comments=[],
            fetched_at=as_of,
        ),
    )


def _write_triage_risks(programs_root: Path) -> None:
    (programs_root / "acme" / "risk_register.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "risks": [
                    {
                        "id": "acme-risk-1",
                        "program_id": "acme",
                        "title": "Firmware sign-off remains open",
                        "description": "Firmware sign-off is the last material gate for pilot readiness.",
                        "probability": "likely",
                        "impact": "high",
                        "category": "schedule",
                        "owner_alias": "owner",
                        "mitigation_plan": "Escalate the sign-off review daily.",
                        "mitigation_due_date": "2026-05-20",
                        "linked_workstream_ids": ["deployment_readiness"],
                        "linked_work_item_ids": [1001],
                        "linked_milestone_ids": [],
                        "linked_claim_ids": [],
                        "linked_action_ids": [],
                        "status": "open",
                        "identified_date": "2026-05-01",
                        "identified_in_vertex_issue": 77,
                        "last_reviewed_date": "2026-05-08",
                        "entity_refs": ["WI:1001"],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_triage_actions(programs_root: Path) -> None:
    append_action(
        "acme",
        ActionItem(
            id="acme-action-1",
            program_id="acme",
            text="Follow up with the firmware team",
            owner_alias="owner",
            due_date=date(2026, 5, 9),
            status=ActionStatus.OPEN,
            source_signal_id="signal-1",
            source_type=ActionSourceType.SIGNAL,
            linked_work_item_ids=(1001,),
            linked_claim_id=None,
            linked_risk_id=None,
            workstream_id="deployment_readiness",
            created_at=datetime(2026, 5, 8, 9, 0, tzinfo=timezone.utc),
            resolved_at=None,
            resolution_note=None,
        ),
        programs_root=programs_root,
    )


def _write_triage_decisions(programs_root: Path) -> None:
    decision_path = programs_root / "acme" / "decisions.yaml"
    decision_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "decisions": [
                    {
                        "id": "decision-1",
                        "program_id": "acme",
                        "title": "Choose rollout path",
                        "context": "Two rollout options remain.",
                        "decision": "Proceed with the guarded rollout.",
                        "rationale": "It minimizes blast radius.",
                        "alternatives_considered": ["pause", "full rollout"],
                        "decided_by": "owner",
                        "decision_date": "2026-05-01",
                        "status": "proposed",
                        "superseded_by": None,
                        "linked_claim_id": None,
                        "linked_risk_id": None,
                        "linked_action_ids": [],
                        "workstream_id": "deployment_readiness",
                        "entity_refs": ["WI:1001"],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_triage_assumptions(programs_root: Path) -> None:
    assumptions_path = programs_root / "acme" / "assumptions.yaml"
    assumptions_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "assumptions": [
                    {
                        "id": "assumption-1",
                        "program_id": "acme",
                        "text": "Kusto team ships schema by Q3.",
                        "validation_method": "Review the schema rollout notes.",
                        "validation_due": "2026-05-10",
                        "status": "unvalidated",
                        "linked_risk_id": None,
                        "linked_milestone_id": None,
                        "owner_alias": "owner",
                        "identified_date": "2026-05-01",
                        "entity_refs": ["WI:1001"],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_triage_dependency_proposals(programs_root: Path) -> None:
    proposals_path = programs_root / "acme" / "_feedback" / "dependency_proposals.yaml"
    proposals_path.parent.mkdir(parents=True, exist_ok=True)
    proposals_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "updated_at": "2026-05-10T18:00:00+00:00",
                "proposals": [
                    {
                        "id": "dep-proposal-1",
                        "program_id": "acme",
                        "from_workstream_id": "deployment_readiness",
                        "to_workstream_id": "platform_readiness",
                        "from_item_id": 1001,
                        "to_item_id": 1002,
                        "from_item_title": "Covered item",
                        "to_item_title": "Uncovered item",
                        "suggested_dependency_type": "shares_resource",
                        "rationale": "Repeated blocked-by language suggests an undeclared dependency.",
                        "evidence_refs": ["sig-1", "sig-2"],
                        "detection_method": "comment_language",
                        "occurrence_count": 2,
                        "first_seen_at": "2026-05-08T18:00:00+00:00",
                        "last_seen_at": "2026-05-10T18:00:00+00:00",
                        "confidence": "medium",
                        "status": "proposed",
                    },
                    {
                        "id": "dep-proposal-accepted",
                        "program_id": "acme",
                        "from_workstream_id": "deployment_readiness",
                        "to_workstream_id": "platform_readiness",
                        "from_item_id": 1001,
                        "to_item_id": 1002,
                        "from_item_title": "Covered item",
                        "to_item_title": "Uncovered item",
                        "suggested_dependency_type": "shares_resource",
                        "rationale": "Already promoted.",
                        "evidence_refs": ["sig-3"],
                        "detection_method": "co_mention",
                        "occurrence_count": 3,
                        "first_seen_at": "2026-05-01T18:00:00+00:00",
                        "last_seen_at": "2026-05-03T18:00:00+00:00",
                        "confidence": "medium",
                        "status": "accepted",
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

