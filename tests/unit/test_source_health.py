from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.core.gather_state_store import GatherState
from src.core.slice_contract_loader import (
    SliceContract,
    SliceDecisionSource,
    SliceDecisionSourceSelector,
)
from src.core.source_health import SourceWaiver, build_slice_source_health_summary, build_slice_source_health_summary_for_legacy_compat_tests, source_health_function_name_for_edition
from tests.support.slice_contract_fixtures import build_test_source_health_slice_contract


def test_build_slice_source_health_summary_marks_hybrid_contract_healthy() -> None:
    summary = build_slice_source_health_summary(
        (_make_slice(source_of_truth="hybrid", fallback_sources=("lt_deck",)),),
        _gather_state(
            channels={
                "ado": {"active": True, "signal_count": 4},
                "workiq": {"active": True, "signal_count": 8, "expected_min": 8, "meets_expected_min": True},
            },
            query_states={"velocity-p50": {"last_cycle_succeeded": True, "row_count": 2, "data_age_hours": 4.0}},
        ),
        function_name="review",
    )

    assert summary is not None
    assert summary.function == "review"
    assert summary.contract_count == 1
    assert summary.healthy_contract_count == 1
    assert summary.unhealthy_roles == ()


def test_source_health_function_name_for_edition_maps_newsletter_families_and_preserves_other_functions() -> None:
    assert source_health_function_name_for_edition("detailed") == "newsletter"
    assert source_health_function_name_for_edition("focused") == "newsletter"
    assert source_health_function_name_for_edition("deck") == "deck"
    assert source_health_function_name_for_edition("lookback") == "review"
    assert source_health_function_name_for_edition("nudge") == "nudge"
    assert source_health_function_name_for_edition(None) == "newsletter"


def test_build_slice_source_health_summary_marks_missing_ado_binding_unbound() -> None:
    summary = build_slice_source_health_summary(
        (_make_slice(source_of_truth="ado_primary", include_ado=False, include_telemetry=False),),
        _gather_state(channels={"ado": {"active": True, "signal_count": 4}}),
    )

    assert summary is not None
    assert summary.healthy_contract_count == 0
    assert summary.unhealthy_roles[0].contract_id == "demo.slice"
    assert summary.unhealthy_roles[0].role == "system_of_record"
    assert summary.unhealthy_roles[0].state == "unbound"
    assert summary.unhealthy_roles[0].blocks_confirm is True


def test_build_slice_source_health_summary_marks_zero_row_telemetry_zero_yield() -> None:
    summary = build_slice_source_health_summary(
        (_make_slice(source_of_truth="telemetry_primary"),),
        _gather_state(
            channels={"ado": {"active": True, "signal_count": 4}},
            query_states={"velocity-p50": {"last_cycle_succeeded": True, "row_count": 0, "data_age_hours": 1.0, "zero_rows_ok": False}},
        ),
    )

    assert summary is not None
    assert summary.healthy_contract_count == 0
    assert summary.unhealthy_roles[0].role == "telemetry"
    assert summary.unhealthy_roles[0].state == "zero_yield"


def test_build_slice_source_health_summary_downgrades_optional_non_ado_roles_to_warnings() -> None:
    summary = build_slice_source_health_summary(
        (_make_slice(source_of_truth="telemetry_primary", required=False),),
        _gather_state(
            channels={"ado": {"active": True, "signal_count": 4}},
            query_states={"velocity-p50": {"last_cycle_succeeded": True, "row_count": 0, "data_age_hours": 1.0, "zero_rows_ok": False}},
        ),
    )

    assert summary is not None
    assert summary.healthy_contract_count == 0
    assert summary.unhealthy_roles[0].role == "telemetry"
    assert summary.unhealthy_roles[0].state == "zero_yield"
    assert summary.unhealthy_roles[0].blocks_confirm is False


def test_build_slice_source_health_summary_skips_hybrid_telemetry_role_for_nudge() -> None:
    summary = build_slice_source_health_summary(
        (_make_slice(source_of_truth="hybrid"),),
        _gather_state(channels={"ado": {"active": True, "signal_count": 4}}),
        function_name="nudge",
    )

    assert summary is not None
    assert summary.function == "nudge"
    assert summary.healthy_contract_count == 1
    assert all(role.role != "telemetry" for role in summary.role_healths)
    assert summary.unhealthy_roles == ()


def test_build_slice_source_health_summary_uses_review_contract_without_requiring_hybrid_telemetry() -> None:
    summary = build_slice_source_health_summary(
        (_make_slice(source_of_truth="hybrid", fallback_sources=("lt_deck",)),),
        _gather_state(
            channels={
                "ado": {"active": True, "signal_count": 4},
                "workiq": {"active": True, "signal_count": 8, "expected_min": 8, "meets_expected_min": True},
            }
        ),
        function_name="review",
    )

    assert summary is not None
    assert summary.function == "review"
    assert summary.healthy_contract_count == 1
    assert {role.role for role in summary.role_healths} == {"system_of_record", "decision"}
    assert summary.unhealthy_roles == ()


def test_build_slice_source_health_summary_uses_structured_decision_source_bindings() -> None:
    summary = build_slice_source_health_summary(
        (
            _make_slice(
                source_of_truth="ado_primary",
                fallback_sources=("custom_review",),
                decision_sources=(
                    SliceDecisionSource(
                        source_id="custom_review",
                        channels=("custom_collab",),
                        blocked_artifact_selectors=(
                            SliceDecisionSourceSelector(workstream_id="custom_ws", artifact_type="meeting_series"),
                        ),
                    ),
                ),
            ),
        ),
        _gather_state(
            channels={
                "ado": {"active": True, "signal_count": 4},
                "custom_collab": {"active": True, "signal_count": 8, "expected_min": 8, "meets_expected_min": True},
            },
            m365_discovery={
                "active": True,
                "promotion_blocked_missing_id_count": 1,
                "promotion_blocked_missing_id_artifacts": [
                    {
                        "artifact_id": "meet:custom-review",
                        "artifact_type": "meeting_series",
                        "inferred_workstream": "custom_ws",
                    }
                ],
            },
        ),
        function_name="review",
    )

    assert summary is not None
    assert any(role.role == "decision" and role.state == "stale" for role in summary.unhealthy_roles)


def test_build_slice_source_health_summary_requires_bound_decision_sources_for_deck() -> None:
    summary = build_slice_source_health_summary(
        (_make_slice(source_of_truth="ado_primary", fallback_sources=()),),
        _gather_state(channels={"ado": {"active": True, "signal_count": 4}}),
        function_name="deck",
    )

    assert summary is not None
    assert summary.healthy_contract_count == 0
    assert any(role.role == "decision" and role.state == "unbound" for role in summary.unhealthy_roles)


def test_build_slice_source_health_summary_marks_deck_decision_sources_healthy_when_channel_meets_expected_min() -> None:
    summary = build_slice_source_health_summary(
        (_make_slice(source_of_truth="ado_primary", fallback_sources=("lt_deck", "program_b_daily")),),
        _gather_state(
            channels={
                "ado": {"active": True, "signal_count": 4},
                "workiq": {"active": True, "signal_count": 6, "expected_min": 4, "meets_expected_min": True},
                "transcript": {"active": True, "signal_count": 0, "expected_min": 2, "meets_expected_min": False},
            }
        ),
        function_name="deck",
    )

    assert summary is not None
    decision_roles = tuple(role for role in summary.role_healths if role.role == "decision")
    assert len(decision_roles) == 1
    assert decision_roles[0].state == "healthy"
    assert decision_roles[0].last_yield == 6


def test_build_slice_source_health_summary_marks_deck_decision_sources_stale_when_only_partial_collaboration_yield_exists() -> None:
    summary = build_slice_source_health_summary(
        (_make_slice(source_of_truth="ado_primary", fallback_sources=("lt_deck",)),),
        _gather_state(
            channels={
                "ado": {"active": True, "signal_count": 4},
                "workiq": {"active": True, "signal_count": 2, "expected_min": 8, "meets_expected_min": False},
            }
        ),
        function_name="deck",
    )

    assert summary is not None
    assert any(role.role == "decision" and role.state == "stale" for role in summary.unhealthy_roles)


def test_build_slice_source_health_summary_requires_structured_decision_sources_for_deck_by_default() -> None:
    summary = build_slice_source_health_summary(
        (
            _make_slice(
                source_of_truth="ado_primary",
                fallback_sources=("lt_deck",),
                populate_structured_decision_sources=False,
            ),
        ),
        _gather_state(
            channels={
                "workiq": {
                    "active": True,
                    "signal_count": 8,
                    "expected_min": 8,
                    "meets_expected_min": True,
                }
            }
        ),
        function_name="deck",
    )

    assert summary is not None
    assert summary.unhealthy_roles[0].state == "unbound"


def test_build_slice_source_health_summary_treats_empty_structured_decision_channels_as_unbound() -> None:
    summary = build_slice_source_health_summary(
        (
            _make_slice(
                source_of_truth="ado_primary",
                fallback_sources=("lt_deck",),
                decision_sources=(
                    SliceDecisionSource(source_id="lt_deck", channels=()),
                ),
                populate_structured_decision_sources=False,
            ),
        ),
        _gather_state(
            channels={
                "workiq": {
                    "active": True,
                    "signal_count": 8,
                    "expected_min": 8,
                    "meets_expected_min": True,
                }
            }
        ),
        function_name="deck",
    )

    assert summary is not None
    assert summary.unhealthy_roles[0].state == "unbound"


def test_build_slice_source_health_summary_uses_legacy_decision_channel_fallback_only_when_enabled() -> None:
    summary = build_slice_source_health_summary_for_legacy_compat_tests(
        (
            _make_slice(
                source_of_truth="ado_primary",
                fallback_sources=("lt_deck",),
                populate_structured_decision_sources=False,
            ),
        ),
        _gather_state(
            channels={
                "ado": {"active": True, "signal_count": 4},
                "workiq": {"active": True, "signal_count": 2, "expected_min": 8, "meets_expected_min": False},
            }
        ),
        function_name="deck",
    )

    assert summary is not None
    assert any(role.role == "decision" and role.state == "stale" for role in summary.unhealthy_roles)


def test_build_slice_source_health_summary_uses_legacy_fallback_when_structured_decision_channels_are_empty() -> None:
    summary = build_slice_source_health_summary_for_legacy_compat_tests(
        (
            _make_slice(
                source_of_truth="ado_primary",
                fallback_sources=("lt_deck",),
                decision_sources=(
                    SliceDecisionSource(source_id="lt_deck", channels=()),
                ),
                populate_structured_decision_sources=False,
            ),
        ),
        _gather_state(
            channels={
                "ado": {"active": True, "signal_count": 4},
                "workiq": {"active": True, "signal_count": 2, "expected_min": 8, "meets_expected_min": False},
            }
        ),
        function_name="deck",
    )

    assert summary is not None
    assert any(role.role == "decision" and role.state == "stale" for role in summary.unhealthy_roles)


def test_build_slice_source_health_summary_skips_legacy_channel_fallback_when_structured_bindings_exist() -> None:
    summary = build_slice_source_health_summary_for_legacy_compat_tests(
        (
            _make_slice(
                source_of_truth="ado_primary",
                fallback_sources=("lt_deck", "contoso_daily"),
                decision_sources=(
                    SliceDecisionSource(
                        source_id="lt_deck",
                        channels=("workiq",),
                    ),
                ),
                populate_structured_decision_sources=False,
            ),
        ),
        _gather_state(
            channels={
                "ado": {"active": True, "signal_count": 4},
                "workiq": {"active": False, "signal_count": 0},
                "transcript": {"active": True, "signal_count": 8, "expected_min": 8, "meets_expected_min": True},
            }
        ),
        function_name="deck",
    )

    assert summary is not None
    assert any(role.role == "decision" and role.state == "auth_failed" for role in summary.unhealthy_roles)


def test_build_slice_source_health_summary_uses_legacy_blocked_id_fallback_only_when_enabled() -> None:
    summary = build_slice_source_health_summary_for_legacy_compat_tests(
        (
            _make_slice(
                source_of_truth="ado_primary",
                fallback_sources=("lt_deck",),
                populate_structured_decision_sources=False,
            ),
        ),
        _gather_state(
            channels={
                "ado": {"active": True, "signal_count": 4},
                "workiq": {"active": True, "signal_count": 8, "expected_min": 8, "meets_expected_min": True},
            },
            m365_discovery={
                "active": True,
                "promotion_blocked_missing_id_count": 1,
                "promotion_blocked_missing_id_ids": ["meet:acme-acme-weekly-ops-review"],
            },
        ),
        function_name="deck",
    )

    assert summary is not None
    assert any(role.role == "decision" and role.state == "stale" for role in summary.unhealthy_roles)


def test_build_slice_source_health_summary_skips_legacy_blocked_id_fallback_when_structured_artifacts_are_present() -> None:
    summary = build_slice_source_health_summary_for_legacy_compat_tests(
        (
            _make_slice(
                source_of_truth="ado_primary",
                fallback_sources=("lt_deck",),
                populate_structured_decision_sources=False,
            ),
        ),
        _gather_state(
            channels={
                "ado": {"active": True, "signal_count": 4},
                "workiq": {"active": True, "signal_count": 8, "expected_min": 8, "meets_expected_min": True},
            },
            m365_discovery={
                "active": True,
                "promotion_blocked_missing_id_count": 1,
                "promotion_blocked_missing_id_ids": ["meet:acme-acme-weekly-ops-review"],
                "promotion_blocked_missing_id_artifacts": [
                    {
                        "artifact_id": "meet:acme-contoso-weekly-review",
                        "artifact_type": "meeting_series",
                        "inferred_workstream": "dd_on_pf",
                    }
                ],
            },
        ),
        function_name="deck",
    )

    assert summary is not None
    assert not any(role.role == "decision" and role.state == "stale" for role in summary.unhealthy_roles)


def test_build_slice_source_health_summary_marks_deck_decision_sources_stale_when_discovery_blocks_promotion() -> None:
    summary = build_slice_source_health_summary(
        (_make_slice(source_of_truth="ado_primary", fallback_sources=("lt_deck",)),),
        _gather_state(
            channels={
                "ado": {"active": True, "signal_count": 4},
                "workiq": {"active": True, "signal_count": 8, "expected_min": 8, "meets_expected_min": True},
            },
            m365_discovery={
                "active": True,
                "promotion_blocked_missing_id_count": 1,
                "promotion_blocked_missing_id_ids": ["meet:acme-acme-weekly-ops-review"],
                "promotion_blocked_missing_id_artifacts": [
                    {
                        "artifact_id": "meet:acme-acme-weekly-ops-review",
                        "artifact_type": "meeting_series",
                        "inferred_workstream": "acme",
                    }
                ],
            },
        ),
        function_name="deck",
    )

    assert summary is not None
    assert any(role.role == "decision" and role.state == "stale" for role in summary.unhealthy_roles)


def test_build_slice_source_health_summary_keeps_deck_decision_healthy_when_only_other_fallback_ids_are_blocked() -> None:
    summary = build_slice_source_health_summary(
        (_make_slice(source_of_truth="ado_primary", fallback_sources=("lt_deck",)),),
        _gather_state(
            channels={
                "ado": {"active": True, "signal_count": 4},
                "workiq": {"active": True, "signal_count": 8, "expected_min": 8, "meets_expected_min": True},
            },
            m365_discovery={
                "active": True,
                "promotion_blocked_missing_id_count": 1,
                "promotion_blocked_missing_id_ids": ["meet:acme-contoso-weekly-review"],
                "promotion_blocked_missing_id_artifacts": [
                    {
                        "artifact_id": "meet:acme-contoso-weekly-review",
                        "artifact_type": "meeting_series",
                        "inferred_workstream": "dd_on_pf",
                    }
                ],
            },
        ),
        function_name="deck",
    )

    assert summary is not None
    assert summary.healthy_contract_count == 1
    assert summary.unhealthy_roles == ()


def test_build_slice_source_health_summary_attaches_active_waiver_without_masking_state() -> None:
    summary = build_slice_source_health_summary(
        (_make_slice(source_of_truth="telemetry_primary"),),
        _gather_state(
            channels={"ado": {"active": True, "signal_count": 4}},
            query_states={"velocity-p50": {"last_cycle_succeeded": True, "row_count": 2, "data_age_hours": 48.0}},
        ),
        waivers=(
            SourceWaiver(
                contract_id="demo.slice",
                role="telemetry",
                owner="owner@example.com",
                reason="Known telemetry delay during cutover.",
                granted=(datetime.now(timezone.utc) - timedelta(days=30)).date(),
                expires=(datetime.now(timezone.utc) + timedelta(days=30)).date(),
            ),
        ),
    )

    assert summary is not None
    assert summary.healthy_contract_count == 0
    assert summary.waived_contract_count == 1
    assert summary.unhealthy_roles[0].state == "stale"
    assert summary.unhealthy_roles[0].waiver is not None
    assert summary.unhealthy_roles[0].blocks_confirm is False


def test_build_slice_source_health_summary_does_not_waive_unbound_roles() -> None:
    summary = build_slice_source_health_summary(
        (_make_slice(source_of_truth="ado_primary", include_ado=False, include_telemetry=False),),
        _gather_state(channels={"ado": {"active": True, "signal_count": 4}}),
        waivers=(
            SourceWaiver(
                contract_id="demo.slice",
                role="system_of_record",
                owner="owner@example.com",
                reason="Attempted waiver should not hide missing bindings.",
                granted=datetime(2026, 5, 1, tzinfo=timezone.utc).date(),
                expires=datetime(2026, 6, 30, tzinfo=timezone.utc).date(),
            ),
        ),
    )

    assert summary is not None
    assert summary.waived_contract_count == 0
    assert summary.unhealthy_roles[0].state == "unbound"
    assert summary.unhealthy_roles[0].waiver is None
    assert summary.unhealthy_roles[0].blocks_confirm is True


def _make_slice(
    *,
    source_of_truth: str,
    include_ado: bool = True,
    include_telemetry: bool = True,
    fallback_sources: tuple[str, ...] = (),
    decision_sources: tuple[SliceDecisionSource, ...] = (),
    populate_structured_decision_sources: bool = True,
    required: bool = True,
) -> SliceContract:
    return build_test_source_health_slice_contract(
        source_of_truth=source_of_truth,
        include_ado=include_ado,
        include_telemetry=include_telemetry,
        fallback_sources=fallback_sources,
        decision_sources=decision_sources,
        populate_structured_decision_sources=populate_structured_decision_sources,
        required=required,
    )


def _gather_state(
    *,
    channels: dict[str, dict[str, object]],
    query_states: dict[str, dict[str, object]] | None = None,
    m365_discovery: dict[str, object] | None = None,
    program_id: str = "acme",
) -> GatherState:
    return GatherState(
        program_id=program_id,
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
        query_states=query_states or {},
        channels=channels,
        m365_discovery=m365_discovery or {},
    )
