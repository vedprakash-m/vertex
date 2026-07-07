from __future__ import annotations

from datetime import datetime, timezone

from src.core.email_signal_extractor import EmailSignalExtractor
from src.core.integration_types import (
    ChannelConfig,
    ChannelRegistration,
    EmailHydrationOutput,
    EmailMessage,
    RegistrationStatus,
    RunContext,
)
from src.core.models_v2 import EmailThreadSource, Program, Workstream, WorkstreamSignalSources
from src.m365.email_discovery import EmailDiscoveryProvider
from src.m365.email_hydration import EmailHydrationConfig, EmailHydrationProvider
from src.m365.graph_mail_client import MailRecord, MailSearchPage


class _FakeMailClient:
    def __init__(self, page: MailSearchPage) -> None:
        self.page = page
        self.queries: list[str] = []

    def search_emails(self, *, query: str, limit: int = 25, cursor: str | None = None, timeout_seconds: int | None = None) -> MailSearchPage:
        del limit, cursor, timeout_seconds
        self.queries.append(query)
        return self.page


def test_email_discovery_provider_emits_static_email_thread_bindings() -> None:
    provider, config = EmailDiscoveryProvider.from_program(
        Program(schema_version="3.0", id="demo", name="Demo"),
        ChannelConfig(channel="email", enabled=True, discovery_threshold_hours=24, ttl_days=30, extra={"instance_id": "mail"}),
        (
            Workstream(
                id="demo.ws",
                name="Demo",
                signal_sources=WorkstreamSignalSources(
                    email_threads=(EmailThreadSource(display_name="Launch Thread", thread_id="thread-123", work_item_ids=(42,)),),
                ),
            ),
        ),
    )

    result = provider.discover("demo", config, (), run_ctx=RunContext())

    assert result.channel == "email"
    assert result.provider_instance_id == "mail"
    assert len(result.discovered_refs) == 1
    registration = result.discovered_refs[0].registration
    assert registration.ref_kind == "email_thread"
    assert registration.ref_id == "thread-123"
    assert registration.ref_title == "Launch Thread"
    assert registration.metadata == {"display_name": "Launch Thread", "thread_id": "thread-123"}


def test_email_hydration_filters_records_to_thread_and_since_window() -> None:
    client = _FakeMailClient(
        MailSearchPage(
            records=(
                MailRecord(
                    source_id="mail-1",
                    subject="Launch Thread",
                    sender="owner@example.com",
                    recipients=("team@example.com",),
                    received_at="2026-06-02T10:00:00Z",
                    web_url="https://outlook.office.com/mail/mail-1",
                    preview="Status remains blocked on WI:1234.",
                    thread_id="thread-123",
                ),
                MailRecord(
                    source_id="mail-2",
                    subject="Launch Thread",
                    sender="owner@example.com",
                    recipients=("team@example.com",),
                    received_at="2026-05-20T10:00:00Z",
                    web_url="https://outlook.office.com/mail/mail-2",
                    preview="Older message.",
                    thread_id="thread-123",
                ),
                MailRecord(
                    source_id="mail-3",
                    subject="Other Thread",
                    sender="other@example.com",
                    recipients=("team@example.com",),
                    received_at="2026-06-02T10:00:00Z",
                    web_url="https://outlook.office.com/mail/mail-3",
                    preview="Wrong thread.",
                    thread_id="thread-999",
                ),
            ),
            next_cursor=None,
            source="workiq",
        )
    )
    provider = EmailHydrationProvider(client)
    registration = ChannelRegistration(
        channel="email",
        program_id="demo",
        provider_instance_id="default",
        ref_id="thread-123",
        ref_kind="email_thread",
        status=RegistrationStatus.ACTIVE,
        first_discovered_at=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        last_seen_at=datetime(2026, 6, 2, 9, 0, tzinfo=timezone.utc),
        ref_title="Launch Thread",
        metadata={"display_name": "Launch Thread"},
        workstream_ids=("demo.ws",),
        work_item_ids=(42,),
    )

    result = provider.hydrate(
        (registration,),
        datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
        "demo",
        EmailHydrationConfig(),
        run_ctx=RunContext(),
    )

    assert client.queries == ["Launch Thread"]
    assert result.hydrated_ref_ids == (("thread-123", "email_thread"),)
    assert result.failed_ref_ids == ()
    assert len(result.resources.messages) == 1
    message = result.resources.messages[0]
    assert message.message_id == "mail-1"
    assert message.thread_id == "thread-123"
    assert message.workstream_ids == ("demo.ws",)
    assert message.work_item_ids == (42,)


def test_email_signal_extractor_emits_workiq_email_signals() -> None:
    signals = EmailSignalExtractor().extract(
        resources=EmailHydrationOutput(
            messages=(
                EmailMessage(
                    message_id="mail-1",
                    thread_id="thread-123",
                    subject="Launch Thread",
                    sent_at=datetime(2026, 6, 2, 10, 0, tzinfo=timezone.utc),
                    preview="Decision: proceed after WI:1234 closes.",
                    sender="owner@example.com",
                    recipients=("team@example.com",),
                    permalink="https://outlook.office.com/mail/mail-1",
                    workstream_ids=("demo.ws",),
                    work_item_ids=(42,),
                ),
            )
        ),
        program_id="demo",
    ).signals

    assert len(signals) == 1
    signal = signals[0]
    assert signal.source == "workiq/email"
    assert signal.thread_id == "thread-123"
    assert "WI:42" in signal.entity_refs
    assert "WI:1234" in signal.entity_refs
    assert signal.metadata is not None
    assert signal.metadata["source_type"] == "email"
    assert signal.metadata["sender_alias"] == "owner"
