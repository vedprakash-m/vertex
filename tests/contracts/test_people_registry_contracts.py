"""specs/people.md PPL-W1.7 (Phase 1): rotation and hash-chain-continuity
contract tests for the field-level signed change journal
(src/core/people_change_journal.py).

specs/people.md §9.1's own verification bar for PPL-W1.7: "hash-chain
continuity holds across a rotation boundary; rotation never truncates
acknowledged history."
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core.archive_signing import manifest_signature_sidecar_path
from src.core.people_change_journal import (
    STREAM_PEOPLE_CHANGES,
    append_people_change_record,
    read_journal_records,
    verify_journal_hash_chain,
)

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)

_TINY_MAX_BYTES = 400  # Small enough that a handful of records forces at least one rotation.


def _append(knowledge_root: Path, index: int) -> dict:
    return append_people_change_record(
        knowledge_root,
        workspace_id="workspace:acme",
        transaction_id=f"registry-tx-{index}",
        generation_id="generation-1",
        authenticated_principal="ACME\\operator",
        operation="field_updated",
        entity_id=f"person:{index}",
        field="manager_entity_id",
        before=f"person:old-{index}",
        after=f"person:new-{index}",
        source="graph",
        reason="structured directory refresh",
        max_bytes=_TINY_MAX_BYTES,
        as_of=_NOW,
    )


def test_hash_chain_continuity_holds_across_a_rotation_boundary(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    written = tuple(_append(knowledge_root, index) for index in range(20))

    archive_dir = knowledge_root / "_journal" / "archive" / str(_NOW.year)
    assert archive_dir.exists() and any(archive_dir.iterdir()), "expected at least one rotation to have occurred"

    all_records = read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES, include_archived=True)
    verification = verify_journal_hash_chain(all_records, workspace_id="workspace:acme", stream=STREAM_PEOPLE_CHANGES)

    assert verification.ok is True, verification.violations
    assert verification.checked_record_count == len(written)
    # Sequence numbers are strictly monotonic across the rotation boundary -- no reset to 1.
    assert [record["sequence"] for record in all_records] == list(range(1, len(written) + 1))


def test_rotation_never_truncates_acknowledged_history(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    written = tuple(_append(knowledge_root, index) for index in range(20))

    all_records = read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES, include_archived=True)

    assert len(all_records) == len(written)
    assert {record["event_id"] for record in all_records} == {record["event_id"] for record in written}
    assert {record["entity_id"] for record in all_records} == {f"person:{index}" for index in range(20)}


def test_rotated_segment_is_signed_when_a_signing_key_is_available(monkeypatch, tmp_path: Path) -> None:
    import src.core.people_change_journal as journal_module

    monkeypatch.setattr(journal_module, "archive_signing_unavailable", lambda: False)
    monkeypatch.setattr(journal_module, "get_archive_signing_key", lambda: b"test-signing-key")

    knowledge_root = tmp_path / "knowledge"
    for index in range(20):
        _append(knowledge_root, index)

    archive_dir = knowledge_root / "_journal" / "archive" / str(_NOW.year)
    segment_paths = tuple(path for path in archive_dir.iterdir() if path.suffix == ".jsonl")
    assert segment_paths, "expected at least one rotated segment"
    for segment_path in segment_paths:
        assert manifest_signature_sidecar_path(segment_path).exists()


def test_rotated_segment_is_unsigned_but_still_written_when_signing_unavailable(monkeypatch, tmp_path: Path) -> None:
    import src.core.people_change_journal as journal_module

    monkeypatch.setattr(journal_module, "archive_signing_unavailable", lambda: True)

    knowledge_root = tmp_path / "knowledge"
    for index in range(20):
        _append(knowledge_root, index)

    archive_dir = knowledge_root / "_journal" / "archive" / str(_NOW.year)
    segment_paths = tuple(path for path in archive_dir.iterdir() if path.suffix == ".jsonl")
    assert segment_paths, "rotation must still succeed unsigned"
    for segment_path in segment_paths:
        assert not manifest_signature_sidecar_path(segment_path).exists()
