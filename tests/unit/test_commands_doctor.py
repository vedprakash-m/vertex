from __future__ import annotations

import csv
import json
import os
import shutil
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
import yaml
from typer.testing import CliRunner

from cli import app
from src.commands.doctor import ADOProbeResult, DoctorCheck, DoctorReport, _build_doctor_payload, _build_m365_registry_review_metadata, _consistency_check, _load_dependency_workstream_ids, _operator_gate_m365_ids_check, _run_platform_readiness_doctor, _uil_registry_check, render_doctor_output, run_doctor
from src.commands.doctor_checks.default_report_support_checks import candidate_queue_backlog_check, claim_freshness_check, coverage_range_check, degraded_confirm_check, ledger_health_check
from src.commands.doctor_checks.refactor_status import RefactorStatusMetric, RefactorStatusReport
from src.core.action_tracker import append_action
from src.core.analytics_store import get_program_autonomy_audit_path
from src.core.channel_registry_store import ChannelRegistryStore
from src.core.checkpoint_store import create_checkpoint_snapshot
from src.core.exceptions import ConfigError, QueryError
from src.core.gather_state_store import write_gather_state
from src.core.journal import append_review_decision, append_signal
from src.core.knowledge_candidate_store import KnowledgeCandidateDecisionRecord, append_candidate as append_knowledge_candidate, append_triage_decision as append_knowledge_candidate_decision, build_candidate as build_knowledge_candidate
from src.core.knowledge_claim_store import append_claim_revision
from src.core.knowledge.vault import ingest_knowledge_source, write_shared_vault_verify_status
from src.core.kusto_client import KustoColumn
from src.core.ledger.candidate_store import CandidateEntityResolution, CandidateEvent, append_candidate
from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope, canonical_json, write_event
from src.core.ledger.program_views import get_current_projection_path, project_program_events
from src.core.ledger.source_refs import EmailRef, KnowledgeDocumentRef, LTDeckRef, OperatorAssertionRef, WorkIQRef
from src.core.projections.snapshot_manager import build_baseline_hardlock_event, write_projection_snapshot
from src.core.m365_registry_store import M365RegistryArtifact, upsert_m365_registry_artifacts
from src.core.metric_models import MetricAggregation, MetricDefinition, MetricSourceBinding, ObservationWindow
from src.core.models import ReviewSection, ReviewState, ReviewStatus
from src.core.reality_store import RealityStore
from src.core.models import Confidence, RiskLevel
from src.core.discovery_intent import DiscoveryAttempt, DiscoveryAttemptOutcome, SourceCandidate, SourceCandidateStatus, SourceIntent, SourceIntentStatus, SourceRefKind, build_discovery_attempt_id, build_source_intent_id
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus, IntegrationError, ReviewPolicy, SectionEvidenceBrief, SectionRevisionProposal, SectionRevisionStatus, Signal, SignalReviewDecision, TrajectoryPoint, Workstream, WorkstreamSignalSources
from src.core.integration_types import ChannelRegistration, DiscoveredRef, DiscoveryCompleteness, DiscoveryResult, RegistrationBinding, RegistrationStatus, ScopeStatus, ScopeStatusKind
from src.core.overrides_store import load_overrides
from src.core.platform_s7_store import save_platform_s7_state
from src.core.profile_encryption import encrypt_people_profiles_file
from src.core.readiness_engine import ReadinessFetchLoaders, build_readiness_snapshot, load_readiness_config, write_readiness_snapshot
from src.core.review_status_store import save_review_status
from src.core.section_proposal_store import append_proposal, write_accepted_proposals_archive
from src.core.semantic_index import get_semantic_index_state_path, rebuild_archive_semantic_index
from src.core.sqlite_stores import SQLiteTrajectoryStore
from src.core.snapshot_store import get_archive_root
from src.core.source_candidate_store import SourceCandidateStore
from src.core.trajectory import append_trajectory_point
from src.core.trusted_baseline_store import TrustedBaseline, TrustedBaselineHistoryEntry, save_trusted_baseline
from src.m365.agency_bridge import AgencyCapabilities
from tests.support.decision_source_fixtures import build_structured_decision_source_docs
from tests.support.report_test_setup import reset_overrides_to_seed_state, stage_v2_report_workspace


runner = CliRunner()
EDITION_NAME = "acme_weekly"


class _FakeKeyring:
        def __init__(self) -> None:
                self._values: dict[tuple[str, str], str] = {}

        def get_password(self, service_name: str, username: str) -> str | None:
                return self._values.get((service_name, username))

        def set_password(self, service_name: str, username: str, password: str) -> None:
                self._values[(service_name, username)] = password


def _write_platform_program(
    program_dir: Path,
    *,
    name: str,
    edition_name: str,
    current_phase: str,
    start_date: str | None = None,
) -> None:
        program_dir.mkdir(parents=True, exist_ok=True)
        lines = [
                'schema_version: "2.0"',
                f"id: {program_dir.name}",
                f"name: {name}",
                f"current_phase: {current_phase}",
        ]
        if start_date is not None:
                lines.append(f"start_date: {start_date}")
        lines.extend(
                [
                        "ado:",
                        "  organization: your-org",
                        "  project: One",
                        "communication_plan:",
                        f"  - edition: {edition_name}",
                        "    cadence: weekly",
                ]
        )
        (program_dir / "program.yaml").write_text(
                "\n".join(lines)
                + "\n",
                encoding="utf-8",
        )
        edition_root = program_dir / "archive" / edition_name
        (edition_root / "overrides").mkdir(parents=True, exist_ok=True)
        (edition_root / "index.json").write_text(
                json.dumps(
                        {
                                "edition": edition_name,
                                "issues": [
                                        {
                                                "issue_number": 1,
                                                "generated_at": "2026-05-20T18:00:00+00:00",
                                                "kind": "confirmed",
                                                "html_path": None,
                                                "md_path": None,
                                                "snapshot_path": None,
                                                "manifest_path": None,
                                        }
                                ],
                        },
                        indent=2,
                ),
                encoding="utf-8",
        )
        (edition_root / "scorecards.json").write_text(
                json.dumps({"schema_version": "1.0", "entries": []}, indent=2),
                encoding="utf-8",
        )


def _write_platform_proof_log(program_dir: Path, *, proofs: list[dict[str, object]]) -> None:
        (program_dir / "platform_proof_log.yaml").write_text(
                yaml.safe_dump(
                        {
                                "schema_version": "1.0",
                                "proofs": proofs,
                        },
                        sort_keys=False,
                        allow_unicode=False,
                ),
                encoding="utf-8",
        )


def test_run_doctor_creates_overrides_with_fix(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        monkeypatch.delenv("GRAPH_TENANT_ID", raising=False)
        monkeypatch.delenv("GRAPH_CLIENT_ID", raising=False)

        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        reset_overrides_to_seed_state(reports_root)

        report = run_doctor(
                edition_name=EDITION_NAME,
                fix=True,
                reports_root=reports_root,
                archive_root=archive_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        assert report.failures == 0
        assert report.warnings >= 1
        mail_preview = next(check for check in report.checks if check.label == "Mail Preview")
        assert mail_preview.status == "warn"
        assert "GRAPH_TENANT_ID" in mail_preview.detail
        assert load_overrides(EDITION_NAME, reports_root=reports_root) is not None


def test_doctor_cli(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        monkeypatch.delenv("GRAPH_TENANT_ID", raising=False)
        monkeypatch.delenv("GRAPH_CLIENT_ID", raising=False)

        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        reset_overrides_to_seed_state(reports_root)

        monkeypatch.setattr("src.commands.doctor.REPORTS_ROOT", reports_root)
        monkeypatch.setattr("src.commands.doctor.ARCHIVE_ROOT", archive_root)
        monkeypatch.setattr(
                "src.commands.doctor._probe_ado_access",
                lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        result = runner.invoke(app, ["doctor", "--edition", EDITION_NAME, "--fix"])

        assert result.exit_code == 0
        assert "Config" in result.stdout
        assert "Slices" in result.stdout
        assert "slice contracts loaded" in result.stdout
        assert "ADO Access" in result.stdout
        assert "Mail Preview" in result.stdout
        assert "GRAPH_TENANT_ID" in result.stdout
        assert "Overall: HEALTHY" in result.stdout


def test_ledger_health_check_flags_dangling_unlock_session(tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        now = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)

        for event_type in ("operator.field_lock.v1", "operator.field_unlock.v1"):
                write_event(
                        build_event_envelope(
                                program_id="acme",
                                event_type=event_type,
                                occurred_at=now,
                                recorded_at=now,
                                temporal_confidence=TemporalConfidence.EXACT,
                                confidence=ConfidenceTier.OPERATOR_CONFIRMED,
                                actor="test-operator",
                                payload={
                                        "entity_id": "milestone:m1",
                                        "field": "target_date",
                                        "override_session_id": "session-1",
                                },
                                source_ref=OperatorAssertionRef(asserted_by="test-operator", asserted_at=now, context=event_type),
                                dedupe_payload={"entity_id": "milestone:m1", "field": "target_date", "override_session_id": f"{event_type}:session-1"},
                        ),
                        programs_root=programs_root,
                )

        check = ledger_health_check("acme", programs_root)

        assert check is not None
        assert check.status == "fail"
        assert "session-1" in check.detail


def test_ledger_health_check_accepts_unlock_session_with_relock(tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        now = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)

        for index, event_type in enumerate(
                ("operator.field_lock.v1", "operator.field_unlock.v1", "operator.field_lock.v1")
        ):
                event_time = now + timedelta(minutes=index)
                write_event(
                        build_event_envelope(
                                program_id="acme",
                                event_type=event_type,
                                occurred_at=event_time,
                                recorded_at=event_time,
                                temporal_confidence=TemporalConfidence.EXACT,
                                confidence=ConfidenceTier.OPERATOR_CONFIRMED,
                                actor="test-operator",
                                payload={
                                        "entity_id": "milestone:m1",
                                        "field": "target_date",
                                        "override_session_id": "session-2",
                                },
                                source_ref=OperatorAssertionRef(asserted_by="test-operator", asserted_at=event_time, context=event_type),
                                dedupe_payload={"entity_id": "milestone:m1", "field": "target_date", "override_session_id": f"{event_type}:session-2:{index}"},
                        ),
                        programs_root=programs_root,
                )

        check = ledger_health_check("acme", programs_root)

        assert check is not None
        assert check.status == "ok"


def test_candidate_queue_backlog_check_warns_on_old_active_candidates(tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        now = datetime.now(timezone.utc)
        append_candidate(
                CandidateEvent(
                        candidate_id="cand-stale",
                        program_id="acme",
                        proposed_event_type="milestone.date_revised.v1",
                        proposed_payload={"milestone_id": "milestone:m1", "new_target_date": "2025-09-30"},
                        proposed_occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
                        proposed_temporal_confidence="approximate",
                        proposed_confidence="ai_extracted",
                        source_ref=LTDeckRef(
                                file_path="deck.pptx",
                                deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date(),
                                slide_number=9,
                        ),
                        pipeline="lt_deck",
                        extraction_confidence=0.9,
                        entity_resolution=(
                                CandidateEntityResolution(
                                        raw_name="Gen9",
                                        resolved_entity_id="milestone:m1",
                                        match_kind="exact",
                                        score=1.0,
                                ),
                        ),
                        dedupe_key="sha256:dedupe",
                        dedupe_core_hash="sha256:core",
                        source_document_key="lt_deck:deck.pptx:2025-03-20:9",
                        corroborating_refs=(),
                        batch_id="batch-stale",
                        staged_at=now - timedelta(days=15),
                ),
                programs_root=programs_root,
        )

        check = candidate_queue_backlog_check("acme", programs_root)

        assert check is not None
        assert check.status == "warn"
        assert "oldest staged 15 day(s) ago" in check.detail


def test_ledger_health_check_warns_on_expiring_field_locks(tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        now = datetime.now(timezone.utc)
        created = build_event_envelope(
                program_id="acme",
                event_type="milestone.created.v1",
                occurred_at=now - timedelta(days=1),
                recorded_at=now - timedelta(days=1),
                temporal_confidence=TemporalConfidence.EXACT,
                confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
                actor="import",
                payload={"milestone_id": "milestone:m1", "name": "GA", "target_date": "2026-09-30"},
                source_ref=LTDeckRef(file_path="deck.pptx", deck_date=(now - timedelta(days=1)).date(), slide_number=5),
        )
        locked = build_event_envelope(
                program_id="acme",
                event_type="operator.field_lock.v1",
                occurred_at=now,
                recorded_at=now,
                temporal_confidence=TemporalConfidence.EXACT,
                confidence=ConfidenceTier.OPERATOR_CONFIRMED,
                actor="test-operator",
                payload={
                        "entity_id": "milestone:m1",
                        "field": "target_date",
                        "locked_value": "2026-10-15",
                        "valid_until": (now + timedelta(days=5)).isoformat(),
                        "override_session_id": "session-expiring",
                },
                source_ref=OperatorAssertionRef(asserted_by="test-operator", asserted_at=now, context="operator.field_lock.v1"),
                dedupe_payload={"entity_id": "milestone:m1", "field": "target_date", "override_session_id": "lock:session-expiring"},
        )
        write_event(created, programs_root=programs_root)
        write_event(locked, programs_root=programs_root)
        project_program_events("acme", programs_root=programs_root)

        check = ledger_health_check("acme", programs_root)

        assert check is not None
        assert check.status == "warn"
        assert "expiring within 7d" in check.detail
        assert "milestone:m1.target_date" in check.detail


def test_ledger_health_check_warns_on_expired_field_locks_in_current_projection(tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        now = datetime.now(timezone.utc)
        created = build_event_envelope(
                program_id="acme",
                event_type="milestone.created.v1",
                occurred_at=now - timedelta(days=3),
                recorded_at=now - timedelta(days=3),
                temporal_confidence=TemporalConfidence.EXACT,
                confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
                actor="import",
                payload={"milestone_id": "milestone:m2", "name": "Preview", "target_date": "2026-08-01"},
                source_ref=LTDeckRef(file_path="deck2.pptx", deck_date=(now - timedelta(days=3)).date(), slide_number=6),
        )
        locked = build_event_envelope(
                program_id="acme",
                event_type="operator.field_lock.v1",
                occurred_at=now - timedelta(days=2),
                recorded_at=now - timedelta(days=2),
                temporal_confidence=TemporalConfidence.EXACT,
                confidence=ConfidenceTier.OPERATOR_CONFIRMED,
                actor="test-operator",
                payload={
                        "entity_id": "milestone:m2",
                        "field": "target_date",
                        "locked_value": "2026-08-15",
                        "valid_until": (now - timedelta(days=1)).isoformat(),
                        "override_session_id": "session-expired",
                },
                source_ref=OperatorAssertionRef(asserted_by="test-operator", asserted_at=now - timedelta(days=2), context="operator.field_lock.v1"),
                dedupe_payload={"entity_id": "milestone:m2", "field": "target_date", "override_session_id": "lock:session-expired"},
        )
        write_event(created, programs_root=programs_root)
        write_event(locked, programs_root=programs_root)
        project_program_events("acme", programs_root=programs_root)

        check = ledger_health_check("acme", programs_root)

        assert check is not None
        assert check.status == "warn"
        assert "expired field lock(s) still present" in check.detail
        assert "milestone:m2.target_date" in check.detail


def test_ledger_health_check_warns_on_unacknowledged_gaps(tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        now = datetime.now(timezone.utc)
        gap_event = build_event_envelope(
                program_id="acme",
                event_type="pipeline.gap_detected.v1",
                occurred_at=now - timedelta(hours=3),
                recorded_at=now - timedelta(hours=3),
                temporal_confidence=TemporalConfidence.EXACT,
                confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
                actor="workiq_pipeline",
                payload={
                        "pipeline": "workiq",
                        "gap_kind": "null_ids",
                        "window_start": (now - timedelta(days=7)).date().isoformat(),
                        "window_end": now.date().isoformat(),
                        "detail": "weekly yield [0,0,0]",
                },
                source_ref=LTDeckRef(file_path="deck-gap.pptx", deck_date=now.date(), slide_number=8),
        )
        write_event(gap_event, programs_root=programs_root)
        project_program_events("acme", programs_root=programs_root)

        check = ledger_health_check("acme", programs_root)

        assert check is not None
        assert check.status == "warn"
        assert "unacknowledged ledger gap" in check.detail
        assert "workiq:null_ids" in check.detail


def test_ledger_health_check_warns_on_projection_schema_drift(tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        now = datetime.now(timezone.utc)
        event = build_event_envelope(
                program_id="acme",
                event_type="pipeline.gap_detected.v1",
                occurred_at=now,
                recorded_at=now,
                temporal_confidence=TemporalConfidence.EXACT,
                confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
                actor="workiq_pipeline",
                payload={"pipeline": "workiq", "gap_kind": "null_ids", "detail": "weekly yield [0,0,0]"},
                source_ref=LTDeckRef(file_path="deck-schema.pptx", deck_date=now.date(), slide_number=3),
        )
        write_event(event, programs_root=programs_root)
        project_program_events("acme", programs_root=programs_root)
        with sqlite3.connect(get_current_projection_path("acme", programs_root=programs_root)) as connection:
                connection.execute("UPDATE projection_meta SET schema_version = ?", ("0",))
                connection.commit()

        check = ledger_health_check("acme", programs_root)

        assert check is not None
        assert check.status == "warn"
        assert "schema version 0 does not match engine version" in check.detail


def test_ledger_health_check_fails_on_external_origin_ref_without_vault_hash(tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        now = datetime.now(timezone.utc)
        written = write_event(
                build_event_envelope(
                program_id="acme",
                event_type="risk.raised.v1",
                occurred_at=now,
                recorded_at=now,
                temporal_confidence=TemporalConfidence.EXACT,
                confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
                actor="import",
                payload={
                        "risk_id": "risk:r1",
                        "title": "Mailbox regression",
                        "severity": "high",
                        "likelihood": "medium",
                        "status": "open",
                },
                source_ref=EmailRef(
                        subject="Escalation",
                        sent_at=now,
                        sender="owner@example.com",
                        message_id="msg-1",
                        vault_hash="sha256:msg-1",
                ),
                ),
                programs_root=programs_root,
        )
        record = written.envelope.to_dict()
        record["source_ref"]["vault_hash"] = None
        written.path.write_text(canonical_json(record) + "\n", encoding="utf-8")

        check = ledger_health_check("acme", programs_root)

        assert check is not None
        assert check.status == "fail"
        assert "missing required vault_hash" in check.detail
        assert "risk.raised.v1:source_ref" in check.detail


def test_ledger_health_check_warns_on_stale_operator_assertion_without_ttl(tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        recorded_at = datetime.now(timezone.utc) - timedelta(days=181)
        write_event(
                build_event_envelope(
                        program_id="acme",
                        event_type="risk.raised.v1",
                        occurred_at=recorded_at,
                        recorded_at=recorded_at,
                        temporal_confidence=TemporalConfidence.EXACT,
                        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
                        actor="test-operator",
                        payload={
                                "risk_id": "risk:r2",
                                "title": "Operator concern",
                                "severity": "medium",
                        },
                        source_ref=OperatorAssertionRef(asserted_by="test-operator", asserted_at=recorded_at),
                ),
                programs_root=programs_root,
        )

        check = ledger_health_check("acme", programs_root)

        assert check is not None
        assert check.status == "warn"
        assert "stale operator assertion(s) missing TTL" in check.detail
        assert "risk.raised.v1" in check.detail


def test_claim_freshness_check_warns_on_latest_issue_stale_claims(tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        archive_root = tmp_path / "archive"
        append_proposal(
                SectionRevisionProposal(
                        proposal_id="proposal-1",
                        edition_id="acme_weekly",
                        issue_number=78,
                        section_id="ws_alpha",
                        current_text="Current",
                        proposed_text="Proposed",
                        evidence_brief=SectionEvidenceBrief(
                                section_id="ws_alpha",
                                ado_delta_summary="delta",
                                new_items=(),
                                closed_items=(),
                                risk_changed_items=(),
                                eta_changed_items=(),
                                top_signals=(),
                                kpi_summary=None,
                                stale_claims=("claim-stale-1",),
                                vitality_summary="vital",
                                confidence=Confidence.HIGH,
                        ),
                        status=SectionRevisionStatus.PENDING,
                        generated_at=datetime.now(timezone.utc),
                ),
                program_id="acme",
                issue_number=78,
                programs_root=programs_root,
        )

        check = claim_freshness_check("acme", EDITION_NAME, archive_root, programs_root)

        assert check is not None
        assert check.status == "warn"
        assert "issue 078 cites 1 stale claim(s)" in check.detail
        assert "claim-stale-1" in check.detail


def test_claim_freshness_check_prefers_latest_confirmed_issue_archive_evidence(tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        archive_root = tmp_path / "archive"
        edition_root = get_archive_root(EDITION_NAME, archive_root=archive_root)
        edition_root.mkdir(parents=True, exist_ok=True)
        (edition_root / "index.json").write_text(
                json.dumps(
                        {
                                "edition": EDITION_NAME,
                                "issues": [
                                        {
                                                "issue_number": 79,
                                                "generated_at": "2026-06-11T12:00:00+00:00",
                                                "kind": "confirmed",
                                                "snapshot_path": None,
                                                "manifest_path": None,
                                        }
                                ],
                        },
                        indent=2,
                ),
                encoding="utf-8",
        )
        archive_narratives_dir = edition_root / "narratives" / "issue_079"
        archive_narratives_dir.mkdir(parents=True, exist_ok=True)
        write_accepted_proposals_archive(
                (
                        SectionRevisionProposal(
                                proposal_id="proposal-archive-1",
                                edition_id=EDITION_NAME,
                                issue_number=79,
                                section_id="ws_alpha",
                                current_text="Current",
                                proposed_text="Proposed",
                                evidence_brief=SectionEvidenceBrief(
                                        section_id="ws_alpha",
                                        ado_delta_summary="delta",
                                        new_items=(),
                                        closed_items=(),
                                        risk_changed_items=(),
                                        eta_changed_items=(),
                                        top_signals=(),
                                        kpi_summary=None,
                                        stale_claims=("claim-confirmed-1",),
                                        vitality_summary="vital",
                                        confidence=Confidence.HIGH,
                                ),
                                status=SectionRevisionStatus.ACCEPTED,
                                generated_at=datetime.now(timezone.utc),
                        ),
                ),
                archive_narratives_dir,
        )

        check = claim_freshness_check("acme", EDITION_NAME, archive_root, programs_root)

        assert check is not None
        assert check.status == "warn"
        assert "Latest confirmed issue 079 cites 1 stale claim(s)" in check.detail
        assert "claim-confirmed-1" in check.detail


def test_claim_freshness_check_accepts_latest_confirmed_issue_archive_without_stale_claims(tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        archive_root = tmp_path / "archive"
        edition_root = get_archive_root(EDITION_NAME, archive_root=archive_root)
        edition_root.mkdir(parents=True, exist_ok=True)
        (edition_root / "index.json").write_text(
                json.dumps(
                        {
                                "edition": EDITION_NAME,
                                "issues": [
                                        {
                                                "issue_number": 79,
                                                "generated_at": "2026-06-11T12:00:00+00:00",
                                                "kind": "confirmed",
                                                "snapshot_path": None,
                                                "manifest_path": None,
                                        }
                                ],
                        },
                        indent=2,
                ),
                encoding="utf-8",
        )
        archive_narratives_dir = edition_root / "narratives" / "issue_079"
        archive_narratives_dir.mkdir(parents=True, exist_ok=True)
        write_accepted_proposals_archive(
                (
                        SectionRevisionProposal(
                                proposal_id="proposal-archive-2",
                                edition_id=EDITION_NAME,
                                issue_number=79,
                                section_id="ws_alpha",
                                current_text="Current",
                                proposed_text="Proposed",
                                evidence_brief=SectionEvidenceBrief(
                                        section_id="ws_alpha",
                                        ado_delta_summary="delta",
                                        new_items=(),
                                        closed_items=(),
                                        risk_changed_items=(),
                                        eta_changed_items=(),
                                        top_signals=(),
                                        kpi_summary=None,
                                        stale_claims=(),
                                        vitality_summary="vital",
                                        confidence=Confidence.HIGH,
                                ),
                                status=SectionRevisionStatus.ACCEPTED,
                                generated_at=datetime.now(timezone.utc),
                        ),
                ),
                archive_narratives_dir,
        )

        check = claim_freshness_check("acme", EDITION_NAME, archive_root, programs_root)

        assert check is not None
        assert check.status == "ok"
        assert "Latest confirmed issue 079 has no stale claim citations in archived accepted proposal evidence." in check.detail


def test_coverage_range_check_warns_when_projection_begins_long_after_program_start(tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        program_dir = programs_root / "acme"
        _write_platform_program(
                program_dir,
                name="Acme",
                edition_name=EDITION_NAME,
                current_phase="Execution",
                start_date="2026-01-01",
        )
        recorded_at = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
        write_event(
                build_event_envelope(
                        program_id="acme",
                        event_type="risk.raised.v1",
                        occurred_at=recorded_at,
                        recorded_at=recorded_at,
                        temporal_confidence=TemporalConfidence.EXACT,
                        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
                        actor="import",
                        payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
                        source_ref=LTDeckRef(file_path="deck.pptx", deck_date=recorded_at.date(), slide_number=3),
                ),
                programs_root=programs_root,
        )
        project_program_events("acme", programs_root=programs_root)

        check = coverage_range_check("acme", {"start_date": "2026-01-01"}, programs_root)

        assert check is not None
        assert check.status == "warn"
        assert "Coverage earliest 2026-04-15 trails program start date 2026-01-01 by 104 day(s)" in check.detail


def test_coverage_range_check_accepts_projection_close_to_program_start(tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        program_dir = programs_root / "acme"
        _write_platform_program(
                program_dir,
                name="Acme",
                edition_name=EDITION_NAME,
                current_phase="Execution",
                start_date="2026-01-01",
        )
        recorded_at = datetime(2026, 2, 15, 12, 0, tzinfo=timezone.utc)
        write_event(
                build_event_envelope(
                        program_id="acme",
                        event_type="risk.raised.v1",
                        occurred_at=recorded_at,
                        recorded_at=recorded_at,
                        temporal_confidence=TemporalConfidence.EXACT,
                        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
                        actor="import",
                        payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
                        source_ref=LTDeckRef(file_path="deck.pptx", deck_date=recorded_at.date(), slide_number=3),
                ),
                programs_root=programs_root,
        )
        project_program_events("acme", programs_root=programs_root)

        check = coverage_range_check("acme", {"start_date": "2026-01-01"}, programs_root)

        assert check is not None
        assert check.status == "ok"
        assert "Coverage earliest 2026-02-15 is within 60 day(s) of program start date 2026-01-01." in check.detail


def test_degraded_confirm_check_fails_when_latest_confirmed_issue_lacks_ledger_snapshot_artifacts(tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        archive_root = tmp_path / "archive"
        edition_root = get_archive_root(EDITION_NAME, archive_root=archive_root)
        edition_root.mkdir(parents=True, exist_ok=True)
        (edition_root / "index.json").write_text(
                json.dumps(
                        {
                                "edition": EDITION_NAME,
                                "issues": [
                                        {
                                                "issue_number": 78,
                                                "generated_at": "2026-06-11T12:00:00+00:00",
                                                "kind": "confirmed",
                                                "snapshot_path": None,
                                                "manifest_path": None,
                                        }
                                ],
                        },
                        indent=2,
                ),
                encoding="utf-8",
        )

        check = degraded_confirm_check("acme", EDITION_NAME, archive_root, programs_root)

        assert check is not None
        assert check.status == "fail"
        assert "issue 078 is missing ledger post-confirm artifact(s)" in check.detail
        assert "projection snapshot" in check.detail
        assert "baseline hardlock event" in check.detail


def test_degraded_confirm_check_accepts_matching_snapshot_and_hardlock(tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        archive_root = tmp_path / "archive"
        edition_root = get_archive_root(EDITION_NAME, archive_root=archive_root)
        edition_root.mkdir(parents=True, exist_ok=True)
        (edition_root / "index.json").write_text(
                json.dumps(
                        {
                                "edition": EDITION_NAME,
                                "issues": [
                                        {
                                                "issue_number": 78,
                                                "generated_at": "2026-06-11T12:00:00+00:00",
                                                "kind": "confirmed",
                                                "snapshot_path": None,
                                                "manifest_path": None,
                                        }
                                ],
                        },
                        indent=2,
                ),
                encoding="utf-8",
        )
        recorded_at = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)
        domain_event = build_event_envelope(
                program_id="acme",
                event_type="risk.raised.v1",
                occurred_at=recorded_at,
                recorded_at=recorded_at,
                temporal_confidence=TemporalConfidence.EXACT,
                confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
                actor="import",
                payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
                source_ref=LTDeckRef(file_path="deck.pptx", deck_date=recorded_at.date(), slide_number=3),
        )
        write_event(domain_event, programs_root=programs_root)
        projection_result = project_program_events("acme", programs_root=programs_root)
        snapshot_paths = write_projection_snapshot(
                "acme",
                78,
                projection_result,
                events=(domain_event,),
                programs_root=programs_root,
        )
        hardlock_event = build_baseline_hardlock_event(
                "acme",
                78,
                snapshot_paths,
                projection_result,
                source_ref=OperatorAssertionRef(asserted_by="test-operator", asserted_at=recorded_at),
                actor="test-operator",
                recorded_at=recorded_at,
        )
        write_event(hardlock_event, programs_root=programs_root)

        check = degraded_confirm_check("acme", EDITION_NAME, archive_root, programs_root)

        assert check is not None
        assert check.status == "ok"
        assert "issue 078 has a ledger projection snapshot and matching baseline hardlock event" in check.detail


def test_run_doctor_operator_gates_surfaces_live_frontier(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        knowledge_root = reports_root.parent / "knowledge"
        reality_db_root = tmp_path / "reality-db"
        reset_overrides_to_seed_state(reports_root)

        golden_queries_path = knowledge_root / "golden_queries.yaml"
        golden_queries_doc = yaml.safe_load(golden_queries_path.read_text(encoding="utf-8"))
        for query in golden_queries_doc["queries"]:
                if query.get("id") == "icm-mttr":
                        query["validated"] = False
                        break
        golden_queries_path.write_text(
                yaml.safe_dump(golden_queries_doc, sort_keys=False, allow_unicode=False),
                encoding="utf-8",
        )

        archive_dir = get_archive_root(EDITION_NAME, archive_root)
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / "index.json").write_text(
                json.dumps(
                        {
                                "edition": EDITION_NAME,
                                "issues": [
                                        {
                                                "issue_number": 1,
                                                "generated_at": "2026-05-25T12:00:00+00:00",
                                                "kind": "confirmed",
                                                "snapshot_path": str(archive_dir / "issue_001.snapshot.json"),
                                        }
                                ],
                        }
                ),
                encoding="utf-8",
        )

        save_trusted_baseline(
                EDITION_NAME,
                TrustedBaseline(
                        schema_version="1.0",
                        edition=EDITION_NAME,
                        trusted_issue_number=1,
                        established_at=datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc),
                        established_by="tester",
                        history=(
                                TrustedBaselineHistoryEntry(
                                        issue=1,
                                        at=datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc),
                                        by="tester",
                                        action="established",
                                ),
                        ),
                ),
                editions_root=reports_root.parent / "editions",
                programs_root=programs_root,
        )

        upsert_m365_registry_artifacts(
                "acme",
                artifacts=(
                        M365RegistryArtifact(
                                artifact_id="meet:acme-weekly-ops-review",
                                artifact_type="meeting_series",
                                display_name="Acme Weekly Ops Review",
                                inferred_workstream="acme",
                                confidence=0.95,
                                confidence_source="pm_confirmed",
                                pm_confirmed=True,
                                promoted_to_workstreams_yaml=False,
                                first_seen=date(2026, 5, 1),
                                last_seen=date(2026, 5, 10),
                        ),
                ),
                programs_root=programs_root,
        )

        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 28, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=1,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=3,
                archived_journal_files=0,
                background_proposals=0,
                integration_errors=1,
                gather_flags={"kusto": True, "workiq": True, "icm": False},
                channels={
                        "ado": {
                                "active": True,
                                "signal_count": 8,
                                "expected_min": 1,
                                "meets_expected_min": True,
                                "last_error": None,
                        },
                        "kusto": {
                                "active": True,
                                "signal_count": 12,
                                "expected_min": 10,
                                "meets_expected_min": True,
                                "reason_not_active": None,
                                "last_error": None,
                        },
                        "workiq": {
                                "active": True,
                                "signal_count": 6,
                                "expected_min": 8,
                                "meets_expected_min": False,
                                "reason_not_active": None,
                                "last_error": None,
                        },
                        "transcript": {
                                "active": True,
                                "signal_count": 0,
                                "expected_min": 1,
                                "meets_expected_min": False,
                                "configured_series": 3,
                                "series_id_null": 3,
                                "reason_not_active": None,
                                "last_error": "WorkIQ transcript lookup timed out.",
                        },
                        "icm": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed", "last_error": None},
                },
                m365_discovery={
                        "active": True,
                        "registry_bootstrapped": True,
                        "first_discovery_completed_at": "2026-05-28T18:00:00+00:00",
                        "signals_without_workstream": 0,
                        "chat_thread_id_null": 0,
                        "promotion_blocked_missing_id_count": 1,
                },
                programs_root=programs_root,
        )

        monkeypatch.setenv("GRAPH_TENANT_ID", "tenant-id")
        monkeypatch.setenv("GRAPH_CLIENT_ID", "client-id")
        monkeypatch.setattr("src.commands.doctor.find_spec", lambda module_name: object() if module_name in {"azure.identity", "requests"} else None)

        class _ReadyAgencyBridge:
                def probe(self) -> AgencyCapabilities:
                        return AgencyCapabilities(
                                available=True,
                                has_workiq=True,
                                has_bluebird=True,
                                has_ado=True,
                                has_icm=True,
                                tier="msft",
                                server_tools={"workiq": ("ask_work_iq", "get_meetings", "search_emails", "get_transcript")},
                        )

        monkeypatch.setattr("src.commands.doctor.AgencyBridge", _ReadyAgencyBridge)

        report = run_doctor(
                edition_name=EDITION_NAME,
                operator_gates=True,
                reports_root=reports_root,
                archive_root=archive_root,
                editions_root=reports_root.parent / "editions",
                programs_root=programs_root,
                reality_db_root=reality_db_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
                kusto_probe=lambda queries: None,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Operator Gates"].status == "fail"
        assert checks["Operator Gates"].metadata is not None
        assert checks["Operator Gates"].metadata["blocking_gate_labels"] == [
                "Gate:M365 IDs",
                "Gate:Transcript Health",
                "Gate:Kusto Validation",
                "Gate:Checkpoint Creation",
                "Gate:Rollback Drill",
        ]
        assert checks["Gate:M365 IDs"].status == "fail"
        assert checks["Gate:M365 IDs"].metadata is not None
        assert checks["Gate:M365 IDs"].metadata["artifact_ids"] == ["meet:acme-weekly-ops-review"]
        assert checks["Gate:M365 IDs"].metadata["artifact_diagnostics"] == [
                {
                        "artifact_id": "meet:acme-weekly-ops-review",
                        "artifact_type": "meeting_series",
                        "inferred_workstream": "acme",
                        "status": "no_candidates_found",
                        "detail": "Latest active discovery completed but returned no durable-ID candidates.",
                }
        ]
        assert checks["Gate:M365 IDs"].metadata["action_category_counts"] == {"source-absent": 1}
        assert checks["Gate:M365 IDs"].metadata["artifact_action_categories"] == [
                {
                        "artifact_id": "meet:acme-weekly-ops-review",
                        "artifact_type": "meeting_series",
                        "category": "source-absent",
                        "next_command": "vertex integration explain-source --program acme --ref-id meet:acme-weekly-ops-review",
                        "intent_id": None,
                        "intent_status": None,
                        "derived_state": None,
                        "candidate_count": 0,
                        "best_candidate_confidence": None,
                }
        ]
        assert "completed discovery but still returned no durable-ID candidates" in checks["Gate:M365 IDs"].detail
        assert "Action categories: 1 source-absent." in checks["Gate:M365 IDs"].detail
        assert checks["Gate:Transcript Health"].status == "fail"
        assert checks["Gate:Transcript Health"].metadata is not None
        assert checks["Gate:Transcript Health"].metadata["action_category"] == "auth-admin-required"
        assert "missing series_id" in checks["Gate:Transcript Health"].detail
        assert "Action category: auth-admin-required." in checks["Gate:Transcript Health"].detail
        assert checks["Gate:Kusto Validation"].status == "fail"
        assert checks["Gate:Kusto Validation"].metadata is not None
        assert checks["Gate:Kusto Validation"].metadata["action_category"] == "pm-decision-required"
        assert "Action category: pm-decision-required." in checks["Gate:Kusto Validation"].detail
        assert checks["Gate:Checkpoint Creation"].status == "fail"
        assert checks["Gate:Checkpoint Creation"].metadata is not None
        assert checks["Gate:Checkpoint Creation"].metadata["action_category"] == "auto-resolvable"
        assert "No checkpoints found" in checks["Gate:Checkpoint Creation"].detail
        assert "Action category: auto-resolvable." in checks["Gate:Checkpoint Creation"].detail
        assert checks["Gate:Rollback Drill"].status == "fail"
        assert checks["Gate:Rollback Drill"].metadata is not None
        assert checks["Gate:Rollback Drill"].metadata["action_category"] == "auto-resolvable"
        assert "rollback_drill_passed" in checks["Gate:Rollback Drill"].detail
        assert "Action category: auto-resolvable." in checks["Gate:Rollback Drill"].detail


def test_operator_gate_m365_ids_classifies_pm_decision_required_from_pending_candidates(tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        program_dir = programs_root / "demo"
        program_dir.mkdir(parents=True, exist_ok=True)
        (program_dir / "workstreams.yaml").write_text(
                yaml.safe_dump(
                        {
                                "schema_version": "2.0",
                                "workstreams": [
                                        {
                                                "id": "ws_demo",
                                                "name": "Demo",
                                                "signal_sources": {
                                                        "teams_meeting_series": [{"display_name": "Demo Weekly", "series_id": None}],
                                                },
                                        }
                                ],
                        },
                        sort_keys=False,
                        allow_unicode=False,
                ),
                encoding="utf-8",
        )
        upsert_m365_registry_artifacts(
                "demo",
                artifacts=(
                        M365RegistryArtifact(
                                artifact_id="meet:demo-weekly",
                                artifact_type="meeting_series",
                                display_name="Demo Weekly",
                                series_id=None,
                                inferred_workstream="ws_demo",
                                confidence=1.0,
                                confidence_source="pm_confirmed",
                                pm_confirmed=True,
                                promoted_to_workstreams_yaml=True,
                                first_seen=date(2026, 5, 22),
                                last_seen=date(2026, 5, 22),
                        ),
                ),
                programs_root=programs_root,
                as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
        )
        store = SourceCandidateStore(program_dir / "channel_registry.sqlite3", "demo")
        intent = SourceIntent(
                intent_id=build_source_intent_id(
                        program_id="demo",
                        workstream_id="ws_demo",
                        ref_kind=SourceRefKind.MEETING_SERIES,
                        display_name="Demo Weekly",
                ),
                program_id="demo",
                workstream_id="ws_demo",
                ref_kind=SourceRefKind.MEETING_SERIES,
                display_name="Demo Weekly",
                normalized_name="demo weekly",
                status=SourceIntentStatus.AMBIGUOUS,
                created_at=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
                updated_at=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
                updated_by="test",
        )
        store.upsert_intent(intent)
        candidate_a = SourceCandidate(
                candidate_id="cand-a",
                program_id="demo",
                channel="teams",
                provider_instance_id="default",
                ref_id="series-a",
                ref_kind=SourceRefKind.MEETING_SERIES,
                display_name="Demo Weekly A",
                confidence=0.92,
                source_provider="workiq",
                status=SourceCandidateStatus.PENDING,
                evidence_json="{}",
                first_discovered_at=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
                last_seen_at=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
        )
        candidate_b = SourceCandidate(
                candidate_id="cand-b",
                program_id="demo",
                channel="teams",
                provider_instance_id="default",
                ref_id="series-b",
                ref_kind=SourceRefKind.MEETING_SERIES,
                display_name="Demo Weekly B",
                confidence=0.88,
                source_provider="workiq",
                status=SourceCandidateStatus.PENDING,
                evidence_json="{}",
                first_discovered_at=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
                last_seen_at=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
        )
        store.upsert_candidate(candidate_a, pii_prescrubbed=True)
        store.upsert_candidate(candidate_b, pii_prescrubbed=True)
        store.link_candidate_to_intent(candidate_a.candidate_id, intent.intent_id, 0.92)
        store.link_candidate_to_intent(candidate_b.candidate_id, intent.intent_id, 0.88)

        check = _operator_gate_m365_ids_check(
                program_id="demo",
                programs_root=programs_root,
                edition_name="demo",
                registry_review={
                        "missing_id_ids": ["meet:demo-weekly"],
                        "missing_id_artifacts": [
                                {
                                        "artifact_id": "meet:demo-weekly",
                                        "artifact_type": "meeting_series",
                                        "inferred_workstream": "ws_demo",
                                }
                        ],
                },
                m365_discovery={"active": True, "first_discovery_completed_at": "2026-05-22T12:00:00Z"},
                agency_caps=AgencyCapabilities(available=True, has_workiq=True, server_tools={"workiq": ("get_meetings",)}),
        )

        assert check.status == "fail"
        assert check.metadata is not None
        assert check.metadata["action_category_counts"] == {"pm-decision-required": 1}
        assert check.metadata["artifact_action_categories"][0]["category"] == "pm-decision-required"
        assert check.metadata["artifact_action_categories"][0]["intent_id"] == intent.intent_id
        assert check.metadata["artifact_action_categories"][0]["candidate_count"] == 2
        assert check.metadata["artifact_action_categories"][0]["best_candidate_confidence"] == 0.92
        assert "Action categories: 1 pm-decision-required." in check.detail


def test_operator_gate_m365_ids_classifies_completed_zero_yield_intent_as_operator_seed_required(tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        program_dir = programs_root / "demo"
        program_dir.mkdir(parents=True, exist_ok=True)
        (program_dir / "workstreams.yaml").write_text(
                yaml.safe_dump(
                        {
                                "schema_version": "2.0",
                                "workstreams": [
                                        {
                                                "id": "ws_demo",
                                                "name": "Demo",
                                                "signal_sources": {
                                                        "teams_meeting_series": [{"display_name": "Demo Weekly", "series_id": None}],
                                                },
                                        }
                                ],
                        },
                        sort_keys=False,
                        allow_unicode=False,
                ),
                encoding="utf-8",
        )
        upsert_m365_registry_artifacts(
                "demo",
                artifacts=(
                        M365RegistryArtifact(
                                artifact_id="meet:demo-weekly",
                                artifact_type="meeting_series",
                                display_name="Demo Weekly",
                                series_id=None,
                                inferred_workstream="ws_demo",
                                confidence=1.0,
                                confidence_source="pm_confirmed",
                                pm_confirmed=True,
                                promoted_to_workstreams_yaml=True,
                                first_seen=date(2026, 6, 4),
                                last_seen=date(2026, 6, 4),
                        ),
                ),
                programs_root=programs_root,
                as_of=datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc),
        )
        store = SourceCandidateStore(program_dir / "channel_registry.sqlite3", "demo")
        intent = SourceIntent(
                intent_id=build_source_intent_id(
                        program_id="demo",
                        workstream_id="ws_demo",
                        ref_kind=SourceRefKind.MEETING_SERIES,
                        display_name="Demo Weekly",
                ),
                program_id="demo",
                workstream_id="ws_demo",
                ref_kind=SourceRefKind.MEETING_SERIES,
                display_name="Demo Weekly",
                normalized_name="demo weekly",
                status=SourceIntentStatus.DECLARED,
                created_at=datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc),
                updated_at=datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc),
                updated_by="test",
        )
        store.upsert_intent(intent)
        attempted_at = datetime(2026, 6, 4, 12, 5, tzinfo=timezone.utc)
        store.record_attempt(
                DiscoveryAttempt(
                        attempt_id=build_discovery_attempt_id(
                                program_id="demo",
                                intent_id=intent.intent_id,
                                source_provider="seeded_resolution",
                                query_hash="query",
                                attempted_at=attempted_at,
                        ),
                        program_id="demo",
                        intent_id=intent.intent_id,
                        workstream_id="ws_demo",
                        channel="teams",
                        provider_instance_id="default",
                        ref_kind=SourceRefKind.MEETING_SERIES,
                        source_provider="seeded_resolution",
                        query_hash="query",
                        config_hash="config",
                        autonomous_run_id=None,
                        outcome=DiscoveryAttemptOutcome.NO_CANDIDATES,
                        reason=None,
                        result_count=0,
                        duration_ms=10,
                        attempted_at=attempted_at,
                        expires_at=attempted_at + timedelta(hours=4),
                )
        )

        check = _operator_gate_m365_ids_check(
                program_id="demo",
                programs_root=programs_root,
                edition_name="demo",
                registry_review={
                        "missing_id_ids": ["meet:demo-weekly"],
                        "missing_id_artifacts": [
                                {
                                        "artifact_id": "meet:demo-weekly",
                                        "artifact_type": "meeting_series",
                                        "inferred_workstream": "ws_demo",
                                }
                        ],
                },
                m365_discovery={"active": True, "first_discovery_completed_at": "2026-06-04T12:05:00Z"},
                agency_caps=AgencyCapabilities(available=True, has_workiq=True, server_tools={"workiq": ("get_meetings",)}),
        )

        assert check.metadata is not None
        assert check.metadata["action_category_counts"] == {"operator-seed-required": 1}
        assert check.metadata["artifact_action_categories"] == [
                {
                        "artifact_id": "meet:demo-weekly",
                        "artifact_type": "meeting_series",
                        "category": "operator-seed-required",
                        "next_command": f"vertex integration seed-id --program demo --intent-id {intent.intent_id} --ref-id <series-or-thread-id> --pm-alias <alias>",
                        "intent_id": intent.intent_id,
                        "intent_status": "declared",
                        "derived_state": "no_candidates",
                        "candidate_count": 0,
                        "best_candidate_confidence": None,
                }
        ]


def test_operator_gate_m365_ids_degrades_cleanly_when_discovery_tables_are_missing(tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        program_dir = programs_root / "demo"
        program_dir.mkdir(parents=True, exist_ok=True)
        upsert_m365_registry_artifacts(
                "demo",
                artifacts=(
                        M365RegistryArtifact(
                                artifact_id="meet:demo-weekly",
                                artifact_type="meeting_series",
                                display_name="Demo Weekly",
                                series_id=None,
                                inferred_workstream="ws_demo",
                                confidence=1.0,
                                confidence_source="pm_confirmed",
                                pm_confirmed=True,
                                promoted_to_workstreams_yaml=True,
                                first_seen=date(2026, 5, 22),
                                last_seen=date(2026, 5, 22),
                        ),
                ),
                programs_root=programs_root,
                as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
        )
        registry_path = program_dir / "channel_registry.sqlite3"
        ChannelRegistryStore(registry_path, "demo")
        with sqlite3.connect(registry_path) as conn:
                conn.execute("DROP TABLE IF EXISTS source_intents")
                conn.execute("DROP TABLE IF EXISTS source_candidates")
                conn.execute("DROP TABLE IF EXISTS candidate_intent_matches")
                conn.execute("DROP TABLE IF EXISTS discovery_attempts")

        check = _operator_gate_m365_ids_check(
                program_id="demo",
                programs_root=programs_root,
                edition_name="demo",
                registry_review={
                        "missing_id_ids": ["meet:demo-weekly"],
                        "missing_id_artifacts": [
                                {
                                        "artifact_id": "meet:demo-weekly",
                                        "artifact_type": "meeting_series",
                                        "inferred_workstream": "ws_demo",
                                }
                        ],
                },
                m365_discovery={"active": True, "first_discovery_completed_at": "2026-05-22T12:00:00Z"},
                agency_caps=AgencyCapabilities(available=True, has_workiq=True, server_tools={"workiq": ("get_meetings",)}),
        )

        assert check.status == "fail"
        assert check.metadata is not None
        assert check.metadata["action_category_counts"] == {"auto-resolvable": 1}
        assert check.metadata["artifact_action_categories"] == [
                {
                        "artifact_id": "meet:demo-weekly",
                        "artifact_type": "meeting_series",
                        "category": "auto-resolvable",
                        "next_command": "vertex integration schema-migrate --program demo",
                        "intent_id": None,
                        "intent_status": None,
                        "derived_state": None,
                        "candidate_count": 0,
                        "best_candidate_confidence": None,
                }
        ]
        assert "Action categories: 1 auto-resolvable." in check.detail



def test_operator_gate_m365_ids_prioritizes_auth_blocker_over_missing_schema(tmp_path: Path) -> None:
        programs_root = tmp_path / "programs"
        program_dir = programs_root / "demo"
        program_dir.mkdir(parents=True, exist_ok=True)
        upsert_m365_registry_artifacts(
                "demo",
                artifacts=(
                        M365RegistryArtifact(
                                artifact_id="meet:demo-weekly",
                                artifact_type="meeting_series",
                                display_name="Demo Weekly",
                                series_id=None,
                                inferred_workstream="ws_demo",
                                confidence=1.0,
                                confidence_source="pm_confirmed",
                                pm_confirmed=True,
                                promoted_to_workstreams_yaml=True,
                                first_seen=date(2026, 5, 22),
                                last_seen=date(2026, 5, 22),
                        ),
                ),
                programs_root=programs_root,
                as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
        )
        registry_path = program_dir / "channel_registry.sqlite3"
        ChannelRegistryStore(registry_path, "demo")
        with sqlite3.connect(registry_path) as conn:
                conn.execute("DROP TABLE IF EXISTS source_intents")
                conn.execute("DROP TABLE IF EXISTS source_candidates")
                conn.execute("DROP TABLE IF EXISTS candidate_intent_matches")
                conn.execute("DROP TABLE IF EXISTS discovery_attempts")

        check = _operator_gate_m365_ids_check(
                program_id="demo",
                programs_root=programs_root,
                edition_name="demo",
                registry_review={
                        "missing_id_ids": ["meet:demo-weekly"],
                        "missing_id_artifacts": [
                                {
                                        "artifact_id": "meet:demo-weekly",
                                        "artifact_type": "meeting_series",
                                        "inferred_workstream": "ws_demo",
                                }
                        ],
                },
                m365_discovery={"active": True, "first_discovery_completed_at": "2026-05-22T12:00:00Z"},
                agency_caps=AgencyCapabilities(available=True, has_workiq=False, has_workiq_cli=False, server_tools={"workiq": ()}),
        )

        assert check.status == "fail"
        assert check.metadata is not None
        assert check.metadata["action_category_counts"] == {"auth-admin-required": 1}
        assert check.metadata["artifact_action_categories"] == [
                {
                        "artifact_id": "meet:demo-weekly",
                        "artifact_type": "meeting_series",
                        "category": "auth-admin-required",
                        "next_command": "vertex doctor --operator-gates --edition <name>",
                        "intent_id": None,
                        "intent_status": None,
                        "derived_state": None,
                        "candidate_count": 0,
                        "best_candidate_confidence": None,
                }
        ]
        assert "Action categories: 1 auth-admin-required." in check.detail


def test_doctor_cli_operator_gates(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        knowledge_root = reports_root.parent / "knowledge"
        reset_overrides_to_seed_state(reports_root)

        golden_queries_path = knowledge_root / "golden_queries.yaml"
        golden_queries_doc = yaml.safe_load(golden_queries_path.read_text(encoding="utf-8"))
        for query in golden_queries_doc["queries"]:
                if query.get("id") == "icm-mttr":
                        query["validated"] = False
                        break
        golden_queries_path.write_text(
                yaml.safe_dump(golden_queries_doc, sort_keys=False, allow_unicode=False),
                encoding="utf-8",
        )

        archive_dir = get_archive_root(EDITION_NAME, archive_root)
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / "index.json").write_text(
                json.dumps(
                        {
                                "edition": EDITION_NAME,
                                "issues": [
                                        {
                                                "issue_number": 1,
                                                "generated_at": "2026-05-25T12:00:00+00:00",
                                                "kind": "confirmed",
                                                "snapshot_path": str(archive_dir / "issue_001.snapshot.json"),
                                        }
                                ],
                        }
                ),
                encoding="utf-8",
        )

        save_trusted_baseline(
                EDITION_NAME,
                TrustedBaseline(
                        schema_version="1.0",
                        edition=EDITION_NAME,
                        trusted_issue_number=1,
                        established_at=datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc),
                        established_by="tester",
                        history=(
                                TrustedBaselineHistoryEntry(
                                        issue=1,
                                        at=datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc),
                                        by="tester",
                                        action="established",
                                ),
                        ),
                ),
                editions_root=reports_root.parent / "editions",
                programs_root=programs_root,
        )

        upsert_m365_registry_artifacts(
                "acme",
                artifacts=(
                        M365RegistryArtifact(
                                artifact_id="meet:acme-weekly-ops-review",
                                artifact_type="meeting_series",
                                display_name="Acme Weekly Ops Review",
                                inferred_workstream="acme",
                                confidence=0.95,
                                confidence_source="pm_confirmed",
                                pm_confirmed=True,
                                promoted_to_workstreams_yaml=False,
                                first_seen=date(2026, 5, 1),
                                last_seen=date(2026, 5, 10),
                        ),
                ),
                programs_root=programs_root,
        )

        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 28, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=1,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=3,
                archived_journal_files=0,
                background_proposals=0,
                integration_errors=1,
                gather_flags={"kusto": True, "workiq": True, "icm": False},
                channels={
                        "ado": {"active": True, "signal_count": 8, "expected_min": 1, "meets_expected_min": True, "last_error": None},
                        "kusto": {"active": True, "signal_count": 12, "expected_min": 10, "meets_expected_min": True, "reason_not_active": None, "last_error": None},
                        "workiq": {"active": True, "signal_count": 6, "expected_min": 8, "meets_expected_min": False, "reason_not_active": None, "last_error": None},
                        "transcript": {"active": True, "signal_count": 0, "expected_min": 1, "meets_expected_min": False, "configured_series": 3, "series_id_null": 3, "reason_not_active": None, "last_error": "WorkIQ transcript lookup timed out."},
                        "icm": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed", "last_error": None},
                },
                m365_discovery={
                        "active": True,
                        "registry_bootstrapped": True,
                        "first_discovery_completed_at": "2026-05-28T18:00:00+00:00",
                        "signals_without_workstream": 0,
                        "chat_thread_id_null": 0,
                        "promotion_blocked_missing_id_count": 1,
                },
                programs_root=programs_root,
        )

        monkeypatch.setenv("GRAPH_TENANT_ID", "tenant-id")
        monkeypatch.setenv("GRAPH_CLIENT_ID", "client-id")
        monkeypatch.setattr("src.commands.doctor.REPORTS_ROOT", reports_root)
        monkeypatch.setattr("src.commands.doctor.ARCHIVE_ROOT", archive_root)
        monkeypatch.setattr("src.commands.doctor.find_spec", lambda module_name: object() if module_name in {"azure.identity", "requests"} else None)
        monkeypatch.setattr(
                "src.commands.doctor._probe_ado_access",
                lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        class _ReadyAgencyBridge:
                def probe(self) -> AgencyCapabilities:
                        return AgencyCapabilities(
                                available=True,
                                has_workiq=True,
                                has_bluebird=True,
                                has_ado=True,
                                has_icm=True,
                                tier="msft",
                                server_tools={"workiq": ("ask_work_iq", "get_meetings", "search_emails", "get_transcript")},
                        )

        monkeypatch.setattr("src.commands.doctor.AgencyBridge", _ReadyAgencyBridge)
        monkeypatch.setattr("src.commands.doctor.build_live_kusto_query_probe", lambda **kwargs: (lambda _queries: None))

        result = runner.invoke(app, ["doctor", "--edition", EDITION_NAME, "--operator-gates"])

        assert result.exit_code == 1
        assert "Operator Gates" in result.stdout
        assert "Gate:M365 IDs" in result.stdout
        assert "Gate:Transcript Health" in result.stdout
        assert "Gate:Kusto Validation" in result.stdout
        assert "Gate:Checkpoint Creation" in result.stdout
        assert "Gate:Rollback Drill" in result.stdout


def test_run_doctor_platform_readiness_reports_provable_and_unproven_state(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        programs_root = reports_root.parent / "programs"
        editions_root = reports_root.parent / "editions"
        reset_overrides_to_seed_state(reports_root)

        armada_dir = programs_root / "fabrikam"
        _write_platform_program(
                armada_dir,
                name="Fabrikam",
                edition_name="fabrikam_weekly",
                current_phase="Buildout planning",
        )
        _write_platform_proof_log(
                armada_dir,
                proofs=[
                        {
                                "proof_id": "p4a_clean_machine",
                                "status": "passed",
                                "recorded_at": "2026-05-20T18:15:00+00:00",
                                "recorded_by": "operator",
                                "edition": "fabrikam_weekly",
                                "program_id": "fabrikam",
                                "elapsed_minutes": 14.0,
                                "no_code_changes": True,
                                "notes": "Clean-machine onboarding proof completed.",
                        }
                ],
        )

        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 28, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=1,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=3,
                archived_journal_files=0,
                background_proposals=0,
                integration_errors=1,
                gather_flags={"kusto": True, "workiq": True, "icm": False},
                channels={
                        "ado": {"active": True, "signal_count": 8, "expected_min": 1, "meets_expected_min": True, "last_error": None},
                        "kusto": {"active": True, "signal_count": 12, "expected_min": 10, "meets_expected_min": True, "reason_not_active": None, "last_error": None},
                        "workiq": {"active": True, "signal_count": 6, "expected_min": 8, "meets_expected_min": False, "reason_not_active": None, "last_error": None},
                        "transcript": {"active": True, "signal_count": 0, "expected_min": 2, "meets_expected_min": False, "configured_series": 3, "series_id_null": 3, "reason_not_active": None, "last_error": "WorkIQ transcript lookup timed out."},
                        "icm": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed", "last_error": None},
                        "teams": {"uil_enabled": True, "uil_health": "ok", "uil_registry_file_present": True, "uil_registry_size": 2},
                },
                programs_root=programs_root,
        )

        teams_timestamp = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
        append_signal(
                Signal(
                        id="teams-proof-signal",
                        timestamp=teams_timestamp,
                        source="teams",
                        program_id="acme",
                        workstream_id="acme",
                        entity_refs=(),
                        text="Direct Teams UIL signal proving adapter coverage.",
                        raw_ref="teams/message/teams-proof-signal",
                        confidence=Confidence.MEDIUM,
                        metadata={"channel": "teams"},
                        thread_id="teams-thread-1",
                        review_policy=ReviewPolicy.PENDING,
                ),
                programs_root=programs_root,
                partition_at=teams_timestamp,
        )

        report = run_doctor(
                platform_readiness=True,
                reports_root=reports_root,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Platform Readiness"].status == "fail"
        assert checks["PR:Fleet Active Programs"].status == "fail"
        assert "fabrikam" in checks["PR:Fleet Active Programs"].detail
        assert checks["PR:Adapter Coverage"].status == "fail"
        assert checks["PR:Adapter Coverage"].metadata is not None
        assert checks["PR:Adapter Coverage"].metadata["program_ids_by_adapter"]["Teams"] == ["acme"]
        assert checks["PR:Adapter Coverage"].metadata["missing_adapters"] == ["IcM"]
        assert checks["PR:Confirmed Program Channel Health"].status == "fail"
        assert checks["PR:P4a Clean-Machine Proof"].status == "ok"
        assert "fabrikam" in checks["PR:P4a Clean-Machine Proof"].detail
        assert checks["PR:P4b ADO-Only Proof"].status == "fail"
        assert checks["PR:P4c Multi-Source Proof"].status == "fail"
        assert checks["PR:P6b Archetype Proofs"].status == "fail"
        assert checks["PR:S7 Position"].status == "warn"


def test_run_doctor_platform_readiness_accepts_recorded_s7_deferral(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        programs_root = reports_root.parent / "programs"
        editions_root = reports_root.parent / "editions"
        reset_overrides_to_seed_state(reports_root)

        save_platform_s7_state(
                position="deferred",
                recorded_at=datetime(2026, 6, 2, 18, 0, tzinfo=timezone.utc),
                recorded_by="operator",
                justification="S7b remains outside the V-11 bar until explicit PM sign-off.",
                programs_root=programs_root,
        )

        report = _run_platform_readiness_doctor(
                reports_root=reports_root,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["PR:S7 Position"].status == "ok"
        assert checks["PR:S7 Position"].metadata is not None
        assert checks["PR:S7 Position"].metadata["position"] == "deferred"
        assert checks["PR:S7 Position"].metadata["machine_readable"] is True
        assert "explicitly deferred" in checks["PR:S7 Position"].detail


def test_doctor_cli_platform_readiness(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        programs_root = reports_root.parent / "programs"
        editions_root = reports_root.parent / "editions"
        reset_overrides_to_seed_state(reports_root)

        armada_dir = programs_root / "fabrikam"
        _write_platform_program(
                armada_dir,
                name="Fabrikam",
                edition_name="fabrikam_weekly",
                current_phase="Buildout planning",
        )
        _write_platform_proof_log(
                armada_dir,
                proofs=[
                        {
                                "proof_id": "p4a_clean_machine",
                                "status": "passed",
                                "recorded_at": "2026-05-20T18:15:00+00:00",
                                "recorded_by": "operator",
                                "edition": "fabrikam_weekly",
                                "program_id": "fabrikam",
                        }
                ],
        )

        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 28, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=1,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=3,
                archived_journal_files=0,
                background_proposals=0,
                integration_errors=1,
                gather_flags={"kusto": True, "workiq": True, "icm": False},
                channels={
                        "ado": {"active": True, "signal_count": 8, "expected_min": 1, "meets_expected_min": True, "last_error": None},
                        "kusto": {"active": True, "signal_count": 12, "expected_min": 10, "meets_expected_min": True, "reason_not_active": None, "last_error": None},
                        "workiq": {"active": True, "signal_count": 6, "expected_min": 8, "meets_expected_min": False, "reason_not_active": None, "last_error": None},
                        "transcript": {"active": True, "signal_count": 0, "expected_min": 2, "meets_expected_min": False, "configured_series": 3, "series_id_null": 3, "reason_not_active": None, "last_error": "WorkIQ transcript lookup timed out."},
                        "icm": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed", "last_error": None},
                        "teams": {"uil_enabled": True, "uil_health": "ok", "uil_registry_file_present": True, "uil_registry_size": 2},
                },
                programs_root=programs_root,
        )

        teams_timestamp = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
        append_signal(
                Signal(
                        id="teams-proof-signal",
                        timestamp=teams_timestamp,
                        source="teams",
                        program_id="acme",
                        workstream_id="acme",
                        entity_refs=(),
                        text="Direct Teams UIL signal proving adapter coverage.",
                        raw_ref="teams/message/teams-proof-signal",
                        confidence=Confidence.MEDIUM,
                        metadata={"channel": "teams"},
                        thread_id="teams-thread-1",
                        review_policy=ReviewPolicy.PENDING,
                ),
                programs_root=programs_root,
                partition_at=teams_timestamp,
        )

        monkeypatch.setattr("src.commands.doctor.REPORTS_ROOT", reports_root)
        monkeypatch.setattr("src.commands.doctor.EDITIONS_ROOT", editions_root)

        result = runner.invoke(app, ["doctor", "--platform-readiness"])

        assert result.exit_code == 1
        assert "Platform Readiness" in result.stdout
        assert "PR:Fleet Active Programs" in result.stdout
        assert "PR:Adapter Coverage" in result.stdout
        assert "PR:P4a Clean-Machine Proof" in result.stdout
        assert "PR:P6b Archetype Proofs" in result.stdout


def test_run_doctor_reports_template_contract_ok_for_staged_nova(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        monkeypatch.delenv("GRAPH_TENANT_ID", raising=False)
        monkeypatch.delenv("GRAPH_CLIENT_ID", raising=False)

        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        reset_overrides_to_seed_state(reports_root)

        report = run_doctor(
                edition_name=EDITION_NAME,
                reports_root=reports_root,
                archive_root=archive_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Template Contract"].status == "ok"
        assert "allows edition type 'detailed'" in checks["Template Contract"].detail


def test_run_doctor_reports_config_governance_ok_for_staged_nova(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        monkeypatch.delenv("GRAPH_TENANT_ID", raising=False)
        monkeypatch.delenv("GRAPH_CLIENT_ID", raising=False)

        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        report = run_doctor(
                edition_name=EDITION_NAME,
                reports_root=reports_root,
                archive_root=archive_root,
                editions_root=reports_root.parent / "editions",
                programs_root=programs_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        checks = {check.label: check for check in report.checks}
        governance = checks["Config Governance"]
        assert governance.status == "ok"
        assert "edition=2.0" in governance.detail
        assert governance.metadata is not None
        assert governance.metadata["assessments"]["program"]["version"] == "3.0"


def test_consistency_check_passes_when_baseline_archive_and_next_draft_align(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        editions_root = reports_root.parent / "editions"
        programs_root = reports_root.parent / "programs"
        archive_root = tmp_path / "archive"

        save_trusted_baseline(
                EDITION_NAME,
                TrustedBaseline(
                        schema_version="1.0",
                        edition=EDITION_NAME,
                        trusted_issue_number=1,
                        established_at=datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc),
                        established_by="tester",
                        history=(
                                TrustedBaselineHistoryEntry(
                                        issue=1,
                                        at=datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc),
                                        by="tester",
                                        action="established",
                                ),
                        ),
                ),
                editions_root=editions_root,
                programs_root=programs_root,
        )

        archive_dir = get_archive_root(EDITION_NAME, archive_root)
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / "index.json").write_text(
                json.dumps(
                        {
                                "edition": EDITION_NAME,
                                "issues": [
                                        {
                                                "issue_number": 1,
                                                "generated_at": "2026-05-25T12:00:00+00:00",
                                                "kind": "confirmed",
                                                "snapshot_path": str(archive_dir / "issue_001.snapshot.json"),
                                                "manifest_path": str(archive_dir / "issue_001.manifest.json"),
                                                "html_path": str(archive_dir / "issue_001.html"),
                                                "md_path": str(archive_dir / "issue_001.md"),
                                        }
                                ],
                        }
                ),
                encoding="utf-8",
        )
        review_dir = archive_dir / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        (review_dir / "issue_001.review.yaml").write_text("issue_number: 1\nsections: []\n", encoding="utf-8")

        save_review_status(
                EDITION_NAME,
                ReviewStatus(
                        issue_number=2,
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
                reports_root=reports_root,
        )

        check = _consistency_check(
                EDITION_NAME,
                archive_root=archive_root,
                reports_root=reports_root,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        assert check.status == "ok"
        assert "trusted baseline, archive, and review state agree on issue 001" in check.detail
        assert "active draft review is staged for issue 002" in check.detail
        assert check.metadata == {
                "trusted_issue_number": 1,
                "latest_confirmed_issue": 1,
                "latest_archived_review_issue": 1,
                "active_review_issue": 2,
        }


def test_consistency_check_fails_when_trusted_baseline_disagrees_with_archive(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        editions_root = reports_root.parent / "editions"
        programs_root = reports_root.parent / "programs"
        archive_root = tmp_path / "archive"

        save_trusted_baseline(
                EDITION_NAME,
                TrustedBaseline(
                        schema_version="1.0",
                        edition=EDITION_NAME,
                        trusted_issue_number=2,
                        established_at=datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc),
                        established_by="tester",
                        history=(
                                TrustedBaselineHistoryEntry(
                                        issue=2,
                                        at=datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc),
                                        by="tester",
                                        action="established",
                                ),
                        ),
                ),
                editions_root=editions_root,
                programs_root=programs_root,
        )

        archive_dir = get_archive_root(EDITION_NAME, archive_root)
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / "index.json").write_text(
                json.dumps(
                        {
                                "edition": EDITION_NAME,
                                "issues": [
                                        {
                                                "issue_number": 1,
                                                "generated_at": "2026-05-25T12:00:00+00:00",
                                                "kind": "confirmed",
                                                "snapshot_path": str(archive_dir / "issue_001.snapshot.json"),
                                                "manifest_path": str(archive_dir / "issue_001.manifest.json"),
                                                "html_path": str(archive_dir / "issue_001.html"),
                                                "md_path": str(archive_dir / "issue_001.md"),
                                        }
                                ],
                        }
                ),
                encoding="utf-8",
        )
        review_dir = archive_dir / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        (review_dir / "issue_001.review.yaml").write_text("issue_number: 1\nsections: []\n", encoding="utf-8")

        save_review_status(
                EDITION_NAME,
                ReviewStatus(issue_number=2, sections=()),
                reports_root=reports_root,
        )

        check = _consistency_check(
                EDITION_NAME,
                archive_root=archive_root,
                reports_root=reports_root,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        assert check.status == "fail"
        assert "trusted baseline issue 002 does not match latest confirmed archive issue 001" in check.detail
        assert check.metadata == {
                "trusted_issue_number": 2,
                "latest_confirmed_issue": 1,
                "latest_archived_review_issue": 1,
                "active_review_issue": 2,
        }


def test_run_doctor_config_governance_fails_on_program_schema_major_mismatch(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        monkeypatch.delenv("GRAPH_TENANT_ID", raising=False)
        monkeypatch.delenv("GRAPH_CLIENT_ID", raising=False)

        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        program_path = programs_root / "acme" / "program.yaml"
        program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        assert isinstance(program_document, dict)
        program_document["schema_version"] = "4.0"
        program_path.write_text(yaml.safe_dump(program_document, sort_keys=False), encoding="utf-8")

        report = run_doctor(
                edition_name=EDITION_NAME,
                reports_root=reports_root,
                archive_root=archive_root,
                editions_root=reports_root.parent / "editions",
                programs_root=programs_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        checks = {check.label: check for check in report.checks}
        governance = checks["Config Governance"]
        assert governance.status == "fail"
        assert "expected major version 3.x" in governance.detail


def test_run_doctor_config_governance_warns_on_minor_version_drift(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        monkeypatch.delenv("GRAPH_TENANT_ID", raising=False)
        monkeypatch.delenv("GRAPH_CLIENT_ID", raising=False)

        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        edition_path = programs_root / "acme" / "editions" / f"{EDITION_NAME}.yaml"
        edition_document = yaml.safe_load(edition_path.read_text(encoding="utf-8"))
        assert isinstance(edition_document, dict)
        edition_document["schema_version"] = "2.1"
        edition_path.write_text(yaml.safe_dump(edition_document, sort_keys=False), encoding="utf-8")

        report = run_doctor(
                edition_name=EDITION_NAME,
                reports_root=reports_root,
                archive_root=archive_root,
                programs_root=programs_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        checks = {check.label: check for check in report.checks}
        governance = checks["Config Governance"]
        assert governance.status == "warn"
        assert "expected baseline 2.0" in governance.detail


def test_run_doctor_fails_when_template_contract_disallows_edition_type(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        monkeypatch.delenv("GRAPH_TENANT_ID", raising=False)
        monkeypatch.delenv("GRAPH_CLIENT_ID", raising=False)

        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        template_contract_path = programs_root / "acme" / "template_contract.yaml"
        template_contract_doc = yaml.safe_load(template_contract_path.read_text(encoding="utf-8"))
        template_contract_doc["edition_family"]["default"] = "focused"
        template_contract_doc["edition_family"]["allowed"] = ["focused"]
        template_contract_doc["families"] = {
                "focused": template_contract_doc["families"]["focused"],
        }
        template_contract_path.write_text(
                yaml.safe_dump(template_contract_doc, sort_keys=False, allow_unicode=False),
                encoding="utf-8",
        )

        report = run_doctor(
                edition_name=EDITION_NAME,
                reports_root=reports_root,
                archive_root=archive_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Template Contract"].status == "fail"
        assert "does not allow edition type 'detailed'" in checks["Template Contract"].detail
        assert checks["Template Contract"].metadata is not None
        assert checks["Template Contract"].metadata["allowed_families"] == ["focused"]


def test_run_doctor_surfaces_latest_gather_integration_diagnostics(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        monkeypatch.delenv("GRAPH_TENANT_ID", raising=False)
        monkeypatch.delenv("GRAPH_CLIENT_ID", raising=False)

        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
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

        report = run_doctor(
                edition_name=EDITION_NAME,
                fix=True,
                reports_root=reports_root,
                archive_root=archive_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        gather_check = next(check for check in report.checks if check.label == "Gather")
        assert gather_check.status == "warn"
        assert "kusto/gather: kusto unavailable" in gather_check.detail
        assert "Run 'vertex admin auth setup'" in gather_check.detail
        assert gather_check.metadata is not None
        assert gather_check.metadata["integration_errors"] == 1
        assert gather_check.metadata["integration_error_details"] == [
                {
                        "source": "kusto",
                        "stage": "gather",
                        "retryable": True,
                        "message": "kusto unavailable",
                        "operator_action": "Run 'vertex admin auth setup' and verify Kusto cluster access before retrying gather.",
                }
        ]

        payload = _build_doctor_payload(report=report, tip=None)
        gather_payload = next(check for check in payload["checks"] if check["label"] == "Gather")
        assert gather_payload["metadata"] == gather_check.metadata

        csv_rows = list(csv.DictReader(render_doctor_output(payload, format="csv").splitlines()))
        gather_row = next(row for row in csv_rows if row["label"] == "Gather")
        assert json.loads(gather_row["metadata_json"]) == gather_check.metadata


def test_run_doctor_surfaces_runtime_slice_telemetry_sla_violations(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        monkeypatch.delenv("GRAPH_TENANT_ID", raising=False)
        monkeypatch.delenv("GRAPH_CLIENT_ID", raising=False)

        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=4,
                discovered_signals=2,
                new_signals=1,
                pending_review=1,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=3,
                archived_journal_files=0,
                background_proposals=0,
                query_states={
                        "velocity-p50": {
                                "last_succeeded_at": "2026-05-08T12:00:00+00:00",
                                "last_cycle_succeeded": True,
                                "row_count": 1,
                                "data_age_hours": 30.0,
                        }
                },
                programs_root=programs_root,
        )

        report = run_doctor(
                edition_name=EDITION_NAME,
                reports_root=reports_root,
                archive_root=archive_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Slice Telemetry"].status == "warn"
        assert "acme.deployment_velocity (velocity-p50, 30.0h > 24h)" in checks["Slice Telemetry"].detail
        assert checks["Slice Telemetry"].metadata is not None
        assert checks["Slice Telemetry"].metadata["stale_contracts"][0]["slice_id"] == "acme.deployment_velocity"


def test_run_doctor_channels_surface_source_health_for_stale_slice_telemetry(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        monkeypatch.delenv("GRAPH_TENANT_ID", raising=False)
        monkeypatch.delenv("GRAPH_CLIENT_ID", raising=False)

        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=4,
                discovered_signals=2,
                new_signals=1,
                pending_review=1,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=3,
                archived_journal_files=0,
                background_proposals=0,
                channels={
                        "ado": {"active": True, "signal_count": 4, "expected_min": 1, "meets_expected_min": True},
                        "kusto": {"active": True, "signal_count": 2, "expected_min": 1, "meets_expected_min": True},
                },
                query_states={
                        "velocity-p50": {
                                "last_succeeded_at": "2026-05-08T12:00:00+00:00",
                                "last_cycle_succeeded": True,
                                "row_count": 1,
                                "data_age_hours": 30.0,
                        }
                },
                programs_root=programs_root,
        )

        report = run_doctor(
                edition_name=EDITION_NAME,
                channels=True,
                reports_root=reports_root,
                archive_root=archive_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Source Health"].status == "warn"
        assert checks["Source Health"].metadata is not None
        assert checks["Source Health"].metadata["function"] == "newsletter"
        assert "acme.deployment_velocity:telemetry=stale" in checks["Source Health"].detail
        assert checks["Source Health"].metadata["unhealthy_roles"][0]["contract_id"] == "acme.deployment_velocity"


def test_run_doctor_channels_marks_waived_source_health_roles(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        monkeypatch.delenv("GRAPH_TENANT_ID", raising=False)
        monkeypatch.delenv("GRAPH_CLIENT_ID", raising=False)

        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)
        (programs_root / "acme" / "source_waivers.yaml").write_text(
                """
schema_version: '1.0'
waivers:
  - contract_id: acme.deployment_velocity
    role: telemetry
    owner: owner@example.com
    reason: Known telemetry lag during validation.
    granted: 2026-05-01
    expires: 2026-06-30
""".strip(),
                encoding="utf-8",
        )

        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=1,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=0,
                archived_journal_files=0,
                background_proposals=0,
                query_states={
                        "velocity-p50": {
                                "last_cycle_succeeded": True,
                                "row_count": 2,
                                "data_age_hours": 48.0,
                        }
                },
                channels={
                        "ado": {
                                "active": True,
                                "signal_count": 4,
                        }
                },
                programs_root=programs_root,
        )

        report = run_doctor(
                edition_name=EDITION_NAME,
                channels=True,
                reports_root=reports_root,
                archive_root=archive_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Source Health"].status == "warn"
        assert checks["Source Health"].metadata is not None
        assert checks["Source Health"].metadata["function"] == "newsletter"
        assert "[waived until 2026-06-30 by owner@example.com]" in checks["Source Health"].detail
        assert checks["Source Health"].metadata["unhealthy_roles"][0]["blocks_confirm"] is False
        assert checks["Source Health"].metadata["unhealthy_roles"][0]["waiver_owner"] == "owner@example.com"


def test_run_doctor_channels_fails_when_required_non_ado_roles_are_unhealthy(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(
                repo_root,
                tmp_path,
                edition_names=("nova_lt_deck",),
                program_names=("acme",),
        )
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        slice_contract_path = programs_root / "acme" / "slice_contracts.yaml"
        payload = yaml.safe_load(slice_contract_path.read_text(encoding="utf-8")) or {}
        slices = payload.get("slices") or []
        assert slices
        first_slice = slices[0]
        assert isinstance(first_slice, dict)
        first_slice["required"] = True
        _set_structured_decision_sources_from_fallback_sources(first_slice)
        slice_contract_path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=1,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=0,
                archived_journal_files=0,
                background_proposals=0,
                query_states={
                        "velocity-p50": {
                                "last_cycle_succeeded": True,
                                "row_count": 2,
                                "data_age_hours": 4.0,
                        }
                },
                channels={
                        "ado": {
                                "active": True,
                                "signal_count": 4,
                        },
                        "workiq": {
                                "active": True,
                                "signal_count": 2,
                                "expected_min": 8,
                                "meets_expected_min": False,
                        },
                        "transcript": {
                                "active": True,
                                "signal_count": 0,
                                "expected_min": 2,
                                "meets_expected_min": False,
                        },
                },
                programs_root=programs_root,
        )

        report = run_doctor(
                edition_name="nova_lt_deck",
                channels=True,
                reports_root=reports_root,
                archive_root=archive_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Source Health"].status == "fail"
        assert checks["Source Health"].metadata is not None
        assert checks["Source Health"].metadata["function"] == "deck"
        assert any(
                role["contract_id"] == "acme.deployment_velocity"
                and role["role"] == "decision"
                and role["state"] == "stale"
                and role["blocks_confirm"] is True
                for role in checks["Source Health"].metadata["unhealthy_roles"]
        )


def test_run_doctor_channels_uses_deck_function_and_surfaces_unbound_decision_sources(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(
                repo_root,
                tmp_path,
                edition_names=("nova_lt_deck",),
                program_names=("acme",),
        )
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        slice_contract_path = programs_root / "acme" / "slice_contracts.yaml"
        payload = yaml.safe_load(slice_contract_path.read_text(encoding="utf-8")) or {}
        slices = payload.get("slices") or []
        assert slices
        first_slice = slices[0]
        assert isinstance(first_slice, dict)
        first_slice["fallback_sources"] = []
        source_contract = first_slice.get("source_contract") or {}
        assert isinstance(source_contract, dict)
        source_contract.pop("decision_sources", None)
        first_slice["decision_sources"] = []
        slice_contract_path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=1,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=0,
                archived_journal_files=0,
                background_proposals=0,
                query_states={
                        "velocity-p50": {
                                "last_cycle_succeeded": True,
                                "row_count": 2,
                                "data_age_hours": 4.0,
                        }
                },
                channels={
                        "ado": {
                                "active": True,
                                "signal_count": 4,
                        }
                },
                programs_root=programs_root,
        )

        report = run_doctor(
                edition_name="nova_lt_deck",
                channels=True,
                reports_root=reports_root,
                archive_root=archive_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Source Health"].status == "fail"
        assert checks["Source Health"].metadata is not None
        assert checks["Source Health"].metadata["function"] == "deck"
        assert any(
                role["contract_id"] == "acme.deployment_velocity" and role["role"] == "decision" and role["state"] == "unbound"
                for role in checks["Source Health"].metadata["unhealthy_roles"]
        )


def _set_structured_decision_sources_from_fallback_sources(slice_doc: dict[str, object]) -> None:
        fallback_sources = tuple(slice_doc.get("fallback_sources") or ())
        source_contract = slice_doc.get("source_contract") or {}
        assert isinstance(source_contract, dict)
        source_contract.pop("decision_sources", None)
        # GAP-14: legacy defaults now return None; generate minimal entries so the
        # slice contract validator (which requires every fallback_source to appear in
        # decision_sources) is satisfied without relying on program-specific defaults.
        built = build_structured_decision_source_docs(fallback_sources)
        if not built:
            built = [
                {"source_id": source_id, "channels": ["workiq"], "blocked_artifact_selectors": [], "blocked_artifact_ids": []}
                for source_id in fallback_sources
            ]
        slice_doc["decision_sources"] = built


def test_run_doctor_channels_uses_runtime_health_for_deck_decision_sources(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(
                repo_root,
                tmp_path,
                edition_names=("nova_lt_deck",),
                program_names=("acme",),
        )
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        slice_contract_path = programs_root / "acme" / "slice_contracts.yaml"
        payload = yaml.safe_load(slice_contract_path.read_text(encoding="utf-8")) or {}
        slices = payload.get("slices") or []
        assert slices
        for slice_doc in slices:
                assert isinstance(slice_doc, dict)
                _set_structured_decision_sources_from_fallback_sources(slice_doc)
        slice_contract_path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=1,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=0,
                archived_journal_files=0,
                background_proposals=0,
                query_states={
                        "velocity-p50": {
                                "last_cycle_succeeded": True,
                                "row_count": 2,
                                "data_age_hours": 4.0,
                        }
                },
                channels={
                        "ado": {
                                "active": True,
                                "signal_count": 4,
                        },
                        "workiq": {
                                "active": True,
                                "signal_count": 2,
                                "expected_min": 8,
                                "meets_expected_min": False,
                        },
                        "transcript": {
                                "active": True,
                                "signal_count": 0,
                                "expected_min": 2,
                                "meets_expected_min": False,
                        },
                },
                programs_root=programs_root,
        )

        report = run_doctor(
                edition_name="nova_lt_deck",
                channels=True,
                reports_root=reports_root,
                archive_root=archive_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Source Health"].status == "fail"
        assert checks["Source Health"].metadata is not None
        assert checks["Source Health"].metadata["function"] == "deck"
        assert any(
                role["contract_id"] == "acme.deployment_velocity" and role["role"] == "decision" and role["state"] == "stale"
                for role in checks["Source Health"].metadata["unhealthy_roles"]
        )


def test_run_doctor_channels_marks_deck_decision_sources_stale_when_m365_identity_is_blocked(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(
                repo_root,
                tmp_path,
                edition_names=("nova_lt_deck",),
                program_names=("acme",),
        )
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        slice_contract_path = programs_root / "acme" / "slice_contracts.yaml"
        payload = yaml.safe_load(slice_contract_path.read_text(encoding="utf-8")) or {}
        slices = payload.get("slices") or []
        assert slices
        for slice_doc in slices:
                assert isinstance(slice_doc, dict)
                _set_structured_decision_sources_from_fallback_sources(slice_doc)
        slice_contract_path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=1,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=0,
                archived_journal_files=0,
                background_proposals=0,
                channels={
                        "ado": {
                                "active": True,
                                "signal_count": 4,
                        },
                        "workiq": {
                                "active": True,
                                "signal_count": 8,
                                "expected_min": 8,
                                "meets_expected_min": True,
                        },
                },
                m365_discovery={
                        "active": True,
                        "promotion_blocked_missing_id_count": 1,
                },
                programs_root=programs_root,
        )

        report = run_doctor(
                edition_name="nova_lt_deck",
                channels=True,
                reports_root=reports_root,
                archive_root=archive_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Source Health"].status == "fail"
        assert checks["Source Health"].metadata is not None
        assert checks["Source Health"].metadata["function"] == "deck"
        assert any(
                role["contract_id"] == "acme.deployment_velocity" and role["role"] == "decision" and role["state"] == "stale"
                for role in checks["Source Health"].metadata["unhealthy_roles"]
        )
        assert checks["M365 Discovery"].status == "warn"
        assert "promotion-blocked by missing series_id/thread_id" in checks["M365 Discovery"].detail


def test_run_doctor_channels_keeps_lt_deck_source_health_clean_when_only_ddpf_identity_is_blocked(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(
                repo_root,
                tmp_path,
                edition_names=("nova_lt_deck",),
                program_names=("acme",),
        )
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        slice_contract_path = programs_root / "acme" / "slice_contracts.yaml"
        payload = yaml.safe_load(slice_contract_path.read_text(encoding="utf-8")) or {}
        slices = payload.get("slices") or []
        assert slices
        first_slice = slices[0]
        assert isinstance(first_slice, dict)
        first_slice["fallback_sources"] = ["lt_deck"]
        source_contract = first_slice.get("source_contract") or {}
        assert isinstance(source_contract, dict)
        source_contract.pop("decision_sources", None)
        built = build_structured_decision_source_docs(first_slice["fallback_sources"])
        if not built:
            built = [
                {"source_id": src, "channels": ["workiq"], "blocked_artifact_selectors": [], "blocked_artifact_ids": []}
                for src in first_slice["fallback_sources"]
            ]
        first_slice["decision_sources"] = built
        slice_contract_path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=1,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=0,
                archived_journal_files=0,
                background_proposals=0,
                channels={
                        "ado": {
                                "active": True,
                                "signal_count": 4,
                        },
                        "workiq": {
                                "active": True,
                                "signal_count": 8,
                                "expected_min": 8,
                                "meets_expected_min": True,
                        },
                },
                m365_discovery={
                        "active": True,
                        "promotion_blocked_missing_id_count": 1,
                        "promotion_blocked_missing_id_ids": ["meet:acme-contoso-weekly-review"],
                },
                programs_root=programs_root,
        )

        report = run_doctor(
                edition_name="nova_lt_deck",
                channels=True,
                reports_root=reports_root,
                archive_root=archive_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Source Health"].status == "fail"
        assert checks["Source Health"].metadata is not None
        assert checks["Source Health"].metadata["function"] == "deck"
        assert not any(
                role["contract_id"] == "acme.deployment_velocity" and role["role"] == "decision"
                for role in checks["Source Health"].metadata["unhealthy_roles"]
        )
        assert checks["M365 Discovery"].status == "warn"


def test_run_doctor_channels_uses_nudge_function_without_requiring_hybrid_telemetry(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(
                repo_root,
                tmp_path,
                edition_names=("acme_weekly", "nova_nudge"),
                program_names=("acme",),
        )
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=1,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=0,
                archived_journal_files=0,
                background_proposals=0,
                channels={
                        "ado": {
                                "active": True,
                                "signal_count": 4,
                        },
                },
                programs_root=programs_root,
        )

        report = run_doctor(
                edition_name="nova_nudge",
                channels=True,
                reports_root=reports_root,
                archive_root=archive_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Source Health"].status == "ok"
        assert checks["Source Health"].metadata is not None
        assert checks["Source Health"].metadata["function"] == "nudge"
        assert checks["Source Health"].metadata["unhealthy_roles"] == []


def test_run_doctor_channels_uses_review_function_without_requiring_hybrid_telemetry(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(
                repo_root,
                tmp_path,
                edition_names=("nova_quarterly",),
                program_names=("acme",),
        )
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        slice_contract_path = programs_root / "acme" / "slice_contracts.yaml"
        payload = yaml.safe_load(slice_contract_path.read_text(encoding="utf-8")) or {}
        slices = payload.get("slices") or []
        assert slices
        for slice_doc in slices:
                assert isinstance(slice_doc, dict)
                _set_structured_decision_sources_from_fallback_sources(slice_doc)
        slice_contract_path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=1,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=0,
                archived_journal_files=0,
                background_proposals=0,
                channels={
                        "ado": {
                                "active": True,
                                "signal_count": 4,
                        },
                        "workiq": {
                                "active": True,
                                "signal_count": 8,
                                "expected_min": 8,
                                "meets_expected_min": True,
                        },
                },
                programs_root=programs_root,
        )

        report = run_doctor(
                edition_name="nova_quarterly",
                channels=True,
                reports_root=reports_root,
                archive_root=archive_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Source Health"].status == "ok"
        assert checks["Source Health"].metadata is not None
        assert checks["Source Health"].metadata["function"] == "review"
        assert checks["Source Health"].metadata["unhealthy_roles"] == []


def test_run_doctor_channels_surfaces_completeness_and_transcript_gaps(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        monkeypatch.delenv("GRAPH_TENANT_ID", raising=False)
        monkeypatch.delenv("GRAPH_CLIENT_ID", raising=False)

        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=1,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=3,
                archived_journal_files=0,
                background_proposals=0,
                gather_flags={"kusto": True, "workiq": True, "icm": False},
                channels={
                        "ado": {"active": True, "signal_count": 4, "expected_min": 1, "meets_expected_min": True},
                        "kusto": {"active": True, "signal_count": 2, "expected_min": 10, "meets_expected_min": False},
                        "workiq": {
                                "active": True,
                                "signal_count": 9,
                                "expected_min": 8,
                                "meets_expected_min": True,
                                "email_signals": 5,
                                "teams_signals": 4,
                        },
                        "transcript": {
                                "active": True,
                                "signal_count": 0,
                                "expected_min": 2,
                                "meets_expected_min": False,
                                "configured_series": 2,
                                "series_id_null": 1,
                                "all_series_ids_present": False,
                        },
                        "icm": {
                                "active": False,
                                "signal_count": 0,
                                "expected_min": 0,
                                "meets_expected_min": False,
                                "reason_not_active": "flag_not_passed",
                        },
                },
                m365_discovery={
                        "active": True,
                        "query_plan_count": 3,
                        "configured_series": 2,
                        "series_id_null": 1,
                        "configured_chats": 2,
                        "chat_thread_id_null": 1,
                        "first_discovery_completed_at": "2026-05-10T18:00:00+00:00",
                        "observed_thread_ids": 2,
                        "untracked_observed_thread_ids": 1,
                        "signals_without_thread_id": 0,
                        "signals_without_workstream": 2,
                        "promotion_blocked_missing_id_count": 1,
                        "discovery_last_error": "workiq gather failed: mcp request timed out",
                        "registry_file_present": False,
                        "feedback_file_present": False,
                        "registry_bootstrapped": False,
                },
                previous_gathered_at=datetime(2026, 5, 3, 18, 0, tzinfo=timezone.utc),
                previous_channels={
                        "ado": {"active": True, "signal_count": 4, "expected_min": 1, "meets_expected_min": True},
                        "kusto": {"active": True, "signal_count": 12, "expected_min": 10, "meets_expected_min": True},
                        "workiq": {
                                "active": True,
                                "signal_count": 11,
                                "expected_min": 8,
                                "meets_expected_min": True,
                                "email_signals": 6,
                                "teams_signals": 5,
                        },
                        "transcript": {
                                "active": True,
                                "signal_count": 2,
                                "expected_min": 2,
                                "meets_expected_min": True,
                                "configured_series": 2,
                                "series_id_null": 0,
                                "all_series_ids_present": True,
                        },
                        "icm": {
                                "active": False,
                                "signal_count": 0,
                                "expected_min": 0,
                                "meets_expected_min": False,
                                "reason_not_active": "flag_not_passed",
                        },
                },
                previous_m365_discovery={
                        "active": True,
                        "query_plan_count": 3,
                        "configured_series": 2,
                        "series_id_null": 0,
                        "configured_chats": 2,
                        "chat_thread_id_null": 0,
                        "observed_thread_ids": 1,
                        "untracked_observed_thread_ids": 0,
                        "signals_without_thread_id": 0,
                        "signals_without_workstream": 1,
                        "promotion_blocked_missing_id_count": 0,
                        "registry_file_present": False,
                        "feedback_file_present": False,
                        "registry_bootstrapped": False,
                },
                previous_query_states={
                        "acme-deployment-p50-p90": {
                                "last_attempted_at": datetime(2026, 5, 3, 17, 55, tzinfo=timezone.utc),
                                "last_cycle_succeeded": True,
                                "last_error": None,
                                "data_freshness_ok": True,
                        },
                        "acme-fleet-healthy-pct": {
                                "last_attempted_at": datetime(2026, 5, 3, 17, 56, tzinfo=timezone.utc),
                                "last_cycle_succeeded": True,
                                "last_error": None,
                                "value_frozen_warning": False,
                        }
                },
                query_states={
                        "acme-deployment-p50-p90": {
                                "last_attempted_at": datetime(2026, 5, 10, 17, 55, tzinfo=timezone.utc),
                                "last_cycle_succeeded": False,
                                "last_error": "kusto unavailable",
                                "data_freshness_ok": False,
                        },
                        "acme-fleet-healthy-pct": {
                                "last_attempted_at": datetime(2026, 5, 10, 17, 56, tzinfo=timezone.utc),
                                "last_cycle_succeeded": True,
                                "last_error": None,
                                "value_frozen_warning": True,
                        }
                },
                programs_root=programs_root,
        )

        report = run_doctor(
                edition_name=EDITION_NAME,
                channels=True,
                reports_root=reports_root,
                archive_root=archive_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Channels"].status == "warn"
        assert "50%" in checks["Channels"].detail
        assert "acme-deployment-p50-p90" in checks["Channels"].detail
        assert "Stale queries: acme-deployment-p50-p90." in checks["Channels"].detail
        assert "Frozen metric queries: acme-fleet-healthy-pct." in checks["Channels"].detail
        assert "M365 registry bootstrap missing" in checks["Channels"].detail
        assert "WorkIQ signals without workstream: 2" in checks["Channels"].detail
        assert "PM-confirmed artifacts blocked on missing IDs: 1" in checks["Channels"].detail
        assert "WorkIQ discovery failure: workiq gather failed: mcp request timed out." in checks["Channels"].detail
        assert "First active discovery completed at 2026-05-10T18:00:00+00:00." in checks["Channels"].detail
        assert checks["Channels"].metadata is not None
        assert checks["Channels"].metadata["gather_flags"] == {"kusto": True, "workiq": True, "icm": False}
        assert checks["Channels"].metadata["failed_queries"] == ["acme-deployment-p50-p90"]
        assert checks["Channels"].metadata["stale_queries"] == ["acme-deployment-p50-p90"]
        assert checks["Channels"].metadata["frozen_queries"] == ["acme-fleet-healthy-pct"]
        assert checks["Channels"].metadata["m365_discovery"]["untracked_observed_thread_ids"] == 1
        assert checks["M365 Discovery"].status == "warn"
        assert "registry bootstrap missing" in checks["M365 Discovery"].detail
        assert "1 observed thread(s) are not yet tracked" in checks["M365 Discovery"].detail
        assert "2 WorkIQ signal(s) lack workstream attribution" in checks["M365 Discovery"].detail
        assert "1 configured chat(s) are missing thread_id" in checks["M365 Discovery"].detail
        assert "1 PM-confirmed artifact(s) are promotion-blocked by missing series_id/thread_id" in checks["M365 Discovery"].detail
        assert "discovery runtime failure: workiq gather failed: mcp request timed out" in checks["M365 Discovery"].detail
        assert "first active discovery completed at 2026-05-10T18:00:00+00:00" in checks["M365 Discovery"].detail
        assert "Previous run: observed thread ids 1 -> 2; untracked threads 0 -> 1; unattributed WorkIQ signals 1 -> 2." in checks["M365 Discovery"].detail
        assert checks["Channels Delta"].status == "warn"
        assert "Previous run completeness 100% -> current 50% (-50 points)" in checks["Channels Delta"].detail
        assert "Regressed channels: kusto, transcript." in checks["Channels Delta"].detail
        assert "Newly failed queries: acme-deployment-p50-p90." in checks["Channels Delta"].detail
        assert "Newly stale queries: acme-deployment-p50-p90." in checks["Channels Delta"].detail
        assert "Newly frozen metric queries: acme-fleet-healthy-pct." in checks["Channels Delta"].detail
        assert "M365 untracked threads increased by 1." in checks["Channels Delta"].detail
        assert "M365 unattributed signals increased by 1." in checks["Channels Delta"].detail


def test_run_doctor_channels_surfaces_uil_ado_registry_health(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        monkeypatch.delenv("GRAPH_TENANT_ID", raising=False)
        monkeypatch.delenv("GRAPH_CLIENT_ID", raising=False)

        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=1,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=3,
                    archived_journal_files=0,
                    background_proposals=0,
                    gather_flags={"kusto": True, "workiq": True, "icm": False},
                    channels={
                            "ado": {
                                    "active": True,
                                    "signal_count": 4,
                                    "expected_min": 1,
                                "meets_expected_min": True,
                                "uil_enabled": True,
                                "uil_registry_file_present": True,
                                "uil_health": "ok",
                                "uil_registry_size": 42,
                                    "uil_last_discovery_at": "2026-05-10T17:45:00+00:00",
                                    "uil_last_delta_summary": "+2 -0 ~1 =39",
                                    "uil_last_delta_shrinkage_pct": 0.0,
                            },
                            "kusto": {
                                    "active": True,
                                    "signal_count": 0,
                                    "expected_min": 10,
                                    "meets_expected_min": False,
                                    "uil_enabled": True,
                                    "uil_registry_file_present": True,
                                    "uil_health": "ok",
                                    "uil_registry_size": 7,
                                    "uil_last_discovery_at": "2026-05-10T17:40:00+00:00",
                                    "uil_last_delta_summary": "+1 -0 ~0 =6",
                                    "uil_last_delta_shrinkage_pct": 0.0,
                            },
                            "workiq": {
                                    "active": True,
                                    "signal_count": 11,
                                    "expected_min": 8,
                                    "meets_expected_min": True,
                                    "email_signals": 6,
                                    "teams_signals": 5,
                            },
                            "transcript": {
                                    "active": True,
                                    "signal_count": 1,
                                    "expected_min": 2,
                                    "meets_expected_min": False,
                                    "configured_series": 2,
                                    "series_id_null": 1,
                                    "all_series_ids_present": False,
                            },
                            "icm": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed"},
                    },
                    previous_gathered_at=datetime(2026, 5, 3, 18, 0, tzinfo=timezone.utc),
                    previous_channels={
                            "ado": {"active": True, "signal_count": 4, "expected_min": 1, "meets_expected_min": True},
                            "kusto": {"active": True, "signal_count": 12, "expected_min": 10, "meets_expected_min": True},
                            "workiq": {
                                    "active": True,
                                    "signal_count": 11,
                                    "expected_min": 8,
                                    "meets_expected_min": True,
                                    "email_signals": 6,
                                    "teams_signals": 5,
                            },
                            "transcript": {
                                    "active": True,
                                    "signal_count": 2,
                                    "expected_min": 2,
                                    "meets_expected_min": True,
                                    "configured_series": 2,
                                    "series_id_null": 0,
                                    "all_series_ids_present": True,
                            },
                            "icm": {
                                    "active": False,
                                    "signal_count": 0,
                                    "expected_min": 0,
                                    "meets_expected_min": False,
                                    "reason_not_active": "flag_not_passed",
                            },
                    },
                    previous_query_states={
                            "acme-deployment-p50-p90": {
                                    "last_attempted_at": datetime(2026, 5, 3, 17, 55, tzinfo=timezone.utc),
                                    "last_cycle_succeeded": True,
                                    "last_error": None,
                                    "data_freshness_ok": True,
                            },
                            "acme-fleet-healthy-pct": {
                                    "last_attempted_at": datetime(2026, 5, 3, 17, 56, tzinfo=timezone.utc),
                                    "last_cycle_succeeded": True,
                                    "last_error": None,
                                    "value_frozen_warning": False,
                            },
                    },
                    query_states={
                            "acme-deployment-p50-p90": {
                                    "last_attempted_at": datetime(2026, 5, 10, 17, 55, tzinfo=timezone.utc),
                                    "last_cycle_succeeded": False,
                                    "last_error": "kusto unavailable",
                                    "data_freshness_ok": False,
                            },
                            "acme-fleet-healthy-pct": {
                                    "last_attempted_at": datetime(2026, 5, 10, 17, 56, tzinfo=timezone.utc),
                                    "last_cycle_succeeded": True,
                                    "last_error": None,
                                    "value_frozen_warning": True,
                            },
                    },
                    programs_root=programs_root,
            )

        report = run_doctor(
                edition_name=EDITION_NAME,
                channels=True,
                reports_root=reports_root,
                archive_root=archive_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable",
                ),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["UIL ADO"].status == "ok"
        assert "42 active registrations" in checks["UIL ADO"].detail
        assert checks["UIL ADO"].metadata["last_delta_summary"] == "+2 -0 ~1 =39"
        assert checks["UIL Kusto"].status == "ok"
        assert "7 active registrations" in checks["UIL Kusto"].detail
        assert checks["UIL Kusto"].metadata["last_delta_summary"] == "+1 -0 ~0 =6"


def test_run_doctor_channels_surfaces_transcript_zero_yield_fail_loud(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=0,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=3,
                archived_journal_files=0,
                background_proposals=0,
                gather_flags={"kusto": False, "workiq": True, "icm": False},
                channels={
                        "ado": {"active": True, "signal_count": 4, "expected_min": 1, "meets_expected_min": True},
                        "workiq": {"active": True, "signal_count": 9, "expected_min": 8, "meets_expected_min": True},
                        "transcript": {
                                "active": True,
                                "signal_count": 0,
                                "expected_min": 0,
                                "meets_expected_min": True,
                                "configured_series": 2,
                                "series_id_null": 0,
                        },
                        "icm": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed"},
                },
                programs_root=programs_root,
        )

        report = run_doctor(
                edition_name=EDITION_NAME,
                channels=True,
                reports_root=reports_root,
                archive_root=archive_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Channel:transcript"].status == "warn"
        assert "yielded 0 signals across 2 configured meeting series" in checks["Channel:transcript"].detail
        assert checks["Source Health"].status == "fail"
        assert checks["Source Health"].metadata is not None
        assert any(
                role["contract_id"] == "vertex/transcript"
                and role["role"] == "transcript"
                and role["state"] == "zero_yield"
                and role["blocks_confirm"] is True
                for role in checks["Source Health"].metadata["unhealthy_roles"]
        )


def test_uil_registry_check_warns_on_degraded_scope_health() -> None:
        check = _uil_registry_check(
                "ado",
                {
                        "uil_enabled": True,
                        "uil_health": "ok",
                        "uil_registry_size": 42,
                        "uil_last_discovery_at": "2026-05-10T17:45:00+00:00",
                        "uil_last_delta_summary": "+2 -0 ~1 =39",
                        "uil_scope_health": {
                                "scope-a": "ok",
                                "scope-b": "error_2x",
                        },
                },
        )

        assert check is not None
        assert check.status == "warn"
        assert "1 discovery scope(s) are degraded" in check.detail
        assert "scope-b=error_2x" in check.detail
        assert check.metadata["degraded_scope_health"] == {"scope-b": "error_2x"}


def test_uil_registry_check_ok_for_teams_channel() -> None:
        check = _uil_registry_check(
                "teams",
                {
                        "uil_enabled": True,
                        "uil_health": "ok",
                        "uil_registry_size": 8,
                        "uil_last_discovery_at": "2026-05-24T10:00:00+00:00",
                        "uil_last_delta_summary": "+3 -0 ~0 =5",
                        "uil_last_delta_shrinkage_pct": 0.0,
                        "uil_scope_health": {"static_config": "ok", "workiq_search": "ok"},
                },
        )

        assert check is not None
        assert check.status == "ok"
        assert check.label == "UIL Teams"
        assert "8 active registrations" in check.detail


def test_uil_registry_check_ok_for_icm_channel() -> None:
        check = _uil_registry_check(
                "icm",
                {
                        "uil_enabled": True,
                        "uil_health": "ok",
                        "uil_registry_size": 5,
                        "uil_last_discovery_at": "2026-05-24T10:00:00+00:00",
                        "uil_last_delta_summary": "+0 -0 ~0 =5",
                        "uil_last_delta_shrinkage_pct": 0.0,
                        "uil_scope_health": {"team_mapping": "ok"},
                },
        )

        assert check is not None
        assert check.status == "ok"
        assert check.label == "UIL ICM"
        assert "5 active registrations" in check.detail


def test_uil_registry_check_returns_none_when_uil_not_enabled() -> None:
        check = _uil_registry_check(
                "teams",
                {
                        "uil_enabled": False,
                        "active": True,
                        "signal_count": 3,
                },
        )
        assert check is None


def test_run_doctor_channels_surfaces_zero_row_queries(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        monkeypatch.delenv("GRAPH_TENANT_ID", raising=False)
        monkeypatch.delenv("GRAPH_CLIENT_ID", raising=False)

        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        # Override min_completeness_pct so the parent Channels status depends only on
        # zero-row-query/degraded-channel logic, not the absolute completeness threshold.
        program_path = programs_root / "acme" / "program.yaml"
        program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        program_document.setdefault("gather", {})["min_completeness_pct"] = 0
        program_path.write_text(yaml.safe_dump(program_document, sort_keys=False, allow_unicode=False), encoding="utf-8")

        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=1,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=3,
                archived_journal_files=0,
                background_proposals=0,
                gather_flags={"kusto": True, "workiq": False, "icm": False},
                channels={
                        "ado": {"active": True, "signal_count": 4, "expected_min": 1, "meets_expected_min": True},
                        "kusto": {"active": True, "signal_count": 0, "expected_min": 1, "meets_expected_min": False},
                        "workiq": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed"},
                        "transcript": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed"},
                        "icm": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed"},
                },
                query_states={
                        "acme-buildout-pipeline": {
                                "last_succeeded_at": "2026-05-10T18:00:00+00:00",
                                "last_cycle_succeeded": True,
                                "row_count": 0,
                                "zero_rows_ok": False,
                        }
                },
                programs_root=programs_root,
        )


def test_run_doctor_channels_honors_program_min_completeness_threshold(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        monkeypatch.delenv("GRAPH_TENANT_ID", raising=False)
        monkeypatch.delenv("GRAPH_CLIENT_ID", raising=False)

        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        program_path = programs_root / "acme" / "program.yaml"
        program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        program_document["gather"] = {"min_completeness_pct": 60}
        if isinstance(program_document.get("m365"), dict):
                program_document["m365"]["enabled"] = False
        program_path.write_text(yaml.safe_dump(program_document, sort_keys=False, allow_unicode=False), encoding="utf-8")

        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=8,
                discovered_signals=7,
                new_signals=3,
                pending_review=0,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=2,
                archived_journal_files=0,
                background_proposals=0,
                gather_flags={"kusto": True, "workiq": False, "icm": True},
                channels={
                        "ado": {"active": True, "signal_count": 4, "expected_min": 1, "meets_expected_min": True},
                        "kusto": {"active": True, "signal_count": 2, "expected_min": 1, "meets_expected_min": True},
                        "workiq": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed"},
                        "transcript": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed"},
                        "icm": {"active": True, "signal_count": 0, "expected_min": 1, "meets_expected_min": False},
                },
                programs_root=programs_root,
        )

        report = run_doctor(
                edition_name=EDITION_NAME,
                channels=True,
                reports_root=reports_root,
                archive_root=archive_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Channels"].status == "ok"
        assert checks["Channels"].metadata is not None
        assert checks["Channels"].metadata["completeness_pct"] == 67
        assert checks["Channels"].metadata["min_completeness_pct"] == 60
        assert checks["Channel:icm"].status == "warn"


def test_run_doctor_channels_ignores_healthy_zero_pipeline_and_pr_cycles(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        monkeypatch.delenv("GRAPH_TENANT_ID", raising=False)
        monkeypatch.delenv("GRAPH_CLIENT_ID", raising=False)

        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=1,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=3,
                archived_journal_files=0,
                background_proposals=0,
                gather_flags={"kusto": False, "workiq": False, "icm": False, "pipelines": True},
                channels={
                        "ado": {"active": True, "signal_count": 4, "expected_min": 1, "meets_expected_min": True},
                        "kusto": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed"},
                        "workiq": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed"},
                        "transcript": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed"},
                        "icm": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed"},
                },
                query_states={
                        "ado-pipeline:deployment_readiness": {
                                "last_succeeded_at": "2026-05-10T18:00:00+00:00",
                                "last_cycle_succeeded": True,
                                "row_count": 2,
                                "failed_run_count": 0,
                                "zero_rows_ok": True,
                                "value_last_4": [0.0, 0.0, 0.0, 0.0],
                                "value_frozen_warning": False,
                        },
                        "ado-pr:deployment_readiness": {
                                "last_succeeded_at": "2026-05-10T18:00:00+00:00",
                                "last_cycle_succeeded": True,
                                "row_count": 0,
                                "open_pr_count": 0,
                                "zero_rows_ok": True,
                                "value_last_4": [0.0, 0.0, 0.0, 0.0],
                                "value_frozen_warning": False,
                        },
                },
                programs_root=programs_root,
        )

        report = run_doctor(
                edition_name=EDITION_NAME,
                channels=True,
                reports_root=reports_root,
                archive_root=archive_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Channels"].status == "ok"
        assert "Zero-row queries:" not in checks["Channels"].detail
        assert "Frozen metric queries:" not in checks["Channels"].detail
        assert checks["Channels"].metadata is not None
        assert checks["Channels"].metadata["zero_row_queries"] == []
        assert checks["Channels"].metadata["frozen_queries"] == []


def test_run_doctor_channels_surfaces_silent_zero_yield_workiq(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        monkeypatch.delenv("GRAPH_TENANT_ID", raising=False)
        monkeypatch.delenv("GRAPH_CLIENT_ID", raising=False)

        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=1,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=3,
                archived_journal_files=0,
                background_proposals=0,
                integration_errors=0,
                gather_flags={"kusto": False, "workiq": True, "icm": False},
                channels={
                        "ado": {"active": True, "signal_count": 4, "expected_min": 1, "meets_expected_min": True},
                        "kusto": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed"},
                        "workiq": {"active": True, "signal_count": 0, "expected_min": 8, "meets_expected_min": False, "email_signals": 0, "teams_signals": 0},
                        "transcript": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed"},
                        "icm": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed"},
                },
                m365_discovery={
                        "active": True,
                        "query_plan_count": 3,
                        "configured_series": 2,
                        "series_id_null": 0,
                        "configured_chats": 1,
                        "chat_thread_id_null": 0,
                        "observed_thread_ids": 0,
                        "untracked_observed_thread_ids": 0,
                        "signals_without_thread_id": 0,
                        "signals_without_workstream": 0,
                        "registry_file_present": True,
                        "feedback_file_present": True,
                        "registry_bootstrapped": True,
                },
                programs_root=programs_root,
        )

        report = run_doctor(
                edition_name=EDITION_NAME,
                channels=True,
                reports_root=reports_root,
                archive_root=archive_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Channels"].status == "warn"
        assert "Flag-omitted channels: icm, kusto, transcript." in checks["Channels"].detail
        assert "Silent zero-yield channels: workiq." in checks["Channels"].detail
        assert checks["Channels"].metadata is not None
        assert checks["Channels"].metadata["flag_omitted_channels"] == ["icm", "kusto", "transcript"]
        assert checks["Channels"].metadata["silent_zero_yield_channels"] == ["workiq"]


def test_run_doctor_channels_surfaces_missing_ado_pr_repository_configuration(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        monkeypatch.delenv("GRAPH_TENANT_ID", raising=False)
        monkeypatch.delenv("GRAPH_CLIENT_ID", raising=False)

        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        # Strip ado_repository_ids so this test exercises the "no repos configured" warn path.
        # (W2.4 operator pass added repo IDs to the live workstreams.yaml; the test workspace
        # copies those, so we remove them here to keep this test scenario valid.)
        ws_yaml_path = programs_root / "acme" / "workstreams.yaml"
        ws_data = yaml.safe_load(ws_yaml_path.read_text(encoding="utf-8"))
        for ws in ws_data.get("workstreams", []):
            ws.pop("ado_repository_ids", None)
        ws_yaml_path.write_text(yaml.dump(ws_data, default_flow_style=False, allow_unicode=True), encoding="utf-8")

        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=1,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=3,
                archived_journal_files=0,
                background_proposals=0,
                gather_flags={"kusto": False, "workiq": False, "icm": False, "pipelines": False},
                channels={
                        "ado": {"active": True, "signal_count": 4, "expected_min": 1, "meets_expected_min": True},
                        "kusto": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed"},
                        "workiq": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed"},
                        "transcript": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed"},
                        "icm": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed"},
                },
                programs_root=programs_root,
        )

        report = run_doctor(
                edition_name=EDITION_NAME,
                channels=True,
                reports_root=reports_root,
                archive_root=archive_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["ADO PR Coverage"].status == "warn"
        assert "no workstreams declare ado_repository_ids" in checks["ADO PR Coverage"].detail
        assert checks["ADO PR Coverage"].metadata is not None
        assert checks["ADO PR Coverage"].metadata["configured_workstream_ids"] == []
        assert checks["ADO PR Coverage"].metadata["missing_workstream_ids"] == ["acme", "dd_on_pf"]


def test_run_doctor_channel_kusto_surfaces_auth_guidance_from_last_error(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        monkeypatch.delenv("GRAPH_TENANT_ID", raising=False)
        monkeypatch.delenv("GRAPH_CLIENT_ID", raising=False)

        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=1,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=3,
                archived_journal_files=0,
                background_proposals=0,
                integration_errors=1,
                gather_flags={"kusto": True, "workiq": False, "icm": False},
                channels={
                        "ado": {"active": True, "signal_count": 4, "expected_min": 1, "meets_expected_min": True, "last_error": None},
                        "kusto": {
                                "active": True,
                                "signal_count": 0,
                                "expected_min": 10,
                                "meets_expected_min": False,
                                "reason_not_active": None,
                                "last_error": "AADSTS700082: The refresh token has expired due to inactivity. Run 'az login'.",
                        },
                        "workiq": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed", "last_error": None},
                        "transcript": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed", "last_error": None},
                        "icm": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed", "last_error": None},
                },
                m365_discovery={
                        "active": False,
                        "reason_not_active": "flag_not_passed",
                },
                programs_root=programs_root,
        )

        report = run_doctor(
                edition_name=EDITION_NAME,
                channels=True,
                reports_root=reports_root,
                archive_root=archive_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Channels"].status == "warn"
        assert "Channel access issues: kusto: Azure CLI session expired or Kusto access failed; run 'az login' and verify cluster access.." in checks["Channels"].detail
        assert checks["Channels"].metadata is not None
        assert checks["Channels"].metadata["auth_guidance_by_channel"] == {
                "kusto": "Azure CLI session expired or Kusto access failed; run 'az login' and verify cluster access."
        }
        assert checks["Channel:kusto"].status == "warn"
        assert "expected at least 10" in checks["Channel:kusto"].detail
        assert "Azure CLI session expired or Kusto access failed; run 'az login' and verify cluster access." in checks["Channel:kusto"].detail
        assert checks["Channel:icm"].status == "ok"
        assert "flag_not_passed" in checks["Channel:icm"].detail


def test_run_doctor_channels_warns_on_active_ado_degradation_with_signal_yield(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        monkeypatch.delenv("GRAPH_TENANT_ID", raising=False)
        monkeypatch.delenv("GRAPH_CLIENT_ID", raising=False)

        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=1,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=3,
                archived_journal_files=0,
                background_proposals=0,
                integration_errors=1,
                gather_flags={"kusto": False, "workiq": False, "icm": False},
                channels={
                        "ado": {
                                "active": True,
                                "signal_count": 8,
                                "expected_min": 1,
                                "meets_expected_min": True,
                                "last_error": "Saved query query-1 failed during expansion: TF51011 invalid area path",
                        },
                        "kusto": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed", "last_error": None},
                        "workiq": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed", "last_error": None},
                        "transcript": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed", "last_error": None},
                        "icm": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed", "last_error": None},
                },
                m365_discovery={
                        "active": False,
                        "reason_not_active": "flag_not_passed",
                },
                programs_root=programs_root,
        )

        report = run_doctor(
                edition_name=EDITION_NAME,
                channels=True,
                reports_root=reports_root,
                archive_root=archive_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Channels"].status == "warn"
        assert "Active degraded channels: ado." in checks["Channels"].detail
        assert checks["Channels"].metadata is not None
        assert checks["Channels"].metadata["degraded_channels"] == ["ado"]
        assert checks["Channel:ado"].status == "warn"
        assert "latest gather recorded degradation" in checks["Channel:ado"].detail
        assert "Saved query query-1 failed during expansion" in checks["Channel:ado"].detail


def test_run_doctor_channels_ignores_stale_removed_kusto_target_degradation(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=1,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=3,
                archived_journal_files=0,
                background_proposals=0,
                integration_errors=1,
                gather_flags={"kusto": True, "workiq": False, "icm": False},
                channels={
                        "ado": {
                                "active": True,
                                "signal_count": 8,
                                "expected_min": 1,
                                "meets_expected_min": True,
                                "last_error": None,
                        },
                        "kusto": {
                                "active": True,
                                "signal_count": 12,
                                "expected_min": 10,
                                "meets_expected_min": True,
                                "reason_not_active": None,
                                "last_error": "Kusto pre-flight failed for https://azcis.kusto.windows.net/krdma_db. Run 'vertex admin auth setup' and verify cluster access or JIT.",
                        },
                        "workiq": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed", "last_error": None},
                        "transcript": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed", "last_error": None},
                        "icm": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed", "last_error": None},
                },
                m365_discovery={
                        "active": False,
                        "reason_not_active": "flag_not_passed",
                },
                programs_root=programs_root,
        )

        report = run_doctor(
                edition_name=EDITION_NAME,
                channels=True,
                reports_root=reports_root,
                archive_root=archive_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Channels"].metadata is not None
        assert checks["Channels"].metadata["degraded_channels"] == []
        assert checks["Channels"].metadata["auth_guidance_by_channel"] == {}
        assert "Active degraded channels" not in checks["Channels"].detail
        assert "Channel access issues" not in checks["Channels"].detail
        assert checks["Channel:kusto"].status == "ok"
        assert "latest gather recorded degradation" not in checks["Channel:kusto"].detail
        assert "krdma_db" not in checks["Channel:kusto"].detail


def test_run_doctor_channels_surfaces_m365_registry_review_queue(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        upsert_m365_registry_artifacts(
                "acme",
                artifacts=(
                        M365RegistryArtifact(
                                artifact_id="thread:auto:review01",
                                artifact_type="email_thread",
                                display_name="Needs review",
                                thread_id="review-thread-1",
                                inferred_workstream="acme",
                                confidence=0.70,
                                confidence_source="keyword",
                                pm_confirmed=False,
                                promoted_to_workstreams_yaml=False,
                                first_seen=date(2026, 5, 1),
                                last_seen=date(2026, 5, 10),
                                signal_yield_last_3=(1, 0, 0),
                                topics=("SCHIE",),
                        ),
                        M365RegistryArtifact(
                                artifact_id="thread:auto:low0001",
                                artifact_type="email_thread",
                                display_name="Unclassified",
                                thread_id="low-thread-1",
                                inferred_workstream="acme",
                                confidence=0.45,
                                confidence_source="keyword",
                                pm_confirmed=False,
                                promoted_to_workstreams_yaml=False,
                                first_seen=date(2026, 5, 1),
                                last_seen=date(2026, 5, 10),
                                signal_yield_last_3=(0, 0, 0),
                                topics=("SCHIE",),
                        ),
                        M365RegistryArtifact(
                                artifact_id="meet:acme-null-series",
                                artifact_type="meeting_series",
                                display_name="Confirmed missing series",
                                series_id=None,
                                inferred_workstream="acme",
                                confidence=1.0,
                                confidence_source="pm_confirmed",
                                pm_confirmed=True,
                                promoted_to_workstreams_yaml=False,
                                first_seen=date(2026, 5, 1),
                                last_seen=date(2026, 5, 10),
                                signal_yield_last_3=(0, 0, 0),
                                topics=("SCHIE",),
                        ),
                        M365RegistryArtifact(
                                artifact_id="chan:acme-promote-ready",
                                artifact_type="teams_channel",
                                display_name="Promotion Ready Chat",
                                thread_id="promote-thread-1",
                                inferred_workstream="acme",
                                confidence=1.0,
                                confidence_source="pm_confirmed",
                                pm_confirmed=True,
                                promoted_to_workstreams_yaml=False,
                                first_seen=date(2026, 5, 1),
                                last_seen=date(2026, 5, 10),
                                signal_yield_last_3=(1, 1, 1),
                                topics=("SCHIE",),
                        ),
                ),
                programs_root=programs_root,
                as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        )
        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=1,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=3,
                archived_journal_files=0,
                background_proposals=0,
                gather_flags={"kusto": False, "workiq": True, "icm": False},
                channels={
                        "ado": {"active": True, "signal_count": 4, "expected_min": 1, "meets_expected_min": True},
                        "workiq": {"active": True, "signal_count": 9, "expected_min": 8, "meets_expected_min": True},
                        "transcript": {"active": True, "signal_count": 2, "expected_min": 2, "meets_expected_min": True, "configured_series": 2, "series_id_null": 0},
                        "icm": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed"},
                },
                m365_discovery={
                        "active": True,
                        "query_plan_count": 3,
                        "configured_series": 2,
                        "series_id_null": 0,
                        "configured_chats": 1,
                        "chat_thread_id_null": 0,
                        "observed_thread_ids": 2,
                        "untracked_observed_thread_ids": 0,
                        "signals_without_thread_id": 0,
                        "signals_without_workstream": 0,
                        "registry_file_present": True,
                        "feedback_file_present": True,
                        "registry_bootstrapped": True,
                },
                programs_root=programs_root,
        )

        report = run_doctor(
                edition_name=EDITION_NAME,
                channels=True,
                reports_root=reports_root,
                archive_root=archive_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Channels"].status == "warn"
        assert "M365 review queue: 1 medium-confidence artifact(s); 1 unclassified artifact(s); 1 confirmed artifact(s) missing IDs; 1 artifact(s) ready for current promotion." in checks["Channels"].detail
        assert checks["Channels"].metadata is not None
        assert checks["Channels"].metadata["m365_registry_review"]["medium_review_count"] == 1
        assert checks["Channels"].metadata["m365_registry_review"]["unclassified_count"] == 1
        assert checks["Channels"].metadata["m365_registry_review"]["missing_id_count"] == 1
        assert checks["Channels"].metadata["m365_registry_review"]["promotion_candidate_count"] == 1
        assert checks["Channels"].metadata["m365_registry_review"]["promotion_blocked_missing_id_count"] == 1
        assert checks["M365 Registry Review"].status == "warn"
        assert "1 medium-confidence artifact(s) need PM review" in checks["M365 Registry Review"].detail
        assert "1 artifact(s) are in the UNCLASSIFIED band" in checks["M365 Registry Review"].detail
        assert "1 PM-confirmed artifact(s) are missing series_id/thread_id" in checks["M365 Registry Review"].detail
        assert checks["M365 Registry Promotion"].status == "warn"
        assert "1 eligible artifact(s) are ready for current promotion via 'vertex registry promote'" in checks["M365 Registry Promotion"].detail
        assert "1 artifact(s) are promotion-blocked by missing series_id/thread_id." in checks["M365 Registry Promotion"].detail


def test_build_m365_registry_review_metadata_reads_workstreams_from_program_facts(monkeypatch, tmp_path: Path) -> None:
        _, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        registry_time = datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc)
        calls: dict[str, object] = {}

        monkeypatch.setattr(
                "src.commands.doctor.load_m365_registry",
                lambda program_id, programs_root: SimpleNamespace(artifacts=(), last_updated=registry_time),
        )
        monkeypatch.setattr("src.commands.doctor.read_m365_routing_feedback_events", lambda program_id, programs_root: ())
        monkeypatch.setattr("src.commands.doctor.load_approved_m365_corpus_signals", lambda program_id, *, as_of, programs_root: ())

        def _fake_load_current_workstreams(program_id: str, *, programs_root: Path) -> tuple[Workstream, ...]:
                calls["program_id"] = program_id
                calls["programs_root"] = programs_root
                return (
                        Workstream(
                                id="ws_fact",
                                name="Fact-backed workstream",
                                signal_sources=WorkstreamSignalSources(workiq_keywords=("launch",)),
                        ),
                )

        monkeypatch.setattr("src.commands.doctor.load_current_workstreams", _fake_load_current_workstreams)
        monkeypatch.setattr(
                "src.commands.doctor.build_m365_corpus_texts_by_workstream",
                lambda **kwargs: {"ws_fact": ("launch review",)},
        )
        monkeypatch.setattr(
                "src.commands.doctor.suggest_keyword_expansions",
                lambda *, existing_keywords, texts: ("status",) if tuple(texts) == ("launch review",) else (),
        )

        metadata = _build_m365_registry_review_metadata("demo", programs_root=programs_root)

        assert calls == {"program_id": "demo", "programs_root": programs_root}
        assert metadata["artifact_count"] == 0
        assert metadata["keyword_suggestions_by_workstream"] == {"ws_fact": ["status"]}


def test_load_dependency_workstream_ids_reads_workstreams_from_program_facts(monkeypatch, tmp_path: Path) -> None:
        _, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        calls: dict[str, object] = {}

        def _fake_load_current_workstreams(program_id: str, *, programs_root: Path) -> tuple[Workstream, ...]:
                calls["program_id"] = program_id
                calls["programs_root"] = programs_root
                return (
                        Workstream(id="ws_beta", name="Beta"),
                        Workstream(id="ws_alpha", name="Alpha"),
                        Workstream(id="ws_beta", name="Beta duplicate"),
                )

        monkeypatch.setattr("src.commands.doctor.load_current_workstreams", _fake_load_current_workstreams)

        assert _load_dependency_workstream_ids("demo", programs_root=programs_root) == ("ws_alpha", "ws_beta")
        assert calls == {"program_id": "demo", "programs_root": programs_root}


def test_run_doctor_channels_excludes_recently_rejected_promotion_candidates(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        upsert_m365_registry_artifacts(
                "acme",
                artifacts=(
                        M365RegistryArtifact(
                                artifact_id="chan:acme-promote-ready",
                                artifact_type="teams_channel",
                                display_name="Promotion Ready Chat",
                                thread_id="promote-thread-1",
                                inferred_workstream="acme",
                                confidence=1.0,
                                confidence_source="pm_confirmed",
                                pm_confirmed=True,
                                promoted_to_workstreams_yaml=False,
                                first_seen=date(2026, 5, 1),
                                last_seen=date(2026, 5, 10),
                                signal_yield_last_3=(1, 1, 1),
                                topics=("SCHIE",),
                        ),
                ),
                programs_root=programs_root,
                as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        )
        feedback_path = programs_root / "acme" / "_feedback" / "m365_routing_feedback.jsonl"
        feedback_path.parent.mkdir(parents=True, exist_ok=True)
        feedback_path.write_text(
                '{"ts": "2026-05-09T18:00:00+00:00", "artifact_id": "chan:acme-promote-ready", "action": "reject", "pm_alias": "operator", "workstream_id": null, "topics": [], "reason": "off topic", "series_id": null, "thread_id": null}\n',
                encoding="utf-8",
        )
        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=1,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=3,
                archived_journal_files=0,
                background_proposals=0,
                gather_flags={"kusto": False, "workiq": True, "icm": False},
                channels={
                        "ado": {"active": True, "signal_count": 4, "expected_min": 1, "meets_expected_min": True},
                        "workiq": {"active": True, "signal_count": 9, "expected_min": 8, "meets_expected_min": True},
                        "transcript": {"active": True, "signal_count": 2, "expected_min": 2, "meets_expected_min": True, "configured_series": 2, "series_id_null": 0},
                        "icm": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed"},
                },
                m365_discovery={
                        "active": True,
                        "query_plan_count": 3,
                        "configured_series": 2,
                        "series_id_null": 0,
                        "configured_chats": 1,
                        "chat_thread_id_null": 0,
                        "observed_thread_ids": 2,
                        "untracked_observed_thread_ids": 0,
                        "signals_without_thread_id": 0,
                        "signals_without_workstream": 0,
                        "registry_file_present": True,
                        "feedback_file_present": True,
                        "registry_bootstrapped": True,
                },
                programs_root=programs_root,
        )

        report = run_doctor(
                edition_name=EDITION_NAME,
                channels=True,
                reports_root=reports_root,
                archive_root=archive_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Channels"].metadata is not None
        assert checks["Channels"].metadata["m365_registry_review"]["promotion_candidate_count"] == 0
        assert checks["Channels"].metadata["m365_registry_review"]["promotion_blocked_recent_rejection_count"] == 1
        assert checks["M365 Registry Promotion"].status == "warn"
        assert "1 artifact(s) are promotion-blocked by recent rejection." in checks["M365 Registry Promotion"].detail


def test_run_doctor_channels_surfaces_high_confidence_promotion_candidates_without_review_queue(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        upsert_m365_registry_artifacts(
                "acme",
                artifacts=(
                        M365RegistryArtifact(
                                artifact_id="thread:auto:steady001",
                                artifact_type="email_thread",
                                display_name="Steady High Confidence Thread",
                                thread_id="steady-thread-1",
                                inferred_workstream="acme",
                                confidence=0.91,
                                confidence_source="keyword_router",
                                pm_confirmed=False,
                                promoted_to_workstreams_yaml=False,
                                first_seen=date(2026, 5, 1),
                                last_seen=date(2026, 5, 10),
                                signal_yield_last_3=(1, 1, 1),
                                high_confidence_streak=3,
                                topics=("SCHIE",),
                        ),
                ),
                programs_root=programs_root,
                as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        )
        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=0,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=3,
                archived_journal_files=0,
                background_proposals=0,
                gather_flags={"kusto": False, "workiq": True, "icm": False},
                channels={
                        "ado": {"active": True, "signal_count": 4, "expected_min": 1, "meets_expected_min": True},
                        "workiq": {"active": True, "signal_count": 9, "expected_min": 8, "meets_expected_min": True},
                        "transcript": {"active": True, "signal_count": 2, "expected_min": 2, "meets_expected_min": True, "configured_series": 2, "series_id_null": 0},
                        "icm": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed"},
                },
                m365_discovery={
                        "active": True,
                        "query_plan_count": 3,
                        "configured_series": 2,
                        "series_id_null": 0,
                        "configured_chats": 1,
                        "chat_thread_id_null": 0,
                        "observed_thread_ids": 2,
                        "untracked_observed_thread_ids": 0,
                        "signals_without_thread_id": 0,
                        "signals_without_workstream": 0,
                        "registry_file_present": True,
                        "feedback_file_present": True,
                        "registry_bootstrapped": True,
                },
                programs_root=programs_root,
        )

        report = run_doctor(
                edition_name=EDITION_NAME,
                channels=True,
                reports_root=reports_root,
                archive_root=archive_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Channels"].metadata is not None
        assert checks["Channels"].metadata["m365_registry_review"]["medium_review_count"] == 0
        assert checks["Channels"].metadata["m365_registry_review"]["promotion_candidate_count"] == 1
        assert checks["Channels"].metadata["m365_registry_review"]["promotion_blocked_recent_rejection_count"] == 0
        assert checks["M365 Registry Review"].status == "ok"
        assert checks["M365 Registry Promotion"].status == "warn"
        assert "1 eligible artifact(s) are ready for current promotion via 'vertex registry promote'." in checks["M365 Registry Promotion"].detail


def test_run_doctor_channels_surfaces_signal_yield_blocked_m365_artifacts(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        upsert_m365_registry_artifacts(
                "acme",
                artifacts=(
                        M365RegistryArtifact(
                                artifact_id="thread:auto:yield000",
                                artifact_type="email_thread",
                                display_name="Yield-starved thread",
                                thread_id="yield-thread-1",
                                inferred_workstream="acme",
                                confidence=1.0,
                                confidence_source="pm_confirmed",
                                pm_confirmed=True,
                                promoted_to_workstreams_yaml=False,
                                first_seen=date(2026, 5, 1),
                                last_seen=date(2026, 5, 10),
                                signal_yield_last_3=(0, 0, 0),
                        ),
                ),
                programs_root=programs_root,
                as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        )
        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=0,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=3,
                archived_journal_files=0,
                background_proposals=0,
                gather_flags={"kusto": False, "workiq": True, "icm": False},
                channels={
                        "ado": {"active": True, "signal_count": 4, "expected_min": 1, "meets_expected_min": True},
                        "workiq": {"active": True, "signal_count": 9, "expected_min": 8, "meets_expected_min": True},
                        "transcript": {"active": True, "signal_count": 2, "expected_min": 2, "meets_expected_min": True, "configured_series": 2, "series_id_null": 0},
                        "icm": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed"},
                },
                m365_discovery={
                        "active": True,
                        "query_plan_count": 3,
                        "configured_series": 2,
                        "series_id_null": 0,
                        "configured_chats": 1,
                        "chat_thread_id_null": 0,
                        "observed_thread_ids": 2,
                        "untracked_observed_thread_ids": 0,
                        "signals_without_thread_id": 0,
                        "signals_without_workstream": 0,
                        "registry_file_present": True,
                        "feedback_file_present": True,
                        "registry_bootstrapped": True,
                },
                programs_root=programs_root,
        )

        report = run_doctor(
                edition_name=EDITION_NAME,
                channels=True,
                reports_root=reports_root,
                archive_root=archive_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Channels"].status == "warn"
        assert "1 artifact(s) blocked on recent signal yield." in checks["Channels"].detail
        assert checks["Channels"].metadata is not None
        assert checks["Channels"].metadata["m365_registry_review"]["promotion_blocked_signal_yield_count"] == 1
        assert checks["M365 Registry Review"].status == "warn"
        assert "1 artifact(s) are blocked on recent signal yield." in checks["M365 Registry Review"].detail
        assert checks["M365 Registry Promotion"].status == "warn"
        assert "1 artifact(s) are promotion-blocked by insufficient recent signal yield." in checks["M365 Registry Promotion"].detail


def test_run_doctor_channels_surfaces_keyword_expansion_suggestions(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        workstreams_path = programs_root / "acme" / "workstreams.yaml"
        workstreams_document = yaml.safe_load(workstreams_path.read_text(encoding="utf-8"))
        assert isinstance(workstreams_document, dict)
        workstreams = workstreams_document.get("workstreams")
        assert isinstance(workstreams, list)
        for workstream in workstreams:
                if not isinstance(workstream, dict) or workstream.get("id") != "acme":
                        continue
                signal_sources = workstream.get("signal_sources")
                assert isinstance(signal_sources, dict)
                signal_sources["workiq_keywords"] = ["SCHIE"]
        workstreams_path.write_text(yaml.safe_dump(workstreams_document, sort_keys=False), encoding="utf-8")

        upsert_m365_registry_artifacts(
                "acme",
                artifacts=(
                        M365RegistryArtifact(
                                artifact_id="thread:auto:pilot001",
                                artifact_type="email_thread",
                                display_name="Pilot readiness sync",
                                thread_id="pilot-thread-1",
                                inferred_workstream="acme",
                                confidence=1.0,
                                confidence_source="pm_confirmed",
                                pm_confirmed=True,
                                promoted_to_workstreams_yaml=False,
                                first_seen=date(2026, 5, 1),
                                last_seen=date(2026, 5, 10),
                                signal_yield_last_3=(1, 1, 1),
                        ),
                        M365RegistryArtifact(
                                artifact_id="thread:auto:pilot002",
                                artifact_type="email_thread",
                                display_name="Pilot readiness review",
                                thread_id="pilot-thread-2",
                                inferred_workstream="acme",
                                confidence=1.0,
                                confidence_source="pm_confirmed",
                                pm_confirmed=True,
                                promoted_to_workstreams_yaml=False,
                                first_seen=date(2026, 5, 2),
                                last_seen=date(2026, 5, 10),
                                signal_yield_last_3=(1, 1, 1),
                        ),
                ),
                programs_root=programs_root,
                as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        )
        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=0,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=3,
                archived_journal_files=0,
                background_proposals=0,
                gather_flags={"kusto": False, "workiq": True, "icm": False},
                channels={
                        "ado": {"active": True, "signal_count": 4, "expected_min": 1, "meets_expected_min": True},
                        "workiq": {"active": True, "signal_count": 9, "expected_min": 8, "meets_expected_min": True},
                        "transcript": {"active": True, "signal_count": 2, "expected_min": 2, "meets_expected_min": True, "configured_series": 2, "series_id_null": 0},
                        "icm": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed"},
                },
                m365_discovery={
                        "active": True,
                        "query_plan_count": 3,
                        "configured_series": 2,
                        "series_id_null": 0,
                        "configured_chats": 1,
                        "chat_thread_id_null": 0,
                        "observed_thread_ids": 2,
                        "untracked_observed_thread_ids": 0,
                        "signals_without_thread_id": 0,
                        "signals_without_workstream": 0,
                        "registry_file_present": True,
                        "feedback_file_present": True,
                        "registry_bootstrapped": True,
                },
                programs_root=programs_root,
        )

        report = run_doctor(
                edition_name=EDITION_NAME,
                channels=True,
                reports_root=reports_root,
                archive_root=archive_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Channels"].metadata is not None
        assert checks["Channels"].metadata["m365_registry_review"]["keyword_suggestion_count"] == 1
        assert checks["Channels"].metadata["m365_registry_review"]["keyword_suggestions_by_workstream"] == {"acme": ["pilot readiness"]}
        assert checks["M365 Registry Review"].status == "warn"
        assert "keyword expansion suggestions -> acme: pilot readiness" in checks["M365 Registry Review"].detail


def test_run_doctor_channels_builds_keyword_suggestions_from_approved_workiq_signal_text(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        workstreams_path = programs_root / "acme" / "workstreams.yaml"
        workstreams_document = yaml.safe_load(workstreams_path.read_text(encoding="utf-8"))
        assert isinstance(workstreams_document, dict)
        workstreams = workstreams_document.get("workstreams")
        assert isinstance(workstreams, list)
        for workstream in workstreams:
                if not isinstance(workstream, dict) or workstream.get("id") != "acme":
                        continue
                signal_sources = workstream.get("signal_sources")
                assert isinstance(signal_sources, dict)
                signal_sources["workiq_keywords"] = ["SCHIE"]
        workstreams_path.write_text(yaml.safe_dump(workstreams_document, sort_keys=False), encoding="utf-8")

        upsert_m365_registry_artifacts(
                "acme",
                artifacts=(
                        M365RegistryArtifact(
                                artifact_id="thread:auto:pilot-corpus-1",
                                artifact_type="email_thread",
                                display_name="Confirmed workstream thread",
                                thread_id="pilot-corpus-thread-1",
                                inferred_workstream="acme",
                                confidence=1.0,
                                confidence_source="pm_confirmed",
                                pm_confirmed=True,
                                promoted_to_workstreams_yaml=False,
                                first_seen=date(2026, 5, 1),
                                last_seen=date(2026, 5, 10),
                                signal_yield_last_3=(1, 1, 1),
                        ),
                        M365RegistryArtifact(
                                artifact_id="thread:auto:pilot-corpus-2",
                                artifact_type="email_thread",
                                display_name="Second confirmed workstream thread",
                                thread_id="pilot-corpus-thread-2",
                                inferred_workstream="acme",
                                confidence=1.0,
                                confidence_source="pm_confirmed",
                                pm_confirmed=True,
                                promoted_to_workstreams_yaml=False,
                                first_seen=date(2026, 5, 2),
                                last_seen=date(2026, 5, 10),
                                signal_yield_last_3=(1, 1, 1),
                        ),
                ),
                programs_root=programs_root,
                as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        )
        for signal_id, thread_id, signal_text in (
                ("sig-pilot-corpus-1", "pilot-corpus-thread-1", "Pilot readiness blockers were reviewed in the weekly mail thread."),
                ("sig-pilot-corpus-2", "pilot-corpus-thread-2", "Pilot readiness owners closed the remaining blockers after review."),
        ):
                signal_timestamp = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
                append_signal(
                        Signal(
                                id=signal_id,
                                timestamp=signal_timestamp,
                                source="workiq/email",
                                program_id="acme",
                                workstream_id="acme",
                                entity_refs=(),
                                text=signal_text,
                                raw_ref=f"message:{signal_id}",
                                confidence=Confidence.MEDIUM,
                                metadata={"source_type": "email", "message_id": signal_id},
                                thread_id=thread_id,
                                review_policy=ReviewPolicy.PENDING,
                        ),
                        programs_root=programs_root,
                        partition_at=signal_timestamp,
                )
                append_review_decision(
                        "acme",
                        SignalReviewDecision(
                                signal_id=signal_id,
                                decision="approved",
                                reviewed_at=signal_timestamp + timedelta(hours=1),
                                reviewed_by="operator",
                        ),
                        programs_root=programs_root,
                )
        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=0,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=3,
                archived_journal_files=0,
                background_proposals=0,
                gather_flags={"kusto": False, "workiq": True, "icm": False},
                channels={
                        "ado": {"active": True, "signal_count": 4, "expected_min": 1, "meets_expected_min": True},
                        "workiq": {"active": True, "signal_count": 9, "expected_min": 8, "meets_expected_min": True},
                        "transcript": {"active": True, "signal_count": 2, "expected_min": 2, "meets_expected_min": True, "configured_series": 2, "series_id_null": 0},
                        "icm": {"active": False, "signal_count": 0, "expected_min": 0, "meets_expected_min": False, "reason_not_active": "flag_not_passed"},
                },
                m365_discovery={
                        "active": True,
                        "query_plan_count": 3,
                        "configured_series": 2,
                        "series_id_null": 0,
                        "configured_chats": 1,
                        "chat_thread_id_null": 0,
                        "observed_thread_ids": 2,
                        "untracked_observed_thread_ids": 0,
                        "signals_without_thread_id": 0,
                        "signals_without_workstream": 0,
                        "registry_file_present": True,
                        "feedback_file_present": True,
                        "registry_bootstrapped": True,
                },
                programs_root=programs_root,
        )

        report = run_doctor(
                edition_name=EDITION_NAME,
                channels=True,
                reports_root=reports_root,
                archive_root=archive_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Channels"].metadata is not None
        assert checks["Channels"].metadata["m365_registry_review"]["approved_m365_signal_corpus_count"] == 2
        assert checks["Channels"].metadata["m365_registry_review"]["keyword_suggestion_count"] == 1
        assert checks["Channels"].metadata["m365_registry_review"]["keyword_suggestions_by_workstream"] == {"acme": ["pilot readiness"]}
        assert checks["M365 Registry Review"].status == "warn"
        assert "keyword expansion suggestions -> acme: pilot readiness" in checks["M365 Registry Review"].detail


def test_run_doctor_semantic_index_warns_when_dirty_and_stale(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        program_path = programs_root / "acme" / "program.yaml"
        program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        assert isinstance(program_document, dict)
        ai_document = program_document.get("ai")
        if not isinstance(ai_document, dict):
                ai_document = {}
        ai_document["semantic_index"] = True
        program_document["ai"] = ai_document
        program_path.write_text(yaml.safe_dump(program_document, sort_keys=False), encoding="utf-8")

        semantic_archive_root = get_archive_root(EDITION_NAME, archive_root)
        semantic_archive_root.mkdir(parents=True, exist_ok=True)
        source_archive_root = repo_root / "programs" / "acme" / "archive" / EDITION_NAME
        shutil.copytree(source_archive_root, semantic_archive_root, dirs_exist_ok=True)

        rebuild_archive_semantic_index(EDITION_NAME, archive_root=archive_root)

        state_path = get_semantic_index_state_path(EDITION_NAME, archive_root=archive_root)
        state_document = json.loads(state_path.read_text(encoding="utf-8"))
        state_document["editions"][EDITION_NAME]["last_built_at"] = "2026-04-20T09:00:00+00:00"
        state_document["editions"][EDITION_NAME]["semantic_index_dirty"] = True
        state_document["editions"][EDITION_NAME]["dirty_reason"] = "confirm issue 077: fts refresh failed"
        state_path.write_text(json.dumps(state_document, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        report = run_doctor(
                edition_name=EDITION_NAME,
                semantic_index=True,
                editions_root=reports_root.parent / "editions",
                programs_root=programs_root,
                archive_root=archive_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Semantic Freshness"].status == "warn"
        assert "latest confirmed issue" in checks["Semantic Freshness"].detail
        assert checks["Semantic Dirty"].status == "warn"
        assert "semantic_index_dirty=true" in checks["Semantic Dirty"].detail


def test_run_doctor_storage_warns_when_archival_is_due(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reality_db_root = tmp_path / "reality-db"

        _set_program_storage_backend(programs_root, program_id="acme", storage_backend="sqlite")
        _write_confirmed_archive_entries(
                archive_root,
                edition_name=EDITION_NAME,
                generated_ats=tuple(
                        datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc) + timedelta(days=7 * offset)
                        for offset in range(8)
                ),
        )

        journal_dir = programs_root / "acme" / "journal"
        journal_dir.mkdir(parents=True, exist_ok=True)
        (journal_dir / "2026-W10.jsonl").write_text('{"signal_id":"s1"}\n', encoding="utf-8")
        (journal_dir / "2026-W11.jsonl").write_text('{"signal_id":"s2"}\n', encoding="utf-8")
        (journal_dir / "reviews.jsonl").write_text('{"record_type":"review"}\n', encoding="utf-8")

        trajectory_dir = programs_root / "acme" / "trajectories"
        trajectory_dir.mkdir(parents=True, exist_ok=True)
        (trajectory_dir / "1001.jsonl").write_text('{"date":"2026-05-01"}\n', encoding="utf-8")

        SQLiteTrajectoryStore(programs_root=programs_root).append(
                "acme",
                1001,
                TrajectoryPoint(
                        date=date(2026, 5, 1),
                        state="Active",
                        assigned_to="owner@example.com",
                        target_date=None,
                        risk_level=None,
                        area_path="Area\\Path",
                        tags=(),
                        risk_assessment=None,
                        risk_assessment_comment=None,
                ),
        )
        RealityStore("acme", db_root=reality_db_root).initialize()

        report = run_doctor(
                edition_name=EDITION_NAME,
                storage=True,
                editions_root=reports_root.parent / "editions",
                programs_root=programs_root,
                archive_root=archive_root,
                reality_db_root=reality_db_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Storage Retention"].status == "warn"
        assert "vertex archive-journals --program acme --before 2026-W17" in checks["Storage Retention"].detail
        assert checks["Trajectory Storage"].status == "ok"
        assert "1 work-item trajectory file" in checks["Trajectory Storage"].detail
        assert checks["Program SQLite"].status == "ok"
        assert "storage_backend=sqlite" in checks["Program SQLite"].detail
        assert checks["Reality DB"].status == "warn"
        assert "journal_mode=wal" in checks["Reality DB"].detail


def test_run_doctor_storage_warns_when_reality_db_is_not_under_default_root(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        custom_reality_root = tmp_path / "custom-reality"

        RealityStore("acme", db_root=custom_reality_root).initialize()

        report = run_doctor(
                edition_name=EDITION_NAME,
                storage=True,
                editions_root=reports_root.parent / "editions",
                programs_root=reports_root.parent / "programs",
                reality_db_root=custom_reality_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Reality DB"].status == "warn"
        assert "outside" in checks["Reality DB"].detail


def test_run_doctor_checkpoints_warns_when_confirmed_issue_has_no_checkpoints(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"

        _write_confirmed_archive_entries(
                archive_root,
                edition_name=EDITION_NAME,
                generated_ats=(datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc),),
        )

        report = run_doctor(
                edition_name=EDITION_NAME,
                checkpoints=True,
                editions_root=reports_root.parent / "editions",
                programs_root=reports_root.parent / "programs",
                archive_root=archive_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Checkpoint Inventory"].status == "warn"
        assert "vertex confirm" in checks["Checkpoint Inventory"].detail
        assert "Checkpoint Coverage" not in checks


def test_run_doctor_checkpoints_fails_when_latest_checkpoint_misses_live_paths(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"

        _write_confirmed_archive_entries(
                archive_root,
                edition_name=EDITION_NAME,
                generated_ats=(datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc),),
        )
        create_checkpoint_snapshot("acme", 1, programs_root=programs_root)
        (programs_root / "acme" / "chronicle.jsonl").write_text('{"event":"after-checkpoint"}\n', encoding="utf-8")

        report = run_doctor(
                edition_name=EDITION_NAME,
                checkpoints=True,
                editions_root=reports_root.parent / "editions",
                programs_root=programs_root,
                archive_root=archive_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Checkpoint Inventory"].status == "ok"
        assert checks["Checkpoint Coverage"].status == "fail"
        assert "chronicle.jsonl" in checks["Checkpoint Coverage"].detail


def test_run_doctor_checkpoints_passes_when_latest_checkpoint_covers_live_paths(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        programs_root = reports_root.parent / "programs"
        archive_root = tmp_path / "archive"

        _write_confirmed_archive_entries(
                archive_root,
                edition_name=EDITION_NAME,
                generated_ats=(datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc),),
        )
        (programs_root / "acme" / "chronicle.jsonl").write_text('{"event":"before-checkpoint"}\n', encoding="utf-8")
        create_checkpoint_snapshot("acme", 1, programs_root=programs_root)

        report = run_doctor(
                edition_name=EDITION_NAME,
                checkpoints=True,
                editions_root=reports_root.parent / "editions",
                programs_root=programs_root,
                archive_root=archive_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Checkpoint Inventory"].status == "ok"
        assert checks["Checkpoint Coverage"].status == "ok"
        assert "captures all" in checks["Checkpoint Coverage"].detail


def test_run_doctor_warns_when_active_autonomy_audit_rows_exceed_threshold(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        program_path = programs_root / "acme" / "program.yaml"
        program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        assert isinstance(program_document, dict)
        audit_document = program_document.get("audit")
        if not isinstance(audit_document, dict):
                audit_document = {}
        audit_document["archive_threshold_rows"] = 1
        audit_document["retention_days"] = 30
        program_document["audit"] = audit_document
        program_path.write_text(yaml.safe_dump(program_document, sort_keys=False), encoding="utf-8")

        audit_path = get_program_autonomy_audit_path("acme", programs_root=programs_root)
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
                "{\"action_id\": \"a1\"}\n{\"action_id\": \"a2\"}\n",
                encoding="utf-8",
        )

        report = run_doctor(
                edition_name=EDITION_NAME,
                reports_root=reports_root,
                archive_root=archive_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Audit Hygiene"].status == "warn"
        assert "Active autonomy audit rows: 2 exceeds threshold 1" in checks["Audit Hygiene"].detail
        assert "configured retention 30 day(s)" in checks["Audit Hygiene"].detail
        assert "vertex audit archive --program acme --before <YYYY-MM-DD>" in checks["Audit Hygiene"].detail
        assert checks["Audit Hygiene"].metadata is not None
        assert checks["Audit Hygiene"].metadata["row_count"] == 2
        assert checks["Audit Hygiene"].metadata["archive_threshold_rows"] == 1
        assert checks["Audit Hygiene"].metadata["retention_days"] == 30


def test_run_doctor_surfaces_capability_review_state(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        (programs_root / "acme" / "capability_status.yaml").write_text(
                "\n".join(
                        (
                                "schema_version: '1.0'",
                                "capabilities:",
                                "  - id: kusto_activation",
                                "    status: in_progress",
                                "    summary: Kusto activation is explicitly in progress for this doctor test.",
                                "    degradation: Live cluster validation is still pending.",
                                "    last_reviewed_on: 2026-05-17",
                                "  - id: m365_activation",
                                "    status: deferred",
                                "    summary: M365 activation is explicitly deferred for this doctor test.",
                                "    degradation: WorkIQ enrichment remains inactive.",
                                "    last_reviewed_on: 2026-05-15",
                        )
                )
                + "\n",
                encoding="utf-8",
        )

        report = run_doctor(
                edition_name=EDITION_NAME,
                fix=True,
                reports_root=reports_root,
                archive_root=archive_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        capability_check = next(check for check in report.checks if check.label == "Capability Reviews")
        assert capability_check.status == "ok"
        assert "Kusto activation in progress" in capability_check.detail
        assert "M365 activation deferred" in capability_check.detail
        assert "Latest review: 2026-05-17" in capability_check.detail


def test_run_doctor_warns_on_missing_capability_review_dates(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)

        (programs_root / "acme" / "capability_status.yaml").write_text(
                "\n".join(
                        (
                                "schema_version: '1.0'",
                                "capabilities:",
                                "  - id: kusto_activation",
                                "    status: in_progress",
                                "    summary: Kusto activation is explicitly in progress for this doctor test.",
                                "    degradation: Live cluster validation is still pending.",
                                "    last_reviewed_on: 2026-05-17",
                                "  - id: m365_activation",
                                "    status: deferred",
                                "    summary: M365 activation is explicitly deferred for this doctor test.",
                                "    degradation: WorkIQ enrichment remains inactive.",
                        )
                )
                + "\n",
                encoding="utf-8",
        )

        report = run_doctor(
                edition_name=EDITION_NAME,
                fix=True,
                reports_root=reports_root,
                archive_root=archive_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        capability_check = next(check for check in report.checks if check.label == "Capability Reviews")
        assert capability_check.status == "warn"
        assert "M365 activation" in capability_check.detail
        assert "Latest review: 2026-05-17" in capability_check.detail


def test_run_doctor_surfaces_hygiene_nudge_readiness(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path, edition_names=("acme_weekly", "nova_nudge"))
        archive_root = tmp_path / "archive"
        reset_overrides_to_seed_state(reports_root)

        report = run_doctor(
                edition_name="nova_nudge",
                reports_root=reports_root,
                archive_root=archive_root,
                editions_root=tmp_path / "editions",
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        hygiene_check = next(check for check in report.checks if check.label == "Hygiene Nudge")
        assert hygiene_check.status == "ok"
        assert "stale_business_days=5" in hygiene_check.detail
        assert "coverage alerts enabled" in hygiene_check.detail
        assert hygiene_check.metadata is not None
        assert hygiene_check.metadata["coverage_alerts_enabled"] is True
        assert hygiene_check.metadata["coverage_workstreams"] == ["acme", "dd_on_pf"]
        assert hygiene_check.metadata["missing_workstream_leads"] == []


def test_run_doctor_warns_when_hygiene_coverage_alert_leads_are_unroutable(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path, edition_names=("acme_weekly", "nova_nudge"))
        archive_root = tmp_path / "archive"
        reset_overrides_to_seed_state(reports_root)
        workstreams_path = tmp_path / "programs" / "acme" / "workstreams.yaml"
        workstreams_doc = yaml.safe_load(workstreams_path.read_text(encoding="utf-8"))
        assert isinstance(workstreams_doc, dict)
        workstreams = workstreams_doc.get("workstreams")
        assert isinstance(workstreams, list)
        for entry in workstreams:
                if not isinstance(entry, dict):
                        continue
                entry["dri_email"] = None
                entry["alternate_owner"] = None
        workstreams_path.write_text(yaml.safe_dump(workstreams_doc, sort_keys=False), encoding="utf-8")

        report = run_doctor(
                edition_name="nova_nudge",
                reports_root=reports_root,
                archive_root=archive_root,
                editions_root=tmp_path / "editions",
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        hygiene_check = next(check for check in report.checks if check.label == "Hygiene Nudge")
        assert hygiene_check.status == "warn"
        assert "missing lead email resolution" in hygiene_check.detail
        assert hygiene_check.metadata is not None
        assert hygiene_check.metadata["missing_workstream_leads"] == ["acme", "dd_on_pf"]


def test_run_doctor_warns_when_hygiene_nudge_edition_is_not_email_configured(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path, edition_names=("acme_weekly", "nova_nudge"))
        archive_root = tmp_path / "archive"
        reset_overrides_to_seed_state(reports_root)
        edition_path = tmp_path / "programs" / "acme" / "editions" / "nova_nudge.yaml"
        edition_doc = yaml.safe_load(edition_path.read_text(encoding="utf-8"))
        assert isinstance(edition_doc, dict)
        edition_doc["send_day"] = "tuesday"
        distribution = edition_doc.get("distribution")
        assert isinstance(distribution, dict)
        distribution["channels"] = ["teams"]
        author = edition_doc.get("author")
        assert isinstance(author, dict)
        author["email"] = ""
        edition_path.write_text(yaml.safe_dump(edition_doc, sort_keys=False), encoding="utf-8")

        report = run_doctor(
                edition_name="nova_nudge",
                reports_root=reports_root,
                archive_root=archive_root,
                programs_root=tmp_path / "programs",
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        hygiene_check = next(check for check in report.checks if check.label == "Hygiene Nudge")
        assert hygiene_check.status == "warn"
        assert "distribution.channels must include email" in hygiene_check.detail
        assert "author.email is required" in hygiene_check.detail
        assert hygiene_check.metadata is not None
        assert hygiene_check.metadata["send_day"] == "tuesday"
        assert hygiene_check.metadata["distribution_channels"] == ["teams"]
        assert hygiene_check.metadata["author_email"] is None


def test_run_doctor_check_auth_reports_ado_graph_and_agency_status(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        programs_root = reports_root.parent / "programs"
        knowledge_root = reports_root.parent / "knowledge"

        golden_queries_path = knowledge_root / "golden_queries.yaml"
        golden_queries_doc = yaml.safe_load(golden_queries_path.read_text(encoding="utf-8"))
        for query in golden_queries_doc["queries"]:
                if query.get("id") == "icm-mttr":
                        query["validated"] = False
                        break
        golden_queries_path.write_text(
                yaml.safe_dump(golden_queries_doc, sort_keys=False, allow_unicode=False),
                encoding="utf-8",
        )

        (programs_root / "acme" / "capability_status.yaml").write_text(
                "\n".join(
                        (
                                "schema_version: '1.0'",
                                "capabilities:",
                                "  - id: kusto_activation",
                                "    status: in_progress",
                                "    summary: Kusto activation is explicitly in progress for this auth doctor test.",
                                "    degradation: Live cluster validation is still pending.",
                                "    last_reviewed_on: 2026-05-17",
                                "  - id: m365_activation",
                                "    status: deferred",
                                "    summary: M365 activation is explicitly deferred for this auth doctor test.",
                                "    degradation: WorkIQ enrichment remains inactive.",
                                "    last_reviewed_on: 2026-05-15",
                        )
                )
                + "\n",
                encoding="utf-8",
        )

        monkeypatch.setenv("GRAPH_TENANT_ID", "tenant-id")
        monkeypatch.setenv("GRAPH_CLIENT_ID", "client-id")
        monkeypatch.setattr("src.commands.doctor.find_spec", lambda module_name: object() if module_name in {"azure.identity", "requests"} else None)

        class _ReadyAgencyBridge:
                def probe(self) -> AgencyCapabilities:
                        return AgencyCapabilities(available=True, has_workiq=True, has_bluebird=True, has_ado=True, has_icm=True, tier="msft")

        monkeypatch.setattr("src.commands.doctor.AgencyBridge", _ReadyAgencyBridge)

        report = run_doctor(
                edition_name=EDITION_NAME,
                check_auth=True,
                reports_root=reports_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
                kusto_probe=lambda queries: None,
        )

        checks = {check.label: check for check in report.checks}
        assert tuple(checks) == ("ADO Access", "Token", "Mail Preview", "Agency CLI", "WorkIQ Access", "Kusto Access", "Kusto Validation", "IcM via Kusto", "Capability Reviews")
        assert checks["ADO Access"].status == "ok"
        assert checks["Token"].status == "ok"
        assert checks["Mail Preview"].status == "ok"
        assert checks["Agency CLI"].status == "ok"
        assert checks["WorkIQ Access"].status == "ok"
        assert checks["Kusto Access"].status == "ok"
        assert checks["Kusto Access"].metadata is not None
        assert "https://adventure.kusto.windows.net/xdataanalytics" in checks["Kusto Access"].metadata["cluster_targets"]
        assert "https://icmcluster.kusto.windows.net/IcMDataWarehouse" in checks["Kusto Access"].metadata["cluster_targets"]
        assert checks["Kusto Validation"].status == "warn"
        assert "icm-mttr" in checks["Kusto Validation"].metadata["excluded_query_ids"]
        assert "icm-mttr" not in checks["Kusto Validation"].metadata["unvalidated_query_ids"]
        assert checks["IcM via Kusto"].status == "warn"
        assert "icm-mttr" in checks["IcM via Kusto"].detail
        assert checks["Capability Reviews"].status == "warn"
        assert "still incomplete" in checks["Capability Reviews"].detail
        assert "Latest review: 2026-05-17" in checks["Capability Reviews"].detail


def test_doctor_cli_check_auth(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        programs_root = reports_root.parent / "programs"

        (programs_root / "acme" / "capability_status.yaml").write_text(
                "\n".join(
                        (
                                "schema_version: '1.0'",
                                "capabilities:",
                                "  - id: kusto_activation",
                                "    status: in_progress",
                                "    summary: Kusto activation is explicitly in progress for this auth doctor test.",
                                "    degradation: Live cluster validation is still pending.",
                                "    last_reviewed_on: 2026-05-17",
                                "  - id: m365_activation",
                                "    status: deferred",
                                "    summary: M365 activation is explicitly deferred for this auth doctor test.",
                                "    degradation: WorkIQ enrichment remains inactive.",
                                "    last_reviewed_on: 2026-05-15",
                        )
                )
                + "\n",
                encoding="utf-8",
        )

        monkeypatch.setenv("GRAPH_TENANT_ID", "tenant-id")
        monkeypatch.setenv("GRAPH_CLIENT_ID", "client-id")
        monkeypatch.setattr("src.commands.doctor.REPORTS_ROOT", reports_root)
        monkeypatch.setattr("src.commands.doctor.find_spec", lambda module_name: object() if module_name in {"azure.identity", "requests"} else None)
        monkeypatch.setattr(
                "src.commands.doctor._probe_ado_access",
                lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        class _ReadyAgencyBridge:
                def probe(self) -> AgencyCapabilities:
                        return AgencyCapabilities(available=True, has_workiq=True, has_bluebird=True, has_ado=True, has_icm=True, tier="msft")

        monkeypatch.setattr("src.commands.doctor.AgencyBridge", _ReadyAgencyBridge)
        monkeypatch.setattr("src.commands.doctor.build_live_kusto_query_probe", lambda **kwargs: (lambda _queries: None))

        result = runner.invoke(app, ["doctor", "--edition", EDITION_NAME, "--check-auth"])

        assert result.exit_code == 0
        assert "ADO Access" in result.stdout
        assert "Token" in result.stdout
        assert "Mail Preview" in result.stdout
        assert "Agency CLI" in result.stdout
        assert "WorkIQ Access" in result.stdout
        assert "Kusto Access" in result.stdout
        assert "Kusto Validation" in result.stdout
        assert "IcM via Kusto" in result.stdout
        assert "Capability Reviews" in result.stdout
        assert "WARN" in result.stdout
        assert "Overall: HEALTHY" in result.stdout


def test_run_doctor_check_auth_reports_kusto_probe_failure(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        knowledge_root = reports_root.parent / "knowledge"

        golden_queries_path = knowledge_root / "golden_queries.yaml"
        golden_queries_doc = yaml.safe_load(golden_queries_path.read_text(encoding="utf-8"))
        for query in golden_queries_doc["queries"]:
                if query.get("id") == "icm-mttr":
                        query["validated"] = False
                        break
        golden_queries_path.write_text(
                yaml.safe_dump(golden_queries_doc, sort_keys=False, allow_unicode=False),
                encoding="utf-8",
        )

        monkeypatch.setenv("GRAPH_TENANT_ID", "tenant-id")
        monkeypatch.setenv("GRAPH_CLIENT_ID", "client-id")
        monkeypatch.setattr("src.commands.doctor.find_spec", lambda module_name: object() if module_name in {"azure.identity", "requests"} else None)

        class _ReadyAgencyBridge:
                def probe(self) -> AgencyCapabilities:
                        return AgencyCapabilities(available=True, has_workiq=True, has_bluebird=True, has_ado=True, has_icm=True, tier="msft")

        monkeypatch.setattr("src.commands.doctor.AgencyBridge", _ReadyAgencyBridge)

        report = run_doctor(
                edition_name=EDITION_NAME,
                check_auth=True,
                reports_root=reports_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
                kusto_probe=lambda queries: (_ for _ in ()).throw(QueryError("Kusto pre-flight probe failed for cluster/db: auth timeout")),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["WorkIQ Access"].status == "ok"
        assert checks["Kusto Access"].status == "fail"
        assert "auth timeout" in checks["Kusto Access"].detail
        assert checks["Kusto Validation"].status == "warn"
        assert "icm-mttr" in checks["Kusto Validation"].metadata["excluded_query_ids"]
        assert "icm-mttr" not in checks["Kusto Validation"].metadata["unvalidated_query_ids"]
        assert checks["IcM via Kusto"].status == "warn"
        assert checks["Capability Reviews"].status == "warn"


def test_run_doctor_check_auth_warns_when_workiq_server_is_missing(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)

        monkeypatch.setenv("GRAPH_TENANT_ID", "tenant-id")
        monkeypatch.setenv("GRAPH_CLIENT_ID", "client-id")
        monkeypatch.setattr("src.commands.doctor.find_spec", lambda module_name: object() if module_name in {"azure.identity", "requests"} else None)

        class _AgencyWithoutWorkIQ:
                def probe(self) -> AgencyCapabilities:
                        return AgencyCapabilities(available=True, has_ado=True, tier="enterprise")

        monkeypatch.setattr("src.commands.doctor.AgencyBridge", _AgencyWithoutWorkIQ)

        report = run_doctor(
                edition_name=EDITION_NAME,
                check_auth=True,
                reports_root=reports_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
                kusto_probe=lambda queries: None,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Agency CLI"].status == "ok"
        assert checks["WorkIQ Access"].status == "warn"
        assert "WorkIQ MCP server" in checks["WorkIQ Access"].detail


def test_run_doctor_check_auth_warns_when_workiq_tool_inventory_is_incomplete(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)

        monkeypatch.setenv("GRAPH_TENANT_ID", "tenant-id")
        monkeypatch.setenv("GRAPH_CLIENT_ID", "client-id")
        monkeypatch.setattr("src.commands.doctor.find_spec", lambda module_name: object() if module_name in {"azure.identity", "requests"} else None)

        class _AgencyWithPartialWorkIQ:
                def probe(self) -> AgencyCapabilities:
                        return AgencyCapabilities(
                                available=True,
                                has_workiq=True,
                                has_ado=True,
                                tier="msft",
                                server_tools={
                                        "workiq": (
                                                "search_emails",
                                                "search_teams",
                                        )
                                },
                        )

        monkeypatch.setattr("src.commands.doctor.AgencyBridge", _AgencyWithPartialWorkIQ)

        report = run_doctor(
                edition_name=EDITION_NAME,
                check_auth=True,
                reports_root=reports_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
                kusto_probe=lambda queries: None,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["WorkIQ Access"].status == "warn"
        assert "missing required tool(s)" in checks["WorkIQ Access"].detail
        assert "get_transcript" in checks["WorkIQ Access"].detail
        assert "get_meetings" in checks["WorkIQ Access"].detail
        assert checks["WorkIQ Access"].metadata is not None
        assert checks["WorkIQ Access"].metadata["available_tools"] == ("search_emails", "search_teams")
        assert checks["WorkIQ Access"].metadata["missing_tools"] == (
                "get_transcript",
                "get_meetings",
        )


def test_run_doctor_check_auth_warns_when_workiq_queries_are_missing(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        programs_root = reports_root.parent / "programs"
        program_path = programs_root / "acme" / "program.yaml"
        program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        program_doc["m365"] = {"enabled": True, "prefer_agency": True}
        program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

        monkeypatch.setenv("GRAPH_TENANT_ID", "tenant-id")
        monkeypatch.setenv("GRAPH_CLIENT_ID", "client-id")
        monkeypatch.setattr("src.commands.doctor.find_spec", lambda module_name: object() if module_name in {"azure.identity", "requests"} else None)

        class _ReadyAgencyBridge:
                def probe(self) -> AgencyCapabilities:
                        return AgencyCapabilities(available=True, has_workiq=True, has_ado=True, tier="msft")

        monkeypatch.setattr("src.commands.doctor.AgencyBridge", _ReadyAgencyBridge)

        report = run_doctor(
                edition_name=EDITION_NAME,
                check_auth=True,
                reports_root=reports_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=1,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 1 sampled item in scope)",
                ),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Agency CLI"].status == "ok"
        assert checks["WorkIQ Access"].status == "warn"
        assert "no m365.workiq_queries configured" in checks["WorkIQ Access"].detail
        assert checks["WorkIQ Access"].metadata is not None
        assert checks["WorkIQ Access"].metadata["m365_enabled"] is True
        assert checks["WorkIQ Access"].metadata["query_count"] == 0


def test_run_doctor_check_auth_warns_when_kusto_is_disabled(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        programs_root = reports_root.parent / "programs"
        program_path = programs_root / "acme" / "program.yaml"
        program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        program_doc["kusto"] = {"enabled": False}
        program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

        monkeypatch.setenv("GRAPH_TENANT_ID", "tenant-id")
        monkeypatch.setenv("GRAPH_CLIENT_ID", "client-id")
        monkeypatch.setattr("src.commands.doctor.find_spec", lambda module_name: object() if module_name in {"azure.identity", "requests"} else None)

        class _ReadyAgencyBridge:
                def probe(self) -> AgencyCapabilities:
                        return AgencyCapabilities(available=True, has_workiq=True, has_ado=True, tier="msft")

        monkeypatch.setattr("src.commands.doctor.AgencyBridge", _ReadyAgencyBridge)

        report = run_doctor(
                edition_name=EDITION_NAME,
                check_auth=True,
                reports_root=reports_root,
                ado_probe=lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=1,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 1 sampled item in scope)",
                ),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Kusto Access"].status == "warn"
        assert "kusto.enabled=false" in checks["Kusto Access"].detail
        assert checks["Kusto Access"].metadata is not None
        assert checks["Kusto Access"].metadata["kusto_enabled"] is False
        assert checks["Kusto Access"].metadata["cluster_targets"] == []


def test_doctor_cli_supports_json_and_csv(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        archive_root = tmp_path / "archive"
        reset_overrides_to_seed_state(reports_root)

        monkeypatch.delenv("GRAPH_TENANT_ID", raising=False)
        monkeypatch.delenv("GRAPH_CLIENT_ID", raising=False)
        monkeypatch.setattr("src.commands.doctor.REPORTS_ROOT", reports_root)
        monkeypatch.setattr("src.commands.doctor.ARCHIVE_ROOT", archive_root)
        monkeypatch.setattr(
                "src.commands.doctor._probe_ado_access",
                lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        json_result = runner.invoke(app, ["doctor", "--edition", EDITION_NAME, "--fix", "--format", "json"])

        assert json_result.exit_code == 0
        payload = json.loads(json_result.stdout)
        assert payload["edition"] == EDITION_NAME
        assert payload["overall"] == "HEALTHY"
        assert payload["failures"] == 0
        assert any(check["label"] == "ADO Access" for check in payload["checks"])

        csv_result = runner.invoke(app, ["doctor", "--edition", EDITION_NAME, "--fix", "--format", "csv"])

        assert csv_result.exit_code == 0
        lines = csv_result.stdout.strip().splitlines()
        assert lines[0] == "edition,overall,warnings,failures,label,status,detail,metadata_json,tip"
        assert any("acme_weekly,HEALTHY," in line for line in lines[1:])
        assert any(",ADO Access," in line for line in lines[1:])


def test_doctor_refactor_status_human_output(monkeypatch) -> None:
        monkeypatch.setattr(
                "src.commands.doctor.build_refactor_status_report",
                lambda *, repo_root: RefactorStatusReport(
                        generated_on="2026-06-05",
                        metrics=(
                                RefactorStatusMetric("gather.py LOC", 10416, 10010, "fail"),
                                RefactorStatusMetric("doctor.py LOC", 8060, 8090, "ok"),
                                RefactorStatusMetric("shadow-write emission rate", "active", None, "info"),
                        ),
                ),
        )

        result = runner.invoke(app, ["doctor", "--refactor-status"])

        assert result.exit_code == 1
        assert "Debt Remediation Progress (2026-06-05)" in result.stdout
        assert "gather.py LOC" in result.stdout
        assert "10,416" in result.stdout
        assert "FAIL" in result.stdout
        assert "shadow-write emission rate" in result.stdout
        assert "INFO" in result.stdout


def test_doctor_refactor_status_json_and_csv(monkeypatch) -> None:
        monkeypatch.setattr(
                "src.commands.doctor.build_refactor_status_report",
                lambda *, repo_root: RefactorStatusReport(
                        generated_on="2026-06-05",
                        metrics=(
                                RefactorStatusMetric("private _load_yaml defs", 0, 0, "ok"),
                                RefactorStatusMetric("chat.completions outside router", 3, 0, "fail"),
                        ),
                ),
        )

        json_result = runner.invoke(app, ["doctor", "--refactor-status", "--format", "json"])

        assert json_result.exit_code == 1
        payload = json.loads(json_result.stdout)
        assert payload["generated_on"] == "2026-06-05"
        assert payload["failures"] == 1
        assert payload["metrics"][0]["name"] == "private _load_yaml defs"
        assert payload["metrics"][1]["status"] == "fail"

        csv_result = runner.invoke(app, ["doctor", "--refactor-status", "--format", "csv"])

        assert csv_result.exit_code == 1
        rows = list(csv.DictReader(csv_result.stdout.splitlines()))
        assert rows[0]["metric"] == "private _load_yaml defs"
        assert rows[0]["budget"] == "0"
        assert rows[1]["metric"] == "chat.completions outside router"
        assert rows[1]["status"] == "fail"


def test_doctor_check_auth_json_and_csv_include_capability_review_metadata(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        programs_root = reports_root.parent / "programs"
        knowledge_root = reports_root.parent / "knowledge"

        golden_queries_path = knowledge_root / "golden_queries.yaml"
        golden_queries_doc = yaml.safe_load(golden_queries_path.read_text(encoding="utf-8"))
        for query in golden_queries_doc["queries"]:
                if query.get("id") == "icm-mttr":
                        query["validated"] = False
                        break
        golden_queries_path.write_text(
                yaml.safe_dump(golden_queries_doc, sort_keys=False, allow_unicode=False),
                encoding="utf-8",
        )

        (programs_root / "acme" / "capability_status.yaml").write_text(
                "\n".join(
                        (
                                "schema_version: '1.0'",
                                "capabilities:",
                                "  - id: kusto_activation",
                                "    status: in_progress",
                                "    summary: Kusto activation is explicitly in progress for this auth doctor test.",
                                "    degradation: Live cluster validation is still pending.",
                                "    last_reviewed_on: 2026-05-17",
                                "  - id: m365_activation",
                                "    status: deferred",
                                "    summary: M365 activation is explicitly deferred for this auth doctor test.",
                                "    degradation: WorkIQ enrichment remains inactive.",
                                "    last_reviewed_on: 2026-05-15",
                        )
                )
                + "\n",
                encoding="utf-8",
        )

        monkeypatch.setenv("GRAPH_TENANT_ID", "tenant-id")
        monkeypatch.setenv("GRAPH_CLIENT_ID", "client-id")
        monkeypatch.setattr("src.commands.doctor.REPORTS_ROOT", reports_root)
        monkeypatch.setattr("src.commands.doctor.find_spec", lambda module_name: object() if module_name in {"azure.identity", "requests"} else None)
        monkeypatch.setattr(
                "src.commands.doctor._probe_ado_access",
                lambda bundle: ADOProbeResult(
                        reachable=True,
                        auth_method="azurecli",
                        item_count=3,
                        token_minutes_remaining=47,
                        detail="your-org/One reachable (auth: azurecli, 3 sampled items in scope)",
                ),
        )

        class _ReadyAgencyBridge:
                def probe(self) -> AgencyCapabilities:
                        return AgencyCapabilities(available=True, has_workiq=True, has_bluebird=True, has_ado=True, has_icm=True, tier="msft")

        monkeypatch.setattr("src.commands.doctor.AgencyBridge", _ReadyAgencyBridge)
        monkeypatch.setattr("src.commands.doctor.build_live_kusto_query_probe", lambda **kwargs: (lambda _queries: None))

        json_result = runner.invoke(app, ["doctor", "--edition", EDITION_NAME, "--check-auth", "--format", "json"])

        assert json_result.exit_code == 0
        payload = json.loads(json_result.stdout)
        workiq_check = next(check for check in payload["checks"] if check["label"] == "WorkIQ Access")
        assert workiq_check["status"] == "ok"
        kusto_access = next(check for check in payload["checks"] if check["label"] == "Kusto Access")
        assert "https://adventure.kusto.windows.net/xdataanalytics" in kusto_access["metadata"]["cluster_targets"]
        assert "https://icmcluster.kusto.windows.net/IcMDataWarehouse" in kusto_access["metadata"]["cluster_targets"]
        validation_check = next(check for check in payload["checks"] if check["label"] == "Kusto Validation")
        assert validation_check["status"] == "warn"
        assert "icm-mttr" in validation_check["metadata"]["excluded_query_ids"]
        assert "icm-mttr" not in validation_check["metadata"]["unvalidated_query_ids"]
        icm_check = next(check for check in payload["checks"] if check["label"] == "IcM via Kusto")
        assert icm_check["status"] == "warn"
        assert "icm-mttr" in icm_check["metadata"]["icm_query_ids"]
        capability_check = next(check for check in payload["checks"] if check["label"] == "Capability Reviews")
        assert capability_check["status"] == "warn"
        assert capability_check["metadata"]["warn_on_incomplete"] is True
        assert capability_check["metadata"]["latest_reviewed_on"] == "2026-05-17"
        assert capability_check["metadata"]["incomplete_capabilities"][0]["capability_id"] == "kusto_activation"

        csv_result = runner.invoke(app, ["doctor", "--edition", EDITION_NAME, "--check-auth", "--format", "csv"])

        assert csv_result.exit_code == 0
        rows = list(csv.DictReader(csv_result.stdout.splitlines()))
        workiq_row = next(row for row in rows if row["label"] == "WorkIQ Access")
        assert workiq_row["status"] == "ok"
        kusto_access_row = next(row for row in rows if row["label"] == "Kusto Access")
        kusto_access_metadata = json.loads(kusto_access_row["metadata_json"])
        assert "https://adventure.kusto.windows.net/xdataanalytics" in kusto_access_metadata["cluster_targets"]
        assert "https://icmcluster.kusto.windows.net/IcMDataWarehouse" in kusto_access_metadata["cluster_targets"]
        validation_row = next(row for row in rows if row["label"] == "Kusto Validation")
        validation_metadata = json.loads(validation_row["metadata_json"])
        assert "icm-mttr" in validation_metadata["excluded_query_ids"]
        icm_row = next(row for row in rows if row["label"] == "IcM via Kusto")
        icm_metadata = json.loads(icm_row["metadata_json"])
        assert "icm-mttr" in icm_metadata["icm_query_ids"]
        capability_row = next(row for row in rows if row["label"] == "Capability Reviews")
        metadata = json.loads(capability_row["metadata_json"])
        assert metadata["warn_on_incomplete"] is True
        assert metadata["latest_reviewed_on"] == "2026-05-17"
        assert metadata["incomplete_capabilities"][1]["capability_id"] == "m365_activation"


def test_run_doctor_watch_sources_ok_when_selected_sources_are_ready(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        program_path = programs_root / "demo" / "program.yaml"
        program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        program_doc["maturity_level"] = 2
        program_doc["m365"] = {
                "enabled": True,
                "workiq_queries": {"weekly_mail": "What changed this week?"},
        }
        program_doc["kusto"] = {"enabled": True}
        program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False), encoding="utf-8")

        class _ReadyAgencyBridge:
                def probe(self) -> AgencyCapabilities:
                        return AgencyCapabilities(available=True, has_workiq=True, tier="msft")

        monkeypatch.setattr("src.commands.watch.AgencyBridge", _ReadyAgencyBridge)

        report = run_doctor(
                edition_name="demo_weekly",
                watch_sources=True,
                watch_source_values=("workiq", "kusto"),
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Watch Sources"].status == "ok"
        assert "workiq, kusto" in checks["Watch Sources"].detail


def test_run_doctor_watch_sources_fail_when_selected_source_is_not_ready(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        program_path = programs_root / "demo" / "program.yaml"
        program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        program_doc["maturity_level"] = 2
        program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False), encoding="utf-8")

        report = run_doctor(
                edition_name="demo_weekly",
                watch_sources=True,
                watch_source_values=("workiq",),
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Watch Sources"].status == "fail"
        assert "source 'workiq' requires enabled m365.workiq_queries" in checks["Watch Sources"].detail


def test_doctor_cli_watch_sources(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        program_path = programs_root / "demo" / "program.yaml"
        program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        program_doc["maturity_level"] = 2
        program_doc["m365"] = {
                "enabled": True,
                "workiq_queries": {"weekly_mail": "What changed this week?"},
        }
        program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False), encoding="utf-8")

        class _ReadyAgencyBridge:
                def probe(self) -> AgencyCapabilities:
                        return AgencyCapabilities(available=True, has_workiq=True, tier="msft")

        monkeypatch.setattr("src.commands.doctor.EDITIONS_ROOT", editions_root)
        monkeypatch.setattr("src.commands.doctor.PROGRAMS_ROOT", programs_root)
        monkeypatch.setattr("src.commands.watch.AgencyBridge", _ReadyAgencyBridge)

        result = runner.invoke(app, ["doctor", "--edition", "demo_weekly", "--watch-sources", "--source", "workiq"])

        assert result.exit_code == 0
        assert "Watch Sources" in result.stdout
        assert "Overall: HEALTHY" in result.stdout


def test_run_doctor_catchup_log_ok_when_no_events(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())

        report = run_doctor(
                edition_name="demo_weekly",
                catchup_log=True,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Catchup Log"].status == "ok"
        assert "No catchup failures or truncation events recorded" in checks["Catchup Log"].detail
        assert checks["Catchup Log"].metadata == {"event_count": 0, "program_id": "demo"}


def test_run_doctor_catchup_log_warns_on_recent_events(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        usage_log_path = programs_root / "demo" / "_feedback" / "usage_log.jsonl"
        usage_log_path.parent.mkdir(parents=True, exist_ok=True)
        usage_log_path.write_text(
                "\n".join(
                        (
                                json.dumps({
                                        "event": "catchup_failed",
                                        "reason": "simulated timeout",
                                        "recorded_at": "2026-05-19T18:00:00+00:00",
                                }),
                                json.dumps({
                                        "event": "catchup_truncated",
                                        "reason": "processed first 500 changes",
                                        "recorded_at": "2026-05-19T18:05:00+00:00",
                                }),
                        )
                )
                + "\n",
                encoding="utf-8",
        )

        report = run_doctor(
                edition_name="demo_weekly",
                catchup_log=True,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Catchup Log"].status == "warn"
        assert "catchup_failed" in checks["Catchup Log"].detail
        assert "catchup_truncated" in checks["Catchup Log"].detail
        assert checks["Catchup Log"].metadata is not None
        assert checks["Catchup Log"].metadata["event_count"] == 2
        assert checks["Catchup Log"].metadata["recent_events"][0]["reason"] == "simulated timeout"


def test_doctor_cli_catchup_log(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        usage_log_path = programs_root / "demo" / "_feedback" / "usage_log.jsonl"
        usage_log_path.parent.mkdir(parents=True, exist_ok=True)
        usage_log_path.write_text(
                json.dumps(
                        {
                                "event": "catchup_failed",
                                "reason": "simulated timeout",
                                "recorded_at": "2026-05-19T18:00:00+00:00",
                        }
                )
                + "\n",
                encoding="utf-8",
        )

        monkeypatch.setattr("src.commands.doctor.EDITIONS_ROOT", editions_root)
        monkeypatch.setattr("src.commands.doctor.PROGRAMS_ROOT", programs_root)

        result = runner.invoke(app, ["doctor", "--edition", "demo_weekly", "--catchup-log"])

        assert result.exit_code == 0
        assert "Catchup Log" in result.stdout
        assert "catchup_failed" in result.stdout


def test_run_doctor_readiness_warns_when_snapshot_is_missing(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _enable_demo_readiness_gate(programs_root, snapshot_max_age_days=7)
        _write_demo_readiness_config(programs_root, snapshot_max_age_days=7)

        report = run_doctor(
                edition_name="demo_weekly",
                readiness=True,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Readiness Config"].status == "ok"
        assert checks["Readiness Snapshot"].status == "warn"
        assert "vertex readiness fetch --program demo" in checks["Readiness Snapshot"].detail


def test_run_doctor_readiness_warns_when_snapshot_is_stale(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _enable_demo_readiness_gate(programs_root, snapshot_max_age_days=7)
        _write_demo_readiness_config(programs_root, snapshot_max_age_days=7)
        config = load_readiness_config("demo", programs_root=programs_root)
        snapshot = build_readiness_snapshot(
                "demo",
                config,
                loaders=ReadinessFetchLoaders(),
                fetched_at=datetime.now(timezone.utc) - timedelta(days=10),
        )
        write_readiness_snapshot("demo", snapshot, programs_root=programs_root)

        report = run_doctor(
                edition_name="demo_weekly",
                readiness=True,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Readiness Config"].status == "ok"
        assert checks["Readiness Snapshot"].status == "warn"
        assert "10 day(s) old" in checks["Readiness Snapshot"].detail
        assert checks["Readiness Snapshot"].metadata is not None
        assert checks["Readiness Snapshot"].metadata["age_days"] == 10


def test_doctor_cli_readiness(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _enable_demo_readiness_gate(programs_root, snapshot_max_age_days=7)
        _write_demo_readiness_config(programs_root, snapshot_max_age_days=7)
        config = load_readiness_config("demo", programs_root=programs_root)
        snapshot = build_readiness_snapshot(
                "demo",
                config,
                loaders=ReadinessFetchLoaders(),
                fetched_at=datetime.now(timezone.utc),
        )
        write_readiness_snapshot("demo", snapshot, programs_root=programs_root)

        monkeypatch.setattr("src.commands.doctor.EDITIONS_ROOT", editions_root)
        monkeypatch.setattr("src.commands.doctor.PROGRAMS_ROOT", programs_root)

        result = runner.invoke(app, ["doctor", "--edition", "demo_weekly", "--readiness"])

        assert result.exit_code == 0
        assert "Readiness Config" in result.stdout
        assert "Readiness Snapshot" in result.stdout
        assert "Overall: HEALTHY" in result.stdout


def test_run_doctor_kb_validates_program_and_edition_roots(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=("11111111-1111-1111-1111-111111111111",))

        class FakeADOClient:
                def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                        self.organization = organization
                        self.project = project

                def get_saved_query(self, query_id: str) -> dict[str, str]:
                        return {"id": query_id}

        monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

        report = run_doctor(kb=True, editions_root=editions_root, programs_root=programs_root)

        assert report.failures == 0
        checks = {check.label: check for check in report.checks}
        assert checks["Knowledge"].status == "ok"
        assert checks["Knowledge Vault"].status == "ok"
        assert checks["Editions"].status == "ok"
        assert checks["Saved Queries"].status == "ok"


def test_run_doctor_kb_fails_on_shared_vault_hash_mismatch(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=("11111111-1111-1111-1111-111111111111",))

        class FakeADOClient:
                def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                        self.organization = organization
                        self.project = project

                def get_saved_query(self, query_id: str) -> dict[str, str]:
                        return {"id": query_id}

        monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

        shared_knowledge_root = programs_root.parent / "knowledge"
        vault_dir = shared_knowledge_root / "vault" / "ab"
        vault_dir.mkdir(parents=True, exist_ok=True)
        content_path = vault_dir / "abc123"
        content_path.write_text("payload", encoding="utf-8")
        (vault_dir / "abc123.meta.json").write_text(
                json.dumps(
                        {
                                "vault_hash": "sha256:deadbeef",
                                "content_type": "text/plain",
                                "original_filename": "demo.txt",
                                "origin_path": "demo.txt",
                                "ingested_at": "2026-06-11T00:00:00+00:00",
                                "size_bytes": 7,
                        },
                        indent=2,
                ),
                encoding="utf-8",
        )

        report = run_doctor(kb=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["Knowledge"].status == "ok"
        assert checks["Knowledge Vault"].status == "fail"
        assert "content hash mismatch for 1 vault file" in checks["Knowledge Vault"].detail
        assert checks["Knowledge Vault"].metadata == {
                "file_count": 1,
                "missing_meta_count": 0,
                "hash_mismatch_count": 1,
                "missing_source_record_count": 0,
                "missing_claim_ref_count": 0,
                "missing_candidate_ref_count": 0,
                "last_deep_verify_at": None,
                "last_deep_verify_ok": None,
                "last_deep_verify_age_seconds": None,
        }


def test_run_doctor_kb_fails_on_missing_vault_reference_from_claim(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=("11111111-1111-1111-1111-111111111111",))

        class FakeADOClient:
                def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                        self.organization = organization
                        self.project = project

                def get_saved_query(self, query_id: str) -> dict[str, str]:
                        return {"id": query_id}

        monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

        append_claim_revision(
                scope="domain:storage-platform",
                subject="sku_generation:gen9",
                predicate="first_deployment",
                value="2025-H2",
                valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
                valid_until=None,
                confidence=ConfidenceTier.OPERATOR_CONFIRMED,
                source_ref=KnowledgeDocumentRef(
                        vault_hash="sha256:missing-vault-hash",
                        original_filename="demo.md",
                        origin_kind="knowledge_markdown",
                        origin_path="demo.md",
                        ingested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                        section="document",
                ),
                knowledge_root=programs_root.parent / "knowledge",
                recorded_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        report = run_doctor(kb=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["Knowledge"].status == "ok"
        assert checks["Knowledge Vault"].status == "fail"
        assert "missing vault entries referenced by 1 claim" in checks["Knowledge Vault"].detail
        assert checks["Knowledge Vault"].metadata == {
                "file_count": 0,
                "missing_meta_count": 0,
                "hash_mismatch_count": 0,
                "missing_source_record_count": 0,
                "missing_claim_ref_count": 1,
                "missing_candidate_ref_count": 0,
                "last_deep_verify_at": None,
                "last_deep_verify_ok": None,
                "last_deep_verify_age_seconds": None,
        }


def test_run_doctor_kb_fails_on_missing_vault_reference_from_source_registry(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=("11111111-1111-1111-1111-111111111111",))

        class FakeADOClient:
                def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                        self.organization = organization
                        self.project = project

                def get_saved_query(self, query_id: str) -> dict[str, str]:
                        return {"id": query_id}

        monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

        source_path = tmp_path / "gen9.md"
        source_path.write_text("# Gen9\n", encoding="utf-8")
        entry = ingest_knowledge_source(source_path, scope="domain:storage-platform", programs_root=programs_root)
        entry.content_path.unlink()
        entry.metadata_path.unlink()

        report = run_doctor(kb=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["Knowledge"].status == "ok"
        assert checks["Knowledge Vault"].status == "fail"
        assert "missing vault entries referenced by 1 source registry record" in checks["Knowledge Vault"].detail
        assert checks["Knowledge Vault"].metadata == {
                "file_count": 0,
                "missing_meta_count": 0,
                "hash_mismatch_count": 0,
                "missing_source_record_count": 1,
                "missing_claim_ref_count": 0,
                "missing_candidate_ref_count": 0,
                "last_deep_verify_at": None,
                "last_deep_verify_ok": None,
                "last_deep_verify_age_seconds": None,
        }


def test_run_doctor_kb_surfaces_last_shared_vault_deep_verify_metadata(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=("11111111-1111-1111-1111-111111111111",))

        class FakeADOClient:
                def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                        self.organization = organization
                        self.project = project

                def get_saved_query(self, query_id: str) -> dict[str, str]:
                        return {"id": query_id}

        monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

        verified_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        write_shared_vault_verify_status(
                verified_at=verified_at,
                ok=True,
                issue_records=[],
                programs_root=programs_root,
                program_id="demo",
        )

        report = run_doctor(kb=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        metadata = checks["Knowledge Vault"].metadata
        assert metadata is not None
        assert metadata["last_deep_verify_at"] is not None
        assert metadata["last_deep_verify_ok"] is True
        assert metadata["last_deep_verify_age_seconds"] is not None
        assert metadata["last_deep_verify_age_seconds"] >= 240


def test_run_doctor_kb_warns_on_active_program_scope_knowledge_overrides(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=("11111111-1111-1111-1111-111111111111",))
        program_path = programs_root / "demo" / "program.yaml"
        program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        program_doc["knowledge_scopes"] = ["domain:storage-platform"]
        program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False), encoding="utf-8")

        class FakeADOClient:
                def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                        self.organization = organization
                        self.project = project

                def get_saved_query(self, query_id: str) -> dict[str, str]:
                        return {"id": query_id}

        monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

        knowledge_root = programs_root.parent / "knowledge"
        append_claim_revision(
                scope="domain:storage-platform",
                subject="sku_generation:gen9",
                predicate="first_deployment",
                value="2025-H1",
                valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
                valid_until=None,
                confidence=ConfidenceTier.OPERATOR_CONFIRMED,
                source_ref=OperatorAssertionRef(asserted_by="demo", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
                knowledge_root=knowledge_root,
                recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        append_claim_revision(
                scope="program:demo",
                subject="sku_generation:gen9",
                predicate="first_deployment",
                value="2025-H2",
                valid_from=datetime(2026, 1, 2, tzinfo=timezone.utc),
                valid_until=None,
                confidence=ConfidenceTier.OPERATOR_CONFIRMED,
                source_ref=OperatorAssertionRef(asserted_by="demo", asserted_at=datetime(2026, 1, 2, tzinfo=timezone.utc)),
                knowledge_root=knowledge_root,
                recorded_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        report = run_doctor(kb=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        override_check = checks["Knowledge Overrides"]
        assert override_check.status == "warn"
        assert "demo overrides sku_generation:gen9/first_deployment" in override_check.detail
        assert override_check.metadata is not None
        assert override_check.metadata["override_program_count"] == 1
        assert override_check.metadata["override_count"] == 1
        assert override_check.metadata["programs"][0]["program_id"] == "demo"
        assert override_check.metadata["programs"][0]["override_count"] == 1
        assert override_check.metadata["programs"][0]["overrides"][0]["entity_id"] == "sku_generation:gen9"
        assert override_check.metadata["programs"][0]["overrides"][0]["predicate"] == "first_deployment"
        assert override_check.metadata["programs"][0]["overrides"][0]["overridden_claim_ids"]


def test_run_doctor_kb_warns_on_stale_pending_knowledge_candidates(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=("11111111-1111-1111-1111-111111111111",))

        class FakeADOClient:
                def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                        self.organization = organization
                        self.project = project

                def get_saved_query(self, query_id: str) -> dict[str, str]:
                        return {"id": query_id}

        monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

        now = datetime.now(timezone.utc)
        append_knowledge_candidate(
                build_knowledge_candidate(
                        candidate_id="cand-stale",
                        scope="domain:storage-platform",
                        subject="sku_generation:gen9",
                        predicate="first_deployment",
                        value="2025-H2",
                        valid_from=now - timedelta(days=30),
                        valid_until=None,
                        proposed_confidence=ConfidenceTier.AI_EXTRACTED,
                        source_ref=OperatorAssertionRef(asserted_by="demo", asserted_at=now - timedelta(days=15)),
                        pipeline="extract",
                        extraction_confidence=0.8,
                        entity_resolution=(),
                        corroborating_refs=(),
                        batch_id="batch-stale",
                        created_at=now - timedelta(days=15),
                ),
                programs_root=programs_root,
        )
        append_knowledge_candidate(
                build_knowledge_candidate(
                        candidate_id="cand-triaged",
                        scope="domain:storage-platform",
                        subject="sku_generation:gen10",
                        predicate="first_deployment",
                        value="2026-H1",
                        valid_from=now - timedelta(days=10),
                        valid_until=None,
                        proposed_confidence=ConfidenceTier.AI_EXTRACTED,
                        source_ref=OperatorAssertionRef(asserted_by="demo", asserted_at=now - timedelta(days=1)),
                        pipeline="extract",
                        extraction_confidence=0.9,
                        entity_resolution=(),
                        corroborating_refs=(),
                        batch_id="batch-triaged",
                        created_at=now - timedelta(days=1),
                ),
                programs_root=programs_root,
        )
        append_knowledge_candidate_decision(
                KnowledgeCandidateDecisionRecord(
                        candidate_id="cand-triaged",
                        kind="approved",
                        decided_at=now - timedelta(minutes=1),
                        triage_actor="demo",
                        batch_id="batch-triaged",
                ),
                programs_root=programs_root,
        )

        report = run_doctor(kb=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        queue_check = checks["Knowledge Candidates"]
        assert queue_check.status == "warn"
        assert "oldest active pending knowledge candidate is 15 day(s) old" in queue_check.detail
        assert "pipelines extract=1" in queue_check.detail
        assert queue_check.metadata is not None
        assert queue_check.metadata["pending_candidate_count"] == 1
        assert queue_check.metadata["pending_candidates_by_pipeline"] == {"extract": 1}
        assert queue_check.metadata["triaged_candidate_count"] == 1
        assert queue_check.metadata["latest_triage_session_actor"] == "demo"
        assert queue_check.metadata["latest_triage_session_decision_count"] == 1
        assert queue_check.metadata["latest_triage_session_throughput_per_minute"] == 1.0
        assert queue_check.metadata["batch_count"] == 2
        assert queue_check.metadata["staged_batch_count"] == 1
        assert queue_check.metadata["approved_batch_count"] == 1
        assert queue_check.metadata["quarantined_batch_count"] == 0
        assert queue_check.metadata["age_threshold_days"] == 14


def test_run_doctor_kb_warns_on_stale_operator_assertion_without_ttl(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=("11111111-1111-1111-1111-111111111111",))

        class FakeADOClient:
                def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                        self.organization = organization
                        self.project = project

                def get_saved_query(self, query_id: str) -> dict[str, str]:
                        return {"id": query_id}

        monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

        knowledge_root = programs_root.parent / "knowledge"
        now = datetime.now(timezone.utc)
        append_claim_revision(
                scope="program:demo",
                subject="sku_generation:gen9",
                predicate="first_deployment",
                value="2025-H2",
                valid_from=now - timedelta(days=200),
                valid_until=None,
                confidence=ConfidenceTier.OPERATOR_CONFIRMED,
                source_ref=OperatorAssertionRef(asserted_by="demo", asserted_at=now - timedelta(days=200)),
                knowledge_root=knowledge_root,
                recorded_at=now - timedelta(days=200),
        )
        append_claim_revision(
                scope="program:demo",
                subject="sku_generation:gen10",
                predicate="first_deployment",
                value="2026-H1",
                valid_from=now - timedelta(days=30),
                valid_until=None,
                confidence=ConfidenceTier.OPERATOR_CONFIRMED,
                source_ref=OperatorAssertionRef(asserted_by="demo", asserted_at=now - timedelta(days=30)),
                knowledge_root=knowledge_root,
                recorded_at=now - timedelta(days=30),
        )

        report = run_doctor(kb=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assertion_check = checks["Knowledge Operator Assertions"]
        assert assertion_check.status == "warn"
        assert "exceed 180 days without TTL" in assertion_check.detail
        assert "sku_generation:gen9/first_deployment" in assertion_check.detail
        assert assertion_check.metadata is not None
        assert assertion_check.metadata["stale_without_ttl_count"] == 1
        assert assertion_check.metadata["age_threshold_days"] == 180
        assert assertion_check.metadata["stale_without_ttl"][0]["program_id"] == "demo"


def test_run_doctor_kb_warns_on_superseded_grounded_claim_reference(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=("11111111-1111-1111-1111-111111111111",))

        class FakeADOClient:
                def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                        self.organization = organization
                        self.project = project

                def get_saved_query(self, query_id: str) -> dict[str, str]:
                        return {"id": query_id}

        monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

        knowledge_root = programs_root.parent / "knowledge"
        first = append_claim_revision(
                scope="domain:storage-platform",
                subject="sku_generation:gen9",
                predicate="first_deployment",
                value="2025-H1",
                valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
                valid_until=None,
                confidence=ConfidenceTier.OPERATOR_CONFIRMED,
                source_ref=OperatorAssertionRef(asserted_by="demo", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
                knowledge_root=knowledge_root,
                recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        append_claim_revision(
                scope="domain:storage-platform",
                subject="sku_generation:gen9",
                predicate="first_deployment",
                value="2025-H2",
                valid_from=datetime(2026, 1, 2, tzinfo=timezone.utc),
                valid_until=None,
                confidence=ConfidenceTier.OPERATOR_CONFIRMED,
                source_ref=OperatorAssertionRef(asserted_by="demo", asserted_at=datetime(2026, 1, 2, tzinfo=timezone.utc)),
                knowledge_root=knowledge_root,
                recorded_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        event = build_event_envelope(
                program_id="demo",
                event_type="risk.raised.v1",
                occurred_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
                recorded_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
                temporal_confidence=TemporalConfidence.EXACT,
                confidence=ConfidenceTier.OPERATOR_CONFIRMED,
                actor="demo",
                payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high", "grounded_in": [first.claim_id]},
                source_ref=OperatorAssertionRef(asserted_by="demo", asserted_at=datetime(2026, 1, 3, tzinfo=timezone.utc)),
        )
        write_event(event, programs_root=programs_root)

        report = run_doctor(kb=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        grounding_check = checks["Knowledge Grounding"]
        assert grounding_check.status == "warn"
        assert "1 event(s) still reference superseded grounded claim revision(s)" in grounding_check.detail
        assert grounding_check.metadata is not None
        assert grounding_check.metadata["superseded_event_count"] == 1
        assert grounding_check.metadata["superseded_groundings"][0]["program_id"] == "demo"
        assert grounding_check.metadata["superseded_groundings"][0]["event_type"] == "risk.raised.v1"
        assert grounding_check.metadata["superseded_groundings"][0]["claim_ids"] == [first.claim_id]


def test_run_doctor_kb_warns_on_stale_knowledge_origin(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=("11111111-1111-1111-1111-111111111111",))

        class FakeADOClient:
                def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                        self.organization = organization
                        self.project = project

                def get_saved_query(self, query_id: str) -> dict[str, str]:
                        return {"id": query_id}

        monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

        source_path = tmp_path / "artha" / "demo.md"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text("Claim: sku_generation:gen9 | first_deployment | 2025-H1\n", encoding="utf-8")
        ingest_knowledge_source(source_path, scope="program:demo", programs_root=programs_root)
        source_path.write_text("Claim: sku_generation:gen9 | first_deployment | 2025-H2\n", encoding="utf-8")

        report = run_doctor(kb=True, kb_check_origins=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        origin_check = checks["Knowledge Origins"]
        assert origin_check.status == "warn"


        def test_run_doctor_kb_warns_on_uncovered_knowledge_entities(monkeypatch, tmp_path: Path) -> None:
                editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=("11111111-1111-1111-1111-111111111111",))

                class FakeADOClient:
                        def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                                self.organization = organization
                                self.project = project

                        def get_saved_query(self, query_id: str) -> dict[str, str]:
                                return {"id": query_id}

                monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

                knowledge_root = programs_root.parent / "knowledge"
                append_claim_revision(
                        scope="domain:storage-platform",
                        subject="sku_generation:gen9",
                        predicate="first_deployment",
                        value="2025-H2",
                        valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
                        valid_until=None,
                        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
                        source_ref=OperatorAssertionRef(asserted_by="demo", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
                        knowledge_root=knowledge_root,
                        recorded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                )
                append_claim_revision(
                        scope="domain:storage-platform",
                        subject="sku_generation:gen10",
                        predicate="first_deployment",
                        value="2026-H1",
                        valid_from=datetime(2026, 1, 2, tzinfo=timezone.utc),
                        valid_until=None,
                        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
                        source_ref=OperatorAssertionRef(asserted_by="demo", asserted_at=datetime(2026, 1, 2, tzinfo=timezone.utc)),
                        knowledge_root=knowledge_root,
                        recorded_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                )

                write_event(
                        build_event_envelope(
                                program_id="demo",
                                event_type="sku_generation.added.v1",
                                occurred_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
                                recorded_at=datetime(2026, 1, 3, tzinfo=timezone.utc),
                                temporal_confidence=TemporalConfidence.EXACT,
                                confidence=ConfidenceTier.OPERATOR_CONFIRMED,
                                actor="demo",
                                payload={"sku_generation_id": "sku_generation:gen9", "name": "Gen9"},
                                source_ref=OperatorAssertionRef(asserted_by="demo", asserted_at=datetime(2026, 1, 3, tzinfo=timezone.utc)),
                        ),
                        programs_root=programs_root,
                )

                report = run_doctor(kb=True, editions_root=editions_root, programs_root=programs_root)

                checks = {check.label: check for check in report.checks}
                coverage_check = checks["Knowledge Coverage"]
                assert coverage_check.status == "warn"
                assert "resolve outside current ledger history" in coverage_check.detail
                assert coverage_check.metadata is not None
                assert coverage_check.metadata["entity_count"] == 2
                assert coverage_check.metadata["stub_count"] == 0
                assert coverage_check.metadata["absent_count"] == 1
                assert coverage_check.metadata["programs"][0]["program_id"] == "demo"
                assert coverage_check.metadata["programs"][0]["present_count"] == 1
                assert coverage_check.metadata["programs"][0]["absent_count"] == 1
                assert coverage_check.metadata["programs"][0]["uncovered_entities"] == ["sku_generation:gen10"]


        def test_run_doctor_kb_warns_on_expired_and_expiring_claims(monkeypatch, tmp_path: Path) -> None:
                editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=("11111111-1111-1111-1111-111111111111",))

                class FakeADOClient:
                        def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                                self.organization = organization
                                self.project = project

                        def get_saved_query(self, query_id: str) -> dict[str, str]:
                                return {"id": query_id}

                monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

                knowledge_root = programs_root.parent / "knowledge"
                now = datetime.now(timezone.utc)
                append_claim_revision(
                        scope="domain:storage-platform",
                        subject="sku_generation:gen9",
                        predicate="first_deployment",
                        value="2025-H2",
                        valid_from=now - timedelta(days=90),
                        valid_until=now - timedelta(days=1),
                        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
                        source_ref=OperatorAssertionRef(asserted_by="demo", asserted_at=now - timedelta(days=90)),
                        knowledge_root=knowledge_root,
                        recorded_at=now - timedelta(days=90),
                )
                append_claim_revision(
                        scope="domain:storage-platform",
                        subject="sku_generation:gen10",
                        predicate="first_deployment",
                        value="2026-H1",
                        valid_from=now - timedelta(days=10),
                        valid_until=now + timedelta(days=7),
                        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
                        source_ref=OperatorAssertionRef(asserted_by="demo", asserted_at=now - timedelta(days=10)),
                        knowledge_root=knowledge_root,
                        recorded_at=now - timedelta(days=10),
                )

                report = run_doctor(kb=True, editions_root=editions_root, programs_root=programs_root)

                checks = {check.label: check for check in report.checks}
                freshness_check = checks["Knowledge Freshness"]
                assert freshness_check.status == "warn"
                assert "already expired" in freshness_check.detail
                assert "expire within 30 days" in freshness_check.detail
                assert freshness_check.metadata is not None
                assert freshness_check.metadata["expired_count"] == 1
                assert freshness_check.metadata["expiring_soon_count"] == 1
                assert freshness_check.metadata["warning_window_days"] == 30
        assert "1 origin file(s) changed since ingest" in origin_check.detail
        assert origin_check.metadata is not None
        assert origin_check.metadata["checked_source_count"] == 1
        assert origin_check.metadata["verified_count"] == 0
        assert origin_check.metadata["changed_count"] == 1
        assert origin_check.metadata["missing_count"] == 0
        assert origin_check.metadata["changed_sources"][0]["origin_path"] == str(source_path)


def test_run_doctor_kb_fails_on_missing_program_evidence_vault_entry(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=("11111111-1111-1111-1111-111111111111",))

        class FakeADOClient:
                def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                        self.organization = organization
                        self.project = project

                def get_saved_query(self, query_id: str) -> dict[str, str]:
                        return {"id": query_id}

        monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

        event = build_event_envelope(
                program_id="demo",
                event_type="risk.raised.v1",
                occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                recorded_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                temporal_confidence=TemporalConfidence.EXACT,
                confidence=ConfidenceTier.AI_EXTRACTED,
                actor="workiq_discovery",
                payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
                source_ref=WorkIQRef(
                        artifact_id="mail-123",
                        artifact_kind="email_excerpt",
                        retrieved_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                        query="What changed?",
                        vault_hash="sha256:deadbeef",
                ),
        )
        write_event(event, programs_root=programs_root)

        report = run_doctor(kb=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        evidence_check = checks["Program Evidence Vault"]
        assert evidence_check.status == "fail"
        assert "1 indexed event vault reference(s) have no matching evidence file" in evidence_check.detail
        assert evidence_check.metadata is not None
        assert evidence_check.metadata["indexed_event_vault_ref_count"] == 1
        assert evidence_check.metadata["missing_event_vault_ref_count"] == 1
        assert evidence_check.metadata["hash_mismatch_event_vault_ref_count"] == 0
        assert evidence_check.metadata["missing_refs"][0]["program_id"] == "demo"
        assert evidence_check.metadata["missing_refs"][0]["vault_hash"] == "sha256:deadbeef"


def test_run_doctor_kb_fails_on_program_evidence_vault_hash_mismatch(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=("11111111-1111-1111-1111-111111111111",))

        class FakeADOClient:
                def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                        self.organization = organization
                        self.project = project

                def get_saved_query(self, query_id: str) -> dict[str, str]:
                        return {"id": query_id}

        monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

        event = build_event_envelope(
                program_id="demo",
                event_type="risk.raised.v1",
                occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                recorded_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                temporal_confidence=TemporalConfidence.EXACT,
                confidence=ConfidenceTier.AI_EXTRACTED,
                actor="workiq_discovery",
                payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
                source_ref=WorkIQRef(
                        artifact_id="mail-456",
                        artifact_kind="email_excerpt",
                        retrieved_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                        query="What changed?",
                        vault_hash="sha256:deadbeef",
                ),
        )
        write_event(event, programs_root=programs_root)

        evidence_dir = programs_root / "demo" / "ledger" / "evidence" / "de"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "deadbeef").write_text("tampered payload", encoding="utf-8")
        (evidence_dir / "deadbeef.meta.json").write_text(
                json.dumps({"vault_hash": "sha256:deadbeef", "content_type": "text/plain"}),
                encoding="utf-8",
        )

        report = run_doctor(kb=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        evidence_check = checks["Program Evidence Vault"]
        assert evidence_check.status == "fail"
        assert "1 indexed event vault reference(s) failed content hash recheck" in evidence_check.detail
        assert evidence_check.metadata is not None
        assert evidence_check.metadata["indexed_event_vault_ref_count"] == 1
        assert evidence_check.metadata["missing_event_vault_ref_count"] == 0
        assert evidence_check.metadata["hash_mismatch_event_vault_ref_count"] == 1
        assert evidence_check.metadata["hash_mismatch_refs"][0]["program_id"] == "demo"
        assert evidence_check.metadata["hash_mismatch_refs"][0]["vault_hash"] == "sha256:deadbeef"


def test_run_doctor_kb_warns_on_orphaned_program_evidence_vault_entry(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=("11111111-1111-1111-1111-111111111111",))

        class FakeADOClient:
                def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                        self.organization = organization
                        self.project = project

                def get_saved_query(self, query_id: str) -> dict[str, str]:
                        return {"id": query_id}

        monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

        evidence_dir = programs_root / "demo" / "ledger" / "evidence" / "de"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "deadbeef").write_text("orphan payload", encoding="utf-8")
        (evidence_dir / "deadbeef.meta.json").write_text(
                json.dumps({"vault_hash": "sha256:deadbeef", "content_type": "text/plain"}),
                encoding="utf-8",
        )

        report = run_doctor(kb=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        evidence_check = checks["Program Evidence Vault"]
        assert evidence_check.status == "warn"
        assert "1 orphaned evidence entrie(s)" in evidence_check.detail
        assert evidence_check.metadata is not None
        assert evidence_check.metadata["indexed_event_vault_ref_count"] == 0
        assert evidence_check.metadata["orphaned_entry_count"] == 1
        assert evidence_check.metadata["orphaned_entries"][0]["program_id"] == "demo"
        assert evidence_check.metadata["orphaned_entries"][0]["vault_hash"] == "sha256:deadbeef"


def test_run_doctor_rejects_kb_origin_check_without_kb(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())

        with pytest.raises(typer.BadParameter, match="--kb-check-origins requires --kb"):
                run_doctor(kb_check_origins=True, editions_root=editions_root, programs_root=programs_root)


def test_run_doctor_kb_warns_on_broken_saved_query_ids(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=("broken-guid",))

        class FakeADOClient:
                def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                        self.organization = organization
                        self.project = project

                def get_saved_query(self, query_id: str) -> dict[str, str]:
                        raise QueryError("query not found")

        monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

        report = run_doctor(kb=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["Saved Queries"].status == "warn"
        assert "broken-guid" in checks["Saved Queries"].detail


def test_run_doctor_kb_warns_on_people_directory_drift(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=("11111111-1111-1111-1111-111111111111",))

        class FakeADOClient:
                def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                        self.organization = organization
                        self.project = project

                def get_saved_query(self, query_id: str) -> dict[str, str]:
                        return {"id": query_id}

        monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

        knowledge_dir = programs_root / "demo" / "knowledge"
        (knowledge_dir / "people_directory.yaml").write_text(
                (
                        'schema_version: "1.0"\n'
                        'people:\n'
                        '  - alias: demo\n'
                        '    email: demo@example.com\n'
                        '    display_name: Demo Author\n'
                        '  - alias: stale\n'
                        '    email: stale@example.com\n'
                        '    display_name: Stale Person\n'
                ),
                encoding="utf-8",
        )

        recent_date = datetime.now(timezone.utc).date() - timedelta(days=1)
        append_trajectory_point(
                "demo",
                1001,
                TrajectoryPoint(
                        date=recent_date,
                        state="Active",
                        assigned_to="demo@example.com",
                        target_date=None,
                        risk_level=RiskLevel.MEDIUM,
                        area_path="One\\Demo",
                ),
                programs_root=programs_root,
        )
        for work_item_id in range(1002, 1007):
                append_trajectory_point(
                        "demo",
                        work_item_id,
                        TrajectoryPoint(
                                date=recent_date,
                                state="Active",
                                assigned_to="unknown@example.com",
                                target_date=None,
                                risk_level=RiskLevel.HIGH,
                                area_path="One\\Demo",
                        ),
                        programs_root=programs_root,
                )

        report = run_doctor(kb=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["KB Drift"].status == "warn"
        assert "knowledge not seen in ADO for 90+ days" in checks["KB Drift"].detail
        assert "recent repeat ADO assignee (>= 5 active items) missing from people_directory.yaml" in checks["KB Drift"].detail
        assert "demo/stale" in checks["KB Drift"].detail
        assert "demo/unknown" in checks["KB Drift"].detail


def test_run_doctor_kb_ignores_one_off_recent_ado_assignees(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=("11111111-1111-1111-1111-111111111111",))

        class FakeADOClient:
                def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                        self.organization = organization
                        self.project = project

                def get_saved_query(self, query_id: str) -> dict[str, str]:
                        return {"id": query_id}

        monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

        knowledge_dir = programs_root / "demo" / "knowledge"
        (knowledge_dir / "people_directory.yaml").write_text(
                (
                        'schema_version: "1.0"\n'
                        'people:\n'
                        '  - alias: demo\n'
                        '    email: demo@example.com\n'
                        '    display_name: Demo Author\n'
                ),
                encoding="utf-8",
        )

        recent_date = datetime.now(timezone.utc).date() - timedelta(days=1)
        append_trajectory_point(
                "demo",
                1001,
                TrajectoryPoint(
                        date=recent_date,
                        state="Active",
                        assigned_to="demo@example.com",
                        target_date=None,
                        risk_level=RiskLevel.MEDIUM,
                        area_path="One\\Demo",
                ),
                programs_root=programs_root,
        )
        append_trajectory_point(
                "demo",
                1002,
                TrajectoryPoint(
                        date=recent_date,
                        state="Active",
                        assigned_to="unknown@example.com",
                        target_date=None,
                        risk_level=RiskLevel.HIGH,
                        area_path="One\\Demo",
                ),
                programs_root=programs_root,
        )

        report = run_doctor(kb=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["KB Drift"].status == "ok"
        assert "No people-directory drift detected against recent ADO assignees." in checks["KB Drift"].detail


def test_run_doctor_kb_reads_sqlite_backed_recent_ado_assignees(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=("11111111-1111-1111-1111-111111111111",))
        _set_program_storage_backend(programs_root, program_id="demo", storage_backend="sqlite")

        class FakeADOClient:
                def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                        self.organization = organization
                        self.project = project

                def get_saved_query(self, query_id: str) -> dict[str, str]:
                        return {"id": query_id}

        monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

        knowledge_dir = programs_root / "demo" / "knowledge"
        (knowledge_dir / "people_directory.yaml").write_text(
                (
                        'schema_version: "1.0"\n'
                        'people:\n'
                        '  - alias: demo\n'
                        '    email: demo@example.com\n'
                        '    display_name: Demo Author\n'
                        '  - alias: stale\n'
                        '    email: stale@example.com\n'
                        '    display_name: Stale Person\n'
                ),
                encoding="utf-8",
        )

        recent_date = datetime.now(timezone.utc).date() - timedelta(days=1)
        trajectory_store = SQLiteTrajectoryStore(programs_root=programs_root)
        trajectory_store.append(
                "demo",
                1001,
                TrajectoryPoint(
                        date=recent_date,
                        state="Active",
                        assigned_to="demo@example.com",
                        target_date=None,
                        risk_level=RiskLevel.MEDIUM,
                        area_path="One\\Demo",
                ),
        )
        for work_item_id in range(1002, 1007):
                trajectory_store.append(
                        "demo",
                        work_item_id,
                        TrajectoryPoint(
                                date=recent_date,
                                state="Active",
                                assigned_to="unknown@example.com",
                                target_date=None,
                                risk_level=RiskLevel.HIGH,
                                area_path="One\\Demo",
                        ),
                )

        report = run_doctor(kb=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["KB Drift"].status == "warn"
        assert "knowledge not seen in ADO for 90+ days" in checks["KB Drift"].detail
        assert "recent repeat ADO assignee (>= 5 active items) missing from people_directory.yaml" in checks["KB Drift"].detail
        assert "demo/stale" in checks["KB Drift"].detail
        assert "demo/unknown" in checks["KB Drift"].detail


def test_run_doctor_kb_distinguishes_referenced_and_unreferenced_stale_people(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=("11111111-1111-1111-1111-111111111111",))

        class FakeADOClient:
                def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                        self.organization = organization
                        self.project = project

                def get_saved_query(self, query_id: str) -> dict[str, str]:
                        return {"id": query_id}

        monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

        knowledge_dir = programs_root / "demo" / "knowledge"
        (knowledge_dir / "people_directory.yaml").write_text(
                (
                        'schema_version: "1.0"\n'
                        'people:\n'
                        '  - alias: demo\n'
                        '    email: demo@example.com\n'
                        '    display_name: Demo Author\n'
                        '  - alias: retained\n'
                        '    email: retained@example.com\n'
                        '    display_name: Retained Person\n'
                        '  - alias: stale\n'
                        '    email: stale@example.com\n'
                        '    display_name: Stale Person\n'
                ),
                encoding="utf-8",
        )

        program_path = programs_root / "demo" / "program.yaml"
        program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        program_doc["charter"] = {
                "stakeholder_register": [
                        {"alias": "retained", "display_name": "Retained Person"},
                ]
        }
        program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False), encoding="utf-8")

        recent_date = datetime.now(timezone.utc).date() - timedelta(days=1)
        append_trajectory_point(
                "demo",
                1001,
                TrajectoryPoint(
                        date=recent_date,
                        state="Active",
                        assigned_to="demo@example.com",
                        target_date=None,
                        risk_level=RiskLevel.MEDIUM,
                        area_path="One\\Demo",
                ),
                programs_root=programs_root,
        )

        report = run_doctor(kb=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["KB Drift"].status == "warn"
        assert "unreferenced by current program YAML" in checks["KB Drift"].detail
        assert "still referenced by current program YAML" in checks["KB Drift"].detail
        assert "alias refs: 1" in checks["KB Drift"].detail
        assert "top files: program.yaml x1" in checks["KB Drift"].detail
        assert "demo/stale" in checks["KB Drift"].detail
        assert "demo/retained" in checks["KB Drift"].detail

        payload = _build_doctor_payload(report=report, tip=None)
        kb_drift = next(check for check in payload["checks"] if check["label"] == "KB Drift")
        assert kb_drift["metadata"]["missing_in_recent_ado_unreferenced"] == ["demo/stale"]
        assert kb_drift["metadata"]["missing_in_recent_ado_referenced"] == ["demo/retained"]
        assert kb_drift["metadata"]["retained_reference_kinds"] == {"alias": 1, "display_name": 1}
        assert kb_drift["metadata"]["retained_reference_files"] == [
                {
                        "aliases": ["demo/retained"],
                        "path": "program.yaml",
                        "reference_locations": [
                                "charter.stakeholder_register[0].alias",
                                "charter.stakeholder_register[0].display_name",
                        ],
                        "retained_alias_count": 1,
                }
        ]


def test_run_doctor_kb_ignores_trusted_baseline_people_references(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=("11111111-1111-1111-1111-111111111111",))

        class FakeADOClient:
                def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                        self.organization = organization
                        self.project = project

                def get_saved_query(self, query_id: str) -> dict[str, str]:
                        return {"id": query_id}

        monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

        shared_knowledge_root = programs_root.parent / "knowledge"
        shared_knowledge_root.mkdir(parents=True, exist_ok=True)
        _people_with_stale = (
                'schema_version: "1.0"\n'
                'people:\n'
                '  - alias: demo\n'
                '    email: demo@example.com\n'
                '    display_name: Demo Person\n'
                '  - alias: stale\n'
                '    email: stale@example.com\n'
                '    display_name: Stale Person\n'
        )
        (shared_knowledge_root / "people_directory.yaml").write_text(_people_with_stale, encoding="utf-8")
        (programs_root / "demo" / "knowledge" / "people_directory.yaml").write_text(_people_with_stale, encoding="utf-8")
        (programs_root / "demo" / "trusted_baseline.yaml").write_text(
                (
                        'schema_version: "1.0"\n'
                        'edition: demo_weekly\n'
                        'trusted_issue_number: 7\n'
                        'established_by: Stale Person\n'
                ),
                encoding="utf-8",
        )

        recent_date = datetime.now(timezone.utc).date() - timedelta(days=1)
        append_trajectory_point(
                "demo",
                1001,
                TrajectoryPoint(
                        date=recent_date,
                        state="Active",
                        assigned_to="demo@example.com",
                        target_date=None,
                        risk_level=RiskLevel.MEDIUM,
                        area_path="One\\Demo",
                ),
                programs_root=programs_root,
        )

        report = run_doctor(kb=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["KB Drift"].status == "warn"
        assert "unreferenced by current program YAML" in checks["KB Drift"].detail
        assert "demo/stale" in checks["KB Drift"].detail
        assert "still referenced by current program YAML" not in checks["KB Drift"].detail

        payload = _build_doctor_payload(report=report, tip=None)
        kb_drift = next(check for check in payload["checks"] if check["label"] == "KB Drift")
        assert kb_drift["metadata"]["missing_in_recent_ado_unreferenced"] == ["demo/stale"]
        assert kb_drift["metadata"]["missing_in_recent_ado_referenced"] == []
        assert "retained_reference_files" not in kb_drift["metadata"]


def test_run_doctor_kb_ignores_program_people_glossary_references(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=("11111111-1111-1111-1111-111111111111",))

        class FakeADOClient:
                def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                        self.organization = organization
                        self.project = project

                def get_saved_query(self, query_id: str) -> dict[str, str]:
                        return {"id": query_id}

        monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

        shared_knowledge_root = programs_root.parent / "knowledge"
        shared_knowledge_root.mkdir(parents=True, exist_ok=True)
        _people_with_stale = (
                'schema_version: "1.0"\n'
                'people:\n'
                '  - alias: demo\n'
                '    email: demo@example.com\n'
                '    display_name: Demo Person\n'
                '  - alias: stale\n'
                '    email: stale@example.com\n'
                '    display_name: Stale Person\n'
        )
        (shared_knowledge_root / "people_directory.yaml").write_text(_people_with_stale, encoding="utf-8")
        (programs_root / "demo" / "knowledge" / "people_directory.yaml").write_text(_people_with_stale, encoding="utf-8")

        program_path = programs_root / "demo" / "program.yaml"
        program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        program_doc["people"] = [
                {
                        "email": "stale@example.com",
                        "display_name": "Stale Person",
                        "role": "Historical contact",
                }
        ]
        program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False), encoding="utf-8")

        recent_date = datetime.now(timezone.utc).date() - timedelta(days=1)
        append_trajectory_point(
                "demo",
                1001,
                TrajectoryPoint(
                        date=recent_date,
                        state="Active",
                        assigned_to="demo@example.com",
                        target_date=None,
                        risk_level=RiskLevel.MEDIUM,
                        area_path="One\\Demo",
                ),
                programs_root=programs_root,
        )

        report = run_doctor(kb=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["KB Drift"].status == "warn"
        assert "unreferenced by current program YAML" in checks["KB Drift"].detail
        assert "demo/stale" in checks["KB Drift"].detail
        assert "still referenced by current program YAML" not in checks["KB Drift"].detail

        payload = _build_doctor_payload(report=report, tip=None)
        kb_drift = next(check for check in payload["checks"] if check["label"] == "KB Drift")
        assert kb_drift["metadata"]["missing_in_recent_ado_unreferenced"] == ["demo/stale"]
        assert kb_drift["metadata"]["missing_in_recent_ado_referenced"] == []


def test_run_doctor_kb_fails_on_unknown_workstream_owner(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=("11111111-1111-1111-1111-111111111111",))

        class FakeADOClient:
                def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                        self.organization = organization
                        self.project = project

                def get_saved_query(self, query_id: str) -> dict[str, str]:
                        return {"id": query_id}

        monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

        (programs_root / "demo" / "workstreams.yaml").write_text(
                (
                        'schema_version: "2.0"\n'
                        'workstreams:\n'
                        '  - id: ws_demo\n'
                        '    name: Demo WS\n'
                        "    area_paths: ['One\\\\Demo']\n"
                        '    pm_owner: missing\n'
                ),
                encoding="utf-8",
        )

        report = run_doctor(kb=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["Knowledge"].status == "fail"
        assert "Unknown pm_owner 'missing' referenced by workstream 'ws_demo'." in checks["Knowledge"].detail


def test_run_doctor_kb_reports_engms_page_counts(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=("11111111-1111-1111-1111-111111111111",))

        class FakeADOClient:
                def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                        self.organization = organization
                        self.project = project

                def get_saved_query(self, query_id: str) -> dict[str, str]:
                        return {"id": query_id}

        monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

        knowledge_dir = programs_root / "demo" / "knowledge"
        (knowledge_dir / "engms_pages.yaml").write_text(
                (
                        'schema_version: "1.0"\n'
                        'pages:\n'
                        '  - id: acme-readiness\n'
                        '    title: Acme Readiness\n'
                        '    url: https://eng.ms/acme-readiness\n'
                ),
                encoding="utf-8",
        )

        report = run_doctor(kb=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["Knowledge"].status == "ok"
        assert "1 eng.ms pages" in checks["Knowledge"].detail


def test_run_doctor_kb_prefers_program_local_engms_pages_over_shared_root(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=("11111111-1111-1111-1111-111111111111",))

        class FakeADOClient:
                def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                        self.organization = organization
                        self.project = project

                def get_saved_query(self, query_id: str) -> dict[str, str]:
                        return {"id": query_id}

        monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

        shared_knowledge_root = programs_root.parent / "knowledge"
        shared_knowledge_root.mkdir(parents=True, exist_ok=True)
        (shared_knowledge_root / "engms_pages.yaml").write_text(
                'schema_version: "1.0"\npages: []\n',
                encoding="utf-8",
        )

        knowledge_dir = programs_root / "demo" / "knowledge"
        (knowledge_dir / "engms_pages.yaml").write_text(
                (
                        'schema_version: "1.0"\n'
                        'pages:\n'
                        '  - id: acme-readiness\n'
                        '    title: Acme Readiness\n'
                        '    url: https://eng.ms/acme-readiness\n'
                ),
                encoding="utf-8",
        )

        report = run_doctor(kb=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["Knowledge"].status == "ok"
        assert "1 eng.ms pages" in checks["Knowledge"].detail


def test_run_doctor_kb_fails_on_unknown_charter_stakeholder_alias(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=("11111111-1111-1111-1111-111111111111",))

        class FakeADOClient:
                def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                        self.organization = organization
                        self.project = project

                def get_saved_query(self, query_id: str) -> dict[str, str]:
                        return {"id": query_id}

        monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

        (programs_root / "demo" / "program.yaml").write_text(
                (
                        'schema_version: "2.0"\n'
                        'id: demo\n'
                        'name: Demo Program\n'
                        'charter:\n'
                        '  stakeholder_register:\n'
                        '    - alias: missing\n'
                        '      role: sponsor\n'
                        '      interest: Delivery confidence\n'
                        'ado:\n'
                        '  organization: your-org\n'
                        '  project: One\n'
                        '  area_paths: []\n'
                        '  work_item_types: [Feature]\n'
                        '  excluded_states: [Removed]\n'
                        '  date_window_days: 14\n'
                        '  api_timeout_seconds: 30\n'
                ),
                encoding="utf-8",
        )

        report = run_doctor(kb=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["Knowledge"].status == "fail"
        assert "Unknown charter stakeholder alias 'missing' referenced by program 'demo'." in checks["Knowledge"].detail


def test_run_doctor_kb_fails_on_unknown_registry_stakeholder(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        editions_root = reports_root.parent / "editions"
        programs_root = reports_root.parent / "programs"

        class FakeADOClient:
                def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                        self.organization = organization
                        self.project = project

                def get_saved_query(self, query_id: str) -> dict[str, str]:
                        return {"id": query_id}

        monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

        registry_path = programs_root / "acme" / "workstream_registry.yaml"
        registry_doc = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        workstreams = registry_doc["workstreams"]
        target_ws = next((ws for ws in workstreams if isinstance(ws, dict) and ws.get("id") == "acme"), workstreams[0])
        if not target_ws.get("stakeholders"):
            target_ws["stakeholders"] = [{"alias": "test_stakeholder", "name": "Existing Person"}]
        target_ws["stakeholders"][0]["name"] = "Missing Registry Person"
        registry_path.write_text(yaml.safe_dump(registry_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

        report = run_doctor(kb=True, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["Knowledge"].status == "fail"
        assert "Unknown registry stakeholder 'Missing Registry Person'" in checks["Knowledge"].detail


def test_run_doctor_kb_warns_when_shared_knowledge_references_unstaged_programs(
        monkeypatch,
        repo_root: Path,
        tmp_path: Path,
) -> None:
        # The real knowledge/teams.yaml already references program 'fabrikam' via the
        # armada_program/armada_service_fabric/armada_deployment teams.  stage_v2_report_workspace
        # copies that directory; the programs_root only contains 'acme', so the Knowledge
        # Scope check should warn about fabrikam being unregistered while the structural
        # Knowledge check still passes (all team_ids and person aliases are consistent).
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        editions_root = reports_root.parent / "editions"
        programs_root = reports_root.parent / "programs"

        class FakeADOClient:
                def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                        self.organization = organization
                        self.project = project

                def get_saved_query(self, query_id: str) -> dict[str, str]:
                        return {"id": query_id}

        monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

        report = run_doctor(kb=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["Knowledge"].status == "ok"
        assert checks["Knowledge Scope"].status == "warn"
        assert "Unknown program 'fabrikam' referenced by team" in checks["Knowledge Scope"].detail


def test_run_doctor_kb_fails_on_invalid_raci_types_and_unknown_alias(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=("11111111-1111-1111-1111-111111111111",))

        class FakeADOClient:
                def __init__(self, organization: str, project: str, timeout: int = 30, show_progress: bool = True) -> None:
                        self.organization = organization
                        self.project = project

                def get_saved_query(self, query_id: str) -> dict[str, str]:
                        return {"id": query_id}

        monkeypatch.setattr("src.commands.doctor.ADOClient", FakeADOClient)

        (programs_root / "demo" / "workstreams.yaml").write_text(
                (
                        'schema_version: "2.0"\n'
                        'workstreams:\n'
                        '  - id: ws_demo\n'
                        '    name: Demo WS\n'
                        "    area_paths: ['One\\\\Demo']\n"
                        '    raci:\n'
                        '      responsible: demo\n'
                        '      accountable:\n'
                        '        - demo\n'
                        '      consulted: [demo, missing]\n'
                        '      informed: []\n'
                ),
                encoding="utf-8",
        )

        report = run_doctor(kb=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["Knowledge"].status == "fail"
        assert "Workstream 'ws_demo' raci.responsible must be a list of aliases." in checks["Knowledge"].detail
        assert "Workstream 'ws_demo' raci.accountable must be a single alias string." in checks["Knowledge"].detail


def test_run_doctor_milestones_warns_when_file_absent(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())

        report = run_doctor(
                edition_name="demo_weekly",
                milestones=True,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        assert report.failures == 0
        checks = {check.label: check for check in report.checks}
        assert checks["Milestones"].status == "warn"
        assert "milestones.yaml is absent" in checks["Milestones"].detail


def test_run_doctor_milestones_validates_schema_and_references(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _write_demo_milestones(programs_root)

        report = run_doctor(
                edition_name="demo_weekly",
                milestones=True,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        assert report.failures == 0
        checks = {check.label: check for check in report.checks}
        assert checks["Milestones"].status == "ok"
        assert "1 milestone" in checks["Milestones"].detail
        assert "schema and references valid" in checks["Milestones"].detail


def test_run_doctor_milestones_warns_on_degraded_health_from_confirmed_snapshot(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        archive_root = tmp_path / "archive"
        _write_demo_milestones(programs_root)
        snapshot_as_of = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
        _write_confirmed_archive_index(
                archive_root,
                edition_name="demo_weekly",
                generated_at=snapshot_as_of,
        )
        _write_demo_snapshot(
                archive_root,
                edition_name="demo_weekly",
                generated_at=snapshot_as_of,
        )
        append_trajectory_point(
                "demo",
                1001,
                TrajectoryPoint(
                        date=snapshot_as_of.date(),
                        state="Active",
                        assigned_to="demo@example.com",
                        target_date=datetime(2026, 6, 2, tzinfo=timezone.utc).date(),
                        risk_level=RiskLevel.HIGH,
                        area_path="One\\Demo",
                ),
                programs_root=programs_root,
        )

        report = run_doctor(
                edition_name="demo_weekly",
                milestones=True,
                editions_root=editions_root,
                programs_root=programs_root,
                archive_root=archive_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Milestones"].status == "warn"
        assert "Latest confirmed snapshot shows 1 at risk milestone." in checks["Milestones"].detail
        assert "Demo Milestone: Tracking 2026-06-02 (3 days late vs target)." in checks["Milestones"].detail
        assert "1 affected milestone missing linked risk coverage." in checks["Milestones"].detail


def test_run_doctor_milestones_warns_on_degraded_health_from_sqlite_snapshot(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        archive_root = tmp_path / "archive"
        _write_demo_milestones(programs_root)
        _set_program_storage_backend(programs_root, program_id="demo", storage_backend="sqlite")
        snapshot_as_of = datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc)
        _write_confirmed_archive_index(
                archive_root,
                edition_name="demo_weekly",
                generated_at=snapshot_as_of,
        )
        _write_demo_snapshot(
                archive_root,
                edition_name="demo_weekly",
                generated_at=snapshot_as_of,
        )
        SQLiteTrajectoryStore(programs_root=programs_root).append(
                "demo",
                1001,
                TrajectoryPoint(
                        date=snapshot_as_of.date(),
                        state="Active",
                        assigned_to="demo@example.com",
                        target_date=datetime(2026, 6, 2, tzinfo=timezone.utc).date(),
                        risk_level=RiskLevel.HIGH,
                        area_path="One\\Demo",
                ),
        )

        report = run_doctor(
                edition_name="demo_weekly",
                milestones=True,
                editions_root=editions_root,
                programs_root=programs_root,
                archive_root=archive_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Milestones"].status == "warn"
        assert "Latest confirmed snapshot shows 1 at risk milestone." in checks["Milestones"].detail
        assert "Demo Milestone: Tracking 2026-06-02 (3 days late vs target)." in checks["Milestones"].detail
        assert "1 affected milestone missing linked risk coverage." in checks["Milestones"].detail


def test_run_doctor_milestones_fails_on_unknown_alias_and_workstream(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _write_demo_milestones(
                programs_root,
                owner_alias="missing",
                linked_workstream_ids=("missing_ws",),
        )

        report = run_doctor(
                edition_name="demo_weekly",
                milestones=True,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Milestones"].status == "fail"
        assert "Unknown owner_alias 'missing' referenced by milestone 'm1'." in checks["Milestones"].detail
        assert "Unknown linked_workstream_id 'missing_ws' referenced by milestone 'm1'." in checks["Milestones"].detail


def test_doctor_cli_milestones(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _write_demo_milestones(programs_root)

        monkeypatch.setattr("src.commands.doctor.EDITIONS_ROOT", editions_root)
        monkeypatch.setattr("src.commands.doctor.PROGRAMS_ROOT", programs_root)

        result = runner.invoke(app, ["doctor", "--edition", "demo_weekly", "--milestones"])

        assert result.exit_code == 0
        assert "Milestones" in result.stdout
        assert "Overall: HEALTHY" in result.stdout


def test_run_doctor_metric_bindings_revalidates_stale_bindings_and_warns_on_drift(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        db_root = tmp_path / "db"
        store = RealityStore("demo", db_root=db_root)
        store.initialize()
        store.upsert_metric_source_binding(
                MetricSourceBinding(
                        binding_id="binding-ok",
                        metric_id="demo.cluster_count",
                        program_id="demo",
                        source_kind="kusto",
                        cluster="https://demo.kusto.windows.net",
                        database="demo",
                        kql_template="DemoMetrics | summarize Count=count()",
                        result_column="Count",
                        validated=True,
                        last_validated_at=datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc),
                )
        )
        store.upsert_metric_source_binding(
                MetricSourceBinding(
                        binding_id="binding-drift",
                        metric_id="demo.cluster_count",
                        program_id="demo",
                        source_kind="kusto",
                        cluster="https://demo.kusto.windows.net",
                        database="demo",
                        kql_template="DemoMetrics | summarize Count=count()",
                        result_column="Count",
                        validated=True,
                        last_validated_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
                        last_validated_kql_hash="stale-hash",
                )
        )

        report = run_doctor(
                edition_name="demo_weekly",
                metric_bindings=True,
                editions_root=editions_root,
                programs_root=programs_root,
                reality_db_root=db_root,
                metric_definitions={
                        "demo.cluster_count": MetricDefinition(
                                id="demo.cluster_count",
                                title="Cluster count",
                                unit="count",
                                aggregation=MetricAggregation.LAST,
                        )
                },
                metric_binding_probe=lambda binding: (
                        [{"Count": 5}],
                        (KustoColumn("Count", "long"),),
                ),
                now=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Metric Bindings"].status == "warn"
        assert checks["Binding Revalidation"].status == "ok"
        assert checks["Binding Drift"].status == "warn"
        assert "binding-drift" in checks["Binding Drift"].detail
        refreshed = store.get_metric_source_binding("binding-ok")
        assert refreshed is not None
        assert refreshed.last_validated_at == datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc)


def test_run_doctor_metric_bindings_marks_failed_revalidation_unvalidated(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        db_root = tmp_path / "db"
        store = RealityStore("demo", db_root=db_root)
        store.initialize()
        store.upsert_metric_source_binding(
                MetricSourceBinding(
                        binding_id="binding-fail",
                        metric_id="demo.cluster_count",
                        program_id="demo",
                        source_kind="kusto",
                        cluster="https://demo.kusto.windows.net",
                        database="demo",
                        kql_template="DemoMetrics | summarize Count=count()",
                        result_column="Count",
                        validated=True,
                        last_validated_at=datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc),
                )
        )

        report = run_doctor(
                edition_name="demo_weekly",
                metric_bindings=True,
                editions_root=editions_root,
                programs_root=programs_root,
                reality_db_root=db_root,
                metric_definitions={
                        "demo.cluster_count": MetricDefinition(
                                id="demo.cluster_count",
                                title="Cluster count",
                                unit="count",
                                aggregation=MetricAggregation.LAST,
                        )
                },
                metric_binding_probe=lambda binding: (_ for _ in ()).throw(QueryError("schema mismatch")),
                now=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Metric Bindings"].status == "warn"
        assert checks["Binding Revalidation"].status == "warn"
        failed = store.get_metric_source_binding("binding-fail")
        assert failed is not None
        assert failed.validated is False


def test_run_doctor_metric_bindings_warns_when_rollout_missing_for_eligible_query(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        metrics_root = tmp_path / "metrics"
        metrics_root.mkdir()
        (metrics_root / "demo.yaml").write_text(
                """
metrics:
  - id: demo.cluster_count
    title: Cluster count
    unit: count
    aggregation: last
    slo_target: 0
    slo_direction: lte
""".strip(),
                encoding="utf-8",
        )
        program_dir = programs_root / "demo"
        (program_dir / "kpis.yaml").write_text(
                """
schema_version: "1.0"
kpis:
  - id: demo-cluster-count
    metric_id: demo.cluster_count
    assertion_ids: [assertion-demo-cluster-count]
    workstream_ids: [demo]
    program_ids: [demo]
    cluster: https://demo.kusto.windows.net
    database: demo
    kql: DemoMetrics | summarize Count=count()
    section: Demo
    render_as: metric_highlight
    confidence: medium
    refresh_on_gather: true
    result_column: Count
""".strip(),
                encoding="utf-8",
        )

        report = run_doctor(
                edition_name="demo_weekly",
                metric_bindings=True,
                editions_root=editions_root,
                programs_root=programs_root,
                reality_db_root=tmp_path / "db",
                metric_definitions={
                        "demo.cluster_count": MetricDefinition(
                                id="demo.cluster_count",
                                title="Cluster count",
                                unit="count",
                                aggregation=MetricAggregation.LAST,
                                slo_target=0,
                                slo_direction="lte",
                        )
                },
                now=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Metric Rollout"].status == "warn"
        assert "demo-cluster-count" in checks["Metric Rollout"].detail


def test_run_doctor_metric_bindings_marks_rollout_ok_when_eligible_query_is_provisioned(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        program_dir = programs_root / "demo"
        (program_dir / "kpis.yaml").write_text(
                """
schema_version: "1.0"
kpis:
  - id: demo-cluster-count
    metric_id: demo.cluster_count
    assertion_ids: [assertion-demo-cluster-count]
    workstream_ids: [demo]
    program_ids: [demo]
    cluster: https://demo.kusto.windows.net
    database: demo
    kql: DemoMetrics | summarize Count=count()
    section: Demo
    render_as: metric_highlight
    confidence: medium
    refresh_on_gather: true
    result_column: Count
""".strip(),
                encoding="utf-8",
        )
        db_root = tmp_path / "db"
        store = RealityStore("demo", db_root=db_root)
        store.initialize()
        store.upsert_metric_source_binding(
                MetricSourceBinding(
                        binding_id="binding-001",
                        metric_id="demo.cluster_count",
                        program_id="demo",
                        source_kind="kusto",
                        cluster="https://demo.kusto.windows.net",
                        database="demo",
                        kql_template="DemoMetrics | summarize Count=count()",
                        result_column="Count",
                        validated=True,
                        last_validated_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
                )
        )
        from src.core.hypothesis_models import TelemetryAssertion, AssertionOperator
        from src.core.metric_models import ObservationWindow
        store.upsert_telemetry_assertion(
                TelemetryAssertion(
                        id="assertion-demo-cluster-count",
                        program_id="demo",
                        metric_id="demo.cluster_count",
                        window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
                        operator=AssertionOperator.LTE,
                        threshold=0,
                )
        )

        report = run_doctor(
                edition_name="demo_weekly",
                metric_bindings=True,
                editions_root=editions_root,
                programs_root=programs_root,
                reality_db_root=db_root,
                metric_definitions={
                        "demo.cluster_count": MetricDefinition(
                                id="demo.cluster_count",
                                title="Cluster count",
                                unit="count",
                                aggregation=MetricAggregation.LAST,
                                slo_target=0,
                                slo_direction="lte",
                        )
                },
                metric_binding_probe=lambda binding: (
                        [{"Count": 5}],
                        (KustoColumn("Count", "long"),),
                ),
                now=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Metric Rollout"].status == "ok"


def test_doctor_cli_metric_bindings_admin_alias(monkeypatch, tmp_path: Path) -> None:
        report = DoctorReport(
                edition="demo_weekly",
                checks=(DoctorCheck("Metric Bindings", "ok", "1 active binding(s)."),),
        )

        monkeypatch.setattr(
                "src.commands.doctor.run_doctor",
                lambda **kwargs: report,
        )

        result = runner.invoke(app, ["admin", "doctor", "--edition", "demo_weekly", "--metric-bindings"])

        assert result.exit_code == 0
        assert "Metric Bindings" in result.stdout


def test_run_doctor_dependencies_warns_when_file_absent_and_legacy_present(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _write_demo_legacy_dependencies(programs_root)

        report = run_doctor(
                edition_name="demo_weekly",
                dependencies=True,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        assert report.failures == 0
        checks = {check.label: check for check in report.checks}
        assert checks["Dependencies"].status == "warn"
        assert "legacy key_dependency" in checks["Dependencies"].detail


def test_run_doctor_dependencies_validates_schema_and_cross_program_refs(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _write_demo_milestones(programs_root)
        _seed_dependency_partner_program(programs_root)
        _write_demo_dependencies(programs_root)

        report = run_doctor(
                edition_name="demo_weekly",
                dependencies=True,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        assert report.failures == 0
        checks = {check.label: check for check in report.checks}
        assert checks["Dependencies"].status == "ok"
        assert "dependencies.yaml loaded (2 dependencies)" in checks["Dependencies"].detail
        assert "resolution_path classification valid" in checks["Dependencies"].detail
        assert "Legacy key_dependencies" not in checks["Dependencies"].detail


def test_run_doctor_dependencies_warns_on_recent_cross_org_dependency_signals(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _write_demo_milestones(programs_root)
        _seed_dependency_partner_program(programs_root)
        _write_demo_dependencies(programs_root)

        signal_timestamp = datetime.now(timezone.utc)
        append_signal(
                Signal(
                        id="dep-signal-1",
                        timestamp=signal_timestamp,
                        source="ado/dependency",
                        program_id="demo",
                        workstream_id="ws_demo",
                        entity_refs=("WI:1234",),
                        text="Dependency OneDeploy stager: owner escalation still overdue.",
                        raw_ref="dependency:OneDeploy stager:1234:FR-21",
                        confidence=Confidence.HIGH,
                        metadata={
                                "dependency_label": "OneDeploy stager",
                                "resolution_path": "cross_org_onedeploy",
                                "work_item_id": 1234,
                                "finding_type": "FR-21",
                                "severity": "block",
                                "date": signal_timestamp.date().isoformat(),
                        },
                ),
                programs_root=programs_root,
                partition_at=signal_timestamp,
        )
        append_review_decision(
                "demo",
                SignalReviewDecision(
                        signal_id="dep-signal-1",
                        decision="approved",
                        reviewed_at=signal_timestamp + timedelta(minutes=5),
                        reviewed_by="system",
                ),
                programs_root=programs_root,
        )

        report = run_doctor(
                edition_name="demo_weekly",
                dependencies=True,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        assert report.failures == 0
        checks = {check.label: check for check in report.checks}
        assert checks["Dependencies"].status == "warn"
        assert "Recent dependency findings" in checks["Dependencies"].detail
        assert "Cross-org dependency stale" in checks["Dependencies"].detail
        assert "OneDeploy stager" in checks["Dependencies"].detail
        assert checks["Dependencies"].metadata is not None
        assert checks["Dependencies"].metadata["program_id"] == "demo"
        assert checks["Dependencies"].metadata["recent_cross_org_dependency_finding_count"] == 1
        assert checks["Dependencies"].metadata["recent_internal_dependency_finding_count"] == 0
        assert checks["Dependencies"].metadata["recent_dependency_findings"][0]["classification"] == "cross_org"
        assert checks["Dependencies"].metadata["recent_dependency_findings"][0]["resolution_path"] == "cross_org_onedeploy"


def test_run_doctor_dependencies_ignore_unrelated_action_loader_failures(tmp_path: Path, monkeypatch) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _write_demo_milestones(programs_root)
        _seed_dependency_partner_program(programs_root)
        _write_demo_dependencies(programs_root)

        def _boom(*args, **kwargs):
                raise ConfigError("actions broken")

        monkeypatch.setattr("src.core.action_tracker.load_actions", _boom)

        report = run_doctor(
                edition_name="demo_weekly",
                dependencies=True,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Dependencies"].status == "ok"
        assert "dependencies.yaml loaded (2 dependencies)" in checks["Dependencies"].detail


def test_run_doctor_risks_validates_register(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _write_demo_milestones(programs_root)
        today = datetime.now(timezone.utc).date()
        _write_demo_risks(
                programs_root,
                identified_date=(today - timedelta(days=5)).isoformat(),
                last_reviewed_date=(today - timedelta(days=1)).isoformat(),
        )

        report = run_doctor(edition_name="demo_weekly", risks=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["Risks"].status == "ok"
        assert "review dates valid" in checks["Risks"].detail


def test_run_doctor_risks_fails_on_unknown_references(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _write_demo_risks(programs_root, owner_alias="unknown", linked_workstream_ids=("ws_missing",))

        report = run_doctor(edition_name="demo_weekly", risks=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["Risks"].status == "fail"
        assert "Unknown owner_alias 'unknown'" in checks["Risks"].detail or "Unknown linked_workstream_id 'ws_missing'" in checks["Risks"].detail


def test_run_doctor_risks_warns_on_stale_open_entries(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _write_demo_risks(programs_root, identified_date="2026-01-01", last_reviewed_date=None)

        report = run_doctor(edition_name="demo_weekly", risks=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["Risks"].status == "warn"
        assert "need review" in checks["Risks"].detail


def test_run_doctor_escalations_validates_rules_and_state(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _write_demo_escalation_rules(programs_root)
        (programs_root / "demo" / "escalation_state.json").write_text(
                json.dumps({"consecutive_high:ws_demo": "2026-05-11T12:00:00+00:00"}, indent=2),
                encoding="utf-8",
        )

        report = run_doctor(
                edition_name="demo_weekly",
                escalations=True,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Escalations"].status == "ok"
        assert "3 rules" in checks["Escalations"].detail
        assert "tracked cooldown key" in checks["Escalations"].detail


def test_run_doctor_escalations_fails_on_invalid_state(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _write_demo_escalation_rules(programs_root)
        (programs_root / "demo" / "escalation_state.json").write_text(
                json.dumps({"consecutive_high:ws_demo": 123}, indent=2),
                encoding="utf-8",
        )

        report = run_doctor(
                edition_name="demo_weekly",
                escalations=True,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Escalations"].status == "fail"
        assert "must be an ISO timestamp string" in checks["Escalations"].detail


def test_doctor_cli_escalations(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _write_demo_escalation_rules(programs_root)

        monkeypatch.setattr("src.commands.doctor.EDITIONS_ROOT", editions_root)
        monkeypatch.setattr("src.commands.doctor.PROGRAMS_ROOT", programs_root)

        result = runner.invoke(app, ["doctor", "--edition", "demo_weekly", "--escalations"])

        assert result.exit_code == 0
        assert "Escalations" in result.stdout
        assert "Overall: HEALTHY" in result.stdout


def test_run_doctor_actions_validates_register(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _write_demo_actions(programs_root)

        report = run_doctor(edition_name="demo_weekly", actions=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["Actions"].status == "ok"
        assert "due dates valid" in checks["Actions"].detail


def test_run_doctor_actions_warns_on_overdue_open_entries(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _write_demo_actions(programs_root, due_date="2026-01-01")

        report = run_doctor(edition_name="demo_weekly", actions=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["Actions"].status == "warn"
        assert "overdue" in checks["Actions"].detail


def test_run_doctor_actions_fails_on_unknown_references(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _write_demo_actions(programs_root, owner_alias="unknown", workstream_id="ws_missing")

        report = run_doctor(edition_name="demo_weekly", actions=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["Actions"].status == "fail"
        assert "Unknown owner_alias 'unknown'" in checks["Actions"].detail or "Unknown workstream_id 'ws_missing'" in checks["Actions"].detail


def test_run_doctor_decisions_validates_register(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _write_demo_decisions(programs_root)

        report = run_doctor(edition_name="demo_weekly", decisions=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["Decisions"].status == "ok"
        assert "proposal ages" in checks["Decisions"].detail


def test_run_doctor_decisions_warns_on_stale_proposals(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _write_demo_decisions(programs_root, status="proposed", decision_date="2026-04-01")

        report = run_doctor(edition_name="demo_weekly", decisions=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["Decisions"].status == "warn"
        assert "pending >14 days" in checks["Decisions"].detail


def test_run_doctor_decisions_warns_on_overdue_review_dates(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _write_demo_decisions(programs_root, review_by="2026-04-01")

        report = run_doctor(edition_name="demo_weekly", decisions=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["Decisions"].status == "warn"
        assert "overdue for review" in checks["Decisions"].detail


def test_run_doctor_decisions_fails_on_unknown_references(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _write_demo_decisions(programs_root, decided_by="unknown", workstream_id="ws_missing")

        report = run_doctor(edition_name="demo_weekly", decisions=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["Decisions"].status == "fail"
        assert "Unknown decided_by 'unknown'" in checks["Decisions"].detail or "Unknown workstream_id 'ws_missing'" in checks["Decisions"].detail


def test_run_doctor_assumptions_validates_register(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _write_demo_assumptions(programs_root)

        report = run_doctor(edition_name="demo_weekly", assumptions=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["Assumptions"].status == "ok"
        assert "validation dates valid" in checks["Assumptions"].detail


def test_run_doctor_assumptions_warns_on_overdue_validation(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _write_demo_assumptions(programs_root, validation_due="2026-04-01")

        report = run_doctor(edition_name="demo_weekly", assumptions=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["Assumptions"].status == "warn"
        assert "overdue for validation" in checks["Assumptions"].detail


def test_run_doctor_assumptions_fails_on_unknown_references(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _write_demo_assumptions(programs_root, owner_alias="unknown", linked_milestone_id="m_missing")

        report = run_doctor(edition_name="demo_weekly", assumptions=True, editions_root=editions_root, programs_root=programs_root)

        checks = {check.label: check for check in report.checks}
        assert checks["Assumptions"].status == "fail"
        assert "Unknown owner_alias 'unknown'" in checks["Assumptions"].detail or "Unknown linked_milestone_id 'm_missing'" in checks["Assumptions"].detail


def test_run_doctor_cadence_warns_when_communication_plan_entry_is_overdue(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        program_path = programs_root / "demo" / "program.yaml"
        program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        program_doc["communication_plan"] = [
                {
                        "edition": "demo_weekly",
                        "audience": "Demo leadership",
                        "channel": "email",
                        "cadence": "weekly",
                        "owner": "demo",
                }
        ]
        program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")
        archive_root = tmp_path / "archive"
        _write_confirmed_archive_index(
                archive_root,
                edition_name="demo_weekly",
                generated_at=datetime.now(timezone.utc) - timedelta(days=9),
        )

        report = run_doctor(
                edition_name="demo_weekly",
                cadence=True,
                editions_root=editions_root,
                programs_root=programs_root,
                archive_root=archive_root,
        )

        assert report.failures == 0
        cadence_checks = [check for check in report.checks if check.label.startswith("Cadence")]
        assert cadence_checks
        assert cadence_checks[0].status == "warn"
        assert "demo_weekly: overdue by" in cadence_checks[0].detail


def test_run_doctor_cadence_fails_on_mismatched_communication_plan_cadence(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        program_path = programs_root / "demo" / "program.yaml"
        program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        program_doc["communication_plan"] = [
                {
                        "edition": "demo_weekly",
                        "audience": "Demo leadership",
                        "channel": "email",
                        "cadence": "daily",
                        "owner": "demo",
                }
        ]
        program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

        report = run_doctor(
                edition_name="demo_weekly",
                cadence=True,
                editions_root=editions_root,
                programs_root=programs_root,
                archive_root=tmp_path / "archive",
        )

        cadence_checks = [check for check in report.checks if check.label.startswith("Cadence")]
        assert cadence_checks
        assert cadence_checks[0].status == "fail"
        assert "communication_plan cadence 'daily' does not match edition cadence 'weekly'" in cadence_checks[0].detail


def test_run_doctor_cadence_preserves_duplicate_communication_plan_entries(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        program_path = programs_root / "demo" / "program.yaml"
        program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        program_doc["communication_plan"] = [
                {
                        "edition": "demo_weekly",
                        "audience": "Demo leadership",
                        "channel": "email",
                        "cadence": "weekly",
                        "owner": "demo",
                },
                {
                        "edition": "demo_weekly",
                        "audience": "Demo DRIs",
                        "channel": "teams",
                        "cadence": "weekly",
                        "owner": "demo",
                },
        ]
        program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

        report = run_doctor(
                edition_name="demo_weekly",
                cadence=True,
                editions_root=editions_root,
                programs_root=programs_root,
                archive_root=tmp_path / "archive",
        )

        cadence_checks = [check for check in report.checks if check.label.startswith("Cadence")]
        assert len(cadence_checks) == 2
        assert "Demo leadership" in cadence_checks[0].label
        assert "Demo DRIs" in cadence_checks[1].label


def test_doctor_cli_cadence(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        archive_root = tmp_path / "archive"
        _write_confirmed_archive_index(
                archive_root,
                edition_name="demo_weekly",
                generated_at=datetime.now(timezone.utc) - timedelta(days=9),
        )

        monkeypatch.setattr("src.commands.doctor.EDITIONS_ROOT", editions_root)
        monkeypatch.setattr("src.commands.doctor.PROGRAMS_ROOT", programs_root)
        monkeypatch.setattr("src.commands.doctor.ARCHIVE_ROOT", archive_root)

        result = runner.invoke(app, ["doctor", "--edition", "demo_weekly", "--cadence"])

        assert result.exit_code == 0
        assert "Cadence" in result.stdout
        assert "demo_weekly" in result.stdout
        assert "Overall: HEALTHY" in result.stdout


def test_run_doctor_dependencies_fails_on_missing_refs_and_cycles(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _write_demo_dependencies(
                programs_root,
                dependencies=(
                        {
                                "id": "dep-a-b",
                                "from_workstream_id": "ws_demo",
                                "to_workstream_id": "missing_ws",
                                "dependency_type": "blocks",
                                "risk_if_broken": "Execution stalls.",
                                "status": "active",
                        },
                ),
        )

        report = run_doctor(
                edition_name="demo_weekly",
                dependencies=True,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Dependencies"].status == "fail"
        assert "Unknown to_workstream_id 'missing_ws'" in checks["Dependencies"].detail

        _write_demo_dependencies(
                programs_root,
                dependencies=(
                        {
                                "id": "dep-a-b",
                                "from_workstream_id": "ws_demo",
                                "to_workstream_id": "demo_partner:partner_ws",
                                "dependency_type": "blocks",
                                "risk_if_broken": "Execution stalls.",
                                "status": "active",
                        },
                        {
                                "id": "dep-b-a",
                                "from_workstream_id": "demo_partner:partner_ws",
                                "to_workstream_id": "ws_demo",
                                "dependency_type": "blocks",
                                "risk_if_broken": "Execution stalls.",
                                "status": "active",
                        },
                ),
        )
        _seed_dependency_partner_program(programs_root)

        report = run_doctor(
                edition_name="demo_weekly",
                dependencies=True,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Dependencies"].status == "fail"
        assert "Dependency cycle detected" in checks["Dependencies"].detail


def test_doctor_cli_dependencies(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _write_demo_milestones(programs_root)
        _seed_dependency_partner_program(programs_root)
        _write_demo_dependencies(programs_root)

        monkeypatch.setattr("src.commands.doctor.EDITIONS_ROOT", editions_root)
        monkeypatch.setattr("src.commands.doctor.PROGRAMS_ROOT", programs_root)

        result = runner.invoke(app, ["doctor", "--edition", "demo_weekly", "--dependencies"])

        assert result.exit_code == 0
        assert "Dependencies" in result.stdout
        assert "Overall: HEALTHY" in result.stdout


def test_doctor_cli_decisions(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _write_demo_decisions(programs_root)

        monkeypatch.setattr("src.commands.doctor.EDITIONS_ROOT", editions_root)
        monkeypatch.setattr("src.commands.doctor.PROGRAMS_ROOT", programs_root)

        result = runner.invoke(app, ["doctor", "--edition", "demo_weekly", "--decisions"])

        assert result.exit_code == 0
        assert "Decisions" in result.stdout
        assert "Overall: HEALTHY" in result.stdout


def test_doctor_cli_assumptions(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        _write_demo_assumptions(programs_root)

        monkeypatch.setattr("src.commands.doctor.EDITIONS_ROOT", editions_root)
        monkeypatch.setattr("src.commands.doctor.PROGRAMS_ROOT", programs_root)

        result = runner.invoke(app, ["doctor", "--edition", "demo_weekly", "--assumptions"])

        assert result.exit_code == 0
        assert "Assumptions" in result.stdout
        assert "Overall: HEALTHY" in result.stdout


def test_run_doctor_circuit_breakers_defaults_to_closed_when_state_absent(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())

        report = run_doctor(
                edition_name="demo_weekly",
                circuit_breakers=True,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Circuit Breakers"].status == "ok"
        assert "effective ADO breaker state is CLOSED" in checks["Circuit Breakers"].detail
        assert "publications/demo_weekly/.ado_breaker.json" in checks["Circuit Breakers"].detail


def test_run_doctor_circuit_breakers_warns_when_open(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        state_path = (tmp_path / "programs" / "demo" / "publications") / "demo_weekly" / ".ado_breaker.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
                json.dumps(
                        {
                                "state": "OPEN",
                                "failure_count": 3,
                                "last_failure_at": "2026-05-11T10:00:00+00:00",
                                "last_opened_at": "2026-05-11T10:00:00+00:00",
                                "last_success_at": None,
                        },
                        indent=2,
                ) + "\n",
                encoding="utf-8",
        )

        report = run_doctor(
                edition_name="demo_weekly",
                circuit_breakers=True,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Circuit Breakers"].status == "warn"
        assert "ADO breaker OPEN" in checks["Circuit Breakers"].detail
        assert "failure_count=3" in checks["Circuit Breakers"].detail
        assert "Live freshness ADO requests remain gated until recovery or reset." in checks["Circuit Breakers"].detail


def test_run_doctor_circuit_breakers_resets_state_when_requested(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        state_path = (tmp_path / "programs" / "demo" / "publications") / "demo_weekly" / ".ado_breaker.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
                json.dumps(
                        {
                                "state": "OPEN",
                                "failure_count": 4,
                                "last_failure_at": "2026-05-11T10:00:00+00:00",
                                "last_opened_at": "2026-05-11T10:00:00+00:00",
                                "last_success_at": None,
                        },
                        indent=2,
                ) + "\n",
                encoding="utf-8",
        )

        report = run_doctor(
                edition_name="demo_weekly",
                circuit_breakers=True,
                reset_circuit_breakers=True,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Circuit Breakers"].status == "ok"
        assert "Reset ADO breaker from OPEN to CLOSED" in checks["Circuit Breakers"].detail

        payload = json.loads(state_path.read_text(encoding="utf-8"))
        assert payload["state"] == "CLOSED"
        assert payload["failure_count"] == 0


def test_doctor_cli_circuit_breakers(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        state_path = (tmp_path / "programs" / "demo" / "publications") / "demo_weekly" / ".ado_breaker.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
                json.dumps(
                        {
                                "state": "OPEN",
                                "failure_count": 2,
                                "last_failure_at": "2026-05-11T10:00:00+00:00",
                                "last_opened_at": "2026-05-11T10:00:00+00:00",
                                "last_success_at": None,
                        },
                        indent=2,
                ) + "\n",
                encoding="utf-8",
        )

        monkeypatch.setattr("src.commands.doctor.EDITIONS_ROOT", editions_root)
        monkeypatch.setattr("src.commands.doctor.PROGRAMS_ROOT", programs_root)
        monkeypatch.setattr("src.commands.doctor.PROGRAMS_ROOT", programs_root)

        result = runner.invoke(app, ["doctor", "--edition", "demo_weekly", "--circuit-breakers"])

        assert result.exit_code == 0
        assert "Circuit Breakers" in result.stdout
        assert "ADO breaker OPEN" in result.stdout
        assert "Overall: HEALTHY" in result.stdout


def test_run_doctor_privacy_ok_when_no_findings(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())

        report = run_doctor(
                edition_name="demo_weekly",
                privacy=True,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Privacy Scan"].status == "ok"
        assert "no obvious credential patterns found" in checks["Privacy Scan"].detail
        assert checks["Privacy Profiles"].status == "ok"
        assert "Privacy Registry" not in checks


def test_run_doctor_privacy_fails_on_credential_patterns_in_journal(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        journal_dir = programs_root / "demo" / "journal"
        journal_dir.mkdir(parents=True, exist_ok=True)
        secret_value = "azdpat_123456789ABCDEF"
        (journal_dir / "2026-W19.jsonl").write_text(
                '{"text":"ADO_PAT=' + secret_value + '"}\n',
                encoding="utf-8",
        )

        report = run_doctor(
                edition_name="demo_weekly",
                privacy=True,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Privacy Scan"].status == "fail"
        assert "programs/demo/journal/2026-W19.jsonl:L1 (ADO_PAT assignment)" in checks["Privacy Scan"].detail
        assert secret_value not in checks["Privacy Scan"].detail


def test_run_doctor_privacy_warns_on_plaintext_sensitive_profiles(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        (programs_root / "demo" / "knowledge" / "people_profiles.yaml").write_text(
                (
                        'schema_version: "1.0"\n'
                        'profiles:\n'
                        '  - alias: demo\n'
                        '    comm_style: concise\n'
                ),
                encoding="utf-8",
        )

        report = run_doctor(
                edition_name="demo_weekly",
                privacy=True,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Privacy Profiles"].status == "warn"
        assert "programs/demo/knowledge/people_profiles.yaml" in checks["Privacy Profiles"].detail
        assert "encryption at rest is still pending" in checks["Privacy Profiles"].detail


def test_run_doctor_privacy_reports_encrypted_sensitive_profiles(tmp_path: Path, monkeypatch) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        profiles_path = programs_root / "demo" / "knowledge" / "people_profiles.yaml"
        profiles_path.write_text(
                (
                        'schema_version: "1.0"\n'
                        'profiles:\n'
                        '  - alias: demo\n'
                        '    comm_style: concise\n'
                ),
                encoding="utf-8",
        )
        fake_keyring = _FakeKeyring()
        monkeypatch.setattr("src.core.profile_encryption._get_keyring_backend", lambda: fake_keyring)
        encrypt_people_profiles_file(profiles_path)

        report = run_doctor(
                edition_name="demo_weekly",
                privacy=True,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Privacy Profiles"].status == "ok"
        assert "verified encrypted at rest" in checks["Privacy Profiles"].detail
        assert "programs/demo/knowledge/people_profiles.yaml" in checks["Privacy Profiles"].detail


def test_run_doctor_privacy_warns_on_registry_pii_and_stale_wal(tmp_path: Path, monkeypatch) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        registry_path = programs_root / "demo" / "channel_registry.sqlite3"
        store = ChannelRegistryStore(registry_path, "demo")
        current_time = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
        store.apply_discovery_result(
                DiscoveryResult(
                        channel="ado",
                        program_id="demo",
                        discovered_refs=(
                                DiscoveredRef(
                                        registration=ChannelRegistration(
                                                channel="ado",
                                                program_id="demo",
                                                provider_instance_id="default",
                                                ref_id="101",
                                                ref_kind="work_item",
                                                status=RegistrationStatus.ACTIVE,
                                                first_discovered_at=current_time,
                                                last_seen_at=current_time,
                                                ref_title="owner@example.com",
                                        ),
                                        bindings=(
                                                RegistrationBinding(
                                                        workstream_id="demo.slice",
                                                        scope_id="scope",
                                                        source_type="wiql_saved_query",
                                                        confidence=1.0,
                                                        confidence_source="wiql_saved_query",
                                                ),
                                        ),
                                ),
                        ),
                        completeness=DiscoveryCompleteness.FULL,
                        scope_statuses={
                                "scope": ScopeStatus(
                                        scope_id="scope",
                                        status=ScopeStatusKind.SUCCESS,
                                        completeness=DiscoveryCompleteness.FULL,
                                        item_count=1,
                                )
                        },
                        scope_state_updates={},
                        errors=(),
                        computed_at=current_time,
                )
        )
        monkeypatch.setattr(
                "src.commands.doctor_checks.privacy_checks.scan_channel_registry_for_privacy_findings",
                lambda path, program_id: ["ado:work_item:101 ref_title looks like it contains email-address PII"],
        )
        wal_path = Path(str(registry_path) + "-wal")
        wal_path.write_text("stale wal", encoding="utf-8")
        stale_epoch = (current_time - timedelta(hours=30)).timestamp()
        os.utime(wal_path, (stale_epoch, stale_epoch))

        report = run_doctor(
                edition_name="demo_weekly",
                privacy=True,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Privacy Registry"].status == "warn"
        assert "ref_title looks like it contains email-address PII" in checks["Privacy Registry"].detail
        assert "stale WAL file programs/demo/channel_registry.sqlite3-wal" in checks["Privacy Registry"].detail


def test_doctor_cli_privacy(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())

        monkeypatch.setattr("src.commands.doctor.EDITIONS_ROOT", editions_root)
        monkeypatch.setattr("src.commands.doctor.PROGRAMS_ROOT", programs_root)

        result = runner.invoke(app, ["doctor", "--edition", "demo_weekly", "--privacy"])

        assert result.exit_code == 0
        assert "Privacy Scan" in result.stdout
        assert "Privacy Profiles" in result.stdout
        assert "Overall: HEALTHY" in result.stdout


def test_run_doctor_kusto_ok_when_queries_probe_successfully(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        program_path = programs_root / "demo" / "program.yaml"
        program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        program_doc["kusto"] = {"enabled": True}
        program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

        probed_queries: list[str] = []
        report = run_doctor(
                edition_name="demo_weekly",
                kusto=True,
                editions_root=editions_root,
                programs_root=programs_root,
                kusto_probe=lambda query: probed_queries.append(query.id),
        )

        checks = {check.label: check for check in report.checks}
        assert probed_queries == ["velocity-p50"]
        assert checks["Kusto Queries"].status == "ok"
        assert "Loaded 1 applicable Kusto query" in checks["Kusto Queries"].detail
        assert checks["Kusto Queries"].metadata is not None
        assert checks["Kusto Queries"].metadata["cluster_targets"] == ["https://adventure.kusto.windows.net/xdataanalytics"]
        assert checks["Kusto Validation"].status == "ok"
        assert checks["Kusto Probe"].status == "ok"
        assert "lightweight take-0 execution" in checks["Kusto Probe"].detail
        assert "https://adventure.kusto.windows.net/xdataanalytics" in checks["Kusto Probe"].detail
        assert checks["Kusto Probe"].metadata is not None
        assert checks["Kusto Probe"].metadata["cluster_targets"] == ["https://adventure.kusto.windows.net/xdataanalytics"]


def test_run_doctor_kusto_warns_on_unvalidated_queries_and_includes_kpis(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        program_path = programs_root / "demo" / "program.yaml"
        program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        program_doc["kusto"] = {"enabled": True}
        program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

        queries_path = programs_root / "demo" / "knowledge" / "golden_queries.yaml"
        queries_doc = yaml.safe_load(queries_path.read_text(encoding="utf-8"))
        queries_doc["queries"][0]["validated"] = False
        # Per the Kusto probe-contract, only refresh_on_gather (non-IcM) queries are
        # validation-gated; mark the golden query so it stays in the validation set.
        queries_doc["queries"][0]["refresh_on_gather"] = True
        queries_path.write_text(yaml.safe_dump(queries_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

        (programs_root / "demo" / "kpis.yaml").write_text(
                (
                        'schema_version: "1.0"\n'
                        'kpis:\n'
                        '  - id: fleet-health\n'
                        '    cluster: https://adventure.kusto.windows.net\n'
                        '    database: xdataanalytics\n'
                        '    kql: Fleet | take 1\n'
                        '    section: Fleet Health\n'
                        '    render_as: metric_highlight\n'
                        '    confidence: medium\n'
                        '    workstream_ids: [ws_demo]\n'
                        '    refresh_on_gather: true\n'
                        '    validated: false\n'
                ),
                encoding="utf-8",
        )

        probed_queries: list[str] = []
        report = run_doctor(
                edition_name="demo_weekly",
                kusto=True,
                editions_root=editions_root,
                programs_root=programs_root,
                kusto_probe=lambda query: probed_queries.append(query.id),
        )

        checks = {check.label: check for check in report.checks}
        assert probed_queries == ["velocity-p50", "fleet-health"]
        assert checks["Kusto Queries"].status == "ok"
        assert "Loaded 2 applicable Kusto queries" in checks["Kusto Queries"].detail
        assert checks["Kusto Validation"].status == "warn"
        assert "validated=false" in checks["Kusto Validation"].detail
        assert "velocity-p50" in checks["Kusto Validation"].detail
        assert "fleet-health" in checks["Kusto Validation"].detail
        assert checks["Kusto Validation"].metadata is not None
        assert checks["Kusto Validation"].metadata["unvalidated_query_ids"] == ["velocity-p50", "fleet-health"]
        assert checks["Kusto Probe"].status == "ok"
        assert checks["Kusto Probe"].metadata is not None
        assert checks["Kusto Probe"].metadata["cluster_targets"] == ["https://adventure.kusto.windows.net/xdataanalytics"]


def test_run_doctor_kusto_warns_when_wired_query_freshness_is_stale(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        program_path = programs_root / "demo" / "program.yaml"
        program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        program_doc["kusto"] = {"enabled": True}
        program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

        queries_path = programs_root / "demo" / "knowledge" / "golden_queries.yaml"
        queries_doc = yaml.safe_load(queries_path.read_text(encoding="utf-8"))
        queries_doc["queries"][0]["refresh_on_gather"] = True
        queries_doc["queries"][0]["validated"] = True
        queries_path.write_text(yaml.safe_dump(queries_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

        (programs_root / "demo" / "gather_state.json").write_text(
                json.dumps(
                        {
                                "schema_version": "2.0",
                                "queries": {
                                        "velocity-p50": {
                                                "last_succeeded_at": "2026-05-01T08:00:00Z",
                                                "last_cycle_succeeded": True,
                                                "row_count": 1,
                                        }
                                },
                        }
                ),
                encoding="utf-8",
        )

        report = run_doctor(
                edition_name="demo_weekly",
                kusto=True,
                editions_root=editions_root,
                programs_root=programs_root,
                kusto_probe=lambda query: None,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Kusto Freshness"].status == "warn"
        assert "stale >7d" in checks["Kusto Freshness"].detail
        assert "velocity-p50" in checks["Kusto Freshness"].detail
        assert checks["Kusto Freshness"].metadata is not None
        assert checks["Kusto Freshness"].metadata["stale_query_ids"] == ["velocity-p50"]


def test_run_doctor_kusto_surfaces_icm_query_readiness(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        program_path = programs_root / "demo" / "program.yaml"
        program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        program_doc["kusto"] = {"enabled": True}
        program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

        queries_path = programs_root / "demo" / "knowledge" / "golden_queries.yaml"
        queries_doc = yaml.safe_load(queries_path.read_text(encoding="utf-8"))
        queries_doc["queries"].append(
                {
                        "id": "icm-active",
                        "cluster": "https://icmcluster.kusto.windows.net",
                        "database": "IcMDataWarehouse",
                        "kql": "Incidents | take 1",
                        "section": "Incidents",
                        "render_as": "table",
                        "confidence": "high",
                        "program_ids": ["demo"],
                        "validated": False,
                }
        )
        queries_path.write_text(yaml.safe_dump(queries_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

        probed_queries: list[str] = []
        report = run_doctor(
                edition_name="demo_weekly",
                kusto=True,
                editions_root=editions_root,
                programs_root=programs_root,
                kusto_probe=lambda query: probed_queries.append(query.id),
        )

        checks = {check.label: check for check in report.checks}
        assert probed_queries == ["velocity-p50", "icm-active"]
        assert checks["IcM via Kusto"].status == "warn"
        assert "icm-active" in checks["IcM via Kusto"].detail
        assert checks["IcM via Kusto"].metadata is not None
        assert checks["IcM via Kusto"].metadata["icm_query_ids"] == ["icm-active"]
        assert checks["IcM via Kusto"].metadata["cluster_targets"] == ["https://icmcluster.kusto.windows.net/IcMDataWarehouse"]
        assert checks["Kusto Probe"].metadata is not None
        assert checks["Kusto Probe"].metadata["cluster_targets"] == [
                "https://adventure.kusto.windows.net/xdataanalytics",
                "https://icmcluster.kusto.windows.net/IcMDataWarehouse",
        ]


def test_run_doctor_kusto_renders_query_templates_before_probe(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        program_path = programs_root / "demo" / "program.yaml"
        program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        program_doc["ado"]["area_paths"] = ["One\\Demo"]
        program_doc["kusto"] = {"enabled": True}
        program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

        queries_path = programs_root / "demo" / "knowledge" / "golden_queries.yaml"
        queries_doc = yaml.safe_load(queries_path.read_text(encoding="utf-8"))
        queries_doc["queries"][0]["kql"] = (
                'Demo | where Program == "{program_id}" | where AreaPath == "{area_path}" | '
                'where Timestamp > ago({date_range}) | summarize Count=count()'
        )
        queries_path.write_text(yaml.safe_dump(queries_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

        probed_kql: list[str] = []

        report = run_doctor(
                edition_name="demo_weekly",
                kusto=True,
                editions_root=editions_root,
                programs_root=programs_root,
                kusto_probe=lambda query: probed_kql.append(query.kql),
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Kusto Queries"].status == "ok"
        assert checks["Kusto Probe"].status == "ok"
        assert probed_kql == [
                'Demo | where Program == "demo" | where AreaPath == "One\\Demo" | where Timestamp > ago(14d) | summarize Count=count()'
        ]


def test_run_doctor_kusto_fails_on_missing_required_fields(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        program_path = programs_root / "demo" / "program.yaml"
        program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        program_doc["kusto"] = {"enabled": True}
        program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

        queries_path = programs_root / "demo" / "knowledge" / "golden_queries.yaml"
        queries_doc = yaml.safe_load(queries_path.read_text(encoding="utf-8"))
        queries_doc["queries"][0]["cluster"] = ""
        queries_path.write_text(yaml.safe_dump(queries_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

        def _unexpected_probe(query: object) -> None:
                raise AssertionError(f"Probe should not run for invalid query definitions: {query}")

        report = run_doctor(
                edition_name="demo_weekly",
                kusto=True,
                editions_root=editions_root,
                programs_root=programs_root,
                kusto_probe=_unexpected_probe,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Kusto Queries"].status == "fail"
        assert "Kusto query 'velocity-p50' is missing cluster." in checks["Kusto Queries"].detail


def test_run_doctor_kusto_fails_on_probe_error(tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        program_path = programs_root / "demo" / "program.yaml"
        program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        program_doc["kusto"] = {"enabled": True}
        program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

        def _failing_probe(query: object) -> None:
                raise QueryError("invalid syntax near summarize")

        report = run_doctor(
                edition_name="demo_weekly",
                kusto=True,
                editions_root=editions_root,
                programs_root=programs_root,
                kusto_probe=_failing_probe,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["Kusto Probe"].status == "fail"
        assert "velocity-p50" in checks["Kusto Probe"].detail
        assert "invalid syntax near summarize" in checks["Kusto Probe"].detail


def test_doctor_cli_kusto(monkeypatch, tmp_path: Path) -> None:
        editions_root, programs_root = _seed_kb_layout(tmp_path, query_ids=())
        program_path = programs_root / "demo" / "program.yaml"
        program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        program_doc["kusto"] = {"enabled": True}
        program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

        monkeypatch.setattr("src.commands.doctor.EDITIONS_ROOT", editions_root)
        monkeypatch.setattr("src.commands.doctor.REPORTS_ROOT", tmp_path / "reports")
        monkeypatch.setattr("src.commands.doctor._live_kusto_probe", lambda: (lambda query: None))

        result = runner.invoke(app, ["doctor", "--edition", "demo_weekly", "--kusto"])

        assert result.exit_code == 0
        assert "Kusto Queries" in result.stdout
        assert "Kusto Validation" in result.stdout
        assert "Kusto Probe" in result.stdout
        assert "Overall: HEALTHY" in result.stdout


def test_run_doctor_ids_validates_canonical_surfaces(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        editions_root = reports_root.parent / "editions"
        programs_root = reports_root.parent / "programs"

        report = run_doctor(
                edition_name=EDITION_NAME,
                reports_root=reports_root,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["IDs"].status == "ok"
        assert "scorecards, chapter contract, slice contracts, registry, and workstreams align" in checks["IDs"].detail


def test_run_doctor_ids_fails_on_unknown_scorecard_workstream(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        editions_root = reports_root.parent / "editions"
        programs_root = reports_root.parent / "programs"
        scorecards_path = programs_root / "acme" / "scorecards.yaml"
        scorecards_doc = yaml.safe_load(scorecards_path.read_text(encoding="utf-8"))
        scorecards_doc["scorecards"][0]["dimensions"][0]["workstream_id"] = "unknown-workstream"
        scorecards_path.write_text(yaml.safe_dump(scorecards_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

        report = run_doctor(
                edition_name=EDITION_NAME,
                reports_root=reports_root,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["IDs"].status == "fail"
        assert "unknown workstream_id 'unknown-workstream'" in checks["IDs"].detail


def test_run_doctor_ids_warns_on_cross_edition_composition_drift(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(
                repo_root,
                tmp_path,
                edition_names=("acme_weekly", "nova_quarterly"),
        )
        editions_root = reports_root.parent / "editions"
        programs_root = reports_root.parent / "programs"
        chapter_contract_path = programs_root / "acme" / "chapter_contract.yaml"
        chapter_contract_doc = yaml.safe_load(chapter_contract_path.read_text(encoding="utf-8"))
        chapter_contract_doc["chapters"][0]["include_in"] = ["detailed", "focused"]
        chapter_contract_path.write_text(
                yaml.safe_dump(chapter_contract_doc, sort_keys=False, allow_unicode=False),
                encoding="utf-8",
        )

        report = run_doctor(
                edition_name=EDITION_NAME,
                ids=True,
                reports_root=reports_root,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["IDs"].status == "ok"
        assert checks["Composition"].status == "warn"
        assert "nova_quarterly (lookback)" in checks["Composition"].detail
        assert "missing" in checks["Composition"].detail


def test_run_doctor_ids_warns_on_raw_anchor_gap(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        editions_root = reports_root.parent / "editions"
        programs_root = reports_root.parent / "programs"
        slice_contracts_path = programs_root / "acme" / "slice_contracts.yaml"
        slice_contracts_doc = yaml.safe_load(slice_contracts_path.read_text(encoding="utf-8"))
        slice_contracts_doc["slices"][0]["source_contract"]["ado"]["saved_queries"] = []
        slice_contracts_doc["slices"][0]["source_contract"]["ado"]["explicit_work_item_ids"] = []
        slice_contracts_path.write_text(
                yaml.safe_dump(slice_contracts_doc, sort_keys=False, allow_unicode=False),
                encoding="utf-8",
        )

        report = run_doctor(
                edition_name=EDITION_NAME,
                ids=True,
                reports_root=reports_root,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["IDs"].status == "ok"
        assert checks["Anchor acme.deployment_velocity"].status == "warn"
        assert "raw anchor gap" in checks["Anchor acme.deployment_velocity"].detail


def test_run_doctor_ids_accepts_filter_only_waiver_with_expiry(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(repo_root, tmp_path)
        editions_root = reports_root.parent / "editions"
        programs_root = reports_root.parent / "programs"
        slice_contracts_path = programs_root / "acme" / "slice_contracts.yaml"
        slice_contracts_doc = yaml.safe_load(slice_contracts_path.read_text(encoding="utf-8"))
        slice_contracts_doc["slices"][0]["source_contract"]["ado"]["saved_queries"] = []
        slice_contracts_doc["slices"][0]["source_contract"]["ado"]["explicit_work_item_ids"] = []
        slice_contracts_doc["slices"][0]["source_contract"]["ado"]["intentional_filter_only"] = True
        slice_contracts_doc["slices"][0]["source_contract"]["ado"]["intentional_filter_only_expires_on"] = "2026-12-31"
        slice_contracts_path.write_text(
                yaml.safe_dump(slice_contracts_doc, sort_keys=False, allow_unicode=False),
                encoding="utf-8",
        )

        report = run_doctor(
                edition_name=EDITION_NAME,
                ids=True,
                reports_root=reports_root,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["IDs"].status == "ok"
        assert "Anchor acme.deployment_velocity" not in checks


def test_run_doctor_ids_allows_narrative_editions_without_chapter_contract(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(
                repo_root,
                tmp_path,
                edition_names=("fabrikam_weekly",),
                program_names=("fabrikam",),
        )
        editions_root = reports_root.parent / "editions"
        programs_root = reports_root.parent / "programs"

        report = run_doctor(
                edition_name="fabrikam_weekly",
                ids=True,
                reports_root=reports_root,
                editions_root=editions_root,
                programs_root=programs_root,
        )

        checks = {check.label: check for check in report.checks}
        assert checks["IDs"].status == "ok"
        assert "scorecards, chapter contract, slice contracts, registry, and workstreams align" in checks["IDs"].detail


def test_run_doctor_channels_require_structured_decision_sources_for_deck(repo_root: Path, tmp_path: Path) -> None:
        reports_root = stage_v2_report_workspace(
                repo_root,
                tmp_path,
                edition_names=("nova_lt_deck",),
                program_names=("acme",),
        )
        editions_root = reports_root.parent / "editions"
        archive_root = tmp_path / "archive"
        programs_root = reports_root.parent / "programs"
        reset_overrides_to_seed_state(reports_root)
        slice_contracts_path = programs_root / "acme" / "slice_contracts.yaml"
        slice_contracts_doc = yaml.safe_load(slice_contracts_path.read_text(encoding="utf-8"))
        slice_contracts_doc["slices"][0]["source_contract"].pop("decision_sources", None)
        slice_contracts_doc["slices"][0]["decision_sources"] = []
        slice_contracts_path.write_text(
                yaml.safe_dump(slice_contracts_doc, sort_keys=False, allow_unicode=False),
                encoding="utf-8",
        )

        write_gather_state(
                "acme",
                gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                scanned_items=12,
                discovered_signals=11,
                new_signals=5,
                pending_review=1,
                trajectory_updates=0,
                auto_reviews_written=0,
                ado_calls=0,
                archived_journal_files=0,
                background_proposals=0,
                query_states={
                        "velocity-p50": {
                                "last_cycle_succeeded": True,
                                "row_count": 2,
                                "data_age_hours": 4.0,
                        }
                },
                channels={
                        "ado": {
                                "active": True,
                                "signal_count": 4,
                        },
                        "workiq": {
                                "active": True,
                                "signal_count": 8,
                                "expected_min": 8,
                                "meets_expected_min": True,
                        },
                },
                programs_root=programs_root,
        )

        with pytest.raises(ConfigError, match="missing decision_sources entries for fallback_sources: lt_deck, contoso_daily"):
                run_doctor(
                        edition_name="nova_lt_deck",
                        channels=True,
                        reports_root=reports_root,
                        archive_root=archive_root,
                        editions_root=editions_root,
                        programs_root=programs_root,
                )


def _seed_kb_layout(tmp_path: Path, *, query_ids: tuple[str, ...]) -> tuple[Path, Path]:
        editions_root = tmp_path / "editions"
        programs_root = tmp_path / "programs"
        program_dir = programs_root / "demo"
        knowledge_dir = program_dir / "knowledge"
        editions_root.mkdir(parents=True)
        knowledge_dir.mkdir(parents=True)

        (editions_root / "demo_weekly.yaml").write_text(
                (
                        'schema_version: "2.0"\n'
                        'id: demo_weekly\n'
                        'program_id: demo\n'
                        'name: "Demo Issue {issue_number}"\n'
                        'type: detailed\n'
                        'altitude: helicopter\n'
                        'cadence: weekly\n'
                        'author:\n'
                        '  display_name: Demo Author\n'
                        '  email: demo@example.com\n'
                        'distribution:\n'
                        '  to: [demo@example.com]\n'
                ),
                encoding="utf-8",
        )
        (program_dir / "program.yaml").write_text(
                (
                        'schema_version: "2.0"\n'
                        'id: demo\n'
                        'name: Demo Program\n'
                        'ado:\n'
                        '  organization: your-org\n'
                        '  project: One\n'
                        '  area_paths: []\n'
                        '  work_item_types: [Feature]\n'
                        '  excluded_states: [Removed]\n'
                        '  date_window_days: 14\n'
                        '  api_timeout_seconds: 30\n'
                ),
                encoding="utf-8",
        )
        saved_query_block = ""
        if query_ids:
                joined_ids = ", ".join(f'"{query_id}"' for query_id in query_ids)
                saved_query_block = f"    ado_saved_query_ids: [{joined_ids}]\n"
        (program_dir / "workstreams.yaml").write_text(
                (
                        'schema_version: "2.0"\n'
                        'workstreams:\n'
                        '  - id: ws_demo\n'
                        '    name: Demo WS\n'
                        "    area_paths: ['One\\\\Demo']\n"
                        f"{saved_query_block}"
                ),
                encoding="utf-8",
        )
        (program_dir / "scorecards.yaml").write_text(
                (
                        'schema_version: "2.0"\n'
                        'scorecards:\n'
                        '  - name: Demo Scorecard\n'
                        '    dimensions:\n'
                        '      - name: Demo Dimension\n'
                        '        workstream_id: ws_demo\n'
                ),
                encoding="utf-8",
        )
        (knowledge_dir / "people_directory.yaml").write_text(
                (
                        'schema_version: "1.0"\n'
                        'people:\n'
                        '  - alias: demo\n'
                        '    email: demo@example.com\n'
                        '    display_name: Demo Author\n'
                ),
                encoding="utf-8",
        )
        (knowledge_dir / "people_profiles.yaml").write_text("schema_version: \"1.0\"\nprofiles: []\n", encoding="utf-8")
        (knowledge_dir / "teams.yaml").write_text("schema_version: \"1.0\"\nteams: []\n", encoding="utf-8")
        (knowledge_dir / "products.yaml").write_text("schema_version: \"1.0\"\nproducts: []\n", encoding="utf-8")
        (knowledge_dir / "golden_queries.yaml").write_text(
                (
                        'schema_version: "1.0"\n'
                        'queries:\n'
                        '  - id: velocity-p50\n'
                        '    cluster: https://adventure.kusto.windows.net\n'
                        '    database: xdataanalytics\n'
                        '    kql: Demo | take 1\n'
                        '    section: Demo Telemetry\n'
                        '    render_as: table\n'
                        '    confidence: high\n'
                        '    validated: true\n'
                        '    program_ids: [demo]\n'
                ),
                encoding="utf-8",
        )
        return editions_root, programs_root


def _enable_demo_readiness_gate(programs_root: Path, *, snapshot_max_age_days: int) -> None:
        program_path = programs_root / "demo" / "program.yaml"
        program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        program_doc["schema_version"] = "3.0"
        program_doc["readiness"] = {
                "gate": True,
                "snapshot_max_age_days": snapshot_max_age_days,
        }
        program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False), encoding="utf-8")


def _write_demo_readiness_config(programs_root: Path, *, snapshot_max_age_days: int) -> None:
        (programs_root / "demo" / "readiness.yaml").write_text(
                yaml.safe_dump(
                        {
                                "schema_version": "1.0",
                                "snapshot_max_age_days": snapshot_max_age_days,
                                "dimensions": {
                                        "rollback_plan": {
                                                "source": {
                                                        "type": "manual_attestation",
                                                        "attested_at": datetime.now(timezone.utc).date().isoformat(),
                                                        "attested_by": "demo",
                                                },
                                                "pass_condition": {
                                                        "kind": "attested_within_days",
                                                        "days": 30,
                                                },
                                        },
                                },
                        },
                        sort_keys=False,
                ),
                encoding="utf-8",
        )


def _write_demo_milestones(
        programs_root: Path,
        *,
        owner_alias: str = "demo",
        linked_workstream_ids: tuple[str, ...] = ("ws_demo",),
) -> None:
        workstream_ids = ", ".join(f'"{workstream_id}"' for workstream_id in linked_workstream_ids)
        (programs_root / "demo" / "milestones.yaml").write_text(
                (
                        'schema_version: "1.0"\n'
                        'milestones:\n'
                        '  - id: m1\n'
                        '    program_id: demo\n'
                        '    name: Demo Milestone\n'
                        '    target_date: 2026-05-30\n'
                        f'    owner_alias: {owner_alias}\n'
                        '    status: on_track\n'
                        '    exit_criteria:\n'
                        '      - Demo gate met\n'
                        f'    linked_workstream_ids: [{workstream_ids}]\n'
                        '    linked_work_item_ids: [1001]\n'
                ),
                encoding="utf-8",
        )


def _write_confirmed_archive_index(archive_root: Path, *, edition_name: str, generated_at: datetime) -> None:
        edition_root = get_archive_root(edition_name, archive_root=archive_root)
        edition_root.mkdir(parents=True, exist_ok=True)
        payload = {
                "schema_version": "1.0",
                "edition": edition_name,
                "issues": [
                        {
                                "issue_number": 1,
                                "generated_at": generated_at.isoformat(),
                                "kind": "confirmed",
                        }
                ],
        }
        (edition_root / "index.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_confirmed_archive_entries(archive_root: Path, *, edition_name: str, generated_ats: tuple[datetime, ...]) -> None:
        edition_root = get_archive_root(edition_name, archive_root=archive_root)
        edition_root.mkdir(parents=True, exist_ok=True)
        payload = {
                "schema_version": "1.0",
                "edition": edition_name,
                "issues": [
                        {
                                "issue_number": issue_number,
                                "generated_at": generated_at.isoformat(),
                                "kind": "confirmed",
                        }
                        for issue_number, generated_at in enumerate(generated_ats, start=1)
                ],
        }
        (edition_root / "index.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _set_program_storage_backend(programs_root: Path, *, program_id: str, storage_backend: str) -> None:
        program_path = programs_root / program_id / "program.yaml"
        program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        assert isinstance(program_document, dict)
        program_document["storage_backend"] = storage_backend
        program_path.write_text(yaml.safe_dump(program_document, sort_keys=False, allow_unicode=False), encoding="utf-8")


def _write_demo_snapshot(archive_root: Path, *, edition_name: str, generated_at: datetime) -> None:
        edition_root = get_archive_root(edition_name, archive_root=archive_root)
        snapshot_path = edition_root / "snapshots" / "issue_001.snapshot.json"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(
                json.dumps(
                        {
                                "schema_version": "1.0",
                                "issue_number": 1,
                                "generated_at": generated_at.isoformat(),
                                "ado_data_as_of": generated_at.isoformat(),
                                "edition_type": "detailed",
                                "items": [
                                        {
                                                "id": 1001,
                                                "type": "Feature",
                                                "title": "Demo milestone delivery",
                                                "state": "Active",
                                                "assigned_to": "demo@example.com",
                                                "area_path": "One\\Demo",
                                                "target_date": "2026-05-30",
                                                "risk_level": "high",
                                                "tags": [],
                                        }
                                ],
                                "scorecards": [],
                        },
                        indent=2,
                ),
                encoding="utf-8",
        )


def _write_demo_risks(
        programs_root: Path,
        *,
        owner_alias: str = "demo",
        linked_workstream_ids: tuple[str, ...] = ("ws_demo",),
        linked_milestone_ids: tuple[str, ...] = (),
        identified_date: str = "2026-05-01",
        last_reviewed_date: str | None = "2026-05-05",
) -> None:
        workstream_ids = [workstream_id for workstream_id in linked_workstream_ids]
        milestone_ids = [milestone_id for milestone_id in linked_milestone_ids]
        risk_entry = {
                "id": "risk-demo-1",
                "program_id": "demo",
                "title": "Demo dependency risk",
                "description": "The demo workstream depends on an external handoff.",
                "probability": "likely",
                "impact": "high",
                "category": "dependency",
                "owner_alias": owner_alias,
                "mitigation_plan": "Review the handoff each week.",
                "mitigation_due_date": "2026-05-20",
                "linked_workstream_ids": workstream_ids,
                "linked_work_item_ids": [1001],
                "linked_milestone_ids": milestone_ids,
                "linked_claim_ids": [],
                "linked_action_ids": [],
                "status": "open",
                "identified_date": identified_date,
                "identified_in_vertex_issue": 7,
                "last_reviewed_date": last_reviewed_date,
                "entity_refs": ["WI:1001"],
        }
        (programs_root / "demo" / "risk_register.yaml").write_text(
                yaml.safe_dump({"schema_version": "1.0", "risks": [risk_entry]}, sort_keys=False),
                encoding="utf-8",
        )


def _write_demo_actions(
        programs_root: Path,
        *,
        owner_alias: str = "demo",
        workstream_id: str | None = "ws_demo",
        due_date: str | None = None,
) -> None:
        # Default to a future date relative to today so the happy-path doctor
        # checks (status == "ok", "due dates valid") do not rot as the calendar
        # advances. Tests that exercise the overdue branch pass an explicit
        # past date (e.g. due_date="2026-01-01").
        resolved_due_date = due_date or (date.today() + timedelta(days=7)).isoformat()
        append_action(
                "demo",
                ActionItem(
                        id="action-demo-1",
                        program_id="demo",
                        text="Follow up with the partner team.",
                        owner_alias=owner_alias,
                        due_date=datetime.fromisoformat(f"{resolved_due_date}T00:00:00+00:00").date(),
                        status=ActionStatus.OPEN,
                        source_signal_id="signal-demo-1",
                        source_type=ActionSourceType.SIGNAL,
                        linked_work_item_ids=(1001,),
                        linked_claim_id=None,
                        linked_risk_id=None,
                        workstream_id=workstream_id,
                        created_at=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
                        resolved_at=None,
                        resolution_note=None,
                ),
                programs_root=programs_root,
        )


def _write_demo_escalation_rules(programs_root: Path) -> None:
        (programs_root / "demo" / "escalation_rules.yaml").write_text(
                yaml.safe_dump(
                        {
                                "schema_version": "1.0",
                                "rules": [
                                        {
                                                "name": "consecutive_high",
                                                "conditions": [
                                                        {"field": "consecutive_high", "op": ">=", "value": 3},
                                                ],
                                                "action": "draft_escalation",
                                                "cooldown_hours": 168,
                                        },
                                        {
                                                "name": "milestone_at_risk",
                                                "conditions": [
                                                        {"field": "milestone_status", "op": "==", "value": "at_risk"},
                                                        {"field": "milestone_days_to_target", "op": "<=", "value": 14},
                                                ],
                                                "action": "draft_escalation",
                                                "cooldown_hours": 168,
                                        },
                                        {
                                                "name": "unresolved_ask",
                                                "conditions": [
                                                        {"field": "decision_ask_age_days", "op": ">=", "value": 21},
                                                        {"field": "decision_ask_status", "op": "==", "value": "open"},
                                                ],
                                                "action": "draft_escalation",
                                                "cooldown_hours": 336,
                                        },
                                ],
                        },
                        sort_keys=False,
                        allow_unicode=False,
                ),
                encoding="utf-8",
        )


def _write_demo_decisions(
        programs_root: Path,
        *,
        decided_by: str = "demo",
        workstream_id: str | None = "ws_demo",
        status: str = "decided",
        decision_date: str = "2026-05-20",
        review_by: str | None = None,
) -> None:
        decision_entry = {
                "id": "decision-demo-1",
                "program_id": "demo",
                "title": "Choose rollout path",
                "context": "Two rollout options remain.",
                "decision": "Proceed with the guarded rollout.",
                "rationale": "It minimizes blast radius.",
                "alternatives_considered": ["pause", "full rollout"],
                "decided_by": decided_by,
                "decision_date": decision_date,
                "status": status,
                "superseded_by": None,
                "linked_claim_id": None,
                "linked_risk_id": None,
                "linked_action_ids": [],
                "workstream_id": workstream_id,
                "entity_refs": ["WI:1001"],
                "review_by": review_by,
        }
        (programs_root / "demo" / "decisions.yaml").write_text(
                yaml.safe_dump({"schema_version": "1.0", "decisions": [decision_entry]}, sort_keys=False),
                encoding="utf-8",
        )


def _write_demo_assumptions(
        programs_root: Path,
        *,
        owner_alias: str | None = "demo",
        linked_milestone_id: str | None = None,
        linked_risk_id: str | None = None,
        validation_due: str | None = None,
) -> None:
        # Default to a future date relative to today so the happy-path doctor
        # checks do not rot as the calendar advances. Tests that exercise the
        # overdue branch pass an explicit past date (e.g. validation_due="2026-04-01").
        resolved_validation_due = validation_due or (date.today() + timedelta(days=7)).isoformat()
        assumption_entry = {
                "id": "assumption-demo-1",
                "program_id": "demo",
                "text": "Kusto team ships schema by Q3.",
                "validation_method": "Review the schema rollout notes.",
                "validation_due": resolved_validation_due,
                "status": "unvalidated",
                "linked_risk_id": linked_risk_id,
                "linked_milestone_id": linked_milestone_id,
                "owner_alias": owner_alias,
                "identified_date": "2026-05-01",
                "entity_refs": ["WI:1001"],
        }
        (programs_root / "demo" / "assumptions.yaml").write_text(
                yaml.safe_dump({"schema_version": "1.0", "assumptions": [assumption_entry]}, sort_keys=False),
                encoding="utf-8",
        )


def _write_demo_legacy_dependencies(programs_root: Path) -> None:
        program_path = programs_root / "demo" / "program.yaml"
        document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        assert isinstance(document, dict)
        document["key_dependencies"] = [
                {
                        "from_item": "ws_demo",
                        "to_item": "Demo milestone",
                        "impact": "Execution stalls.",
                }
        ]
        program_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _write_demo_dependencies(programs_root: Path, *, dependencies: tuple[dict[str, str], ...] | None = None) -> None:
        dependency_rows = dependencies or (
                {
                        "id": "dep-demo-local",
                        "from_milestone_id": "m1",
                        "to_workstream_id": "ws_demo",
                        "resolution_path": "intra_storage",
                        "dependency_type": "blocks",
                        "risk_if_broken": "Demo execution stalls.",
                        "status": "active",
                },
                {
                        "id": "dep-demo-cross-program",
                        "from_workstream_id": "ws_demo",
                        "to_workstream_id": "demo_partner:partner_ws",
                        "resolution_path": "cross_org_compute_pf",
                        "dependency_type": "informs",
                        "risk_if_broken": "Partner sequencing stays provisional.",
                        "status": "active",
                },
        )
        (programs_root / "demo" / "dependencies.yaml").write_text(
                yaml.safe_dump(
                        {
                                "schema_version": "1.0",
                                "dependencies": list(dependency_rows),
                        },
                        sort_keys=False,
                ),
                encoding="utf-8",
        )


def _seed_dependency_partner_program(programs_root: Path) -> None:
        partner_dir = programs_root / "demo_partner"
        knowledge_dir = partner_dir / "knowledge"
        knowledge_dir.mkdir(parents=True, exist_ok=True)
        (partner_dir / "program.yaml").write_text(
                (
                        'schema_version: "2.0"\n'
                        'id: demo_partner\n'
                        'name: Demo Partner\n'
                        'ado:\n'
                        '  organization: your-org\n'
                        '  project: One\n'
                        '  area_paths: []\n'
                        '  work_item_types: [Feature]\n'
                        '  excluded_states: [Removed]\n'
                        '  date_window_days: 14\n'
                        '  api_timeout_seconds: 30\n'
                ),
                encoding="utf-8",
        )
        (partner_dir / "workstreams.yaml").write_text(
                (
                        'schema_version: "2.0"\n'
                        'workstreams:\n'
                        '  - id: partner_ws\n'
                        '    name: Partner WS\n'
                        "    area_paths: ['One\\Partner']\n"
                ),
                encoding="utf-8",
        )
