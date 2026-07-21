"""specs/people.md §7.4, PPL-W5a.5: audience precedence pipeline.

§7.4's exact 7-stage order: "Explicit exclusions, tenant/external-guest
policy, active delegation, opt-outs, inactive status, ambiguous identity,
and NudgeAudiencePolicy take precedence in that order." A person excluded
at an earlier stage is never re-evaluated at a later one.

Stage 3, "active delegation" (PPL-W5b.5), is real as of this module's
current form. It never excludes -- §7.6: "delegation changes routing only
and never transfers RACI/accountability" -- so a delegated person's
`person_entity_id` stays in `remaining`/the manifest exactly as if no
delegation existed; the pipeline separately returns a `DelegatedRouting`
entry the caller uses to send to the delegate's own contact instead. Gated
by the `delegation_enabled` kill switch (PPL-W5b.2), checked here via
`load_effective_registry_config` before any delegation lookup -- off is a
true no-op, byte-identical to Phase 5a's own stage-3 pass-through.

One stage remains a deliberately ordered NO-OP PASS-THROUGH:

- Stage 7, "NudgeAudiencePolicy": that policy's real application
  (`_apply_audience_policy` -- `max_recipients` cap, `new_recipient_approval`,
  `unresolved_owner`, `delivery_mode`) lives in `nudge.py` (Zone C, the
  command layer) and is deliberately NOT duplicated into this Zone A
  pipeline -- PPL-W5a.6 wires this pipeline's output into that EXISTING
  function, which still runs LAST exactly as it does today, unchanged.

Stage 1, "explicit exclusions," is already applied upstream by PPL-W5a.2's
`exclude_people` handling inside scope resolution -- this pipeline's
input (an already-resolved candidate list) has that stage's effect baked
in by construction; it is named for completeness in `PRECEDENCE_STAGES`,
not re-applied a second time here.

Stage 4, "opt-outs," IS implemented directly in this module (not deferred
like stage 7), since `NudgeAudiencePolicy.opt_out` is a plain
`frozenset[str]` of aliases -- pure data with no Zone C dependency,
checkable per-candidate without needing the rest of that module's
machinery.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.core.people_delegation_resolution import resolve_active_delegation
from src.core.people_directory_schema import ContactKind, PersonDirectory, PersonStatus, load_people_directory
from src.core.people_registry_modes import load_effective_registry_config

STAGE_EXPLICIT_EXCLUSIONS = "explicit_exclusions"
STAGE_TENANT_GUEST_POLICY = "tenant_guest_policy"
STAGE_ACTIVE_DELEGATION = "active_delegation"
STAGE_OPT_OUTS = "opt_outs"
STAGE_INACTIVE_STATUS = "inactive_status"
STAGE_AMBIGUOUS_IDENTITY = "ambiguous_identity"
STAGE_NUDGE_AUDIENCE_POLICY = "nudge_audience_policy"

#: The exact §7.4 order, for callers that want to render/audit the full
#: stage sequence including the structural (already-applied/deferred) ones.
PRECEDENCE_STAGES = (
    STAGE_EXPLICIT_EXCLUSIONS,
    STAGE_TENANT_GUEST_POLICY,
    STAGE_ACTIVE_DELEGATION,
    STAGE_OPT_OUTS,
    STAGE_INACTIVE_STATUS,
    STAGE_AMBIGUOUS_IDENTITY,
    STAGE_NUDGE_AUDIENCE_POLICY,
)


@dataclass(frozen=True, slots=True)
class PrecedenceExclusion:
    person_entity_id: str
    stage: str
    reason: str


@dataclass(frozen=True, slots=True)
class DelegatedRouting:
    """§7.6: routing changes only -- `person_entity_id` is the ORIGINAL
    accountable person, still present in `apply_precedence_pipeline`'s
    `remaining` output; `delegate_entity_id` is who delivery should
    actually address instead."""

    person_entity_id: str
    delegate_entity_id: str
    delegation_id: str


def _primary_email(person: PersonDirectory | None) -> str | None:
    if person is None:
        return None
    for contact in person.contacts:
        if contact.kind is ContactKind.PRIMARY_EMAIL:
            return contact.value
    return None


def _domain_allowed(email: str, allowed_domains: frozenset[str]) -> bool:
    if not allowed_domains:
        return True
    _, _, domain = email.partition("@")
    return domain.casefold() in {d.casefold() for d in allowed_domains}


def apply_precedence_pipeline(
    person_entity_ids: tuple[str, ...],
    *,
    knowledge_root: Path,
    scope_allow_external_guests: bool,
    allowed_domains: frozenset[str],
    opt_out_aliases: frozenset[str],
    surface: str = "vertex::nudge",
    program_id: str | None = None,
    workstream_id: str | None = None,
    as_of: datetime | None = None,
) -> tuple[tuple[str, ...], tuple[PrecedenceExclusion, ...], tuple[DelegatedRouting, ...]]:
    """Applies stages 2, 3, 4, 5, 6 in order. Stage 1 is structural and
    stage 7 is a deliberate pass-through (see module docstring); neither
    produces output here."""
    people_result = load_people_directory(knowledge_root / "people_directory.yaml")
    people_by_entity_id = {person.entity_id: person for person in (people_result.people if people_result else ())}

    remaining = list(person_entity_ids)
    exclusions: list[PrecedenceExclusion] = []

    # Stage 2: tenant/external-guest policy. External/guest recipients
    # (email domain not in allowed_domains) are denied unless the scope
    # explicitly allows external guests -- the scope-side half of §7.4's
    # AND; the edition-domain-policy half is `allowed_domains` itself,
    # passed in by the caller from the real `NudgeAudiencePolicy`.
    next_remaining: list[str] = []
    for entity_id in remaining:
        email = _primary_email(people_by_entity_id.get(entity_id))
        if email is not None and not _domain_allowed(email, allowed_domains) and not scope_allow_external_guests:
            exclusions.append(
                PrecedenceExclusion(
                    person_entity_id=entity_id, stage=STAGE_TENANT_GUEST_POLICY,
                    reason=f"email domain for {email!r} is not in the allowed domains and the scope does not allow external guests",
                )
            )
            continue
        next_remaining.append(entity_id)
    remaining = next_remaining

    # Stage 3: active delegation. Never excludes -- routing only.
    routings: list[DelegatedRouting] = []
    effective_config = load_effective_registry_config(knowledge_root)
    if effective_config is not None and effective_config.effective_delegation_enabled:
        for entity_id in remaining:
            delegation = resolve_active_delegation(
                entity_id, surface=surface, program_id=program_id, workstream_id=workstream_id,
                knowledge_root=knowledge_root, as_of=as_of,
            )
            if delegation is not None:
                routings.append(
                    DelegatedRouting(
                        person_entity_id=entity_id,
                        delegate_entity_id=delegation.to_person_entity_id,
                        delegation_id=delegation.delegation_id,
                    )
                )

    # Stage 4: opt-outs.
    normalized_opt_outs = {alias.casefold() for alias in opt_out_aliases}
    next_remaining = []
    for entity_id in remaining:
        person = people_by_entity_id.get(entity_id)
        if person is not None and person.alias.casefold() in normalized_opt_outs:
            exclusions.append(PrecedenceExclusion(person_entity_id=entity_id, stage=STAGE_OPT_OUTS, reason="person is on the opt-out list"))
            continue
        next_remaining.append(entity_id)
    remaining = next_remaining

    # Stage 5: inactive status.
    next_remaining = []
    for entity_id in remaining:
        person = people_by_entity_id.get(entity_id)
        if person is not None and person.status is not PersonStatus.ACTIVE:
            exclusions.append(
                PrecedenceExclusion(
                    person_entity_id=entity_id, stage=STAGE_INACTIVE_STATUS,
                    reason=f"person status is {person.status.value!r}, not active",
                )
            )
            continue
        next_remaining.append(entity_id)
    remaining = next_remaining

    # Stage 6: ambiguous identity. A resolved canonical entity with no
    # people_directory.yaml record is an incomplete/ambiguous identity --
    # there is nothing to deliver to.
    next_remaining = []
    for entity_id in remaining:
        if entity_id not in people_by_entity_id:
            exclusions.append(
                PrecedenceExclusion(
                    person_entity_id=entity_id, stage=STAGE_AMBIGUOUS_IDENTITY,
                    reason="no people_directory.yaml record exists for this canonical entity",
                )
            )
            continue
        next_remaining.append(entity_id)
    remaining = next_remaining

    # Stage 7: NudgeAudiencePolicy -- deliberate pass-through, see module docstring.

    return tuple(remaining), tuple(exclusions), tuple(routings)
