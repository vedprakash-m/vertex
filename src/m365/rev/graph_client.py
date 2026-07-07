"""REV Graph client interface + fake (Zone C).

specs/program-context-intelligence.md §5.3/§5.5. A **mockable** Microsoft Graph
client surface for the REV mail pipeline. The real Graph integration (direct
delegated Graph via ``DeviceCodeCredential`` or the Agency/WorkIQ bridge) is
**P0 operator-gated** (live M365 consent) and lives behind this interface; P1
ships the mail walking skeleton against ``FakeRevGraphClient`` so the pipeline
is fully testable without consent.

Zone C: imports only ``src.core.*`` + stdlib — never ``src.ai`` /
``src.commands``. Returns the typed ``PortResult`` union (§5.3) so callers
never see cross-boundary exceptions; a Graph 429 → ``RateLimited``, a 403 →
``Forbidden``, an unsupported surface → ``Unsupported``.

The client speaks raw Graph-shaped records (``GraphMessageHit`` /
``GraphMessage`` / ``GraphSearchHit``); the enumerators and hydrator adapt
those to the Zone-A port contracts (``EnumeratedCandidate`` /
``HydratedContent``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from src.core.rev.result import (
    Forbidden,
    Incomplete,
    PortResult,
    RateLimited,
    Success,
    Unsupported,
)


# --- minimal KQL parser for the fake (subset of Graph message KQL) ---
_KQL_FIELD_PARENS_RE = re.compile(r'(\w+):\(([^)]*)\)')
_KQL_QUOTED_RE = re.compile(r'"([^"]+)"')
_KQL_DATE_RE = re.compile(r'(received|from)\s*(>=|<=|>|<)\s*"(\d{4}-\d{2}-\d{2})"')


def _parse_kql(kql: str) -> dict[str, Any]:
    """Parse the subset of Graph message KQL the planners emit.

    Returns ``{subject_terms, sender_terms, body_terms, from_date, to_date}``.
    Used only by ``FakeRevGraphClient`` to simulate server-side matching; the
    real Graph does this server-side.
    """
    subject_terms: list[str] = []
    sender_terms: list[str] = []
    body_terms: list[str] = []
    from_date: str | None = None
    to_date: str | None = None

    # field:(...) groups
    for field_name, group in _KQL_FIELD_PARENS_RE.findall(kql):
        terms = [t.strip().strip('"').lower() for t in group.split() if t.strip()]
        fname = field_name.lower()
        if fname == "subject":
            subject_terms.extend(terms)
        elif fname in ("from", "sender"):
            sender_terms.extend(terms)
        else:
            body_terms.extend(terms)
    # strip field:(...) and field>=... so the bare-quote pass doesn't pick up field names
    stripped = _KQL_FIELD_PARENS_RE.sub(" ", kql)
    stripped = _KQL_DATE_RE.sub(" ", stripped)
    # date bounds
    for _field, op, dval in _KQL_DATE_RE.findall(kql):
        if op in (">", ">="):
            from_date = dval
        elif op in ("<", "<="):
            to_date = dval
    # bare quoted free-text terms (body)
    for term in _KQL_QUOTED_RE.findall(stripped):
        body_terms.append(term.lower())
    return {
        "subject_terms": subject_terms,
        "sender_terms": sender_terms,
        "body_terms": body_terms,
        "from_date": from_date,
        "to_date": to_date,
    }


def _matches(message_fields: dict[str, str], parsed: dict[str, Any]) -> bool:
    subject = message_fields.get("subject", "").lower()
    sender = message_fields.get("sender", "").lower()
    body = message_fields.get("body", "").lower()
    received = message_fields.get("received_at", "")[:10]
    for term in parsed["subject_terms"]:
        if term not in subject:
            return False
    for term in parsed["sender_terms"]:
        if term not in sender:
            return False
    for term in parsed["body_terms"]:
        if term not in f"{subject} {body} {sender}":
            return False
    if parsed["from_date"] and received and received < parsed["from_date"]:
        return False
    if parsed["to_date"] and received and received > parsed["to_date"]:
        return False
    return True


@dataclass(frozen=True, slots=True)
class GraphMessageHit:
    """One row from a collection ``$search`` on messages (native ``message.id``)."""

    message_id: str             # native message.id — deterministic mail path (§5.3)
    subject: str = ""
    sender: str = ""
    received_at: str = ""       # ISO-8601
    preview: str = ""
    conversation_id: str = ""
    web_url: str = ""


@dataclass(frozen=True, slots=True)
class GraphSearchHit:
    """One row from the secondary Search API (Microsoft Graph ``/search/query``).

    Search-API hits carry a ``hitId`` that may differ from native ``message.id``
    (rendezvous IDs / immutable IDs); the ``SearchHitLocator`` resolves them to
    a deterministic ``HydrationLocator`` via a follow-up lookup.
    """

    hit_id: str
    resource_id: str | None     # native message.id if the hit includes it
    subject: str = ""
    sender: str = ""
    received_at: str = ""
    preview: str = ""
    container_id: str = ""


@dataclass(frozen=True, slots=True)
class GraphMessage:
    """A hydrated message with the body ladder (§5.5/§5.6).

    ``unique_body`` is the preferred canonical body (de-duplicated across the
    conversation); ``body`` is the full per-message body; ``conversation_id``
    drives the conversation rung; ``has_attachments`` gates the attachment rung.
    """

    message_id: str
    subject: str = ""
    sender: str = ""
    received_at: str = ""
    unique_body: str = ""
    body: str = ""
    body_content_type: str = "text"   # "text" | "html"
    unique_body_content_type: str = "text"
    conversation_id: str = ""
    has_attachments: bool = False
    web_url: str = ""
    etag: str = ""
    immutable_id: str = ""


class RevGraphClient(Protocol):
    """Mockable Graph surface for the REV mail pipeline (§5.3/§5.5)."""

    def collection_search_messages(
        self,
        *,
        kql: str,
        limit: int,
        correlation_id: str = "",
    ) -> PortResult[tuple[GraphMessageHit, ...]]:
        ...

    def search_api_messages(
        self,
        *,
        kql: str,
        limit: int,
        correlation_id: str = "",
    ) -> PortResult[tuple[GraphSearchHit, ...]]:
        ...

    def get_message(
        self,
        *,
        mailbox: str,
        message_id: str,
        correlation_id: str = "",
    ) -> PortResult[GraphMessage]:
        ...


class FakeRevGraphClient:
    """In-memory Graph client for P1 tests / mail walking skeleton (no consent).

    Holds a fixture list of ``GraphMessage`` objects and serves collection
    ``$search`` (substring match on subject/body/sender), the Search API
    (same fixtures, returned as ``GraphSearchHit``), and ``get_message``.
    Simulates 429/403 by message-id deny-lists so the governor + result-union
    paths are exercised without a live tenant.
    """

    def __init__(
        self,
        messages: tuple[GraphMessage, ...] = (),
        *,
        rate_limited_ids: frozenset[str] = frozenset(),
        forbidden_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._messages: dict[str, GraphMessage] = {m.message_id: m for m in messages}
        self._rate_limited_ids = rate_limited_ids
        self._forbidden_ids = forbidden_ids
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._rate_limited_once: set[str] = set()

    def collection_search_messages(
        self,
        *,
        kql: str,
        limit: int,
        correlation_id: str = "",
    ) -> PortResult[tuple[GraphMessageHit, ...]]:
        self.calls.append(("collection_search_messages", {"kql": kql, "limit": limit, "correlation_id": correlation_id}))
        parsed = _parse_kql(kql)
        hits: list[GraphMessageHit] = []
        for message in self._messages.values():
            if not _matches(
                {"subject": message.subject, "sender": message.sender, "body": message.body, "received_at": message.received_at},
                parsed,
            ):
                continue
            hits.append(
                GraphMessageHit(
                    message_id=message.message_id,
                    subject=message.subject,
                    sender=message.sender,
                    received_at=message.received_at,
                    preview=message.body[:160],
                    conversation_id=message.conversation_id,
                    web_url=message.web_url,
                )
            )
            if len(hits) >= limit:
                break
        return Success(tuple(hits))

    def search_api_messages(
        self,
        *,
        kql: str,
        limit: int,
        correlation_id: str = "",
    ) -> PortResult[tuple[GraphSearchHit, ...]]:
        self.calls.append(("search_api_messages", {"kql": kql, "limit": limit, "correlation_id": correlation_id}))
        parsed = _parse_kql(kql)
        hits: list[GraphSearchHit] = []
        for message in self._messages.values():
            if not _matches(
                {"subject": message.subject, "sender": message.sender, "body": message.body, "received_at": message.received_at},
                parsed,
            ):
                continue
            hits.append(
                GraphSearchHit(
                    hit_id=f"search:{message.message_id}",
                    resource_id=message.message_id,
                    subject=message.subject,
                    sender=message.sender,
                    received_at=message.received_at,
                    preview=message.body[:160],
                    container_id=message.conversation_id,
                )
            )
            if len(hits) >= limit:
                break
        return Success(tuple(hits))

    def get_message(
        self,
        *,
        mailbox: str,
        message_id: str,
        correlation_id: str = "",
    ) -> PortResult[GraphMessage]:
        self.calls.append(("get_message", {"mailbox": mailbox, "message_id": message_id, "correlation_id": correlation_id}))
        if message_id in self._rate_limited_ids and message_id not in self._rate_limited_once:
            self._rate_limited_once.add(message_id)
            return RateLimited(provider="graph", retry_after_seconds=1.0)
        if message_id in self._forbidden_ids:
            return Forbidden(scope="mail", reason="forbidden_by_fake")
        message = self._messages.get(message_id)
        if message is None:
            # Not-found: carry an empty shell as the salvage so the result is
            # type-correct (Incomplete[GraphMessage]); the hydrator's
            # Incomplete branch takes the metadata-only fallback and never
            # reads this value.
            return Incomplete(GraphMessage(message_id=message_id), reason=f"message_not_found:{message_id}")
        return Success(message)


__all__ = [
    "RevGraphClient",
    "FakeRevGraphClient",
    "GraphMessage",
    "GraphMessageHit",
    "GraphSearchHit",
]