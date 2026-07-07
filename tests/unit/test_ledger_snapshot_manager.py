from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import stat

from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope
from src.core.ledger.program_views import project_events_to_sqlite
from src.core.ledger.source_refs import LTDeckRef, OperatorAssertionRef
from src.core.projections.snapshot_manager import build_baseline_hardlock_event, build_snapshot_manifest, compute_snapshot_hash, write_projection_snapshot


def _deck_ref() -> LTDeckRef:
    return LTDeckRef(file_path="deck.pptx", deck_date=datetime(2025, 3, 20, tzinfo=timezone.utc).date(), slide_number=8)


def test_snapshot_hash_is_stable_for_same_projection(tmp_path) -> None:
    event = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
        recorded_at=datetime(2025, 3, 21, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
        source_ref=_deck_ref(),
    )
    first = tmp_path / "first.sqlite3"
    second = tmp_path / "second.sqlite3"
    project_events_to_sqlite("acme", (event,), projection_path=first)
    project_events_to_sqlite("acme", (event,), projection_path=second)

    assert compute_snapshot_hash(first) == compute_snapshot_hash(second)


def test_write_projection_snapshot_writes_content_addressed_files_and_manifest(tmp_path) -> None:
    event = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
        recorded_at=datetime(2025, 3, 21, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
        source_ref=_deck_ref(),
    )
    projection_path = tmp_path / "current.sqlite3"
    result = project_events_to_sqlite("acme", (event,), projection_path=projection_path)

    snapshot = write_projection_snapshot(
        "acme",
        79,
        result,
        events=(event,),
        as_of=datetime(2025, 3, 31, tzinfo=timezone.utc),
        programs_root=tmp_path / "programs",
    )

    manifest = json.loads(snapshot.manifest_path.read_text(encoding="utf-8"))

    assert snapshot.snapshot_path.exists()
    assert snapshot.manifest_path.exists()
    assert snapshot.snapshot_hash in snapshot.snapshot_path.name
    assert manifest["issue_number"] == 79
    assert manifest["snapshot_hash"] == snapshot.snapshot_hash
    assert manifest["event_id_watermark"] == result.event_watermark
    assert manifest["contributing_event_count"] == result.event_count
    assert snapshot.snapshot_path.stat().st_mode & stat.S_IWRITE == 0


def test_build_snapshot_manifest_uses_chain_head_hash() -> None:
    event = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
        recorded_at=datetime(2025, 3, 21, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
        source_ref=_deck_ref(),
    )
    projection_path = Path("unused.sqlite3")
    result = type("ProjectionResultStub", (), {
        "event_watermark": event.event_id,
        "event_count": 1,
        "projection_path": projection_path,
        "coverage_earliest": event.occurred_at.isoformat(),
        "coverage_latest": event.occurred_at.isoformat(),
    })()

    manifest = build_snapshot_manifest(
        issue_number=1,
        snapshot_hash="abc123",
        projection_result=result,
        events=(event,),
        as_of=None,
    )

    assert manifest["hash_chain_head"].startswith("sha256:")


def test_build_baseline_hardlock_event_uses_snapshot_manifest_fields(tmp_path) -> None:
    event = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
        recorded_at=datetime(2025, 3, 21, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
        source_ref=_deck_ref(),
    )
    projection_path = tmp_path / "current.sqlite3"
    result = project_events_to_sqlite("acme", (event,), projection_path=projection_path)
    snapshot = write_projection_snapshot("acme", 79, result, events=(event,), programs_root=tmp_path / "programs")

    hardlock = build_baseline_hardlock_event(
        "acme",
        79,
        snapshot,
        result,
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=datetime(2026, 6, 11, tzinfo=timezone.utc)),
        actor="operator",
        recorded_at=datetime(2026, 6, 11, tzinfo=timezone.utc),
    )

    assert hardlock.event_type == "operator.baseline_hardlock.v1"
    assert hardlock.payload["issue_number"] == 79
    assert hardlock.payload["snapshot_hash"] == snapshot.snapshot_hash
    assert hardlock.payload["event_id_watermark"] == result.event_watermark