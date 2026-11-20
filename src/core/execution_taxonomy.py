"""RELO-W1.12: execution taxonomy and declarative model registry seam.

Zone A, deterministic and in-memory. Classifies how a unit of work executes
and validates which models may be used for which purpose. This is distinct
from ``src.core.model_registry``, which persists per-feature deployment pins.
The opt-in dependency blast-radius runner caller adapts its active pin into
this registry and carries the resulting classification into tier telemetry.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from src.core.model_registry import ModelPin


class ExecutionClass(str, Enum):
    DETERMINISTIC = "deterministic"
    AI_ASSISTED = "ai_assisted"
    AI_GENERATED = "ai_generated"
    EXTERNAL_EFFECT = "external_effect"


class RegistryFindingCode(str, Enum):
    DUPLICATE_MODEL_ID = "duplicate_model_id"
    UNKNOWN_EXECUTION_CLASS = "unknown_execution_class"
    DETERMINISTIC_MODEL = "deterministic_model"
    UNKNOWN_MODEL = "unknown_model"
    DISABLED_MODEL = "disabled_model"


@dataclass(frozen=True, slots=True)
class ModelRegistryEntry:
    model_id: str
    provider: str
    purpose: str
    execution_class: ExecutionClass | str
    enabled: bool


@dataclass(frozen=True, slots=True)
class RegistryFinding:
    code: RegistryFindingCode
    model_id: str
    detail: str


def model_pin_to_registry_entry(
    pin: ModelPin,
    *,
    provider: str,
    purpose: str,
    execution_class: ExecutionClass | str,
    enabled: bool,
) -> ModelRegistryEntry:
    """Adapt a persisted pin using caller-supplied declarative policy.

    The adapter projects only the pinned model identity; deployment lifecycle
    remains owned by ``ModelPin`` and is never derived from this metadata.
    """
    return ModelRegistryEntry(
        model_id=pin.model_id,
        provider=provider,
        purpose=purpose,
        execution_class=execution_class,
        enabled=enabled,
    )


def parse_execution_class(value: object) -> ExecutionClass | None:
    if isinstance(value, ExecutionClass):
        return value
    try:
        return ExecutionClass(value)
    except ValueError:
        return None


def validate_registry(entries: Iterable[ModelRegistryEntry]) -> tuple[RegistryFinding, ...]:
    entries = tuple(entries)
    findings: set[RegistryFinding] = set()
    for model_id, count in Counter(entry.model_id for entry in entries).items():
        if count > 1:
            findings.add(RegistryFinding(
                RegistryFindingCode.DUPLICATE_MODEL_ID, model_id, f"model_id registered {count} times"
            ))
    for entry in entries:
        execution_class = parse_execution_class(entry.execution_class)
        if execution_class is None:
            findings.add(RegistryFinding(
                RegistryFindingCode.UNKNOWN_EXECUTION_CLASS,
                entry.model_id,
                f"unknown execution_class {str(entry.execution_class)!r}",
            ))
        elif execution_class is ExecutionClass.DETERMINISTIC:
            findings.add(RegistryFinding(
                RegistryFindingCode.DETERMINISTIC_MODEL,
                entry.model_id,
                "model entries cannot use the deterministic execution class",
            ))
    return tuple(sorted(findings, key=lambda f: (f.model_id, f.code.value, f.detail)))


def lookup_model(entries: Iterable[ModelRegistryEntry], model_id: str) -> ModelRegistryEntry | None:
    """Return the single entry for ``model_id``; ``None`` if absent or ambiguous."""
    matches = [entry for entry in entries if entry.model_id == model_id]
    return matches[0] if len(matches) == 1 else None


def check_model_use(entries: Iterable[ModelRegistryEntry], model_id: str) -> tuple[RegistryFinding, ...]:
    matches = [entry for entry in entries if entry.model_id == model_id]
    if not matches:
        return (RegistryFinding(RegistryFindingCode.UNKNOWN_MODEL, model_id, "model is not registered"),)
    if len(matches) > 1:
        return (RegistryFinding(
            RegistryFindingCode.DUPLICATE_MODEL_ID, model_id, f"model_id registered {len(matches)} times"
        ),)
    if not matches[0].enabled:
        return (RegistryFinding(RegistryFindingCode.DISABLED_MODEL, model_id, "model is disabled"),)
    return ()
