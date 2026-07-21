"""specs/people.md Phase 1, PPL-W1.9: per-program modes and kill switches.

§6.6: `registry.yaml`'s `write_mode`/`program_modes`/`provider_refresh_enabled`/
`shared_writes_enabled`/`audience_scopes_enabled` (all already parsed by
PPL-W1.1's `RegistryConfig`). "Every mode/flag defaults to the behavior-
preserving state; an unlisted program is `legacy`... Environment kill
switches can force legacy reads or disable refresh/audience expansion
without editing customer data."

§9.1's own verification bar: "Flipping any mode/flag returns to legacy
behavior without rewriting customer data" (§11.1 "Feature rollback").
Every setter below is a pure `registry.yaml` metadata edit -- none of
them touch `registry_synthetic_records.yaml`, the journal, or any other
factual file, satisfying that bar by construction.

Workspace `write_mode -> primary` is gated by PPL-W1.3 storage
qualification. Program `shadow -> primary` is gated separately by
PPL-W2B.6's persisted five-clean-cycle state. The program gate mirrors
``fact_sor_state.evaluate_family_flip_gate``: each clean cycle advances a
counter; any failed prerequisite resets it. It never writes factual
registry data.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from src.core.exceptions import ConfigError
from src.core.people_registry_identity import (
    WRITE_MODES,
    RegistryConfig,
    load_registry_config,
    registry_config_path,
    write_registry_config,
)
from src.core.people_registry_lease import acquire_registry_lease, release_registry_lease
from src.core.people_registry_promotion import (
    persist_primary_storage_qualification,
    program_promotion_status,
    reset_program_promotion_cycles,
)
from src.core.people_registry_storage_class import ensure_storage_qualified_for_primary

#: §6.6: "Environment kill switches can force legacy reads or disable
#: refresh/audience expansion without editing customer data." Deliberately
#: read-time-only overrides -- never written to registry.yaml.
ENV_FORCE_LEGACY = "VERTEX_REGISTRY_FORCE_LEGACY"
ENV_DISABLE_PROVIDER_REFRESH = "VERTEX_REGISTRY_DISABLE_PROVIDER_REFRESH"
ENV_DISABLE_AUDIENCE_EXPANSION = "VERTEX_REGISTRY_DISABLE_AUDIENCE_EXPANSION"

_MUTABLE_FLAG_NAMES = ("provider_refresh_enabled", "shared_writes_enabled", "audience_scopes_enabled", "delegation_enabled")


@dataclass(frozen=True, slots=True)
class EffectiveRegistryConfig:
    """The persisted `RegistryConfig` with environment kill switches
    applied at READ time. `persisted` is always the untouched on-disk
    value -- callers that need to know whether a kill switch is currently
    masking it can compare `effective_write_mode`/`effective_*` against
    `persisted`."""

    persisted: RegistryConfig
    effective_write_mode: str
    effective_provider_refresh_enabled: bool
    effective_audience_scopes_enabled: bool
    #: specs/people.md §7.6/PPL-W5b.2: no dedicated env kill switch exists
    #: for delegation (§6.6 only names refresh/audience expansion) -- this
    #: is masked by `force_legacy` alone, same as `shared_writes_enabled`.
    effective_delegation_enabled: bool
    force_legacy_active: bool
    provider_refresh_disabled_by_env: bool
    audience_expansion_disabled_by_env: bool

    def effective_program_mode(self, program_id: str) -> str:
        if self.force_legacy_active:
            return "legacy"
        return self.persisted.program_mode(program_id)


def _env_flag_set(name: str) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return value in ("1", "true", "yes", "on")


def load_effective_registry_config(knowledge_root: Path) -> EffectiveRegistryConfig | None:
    persisted = load_registry_config(knowledge_root)
    if persisted is None:
        return None
    force_legacy = _env_flag_set(ENV_FORCE_LEGACY)
    refresh_disabled = _env_flag_set(ENV_DISABLE_PROVIDER_REFRESH)
    audience_disabled = _env_flag_set(ENV_DISABLE_AUDIENCE_EXPANSION)
    return EffectiveRegistryConfig(
        persisted=persisted,
        effective_write_mode="legacy" if force_legacy else persisted.write_mode,
        effective_provider_refresh_enabled=False if (force_legacy or refresh_disabled) else persisted.provider_refresh_enabled,
        effective_audience_scopes_enabled=False if (force_legacy or audience_disabled) else persisted.audience_scopes_enabled,
        effective_delegation_enabled=False if force_legacy else persisted.delegation_enabled,
        force_legacy_active=force_legacy,
        provider_refresh_disabled_by_env=refresh_disabled,
        audience_expansion_disabled_by_env=audience_disabled,
    )


def _with_lease(knowledge_root: Path, owner: str, fn) -> RegistryConfig:
    """Serializes concurrent mode/flag flips through the workspace-global
    lease (PPL-W1.2) -- a metadata race between two operators flipping
    different fields at once could otherwise silently drop one edit
    (read-modify-write on the same file)."""
    lease = acquire_registry_lease(owner, knowledge_root=knowledge_root)
    try:
        return fn()
    finally:
        release_registry_lease(lease, knowledge_root=knowledge_root)


def set_workspace_write_mode(knowledge_root: Path, new_mode: str, *, actor: str) -> RegistryConfig:
    if new_mode not in WRITE_MODES:
        raise ConfigError(f"write_mode must be one of {WRITE_MODES}, got {new_mode!r}")
    config = load_registry_config(knowledge_root)
    if config is None:
        raise ConfigError("The registry has not been bootstrapped yet.")
    if new_mode == "primary":
        ensure_storage_qualified_for_primary(knowledge_root)  # Raises if unqualified.

    def _apply() -> RegistryConfig:
        updated = replace(config, write_mode=new_mode)
        write_registry_config(registry_config_path(knowledge_root), updated)
        return updated

    return _with_lease(knowledge_root, actor, _apply)


def set_program_mode(knowledge_root: Path, program_id: str, new_mode: str, *, actor: str) -> RegistryConfig:
    if new_mode not in WRITE_MODES:
        raise ConfigError(f"program mode must be one of {WRITE_MODES}, got {new_mode!r}")
    config = load_registry_config(knowledge_root)
    if config is None:
        raise ConfigError("The registry has not been bootstrapped yet.")
    current_mode = config.program_mode(program_id)
    if new_mode == "primary":
        if current_mode != "shadow":
            raise ConfigError(
                f"Program {program_id!r} must be in 'shadow' mode before promotion to 'primary'; "
                f"current mode is {current_mode!r}."
            )
        status = program_promotion_status(knowledge_root, program_id)
        if not status.ready_to_promote:
            raise ConfigError(
                "Promoting a program to 'primary' requires the PPL-W2B.6 five-clean-cycle gate: "
                + "; ".join(status.blocked_reasons)
            )
        persist_primary_storage_qualification(knowledge_root)

    def _apply() -> RegistryConfig:
        other_programs = tuple((pid, mode) for pid, mode in config.program_modes if pid != program_id)
        updated = replace(config, program_modes=tuple(sorted((*other_programs, (program_id, new_mode)))))
        write_registry_config(registry_config_path(knowledge_root), updated)
        if current_mode == "primary" and new_mode in {"shadow", "legacy"}:
            reset_program_promotion_cycles(
                knowledge_root,
                program_id,
                reason=f"Program mode rolled back from primary to {new_mode}.",
            )
        return updated

    return _with_lease(knowledge_root, actor, _apply)


def rollback_program_mode(
    knowledge_root: Path,
    program_id: str,
    *,
    target_mode: str = "shadow",
    actor: str,
) -> RegistryConfig:
    """Safely roll one persisted primary program back without touching facts."""

    if target_mode not in {"shadow", "legacy"}:
        raise ConfigError("rollback target_mode must be 'shadow' or 'legacy'.")
    config = load_registry_config(knowledge_root)
    if config is None:
        raise ConfigError("The registry has not been bootstrapped yet.")
    if config.program_mode(program_id) != "primary":
        raise ConfigError(f"Program {program_id!r} is not in 'primary' mode.")
    return set_program_mode(knowledge_root, program_id, target_mode, actor=actor)


def set_registry_flag(knowledge_root: Path, flag_name: str, value: bool, *, actor: str) -> RegistryConfig:
    if flag_name not in _MUTABLE_FLAG_NAMES:
        raise ConfigError(f"flag_name must be one of {_MUTABLE_FLAG_NAMES}, got {flag_name!r}")
    config = load_registry_config(knowledge_root)
    if config is None:
        raise ConfigError("The registry has not been bootstrapped yet.")

    def _apply() -> RegistryConfig:
        # flag_name is validated above against _MUTABLE_FLAG_NAMES (all bool
        # fields on RegistryConfig); mypy cannot verify a dynamic field name
        # against dataclasses.replace()'s precise per-field types.
        updated = replace(config, **{flag_name: value})  # type: ignore[arg-type]
        write_registry_config(registry_config_path(knowledge_root), updated)
        return updated

    return _with_lease(knowledge_root, actor, _apply)


@dataclass(frozen=True, slots=True)
class ProgramShadowStatus:
    """The current per-program mode and whether live parity is tracked."""

    program_id: str
    mode: str
    divergence_tracking_available: bool = True
    last_comparison_at: datetime | None = None
    note: str = "Use 'vertex kb registry mode shadow-parity <program> --record' to refresh parity evidence."


def program_shadow_status(knowledge_root: Path, program_id: str) -> ProgramShadowStatus | None:
    config = load_registry_config(knowledge_root)
    if config is None:
        return None
    return ProgramShadowStatus(program_id=program_id, mode=config.program_mode(program_id))
