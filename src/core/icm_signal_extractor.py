from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from src.core.integration_types import ExtractionResult, IcMHydrationOutput, IncidentState
from src.core.models import Confidence
from src.core.models_v2 import Signal
from src.core.signal_ref_utils import extract_work_item_refs, merge_entity_refs


class IcMSignalExtractor:
    @property
    def channel(self) -> str:
        return "icm"

    def extract(self, resources: IcMHydrationOutput, program_id: str) -> ExtractionResult:
        signals: list[Signal] = []
        for incident in resources.incident_states:
            signals.extend(_incident_signals(incident, program_id))
        return ExtractionResult(
            channel="icm",
            signals=tuple(signals),
            trajectory_points=(),
            side_artifacts={},
            errors=(),
        )


def _incident_signals(incident: IncidentState, program_id: str) -> list[Signal]:
    signals: list[Signal] = []
    ref = f"icm:{incident.incident_id}"
    sig_id = f"icm/incident/{_short_hash(incident.incident_id)}"
    sev_str = f"Sev {incident.severity}" if incident.severity is not None else "Sev ?"
    title = incident.title or f"IcM incident {incident.incident_id}"
    text = f"[{sev_str}] {title}"
    ts = incident.updated_at
    workstream_ids = incident.workstream_ids or (None,)
    for ws_id in workstream_ids:
        signals.append(
            Signal(
                id=sig_id if ws_id is None else f"{sig_id}/{ws_id}",
                timestamp=ts,
                source="icm",
                program_id=program_id,
                workstream_id=ws_id,
                entity_refs=merge_entity_refs(
                    provider_refs=(ref,),
                    workstream_id=ws_id,
                    additional_refs=extract_work_item_refs(title),
                ),
                text=text,
                raw_ref=sig_id,
                confidence=Confidence.HIGH,
                review_policy=None,
                metadata={
                    "incident_id": incident.incident_id,
                    "severity": incident.severity,
                    "status": incident.status or "",
                    "owning_team": incident.owning_team or "",
                },
            )
        )
    return signals


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]
