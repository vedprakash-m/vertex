"""Tests for Phase 4: EvidenceCorrectionPattern (BL-27) and evidence_provenance (BL-40)."""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from src.ai.edit_learner import EvidenceCorrectionPattern, _EVIDENCE_CORRECTIONS_FILENAME
from src.core.evidence_provenance import (
    EvidenceProvenanceRecord,
    make_provenance_record,
    record_provenance,
)


_NOW = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)


# ── BL-27: EvidenceCorrectionPattern ─────────────────────────────────────────

def test_evidence_correction_pattern_is_frozen_dataclass():
    pattern = EvidenceCorrectionPattern(
        program_id="acme",
        lane_id="acme.networking",
        corrected_at=_NOW,
        workiq_latest_before=None,
        workiq_latest_after="2026-06-17: NMAgent IcM 771996570 ACTIVE.",
        risk_level_before=None,
        risk_level_after="high",
        ado_ids_added=(),
        icm_ids_added=("771996570",),
        source_hint="cowork_manual",
        operator="maintainer",
    )
    assert pattern.lane_id == "acme.networking"
    assert pattern.icm_ids_added == ("771996570",)
    assert pattern.source_hint == "cowork_manual"


def test_evidence_correction_pattern_frozen():
    pattern = EvidenceCorrectionPattern(
        program_id="acme",
        lane_id="x",
        corrected_at=_NOW,
        workiq_latest_before=None,
        workiq_latest_after="2026-06-17: test",
        risk_level_before=None,
        risk_level_after="low",
        ado_ids_added=("12345678",),
        icm_ids_added=(),
        source_hint="local_kb",
        operator="auto",
    )
    import pytest
    with pytest.raises((AttributeError, TypeError)):
        pattern.lane_id = "modified"  # type: ignore[misc]


def test_evidence_corrections_filename_constant():
    assert _EVIDENCE_CORRECTIONS_FILENAME == "evidence_corrections.jsonl"


# ── BL-40: EvidenceProvenanceRecord ──────────────────────────────────────────

def test_make_provenance_record_fills_run_at():
    record = make_provenance_record(
        lane_id="acme.networking",
        source_type="workiq_email",
        source_id="msg:ABC123",
        source_date="2026-06-17",
        confidence=0.85,
        fields_populated=("risk_level", "blocking_items"),
        operator="auto",
        run_at=_NOW,
    )
    assert record.lane_id == "acme.networking"
    assert record.run_at == "2026-06-17T12:00:00+00:00"
    assert record.confidence == 0.85
    assert "risk_level" in record.fields_populated


def test_record_provenance_writes_jsonl(tmp_path):
    programs_root = tmp_path / "programs"
    programs_root.mkdir()
    record = make_provenance_record(
        lane_id="acme.deployment",
        source_type="workiq_transcript",
        source_id="meet:XYZ",
        source_date="2026-06-16",
        confidence=0.72,
        fields_populated=("risk_level",),
        run_at=_NOW,
    )
    record_provenance(record, program_id="acme", programs_root=programs_root)
    journal_path = programs_root / "acme" / "journal" / "evidence_provenance.jsonl"
    assert journal_path.exists()
    lines = journal_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["lane_id"] == "acme.deployment"
    assert data["source_type"] == "workiq_transcript"
    assert data["confidence"] == 0.72


def test_record_provenance_appends_multiple(tmp_path):
    programs_root = tmp_path / "programs"
    programs_root.mkdir()
    for lane in ("acme.networking", "acme.performance"):
        rec = make_provenance_record(
            lane_id=lane,
            source_type="workiq_email",
            source_id=None,
            source_date=None,
            confidence=0.5,
            fields_populated=(),
            run_at=_NOW,
        )
        record_provenance(rec, program_id="acme", programs_root=programs_root)
    journal_path = programs_root / "acme" / "journal" / "evidence_provenance.jsonl"
    lines = journal_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_record_provenance_creates_journal_dir(tmp_path):
    programs_root = tmp_path / "programs"
    programs_root.mkdir()
    rec = make_provenance_record(
        lane_id="x",
        source_type="manual",
        source_id=None,
        source_date=None,
        confidence=1.0,
        fields_populated=("risk_level",),
        run_at=_NOW,
    )
    record_provenance(rec, program_id="new_program", programs_root=programs_root)
    assert (programs_root / "new_program" / "journal").is_dir()
