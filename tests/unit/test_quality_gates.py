from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from src.core import quality_gates as quality_gates_module
from src.core.quality_gates import current_state as current_state_module
from src.core.quality_gates import source_health as source_health_module  # D-09: QG-SG-01 moved here
from src.core.action_tracker import append_action
from src.core.claim_tracker import append_claim_entry
from src.core.continuation_contract import ContinuationContract, ContinuationContractEvidenceQuality, ContinuationContractNarrativeSeeding
from src.core.continuation_contract import ContinuationContractOverridesSeeding, ContinuationContractScorecardComposition, ContinuationContractSectionRoster
from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope, read_events, write_event
from src.core.ledger.program_views import project_program_events
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus, ClaimEntry, Dependency, DependencyScheduleStatus, DependencyStatus
from src.core.models_v2 import DependencyType, Milestone, MilestoneStatus, TrajectoryPoint
from src.core.config_loader import NarrativeProgramContext, ProgramWorkstream
from src.core.ledger.candidate_store import CandidateEvent, append_candidate
from src.core.ledger.source_refs import LTDeckRef, OperatorAssertionRef
from src.core.models import AttributionTier, Confidence, DeltaKind, DeltaSet, DimensionRisk, EvidencePacket, FreshnessItem, FreshnessReport, ItemDelta, ReviewSection, ReviewState, ReviewStatus, RiskLevel, RunManifest, WorkItem
from src.core.models_v2 import RiskCategory, RiskEntry, RiskImpact, RiskProbability, RiskStatus, Scorecard, ScorecardDimension, Signal, Workstream
from src.core.overrides_store import DimensionOverride, OverridesDocument, ScorecardOverrides
from src.core.program_fact_store import FactPrecedence, FactReviewState, ProgramFactInput, ProgramFactStore
from src.core.projections.snapshot_manager import build_baseline_hardlock_event, write_projection_snapshot
from src.core.quality_gates import combine_gate_reports, evaluate_bridge_gates, evaluate_continuity_gates, evaluate_context_integrity_gates, evaluate_phase_1a_gates, evaluate_phase_1b_gates
from src.core.quality_gates import evaluate_phase_1c_gates, evaluate_readiness_gates, evaluate_source_health_gates
from src.core.readiness_engine import ReadinessConfig, ReadinessDimensionConfig, ReadinessFetchLoaders, ReadinessPassCondition, ReadinessSourceConfig, build_readiness_snapshot, write_readiness_snapshot
from src.core.risk_register_engine import save_risk_register
from src.core.sqlite_stores import SQLiteTrajectoryStore
from src.core.gather_state_store import write_gather_state
from tests.support.slice_contract_fixtures import build_test_ado_source_contract, build_test_slice_contract


EDITION_NAME = "acme_weekly"


def _dimension_risk(name: str, risk: RiskLevel) -> DimensionRisk:
    return DimensionRisk(
        name=name,
        risk=risk,
        summary=f"{name} summary.",
        evidence=EvidencePacket(
            work_item_id=1,
            revisions=(),
            comments=(),
            enrichments=(),
            confidence=Confidence.MEDIUM,
            tier=AttributionTier.TIER2,
            summary_for_reviewer="evidence",
        ),
    )


def _manifest(snapshot_hash: str) -> RunManifest:
    return RunManifest(
        manifest_id="12345678-1234-5678-1234-567812345678",
        issue_number=78,
        edition=EDITION_NAME,
        started_at=datetime(2026, 5, 5, 8, 59, tzinfo=timezone.utc),
        ended_at=datetime(2026, 5, 5, 9, 1, tzinfo=timezone.utc),
        config_hash="sha256:config",
        snapshot_hash=snapshot_hash,
        html_hash="sha256:html",
        md_hash="sha256:md",
        ado_calls=3,
        ai_calls=0,
        ai_cost_usd=0.0,
        freshness_summary={"blocks": 0, "warns": 0, "infos": 0},
        qg_results={},
        git_sha="abcdef0",
    )


def _work_item(
    work_item_id: int,
    *,
    state: str = "Active",
    target_date: date | None = None,
    risk_level: RiskLevel = RiskLevel.MEDIUM,
    area_path: str = "Acme\\Velocity",
) -> WorkItem:
    return WorkItem(
        id=work_item_id,
        type="Feature",
        title=f"Item {work_item_id}",
        state=state,
        assigned_to="owner",
        assigned_to_email="owner@example.com",
        area_path=area_path,
        iteration_path="Acme",
        target_date=target_date,
        risk_level=risk_level,
        tags=[],
        custom_fields={},
        revisions=[],
        comments=[],
        fetched_at=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
    )


def _delta(work_item_id: int, kind: DeltaKind) -> DeltaSet:
    evidence = EvidencePacket(
        work_item_id=work_item_id,
        revisions=(),
        comments=(),
        enrichments=(),
        confidence=Confidence.MEDIUM,
        tier=AttributionTier.TIER2,
        summary_for_reviewer="evidence",
    )
    item_delta = ItemDelta(
        work_item_id=work_item_id,
        kind=kind,
        field_changes={},
        old_risk=RiskLevel.MEDIUM,
        new_risk=RiskLevel.HIGH if kind == DeltaKind.RISK_UP else RiskLevel.MEDIUM,
        old_eta=date(2026, 5, 12),
        new_eta=date(2026, 5, 15),
        evidence=evidence,
    )
    return DeltaSet(
        issue_number=78,
        previous_issue_number=77,
        new_items=(),
        closed_items=(),
        risk_changes=(item_delta,) if kind == DeltaKind.RISK_UP else (),
        eta_changes=(item_delta,) if kind == DeltaKind.ETA_CHANGED else (),
        unchanged_count=0,
        owner_changes=(),
    )


def _program_context() -> NarrativeProgramContext:
    return NarrativeProgramContext(
        schema_version="1.0",
        program_name="Acme",
        objective=None,
        mission=None,
        pillars=(),
        glossary={},
        workstreams=(
            ProgramWorkstream(
                name="Deployment Velocity",
                aliases=(),
                area_paths=("Acme\\Velocity",),
                dri_email=None,
                alternate_owner=None,
                description=None,
            ),
        ),
        people=(),
    )


def _resolved_workstreams() -> tuple[Workstream, ...]:
    return (
        Workstream(
            id="velocity",
            name="Deployment Velocity",
            area_paths=("Acme\\Velocity",),
        ),
    )


def _resolved_scorecards() -> tuple[Scorecard, ...]:
    return (
        Scorecard(
            name="Acme Health",
            dimensions=(
                ScorecardDimension(name="Deployment Velocity", workstream_id="velocity"),
            ),
        ),
    )


def _write_archive_index(archive_root: Path, *, previous_issue_number: int) -> None:
    edition_root = archive_root / EDITION_NAME
    edition_root.mkdir(parents=True, exist_ok=True)
    (edition_root / "index.json").write_text(
        json.dumps(
            {
                "edition": EDITION_NAME,
                "issues": [
                    {
                        "issue_number": previous_issue_number,
                        "generated_at": "2026-05-03T12:00:00+00:00",
                        "kind": "confirmed",
                        "snapshot_path": "snapshot.json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _valid_continuity_html(issue_number: int = 77) -> str:
        return f"""
        <html>
            <body>
                <!-- vertex: manifest=12345678-1234-5678-1234-567812345678 -->
                <table data-vertex-block=\"brand-header\"></table>
                <h1>Platform on PF | Issue {issue_number} | May 08, 2026</h1>
                <table data-vertex-block=\"cadence-note\"><tr><td>Detailed edition cadence note.</td></tr></table>
                <table data-vertex-block=\"scorecard-band-primary\"></table>
                <table data-vertex-block=\"scorecard-band-secondary\"></table>
                <table data-vertex-block=\"exec-summary\"><tr><td>Leadership ask: None this week.</td></tr></table>
                <table data-vertex-block=\"jump-to-section\"></table>
                <table data-vertex-block=\"chapter-schie_map_day_gaps\"></table>
            </body>
        </html>
        """


def _continuation_contract(
    *,
    scorecard_additions: tuple[tuple[str, str], ...] = (),
    scorecard_removals: tuple[tuple[str, str], ...] = (),
    removed_by_override: tuple[tuple[str, str], ...] = (),
    section_additions: tuple[str, ...] = (),
    section_removals: tuple[str, ...] = (),
    seeded_files: tuple[str, ...] = (),
    source_hashes: dict[str, str] | None = None,
) -> ContinuationContract:
    return ContinuationContract(
        schema_version="1.0",
        edition=EDITION_NAME,
        issue_number=78,
        prior_trusted_issue=77,
        first_inherited_at=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        last_refreshed_at=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        scorecard_composition=ContinuationContractScorecardComposition(
            frozen_from_issue=77,
            inherited_dimensions=(("Acme Health", "Deployment Velocity"),),
            proposed_additions=scorecard_additions,
            proposed_removals=scorecard_removals,
            removed_by_override=removed_by_override,
        ),
        section_roster=ContinuationContractSectionRoster(
            inherited_sections=("exec_summary.md", "ws_deployment-velocity.md"),
            seeded_from_prior=bool(seeded_files),
            sections_missing_evidence=(),
            added_sections=section_additions,
            removed_sections=section_removals,
        ),
        narrative_seeding=ContinuationContractNarrativeSeeding(
            seeded=bool(seeded_files),
            source_issue=77,
            source_path="archive",
            files_seeded=seeded_files,
            source_hashes=source_hashes or {},
        ),
        overrides_seeding=ContinuationContractOverridesSeeding(
            seeded=True,
            source_issue=77,
            fields_carried=("scorecards",),
            fields_cleared=("top_3_now",),
        ),
        evidence_quality=ContinuationContractEvidenceQuality(
            sections_with_ado_coverage=1,
            sections_with_query_only=0,
            sections_with_connector_only=0,
            sections_manual_only=0,
        ),
    )


def test_evaluate_phase_1a_gates_returns_all_green_results() -> None:
    report = evaluate_phase_1a_gates(
        ban_list_violations=(),
        verbosity_violations=(),
        manifest=_manifest("sha256:snapshot"),
        expected_snapshot_hash="sha256:snapshot",
        dimension_risks=(
            _dimension_risk("Deployment Velocity", RiskLevel.LOW),
            _dimension_risk("Safety", RiskLevel.MEDIUM),
        ),
    )

    assert report.passed is True
    assert report.exit_code == 0
    assert report.qg_results == {"QG-4": True, "QG-5": True, "QG-6": True, "QG-DM-1": True, "QG-DM-4": True, "QG-8": True}


def test_qg_dm_1_skips_when_program_is_not_provided() -> None:
    report = evaluate_phase_1a_gates(
        ban_list_violations=(),
        verbosity_violations=(),
        manifest=_manifest("sha256:snapshot"),
        expected_snapshot_hash="sha256:snapshot",
        dimension_risks=(_dimension_risk("Deployment Velocity", RiskLevel.LOW),),
    )

    assert report.qg_results["QG-DM-1"] is True


def test_qg_dm_1_fails_when_event_log_is_tampered(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    event = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
        source_ref=LTDeckRef(file_path="deck.pptx", deck_date=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc).date(), slide_number=1),
    )
    write_result = write_event(event, programs_root=programs_root)
    tampered_payload = json.loads(write_result.path.read_text(encoding="utf-8").splitlines()[0])
    tampered_payload["payload"]["title"] = "Tampered"
    write_result.path.write_text(json.dumps(tampered_payload) + "\n", encoding="utf-8")

    report = evaluate_phase_1a_gates(
        ban_list_violations=(),
        verbosity_violations=(),
        manifest=_manifest("sha256:snapshot"),
        expected_snapshot_hash="sha256:snapshot",
        dimension_risks=(_dimension_risk("Deployment Velocity", RiskLevel.LOW),),
        program_id="acme",
        edition_name=EDITION_NAME,
        issue_number=78,
        archive_root=tmp_path / "archive",
        programs_root=programs_root,
    )

    assert report.passed is False
    assert report.exit_code == 3
    assert report.qg_results["QG-DM-1"] is False


def test_qg_dm_4_skips_when_previous_confirmed_issue_has_no_ledger_snapshot_manifest(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    _write_archive_index(archive_root, previous_issue_number=77)

    report = evaluate_phase_1a_gates(
        ban_list_violations=(),
        verbosity_violations=(),
        manifest=_manifest("sha256:snapshot"),
        expected_snapshot_hash="sha256:snapshot",
        dimension_risks=(_dimension_risk("Deployment Velocity", RiskLevel.LOW),),
        program_id="acme",
        edition_name=EDITION_NAME,
        issue_number=78,
        archive_root=archive_root,
        programs_root=tmp_path / "programs",
    )

    assert report.passed is True
    assert report.qg_results["QG-DM-4"] is True


def test_qg_dm_4_fails_when_previous_confirmed_issue_manifest_has_no_matching_hardlock(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    archive_root = tmp_path / "archive"
    _write_archive_index(archive_root, previous_issue_number=77)
    recorded_at = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    event = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=recorded_at,
        recorded_at=recorded_at,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
        source_ref=LTDeckRef(file_path="deck.pptx", deck_date=recorded_at.date(), slide_number=1),
    )
    write_event(event, programs_root=programs_root)
    projection_result = project_program_events("acme", programs_root=programs_root)
    write_projection_snapshot(
        "acme",
        77,
        projection_result,
        events=read_events("acme", programs_root=programs_root),
        programs_root=programs_root,
    )

    report = evaluate_phase_1a_gates(
        ban_list_violations=(),
        verbosity_violations=(),
        manifest=_manifest("sha256:snapshot"),
        expected_snapshot_hash="sha256:snapshot",
        dimension_risks=(_dimension_risk("Deployment Velocity", RiskLevel.LOW),),
        program_id="acme",
        edition_name=EDITION_NAME,
        issue_number=78,
        archive_root=archive_root,
        programs_root=programs_root,
    )

    assert report.passed is False
    assert report.exit_code == 3
    assert report.qg_results["QG-DM-4"] is False


def test_qg_dm_4_passes_when_previous_confirmed_issue_has_matching_manifest_hardlock_and_watermark(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    archive_root = tmp_path / "archive"
    _write_archive_index(archive_root, previous_issue_number=77)
    recorded_at = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
    event = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=recorded_at,
        recorded_at=recorded_at,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
        source_ref=LTDeckRef(file_path="deck.pptx", deck_date=recorded_at.date(), slide_number=1),
    )
    write_event(event, programs_root=programs_root)
    projection_result = project_program_events("acme", programs_root=programs_root)
    snapshot_paths = write_projection_snapshot(
        "acme",
        77,
        projection_result,
        events=read_events("acme", programs_root=programs_root),
        programs_root=programs_root,
    )
    hardlock = build_baseline_hardlock_event(
        "acme",
        77,
        snapshot_paths,
        projection_result,
        source_ref=OperatorAssertionRef(asserted_by="test-operator", asserted_at=recorded_at, context="confirm"),
        actor="test-operator",
        recorded_at=recorded_at,
    )
    write_event(hardlock, programs_root=programs_root)

    report = evaluate_phase_1a_gates(
        ban_list_violations=(),
        verbosity_violations=(),
        manifest=_manifest("sha256:snapshot"),
        expected_snapshot_hash="sha256:snapshot",
        dimension_risks=(_dimension_risk("Deployment Velocity", RiskLevel.LOW),),
        program_id="acme",
        edition_name=EDITION_NAME,
        issue_number=78,
        archive_root=archive_root,
        programs_root=programs_root,
    )

    assert report.qg_results["QG-DM-4"] is True


def test_evaluate_continuity_gates_passes_for_valid_html() -> None:
    report = evaluate_continuity_gates(
        html_content=_valid_continuity_html(),
        issue_number=77,
    )

    assert report.passed is True
    assert report.qg_results == {
        "CG-01": True,
        "CG-02": True,
        "CG-03": True,
        "CG-04": True,
        "CG-05": True,
        "CG-06": True,
        "CG-07": True,
        "CG-08": True,
        "CG-09": True,
    }


def test_evaluate_readiness_gates_warns_when_snapshot_is_missing(tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        program_dir = programs_root / "demo"
        program_dir.mkdir(parents=True)
        program_dir.joinpath("program.yaml").write_text(
                """
schema_version: '3.0'
id: demo
readiness:
    gate: true
    snapshot_max_age_days: 7
""".strip(),
                encoding="utf-8",
        )
        program_dir.joinpath("readiness.yaml").write_text(
                """
schema_version: '1.0'
snapshot_max_age_days: 7
dimensions:
    rollback_plan:
        source:
            type: manual_attestation
            attested_by: operator
        pass_condition:
            kind: attested_within_days
            days: 30
""".strip(),
                encoding="utf-8",
        )

        report = evaluate_readiness_gates(program_id="demo", programs_root=programs_root, max_age_days=7)

        assert report.passed is False
        assert report.exit_code == 1
        assert report.qg_results == {"QG-RD4": False}
        assert report.failing_results[0].message.endswith("Run `vertex readiness fetch --program demo`.")


def test_evaluate_readiness_gates_uses_signed_snapshot_results(tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        program_dir = programs_root / "demo"
        program_dir.mkdir(parents=True)
        # Use a recent fetched_at so the snapshot is never stale (9d > 7d max was breaking this test)
        fetched_at = datetime.now(timezone.utc) - timedelta(days=1)
        attested_at_date = fetched_at.date() - timedelta(days=2)
        program_dir.joinpath("program.yaml").write_text(
                """
schema_version: '3.0'
id: demo
readiness:
    gate: true
    snapshot_max_age_days: 7
""".strip(),
                encoding="utf-8",
        )
        program_dir.joinpath("readiness.yaml").write_text(
                f"""
schema_version: '1.0'
snapshot_max_age_days: 7
dimensions:
    rollback_plan:
        source:
            type: manual_attestation
            attested_at: '{attested_at_date.isoformat()}'
            attested_by: operator
        pass_condition:
            kind: attested_within_days
            days: 30
""".strip(),
                encoding="utf-8",
        )
        snapshot = build_readiness_snapshot(
                "demo",
                ReadinessConfig(
                        program_id="demo",
                        snapshot_max_age_days=7,
                        dimensions=(
                                ReadinessDimensionConfig(
                                        id="rollback_plan",
                                        name="Rollback plan",
                                        gate_id="QG-RD4",
                                        source=ReadinessSourceConfig(
                                                type="manual_attestation",
                                                attested_at=attested_at_date,
                                                attested_by="operator",
                                        ),
                                        pass_condition=ReadinessPassCondition(kind="attested_within_days", days=30),
                                ),
                        ),
                ),
                loaders=ReadinessFetchLoaders(),
                fetched_at=fetched_at,
        )
        write_readiness_snapshot("demo", snapshot, programs_root=programs_root)

        report = evaluate_readiness_gates(program_id="demo", programs_root=programs_root, max_age_days=7)

        assert report.passed is True
        assert report.qg_results == {"QG-RD4": True}
        assert report.results[0].message == "Launch readiness gate 'Rollback plan' passed: Rollback plan attestation is 2 day(s) old, last signed by operator."


def test_evaluate_source_health_gates_warns_when_gather_state_is_missing() -> None:
    report = evaluate_source_health_gates(
        program_id="demo",
        edition_name=EDITION_NAME,
        slice_contracts=("placeholder",),
        gather_state=None,
        waivers=(),
        function_name="review",
    )

    assert report.passed is False
    assert report.results[0].gate_id == "QG-SG-01"
    assert report.results[0].exit_code == 1
    assert "no gather state recorded" in report.results[0].message


def test_evaluate_source_health_gates_blocks_forceably_when_required_role_is_unhealthy(monkeypatch) -> None:
    monkeypatch.setattr(
        source_health_module,
        "build_slice_source_health_summary",
        lambda slice_contracts, gather_state, waivers=(), function_name="newsletter": SimpleNamespace(
            function=function_name,
            contract_count=1,
            unhealthy_roles=(
                SimpleNamespace(
                    contract_id="demo.slice",
                    role="telemetry",
                    state="stale",
                    blocks_confirm=True,
                    waiver=None,
                ),
            ),
        ),
    )

    report = evaluate_source_health_gates(
        program_id="demo",
        edition_name=EDITION_NAME,
        slice_contracts=("placeholder",),
        gather_state=object(),
        waivers=(),
        function_name="review",
    )

    assert report.passed is False
    result = report.results[0]
    assert result.gate_id == "QG-SG-01"
    assert result.forceable is True
    assert result.exit_code == 3
    assert result.message.startswith("Review source health gate failed")
    assert "demo.slice:telemetry=stale" in result.message


def test_evaluate_source_health_gates_passes_when_all_required_roles_are_healthy(monkeypatch) -> None:
    monkeypatch.setattr(
        source_health_module,
        "build_slice_source_health_summary",
        lambda slice_contracts, gather_state, waivers=(), function_name="newsletter": SimpleNamespace(
            function=function_name,
            contract_count=2,
            unhealthy_roles=(),
        ),
    )

    report = evaluate_source_health_gates(
        program_id="demo",
        edition_name=EDITION_NAME,
        slice_contracts=("placeholder",),
        gather_state=object(),
        waivers=(),
        function_name="review",
    )

    assert report.passed is True
    result = report.results[0]
    assert result.gate_id == "QG-SG-01"
    assert result.forceable is True
    assert result.message.startswith("Review source health gate passed")
    assert "passed for 2 slice source contract(s)" in result.message


def test_evaluate_source_health_gates_passes_with_active_waivers(monkeypatch) -> None:
    monkeypatch.setattr(
        source_health_module,
        "build_slice_source_health_summary",
        lambda slice_contracts, gather_state, waivers=(), function_name="newsletter": SimpleNamespace(
            function=function_name,
            contract_count=1,
            unhealthy_roles=(
                SimpleNamespace(
                    contract_id="demo.slice",
                    role="telemetry",
                    state="stale",
                    blocks_confirm=False,
                    waiver=SimpleNamespace(owner="owner@example.com", expires=date(2026, 6, 30)),
                ),
            ),
        ),
    )

    report = evaluate_source_health_gates(
        program_id="demo",
        edition_name=EDITION_NAME,
        slice_contracts=("placeholder",),
        gather_state=object(),
        waivers=("placeholder",),
        function_name="review",
    )

    assert report.passed is True
    assert "active waiver" in report.results[0].message


def test_evaluate_source_health_gates_keeps_unbound_roles_non_forceable(monkeypatch) -> None:
    monkeypatch.setattr(
        source_health_module,
        "build_slice_source_health_summary",
        lambda slice_contracts, gather_state, waivers=(), function_name="newsletter": SimpleNamespace(
            function=function_name,
            contract_count=1,
            unhealthy_roles=(
                SimpleNamespace(
                    contract_id="demo.slice",
                    role="system_of_record",
                    state="unbound",
                    blocks_confirm=True,
                    waiver=None,
                ),
            ),
        ),
    )

    report = evaluate_source_health_gates(
        program_id="demo",
        edition_name=EDITION_NAME,
        slice_contracts=("placeholder",),
        gather_state=object(),
        waivers=(),
        function_name="review",
    )

    assert report.passed is False
    result = report.results[0]
    assert result.gate_id == "QG-SG-01"
    assert result.forceable is False
    assert result.message.startswith("Review source health gate failed")
    assert "demo.slice:system_of_record=unbound" in result.message
    assert "Fix the slice/source binding before confirming." in result.message


def test_evaluate_source_health_gates_require_structured_decision_sources_for_deck() -> None:
    from src.core.gather_state_store import GatherState

    report = evaluate_source_health_gates(
        program_id="demo",
        edition_name=EDITION_NAME,
        slice_contracts=(
            build_test_slice_contract(
                contract_id="demo.slice",
                scorecard_name="Demo Scorecard",
                section="demo_section",
                workstream="demo_ws",
                ado=build_test_ado_source_contract(
                    saved_queries=("query-1",),
                    filters=None,
                    explicit_work_item_ids=(),
                    required_fields=("state",),
                ),
                fallback_sources=("lt_deck",),
            ),
        ),
        gather_state=GatherState(
            program_id="demo",
            gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
            scanned_items=0,
            discovered_signals=0,
            new_signals=0,
            pending_review=0,
            trajectory_updates=0,
            auto_reviews_written=0,
            ado_calls=0,
            archived_journal_files=0,
            background_proposals=0,
            query_states={},
            channels={
                "workiq": {
                    "active": True,
                    "signal_count": 8,
                    "expected_min": 8,
                    "meets_expected_min": True,
                }
            },
            m365_discovery={},
        ),
        waivers=(),
        function_name="deck",
    )

    assert report.passed is False
    result = report.results[0]
    assert result.gate_id == "QG-SG-01"
    assert result.forceable is False
    assert "demo.slice:decision=unbound" in result.message


def test_evaluate_continuity_gates_flags_legacy_sections_and_order_violations() -> None:
    html = """
        <html>
            <body>
                <a id="health"></a>
                <p>Program Health</p>
                <a id="top-3"></a>
                <p>DECISIONS &amp; SIGNALS</p>
                <a id="changes"></a>
                <p>WHAT CHANGED</p>
                <table>
                    <tr>
                        <td><a href="#health">Health</a></td>
                        <td><a href="#top-3">Decisions</a></td>
                    </tr>
                </table>
                <table data-vertex-block="brand-header"></table>
                <h1>Platform on PF | Issue 77 | May 08, 2026</h1>
                <table data-vertex-block="exec-summary"></table>
                <table data-vertex-block="scorecard-band-primary"></table>
                <table data-vertex-block="scorecard-band-secondary"></table>
                <table data-vertex-block="cadence-note"></table>
                <table data-vertex-block="jump-to-section"></table>
                <div data-vertex-block="snapshot-ribbon">Compact Snapshot</div>
                <table data-vertex-block="chapter-schie_map_day_gaps"></table>
            </body>
        </html>
        """

    report = evaluate_continuity_gates(html_content=html, issue_number=77)

    assert report.passed is False
    assert report.qg_results["CG-01"] is False
    assert report.qg_results["CG-02"] is False
    assert report.qg_results["CG-03"] is False
    assert report.qg_results["CG-04"] is False
    assert report.qg_results["CG-05"] is False
    assert report.qg_results["CG-06"] is False
    assert report.qg_results["CG-08"] is False


def test_evaluate_continuity_gates_does_not_accept_legacy_khabari_block_markers() -> None:
    report = evaluate_continuity_gates(
        html_content=_valid_continuity_html().replace("data-vertex-block", "data-khabari-block"),
        issue_number=77,
    )

    assert report.passed is False


def test_evaluate_continuity_gates_blocks_visible_tool_name_and_repeated_issue_number() -> None:
    html = _valid_continuity_html().replace(
        "<table data-vertex-block=\"exec-summary\"><tr><td>Leadership ask: None this week.</td></tr></table>",
        "<table data-vertex-block=\"exec-summary\"><tr><td>Vertex manifest 12345678-1234-5678-1234-567812345678. Issue 077 remains blocked.</td></tr></table>",
    )

    report = evaluate_continuity_gates(html_content=html, issue_number=77)

    assert report.passed is False
    assert report.qg_results["CG-07"] is False
    assert report.qg_results["CG-09"] is False
    assert 'Tool attribution found in published HTML ("Vertex")' in report.failing_results[0].message


def test_qg8_blocks_when_any_dimension_is_unknown() -> None:
    report = evaluate_phase_1a_gates(
        ban_list_violations=(),
        verbosity_violations=(),
        manifest=_manifest("sha256:snapshot"),
        expected_snapshot_hash="sha256:snapshot",
        dimension_risks=(
            _dimension_risk("Deployment Velocity", RiskLevel.UNKNOWN),
        ),
    )

    assert report.passed is False
    assert report.exit_code == 3
    assert report.qg_results["QG-8"] is False
    assert report.failing_results[0].message == "Missing risk levels for: Deployment Velocity"


def test_evaluate_bridge_gates_flags_roster_composition_and_unchanged_seeded_narratives() -> None:
    exec_summary_hash = f"sha256:{hashlib.sha256('Prior exec summary.'.encode('utf-8')).hexdigest()}"
    report = evaluate_bridge_gates(
        continuation_contract=_continuation_contract(
            scorecard_additions=(("Acme Health", "Safety"),),
            scorecard_removals=(("Acme Health", "Deployment Velocity"),),
            section_additions=("new-section",),
            section_removals=("deployment-velocity",),
            seeded_files=("exec_summary.md",),
            source_hashes={"exec_summary.md": exec_summary_hash},
        ),
        narratives={"exec_summary.md": "<!-- SEEDED from Issue 077 -->\n\nPrior exec summary."},
        review_status=ReviewStatus(
            issue_number=78,
            sections=(
                ReviewSection(section_id="exec_summary", state=ReviewState.PENDING, reviewer=None, note=None, updated_at=None),
            ),
        ),
    )

    assert report.passed is False
    assert report.exit_code == 2
    assert report.qg_results == {"QG-B1": False, "QG-B2": False, "QG-B3": False}
    assert all(result.forceable for result in report.failing_results)


def test_evaluate_bridge_gates_honors_removed_dimensions_and_review_approval() -> None:
    exec_summary_hash = f"sha256:{hashlib.sha256('Prior exec summary.'.encode('utf-8')).hexdigest()}"
    report = evaluate_bridge_gates(
        continuation_contract=_continuation_contract(
            removed_by_override=(("Acme Health", "Deployment Velocity"),),
            section_removals=("acme-health-deployment-velocity",),
            seeded_files=("exec_summary.md",),
            source_hashes={"exec_summary.md": exec_summary_hash},
        ),
        narratives={"exec_summary.md": "<!-- SEEDED from Issue 077 -->\n\nPrior exec summary."},
        review_status=ReviewStatus(
            issue_number=78,
            sections=(
                ReviewSection(
                    section_id="exec_summary",
                    state=ReviewState.APPROVED,
                    reviewer="operator",
                    note=None,
                    updated_at=datetime(2026, 5, 10, 13, 0, tzinfo=timezone.utc),
                ),
            ),
        ),
    )

    assert report.passed is True
    assert report.qg_results == {"QG-B1": True, "QG-B2": True, "QG-B3": True}


def test_evaluate_bridge_gates_downgrades_structural_drift_after_graduation() -> None:
    report = evaluate_bridge_gates(
        continuation_contract=_continuation_contract(
            scorecard_additions=(("Acme Health", "Safety"),),
            section_additions=("new-section",),
        ),
        narratives={},
        review_status=ReviewStatus(issue_number=78, sections=()),
        bridge_graduated=True,
    )

    assert report.passed is False
    assert report.exit_code == 1
    assert report.qg_results == {"QG-B1": False, "QG-B2": False}
    assert report.failing_results[0].forceable is False
    assert report.failing_results[1].forceable is False
    assert "advisory after graduation" in report.failing_results[0].message
    assert "advisory after graduation" in report.failing_results[1].message


def test_qg6_uses_exit_code_three_for_manifest_hash_mismatch() -> None:
    report = evaluate_phase_1a_gates(
        ban_list_violations=(),
        verbosity_violations={"exec_summary": ()},
        manifest=_manifest("sha256:old"),
        expected_snapshot_hash="sha256:new",
        dimension_risks=(
            _dimension_risk("Deployment Velocity", RiskLevel.MEDIUM),
        ),
    )

    assert report.passed is False
    assert report.exit_code == 3
    assert report.qg_results["QG-6"] is False


def test_qg4_and_qg5_report_violation_counts() -> None:
    report = evaluate_phase_1a_gates(
        ban_list_violations=("due to", "because of"),
        verbosity_violations={"exec_summary": ("too long",), "workstream": ("too many sentences",)},
        manifest=_manifest("sha256:snapshot"),
        expected_snapshot_hash="sha256:snapshot",
        dimension_risks=(
            _dimension_risk("Deployment Velocity", RiskLevel.MEDIUM),
        ),
    )

    assert report.passed is False
    assert report.qg_results["QG-4"] is False
    assert report.qg_results["QG-5"] is False
    assert report.failing_results[0].message == "Ban-list validation failed with 2 violation(s)."
    assert report.failing_results[1].message == "Verbosity validation failed with 2 violation(s)."


def test_qg1_blocks_when_freshness_has_blocking_items() -> None:
    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=2, warns=1, infos=0)
    )

    assert report.passed is False
    assert report.exit_code == 2
    assert report.qg_results == {
        "QG-1": False,
        "QG-DM-13": True,
        "QG-DM-5": True,
        "QG-DM-7": True,
        "QG-DM-6": True,
        "QG-DM-10": True,
        "QG-9": True,
        "QG-10": True,
        "QG-11": True,
        "QG-17": True,
        "QG-12": True,
        "QG-13": True,
        "QG-14": True,
        "QG-15": True,
        "QG-16": True,
        "QG-19": True,
        "QG-23": True,
        "QG-24": True,
        "QG-25": True,
        "QG-26": True,
        "QG-WS5B": True,
        "QG-28": True,
    }
    assert report.failing_results[0].message == "Freshness gate failed with 2 blocking item(s)."


def test_qg_dm_13_warns_when_stale_claim_ids_are_present() -> None:
    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        stale_claim_ids=("claim-1", "claim-2"),
    )

    assert report.passed is False
    assert report.exit_code == 1
    assert report.qg_results["QG-DM-13"] is False
    qg_dm_13 = next(result for result in report.results if result.gate_id == "QG-DM-13")
    assert qg_dm_13.forceable is True
    assert "claim-1, claim-2" in qg_dm_13.message


def test_qg_dm_5_warns_when_gather_heartbeat_is_stale_without_gap_record(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text(
        "\n".join(
            (
                "schema_version: '3.0'",
                "id: acme",
                "name: Acme",
                "reality:",
                "  expected_gather_cadence_hours: 24",
            )
        ),
        encoding="utf-8",
    )
    write_gather_state(
        "acme",
        gathered_at=datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc),
        scanned_items=0,
        discovered_signals=0,
        new_signals=0,
        pending_review=0,
        trajectory_updates=0,
        auto_reviews_written=0,
        ado_calls=0,
        archived_journal_files=0,
        background_proposals=0,
        channels={"workiq": {"active": True}},
        programs_root=programs_root,
    )

    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        program_id="acme",
        programs_root=programs_root,
        as_of=datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc),
    )

    assert report.passed is False
    assert report.exit_code == 1
    assert report.qg_results["QG-DM-5"] is False
    qg_dm_5 = next(result for result in report.results if result.gate_id == "QG-DM-5")
    assert qg_dm_5.forceable is True
    assert "workiq" in qg_dm_5.message


def test_qg_dm_7_warns_when_unresolved_material_fact_conflicts_exist(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ProgramFactStore("acme", db_root=programs_root.parent).append_fact(
        ProgramFactInput(
            fact_type="fact.conflict",
            natural_key="conflict:commitment:1",
            entity_refs=("COMMIT-1",),
            payload={
                "family": "commitment",
                "description": "ADO due date disagrees with Teams due date.",
                "resolved": False,
                "is_material": True,
            },
            precedence=FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
            review_state=FactReviewState.ACCEPTED,
        )
    )

    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        program_id="acme",
        programs_root=programs_root,
    )

    assert report.passed is False
    assert report.exit_code == 1
    assert report.qg_results["QG-DM-7"] is False
    qg_dm_7 = next(result for result in report.results if result.gate_id == "QG-DM-7")
    assert qg_dm_7.forceable is True
    assert "ADO due date disagrees with Teams due date." in qg_dm_7.message


def test_qg_dm_6_warns_when_active_candidates_are_stale(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    append_candidate(
        CandidateEvent(
            candidate_id="cand-1",
            program_id="acme",
            proposed_event_type="risk.raised.v1",
            proposed_payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
            proposed_occurred_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            proposed_temporal_confidence="exact",
            proposed_confidence="source_authoritative",
            source_ref=LTDeckRef(file_path="deck.pptx", deck_date=date(2026, 5, 1), slide_number=3),
            pipeline="deck_backfill",
            extraction_confidence=0.9,
            entity_resolution=(),
            dedupe_key="dedupe-1",
            dedupe_core_hash="sha256:abc",
            source_document_key="deck:1",
            corroborating_refs=(),
            batch_id="batch-1",
            staged_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        ),
        programs_root=programs_root,
    )

    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        program_id="acme",
        programs_root=programs_root,
        as_of=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
    )

    assert report.passed is False
    assert report.exit_code == 1
    assert report.qg_results["QG-DM-6"] is False
    qg_dm_6 = next(result for result in report.results if result.gate_id == "QG-DM-6")
    assert qg_dm_6.forceable is True
    assert "oldest staged 19 day(s) ago" in qg_dm_6.message


def test_qg_dm_10_warns_when_projection_watermark_lags_ledger_head(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    first = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
        source_ref=LTDeckRef(file_path="deck-a.pptx", deck_date=date(2026, 5, 1), slide_number=1),
    )
    second = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r2", "title": "Risk two", "severity": "high"},
        source_ref=LTDeckRef(file_path="deck-b.pptx", deck_date=date(2026, 5, 2), slide_number=2),
    )
    quality_gates_module.write_event(first, programs_root=programs_root)
    quality_gates_module.project_program_events("acme", programs_root=programs_root)
    quality_gates_module.write_event(second, programs_root=programs_root)

    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        program_id="acme",
        programs_root=programs_root,
    )

    assert report.passed is False
    assert report.exit_code == 1
    assert report.qg_results["QG-DM-10"] is False
    qg_dm_10 = next(result for result in report.results if result.gate_id == "QG-DM-10")
    assert qg_dm_10.forceable is True
    assert "lags ledger head" in qg_dm_10.message


def test_qg9_flags_overdue_non_terminal_items() -> None:
    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        items=(_work_item(1001, target_date=date(2026, 5, 1)),),
        as_of=date(2026, 5, 10),
    )

    assert report.passed is False
    assert report.exit_code == 2
    assert report.qg_results["QG-9"] is False
    assert report.failing_results[0].gate_id == "QG-9"
    assert report.failing_results[0].forceable is True


def test_qg10_flags_unchanged_narrative_when_material_changes_exist(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    _write_archive_index(archive_root, previous_issue_number=77)
    narrative_dir = archive_root / EDITION_NAME / "narratives" / "issue_077"
    narrative_dir.mkdir(parents=True, exist_ok=True)
    (narrative_dir / "ws_deployment-velocity.md").write_text("Steady narrative.", encoding="utf-8")

    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        items=(_work_item(1001),),
        as_of=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        deltas=_delta(1001, DeltaKind.RISK_UP),
        edition_name=EDITION_NAME,
        issue_number=78,
        workstream_blurbs={"deployment-velocity": "Steady narrative."},
        program_context=_program_context(),
        archive_root=archive_root,
    )

    assert report.passed is False
    assert report.exit_code == 2
    assert report.qg_results["QG-10"] is False
    assert report.failing_results[0].gate_id == "QG-10"


def test_qg12_flags_chronic_high_without_risk_or_escalation(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    edition_root = archive_root / EDITION_NAME
    edition_root.mkdir(parents=True)
    (edition_root / "scorecards.json").write_text(
        json.dumps(
            {
                "entries": [
                    {"issue_number": 75, "scorecard_name": "Acme Health", "dimension": "Deployment Velocity", "risk": "high"},
                    {"issue_number": 76, "scorecard_name": "Acme Health", "dimension": "Deployment Velocity", "risk": "high"},
                    {"issue_number": 77, "scorecard_name": "Acme Health", "dimension": "Deployment Velocity", "risk": "high"},
                ]
            }
        ),
        encoding="utf-8",
    )

    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        dimension_risks=(_dimension_risk("Deployment Velocity", RiskLevel.HIGH),),
        edition_name=EDITION_NAME,
        program_id="acme",
        workstreams=_resolved_workstreams(),
        scorecards=_resolved_scorecards(),
        archive_root=archive_root,
        programs_root=tmp_path / "programs",
    )

    assert report.passed is False
    assert report.exit_code == 2
    assert report.qg_results["QG-12"] is False
    assert report.failing_results[0].gate_id == "QG-12"
    assert report.failing_results[0].forceable is True


def test_qg12_passes_when_linked_risk_register_entry_exists(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    edition_root = archive_root / EDITION_NAME
    edition_root.mkdir(parents=True)
    (edition_root / "scorecards.json").write_text(
        json.dumps(
            {
                "entries": [
                    {"issue_number": 75, "scorecard_name": "Acme Health", "dimension": "Deployment Velocity", "risk": "high"},
                    {"issue_number": 76, "scorecard_name": "Acme Health", "dimension": "Deployment Velocity", "risk": "high"},
                    {"issue_number": 77, "scorecard_name": "Acme Health", "dimension": "Deployment Velocity", "risk": "high"},
                ]
            }
        ),
        encoding="utf-8",
    )
    save_risk_register(
        "acme",
        (
            RiskEntry(
                id="risk-1",
                program_id="acme",
                title="Deployment Velocity chronic risk",
                description="Deployment Velocity remains high.",
                probability=RiskProbability.LIKELY,
                impact=RiskImpact.HIGH,
                category=RiskCategory.SCHEDULE,
                owner_alias="owner",
                mitigation_plan="Escalate and track.",
                mitigation_due_date=date(2026, 5, 20),
                linked_workstream_ids=("velocity",),
                linked_work_item_ids=(),
                linked_milestone_ids=(),
                linked_claim_ids=(),
                linked_action_ids=(),
                status=RiskStatus.OPEN,
                identified_date=date(2026, 5, 1),
                identified_in_vertex_issue=77,
                last_reviewed_date=date(2026, 5, 9),
                entity_refs=(),
            ),
        ),
        programs_root=tmp_path / "programs",
    )

    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        dimension_risks=(_dimension_risk("Deployment Velocity", RiskLevel.HIGH),),
        edition_name=EDITION_NAME,
        program_id="acme",
        workstreams=_resolved_workstreams(),
        scorecards=_resolved_scorecards(),
        archive_root=archive_root,
        programs_root=tmp_path / "programs",
    )

    assert report.qg_results["QG-12"] is True


def test_qg13_blocks_uncovered_high_risk_items() -> None:
    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        items=(_work_item(1001, risk_level=RiskLevel.HIGH),),
        as_of=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        narratives={"ws_deployment-velocity.md": "General update without explicit work item references."},
        approved_signals=(),
    )

    assert report.passed is False
    assert report.exit_code == 3
    assert report.qg_results["QG-13"] is False
    assert report.failing_results[0].gate_id == "QG-13"
    assert report.failing_results[0].forceable is False


def test_phase_1b_gates_scope_item_based_checks_to_publishable_items() -> None:
    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(
            issue_number=78,
            items=(
                FreshnessItem(
                    work_item_id=1001,
                    rule_id="changed_date",
                    severity="block",
                    message="Out-of-scope item is stale.",
                    suggested_fix=None,
                ),
            ),
            blocks=1,
            warns=0,
            infos=0,
        ),
        items=(
            _work_item(1001, target_date=date(2026, 5, 1), risk_level=RiskLevel.HIGH),
            _work_item(1002, target_date=date(2026, 5, 20), risk_level=RiskLevel.LOW),
        ),
        publishable_item_ids=(1002,),
        as_of=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        narratives={},
        approved_signals=(),
    )

    assert report.qg_results["QG-1"] is True
    assert report.qg_results["QG-9"] is True
    assert report.qg_results["QG-13"] is True


def test_qg13_treats_caller_supplied_covered_items_as_narrative_coverage() -> None:
    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        items=(_work_item(1001, risk_level=RiskLevel.HIGH),),
        covered_item_ids=(1001,),
        as_of=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        narratives={},
        approved_signals=(),
    )

    assert report.qg_results["QG-13"] is True


def test_qg11_flags_open_claims_contradicted_by_current_ado_state(tmp_path: Path) -> None:
    append_claim_entry(
        ClaimEntry(
            id="claim-1",
            edition_id=EDITION_NAME,
            issue_number=78,
            program_id="acme",
            workstream_id="velocity",
            text="Deployment will complete by 2026-05-12.",
            claim_date=date(2026, 5, 9),
            due_date=date(2026, 5, 12),
            owner_alias="owner",
            status="open",
            entity_refs=("WI:1001",),
        ),
        programs_root=tmp_path / "programs",
    )

    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        items=(_work_item(1001, target_date=date(2026, 5, 20)),),
        program_id="acme",
        workstreams=_resolved_workstreams(),
        programs_root=tmp_path / "programs",
    )

    assert report.qg_results["QG-11"] is False
    qg11 = next(result for result in report.failing_results if result.gate_id == "QG-11")
    assert qg11.forceable is True


def test_qg11_is_not_forceable_at_l2_and_above() -> None:
    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        items=(_work_item(1001, target_date=date(2026, 5, 20)),),
        program_id="acme",
        program_maturity_level=2,
        workstreams=_resolved_workstreams(),
        programs_root=Path(".") / "missing-programs-root",
    )

    qg11 = next(result for result in report.results if result.gate_id == "QG-11")
    assert qg11.passed is True
    assert qg11.forceable is False


def test_qg17_flags_multiple_contradicted_items_without_narrative_acknowledgment(tmp_path: Path) -> None:
    append_claim_entry(
        ClaimEntry(
            id="claim-1",
            edition_id=EDITION_NAME,
            issue_number=78,
            program_id="acme",
            workstream_id="velocity",
            text="Deployment will complete by 2026-05-12.",
            claim_date=date(2026, 5, 9),
            due_date=date(2026, 5, 12),
            owner_alias="owner",
            status="open",
            entity_refs=("WI:1001",),
        ),
        programs_root=tmp_path / "programs",
    )
    approved_signals = (
        Signal(
            id="signal-1",
            timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
            source="workiq/email",
            program_id="acme",
            workstream_id="velocity",
            entity_refs=("WI:1002",),
            text="Latest mail says rollout moved to 2026-05-25.",
            raw_ref=None,
            confidence=Confidence.MEDIUM,
            metadata=None,
            thread_id=None,
        ),
    )

    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        items=(
            _work_item(1001, target_date=date(2026, 5, 20)),
            _work_item(1002, target_date=date(2026, 5, 10)),
        ),
        as_of=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        approved_signals=approved_signals,
        workstream_blurbs={"deployment-velocity": "General update without specific work item references."},
        program_id="acme",
        workstreams=_resolved_workstreams(),
        programs_root=tmp_path / "programs",
    )

    assert report.qg_results["QG-17"] is False
    qg17 = next(result for result in report.results if result.gate_id == "QG-17")
    assert qg17.forceable is True
    assert "Deployment Velocity" in qg17.message
    assert "WI:1001" in qg17.message
    assert "WI:1002" in qg17.message


def test_qg17_passes_when_workstream_narrative_mentions_any_contradicted_item(tmp_path: Path) -> None:
    append_claim_entry(
        ClaimEntry(
            id="claim-1",
            edition_id=EDITION_NAME,
            issue_number=78,
            program_id="acme",
            workstream_id="velocity",
            text="Deployment will complete by 2026-05-12.",
            claim_date=date(2026, 5, 9),
            due_date=date(2026, 5, 12),
            owner_alias="owner",
            status="open",
            entity_refs=("WI:1001",),
        ),
        programs_root=tmp_path / "programs",
    )
    approved_signals = (
        Signal(
            id="signal-1",
            timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
            source="workiq/email",
            program_id="acme",
            workstream_id="velocity",
            entity_refs=("WI:1002",),
            text="Latest mail says rollout moved to 2026-05-25.",
            raw_ref=None,
            confidence=Confidence.MEDIUM,
            metadata=None,
            thread_id=None,
        ),
    )

    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        items=(
            _work_item(1001, target_date=date(2026, 5, 20)),
            _work_item(1002, target_date=date(2026, 5, 10)),
        ),
        as_of=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        approved_signals=approved_signals,
        workstream_blurbs={"deployment-velocity": "WI:1002 remains contradictory and needs follow-up."},
        program_id="acme",
        workstreams=_resolved_workstreams(),
        programs_root=tmp_path / "programs",
    )

    assert report.qg_results["QG-17"] is True


def test_qg14_flags_high_risk_dimension_without_next_action() -> None:
    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        dimension_risks=(_dimension_risk("Deployment Velocity", RiskLevel.HIGH),),
        scorecards=_resolved_scorecards(),
        workstreams=_resolved_workstreams(),
        overrides_document=OverridesDocument(
            issue_number=78,
            top_3_now=(),
            scorecards=(
                ScorecardOverrides(
                    name="Acme Health",
                    dimensions=(
                        DimensionOverride(name="Deployment Velocity", risk=RiskLevel.HIGH, note="carry"),
                    ),
                ),
            ),
        ),
        workstream_blurbs={"deployment-velocity": "Risk remains elevated."},
    )

    assert report.qg_results["QG-14"] is False
    assert report.failing_results[0].gate_id == "QG-14"


def test_qg14_passes_when_high_risk_dimension_has_next_action_note() -> None:
    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        dimension_risks=(_dimension_risk("Deployment Velocity", RiskLevel.HIGH),),
        scorecards=_resolved_scorecards(),
        workstreams=_resolved_workstreams(),
        overrides_document=OverridesDocument(
            issue_number=78,
            top_3_now=(),
            scorecards=(
                ScorecardOverrides(
                    name="Acme Health",
                    dimensions=(
                        DimensionOverride(
                            name="Deployment Velocity",
                            risk=RiskLevel.HIGH,
                            note="Next step: confirm the mitigation owner and close the open blocker by 2026-05-20.",
                        ),
                    ),
                ),
            ),
        ),
        workstream_blurbs={"deployment-velocity": "Risk remains elevated."},
    )

    assert report.qg_results["QG-14"] is True


def test_qg15_flags_open_actions_missing_due_date_or_owner(tmp_path: Path) -> None:
    append_action(
        "acme",
        ActionItem(
            id="action-1",
            program_id="acme",
            text="Follow up with the firmware team",
            owner_alias="unknown",
            due_date=None,
            status=ActionStatus.OPEN,
            source_signal_id="signal-1",
            source_type=ActionSourceType.SIGNAL,
            linked_work_item_ids=(1001,),
            linked_claim_id=None,
            linked_risk_id=None,
            workstream_id="velocity",
            created_at=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
            resolved_at=None,
            resolution_note=None,
        ),
        programs_root=tmp_path / "programs",
    )

    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        program_id="acme",
        programs_root=tmp_path / "programs",
    )

    assert report.qg_results["QG-15"] is False
    assert report.failing_results[0].gate_id == "QG-15"


def test_quality_gate_current_fact_loaders_use_program_facts(monkeypatch, tmp_path: Path) -> None:
    action_snapshot = object()
    milestone_snapshot = object()
    risk_snapshot = object()
    dependency_snapshot = object()
    captured: list[tuple[str, tuple[str, ...]]] = []

    monkeypatch.setattr(
        current_state_module,
        "load_program_facts",
        lambda program_id, *, programs_root, fact_types: captured.append((program_id, fact_types)) or {
            ("action.item",): action_snapshot,
            ("milestone.entry",): milestone_snapshot,
            ("risk.entry",): risk_snapshot,
            ("dependency.link",): dependency_snapshot,
        }[fact_types],
    )
    monkeypatch.setattr(
        current_state_module,
        "project_action_items",
        lambda snapshot: (
            ActionItem(
                id="action-1",
                program_id="acme",
                text="Follow up with the firmware team",
                owner_alias="unknown",
                due_date=None,
                status=ActionStatus.OPEN,
                source_signal_id="signal-1",
                source_type=ActionSourceType.SIGNAL,
                linked_work_item_ids=(1001,),
                linked_claim_id=None,
                linked_risk_id=None,
                workstream_id="velocity",
                created_at=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
                resolved_at=None,
                resolution_note=None,
            ),
        ) if snapshot is action_snapshot else (),
    )
    monkeypatch.setattr(
        current_state_module,
        "project_milestones",
        lambda snapshot: (
            Milestone(
                id="ms-1",
                program_id="acme",
                name="Ramp readiness",
                target_date=date(2026, 5, 15),
                owner_alias="owner",
                status=MilestoneStatus.ON_TRACK,
                exit_criteria=(),
                linked_workstream_ids=("velocity",),
                linked_work_item_ids=(1001,),
                notes="",
            ),
        ) if snapshot is milestone_snapshot else (),
    )
    monkeypatch.setattr(
        current_state_module,
        "project_risk_entries",
        lambda snapshot: () if snapshot is risk_snapshot else (),
    )
    monkeypatch.setattr(
        current_state_module,
        "project_dependencies",
        lambda snapshot: (
            Dependency(
                id="dep-1",
                from_program_id="acme",
                from_workstream_id="velocity",
                from_item_id=1001,
                from_milestone_id=None,
                to_program_id="fabrikam",
                to_workstream_id="buildouts",
                to_item_id=None,
                to_milestone_id=None,
                dependency_type=DependencyType.BLOCKS,
                risk_if_broken="Fabrikam buildout planning depends on Acme readiness.",
                mitigation=None,
                status=DependencyStatus.BROKEN,
                owner_alias="owner",
                resolution_path=None,
                planned_resolution_date=None,
                schedule_status=DependencyScheduleStatus.BLOCKED,
            ),
        ) if snapshot is dependency_snapshot else (),
    )

    qg15 = quality_gates_module._evaluate_open_action_completeness_gate(program_id="acme", programs_root=tmp_path / "programs")
    qg16 = quality_gates_module._load_current_milestones("acme", programs_root=tmp_path / "programs")
    qg16_risks = quality_gates_module._load_current_risks("acme", programs_root=tmp_path / "programs")
    qg19 = quality_gates_module._load_current_dependencies("acme", programs_root=tmp_path / "programs")

    assert qg15.passed is False
    assert [entry.id for entry in qg16] == ["ms-1"]
    assert qg16_risks == ()
    assert [entry.id for entry in qg19] == ["dep-1"]
    assert captured == [
        ("acme", ("action.item",)),
        ("acme", ("milestone.entry",)),
        ("acme", ("risk.entry",)),
        ("acme", ("dependency.link",)),
    ]


def test_qg16_flags_at_risk_milestone_without_linked_risk(tmp_path: Path) -> None:
    program_dir = tmp_path / "programs" / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "milestones.yaml").write_text(
        """
schema_version: "1.0"
milestones:
  - id: ms-1
    name: Ramp readiness
    target_date: 2026-05-15
    owner_alias: owner
    status: on_track
    exit_criteria: []
    linked_workstream_ids: [velocity]
    linked_work_item_ids: [1001]
""".strip(),
        encoding="utf-8",
    )

    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        items=(_work_item(1001, risk_level=RiskLevel.HIGH),),
        as_of=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        narratives={"ws_deployment-velocity.md": "WI:1001 remains covered in the narrative while the milestone stays at risk."},
        program_id="acme",
        programs_root=tmp_path / "programs",
    )

    assert report.qg_results["QG-16"] is False
    assert report.failing_results[0].gate_id == "QG-16"


def test_qg16_passes_when_at_risk_milestone_has_linked_risk(tmp_path: Path) -> None:
    program_dir = tmp_path / "programs" / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "milestones.yaml").write_text(
        """
schema_version: "1.0"
milestones:
  - id: ms-1
    name: Ramp readiness
    target_date: 2026-05-15
    owner_alias: owner
    status: on_track
    exit_criteria: []
    linked_workstream_ids: [velocity]
    linked_work_item_ids: [1001]
""".strip(),
        encoding="utf-8",
    )
    save_risk_register(
        "acme",
        (
            RiskEntry(
                id="risk-1",
                program_id="acme",
                title="Ramp readiness risk",
                description="Tracks the milestone risk directly.",
                probability=RiskProbability.LIKELY,
                impact=RiskImpact.HIGH,
                category=RiskCategory.SCHEDULE,
                owner_alias="owner",
                mitigation_plan="Track the milestone explicitly.",
                mitigation_due_date=date(2026, 5, 20),
                linked_workstream_ids=("velocity",),
                linked_work_item_ids=(1001,),
                linked_milestone_ids=("ms-1",),
                linked_claim_ids=(),
                linked_action_ids=(),
                status=RiskStatus.OPEN,
                identified_date=date(2026, 5, 1),
                identified_in_vertex_issue=77,
                last_reviewed_date=date(2026, 5, 9),
                entity_refs=(),
            ),
        ),
        programs_root=tmp_path / "programs",
    )

    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        items=(_work_item(1001, risk_level=RiskLevel.HIGH),),
        as_of=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        narratives={"ws_deployment-velocity.md": "WI:1001 remains covered in the narrative while the milestone stays at risk."},
        program_id="acme",
        programs_root=tmp_path / "programs",
    )

    assert report.qg_results["QG-16"] is True


def test_qg16_fails_when_sqlite_backed_at_risk_milestone_lacks_linked_risk(tmp_path: Path) -> None:
    program_dir = tmp_path / "programs" / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        "\n".join(
            (
                'schema_version: "2.0"',
                "id: acme",
                "name: Acme",
                "current_phase: Ramp readiness",
                "storage_backend: sqlite",
            )
        ),
        encoding="utf-8",
    )
    (program_dir / "milestones.yaml").write_text(
        """
schema_version: "1.0"
milestones:
  - id: ms-1
    name: Ramp readiness
    target_date: 2026-05-15
    owner_alias: owner
    status: on_track
    exit_criteria: []
    linked_workstream_ids: [velocity]
    linked_work_item_ids: [1001]
""".strip(),
        encoding="utf-8",
    )
    SQLiteTrajectoryStore(programs_root=tmp_path / "programs").append(
        "acme",
        1001,
        TrajectoryPoint(
            date=date(2026, 5, 10),
            state="Active",
            assigned_to="owner",
            target_date=date(2026, 5, 20),
            risk_level=RiskLevel.HIGH,
            area_path="Acme\\Velocity",
        ),
    )

    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        items=(_work_item(1001, risk_level=RiskLevel.HIGH),),
        as_of=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        narratives={"ws_deployment-velocity.md": "WI:1001 remains covered in the narrative while the milestone stays at risk."},
        program_id="acme",
        programs_root=tmp_path / "programs",
    )

    assert report.qg_results["QG-16"] is False
    assert report.failing_results[0].gate_id == "QG-16"


def test_qg19_flags_unresolved_cross_program_dependency_cascade(tmp_path: Path) -> None:
    program_dir = tmp_path / "programs" / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "dependencies.yaml").write_text(
        """
schema_version: "1.0"
dependencies:
  - id: dep-1
    from_item_id: 1001
    to_workstream_id: fabrikam:buildouts
    dependency_type: blocks
    risk_if_broken: Fabrikam buildout planning depends on Acme readiness.
    status: broken
""".strip(),
        encoding="utf-8",
    )

    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        items=(_work_item(1001),),
        as_of=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        approved_signals=(
            Signal(
                id="signal-1",
                timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
                source="workiq/email",
                program_id="acme",
                workstream_id="velocity",
                entity_refs=("WI:1001",),
                text="Latest mail says Acme readiness slipped again.",
                raw_ref=None,
                confidence=Confidence.HIGH,
                metadata=None,
                thread_id=None,
            ),
        ),
        program_id="acme",
        programs_root=tmp_path / "programs",
    )

    assert report.qg_results["QG-19"] is False
    qg19 = next(result for result in report.results if result.gate_id == "QG-19")
    assert qg19.forceable is True
    assert "WI#1001" in qg19.message
    assert "fabrikam:buildouts" in qg19.message


def test_qg19_ignores_resolved_cross_program_dependencies(tmp_path: Path) -> None:
    program_dir = tmp_path / "programs" / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "dependencies.yaml").write_text(
        """
schema_version: "1.0"
dependencies:
  - id: dep-1
    from_item_id: 1001
    to_workstream_id: fabrikam:buildouts
    dependency_type: blocks
    risk_if_broken: Fabrikam buildout planning depends on Acme readiness.
    status: resolved
""".strip(),
        encoding="utf-8",
    )

    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        items=(_work_item(1001),),
        as_of=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        approved_signals=(
            Signal(
                id="signal-1",
                timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
                source="workiq/email",
                program_id="acme",
                workstream_id="velocity",
                entity_refs=("WI:1001",),
                text="Latest mail says Acme readiness slipped again.",
                raw_ref=None,
                confidence=Confidence.HIGH,
                metadata=None,
                thread_id=None,
            ),
        ),
        program_id="acme",
        programs_root=tmp_path / "programs",
    )

    assert report.qg_results["QG-19"] is True


def test_combine_gate_reports_merges_phase_results() -> None:
    phase_1a = evaluate_phase_1a_gates(
        ban_list_violations=(),
        verbosity_violations=(),
        manifest=_manifest("sha256:snapshot"),
        expected_snapshot_hash="sha256:snapshot",
        dimension_risks=(_dimension_risk("Deployment Velocity", RiskLevel.LOW),),
    )
    phase_1b = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=1, infos=0)
    )

    combined = combine_gate_reports(phase_1a, phase_1b)

    assert combined.passed is True
    assert combined.qg_results == {
        "QG-4": True,
        "QG-5": True,
        "QG-6": True,
        "QG-DM-1": True,
        "QG-DM-4": True,
        "QG-8": True,
        "QG-1": True,
        "QG-DM-13": True,
        "QG-DM-5": True,
        "QG-DM-7": True,
        "QG-DM-6": True,
        "QG-DM-10": True,
        "QG-9": True,
        "QG-10": True,
        "QG-11": True,
        "QG-17": True,
        "QG-12": True,
        "QG-13": True,
        "QG-14": True,
        "QG-15": True,
        "QG-16": True,
        "QG-19": True,
        "QG-23": True,
        "QG-24": True,
        "QG-25": True,
        "QG-26": True,
        "QG-WS5B": True,
        "QG-28": True,
    }


def test_evaluate_phase_1c_gates_marks_hygiene_review_and_archive_failures_forceable() -> None:
    report = evaluate_phase_1c_gates(
        hygiene_warnings=("missing citation",),
        review_status=ReviewStatus(
            issue_number=12,
            sections=(
                ReviewSection(
                    section_id="exec_summary",
                    state=ReviewState.PENDING,
                    reviewer=None,
                    note=None,
                    updated_at=None,
                ),
            ),
        ),
        review_required=True,
        archive_inconsistencies=("Issue 011 manifest file is missing",),
    )

    assert report.passed is False
    assert report.exit_code == 2
    assert report.qg_results == {"QG-2": False, "QG-3": False, "QG-7": False}
    assert all(result.forceable for result in report.failing_results)


def test_evaluate_phase_1c_gates_includes_outlook_compatibility_gate() -> None:
    html = (
        '<table role="presentation" style="width:100%; border-collapse:collapse; background-color:#FFFFFF;">'
        '<tr><td style="color:#2563EB;">Hello</td></tr>'
        "</table>"
    )

    report = evaluate_phase_1c_gates(
        hygiene_warnings=(),
        review_status=ReviewStatus(issue_number=12, sections=()),
        review_required=False,
        archive_inconsistencies=(),
        html_content=html,
    )

    assert report.qg_results["QG-18"] is True


def test_evaluate_phase_1c_gates_rejects_style_blocks() -> None:
    report = evaluate_phase_1c_gates(
        hygiene_warnings=(),
        review_status=ReviewStatus(issue_number=12, sections=()),
        review_required=False,
        archive_inconsistencies=(),
        html_content='<style type="text/css">body{color:#FFFFFF;}</style><table style="width:100%;"></table>',
    )

    assert report.qg_results["QG-18"] is False
    qg18 = next(result for result in report.results if result.gate_id == "QG-18")
    assert qg18.forceable is True
    assert "<style>" in qg18.message


def test_evaluate_phase_1c_gates_rejects_non_canonical_colors() -> None:
    report = evaluate_phase_1c_gates(
        hygiene_warnings=(),
        review_status=ReviewStatus(issue_number=12, sections=()),
        review_required=False,
        archive_inconsistencies=(),
        html_content='<table style="width:100%; background-color:#123456;"><tr><td>Bad</td></tr></table>',
    )

    assert report.qg_results["QG-18"] is False
    qg18 = next(result for result in report.results if result.gate_id == "QG-18")
    assert "#123456" in qg18.message


# ---------------------------------------------------------------------------
# QG-CI-01 and QG-CI-02: Context integrity gates
# ---------------------------------------------------------------------------

def test_qg_ci_01_passes_when_no_milestones_file(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    programs_root.mkdir(parents=True)
    (programs_root / "acme").mkdir()
    report = evaluate_context_integrity_gates(program_id="acme", programs_root=programs_root)
    assert report.qg_results["QG-CI-01"] is True


def test_qg_ci_01_fails_when_stub_wi_id_present(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "acme").mkdir(parents=True)
    (programs_root / "acme" / "milestones.yaml").write_text(
        "milestones:\n"
        "  - name: Fake Milestone\n"
        "    linked_work_item_ids: [999999]\n",
        encoding="utf-8",
    )
    report = evaluate_context_integrity_gates(program_id="acme", programs_root=programs_root)
    assert report.qg_results["QG-CI-01"] is False
    result = next(r for r in report.results if r.gate_id == "QG-CI-01")
    assert "999999" in result.message


def test_qg_ci_02_passes_when_no_scorecards_file(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "acme").mkdir(parents=True)
    report = evaluate_context_integrity_gates(program_id="acme", programs_root=programs_root)
    assert report.qg_results["QG-CI-02"] is True


def test_qg_ci_02_fails_when_informal_filter_present(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "acme").mkdir(parents=True)
    (programs_root / "acme" / "scorecards.yaml").write_text(
        "scorecards:\n"
        "  - name: Delivery\n"
        "    dimensions:\n"
        "      - name: Velocity\n"
        "        ado_filter: active bugs\n",
        encoding="utf-8",
    )
    report = evaluate_context_integrity_gates(program_id="acme", programs_root=programs_root)
    assert report.qg_results["QG-CI-02"] is False


def test_qg23_staleness_warning(mocker) -> None:
    mock_check = mocker.patch("src.core.exec_summary_diff_engine.check_exec_summary_staleness")
    from src.core.exec_summary_diff_engine import ExecSummaryStalenessFinding
    mock_check.return_value = [
        ExecSummaryStalenessFinding(
            workstream_id="velocity",
            workstream_section_id="ws_velocity",
            exec_bullet_text="Velocity is high.",
            workstream_lead_sentence="Velocity has slowed down.",
            prior_workstream_lead_sentence="Velocity is high.",
            divergence_score=0.2,
            is_stale=True
        )
    ]

    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        edition_name="acme_weekly",
        issue_number=78,
    )

    assert report.qg_results["QG-23"] is False
    failing = [r for r in report.results if r.gate_id == "QG-23"][0]
    assert "appears stale" in failing.message
    assert failing.exit_code == 1
    assert failing.forceable is True


def test_qg24_missing_metric_warning(mocker) -> None:
    mock_store_cls = mocker.patch("src.core.reality_store.RealityStore")
    mock_store = mock_store_cls.return_value
    mock_store.list_metric_observations.return_value = []

    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        program_id="acme",
        narratives={"ws_velocity.md": "Deployment velocity: <!-- vertex:metric: acme.deployment_p50_mins -->"},
    )

    assert report.qg_results["QG-24"] is False
    failing = [r for r in report.results if r.gate_id == "QG-24"][0]
    assert "cannot be resolved from reality_store" in failing.message


def test_qg24_missing_ado_fields(mocker) -> None:
    mock_store_cls = mocker.patch("src.core.reality_store.RealityStore")
    mock_store = mock_store_cls.return_value
    mock_store.list_metric_observations.return_value = [object()]

    item = _work_item(1001)

    report = evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        program_id="acme",
        items=(item,),
        narratives={"ws_velocity.md": "Deployment velocity: <!-- vertex:metric: acme.deployment_p50_mins -->"},
    )

    assert report.qg_results["QG-24"] is False
    failing = [r for r in report.results if r.gate_id == "QG-24"][0]
    assert "Custom.RiskAssessment" in failing.message


def test_qg25_passes_when_program_id_not_provided() -> None:
    """QG-25: gate passes (skipped) when program_id is not provided."""
    from src.core.quality_gates import _evaluate_email_signal_coverage_gate

    result = _evaluate_email_signal_coverage_gate(
        channel_states=None,
        program_id=None,
    )
    assert result.gate_id == "QG-25"
    assert result.passed is True
    assert result.forceable is True


def test_qg25_passes_when_channel_states_not_provided() -> None:
    """QG-25: gate passes (skipped) when channel_states is None."""
    from src.core.quality_gates import _evaluate_email_signal_coverage_gate

    result = _evaluate_email_signal_coverage_gate(
        channel_states=None,
        program_id="acme",
    )
    assert result.gate_id == "QG-25"
    assert result.passed is True


def test_qg25_passes_when_workiq_not_active() -> None:
    """QG-25: gate passes when WorkIQ channel is not active."""
    from src.core.quality_gates import _evaluate_email_signal_coverage_gate

    result = _evaluate_email_signal_coverage_gate(
        channel_states={"workiq": {"active": False}},
        program_id="acme",
    )
    assert result.gate_id == "QG-25"
    assert result.passed is True


def test_qg_sg_09_passes_when_no_high_confidence_contradictions() -> None:
    """QG-SG-09: gate passes when no HIGH-confidence contradiction packets exist."""
    from src.core.quality_gates import evaluate_contradiction_gate
    from src.core.models_v2 import ContradictionPacket
    result = evaluate_contradiction_gate(())
    assert result.results[0].gate_id == "QG-SG-09"
    assert result.results[0].passed is True


def test_qg_sg_09_blocks_on_high_confidence_contradiction() -> None:
    """QG-SG-09: gate fails (hard block) when HIGH-confidence contradiction exists."""
    from src.core.quality_gates import evaluate_contradiction_gate
    from src.core.models_v2 import ContradictionPacket, Contradiction, ResolvedContradiction
    now = datetime.now(timezone.utc)
    contradiction = Contradiction(
        field="status",
        source_a="ado",
        source_b="workiq",
        summary="ADO says Complete but WorkIQ says blocked",
        confidence=Confidence.HIGH,
        evidence_refs=(),
    )
    packet = ContradictionPacket(
        work_item_id=12345,
        workstream_id="ws1",
        contradictions=(contradiction,),
        confidence=Confidence.HIGH,
        recommended_resolution=None,
        generated_at=now,
    )
    result = evaluate_contradiction_gate((packet,))
    assert result.results[0].gate_id == "QG-SG-09"
    assert result.results[0].passed is False
    assert result.results[0].exit_code == 3
    """QG-25: gate passes when email signals are present."""
    from src.core.quality_gates import _evaluate_email_signal_coverage_gate

    result = _evaluate_email_signal_coverage_gate(
        channel_states={"workiq": {"active": True, "email_signals": 5}},
        program_id="acme",
    )
    assert result.gate_id == "QG-25"
    assert result.passed is True
