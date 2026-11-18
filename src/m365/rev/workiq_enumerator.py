"""WorkIQ acquisition behind the REV ports (RELO-W3.1, Tech Spec §8.3.4).

Zone C adapter that wraps an injected WorkIQ query callable and exposes it as a
``CandidateEnumerator`` / ``ContentHydrator``. It is not wired into the live
gather pipeline (``src/commands/gather_workiq*``); that path is unchanged.

The query callable takes a ``RetrievalIntent`` and returns WorkIQ records
(dicts). The enumerator is metadata-first and deterministic: candidates are
ordered newest-first by UTC timestamp, then by resource id, deduped by resource
id and cut at ``intent.limit``. Exceptions never cross the port boundary.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.core.rev.entity_types import EntityType
from src.core.rev.identity import CanonicalItemIdentity, HydrationLocator
from src.core.rev.normalizer import normalize
from src.core.rev.ports import EnumeratedCandidate, HydratedContent
from src.core.rev.privacy import run_local_checks
from src.core.rev.query_planner import RetrievalIntent
from src.core.rev.result import (
    Forbidden,
    Incomplete,
    PortResult,
    Success,
    Unsupported,
)
from src.m365.rev.hydrator import RUNG_METADATA_ONLY

ENUMERATOR_LABEL = "workiq"
RUNG_WORKIQ_PREVIEW = "workiq_preview"

WorkIQQuery = Callable[[RetrievalIntent], Sequence[Mapping[str, Any]]]

_TIMESTAMP_KEYS = ("receivedDateTime", "sentDateTime", "createdDateTime", "timestamp", "startDateTime")
_ID_KEYS = ("id", "messageId", "emailId", "eventId", "transcriptId")
_THREAD_KEYS = ("conversationId", "threadId")
_TRANSIENT_ID_RE = re.compile(r"^turn\d+(?:search|result)\d+$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class WorkIQContext:
    tenant_id: str
    principal_mailbox: str
    container: str = "workiq"


def _text(record: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _native_id(value: str) -> str:
    if not value or "://" in value or value.lower().startswith("semantic:"):
        return ""
    if _TRANSIENT_ID_RE.match(value):
        return ""
    return value


def _utc_timestamp(record: Mapping[str, Any]) -> datetime | None:
    raw = _text(record, *_TIMESTAMP_KEYS)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _sender(record: Mapping[str, Any]) -> str:
    value = record.get("from") or record.get("sender")
    if isinstance(value, Mapping):
        address = value.get("emailAddress", value)
        if isinstance(address, Mapping):
            return str(address.get("address") or address.get("name") or "")
        return ""
    return value.strip() if isinstance(value, str) else ""


class WorkIQEnumerator:
    """``CandidateEnumerator`` over an injected WorkIQ query callable."""

    def __init__(
        self,
        query: WorkIQQuery,
        context: WorkIQContext,
        *,
        entity_type: EntityType = EntityType.MESSAGE,
    ) -> None:
        self._query = query
        self._context = context
        self.entity_type = entity_type

    def enumerate(
        self, intent: RetrievalIntent, *, correlation_id: str
    ) -> PortResult[tuple[EnumeratedCandidate, ...]]:
        if intent.entity_type is not self.entity_type:
            return Unsupported(
                entity_type=str(intent.entity_type.value),
                reason=f"workiq enumerator bound to {self.entity_type.value}",
            )
        try:
            records = list(self._query(intent))
        except Exception as exc:  # noqa: BLE001 - exceptions must not cross the port
            return Incomplete(value=(), reason=f"provider_error:{type(exc).__name__}")

        by_id: dict[str, EnumeratedCandidate] = {}
        unidentified = 0
        for record in records:
            candidate = self._candidate(record, correlation_id)
            if candidate is None:
                unidentified += 1
                continue
            by_id.setdefault(candidate.locator.resource_id, candidate)

        ordered = sorted(
            by_id.values(),
            key=lambda c: (
                c.received_at is None,
                -(c.received_at.timestamp() if c.received_at else 0.0),
                c.locator.resource_id,
            ),
        )
        page = tuple(ordered[: max(intent.limit, 0)])
        if len(ordered) > len(page):
            return Incomplete(value=page, reason="page_cut")
        if unidentified:
            return Incomplete(value=page, reason=f"unidentified_records:{unidentified}")
        return Success(page)

    def _candidate(self, record: Mapping[str, Any], correlation_id: str) -> EnumeratedCandidate | None:
        if not isinstance(record, Mapping):
            return None
        thread_id = _native_id(_text(record, *_THREAD_KEYS))
        resource_id = _native_id(_text(record, *_ID_KEYS))
        identity_kind = "native_message"
        if not resource_id:
            resource_id, identity_kind = thread_id, "native_thread"
        if not resource_id:
            return None
        received_at = _utc_timestamp(record)
        return EnumeratedCandidate(
            locator=HydrationLocator(
                source_type=self.entity_type,
                tenant_id=self._context.tenant_id,
                principal_mailbox=self._context.principal_mailbox,
                container=self._context.container,
                resource_id=resource_id,
            ),
            relevance_score=0.0,
            partial_metadata={
                "subject": _text(record, "subject", "title"),
                "sender": _sender(record),
                "preview": _text(record, "preview", "bodyPreview", "summary"),
                "received_at": received_at.isoformat() if received_at else None,
                "conversation_id": thread_id or None,
                "identity_kind": identity_kind,
                "web_link": _text(record, "webLink", "url") or None,
                "enumerator": ENUMERATOR_LABEL,
            },
            correlation_id=correlation_id,
            enumerator=ENUMERATOR_LABEL,
            received_at=received_at,
        )


class WorkIQHydrator:
    """``ContentHydrator`` over WorkIQ candidate metadata (no network).

    WorkIQ returns summary/preview text rather than full bodies, so the preview
    is the hydrated content; without one the result is metadata-only.
    """

    def __init__(self, *, max_body_bytes: int = 1_048_576) -> None:
        self._max_body_bytes = max_body_bytes

    def hydrate(self, candidate: EnumeratedCandidate, *, correlation_id: str) -> PortResult[HydratedContent]:
        locator = candidate.locator
        meta = candidate.partial_metadata
        subject = str(meta.get("subject") or "")
        preview = str(meta.get("preview") or "")
        text = "\n\n".join(part for part in (subject, preview) if part)

        local = run_local_checks(text, source_type=locator.source_type, max_bytes=self._max_body_bytes)
        if local.quarantined:
            return Forbidden(scope="content", reason=local.reason)
        if local.sensitivity_denied:
            return Forbidden(scope="sensitivity", reason=local.reason)
        if local.size_exceeded:
            return Unsupported(entity_type=locator.source_type.value, reason=local.reason)

        normalized = normalize(text, is_html=False, source_type=locator.source_type)
        return Success(
            HydratedContent(
                identity=CanonicalItemIdentity(
                    source_type=locator.source_type,
                    tenant_id=locator.tenant_id,
                    principal_mailbox=locator.principal_mailbox,
                    container=locator.container,
                    resource_id=locator.resource_id,
                ),
                canonical_text=normalized.canonical_text,
                normalized_source_hash=normalized.normalized_source_hash,
                chunks=normalized.chunks,
                route_metadata={
                    "route": ENUMERATOR_LABEL,
                    "conversation_id": meta.get("conversation_id"),
                    "identity_kind": meta.get("identity_kind"),
                    "received_at": meta.get("received_at"),
                },
                hydration_rung=RUNG_WORKIQ_PREVIEW if preview else RUNG_METADATA_ONLY,
                metadata_only=not preview,
                retrieved_at=candidate.received_at,
                correlation_id=correlation_id,
            )
        )
