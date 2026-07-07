from __future__ import annotations

from datetime import datetime, timezone

from src.core.models import Confidence
from src.core.models_v2 import PersonDirectory, Signal
from src.core.signal_ranking import sort_signals_for_ai_context


def test_sort_signals_for_ai_context_prefers_higher_signal_class_within_same_source() -> None:
    timestamp = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    decision_signal = Signal(
        id="decision",
        timestamp=timestamp,
        source="workiq/email",
        program_id="acme",
        workstream_id="deployment_velocity",
        entity_refs=("WI:1",),
        text="Decision: leadership approved the rollout.",
        raw_ref="raw:decision",
        confidence=Confidence.MEDIUM,
        metadata={"sender_alias": "owner"},
    )
    status_signal = Signal(
        id="status",
        timestamp=timestamp,
        source="workiq/email",
        program_id="acme",
        workstream_id="deployment_velocity",
        entity_refs=("WI:2",),
        text="Status update: rollout remains on track.",
        raw_ref="raw:status",
        confidence=Confidence.MEDIUM,
        metadata={"sender_alias": "owner"},
    )

    ordered = sort_signals_for_ai_context((status_signal, decision_signal), as_of=timestamp)

    assert [signal.id for signal in ordered] == ["decision", "status"]


def test_sort_signals_for_ai_context_keeps_existing_workiq_seniority_signal() -> None:
    timestamp = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
    vp_signal = Signal(
        id="vp",
        timestamp=timestamp,
        source="workiq/email",
        program_id="acme",
        workstream_id=None,
        entity_refs=(),
        text="Status update: no major changes.",
        raw_ref="raw:vp",
        confidence=Confidence.MEDIUM,
        metadata={"sender_alias": "vp"},
    )
    ic_signal = Signal(
        id="ic",
        timestamp=timestamp,
        source="workiq/email",
        program_id="acme",
        workstream_id=None,
        entity_refs=(),
        text="Status update: no major changes.",
        raw_ref="raw:ic",
        confidence=Confidence.MEDIUM,
        metadata={"sender_alias": "ic"},
    )

    ordered = sort_signals_for_ai_context(
        (ic_signal, vp_signal),
        as_of=timestamp,
        people_directory=(
            PersonDirectory(alias="vp", title="Vice President"),
            PersonDirectory(alias="ic", title="Software Engineer"),
        ),
    )

    assert [signal.id for signal in ordered] == ["vp", "ic"]
