"""specs/people.md Phase 1, PPL-W1.3: storage-class qualification.

specs/people.md §6.7's explicit storage support matrix:

| Class | Support |
|---|---|
| Local NTFS/ReFS | Supported |
| Mapped SMB/network workspace | Conditional on Phase 1 lease/rename/crash qualification using the existing bounded backoff hardening |
| OneDrive/SharePoint sync folder, consumer cloud-sync folder, filesystem without reliable locking/replace semantics | Unsupported for primary shared writes |

"The storage qualification result is persisted in registry capability
status and surfaced by doctor. A conditional/unsupported store cannot
promote `write_mode` to primary." (§6.7). This module owns detection,
persistence, and the reusable promotion gate; it does not itself
implement a `write_mode` mutator -- workspace `write_mode` only promotes
"after writer/recovery proof" (§6.6), i.e. once PPL-W1.4/W1.5's staged
transaction and crash-recovery machinery exists. PPL-W1.9 (per-program
modes and kill switches, the actual promotion/flip CLI) is the intended
caller of `ensure_storage_qualified_for_primary`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path

import yaml

from src.core.exceptions import ConfigError
from src.core.fs_utils import _is_network_filesystem_path

STORAGE_CLASS_LOCAL = "local"
STORAGE_CLASS_NETWORK_CONDITIONAL = "network_conditional"
STORAGE_CLASS_UNSUPPORTED_SYNC = "unsupported_sync"

STORAGE_CLASSES = (STORAGE_CLASS_LOCAL, STORAGE_CLASS_NETWORK_CONDITIONAL, STORAGE_CLASS_UNSUPPORTED_SYNC)

#: Env vars OneDrive's desktop sync client sets to the root(s) it syncs
#: locally; a knowledge_root under any of these is a cloud-sync folder,
#: not durable local storage, regardless of it presenting as NTFS.
_CLOUD_SYNC_ENV_VARS = ("OneDriveCommercial", "OneDriveConsumer", "OneDrive")

_CAPABILITY_STATUS_FILENAME = "registry_capability_status.yaml"


@dataclass(frozen=True, slots=True)
class RegistryStorageQualification:
    storage_class: str
    qualified_for_primary: bool
    detail: str
    checked_at: datetime = datetime.min.replace(tzinfo=timezone.utc)

    def to_payload(self) -> dict[str, object]:
        return {
            "storage_class": self.storage_class,
            "qualified_for_primary": self.qualified_for_primary,
            "detail": self.detail,
            "checked_at": self.checked_at.isoformat(),
        }


def _is_cloud_sync_path(path: Path) -> bool:
    """Best-effort detection of a OneDrive/SharePoint (or similar consumer
    cloud-sync) folder. Detection is intentionally conservative
    (env-var-anchored prefix match): a false negative here is safe -- the
    write path's own crash-injection hardening (PPL-W1.5) is the real
    backstop -- while a false positive would incorrectly block a
    legitimate local path, so only paths under a *known* synced folder
    root are flagged.
    """
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    for env_var in _CLOUD_SYNC_ENV_VARS:
        raw_root = os.environ.get(env_var)
        if not raw_root:
            continue
        try:
            root = Path(raw_root).resolve()
        except OSError:
            continue
        if resolved == root or root in resolved.parents:
            return True
    return False


def qualify_registry_storage(knowledge_root: Path, *, as_of: datetime | None = None) -> RegistryStorageQualification:
    """Live detection -- always recomputed, never trusts a cached file.
    `ensure_storage_qualified_for_primary` calls this directly rather than
    `load_registry_storage_status` so a promotion decision is never made
    against a stale on-disk snapshot."""
    now = as_of or datetime.now(timezone.utc)
    if _is_cloud_sync_path(knowledge_root):
        return RegistryStorageQualification(
            storage_class=STORAGE_CLASS_UNSUPPORTED_SYNC,
            qualified_for_primary=False,
            detail=(
                f"{knowledge_root} is under a consumer/cloud-sync folder (OneDrive/SharePoint sync client); "
                "unsupported for primary shared writes -- no reliable locking/replace semantics (specs/people.md §6.7)."
            ),
            checked_at=now,
        )
    if _is_network_filesystem_path(knowledge_root):
        return RegistryStorageQualification(
            storage_class=STORAGE_CLASS_NETWORK_CONDITIONAL,
            qualified_for_primary=True,
            detail=(
                f"{knowledge_root} is a mapped SMB/network path; conditionally supported using the existing "
                "bounded-backoff write hardening (src/core/_db.py's network-path retry/journal-mode handling)."
            ),
            checked_at=now,
        )
    return RegistryStorageQualification(
        storage_class=STORAGE_CLASS_LOCAL,
        qualified_for_primary=True,
        detail=f"{knowledge_root} is on local NTFS/ReFS-class storage.",
        checked_at=now,
    )


def registry_capability_status_path(knowledge_root: Path) -> Path:
    return knowledge_root / _CAPABILITY_STATUS_FILENAME


def load_registry_storage_status(knowledge_root: Path) -> RegistryStorageQualification | None:
    path = registry_capability_status_path(knowledge_root)
    if not path.exists():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"Expected mapping at top-level in {path}")
    storage_class = str(raw.get("storage_class") or "")
    if storage_class not in STORAGE_CLASSES:
        raise ConfigError(f"{path}: storage_class must be one of {STORAGE_CLASSES}, got {storage_class!r}")
    checked_at_raw = raw.get("checked_at")
    return RegistryStorageQualification(
        storage_class=storage_class,
        qualified_for_primary=bool(raw.get("qualified_for_primary", False)),
        detail=str(raw.get("detail") or ""),
        checked_at=datetime.fromisoformat(str(checked_at_raw)) if checked_at_raw else datetime.min.replace(tzinfo=timezone.utc),
    )


def _write_registry_storage_status(path: Path, qualification: RegistryStorageQualification) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(qualification.to_payload(), handle, sort_keys=False, default_flow_style=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def refresh_registry_storage_status(
    knowledge_root: Path, *, as_of: datetime | None = None
) -> RegistryStorageQualification:
    """Recompute the live storage-class qualification and persist it to
    `registry_capability_status.yaml` (§6.7's "persisted in registry
    capability status"). Deterministic, non-destructive, and independent
    of whether the registry has been bootstrapped yet -- it describes
    `knowledge_root`'s storage medium, not registry content."""
    qualification = qualify_registry_storage(knowledge_root, as_of=as_of)
    _write_registry_storage_status(registry_capability_status_path(knowledge_root), qualification)
    return qualification


def ensure_storage_qualified_for_primary(knowledge_root: Path) -> None:
    """The promotion gate (§6.7: "A conditional/unsupported store cannot
    promote `write_mode` to primary."). Always recomputes live rather than
    trusting a cached status file, and always re-persists the fresh result
    so the on-disk status file reflects the exact qualification the
    promotion decision was made against. Raises `ConfigError` when
    unqualified; callers (PPL-W1.9's flip CLI) must not catch this to
    force a promotion through."""
    qualification = refresh_registry_storage_status(knowledge_root)
    if not qualification.qualified_for_primary:
        raise ConfigError(
            f"Cannot promote registry write_mode to 'primary': {qualification.detail} "
            "Resolve the storage-class issue (move knowledge/ off the unsupported filesystem) before retrying."
        )
