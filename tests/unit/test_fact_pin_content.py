from dataclasses import replace
from pathlib import Path

import pytest

from src.core.exceptions import ConfigError
from src.core.program_fact_store import ProgramFactStore
from src.core.quality_gates.fact_drift import evaluate_program_fact_drift_from_draft
from tests.unit.test_fact_pin_membership import NOW, _fact


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("payload_json", '{"status":"closed"}'),
        ("entity_refs_json", '["WI:2"]'),
        ("source_hash", "changed-source"),
        ("confidence", "low"),
        ("privacy_classification", "confidential"),
        ("projection_history_json", '[{"correction":"changed"}]'),
        ("valid_until", "2099-01-01T00:00:00+00:00"),
        ("scope", "changed-scope"),
    ],
)
def test_same_revision_content_change_blocks_actual_drift_gate(
    tmp_path: Path, column: str, value: str,
) -> None:
    store = ProgramFactStore("fixture", db_root=tmp_path)
    revision = store.append_fact(_fact("WI:1"), recorded_at=NOW).revision
    pin = store.pin_snapshot()
    with store._connect() as connection:
        connection.execute(
            f"UPDATE program_fact_revisions SET {column} = ? WHERE revision_id = ?",
            (value, revision.revision_id),
        )

    with pytest.raises(ConfigError, match="content"):
        ProgramFactStore("fixture", db_root=tmp_path).detect_drift(pin.snapshot_id)
    with pytest.raises(ConfigError, match="content"):
        evaluate_program_fact_drift_from_draft(
            draft_state={"program_fact_snapshot": {"snapshot_id": pin.snapshot_id}},
            program_id="fixture", db_root=tmp_path,
        )


@pytest.mark.parametrize("digest", [None, "", "z" * 64, "a" * 63, "0" * 64, b"binary"])
def test_missing_or_damaged_content_digest_is_not_legacy_success(
    tmp_path: Path, digest: str | bytes | None,
) -> None:
    store = ProgramFactStore("fixture", db_root=tmp_path)
    pin = store.pin_snapshot()
    with store._connect() as connection:
        connection.execute(
            "UPDATE program_fact_snapshot_pins SET content_sha256 = ? WHERE snapshot_id = ?",
            (digest, pin.snapshot_id),
        )

    with pytest.raises(ConfigError, match="content"):
        store.detect_drift(pin.snapshot_id)


def test_content_pin_survives_reopen_and_json_key_order(tmp_path: Path) -> None:
    store = ProgramFactStore("fixture", db_root=tmp_path)
    revision = store.append_fact(
        replace(_fact("WI:1"), payload={"a": 1, "b": 2}), recorded_at=NOW,
    ).revision
    pin = store.pin_snapshot(metadata={"edition": "fixture"})
    with store._connect() as connection:
        connection.execute(
            "UPDATE program_fact_revisions SET payload_json = ? WHERE revision_id = ?",
            ('{"b":2,"a":1}', revision.revision_id),
        )

    reopened = ProgramFactStore("fixture", db_root=tmp_path)
    loaded = reopened.load_snapshot_pin(pin.snapshot_id)
    assert loaded is not None
    assert loaded.membership_version == 2
    assert isinstance(loaded.content_sha256, str)
    assert len(loaded.content_sha256) == 64
    assert loaded.metadata == {"edition": "fixture"}
    assert reopened.detect_drift(pin.snapshot_id) == ()
    assert reopened.pin_snapshot().content_sha256 == loaded.content_sha256


def test_v1_schema_upgrade_keeps_existing_membership_pin(tmp_path: Path) -> None:
    store = ProgramFactStore("fixture", db_root=tmp_path)
    store.append_fact(_fact("WI:1"), recorded_at=NOW)
    pin = store.pin_snapshot()
    with store._connect() as connection:
        connection.execute(
            "UPDATE program_fact_snapshot_pins SET membership_version = 1 WHERE snapshot_id = ?",
            (pin.snapshot_id,),
        )
        connection.execute("ALTER TABLE program_fact_snapshot_pins DROP COLUMN content_sha256")

    reopened = ProgramFactStore("fixture", db_root=tmp_path)
    loaded = reopened.load_snapshot_pin(pin.snapshot_id)
    assert loaded is not None
    assert loaded.membership_version == 1
    assert loaded.content_sha256 is None
    assert reopened.detect_drift(pin.snapshot_id) == ()
    assert reopened.pin_snapshot().membership_version == 2
