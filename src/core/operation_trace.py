"""ADF-W2.12 (specs/arch-data-fix.md Section 9.5, Appendix A.2): OperationTrace.

The cross-cutting correlation record linking a single logical run's
acquisition, fact, context, release, and render artifacts. ``OperationTrace``
itself is a read-time *aggregate view*, not a separate write path: every
call to ``record_trace_link`` writes one ``operation.trace_linked.v1``
ledger event (already registered by ADF-W0.18, unused until now -- the same
"schema registered, no writer yet" state ADF-W2.8 found for its own three
event types), and ``load_operation_trace`` groups every event sharing a
``correlation_id`` into one aggregated ``OperationTrace``. This mirrors
ADF-W2.8's `released_terminal_for_run` -- read the append-only ledger,
don't maintain a parallel mutable store.

Zone A throughout: reading/writing ledger events is deterministic, no AI
call, no Zone B/C import (INV-ADF-17).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope, read_events, write_event
from src.core.ledger.source_refs import OperatorAssertionRef

SCHEMA_VERSION = "1.0"

#: The six ref_type values this module aggregates, one-to-one with
#: OperationTrace's six ref-bearing fields. CONTEXT_MANIFEST is singular
#: (most-recent write wins on aggregation); the other five accumulate.
REF_TYPE_SOURCE = "source"
REF_TYPE_FACT = "fact"
REF_TYPE_CONTEXT_MANIFEST = "context_manifest"
REF_TYPE_OUTPUT = "output"
REF_TYPE_ACTUATION_INTENT = "actuation_intent"
REF_TYPE_RECEIPT = "receipt"

_MULTI_VALUE_REF_TYPES = (REF_TYPE_SOURCE, REF_TYPE_FACT, REF_TYPE_OUTPUT, REF_TYPE_ACTUATION_INTENT, REF_TYPE_RECEIPT)


@dataclass(frozen=True, slots=True)
class OperationTrace:
    schema_version: str
    program_id: str
    edition_id: str | None
    workflow_id: str
    run_id: str
    correlation_id: str
    parent_event_id: str | None
    source_refs: tuple[str, ...]
    fact_refs: tuple[str, ...]
    context_manifest_ref: str | None
    output_refs: tuple[str, ...]
    actuation_intent_refs: tuple[str, ...]
    receipt_refs: tuple[str, ...]


def record_trace_link(
    *,
    program_id: str,
    correlation_id: str,
    workflow_id: str,
    run_id: str,
    stage: str,
    ref_type: str,
    ref_id: str,
    parent_event_id: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    """Record one (stage, ref_type, ref_id) link under a shared
    ``correlation_id``. Call once per artifact a pipeline stage produces --
    a source acquisition, a fact write, a context manifest, a release
    decision, a rendered output, an actuation intent/receipt.
    ``load_operation_trace`` aggregates every call sharing the same
    ``correlation_id`` into one view."""
    now = datetime.now(timezone.utc)
    payload: dict[str, object] = {
        "correlation_id": correlation_id,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "stage": stage,
        "ref_type": ref_type,
        "ref_id": ref_id,
    }
    if parent_event_id is not None:
        payload["parent_event_id"] = parent_event_id
    envelope = build_event_envelope(
        program_id=program_id,
        event_type="operation.trace_linked.v1",
        occurred_at=now,
        recorded_at=now,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="operation_trace",
        payload=payload,
        source_ref=OperatorAssertionRef(
            asserted_by="system:operation_trace",
            asserted_at=now,
            context=f"{correlation_id} stage={stage} {ref_type}={ref_id}",
        ),
    )
    write_event(envelope, programs_root=programs_root)


def load_operation_trace(
    program_id: str,
    correlation_id: str,
    *,
    edition_id: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> OperationTrace | None:
    """Aggregate every ``operation.trace_linked.v1`` event sharing
    ``correlation_id`` into one read-time ``OperationTrace``, or ``None`` if
    nothing has been recorded for it yet. ``edition_id`` is caller-supplied
    (not part of the registered event payload -- Appendix A.2's
    ``operation.trace_linked.v1`` schema, ADF-W0.18, does not carry it; a
    caller that already knows the edition for this correlation_id may pass
    it through for display, but it is never derived from the ledger)."""
    events = read_events(program_id, programs_root=programs_root)
    matching = [
        event
        for event in events
        if event.event_type == "operation.trace_linked.v1" and event.payload.get("correlation_id") == correlation_id
    ]
    if not matching:
        return None

    buckets: dict[str, list[str]] = {ref_type: [] for ref_type in _MULTI_VALUE_REF_TYPES}
    context_manifest_ref: str | None = None
    workflow_id = ""
    run_id = ""
    parent_event_id: str | None = None

    for event in matching:
        workflow_id = str(event.payload.get("workflow_id") or workflow_id)
        run_id = str(event.payload.get("run_id") or run_id)
        ref_type = event.payload.get("ref_type")
        ref_id = event.payload.get("ref_id")
        if ref_type == REF_TYPE_CONTEXT_MANIFEST:
            if isinstance(ref_id, str):
                context_manifest_ref = ref_id  # most-recent write wins
        elif ref_type in buckets and isinstance(ref_id, str) and ref_id not in buckets[ref_type]:
            buckets[ref_type].append(ref_id)
        candidate_parent = event.payload.get("parent_event_id")
        if isinstance(candidate_parent, str):
            parent_event_id = candidate_parent

    return OperationTrace(
        schema_version=SCHEMA_VERSION,
        program_id=program_id,
        edition_id=edition_id,
        workflow_id=workflow_id,
        run_id=run_id,
        correlation_id=correlation_id,
        parent_event_id=parent_event_id,
        source_refs=tuple(buckets[REF_TYPE_SOURCE]),
        fact_refs=tuple(buckets[REF_TYPE_FACT]),
        context_manifest_ref=context_manifest_ref,
        output_refs=tuple(buckets[REF_TYPE_OUTPUT]),
        actuation_intent_refs=tuple(buckets[REF_TYPE_ACTUATION_INTENT]),
        receipt_refs=tuple(buckets[REF_TYPE_RECEIPT]),
    )


__all__ = [
    "REF_TYPE_ACTUATION_INTENT",
    "REF_TYPE_CONTEXT_MANIFEST",
    "REF_TYPE_FACT",
    "REF_TYPE_OUTPUT",
    "REF_TYPE_RECEIPT",
    "REF_TYPE_SOURCE",
    "SCHEMA_VERSION",
    "OperationTrace",
    "load_operation_trace",
    "record_trace_link",
]
