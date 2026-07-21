"""specs/people.md §7.4, PPL-W5a.6: bridges PPL-W5a.1-5a.5's audience-scope
pipeline (scope load -> manifest -> precedence) to `nudge_models.py`'s
`ResolvedRecipient` shape, so `nudge.py` (Zone C, the command layer) only
needs a thin call site rather than hosting this orchestration logic
itself -- kept here, not in `nudge.py`, per this repo's own enforced
`nudge.py` LOC budget (`tests/contracts/test_nudge_contracts.py::test_nc8_nudge_py_loc_budget`,
"Move logic to src/core/ modules"), which a first draft of this exact
change tripped and corrected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from src.core.audience_manifest import build_audience_manifest
from src.core.audience_precedence import apply_precedence_pipeline
from src.core.audience_scopes import load_audience_scopes
from src.core.exceptions import ConfigError
from src.core.knowledge_store import get_shared_knowledge_root
from src.core.nudge_models import NudgeAudiencePolicy, NudgeDeliveryConfig, ResolvedRecipient
from src.core.people_directory_schema import ContactKind, load_people_directory
from src.core.people_registry_modes import load_effective_registry_config


def resolve_audience_scope_recipients(
    *,
    program_id: str,
    delivery: NudgeDeliveryConfig,
    audience_policy: NudgeAudiencePolicy | None,
    programs_root: Path,
    is_valid_email: Callable[[str], bool],
) -> list[ResolvedRecipient]:
    """Empty `delivery.audience_scope_ids` (every edition/nudge config
    that predates this item) is a true no-op -- returns `[]` immediately,
    never touching the shared registry. Also a true no-op when the
    `audience_scopes_enabled` registry kill switch (PPL-W1.9) is off --
    that flag has existed since Phase 1 but PPL-W5a.6's first draft never
    actually consulted it, a real gap found and fixed here rather than
    left in place; matches `identity_provider_refresh.py::refresh_people_from_provider`'s
    own `effective_provider_refresh_enabled` precedent exactly. `is_valid_email`
    is the caller's own validity check (`nudge.py::_is_valid_email`), passed in
    rather than duplicated, so this module stays the single source of
    email-validity logic that lives in `nudge.py` today."""
    if not delivery.audience_scope_ids:
        return []
    knowledge_root = get_shared_knowledge_root(programs_root)
    effective_config = load_effective_registry_config(knowledge_root)
    if effective_config is None or not effective_config.effective_audience_scopes_enabled:
        return []
    all_scopes = load_audience_scopes(program_id=program_id, programs_root=programs_root)
    selected_scopes = tuple(scope for scope in all_scopes if scope.id in delivery.audience_scope_ids)
    missing = set(delivery.audience_scope_ids) - {scope.id for scope in selected_scopes}
    if missing:
        raise ConfigError(f"audience_scope_ids references undefined audience scope(s): {sorted(missing)!r} for program {program_id!r}.")

    manifest = build_audience_manifest(selected_scopes, program_id=program_id, knowledge_root=knowledge_root)
    allowed_domains = frozenset(audience_policy.allowed_domains) if audience_policy is not None else frozenset()
    opt_out_aliases = audience_policy.opt_out if audience_policy is not None else frozenset()
    scope_allow_external_guests = any(scope.allow_external_guests for scope in selected_scopes)
    remaining_entity_ids, _, routings = apply_precedence_pipeline(
        manifest.included_person_entity_ids, knowledge_root=knowledge_root,
        scope_allow_external_guests=scope_allow_external_guests,
        allowed_domains=allowed_domains, opt_out_aliases=opt_out_aliases,
        program_id=program_id,
    )
    delegate_by_person_entity_id = {routing.person_entity_id: routing.delegate_entity_id for routing in routings}

    people_result = load_people_directory(knowledge_root / "people_directory.yaml")
    people_by_entity_id = {person.entity_id: person for person in (people_result.people if people_result else ())}
    recipients: list[ResolvedRecipient] = []
    for entity_id in remaining_entity_ids:
        # §7.6: delegation changes routing only. When an active delegation
        # resolved for this person, address delivery to the delegate's own
        # contact; the manifest above still names the ORIGINAL person
        # (`entity_id`, untouched by stage 3). A delegate with no directory
        # record or no valid email falls back to the original person's own
        # contact rather than silently dropping the recipient.
        delegate_entity_id = delegate_by_person_entity_id.get(entity_id)
        candidate_entity_ids = (delegate_entity_id, entity_id) if delegate_entity_id else (entity_id,)
        person = None
        email = None
        for candidate_entity_id in candidate_entity_ids:
            candidate_person = people_by_entity_id.get(candidate_entity_id)
            if candidate_person is None:
                continue
            candidate_email = next((contact.value for contact in candidate_person.contacts if contact.kind is ContactKind.PRIMARY_EMAIL), None)
            if candidate_email is not None and is_valid_email(candidate_email):
                person, email = candidate_person, candidate_email
                break
        if person is None or email is None:
            continue
        recipients.append(ResolvedRecipient(alias=person.alias, email=email.strip().lower(), display_name=person.display_name or person.alias))
    return recipients


def merge_audience_scope_recipients(
    recipients: list[ResolvedRecipient], audience_scope_recipients: list[ResolvedRecipient],
) -> list[ResolvedRecipient]:
    """§7.4's "Scope resolution adds candidates to the existing recipient
    model" -- additive only, never removes or reorders an existing
    explicit recipient."""
    if not audience_scope_recipients:
        return recipients
    seen = {recipient.email.lower() for recipient in recipients}
    merged = list(recipients)
    for candidate in audience_scope_recipients:
        key = candidate.email.lower()
        if key not in seen:
            seen.add(key)
            merged.append(candidate)
    return merged
