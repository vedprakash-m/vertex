"""ME-01: Provenance recording wired into gather WorkIQ stage."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
import pytest

from src.core.models_v2 import Confidence, Signal


def _make_signal(
    lane_id: str,
    source_type: str = "email",
    message_id: str = "msg-001",
    text: str = "Subject: Test",
) -> Signal:
    return Signal(
        id=f"sig-{lane_id}-{message_id}",
        timestamp=datetime(2026, 6, 17, 18, 0, 0, tzinfo=timezone.utc),
        source=f"workiq/{source_type}",
        program_id="acme",
        workstream_id=lane_id,
        entity_refs=(),
        text=text,
        raw_ref=f"workiq:{source_type}:{message_id}",
        confidence=Confidence.MEDIUM,
        metadata={"source_type": source_type, "message_id": message_id},
    )


def test_provenance_written_per_lane(tmp_path: Path) -> None:
    """One provenance record per unique workstream_id."""
    from src.commands.gather import _record_workiq_provenance
    signals = (
        _make_signal("acme.networking", "email", "msg-1"),
        _make_signal("acme.networking", "email", "msg-2"),
        _make_signal("acme.deployment_velocity", "transcript", "msg-3"),
    )
    run_at = datetime(2026, 6, 17, 18, 0, 0, tzinfo=timezone.utc)
    _record_workiq_provenance(
        workiq_signals=signals,
        program_id="acme",
        programs_root=tmp_path,
        run_at=run_at,
    )
    prov_file = tmp_path / "acme" / "journal" / "evidence_provenance.jsonl"
    assert prov_file.exists()
    records = [json.loads(line) for line in prov_file.read_text(encoding="utf-8").splitlines()]
    lane_ids = {r["lane_id"] for r in records}
    assert lane_ids == {"acme.networking", "acme.deployment_velocity"}


def test_provenance_skipped_for_no_workstream(tmp_path: Path) -> None:
    """Signals with workstream_id=None are skipped."""
    from src.commands.gather import _record_workiq_provenance
    no_ws = Signal(
        id="sig-nows",
        timestamp=datetime(2026, 6, 17, 18, 0, 0, tzinfo=timezone.utc),
        source="workiq/email",
        program_id="acme",
        workstream_id=None,
        entity_refs=(),
        text="unrouted signal",
        raw_ref=None,
        confidence=Confidence.MEDIUM,
        metadata=None,
    )
    _record_workiq_provenance(
        workiq_signals=(no_ws,),
        program_id="acme",
        programs_root=tmp_path,
        run_at=datetime(2026, 6, 17, 18, 0, 0, tzinfo=timezone.utc),
    )
    prov_file = tmp_path / "acme" / "journal" / "evidence_provenance.jsonl"
    assert not prov_file.exists()


def test_provenance_failure_does_not_propagate(tmp_path: Path) -> None:
    """Exception in record_provenance must not raise."""
    from src.commands.gather import _record_workiq_provenance
    signal = _make_signal("acme.networking")
    with patch("src.core.evidence_provenance.record_provenance", side_effect=OSError("disk full")):
        # Must not raise
        _record_workiq_provenance(
            workiq_signals=(signal,),
            program_id="acme",
            programs_root=tmp_path,
            run_at=datetime(2026, 6, 17, 18, 0, 0, tzinfo=timezone.utc),
        )


def test_dominant_source_type_selected(tmp_path: Path) -> None:
    """Lane with 2 email + 1 transcript signals uses 'email' as dominant source."""
    from src.commands.gather import _record_workiq_provenance
    signals = (
        _make_signal("acme.networking", "email", "msg-1"),
        _make_signal("acme.networking", "email", "msg-2"),
        _make_signal("acme.networking", "transcript", "msg-3"),
    )
    _record_workiq_provenance(
        workiq_signals=signals,
        program_id="acme",
        programs_root=tmp_path,
        run_at=datetime(2026, 6, 17, 18, 0, 0, tzinfo=timezone.utc),
    )
    prov_file = tmp_path / "acme" / "journal" / "evidence_provenance.jsonl"
    records = [json.loads(line) for line in prov_file.read_text(encoding="utf-8").splitlines()]
    assert records[0]["source_type"] == "email"
