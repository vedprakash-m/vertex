from __future__ import annotations

from datetime import datetime, timezone
import json

from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope
from src.core.ledger.program_views import canonical_projection_dump, project_events_to_sqlite
from src.core.ledger.source_refs import LTDeckRef


def _deck_ref() -> LTDeckRef:
    return LTDeckRef(file_path="deck.pptx", deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date(), slide_number=5)


def test_broader_update_events_fold_correctly(tmp_path) -> None:
    events = (
        build_event_envelope(
            program_id="acme",
            event_type="workstream.created.v1",
            occurred_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"workstream_id": "workstream:w1", "name": "Ramp", "owner_person_id": "person:a"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="workstream.owner_changed.v1",
            occurred_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"workstream_id": "workstream:w1", "new_owner_person_id": "person:b"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="workstream.status_changed.v1",
            occurred_at=datetime(2025, 1, 3, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 3, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"workstream_id": "workstream:w1", "new_status": "blocked"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="deliverable.created.v1",
            occurred_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"deliverable_id": "deliverable:d1", "name": "Mailer", "workstream_id": "workstream:w1"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="deliverable.status_changed.v1",
            occurred_at=datetime(2025, 1, 4, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 4, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"deliverable_id": "deliverable:d1", "new_status": "done"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="commitment.made.v1",
            occurred_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"commitment_id": "commitment:c1", "text": "Ship", "owner_person_id": "person:a", "due_date": "2025-03-01"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="commitment.slipped.v1",
            occurred_at=datetime(2025, 1, 5, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 5, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"commitment_id": "commitment:c1", "new_due_date": "2025-04-01"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="commitment.fulfilled.v1",
            occurred_at=datetime(2025, 1, 6, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 6, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"commitment_id": "commitment:c1", "fulfilled_on": "2025-01-06"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="kpi.defined.v1",
            occurred_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"kpi_id": "kpi:k1", "name": "Deployments"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="kpi.decommissioned.v1",
            occurred_at=datetime(2025, 1, 7, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 7, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"kpi_id": "kpi:k1", "reason": "retired"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="incident.opened.v1",
            occurred_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"incident_id": "incident:i1", "severity": "sev2", "title": "Outage"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="incident.resolved.v1",
            occurred_at=datetime(2025, 1, 8, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 8, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"incident_id": "incident:i1", "resolved_on": "2025-01-08", "mttr_minutes": 15, "root_cause": "patch"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="knowledge.article_added.v1",
            occurred_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"article_id": "article:a1", "title": "Runbook", "location": "vault://old"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="knowledge.article_revised.v1",
            occurred_at=datetime(2025, 1, 9, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 9, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"article_id": "article:a1", "revision_summary": "new", "location": "vault://new"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="knowledge.article_removed.v1",
            occurred_at=datetime(2025, 1, 10, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 10, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"article_id": "article:a1", "reason": "obsolete"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="decision.made.v1",
            occurred_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"decision_id": "decision:d1", "title": "Ship", "decision_text": "Ship", "decided_by": ["operator"]},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="decision.superseded.v1",
            occurred_at=datetime(2025, 1, 11, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 11, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"decision_id": "decision:d1", "supersedes_decision_id": "decision:d0", "reason": "new plan"},
            source_ref=_deck_ref(),
        ),
    )

    projection_path = tmp_path / "updates.sqlite3"
    project_events_to_sqlite("acme", events, projection_path=projection_path)
    dump = canonical_projection_dump(projection_path)

    assert dump["proj_workstream"][0]["owner_person_id"] == "person:b"
    assert dump["proj_workstream"][0]["status"] == "blocked"
    assert dump["proj_deliverable"][0]["status"] == "done"
    assert dump["proj_commitment"][0]["status"] == "fulfilled"
    assert dump["proj_commitment"][0]["due_date"] == "2025-04-01"
    assert dump["proj_commitment"][0]["slip_count"] == 1
    assert dump["proj_kpi"][0]["status"] == "decommissioned"
    assert dump["proj_incident"][0]["status"] == "resolved"
    assert dump["proj_incident"][0]["mttr_minutes"] == 15
    assert dump["proj_knowledge_article"][0]["status"] == "removed"
    assert dump["proj_knowledge_article"][0]["location"] == "vault://new"
    assert dump["proj_decision"][0]["status"] == "superseded"
    links = {(row["from_entity"], row["link_kind"], row["to_entity"]) for row in dump["entity_links"]}
    assert ("decision:d1", "supersedes", "decision:d0") in links