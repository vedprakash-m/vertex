from __future__ import annotations

import hashlib
import json
from typing import Callable

from src.core.integration_types import ExtractionResult, KustoHydrationOutput, KustoResultSet
from src.core.kusto_ref_utils import extract_kusto_entity_refs
from src.core.models import Confidence
from src.core.models_v2 import Signal
from src.core.signal_ref_utils import merge_entity_refs

#: ADF-W2.3 (Section 8.5.3): injected waiver lookup -- given a query_id,
#: returns the active ``SourceWaiver`` for that query's telemetry source
#: (or None). Dependency-injected so the extractor stays testable without
#: file I/O; the caller (gather pipeline) wires it to
#: ``source_waiver_store.find_waiver_for_query`` with the loaded waivers
#: and slice contracts.
WaiverLookup = Callable[[str], "object | None"]


class KustoSignalExtractor:
    def __init__(self, *, waiver_lookup: WaiverLookup | None = None) -> None:
        # ADF-W2.3: optional. Existing construction (channel_wiring registry)
        # passes no arguments, so waiver annotation is opt-in -- a caller that
        # does not inject a lookup gets exactly the pre-existing behavior.
        self._waiver_lookup = waiver_lookup

    @property
    def channel(self) -> str:
        return "kusto"

    def extract(self, resources: KustoHydrationOutput, program_id: str) -> ExtractionResult:
        signals: list[Signal] = []
        for result_set in resources.result_sets:
            waiver = self._lookup_waiver(result_set.query_id) if self._waiver_lookup is not None else None
            if not result_set.rows:
                text = _empty_result_text(result_set)
                if text is None:
                    # ADF-W2.3 (Section 8.5.2) "expected quiet": zero rows and
                    # the query declared expected_cardinality=zero_ok. Not an
                    # actionable finding -- preserves the pre-existing
                    # behavior of emitting nothing for this specific case.
                    continue
                signals.extend(_build_signals(result_set, program_id, text=text, confidence=Confidence.MEDIUM, waiver=waiver))
                continue
            text = _semantic_or_generic_text(result_set, waiver=waiver)
            signals.extend(_build_signals(result_set, program_id, text=text, confidence=Confidence.HIGH, waiver=waiver))
        return ExtractionResult(channel="kusto", signals=tuple(signals), trajectory_points=(), side_artifacts={}, errors=())

    def _lookup_waiver(self, query_id: str) -> object | None:
        assert self._waiver_lookup is not None
        try:
            return self._waiver_lookup(query_id)
        except Exception:
            # A waiver-store failure (missing file, parse error) must never
            # break signal extraction -- the breach signal is still emitted
            # without the waiver annotation.
            return None


def _empty_result_text(result_set: KustoResultSet) -> str | None:
    """ADF-W2.3 (Section 8.5.2): a zero-row result is one of two explicit
    states here (the other two -- "unavailable" from an execution error, and
    the query/data-gap-vs-invalid-schema split for non-empty results -- are
    handled elsewhere: execution errors become IntegrationErrors before a
    KustoResultSet even exists, and "invalid schema" is a non-empty-rows
    case handled by ``_semantic_or_generic_text``).

    Returns None for "expected quiet" (nothing to surface); a message for
    "query/data gap" (rows were expected but none came back).
    """
    # KustoResultSet does not carry expected_cardinality directly (it is a
    # per-query config field, not a per-result-set fact); the caller wires
    # is_partial=False and no rows through unconditionally today, so the
    # "expected quiet" vs "data gap" split lives in whether metric_id/
    # result_column semantic config is present -- an unconfigured legacy
    # query with zero rows keeps the pre-existing silent-skip behavior.
    if result_set.metric_id is None and result_set.result_column is None:
        return None
    return (
        f"Kusto query {result_set.query_id}: no rows returned for metric "
        f"'{result_set.metric_id or result_set.query_id}' -- query or data gap."
    )


def _semantic_or_generic_text(result_set: KustoResultSet, *, waiver: object | None = None) -> str:
    if result_set.metric_id is None:
        return f"Kusto query {result_set.query_id}: {len(result_set.rows)} row(s) observed."

    if result_set.observed_value is None:
        # metric_id/result_column configured but the column wasn't found (or
        # wasn't numeric) in the actual result rows -- Section 8.5.2's
        # "invalid schema" state, made explicit rather than silently
        # producing a metric-less signal.
        return (
            f"Kusto query {result_set.query_id}: configured result_column "
            f"'{result_set.result_column}' not found or not numeric in results "
            f"-- metric '{result_set.metric_id}' could not be computed (invalid schema)."
        )

    value_text = _format_value(result_set.observed_value, result_set.unit)
    if result_set.is_breach is None:
        # Measured, but no SLO configured to judge it against.
        return _with_waiver_suffix(f"{result_set.metric_id} = {value_text}.", waiver)

    target_text = _format_value(result_set.slo_target, result_set.unit)
    verdict = "BREACH" if result_set.is_breach else "OK"
    text = f"{result_set.metric_id} = {value_text} against SLO {result_set.comparison}{target_text}: {verdict}."
    return _with_waiver_suffix(text, waiver)


def _format_value(value: float | None, unit: str | None) -> str:
    if value is None:
        return ""
    formatted = f"{value:g}"
    return f"{formatted}{unit}" if unit else formatted


def _waiver_suffix(waiver: object | None) -> str:
    """ADF-W2.3 (Section 8.5.3): an active source waiver annotates the signal
    text so a known-bad metric that is formally waived does not present as an
    un-contextualized alarm. The waiver's reason and expiry are surfaced so
    the operator knows the gate is covered and when coverage ends."""
    if waiver is None:
        return ""
    reason = getattr(waiver, "reason", None)
    expires = getattr(waiver, "expires", None)
    parts = ["(waived"]
    if reason:
        parts.append(f": {reason}")
    if expires is not None:
        parts.append(f", expires {expires}")
    parts.append(")")
    return "".join(parts)


def _with_waiver_suffix(text: str, waiver: object | None) -> str:
    suffix = _waiver_suffix(waiver)
    if not suffix:
        return text
    return f"{text} {suffix}"


def _build_signals(
    result_set: KustoResultSet, program_id: str, *, text: str, confidence: Confidence, waiver: object | None = None
) -> list[Signal]:
    signals: list[Signal] = []
    result_hash = hashlib.sha256(json.dumps(result_set.rows, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]
    for workstream_id, suffix in _workstream_suffixes(result_set.workstream_ids):
        raw_ref = f"kusto/{result_set.query_id}/{result_hash}/{suffix}"
        entity_refs = merge_entity_refs(
            provider_refs=(f"kusto:{result_set.query_id}",),
            workstream_id=workstream_id,
            additional_refs=extract_kusto_entity_refs(result_set.rows),
        )
        signals.append(
            Signal(
                id=raw_ref,
                timestamp=result_set.observed_at,
                source="kusto",
                program_id=program_id,
                workstream_id=workstream_id,
                entity_refs=entity_refs,
                text=text,
                raw_ref=raw_ref,
                confidence=confidence,
                review_policy=None,
                metadata={
                    "query_id": result_set.query_id,
                    "row_count": len(result_set.rows),
                    "result_hash": result_hash,
                    "metric_id": result_set.metric_id,
                    "observed_value": result_set.observed_value,
                    "unit": result_set.unit,
                    "slo_target": result_set.slo_target,
                    "comparison": result_set.comparison,
                    "is_breach": result_set.is_breach,
                    # ADF-W2.3 (Section 8.5.3): waiver annotation for downstream
                    # consumers (report rendering, cockpit). False (not None)
                    # when no waiver was looked up, so a reader can distinguish
                    # "actively waived" from "not waived" without a None check.
                    "waiver_active": waiver is not None,
                    "waiver_contract_id": getattr(waiver, "contract_id", None) if waiver is not None else None,
                },
            )
        )
    return signals


def _workstream_suffixes(workstream_ids: tuple[str, ...]) -> tuple[tuple[str | None, str], ...]:
    if not workstream_ids:
        return ((None, "_unassigned"),)
    return tuple((workstream_id, workstream_id) for workstream_id in dict.fromkeys(workstream_ids))
