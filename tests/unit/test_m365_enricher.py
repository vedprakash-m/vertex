from __future__ import annotations

from datetime import date, datetime, timezone

from src.core.config_loader import M365BluebirdSettings, M365OfflineSettings, M365Settings, M365WorkIQSettings
from src.core.config_loader import NarrativeProgramContext, ProgramWorkstream
from src.core.models import RiskLevel, WorkItem
from src.m365.agency_bridge import AgencyCapabilities
from src.m365.enricher import M365Enricher
from src.m365.graph_calendar_client import CalendarEventPage, CalendarEventRecord
from src.m365.graph_mail_client import MailRecord, MailSearchPage
from src.m365.teams_reader import TeamsMessagePage, TeamsMessageRecord
from src.m365.transcript_reader import TranscriptRecord


class _FakeBridge:
    def __init__(self, capabilities: AgencyCapabilities) -> None:
        self.capabilities = capabilities
        self.probe_calls = 0

    def probe(self) -> AgencyCapabilities:
        self.probe_calls += 1
        return self.capabilities


class _FakeMailClient:
    def __init__(self) -> None:
        self.email_queries: list[str] = []
        self.thread_questions: list[str] = []

    def search_emails(self, *, query: str, limit: int = 25, cursor: str | None = None) -> MailSearchPage:
        self.email_queries.append(query)
        return MailSearchPage(
            records=(
                MailRecord(
                    source_id="mail-1",
                    subject="Acme Weekly digest",
                    sender="operator@example.com",
                    recipients=("acme_newsletter@example.com",),
                    received_at="2026-05-06T17:00:00Z",
                    web_url="https://outlook.office.com/mail/1",
                    preview="ignored raw preview",
                ),
            ),
            next_cursor=None,
            source="workiq",
        )

    def search_threads(self, *, question: str) -> MailSearchPage:
        self.thread_questions.append(question)
        return MailSearchPage(
            records=(
                MailRecord(
                    source_id="mail-2",
                    subject="Review feedback for deployment section",
                    sender="rushi@example.com",
                    recipients=("maintainer@example.com",),
                    received_at="2026-05-07T18:00:00Z",
                    web_url="https://outlook.office.com/mail/2",
                    preview="ignored thread preview",
                ),
            ),
            next_cursor=None,
            source="workiq",
        )


class _FakeCalendarClient:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def search_events(self, *, query: str, limit: int = 25, cursor: str | None = None) -> CalendarEventPage:
        self.queries.append(query)
        return CalendarEventPage(
            records=(
                CalendarEventRecord(
                    source_id="meeting-1",
                    subject="Deployment Velocity sync",
                    organizer="owner@example.com",
                    start_at="2026-05-07T16:00:00Z",
                    end_at="2026-05-07T16:30:00Z",
                    location="Teams",
                    web_url="https://outlook.office.com/calendar/item/1",
                ),
            ),
            next_cursor=None,
            source="workiq",
        )


class _FakeTeamsReader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def search_messages(
        self,
        *,
        channel: str,
        query: str,
        since: str | None = None,
        limit: int = 25,
        cursor: str | None = None,
    ) -> TeamsMessagePage:
        self.calls.append((channel, query, since))
        return TeamsMessagePage(
            records=(
                TeamsMessageRecord(
                    source_id="teams-1",
                    channel=channel,
                    sender="isaiah@example.com",
                    sent_at="2026-05-07T15:00:00Z",
                    web_url="https://teams.microsoft.com/l/message/1",
                    preview="ignored chat preview",
                ),
            ),
            next_cursor=None,
            source="workiq",
        )


class _FakeTranscriptReader:
    def __init__(self) -> None:
        self.meeting_ids: list[str] = []

    def get_transcript(self, *, meeting_id: str) -> TranscriptRecord | None:
        self.meeting_ids.append(meeting_id)
        return TranscriptRecord(
            meeting_id=meeting_id,
            title="Deployment Velocity sync",
            captured_at="2026-05-07T16:35:00Z",
            web_url="https://teams.microsoft.com/l/transcript/1",
            content="raw transcript content stays in memory only",
        )


def test_m365_enricher_builds_metadata_only_workstream_evidence() -> None:
    config = _m365_settings(enabled=True)
    bridge = _FakeBridge(
        AgencyCapabilities(
            available=True,
            has_workiq=True,
            has_bluebird=True,
            tier="msft",
        )
    )
    mail_client = _FakeMailClient()
    calendar_client = _FakeCalendarClient()
    teams_reader = _FakeTeamsReader()
    transcript_reader = _FakeTranscriptReader()
    enricher = M365Enricher(
        bridge,
        mail_client=mail_client,
        calendar_client=calendar_client,
        teams_reader=teams_reader,
        transcript_reader=transcript_reader,
    )
    as_of = datetime(2026, 5, 7, 19, 0, tzinfo=timezone.utc)

    workstreams = enricher.enrich_workstreams(
        config=config,
        program_context=_program_context(),
        items=(_item(101), _item(102)),
        as_of=as_of,
    )

    assert bridge.probe_calls == 1
    assert len(workstreams) == 1
    workstream = workstreams[0]
    assert workstream.workstream_name == "Deployment Velocity"
    assert workstream.item_ids == (101, 102)
    assert {enrichment.source for enrichment in workstream.enrichments} == {"mail", "calendar", "teams_chat", "transcript"}
    assert any(enrichment.excerpt == "Subject: Acme Weekly digest" for enrichment in workstream.enrichments)
    assert any(enrichment.excerpt == "Subject: Review feedback for deployment section" for enrichment in workstream.enrichments)
    assert any(enrichment.excerpt == "Channel: xInfraSWPM: Acme Weekly" for enrichment in workstream.enrichments)
    assert any(enrichment.excerpt == "Transcript: Deployment Velocity sync" for enrichment in workstream.enrichments)
    assert all("ignored" not in enrichment.excerpt for enrichment in workstream.enrichments)

    enrichments_by_item = enricher.enrich_items(
        config=config,
        program_context=_program_context(),
        items=(_item(101), _item(102)),
        as_of=as_of,
    )

    assert enrichments_by_item[101] == workstream.enrichments
    assert enrichments_by_item[102] == workstream.enrichments
    assert mail_client.email_queries == ["emails to acme_newsletter@example.com Deployment Velocity", "emails to acme_newsletter@example.com Deployment Velocity"]
    assert "Focus on Deployment Velocity" in mail_client.thread_questions[0]
    assert calendar_client.queries == ["Deployment Velocity", "Deployment Velocity"]
    assert teams_reader.calls[0][0] == "xInfraSWPM: Acme Weekly"
    assert teams_reader.calls[0][1] == "xInfraSWPM channel discussions about newsletter content Deployment Velocity"
    assert teams_reader.calls[0][2] == "2026-04-30T19:00:00+00:00"
    assert transcript_reader.meeting_ids == ["meeting-1", "meeting-1"]


def test_m365_enricher_returns_empty_when_disabled_or_unavailable() -> None:
    bridge = _FakeBridge(AgencyCapabilities(available=False))
    mail_client = _FakeMailClient()
    enricher = M365Enricher(bridge, mail_client=mail_client)

    assert enricher.enrich_items(
        config=_m365_settings(enabled=False),
        program_context=_program_context(),
        items=(_item(101),),
        as_of=datetime(2026, 5, 7, 19, 0, tzinfo=timezone.utc),
    ) == {}
    assert mail_client.email_queries == []
    assert bridge.probe_calls == 0


def _m365_settings(*, enabled: bool) -> M365Settings:
    return M365Settings(
        enabled=enabled,
        prefer_agency=True,
        workiq=M365WorkIQSettings(
            newsletter_search="emails to acme_newsletter@example.com",
            feedback_search="review feedback from Rushi on Acme newsletter drafts",
            teams_search="xInfraSWPM channel discussions about newsletter content",
        ),
        bluebird=M365BluebirdSettings(
            teams_channels=("xInfraSWPM: Acme Weekly",),
            lookback_days=7,
        ),
        offline=M365OfflineSettings(
            newsletter_dir="backfill/emails/",
            transcript_dir="backfill/transcripts/",
        ),
    )


def _program_context() -> NarrativeProgramContext:
    return NarrativeProgramContext(
        schema_version="1.0",
        program_name="Acme",
        objective="Track ramp readiness.",
        mission=None,
        pillars=(),
        glossary={},
        workstreams=(
            ProgramWorkstream(
                name="Deployment Velocity",
                aliases=("deployment",),
                area_paths=("One\\Adventure\\Acme\\Deployment",),
                dri_email="owner@example.com",
                alternate_owner=None,
                description="Deployment stream",
            ),
        ),
        people=(),
    )


def _item(work_item_id: int) -> WorkItem:
    return WorkItem(
        id=work_item_id,
        type="Feature",
        title="Deployment readiness item",
        state="Active",
        assigned_to="Owner",
        assigned_to_email="owner@example.com",
        area_path="One\\Adventure\\Acme\\Deployment",
        iteration_path="One\\Iteration",
        target_date=date(2026, 5, 30),
        risk_level=RiskLevel.MEDIUM,
        tags=["deployment"],
        custom_fields={},
    )