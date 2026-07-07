from __future__ import annotations

from datetime import datetime, timezone
import json

from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope
from src.core.ledger.program_views import canonical_projection_dump, project_events_to_sqlite
from src.core.ledger.source_refs import LTDeckRef


def _deck_ref() -> LTDeckRef:
    return LTDeckRef(file_path="deck.pptx", deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date(), slide_number=3)


def test_identity_structure_observation_and_knowledge_folds(tmp_path) -> None:
    events = (
        build_event_envelope(
            program_id="acme",
            event_type="program.charter_established.v1",
            occurred_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"charter_id": "charter:c1", "mission": "Ship", "scope_statement": "Ramp", "success_criteria": ["Done"]},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="program.phase_entered.v1",
            occurred_at=datetime(2025, 1, 3, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 3, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"phase_id": "phase:p1", "phase_name": "Pilot"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="program.sub_program_added.v1",
            occurred_at=datetime(2025, 1, 4, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 4, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"sub_program_id": "program:contoso", "name": "Contoso", "relationship": "sub_program", "cadence": "daily"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="schedule.baseline_set.v1",
            occurred_at=datetime(2025, 1, 5, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 5, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"schedule_id": "schedule:s1", "baseline_name": "v1", "milestone_dates": {"milestone:m1": "2025-09-30"}},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="workstream.created.v1",
            occurred_at=datetime(2025, 1, 6, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 6, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"workstream_id": "workstream:w1", "name": "Ramp", "owner_person_id": "person:operator"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="deliverable.created.v1",
            occurred_at=datetime(2025, 1, 7, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 7, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"deliverable_id": "deliverable:d1", "name": "Mailer", "workstream_id": "workstream:w1", "due_date": "2025-04-01"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="sku_generation.added.v1",
            occurred_at=datetime(2025, 1, 8, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 8, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"sku_generation_id": "sku_generation:gen9", "name": "Gen9", "first_deployment_date": "2025-07-01", "products": ["product:xio"]},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="commitment.made.v1",
            occurred_at=datetime(2025, 1, 9, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 9, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"commitment_id": "commitment:c1", "text": "Ship by Q3", "owner_person_id": "person:operator", "due_date": "2025-09-30"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="kpi.defined.v1",
            occurred_at=datetime(2025, 1, 10, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 10, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"kpi_id": "kpi:k1", "name": "Deployments", "unit": "count", "thresholds": {"green": 10}},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="metric.observed.v1",
            occurred_at=datetime(2025, 1, 11, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 11, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"kpi_id": "kpi:k1", "value": 12, "unit": "count", "window_end": "2025-01-11T00:00:00+00:00", "dimensions": {"ring": "prod"}},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="incident.opened.v1",
            occurred_at=datetime(2025, 1, 12, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 12, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"incident_id": "incident:i1", "severity": "sev2", "title": "Outage", "impacted_entities": ["workstream:w1"]},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="knowledge.article_added.v1",
            occurred_at=datetime(2025, 1, 13, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 13, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"article_id": "article:a1", "title": "Runbook", "location": "vault://a1", "topics": ["ops"]},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="playbook.created.v1",
            occurred_at=datetime(2025, 1, 14, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 14, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"playbook_id": "playbook:p1", "title": "Escalation", "location": "vault://p1"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="artifact.published.v1",
            occurred_at=datetime(2025, 1, 15, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 15, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"artifact_id": "artifact:art1", "artifact_kind": "newsletter", "title": "Issue 1", "location": "archive://issue1"},
            source_ref=_deck_ref(),
        ),
    )

    projection_path = tmp_path / "broader.sqlite3"
    project_events_to_sqlite("acme", events, projection_path=projection_path)
    dump = canonical_projection_dump(projection_path)

    assert dump["proj_program"][0]["program_id"] == "acme"
    assert dump["proj_program"][0]["current_phase_id"] == "phase:p1"
    assert json.loads(dump["proj_program"][0]["sub_programs"])[0]["sub_program_id"] == "program:contoso"

    assert dump["proj_phase"][0]["phase_id"] == "phase:p1"
    assert dump["proj_schedule_baseline"][0]["schedule_id"] == "schedule:s1"
    assert dump["proj_workstream"][0]["workstream_id"] == "workstream:w1"
    assert dump["proj_deliverable"][0]["deliverable_id"] == "deliverable:d1"
    assert dump["proj_sku_generation"][0]["sku_generation_id"] == "sku_generation:gen9"
    assert json.loads(dump["proj_sku_generation"][0]["products"]) == ["product:xio"]

    assert dump["proj_commitment"][0]["commitment_id"] == "commitment:c1"
    assert dump["proj_kpi"][0]["kpi_id"] == "kpi:k1"
    assert dump["proj_kpi_series"][0]["kpi_id"] == "kpi:k1"
    assert json.loads(dump["proj_kpi_series"][0]["dimensions"]) == {"ring": "prod"}
    assert dump["proj_incident"][0]["incident_id"] == "incident:i1"
    assert dump["proj_knowledge_article"][0]["article_id"] == "article:a1"
    assert dump["proj_playbook"][0]["playbook_id"] == "playbook:p1"
    assert dump["proj_published_artifact"][0]["artifact_id"] == "artifact:art1"

    links = {(row["from_entity"], row["link_kind"], row["to_entity"]) for row in dump["entity_links"]}
    assert ("acme", "sub_program", "program:contoso") in links
    assert ("sku_generation:gen9", "product", "product:xio") in links
