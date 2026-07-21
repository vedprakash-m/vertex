"""specs/people.md Phase 5b, PPL-W5b.4: delegation resolution engine.

`resolve_active_delegation` is deliberately depth-one by construction:
it only ever reads `from_person_entity_id`'s own outbound delegations
via `load_delegations`, and never re-queries using a resolved delegate's
`entity_id` -- there is no recursive call anywhere in this module, so a
delegate's own outbound delegation structurally cannot be followed
(§7.6: "Delegation depth is one: a delegate's own delegation is not
followed transitively").

Returns `None` (routing falls back to the original person) for: no
matching delegation, an expired/revoked one, a wrong-surface one, an
out-of-scope one, or an unresolved overlap conflict per PPL-W5b.3's own
rule -- never guessing which of several conflicting delegations wins.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core.people_delegation_schema import Delegation, DelegationStatus, delegations_path, load_delegations


def _in_scope(query_id: str | None, delegation_scope_ids: tuple[str, ...]) -> bool:
    """§7.6: "Empty program/workstream scopes mean all otherwise-authorized
    scopes for the person; populated scopes narrow the delegation." A
    delegation narrowed to specific scope ids does not apply when the
    caller resolves without that scope context (`query_id=None`)."""
    if not delegation_scope_ids:
        return True
    if query_id is None:
        return False
    return query_id in delegation_scope_ids


def resolve_active_delegation(
    from_person_entity_id: str,
    *,
    surface: str,
    program_id: str | None = None,
    workstream_id: str | None = None,
    knowledge_root: Path,
    as_of: datetime | None = None,
) -> Delegation | None:
    now = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
    all_delegations = load_delegations(delegations_path(knowledge_root))
    candidates = tuple(
        delegation
        for delegation in all_delegations
        if delegation.from_person_entity_id == from_person_entity_id
        and delegation.status is DelegationStatus.ACTIVE
        and surface in delegation.surfaces
        and delegation.valid_from <= now <= delegation.valid_until
        and _in_scope(program_id, delegation.program_ids)
        and _in_scope(workstream_id, delegation.workstream_ids)
    )
    if len(candidates) == 1:
        return candidates[0]
    # Zero candidates: no delegation applies. More than one: every
    # candidate already matched the identical surface/as-of/scope
    # predicate above, so by PPL-W5b.3's own overlap rule (same source,
    # overlapping surface, overlapping window, intersecting scope) any
    # two of them necessarily conflict -- refuse to guess which wins
    # rather than picking one arbitrarily.
    return None
