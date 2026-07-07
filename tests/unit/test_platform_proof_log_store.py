from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from src.core.exceptions import ConfigError
from src.core.platform_proof_log_store import load_platform_proof_records, record_platform_proof


def test_record_and_load_platform_proof_round_trip(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    recorded_at = datetime(2026, 6, 7, 15, 0, tzinfo=timezone.utc)

    record_platform_proof(
        program_id="acme",
        proof_id="p4a_clean_machine",
        status="passed",
        recorded_at=recorded_at,
        recorded_by="operator",
        edition="acme_weekly",
        notes="Validated on clean workspace.",
        elapsed_minutes=12.5,
        no_code_changes=False,
        confirm_exit_code=0,
        programs_root=programs_root,
    )

    loaded = load_platform_proof_records("acme", programs_root=programs_root)

    assert len(loaded) == 1
    assert loaded[0].proof_id == "p4a_clean_machine"
    assert loaded[0].recorded_at == recorded_at
    assert loaded[0].confirm_exit_code == 0


def test_load_platform_proof_records_rejects_non_string_status(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = programs_root / "acme" / "platform_proof_log.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "proofs": [
                    {
                        "proof_id": "proof-1",
                        "archetype": None,
                        "status": 1,
                        "recorded_at": "2026-06-07T15:00:00+00:00",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="platform proof log status must be a string"):
        load_platform_proof_records("acme", programs_root=programs_root)


def test_load_platform_proof_records_rejects_non_string_recorded_by(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = programs_root / "acme" / "platform_proof_log.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "proofs": [
                    {
                        "proof_id": "p4a_clean_machine",
                        "status": "passed",
                        "recorded_at": "2026-06-07T15:00:00+00:00",
                        "recorded_by": 123,
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="platform proof log recorded_by must be a string"):
        load_platform_proof_records("acme", programs_root=programs_root)


def test_load_platform_proof_records_rejects_numeric_string_confirm_exit_code(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = programs_root / "acme" / "platform_proof_log.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "proofs": [
                    {
                        "proof_id": "p4a_clean_machine",
                        "status": "passed",
                        "recorded_at": "2026-06-07T15:00:00+00:00",
                        "confirm_exit_code": "0",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="platform proof log confirm_exit_code must be an integer"):
        load_platform_proof_records("acme", programs_root=programs_root)


def test_record_platform_proof_rejects_naive_recorded_at(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="recorded_at must include timezone information"):
        record_platform_proof(
            program_id="acme",
            proof_id="p4a_clean_machine",
            status="passed",
            recorded_at=datetime(2026, 6, 7, 15, 0),
            recorded_by="operator",
            programs_root=tmp_path / "programs",
        )


def test_load_platform_proof_records_rejects_naive_recorded_at(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = programs_root / "acme" / "platform_proof_log.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "proofs": [
                    {
                        "proof_id": "p4a_clean_machine",
                        "status": "passed",
                        "recorded_at": "2026-06-07T15:00:00",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="platform proof log recorded_at must include timezone information"):
        load_platform_proof_records("acme", programs_root=programs_root)


def test_record_platform_proof_rejects_unknown_proof_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown proof_id"):
        record_platform_proof(
            program_id="acme",
            proof_id="proof-1",
            status="passed",
            recorded_at=datetime(2026, 6, 7, 15, 0, tzinfo=timezone.utc),
            recorded_by="operator",
            programs_root=tmp_path / "programs",
        )


def test_record_platform_proof_requires_matching_archetype_for_p6b(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires archetype 'ADO-only'"):
        record_platform_proof(
            program_id="acme",
            proof_id="p6b_ado_only",
            status="passed",
            recorded_at=datetime(2026, 6, 7, 15, 0, tzinfo=timezone.utc),
            recorded_by="operator",
            programs_root=tmp_path / "programs",
        )


def test_load_platform_proof_records_rejects_unknown_proof_id(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    path = programs_root / "acme" / "platform_proof_log.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "proofs": [
                    {
                        "proof_id": "proof-1",
                        "status": "passed",
                        "recorded_at": "2026-06-07T15:00:00+00:00",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="Unknown proof_id"):
        load_platform_proof_records("acme", programs_root=programs_root)
