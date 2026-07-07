from __future__ import annotations

from datetime import datetime, timezone

from src.core.ado_pr_client import PullRequestSummary
from src.core.ado_signal_extractor import ADOSignalExtractor
from src.core.engms_signal_extractor import EngMsSignalExtractor
from src.core.icm_signal_extractor import IcMSignalExtractor
from src.core.integration_types import (
    ADOHydrationOutput,
    IcMHydrationOutput,
    IncidentState,
    KustoHydrationOutput,
    KustoResultSet,
    MeetingEvent,
    ThreadMessage,
    TeamsHydrationOutput,
)
from src.core.kusto_signal_extractor import KustoSignalExtractor
from src.core.models import Comment, Revision, RiskLevel, WorkItem
from src.core.teams_signal_extractor import TeamsSignalExtractor
from unittest.mock import patch


_NOW = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)


def _ado_item() -> WorkItem:
    return WorkItem(
        id=101,
        type="Feature",
        title="Deployment",
        state="Active",
        assigned_to="Owner",
        assigned_to_email="owner@example.com",
        area_path="One\\Demo",
        iteration_path="One\\Iteration",
        target_date=None,
        risk_level=RiskLevel.UNKNOWN,
        tags=["RAMPP1"],
        custom_fields={"workstream_ids": ("ws-a",)},
        revisions=(
            Revision(
                work_item_id=101,
                rev_number=2,
                changed_by="Owner",
                changed_by_email="owner@example.com",
                changed_date=_NOW,
                fields_changed={"System.State": ("Active", "Closed")},
            ),
        ),
        comments=(
            Comment(
                work_item_id=101,
                comment_id=7,
                created_by="Owner",
                created_by_email="owner@example.com",
                created_date=_NOW,
                text="Ready for review",
            ),
        ),
        fetched_at=_NOW,
    )


def test_provider_extractors_emit_provider_qualified_entity_refs() -> None:
    ado_signals = ADOSignalExtractor().extract(
        ADOHydrationOutput(work_items=(_ado_item(),), freshness_items=()),
        "demo",
    ).signals
    kusto_signals = KustoSignalExtractor().extract(
        KustoHydrationOutput(
            result_sets=(
                KustoResultSet(
                    query_id="query-a",
                    rows=({"Value": 1},),
                    observed_at=_NOW,
                    workstream_ids=("ws-a",),
                ),
            )
        ),
        "demo",
    ).signals
    teams_signals = TeamsSignalExtractor().extract(
        TeamsHydrationOutput(
            meeting_events=(
                MeetingEvent(
                    event_id="evt-001",
                    series_id="series-abc",
                    thread_id="thread-xyz",
                    title="Weekly Sync",
                    started_at=_NOW,
                    ended_at=None,
                    organizer="pm@example.com",
                    workstream_ids=("ws-a",),
                ),
            ),
            thread_messages=(),
        ),
        "demo",
    ).signals
    icm_signals = IcMSignalExtractor().extract(
        IcMHydrationOutput(
            incident_states=(
                IncidentState(
                    incident_id="98765",
                    title="Disk full on storage node",
                    severity=1,
                    status="Active",
                    owning_team="StoragePM",
                    updated_at=_NOW,
                    workstream_ids=("ws-a",),
                ),
            )
        ),
        "demo",
    ).signals

    assert all(any(ref.startswith("ado:") for ref in signal.entity_refs) for signal in ado_signals)
    assert all(any(ref.startswith("kusto:") for ref in signal.entity_refs) for signal in kusto_signals)
    assert all(any(ref.startswith("teams:") for ref in signal.entity_refs) for signal in teams_signals)
    assert all(any(ref.startswith("icm:") for ref in signal.entity_refs) for signal in icm_signals)


def test_provider_extractors_emit_stable_raw_refs_for_traceability() -> None:
    ado_signals = ADOSignalExtractor().extract(
        ADOHydrationOutput(work_items=(_ado_item(),), freshness_items=()),
        "demo",
    ).signals
    kusto_signals = KustoSignalExtractor().extract(
        KustoHydrationOutput(
            result_sets=(
                KustoResultSet(
                    query_id="query-a",
                    rows=({"Value": 1},),
                    observed_at=_NOW,
                    workstream_ids=("ws-a",),
                ),
            )
        ),
        "demo",
    ).signals
    teams_signals = TeamsSignalExtractor().extract(
        TeamsHydrationOutput(
            meeting_events=(
                MeetingEvent(
                    event_id="evt-001",
                    series_id="series-abc",
                    thread_id="thread-xyz",
                    title="Weekly Sync",
                    started_at=_NOW,
                    ended_at=None,
                    organizer="pm@example.com",
                    workstream_ids=("ws-a",),
                ),
            ),
            thread_messages=(),
        ),
        "demo",
    ).signals
    icm_signals = IcMSignalExtractor().extract(
        IcMHydrationOutput(
            incident_states=(
                IncidentState(
                    incident_id="98765",
                    title="Disk full on storage node",
                    severity=1,
                    status="Active",
                    owning_team="StoragePM",
                    updated_at=_NOW,
                    workstream_ids=("ws-a",),
                ),
            )
        ),
        "demo",
    ).signals

    provider_signal_groups = {
        "ado": ado_signals,
        "kusto": kusto_signals,
        "teams": teams_signals,
        "icm": icm_signals,
    }

    for provider, signals in provider_signal_groups.items():
        assert signals, f"Expected {provider} extractor to produce at least one signal"
        assert all(signal.raw_ref for signal in signals), f"{provider} signals must carry raw_ref"
        assert all(signal.entity_refs for signal in signals), f"{provider} signals must carry entity_refs"


def test_ado_entity_refs_preserve_wi_compatibility_alias() -> None:
    signals = ADOSignalExtractor().extract(
        ADOHydrationOutput(work_items=(_ado_item(),), freshness_items=()),
        "demo",
    ).signals

    for signal in signals:
        assert "ado:101" in signal.entity_refs
        assert "WI:101" in signal.entity_refs
        assert f"WS:{signal.workstream_id}" in signal.entity_refs
        assert signal.metadata is not None
        assert signal.metadata["legacy_entity_ref"] == "WI:101"


def test_ado_pr_entity_refs_preserve_explicit_work_item_refs_when_present() -> None:
    signals = ADOSignalExtractor().extract(
        ADOHydrationOutput(
            work_items=(),
            freshness_items=(),
            pull_requests=(
                PullRequestSummary(
                    pr_id=77,
                    title="Tighten rollout gating for WI:45678",
                    status="active",
                    created_by="Owner",
                    target_ref="refs/heads/main",
                    source_ref="refs/heads/feature/gating",
                    url="https://example.test/pr/77",
                    created_at=_NOW,
                    merged_at=None,
                    repository_id="repo-a",
                    workstream_ids=("ws-a",),
                ),
            ),
        ),
        "demo",
    ).signals

    assert len(signals) == 1
    assert signals[0].entity_refs == ("pr:77", "ado/pr:77", "WS:ws-a", "WI:45678")


def test_teams_entity_refs_preserve_explicit_work_item_refs_when_present() -> None:
    signals = TeamsSignalExtractor().extract(
        TeamsHydrationOutput(
            meeting_events=(),
            thread_messages=(
                ThreadMessage(
                    message_id="msg-001",
                    thread_id="thread-chat",
                    sender="user@example.com",
                    sent_at=_NOW,
                    text="Need WI:12345 and bug 23456 aligned before rollout.",
                    workstream_ids=("ws-a",),
                ),
            ),
        ),
        "demo",
    ).signals

    assert signals
    assert all("teams:thread-chat" in signal.entity_refs for signal in signals)
    assert all("WS:ws-a" in signal.entity_refs for signal in signals)
    assert any("WI:12345" in signal.entity_refs for signal in signals)
    assert any("WI:23456" in signal.entity_refs for signal in signals)


def test_teams_entity_refs_preserve_workstream_anchor_when_present() -> None:
    signals = TeamsSignalExtractor().extract(
        TeamsHydrationOutput(
            meeting_events=(
                MeetingEvent(
                    event_id="evt-001",
                    series_id="series-abc",
                    thread_id="thread-xyz",
                    title="Weekly Sync",
                    started_at=_NOW,
                    ended_at=None,
                    organizer="pm@example.com",
                    workstream_ids=("ws-a",),
                ),
            ),
            thread_messages=(),
        ),
        "demo",
    ).signals

    assert len(signals) == 1
    assert signals[0].entity_refs == ("teams:series-abc", "WS:ws-a")


def test_teams_entity_refs_preserve_configured_work_item_refs_when_present() -> None:
    signals = TeamsSignalExtractor().extract(
        TeamsHydrationOutput(
            meeting_events=(),
            thread_messages=(
                ThreadMessage(
                    message_id="msg-config",
                    thread_id="thread-chat",
                    sender="user@example.com",
                    sent_at=_NOW,
                    text="Status unchanged",
                    workstream_ids=("ws-a",),
                    work_item_ids=(12345,),
                ),
            ),
        ),
        "demo",
    ).signals

    assert len(signals) == 1
    assert signals[0].entity_refs == ("teams:thread-chat", "WS:ws-a", "WI:12345")


def test_icm_entity_refs_preserve_explicit_work_item_refs_when_present() -> None:
    signals = IcMSignalExtractor().extract(
        IcMHydrationOutput(
            incident_states=(
                IncidentState(
                    incident_id="34567",
                    title="WI:12345 rollout validation regressed after failover",
                    severity=2,
                    status="Active",
                    owning_team="StoragePM",
                    updated_at=_NOW,
                    workstream_ids=("ws-a",),
                ),
            )
        ),
        "demo",
    ).signals

    assert signals
    for signal in signals:
        assert "icm:34567" in signal.entity_refs
        assert "WS:ws-a" in signal.entity_refs
        assert "WI:12345" in signal.entity_refs


def test_engms_entity_refs_preserve_work_item_aliases_for_origin_items() -> None:
    item = WorkItem(
        id=101,
        type="Feature",
        title="Deployment",
        state="Active",
        assigned_to="Owner",
        assigned_to_email="owner@example.com",
        area_path="One\\Demo",
        iteration_path="One\\Iteration",
        target_date=None,
        risk_level=RiskLevel.UNKNOWN,
        tags=["RAMPP1"],
        custom_fields={"System.Description": "See https://eng.ms/docs/demo/spec for details."},
        fetched_at=_NOW,
    )

    with patch("src.core.engms_signal_extractor.fetch_engms_page_summary", return_value="Page summary text"):
        signals = EngMsSignalExtractor().extract((item,), "demo").signals

    assert signals
    for signal in signals:
        assert "ado:101" in signal.entity_refs
        assert "WI:101" in signal.entity_refs


def test_kusto_entity_refs_preserve_structured_work_item_refs_when_present() -> None:
    signals = KustoSignalExtractor().extract(
        KustoHydrationOutput(
            result_sets=(
                KustoResultSet(
                    query_id="query-refs",
                    rows=(
                        {
                            "WorkItemId": 12345,
                            "Summary": "Tracking bug 23456 with ICM 98765.",
                        },
                    ),
                    observed_at=_NOW,
                    workstream_ids=("ws-a",),
                ),
            )
        ),
        "demo",
    ).signals

    assert signals
    for signal in signals:
        assert "kusto:query-refs" in signal.entity_refs
        assert "WS:ws-a" in signal.entity_refs
        assert "WI:12345" in signal.entity_refs
        assert "WI:23456" in signal.entity_refs
        assert "ICM:98765" in signal.entity_refs
