from __future__ import annotations

from datetime import datetime, timezone

from src.core.incident_learning_synthesizer import build_incident_class_patterns, build_incident_ref_patterns
from src.core.models import Confidence
from src.core.models_v2 import IncidentEntry


def test_build_incident_ref_patterns_groups_by_shared_ado_ref() -> None:
    patterns = build_incident_ref_patterns(
        (
            _entry("4101", "sig-1", "IcM 4101: WI:1001 rollout validation regressed under failover.", refs=("WI:1001",)),
            _entry("4102", "sig-2", "IcM 4102: WI:1001 rollout validation regressed under failover again.", refs=("WI:1001",), confidence=Confidence.MEDIUM),
        )
    )

    assert len(patterns) == 1
    assert patterns[0].ref == "WI:1001"
    assert patterns[0].entry_count == 2


def test_build_incident_class_patterns_groups_similar_summaries_across_distinct_refs() -> None:
    patterns = build_incident_class_patterns(
        (
            _entry("4101", "sig-1", "IcM 4101: WI:1001 rollout validation regressed under failover.", refs=("WI:1001",)),
            _entry("4102", "sig-2", "IcM 4102: WI:2002 rollout validation regressed after failover.", refs=("WI:2002",), confidence=Confidence.MEDIUM),
            _entry("4103", "sig-3", "IcM 4103: WI:3003 rollout validation regressed during failover drills.", refs=("WI:3003",), confidence=Confidence.MEDIUM),
        )
    )

    assert len(patterns) == 1
    assert patterns[0].entry_count == 3
    assert patterns[0].linked_refs == ("WI:1001", "WI:2002", "WI:3003")
    assert "rollout" in patterns[0].class_label
    assert patterns[0].confidence is Confidence.HIGH


def _entry(
    incident_id: str,
    signal_id: str,
    summary: str,
    *,
    refs: tuple[str, ...] = (),
    confidence: Confidence = Confidence.HIGH,
) -> IncidentEntry:
    observed_at = datetime(2026, 5, 20, 15, 0, tzinfo=timezone.utc)
    return IncidentEntry(
        program_id="acme",
        incident_id=incident_id,
        signal_id=signal_id,
        observed_at=observed_at,
        recorded_at=observed_at,
        belief_change_summary=summary,
        workstream_id="deployment_readiness",
        severity=2,
        ado_entity_refs=refs,
        confidence=confidence,
    )