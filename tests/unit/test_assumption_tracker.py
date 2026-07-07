from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from src.core.assumption_tracker import check_validation_due, load_assumptions, save_assumptions
from src.core.models_v2 import Assumption, AssumptionStatus


def test_save_and_load_assumptions_round_trip(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    entry = Assumption(
        id="assumption-1",
        program_id="demo",
        text="Kusto team ships schema by Q3.",
        validation_method="Review the schema rollout notes.",
        validation_due=date(2026, 5, 15),
        status=AssumptionStatus.CONFIRMED,
        linked_risk_id=None,
        linked_milestone_id="m1",
        owner_alias="demo",
        identified_date=date(2026, 5, 1),
        entity_refs=("WI:1001",),
        resolved_date=date(2026, 5, 12),
    )

    save_assumptions("demo", (entry,), programs_root=programs_root)

    assert load_assumptions("demo", programs_root=programs_root) == (entry,)


def test_load_assumptions_accepts_empty_store(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    save_assumptions("demo", (), programs_root=programs_root)

    assert load_assumptions("demo", programs_root=programs_root) == ()


def test_load_assumptions_defaults_missing_resolved_date_to_none(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    assumptions_path = programs_root / "demo" / "assumptions.yaml"
    assumptions_path.parent.mkdir(parents=True, exist_ok=True)
    assumptions_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "assumptions": [
                    {
                        "id": "assumption-1",
                        "program_id": "demo",
                        "text": "Legacy assumption entry.",
                        "validation_method": None,
                        "validation_due": None,
                        "status": "unvalidated",
                        "linked_risk_id": None,
                        "linked_milestone_id": None,
                        "owner_alias": None,
                        "identified_date": "2026-05-01",
                        "entity_refs": [],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    loaded = load_assumptions("demo", programs_root=programs_root)

    assert len(loaded) == 1
    assert loaded[0].resolved_date is None


def test_check_validation_due_returns_overdue_unvalidated_assumptions() -> None:
    overdue = Assumption(
        id="assumption-1",
        program_id="demo",
        text="Kusto team ships schema by Q3.",
        validation_method=None,
        validation_due=date(2026, 5, 10),
        status=AssumptionStatus.UNVALIDATED,
        linked_risk_id=None,
        linked_milestone_id=None,
        owner_alias=None,
        identified_date=date(2026, 5, 1),
        entity_refs=(),
    )
    confirmed = Assumption(
        id="assumption-2",
        program_id="demo",
        text="Deployment freeze lifts in May.",
        validation_method=None,
        validation_due=date(2026, 5, 10),
        status=AssumptionStatus.CONFIRMED,
        linked_risk_id=None,
        linked_milestone_id=None,
        owner_alias=None,
        identified_date=date(2026, 5, 1),
        entity_refs=(),
    )

    assert check_validation_due((overdue, confirmed), date(2026, 5, 20)) == (overdue,)


def test_load_assumptions_rejects_non_string_status(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    assumptions_path = programs_root / "demo" / "assumptions.yaml"
    assumptions_path.parent.mkdir(parents=True, exist_ok=True)
    assumptions_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "assumptions": [
                    {
                        "id": "assumption-1",
                        "program_id": "demo",
                        "text": "Legacy assumption entry.",
                        "validation_method": None,
                        "validation_due": None,
                        "status": 1,
                        "linked_risk_id": None,
                        "linked_milestone_id": None,
                        "owner_alias": None,
                        "identified_date": "2026-05-01",
                        "entity_refs": [],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    try:
        load_assumptions("demo", programs_root=programs_root)
        raise AssertionError("Expected ConfigError")
    except Exception as error:
        assert "status must be a string" in str(error)


def test_load_assumptions_rejects_non_string_entity_ref(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    assumptions_path = programs_root / "demo" / "assumptions.yaml"
    assumptions_path.parent.mkdir(parents=True, exist_ok=True)
    assumptions_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "assumptions": [
                    {
                        "id": "assumption-1",
                        "program_id": "demo",
                        "text": "Legacy assumption entry.",
                        "validation_method": None,
                        "validation_due": None,
                        "status": "unvalidated",
                        "linked_risk_id": None,
                        "linked_milestone_id": None,
                        "owner_alias": None,
                        "identified_date": "2026-05-01",
                        "entity_refs": [1001],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    try:
        load_assumptions("demo", programs_root=programs_root)
        raise AssertionError("Expected ConfigError")
    except Exception as error:
        assert "entity_refs must contain strings only" in str(error)


def test_load_assumptions_rejects_non_string_owner_alias(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    assumptions_path = programs_root / "demo" / "assumptions.yaml"
    assumptions_path.parent.mkdir(parents=True, exist_ok=True)
    assumptions_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "assumptions": [
                    {
                        "id": "assumption-1",
                        "program_id": "demo",
                        "text": "Legacy assumption entry.",
                        "validation_method": None,
                        "validation_due": None,
                        "status": "unvalidated",
                        "linked_risk_id": None,
                        "linked_milestone_id": None,
                        "owner_alias": 123,
                        "identified_date": "2026-05-01",
                        "entity_refs": [],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    try:
        load_assumptions("demo", programs_root=programs_root)
        raise AssertionError("Expected ConfigError")
    except Exception as error:
        assert "owner_alias must be a string" in str(error)