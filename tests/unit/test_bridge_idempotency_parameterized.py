"""Bridge idempotency, parameterized across every appender the shared
`sor_gated_family_load` helper serves (fix-data-flow.md Track B.5, resolves
a gap GLM flagged in v1.3: the idempotency contract was previously tested
for only one appender via `approval_event_id`'s regression test).

Each case: append the identical `EventEnvelope` (same `event_id`, i.e. same
`domain_event_id`) through the real bridge appender TWICE, and assert the
fact store contains exactly one resulting fact afterward, converting the
`domain_event_id`-uniqueness idempotency claim (`ledger.py`'s bridge
docstring) into a tested contract for every family, not just milestone.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

from src.core.ledger.event_log import ConfidenceTier, EventEnvelope, TemporalConfidence
from src.core.ledger.fact_bridge import (
    append_bridged_assumption_event,
    append_bridged_decision_event,
    append_bridged_dependency_event,
    append_bridged_milestone_event,
    append_bridged_risk_event,
    append_bridged_workstream_event,
)
from src.core.ledger.source_refs import EmailRef
from src.core.program_fact_store import ProgramFactStore

NOW = datetime(2026, 7, 7, 20, 0, tzinfo=timezone.utc)


def _email_ref(*, message_id: str) -> EmailRef:
    return EmailRef(
        subject="Idempotency probe",
        sent_at=NOW,
        sender="pm@example.com",
        message_id=message_id,
        vault_hash=f"sha256:vault-{message_id}",
    )


def _event(*, event_id: str, event_type: str, payload: dict[str, Any]) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        program_id="acme",
        event_type=event_type,
        occurred_at=NOW,
        recorded_at=NOW,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="rev-mail",
        payload=payload,
        source_ref=_email_ref(message_id=f"{event_id}@example.com"),
        prev_event_hash="sha256:prev",
        content_hash=f"sha256:content-{event_id}",
    )


_CASES: list[tuple[str, str, str, dict[str, Any], Callable[..., Any]]] = [
    (
        "risk",
        "risk.entry",
        "risk.raised.v1",
        {"risk_id": "risk:idem", "title": "Vendor delay", "severity": "high", "likelihood": "possible"},
        append_bridged_risk_event,
    ),
    (
        "decision",
        "decision.entry",
        "decision.made.v1",
        {
            "decision_id": "decision:idem",
            "title": "Adopt vendor X",
            "decision_text": "We will adopt vendor X.",
            "decided_by": ["pm@example.com"],
        },
        append_bridged_decision_event,
    ),
    (
        "assumption",
        "assumption.entry",
        "assumption.stated.v1",
        {"assumption_id": "assumption:idem", "statement": "Vendor ships on time."},
        append_bridged_assumption_event,
    ),
    (
        "milestone",
        "milestone.entry",
        "milestone.completed.v1",
        {"milestone_id": "milestone:idem", "completed_on": "2026-07-03"},
        append_bridged_milestone_event,
    ),
    (
        "dependency",
        "dependency.link",
        "dependency.declared.v1",
        {"dependency_id": "dependency:idem", "from_entity": "workstream:a", "to_entity": "workstream:b"},
        append_bridged_dependency_event,
    ),
    (
        "workstream",
        "workstream.entry",
        "workstream.created.v1",
        {"workstream_id": "workstream:idem", "name": "Deployment readiness"},
        append_bridged_workstream_event,
    ),
]


@pytest.mark.parametrize("family,fact_type,event_type,payload,appender", _CASES, ids=[case[0] for case in _CASES])
def test_bridge_appender_is_idempotent_on_domain_event_id(
    tmp_path: Path,
    family: str,
    fact_type: str,
    event_type: str,
    payload: dict[str, Any],
    appender: Callable[..., Any],
) -> None:
    event = _event(event_id=f"evt-idempotent-{family}", event_type=event_type, payload=payload)

    first = appender(event, db_root=tmp_path)
    second = appender(event, db_root=tmp_path)

    snapshot = ProgramFactStore("acme", db_root=tmp_path).snapshot()
    matching = [
        fact
        for fact in snapshot.facts
        if fact.fact_type == fact_type and fact.domain_event_id == event.event_id
    ]

    assert first.action == "created", f"{family}: expected first append to create a fact"
    assert second.action == "noop", f"{family}: expected second append (same domain_event_id) to be a no-op"
    assert len(matching) == 1, f"{family}: expected exactly one fact, found {len(matching)}"
