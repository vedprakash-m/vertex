from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class KnowledgeClass(str, Enum):
    GLOSSARY = "glossary"
    ENTITY_ALIASES = "entity_aliases"
    STAKEHOLDER_ROLES = "stakeholder_roles"
    WORKSTREAM_OWNERSHIP = "workstream_ownership"
    PROGRAM_CHARTER = "program_charter"
    MILESTONE_HORIZONS = "milestone_horizons"
    RECURRING_RISK_DEPENDENCY_PATTERNS = "recurring_risk_dependency_patterns"
    DOCUMENT_LOCATIONS = "document_locations"


@dataclass(frozen=True, slots=True)
class PredicateDefinition:
    name: str
    knowledge_class: KnowledgeClass
    value_kind: str
    description: str
    nullable: bool = True

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "name": self.name,
            "knowledge_class": self.knowledge_class.value,
            "value_kind": self.value_kind,
            "description": self.description,
            "nullable": self.nullable,
        }


_PREDICATES = {
    "term_definition": PredicateDefinition(
        name="term_definition",
        knowledge_class=KnowledgeClass.GLOSSARY,
        value_kind="string",
        description="Approved definition for a domain term.",
        nullable=False,
    ),
    "is_deprecated": PredicateDefinition(
        name="is_deprecated",
        knowledge_class=KnowledgeClass.GLOSSARY,
        value_kind="boolean",
        description="Whether a term or interface is deprecated.",
        nullable=False,
    ),
    "entity_alias": PredicateDefinition(
        name="entity_alias",
        knowledge_class=KnowledgeClass.ENTITY_ALIASES,
        value_kind="string",
        description="Approved alias for a canonical entity.",
        nullable=False,
    ),
    "stakeholder_role": PredicateDefinition(
        name="stakeholder_role",
        knowledge_class=KnowledgeClass.STAKEHOLDER_ROLES,
        value_kind="string",
        description="Declared role for a stakeholder.",
        nullable=False,
    ),
    "workstream_owner": PredicateDefinition(
        name="workstream_owner",
        knowledge_class=KnowledgeClass.WORKSTREAM_OWNERSHIP,
        value_kind="string",
        description="Owner entity for a workstream.",
        nullable=False,
    ),
    "owner_changed": PredicateDefinition(
        name="owner_changed",
        knowledge_class=KnowledgeClass.WORKSTREAM_OWNERSHIP,
        value_kind="string",
        description="New owner entity after an ownership change.",
        nullable=False,
    ),
    "program_charter": PredicateDefinition(
        name="program_charter",
        knowledge_class=KnowledgeClass.PROGRAM_CHARTER,
        value_kind="string",
        description="Approved program charter statement.",
        nullable=False,
    ),
    "first_deployment": PredicateDefinition(
        name="first_deployment",
        knowledge_class=KnowledgeClass.MILESTONE_HORIZONS,
        value_kind="string",
        description="First deployment window or date for a subject.",
        nullable=False,
    ),
    "status": PredicateDefinition(
        name="status",
        knowledge_class=KnowledgeClass.MILESTONE_HORIZONS,
        value_kind="string",
        description="Current lifecycle status for a subject.",
        nullable=False,
    ),
    "milestone_horizon": PredicateDefinition(
        name="milestone_horizon",
        knowledge_class=KnowledgeClass.MILESTONE_HORIZONS,
        value_kind="string",
        description="Expected delivery horizon for a milestone.",
        nullable=False,
    ),
    "launch_blocker": PredicateDefinition(
        name="launch_blocker",
        knowledge_class=KnowledgeClass.RECURRING_RISK_DEPENDENCY_PATTERNS,
        value_kind="string",
        description="Named launch blocker for a subject.",
    ),
    "depends_on": PredicateDefinition(
        name="depends_on",
        knowledge_class=KnowledgeClass.RECURRING_RISK_DEPENDENCY_PATTERNS,
        value_kind="string",
        description="Entity or dependency required by the subject.",
        nullable=False,
    ),
    "recurring_pattern": PredicateDefinition(
        name="recurring_pattern",
        knowledge_class=KnowledgeClass.RECURRING_RISK_DEPENDENCY_PATTERNS,
        value_kind="string",
        description="Repeated risk or dependency pattern affecting the subject.",
        nullable=False,
    ),
    "risk_accepted": PredicateDefinition(
        name="risk_accepted",
        knowledge_class=KnowledgeClass.RECURRING_RISK_DEPENDENCY_PATTERNS,
        value_kind="boolean",
        description="Whether the subject's stated risk is accepted.",
        nullable=False,
    ),
    "fleet_health_threshold": PredicateDefinition(
        name="fleet_health_threshold",
        knowledge_class=KnowledgeClass.PROGRAM_CHARTER,
        value_kind="string",
        description="Expected fleet health target or threshold.",
    ),
    "service_tier": PredicateDefinition(
        name="service_tier",
        knowledge_class=KnowledgeClass.PROGRAM_CHARTER,
        value_kind="string",
        description="Declared service tier for a subject.",
        nullable=False,
    ),
    "document_location": PredicateDefinition(
        name="document_location",
        knowledge_class=KnowledgeClass.DOCUMENT_LOCATIONS,
        value_kind="string",
        description="Authoritative location or stable reference for a document.",
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
    if definition.value_kind == "boolean" and not isinstance(value, bool):
        raise ValueError(f"Predicate '{name}' requires a boolean value.")


def count() -> int:
    return len(_PREDICATES)


def all_predicates() -> tuple[PredicateDefinition, ...]:
    return tuple(sorted(_PREDICATES.values(), key=lambda item: item.name))


def all_knowledge_classes() -> tuple[KnowledgeClass, ...]:
    return tuple(KnowledgeClass)


def knowledge_class_for_predicate(name: str) -> KnowledgeClass:
    definition = get_predicate_definition(name)
    if definition is None:
        raise ValueError(f"Unknown knowledge predicate: {name}")
    return definition.knowledge_class