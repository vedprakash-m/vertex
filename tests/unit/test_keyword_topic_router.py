from __future__ import annotations

from src.core.keyword_topic_router import KeywordM365TopicRouter, route_m365_artifact
from src.core.m365_router_interface import M365ReassignCorrection
from src.core.models_v2 import Workstream, WorkstreamSignalSources


def test_route_m365_artifact_uses_participant_aliases_for_owner_bonus() -> None:
    workstreams = (
        Workstream(id="acme", name="Store rollout", pm_owner="operator"),
        Workstream(id="contoso", name="Device delivery", pm_owner="priya"),
    )

    decision = route_m365_artifact(
        display_name="Weekly sync",
        subject_or_title="General status follow-up",
        participant_aliases=("priya",),
        sample_text="No workstream keywords were mentioned explicitly.",
        workstreams=workstreams,
    )

    assert decision.workstream_id == "contoso"
    assert decision.confidence == 0.15
    assert "participant aliases ('priya',)" in decision.reasoning


def test_keyword_topic_router_combines_keyword_and_participant_signals() -> None:
    router = KeywordM365TopicRouter()
    workstreams = (
        Workstream(
            id="acme",
            name="Store rollout",
            pm_owner="operator",
            signal_sources=WorkstreamSignalSources(workiq_keywords=("schie",)),
        ),
        Workstream(
            id="contoso",
            name="Device delivery",
            pm_owner="priya",
            signal_sources=WorkstreamSignalSources(workiq_keywords=("firmware",)),
        ),
    )

    decision = router.route_artifact(
        display_name="Firmware follow-up",
        subject_or_title="DD pilot status",
        participant_aliases=("priya",),
        sample_text="Firmware sign-off remains the blocking item for the DD pilot.",
        workstream_profiles=workstreams,
        recent_confirmed_signals=None,
    )

    assert decision.workstream_id == "contoso"
    assert decision.confidence > 0.45
    assert "participant aliases ('priya',)" in decision.reasoning
    assert "Matched keywords ('firmware',)" in decision.reasoning


def test_route_m365_artifact_uses_workstream_aliases_for_owner_bonus() -> None:
    workstreams = (
        Workstream(id="acme", name="Store rollout", aliases=("lidavidson",)),
        Workstream(id="contoso", name="Device delivery"),
    )

    decision = route_m365_artifact(
        display_name="Weekly sync",
        subject_or_title="General status follow-up",
        participant_aliases=("lidavidson",),
        sample_text="No workstream keywords were mentioned explicitly.",
        workstreams=workstreams,
    )

    assert decision.workstream_id == "acme"
    assert decision.confidence == 0.15
    assert "participant aliases ('lidavidson',)" in decision.reasoning


def test_route_m365_artifact_uses_responsible_owner_aliases_for_owner_bonus() -> None:
    workstreams = (
        Workstream(id="acme", name="Store rollout", responsible_owners=("operator",)),
        Workstream(id="contoso", name="Device delivery", responsible_owners=("priya",)),
    )

    decision = route_m365_artifact(
        display_name="Weekly sync",
        subject_or_title="General status follow-up",
        participant_aliases=("priya",),
        sample_text="No workstream keywords were mentioned explicitly.",
        workstreams=workstreams,
    )

    assert decision.workstream_id == "contoso"
    assert decision.confidence == 0.15
    assert "participant aliases ('priya',)" in decision.reasoning


def test_route_m365_artifact_uses_area_path_anchors_to_override_weaker_keyword_match() -> None:
    workstreams = (
        Workstream(
            id="acme",
            name="Store rollout",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(workiq_keywords=("ramp",)),
        ),
        Workstream(
            id="contoso",
            name="Device delivery",
            area_paths=("One\\Adventure\\Contoso\\Networking",),
        ),
    )

    decision = route_m365_artifact(
        display_name="Ramp review",
        subject_or_title="Contoso networking blockers",
        participant_aliases=(),
        sample_text="One\\Adventure\\Contoso\\Networking remains blocked for the pilot.",
        workstreams=workstreams,
    )

    assert decision.workstream_id == "contoso"
    assert decision.confidence > 0.57
    assert "area-path anchors" in decision.reasoning
    assert "contoso networking" in decision.reasoning


def test_route_m365_artifact_uses_rejected_feedback_to_penalize_stale_match() -> None:
    workstreams = (
        Workstream(
            id="acme",
            name="Store rollout",
            area_paths=("One\\Adventure\\Acme",),
            signal_sources=WorkstreamSignalSources(workiq_keywords=("ramp", "planning")),
        ),
        Workstream(
            id="contoso",
            name="Device delivery",
            area_paths=("One\\Adventure\\Contoso\\Networking",),
        ),
    )

    baseline = route_m365_artifact(
        display_name="Ramp finance planning review",
        subject_or_title="Contoso networking blockers",
        participant_aliases=(),
        sample_text="One\\Adventure\\Contoso\\Networking remains blocked while finance planning is discussed.",
        workstreams=workstreams,
    )

    decision = route_m365_artifact(
        display_name="Ramp finance planning review",
        subject_or_title="Contoso networking blockers",
        participant_aliases=(),
        sample_text="One\\Adventure\\Contoso\\Networking remains blocked while finance planning is discussed.",
        workstreams=workstreams,
        recent_rejected_signals_by_workstream={
            "acme": (
                "Ramp finance planning follow-up",
                "Finance planning was rejected as off-topic for store rollout.",
            )
        },
    )

    assert baseline.workstream_id == "acme"
    assert decision.workstream_id == "contoso"


def test_route_m365_artifact_weights_repeated_confirmed_feedback() -> None:
    workstreams = (
        Workstream(id="acme", name="Store rollout"),
        Workstream(id="contoso", name="Device delivery"),
    )

    low_reinforcement = route_m365_artifact(
        display_name="Firmware sign-off follow-up",
        subject_or_title="DD pilot readiness",
        participant_aliases=(),
        sample_text="Firmware sign-off remains the blocking item for the DD pilot.",
        workstreams=workstreams,
        recent_confirmed_signals_by_workstream={
            "contoso": (
                "Firmware sign-off remains the blocking item for the DD pilot.",
                "Firmware sign-off remains the blocking item for the DD pilot.",
            )
        },
    )

    high_reinforcement = route_m365_artifact(
        display_name="Firmware sign-off follow-up",
        subject_or_title="DD pilot readiness",
        participant_aliases=(),
        sample_text="Firmware sign-off remains the blocking item for the DD pilot.",
        workstreams=workstreams,
        recent_confirmed_signals_by_workstream={
            "contoso": (
                "Firmware sign-off remains the blocking item for the DD pilot.",
                "Firmware sign-off remains the blocking item for the DD pilot.",
                "Firmware sign-off remains the blocking item for the DD pilot.",
                "Firmware sign-off remains the blocking item for the DD pilot.",
            )
        },
    )

    assert low_reinforcement.workstream_id == "contoso"
    assert high_reinforcement.workstream_id == "contoso"
    assert high_reinforcement.confidence > low_reinforcement.confidence
    assert "learned phrase evidence hits" in high_reinforcement.reasoning


def test_route_m365_artifact_weights_repeated_rejected_feedback() -> None:
    workstreams = (
        Workstream(
            id="acme",
            name="Store rollout",
            signal_sources=WorkstreamSignalSources(workiq_keywords=("planning",)),
        ),
    )

    single_rejection = route_m365_artifact(
        display_name="Finance planning follow-up",
        subject_or_title="Planning check-in",
        participant_aliases=(),
        sample_text="Finance planning remains under discussion.",
        workstreams=workstreams,
        recent_rejected_signals_by_workstream={
            "acme": (
                "Finance planning was rejected as off-topic for store rollout.",
            )
        },
    )

    repeated_rejection = route_m365_artifact(
        display_name="Finance planning follow-up",
        subject_or_title="Planning check-in",
        participant_aliases=(),
        sample_text="Finance planning remains under discussion.",
        workstreams=workstreams,
        recent_rejected_signals_by_workstream={
            "acme": (
                "Finance planning was rejected as off-topic for store rollout.",
                "Finance planning was rejected as off-topic for store rollout.",
                "Finance planning was rejected as off-topic for store rollout.",
            )
        },
    )

    assert repeated_rejection.workstream_id == "acme"
    assert repeated_rejection.confidence < single_rejection.confidence
    assert "learned exclusion evidence hits" in repeated_rejection.reasoning


def test_route_m365_artifact_uses_structured_reassign_corrections_to_override_stale_keyword_match() -> None:
    workstreams = (
        Workstream(
            id="acme",
            name="Store rollout",
            signal_sources=WorkstreamSignalSources(workiq_keywords=("pilot",)),
        ),
        Workstream(
            id="contoso",
            name="Device delivery",
            area_paths=("One\\Adventure\\Contoso",),
        ),
    )

    baseline = route_m365_artifact(
        display_name="DD pilot readiness thread",
        subject_or_title="Pilot readiness follow-up",
        participant_aliases=(),
        sample_text="DD pilot execution remains blocked and needs follow-up.",
        workstreams=workstreams,
    )

    decision = route_m365_artifact(
        display_name="DD pilot readiness thread",
        subject_or_title="Pilot readiness follow-up",
        participant_aliases=(),
        sample_text="DD pilot execution remains blocked and needs follow-up.",
        workstreams=workstreams,
        recent_reassign_corrections_by_workstream={
            "contoso": (
                M365ReassignCorrection(
                    prior_workstream_id="acme",
                    corrected_workstream_id="contoso",
                    artifact_display_name="DD pilot readiness thread",
                    reason="Belongs with DD pilot execution.",
                ),
            )
        },
    )

    assert baseline.workstream_id == "acme"
    assert decision.workstream_id == "contoso"
    assert "structured reassign corrections" in decision.reasoning
