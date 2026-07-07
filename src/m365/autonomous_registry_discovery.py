from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.m365_discovery_support import build_m365_discovery_queries
from src.core.m365_payload_support import (
    optional_string,
    sender_alias,
    workiq_participant_aliases,
    workiq_payload_records,
    workiq_preview,
    workiq_subject,
    workiq_thread_id,
)
from src.core.m365_router_interface import IM365TopicRouter, M365ReassignCorrection
from src.core.m365_signal_corpus import (
    build_m365_corpus_texts_by_workstream,
    build_m365_reassign_corrections_by_workstream,
    build_m365_rejected_texts_by_workstream,
    load_approved_m365_corpus_signals,
)
from src.core.m365_identifiers import normalize_meeting_id
from src.core.m365_registry_store import (
    M365RegistryArtifact,
    build_auto_meeting_artifact_id,
    build_auto_thread_artifact_id,
    load_m365_registry,
    read_m365_routing_feedback_events,
    tracked_registry_thread_ids,
    upsert_m365_registry_artifacts,
)
from src.core.models_v2 import Workstream
from src.m365.agency_bridge import AgencyBridge
from src.m365.graph_calendar_client import CalendarEventRecord, GraphCalendarClient
from src.m365.graph_mail_client import GraphMailClient, MailRecord
from src.m365.teams_reader import TeamsMessageRecord, TeamsReader


def run_m365_discovery_pass(
    *,
    program_id: str,
    as_of: datetime,
    workstreams: tuple[Workstream, ...],
    bridge_client: AgencyBridge,
    topic_router: IM365TopicRouter,
    programs_root: Path,
    result_limit: int,
) -> tuple[tuple[M365RegistryArtifact, ...], tuple[str, ...]]:
    registry = load_m365_registry(program_id, programs_root)
    feedback_events = read_m365_routing_feedback_events(program_id, programs_root)
    recent_confirmed_signals_by_workstream = build_m365_corpus_texts_by_workstream(
        workstreams=workstreams,
        registry_artifacts=registry.artifacts,
        feedback_events=feedback_events,
        approved_signals=load_approved_m365_corpus_signals(program_id, as_of=as_of, programs_root=programs_root),
        as_of=as_of,
    )
    recent_rejected_signals_by_workstream = build_m365_rejected_texts_by_workstream(
        workstreams=workstreams,
        registry_artifacts=registry.artifacts,
        feedback_events=feedback_events,
        as_of=as_of,
    )
    recent_reassign_corrections_by_workstream = build_m365_reassign_corrections_by_workstream(
        workstreams=workstreams,
        registry_artifacts=registry.artifacts,
        feedback_events=feedback_events,
        as_of=as_of,
    )
    discovery_queries = build_m365_discovery_queries(
        workstreams=workstreams,
        registry_artifacts=registry.artifacts,
        feedback_events=feedback_events,
        as_of=as_of,
    )
    if not discovery_queries:
        return (), ()

    tracked_ids = tracked_registry_thread_ids(
        registry.artifacts,
        feedback_events=feedback_events,
        as_of=as_of,
    )
    discovered_artifacts: list[M365RegistryArtifact] = []
    discovery_errors: list[str] = []
    mail_client = GraphMailClient(bridge_client)
    teams_reader = TeamsReader(bridge_client)
    calendar_client = GraphCalendarClient(bridge_client)
    discovery_requests = [
        (
            "calendar",
            lambda query, limit: discover_calendar_registry_artifacts(
                query=query,
                limit=limit,
                as_of=as_of,
                tracked_ids=tracked_ids,
                workstreams=workstreams,
                topic_router=topic_router,
                recent_confirmed_signals=recent_confirmed_signals_by_workstream,
                recent_rejected_signals=recent_rejected_signals_by_workstream,
                recent_reassign_corrections=recent_reassign_corrections_by_workstream,
                calendar_client=calendar_client,
                bridge_client=bridge_client,
            ),
        ),
        (
            "email",
            lambda query, limit: discover_thread_registry_artifacts(
                source_hint="email",
                query=query,
                limit=limit,
                as_of=as_of,
                tracked_ids=tracked_ids,
                workstreams=workstreams,
                topic_router=topic_router,
                recent_confirmed_signals=recent_confirmed_signals_by_workstream,
                recent_rejected_signals=recent_rejected_signals_by_workstream,
                recent_reassign_corrections=recent_reassign_corrections_by_workstream,
                mail_client=mail_client,
                teams_reader=None,
                bridge_client=bridge_client,
            ),
        ),
        (
            "teams",
            lambda query, limit: discover_thread_registry_artifacts(
                source_hint="teams",
                query=query,
                limit=limit,
                as_of=as_of,
                tracked_ids=tracked_ids,
                workstreams=workstreams,
                topic_router=topic_router,
                recent_confirmed_signals=recent_confirmed_signals_by_workstream,
                recent_rejected_signals=recent_rejected_signals_by_workstream,
                recent_reassign_corrections=recent_reassign_corrections_by_workstream,
                mail_client=None,
                teams_reader=teams_reader,
                bridge_client=bridge_client,
            ),
        ),
    ]
    remaining_budget = result_limit
    for index, (_source_hint, discovery_loader) in enumerate(discovery_requests):
        remaining_sources = len(discovery_requests) - index
        per_source_limit = max(1, remaining_budget // remaining_sources)
        per_query_limit = max(1, per_source_limit // max(1, len(discovery_queries)))
        source_artifacts: list[M365RegistryArtifact] = []
        for query in discovery_queries:
            query_artifacts, query_error = discovery_loader(query, per_query_limit)
            source_artifacts.extend(query_artifacts)
            if query_error:
                discovery_errors.append(query_error)
        remaining_budget = max(0, remaining_budget - per_source_limit)
        discovered_artifacts.extend(source_artifacts)

    if not discovered_artifacts:
        return (), tuple(discovery_errors)

    upsert_m365_registry_artifacts(
        program_id,
        artifacts=tuple(discovered_artifacts),
        programs_root=programs_root,
        as_of=as_of,
    )
    return tuple(discovered_artifacts), tuple(discovery_errors)


def discover_thread_registry_artifacts(
    *,
    source_hint: str,
    query: str,
    limit: int,
    as_of: datetime,
    tracked_ids: set[str],
    workstreams: tuple[Workstream, ...],
    topic_router: IM365TopicRouter,
    recent_confirmed_signals: dict[str, tuple[str, ...]],
    recent_rejected_signals: dict[str, tuple[str, ...]],
    recent_reassign_corrections: dict[str, tuple[M365ReassignCorrection, ...]],
    mail_client: GraphMailClient | None,
    teams_reader: TeamsReader | None,
    bridge_client: AgencyBridge,
) -> tuple[tuple[M365RegistryArtifact, ...], str | None]:
    if source_hint == "email":
        payload = {
            "emails": [
                _mail_record_payload(record)
                for record in (mail_client or GraphMailClient(bridge_client)).search_emails(query=query, limit=limit).records
            ]
        }
    else:
        payload = {
            "messages": [
                _teams_record_payload(record)
                for record in (teams_reader or TeamsReader(bridge_client)).search_messages(
                    channel="all",
                    query=query,
                    limit=limit,
                ).records
            ]
        }
    last_error = bridge_client.last_mcp_error() if hasattr(bridge_client, "last_mcp_error") else None
    if not workiq_payload_records(payload) and last_error:
        return (), f"{source_hint} discovery failed: {last_error}"
    discovered_artifacts: list[M365RegistryArtifact] = []
    for record in workiq_payload_records(payload):
        thread_id = workiq_thread_id(record)
        if thread_id is None or thread_id in tracked_ids:
            continue
        channel_name = optional_string(record.get("channel") or record.get("teamChannel"))
        decision = topic_router.route_artifact(
            display_name=channel_name,
            subject_or_title=workiq_subject(record),
            participant_aliases=workiq_participant_aliases(record),
            sample_text=workiq_preview(record),
            workstream_profiles=workstreams,
            recent_confirmed_signals=recent_confirmed_signals,
            recent_rejected_signals=recent_rejected_signals,
            recent_reassign_corrections=recent_reassign_corrections,
        )
        discovered_artifacts.append(
            M365RegistryArtifact(
                artifact_id=build_auto_thread_artifact_id(thread_id),
                artifact_type="teams_channel" if source_hint == "teams" else "email_thread",
                display_name=workiq_subject(record) or channel_name or f"M365 thread {thread_id[:8]}",
                thread_id=thread_id,
                inferred_workstream=decision.workstream_id or workstreams[0].id,
                confidence=decision.confidence,
                confidence_source=decision.confidence_source,
                pm_confirmed=False,
                promoted_to_workstreams_yaml=False,
                first_seen=as_of.date(),
                last_seen=as_of.date(),
                topics=decision.topics,
                routing_reasoning=decision.reasoning,
            )
        )
        tracked_ids.add(thread_id)
    return tuple(discovered_artifacts), None


def discover_calendar_registry_artifacts(
    *,
    query: str,
    limit: int,
    as_of: datetime,
    tracked_ids: set[str],
    workstreams: tuple[Workstream, ...],
    topic_router: IM365TopicRouter,
    recent_confirmed_signals: dict[str, tuple[str, ...]],
    recent_rejected_signals: dict[str, tuple[str, ...]],
    recent_reassign_corrections: dict[str, tuple[M365ReassignCorrection, ...]],
    calendar_client: GraphCalendarClient,
    bridge_client: AgencyBridge,
) -> tuple[tuple[M365RegistryArtifact, ...], str | None]:
    page = calendar_client.search_events(query=query, limit=limit)
    last_error = bridge_client.last_mcp_error() if hasattr(bridge_client, "last_mcp_error") else None
    if not page.records and last_error:
        return (), f"calendar discovery failed: {last_error}"
    discovered_artifacts: list[M365RegistryArtifact] = []
    for record in page.records:
        series_id = normalize_meeting_id(record.series_master_id or record.meeting_id or record.web_url or record.source_id)
        if series_id is None or series_id in tracked_ids:
            continue
        label = (record.subject or f"M365 meeting {series_id[:8]}").strip()
        preview_parts = [label]
        if record.organizer:
            preview_parts.append(f"Organizer: {record.organizer}")
        if record.attendees:
            preview_parts.append(f"Attendees: {', '.join(record.attendees[:4])}")
        decision = topic_router.route_artifact(
            display_name=label,
            subject_or_title=label,
            participant_aliases=tuple(
                dict.fromkeys(
                    alias
                    for alias in (
                        sender_alias(record.organizer),
                        *(sender_alias(attendee) for attendee in record.attendees),
                    )
                    if alias
                )
            ),
            sample_text="; ".join(preview_parts),
            workstream_profiles=workstreams,
            recent_confirmed_signals=recent_confirmed_signals,
            recent_rejected_signals=recent_rejected_signals,
            recent_reassign_corrections=recent_reassign_corrections,
        )
        discovered_artifacts.append(
            M365RegistryArtifact(
                artifact_id=build_auto_meeting_artifact_id(series_id),
                artifact_type="meeting_series",
                display_name=label,
                series_id=series_id,
                inferred_workstream=decision.workstream_id or workstreams[0].id,
                confidence=decision.confidence,
                confidence_source=decision.confidence_source,
                pm_confirmed=False,
                promoted_to_workstreams_yaml=False,
                first_seen=as_of.date(),
                last_seen=as_of.date(),
                topics=decision.topics,
                routing_reasoning=decision.reasoning,
            )
        )
        tracked_ids.add(series_id)
    return tuple(discovered_artifacts), None


def _mail_record_payload(record: MailRecord) -> dict[str, Any]:
    return {
        "id": record.source_id,
        "subject": record.subject,
        "from": {"emailAddress": {"address": record.sender}} if record.sender else None,
        "toRecipients": [{"emailAddress": {"address": recipient}} for recipient in record.recipients],
        "receivedDateTime": record.received_at,
        "webUrl": record.web_url,
        "bodyPreview": record.preview,
        "threadId": record.thread_id,
        "conversationId": record.conversation_id,
    }


def _teams_record_payload(record: TeamsMessageRecord) -> dict[str, Any]:
    return {
        "id": record.source_id,
        "channel": record.channel,
        "from": {"user": {"displayName": record.sender}} if record.sender else None,
        "createdDateTime": record.sent_at,
        "webUrl": record.web_url,
        "bodyPreview": record.preview,
        "threadId": record.thread_id,
        "conversationId": record.conversation_id,
        "title": record.title,
    }
