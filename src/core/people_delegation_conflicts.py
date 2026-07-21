"""specs/people.md Phase 5b, PPL-W5b.3: delegation overlap-conflict detection.

§7.6's exact rule: "Overlapping active delegations for the same
source/surface/program/workstream are conflicts and block routing until
resolved." A pure function -- no I/O, no registry access -- comparing a
candidate delegation against a caller-supplied list of existing
delegations for the same `from_person_entity_id`. This item deliberately
does not wire rejection into `people_delegation_lifecycle.py::create_delegation`:
§7.6 says overlap "block[s] routing," not creation, so two delegations MAY
coexist unresolved -- it is PPL-W5b.4's resolution engine that consumes
this module's output and returns `None` (routing falls back to the
original person) rather than guessing which of several conflicting
delegations wins.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.people_delegation_schema import Delegation, DelegationStatus


@dataclass(frozen=True, slots=True)
class DelegationOverlap:
    other: Delegation
    overlapping_surfaces: tuple[str, ...]


def _windows_overlap(candidate: Delegation, other: Delegation) -> bool:
    return candidate.valid_from <= other.valid_until and other.valid_from <= candidate.valid_until


def _scopes_intersect(candidate_ids: tuple[str, ...], other_ids: tuple[str, ...]) -> bool:
    """§7.6: "Empty program/workstream scopes mean all otherwise-authorized
    scopes for the person" -- an empty scope intersects any scope,
    including another empty one."""
    if not candidate_ids or not other_ids:
        return True
    return bool(set(candidate_ids) & set(other_ids))


def find_overlapping_delegations(
    candidate: Delegation,
    existing: tuple[Delegation, ...],
) -> tuple[DelegationOverlap, ...]:
    """Only ACTIVE delegations for the SAME `from_person_entity_id` are
    compared -- a revoked/expired delegation, a different source person,
    or `candidate` itself (matched by `delegation_id`, so re-checking an
    already-persisted delegation against its siblings is safe) can never
    conflict."""
    conflicts: list[DelegationOverlap] = []
    for other in existing:
        if other.delegation_id == candidate.delegation_id:
            continue
        if other.status is not DelegationStatus.ACTIVE:
            continue
        if other.from_person_entity_id != candidate.from_person_entity_id:
            continue
        overlapping_surfaces = tuple(sorted(set(candidate.surfaces) & set(other.surfaces)))
        if not overlapping_surfaces:
            continue
        if not _windows_overlap(candidate, other):
            continue
        if not _scopes_intersect(candidate.program_ids, other.program_ids):
            continue
        if not _scopes_intersect(candidate.workstream_ids, other.workstream_ids):
            continue
        conflicts.append(DelegationOverlap(other=other, overlapping_surfaces=overlapping_surfaces))
    return tuple(conflicts)


def has_overlapping_delegation(candidate: Delegation, existing: tuple[Delegation, ...]) -> bool:
    return bool(find_overlapping_delegations(candidate, existing))
