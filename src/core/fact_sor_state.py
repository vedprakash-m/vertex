from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from src.core.exceptions import ConfigError


PROGRAMS_ROOT = Path(__file__).resolve().parents[2] / "programs"

# Valid modes for program-level and per-family SoR state.
_VALID_MODES = frozenset({"legacy", "shadow", "primary"})

# Authority families defined in source_authority.yaml (§6.2.2).
AUTHORITY_FAMILIES: tuple[str, ...] = (
    "workitem.state",
    "metric",
    "incident",
    "judgment",
    "commitment",
    "narrative",
)


@dataclass(frozen=True, slots=True)
class FactSorState:
    """SoR mode for a program, with optional per-family overrides (WI-5.2).

    ``mode`` is the program-level default.
    ``family_modes`` maps authority family name → mode for families that
    have been individually flipped (§6.7 per-family SoR flip).
    v1 files (schema_version 1.x) have no ``family_modes`` and load with
    an empty dict — backwards-compatible by design.
    """
    mode: str
    recorded_at: datetime
    recorded_by: str | None = None
    family_modes: dict[str, str] = field(default_factory=dict)


def get_fact_sor_state_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "fact_store_sor.yaml"


def resolve_family_sor_mode(
    program_id: str,
    family: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> str:
    """Return the SoR mode for a specific authority family.

    Checks ``family_modes`` first; falls back to the program-level mode.
    Returns "legacy" when no state file exists.
    """
    state = load_fact_sor_state(program_id, programs_root=programs_root)
    if state is None:
        return "legacy"
    return state.family_modes.get(family, state.mode)


def load_fact_sor_state(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> FactSorState | None:
    path = get_fact_sor_state_path(program_id, programs_root=programs_root)
    if not path.exists():
        return None
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"Invalid YAML in {path}.") from error
    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping in {path}.")

    schema_version = _required_string(document.get("schema_version"), field_name="schema_version").strip()
    major = schema_version.split(".", 1)[0]
    if major not in {"1", "2"}:
        raise ConfigError(f"Unsupported fact-store SoR schema_version {schema_version!r} in {path}.")

    mode = _required_string(document.get("mode"), field_name="mode").strip().lower()
    if mode not in _VALID_MODES:
        raise ConfigError(f"Fact-store SoR mode in {path} must be legacy, shadow, or primary.")

    family_modes: dict[str, str] = {}
    if major == "2":
        raw_fm = document.get("family_modes") or {}
        if not isinstance(raw_fm, dict):
            raise ConfigError(f"fact-store SoR family_modes in {path} must be a mapping.")
        for fam, fmode in raw_fm.items():
            if not isinstance(fmode, str) or fmode.strip().lower() not in _VALID_MODES:
                raise ConfigError(
                    f"fact-store SoR family_modes[{fam!r}] in {path} must be legacy, shadow, or primary."
                )
            family_modes[str(fam)] = fmode.strip().lower()

    return FactSorState(
        mode=mode,
        recorded_at=_parse_datetime(document.get("recorded_at")),
        recorded_by=_optional_string(document.get("recorded_by"), field_name="recorded_by"),
        family_modes=family_modes,
    )


def save_fact_sor_state(
    program_id: str,
    *,
    mode: str,
    recorded_at: datetime,
    recorded_by: str | None = None,
    family_modes: dict[str, str] | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> FactSorState:
    normalized_mode = mode.strip().lower()
    if normalized_mode not in _VALID_MODES:
        raise ValueError("mode must be legacy, shadow, or primary.")

    normalized_fm: dict[str, str] = {}
    for fam, fmode in (family_modes or {}).items():
        fm_norm = fmode.strip().lower()
        if fm_norm not in _VALID_MODES:
            raise ValueError(f"family_modes[{fam!r}] must be legacy, shadow, or primary.")
        normalized_fm[str(fam)] = fm_norm

    state = FactSorState(
        mode=normalized_mode,
        recorded_at=recorded_at,
        recorded_by=_optional_string(recorded_by, field_name="recorded_by"),
        family_modes=normalized_fm,
    )
    document: dict = {
        "schema_version": "2.0",
        "mode": state.mode,
        "recorded_at": state.recorded_at.isoformat(),
        "recorded_by": state.recorded_by,
    }
    if normalized_fm:
        document["family_modes"] = dict(normalized_fm)

    path = get_fact_sor_state_path(program_id, programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    return state


# ---------------------------------------------------------------------------
# Per-family clean-cycle counter (WI-5.3 gate)
# ---------------------------------------------------------------------------

_FAMILY_CYCLES_FILENAME = "fact_store_family_cycles.yaml"


def get_family_cycles_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / _FAMILY_CYCLES_FILENAME


def load_family_clean_cycles(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> dict[str, int]:
    """Return per-family consecutive clean-cycle counts.

    Returns an empty dict when the file does not exist (no cycles recorded).
    """
    path = get_family_cycles_path(program_id, programs_root=programs_root)
    if not path.exists():
        return {}
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"Invalid YAML in {path}.") from error
    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping in {path}.")
    result: dict[str, int] = {}
    for key, value in document.items():
        if isinstance(value, int) and value >= 0:
            result[str(key)] = value
    return result


def record_family_clean_cycle(
    program_id: str,
    family: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> int:
    """Increment the clean-cycle counter for one authority family.

    Returns the new count. Safe to call concurrently (last-writer-wins is
    acceptable since gather runs are sequential per program).
    """
    cycles = load_family_clean_cycles(program_id, programs_root=programs_root)
    new_count = cycles.get(family, 0) + 1
    cycles[family] = new_count
    path = get_family_cycles_path(program_id, programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(dict(cycles), sort_keys=True, allow_unicode=False),
        encoding="utf-8",
    )
    return new_count


def reset_family_clean_cycles(
    program_id: str,
    family: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    """Reset the clean-cycle counter for one family (called after a parity failure)."""
    cycles = load_family_clean_cycles(program_id, programs_root=programs_root)
    if family in cycles:
        cycles[family] = 0
        path = get_family_cycles_path(program_id, programs_root=programs_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(dict(cycles), sort_keys=True, allow_unicode=False),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# S-5c: Clean-cycle flip gate + rollback checkpoint
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FamilyFlipResult:
    """Outcome of evaluating the flip gate for one authority family."""
    family: str
    previous_mode: str
    new_mode: str
    action: str   # "flipped_to_primary" | "rolled_back_to_shadow" | "no_change"
    clean_cycles: int
    reason: str


def evaluate_family_flip_gate(
    program_id: str,
    family: str,
    divergence_count: int,
    total_entities_in_family: int,
    *,
    sor_flip_config: object,
    recorded_at: datetime,
    recorded_by: str | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> FamilyFlipResult:
    """Evaluate the S-5c flip gate for one authority family.

    - If the family is in ``shadow`` mode and has reached the clean-cycle
      threshold (and policy allows), flip it to ``primary``.
    - If the family is in ``primary`` mode and divergence exceeds tolerance,
      roll back to ``shadow`` and reset the counter.
    - In all other cases, increment or reset the clean-cycle counter.

    ``sor_flip_config`` must be a ``SorFlipFamilyConfig``-like object with
    ``.clean_cycles_to_flip``, ``.divergence_tolerance``, ``.critical_zero``,
    and ``.require_s0g_policy`` attributes.

    ``total_entities_in_family`` is used to compute the divergence fraction
    when ``critical_zero=False``.  Pass 0 to use strict zero-divergence gate.

    Returns a ``FamilyFlipResult`` describing what happened.  Persists the new
    SoR state if a flip or rollback occurred.  Counter updates are always
    persisted.
    """
    state = load_fact_sor_state(program_id, programs_root=programs_root)
    current_mode = (state.family_modes.get(family) if state else None) or (state.mode if state else "legacy")

    clean_cycles_threshold: int = getattr(sor_flip_config, "clean_cycles_to_flip", 5)
    tolerance: float = getattr(sor_flip_config, "divergence_tolerance", 0.02)
    critical_zero: bool = getattr(sor_flip_config, "critical_zero", True)
    require_s0g_policy: bool = getattr(sor_flip_config, "require_s0g_policy", True)

    # Determine if this cycle is "clean" for this family
    if critical_zero or total_entities_in_family <= 0:
        is_clean = divergence_count == 0
    else:
        fraction = divergence_count / total_entities_in_family
        is_clean = fraction <= tolerance

    if is_clean:
        clean_cycles = record_family_clean_cycle(program_id, family, programs_root=programs_root)
    else:
        reset_family_clean_cycles(program_id, family, programs_root=programs_root)
        clean_cycles = 0

    # Rollback: primary → shadow if divergence exceeded
    if current_mode == "primary" and not is_clean:
        new_family_modes = dict(state.family_modes if state else {})
        new_family_modes[family] = "shadow"
        save_fact_sor_state(
            program_id,
            mode=state.mode if state else "legacy",
            recorded_at=recorded_at,
            recorded_by=recorded_by or "sor_flip_gate",
            family_modes=new_family_modes,
            programs_root=programs_root,
        )
        return FamilyFlipResult(
            family=family,
            previous_mode="primary",
            new_mode="shadow",
            action="rolled_back_to_shadow",
            clean_cycles=0,
            reason=f"divergence={divergence_count} exceeds tolerance for family {family!r}; rolled back",
        )

    # Flip: shadow → primary if clean-cycle gate met and policy permits
    if (
        current_mode == "shadow"
        and is_clean
        and clean_cycles >= clean_cycles_threshold
        and not require_s0g_policy
    ):
        new_family_modes = dict(state.family_modes if state else {})
        new_family_modes[family] = "primary"
        save_fact_sor_state(
            program_id,
            mode=state.mode if state else "legacy",
            recorded_at=recorded_at,
            recorded_by=recorded_by or "sor_flip_gate",
            family_modes=new_family_modes,
            programs_root=programs_root,
        )
        return FamilyFlipResult(
            family=family,
            previous_mode="shadow",
            new_mode="primary",
            action="flipped_to_primary",
            clean_cycles=clean_cycles,
            reason=f"{clean_cycles} consecutive clean cycles reached threshold {clean_cycles_threshold}",
        )

    # Policy gate blocks flip even though clean-cycle count is met
    if (
        current_mode == "shadow"
        and is_clean
        and clean_cycles >= clean_cycles_threshold
        and require_s0g_policy
    ):
        return FamilyFlipResult(
            family=family,
            previous_mode=current_mode,
            new_mode=current_mode,
            action="no_change",
            clean_cycles=clean_cycles,
            reason=f"clean-cycle gate met ({clean_cycles} cycles) but require_s0g_policy blocks flip for {family!r}",
        )

    return FamilyFlipResult(
        family=family,
        previous_mode=current_mode,
        new_mode=current_mode,
        action="no_change",
        clean_cycles=clean_cycles,
        reason=f"{'clean' if is_clean else 'divergent'} cycle; {clean_cycles}/{clean_cycles_threshold} clean cycles",
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _parse_datetime(value: object) -> datetime:
    if value in (None, ""):
        raise ConfigError("fact-store SoR state requires recorded_at.")
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ConfigError("fact-store SoR recorded_at must be a string.")
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise ConfigError(f"Invalid datetime value {value!r} in fact-store SoR state.") from error


def _required_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"fact-store SoR {field_name} must be a string.")
    return value


def _optional_string(value: object, *, field_name: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ConfigError(f"fact-store SoR {field_name} must be a string.")
    return value.strip() or None