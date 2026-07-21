"""specs/people.md Phase 3, PPL-W3.4: scoped/content-hash cache (§8.5).

§8.5's own ordering is deliberate: "The FIRST performance lever is
scoped/lazy loading, NOT an immediate source-of-truth migration to
SQLite... persist a disposable `knowledge/.cache/people_registry.sqlite3`
ONLY WHEN BENCHMARK EVIDENCE REQUIRES IT." No benchmark evidence exists
yet -- that is PPL-W3.5's own explicit scope (the 10,000-person/2,000-
team/100-program synthetic fixture and budget measurements), which this
item's own row names as depending on PPL-W3.4, not the reverse. This
module therefore builds the CACHE MECHANISM itself (manifest keying,
atomic rebuild, corruption/staleness detection, DIR-13A/13B's doctor
contract) as a correct, independently-testable primitive -- matching
this session's established "build the primitive first, real production
traffic gets redirected once there is evidence to justify it" precedent
(e.g. PPL-W1.6/PPL-W1.7's outbox/journal, initially built and tested
unwired from `commit_registry_transaction`; PPL-W2A.7's shadow-parity
proof built before PPL-W2B.6's promotion counter consumed it). Real
`people_query.py` read-path traffic is NOT redirected through this cache
in this item -- that switch-over is exactly the kind of evidence-gated
decision §8.5's own text reserves for benchmark results, which PPL-W3.5
produces next.

Cache contents are deliberately minimal for the same reason: a single
SQLite table (`entity_alias_index`) mapping normalized alias -> canonical
`entity_id`/`entity_type`, matching §8.5's own first-listed lever
verbatim ("load identity/alias indexes first"). It is NOT a duplicate
query engine competing with `people_query.py`'s in-memory implementation
-- building a second, parallel query surface without evidence it is
needed would be exactly the "immediate source-of-truth migration to
SQLite" §8.5 warns against.

The manifest (`people_registry_cache_manifest.json`, sibling to the
`.sqlite3` file) is keyed by source content hashes (via the sanctioned
`jsonl_utils.compute_file_checksum` seam, D-18's convention reused
verbatim rather than a second hashing scheme), the cache's own schema
version, and the platform's existing `REGISTRY_COMPILER_VERSION`
(`people_registry_identity.py`) -- reused rather than a second,
parallel "compiler version" concept. Any staleness in ANY of these
triggers a full atomic rebuild (write-temp-then-`os.replace`, both the
`.sqlite3` file and its manifest); a rebuild NEVER reads from or writes
to the cache to repair source YAML -- `rebuild_cache` only ever READS
the already-typed loaders and WRITES the cache, matching §8.5's explicit
"never repair source YAML from the cache."
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import sqlite3
from pathlib import Path

from src.core.jsonl_utils import compute_file_checksum
from src.core.people_directory_schema import load_people_directory, load_teams
from src.core.people_entity_schema import is_legacy_schema_0_entities_document, load_entities_document
from src.core.people_registry_identity import REGISTRY_COMPILER_VERSION

CACHE_SCHEMA_VERSION = "1.0"
_CACHE_DIRNAME = ".cache"
_CACHE_DB_FILENAME = "people_registry.sqlite3"
_CACHE_MANIFEST_FILENAME = "people_registry_cache_manifest.json"
_SOURCE_FILENAMES = ("entities.yaml", "people_directory.yaml", "teams.yaml", "memberships.yaml")


def cache_dir(knowledge_root: Path) -> Path:
    return knowledge_root / _CACHE_DIRNAME


def cache_db_path(knowledge_root: Path) -> Path:
    return cache_dir(knowledge_root) / _CACHE_DB_FILENAME


def cache_manifest_path(knowledge_root: Path) -> Path:
    return cache_dir(knowledge_root) / _CACHE_MANIFEST_FILENAME


@dataclass(frozen=True, slots=True)
class CacheManifest:
    cache_schema_version: str
    compiler_version: str
    source_hashes: tuple[tuple[str, str], ...]
    built_at: datetime

    def to_payload(self) -> dict:
        return {
            "cache_schema_version": self.cache_schema_version,
            "compiler_version": self.compiler_version,
            "source_hashes": dict(self.source_hashes),
            "built_at": self.built_at.astimezone(timezone.utc).isoformat(),
        }

    @staticmethod
    def from_payload(raw: dict) -> "CacheManifest":
        return CacheManifest(
            cache_schema_version=str(raw.get("cache_schema_version") or ""),
            compiler_version=str(raw.get("compiler_version") or ""),
            source_hashes=tuple(sorted((str(k), str(v)) for k, v in (raw.get("source_hashes") or {}).items())),
            built_at=datetime.fromisoformat(str(raw["built_at"])),
        )


@dataclass(frozen=True, slots=True)
class CacheStatus:
    exists: bool
    valid: bool
    manifest: CacheManifest | None
    reason: str  # "missing" | "corrupt_manifest" | "schema_mismatch" | "stale_source" | "valid"


def _existing_source_hashes(knowledge_root: Path) -> tuple[tuple[str, str], ...]:
    hashes: list[tuple[str, str]] = []
    for filename in _SOURCE_FILENAMES:
        path = knowledge_root / filename
        if path.exists():
            hashes.append((filename, compute_file_checksum(path)))
    return tuple(sorted(hashes))


def compute_expected_manifest(knowledge_root: Path, *, as_of: datetime | None = None) -> CacheManifest:
    return CacheManifest(
        cache_schema_version=CACHE_SCHEMA_VERSION,
        compiler_version=REGISTRY_COMPILER_VERSION,
        source_hashes=_existing_source_hashes(knowledge_root),
        built_at=as_of or datetime.now(timezone.utc),
    )


def read_cache_status(knowledge_root: Path) -> CacheStatus:
    manifest_path = cache_manifest_path(knowledge_root)
    db_path = cache_db_path(knowledge_root)
    if not manifest_path.exists() or not db_path.exists():
        return CacheStatus(exists=False, valid=False, manifest=None, reason="missing")

    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        stored = CacheManifest.from_payload(raw)
    except (json.JSONDecodeError, OSError, KeyError, ValueError):
        return CacheStatus(exists=True, valid=False, manifest=None, reason="corrupt_manifest")

    if stored.cache_schema_version != CACHE_SCHEMA_VERSION or stored.compiler_version != REGISTRY_COMPILER_VERSION:
        return CacheStatus(exists=True, valid=False, manifest=stored, reason="schema_mismatch")

    if stored.source_hashes != _existing_source_hashes(knowledge_root):
        return CacheStatus(exists=True, valid=False, manifest=stored, reason="stale_source")

    return CacheStatus(exists=True, valid=True, manifest=stored, reason="valid")


def _build_index_rows(
    knowledge_root: Path,
    *,
    entities: tuple | None = None,
    people: tuple | None = None,
    teams: tuple | None = None,
) -> tuple[tuple[str, str, str], ...]:
    """(normalized_alias, entity_id, entity_type) rows -- §8.5's "identity/
    alias indexes first" lever, sourced from the SAME typed loaders every
    other Phase 2a/3 consumer already uses (no separate parse path).

    specs/people.md PPL-W3.5d: `entities`/`people`/`teams`, when supplied
    (e.g. by `registry_dir13_cache_check` reusing `kb_checks.py`'s own
    already-loaded `_SharedRegistrySnapshot`), skip the fresh disk load
    for that collection entirely -- closing the last of PPL-W3.5c's three
    named redundant one-time full-registry-parse call sites. `None` (the
    default) preserves the exact original always-load-from-disk behavior
    for every other caller, including the exact original failure mode for
    a legacy schema-0 `entities.yaml` (`load_entities_document` raises
    `ConfigError` for it -- not this function's concern to change)."""
    rows: list[tuple[str, str, str]] = []

    if entities is None:
        entities_path = knowledge_root / "entities.yaml"
        # PPL-W3.5d fix: a legacy schema-0 entities.yaml (no schema_version
        # key) previously reached load_entities_document unguarded here,
        # which raises ConfigError for it -- not caught by
        # registry_dir13_cache_check's own `except (OSError, ValueError)`,
        # so a workspace with a legacy shared entities.yaml and a missing/
        # stale cache crashed the ENTIRE `doctor --kb` run uncaught.
        # Confirmed empirically before this fix, not assumed. Mirrors
        # kb_checks.py::_load_shared_registry_snapshot's own already-safe
        # `has_schema2_entities` gate.
        entities_doc = (
            load_entities_document(entities_path)
            if entities_path.exists() and not is_legacy_schema_0_entities_document(entities_path)
            else None
        )
        entities = entities_doc.entities if entities_doc else ()
    for entity in entities:
        for alias in entity.aliases:
            if alias.value.strip():
                rows.append((alias.value.strip().casefold(), entity.entity_id, entity.entity_type))

    if people is None:
        people_result = load_people_directory(knowledge_root / "people_directory.yaml")
        people = people_result.people if people_result else ()
    for person in people:
        if person.alias.strip() and person.entity_id:
            rows.append((person.alias.strip().casefold(), person.entity_id, "person"))

    if teams is None:
        teams_result = load_teams(knowledge_root / "teams.yaml")
        teams = teams_result.teams if teams_result else ()
    for team in teams:
        if team.id.strip() and team.entity_id:
            rows.append((team.id.strip().casefold(), team.entity_id, "team"))

    return tuple(dict.fromkeys(rows))  # de-duplicate while preserving order


def rebuild_cache(
    knowledge_root: Path,
    *,
    as_of: datetime | None = None,
    entities: tuple | None = None,
    people: tuple | None = None,
    teams: tuple | None = None,
) -> CacheManifest:
    """Atomic rebuild: writes a temp `.sqlite3` file and temp manifest,
    then `os.replace`s both into place. Reads ONLY from the already-typed
    source loaders; never reads from or writes to any source YAML file --
    a rebuild cannot "repair" source data even if it wanted to.

    `entities`/`people`/`teams` (PPL-W3.5d) are forwarded to
    `_build_index_rows` unchanged -- see its own docstring."""
    cache_directory = cache_dir(knowledge_root)
    cache_directory.mkdir(parents=True, exist_ok=True)

    manifest = compute_expected_manifest(knowledge_root, as_of=as_of)
    rows = _build_index_rows(knowledge_root, entities=entities, people=people, teams=teams)

    db_path = cache_db_path(knowledge_root)
    temp_db_path = db_path.with_suffix(db_path.suffix + ".tmp")
    if temp_db_path.exists():
        temp_db_path.unlink()
    connection = sqlite3.connect(str(temp_db_path))
    try:
        connection.execute("CREATE TABLE entity_alias_index (alias TEXT NOT NULL, entity_id TEXT NOT NULL, entity_type TEXT NOT NULL)")
        connection.executemany("INSERT INTO entity_alias_index (alias, entity_id, entity_type) VALUES (?, ?, ?)", rows)
        connection.execute("CREATE INDEX idx_entity_alias_index_alias ON entity_alias_index (alias)")
        connection.commit()
    finally:
        connection.close()
    os.replace(temp_db_path, db_path)

    manifest_path = cache_manifest_path(knowledge_root)
    temp_manifest_path = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    temp_manifest_path.write_text(json.dumps(manifest.to_payload(), indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temp_manifest_path, manifest_path)

    return manifest


def ensure_cache_fresh(knowledge_root: Path, *, as_of: datetime | None = None) -> CacheManifest:
    status = read_cache_status(knowledge_root)
    if status.valid:
        assert status.manifest is not None
        return status.manifest
    return rebuild_cache(knowledge_root, as_of=as_of)


def lookup_alias_in_cache(knowledge_root: Path, alias: str) -> tuple[tuple[str, str], ...]:
    """(entity_id, entity_type) pairs for a normalized alias, read from
    the on-disk cache AS-IS -- does not validate freshness or rebuild;
    callers that need a guaranteed-fresh answer should call
    `ensure_cache_fresh` first. Returns `()` if the cache doesn't exist,
    never raises."""
    db_path = cache_db_path(knowledge_root)
    if not db_path.exists():
        return ()
    connection = sqlite3.connect(str(db_path))
    try:
        cursor = connection.execute(
            "SELECT entity_id, entity_type FROM entity_alias_index WHERE alias = ?", (alias.strip().casefold(),)
        )
        return tuple((row[0], row[1]) for row in cursor.fetchall())
    finally:
        connection.close()
