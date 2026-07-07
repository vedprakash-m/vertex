from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.program_fact_store import (
    ProgramFactStore,
    load_program_facts,
    project_workstream_associations,
)
from src.core.workstream_association_store import (
    WorkstreamAssociationRecord,
    append_workstream_association_records,
    read_workstream_association_records,
)


def test_read_workstream_association_records_rejects_numeric_string_issue_number(tmp_path: Path) -> None:
    ledger_path = tmp_path / "acme" / "journal" / "workstream_associations.jsonl"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        json.dumps(
            {
                "recorded_at": "2026-06-06T12:00:00+00:00",
                "edition": "acme_weekly",
                "issue_number": "1",
                "workstream_id": "deployment",
                "source_type": "review",
                "source_slice_id": None,
                "section_id": "ws:deployment",
                "work_item_id": 123,
                "note": "kept",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="issue_number must be an integer"):
        read_workstream_association_records("acme", programs_root=tmp_path)


def test_read_workstream_association_records_rejects_numeric_string_work_item_id(tmp_path: Path) -> None:
    ledger_path = tmp_path / "acme" / "journal" / "workstream_associations.jsonl"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        json.dumps(
            {
                "recorded_at": "2026-06-06T12:00:00+00:00",
                "edition": "acme_weekly",
                "issue_number": 1,
                "workstream_id": "deployment",
                "source_type": "review",
                "source_slice_id": None,
                "section_id": "ws:deployment",
                "work_item_id": "123",
                "note": "kept",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="work_item_id must be an integer"):
        read_workstream_association_records("acme", programs_root=tmp_path)


def test_read_workstream_association_records_rejects_non_string_edition(tmp_path: Path) -> None:
    ledger_path = tmp_path / "acme" / "journal" / "workstream_associations.jsonl"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        json.dumps(
            {
                "recorded_at": "2026-06-06T12:00:00+00:00",
                "edition": 123,
                "issue_number": 1,
                "workstream_id": "deployment",
                "source_type": "review",
                "source_slice_id": None,
                "section_id": "ws:deployment",
                "work_item_id": 123,
                "note": "kept",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="edition must be a string"):
        read_workstream_association_records("acme", programs_root=tmp_path)


def test_read_workstream_association_records_rejects_non_string_workstream_id(tmp_path: Path) -> None:
    ledger_path = tmp_path / "acme" / "journal" / "workstream_associations.jsonl"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        json.dumps(
            {
                "recorded_at": "2026-06-06T12:00:00+00:00",
                "edition": "acme_weekly",
                "issue_number": 1,
                "workstream_id": 456,
                "source_type": "review",
                "source_slice_id": None,
                "section_id": "ws:deployment",
                "work_item_id": 123,
                "note": "kept",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="workstream_id must be a string"):
        read_workstream_association_records("acme", programs_root=tmp_path)


def test_read_workstream_association_records_rejects_non_string_section_id(tmp_path: Path) -> None:
    ledger_path = tmp_path / "acme" / "journal" / "workstream_associations.jsonl"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        json.dumps(
            {
                "recorded_at": "2026-06-06T12:00:00+00:00",
                "edition": "acme_weekly",
                "issue_number": 1,
                "workstream_id": "deployment",
                "source_type": "review",
                "source_slice_id": None,
                "section_id": 789,
                "work_item_id": 123,
                "note": "kept",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="section_id must be a string"):
        read_workstream_association_records("acme", programs_root=tmp_path)


def test_read_workstream_association_records_rejects_non_string_note(tmp_path: Path) -> None:
    ledger_path = tmp_path / "acme" / "journal" / "workstream_associations.jsonl"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        json.dumps(
            {
                "recorded_at": "2026-06-06T12:00:00+00:00",
                "edition": "acme_weekly",
                "issue_number": 1,
                "workstream_id": "deployment",
                "source_type": "review",
                "source_slice_id": None,
                "section_id": "ws:deployment",
                "work_item_id": 123,
                "note": 999,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="note must be a string"):
        read_workstream_association_records("acme", programs_root=tmp_path)


def test_read_workstream_association_records_rejects_non_string_recorded_at(tmp_path: Path) -> None:
    ledger_path = tmp_path / "acme" / "journal" / "workstream_associations.jsonl"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        json.dumps(
            {
                "recorded_at": 123,
                "edition": "acme_weekly",
                "issue_number": 1,
                "workstream_id": "deployment",
                "source_type": "review",
                "source_slice_id": None,
                "section_id": "ws:deployment",
                "work_item_id": 123,
                "note": "kept",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="recorded_at must be a string"):
        read_workstream_association_records("acme", programs_root=tmp_path)


def test_read_workstream_association_records_rejects_non_string_source_slice_id(tmp_path: Path) -> None:
    ledger_path = tmp_path / "acme" / "journal" / "workstream_associations.jsonl"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        json.dumps(
            {
                "recorded_at": "2026-06-06T12:00:00+00:00",
                "edition": "acme_weekly",
                "issue_number": 1,
                "workstream_id": "deployment",
                "source_type": "review",
                "source_slice_id": 789,
                "section_id": "ws:deployment",
                "work_item_id": 123,
                "note": "kept",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="source_slice_id must be a string"):
        read_workstream_association_records("acme", programs_root=tmp_path)


def test_read_workstream_association_records_rejects_non_object_rows(tmp_path: Path) -> None:
    ledger_path = tmp_path / "acme" / "journal" / "workstream_associations.jsonl"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")

    with pytest.raises(TypeError, match="workstream association rows must be JSON objects"):
        read_workstream_association_records("acme", programs_root=tmp_path)


# ---------------------------------------------------------------------------
# spec §22 Step 6: workstream.association fact-store dual-write (rev. 321)
# ---------------------------------------------------------------------------


def _build_association_record(
    *,
    recorded_at: datetime,
    edition: str = "acme_weekly",
    issue_number: int = 78,
    workstream_id: str = "deployment",
    source_type: str = "review",
    work_item_id: int | None = 12345,
) -> WorkstreamAssociationRecord:
    return WorkstreamAssociationRecord(
        recorded_at=recorded_at,
        edition=edition,
        issue_number=issue_number,
        workstream_id=workstream_id,
        source_type=source_type,
        source_slice_id=None,
        section_id="ws:deployment",
        work_item_id=work_item_id,
        note="kept",
    )


def test_append_workstream_association_records_also_writes_fact_to_fact_store(tmp_path: Path) -> None:
    """Spec §22 Step 6: ``append_workstream_association_records`` writes the
    JSONL row AND a ``workstream.association`` fact revision in the same call.
    """
    recorded_at = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    programs_root = tmp_path / "programs"
    record = _build_association_record(recorded_at=recorded_at)

    append_workstream_association_records("acme", (record,), programs_root=programs_root)

    # JSONL row landed.
    ledger_path = programs_root / "acme" / "journal" / "workstream_associations.jsonl"
    assert ledger_path.exists()
    rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line]
    assert len(rows) == 1
    assert rows[0]["workstream_id"] == "deployment"

    # Fact-store revision landed.  The writer resolves ``db_root`` from
    # ``programs_root.parent`` so we use the same value here.
    store = ProgramFactStore("acme", db_root=programs_root.parent)
    store.initialize()
    snapshot = store.snapshot()
    assoc_facts = tuple(fact for fact in snapshot.facts if fact.fact_type == "workstream.association")
    assert len(assoc_facts) == 1
    fact = assoc_facts[0]
    assert fact.payload["edition"] == "acme_weekly"
    assert fact.payload["issue_number"] == 78
    assert fact.payload["workstream_id"] == "deployment"
    assert fact.payload["source_type"] == "review"
    assert fact.payload["work_item_id"] == 12345
    assert fact.entity_refs == (
        "WORKSTREAM_ASSOC:acme_weekly:78:deployment:12345:review",
    )


def test_append_workstream_association_records_is_idempotent_within_a_single_call(tmp_path: Path) -> None:
    """Spec §22 Step 6: re-running append with the same record dedupes to a
    single fact revision in the fact store (the natural key is stable).
    """
    recorded_at = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    programs_root = tmp_path / "programs"
    record = _build_association_record(recorded_at=recorded_at)

    append_workstream_association_records("acme", (record,), programs_root=programs_root)
    append_workstream_association_records("acme", (record,), programs_root=programs_root)

    # JSONL has 2 rows (append-only contract).
    ledger_path = programs_root / "acme" / "journal" / "workstream_associations.jsonl"
    rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines() if line]
    assert len(rows) == 2

    # Fact store has exactly 1 active revision (the second call's fact write
    # returned ``noop`` because the natural key already exists with an
    # equivalent payload).
    store = ProgramFactStore("acme", db_root=programs_root.parent)
    snapshot = store.snapshot()
    assoc_facts = tuple(fact for fact in snapshot.facts if fact.fact_type == "workstream.association")
    assert len(assoc_facts) == 1


def test_append_workstream_association_records_with_distinct_recorded_at_writes_distinct_facts(tmp_path: Path) -> None:
    """Spec §22 Step 6: re-running confirm with a fresh ``recorded_at``
    writes a new fact revision (the natural key includes ``recorded_at`` so
    the dedupe contract does not collapse them — matching the JSONL
    append-only contract).
    """
    programs_root = tmp_path / "programs"
    first = _build_association_record(
        recorded_at=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc),
    )
    second = _build_association_record(
        recorded_at=datetime(2026, 6, 8, 13, 0, tzinfo=timezone.utc),
    )

    append_workstream_association_records("acme", (first, second), programs_root=programs_root)

    store = ProgramFactStore("acme", db_root=programs_root.parent)
    snapshot = store.snapshot()
    assoc_facts = tuple(fact for fact in snapshot.facts if fact.fact_type == "workstream.association")
    assert len(assoc_facts) == 2
    assert assoc_facts[0].payload["recorded_at"] != assoc_facts[1].payload["recorded_at"]


def test_project_workstream_associations_round_trips_record_via_fact_store(tmp_path: Path) -> None:
    """Spec §22 Step 6: ``project_workstream_associations`` returns the
    original ``WorkstreamAssociationRecord`` instances with full field
    fidelity (round-trip via the fact store).  When the program is in
    ``legacy`` SoR mode the shim also projects the JSONL row, so the
    snapshot can contain both the live fact and the shim projection —
    parity-check downstream dedupes them by canonical form.  This test
    asserts the original record is *among* the projected records, not
    that the snapshot has exactly one (the legacy+live double-projection
    is correct for the legacy SoR mode and disappears in ``primary``).
    """
    recorded_at = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)
    programs_root = tmp_path / "programs"
    record = _build_association_record(recorded_at=recorded_at)

    append_workstream_association_records("acme", (record,), programs_root=programs_root)

    snapshot = load_program_facts(
        "acme",
        programs_root=programs_root,
        db_root=programs_root.parent,
        fact_types=("workstream.association",),
    )
    projected = project_workstream_associations(snapshot)
    assert projected  # at least one projection of the original record exists
    # The original record is present in the projection (regardless of
    # whether it came from the live fact, the shim, or both).
    for round_tripped in projected:
        if (
            round_tripped.edition == record.edition
            and round_tripped.issue_number == record.issue_number
            and round_tripped.workstream_id == record.workstream_id
            and round_tripped.source_type == record.source_type
            and round_tripped.work_item_id == record.work_item_id
        ):
            assert round_tripped.section_id == record.section_id
            assert round_tripped.note == record.note
            # ``recorded_at`` survives round-trip with the timezone preserved.
            assert round_tripped.recorded_at == record.recorded_at
            return
    pytest.fail("Original record was not found in the projected snapshot")