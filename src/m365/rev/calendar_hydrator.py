"""REV calendar hydrator (Zone C) — FR-PCI-5 / REV-11.

specs/program-context-intelligence.md §5.4/§5.6. Hydrates one enumerated
calendar event candidate to canonical text + chunks.

**Hydration path (§5.5)**:
    GET /events/{id}?$select=body,iCalUId,seriesMasterId + Prefer: IdType="ImmutableId"

Key identity note: ``seriesMasterId`` is the series identity for recurring
events — **NOT** ``iCalUId`` (which differs per occurrence). ``ImmutableId`` is
established at the hydration GET via the ``Prefer`` header, not at enumeration.

**P2 operator-gated.** Requires ``Calendars.Read`` live consent (RV-S1-CALENDAR).
The live path returns ``Unsupported`` with the spike reference until consent is
granted. The fake path is used for tests + ``--mock-fixture``.

Zone C: imports only ``src.core.*`` + stdlib.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from src.core.rev.entity_types import EntityType
from src.core.rev.identity import CanonicalItemIdentity
from src.core.rev.normalizer import normalize
from src.core.rev.ports import EnumeratedCandidate, HydratedContent
from src.core.rev.privacy import run_local_checks
from src.core.rev.result import (
    Forbidden,
    Incomplete,
    PortResult,
    RateLimited,
    Success,
    Unsupported,
)

log = logging.getLogger(__name__)

RUNG_EVENT_BODY = "event_body"
RUNG_EVENT_METADATA_ONLY = "metadata_only_flagged"

_RV_S1_CALENDAR_GATE = (
    "CalendarHydrator: live calendar hydration requires RV-S1-CALENDAR operator spike. "
    "Confirm: (1) Calendars.Read consent granted, (2) GET /events/{id} with "
    "Prefer: IdType=ImmutableId returns body + seriesMasterId, (3) seriesMasterId "
    "(not iCalUId) is the series identity. Run 'vertex rev run --mock-fixture' for P1."
)


# ---------------------------------------------------------------------------
# Graph event data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GraphEvent:
    """A hydrated calendar event (§5.4/§5.5).

    ``series_master_id`` is the series identity for recurring events (NOT
    ``i_cal_uid`` which differs per occurrence). ``immutable_id`` is established
    at the hydration GET via ``Prefer: IdType="ImmutableId"``.
    """

    event_id: str
    subject: str = ""
    organizer: str = ""
    start_datetime: str = ""
    end_datetime: str = ""
    body: str = ""
    body_content_type: str = "text"
    i_cal_uid: str = ""
    series_master_id: str | None = None   # non-None for recurring series occurrences
    etag: str = ""
    immutable_id: str = ""
    is_all_day: bool = False
    attendees: tuple[str, ...] = ()
    online_meeting_url: str = ""


class RevCalendarClient(Protocol):
    """Mockable Graph surface for the REV calendar pipeline."""

    def get_event(
        self,
        *,
        event_id: str,
        correlation_id: str = "",
    ) -> PortResult[GraphEvent]:
        ...

    def list_events(
        self,
        *,
        start_datetime: str,
        end_datetime: str,
        kql: str = "",
        limit: int = 25,
        correlation_id: str = "",
    ) -> PortResult[tuple[GraphEvent, ...]]:
        ...


class FakeRevCalendarClient:
    """In-memory calendar client for tests / ``--mock-fixture``."""

    def __init__(
        self,
        events: tuple[GraphEvent, ...] = (),
        *,
        rate_limited_ids: frozenset[str] = frozenset(),
        forbidden_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._events: dict[str, GraphEvent] = {e.event_id: e for e in events}
        self._rate_limited_ids = rate_limited_ids
        self._forbidden_ids = forbidden_ids
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_event(
        self,
        *,
        event_id: str,
        correlation_id: str = "",
    ) -> PortResult[GraphEvent]:
        self.calls.append(("get_event", {"event_id": event_id}))
        if event_id in self._rate_limited_ids:
            return RateLimited(provider="graph_calendar", retry_after_seconds=1.0)
        if event_id in self._forbidden_ids:
            return Forbidden(scope="calendars", reason="forbidden_by_fake")
        event = self._events.get(event_id)
        if event is None:
            return Incomplete(GraphEvent(event_id=event_id), reason=f"event_not_found:{event_id}")
        return Success(event)

    def list_events(
        self,
        *,
        start_datetime: str,
        end_datetime: str,
        kql: str = "",
        limit: int = 25,
        correlation_id: str = "",
    ) -> PortResult[tuple[GraphEvent, ...]]:
        self.calls.append(("list_events", {"start": start_datetime, "end": end_datetime}))
        events = list(self._events.values())[:limit]
        return Success(tuple(events))


# ---------------------------------------------------------------------------
# CalendarHydrator
# ---------------------------------------------------------------------------


@dataclass
class CalendarContext:
    """Calendar connection context (mirrors MailboxContext for mail)."""

    tenant_id: str
    principal_mailbox: str
    calendar_id: str = "primary"
    max_body_bytes: int = 1_048_576


class CalendarHydrator:
    """ContentHydrator for calendar events (§5.6 fixed-order normalization).

    **P2 operator-gated** for the live Graph path; the fake client makes the
    full value chain testable without consent.
    """

    entity_type = EntityType.EVENT

    def __init__(
        self,
        client: Any,    # RevCalendarClient | FakeRevCalendarClient
        context: CalendarContext,
    ) -> None:
        self._client = client
        self._context = context

    def hydrate(
        self,
        candidate: EnumeratedCandidate,
        *,
        correlation_id: str,
    ) -> PortResult[HydratedContent]:
        event_id = candidate.locator.resource_id
        result = self._client.get_event(event_id=event_id, correlation_id=correlation_id)
        if isinstance(result, (Forbidden, RateLimited, Unsupported)):
            return result
        if isinstance(result, Incomplete):
            return self._metadata_only_fallback(candidate, correlation_id, reason=result.reason)
        event: GraphEvent = result.value

        if not event.body:
            return self._metadata_only_fallback(candidate, correlation_id, reason="no_event_body")

        # Privacy gate (§5.7 Stage 1 step 2) — fail-closed before normalization.
        local = run_local_checks(
            event.body,
            source_type=self.entity_type,
            sensitivity_label=None,
            max_bytes=self._context.max_body_bytes,
        )
        if not local.passed:
            if local.quarantined:
                return Forbidden(scope="content", reason=local.reason)
            if local.sensitivity_denied:
                return Forbidden(scope="sensitivity", reason=local.reason)
            return Unsupported(entity_type=self.entity_type.value, reason=local.reason or "local_check_failed")

        norm = normalize(event.body, is_html=event.body_content_type.lower() == "html", source_type=self.entity_type)
        identity = CanonicalItemIdentity(
            source_type=self.entity_type,
            tenant_id=self._context.tenant_id,
            principal_mailbox=self._context.principal_mailbox,
            container=self._context.calendar_id,
            resource_id=event.event_id,
            immutable_id=event.immutable_id or None,
            etag=event.etag or None,
        )
        route_meta: dict[str, Any] = {
            "subject": event.subject,
            "organizer": event.organizer,
            "start_datetime": event.start_datetime,
            "end_datetime": event.end_datetime,
            "i_cal_uid": event.i_cal_uid,
            "series_master_id": event.series_master_id,
            "rung": RUNG_EVENT_BODY,
        }
        return Success(
            HydratedContent(
                identity=identity,
                canonical_text=norm.canonical_text,
                normalized_source_hash=norm.normalized_source_hash,
                chunks=norm.chunks,
                route_metadata=route_meta,
                hydration_rung=RUNG_EVENT_BODY,
                metadata_only=False,
                retrieved_at=None,
                correlation_id=correlation_id,
            )
        )

    def _metadata_only_fallback(
        self,
        candidate: EnumeratedCandidate,
        correlation_id: str,
        *,
        reason: str,
    ) -> PortResult[HydratedContent]:
        log.warning("rev.calendar.hydrate metadata_only cid=%s reason=%s", correlation_id, reason)
        identity = CanonicalItemIdentity(
            source_type=self.entity_type,
            tenant_id=self._context.tenant_id,
            principal_mailbox=self._context.principal_mailbox,
            container=self._context.calendar_id,
            resource_id=candidate.locator.resource_id,
        )
        subject = str(candidate.partial_metadata.get("subject", ""))
        norm = normalize(subject, is_html=False, source_type=self.entity_type)
        return Success(
            HydratedContent(
                identity=identity,
                canonical_text=norm.canonical_text,
                normalized_source_hash=norm.normalized_source_hash,
                chunks=norm.chunks,
                route_metadata={"fallback_reason": reason},
                hydration_rung=RUNG_EVENT_METADATA_ONLY,
                metadata_only=True,
                retrieved_at=None,
                correlation_id=correlation_id,
            )
        )


class LiveCalendarHydrator:
    """Operator-gated live CalendarHydrator stub (§5.4 / RV-S1-CALENDAR).

    Returns ``Unsupported`` with the spike reference until Calendars.Read
    consent is granted. Once wired (post-spike), replace this with the real
    implementation that calls ``GET /events/{id}?$select=body,iCalUId,seriesMasterId``
    with ``Prefer: IdType="ImmutableId"``.
    """

    entity_type = EntityType.EVENT

    def hydrate(
        self,
        candidate: EnumeratedCandidate,
        *,
        correlation_id: str,
    ) -> PortResult[HydratedContent]:
        log.warning(_RV_S1_CALENDAR_GATE)
        return Unsupported(
            entity_type=EntityType.EVENT.value,
            reason="rv_s1_calendar_gate: live calendar hydration requires Calendars.Read consent",
        )


__all__ = [
    "GraphEvent",
    "RevCalendarClient",
    "FakeRevCalendarClient",
    "CalendarContext",
    "CalendarHydrator",
    "LiveCalendarHydrator",
    "RUNG_EVENT_BODY",
    "RUNG_EVENT_METADATA_ONLY",
]
