from __future__ import annotations

import hashlib
import json

from src.core.integration_types import ExtractionResult, KustoHydrationOutput
from src.core.kusto_ref_utils import extract_kusto_entity_refs
from src.core.models import Confidence
from src.core.models_v2 import ReviewPolicy, Signal
from src.core.signal_ref_utils import merge_entity_refs


class KustoSignalExtractor:
    @property
    def channel(self) -> str:
        return "kusto"

    def extract(self, resources: KustoHydrationOutput, program_id: str) -> ExtractionResult:
        signals: list[Signal] = []
        for result_set in resources.result_sets:
            if not result_set.rows:
                continue
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
                        text=f"Kusto query {result_set.query_id}: {len(result_set.rows)} row(s) observed.",
                        raw_ref=raw_ref,
                        confidence=Confidence.HIGH,
                        review_policy=None,
                        metadata={
                            "query_id": result_set.query_id,
                            "row_count": len(result_set.rows),
                            "result_hash": result_hash,
                        },
                    )
                )
        return ExtractionResult(channel="kusto", signals=tuple(signals), trajectory_points=(), side_artifacts={}, errors=())


def _workstream_suffixes(workstream_ids: tuple[str, ...]) -> tuple[tuple[str | None, str], ...]:
    if not workstream_ids:
        return ((None, "_unassigned"),)
    return tuple((workstream_id, workstream_id) for workstream_id in dict.fromkeys(workstream_ids))