from __future__ import annotations

from datetime import date, datetime, timezone

from src.core.m365_registry_store import M365RegistryArtifact, M365RoutingFeedbackEvent
from src.core.m365_signal_corpus import build_m365_corpus_texts_by_workstream, build_m365_reassign_corrections_by_workstream, build_m365_rejected_texts_by_workstream
from src.core.models_v2 import Workstream


def test_build_m365_corpus_texts_by_workstream_ignores_stale_feedback() -> None:
    workstreams = (Workstream(id="acme", name="Store rollout"),)
    artifacts = (
        M365RegistryArtifact(
            artifact_id="thread:auto:stale1",
            artifact_type="email_thread",
            inferred_workstream="acme",
            confidence=0.91,
            confidence_source="pm_confirmed",
            pm_confirmed=True,
            promoted_to_workstreams_yaml=False,
            first_seen=date(2026, 3, 1),
            last_seen=date(2026, 3, 1),
            display_name="Legacy ramp planning review",
            routing_reasoning="Legacy confirmed routing reason.",
        ),
        M365RegistryArtifact(
            artifact_id="thread:auto:recent1",
            artifact_type="email_thread",
            inferred_workstream="acme",
            confidence=0.91,
            confidence_source="pm_confirmed",
            pm_confirmed=True,
            promoted_to_workstreams_yaml=False,
            first_seen=date(2026, 5, 8),
            last_seen=date(2026, 5, 8),
            display_name="Current ramp review",
            routing_reasoning="Current confirmed routing reason.",
        ),
    )
    feedback_events = (
        M365RoutingFeedbackEvent(
            ts=datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc),
            artifact_id="thread:auto:stale1",
            action="confirm",
            pm_alias="operator",
            workstream_id="acme",
            reason="Legacy ramp planning confirmation.",
        ),
        M365RoutingFeedbackEvent(
            ts=datetime(2026, 5, 9, 9, 0, tzinfo=timezone.utc),
            artifact_id="thread:auto:recent1",
            action="confirm",
            pm_alias="operator",
            workstream_id="acme",
            reason="Current ramp review confirmation.",
        ),
    )

    corpus = build_m365_corpus_texts_by_workstream(
        workstreams=workstreams,
        registry_artifacts=artifacts,
        feedback_events=feedback_events,
        approved_signals=(),
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    assert corpus["acme"] == (
        "Current ramp review",
        "Current confirmed routing reason.",
        "Current ramp review confirmation.",
    )


def test_build_m365_corpus_texts_by_workstream_preserves_repeated_recent_feedback() -> None:
    workstreams = (Workstream(id="acme", name="Store rollout"),)
    artifacts = (
        M365RegistryArtifact(
            artifact_id="thread:auto:recent-a",
            artifact_type="email_thread",
            inferred_workstream="acme",
            confidence=0.91,
            confidence_source="pm_confirmed",
            pm_confirmed=True,
            promoted_to_workstreams_yaml=False,
            first_seen=date(2026, 5, 8),
            last_seen=date(2026, 5, 8),
            display_name="Current ramp review A",
            routing_reasoning="Current confirmed routing reason A.",
        ),
        M365RegistryArtifact(
            artifact_id="thread:auto:recent-b",
            artifact_type="email_thread",
            inferred_workstream="acme",
            confidence=0.91,
            confidence_source="pm_confirmed",
            pm_confirmed=True,
            promoted_to_workstreams_yaml=False,
            first_seen=date(2026, 5, 9),
            last_seen=date(2026, 5, 9),
            display_name="Current ramp review B",
            routing_reasoning="Current confirmed routing reason B.",
        ),
    )
    feedback_events = (
        M365RoutingFeedbackEvent(
            ts=datetime(2026, 5, 9, 9, 0, tzinfo=timezone.utc),
            artifact_id="thread:auto:recent-a",
            action="confirm",
            pm_alias="operator",
            workstream_id="acme",
            reason="Repeated ramp confirmation.",
        ),
        M365RoutingFeedbackEvent(
            ts=datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc),
            artifact_id="thread:auto:recent-b",
            action="confirm",
            pm_alias="operator",
            workstream_id="acme",
            reason="Repeated ramp confirmation.",
        ),
    )

    corpus = build_m365_corpus_texts_by_workstream(
        workstreams=workstreams,
        registry_artifacts=artifacts,
        feedback_events=feedback_events,
        approved_signals=(),
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    assert corpus["acme"].count("Repeated ramp confirmation.") == 2


def test_build_m365_rejected_texts_by_workstream_ignores_stale_feedback() -> None:
    workstreams = (Workstream(id="acme", name="Store rollout"),)
    artifacts = (
        M365RegistryArtifact(
            artifact_id="thread:auto:stale2",
            artifact_type="email_thread",
            inferred_workstream="acme",
            confidence=0.05,
            confidence_source="pm_rejected",
            pm_confirmed=False,
            promoted_to_workstreams_yaml=False,
            first_seen=date(2026, 3, 1),
            last_seen=date(2026, 3, 1),
            display_name="Legacy finance planning thread",
            routing_reasoning="Legacy rejection reason.",
        ),
        M365RegistryArtifact(
            artifact_id="thread:auto:recent2",
            artifact_type="email_thread",
            inferred_workstream="acme",
            confidence=0.05,
            confidence_source="pm_rejected",
            pm_confirmed=False,
            promoted_to_workstreams_yaml=False,
            first_seen=date(2026, 5, 8),
            last_seen=date(2026, 5, 8),
            display_name="Current finance planning thread",
            routing_reasoning="Current rejection reason.",
        ),
    )
    feedback_events = (
        M365RoutingFeedbackEvent(
            ts=datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc),
            artifact_id="thread:auto:stale2",
            action="reject",
            pm_alias="operator",
            workstream_id="acme",
            reason="Legacy finance planning rejection.",
        ),
        M365RoutingFeedbackEvent(
            ts=datetime(2026, 5, 9, 9, 0, tzinfo=timezone.utc),
            artifact_id="thread:auto:recent2",
            action="reject",
            pm_alias="operator",
            workstream_id="acme",
            reason="Current finance planning rejection.",
        ),
    )

    corpus = build_m365_rejected_texts_by_workstream(
        workstreams=workstreams,
        registry_artifacts=artifacts,
        feedback_events=feedback_events,
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    assert corpus["acme"] == (
        "Current finance planning thread",
        "Current rejection reason.",
        "Current finance planning rejection.",
    )


def test_build_m365_rejected_texts_by_workstream_preserves_repeated_recent_feedback() -> None:
    workstreams = (Workstream(id="acme", name="Store rollout"),)
    artifacts = (
        M365RegistryArtifact(
            artifact_id="thread:auto:recent-c",
            artifact_type="email_thread",
            inferred_workstream="acme",
            confidence=0.05,
            confidence_source="pm_rejected",
            pm_confirmed=False,
            promoted_to_workstreams_yaml=False,
            first_seen=date(2026, 5, 8),
            last_seen=date(2026, 5, 8),
            display_name="Current finance planning thread A",
            routing_reasoning="Current rejection reason A.",
        ),
        M365RegistryArtifact(
            artifact_id="thread:auto:recent-d",
            artifact_type="email_thread",
            inferred_workstream="acme",
            confidence=0.05,
            confidence_source="pm_rejected",
            pm_confirmed=False,
            promoted_to_workstreams_yaml=False,
            first_seen=date(2026, 5, 9),
            last_seen=date(2026, 5, 9),
            display_name="Current finance planning thread B",
            routing_reasoning="Current rejection reason B.",
        ),
    )
    feedback_events = (
        M365RoutingFeedbackEvent(
            ts=datetime(2026, 5, 9, 9, 0, tzinfo=timezone.utc),
            artifact_id="thread:auto:recent-c",
            action="reject",
            pm_alias="operator",
            workstream_id="acme",
            reason="Repeated finance planning rejection.",
        ),
        M365RoutingFeedbackEvent(
            ts=datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc),
            artifact_id="thread:auto:recent-d",
            action="reject",
            pm_alias="operator",
            workstream_id="acme",
            reason="Repeated finance planning rejection.",
        ),
    )

    corpus = build_m365_rejected_texts_by_workstream(
        workstreams=workstreams,
        registry_artifacts=artifacts,
        feedback_events=feedback_events,
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    assert corpus["acme"].count("Repeated finance planning rejection.") == 2


def test_build_m365_rejected_texts_by_workstream_uses_reassign_source_workstream() -> None:
    workstreams = (
        Workstream(id="acme", name="Store rollout"),
        Workstream(id="contoso", name="Device delivery"),
    )
    artifacts = (
        M365RegistryArtifact(
            artifact_id="thread:auto:reassign1",
            artifact_type="email_thread",
            inferred_workstream="contoso",
            confidence=0.91,
            confidence_source="pm_confirmed",
            pm_confirmed=True,
            promoted_to_workstreams_yaml=False,
            first_seen=date(2026, 5, 8),
            last_seen=date(2026, 5, 8),
            display_name="DD pilot readiness thread",
            routing_reasoning="Belongs with DD pilot execution.",
        ),
    )
    feedback_events = (
        M365RoutingFeedbackEvent(
            ts=datetime(2026, 5, 9, 9, 0, tzinfo=timezone.utc),
            artifact_id="thread:auto:reassign1",
            action="reassign",
            pm_alias="operator",
            workstream_id="contoso",
            prior_workstream_id="acme",
            reason="Belongs with DD pilot execution.",
        ),
    )

    corpus = build_m365_rejected_texts_by_workstream(
        workstreams=workstreams,
        registry_artifacts=artifacts,
        feedback_events=feedback_events,
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    assert corpus["acme"] == (
        "DD pilot readiness thread",
        "Belongs with DD pilot execution.",
    )
    assert "contoso" not in corpus


def test_build_m365_reassign_corrections_by_workstream_keeps_structured_source_and_destination() -> None:
    workstreams = (
        Workstream(id="acme", name="Store rollout"),
        Workstream(id="contoso", name="Device delivery"),
    )
    artifacts = (
        M365RegistryArtifact(
            artifact_id="thread:auto:reassign2",
            artifact_type="email_thread",
            inferred_workstream="contoso",
            confidence=0.91,
            confidence_source="pm_confirmed",
            pm_confirmed=True,
            promoted_to_workstreams_yaml=False,
            first_seen=date(2026, 5, 8),
            last_seen=date(2026, 5, 8),
            display_name="DD pilot readiness thread",
            routing_reasoning="Belongs with DD pilot execution.",
        ),
    )
    feedback_events = (
        M365RoutingFeedbackEvent(
            ts=datetime(2026, 5, 9, 9, 0, tzinfo=timezone.utc),
            artifact_id="thread:auto:reassign2",
            action="reassign",
            pm_alias="operator",
            workstream_id="contoso",
            prior_workstream_id="acme",
            reason="Belongs with DD pilot execution.",
        ),
    )

    corrections = build_m365_reassign_corrections_by_workstream(
        workstreams=workstreams,
        registry_artifacts=artifacts,
        feedback_events=feedback_events,
        as_of=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
    )

    assert len(corrections["contoso"]) == 1
    correction = corrections["contoso"][0]
    assert correction.prior_workstream_id == "acme"
    assert correction.corrected_workstream_id == "contoso"
    assert correction.artifact_display_name == "DD pilot readiness thread"
    assert correction.reason == "Belongs with DD pilot execution."