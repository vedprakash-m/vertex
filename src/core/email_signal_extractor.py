from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from src.core.integration_types import EmailHydrationOutput, EmailMessage, ExtractionResult
from src.core.models import Confidence
from src.core.models_v2 import Signal
from src.core.signal_fragment_utils import fragment_resource_id, split_signal_fragments
from src.core.signal_ref_utils import extract_work_item_refs, merge_entity_refs


class EmailSignalExtractor:
    @property
    def channel(self) -> str:
        return "email"

    def extract(self, resources: EmailHydrationOutput, program_id: str) -> ExtractionResult:
        signals: list[Signal] = []
        for message in resources.messages:
            signals.extend(_message_signals(message, program_id))
        return ExtractionResult(
            channel="email",
            signals=tuple(signals),
            trajectory_points=(),
            side_artifacts={},
            errors=(),
        )


def _message_signals(message: EmailMessage, program_id: str) -> list[Signal]:
    provider_ref = f"email:{message.thread_id}"
    base_sig_id = f"email/message/{_short_hash(message.message_id)}"
    text = message.preview or message.subject or f"Email thread {message.thread_id}"
    fragments = split_signal_fragments(text) or (text,)
    workstream_ids = message.workstream_ids or (None,)
    signals: list[Signal] = []
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
            "subject": message.subject or "",
            "sender_alias": _sender_alias(message.sender),
            "source_type": "email",
            "segment_index": segment_index,
            "segment_count": len(fragments),
        }
        for workstream_id in workstream_ids:
            entity_refs = merge_entity_refs(
                provider_refs=(provider_ref,),
                workstream_id=workstream_id,
                additional_refs=_merge_work_item_refs(message.work_item_ids, fragment_text),
            )
            signals.append(
                Signal(
                    id=fragment_sig_id if workstream_id is None else f"{fragment_sig_id}/{workstream_id}",
                    timestamp=message.sent_at,
                    source="workiq/email",
                    program_id=program_id,
                    workstream_id=workstream_id,
                    entity_refs=entity_refs,
                    text=text if len(fragments) == 1 else fragment_text,
                    raw_ref=fragment_sig_id,
                    thread_id=message.thread_id,
                    confidence=Confidence.MEDIUM,
                    review_policy=None,
                    metadata=metadata,
                )
            )
    return signals


def _merge_work_item_refs(work_item_ids: tuple[int, ...], fragment_text: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys((*(f"WI:{work_item_id}" for work_item_id in work_item_ids), *extract_work_item_refs(fragment_text)))
    )


def _sender_alias(sender: str | None) -> str | None:
    if sender is None:
        return None
    normalized = sender.strip().lower()
    if not normalized:
        return None
    alias = normalized.split("@", 1)[0]
    return alias or None


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
