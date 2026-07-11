"""NFR/OpEx budget candidate registry (arch-fix.md Phase 0, §8/§A.0).

Loads `governance/nfr-budgets.yaml`. Ratification (candidate -> ratified) is
a human/PM decision, not something this module performs; it only loads and
validates the structure so a freeze contract test can enforce that a
ratified budget's numeric value is never loosened by a later edit.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from src.core.exceptions import ConfigError

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PATH = _REPO_ROOT / "governance" / "nfr-budgets.yaml"

_VALID_BUDGET_CLASSES = frozenset(
    {
        "microbenchmark",
        "integration",
        "fault-injection",
        "restore-drill",
        "production-SLO",
        "periodic-governance-review",
    }
)
_VALID_DIRECTIONS = frozenset({"ceiling", "floor", "qualitative"})
_VALID_STATUSES = frozenset({"candidate", "ratified"})


@dataclass(frozen=True, slots=True)
class NfrBudget:
    id: str
    area: str
    description: str
    budget_class: str
    direction: str
    unit: str | None
    value: float | int | None
    status: str
    ratified_at: str | None
    ratified_by: str | None

    @property
    def is_ratified(self) -> bool:
        return self.status == "ratified"


def load_nfr_budgets(path: Path = _DEFAULT_PATH) -> tuple[NfrBudget, ...]:
    if not path.exists():
        raise ConfigError(f"NFR budget registry not found: {path}")
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping in {path}.")

    schema_version = str(document.get("schema_version", ""))
    if schema_version != "1":
        raise ConfigError(f"Unsupported nfr-budgets schema_version {schema_version!r} in {path}.")

    raw_budgets = document.get("budgets") or []
    if not isinstance(raw_budgets, list):
        raise ConfigError(f"'budgets' must be a list in {path}.")

    budgets: list[NfrBudget] = []
    seen_ids: set[str] = set()
    for raw in raw_budgets:
        if not isinstance(raw, dict):
            raise ConfigError(f"Each budget entry in {path} must be a mapping.")
        budget_id = str(raw.get("id", "")).strip()
        if not budget_id:
            raise ConfigError(f"Budget entry missing 'id' in {path}.")
        if budget_id in seen_ids:
            raise ConfigError(f"Duplicate budget id {budget_id!r} in {path}.")
        seen_ids.add(budget_id)

        budget_class = str(raw.get("budget_class", ""))
        if budget_class not in _VALID_BUDGET_CLASSES:
            raise ConfigError(f"Budget {budget_id!r} has invalid budget_class {budget_class!r} in {path}.")

        direction = str(raw.get("direction", ""))
        if direction not in _VALID_DIRECTIONS:
            raise ConfigError(f"Budget {budget_id!r} has invalid direction {direction!r} in {path}.")

        status = str(raw.get("status", ""))
        if status not in _VALID_STATUSES:
            raise ConfigError(f"Budget {budget_id!r} has invalid status {status!r} in {path}.")

        budgets.append(
            NfrBudget(
                id=budget_id,
                area=str(raw.get("area", "")),
                description=str(raw.get("description", "")),
                budget_class=budget_class,
                direction=direction,
                unit=raw.get("unit"),
                value=raw.get("value"),
                status=status,
                ratified_at=raw.get("ratified_at"),
                ratified_by=raw.get("ratified_by"),
            )
        )

    return tuple(budgets)


def ratified_budgets(path: Path = _DEFAULT_PATH) -> tuple[NfrBudget, ...]:
    return tuple(b for b in load_nfr_budgets(path) if b.is_ratified)
