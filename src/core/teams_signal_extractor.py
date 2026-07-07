from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from src.core.integration_types import ExtractionResult, MeetingEvent, TeamsHydrationOutput, ThreadMessage
from src.core.models import Confidence
from src.core.models_v2 import Signal
from src.core.signal_fragment_utils import fragment_resource_id, split_signal_fragments
from src.core.signal_ref_utils import extract_work_item_refs, merge_entity_refs


class TeamsSignalExtractor:
    @property
    def channel(self) -> str:
        return "teams"

    def extract(self, resources: TeamsHydrationOutput, program_id: str) -> ExtractionResult:
        signals: list[Signal] = []
        for event in resources.meeting_events:
            signals.extend(_meeting_signals(event, program_id))
        for message in resources.thread_messages:
            signals.extend(_message_signals(message, program_id))
        return ExtractionResult(
            channel="teams",
            signals=tuple(signals),
            trajectory_points=(),
            side_artifacts={},
            errors=(),
        )


def _meeting_signals(event: MeetingEvent, program_id: str) -> list[Signal]:
    signals: list[Signal] = []
    provider_ref = f"teams:{event.series_id or event.event_id}"
    base_sig_id = f"teams/meeting/{_short_hash(event.event_id)}"
    fragments = split_signal_fragments(_meeting_fragment_source_text(event)) or (_default_meeting_fragment_text(event),)
    workstream_ids = event.workstream_ids or (None,)
    for segment_index, fragment_text in enumerate(fragments):
        fragment_sig_id = fragment_resource_id(
            resource_id=base_sig_id,
            segment_index=segment_index,
            segment_count=len(fragments),
        )
        text = _meeting_signal_text(event, fragment_text=fragment_text, segment_count=len(fragments))
        fragment_event_id = fragment_resource_id(
            resource_id=event.event_id,
            segment_index=segment_index,
            segment_count=len(fragments),
        )
        metadata = {
            "event_id": fragment_event_id,
            "parent_event_id": event.event_id,
            "series_id": event.series_id or "",
            "thread_id": event.thread_id or "",
            "organizer": event.organizer or "",
            "segment_index": segment_index,
            "segment_count": len(fragments),
        }
        for ws_id in workstream_ids:
            entity_refs = merge_entity_refs(
                provider_refs=(provider_ref,),
                workstream_id=ws_id,
                additional_refs=_merge_work_item_refs(event.work_item_ids, fragment_text),
            )
            signals.append(
                Signal(
                    id=fragment_sig_id if ws_id is None else f"{fragment_sig_id}/{ws_id}",
                    timestamp=event.started_at,
                    source="teams",
                    program_id=program_id,
                    workstream_id=ws_id,
                    entity_refs=entity_refs,
                    text=text,
                    raw_ref=fragment_sig_id,
                    confidence=Confidence.MEDIUM,
                    review_policy=None,
                    metadata=metadata,
                )
            )
    return signals


def _message_signals(message: ThreadMessage, program_id: str) -> list[Signal]:
    signals: list[Signal] = []
    provider_ref = f"teams:{message.thread_id}"
    base_sig_id = f"teams/message/{_short_hash(message.message_id)}"
    text = message.text or f"Teams message in thread {message.thread_id}"
    ts = message.sent_at
    fragments = split_signal_fragments(text) or (text,)
    workstream_ids = message.workstream_ids or (None,)
    for segment_index, fragment_text in enumerate(fragments):
        fragment_sig_id = fragment_resource_id(
            resource_id=base_sig_id,
            segment_index=segment_index,
            segment_count=len(fragments),
        )
        metadata = {
            "message_id": fragment_resource_id(
                resource_id=message.message_id,
                segment_index=segment_index,
                segment_count=len(fragments),
            ),
            "parent_message_id": message.message_id,
            "thread_id": message.thread_id,
            "sender": message.sender or "",
            "segment_index": segment_index,
            "segment_count": len(fragments),
        }
        for ws_id in workstream_ids:
            entity_refs = merge_entity_refs(
                provider_refs=(provider_ref,),
                workstream_id=ws_id,
                additional_refs=_merge_work_item_refs(message.work_item_ids, fragment_text),
            )
            signals.append(
                Signal(
                    id=fragment_sig_id if ws_id is None else f"{fragment_sig_id}/{ws_id}",
                    timestamp=ts,
                    source="teams",
                    program_id=program_id,
                    workstream_id=ws_id,
                    entity_refs=entity_refs,
                    text=text if len(fragments) == 1 else fragment_text,
                    raw_ref=fragment_sig_id,
                    confidence=Confidence.MEDIUM,
                    review_policy=None,
                    metadata=metadata,
                )
            )
    return signals


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _meeting_fragment_source_text(event: MeetingEvent) -> str:
    title = (event.title or "").strip()
    summary = (event.summary or "").strip()
    if summary and title and extract_work_item_refs(title):
        return f"{title}\n{summary}"
    return summary or title


def _default_meeting_fragment_text(event: MeetingEvent) -> str:
    return f"{event.title or '(untitled)'} at {event.started_at.isoformat()}"


def _meeting_signal_text(event: MeetingEvent, *, fragment_text: str, segment_count: int) -> str:
    title = (event.title or "").strip()
    summary = (event.summary or "").strip()
    if segment_count == 1 and fragment_text == title and not summary:
        return f"Teams meeting: {fragment_text} at {event.started_at.isoformat()}"
    return f"Teams meeting: {fragment_text}"


def _merge_work_item_refs(work_item_ids: tuple[int, ...], fragment_text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*(f"WI:{work_item_id}" for work_item_id in work_item_ids), *extract_work_item_refs(fragment_text))))
