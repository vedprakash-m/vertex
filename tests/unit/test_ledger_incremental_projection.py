"""Tests for project_events_incremental_to_sqlite (W7-2 / PS-28).

Critical: tests use the two-call pattern (base build → delta apply) to
exercise the actual incremental fold path, not just all-at-once which
always falls through to full rebuild.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope, write_event
from src.core.ledger.event_index import rebuild_event_index
from src.core.ledger.program_views import canonical_projection_dump, project_events_incremental_to_sqlite, project_events_to_sqlite
from src.core.ledger.source_refs import LTDeckRef


def _deck_ref() -> LTDeckRef:
    return LTDeckRef(file_path="deck.pptx", deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date(), slide_number=4)


def _risk_raised(program_id: str, risk_id: str, recorded_at: datetime, *, title: str = "Risk", severity: str = "high") -> object:
    return build_event_envelope(
        program_id=program_id,
        event_type="risk.raised.v1",
        occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
        recorded_at=recorded_at,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": risk_id, "title": title, "severity": severity},
        source_ref=_deck_ref(),
    )


def _risk_status(program_id: str, risk_id: str, recorded_at: datetime, *, new_status: str = "blocked") -> object:
    return build_event_envelope(
        program_id=program_id,
        event_type="risk.status_changed.v1",
        occurred_at=recorded_at,
        recorded_at=recorded_at,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": risk_id, "new_status": new_status},
        source_ref=_deck_ref(),
    )


def test_incremental_equals_full_for_supported_delta(tmp_path) -> None:
    """Regression: all-at-once incremental produces same result as full rebuild."""
    programs_root = tmp_path / "programs"
    base_events = (
        build_event_envelope(
            program_id="acme",
            event_type="risk.raised.v1",
            occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 3, 21, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="decision.made.v1",
            occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 3, 21, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"decision_id": "decision:d1", "title": "Ship", "decision_text": "Ship it", "decided_by": ["operator"]},
            source_ref=_deck_ref(),
        ),
    )
    delta_events = (
        build_event_envelope(
            program_id="acme",
            event_type="risk.status_changed.v1",
            occurred_at=datetime(2025, 3, 22, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 3, 22, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
            actor="import",
            payload={"risk_id": "risk:r1", "new_status": "blocked"},
            source_ref=_deck_ref(),
        ),
        build_event_envelope(
            program_id="acme",
            event_type="decision.revised.v1",
            occurred_at=datetime(2025, 3, 23, tzinfo=timezone.utc),
            recorded_at=datetime(2025, 3, 23, tzinfo=timezone.utc),
            temporal_confidence=TemporalConfidence.EXACT,
            confidence=ConfidenceTier.OPERATOR_CONFIRMED,
            actor="operator",
            payload={"decision_id": "decision:d1", "revision_text": "Ship after review", "reason": "Need gate"},
            source_ref=_deck_ref(),
        ),
    )

    for event in (*base_events, *delta_events):
        write_event(event, programs_root=programs_root)
    rebuild_event_index("acme", programs_root=programs_root)

    incremental_path = tmp_path / "incremental_result.sqlite3"
    full_path = tmp_path / "full.sqlite3"

    project_events_incremental_to_sqlite("acme", (*base_events, *delta_events), projection_path=incremental_path, programs_root=programs_root)
    project_events_to_sqlite("acme", (*base_events, *delta_events), projection_path=full_path)

    assert canonical_projection_dump(incremental_path) == canonical_projection_dump(full_path)


def test_two_call_incremental_equals_full(tmp_path) -> None:
    """Core W7-2 test: base build followed by delta call produces same result as full rebuild.

    Before W7-2, the small-delta path fell through to full rebuild (PS-28).
    After W7-2, it uses _incremental_fold — this test verifies correctness.
    """
    programs_root = tmp_path / "programs"
    prog = "acme"
    t1 = datetime(2025, 3, 20, tzinfo=timezone.utc)
    t2 = datetime(2025, 3, 21, tzinfo=timezone.utc)

    base_events = (
        _risk_raised(prog, "risk:r1", t1, title="Deployment risk"),
        _risk_raised(prog, "risk:r2", t1, title="Scope risk", severity="medium"),
    )
    delta = (_risk_status(prog, "risk:r1", t2, new_status="mitigated"),)

    for event in (*base_events, *delta):
        write_event(event, programs_root=programs_root)
    rebuild_event_index(prog, programs_root=programs_root)

    incr_path = tmp_path / "incr.sqlite3"
    full_path = tmp_path / "full.sqlite3"

    # Build base projection
    project_events_incremental_to_sqlite(prog, base_events, projection_path=incr_path, programs_root=programs_root)
    # Apply delta incrementally (this is the new W7-2 path)
    result = project_events_incremental_to_sqlite(prog, (*base_events, *delta), projection_path=incr_path, programs_root=programs_root)

    # Full rebuild for comparison
    project_events_to_sqlite(prog, (*base_events, *delta), projection_path=full_path)

    incr_dump = canonical_projection_dump(incr_path)
    full_dump = canonical_projection_dump(full_path)

    # Entity state must match full rebuild
    assert incr_dump["proj_risk"] == full_dump["proj_risk"], "risk entity state mismatch"
    # Watermark must advance to the last event
    assert result.event_watermark == full_dump["projection_meta"][0]["event_watermark"]


def test_incremental_does_not_touch_unchanged_entity(tmp_path) -> None:
    """Entity with no delta events keeps its row unchanged after incremental fold."""
    programs_root = tmp_path / "programs"
    prog = "acme"
    t1 = datetime(2025, 3, 20, tzinfo=timezone.utc)
    t2 = datetime(2025, 3, 21, tzinfo=timezone.utc)

    base_events = (
        _risk_raised(prog, "risk:r1", t1, title="Alpha"),
        _risk_raised(prog, "risk:r2", t1, title="Beta"),
    )
    delta = (_risk_status(prog, "risk:r1", t2, new_status="closed"),)

    for event in (*base_events, *delta):
        write_event(event, programs_root=programs_root)
    rebuild_event_index(prog, programs_root=programs_root)

    incr_path = tmp_path / "incr.sqlite3"
    project_events_incremental_to_sqlite(prog, base_events, projection_path=incr_path, programs_root=programs_root)
    project_events_incremental_to_sqlite(prog, (*base_events, *delta), projection_path=incr_path, programs_root=programs_root)

    dump = canonical_projection_dump(incr_path)
    risk_by_id = {r["risk_id"]: r for r in dump["proj_risk"]}

    # r1 should be closed (delta updated it)
    assert risk_by_id["risk:r1"]["status"] == "closed"
    # r2 should still be its original state (not touched by delta)
    assert risk_by_id["risk:r2"]["title"] == "Beta"


def test_large_delta_falls_through_to_full_rebuild(tmp_path) -> None:
    """Delta > _MAX_INCREMENTAL_DELTA triggers full rebuild, not incremental fold."""
    programs_root = tmp_path / "programs"
    prog = "acme"
    t0 = datetime(2025, 3, 20, tzinfo=timezone.utc)

    base_events = (_risk_raised(prog, "risk:r0", t0),)
    # 51 new events exceeds _MAX_INCREMENTAL_DELTA (50)
    delta = tuple(
        _risk_raised(prog, f"risk:r{i}", t0, title=f"Risk {i}")
        for i in range(1, 52)
    )

    for event in (*base_events, *delta):
        write_event(event, programs_root=programs_root)
    rebuild_event_index(prog, programs_root=programs_root)

    incr_path = tmp_path / "incr.sqlite3"
    full_path = tmp_path / "full.sqlite3"

    project_events_incremental_to_sqlite(prog, base_events, projection_path=incr_path, programs_root=programs_root)
    project_events_incremental_to_sqlite(prog, (*base_events, *delta), projection_path=incr_path, programs_root=programs_root)
    project_events_to_sqlite(prog, (*base_events, *delta), projection_path=full_path)

    incr_dump = canonical_projection_dump(incr_path)
    full_dump = canonical_projection_dump(full_path)
    assert incr_dump["proj_risk"] == full_dump["proj_risk"]


def test_correction_event_triggers_full_rebuild(tmp_path) -> None:
    """operator.correction.v1 in delta triggers full rebuild (retroactive state change)."""
    programs_root = tmp_path / "programs"
    prog = "acme"
    t1 = datetime(2025, 3, 20, tzinfo=timezone.utc)
    t2 = datetime(2025, 3, 21, tzinfo=timezone.utc)

    base = (_risk_raised(prog, "risk:r1", t1, severity="low"),)
    for event in base:
        write_event(event, programs_root=programs_root)

    incr_path = tmp_path / "incr.sqlite3"
    project_events_incremental_to_sqlite(prog, base, projection_path=incr_path, programs_root=programs_root)

    correction = build_event_envelope(
        program_id=prog,
        event_type="operator.correction.v1",
        occurred_at=t2,
        recorded_at=t2,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="operator",
        payload={"corrects_event_id": base[0].event_id, "corrected_payload": {"risk_id": "risk:r1", "title": "Risk", "severity": "critical"}, "reason": "wrong severity"},
        source_ref=_deck_ref(),
    )
    write_event(correction, programs_root=programs_root)
    rebuild_event_index(prog, programs_root=programs_root)

    project_events_incremental_to_sqlite(prog, (*base, correction), projection_path=incr_path, programs_root=programs_root)

    full_path = tmp_path / "full.sqlite3"
    project_events_to_sqlite(prog, (*base, correction), projection_path=full_path)

    incr_dump = canonical_projection_dump(incr_path)
    full_dump = canonical_projection_dump(full_path)
    assert incr_dump["proj_risk"] == full_dump["proj_risk"]


def test_no_delta_returns_cached_result(tmp_path) -> None:
    """Calling incremental with same events as before returns cached projection."""
    programs_root = tmp_path / "programs"
    prog = "acme"
    t1 = datetime(2025, 3, 20, tzinfo=timezone.utc)
    events = (_risk_raised(prog, "risk:r1", t1),)

    for event in events:
        write_event(event, programs_root=programs_root)
    rebuild_event_index(prog, programs_root=programs_root)

    incr_path = tmp_path / "incr.sqlite3"
    r1 = project_events_incremental_to_sqlite(prog, events, projection_path=incr_path, programs_root=programs_root)
    r2 = project_events_incremental_to_sqlite(prog, events, projection_path=incr_path, programs_root=programs_root)

    assert r1.event_watermark == r2.event_watermark
    assert r2.event_count == 1
