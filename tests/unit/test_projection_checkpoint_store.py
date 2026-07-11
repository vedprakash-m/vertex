from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core.ledger.projection_checkpoint_store import (
    list_checkpoints,
    load_checkpoint,
    record_checkpoint,
)


def test_load_checkpoint_missing_returns_none(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    assert load_checkpoint("acme", "fact_bridge", programs_root=programs_root) is None


def test_record_and_load_round_trip(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    watermark_at = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
    recorded = record_checkpoint(
        "acme",
        "fact_bridge",
        watermark_event_id="01HZXYZ",
        watermark_recorded_at=watermark_at,
        projector_version="v3",
        policy_version="p2",
        checksum="sha256:abc",
        programs_root=programs_root,
    )
    loaded = load_checkpoint("acme", "fact_bridge", programs_root=programs_root)

    assert loaded is not None
    assert loaded.watermark_event_id == "01HZXYZ"
    assert loaded.watermark_recorded_at == watermark_at
    assert loaded.projector_version == "v3"
    assert loaded.policy_version == "p2"
    assert loaded.checksum == "sha256:abc"
    assert loaded == recorded


def test_record_overwrites_existing_checkpoint(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    record_checkpoint(
        "acme",
        "fact_bridge",
        watermark_event_id="01HZ_FIRST",
        watermark_recorded_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        projector_version="v1",
        policy_version="p1",
        programs_root=programs_root,
    )
    record_checkpoint(
        "acme",
        "fact_bridge",
        watermark_event_id="01HZ_SECOND",
        watermark_recorded_at=datetime(2026, 7, 9, tzinfo=timezone.utc),
        projector_version="v2",
        policy_version="p1",
        programs_root=programs_root,
    )
    loaded = load_checkpoint("acme", "fact_bridge", programs_root=programs_root)
    assert loaded is not None
    assert loaded.watermark_event_id == "01HZ_SECOND"
    assert loaded.projector_version == "v2"


def test_checksum_defaults_to_none(tmp_path: Path) -> None:
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
    loaded = load_checkpoint("acme", "fact_bridge", programs_root=programs_root)
    assert loaded is not None
    assert loaded.checksum is None


def test_list_checkpoints_returns_all_projections_sorted(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    for name in ("rev_candidates", "fact_bridge", "annotations"):
        record_checkpoint(
            "acme",
            name,
            watermark_event_id="01HZ",
            watermark_recorded_at=datetime(2026, 7, 9, tzinfo=timezone.utc),
            projector_version="v1",
            policy_version="p1",
            programs_root=programs_root,
        )
    names = [c.projection_name for c in list_checkpoints("acme", programs_root=programs_root)]
    assert names == ["annotations", "fact_bridge", "rev_candidates"]


def test_list_checkpoints_empty_when_no_db(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    assert list_checkpoints("acme", programs_root=programs_root) == ()


def test_checkpoints_are_isolated_per_program(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    record_checkpoint(
        "acme",
        "fact_bridge",
        watermark_event_id="01HZ_ACME",
        watermark_recorded_at=datetime(2026, 7, 9, tzinfo=timezone.utc),
        projector_version="v1",
        policy_version="p1",
        programs_root=programs_root,
    )
    assert load_checkpoint("nova", "fact_bridge", programs_root=programs_root) is None
