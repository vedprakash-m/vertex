from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from src.core.ledger.event_log import (
    ConfidenceTier,
    EventEnvelope,
    TemporalConfidence,
    build_event_envelope,
    read_events,
    write_event,
    write_events_atomic,
)
from src.core.ledger.source_refs import OperatorAssertionRef


def _event():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=now,
        recorded_at=now,
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="operator",
        payload={"risk_id": "risk:synthetic", "title": "Synthetic risk", "severity": "high"},
        source_ref=OperatorAssertionRef(asserted_by="operator", asserted_at=now),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("program_id", ""), ("actor", ""), ("event_type", ""), ("dedupe_core_hash", 1)],
)
@pytest.mark.parametrize("batch", [False, True])
def test_invalid_envelope_is_rejected_before_fence_or_filesystem_writes(
    tmp_path: Path, field: str, value: object, batch: bool
) -> None:
    root = tmp_path / "programs"
    event = replace(_event(), **{field: value})

    with pytest.raises(ValueError, match=field):
        if batch:
            write_events_atomic((event,), programs_root=root)
        else:
            write_event(event, programs_root=root)

    assert not root.exists()


@pytest.mark.parametrize("corroborating", [False, True])
@pytest.mark.parametrize("batch", [False, True])
def test_invalid_source_reference_is_rejected_before_fence_or_filesystem_writes(
    tmp_path: Path, corroborating: bool, batch: bool
) -> None:
    root = tmp_path / "programs"
    invalid_ref = OperatorAssertionRef(asserted_by="", asserted_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    event = (
        replace(_event(), corroborating_refs=(invalid_ref,))
        if corroborating else replace(_event(), source_ref=invalid_ref)
    )

    with pytest.raises(ValueError, match="asserted_by"):
        if batch:
            write_events_atomic((event,), programs_root=root)
        else:
            write_event(event, programs_root=root)

    assert not root.exists()


def test_invalid_later_batch_member_preserves_all_existing_files(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    previous = write_event(_event(), programs_root=root)
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    with pytest.raises(ValueError, match="actor"):
        write_events_atomic((_event(), replace(_event(), actor="")), programs_root=root)

    after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert after == before
    assert read_events("acme", programs_root=root, repair_corruption=False) == (previous.envelope,)


def test_invalid_envelope_does_not_repair_or_change_retained_damaged_history(tmp_path: Path) -> None:
    root = tmp_path / "programs"
    previous = write_event(_event(), programs_root=root)
    previous.path.write_bytes(previous.path.read_bytes() + b'{"damaged"\n')
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}

    with pytest.raises(ValueError, match="actor"):
        write_event(replace(_event(), actor=""), programs_root=root)

    after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert after == before


@pytest.mark.parametrize("batch", [False, True])
def test_writer_rejects_an_invalid_final_predecessor_without_appending(
    tmp_path: Path, batch: bool
) -> None:
    root = tmp_path / "programs"
    previous = write_event(_event(), programs_root=root)
    tail_path = previous.path.parent / "_tail.json"
    tail = json.loads(tail_path.read_text(encoding="utf-8"))
    tail["last_event_hash"] = ""
    tail_path.write_text(json.dumps(tail), encoding="utf-8")
    before = previous.path.read_bytes()

    with pytest.raises(ValueError, match="prev_event_hash"):
        if batch:
            write_events_atomic((_event(),), programs_root=root)
        else:
            write_event(_event(), programs_root=root)

    assert previous.path.read_bytes() == before


@pytest.mark.parametrize("batch", [False, True])
def test_valid_builder_envelopes_remain_replayable(tmp_path: Path, batch: bool) -> None:
    root = tmp_path / "programs"
    event = _event()
    assert event.prev_event_hash == ""
    if batch:
        persisted = write_events_atomic((event, event), programs_root=root).envelopes
    else:
        persisted = (write_event(event, programs_root=root).envelope,)

    assert read_events("acme", programs_root=root, repair_corruption=False) == persisted
    assert all(EventEnvelope.from_dict(record.to_dict()) == record for record in persisted)
