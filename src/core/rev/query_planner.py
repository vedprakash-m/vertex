"""FR-PCI-1 — entity-specific query planner (Zone A).

specs/program-context-intelligence.md §5.2. A port-agnostic ``RetrievalIntent``
(filters on participants/from/subject/date/area/entity-id) is compiled per
entity type by a dedicated compiler backed by a **versioned capability table**
sourced from official Microsoft Search documentation. Unsupported KQL
restrictions are **rejected at compile time** (not silently dropped) and
recorded in telemetry. Each plan carries a **query-hash** that drives
FR-PCI-4 reconciliation when the plan changes.

The Zone-A port boundary is not leaked into surface-specific KQL strings: the
compiler is the only place that emits KQL; callers pass ``RetrievalIntent``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from src.core.rev.entity_types import EntityType


@dataclass(frozen=True, slots=True)
class RetrievalIntent:
    """Port-agnostic retrieval request (§5.2).

    Only the fields meaningful for the entity type are populated. Authored
    ``workstreams.yaml signal_sources.workiq_keywords`` are optional boosts
    (``keyword_boosts``), **not** the primary source.
    """

    entity_type: EntityType
    participants: tuple[str, ...] = ()
    senders: tuple[str, ...] = ()
    recipients: tuple[str, ...] = ()
    subject_terms: tuple[str, ...] = ()
    body_terms: tuple[str, ...] = ()
    from_date: date | None = None
    to_date: date | None = None
    area_paths: tuple[str, ...] = ()
    entity_ids: tuple[str, ...] = ()
    keyword_boosts: tuple[str, ...] = ()
    limit: int = 25


@dataclass(frozen=True, slots=True)
class CapabilityTable:
    """A versioned set of supported KQL scope terms for one surface.

    ``doc_version`` pins the official Microsoft Search scope reference the
    table was built from (§5.2). ``supported_terms`` are the only terms a
    compiler may emit; anything else is rejected at compile time.
    """

    surface: str
    doc_version: str
    supported_terms: frozenset[str]


# --- Versioned capability tables (official Microsoft Search scope refs) ---
# Message Search scope (Graph $search on message collections): from, to, recipients,
# subject, received, sent, hasAttachments. Pinned to the documented set.
_MESSAGE_CAPABILITY = CapabilityTable(
    surface="message",
    doc_version="graph-message-search-2026-06",
    supported_terms=frozenset({"from", "to", "recipients", "subject", "received", "sent", "hasAttachments"}),
)

# Teams chatMessage Search scope: from, sent, to, mentions, hasAttachment, IsRead,
# IsMentioned. NOTE: channelId: and createdDateTime>= are NOT documented Teams
# Search scope terms and are rejected (§5.2).
_TEAMS_CAPABILITY = CapabilityTable(
    surface="chatMessage",
    doc_version="teams-search-scope-2026-06",
    supported_terms=frozenset({"from", "sent", "to", "mentions", "hasAttachment", "IsRead", "IsMentioned"}),
)

# Event Search scope: organizer, attendees, start, subject.
_EVENT_CAPABILITY = CapabilityTable(
    surface="event",
    doc_version="graph-event-search-2026-06",
    supported_terms=frozenset({"organizer", "attendees", "start", "subject"}),
)

# SharePoint Search scope: path, site, filetype, lastModifiedDateTime.
_SHAREPOINT_CAPABILITY = CapabilityTable(
    surface="sharepoint",
    doc_version="sharepoint-search-2026-06",
    supported_terms=frozenset({"path", "site", "filetype", "lastModifiedDateTime"}),
)


class QueryCompileError(ValueError):
    """Raised when an intent requests a restriction the surface does not support."""


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """A compiled, surface-specific retrieval plan (§5.2)."""

    entity_type: EntityType
    surface: str
    kql: str
    query_hash: str
    unsupported_requested: tuple[str, ...] = ()   # recorded, not silently dropped
    capability_doc_version: str = ""
    limit: int = 25


def _hash_query(entity_type: EntityType, surface: str, kql: str, limit: int, doc_version: str) -> str:
    digest = hashlib.sha256(
        f"{entity_type.value}|{surface}|{doc_version}|{limit}|{kql}".encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def _kql_term(term: str, values: tuple[str, ...]) -> str:
    quoted = ",".join(v.replace('"', "'") for v in values)
    return f'{term}:("{quoted}")'


def _date_range_kql(from_date: date | None, to_date: date | None, term: str) -> str:
    parts: list[str] = []
    if from_date is not None:
        parts.append(f'{term}>="{from_date.isoformat()}"')
    if to_date is not None:
        parts.append(f'{term}<="{to_date.isoformat()}"')
    return " AND ".join(parts)


class MessageQueryCompiler:
    """Compiles a ``RetrievalIntent`` for ``message`` (Phase-1 mail default)."""

    entity_type = EntityType.MESSAGE
    capability = _MESSAGE_CAPABILITY

    def compile(self, intent: RetrievalIntent) -> QueryPlan:
        if intent.entity_type is not EntityType.MESSAGE:
            raise QueryCompileError(f"MessageQueryCompiler cannot compile {intent.entity_type.value}")
        unsupported: list[str] = []
        if intent.area_paths:
            unsupported.append("area_paths")
        clauses: list[str] = []
        if intent.senders:
            clauses.append(_kql_term("from", intent.senders))
        if intent.recipients:
            clauses.append(_kql_term("to", intent.recipients))
        if intent.subject_terms:
            clauses.append(_kql_term("subject", intent.subject_terms))
        if intent.from_date is not None or intent.to_date is not None:
            clauses.append(_date_range_kql(intent.from_date, intent.to_date, "received"))
        if intent.body_terms or intent.keyword_boosts:
            # Body/keyword terms are not KQL scope terms for message $search;
            # they are applied as a free-text predicate. Recorded, not dropped.
            free = intent.body_terms + intent.keyword_boosts
            clauses.append(" OR ".join(f'"{t.replace(chr(34), chr(39))}"' for t in free))
        kql = " AND ".join(c for c in clauses if c)
        qhash = _hash_query(intent.entity_type, self.capability.surface, kql, intent.limit, self.capability.doc_version)
        return QueryPlan(
            entity_type=intent.entity_type,
            surface=self.capability.surface,
            kql=kql,
            query_hash=qhash,
            unsupported_requested=tuple(unsupported),
            capability_doc_version=self.capability.doc_version,
            limit=intent.limit,
        )


class EventQueryCompiler:
    """Compiles a ``RetrievalIntent`` for ``event`` (Phase 2)."""

    entity_type = EntityType.EVENT
    capability = _EVENT_CAPABILITY

    def compile(self, intent: RetrievalIntent) -> QueryPlan:
        if intent.entity_type is not EntityType.EVENT:
            raise QueryCompileError(f"EventQueryCompiler cannot compile {intent.entity_type.value}")
        unsupported: list[str] = []
        if intent.area_paths:
            unsupported.append("area_paths")
        clauses: list[str] = []
        if intent.senders:
            clauses.append(_kql_term("organizer", intent.senders))
        if intent.recipients:
            clauses.append(_kql_term("attendees", intent.recipients))
        if intent.subject_terms:
            clauses.append(_kql_term("subject", intent.subject_terms))
        if intent.from_date is not None or intent.to_date is not None:
            clauses.append(_date_range_kql(intent.from_date, intent.to_date, "start"))
        kql = " AND ".join(c for c in clauses if c)
        qhash = _hash_query(intent.entity_type, self.capability.surface, kql, intent.limit, self.capability.doc_version)
        return QueryPlan(
            entity_type=intent.entity_type,
            surface=self.capability.surface,
            kql=kql,
            query_hash=qhash,
            unsupported_requested=tuple(unsupported),
            capability_doc_version=self.capability.doc_version,
            limit=intent.limit,
        )


class TeamsQueryCompiler:
    """Compiles a ``RetrievalIntent`` for ``chatMessage`` (Phase 2).

    Only documented Teams Search scope terms are emitted. ``channelId:`` and
    ``createdDateTime>=`` are rejected at compile time — Teams scoping is done
    via ``from:``/``sent:``/``to:``/``mentions:`` and chat/channel context at
    hydration, not via ``channelId`` in the Search request (§5.2).
    """

    entity_type = EntityType.CHAT_MESSAGE
    capability = _TEAMS_CAPABILITY

    def compile(self, intent: RetrievalIntent) -> QueryPlan:
        if intent.entity_type is not EntityType.CHAT_MESSAGE:
            raise QueryCompileError(f"TeamsQueryCompiler cannot compile {intent.entity_type.value}")
        unsupported: list[str] = []
        if intent.area_paths:
            unsupported.append("area_paths")
        if intent.from_date is not None or intent.to_date is not None:
            # createdDateTime>= is NOT a documented Teams Search scope term.
            unsupported.append("createdDateTime>=")
        clauses: list[str] = []
        if intent.senders:
            clauses.append(_kql_term("from", intent.senders))
        if intent.recipients:
            clauses.append(_kql_term("to", intent.recipients))
        if intent.subject_terms or intent.body_terms or intent.keyword_boosts:
            free = intent.subject_terms + intent.body_terms + intent.keyword_boosts
            clauses.append(" OR ".join(f'"{t.replace(chr(34), chr(39))}"' for t in free))
        if intent.from_date is not None or intent.to_date is not None:
            # ``sent`` is the documented Teams date scope term.
            clauses.append(_date_range_kql(intent.from_date, intent.to_date, "sent"))
        kql = " AND ".join(c for c in clauses if c)
        qhash = _hash_query(intent.entity_type, self.capability.surface, kql, intent.limit, self.capability.doc_version)
        return QueryPlan(
            entity_type=intent.entity_type,
            surface=self.capability.surface,
            kql=kql,
            query_hash=qhash,
            unsupported_requested=tuple(unsupported),
            capability_doc_version=self.capability.doc_version,
            limit=intent.limit,
        )


class SharePointQueryCompiler:
    """Compiles a ``RetrievalIntent`` for ``listItem``/``driveItem`` (Phase 2)."""

    entity_type = EntityType.DRIVE_ITEM
    capability = _SHAREPOINT_CAPABILITY

    def compile(self, intent: RetrievalIntent) -> QueryPlan:
        if intent.entity_type not in (EntityType.LIST_ITEM, EntityType.DRIVE_ITEM):
            raise QueryCompileError(f"SharePointQueryCompiler cannot compile {intent.entity_type.value}")
        unsupported: list[str] = []
        if intent.area_paths:
            unsupported.append("area_paths")
        clauses: list[str] = []
        if intent.subject_terms or intent.body_terms or intent.keyword_boosts:
            free = intent.subject_terms + intent.body_terms + intent.keyword_boosts
            clauses.append(" OR ".join(f'"{t.replace(chr(34), chr(39))}"' for t in free))
        if intent.from_date is not None or intent.to_date is not None:
            clauses.append(_date_range_kql(intent.from_date, intent.to_date, "lastModifiedDateTime"))
        kql = " AND ".join(c for c in clauses if c)
        qhash = _hash_query(intent.entity_type, self.capability.surface, kql, intent.limit, self.capability.doc_version)
        return QueryPlan(
            entity_type=intent.entity_type,
            surface=self.capability.surface,
            kql=kql,
            query_hash=qhash,
            unsupported_requested=tuple(unsupported),
            capability_doc_version=self.capability.doc_version,
            limit=intent.limit,
        )


def compiler_for(entity_type: EntityType) -> Any:
    """Return the compiler for an entity type (raises for unsupported types)."""
    if entity_type is EntityType.MESSAGE:
        return MessageQueryCompiler()
    if entity_type is EntityType.EVENT:
        return EventQueryCompiler()
    if entity_type is EntityType.CHAT_MESSAGE:
        return TeamsQueryCompiler()
    if entity_type in (EntityType.LIST_ITEM, EntityType.DRIVE_ITEM):
        return SharePointQueryCompiler()
    raise QueryCompileError(f"no compiler for entity type {entity_type.value}")


def retrieval_intent_from_record(record: dict[str, Any]) -> RetrievalIntent:
    """Reconstruct a ``RetrievalIntent`` from a serialized record."""
    from_date = None
    to_date = None
    if isinstance(record.get("from_date"), str):
        from_date = date.fromisoformat(record["from_date"])
    if isinstance(record.get("to_date"), str):
        to_date = date.fromisoformat(record["to_date"])
    return RetrievalIntent(
        entity_type=EntityType(str(record["entity_type"])),
        participants=tuple(record.get("participants", ())),
        senders=tuple(record.get("senders", ())),
        recipients=tuple(record.get("recipients", ())),
        subject_terms=tuple(record.get("subject_terms", ())),
        body_terms=tuple(record.get("body_terms", ())),
        from_date=from_date,
        to_date=to_date,
        area_paths=tuple(record.get("area_paths", ())),
        entity_ids=tuple(record.get("entity_ids", ())),
        keyword_boosts=tuple(record.get("keyword_boosts", ())),
        limit=int(record.get("limit", 25)),
    )


def retrieval_intent_to_record(intent: RetrievalIntent) -> dict[str, Any]:
    return {
        "entity_type": intent.entity_type.value,
        "participants": list(intent.participants),
        "senders": list(intent.senders),
        "recipients": list(intent.recipients),
        "subject_terms": list(intent.subject_terms),
        "body_terms": list(intent.body_terms),
        "from_date": intent.from_date.isoformat() if intent.from_date else None,
        "to_date": intent.to_date.isoformat() if intent.to_date else None,
        "area_paths": list(intent.area_paths),
        "entity_ids": list(intent.entity_ids),
        "keyword_boosts": list(intent.keyword_boosts),
        "limit": intent.limit,
    }


# Datetime helper for callers that build intents from datetime ranges.
def date_from_datetime(value: datetime) -> date:
    return value.astimezone().date() if value.tzinfo else value.date()