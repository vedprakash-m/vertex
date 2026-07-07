"""Unit tests for TeamsDiscoveryProvider."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.core.integration_types import (
    ChannelConfig,
    DiscoveryCompleteness,
    RunContext,
)
from src.core.models_v2 import (
    Program,
    TeamsMeetingSeries,
    TeamsChat,
    Workstream,
    WorkstreamSignalSources,
)
from src.m365.teams_discovery import TeamsDiscoveryConfig, TeamsDiscoveryProvider


_NOW = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)

_CHANNEL_CONFIG = ChannelConfig(
    channel="teams",
    enabled=True,
    discovery_threshold_hours=48,
    ttl_days=7,
)

_RUN_CTX = RunContext(dry_run=False, force_discovery=False, accept_shrinkage=False)


def _make_workstream(
    *,
    series_id: str | None = None,
    thread_id: str | None = None,
    meeting_work_item_ids: tuple[int, ...] = (),
    chat_work_item_ids: tuple[int, ...] = (),
    keywords: tuple[str, ...] = (),
) -> Workstream:
    series = ()
    chats = ()
    if series_id is not None:
        series = (
            TeamsMeetingSeries(
                display_name="Weekly Sync",
                series_id=series_id,
                work_item_ids=meeting_work_item_ids,
            ),
        )
    if thread_id is not None:
        chats = (
            TeamsChat(
                display_name="Deployment Chat",
                thread_id=thread_id,
                work_item_ids=chat_work_item_ids,
            ),
        )
    sources = WorkstreamSignalSources(
        teams_meeting_series=series,
        teams_chats=chats,
        workiq_keywords=keywords,
    )
    return Workstream(id="ws-a", name="WS A", signal_sources=sources)


def _make_provider(
    workstreams: tuple[Workstream, ...] = (),
    workiq_enabled: bool = False,
) -> tuple[TeamsDiscoveryProvider, TeamsDiscoveryConfig]:
    mock_calendar = MagicMock()
    mock_reader = MagicMock()
    config = TeamsDiscoveryConfig(
        workstreams=workstreams,
        workiq_keywords=(),
        workiq_enabled=workiq_enabled,
    )
    return TeamsDiscoveryProvider(mock_calendar, mock_reader), config


class TestTeamsDiscoveryStaticScope:
    def test_discovers_configured_meeting_series(self) -> None:
        ws = _make_workstream(series_id="series-abc", meeting_work_item_ids=(12345,))
        provider, config = _make_provider(workstreams=(ws,))

        result = provider.discover("prog1", config, (), run_ctx=_RUN_CTX)

        refs = [r for r in result.discovered_refs if r.registration.ref_kind == "meeting_series"]
        assert len(refs) == 1
        assert refs[0].registration.ref_id == "series-abc"
        assert refs[0].registration.confidence == 1.0
        assert refs[0].registration.work_item_ids == (12345,)

    def test_discovers_configured_teams_chat(self) -> None:
        ws = _make_workstream(thread_id="thread-xyz", chat_work_item_ids=(67890,))
        provider, config = _make_provider(workstreams=(ws,))

        result = provider.discover("prog1", config, (), run_ctx=_RUN_CTX)

        refs = [r for r in result.discovered_refs if r.registration.ref_kind == "teams_chat"]
        assert len(refs) == 1
        assert refs[0].registration.ref_id == "thread-xyz"
        assert refs[0].registration.confidence == 1.0
        assert refs[0].registration.work_item_ids == (67890,)

    def test_skips_series_without_series_id(self) -> None:
        ws = _make_workstream(series_id=None, thread_id=None)
        provider, config = _make_provider(workstreams=(ws,))

        result = provider.discover("prog1", config, (), run_ctx=_RUN_CTX)

        assert result.discovered_refs == ()

    def test_completeness_is_incremental_overall(self) -> None:
        ws = _make_workstream(series_id="series-abc")
        provider, config = _make_provider(workstreams=(ws,))

        result = provider.discover("prog1", config, (), run_ctx=_RUN_CTX)

        # Overall completeness is INCREMENTAL because WorkIQ scope is INCREMENTAL
        assert result.completeness == DiscoveryCompleteness.INCREMENTAL

    def test_static_scope_completeness_is_full(self) -> None:
        ws = _make_workstream(series_id="series-abc")
        provider, config = _make_provider(workstreams=(ws,))

        result = provider.discover("prog1", config, (), run_ctx=_RUN_CTX)

        from src.m365.teams_discovery import _STATIC_SCOPE_ID
        assert result.scope_statuses[_STATIC_SCOPE_ID].completeness == DiscoveryCompleteness.FULL

    def test_empty_workstreams_returns_empty_result(self) -> None:
        provider, config = _make_provider(workstreams=())

        result = provider.discover("prog1", config, (), run_ctx=_RUN_CTX)

        assert result.discovered_refs == ()
        assert result.errors == ()

    def test_program_id_set_on_all_registrations(self) -> None:
        ws = _make_workstream(series_id="series-abc", thread_id="thread-xyz")
        provider, config = _make_provider(workstreams=(ws,))

        result = provider.discover("prog1", config, (), run_ctx=_RUN_CTX)

        for ref in result.discovered_refs:
            assert ref.registration.program_id == "prog1"


class TestTeamsDiscoveryWorkIQScope:
    def test_workiq_disabled_returns_no_workiq_refs(self) -> None:
        ws = _make_workstream(keywords=("azure storage",))
        provider, config = _make_provider(workstreams=(ws,), workiq_enabled=False)
        # Override config to have keywords
        config = TeamsDiscoveryConfig(
            workstreams=(ws,),
            workiq_keywords=("azure storage",),
            workiq_enabled=False,
        )

        result = provider.discover("prog1", config, (), run_ctx=_RUN_CTX)

        # No WorkIQ refs because disabled
        workiq_refs = [r for r in result.discovered_refs if r.registration.confidence_source == "workiq_search"]
        assert workiq_refs == []

    def test_workiq_error_results_in_error_scope_status(self) -> None:
        from src.m365.teams_discovery import _WORKIQ_SCOPE_ID
        from src.core.integration_types import ScopeStatusKind

        mock_reader = MagicMock()
        mock_reader.search_messages.side_effect = Exception("WorkIQ unavailable")
        config = TeamsDiscoveryConfig(
            workstreams=(),
            workiq_keywords=("azure",),
            workiq_enabled=True,
        )
        provider = TeamsDiscoveryProvider(MagicMock(), mock_reader)

        result = provider.discover("prog1", config, (), run_ctx=_RUN_CTX)

        assert result.scope_statuses[_WORKIQ_SCOPE_ID].status == ScopeStatusKind.ERROR
        assert len(result.errors) == 1


def _make_workstream_with_named_series(
    display_name: str,
    *,
    series_id: str | None = None,
    keywords: tuple[str, ...] = (),
) -> Workstream:
    """Create a workstream that has a meeting series entry with the given display_name.

    When ``series_id`` is None the series is configured with only a display_name,
    which is the scenario that triggers auto-resolution.
    """
    sources = WorkstreamSignalSources(
        teams_meeting_series=(
            TeamsMeetingSeries(display_name=display_name, series_id=series_id),
        ),
        workiq_keywords=keywords,
    )
    return Workstream(id="ws-auto", name="WS Auto", signal_sources=sources)


def _make_provider_with_resolver(
    workstreams: tuple[Workstream, ...],
    resolver_return: tuple[str, float] | None,
) -> tuple[TeamsDiscoveryProvider, TeamsDiscoveryConfig]:
    """Build a provider whose series_id_resolver is a mock returning ``resolver_return``."""
    mock_resolver = MagicMock(return_value=resolver_return)
    config = TeamsDiscoveryConfig(
        workstreams=workstreams,
        workiq_keywords=(),
        workiq_enabled=False,
    )
    provider = TeamsDiscoveryProvider(
        MagicMock(),
        MagicMock(),
        series_id_resolver=mock_resolver,
    )
    return provider, config


class TestSeriesIdAutoResolution:
    """Auto-discovery of series_ids when only display_name is configured."""

    def test_exact_match_populates_series_id(self) -> None:
        """When resolver returns a high-confidence ID, the ref is emitted."""
        ws = _make_workstream_with_named_series("Weekly Ops Review")
        provider, config = _make_provider_with_resolver(
            (ws,),
            resolver_return=("series-auto-001", 1.0),
        )

        result = provider.discover("prog1", config, (), run_ctx=_RUN_CTX)

        meeting_refs = [r for r in result.discovered_refs if r.registration.ref_kind == "meeting_series"]
        assert len(meeting_refs) == 1
        assert meeting_refs[0].registration.ref_id == "series-auto-001"
        assert meeting_refs[0].registration.confidence_source == "auto_resolved"
        assert meeting_refs[0].registration.confidence == 1.0

    def test_auto_resolved_ref_is_not_pm_confirmed(self) -> None:
        """Auto-resolved refs must have pm_confirmed=False in bindings."""
        ws = _make_workstream_with_named_series("Ops Review")
        provider, config = _make_provider_with_resolver(
            (ws,),
            resolver_return=("series-auto-002", 0.92),
        )

        result = provider.discover("prog1", config, (), run_ctx=_RUN_CTX)

        meeting_refs = [r for r in result.discovered_refs if r.registration.ref_kind == "meeting_series"]
        assert len(meeting_refs) == 1
        assert all(not b.pm_confirmed for b in meeting_refs[0].bindings)

    def test_low_confidence_resolver_result_skips_entry(self) -> None:
        """When resolver returns None (low confidence / ambiguous), the series is skipped."""
        ws = _make_workstream_with_named_series("Ambiguous Meeting")
        provider, config = _make_provider_with_resolver(
            (ws,),
            resolver_return=None,
        )

        result = provider.discover("prog1", config, (), run_ctx=_RUN_CTX)

        meeting_refs = [r for r in result.discovered_refs if r.registration.ref_kind == "meeting_series"]
        assert meeting_refs == []

    def test_no_resolver_skips_missing_series_id(self) -> None:
        """Without a resolver the provider falls back to the original skip behaviour."""
        ws = _make_workstream_with_named_series("No Resolver Meeting")
        # Provider has no resolver (None)
        config = TeamsDiscoveryConfig(workstreams=(ws,), workiq_keywords=(), workiq_enabled=False)
        provider = TeamsDiscoveryProvider(MagicMock(), MagicMock(), series_id_resolver=None)

        result = provider.discover("prog1", config, (), run_ctx=_RUN_CTX)

        meeting_refs = [r for r in result.discovered_refs if r.registration.ref_kind == "meeting_series"]
        assert meeting_refs == []

    def test_configured_series_id_bypasses_resolver(self) -> None:
        """When series_id is already set, the resolver is never called."""
        ws = _make_workstream_with_named_series("Pre-configured Review", series_id="series-static-999")
        mock_resolver = MagicMock(return_value=("series-should-not-use", 1.0))
        config = TeamsDiscoveryConfig(workstreams=(ws,), workiq_keywords=(), workiq_enabled=False)
        provider = TeamsDiscoveryProvider(MagicMock(), MagicMock(), series_id_resolver=mock_resolver)

        result = provider.discover("prog1", config, (), run_ctx=_RUN_CTX)

        meeting_refs = [r for r in result.discovered_refs if r.registration.ref_kind == "meeting_series"]
        assert len(meeting_refs) == 1
        assert meeting_refs[0].registration.ref_id == "series-static-999"
        assert meeting_refs[0].registration.confidence == 1.0
        assert meeting_refs[0].registration.confidence_source == "static_config"
        mock_resolver.assert_not_called()

    def test_resolver_exception_is_swallowed_and_series_skipped(self) -> None:
        """A crashing resolver must not propagate — the series is simply skipped."""
        ws = _make_workstream_with_named_series("Crash Meeting")
        mock_resolver = MagicMock(side_effect=RuntimeError("network timeout"))
        config = TeamsDiscoveryConfig(workstreams=(ws,), workiq_keywords=(), workiq_enabled=False)
        provider = TeamsDiscoveryProvider(MagicMock(), MagicMock(), series_id_resolver=mock_resolver)

        result = provider.discover("prog1", config, (), run_ctx=_RUN_CTX)

        # No exception raised, and no refs (resolver failed)
        meeting_refs = [r for r in result.discovered_refs if r.registration.ref_kind == "meeting_series"]
        assert meeting_refs == []
        assert result.errors == ()  # resolver errors don't surface as IntegrationErrors

    def test_auto_resolved_metadata_flag(self) -> None:
        """The auto_resolved metadata flag is True for auto-resolved refs."""
        ws = _make_workstream_with_named_series("Ramp Weekly Sync")
        provider, config = _make_provider_with_resolver(
            (ws,),
            resolver_return=("series-ramp-001", 0.95),
        )

        result = provider.discover("prog1", config, (), run_ctx=_RUN_CTX)

        meeting_refs = [r for r in result.discovered_refs if r.registration.ref_kind == "meeting_series"]
        assert len(meeting_refs) == 1
        assert meeting_refs[0].registration.metadata.get("auto_resolved") is True

    def test_static_metadata_flag_false_for_preconfigured(self) -> None:
        """The auto_resolved metadata flag is False when series_id was already configured."""
        ws = _make_workstream_with_named_series("Static Meeting", series_id="series-static-001")
        config = TeamsDiscoveryConfig(workstreams=(ws,), workiq_keywords=(), workiq_enabled=False)
        provider = TeamsDiscoveryProvider(MagicMock(), MagicMock(), series_id_resolver=None)

        result = provider.discover("prog1", config, (), run_ctx=_RUN_CTX)

        meeting_refs = [r for r in result.discovered_refs if r.registration.ref_kind == "meeting_series"]
        assert len(meeting_refs) == 1
        assert meeting_refs[0].registration.metadata.get("auto_resolved") is False


class TestTeamsDiscoveryChannel:
    def test_channel_name(self) -> None:
        provider, _ = _make_provider()
        assert provider.channel == "teams"

    def test_from_program_creates_provider_and_config(self) -> None:
        with patch("src.m365.teams_discovery.AgencyBridge"), \
             patch("src.m365.teams_discovery.GraphCalendarClient"), \
             patch("src.m365.teams_discovery.TeamsReader"):
            program = Program(schema_version="3.0", id="prog1", name="Prog")
            provider, config = TeamsDiscoveryProvider.from_program(
                program, _CHANNEL_CONFIG, (), programs_root=MagicMock()
            )
            assert isinstance(provider, TeamsDiscoveryProvider)
            assert isinstance(config, TeamsDiscoveryConfig)
