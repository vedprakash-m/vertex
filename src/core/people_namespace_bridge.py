"""specs/people.md Phase 2a, PPL-W2A.4: namespace bridge and legacy
ledger mapping.

§7.2a: "The entity namespace bridge dual-reads legacy `P:<alias>`/
`person:<alias>` and canonical refs. New machine-authored events write
canonical IDs after shadow parity; historical events are never rewritten
solely for alias migration." Reuses the EXISTING `EntityNsMapper`
(`src/core/ledger/entity_ns.py`, §6.2) for the `P:<alias>` <-> `person:<alias>`
half of the bridge rather than reimplementing it -- that module already
does exactly this translation, just not yet wired to canonical
`entity_id` resolution. This module adds the missing third leg: resolving
either legacy form (or an already-canonical `person:<ULID>`/`team:<ULID>`
ref) to the CURRENT canonical `entity_id`, following `EntityRedirect`s
and alias-rename history along the way.

§7.2a's normalization ladder (exact quoted list): "Unicode normalization
plus casefold for aliases/names, provider-subject exact matching for
authority, conservative email/UPN normalization, IDN-safe domain
comparison, and email-header/control-character validation. Email local
parts are not blindly casefolded as an identity authority." Implemented
literally: `normalize_alias_for_lookup` (NFC + casefold, for
alias/display-name matching only -- never for provider-subject exact
matching, which must stay byte-exact); `normalize_email_for_lookup`
(NFC-normalizes and IDNA-encodes the domain, leaves the local part's
case untouched since email local parts are case-sensitive by RFC and
§7.2a explicitly forbids treating case-folding as an identity authority
there); `reject_header_injection_risk` (a real security check -- refuses
any value containing a carriage return, line feed, or other control
character, since these values eventually flow into generated email
headers/AI prompts elsewhere in the platform).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from src.core.exceptions import ConfigError
from src.core.ledger.entity_ns import EntityNsMapper
from src.core.people_entity_schema import CanonicalEntity, EntityRedirect

_entity_ns_mapper = EntityNsMapper()


def reject_header_injection_risk(value: str, *, field_name: str = "value") -> None:
    """§7.2a: "email-header/control-character validation." A real
    security check, not a formality -- aliases/emails eventually flow
    into generated email headers and AI prompts elsewhere in the
    platform; a bare `\\r`/`\\n` in an identity value is a header-
    injection vector."""
    for char in value:
        if char in ("\r", "\n") or (unicodedata.category(char).startswith("C") and char not in ("\t",)):
            raise ConfigError(f"{field_name} contains a control character (header-injection risk): {value!r}")


def normalize_alias_for_lookup(value: str) -> str:
    """Unicode NFC normalization plus casefold, for alias/name MATCHING
    only. Never use this for provider-subject exact matching (§7.2a
    requires that stay byte-exact) or as the value actually stored."""
    reject_header_injection_risk(value, field_name="alias")
    return unicodedata.normalize("NFC", value).strip().casefold()


def normalize_email_for_lookup(value: str) -> str:
    """Conservative email/UPN normalization: NFC-normalize and lowercase
    only the DOMAIN part (IDN-safe -- attempts IDNA encoding so visually
    similar Unicode domains compare equal to their ASCII form; falls back
    to plain lowercasing if the domain isn't valid IDNA). The local part's
    case is preserved -- §7.2a: "Email local parts are not blindly
    casefolded as an identity authority.\""""
    reject_header_injection_risk(value, field_name="email")
    normalized = unicodedata.normalize("NFC", value).strip()
    if "@" not in normalized:
        return normalized
    local_part, _, domain = normalized.rpartition("@")
    try:
        domain_normalized = domain.encode("idna").decode("ascii").lower()
    except (UnicodeError, UnicodeDecodeError):
        domain_normalized = domain.casefold()
    return f"{local_part}@{domain_normalized}"


def _extract_alias_from_legacy_ref(ref: str) -> str | None:
    """Accepts `P:<alias>` (signal-layer) or `person:<alias>` (ledger
    form) and returns the bare alias, or None if `ref` isn't either
    shape. Reuses `EntityNsMapper` -- does not reimplement the P:/person:
    translation."""
    if ref.startswith("P:"):
        ledger_form = _entity_ns_mapper.person_to_ledger(ref)  # "P:jdoe" -> "person:jdoe"
        return ledger_form[len("person:"):]
    if ref.startswith("person:"):
        signal_form = _entity_ns_mapper.ledger_to_person(ref)  # "person:jdoe" -> "P:jdoe", or None
        if signal_form is not None:
            return signal_form[len("P:"):]
    return None


def resolve_entity_redirect(entity_id: str, redirects: tuple[EntityRedirect, ...], *, max_hops: int = 8) -> str:
    """§7.2: "A tombstoned entity with one valid redirect resolves to the
    target... Redirect cycles are forbidden; merge chains are compacted
    to one target" -- redirects are a single hop by policy, but this
    walks defensively (bounded, cycle-detected) rather than trusting that
    invariant blindly."""
    redirect_by_source = {r.from_entity_id: r.to_entity_id for r in redirects}
    current = entity_id
    seen: set[str] = set()
    for _ in range(max_hops):
        target = redirect_by_source.get(current)
        if target is None:
            return current
        if target in seen or target == current:
            raise ConfigError(f"Redirect cycle detected resolving {entity_id!r} (redirects are supposed to be forbidden from cycling).")
        seen.add(current)
        current = target
    raise ConfigError(f"Redirect chain for {entity_id!r} exceeded {max_hops} hops -- expected a single-hop, compacted chain.")


@dataclass(frozen=True, slots=True)
class NamespaceResolution:
    input_ref: str
    canonical_entity_id: str | None
    resolved_via: str  # "already_canonical" | "alias_match" | "redirect" | "unresolved"


def resolve_ref_to_canonical_entity_id(
    ref: str,
    *,
    entities: tuple[CanonicalEntity, ...],
    redirects: tuple[EntityRedirect, ...] = (),
) -> NamespaceResolution:
    """The namespace bridge's main entry point: accepts a `P:<alias>`,
    `person:<alias>` (legacy ledger form), or an already-canonical
    `person:<ULID>`/`team:<ULID>` ref, and resolves it to the CURRENT
    canonical `entity_id` -- following alias history (§7.2a: "Alias
    history remains resolvable after rename") and `EntityRedirect`s along
    the way. Returns `canonical_entity_id=None` if nothing matches
    (`resolved_via="unresolved"`) rather than raising -- an unresolved
    reference is a normal, expected outcome for a caller to handle, not
    an error condition in this function itself."""
    entities_by_id = {entity.entity_id: entity for entity in entities}

    if ref in entities_by_id:
        resolved = resolve_entity_redirect(ref, redirects)
        return NamespaceResolution(input_ref=ref, canonical_entity_id=resolved, resolved_via="already_canonical" if resolved == ref else "redirect")

    alias = _extract_alias_from_legacy_ref(ref)
    lookup_value = alias if alias is not None else ref
    normalized_lookup = normalize_alias_for_lookup(lookup_value)

    for entity in entities:
        for entity_alias in entity.aliases:
            if normalize_alias_for_lookup(entity_alias.value) == normalized_lookup:
                resolved = resolve_entity_redirect(entity.entity_id, redirects)
                via = "alias_match" if resolved == entity.entity_id else "redirect"
                return NamespaceResolution(input_ref=ref, canonical_entity_id=resolved, resolved_via=via)

    return NamespaceResolution(input_ref=ref, canonical_entity_id=None, resolved_via="unresolved")
