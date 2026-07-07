from __future__ import annotations

import csv
from datetime import date, datetime, timezone
import json
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.core.assumption_tracker import load_assumptions, save_assumptions
from src.core.exceptions import ConfigError
from src.core.models_v2 import Assumption, AssumptionStatus
from src.core.risk_register_engine import load_risk_register


runner = CliRunner()


def test_assumptions_add_and_list_cli(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.assumptions.PROGRAMS_ROOT", programs_root)
    monkeypatch.setenv("USERNAME", "demo")

    add_result = runner.invoke(
        app,
        [
            "assumptions",
            "add",
            "--program",
            "demo",
            "--text",
            "Kusto team ships schema by Q3.",
            "--validation-due",
            "2026-05-20",
        ],
    )
    list_result = runner.invoke(app, ["assumptions", "list", "--program", "demo"])

    assert add_result.exit_code == 0
    assert "Added assumption" in add_result.stdout
    assert list_result.exit_code == 0
    assert "ASSUMPTIONS" in list_result.stdout
    assert "Kusto team ships schema by Q3." in list_result.stdout


def test_assumptions_list_cli_json(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.assumptions.PROGRAMS_ROOT", programs_root)
    save_assumptions(
        "demo",
        (
            Assumption(
                id="assumption-1",
                program_id="demo",
                text="Kusto team ships schema by Q3.",
                validation_method=None,
                validation_due=date(2026, 5, 20),
                status=AssumptionStatus.UNVALIDATED,
                linked_risk_id=None,
                linked_milestone_id=None,
                owner_alias="demo",
                identified_date=date(2026, 5, 1),
                entity_refs=("WI:1001",),
            ),
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(app, ["assumptions", "list", "--program", "demo", "--format", "json"])
    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["program_id"] == "demo"
    assert payload["assumptions"][0]["id"] == "assumption-1"
    assert payload["assumptions"][0]["validation_due"] == "2026-05-20"


def test_assumptions_list_ignores_unrelated_risk_loader_failures(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.assumptions.PROGRAMS_ROOT", programs_root)
    save_assumptions(
        "demo",
        (
            Assumption(
                id="assumption-1",
                program_id="demo",
                text="Kusto team ships schema by Q3.",
                validation_method=None,
                validation_due=date(2026, 5, 20),
                status=AssumptionStatus.UNVALIDATED,
                linked_risk_id=None,
                linked_milestone_id=None,
                owner_alias="demo",
                identified_date=date(2026, 5, 1),
                entity_refs=("WI:1001",),
            ),
        ),
        programs_root=programs_root,
    )

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise ConfigError("risk loader should not be used")

    monkeypatch.setattr("src.core.program_fact_store.load_risk_register", _boom)

    result = runner.invoke(app, ["assumptions", "list", "--program", "demo"])

    assert result.exit_code == 0
    assert "Kusto team ships schema by Q3." in result.stdout


def test_assumptions_list_cli_csv(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.assumptions.PROGRAMS_ROOT", programs_root)
    save_assumptions(
        "demo",
        (
            Assumption(
                id="assumption-1",
                program_id="demo",
                text="Kusto team ships schema by Q3.",
                validation_method="Check partner schedule",
                validation_due=date(2026, 5, 20),
                status=AssumptionStatus.UNVALIDATED,
                linked_risk_id=None,
                linked_milestone_id=None,
                owner_alias="demo",
                identified_date=date(2026, 5, 1),
                entity_refs=("WI:1001",),
            ),
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(app, ["assumptions", "list", "--program", "demo", "--format", "csv"])
    rows = list(csv.DictReader(result.stdout.splitlines()))

    assert result.exit_code == 0
    assert rows[0]["id"] == "assumption-1"
    assert rows[0]["validation_method"] == "Check partner schedule"


def test_assumptions_validate_updates_status(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.assumptions.PROGRAMS_ROOT", programs_root)
    save_assumptions(
        "demo",
        (
            Assumption(
                id="assumption-1",
                program_id="demo",
                text="Kusto team ships schema by Q3.",
                validation_method=None,
                validation_due=date(2026, 5, 20),
                status=AssumptionStatus.UNVALIDATED,
                linked_risk_id=None,
                linked_milestone_id=None,
                owner_alias="demo",
                identified_date=date(2026, 5, 1),
                entity_refs=(),
            ),
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(app, ["assumptions", "validate", "--program", "demo", "--id", "assumption-1"])

    assumptions = load_assumptions("demo", programs_root=programs_root)

    assert result.exit_code == 0
    assert assumptions[0].status is AssumptionStatus.CONFIRMED
    assert assumptions[0].resolved_date == datetime.now(timezone.utc).date()


def test_assumptions_invalidate_force_skips_risk_prompt(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.assumptions.PROGRAMS_ROOT", programs_root)
    save_assumptions(
        "demo",
        (
            Assumption(
                id="assumption-1",
                program_id="demo",
                text="Kusto team ships schema by Q3.",
                validation_method=None,
                validation_due=date(2026, 5, 20),
                status=AssumptionStatus.UNVALIDATED,
                linked_risk_id=None,
                linked_milestone_id=None,
                owner_alias="demo",
                identified_date=date(2026, 5, 1),
                entity_refs=(),
            ),
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(app, ["assumptions", "invalidate", "--program", "demo", "--id", "assumption-1", "--force"])

    assumptions = load_assumptions("demo", programs_root=programs_root)

    assert result.exit_code == 0
    assert assumptions[0].status is AssumptionStatus.INVALIDATED
    assert assumptions[0].resolved_date == datetime.now(timezone.utc).date()
    assert assumptions[0].linked_risk_id is None
    assert "Warning:" in result.stdout


def test_assumptions_invalidate_can_create_linked_risk(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.assumptions.PROGRAMS_ROOT", programs_root)
    monkeypatch.setenv("USERNAME", "demo")
    save_assumptions(
        "demo",
        (
            Assumption(
                id="assumption-1",
                program_id="demo",
                text="Kusto team ships schema by Q3.",
                validation_method=None,
                validation_due=date(2026, 5, 20),
                status=AssumptionStatus.UNVALIDATED,
                linked_risk_id=None,
                linked_milestone_id="m1",
                owner_alias="demo",
                identified_date=date(2026, 5, 1),
                entity_refs=("WI:1001",),
            ),
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(
        app,
        ["assumptions", "invalidate", "--program", "demo", "--id", "assumption-1"],
        input="y\npossible\nmedium\ndependency\n",
    )

    assumptions = load_assumptions("demo", programs_root=programs_root)
    risks = load_risk_register("demo", programs_root=programs_root)

    assert result.exit_code == 0
    assert assumptions[0].status is AssumptionStatus.INVALIDATED
    assert assumptions[0].resolved_date == datetime.now(timezone.utc).date()
    assert assumptions[0].linked_risk_id is not None
    assert len(risks) == 1
    assert risks[0].id == assumptions[0].linked_risk_id
    assert risks[0].linked_milestone_ids == ("m1",)