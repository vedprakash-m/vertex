from __future__ import annotations

from datetime import datetime, timezone

from src.commands.integration_lifecycle import _clear_candidate_rejection, _reassign_candidate
from src.core.discovery_intent import SourceCandidate, SourceCandidateStatus, SourceIntentStatus, SourceRefKind, build_source_candidate_id
from src.core.models_v2 import TeamsMeetingSeries, Workstream, WorkstreamSignalSources
from src.core.source_candidate_store import SourceCandidateStore, candidate_evidence_json


def test_clear_candidate_rejection_restores_pending_candidate_and_recomputes_intent(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "demo").mkdir(parents=True)
    candidate_store = SourceCandidateStore(programs_root / "demo" / "channel_registry.sqlite3", "demo")
    as_of = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    candidate_store.bootstrap_intents(
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
    intent = candidate_store.list_intents(workstream_id="demo.acme")[0]
    candidate = SourceCandidate(
        candidate_id=build_source_candidate_id(
            program_id="demo",
            channel="teams",
            provider_instance_id="default",
            ref_kind=SourceRefKind.MEETING_SERIES,
            ref_id="series-123",
        ),
        program_id="demo",
        channel="teams",
        provider_instance_id="default",
        ref_id="series-123",
        ref_kind=SourceRefKind.MEETING_SERIES,
        display_name="Acme Weekly Review",
        confidence=0.93,
        source_provider="graph_calendar",
        status=SourceCandidateStatus.REJECTED,
        evidence_json=candidate_evidence_json({"matched_terms": ["Acme Weekly Review"]}),
        first_discovered_at=as_of,
        last_seen_at=as_of,
        decided_by="pm@test",
        decision_reason="wrong meeting",
    )
    candidate_store.upsert_candidate(candidate, pii_prescrubbed=True)
    candidate_store.link_candidate_to_intent(candidate.candidate_id, intent.intent_id, 0.93)

    updated_candidate, intent_updates = _clear_candidate_rejection(
        program="demo",
        candidate_store=candidate_store,
        candidate=candidate,
        pm_alias="pm@test",
    )

    assert updated_candidate.status == SourceCandidateStatus.PENDING
    assert updated_candidate.decision_reason is None
    assert candidate_store.get_candidate(candidate.candidate_id).status == SourceCandidateStatus.PENDING
    assert len(intent_updates) == 1
    assert intent_updates[0][0].intent_id == intent.intent_id
    assert intent_updates[0][1].status == SourceIntentStatus.CANDIDATE_FOUND


def test_reassign_candidate_moves_candidate_to_target_intent_and_recomputes_statuses(tmp_path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "demo").mkdir(parents=True)
    candidate_store = SourceCandidateStore(programs_root / "demo" / "channel_registry.sqlite3", "demo")
    as_of = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    candidate_store.bootstrap_intents(
        workstreams=(
            Workstream(
                id="demo.acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Review"),),
                ),
            ),
            Workstream(
                id="demo.ops",
                name="Ops",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Review"),),
                ),
            ),
        ),
        registry_artifacts=(),
        as_of=as_of,
    )
    old_intent = candidate_store.list_intents(workstream_id="demo.acme")[0]
    new_intent = candidate_store.list_intents(workstream_id="demo.ops")[0]
    candidate = SourceCandidate(
        candidate_id=build_source_candidate_id(
            program_id="demo",
            channel="teams",
            provider_instance_id="default",
            ref_kind=SourceRefKind.MEETING_SERIES,
            ref_id="series-123",
        ),
        program_id="demo",
        channel="teams",
        provider_instance_id="default",
        ref_id="series-123",
        ref_kind=SourceRefKind.MEETING_SERIES,
        display_name="Acme Weekly Review",
        confidence=0.93,
        source_provider="graph_calendar",
        status=SourceCandidateStatus.PENDING,
        evidence_json=candidate_evidence_json({"matched_terms": ["Acme Weekly Review"]}),
        first_discovered_at=as_of,
        last_seen_at=as_of,
    )
    candidate_store.upsert_candidate(candidate, pii_prescrubbed=True)
    candidate_store.link_candidate_to_intent(candidate.candidate_id, old_intent.intent_id, 0.93)

    (old_before, old_after), (new_before, new_after) = _reassign_candidate(
        program="demo",
        candidate_store=candidate_store,
        candidate=candidate,
        workstream_id="demo.ops",
        pm_alias="pm@test",
        reason=None,
        from_intent_id=None,
    )

    assert old_before.intent_id == old_intent.intent_id
    assert old_after.status == SourceIntentStatus.DECLARED
    assert new_before.intent_id == new_intent.intent_id
    assert new_after.status == SourceIntentStatus.CANDIDATE_FOUND
    assert candidate_store.list_candidates_for_intent(old_intent.intent_id) == ()
    assert candidate_store.list_candidates_for_intent(new_intent.intent_id)[0].candidate_id == candidate.candidate_id
