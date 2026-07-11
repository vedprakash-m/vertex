from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from src.core.ledger.event_log import (
    ConfidenceTier,
    TemporalConfidence,
    build_event_envelope,
    write_event,
)
from src.core.ledger.program_checkpoint_manifest import (
    build_checkpoint_manifest,
    read_manifest,
    verify_manifest_against_disk,
    verify_manifest_self_hash,
    write_manifest,
)
from src.core.ledger.program_sequence import next_sequence
from src.core.ledger.projection_checkpoint_store import record_checkpoint
from src.core.ledger.source_refs import LTDeckRef


def _write_one_event(program_id: str, programs_root: Path) -> None:
    ref = LTDeckRef(file_path="deck.pptx", deck_date=date(2025, 3, 20))
    event = build_event_envelope(
        program_id=program_id,
        event_type="risk.raised.v1",
        occurred_at=datetime(2025, 3, 20, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 7, 9, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
        source_ref=ref,
    )
    write_event(event, programs_root=programs_root)


def test_manifest_on_empty_program(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    manifest = build_checkpoint_manifest("acme", programs_root=programs_root)
    assert manifest.event_log_last_hash is None
    assert manifest.event_log_sequence == 0
    assert manifest.projection_checkpoints == ()
    assert manifest.manifest_hash.startswith("sha256:")


def test_manifest_captures_event_log_position(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_one_event("acme", programs_root)
    manifest = build_checkpoint_manifest("acme", programs_root=programs_root)
    assert manifest.event_log_last_hash is not None
    assert manifest.event_log_last_hash.startswith("sha256:")


def test_manifest_captures_program_sequence(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    next_sequence("acme", programs_root=programs_root)
    next_sequence("acme", programs_root=programs_root)
    manifest = build_checkpoint_manifest("acme", programs_root=programs_root)
    assert manifest.event_log_sequence == 2


def test_manifest_captures_projection_checkpoints(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    record_checkpoint(
        "acme",
        "fact_bridge",
        watermark_event_id="01HZ",
        watermark_recorded_at=datetime(2026, 7, 9, tzinfo=timezone.utc),
        projector_version="v1",
        policy_version="p1",
        programs_root=programs_root,
    )
    manifest = build_checkpoint_manifest("acme", programs_root=programs_root)
    assert len(manifest.projection_checkpoints) == 1
    assert manifest.projection_checkpoints[0].projection_name == "fact_bridge"


def test_manifest_hashes_tracked_files(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    tracked = tmp_path / "some_store.sqlite3"
    tracked.write_bytes(b"fake db contents")
    manifest = build_checkpoint_manifest(
        "acme", programs_root=programs_root, tracked_files={"some_store": tracked}
    )
    assert "some_store" in manifest.tracked_file_hashes
    assert manifest.tracked_file_hashes["some_store"].startswith("sha256:")


def test_manifest_omits_missing_tracked_files(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    missing = tmp_path / "does_not_exist.sqlite3"
    manifest = build_checkpoint_manifest(
        "acme", programs_root=programs_root, tracked_files={"missing_store": missing}
    )
    assert "missing_store" not in manifest.tracked_file_hashes


def test_write_and_read_manifest_round_trip(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    _write_one_event("acme", programs_root)
    manifest = build_checkpoint_manifest("acme", programs_root=programs_root)

    manifest_path = tmp_path / "manifest.json"
    write_manifest(manifest, manifest_path)
    loaded = read_manifest(manifest_path)

    assert loaded == manifest


def test_verify_manifest_self_hash_detects_tamper(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    manifest = build_checkpoint_manifest("acme", programs_root=programs_root)
    assert verify_manifest_self_hash(manifest)

    from dataclasses import replace

    tampered = replace(manifest, event_log_sequence=999)
    assert not verify_manifest_self_hash(tampered)


def test_verify_manifest_against_disk_detects_drift(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    tracked = tmp_path / "some_store.sqlite3"
    tracked.write_bytes(b"original contents")
    manifest = build_checkpoint_manifest(
        "acme", programs_root=programs_root, tracked_files={"some_store": tracked}
    )

    assert verify_manifest_against_disk(manifest, tracked_files={"some_store": tracked}) == ()

    tracked.write_bytes(b"drifted contents")
    issues = verify_manifest_against_disk(manifest, tracked_files={"some_store": tracked})
    assert len(issues) == 1
    assert "hash mismatch" in issues[0]


def test_verify_manifest_against_disk_detects_missing_file(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    tracked = tmp_path / "some_store.sqlite3"
    tracked.write_bytes(b"original contents")
    manifest = build_checkpoint_manifest(
        "acme", programs_root=programs_root, tracked_files={"some_store": tracked}
    )

    tracked.unlink()
    issues = verify_manifest_against_disk(manifest, tracked_files={"some_store": tracked})
    assert len(issues) == 1
    assert "missing" in issues[0]


def test_outbox_watermarks_round_trip(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    manifest = build_checkpoint_manifest(
        "acme", programs_root=programs_root, outbox_watermarks={"ado_actuation": 5, "teams_actuation": 2}
    )
    assert manifest.outbox_watermarks == {"ado_actuation": 5, "teams_actuation": 2}
