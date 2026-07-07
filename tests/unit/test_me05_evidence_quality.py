"""ME-05: Evidence quality JSONL and drift detection."""
from __future__ import annotations
from datetime import date, datetime, timezone
from pathlib import Path
import pytest


def _make_quality_record(lane_id: str, confidence: float, run_at: datetime) -> "EvidenceQualityRecord":
    from src.core.evidence_quality import EvidenceQualityRecord
    return EvidenceQualityRecord(
        run_at=run_at,
        lane_id=lane_id,
        confidence=confidence,
        etas_found=1 if confidence > 0 else 0,
        owners_found=1 if confidence > 0 else 0,
        blocking_found=0,
        body_text_chars=500,
        source_type="transcript",
        extractor="ContentExtractionAgent" if confidence > 0 else "placeholder",
    )


def test_round_trip(tmp_path: Path) -> None:
    from src.core.evidence_quality import record_evidence_quality, load_evidence_quality
    rec = _make_quality_record("acme.networking", 0.82, datetime(2026, 6, 17, tzinfo=timezone.utc))
    record_evidence_quality(rec, program_id="acme", programs_root=tmp_path)
    loaded = load_evidence_quality("acme", programs_root=tmp_path)
    assert len(loaded) == 1
    assert loaded[0].lane_id == "acme.networking"
    assert abs(loaded[0].confidence - 0.82) < 0.001


def test_drift_detected(tmp_path: Path) -> None:
    """Confidence drop >20% triggers CONF_DRIFT warn."""
    from src.core.evidence_quality import record_evidence_quality
    from src.commands.doctor_checks.evidence_checks import check_evidence_quality_drift

    # 4 "early" records at 0.80, 4 "recent" records at 0.50 → 37.5% drop → WARN
    for i, conf in enumerate([0.80, 0.80, 0.80, 0.80, 0.50, 0.50, 0.50, 0.50]):
        rec = _make_quality_record(
            "acme.schie_gaps",
            conf,
            datetime(2026, 6, i + 1, tzinfo=timezone.utc),
        )
        record_evidence_quality(rec, program_id="acme", programs_root=tmp_path)

    issues = check_evidence_quality_drift("acme", tmp_path, as_of=date(2026, 6, 30))
    assert any("[CONF_DRIFT]" in i.detail and "acme.schie_gaps" in i.detail for i in issues)


def test_all_zero_confidence_warns(tmp_path: Path) -> None:
    """All confidence=0.0 records → CONF_ZERO warn."""
    from src.core.evidence_quality import record_evidence_quality
    from src.commands.doctor_checks.evidence_checks import check_evidence_quality_drift

    for i in range(3):
        rec = _make_quality_record("acme.lso", 0.0, datetime(2026, 6, i + 1, tzinfo=timezone.utc))
        record_evidence_quality(rec, program_id="acme", programs_root=tmp_path)

    issues = check_evidence_quality_drift("acme", tmp_path, as_of=date(2026, 6, 30))
    assert any("[CONF_ZERO]" in i.detail and "acme.lso" in i.detail for i in issues)


def test_no_records_returns_empty(tmp_path: Path) -> None:
    from src.commands.doctor_checks.evidence_checks import check_evidence_quality_drift
    issues = check_evidence_quality_drift("acme", tmp_path, as_of=date(2026, 6, 30))
    assert issues == []


def test_load_filtered_by_lane(tmp_path: Path) -> None:
    from src.core.evidence_quality import record_evidence_quality, load_evidence_quality
    for lane in ["acme.networking", "acme.lso"]:
        rec = _make_quality_record(lane, 0.75, datetime(2026, 6, 17, tzinfo=timezone.utc))
        record_evidence_quality(rec, program_id="acme", programs_root=tmp_path)
    result = load_evidence_quality("acme", programs_root=tmp_path, lane_id="acme.networking")
    assert len(result) == 1
    assert result[0].lane_id == "acme.networking"
