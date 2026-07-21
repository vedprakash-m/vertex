"""specs/people.md Phase 3, PPL-W3.1: query surfaces (§8.1/§8.2).

§8.1 names commands this module is the library layer for, none of which
existed before this item (confirmed by grep of `src/commands/kb.py`
before writing this module -- only `people programs`/`overlaps`/`pin`/
`unpin`/`attest`/`merge`/`bind`/`split`/`unmerge` and `registry
bootstrap`/`migrate-shared`/`adopt`/`status` existed, no `people show`/
`find`/`stale`/`conflicts`, no `teams show`/`members` app at all):
`people show`, `people find`, `people stale`, `people conflicts`,
`teams show`, `teams members`.

§8.2's versioned JSON envelope (`schema_version: "people-query.v1"`,
`generation_id`, `as_of`, `items`, `next_cursor`) is built once here
(`QueryEnvelope`/`paginate`) and reused by every query function's CLI
command, rather than each command inventing its own pagination/envelope
shape.

`find_person`/`find_team` resolve `--person`/`--team` through
PPL-W2A.4's `resolve_ref_to_canonical_entity_id` (the "EntityRegistry
resolution ladder" §8.2 names for `people find`'s bounded lookup) against
the shared `entities.yaml`, so `P:<alias>`, `person:<alias>`, a bare
alias, or an already-canonical `person:<ULID>` all resolve identically --
reusing the SAME resolution this session's namespace-bridge item already
proved correct, not a second ad hoc lookup.

`search_people` (`people find`) is explicitly a BOUNDED, SCORED candidate
list, never an automatic binding (§8.2: "Fuzzy matches are candidates
with scores/ambiguity, never automatic bindings") -- exact/prefix/
substring matching over normalized alias and display_name, in that
priority order, capped at `limit`.

`list_stale_people` (`people stale`) implements a v1 placeholder
freshness threshold (`DEFAULT_STALE_FRESHNESS_DAYS`), explicitly NOT the
"configured freshness SLA" §8.3's DIR-03 and §7's `freshness_policy.yaml`
`people_registry` section describe -- that governed config doesn't exist
yet (grepped `vertex/policies/freshness_policy.yaml` before writing this:
no `people_registry` section). DIR-03 (PPL-W3.2) is the item that wires a
real configured SLA in; this function's job is proving the STALENESS
QUERY shape is right, matching this session's established "build the
query/primitive, the harder governed-config version is a later item's
named scope" precedent (e.g. PPL-W2A.3's `DEFAULT_HOT_WINDOW_DAYS`).

`list_conflicts` (`people conflicts --status open|resolved`) reads
`people_conflicts.jsonl` (PPL-W1.7, first real multi-writer populated at
PPL-W2B.1/2B.3/2B.5) and classifies each `quarantined`/`conflict` record
as "resolved" iff a LATER record for the same `entity_id` carries a
`merge`/`split`/`bind`/`unmerge` decision (confirmed these are the exact
decision values every real writer uses by grepping every
`append_people_conflict_record` call site before writing this) --
"resolved" here means "a steward operation subsequently touched this
entity," not a separate resolved-flag this session would need to
introduce and keep in sync.

specs/people.md Phase 3, PPL-W3.3 adds `find_registry_program_affiliations`:
`Team.legacy_programs` (PPL-W2A.2) is ALREADY the typed carrier §7.2's own
text names for exactly this purpose -- "`Team.programs` is parsed into a
temporary `legacy_programs` carrier and emitted as low-precedence
`legacy_team_program` affiliation edges until typed program references
supersede it." This function is that emission: for a resolved person, walk
their active memberships to each team, then each team's `legacy_programs`,
producing `LegacyAffiliationEdge`s (reusing the EXACT type
`people_legacy_affiliation.py`'s Phase 0c scan already emits, not a new
parallel shape) with `relation_type="legacy_team_program"`. This is
additive to, not a replacement for, `find_alias_edges`'s legacy
stakeholder/RACI/owner scan -- a person can have both kinds of edges for
the same program simultaneously, and neither implies the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.core.people_change_journal import STREAM_PEOPLE_CONFLICTS, read_journal_records
from src.core.people_directory_schema import (
    PersonDirectory,
    PersonProfile,
    Team,
    load_people_directory,
    load_people_profiles,
    load_teams,
)
from src.core.people_entity_schema import CanonicalEntity, EntityRedirect, load_entities_document
from src.core.people_legacy_affiliation import LegacyAffiliationEdge
from src.core.people_membership_schema import TeamMembership, read_all_memberships, read_memberships_as_of
from src.core.people_namespace_bridge import normalize_alias_for_lookup, resolve_ref_to_canonical_entity_id

QUERY_SCHEMA_VERSION = "people-query.v1"
DEFAULT_QUERY_LIMIT = 50
#: v1 placeholder pending a real `people_registry` section in
#: `freshness_policy.yaml` (§7, DIR-03's actual scope -- PPL-W3.2).
DEFAULT_STALE_FRESHNESS_DAYS = 90

_RESOLVING_DECISIONS = frozenset({"merge", "split", "bind", "unmerge"})
_OPEN_DECISIONS = frozenset({"quarantined", "conflict"})


@dataclass(frozen=True, slots=True)
class QueryEnvelope:
    generation_id: str | None
    as_of: datetime
    items: tuple[object, ...]
    next_cursor: str | None

    def to_payload(self, item_to_payload) -> dict:
        return {
            "schema_version": QUERY_SCHEMA_VERSION,
            "generation_id": self.generation_id,
            "as_of": self.as_of.astimezone(timezone.utc).isoformat(),
            "items": [item_to_payload(item) for item in self.items],
            "next_cursor": self.next_cursor,
        }


def paginate(items: tuple, *, limit: int = DEFAULT_QUERY_LIMIT, cursor: str | None = None) -> tuple[tuple, str | None]:
    start = 0
    if cursor:
        try:
            start = max(0, int(cursor))
        except ValueError:
            start = 0
    page = items[start : start + limit]
    next_cursor = str(start + limit) if start + limit < len(items) else None
    return page, next_cursor


def _load_entities(knowledge_root: Path) -> tuple[tuple[CanonicalEntity, ...], tuple[EntityRedirect, ...]]:
    document = load_entities_document(knowledge_root / "entities.yaml")
    return (document.entities, document.redirects) if document is not None else ((), ())


@dataclass(frozen=True, slots=True)
class PersonQueryResult:
    entity: CanonicalEntity
    directory: PersonDirectory | None
    profile: PersonProfile | None
    memberships: tuple[TeamMembership, ...]
    resolved_via: str


def find_person(ref: str, *, knowledge_root: Path) -> PersonQueryResult | None:
    entities, redirects = _load_entities(knowledge_root)
    resolution = resolve_ref_to_canonical_entity_id(ref, entities=entities, redirects=redirects)
    if resolution.canonical_entity_id is None:
        return None
    entity = next((e for e in entities if e.entity_id == resolution.canonical_entity_id), None)
    if entity is None or entity.entity_type != "person":
        return None
    people_result = load_people_directory(knowledge_root / "people_directory.yaml")
    directory = next((p for p in (people_result.people if people_result else ()) if p.entity_id == entity.entity_id), None)
    profiles_result = load_people_profiles(knowledge_root / "people_profiles.yaml")
    profile = next((p for p in (profiles_result.profiles if profiles_result else ()) if p.entity_id == entity.entity_id), None)
    memberships = tuple(m for m in read_all_memberships(knowledge_root) if m.person_entity_id == entity.entity_id)
    return PersonQueryResult(entity=entity, directory=directory, profile=profile, memberships=memberships, resolved_via=resolution.resolved_via)


@dataclass(frozen=True, slots=True)
class TeamQueryResult:
    entity: CanonicalEntity
    team: Team | None
    resolved_via: str


def find_team(ref: str, *, knowledge_root: Path) -> TeamQueryResult | None:
    entities, redirects = _load_entities(knowledge_root)
    resolution = resolve_ref_to_canonical_entity_id(ref, entities=entities, redirects=redirects)
    if resolution.canonical_entity_id is None:
        return None
    entity = next((e for e in entities if e.entity_id == resolution.canonical_entity_id), None)
    if entity is None or entity.entity_type != "team":
        return None
    teams_result = load_teams(knowledge_root / "teams.yaml")
    team = next((t for t in (teams_result.teams if teams_result else ()) if t.entity_id == entity.entity_id), None)
    return TeamQueryResult(entity=entity, team=team, resolved_via=resolution.resolved_via)


@dataclass(frozen=True, slots=True)
class TeamMembersResult:
    team: TeamQueryResult
    members: tuple[TeamMembership, ...]


def team_members(ref: str, *, knowledge_root: Path, as_of: datetime | None = None) -> TeamMembersResult | None:
    team_result = find_team(ref, knowledge_root=knowledge_root)
    if team_result is None:
        return None
    memberships = read_memberships_as_of(knowledge_root, as_of=as_of) if as_of is not None else read_all_memberships(knowledge_root)
    members = tuple(m for m in memberships if m.team_entity_id == team_result.entity.entity_id)
    return TeamMembersResult(team=team_result, members=members)


@dataclass(frozen=True, slots=True)
class PersonSearchCandidate:
    entity_id: str
    alias: str
    display_name: str | None
    score: float
    match_kind: str  # "exact" | "prefix" | "substring"


def search_people(text: str, *, knowledge_root: Path, limit: int = 20) -> tuple[PersonSearchCandidate, ...]:
    query = normalize_alias_for_lookup(text) if text.strip() else ""
    if not query:
        return ()
    people_result = load_people_directory(knowledge_root / "people_directory.yaml")
    candidates: list[PersonSearchCandidate] = []
    for person in people_result.people if people_result else ():
        alias_norm = normalize_alias_for_lookup(person.alias) if person.alias.strip() else ""
        display_norm = normalize_alias_for_lookup(person.display_name) if person.display_name else ""
        if query == alias_norm or (display_norm and query == display_norm):
            score, kind = 1.0, "exact"
        elif (alias_norm and alias_norm.startswith(query)) or (display_norm and display_norm.startswith(query)):
            score, kind = 0.8, "prefix"
        elif (alias_norm and query in alias_norm) or (display_norm and query in display_norm):
            score, kind = 0.5, "substring"
        else:
            continue
        candidates.append(
            PersonSearchCandidate(entity_id=person.entity_id, alias=person.alias, display_name=person.display_name, score=score, match_kind=kind)
        )
    candidates.sort(key=lambda c: (-c.score, c.alias))
    return tuple(candidates[:limit])


@dataclass(frozen=True, slots=True)
class StalePersonEntry:
    entity_id: str
    alias: str
    field_name: str
    verified_at: datetime
    age_days: int


def list_stale_people(
    *, knowledge_root: Path, as_of: datetime | None = None, freshness_days: int = DEFAULT_STALE_FRESHNESS_DAYS
) -> tuple[StalePersonEntry, ...]:
    now = as_of or datetime.now(timezone.utc)
    people_result = load_people_directory(knowledge_root / "people_directory.yaml")
    entries: list[StalePersonEntry] = []
    for person in people_result.people if people_result else ():
        for verification in person.verifications:
            age_days = (now - verification.verified_at).days
            if age_days > freshness_days:
                entries.append(
                    StalePersonEntry(
                        entity_id=person.entity_id, alias=person.alias, field_name=verification.field_name,
                        verified_at=verification.verified_at, age_days=age_days,
                    )
                )
        for contact in person.contacts:
            age_days = (now - contact.verified_at).days
            if age_days > freshness_days:
                entries.append(
                    StalePersonEntry(
                        entity_id=person.entity_id, alias=person.alias, field_name=f"contact:{contact.kind.value}",
                        verified_at=contact.verified_at, age_days=age_days,
                    )
                )
    entries.sort(key=lambda e: -e.age_days)
    return tuple(entries)


@dataclass(frozen=True, slots=True)
class ConflictQueryEntry:
    conflict_id: str
    decision: str
    entity_id: str | None
    reason: str
    recorded_at: datetime
    sequence: int
    status: str  # "open" | "resolved"


def list_conflicts(*, knowledge_root: Path, status: str | None = None) -> tuple[ConflictQueryEntry, ...]:
    records = read_journal_records(knowledge_root, STREAM_PEOPLE_CONFLICTS)

    latest_resolution_sequence: dict[str, int] = {}
    for record in records:
        if record.get("decision") in _RESOLVING_DECISIONS and record.get("entity_id"):
            entity_id = str(record["entity_id"])
            sequence = int(record["sequence"])
            latest_resolution_sequence[entity_id] = max(latest_resolution_sequence.get(entity_id, -1), sequence)

    entries: list[ConflictQueryEntry] = []
    for record in records:
        decision = str(record.get("decision") or "")
        if decision not in _OPEN_DECISIONS:
            continue
        entity_id = record.get("entity_id")
        sequence = int(record["sequence"])
        resolved = entity_id is not None and latest_resolution_sequence.get(str(entity_id), -1) > sequence
        entries.append(
            ConflictQueryEntry(
                conflict_id=str(record["conflict_id"]), decision=decision,
                entity_id=str(entity_id) if entity_id else None, reason=str(record.get("reason") or ""),
                recorded_at=datetime.fromisoformat(str(record["recorded_at"])), sequence=sequence,
                status="resolved" if resolved else "open",
            )
        )
    if status is not None:
        entries = [e for e in entries if e.status == status]
    entries.sort(key=lambda e: -e.sequence)
    return tuple(entries)


def find_registry_program_affiliations(ref: str, *, knowledge_root: Path) -> tuple[LegacyAffiliationEdge, ...]:
    """§7.2's `legacy_team_program` affiliation-edge emission (PPL-W3.3):
    resolve `ref` to a canonical person, walk their active memberships to
    each team, then each team's `legacy_programs`, emitting one
    `LegacyAffiliationEdge` per (team, program) pair. Returns `()` for an
    unresolvable ref or a person with no team memberships carrying any
    `legacy_programs` -- never raises, matching every other query
    function in this module."""
    person = find_person(ref, knowledge_root=knowledge_root)
    if person is None:
        return ()
    teams_result = load_teams(knowledge_root / "teams.yaml")
    teams_by_id = {t.entity_id: t for t in (teams_result.teams if teams_result else ())}

    edges: list[LegacyAffiliationEdge] = []
    for membership in person.memberships:
        team = teams_by_id.get(membership.team_entity_id)
        if team is None:
            continue
        for program_id in team.legacy_programs:
            edges.append(
                LegacyAffiliationEdge(
                    alias=person.directory.alias if person.directory is not None else ref,
                    program_id=program_id,
                    relation_type="legacy_team_program",
                    source_path="registry:teams.yaml",
                    workstream_id=None,
                )
            )
    return tuple(edges)
