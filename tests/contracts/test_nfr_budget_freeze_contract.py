"""Public budget templates and synthetic ratification-freeze contracts.

Deployment ratifications and their measurement baselines are private. The
public registry is a candidate template, not deployment approval or evidence.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.core.nfr_budgets import load_nfr_budgets, ratified_budgets


def _write_synthetic_registry(path: Path, *, ceiling: int = 10, floor: int = 3) -> None:
    rows = []
    for identifier, direction, value, status in (
        ("synthetic-ceiling", "ceiling", ceiling, "ratified"),
        ("synthetic-floor", "floor", floor, "ratified"),
        ("synthetic-policy", "qualitative", None, "ratified"),
        ("synthetic-candidate", "ceiling", None, "candidate"),
    ):
        rows.append({
            "id": identifier,
            "area": "Synthetic test",
            "description": "Constructed test contract; not deployment evidence.",
            "budget_class": "microbenchmark",
            "direction": direction,
            "unit": None,
            "value": value,
            "status": status,
            "ratified_at": "2000-01-01" if status == "ratified" else None,
            "ratified_by": "Synthetic test actor" if status == "ratified" else None,
        })
    path.write_text(
        yaml.safe_dump({"schema_version": "1", "budgets": rows}),
        encoding="utf-8",
    )


def _assert_frozen_registry(path: Path) -> None:
    baseline = {"synthetic-ceiling": 10, "synthetic-floor": 3}
    budgets = ratified_budgets(path)
    assert {budget.id for budget in budgets} == {
        "synthetic-ceiling", "synthetic-floor", "synthetic-policy",
    }
    for budget in budgets:
        if budget.direction == "qualitative" or budget.value is None:
            continue
        assert budget.id in baseline, "ratified numeric budget has no recorded baseline"
        if budget.direction == "ceiling":
            assert budget.value <= baseline[budget.id], "ratified ceiling was loosened"
        else:
            assert budget.value >= baseline[budget.id], "ratified floor was loosened"


def test_registry_loads_and_validates() -> None:
    budgets = load_nfr_budgets()
    assert budgets
    ids = [budget.id for budget in budgets]
    assert len(ids) == len(set(ids)), "duplicate budget ids"


def test_every_area_from_spec_table_is_present() -> None:
    areas = {budget.area for budget in load_nfr_budgets()}
    expected = {
        "Audit write", "Context compile", "Local encode", "Startup",
        "Capacity", "Concurrency", "Reliability", "Security", "Privacy",
        "Observability", "OpEx", "Compatibility", "Operations",
    }
    assert expected <= areas


def test_public_registry_contains_candidates_not_deployment_authority() -> None:
    for budget in load_nfr_budgets():
        assert budget.status == "candidate"
        assert budget.value is None
        assert budget.ratified_at is None
        assert budget.ratified_by is None
    assert ratified_budgets() == ()


def test_explicit_registry_preserves_ratification_without_using_public_template(
    tmp_path: Path,
) -> None:
    path = tmp_path / "synthetic-budgets.yaml"
    _write_synthetic_registry(path)
    assert len(load_nfr_budgets(path)) == 4
    _assert_frozen_registry(path)
    assert ratified_budgets() == ()


@pytest.mark.parametrize(("ceiling", "floor"), [(10, 3), (9, 3), (10, 4)])
def test_ratified_budgets_may_stay_equal_or_tighten(
    tmp_path: Path, ceiling: int, floor: int,
) -> None:
    path = tmp_path / "synthetic-budgets.yaml"
    _write_synthetic_registry(path, ceiling=ceiling, floor=floor)
    _assert_frozen_registry(path)


@pytest.mark.parametrize(
    ("ceiling", "floor", "message"),
    [(11, 3, "ceiling was loosened"), (10, 2, "floor was loosened")],
)
def test_ratified_budgets_cannot_loosen(
    tmp_path: Path, ceiling: int, floor: int, message: str,
) -> None:
    path = tmp_path / "synthetic-budgets.yaml"
    _write_synthetic_registry(path, ceiling=ceiling, floor=floor)
    with pytest.raises(AssertionError, match=message):
        _assert_frozen_registry(path)


@pytest.mark.parametrize("status", ["candidate", "ratified"])
def test_ratification_set_cannot_silently_change(tmp_path: Path, status: str) -> None:
    path = tmp_path / "synthetic-budgets.yaml"
    _write_synthetic_registry(path)
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["budgets"][0 if status == "candidate" else 3]["status"] = status
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(AssertionError):
        _assert_frozen_registry(path)
