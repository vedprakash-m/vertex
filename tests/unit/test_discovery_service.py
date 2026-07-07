from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core.channel_registry_store import ChannelRegistryStore
from src.core.discovery_intent import (
    SourceCandidate,
    SourceCandidateStatus,
    SourceIntent,
    SourceIntentStatus,
    SourceRefKind,
    build_source_candidate_id,
    build_source_intent_id,
    normalize_intent_display_name,
)
from src.core.discovery_service import (
    accept_candidate_and_resolve_intent,
    build_accepted_candidate_result,
    channel_for_source_ref_kind,
    seeded_source_attempt_reason,
)
from src.core.source_candidate_store import SourceCandidateStore, candidate_evidence_json


_AS_OF = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)


def _build_intent() -> SourceIntent:
    return SourceIntent(
        intent_id=build_source_intent_id(
            program_id="demo",
            workstream_id="demo.ws",
            ref_kind=SourceRefKind.EMAIL_THREAD,
            display_name="Launch Thread",
        ),
        program_id="demo",
        workstream_id="demo.ws",
        ref_kind=SourceRefKind.EMAIL_THREAD,
        display_name="Launch Thread",
        normalized_name=normalize_intent_display_name("Launch Thread"),
        status=SourceIntentStatus.DECLARED,
        created_at=_AS_OF,
        updated_at=_AS_OF,
    )


def _build_candidate(*, status: SourceCandidateStatus = SourceCandidateStatus.PENDING) -> SourceCandidate:
    return SourceCandidate(
        candidate_id=build_source_candidate_id(
            program_id="demo",
            channel="email",
            provider_instance_id="default",
            ref_kind=SourceRefKind.EMAIL_THREAD,
            ref_id="thread-launch-123",
        ),
        program_id="demo",
        channel="email",
        provider_instance_id="default",
        ref_id="thread-launch-123",
        ref_kind=SourceRefKind.EMAIL_THREAD,
        display_name="Launch Thread",
        confidence=0.91,
        source_provider="graph_mail",
        status=status,
        evidence_json=candidate_evidence_json({"matched_terms": ["Launch Thread"]}),
        first_discovered_at=_AS_OF,
        last_seen_at=_AS_OF,
    )


def test_build_accepted_candidate_result_preserves_scope_prefix_and_auto_metadata() -> None:
    intent = _build_intent()
    candidate = _build_candidate()

    manual = build_accepted_candidate_result(
        program_id="demo",
        intent=intent,
        candidate=candidate,
        current_time=_AS_OF,
        scope_prefix="manual",
        auto_resolved=False,
        first_discovered_at=_AS_OF,
    )
    auto = build_accepted_candidate_result(
        program_id="demo",
        intent=intent,
        candidate=candidate,
        current_time=_AS_OF,
        scope_prefix="auto",
        auto_resolved=True,
        first_discovered_at=_AS_OF,
    )

    assert manual.discovered_refs[0].bindings[0].scope_id == f"manual:{intent.intent_id}"
    assert "auto_resolved" not in (manual.discovered_refs[0].registration.metadata or {})
    assert auto.discovered_refs[0].bindings[0].scope_id == f"auto:{intent.intent_id}"
    assert auto.discovered_refs[0].registration.metadata is not None
    assert auto.discovered_refs[0].registration.metadata["auto_resolved"] is True


def test_accept_candidate_and_resolve_intent_writes_registration_and_resolves_intent(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "demo").mkdir(parents=True)
    store = SourceCandidateStore(programs_root / "demo" / "channel_registry.sqlite3", "demo")
    intent = _build_intent()
    candidate = _build_candidate()
    store.upsert_intent(intent)
    store.upsert_candidate(candidate, pii_prescrubbed=True)
    store.link_candidate_to_intent(candidate.candidate_id, intent.intent_id, 0.91)

    result = accept_candidate_and_resolve_intent(
        candidate_store=store,
        program_id="demo",
        programs_root=programs_root,
        intent=intent,
        candidate_id=candidate.candidate_id,
        as_of=_AS_OF,
        ttl_days=30,
        actor_alias="vertex.gather",
        scope_prefix="auto",
        auto_resolved=True,
        first_discovered_at_override=candidate.first_discovered_at,
    )

    assert result.stale_plan is False
    assert result.accepted_candidate is not None
    assert result.accepted_candidate.status == SourceCandidateStatus.ACCEPTED
    assert result.updated_intent is not None
    assert result.updated_intent.status == SourceIntentStatus.RESOLVED
    registrations = ChannelRegistryStore(programs_root / "demo" / "channel_registry.sqlite3", "demo").active_registrations("email")
    assert len(registrations) == 1
    assert registrations[0].workstream_ids == ("demo.ws",)
    assert registrations[0].metadata is not None
    assert registrations[0].metadata["auto_resolved"] is True


def test_accept_candidate_and_resolve_intent_returns_stale_plan_without_mutation(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "demo").mkdir(parents=True)
    store = SourceCandidateStore(programs_root / "demo" / "channel_registry.sqlite3", "demo")
    intent = _build_intent()
    candidate = _build_candidate()
    store.upsert_intent(intent)
    store.upsert_candidate(candidate, pii_prescrubbed=True)
    store.link_candidate_to_intent(candidate.candidate_id, intent.intent_id, 0.91)
    store.update_intent_status(intent.intent_id, status=SourceIntentStatus.SEARCHING, updated_by="pm@test")

    result = accept_candidate_and_resolve_intent(
        candidate_store=store,
        program_id="demo",
        programs_root=programs_root,
        intent=intent,
        candidate_id=candidate.candidate_id,
        as_of=_AS_OF,
        ttl_days=30,
        actor_alias="vertex.gather",
        scope_prefix="auto",
        auto_resolved=True,
        first_discovered_at_override=candidate.first_discovered_at,
    )

    assert result.stale_plan is True
    refreshed = store.get_candidate(candidate.candidate_id)
    assert refreshed is not None
    assert refreshed.status == SourceCandidateStatus.PENDING


def test_seeded_source_attempt_reason_pluralizes_rejections() -> None:
    message = seeded_source_attempt_reason(unavailable_reason=None, suppressed_candidate_count=2)
    assert message is not None
    assert "2 recently rejected candidates" in message
    assert "60-day rejection window" in message


def test_channel_for_source_ref_kind_maps_email_and_teams() -> None:
    assert channel_for_source_ref_kind(SourceRefKind.EMAIL_THREAD) == "email"
    assert channel_for_source_ref_kind(SourceRefKind.TEAMS_CHAT) == "teams"
