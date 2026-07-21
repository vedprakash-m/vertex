"""specs/people.md §7.4, PPL-W5a.4: provenance-rich audience manifest.

"Provenance-rich audience manifest" is §7.4's own phrase. This module
gives every candidate a NAMED disposition and reason (included /
excluded by explicit `exclude_people` / excluded by stale freshness),
combining PPL-W5a.2's resolution and PPL-W5a.3's freshness gate into one
typed, inspectable record per nudge send -- not just a final email list.

Deliberately does not attempt the full 7-stage precedence pipeline
(`NudgeAudiencePolicy`, delegation, opt-outs, tenant/guest policy) --
that is PPL-W5a.5's scope. This manifest is built from `AudienceScope`
resolution alone; PPL-W5a.5's pipeline consumes an ALREADY-built
manifest's included candidates as its own starting input, rather than
this module trying to anticipate stages it doesn't own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.core.audience_freshness import filter_candidates_by_freshness
from src.core.audience_scope_resolver import resolve_audience_scope_with_exclusions
from src.core.audience_scopes import AudienceScope

DISPOSITION_INCLUDED = "included"
DISPOSITION_EXCLUDED_EXPLICIT = "excluded_explicit"
DISPOSITION_EXCLUDED_FRESHNESS = "excluded_freshness"


@dataclass(frozen=True, slots=True)
class AudienceManifestEntry:
    person_entity_id: str
    scope_id: str
    disposition: str  # DISPOSITION_*
    reason: str
    source: str | None = None  # AudienceCandidate.source, for included/freshness-excluded entries
    source_team_entity_id: str | None = None


@dataclass(frozen=True, slots=True)
class AudienceManifest:
    generated_at: datetime
    program_id: str
    entries: tuple[AudienceManifestEntry, ...]

    @property
    def included_person_entity_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(entry.person_entity_id for entry in self.entries if entry.disposition == DISPOSITION_INCLUDED))


def build_audience_manifest(
    scopes: tuple[AudienceScope, ...],
    *,
    program_id: str,
    knowledge_root: Path,
    allow_non_org_team_expansion: bool = False,
    as_of: datetime | None = None,
) -> AudienceManifest:
    now = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
    entries: list[AudienceManifestEntry] = []

    for scope in scopes:
        candidates, excluded_explicit = resolve_audience_scope_with_exclusions(
            scope, knowledge_root=knowledge_root, allow_non_org_team_expansion=allow_non_org_team_expansion,
        )
        fresh, freshness_exclusions = filter_candidates_by_freshness(
            candidates, knowledge_root=knowledge_root, require_verified_within_days=scope.require_verified_within_days, as_of=now,
        )
        for candidate in fresh:
            entries.append(
                AudienceManifestEntry(
                    person_entity_id=candidate.person_entity_id, scope_id=scope.id, disposition=DISPOSITION_INCLUDED,
                    reason="resolved and within freshness threshold", source=candidate.source,
                    source_team_entity_id=candidate.source_team_entity_id,
                )
            )
        for exclusion in freshness_exclusions:
            entries.append(
                AudienceManifestEntry(
                    person_entity_id=exclusion.person_entity_id, scope_id=scope.id, disposition=DISPOSITION_EXCLUDED_FRESHNESS,
                    reason=f"{exclusion.field_name} verification is {exclusion.age_days}d old (threshold {exclusion.threshold_days}d)",
                )
            )
        for person_entity_id in excluded_explicit:
            entries.append(
                AudienceManifestEntry(
                    person_entity_id=person_entity_id, scope_id=scope.id, disposition=DISPOSITION_EXCLUDED_EXPLICIT,
                    reason="explicit exclude_people entry",
                )
            )

    return AudienceManifest(generated_at=now, program_id=program_id, entries=tuple(entries))
