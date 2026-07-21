"""specs/people.md Phase 1, PPL-W1.1: workspace registry identity.

Mints one immutable `workspace_id`/`customer_boundary_id` pair per
customer-controlled `knowledge/` root and persists it to
`registry.yaml` + `registry_manifest.json` (specs/people.md §6.6, §6.7).

This module is the ONLY registry write path allowed to create these two
files without holding the workspace-global fenced lease (PPL-W1.2) --
bootstrap is necessarily a chicken-and-egg case: the lease's own SQLite
file lives under the same `knowledge/.state/` tree this step first
establishes. Every subsequent registry mutation (PPL-W1.4/W1.5 onward,
once the staged-transaction writer exists) must go through that writer,
never this module directly. The bootstrap manifest therefore carries
`fencing_token=0` and `transaction_id=None` -- honest markers that no
lease-governed transaction produced it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import yaml

from src.core.exceptions import ConfigError
from src.core.ledger.ulid import new_ulid

REGISTRY_SCHEMA_VERSION = "1.0"
REGISTRY_MANIFEST_SCHEMA_VERSION = "1.0"
#: Bumped whenever registry_manifest.json's own shape changes (specs/people.md
#: §6.7: "schema/compiler versions" are tracked independently of the
#: per-file schema_version fields so a manifest-format change is
#: detectable even when no factual file changed).
REGISTRY_COMPILER_VERSION = "1.0"

WRITE_MODES = ("legacy", "shadow", "primary")


@dataclass(frozen=True, slots=True)
class RegistryIdentity:
    """specs/people.md §7.2's binding RegistryIdentity contract."""

    workspace_id: str
    customer_boundary_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RegistryConfig:
    """The parsed contents of `knowledge/registry.yaml` (specs/people.md §6.6)."""

    schema_version: str
    workspace_id: str
    customer_boundary_id: str
    write_mode: str
    program_modes: tuple[tuple[str, str], ...] = ()
    provider_refresh_enabled: bool = False
    shared_writes_enabled: bool = False
    audience_scopes_enabled: bool = False
    #: specs/people.md §7.6/PPL-W5b.2: delegation is default-off per Phase
    #: 5b's own scope line ("delegation remains default-off and does not
    #: block registry/audience v1"), mirroring every other feature flag's
    #: exact off-by-default precedent.
    delegation_enabled: bool = False
    directory_steward_principals: tuple[str, ...] = ()
    pii_reveal_principals: tuple[str, ...] = ()

    def program_mode(self, program_id: str) -> str:
        # "Every mode/flag defaults to the behavior-preserving state; an
        # unlisted program is legacy." (specs/people.md §6.6)
        for known_id, mode in self.program_modes:
            if known_id == program_id:
                return mode
        return "legacy"


@dataclass(frozen=True, slots=True)
class RegistryManifest:
    """The parsed contents of `knowledge/registry_manifest.json`
    (specs/people.md §6.7): "workspace/customer identity, generation ID,
    prior generation, fencing token, schema/compiler versions,
    source/policy hashes, transaction ID and committed timestamp."."""

    generation_id: str
    prior_generation: str | None
    workspace_id: str
    customer_boundary_id: str
    fencing_token: int
    schema_version: str
    compiler_version: str
    source_hashes: tuple[tuple[str, str], ...] = ()
    policy_hashes: tuple[tuple[str, str], ...] = ()
    transaction_id: str | None = None
    committed_at: datetime = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class RegistryBootstrapResult:
    identity: RegistryIdentity
    config: RegistryConfig
    manifest: RegistryManifest
    created: bool  # True only if this call actually minted a NEW identity.


def registry_config_path(knowledge_root: Path) -> Path:
    return knowledge_root / "registry.yaml"


def registry_manifest_path(knowledge_root: Path) -> Path:
    return knowledge_root / "registry_manifest.json"


def load_registry_config(knowledge_root: Path) -> RegistryConfig | None:
    path = registry_config_path(knowledge_root)
    if not path.exists():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}")
    return _parse_registry_config(raw, path)


def _parse_registry_config(raw: dict, path: Path) -> RegistryConfig:
    workspace_id = str(raw.get("workspace_id") or "").strip()
    customer_boundary_id = str(raw.get("customer_boundary_id") or "").strip()
    if not workspace_id or not customer_boundary_id:
        raise ConfigError(f"{path}: workspace_id and customer_boundary_id are both required")
    write_mode = str(raw.get("write_mode") or "legacy").strip()
    if write_mode not in WRITE_MODES:
        raise ConfigError(f"{path}: write_mode must be one of {WRITE_MODES}, got {write_mode!r}")
    program_modes_raw = raw.get("program_modes") or {}
    if not isinstance(program_modes_raw, dict):
        raise ConfigError(f"{path}: program_modes must be a mapping")
    for program_id, mode in program_modes_raw.items():
        if str(mode) not in WRITE_MODES:
            raise ConfigError(f"{path}: program_modes.{program_id} must be one of {WRITE_MODES}, got {mode!r}")
    return RegistryConfig(
        schema_version=str(raw.get("schema_version") or REGISTRY_SCHEMA_VERSION),
        workspace_id=workspace_id,
        customer_boundary_id=customer_boundary_id,
        write_mode=write_mode,
        program_modes=tuple(sorted((str(k), str(v)) for k, v in program_modes_raw.items())),
        provider_refresh_enabled=bool(raw.get("provider_refresh_enabled", False)),
        shared_writes_enabled=bool(raw.get("shared_writes_enabled", False)),
        audience_scopes_enabled=bool(raw.get("audience_scopes_enabled", False)),
        delegation_enabled=bool(raw.get("delegation_enabled", False)),
        directory_steward_principals=tuple(str(v) for v in (raw.get("directory_steward_principals") or ())),
        pii_reveal_principals=tuple(str(v) for v in (raw.get("pii_reveal_principals") or ())),
    )


def load_registry_manifest(knowledge_root: Path) -> RegistryManifest | None:
    path = registry_manifest_path(knowledge_root)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as error:
        raise ConfigError(f"Invalid JSON in {path}: {error}") from error
    if not isinstance(raw, dict):
        raise ConfigError(f"Expected a JSON object at top-level in {path}")
    return _parse_registry_manifest(raw, path)


def _parse_registry_manifest(raw: dict, path: Path) -> RegistryManifest:
    generation_id = str(raw.get("generation_id") or "").strip()
    workspace_id = str(raw.get("workspace_id") or "").strip()
    customer_boundary_id = str(raw.get("customer_boundary_id") or "").strip()
    committed_at_raw = raw.get("committed_at")
    if not generation_id or not workspace_id or not committed_at_raw:
        raise ConfigError(f"{path}: generation_id, workspace_id, and committed_at are all required")
    return RegistryManifest(
        generation_id=generation_id,
        prior_generation=raw.get("prior_generation"),
        workspace_id=workspace_id,
        customer_boundary_id=customer_boundary_id,
        fencing_token=int(raw.get("fencing_token", 0)),
        schema_version=str(raw.get("schema_version") or REGISTRY_MANIFEST_SCHEMA_VERSION),
        compiler_version=str(raw.get("compiler_version") or REGISTRY_COMPILER_VERSION),
        source_hashes=tuple(sorted((str(k), str(v)) for k, v in (raw.get("source_hashes") or {}).items())),
        policy_hashes=tuple(sorted((str(k), str(v)) for k, v in (raw.get("policy_hashes") or {}).items())),
        transaction_id=raw.get("transaction_id"),
        committed_at=datetime.fromisoformat(str(committed_at_raw)),
    )


def write_registry_config(path: Path, config: RegistryConfig) -> None:
    payload = {
        "schema_version": config.schema_version,
        "workspace_id": config.workspace_id,
        "customer_boundary_id": config.customer_boundary_id,
        "write_mode": config.write_mode,
        "program_modes": dict(config.program_modes),
        "provider_refresh_enabled": config.provider_refresh_enabled,
        "shared_writes_enabled": config.shared_writes_enabled,
        "audience_scopes_enabled": config.audience_scopes_enabled,
        "delegation_enabled": config.delegation_enabled,
        "directory_steward_principals": list(config.directory_steward_principals),
        "pii_reveal_principals": list(config.pii_reveal_principals),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, sort_keys=False, default_flow_style=False)
        handle.flush()
        _fsync(handle)
    _atomic_replace(temp_path, path)


def write_registry_manifest(path: Path, manifest: RegistryManifest) -> None:
    payload = {
        "generation_id": manifest.generation_id,
        "prior_generation": manifest.prior_generation,
        "workspace_id": manifest.workspace_id,
        "customer_boundary_id": manifest.customer_boundary_id,
        "fencing_token": manifest.fencing_token,
        "schema_version": manifest.schema_version,
        "compiler_version": manifest.compiler_version,
        "source_hashes": dict(manifest.source_hashes),
        "policy_hashes": dict(manifest.policy_hashes),
        "transaction_id": manifest.transaction_id,
        "committed_at": manifest.committed_at.isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        _fsync(handle)
    _atomic_replace(temp_path, path)


def _fsync(handle) -> None:
    os.fsync(handle.fileno())


def _atomic_replace(temp_path: Path, path: Path) -> None:
    os.replace(temp_path, path)


def bootstrap_registry_identity(
    *,
    knowledge_root: Path,
    customer_boundary_id: str | None = None,
    apply: bool = False,
    as_of: datetime | None = None,
) -> RegistryBootstrapResult:
    """PPL-W1.1: mint the workspace identity exactly once.

    Idempotent by design: if `registry.yaml` already exists, this
    function returns the EXISTING identity untouched (`created=False`)
    regardless of `apply` or a differing `customer_boundary_id` argument
    -- workspace_id/customer_boundary_id are immutable once minted
    (specs/people.md §6.6 item 18, §9.1's own verification requirement:
    "Repeated bootstrap-detection calls observe one immutable identity").
    """
    existing_config = load_registry_config(knowledge_root)
    if existing_config is not None:
        existing_manifest = load_registry_manifest(knowledge_root)
        if existing_manifest is None:
            raise ConfigError(
                f"{registry_config_path(knowledge_root)} exists but {registry_manifest_path(knowledge_root)} "
                "is missing -- the registry is in an inconsistent state. Recovering from a partial/crashed "
                "write is PPL-W1.5's scope (startup/doctor recovery), not this bootstrap path."
            )
        identity = RegistryIdentity(
            workspace_id=existing_config.workspace_id,
            customer_boundary_id=existing_config.customer_boundary_id,
            created_at=existing_manifest.committed_at,
        )
        return RegistryBootstrapResult(identity=identity, config=existing_config, manifest=existing_manifest, created=False)

    if not apply:
        # Preview: describe the action without minting or persisting anything,
        # matching the platform-wide "mutation commands preview by default"
        # convention (specs/people.md §8.1).
        placeholder_config = RegistryConfig(
            schema_version=REGISTRY_SCHEMA_VERSION,
            workspace_id="<not yet minted -- preview only>",
            customer_boundary_id=customer_boundary_id or "<not yet minted -- preview only>",
            write_mode="legacy",
        )
        placeholder_manifest = RegistryManifest(
            generation_id="<not yet minted -- preview only>",
            prior_generation=None,
            workspace_id=placeholder_config.workspace_id,
            customer_boundary_id=placeholder_config.customer_boundary_id,
            fencing_token=0,
            schema_version=REGISTRY_MANIFEST_SCHEMA_VERSION,
            compiler_version=REGISTRY_COMPILER_VERSION,
        )
        return RegistryBootstrapResult(
            identity=RegistryIdentity(
                workspace_id=placeholder_config.workspace_id,
                customer_boundary_id=placeholder_config.customer_boundary_id,
                created_at=as_of or datetime.now(timezone.utc),
            ),
            config=placeholder_config,
            manifest=placeholder_manifest,
            created=False,
        )

    if not customer_boundary_id or not customer_boundary_id.strip():
        raise ConfigError(
            "Bootstrapping a new registry requires --customer-boundary-id (a customer-controlled identifier, "
            "e.g. your tenant/org short name). This is real customer configuration Vertex cannot invent on its "
            "own -- specs/people.md §6.6."
        )

    now = as_of or datetime.now(timezone.utc)
    workspace_id = f"workspace:{new_ulid(now)}"
    generation_id = new_ulid(now)

    config = RegistryConfig(
        schema_version=REGISTRY_SCHEMA_VERSION,
        workspace_id=workspace_id,
        customer_boundary_id=customer_boundary_id.strip(),
        write_mode="legacy",
    )
    manifest = RegistryManifest(
        generation_id=generation_id,
        prior_generation=None,
        workspace_id=workspace_id,
        customer_boundary_id=config.customer_boundary_id,
        fencing_token=0,  # No lease-governed transaction produced this bootstrap manifest -- see module docstring.
        schema_version=REGISTRY_MANIFEST_SCHEMA_VERSION,
        compiler_version=REGISTRY_COMPILER_VERSION,
        transaction_id=None,
        committed_at=now,
    )

    write_registry_config(registry_config_path(knowledge_root), config)
    write_registry_manifest(registry_manifest_path(knowledge_root), manifest)

    identity = RegistryIdentity(workspace_id=workspace_id, customer_boundary_id=config.customer_boundary_id, created_at=now)
    return RegistryBootstrapResult(identity=identity, config=config, manifest=manifest, created=True)
