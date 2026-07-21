"""PPL-W2B.6: per-program registry promotion evidence and gate.

This is the program-level counterpart to ``evaluate_family_flip_gate``:
clean evidence advances one consecutive counter, any failed requirement
resets it, and a separate guarded mode mutation may promote only after the
counter reaches the configured threshold.  The state is metadata only; it
never changes customer factual registry files.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from src.core.exceptions import ConfigError
from src.core.people_registry_governance import inspect_registry_manifest_integrity
from src.core.people_registry_identity import load_registry_config, load_registry_manifest
from src.core.people_registry_storage_class import qualify_registry_storage, refresh_registry_storage_status


PROGRAM_PROMOTION_SCHEMA_VERSION = "1.0"
PROGRAM_PROMOTION_CLEAN_CYCLES_REQUIRED = 5
PROGRAM_PROMOTION_REQUIRED_CONSUMERS = ("readiness", "report", "nudge", "doctor")
_PROMOTION_STATE_DIRNAME = "program_promotion"


@dataclass(frozen=True, slots=True)
class ProgramPromotionConsumerEvidence:
    """One enabled consumer's outcome for a prospective clean cycle."""

    consumer: str
    generation_id: str
    succeeded: bool


@dataclass(frozen=True, slots=True)
class ProgramPromotionCycleEvidence:
    """Evidence that one program completed one promotion-cycle attempt."""

    generation_id: str
    load_succeeded: bool
    load_generation_id: str | None
    consumers: tuple[ProgramPromotionConsumerEvidence, ...]
    parity_divergence_count: int
    unresolved_critical_identity_conflicts: int
    nfr_compliant: bool
    enabled_consumers: tuple[str, ...] = PROGRAM_PROMOTION_REQUIRED_CONSUMERS


@dataclass(frozen=True, slots=True)
class ProgramPromotionState:
    """Crash-safe, per-program promotion metadata stored under ``knowledge/.state``."""

    program_id: str
    clean_cycles: int = 0
    current_generation_id: str | None = None
    rollback_restore_drill_generation_id: str | None = None
    rollback_restore_drill_at: datetime | None = None
    last_cycle_at: datetime | None = None
    last_cycle_clean: bool = False
    last_failure_reason: str | None = None
    last_cycle_consumers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProgramPromotionPrerequisites:
    """Live registry prerequisites that cannot be trusted from old evidence."""

    generation_id: str | None
    storage_qualified: bool
    manifest_integrity_clean: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProgramPromotionGateResult:
    """Outcome of recording one clean-cycle gate evaluation."""

    program_id: str
    mode: str
    clean_cycles: int
    required_clean_cycles: int
    ready_to_promote: bool
    action: str
    reason: str
    state: ProgramPromotionState


@dataclass(frozen=True, slots=True)
class ProgramPromotionStatus:
    """Diagnostic status for one program's promotion evidence and blockers."""

    program_id: str
    mode: str
    clean_cycles: int
    required_clean_cycles: int
    current_generation_id: str | None
    rollback_restore_drill_generation_id: str | None
    ready_to_promote: bool
    blocked_reasons: tuple[str, ...]
    last_failure_reason: str | None
    last_cycle_consumers: tuple[str, ...]


def program_promotion_state_path(knowledge_root: Path, program_id: str) -> Path:
    """Return this program's state file without permitting path traversal."""

    _validate_program_id(program_id)
    return knowledge_root / ".state" / _PROMOTION_STATE_DIRNAME / f"{program_id}.json"


def load_program_promotion_state(knowledge_root: Path, program_id: str) -> ProgramPromotionState | None:
    path = program_promotion_state_path(knowledge_root, program_id)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigError(f"Invalid program promotion state at {path}: {error}") from error
    if not isinstance(raw, dict):
        raise ConfigError(f"Program promotion state at {path} must be a JSON object.")
    if raw.get("schema_version") != PROGRAM_PROMOTION_SCHEMA_VERSION:
        raise ConfigError(f"Unsupported program promotion state schema at {path}.")
    if raw.get("program_id") != program_id:
        raise ConfigError(f"Program promotion state at {path} is for a different program.")

    clean_cycles = raw.get("clean_cycles", 0)
    if not isinstance(clean_cycles, int) or clean_cycles < 0:
        raise ConfigError(f"Program promotion state at {path} has an invalid clean_cycles value.")
    consumers = raw.get("last_cycle_consumers") or []
    if not isinstance(consumers, list) or not all(isinstance(value, str) for value in consumers):
        raise ConfigError(f"Program promotion state at {path} has invalid last_cycle_consumers.")
    return ProgramPromotionState(
        program_id=program_id,
        clean_cycles=clean_cycles,
        current_generation_id=_optional_string(raw.get("current_generation_id"), field_name="current_generation_id", path=path),
        rollback_restore_drill_generation_id=_optional_string(
            raw.get("rollback_restore_drill_generation_id"),
            field_name="rollback_restore_drill_generation_id",
            path=path,
        ),
        rollback_restore_drill_at=_optional_datetime(raw.get("rollback_restore_drill_at"), field_name="rollback_restore_drill_at", path=path),
        last_cycle_at=_optional_datetime(raw.get("last_cycle_at"), field_name="last_cycle_at", path=path),
        last_cycle_clean=bool(raw.get("last_cycle_clean", False)),
        last_failure_reason=_optional_string(raw.get("last_failure_reason"), field_name="last_failure_reason", path=path),
        last_cycle_consumers=tuple(consumers),
    )


def record_program_rollback_restore_drill(
    knowledge_root: Path,
    program_id: str,
    *,
    generation_id: str,
    restore_verified: bool,
    recorded_at: datetime | None = None,
) -> ProgramPromotionState:
    """Persist successful restore evidence for the current registry generation.

    Callers must supply the generation verified by the actual restore result.
    A failed drill never becomes promotion evidence.
    """

    manifest = load_registry_manifest(knowledge_root)
    if manifest is None:
        raise ConfigError("Cannot record a rollback/restore drill before the registry is bootstrapped.")
    if not restore_verified:
        raise ConfigError("Cannot record rollback/restore drill evidence because restore verification did not succeed.")
    if generation_id != manifest.generation_id:
        raise ConfigError(
            "Cannot record rollback/restore drill evidence for a stale generation: "
            f"expected {manifest.generation_id!r}, got {generation_id!r}."
        )

    prior = load_program_promotion_state(knowledge_root, program_id) or ProgramPromotionState(program_id=program_id)
    state = ProgramPromotionState(
        program_id=program_id,
        clean_cycles=prior.clean_cycles,
        current_generation_id=prior.current_generation_id,
        rollback_restore_drill_generation_id=generation_id,
        rollback_restore_drill_at=_utc(recorded_at),
        last_cycle_at=prior.last_cycle_at,
        last_cycle_clean=prior.last_cycle_clean,
        last_failure_reason=prior.last_failure_reason,
        last_cycle_consumers=prior.last_cycle_consumers,
    )
    _write_program_promotion_state(knowledge_root, state)
    return state


def record_program_promotion_cycle(
    knowledge_root: Path,
    program_id: str,
    evidence: ProgramPromotionCycleEvidence,
    *,
    recorded_at: datetime | None = None,
) -> ProgramPromotionGateResult:
    """Evaluate and persist one sequential program clean-cycle attempt.

    This deliberately mirrors ``evaluate_family_flip_gate``'s semantics:
    a clean cycle increments a persisted consecutive counter; every failed
    condition resets it.  Unlike the fact-family gate, this function does
    not flip ``registry.yaml`` itself, so CLI preview/apply and direct mode
    setters have one explicit guarded transition point.
    """

    config = load_registry_config(knowledge_root)
    manifest = load_registry_manifest(knowledge_root)
    if config is None or manifest is None:
        raise ConfigError("Cannot record a promotion cycle before the registry is bootstrapped.")
    now = _utc(recorded_at)
    prior = load_program_promotion_state(knowledge_root, program_id) or ProgramPromotionState(program_id=program_id)
    mode = config.program_mode(program_id)
    prerequisites = inspect_program_promotion_prerequisites(knowledge_root)
    reasons = _cycle_failure_reasons(
        evidence,
        expected_generation_id=manifest.generation_id,
        state=prior,
        mode=mode,
        prerequisites=prerequisites,
    )

    if prior.current_generation_id is not None and prior.current_generation_id != evidence.generation_id:
        reasons = (
            f"registry generation changed from {prior.current_generation_id!r} to {evidence.generation_id!r}; "
            "clean-cycle evidence was reset",
            *reasons,
        )

    if reasons:
        state = ProgramPromotionState(
            program_id=program_id,
            clean_cycles=0,
            current_generation_id=evidence.generation_id,
            rollback_restore_drill_generation_id=prior.rollback_restore_drill_generation_id,
            rollback_restore_drill_at=prior.rollback_restore_drill_at,
            last_cycle_at=now,
            last_cycle_clean=False,
            last_failure_reason="; ".join(reasons),
            last_cycle_consumers=tuple(sorted({entry.consumer for entry in evidence.consumers})),
        )
        _write_program_promotion_state(knowledge_root, state)
        return ProgramPromotionGateResult(
            program_id=program_id,
            mode=mode,
            clean_cycles=0,
            required_clean_cycles=PROGRAM_PROMOTION_CLEAN_CYCLES_REQUIRED,
            ready_to_promote=False,
            action="reset",
            reason=state.last_failure_reason,
            state=state,
        )

    state = ProgramPromotionState(
        program_id=program_id,
        clean_cycles=prior.clean_cycles + 1,
        current_generation_id=evidence.generation_id,
        rollback_restore_drill_generation_id=prior.rollback_restore_drill_generation_id,
        rollback_restore_drill_at=prior.rollback_restore_drill_at,
        last_cycle_at=now,
        last_cycle_clean=True,
        last_failure_reason=None,
        last_cycle_consumers=tuple(sorted(entry.consumer for entry in evidence.consumers if entry.succeeded)),
    )
    _write_program_promotion_state(knowledge_root, state)
    ready = state.clean_cycles >= PROGRAM_PROMOTION_CLEAN_CYCLES_REQUIRED
    return ProgramPromotionGateResult(
        program_id=program_id,
        mode=mode,
        clean_cycles=state.clean_cycles,
        required_clean_cycles=PROGRAM_PROMOTION_CLEAN_CYCLES_REQUIRED,
        ready_to_promote=ready,
        action="ready" if ready else "recorded_clean_cycle",
        reason=(
            f"{state.clean_cycles} consecutive clean cycles satisfy the promotion gate."
            if ready
            else f"{state.clean_cycles}/{PROGRAM_PROMOTION_CLEAN_CYCLES_REQUIRED} consecutive clean cycles recorded."
        ),
        state=state,
    )


def inspect_program_promotion_prerequisites(knowledge_root: Path) -> ProgramPromotionPrerequisites:
    """Check live storage and manifest conditions required for promotion."""

    config = load_registry_config(knowledge_root)
    manifest = load_registry_manifest(knowledge_root)
    if config is None or manifest is None:
        return ProgramPromotionPrerequisites(
            generation_id=None,
            storage_qualified=False,
            manifest_integrity_clean=False,
            reasons=("registry is not bootstrapped",),
        )
    qualification = qualify_registry_storage(knowledge_root)
    integrity = inspect_registry_manifest_integrity(knowledge_root)
    reasons: list[str] = []
    if not qualification.qualified_for_primary:
        reasons.append(f"storage is not qualified for primary: {qualification.detail}")
    if not integrity.is_clean:
        reasons.append("registry manifest integrity is not clean")
    if config.workspace_id != manifest.workspace_id or config.customer_boundary_id != manifest.customer_boundary_id:
        reasons.append("registry config and manifest identity do not match")
    return ProgramPromotionPrerequisites(
        generation_id=manifest.generation_id,
        storage_qualified=qualification.qualified_for_primary,
        manifest_integrity_clean=integrity.is_clean,
        reasons=tuple(reasons),
    )


def program_promotion_status(knowledge_root: Path, program_id: str) -> ProgramPromotionStatus:
    """Return promotion progress and all current blockers for one program."""

    config = load_registry_config(knowledge_root)
    state = load_program_promotion_state(knowledge_root, program_id)
    prerequisites = inspect_program_promotion_prerequisites(knowledge_root)
    mode = config.program_mode(program_id) if config is not None else "legacy"
    clean_cycles = state.clean_cycles if state is not None else 0
    blocked: list[str] = list(prerequisites.reasons)
    if mode == "primary":
        blocked.append("program is already in primary mode")
    elif mode != "shadow":
        blocked.append(f"program mode must be 'shadow' before promotion (currently {mode!r})")
    if state is None:
        blocked.append("no promotion cycles have been recorded")
    else:
        if state.current_generation_id != prerequisites.generation_id:
            blocked.append("clean-cycle evidence is not for the current registry generation")
        if state.rollback_restore_drill_generation_id != prerequisites.generation_id:
            blocked.append("no successful rollback/restore drill is recorded for the current registry generation")
        if not state.last_cycle_clean:
            blocked.append(state.last_failure_reason or "the latest promotion cycle was not clean")
        if clean_cycles < PROGRAM_PROMOTION_CLEAN_CYCLES_REQUIRED:
            blocked.append(
                f"requires {PROGRAM_PROMOTION_CLEAN_CYCLES_REQUIRED} consecutive clean cycles; currently {clean_cycles}"
            )
    return ProgramPromotionStatus(
        program_id=program_id,
        mode=mode,
        clean_cycles=clean_cycles,
        required_clean_cycles=PROGRAM_PROMOTION_CLEAN_CYCLES_REQUIRED,
        current_generation_id=prerequisites.generation_id,
        rollback_restore_drill_generation_id=(
            state.rollback_restore_drill_generation_id if state is not None else None
        ),
        ready_to_promote=not blocked,
        blocked_reasons=tuple(blocked),
        last_failure_reason=state.last_failure_reason if state is not None else None,
        last_cycle_consumers=state.last_cycle_consumers if state is not None else (),
    )


def reset_program_promotion_cycles(
    knowledge_root: Path,
    program_id: str,
    *,
    reason: str,
    recorded_at: datetime | None = None,
) -> ProgramPromotionState:
    """Reset only this program's promotion counter after a rollback/failure."""

    if not reason.strip():
        raise ConfigError("A non-empty reset reason is required.")
    prior = load_program_promotion_state(knowledge_root, program_id) or ProgramPromotionState(program_id=program_id)
    manifest = load_registry_manifest(knowledge_root)
    state = ProgramPromotionState(
        program_id=program_id,
        clean_cycles=0,
        current_generation_id=manifest.generation_id if manifest is not None else prior.current_generation_id,
        rollback_restore_drill_generation_id=prior.rollback_restore_drill_generation_id,
        rollback_restore_drill_at=prior.rollback_restore_drill_at,
        last_cycle_at=_utc(recorded_at),
        last_cycle_clean=False,
        last_failure_reason=reason.strip(),
        last_cycle_consumers=(),
    )
    _write_program_promotion_state(knowledge_root, state)
    return state


def persist_primary_storage_qualification(knowledge_root: Path) -> None:
    """Persist the live storage qualification used by a primary promotion."""

    qualification = refresh_registry_storage_status(knowledge_root)
    if not qualification.qualified_for_primary:
        raise ConfigError(
            f"Cannot promote a program to 'primary': {qualification.detail} "
            "Resolve the storage-class issue before retrying."
        )


def _cycle_failure_reasons(
    evidence: ProgramPromotionCycleEvidence,
    *,
    expected_generation_id: str,
    state: ProgramPromotionState,
    mode: str,
    prerequisites: ProgramPromotionPrerequisites,
) -> tuple[str, ...]:
    reasons: list[str] = list(prerequisites.reasons)
    if mode not in {"shadow", "primary"}:
        reasons.append(f"program mode must be shadow or primary while recording a cycle (currently {mode!r})")
    if evidence.generation_id != expected_generation_id:
        reasons.append(
            f"cycle generation {evidence.generation_id!r} does not match current registry generation {expected_generation_id!r}"
        )
    if not evidence.load_succeeded:
        reasons.append("registry load did not succeed")
    if evidence.load_generation_id != evidence.generation_id:
        reasons.append("registry load did not use the cycle generation")
    if evidence.parity_divergence_count != 0:
        reasons.append(f"shadow parity has {evidence.parity_divergence_count} divergence(s)")
    if evidence.parity_divergence_count < 0:
        reasons.append("shadow parity divergence count cannot be negative")
    if evidence.unresolved_critical_identity_conflicts != 0:
        reasons.append(
            f"{evidence.unresolved_critical_identity_conflicts} unresolved critical identity conflict(s) remain"
        )
    if evidence.unresolved_critical_identity_conflicts < 0:
        reasons.append("critical identity conflict count cannot be negative")
    if not evidence.nfr_compliant:
        reasons.append("registry NFR evidence is not compliant")
    if state.rollback_restore_drill_generation_id != evidence.generation_id:
        reasons.append("no successful rollback/restore drill is recorded for this generation")

    enabled = tuple(dict.fromkeys(entry.strip() for entry in evidence.enabled_consumers if entry.strip()))
    if set(enabled) != set(PROGRAM_PROMOTION_REQUIRED_CONSUMERS):
        reasons.append(
            "a promotion cycle must include every enabled consumer: "
            + ", ".join(PROGRAM_PROMOTION_REQUIRED_CONSUMERS)
        )
    by_consumer: dict[str, ProgramPromotionConsumerEvidence] = {}
    for entry in evidence.consumers:
        if entry.consumer in by_consumer:
            reasons.append(f"duplicate consumer evidence for {entry.consumer!r}")
            continue
        by_consumer[entry.consumer] = entry
    unknown_evidence = sorted(set(by_consumer) - set(PROGRAM_PROMOTION_REQUIRED_CONSUMERS))
    if unknown_evidence:
        reasons.append(f"unknown promotion consumer evidence: {', '.join(unknown_evidence)}")
    for consumer in enabled:
        entry = by_consumer.get(consumer)
        if entry is None:
            reasons.append(f"enabled {consumer} consumer did not run")
        elif not entry.succeeded:
            reasons.append(f"enabled {consumer} consumer failed")
        elif entry.generation_id != evidence.generation_id:
            reasons.append(f"enabled {consumer} consumer did not use the cycle generation")
    return tuple(reasons)


def _write_program_promotion_state(knowledge_root: Path, state: ProgramPromotionState) -> None:
    path = program_promotion_state_path(knowledge_root, state.program_id)
    payload = {
        "schema_version": PROGRAM_PROMOTION_SCHEMA_VERSION,
        "program_id": state.program_id,
        "clean_cycles": state.clean_cycles,
        "current_generation_id": state.current_generation_id,
        "rollback_restore_drill_generation_id": state.rollback_restore_drill_generation_id,
        "rollback_restore_drill_at": _format_datetime(state.rollback_restore_drill_at),
        "last_cycle_at": _format_datetime(state.last_cycle_at),
        "last_cycle_clean": state.last_cycle_clean,
        "last_failure_reason": state.last_failure_reason,
        "last_cycle_consumers": list(state.last_cycle_consumers),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def _validate_program_id(program_id: str) -> None:
    if not program_id or program_id.strip() != program_id or Path(program_id).name != program_id or program_id in {".", ".."}:
        raise ConfigError("program_id must be a non-empty single path segment.")


def _utc(value: datetime | None) -> datetime:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _format_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _optional_datetime(value: object, *, field_name: str, path: Path) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ConfigError(f"Program promotion state at {path} has invalid {field_name}.")
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise ConfigError(f"Program promotion state at {path} has invalid {field_name}.") from error


def _optional_string(value: object, *, field_name: str, path: Path) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ConfigError(f"Program promotion state at {path} has invalid {field_name}.")
    return value
