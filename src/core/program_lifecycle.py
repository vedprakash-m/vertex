"""specs/people.md Phase 3, PPL-W3.3: §8.4's active-reference predicate.

§8.4's DRI-accepted resolution (verbatim, signed off 2026-07-16): "`active`
= has `program.yaml` and at least one configured edition and no explicit
archive marker; `archived` = an explicit operator-authored marker on
`program.yaml`... `inactive` is reserved for a future explicit dormant
state and is not auto-derived from activity recency in v1."

Confirmed via repo-wide grep before writing this module: no
`is_program_active`/`lifecycle_status`/`program_lifecycle` predicate
existed anywhere in the codebase -- every one of ProgramContext,
ProgramReality/FleetReality, readiness, and affiliation/query checks
that needs an active/archived answer today has NONE, not an ad hoc
duplicate. This module is the "one centralized predicate" §8.4 requires,
reused by `FleetReality` (PPL-W3.3's own wiring) rather than copied.

The archive-marker field name itself is explicitly left open by §8.4
("the exact field name... is fixed by the cross-subsystem decision
record, not by this feature") -- `program.yaml`'s `archived: true` and
`lifecycle_status: archived` are both accepted here as the two forms
§8.4's own text names as examples, so this predicate is correct under
either eventual naming choice rather than guessing one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

ProgramLifecycleStatus = str  # "active" | "archived"


@dataclass(frozen=True, slots=True)
class ProgramLifecycleAssessment:
    program_id: str
    status: ProgramLifecycleStatus
    has_program_yaml: bool
    has_configured_edition: bool
    archive_marker_present: bool


def _has_configured_edition(program_dir: Path) -> bool:
    editions_dir = program_dir / "editions"
    if not editions_dir.exists():
        return False
    return any(editions_dir.glob("*.yaml"))


def _archive_marker_present(program_yaml: dict) -> bool:
    if bool(program_yaml.get("archived")):
        return True
    return str(program_yaml.get("lifecycle_status") or "").strip().casefold() == "archived"


def assess_program_lifecycle(program_id: str, *, programs_root: Path) -> ProgramLifecycleAssessment:
    """§8.4's exact v1 predicate, applied to one program. Never raises on
    malformed/missing `program.yaml` -- an assessment always returns
    SOMETHING usable rather than propagating a parse error into every
    consumer this predicate is meant to centralize for."""
    program_dir = programs_root / program_id
    program_yaml_path = program_dir / "program.yaml"
    has_program_yaml = program_yaml_path.exists()
    has_edition = _has_configured_edition(program_dir)

    archive_marker = False
    if has_program_yaml:
        try:
            raw = yaml.safe_load(program_yaml_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            raw = {}
        if isinstance(raw, dict):
            archive_marker = _archive_marker_present(raw)

    status: ProgramLifecycleStatus = "archived" if archive_marker else ("active" if has_program_yaml and has_edition else "archived")
    return ProgramLifecycleAssessment(
        program_id=program_id, status=status, has_program_yaml=has_program_yaml,
        has_configured_edition=has_edition, archive_marker_present=archive_marker,
    )


def is_program_active(program_id: str, *, programs_root: Path) -> bool:
    """The centralized boolean §8.4 asks ProgramContext/FleetReality/
    readiness/audience checks to share. A program with no `program.yaml`
    or no configured edition is treated as not-active (the v1 predicate's
    own "has program.yaml and at least one configured edition" clause),
    not as a distinct third state -- `inactive` is explicitly reserved
    for a future explicit dormant marker, not derived here."""
    return assess_program_lifecycle(program_id, programs_root=programs_root).status == "active"
