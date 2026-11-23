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

import hashlib
import inspect
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path
from typing import Any

from src.core.ledger.candidate_store import PROGRAMS_ROOT
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
from src.core.rev.sync_state import SyncState, SyncStateStore
from src.m365.workiq_retriever import WorkIQRunBudget
from src.m365.rev.hydrator import RUNG_METADATA_ONLY

ENUMERATOR_LABEL = "workiq"
RUNG_WORKIQ_PREVIEW = "workiq_preview"

_TIMESTAMP_KEYS = ("receivedDateTime", "sentDateTime", "createdDateTime", "timestamp", "startDateTime")
_ID_KEYS = ("id", "messageId", "emailId", "eventId", "occurrenceId", "transcriptId")
_THREAD_KEYS = ("conversationId", "threadId")
_TRANSIENT_ID_RE = re.compile(r"^turn\d+(?:search|result)\d+$", re.IGNORECASE)
_COMPLETE_MARKER = "workiq_window_complete"
_EMPTY_COMPLETE_MARKER = "workiq_window_complete_empty"
_CURSOR_MAX_CHARS = 2048


@dataclass(frozen=True, slots=True)
class WorkIQContext:
    tenant_id: str
    principal_mailbox: str
    container: str = "workiq"


@dataclass(frozen=True, slots=True)
class WorkIQPage:
    records: tuple[Mapping[str, Any], ...]
    next_cursor: str | None
    rejected_source_time_count: int = 0
    rejected_identity_count: int = 0
    rejected_invalid_record_count: int = 0
    rejected_outside_window_count: int = 0
    response_usable: bool = True


@dataclass(frozen=True, slots=True)
class WorkIQQueryPage:
    records: tuple[Mapping[str, Any], ...]
    rejected_source_time_count: int = 0
    rejected_identity_count: int = 0
    rejected_invalid_record_count: int = 0
    rejected_outside_window_count: int = 0
    response_usable: bool = True


WorkIQQuery = Callable[..., Sequence[Mapping[str, Any]] | WorkIQQueryPage | WorkIQPage]


class _WorkIQBudgetExhausted(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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


def _first_native_id(record: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = _native_id(_text(record, key))
        if value:
            return value
    return ""


def _is_tombstone(record: Mapping[str, Any]) -> bool:
    return (
        isinstance(record.get("@removed"), Mapping)
        or record.get("@removed") is True
        or record.get("deleted") is True
        or record.get("changeType") in ("deleted", "removed")
        or record.get("status") in ("deleted", "cancelled", "canceled")
    )


def _utc_timestamp(record: Mapping[str, Any]) -> tuple[datetime | None, str]:
    raw = _text(record, *_TIMESTAMP_KEYS)
    if not raw:
        return None, "missing"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        return None, "invalid"
    if parsed.tzinfo is None:
        return None, "invalid"
    try:
        return parsed.astimezone(timezone.utc), "qualified"
    except (ValueError, OverflowError):
        return None, "invalid"


def _sender(record: Mapping[str, Any]) -> str:
    value = record.get("from") or record.get("sender")
    if isinstance(value, Mapping):
        address = value.get("emailAddress", value)
        if isinstance(address, Mapping):
            return str(address.get("address") or address.get("name") or "")
        return ""
    return value.strip() if isinstance(value, str) else ""


def _validated_cursor(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("WorkIQ continuation cursor must be a string")
    cursor = value.strip()
    if (
        not cursor
        or len(cursor) > _CURSOR_MAX_CHARS
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in cursor)
    ):
        raise ValueError("WorkIQ continuation cursor is invalid")
    return cursor


class WorkIQEnumerator:
    """``CandidateEnumerator`` over an injected WorkIQ query callable."""

    def __init__(
        self,
        query: WorkIQQuery,
        context: WorkIQContext,
        *,
        entity_type: EntityType = EntityType.MESSAGE,
        window_days: int = 7,
        max_windows: int = 4,
        program_id: str | None = None,
        programs_root: Path = PROGRAMS_ROOT,
        checkpoint_namespace: str = "",
        run_budget: WorkIQRunBudget | None = None,
    ) -> None:
        if (
            not isinstance(window_days, int)
            or isinstance(window_days, bool)
            or window_days < 1
            or not isinstance(max_windows, int)
            or isinstance(max_windows, bool)
            or max_windows < 1
        ):
            raise ValueError("WorkIQ window_days and max_windows must be positive integers.")
        self._query = query
        self._context = context
        self.entity_type = entity_type
        self._window_days = window_days
        self._max_windows = max_windows
        self._program_id = program_id
        self._programs_root = programs_root
        self._checkpoint_namespace = checkpoint_namespace
        self._run_budget = run_budget
        self._checkpoint_store: SyncStateStore | None = None
        self._checkpoint_state: SyncState | None = None
        self._pending_cursor: str | None = None
        self._pending_complete = False
        self._pending_empty_complete = False

    def enumerate(
        self, intent: RetrievalIntent, *, correlation_id: str
    ) -> PortResult[tuple[EnumeratedCandidate, ...]]:
        if intent.entity_type is not self.entity_type:
            return Unsupported(
                entity_type=str(intent.entity_type.value),
                reason=f"workiq enumerator bound to {self.entity_type.value}",
            )
        if intent.limit < 1:
            return Incomplete(value=(), reason="invalid_limit")
        if self._program_id is not None:
            return self._enumerate_checkpoint_page(intent, correlation_id=correlation_id)
        windowed, budget_stopped = self._windowed_intents(intent)
        if not windowed:
            return Incomplete(value=(), reason="invalid_window")
        records: list[Mapping[str, Any]] = []
        failures: list[str] = []
        window_cut = False
        rejected_identity = 0
        rejected_source_time = 0
        rejected_invalid_record = 0
        rejected_outside_window = 0
        budget_stop_reason = "budget_stop:max_windows" if budget_stopped else ""
        for window in windowed:
            try:
                page_result = self._query_with_budget(window)
            except _WorkIQBudgetExhausted as exc:
                budget_stop_reason = exc.reason
                break
            except Exception as exc:  # noqa: BLE001 - exceptions must not cross the port
                failures.append(type(exc).__name__)
                continue
            if isinstance(page_result, (WorkIQPage, WorkIQQueryPage)):
                rejected_identity += page_result.rejected_identity_count
                rejected_source_time += page_result.rejected_source_time_count
                rejected_invalid_record += page_result.rejected_invalid_record_count
                rejected_outside_window += page_result.rejected_outside_window_count
                if not page_result.response_usable:
                    failures.append("structured_response_unusable")
                    continue
                page_records = list(page_result.records)
            else:
                page_records = list(page_result)
            records.extend(page_records)
            if len(page_records) >= min(max(intent.limit, 1), 50):
                window_cut = True

        by_id: dict[str, EnumeratedCandidate] = {}
        unidentified = rejected_identity
        mismatched_identity = 0
        outside_window = rejected_outside_window
        unqualified_source_time = rejected_source_time
        invalid_records = rejected_invalid_record
        for record in records:
            if isinstance(record, Mapping) and _is_tombstone(record):
                tombstone_id = _first_native_id(record, _ID_KEYS)
                if tombstone_id:
                    by_id.pop(tombstone_id, None)
                continue
            candidate = self._candidate(record, correlation_id)
            if candidate is None:
                unidentified += 1
                continue
            if intent.entity_ids and not _candidate_matches_exact_ids(candidate, intent.entity_ids):
                mismatched_identity += 1
                continue
            received_at = candidate.received_at
            if candidate.partial_metadata.get("source_time_state") == "invalid":
                unqualified_source_time += 1
            elif (
                received_at is not None
                and intent.from_date is not None
                and intent.to_date is not None
                and not intent.from_date <= received_at.date() <= intent.to_date
            ):
                outside_window += 1
                continue
            by_id[candidate.locator.resource_id] = candidate

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
        if failures:
            return Incomplete(value=page, reason=f"provider_error:{failures[0]}")
        if budget_stop_reason:
            return Incomplete(value=page, reason=budget_stop_reason)
        if window_cut and intent.from_date is not None and intent.to_date is not None:
            return Incomplete(value=page, reason="page_cut:window")
        admission_errors = tuple(
            (label, count)
            for label, count in (
                ("unidentified_records", unidentified),
                ("unqualified_source_time", unqualified_source_time),
                ("outside_window", outside_window),
                ("invalid_records", invalid_records),
                ("mismatched_identity", mismatched_identity),
            )
            if count
        )
        if len(admission_errors) == 1:
            label, count = admission_errors[0]
            return Incomplete(value=page, reason=f"{label}:{count}")
        if admission_errors:
            detail = ",".join(f"{label}={count}" for label, count in admission_errors)
            return Incomplete(value=page, reason=f"admission_rejected:{detail}")
        if not records:
            return Incomplete(value=page, reason="coverage_unverified:empty")
        return Success(page)

    def _enumerate_checkpoint_page(
        self, intent: RetrievalIntent, *, correlation_id: str
    ) -> PortResult[tuple[EnumeratedCandidate, ...]]:
        self._prepare_checkpoint(intent)
        if (
            self._checkpoint_state is not None
            and self._checkpoint_state.program_watermarks.get(_COMPLETE_MARKER) == "true"
        ):
            if (
                self._checkpoint_state.program_watermarks.get(_EMPTY_COMPLETE_MARKER)
                == "true"
            ):
                return Incomplete(value=(), reason="coverage_unverified:empty")
            return Success(())
        try:
            result = self._query_page(intent)
        except _WorkIQBudgetExhausted as exc:
            return Incomplete(value=(), reason=exc.reason)
        except Exception as exc:  # noqa: BLE001
            return Incomplete(value=(), reason=f"provider_error:{type(exc).__name__}")
        by_id: dict[str, EnumeratedCandidate] = {}
        unidentified = 0
        rejected = result.rejected_source_time_count
        rejected_identity = result.rejected_identity_count
        invalid_records = result.rejected_invalid_record_count
        outside_window = result.rejected_outside_window_count
        mismatched_identity = 0
        if not result.response_usable:
            self._discard_staged_checkpoint()
            return Incomplete(value=(), reason="provider_error:structured_response_unusable")
        for record in result.records:
            if isinstance(record, Mapping) and _is_tombstone(record):
                tombstone_id = _first_native_id(record, _ID_KEYS)
                if tombstone_id:
                    by_id.pop(tombstone_id, None)
                continue
            candidate = self._candidate(record, correlation_id)
            if candidate is None:
                unidentified += 1
                continue
            if intent.entity_ids and not _candidate_matches_exact_ids(candidate, intent.entity_ids):
                mismatched_identity += 1
                continue
            if not candidate.partial_metadata.get("source_time_qualified"):
                rejected += 1
            elif (
                intent.from_date is not None
                and intent.to_date is not None
                and candidate.received_at is not None
                and not intent.from_date <= candidate.received_at.date() <= intent.to_date
            ):
                outside_window += 1
                continue
            by_id[candidate.locator.resource_id] = candidate
        ordered = sorted(
            by_id.values(),
            key=lambda candidate: (
                candidate.received_at is None,
                -(candidate.received_at.timestamp() if candidate.received_at else 0.0),
                candidate.locator.resource_id,
            ),
        )
        page = tuple(ordered[:intent.limit])
        if result.next_cursor is not None:
            self._stage_checkpoint(next_cursor=result.next_cursor, empty_page=False)
            return Incomplete(value=page, reason="page_cut")
        if len(ordered) > len(page):
            self._discard_staged_checkpoint()
            return Incomplete(value=page, reason="page_cut")
        initial_empty_page = not result.records and self._checkpoint_cursor() is None
        self._stage_checkpoint(
            next_cursor=None,
            empty_page=not result.records,
        )
        if rejected:
            return Incomplete(value=page, reason=f"unqualified_source_time:{rejected}")
        if outside_window:
            return Incomplete(value=page, reason=f"outside_window:{outside_window}")
        if mismatched_identity:
            return Incomplete(value=page, reason=f"mismatched_identity:{mismatched_identity}")
        unidentified += rejected_identity
        if unidentified and rejected:
            return Incomplete(
                value=page,
                reason=f"admission_rejected:unidentified_records={unidentified},unqualified_source_time={rejected}",
            )
        if rejected and invalid_records:
            return Incomplete(
                value=page,
                reason=f"admission_rejected:unqualified_source_time={rejected},invalid_records={invalid_records}",
            )
        if invalid_records:
            return Incomplete(value=page, reason=f"invalid_records:{invalid_records}")
        if unidentified:
            return Incomplete(value=page, reason=f"unidentified_records:{unidentified}")
        if initial_empty_page:
            return Incomplete(value=page, reason="coverage_unverified:empty")
        return Success(page)

    def commit_enumeration_checkpoint(self) -> None:
        if (
            self._checkpoint_store is None
            or self._checkpoint_state is None
            or not (self._pending_cursor is not None or self._pending_complete)
        ):
            return
        self._checkpoint_state.last_page_token = self._pending_cursor
        if self._pending_complete:
            self._checkpoint_state.program_watermarks[_COMPLETE_MARKER] = "true"
            if self._pending_empty_complete:
                self._checkpoint_state.program_watermarks[_EMPTY_COMPLETE_MARKER] = "true"
            else:
                self._checkpoint_state.program_watermarks.pop(_EMPTY_COMPLETE_MARKER, None)
        else:
            self._checkpoint_state.program_watermarks.pop(_COMPLETE_MARKER, None)
            self._checkpoint_state.program_watermarks.pop(_EMPTY_COMPLETE_MARKER, None)
        self._checkpoint_state.touch()
        self._checkpoint_store.upsert(self._checkpoint_state)
        if self._program_id is not None:
            self._checkpoint_store.save(
                self._program_id, programs_root=self._programs_root,
            )
        self._pending_cursor = None
        self._pending_complete = False
        self._pending_empty_complete = False

    def _query_page(self, intent: RetrievalIntent) -> WorkIQPage:
        cursor = self._checkpoint_cursor()
        outcome = self._query_with_budget(intent, cursor=cursor)
        if isinstance(outcome, WorkIQPage):
            page = outcome
        else:
            if isinstance(outcome, WorkIQQueryPage):
                page = WorkIQPage(
                    records=outcome.records,
                    next_cursor=None,
                    rejected_source_time_count=outcome.rejected_source_time_count,
                    rejected_identity_count=outcome.rejected_identity_count,
                    rejected_invalid_record_count=outcome.rejected_invalid_record_count,
                    rejected_outside_window_count=outcome.rejected_outside_window_count,
                    response_usable=outcome.response_usable,
                )
            else:
                page = WorkIQPage(records=tuple(outcome), next_cursor=None)
        next_cursor = page.next_cursor
        cursor_value = _validated_cursor(next_cursor)
        if cursor_value is not None and cursor_value == cursor:
            raise ValueError("WorkIQ continuation cursor did not advance")
        return replace(page, records=tuple(page.records), next_cursor=cursor_value)

    def _query_with_budget(
        self,
        intent: RetrievalIntent,
        *,
        cursor: str | None = None,
    ) -> Sequence[Mapping[str, Any]] | WorkIQQueryPage | WorkIQPage:
        timeout_seconds: int | None = None
        if self._run_budget is not None:
            remaining = self._run_budget.reserve_call()
            if remaining is None:
                reason = (
                    "budget_stop:workiq_calls"
                    if self._run_budget.calls_made >= self._run_budget.max_calls
                    else "budget_stop:workiq_wall_clock"
                )
                raise _WorkIQBudgetExhausted(reason)
            timeout_seconds = max(1, int(ceil(remaining)))
        try:
            signature = inspect.signature(self._query)
        except (TypeError, ValueError):
            signature = None
        parameters = signature.parameters if signature is not None else {}
        accepts_kwargs = signature is not None and any(
            parameter.kind is parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        kwargs: dict[str, Any] = {}
        if cursor is not None or "cursor" in parameters or accepts_kwargs:
            kwargs["cursor"] = cursor
        if timeout_seconds is not None and (
            "timeout_seconds" in parameters or accepts_kwargs
        ):
            kwargs["timeout_seconds"] = timeout_seconds
        if self._program_id is not None and cursor is not None and "cursor" not in parameters and not accepts_kwargs:
            raise ValueError("WorkIQ checkpoint requires a cursor-aware query")
        return self._query(intent, **kwargs)

    def _prepare_checkpoint(self, intent: RetrievalIntent) -> None:
        if self._program_id is None:
            return
        self._checkpoint_store = SyncStateStore.load(
            self._program_id, programs_root=self._programs_root,
        )
        identity = json.dumps(
            {
                "entity_type": self.entity_type.value,
                "from_date": intent.from_date.isoformat() if intent.from_date else None,
                "to_date": intent.to_date.isoformat() if intent.to_date else None,
                "senders": intent.senders,
                "subject_terms": intent.subject_terms,
                "body_terms": intent.body_terms,
                "limit": intent.limit,
                "namespace": self._checkpoint_namespace,
            },
            sort_keys=True,
        )
        api_version = "workiq-window-v1:" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        self._checkpoint_state = self._checkpoint_store.get_or_create(
            self._context.tenant_id,
            self._context.principal_mailbox,
            self._context.container,
            api_version,
        )

    def _checkpoint_cursor(self) -> str | None:
        if self._checkpoint_state is None:
            return None
        return _validated_cursor(self._checkpoint_state.last_page_token)

    def _stage_checkpoint(
        self, *, next_cursor: str | None, empty_page: bool = False,
    ) -> None:
        if self._checkpoint_state is None:
            return
        had_prior_page = self._checkpoint_state.last_page_token is not None
        self._pending_cursor = next_cursor
        self._pending_complete = next_cursor is None
        self._pending_empty_complete = (
            self._pending_complete and empty_page and not had_prior_page
        )

    def _discard_staged_checkpoint(self) -> None:
        self._pending_cursor = None
        self._pending_complete = False
        self._pending_empty_complete = False

    def _windowed_intents(
        self, intent: RetrievalIntent
    ) -> tuple[tuple[RetrievalIntent, ...], bool]:
        if intent.from_date is None and intent.to_date is None:
            return (intent,), False
        if intent.from_date is None or intent.to_date is None or intent.from_date > intent.to_date:
            return (), False
        windows: list[RetrievalIntent] = []
        window_end = intent.to_date
        while window_end >= intent.from_date and len(windows) < self._max_windows:
            window_start = max(
                intent.from_date,
                window_end - timedelta(days=self._window_days - 1),
            )
            windows.append(replace(intent, from_date=window_start, to_date=window_end))
            if window_start == intent.from_date:
                return tuple(windows), False
            window_end = window_start - timedelta(days=1)
        return tuple(windows), window_end >= intent.from_date

    def _candidate(self, record: Mapping[str, Any], correlation_id: str) -> EnumeratedCandidate | None:
        if not isinstance(record, Mapping):
            return None
        thread_id = _first_native_id(record, _THREAD_KEYS)
        resource_id = _first_native_id(record, _ID_KEYS)
        identity_kind = "native_message"
        if not resource_id:
            resource_id, identity_kind = thread_id, "native_thread"
        if not resource_id:
            return None
        received_at, source_time_state = _utc_timestamp(record)
        return EnumeratedCandidate(
            locator=HydrationLocator(
                source_type=self.entity_type,
                tenant_id=self._context.tenant_id,
                principal_mailbox=self._context.principal_mailbox,
                container=self._context.container,
                resource_id=resource_id,
            ),
            relevance_score=1.0,
            partial_metadata={
                "subject": _text(record, "subject", "title"),
                "sender": _sender(record),
                "preview": _text(record, "preview", "bodyPreview", "summary"),
                "received_at": received_at.isoformat() if received_at else None,
                "conversation_id": thread_id or None,
                "series_master_id": _text(record, "seriesMasterId") or None,
                "occurrence_id": _text(record, "occurrenceId") or None,
                "identity_kind": identity_kind,
                "web_link": _text(record, "webLink", "url") or None,
                "enumerator": ENUMERATOR_LABEL,
                "relevance_basis": "provider_query_match",
                "source_time_state": source_time_state,
                "source_time_qualified": source_time_state == "qualified",
            },
            correlation_id=correlation_id,
            enumerator=ENUMERATOR_LABEL,
            received_at=received_at,
        )


def _candidate_matches_exact_ids(
    candidate: EnumeratedCandidate,
    entity_ids: tuple[str, ...],
) -> bool:
    accepted = {value.strip() for value in entity_ids if value.strip()}
    conversation_id = str(candidate.partial_metadata.get("conversation_id") or "").strip()
    return bool(accepted) and (
        candidate.locator.resource_id in accepted or conversation_id in accepted
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
        source_time_qualified = meta.get("source_time_qualified") is True
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
                    "series_master_id": meta.get("series_master_id"),
                    "occurrence_id": meta.get("occurrence_id"),
                    "identity_kind": meta.get("identity_kind"),
                    "received_at": meta.get("received_at") if source_time_qualified else None,
                    "subject": subject,
                    "sender": str(meta.get("sender") or ""),
                    "web_link": meta.get("web_link"),
                    "enumerator": meta.get("enumerator"),
                    "source_time_qualified": bool(meta.get("source_time_qualified")),
                    "source_time_state": meta.get("source_time_state"),
                },
                hydration_rung=RUNG_WORKIQ_PREVIEW if preview else RUNG_METADATA_ONLY,
                metadata_only=not preview,
                retrieved_at=candidate.received_at if source_time_qualified else None,
                correlation_id=correlation_id,
            )
        )
