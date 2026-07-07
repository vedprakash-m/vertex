from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PredicateDefinition:
    name: str
    value_kind: str
    description: str
    nullable: bool = True


_PREDICATES = {
    "first_deployment": PredicateDefinition(
        name="first_deployment",
        value_kind="string",
        description="First deployment window or date for a subject.",
        nullable=False,
    ),
    "launch_blocker": PredicateDefinition(
        name="launch_blocker",
        value_kind="string",
        description="Named launch blocker for a subject.",
    ),
    "fleet_health_threshold": PredicateDefinition(
        name="fleet_health_threshold",
        value_kind="string",
        description="Expected fleet health target or threshold.",
    ),
    "service_tier": PredicateDefinition(
        name="service_tier",
        value_kind="string",
        description="Declared service tier for a subject.",
        nullable=False,
    ),
}


def get_predicate_definition(name: str) -> PredicateDefinition | None:
    return _PREDICATES.get(name)


def validate_predicate_value(name: str, value: Any) -> None:
    definition = get_predicate_definition(name)
    if definition is None:
        raise ValueError(f"Unknown knowledge predicate: {name}")
    if value is None:
        if definition.nullable:
            return
        raise ValueError(f"Predicate '{name}' does not allow null values.")
    if definition.value_kind == "string" and not isinstance(value, str):
        raise ValueError(f"Predicate '{name}' requires a string value.")


def count() -> int:
    return len(_PREDICATES)


def all_predicates() -> tuple[PredicateDefinition, ...]:
    return tuple(sorted(_PREDICATES.values(), key=lambda item: item.name))