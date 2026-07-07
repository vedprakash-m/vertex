from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from src.core.discovery_intent import (
    DiscoveryAttempt,
    DiscoveryAttemptOutcome,
    SourceCandidate,
    SourceCandidateStatus,
    SourceIntentStatus,
    SourceRefKind,
    build_discovery_attempt_id,
    build_source_candidate_id,
)
from src.core.m365_registry_store import M365RegistryArtifact
from src.core.models_v2 import EmailThreadSource, TeamsChat, TeamsMeetingSeries, Workstream, WorkstreamSignalSources
from src.core.source_candidate_store import SourceCandidateStore, candidate_evidence_json


def test_bootstrap_intents_creates_workstream_and_legacy_intents_without_duplicates(tmp_path) -> None:
    store = SourceCandidateStore(tmp_path / "channel_registry.sqlite3", "demo")
    as_of = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    workstream = Workstream(
        id="demo.acme",
        name="Acme",
        signal_sources=WorkstreamSignalSources(
            teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Review"),),
            teams_chats=(TeamsChat(display_name="Acme Core Chat"),),
            email_subject_filters=("Ramp weekly digest",),
            email_threads=(EmailThreadSource(display_name="Ramp thread", thread_id="thread-1"),),
        ),
    )
    artifact = M365RegistryArtifact(
        artifact_id="meet:acme-weekly-review",
        artifact_type="meeting_series",
        inferred_workstream="demo.acme",
        confidence=0.92,
        confidence_source="pm",
        pm_confirmed=True,
        promoted_to_workstreams_yaml=True,
        first_seen=date(2026, 5, 1),
        last_seen=date(2026, 5, 20),
        display_name="Acme Weekly Review",
        series_id=None,
    )

    created = store.bootstrap_intents(
        workstreams=(workstream,),
        registry_artifacts=(artifact,),
        as_of=as_of,
    )

    intents = store.list_intents(workstream_id="demo.acme")
    assert created == 5
    assert len(intents) == 4
    assert {(intent.ref_kind.value, intent.display_name) for intent in intents} == {
        ("meeting_series", "Acme Weekly Review"),
        ("teams_chat", "Acme Core Chat"),
        ("email_thread", "Ramp weekly digest"),
        ("email_thread", "Ramp thread"),
    }


def test_derive_intent_state_reflects_candidates_and_attempts(tmp_path) -> None:
    store = SourceCandidateStore(tmp_path / "channel_registry.sqlite3", "demo")
    as_of = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    store.bootstrap_intents(
        workstreams=(
            Workstream(
                id="demo.acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Review"),),
                ),
            ),
        ),
        registry_artifacts=(),
        as_of=as_of,
    )
    intent = store.list_intents(workstream_id="demo.acme")[0]
    pending_candidate = SourceCandidate(
        candidate_id=build_source_candidate_id(
            program_id="demo",
            channel="teams",
            provider_instance_id="default",
            ref_kind=SourceRefKind.MEETING_SERIES,
            ref_id="series-1",
        ),
        program_id="demo",
        channel="teams",
        provider_instance_id="default",
        ref_id="series-1",
        ref_kind=SourceRefKind.MEETING_SERIES,
        display_name="Acme Weekly Review",
        confidence=0.91,
        source_provider="graph_calendar",
        status=SourceCandidateStatus.PENDING,
        evidence_json=candidate_evidence_json({"matched_terms": ["Acme Weekly Review"]}),
        first_discovered_at=as_of,
        last_seen_at=as_of,
    )
    store.upsert_candidate(pending_candidate, pii_prescrubbed=True)
    store.link_candidate_to_intent(pending_candidate.candidate_id, intent.intent_id, 0.91)

    assert store.derive_intent_state(intent.intent_id, as_of=as_of) == SourceIntentStatus.CANDIDATE_FOUND.value

    attempt_at = as_of + timedelta(minutes=5)
    store.record_attempt(
        DiscoveryAttempt(
            attempt_id=build_discovery_attempt_id(
                program_id="demo",
                intent_id=intent.intent_id,
                source_provider="graph_calendar",
                query_hash="query",
                attempted_at=attempt_at,
            ),
            program_id="demo",
            intent_id=intent.intent_id,
            workstream_id=intent.workstream_id,
            channel="teams",
            provider_instance_id="default",
            ref_kind=SourceRefKind.MEETING_SERIES,
            source_provider="graph_calendar",
            query_hash="query",
            config_hash="config",
            autonomous_run_id=None,
            outcome=DiscoveryAttemptOutcome.NO_CANDIDATES,
            reason="no_match",
            result_count=0,
            duration_ms=10,
            attempted_at=attempt_at,
            expires_at=attempt_at + timedelta(hours=4),
        )
    )

    store.update_candidate_status(
        pending_candidate.candidate_id,
        status=SourceCandidateStatus.ACCEPTED,
        decided_by="pm",
        decision_reason="manual accept",
        expected_decision_version=0,
    )

    assert store.derive_intent_state(intent.intent_id, as_of=attempt_at) == SourceIntentStatus.RESOLVED.value


def test_derive_intent_state_reports_no_candidates_after_completed_zero_yield_attempt(tmp_path) -> None:
    store = SourceCandidateStore(tmp_path / "channel_registry.sqlite3", "demo")
    as_of = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    store.bootstrap_intents(
        workstreams=(
            Workstream(
                id="demo.acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Review"),),
                ),
            ),
        ),
        registry_artifacts=(),
        as_of=as_of,
    )
    intent = store.list_intents(workstream_id="demo.acme")[0]
    attempt_at = as_of + timedelta(minutes=5)
    store.record_attempt(
        DiscoveryAttempt(
            attempt_id=build_discovery_attempt_id(
                program_id="demo",
                intent_id=intent.intent_id,
                source_provider="graph_calendar",
                query_hash="query",
                attempted_at=attempt_at,
            ),
            program_id="demo",
            intent_id=intent.intent_id,
            workstream_id=intent.workstream_id,
            channel="teams",
            provider_instance_id="default",
            ref_kind=SourceRefKind.MEETING_SERIES,
            source_provider="graph_calendar",
            query_hash="query",
            config_hash="config",
            autonomous_run_id=None,
            outcome=DiscoveryAttemptOutcome.NO_CANDIDATES,
            reason="no_match",
            result_count=0,
            duration_ms=10,
            attempted_at=attempt_at,
            expires_at=attempt_at + timedelta(hours=4),
        )
    )

    assert store.derive_intent_state(intent.intent_id, as_of=attempt_at) == SourceIntentStatus.NO_CANDIDATES.value


def test_upsert_candidate_preserves_pm_decision_lifecycle(tmp_path) -> None:
    store = SourceCandidateStore(tmp_path / "channel_registry.sqlite3", "demo")
    as_of = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    candidate = SourceCandidate(
        candidate_id=build_source_candidate_id(
            program_id="demo",
            channel="teams",
            provider_instance_id="default",
            ref_kind=SourceRefKind.MEETING_SERIES,
            ref_id="series-1",
        ),
        program_id="demo",
        channel="teams",
        provider_instance_id="default",
        ref_id="series-1",
        ref_kind=SourceRefKind.MEETING_SERIES,
        display_name="Acme Weekly Review",
        confidence=0.91,
        source_provider="graph_calendar",
        status=SourceCandidateStatus.PENDING,
        evidence_json=candidate_evidence_json({"matched_terms": ["Acme Weekly Review"]}),
        first_discovered_at=as_of,
        last_seen_at=as_of,
    )
    store.upsert_candidate(candidate, pii_prescrubbed=True)
    rejected = store.update_candidate_status(
        candidate.candidate_id,
        status=SourceCandidateStatus.REJECTED,
        decided_by="pm",
        decision_reason="wrong meeting",
        expected_decision_version=0,
    )

    store.upsert_candidate(
        SourceCandidate(
            candidate_id=candidate.candidate_id,
            program_id="demo",
            channel="teams",
            provider_instance_id="default",
            ref_id="series-1",
            ref_kind=SourceRefKind.MEETING_SERIES,
            display_name="Acme Weekly Review renamed",
            confidence=0.99,
            source_provider="graph_calendar",
            status=SourceCandidateStatus.PENDING,
            evidence_json=candidate_evidence_json({"matched_terms": ["renamed"]}),
            first_discovered_at=as_of,
            last_seen_at=as_of + timedelta(hours=1),
        ),
        pii_prescrubbed=True,
    )

    persisted = store.get_candidate(candidate.candidate_id)
    assert persisted is not None
    assert persisted.status == SourceCandidateStatus.REJECTED
    assert persisted.decision_reason == "wrong meeting"
    assert persisted.decision_version == rejected.decision_version


def test_derive_intent_state_requires_two_high_confidence_pending_candidates_for_ambiguity(tmp_path) -> None:
    store = SourceCandidateStore(tmp_path / "channel_registry.sqlite3", "demo")
    as_of = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    store.bootstrap_intents(
        workstreams=(
            Workstream(
                id="demo.acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Review"),),
                ),
            ),
        ),
        registry_artifacts=(),
        as_of=as_of,
    )
    intent = store.list_intents(workstream_id="demo.acme")[0]
    for index, ref_id in enumerate(("series-1", "series-2"), start=1):
        candidate = SourceCandidate(
            candidate_id=build_source_candidate_id(
                program_id="demo",
                channel="teams",
                provider_instance_id="default",
                ref_kind=SourceRefKind.MEETING_SERIES,
                ref_id=ref_id,
            ),
            program_id="demo",
            channel="teams",
            provider_instance_id="default",
            ref_id=ref_id,
            ref_kind=SourceRefKind.MEETING_SERIES,
            display_name=f"Candidate {index}",
            confidence=0.30,
            source_provider="graph_calendar",
            status=SourceCandidateStatus.PENDING,
            evidence_json=candidate_evidence_json({"matched_terms": [ref_id]}),
            first_discovered_at=as_of,
            last_seen_at=as_of,
        )
        store.upsert_candidate(candidate, pii_prescrubbed=True)
        store.link_candidate_to_intent(candidate.candidate_id, intent.intent_id, 0.30)

    assert store.derive_intent_state(intent.intent_id, as_of=as_of) == SourceIntentStatus.CANDIDATE_FOUND.value


def test_connect_is_atomic_across_multiple_statements_in_one_block(tmp_path) -> None:
    """INV-AF-13 (WO-2 item 10) atomicity regression.

    ``_connect()`` used to open the connection with ``isolation_level=None``
    (autocommit): each statement inside a ``with store._connect() as conn:``
    block committed durably the instant it ran, independent of whatever
    happened later in the same block. After migrating to
    ``open_program_db()``, all statements in one block now share a single
    implicit transaction that commits -- or rolls back -- together. This
    test would have FAILED under the old autocommit behaviour (the INSERT
    below would have persisted despite the later exception); it must pass
    now that the block is atomic.
    """
    store = SourceCandidateStore(tmp_path / "channel_registry.sqlite3", "demo")

    with pytest.raises(RuntimeError, match="simulate interruption"):
        with store._connect() as conn:
            conn.execute(
                """
                INSERT INTO source_intents(
                    intent_id, program_id, workstream_id, ref_kind, display_name, normalized_name,
                    status, created_at, updated_at, updated_by, decision_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "intent-atomicity-test",
                    "demo",
                    "demo.acme",
                    SourceRefKind.MEETING_SERIES.value,
                    "Should Not Persist",
                    "should not persist",
                    SourceIntentStatus.DECLARED.value,
                    "2026-06-01T00:00:00+00:00",
                    "2026-06-01T00:00:00+00:00",
                    None,
                    0,
                ),
            )
            raise RuntimeError("simulate interruption")

    with store._connect() as conn:
        rows = conn.execute("SELECT intent_id FROM source_intents WHERE intent_id = ?", ("intent-atomicity-test",)).fetchall()

    assert rows == []
