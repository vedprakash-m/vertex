"""specs/people.md §7.5, PPL-W5a.3: audience-critical freshness gating.

§7.5 names audience-critical freshness fields as "active status, verified
delivery-eligible contact, identity binding, tenant relationship, and
membership" -- "a stricter default than informational profile freshness."
Identity binding needs no separate check here: a candidate only exists
because it already resolved to a canonical `entity_id` (PPL-W5a.2), so
binding freshness is implicit in having reached this stage at all.

Reuses `people_query.py::list_stale_people` -- the SAME per-field
staleness primitive DIR-03 (PPL-W3.2) already established -- rather than
a second staleness engine; this module narrows that primitive's output
to the audience-critical field subset and adds the one signal
`list_stale_people` doesn't cover: membership verification age, read
directly from the specific `TeamMembership` that made a candidate
eligible (PPL-W5a.2's `AudienceCandidate.source_team_entity_id`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.core.audience_scope_resolver import AudienceCandidate
from src.core.people_membership_schema import read_all_memberships
from src.core.people_query import list_stale_people

#: §7.5's audience-critical PERSON fields (membership is handled
#: separately below, since it isn't a person-record verification).
#: `contact:*`-prefixed field names (from `list_stale_people`) are
#: matched by prefix, not listed here, since the contact KIND varies.
AUDIENCE_CRITICAL_PERSON_FIELDS = frozenset({"status", "tenant_relationship"})


@dataclass(frozen=True, slots=True)
class FreshnessExclusion:
    person_entity_id: str
    field_name: str
    age_days: int
    threshold_days: int


def filter_candidates_by_freshness(
    candidates: tuple[AudienceCandidate, ...],
    *,
    knowledge_root: Path,
    require_verified_within_days: int | None,
    as_of: datetime | None = None,
) -> tuple[tuple[AudienceCandidate, ...], tuple[FreshnessExclusion, ...]]:
    """No threshold configured (`None`) is a true no-op -- every candidate
    passes unchanged, matching §7.4's scope-optional field semantics."""
    if require_verified_within_days is None:
        return candidates, ()
    now = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)

    stale_entries = list_stale_people(knowledge_root=knowledge_root, as_of=now, freshness_days=require_verified_within_days)
    worst_stale_by_entity_id: dict[str, tuple[str, int]] = {}
    for entry in stale_entries:
        if entry.field_name not in AUDIENCE_CRITICAL_PERSON_FIELDS and not entry.field_name.startswith("contact:"):
            continue
        existing = worst_stale_by_entity_id.get(entry.entity_id)
        if existing is None or entry.age_days > existing[1]:
            worst_stale_by_entity_id[entry.entity_id] = (entry.field_name, entry.age_days)

    membership_verified_at = {
        (membership.person_entity_id, membership.team_entity_id): membership.verified_at
        for membership in read_all_memberships(knowledge_root)
    }

    fresh: list[AudienceCandidate] = []
    exclusions: list[FreshnessExclusion] = []
    for candidate in candidates:
        stale = worst_stale_by_entity_id.get(candidate.person_entity_id)
        if stale is not None:
            field_name, age_days = stale
            exclusions.append(
                FreshnessExclusion(
                    person_entity_id=candidate.person_entity_id, field_name=field_name,
                    age_days=age_days, threshold_days=require_verified_within_days,
                )
            )
            continue
        if candidate.source_team_entity_id is not None:
            verified_at = membership_verified_at.get((candidate.person_entity_id, candidate.source_team_entity_id))
            if verified_at is not None:
                age_days = (now - verified_at.astimezone(timezone.utc)).days
                if age_days > require_verified_within_days:
                    exclusions.append(
                        FreshnessExclusion(
                            person_entity_id=candidate.person_entity_id, field_name="membership",
                            age_days=age_days, threshold_days=require_verified_within_days,
                        )
                    )
                    continue
        fresh.append(candidate)
    return tuple(fresh), tuple(exclusions)
