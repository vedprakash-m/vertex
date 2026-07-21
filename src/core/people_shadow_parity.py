"""specs/people.md Phase 2a, PPL-W2A.7: shadow-mode parity proof.

§6.6: "`shadow`: return the legacy result while compiling canonical v2
in parallel and recording parity." This module is the actual dual-
computation + comparison PPL-W1.9's `write_mode`/`program_modes` gate
was built to eventually drive, using every schema/dual-read module
Phase 2a shipped (PPL-W2A.1-2A.6) against the SAME on-disk files the
legacy loader already reads.

This is not a simulation: `people_directory_schema.load_people_directory`/
`load_teams` (PPL-W2A.2) read the identical top-level `people`/`teams`
YAML keys `knowledge_store.py`'s legacy loader does (confirmed by direct
comparison of both parsers before writing this module) -- calling them
directly against a real program's `people_directory.yaml`/`teams.yaml`
IS "compiling canonical v2 in parallel," not a separate synthetic
exercise. Every real program's knowledge files today are still legacy-
shaped (no `entity_id`, no typed `contacts`/`verifications`), so the v2
loader parses them entirely through its dual-read fallback path (WARN
diagnostics, honest placeholders) -- the parity proof is exactly that
the ALIAS/ID identity surface both views expose is identical, even
though the v2 view's field-level detail is currently thin.

§9.1's exact verification bar: "zero divergence on a representative
existing program's data before any program can request `primary`."
`is_zero_divergence` is the gate PPL-W2B.6's later five-clean-cycle
promotion logic is expected to consult -- this item proves the
COMPARISON is correct and currently green on a real program, not the
full clean-cycle counter/promotion state machine (that remains
PPL-W2B.6's explicit scope, per its own row's text).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.core.knowledge_store import get_shared_knowledge_root, load_program_knowledge
from src.core.people_directory_schema import load_people_directory, load_teams
from src.core.people_registry_diagnostics import RegistryDiagnostic
from src.core.people_registry_modes import load_effective_registry_config


@dataclass(frozen=True, slots=True)
class ShadowParityDivergence:
    kind: str  # "person_alias_missing_in_canonical" | "person_alias_missing_in_legacy" | "team_id_missing_in_canonical" | "team_id_missing_in_legacy"
    key: str
    detail: str


@dataclass(frozen=True, slots=True)
class ShadowParityRecord:
    program_id: str
    checked_at: datetime
    legacy_person_count: int
    canonical_person_count: int
    legacy_team_count: int
    canonical_team_count: int
    divergences: tuple[ShadowParityDivergence, ...]
    diagnostics: tuple[RegistryDiagnostic, ...]

    @property
    def is_zero_divergence(self) -> bool:
        return not self.divergences

    def to_payload(self) -> dict:
        return {
            "program_id": self.program_id,
            "checked_at": self.checked_at.isoformat(),
            "legacy_person_count": self.legacy_person_count,
            "canonical_person_count": self.canonical_person_count,
            "legacy_team_count": self.legacy_team_count,
            "canonical_team_count": self.canonical_team_count,
            "is_zero_divergence": self.is_zero_divergence,
            "divergences": [{"kind": d.kind, "key": d.key, "detail": d.detail} for d in self.divergences],
            "diagnostic_count": len(self.diagnostics),
        }


def _normalize_key(value: str) -> str:
    return value.strip().casefold()


def _resolve_knowledge_file(program_id: str, filename: str, *, programs_root: Path) -> Path:
    """Mirrors `knowledge_store.py::load_program_knowledge`'s EXACT
    per-file resolution order (shared workspace root first, program-scope
    as fallback only when the shared root's copy is absent) -- confirmed
    by direct comparison of `_load_optional_yaml`'s primary/fallback
    order before writing this function. A real bug was caught here during
    live-verification against real local program data before this test
    suite was written: an earlier draft only checked program scope,
    silently reporting hundreds of false "missing in canonical" divergences
    because production programs keep their people/team data in the shared
    root, not program-scope files that don't exist at all."""
    shared_path = get_shared_knowledge_root(programs_root) / filename
    if shared_path.exists():
        return shared_path
    return programs_root / program_id / "knowledge" / filename


def compute_shadow_parity(program_id: str, *, programs_root: Path, as_of: datetime | None = None) -> ShadowParityRecord:
    now = as_of or datetime.now(timezone.utc)

    legacy = load_program_knowledge(program_id, programs_root=programs_root)
    legacy_aliases = {_normalize_key(p.alias) for p in legacy.people_directory if p.alias}
    legacy_team_ids = {_normalize_key(t.id) for t in legacy.teams if t.id}

    canonical_people = load_people_directory(_resolve_knowledge_file(program_id, "people_directory.yaml", programs_root=programs_root))
    canonical_teams = load_teams(_resolve_knowledge_file(program_id, "teams.yaml", programs_root=programs_root))
    canonical_aliases = {_normalize_key(p.alias) for p in (canonical_people.people if canonical_people else ()) if p.alias}
    canonical_team_ids = {_normalize_key(t.id) for t in (canonical_teams.teams if canonical_teams else ()) if t.id}

    divergences: list[ShadowParityDivergence] = []
    for alias in sorted(legacy_aliases - canonical_aliases):
        divergences.append(ShadowParityDivergence(
            kind="person_alias_missing_in_canonical", key=alias,
            detail=f"alias {alias!r} present in legacy people_directory.yaml but not resolved by the canonical v2 loader",
        ))
    for alias in sorted(canonical_aliases - legacy_aliases):
        divergences.append(ShadowParityDivergence(
            kind="person_alias_missing_in_legacy", key=alias,
            detail=f"alias {alias!r} present in the canonical v2 view but not in the legacy loader's result",
        ))
    for team_id in sorted(legacy_team_ids - canonical_team_ids):
        divergences.append(ShadowParityDivergence(
            kind="team_id_missing_in_canonical", key=team_id,
            detail=f"team id {team_id!r} present in legacy teams.yaml but not resolved by the canonical v2 loader",
        ))
    for team_id in sorted(canonical_team_ids - legacy_team_ids):
        divergences.append(ShadowParityDivergence(
            kind="team_id_missing_in_legacy", key=team_id,
            detail=f"team id {team_id!r} present in the canonical v2 view but not in the legacy loader's result",
        ))

    diagnostics = tuple(
        (canonical_people.diagnostics if canonical_people else ()) + (canonical_teams.diagnostics if canonical_teams else ())
    )

    return ShadowParityRecord(
        program_id=program_id,
        checked_at=now,
        legacy_person_count=len(legacy.people_directory),
        canonical_person_count=len(canonical_people.people) if canonical_people else 0,
        legacy_team_count=len(legacy.teams),
        canonical_team_count=len(canonical_teams.teams) if canonical_teams else 0,
        divergences=tuple(divergences),
        diagnostics=diagnostics,
    )


def shadow_parity_status_path(programs_root: Path, program_id: str) -> Path:
    return programs_root / program_id / "knowledge" / ".state" / "shadow_parity.json"


def record_shadow_parity(record: ShadowParityRecord, *, programs_root: Path) -> Path:
    """Persists the LATEST parity check as current state (not an
    append-only journal -- PPL-W2B.6's five-clean-cycle promotion gate is
    the consumer that turns a sequence of these into a counter; this item
    proves one check's correctness, not the counter's persistence
    format)."""
    path = shadow_parity_status_path(programs_root, record.program_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(record.to_payload(), handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)
    return path


def compute_and_record_shadow_parity_if_in_shadow_mode(
    program_id: str, *, programs_root: Path, as_of: datetime | None = None
) -> ShadowParityRecord | None:
    """§6.6 PPL-W1.9 wiring: only compiles/records parity when this
    program's EFFECTIVE mode (kill switches included, PPL-W1.9) is
    `shadow` or `primary` -- a `legacy`-mode program does not pay the
    double-compile cost, matching §6.6's read-semantics table ("`legacy`:
    preserve the current loader's ... behavior exactly")."""
    knowledge_root = get_shared_knowledge_root(programs_root)
    effective = load_effective_registry_config(knowledge_root)
    if effective is None:
        return None  # Registry not bootstrapped -- nothing to gate on yet.
    mode = effective.effective_program_mode(program_id)
    if mode == "legacy":
        return None

    record = compute_shadow_parity(program_id, programs_root=programs_root, as_of=as_of)
    record_shadow_parity(record, programs_root=programs_root)
    return record
