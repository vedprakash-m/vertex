"""specs/people.md Phase 1, PPL-W1.7: tests for the field-level signed
change journal (src/core/people_change_journal.py). Rotation/hash-chain-
continuity contract tests live in
tests/contracts/test_people_registry_contracts.py (the exact file
specs/people.md §9.1 names for this work item's verification bar)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core.people_change_journal import (
    STREAM_PEOPLE_CHANGES,
    STREAM_PEOPLE_CONFLICTS,
    STREAM_PEOPLE_REFRESH_TELEMETRY,
    append_people_change_record,
    append_people_conflict_record,
    append_people_refresh_telemetry_record,
    find_refresh_telemetry_record,
    genesis_prev_hash,
    journal_active_path,
    read_journal_records,
    verify_journal_hash_chain,
)

_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def _append_change(knowledge_root: Path, *, as_of: datetime = _NOW, **overrides) -> dict:
    defaults = dict(
        workspace_id="workspace:acme",
        transaction_id="registry-tx-1",
        generation_id="generation-1",
        authenticated_principal="ACME\\operator",
        operation="field_updated",
        entity_id="person:1",
        field="manager_entity_id",
        before="person:old",
        after="person:new",
        source="graph",
        reason="structured directory refresh",
    )
    defaults.update(overrides)
    return append_people_change_record(knowledge_root, as_of=as_of, **defaults)


def test_first_record_chains_to_the_genesis_hash(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"

    record = _append_change(knowledge_root)

    assert record["previous_event_hash"] == genesis_prev_hash("workspace:acme", STREAM_PEOPLE_CHANGES)
    assert record["sequence"] == 1
    assert record["event_hash"].startswith("sha256:")


def test_second_record_chains_to_the_first_records_event_hash(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    first = _append_change(knowledge_root)

    second = _append_change(knowledge_root, entity_id="person:2")

    assert second["previous_event_hash"] == first["event_hash"]
    assert second["sequence"] == 2


def test_before_after_hash_are_computed_and_none_safe(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"

    record = _append_change(knowledge_root, before=None, after="new value")

    assert record["before_hash"] is None
    assert record["after_hash"] is not None
    assert record["after_hash"].startswith("sha256:")


def test_journal_file_is_written_at_the_expected_path(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _append_change(knowledge_root)

    assert journal_active_path(knowledge_root, STREAM_PEOPLE_CHANGES) == knowledge_root / "_journal" / "people_changes.jsonl"
    assert journal_active_path(knowledge_root, STREAM_PEOPLE_CHANGES).exists()


def test_verify_hash_chain_ok_on_untampered_records(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _append_change(knowledge_root)
    _append_change(knowledge_root, entity_id="person:2")
    records = read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES)

    verification = verify_journal_hash_chain(records, workspace_id="workspace:acme", stream=STREAM_PEOPLE_CHANGES)

    assert verification.ok is True
    assert verification.checked_record_count == 2
    assert verification.violations == ()


def test_verify_hash_chain_detects_tampering(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _append_change(knowledge_root)
    records = read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES)
    tampered = (dict(records[0], after="tampered value"),)

    verification = verify_journal_hash_chain(tampered, workspace_id="workspace:acme", stream=STREAM_PEOPLE_CHANGES)

    assert verification.ok is False
    assert any("event_hash does not match" in violation for violation in verification.violations)


def test_people_conflicts_stream_is_independent_of_people_changes(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _append_change(knowledge_root)

    conflict = append_people_conflict_record(
        knowledge_root,
        workspace_id="workspace:acme",
        conflict_id="conflict-1",
        decision="dismissed",
        authenticated_principal="ACME\\operator",
        reason="duplicate alias, same person confirmed",
        as_of=_NOW,
    )

    assert conflict["sequence"] == 1  # Independent sequence counter from people_changes.
    assert conflict["previous_event_hash"] == genesis_prev_hash("workspace:acme", STREAM_PEOPLE_CONFLICTS)
    assert journal_active_path(knowledge_root, STREAM_PEOPLE_CONFLICTS).exists()
    assert len(read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES)) == 1
    assert len(read_journal_records(knowledge_root, STREAM_PEOPLE_CONFLICTS)) == 1


def test_read_journal_records_excludes_archived_when_requested(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _append_change(knowledge_root)

    only_active = read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES, include_archived=False)

    assert len(only_active) == 1


def _append_telemetry(knowledge_root: Path, *, refresh_run_id: str = "refresh-1", **overrides) -> dict:
    defaults = dict(
        workspace_id="workspace:acme", refresh_run_id=refresh_run_id, provider="acme_directory_export",
        tenant_id="acme-tenant", requested_count=3, observed_count=3, accepted_count=2, quarantined_count=1,
        rejected_count=0, error_count=0, wall_time_seconds=0.42, kill_switch_engaged=False,
        authenticated_principal="ACME\\operator",
    )
    defaults.update(overrides)
    return append_people_refresh_telemetry_record(knowledge_root, as_of=_NOW, **defaults)


def test_refresh_telemetry_stream_is_independent_and_hash_chained(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _append_change(knowledge_root)
    _append_telemetry(knowledge_root)

    records = read_journal_records(knowledge_root, STREAM_PEOPLE_REFRESH_TELEMETRY)
    assert len(records) == 1
    assert records[0]["sequence"] == 1
    assert records[0]["previous_event_hash"] == genesis_prev_hash("workspace:acme", STREAM_PEOPLE_REFRESH_TELEMETRY)
    assert journal_active_path(knowledge_root, STREAM_PEOPLE_REFRESH_TELEMETRY).exists()
    assert len(read_journal_records(knowledge_root, STREAM_PEOPLE_CHANGES)) == 1

    verification = verify_journal_hash_chain(records, workspace_id="workspace:acme", stream=STREAM_PEOPLE_REFRESH_TELEMETRY)
    assert verification.ok is True


def test_find_refresh_telemetry_record_retrieves_by_run_id(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _append_telemetry(knowledge_root, refresh_run_id="refresh-a")
    _append_telemetry(knowledge_root, refresh_run_id="refresh-b", accepted_count=5)

    found = find_refresh_telemetry_record(knowledge_root, refresh_run_id="refresh-b")

    assert found is not None
    assert found["accepted_count"] == 5


def test_find_refresh_telemetry_record_returns_none_for_unknown_run_id(tmp_path: Path) -> None:
    knowledge_root = tmp_path / "knowledge"
    _append_telemetry(knowledge_root)

    assert find_refresh_telemetry_record(knowledge_root, refresh_run_id="nonexistent") is None
