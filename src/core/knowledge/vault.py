from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import mimetypes
import os
from pathlib import Path
from typing import Any

import json
import yaml

from src.core.backup import _hash_file
from src.core.exceptions import ConfigError
from src.core.knowledge_store import get_shared_knowledge_root
from src.core.yaml_utils import load_yaml_mapping


PROGRAMS_ROOT = Path(__file__).resolve().parents[3] / "programs"


@dataclass(frozen=True, slots=True)
class KnowledgeVaultEntry:
    scope: str
    vault_hash: str
    content_path: Path
    metadata_path: Path
    content_type: str
    original_filename: str
    origin_path: str
    ingested_at: datetime
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "vault_hash": self.vault_hash,
            "content_path": self.content_path.as_posix(),
            "metadata_path": self.metadata_path.as_posix(),
            "content_type": self.content_type,
            "original_filename": self.original_filename,
            "origin_path": self.origin_path,
            "ingested_at": self.ingested_at.isoformat(),
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class KnowledgeSourceRecord:
    scope: str
    vault_hash: str
    content_type: str
    original_filename: str
    origin_path: str | None
    ingested_at: datetime
    size_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "vault_hash": self.vault_hash,
            "content_type": self.content_type,
            "original_filename": self.original_filename,
            "origin_path": self.origin_path,
            "ingested_at": self.ingested_at.isoformat(),
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class SharedKnowledgeVaultVerifyStatus:
    verified_at: datetime
    ok: bool
    issue_records: tuple[dict[str, object], ...]
    program_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verified_at": self.verified_at.isoformat(),
            "ok": self.ok,
            "issue_records": [dict(record) for record in self.issue_records],
            "program_id": self.program_id,
        }


def ingest_knowledge_source(
    source_path: Path,
    *,
    scope: str,
    programs_root: Path = PROGRAMS_ROOT,
    ingested_at: datetime | None = None,
) -> KnowledgeVaultEntry:
    if not source_path.exists() or not source_path.is_file():
        raise ConfigError(f"Knowledge source file not found: {source_path}")

    knowledge_root = get_shared_knowledge_root(programs_root)
    content_hash = _hash_file(source_path)
    hash_value = _hash_suffix(content_hash)
    bucket = hash_value[:2]
    vault_root = knowledge_root / "vault" / bucket
    vault_root.mkdir(parents=True, exist_ok=True)
    content_path = vault_root / hash_value
    metadata_path = vault_root / f"{hash_value}.meta.json"
    if not content_path.exists():
        content_path.write_bytes(source_path.read_bytes())

    resolved_ingested_at = _ensure_utc(ingested_at or datetime.now(timezone.utc))
    content_type = mimetypes.guess_type(source_path.name)[0] or "application/octet-stream"
    metadata = {
        "vault_hash": content_hash,
        "content_type": content_type,
        "original_filename": source_path.name,
        "origin_path": str(source_path),
        "ingested_at": resolved_ingested_at.isoformat(),
        "size_bytes": source_path.stat().st_size,
    }
    metadata_path.write_text(yaml.safe_dump(metadata, sort_keys=False, allow_unicode=False), encoding="utf-8")

    entry = KnowledgeVaultEntry(
        scope=scope,
        vault_hash=content_hash,
        content_path=content_path,
        metadata_path=metadata_path,
        content_type=content_type,
        original_filename=source_path.name,
        origin_path=str(source_path),
        ingested_at=resolved_ingested_at,
        size_bytes=source_path.stat().st_size,
    )
    _record_scope_source(entry, knowledge_root=knowledge_root)
    return entry


def load_scope_sources(scope: str, *, programs_root: Path = PROGRAMS_ROOT) -> tuple[KnowledgeSourceRecord, ...]:
    knowledge_root = get_shared_knowledge_root(programs_root)
    sources_path = _scope_root(scope, knowledge_root=knowledge_root) / "sources.yaml"
    document = load_yaml_mapping(sources_path, required=False, default={"sources": []})
    sources: list[KnowledgeSourceRecord] = []
    for item in document.get("sources", []):
        if not isinstance(item, dict):
            continue
        ingested_at = item.get("ingested_at")
        if not isinstance(ingested_at, str):
            continue
        sources.append(
            KnowledgeSourceRecord(
                scope=scope,
                vault_hash=str(item.get("vault_hash", "")),
                content_type=str(item.get("content_type", "application/octet-stream")),
                original_filename=str(item.get("original_filename", "")),
                origin_path=str(item["origin_path"]) if isinstance(item.get("origin_path"), str) else None,
                ingested_at=_ensure_utc(datetime.fromisoformat(ingested_at.replace("Z", "+00:00"))),
                size_bytes=int(item.get("size_bytes", 0)),
            )
        )
    return tuple(source for source in sources if source.vault_hash)


def load_all_scope_sources(*, programs_root: Path = PROGRAMS_ROOT) -> tuple[KnowledgeSourceRecord, ...]:
    knowledge_root = get_shared_knowledge_root(programs_root)
    records: list[KnowledgeSourceRecord] = []
    for sources_path in sorted(knowledge_root.glob("**/sources.yaml")):
        scope = _scope_from_sources_path(sources_path, knowledge_root=knowledge_root)
        if scope is None:
            continue
        records.extend(load_scope_sources(scope, programs_root=programs_root))
    return tuple(records)


def load_vault_entry(vault_hash: str, *, programs_root: Path = PROGRAMS_ROOT) -> KnowledgeVaultEntry:
    knowledge_root = get_shared_knowledge_root(programs_root)
    hash_value = _hash_suffix(vault_hash)
    bucket = hash_value[:2]
    content_path = knowledge_root / "vault" / bucket / hash_value
    metadata_path = knowledge_root / "vault" / bucket / f"{hash_value}.meta.json"
    if not content_path.exists() or not metadata_path.exists():
        raise ConfigError(f"Knowledge vault entry not found: {vault_hash}")
    metadata = _load_metadata(metadata_path)
    return KnowledgeVaultEntry(
        scope="",
        vault_hash=vault_hash,
        content_path=content_path,
        metadata_path=metadata_path,
        content_type=str(metadata.get("content_type", "application/octet-stream")),
        original_filename=str(metadata.get("original_filename", content_path.name)),
        origin_path=str(metadata.get("origin_path", "")),
        ingested_at=_ensure_utc(datetime.fromisoformat(str(metadata["ingested_at"]).replace("Z", "+00:00"))),
        size_bytes=int(metadata.get("size_bytes", content_path.stat().st_size)),
    )


def load_all_vault_entries(*, programs_root: Path = PROGRAMS_ROOT) -> tuple[KnowledgeVaultEntry, ...]:
    knowledge_root = get_shared_knowledge_root(programs_root)
    entries: list[KnowledgeVaultEntry] = []
    for metadata_path in sorted((knowledge_root / "vault").glob("**/*.meta.json")):
        metadata = _load_metadata(metadata_path)
        vault_hash = str(metadata.get("vault_hash", ""))
        if not vault_hash:
            continue
        hash_value = _hash_suffix(vault_hash)
        content_path = metadata_path.with_name(hash_value)
        if not content_path.exists():
            continue
        entries.append(
            KnowledgeVaultEntry(
                scope="",
                vault_hash=vault_hash,
                content_path=content_path,
                metadata_path=metadata_path,
                content_type=str(metadata.get("content_type", "application/octet-stream")),
                original_filename=str(metadata.get("original_filename", content_path.name)),
                origin_path=str(metadata.get("origin_path", "")),
                ingested_at=_ensure_utc(datetime.fromisoformat(str(metadata["ingested_at"]).replace("Z", "+00:00"))),
                size_bytes=int(metadata.get("size_bytes", content_path.stat().st_size)),
            )
        )
    return tuple(entries)


def vault_content_matches_metadata(content_path: Path, metadata_path: Path) -> bool:
    if not content_path.exists() or not metadata_path.exists():
        return False
    metadata = _load_metadata(metadata_path)
    vault_hash = metadata.get("vault_hash")
    if not isinstance(vault_hash, str) or not vault_hash:
        return False
    return _hash_file(content_path) == vault_hash


def delete_vault_entry(vault_hash: str, *, programs_root: Path = PROGRAMS_ROOT) -> KnowledgeVaultEntry:
    entry = load_vault_entry(vault_hash, programs_root=programs_root)
    if entry.content_path.exists():
        entry.content_path.unlink()
    if entry.metadata_path.exists():
        entry.metadata_path.unlink()
    _remove_scope_sources(vault_hash, knowledge_root=get_shared_knowledge_root(programs_root))
    for parent in (entry.metadata_path.parent, entry.content_path.parent):
        if parent.exists() and not any(parent.iterdir()):
            os.rmdir(parent)
    return entry


def source_registry_paths_for_vault_hash(vault_hash: str, *, programs_root: Path = PROGRAMS_ROOT) -> tuple[Path, ...]:
    knowledge_root = get_shared_knowledge_root(programs_root)
    matches: list[Path] = []
    for sources_path in sorted(knowledge_root.glob("**/sources.yaml")):
        document = load_yaml_mapping(sources_path, required=False, default={"sources": []})
        raw_sources = document.get("sources", [])
        sources = [item for item in raw_sources if isinstance(item, dict)]
        if any(item.get("vault_hash") == vault_hash for item in sources):
            matches.append(sources_path)
    return tuple(matches)


def load_shared_vault_verify_status(*, programs_root: Path = PROGRAMS_ROOT) -> SharedKnowledgeVaultVerifyStatus | None:
    status_path = _shared_vault_verify_status_path(programs_root=programs_root)
    if not status_path.exists():
        return None
    payload = _load_metadata(status_path)
    verified_at = payload.get("verified_at")
    ok = payload.get("ok")
    issue_records = payload.get("issue_records", [])
    if not isinstance(verified_at, str) or not isinstance(ok, bool) or not isinstance(issue_records, list):
        raise ConfigError(f"Invalid shared vault verify status payload: {status_path}")
    records: list[dict[str, object]] = []
    for item in issue_records:
        if isinstance(item, dict):
            records.append(dict(item))
    program_id = payload.get("program_id")
    return SharedKnowledgeVaultVerifyStatus(
        verified_at=_ensure_utc(datetime.fromisoformat(verified_at.replace("Z", "+00:00"))),
        ok=ok,
        issue_records=tuple(records),
        program_id=program_id if isinstance(program_id, str) else None,
    )


def write_shared_vault_verify_status(
    *,
    verified_at: datetime,
    ok: bool,
    issue_records: list[dict[str, object]] | tuple[dict[str, object], ...],
    programs_root: Path = PROGRAMS_ROOT,
    program_id: str | None = None,
) -> SharedKnowledgeVaultVerifyStatus:
    status = SharedKnowledgeVaultVerifyStatus(
        verified_at=_ensure_utc(verified_at),
        ok=ok,
        issue_records=tuple(dict(record) for record in issue_records),
        program_id=program_id,
    )
    status_path = _shared_vault_verify_status_path(programs_root=programs_root)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(status.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    return status


def _record_scope_source(entry: KnowledgeVaultEntry, *, knowledge_root: Path) -> None:
    scope_root = _scope_root(entry.scope, knowledge_root=knowledge_root)
    sources_path = scope_root / "sources.yaml"
    document = load_yaml_mapping(sources_path, required=False, default={"sources": []})
    raw_sources = document.get("sources", [])
    sources = [item for item in raw_sources if isinstance(item, dict)]
    replacement = {
        "vault_hash": entry.vault_hash,
        "content_type": entry.content_type,
        "original_filename": entry.original_filename,
        "origin_path": entry.origin_path,
        "ingested_at": entry.ingested_at.isoformat(),
        "size_bytes": entry.size_bytes,
    }
    updated = False
    for index, item in enumerate(sources):
        if item.get("vault_hash") == entry.vault_hash:
            sources[index] = replacement
            updated = True
            break
    if not updated:
        sources.append(replacement)
    scope_root.mkdir(parents=True, exist_ok=True)
    sources_path.write_text(yaml.safe_dump({"sources": sources}, sort_keys=False, allow_unicode=False), encoding="utf-8")


def _shared_vault_verify_status_path(*, programs_root: Path) -> Path:
    return get_shared_knowledge_root(programs_root) / ".shared_vault_verify.json"


def _load_metadata(path: Path) -> dict[str, Any]:
    try:
        if path.suffix == ".json":
            return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        pass
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict):
        raise ConfigError(f"Expected mapping metadata in {path}")
    return document


def _remove_scope_sources(vault_hash: str, *, knowledge_root: Path) -> None:
    for sources_path in knowledge_root.glob("**/sources.yaml"):
        document = load_yaml_mapping(sources_path, required=False, default={"sources": []})
        raw_sources = document.get("sources", [])
        sources = [item for item in raw_sources if isinstance(item, dict)]
        filtered = [item for item in sources if item.get("vault_hash") != vault_hash]
        if len(filtered) == len(sources):
            continue
        sources_path.write_text(yaml.safe_dump({"sources": filtered}, sort_keys=False, allow_unicode=False), encoding="utf-8")


def _scope_root(scope: str, *, knowledge_root: Path) -> Path:
    if scope == "org":
        return knowledge_root / "org"
    if scope == "operator":
        return knowledge_root / "operator"
    if ":" not in scope:
        raise ConfigError(f"Unsupported knowledge scope: {scope}")
    family, value = scope.split(":", maxsplit=1)
    if family == "program":
        return knowledge_root / "programs" / value
    if family == "domain":
        return knowledge_root / "domains" / value
    if family == "portfolio":
        return knowledge_root / "portfolio" / value
    raise ConfigError(f"Unsupported knowledge scope: {scope}")


def _scope_from_sources_path(path: Path, *, knowledge_root: Path) -> str | None:
    try:
        relative = path.relative_to(knowledge_root)
    except ValueError:
        return None
    parts = relative.parts
    if parts == ("org", "sources.yaml"):
        return "org"
    if parts == ("operator", "sources.yaml"):
        return "operator"
    if len(parts) == 3 and parts[0] == "programs" and parts[2] == "sources.yaml":
        return f"program:{parts[1]}"
    if len(parts) == 3 and parts[0] == "domains" and parts[2] == "sources.yaml":
        return f"domain:{parts[1]}"
    if len(parts) == 3 and parts[0] == "portfolio" and parts[2] == "sources.yaml":
        return f"portfolio:{parts[1]}"
    return None


def _hash_suffix(content_hash: str) -> str:
    if not content_hash.startswith("sha256:"):
        raise ConfigError(f"Unsupported content hash format: {content_hash}")
    return content_hash.split(":", maxsplit=1)[1]


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)