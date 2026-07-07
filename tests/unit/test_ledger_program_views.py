from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core import _db
from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope
from src.core.ledger.program_views import _ensure_schema, canonical_projection_dump, collapse_orphan_links, collapse_shadow_links, connect_projection_db, project_events_to_memory, project_events_to_sqlite
from src.core.ledger.source_refs import LTDeckRef, OperatorAssertionRef


def test_connect_projection_db_uses_strict_local_shared_db_policy(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(_db, "_is_network_filesystem_path", lambda _path: False)

    with connect_projection_db(tmp_path / "projection.sqlite3") as connection:
        _ensure_schema(connection)
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]

    assert str(journal_mode).lower() == "wal"
    assert busy_timeout == 5000
    assert synchronous == 1  # SQLite NORMAL


def test_connect_projection_db_uses_network_safe_delete_journal(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(_db, "_is_network_filesystem_path", lambda _path: True)

    with connect_projection_db(tmp_path / "projection.sqlite3") as connection:
        _ensure_schema(connection)
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

    assert str(journal_mode).lower() == "delete"


def test_connect_projection_db_rolls_back_failed_transaction_and_recovers(tmp_path) -> None:
    db_path = tmp_path / "projection.sqlite3"

    with pytest.raises(RuntimeError, match="simulate interruption"):
        with connect_projection_db(db_path) as connection:
            _ensure_schema(connection)
            connection.execute(
                "INSERT INTO projection_meta (schema_version, built_at, event_watermark, as_of, knowledge_as_of, coverage_earliest, coverage_latest, projector_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("interrupted", "2026-07-21T00:00:00+00:00", "", None, None, None, None, "test"),
            )
            raise RuntimeError("simulate interruption")

    with connect_projection_db(db_path) as connection:
        _ensure_schema(connection)
        rows = connection.execute("SELECT schema_version FROM projection_meta").fetchall()

    assert rows == []


def _deck_ref() -> LTDeckRef:
    return LTDeckRef(file_path="deck.pptx", deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date())


def test_projection_order_independence(tmp_path) -> None:
    events = (
        build_event_envelope(
            program_id="acme",
            event_type="risk.raised.v1",
            occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 6, 10, 9, 0, 0, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="risk.status_changed.v1",
            occurred_at=datetime(2025, 3, 21, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 6, 10, 10, 0, 0, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"risk_id": "risk:r1", "new_status": "active"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="milestone.created.v1",
            occurred_at=datetime(2025, 3, 22, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 6, 10, 11, 0, 0, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"milestone_id": "milestone:m1", "name": "GA", "target_date": "2025-09-30"},
            source_ref=_deck_ref(),
        ),
    )

    first_path = tmp_path / "first.sqlite3"
    second_path = tmp_path / "second.sqlite3"
    project_events_to_sqlite("acme", events, projection_path=first_path)
    project_events_to_sqlite("acme", tuple(reversed(events)), projection_path=second_path)

    assert canonical_projection_dump(first_path) == canonical_projection_dump(second_path)


def test_correction_time_semantics(tmp_path) -> None:
    original = build_event_envelope(
        program_id="acme",
        event_type="milestone.date_revised.v1",
        occurred_at=datetime(2021, 6, 1, tzinfo=timezone.utc),
        recorded_at=datetime(2021, 6, 2, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"milestone_id": "milestone:m1", "new_target_date": "2021-09-30"},
        source_ref=_deck_ref(),
    )
    correction = build_event_envelope(
        program_id="acme",
        event_type="operator.correction.v1",
        occurred_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="operator",
        payload={
            "corrects_event_id": original.event_id,
            "corrected_payload": {"milestone_id": "milestone:m1", "new_target_date": "2021-10-15"},
            "reason": "Deck note corrected",
        },
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 10, tzinfo=timezone.utc)),
    )

    current_path = tmp_path / "current.sqlite3"
    prior_knowledge_path = tmp_path / "prior.sqlite3"
    project_events_to_sqlite(
        "acme",
        (original, correction),
        projection_path=current_path,
        as_of=datetime(2021, 12, 31, tzinfo=timezone.utc),
        knowledge_as_of=datetime(2026, 12, 31, tzinfo=timezone.utc),
    )
    project_events_to_sqlite(
        "acme",
        (original, correction),
        projection_path=prior_knowledge_path,
        as_of=datetime(2021, 12, 31, tzinfo=timezone.utc),
        knowledge_as_of=datetime(2025, 12, 31, tzinfo=timezone.utc),
    )

    current_dump = canonical_projection_dump(current_path)
    prior_dump = canonical_projection_dump(prior_knowledge_path)

    assert current_dump["proj_milestone"][0]["milestone_id"] == "milestone:m1"
    assert current_dump["proj_milestone"][0]["target_date"] == "2021-10-15"
    assert current_dump["proj_milestone"][0]["status"] == "stub"
    assert prior_dump["proj_milestone"][0]["target_date"] == "2021-09-30"


def test_replay_idempotence(tmp_path) -> None:
    event = build_event_envelope(
        program_id="acme",
        event_type="pipeline.gap_detected.v1",
        occurred_at=datetime(2026, 6, 10, 3, 0, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 6, 10, 3, 0, 5, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="workiq_pipeline",
        payload={"pipeline": "workiq", "gap_kind": "null_ids", "detail": "weekly yield [0,0,0]"},
        source_ref=_deck_ref(),
    )
    projection_path = tmp_path / "current.sqlite3"
    second_projection_path = tmp_path / "current_second.sqlite3"

    project_events_to_sqlite("acme", (event,), projection_path=projection_path)
    first_dump = canonical_projection_dump(projection_path)
    project_events_to_sqlite("acme", (event,), projection_path=second_projection_path)
    second_dump = canonical_projection_dump(second_projection_path)

    assert first_dump == second_dump


def test_bitemporal_slice_hides_later_domain_events(tmp_path) -> None:
    early = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=datetime(2025, 1, 5, tzinfo=timezone.utc),
        recorded_at=datetime(2025, 1, 6, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
        source_ref=_deck_ref(),
    )
    later = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=datetime(2025, 2, 5, tzinfo=timezone.utc),
        recorded_at=datetime(2025, 2, 6, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r2", "title": "Risk two", "severity": "medium"},
        source_ref=_deck_ref(),
    )

    projection_path = tmp_path / "slice.sqlite3"
    project_events_to_sqlite(
        "acme",
        (early, later),
        projection_path=projection_path,
        as_of=datetime(2025, 1, 31, tzinfo=timezone.utc),
    )
    dump = canonical_projection_dump(projection_path)

    assert [row["risk_id"] for row in dump["proj_risk"]] == ["risk:r1"]


def test_knowledge_as_of_hides_later_recorded_backfill(tmp_path) -> None:
    backfilled = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=datetime(2024, 12, 15, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "title": "Historic risk", "severity": "high"},
        source_ref=_deck_ref(),
    )

    visible_path = tmp_path / "visible.sqlite3"
    hidden_path = tmp_path / "hidden.sqlite3"
    project_events_to_sqlite(
        "acme",
        (backfilled,),
        projection_path=visible_path,
        as_of=datetime(2025, 1, 31, tzinfo=timezone.utc),
        knowledge_as_of=datetime(2026, 12, 31, tzinfo=timezone.utc),
    )
    project_events_to_sqlite(
        "acme",
        (backfilled,),
        projection_path=hidden_path,
        as_of=datetime(2025, 1, 31, tzinfo=timezone.utc),
        knowledge_as_of=datetime(2025, 12, 31, tzinfo=timezone.utc),
    )

    assert [row["risk_id"] for row in canonical_projection_dump(visible_path)["proj_risk"]] == ["risk:r1"]
    assert canonical_projection_dump(hidden_path)["proj_risk"] == []


def test_in_memory_projection_matches_sqlite_projection(tmp_path) -> None:
    events = (
        build_event_envelope(
            program_id="acme",
            event_type="risk.raised.v1",
            occurred_at=datetime(2025, 1, 5, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 6, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="decision.made.v1",
            occurred_at=datetime(2025, 1, 7, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 1, 8, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"decision_id": "decision:d1", "title": "Ship", "decision_text": "Ship it", "decided_by": ["operator"]},
            source_ref=_deck_ref(),
        ),
    )

    projection_path = tmp_path / "projection.sqlite3"
    project_events_to_sqlite("acme", events, projection_path=projection_path)

    assert project_events_to_memory("acme", events) == canonical_projection_dump(projection_path)


def test_projection_records_unambiguous_risk_shadow_links() -> None:
    original = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=datetime(2025, 1, 5, tzinfo=timezone.utc),
        recorded_at=datetime(2025, 1, 6, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "title": "Old risk", "severity": "medium"},
        source_ref=_deck_ref(),
    )
    replacement = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=datetime(2025, 1, 7, tzinfo=timezone.utc),
        recorded_at=datetime(2025, 1, 8, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "title": "New risk", "severity": "high"},
        source_ref=_deck_ref(),
    )

    projection = project_events_to_memory("acme", (original, replacement))

    assert collapse_shadow_links(projection["event_shadow_links"]) == {original.event_id: replacement.event_id}


def test_projection_records_unambiguous_milestone_shadow_links() -> None:
    original = build_event_envelope(
        program_id="acme",
        event_type="milestone.date_revised.v1",
        occurred_at=datetime(2025, 1, 5, tzinfo=timezone.utc),
        recorded_at=datetime(2025, 1, 6, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"milestone_id": "milestone:m1", "new_target_date": "2025-09-30"},
        source_ref=_deck_ref(),
    )
    replacement = build_event_envelope(
        program_id="acme",
        event_type="milestone.date_revised.v1",
        occurred_at=datetime(2025, 1, 7, tzinfo=timezone.utc),
        recorded_at=datetime(2025, 1, 8, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"milestone_id": "milestone:m1", "new_target_date": "2025-10-15"},
        source_ref=_deck_ref(),
    )

    projection = project_events_to_memory("acme", (original, replacement))

    assert collapse_shadow_links(projection["event_shadow_links"]) == {original.event_id: replacement.event_id}


def test_projection_records_orphan_links_when_creation_is_tombstoned() -> None:
    created = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=datetime(2025, 1, 5, tzinfo=timezone.utc),
        recorded_at=datetime(2025, 1, 6, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
        source_ref=_deck_ref(),
    )
    updated = build_event_envelope(
        program_id="acme",
        event_type="risk.status_changed.v1",
        occurred_at=datetime(2025, 1, 7, tzinfo=timezone.utc),
        recorded_at=datetime(2025, 1, 8, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "new_status": "active"},
        source_ref=_deck_ref(),
    )
    tombstone = build_event_envelope(
        program_id="acme",
        event_type="operator.correction.v1",
        occurred_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="operator",
        payload={"corrects_event_id": created.event_id, "corrected_payload": None, "reason": "invalid creation"},
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 1, 10, tzinfo=timezone.utc)),
    )

    projection = project_events_to_memory("acme", (created, updated, tombstone))

    assert collapse_orphan_links(projection["event_orphan_links"]) == {updated.event_id: tombstone.event_id}