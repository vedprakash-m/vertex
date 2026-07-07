"""ME-04: EvidenceCorrectionPattern write/read path."""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import pytest


def _make_pattern(lane_id: str = "acme.networking") -> "EvidenceCorrectionPattern":
    from src.ai.edit_learner import EvidenceCorrectionPattern
    return EvidenceCorrectionPattern(
        program_id="acme",
        lane_id=lane_id,
        corrected_at=datetime(2026, 6, 17, 18, 0, 0, tzinfo=timezone.utc),
        workiq_latest_before="2026-06-10: Low.",
        workiq_latest_after="2026-06-17: Networking parity NOT achieved. IcM 771996570 ACTIVE.",
        risk_level_before="LOW",
        risk_level_after="HIGH",
        ado_ids_added=(),
        icm_ids_added=("771996570",),
        source_hint="cowork_manual",
        operator="maintainer",
    )


def test_round_trip(tmp_path: Path) -> None:
    """append + load returns identical pattern."""
    from src.ai.edit_learner import append_evidence_correction, load_evidence_corrections
    pattern = _make_pattern()
    append_evidence_correction(pattern, programs_root=tmp_path)
    loaded = load_evidence_corrections("acme", programs_root=tmp_path)
    assert len(loaded) == 1
    assert loaded[0].lane_id == "acme.networking"
    assert loaded[0].risk_level_after == "HIGH"
    assert "771996570" in loaded[0].icm_ids_added


def test_filter_by_lane(tmp_path: Path) -> None:
    """lane_id filter returns only matching patterns."""
    from src.ai.edit_learner import append_evidence_correction, load_evidence_corrections
    append_evidence_correction(_make_pattern("acme.networking"), programs_root=tmp_path)
    append_evidence_correction(_make_pattern("acme.deployment_velocity"), programs_root=tmp_path)
    networking = load_evidence_corrections("acme", programs_root=tmp_path, lane_id="acme.networking")
    assert len(networking) == 1
    assert networking[0].lane_id == "acme.networking"


def test_empty_file_returns_empty_list(tmp_path: Path) -> None:
    from src.ai.edit_learner import load_evidence_corrections
    result = load_evidence_corrections("acme", programs_root=tmp_path)
    assert result == []


def test_corrupt_line_skipped(tmp_path: Path) -> None:
    """Corrupt JSONL line is skipped gracefully."""
    from src.ai.edit_learner import append_evidence_correction, load_evidence_corrections
    append_evidence_correction(_make_pattern(), programs_root=tmp_path)
    corrections_path = tmp_path / "acme" / "journal" / "evidence_corrections.jsonl"
    with open(corrections_path, "a", encoding="utf-8") as f:
        f.write("NOT_VALID_JSON\n")
    loaded = load_evidence_corrections("acme", programs_root=tmp_path)
    assert len(loaded) == 1  # corrupt line skipped, valid line returned
