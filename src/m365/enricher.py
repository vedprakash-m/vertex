from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.core.config_loader import M365Settings, NarrativeProgramContext, ProgramWorkstream
from src.core.models import Enrichment, WorkItem
from src.m365.agency_bridge import AgencyBridge
from src.m365.graph_calendar_client import GraphCalendarClient
from src.m365.graph_mail_client import GraphMailClient
from src.m365.teams_reader import TeamsReader
from src.m365.transcript_reader import TranscriptReader


log = logging.getLogger(__name__)
_MAX_RECORDS_PER_SOURCE = 3
_MAIL_PREVIEW_MAX_CHARS = 2000
_TRANSCRIPT_BODY_MAX_CHARS = 8000


@dataclass(frozen=True, slots=True)
class WorkstreamEnrichment:
    workstream_name: str
    item_ids: tuple[int, ...]
    enrichments: tuple[Enrichment, ...]


class M365Enricher:
    """Collects reviewer-only M365 metadata and maps it onto workstream items."""

    def __init__(
        self,
        bridge: AgencyBridge,
        *,
        mail_client: GraphMailClient | None = None,
        calendar_client: GraphCalendarClient | None = None,
        teams_reader: TeamsReader | None = None,
        transcript_reader: TranscriptReader | None = None,
    ) -> None:
        self._bridge = bridge
        self._mail_client = mail_client or GraphMailClient(bridge)
        self._calendar_client = calendar_client or GraphCalendarClient(bridge)
        self._teams_reader = teams_reader or TeamsReader(bridge)
        self._transcript_reader = transcript_reader or TranscriptReader(bridge)

    def enrich_items(
        self,
        *,
        config: M365Settings,
        program_context: NarrativeProgramContext | None,
        items: tuple[WorkItem, ...],
        as_of: datetime,
    ) -> dict[int, tuple[Enrichment, ...]]:
        workstream_evidence = self.enrich_workstreams(
            config=config,
            program_context=program_context,
            items=items,
            as_of=as_of,
        )
        enrichments_by_item: dict[int, tuple[Enrichment, ...]] = {}
        for workstream in workstream_evidence:
            for item_id in workstream.item_ids:
                enrichments_by_item[item_id] = workstream.enrichments
        return enrichments_by_item

    def enrich_workstreams(
        self,
        *,
        config: M365Settings,
        program_context: NarrativeProgramContext | None,
        items: tuple[WorkItem, ...],
        as_of: datetime,
    ) -> tuple[WorkstreamEnrichment, ...]:
        if not config.enabled or not items or program_context is None or not program_context.workstreams:
            return ()

        capabilities = self._bridge.probe()
        if not capabilities.available or (not capabilities.has_bluebird and not capabilities.has_workiq):
            return ()

        workstreams: list[WorkstreamEnrichment] = []
        for workstream in program_context.workstreams:
            item_ids = tuple(item.id for item in items if _matches_workstream(item, workstream))
            if not item_ids:
                continue
            enrichments = self._collect_workstream_enrichments(
                workstream=workstream,
                config=config,
                as_of=as_of,
                has_bluebird=capabilities.has_bluebird,
                has_workiq=capabilities.has_workiq,
            )
            if not enrichments:
                continue
            workstreams.append(
                WorkstreamEnrichment(
                    workstream_name=workstream.name,
                    item_ids=item_ids,
                    enrichments=enrichments,
                )
            )
        return tuple(workstreams)

    def _collect_workstream_enrichments(
        self,
        *,
        workstream: ProgramWorkstream,
        config: M365Settings,
        as_of: datetime,
        has_bluebird: bool,
        has_workiq: bool,
    ) -> tuple[Enrichment, ...]:
        search_terms = _search_terms(workstream)
        primary_term = search_terms[0] if search_terms else workstream.name
        since = _ensure_utc(as_of) - timedelta(days=config.bluebird.lookback_days)
        enrichments: list[Enrichment] = []

        if has_bluebird and config.workiq.newsletter_search:
            query = _join_query(config.workiq.newsletter_search, primary_term)
            try:
                page = self._mail_client.search_emails(query=query, limit=_MAX_RECORDS_PER_SOURCE)
                for record in page.records[:_MAX_RECORDS_PER_SOURCE]:
                    enrichment = _mail_enrichment(record)
                    if enrichment is not None:
                        enrichments.append(enrichment)
            except Exception as exc:
                log.info("M365 email enrichment skipped for %s: %s", workstream.name, exc)

        if has_workiq and config.workiq.feedback_search:
            question = _build_feedback_question(config.workiq.feedback_search, workstream, search_terms)
            try:
                page = self._mail_client.search_threads(question=question)
                for record in page.records[:_MAX_RECORDS_PER_SOURCE]:
                    enrichment = _mail_enrichment(record)
                    if enrichment is not None:
                        enrichments.append(enrichment)
            except Exception as exc:
                log.info("M365 feedback enrichment skipped for %s: %s", workstream.name, exc)

        calendar_records: tuple = ()
        if has_bluebird:
            try:
                calendar_page = self._calendar_client.search_events(query=primary_term, limit=_MAX_RECORDS_PER_SOURCE)
                calendar_records = calendar_page.records[:_MAX_RECORDS_PER_SOURCE]
                for cal_record in calendar_records:
                    enrichment = _calendar_enrichment(cal_record, as_of=as_of)
                    if enrichment is not None:
                        enrichments.append(enrichment)
            except Exception as exc:
                log.info("M365 calendar enrichment skipped for %s: %s", workstream.name, exc)

        if has_bluebird and config.workiq.teams_search and config.bluebird.teams_channels:
            for channel in config.bluebird.teams_channels:
                try:
                    teams_page = self._teams_reader.search_messages(
                        channel=channel,
                        query=_join_query(config.workiq.teams_search, primary_term),
                        since=since.isoformat(),
                        limit=_MAX_RECORDS_PER_SOURCE,
                    )
                    for teams_record in teams_page.records[:_MAX_RECORDS_PER_SOURCE]:
                        enrichment = _teams_enrichment(teams_record, fallback_channel=channel, as_of=as_of)
                        if enrichment is not None:
                            enrichments.append(enrichment)
                except Exception as exc:
                    log.info("M365 Teams enrichment skipped for %s via %s: %s", workstream.name, channel, exc)

        if has_bluebird:
            for cal_record in calendar_records:
                if cal_record.source_id is None:
                    continue
                try:
                    transcript = self._transcript_reader.get_transcript(meeting_id=cal_record.source_id)
                    enrichment = _transcript_enrichment(transcript, as_of=as_of)
                    if enrichment is not None:
                        enrichments.append(enrichment)
                except Exception as exc:
                    log.info("M365 transcript enrichment skipped for %s meeting %s: %s", workstream.name, cal_record.source_id, exc)

        return _dedupe_and_sort(enrichments)


def _matches_workstream(item: WorkItem, workstream: ProgramWorkstream) -> bool:
    item_area = item.area_path.lower()
    item_title = item.title.lower()
    item_tags = {tag.lower() for tag in item.tags}
    normalized_terms = [workstream.name.lower(), *(alias.lower() for alias in workstream.aliases)]

    if any(path.lower() in item_area for path in workstream.area_paths):
        return True
    return any(term in item_area or term in item_title or term in item_tags for term in normalized_terms if term)


def _search_terms(workstream: ProgramWorkstream) -> tuple[str, ...]:
    ordered = [workstream.name, *workstream.aliases]
    seen: set[str] = set()
    terms: list[str] = []
    for term in ordered:
        normalized = term.strip()
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        terms.append(normalized)
    return tuple(terms)


def _join_query(base: str, term: str) -> str:
    return f"{base} {term}".strip()


def _build_feedback_question(base: str, workstream: ProgramWorkstream, search_terms: tuple[str, ...]) -> str:
    aliases = ", ".join(search_terms[1:])
    if aliases:
        return f"{base}. Focus on {workstream.name}. Related aliases: {aliases}."
    return f"{base}. Focus on {workstream.name}."


def _mail_enrichment(record) -> Enrichment | None:
    source_id = record.source_id or record.web_url
    if source_id is None:
        return None
    subject = record.subject or "Untitled email"
    body_text: str | None = None
    if record.preview and record.preview.strip():
        body_text = record.preview.strip()[:_MAIL_PREVIEW_MAX_CHARS]
    return Enrichment(
        source="mail",
        source_id=source_id,
        author=record.sender or "mail sender",
        timestamp=_parse_timestamp(record.received_at),
        excerpt=f"Subject: {subject}",
        permalink=record.web_url,
        body_text=body_text,
    )


def _calendar_enrichment(record, *, as_of: datetime) -> Enrichment | None:
    source_id = record.source_id or record.web_url
    if source_id is None:
        return None
    title = record.subject or "Untitled meeting"
    start_label = record.start_at or _ensure_utc(as_of).isoformat()
    location = f" at {record.location}" if record.location else ""
    return Enrichment(
        source="calendar",
        source_id=source_id,
        author=record.organizer or "meeting organizer",
        timestamp=_parse_timestamp(record.start_at, fallback=as_of),
        excerpt=f"Meeting: {title} starting {start_label}{location}",
        permalink=record.web_url,
    )


def _teams_enrichment(record, *, fallback_channel: str, as_of: datetime) -> Enrichment | None:
    source_id = record.source_id or record.web_url
    if source_id is None:
        return None
    channel = record.channel or fallback_channel
    return Enrichment(
        source="teams_chat",
        source_id=source_id,
        author=record.sender or "teams sender",
        timestamp=_parse_timestamp(record.sent_at, fallback=as_of),
        excerpt=f"Channel: {channel}",
        permalink=record.web_url,
    )


def _transcript_enrichment(record, *, as_of: datetime) -> Enrichment | None:
    if record is None:
        return None
    source_id = record.meeting_id or record.web_url
    if source_id is None:
        return None
    title = record.title or "Untitled meeting"
    body_text: str | None = None
    if record.content and record.content.strip():
        body_text = record.content.strip()[:_TRANSCRIPT_BODY_MAX_CHARS]
    return Enrichment(
        source="transcript",
        source_id=source_id,
        author="meeting transcript",
        timestamp=_parse_timestamp(record.captured_at, fallback=as_of),
        excerpt=f"Transcript: {title}",
        permalink=record.web_url,
        body_text=body_text,
    )


def _dedupe_and_sort(enrichments: list[Enrichment]) -> tuple[Enrichment, ...]:
    deduped: dict[tuple[str, str], Enrichment] = {}
    for enrichment in enrichments:
        deduped[(enrichment.source, enrichment.source_id)] = enrichment
    return tuple(sorted(deduped.values(), key=lambda enrichment: enrichment.timestamp, reverse=True))


def _parse_timestamp(value: str | None, *, fallback: datetime | None = None) -> datetime:
    candidate = value.strip() if isinstance(value, str) else ""
    if candidate:
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            return _ensure_utc(parsed)
        except ValueError:
            pass
    return _ensure_utc(fallback or datetime.now(timezone.utc))


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)