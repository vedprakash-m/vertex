from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.config_loader import PROGRAMS_ROOT
from src.core.exceptions import QueryError
from src.core.integration_types import (
    ChannelConfig,
    ChannelRegistration,
    DiscoveryCompleteness,
    EmailHydrationOutput,
    EmailMessage,
    HydrationMode,
    HydrationResult,
    IntegrationError,
    ProviderCapability,
    RunContext,
)
from src.core.m365_identifiers import normalize_thread_id
from src.core.models_v2 import Program, Workstream
from src.m365.agency_bridge import AgencyBridge
from src.m365.graph_mail_client import GraphMailClient, MailRecord


@dataclass(frozen=True, slots=True)
class EmailHydrationConfig:
    batch_size: int = 50


class EmailHydrationProvider:
    def __init__(self, mail_client: GraphMailClient) -> None:
        self._mail_client = mail_client

    @classmethod
    def from_program(
        cls,
        program: Program,
        channel_config: ChannelConfig,
        workstreams: tuple[Workstream, ...],
        *,
        programs_root: Path = PROGRAMS_ROOT,
    ) -> tuple["EmailHydrationProvider", EmailHydrationConfig]:
        del program, channel_config, workstreams, programs_root
        return cls(GraphMailClient(AgencyBridge())), EmailHydrationConfig()

    @property
    def channel(self) -> str:
        return "email"

    @property
    def capability(self) -> ProviderCapability:
        return ProviderCapability(
            channel="email",
            discovery_modes=(DiscoveryCompleteness.FULL,),
            hydration_modes=(HydrationMode.FULL,),
            supports_since=False,
            max_batch_size=50,
            rate_limit_rpm=60,
            retry_max_attempts=3,
            retry_backoff_seconds=2.0,
            privacy_class="internal_content",
            timeout_seconds=30,
        )

    def hydrate(
        self,
        registrations: tuple[ChannelRegistration, ...],
        since: datetime,
        program_id: str,
        config: EmailHydrationConfig,
        *,
        mode: HydrationMode = HydrationMode.FULL,
        run_ctx: RunContext,
    ) -> HydrationResult[EmailHydrationOutput]:
        del program_id, config, mode, run_ctx
        errors: list[IntegrationError] = []
        messages: list[EmailMessage] = []
        hydrated_ref_ids: list[tuple[str, str]] = []
        failed_ref_ids: list[tuple[str, str]] = []
        api_call_count = 0

        for registration in registrations:
            if registration.ref_kind != "email_thread":
                continue
            try:
                hydrated_messages, calls = self._hydrate_email_thread(registration, since)
                messages.extend(hydrated_messages)
                hydrated_ref_ids.append((registration.ref_id, registration.ref_kind))
                api_call_count += calls
            except (QueryError, RuntimeError, ValueError) as exc:
                errors.append(
                    IntegrationError(
                        source="email",
                        stage="hydration",
                        message=f"Failed to hydrate email_thread {registration.ref_id}: {exc}",
                        retryable=True,
                    )
                )
                failed_ref_ids.append((registration.ref_id, registration.ref_kind))

        return HydrationResult(
            channel="email",
            resources=EmailHydrationOutput(messages=tuple(messages)),
            api_call_count=api_call_count,
            errors=tuple(errors),
            hydrated_ref_ids=tuple(hydrated_ref_ids),
            failed_ref_ids=tuple(failed_ref_ids),
        )

    def _hydrate_email_thread(
        self,
        registration: ChannelRegistration,
        since: datetime,
    ) -> tuple[list[EmailMessage], int]:
        query = str((registration.metadata or {}).get("display_name") or registration.ref_title or registration.ref_id)
        target_thread_id = normalize_thread_id(registration.ref_id)
        page = self._mail_client.search_emails(query=query, limit=50)
        messages: list[EmailMessage] = []
        for record in page.records:
            normalized_record_thread_id = normalize_thread_id(record.thread_id or record.conversation_id)
            if normalized_record_thread_id != target_thread_id:
                continue
            sent_at = _parse_dt(record.received_at)
            if sent_at is not None and sent_at < since:
                continue
            messages.append(
                _mail_record_to_email_message(
                    record,
                    thread_id=target_thread_id or registration.ref_id,
                    workstream_ids=registration.workstream_ids,
                    work_item_ids=registration.work_item_ids,
                )
            )
        return messages, 1


def _mail_record_to_email_message(
    record: MailRecord,
    *,
    thread_id: str,
    workstream_ids: tuple[str, ...],
    work_item_ids: tuple[int, ...],
) -> EmailMessage:
    return EmailMessage(
        message_id=record.source_id or thread_id,
        thread_id=thread_id,
        subject=record.subject,
        sent_at=_parse_dt(record.received_at) or datetime.now(timezone.utc),
        preview=record.preview or record.subject or f"Email thread {thread_id}",
        sender=record.sender,
        recipients=record.recipients,
        permalink=record.web_url,
        workstream_ids=workstream_ids,
        work_item_ids=work_item_ids,
    )


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
