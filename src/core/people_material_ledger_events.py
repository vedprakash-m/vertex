"""specs/people.md Phase 6, PPL-W6.1/PPL-W6.2: real material-ledger event
registration.

§7.6: "Phase 6 registers and emits reviewed material events for affected
active programs: `identity.lifecycle_changed`, existing/extended
`ownership.changed`, `team.membership.changed` when the team/person is in
program scope." Title/display-name/department/non-program-relevant
hierarchy churn stay journal-only (§7.6's own text: "Title, display-name,
department, and non-program-relevant hierarchy churn remain only in the
shared journal unless an explicit consumer later establishes
materiality") -- this module deliberately never emits those.

A thin post-commit step, not folded into `commit_registry_files_transaction`
itself: that Zone A primitive (PPL-W1.5) has many existing callers across
merge/split/bind/unmerge/provider-refresh/delegation-lifecycle, and giving
it a required event parameter would be an invasive signature change for
code most of those callers don't need. Instead this mirrors the
codebase's own established pattern of an explicit step invoked right
after a successful commit (e.g. `people_registry_corrections.py`'s
`_record_correction`, itself called after `commit_registry_files_transaction`
returns, not folded into it).

`identity.lifecycle_changed` (PPL-W6.3b) is now implemented too, once
PPL-W6.3a built the previously-missing trigger site: `people_lifecycle_transitions.py::transition_person_lifecycle_status`,
the first (and, as of this writing, only) write path in this codebase
that transitions `PersonStatus` after initial record creation.
"""

from __future__ import annotations

from pathlib import Path

from src.core.people_directory_schema import PersonStatus
from src.core.people_query import find_registry_program_affiliations
from src.core.people_registry_outbox import enqueue_registry_outbox_items

EVENT_TEAM_MEMBERSHIP_CHANGED = "team.membership.changed"
EVENT_OWNERSHIP_CHANGED = "ownership.changed"
EVENT_IDENTITY_LIFECYCLE_CHANGED = "identity.lifecycle_changed"


def _affected_program_ids(person_entity_id: str, *, knowledge_root: Path) -> tuple[str, ...]:
    affiliations = find_registry_program_affiliations(person_entity_id, knowledge_root=knowledge_root)
    return tuple(sorted({edge.program_id for edge in affiliations}))


def enqueue_team_membership_changed_events(
    knowledge_root: Path,
    *,
    transaction_id: str,
    person_entity_ids: tuple[str, ...],
) -> dict[str, tuple[str, ...]]:
    """One idempotent outbox item per (person, affected active program)
    pair, per §7.6's "when the team/person is in program scope." A
    person with no team memberships carrying `legacy_programs` enqueues
    nothing. Returns the affected program ids actually enqueued, keyed by
    `person_entity_id`, so a caller can log/assert on what happened."""
    enqueued: dict[str, tuple[str, ...]] = {}
    for person_entity_id in person_entity_ids:
        program_ids = _affected_program_ids(person_entity_id, knowledge_root=knowledge_root)
        if program_ids:
            enqueue_registry_outbox_items(
                knowledge_root,
                transaction_id=transaction_id,
                program_ids=program_ids,
                payload={"person_entity_id": person_entity_id},
                event_type=EVENT_TEAM_MEMBERSHIP_CHANGED,
            )
        enqueued[person_entity_id] = program_ids
    return enqueued


def enqueue_ownership_changed_event(
    knowledge_root: Path,
    *,
    transaction_id: str,
    person_entity_id: str,
    new_manager_entity_id: str | None,
) -> tuple[str, ...]:
    """PPL-W6.2: fired when `person_entity_id`'s `manager_entity_id` is
    rewritten (today, only by `people_registry_corrections.py::merge_people`'s
    entity-ref carry-over). Returns the affected program ids actually
    enqueued."""
    program_ids = _affected_program_ids(person_entity_id, knowledge_root=knowledge_root)
    if program_ids:
        enqueue_registry_outbox_items(
            knowledge_root,
            transaction_id=transaction_id,
            program_ids=program_ids,
            payload={"person_entity_id": person_entity_id, "new_manager_entity_id": new_manager_entity_id},
            event_type=EVENT_OWNERSHIP_CHANGED,
        )
    return program_ids


def enqueue_identity_lifecycle_changed_event(
    knowledge_root: Path,
    *,
    transaction_id: str,
    person_entity_id: str,
    from_status: PersonStatus,
    to_status: PersonStatus,
) -> tuple[str, ...]:
    """PPL-W6.3b: fired when `person_entity_id`'s `PersonStatus` is
    transitioned (today, only by `people_lifecycle_transitions.py::transition_person_lifecycle_status`).
    Returns the affected program ids actually enqueued."""
    program_ids = _affected_program_ids(person_entity_id, knowledge_root=knowledge_root)
    if program_ids:
        enqueue_registry_outbox_items(
            knowledge_root,
            transaction_id=transaction_id,
            program_ids=program_ids,
            payload={"person_entity_id": person_entity_id, "from_status": from_status.value, "to_status": to_status.value},
            event_type=EVENT_IDENTITY_LIFECYCLE_CHANGED,
        )
    return program_ids
