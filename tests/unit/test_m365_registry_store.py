from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
import yaml

from src.core.exceptions import ConfigError
from src.core.m365_registry_store import M365RegistryArtifact, M365RoutingFeedbackEvent, apply_m365_routing_feedback, ensure_m365_registry_bootstrap, get_m365_routing_feedback_path, is_current_m365_registry_promotion_candidate, load_m365_registry, promote_m365_registry_artifact, read_m365_routing_feedback_events, refresh_m365_registry_metrics, upsert_m365_registry_artifacts
from src.core.models_v2 import EmailThreadSource, TeamsChat, TeamsMeetingSeries, Workstream, WorkstreamSignalSources


def test_ensure_m365_registry_bootstrap_seeds_workstream_signal_sources(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    workstreams = (
        Workstream(
            id="acme",
            name="Acme",
            signal_sources=WorkstreamSignalSources(
                teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Ops Review", series_id="meeting-1"),),
                teams_chats=(TeamsChat(display_name="Acme Eng Core Chat", thread_id="thread-1"),),
                workiq_keywords=("SCHIE", "Ramp Review"),
                email_threads=(EmailThreadSource(display_name="SCHIE Mail Thread", thread_id="mail-thread-1"),),
            ),
        ),
    )

    registry = ensure_m365_registry_bootstrap(
        "acme",
        workstreams=workstreams,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )

    persisted = load_m365_registry("acme", programs_root)
    feedback_path = get_m365_routing_feedback_path("acme", programs_root)

    assert registry.artifacts == persisted.artifacts
    assert len(persisted.artifacts) == 3
    assert {artifact.artifact_type for artifact in persisted.artifacts} == {"meeting_series", "teams_channel", "email_thread"}
    assert all(artifact.pm_confirmed is True for artifact in persisted.artifacts)
    assert all(artifact.promoted_to_workstreams_yaml is True for artifact in persisted.artifacts)
    assert all(artifact.topics == ("SCHIE", "Ramp Review") for artifact in persisted.artifacts)
    assert feedback_path.exists()
    assert feedback_path.read_text(encoding="utf-8") == ""


def test_apply_m365_routing_feedback_event_updates_registry_and_appends_feedback(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ensure_m365_registry_bootstrap(
        "acme",
        workstreams=(
            Workstream(
                id="acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_chats=(TeamsChat(display_name="Acme Eng Core Chat", thread_id="thread-1"),),
                    workiq_keywords=("SCHIE",),
                ),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )

    updated_registry = apply_m365_routing_feedback(
        "acme",
        event=M365RoutingFeedbackEvent(
            ts=datetime(2026, 5, 23, 9, 0, tzinfo=timezone.utc),
            artifact_id="chan:acme-acme-eng-core-chat",
            action="reassign",
            pm_alias="operator",
            workstream_id="dd_on_pf",
            topics=("pilot-ready",),
            reason="Belongs with DD pilot execution.",
        ),
        programs_root=programs_root,
    )

    artifact = next(artifact for artifact in updated_registry.artifacts if artifact.artifact_id == "chan:acme-acme-eng-core-chat")
    events = read_m365_routing_feedback_events("acme", programs_root)

    assert artifact.inferred_workstream == "dd_on_pf"
    assert artifact.pm_confirmed is True
    assert artifact.confidence_source == "pm_confirmed"
    assert artifact.confidence >= 0.90
    assert artifact.topics == ("SCHIE", "pilot-ready")
    assert artifact.routing_reasoning == "Belongs with DD pilot execution."
    assert len(events) == 1
    assert events[0].action == "reassign"
    assert events[0].pm_alias == "operator"
    assert events[0].prior_workstream_id == "acme"


def test_apply_m365_routing_feedback_set_id_updates_matching_registry_field(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ensure_m365_registry_bootstrap(
        "acme",
        workstreams=(
            Workstream(
                id="acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_meeting_series=(TeamsMeetingSeries(display_name="Acme Weekly Ops Review", series_id=None),),
                    teams_chats=(TeamsChat(display_name="Acme Eng Core Chat", thread_id=None),),
                    workiq_keywords=("SCHIE",),
                ),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )

    apply_m365_routing_feedback(
        "acme",
        event=M365RoutingFeedbackEvent(
            ts=datetime(2026, 5, 23, 9, 0, tzinfo=timezone.utc),
            artifact_id="meet:acme-acme-weekly-ops-review",
            action="set_series_id",
            pm_alias="operator",
            series_id="series-123",
            reason="Mapped from the recurring invite.",
        ),
        programs_root=programs_root,
    )
    updated_registry = apply_m365_routing_feedback(
        "acme",
        event=M365RoutingFeedbackEvent(
            ts=datetime(2026, 5, 23, 9, 5, tzinfo=timezone.utc),
            artifact_id="chan:acme-acme-eng-core-chat",
            action="set_thread_id",
            pm_alias="operator",
            thread_id="thread-123",
            reason="Mapped from the Teams URL.",
        ),
        programs_root=programs_root,
    )

    series_artifact = next(artifact for artifact in updated_registry.artifacts if artifact.artifact_id == "meet:acme-acme-weekly-ops-review")
    chat_artifact = next(artifact for artifact in updated_registry.artifacts if artifact.artifact_id == "chan:acme-acme-eng-core-chat")
    events = read_m365_routing_feedback_events("acme", programs_root)

    assert series_artifact.series_id == "series-123"
    assert series_artifact.thread_id is None
    assert series_artifact.routing_reasoning == "Mapped from the recurring invite."
    assert chat_artifact.thread_id == "thread-123"
    assert chat_artifact.series_id is None
    assert chat_artifact.routing_reasoning == "Mapped from the Teams URL."
    assert [event.action for event in events] == ["set_series_id", "set_thread_id"]


def test_refresh_m365_registry_metrics_decays_unconfirmed_artifacts_and_rolls_signal_yield(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ensure_m365_registry_bootstrap(
        "acme",
        workstreams=(
            Workstream(
                id="acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    teams_chats=(TeamsChat(display_name="Acme Eng Core Chat", thread_id="thread-1"),),
                    workiq_keywords=("SCHIE",),
                ),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="thread:auto:observed1",
                artifact_type="email_thread",
                display_name="Observed thread",
                thread_id="observed-thread-1",
                inferred_workstream="acme",
                confidence=0.70,
                confidence_source="keyword",
                pm_confirmed=False,
                promoted_to_workstreams_yaml=False,
                first_seen=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc).date(),
                last_seen=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc).date(),
                signal_yield_last_3=(1, 1, 1),
                topics=("SCHIE",),
            ),
            M365RegistryArtifact(
                artifact_id="thread:auto:stale001",
                artifact_type="email_thread",
                display_name="Stale thread",
                thread_id="stale-thread-1",
                inferred_workstream="acme",
                confidence=0.70,
                confidence_source="keyword",
                pm_confirmed=False,
                promoted_to_workstreams_yaml=False,
                first_seen=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc).date(),
                last_seen=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc).date(),
                signal_yield_last_3=(1, 1, 1),
                topics=("SCHIE",),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )

    refreshed = refresh_m365_registry_metrics(
        "acme",
        as_of=datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc),
        observed_thread_ids=("observed-thread-1",),
        programs_root=programs_root,
    )

    observed = next(artifact for artifact in refreshed.artifacts if artifact.artifact_id == "thread:auto:observed1")
    stale = next(artifact for artifact in refreshed.artifacts if artifact.artifact_id == "thread:auto:stale001")

    assert observed.confidence == 0.70
    assert observed.signal_yield_last_3 == (1, 1, 1)
    assert observed.last_seen == datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc).date()
    assert stale.confidence == 0.65
    assert stale.signal_yield_last_3 == (1, 1, 0)
    assert stale.last_seen == datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc).date()


def test_promote_m365_registry_artifact_updates_workstreams_yaml_and_marks_registry(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    workstreams_path = programs_root / "acme" / "workstreams.yaml"
    workstreams_path.parent.mkdir(parents=True, exist_ok=True)
    workstreams_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "workstreams": [
                    {
                        "id": "acme",
                        "name": "Acme",
                        "signal_sources": {"workiq_keywords": ["SCHIE"]},
                    }
                ],
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="chan:acme-acme-eng-core-chat",
                artifact_type="teams_channel",
                display_name="Acme Eng Core Chat",
                thread_id="thread-123",
                inferred_workstream="acme",
                confidence=0.95,
                confidence_source="pm_confirmed",
                pm_confirmed=True,
                promoted_to_workstreams_yaml=False,
                first_seen=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc).date(),
                last_seen=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc).date(),
                topics=("SCHIE",),
                signal_yield_last_3=(1, 1, 1),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )

    promote_m365_registry_artifact(
        "acme",
        artifact_id="chan:acme-acme-eng-core-chat",
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 13, 0, tzinfo=timezone.utc),
    )

    registry = load_m365_registry("acme", programs_root)
    artifact = next(item for item in registry.artifacts if item.artifact_id == "chan:acme-acme-eng-core-chat")
    workstreams_document = yaml.safe_load(workstreams_path.read_text(encoding="utf-8"))
    teams_chats = workstreams_document["workstreams"][0]["signal_sources"]["teams_chats"]

    assert artifact.promoted_to_workstreams_yaml is True
    assert teams_chats == [{"display_name": "Acme Eng Core Chat", "thread_id": "thread-123"}]


def test_promote_m365_registry_artifact_updates_email_threads_and_marks_registry(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    workstreams_path = programs_root / "acme" / "workstreams.yaml"
    workstreams_path.parent.mkdir(parents=True, exist_ok=True)
    workstreams_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "workstreams": [{"id": "acme", "name": "Acme", "signal_sources": {}}],
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="thread:auto:abc12345",
                artifact_type="email_thread",
                display_name="SCHIE Mail Thread",
                thread_id="thread-123",
                inferred_workstream="acme",
                confidence=0.95,
                confidence_source="pm_confirmed",
                pm_confirmed=True,
                promoted_to_workstreams_yaml=False,
                first_seen=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc).date(),
                last_seen=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc).date(),
                signal_yield_last_3=(1, 1, 1),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )

    promote_m365_registry_artifact(
        "acme",
        artifact_id="thread:auto:abc12345",
        programs_root=programs_root,
    )

    registry = load_m365_registry("acme", programs_root)
    artifact = next(item for item in registry.artifacts if item.artifact_id == "thread:auto:abc12345")
    workstreams_document = yaml.safe_load(workstreams_path.read_text(encoding="utf-8"))
    email_threads = workstreams_document["workstreams"][0]["signal_sources"]["email_threads"]

    assert artifact.promoted_to_workstreams_yaml is True
    assert email_threads == [{"display_name": "SCHIE Mail Thread", "thread_id": "thread-123"}]


def test_current_m365_registry_promotion_candidate_requires_yield_and_no_active_recent_rejection() -> None:
    artifact = M365RegistryArtifact(
        artifact_id="chan:acme-promote-ready",
        artifact_type="teams_channel",
        display_name="Promotion Ready Chat",
        thread_id="thread-123",
        inferred_workstream="acme",
        confidence=1.0,
        confidence_source="pm_confirmed",
        pm_confirmed=True,
        promoted_to_workstreams_yaml=False,
        first_seen=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc).date(),
        last_seen=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc).date(),
        signal_yield_last_3=(1, 1, 0),
    )

    assert is_current_m365_registry_promotion_candidate(
        artifact,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    ) is False

    eligible_artifact = M365RegistryArtifact(
        artifact_id=artifact.artifact_id,
        artifact_type=artifact.artifact_type,
        display_name=artifact.display_name,
        thread_id=artifact.thread_id,
        inferred_workstream=artifact.inferred_workstream,
        confidence=artifact.confidence,
        confidence_source=artifact.confidence_source,
        pm_confirmed=artifact.pm_confirmed,
        promoted_to_workstreams_yaml=artifact.promoted_to_workstreams_yaml,
        first_seen=artifact.first_seen,
        last_seen=artifact.last_seen,
        signal_yield_last_3=(1, 1, 1),
    )
    recent_reject = M365RoutingFeedbackEvent(
        ts=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
        artifact_id="chan:acme-promote-ready",
        action="reject",
        pm_alias="operator",
    )
    later_confirm = M365RoutingFeedbackEvent(
        ts=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        artifact_id="chan:acme-promote-ready",
        action="confirm",
        pm_alias="operator",
    )

    assert is_current_m365_registry_promotion_candidate(
        eligible_artifact,
        feedback_events=(recent_reject,),
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    ) is False
    assert is_current_m365_registry_promotion_candidate(
        eligible_artifact,
        feedback_events=(recent_reject, later_confirm),
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    ) is True

    auto_promotable_artifact = M365RegistryArtifact(
        artifact_id="thread:auto:steady001",
        artifact_type="email_thread",
        display_name="Steady High Confidence Thread",
        thread_id="steady-thread-1",
        inferred_workstream="acme",
        confidence=0.9,
        confidence_source="keyword_router",
        pm_confirmed=False,
        promoted_to_workstreams_yaml=False,
        first_seen=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc).date(),
        last_seen=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc).date(),
        signal_yield_last_3=(1, 1, 1),
        high_confidence_streak=3,
    )

    assert is_current_m365_registry_promotion_candidate(
        auto_promotable_artifact,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    ) is True


def test_promote_m365_registry_artifact_blocks_insufficient_signal_yield(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    workstreams_path = programs_root / "acme" / "workstreams.yaml"
    workstreams_path.parent.mkdir(parents=True, exist_ok=True)
    workstreams_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "workstreams": [{"id": "acme", "name": "Acme", "signal_sources": {}}],
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="chan:acme-low-yield",
                artifact_type="teams_channel",
                display_name="Low Yield Chat",
                thread_id="thread-123",
                inferred_workstream="acme",
                confidence=1.0,
                confidence_source="pm_confirmed",
                pm_confirmed=True,
                promoted_to_workstreams_yaml=False,
                first_seen=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc).date(),
                last_seen=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc).date(),
                signal_yield_last_3=(1, 0, 0),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
    )

    with pytest.raises(ConfigError, match="signal_yield_last_3 sum >= 3"):
        promote_m365_registry_artifact(
            "acme",
            artifact_id="chan:acme-low-yield",
            programs_root=programs_root,
            as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
        )


def test_promote_m365_registry_artifact_rejects_non_string_workstream_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    workstreams_path = programs_root / "acme" / "workstreams.yaml"
    workstreams_path.parent.mkdir(parents=True, exist_ok=True)
    workstreams_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "2.0",
                "workstreams": [{"id": 1, "name": "Acme", "signal_sources": {}}],
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="chan:acme-acme-eng-core-chat",
                artifact_type="teams_channel",
                display_name="Acme Eng Core Chat",
                thread_id="thread-123",
                inferred_workstream="acme",
                confidence=0.95,
                confidence_source="pm_confirmed",
                pm_confirmed=True,
                promoted_to_workstreams_yaml=False,
                first_seen=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc).date(),
                last_seen=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc).date(),
                topics=("SCHIE",),
                signal_yield_last_3=(1, 1, 1),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )

    with pytest.raises(ConfigError, match=r"workstream entry #1 id must be a string"):
        promote_m365_registry_artifact(
            "acme",
            artifact_id="chan:acme-acme-eng-core-chat",
            programs_root=programs_root,
            as_of=datetime(2026, 5, 22, 13, 0, tzinfo=timezone.utc),
        )


def test_refresh_m365_registry_metrics_tracks_high_confidence_streak(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ensure_m365_registry_bootstrap(
        "acme",
        workstreams=(
            Workstream(
                id="acme",
                name="Acme",
                signal_sources=WorkstreamSignalSources(
                    workiq_keywords=("SCHIE",),
                ),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="thread:auto:steady001",
                artifact_type="email_thread",
                display_name="Steady High Confidence Thread",
                thread_id="steady-thread-1",
                inferred_workstream="acme",
                confidence=0.9,
                confidence_source="keyword",
                pm_confirmed=False,
                promoted_to_workstreams_yaml=False,
                first_seen=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc).date(),
                last_seen=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc).date(),
                signal_yield_last_3=(1, 1, 1),
                high_confidence_streak=2,
                topics=("SCHIE",),
            ),
            M365RegistryArtifact(
                artifact_id="thread:auto:cooling01",
                artifact_type="email_thread",
                display_name="Cooling Thread",
                thread_id="cooling-thread-1",
                inferred_workstream="acme",
                confidence=0.84,
                confidence_source="keyword",
                pm_confirmed=False,
                promoted_to_workstreams_yaml=False,
                first_seen=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc).date(),
                last_seen=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc).date(),
                signal_yield_last_3=(1, 1, 1),
                high_confidence_streak=2,
                topics=("SCHIE",),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
    )

    refreshed = refresh_m365_registry_metrics(
        "acme",
        as_of=datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc),
        observed_thread_ids=("steady-thread-1",),
        programs_root=programs_root,
    )

    steady = next(artifact for artifact in refreshed.artifacts if artifact.artifact_id == "thread:auto:steady001")
    cooling = next(artifact for artifact in refreshed.artifacts if artifact.artifact_id == "thread:auto:cooling01")

    assert steady.confidence == 0.9
    assert steady.high_confidence_streak == 3
    assert cooling.confidence == 0.79
    assert cooling.high_confidence_streak == 0


def test_upsert_m365_registry_artifacts_rebinds_recreated_chat_and_preserves_legacy_ids(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="chan:acme-ramp-chat",
                artifact_type="teams_channel",
                display_name="Ramp Chat",
                thread_id="thread-old",
                inferred_workstream="acme",
                confidence=1.0,
                confidence_source="pm_confirmed",
                pm_confirmed=True,
                promoted_to_workstreams_yaml=False,
                first_seen=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc).date(),
                last_seen=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc).date(),
                topics=("northwind", "ramp"),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
    )

    registry = upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="thread:auto:newchat1",
                artifact_type="teams_channel",
                display_name="Ramp Chat",
                thread_id="thread-new",
                inferred_workstream="acme",
                confidence=0.86,
                confidence_source="keyword",
                pm_confirmed=False,
                promoted_to_workstreams_yaml=False,
                first_seen=datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc).date(),
                last_seen=datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc).date(),
                topics=("northwind", "ramp"),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc),
    )

    assert len(registry.artifacts) == 1
    artifact = registry.artifacts[0]
    assert artifact.artifact_id == "chan:acme-ramp-chat"
    assert artifact.thread_id == "thread-new"
    assert "thread:auto:newchat1" in artifact.legacy_artifact_ids


def test_upsert_m365_registry_artifacts_rebinds_recreated_meeting_series(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="meet:acme-ramp-weekly",
                artifact_type="meeting_series",
                display_name="Ramp Weekly",
                series_id="series-old",
                inferred_workstream="acme",
                confidence=1.0,
                confidence_source="pm_confirmed",
                pm_confirmed=True,
                promoted_to_workstreams_yaml=False,
                first_seen=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc).date(),
                last_seen=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc).date(),
                topics=("northwind", "weekly"),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
    )

    registry = upsert_m365_registry_artifacts(
        "acme",
        artifacts=(
            M365RegistryArtifact(
                artifact_id="meet:acme-ramp-weekly-recreated",
                artifact_type="meeting_series",
                display_name="Ramp Weekly",
                series_id="series-new",
                inferred_workstream="acme",
                confidence=0.88,
                confidence_source="keyword",
                pm_confirmed=False,
                promoted_to_workstreams_yaml=False,
                first_seen=datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc).date(),
                last_seen=datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc).date(),
                topics=("northwind", "weekly"),
            ),
        ),
        programs_root=programs_root,
        as_of=datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc),
    )

    assert len(registry.artifacts) == 1
    artifact = registry.artifacts[0]
    assert artifact.artifact_id == "meet:acme-ramp-weekly"
    assert artifact.series_id == "series-new"
    assert "meet:acme-ramp-weekly-recreated" in artifact.legacy_artifact_ids


def test_load_m365_registry_rejects_non_string_schema_version(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    registry_path = programs_root / "acme" / "m365_registry.yaml"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "program_id": "acme",
                "artifacts": [],
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="schema_version must be a string"):
        load_m365_registry("acme", programs_root)


def test_load_m365_registry_rejects_non_string_display_name(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    registry_path = programs_root / "acme" / "m365_registry.yaml"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "program_id": "acme",
                "artifacts": [
                    {
                        "artifact_id": "thread:auto:test001",
                        "artifact_type": "email_thread",
                        "display_name": 1,
                        "thread_id": "thread-1",
                        "inferred_workstream": "acme",
                        "confidence": 0.7,
                        "confidence_source": "keyword",
                        "pm_confirmed": False,
                        "promoted_to_workstreams_yaml": False,
                        "first_seen": "2026-05-20",
                        "last_seen": "2026-05-21",
                        "signal_yield_last_3": [1, 1, 1],
                        "topics": ["SCHIE"],
                    }
                ],
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="display_name must be a string"):
        load_m365_registry("acme", programs_root)


def test_load_m365_registry_rejects_non_string_topic_entry(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    registry_path = programs_root / "acme" / "m365_registry.yaml"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "program_id": "acme",
                "artifacts": [
                    {
                        "artifact_id": "thread:auto:test001",
                        "artifact_type": "email_thread",
                        "display_name": "Observed thread",
                        "thread_id": "thread-1",
                        "inferred_workstream": "acme",
                        "confidence": 0.7,
                        "confidence_source": "keyword",
                        "pm_confirmed": False,
                        "promoted_to_workstreams_yaml": False,
                        "first_seen": "2026-05-20",
                        "last_seen": "2026-05-21",
                        "signal_yield_last_3": [1, 1, 1],
                        "topics": [1],
                    }
                ],
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="topics must contain strings only"):
        load_m365_registry("acme", programs_root)


def test_load_m365_registry_rejects_numeric_string_high_confidence_streak(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    registry_path = programs_root / "acme" / "m365_registry.yaml"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "program_id": "acme",
                "artifacts": [
                    {
                        "artifact_id": "thread:auto:test001",
                        "artifact_type": "email_thread",
                        "display_name": "Observed thread",
                        "thread_id": "thread-1",
                        "inferred_workstream": "acme",
                        "confidence": 0.7,
                        "confidence_source": "keyword",
                        "pm_confirmed": False,
                        "promoted_to_workstreams_yaml": False,
                        "first_seen": "2026-05-20",
                        "last_seen": "2026-05-21",
                        "signal_yield_last_3": [1, 1, 1],
                        "topics": ["SCHIE"],
                        "high_confidence_streak": "2",
                    }
                ],
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="invalid artifact 'thread:auto:test001' high_confidence_streak"):
        load_m365_registry("acme", programs_root)


def test_load_m365_registry_rejects_numeric_string_signal_yield_entry(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    registry_path = programs_root / "acme" / "m365_registry.yaml"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "program_id": "acme",
                "artifacts": [
                    {
                        "artifact_id": "thread:auto:test001",
                        "artifact_type": "email_thread",
                        "display_name": "Observed thread",
                        "thread_id": "thread-1",
                        "inferred_workstream": "acme",
                        "confidence": 0.7,
                        "confidence_source": "keyword",
                        "pm_confirmed": False,
                        "promoted_to_workstreams_yaml": False,
                        "first_seen": "2026-05-20",
                        "last_seen": "2026-05-21",
                        "signal_yield_last_3": [1, "1", 1],
                        "topics": ["SCHIE"],
                    }
                ],
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="artifact 'thread:auto:test001' signal_yield_last_3 must contain integers"):
        load_m365_registry("acme", programs_root)


def test_read_m365_routing_feedback_events_rejects_non_string_pm_alias(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    feedback_path = get_m365_routing_feedback_path("acme", programs_root)
    feedback_path.parent.mkdir(parents=True, exist_ok=True)
    feedback_path.write_text(
        json.dumps(
            {
                "ts": "2026-05-23T09:00:00+00:00",
                "artifact_id": "chan:acme-acme-eng-core-chat",
                "action": "confirm",
                "pm_alias": 1,
                "workstream_id": "acme",
                "topics": ["SCHIE"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="pm_alias must be a string"):
        read_m365_routing_feedback_events("acme", programs_root)


def test_read_m365_routing_feedback_events_rejects_missing_ts(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    feedback_path = get_m365_routing_feedback_path("acme", programs_root)
    feedback_path.parent.mkdir(parents=True, exist_ok=True)
    feedback_path.write_text(
        json.dumps(
            {
                "artifact_id": "chan:acme-acme-eng-core-chat",
                "action": "confirm",
                "pm_alias": "operator",
                "workstream_id": "acme",
                "topics": ["SCHIE"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="invalid ts"):
        read_m365_routing_feedback_events("acme", programs_root)


def test_read_m365_routing_feedback_events_rejects_naive_ts(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    feedback_path = get_m365_routing_feedback_path("acme", programs_root)
    feedback_path.parent.mkdir(parents=True, exist_ok=True)
    feedback_path.write_text(
        json.dumps(
            {
                "ts": "2026-05-23T09:00:00",
                "artifact_id": "chan:acme-acme-eng-core-chat",
                "action": "confirm",
                "pm_alias": "operator",
                "workstream_id": "acme",
                "topics": ["SCHIE"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="invalid ts"):
        read_m365_routing_feedback_events("acme", programs_root)


def test_read_m365_routing_feedback_events_rejects_non_object_row(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    feedback_path = get_m365_routing_feedback_path("acme", programs_root)
    feedback_path.parent.mkdir(parents=True, exist_ok=True)
    feedback_path.write_text(json.dumps(["not", "an", "object"]) + "\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="M365 routing feedback entry .* must be a mapping"):
        read_m365_routing_feedback_events("acme", programs_root)


def test_load_m365_registry_rejects_numeric_string_confidence(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    registry_path = programs_root / "acme" / "m365_registry.yaml"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "program_id": "acme",
                "artifacts": [
                    {
                        "artifact_id": "thread:auto:test001",
                        "artifact_type": "email_thread",
                        "display_name": "Observed thread",
                        "thread_id": "thread-1",
                        "inferred_workstream": "acme",
                        "confidence": "0.7",
                        "confidence_source": "keyword",
                        "pm_confirmed": False,
                        "promoted_to_workstreams_yaml": False,
                        "first_seen": "2026-05-20",
                        "last_seen": "2026-05-21",
                        "signal_yield_last_3": [1, 1, 1],
                        "topics": ["SCHIE"],
                    }
                ],
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="artifact 'thread:auto:test001' confidence must be numeric"):
        load_m365_registry("acme", programs_root)


def test_load_m365_registry_rejects_non_boolean_pm_confirmed(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    registry_path = programs_root / "acme" / "m365_registry.yaml"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "program_id": "acme",
                "artifacts": [
                    {
                        "artifact_id": "thread:auto:test001",
                        "artifact_type": "email_thread",
                        "display_name": "Observed thread",
                        "thread_id": "thread-1",
                        "inferred_workstream": "acme",
                        "confidence": 0.7,
                        "confidence_source": "keyword",
                        "pm_confirmed": "true",
                        "promoted_to_workstreams_yaml": False,
                        "first_seen": "2026-05-20",
                        "last_seen": "2026-05-21",
                        "signal_yield_last_3": [1, 1, 1],
                        "topics": ["SCHIE"],
                    }
                ],
            },
            sort_keys=False,
            allow_unicode=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="pm_confirmed must be a boolean"):
        load_m365_registry("acme", programs_root)
