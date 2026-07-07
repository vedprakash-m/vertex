from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml

from src.core.backup import _hash_file
from src.core.config_loader import PROGRAMS_ROOT


@dataclass(frozen=True, slots=True)
class EvidenceVaultEntry:
    program_id: str
    vault_hash: str
    content_path: Path
    metadata_path: Path


def store_evidence_vault_bytes(
    *,
    program_id: str,
    content_bytes: bytes,
    content_type: str,
    original_filename: str,
    origin_path: str | None,
    programs_root: Path = PROGRAMS_ROOT,
) -> EvidenceVaultEntry:
    digest = hashlib.sha256(content_bytes).hexdigest()
    vault_hash = f"sha256:{digest}"
    content_path, metadata_path = evidence_vault_paths(program_id=program_id, vault_hash=vault_hash, programs_root=programs_root)
    content_path.parent.mkdir(parents=True, exist_ok=True)
    if not content_path.exists():
        content_path.write_bytes(content_bytes)
    if not metadata_path.exists():
        metadata_path.write_text(
            json.dumps(
                {
                    "vault_hash": vault_hash,
                    "content_type": content_type,
                    "original_filename": original_filename,
                    "origin_path": origin_path,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    return EvidenceVaultEntry(
        program_id=program_id,
        vault_hash=vault_hash,
        content_path=content_path,
        metadata_path=metadata_path,
    )


def evidence_vault_entry_status(*, program_id: str, vault_hash: str, programs_root: Path = PROGRAMS_ROOT) -> str:
    content_path, metadata_path = evidence_vault_paths(program_id=program_id, vault_hash=vault_hash, programs_root=programs_root)
    if not content_path.exists() or not metadata_path.exists():
        return "missing"
    metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8")) or {}
    if metadata.get("vault_hash") != vault_hash:
        return "hash_mismatch"
    return "ok" if _hash_file(content_path) == vault_hash else "hash_mismatch"


def load_evidence_vault_entries(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> tuple[EvidenceVaultEntry, ...]:
    evidence_root = programs_root / program_id / "ledger" / "evidence"
    if not evidence_root.exists():
        return ()
    entries: list[EvidenceVaultEntry] = []
    for metadata_path in sorted(evidence_root.glob("**/*.meta.json")):
        hash_value = metadata_path.name.removesuffix(".meta.json")
        content_path = metadata_path.with_name(hash_value)
        if not content_path.exists():
            continue
        metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8")) or {}
        vault_hash = metadata.get("vault_hash")
        if not isinstance(vault_hash, str) or not vault_hash:
            vault_hash = f"sha256:{hash_value}"
        entries.append(
            EvidenceVaultEntry(
                program_id=program_id,
                vault_hash=vault_hash,
                content_path=content_path,
                metadata_path=metadata_path,
            )
        )
    return tuple(entries)


def evidence_vault_paths(*, program_id: str, vault_hash: str, programs_root: Path = PROGRAMS_ROOT) -> tuple[Path, Path]:
    hash_value = vault_hash.removeprefix("sha256:")
    bucket = hash_value[:2]
    evidence_root = programs_root / program_id / "ledger" / "evidence" / bucket
    content_path = evidence_root / hash_value
    metadata_path = evidence_root / f"{hash_value}.meta.json"
    return content_path, metadata_path


def delete_evidence_vault_entry(*, program_id: str, vault_hash: str, programs_root: Path = PROGRAMS_ROOT) -> EvidenceVaultEntry | None:
    """Delete a vault entry's content and metadata files (§10.8 cascade delete).

    Returns the entry descriptor if it existed, None if not found.
    Removes the bucket directory if it becomes empty.
    """
    content_path, metadata_path = evidence_vault_paths(program_id=program_id, vault_hash=vault_hash, programs_root=programs_root)
    if not content_path.exists() and not metadata_path.exists():
        return None
    entry = EvidenceVaultEntry(
        program_id=program_id,
        vault_hash=vault_hash,
        content_path=content_path,
        metadata_path=metadata_path,
    )
    if content_path.exists():
        content_path.unlink()
    if metadata_path.exists():
        metadata_path.unlink()
    bucket_dir = content_path.parent
    if bucket_dir.exists() and not any(bucket_dir.iterdir()):
        os.rmdir(bucket_dir)
    return entry