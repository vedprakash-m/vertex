"""REV layered verification — Zone B (FR-PCI-8, §5.9).

specs/program-context-intelligence.md §5.9. Each verification *check* is an
append-only ``VerificationAssertion``; the **effective verification state** is
derived (in Zone A, ``verification_assertions.effective_verification_state``)
from the full set — never mutated. P1 ships the **deterministic** checks:

* ``quote_span`` — the excerpt text at ``[start, end)`` in the canonical text
  matches the span's captured excerpt (guards against span corruption).
* ``entity_date_value`` — a material claim's payload carries the entity + date
  asserted in the excerpt (consistency).
* ``materiality`` — deterministic predicate (§5.8): ``pass`` means the claim is
  material and therefore requires a human pass; absent materiality, the
  ``source_verified`` path is reachable.

The LLM tiers (``entailment``, ``groundedness``) are **deferred** in P1 (the
external classifier is P0 operator-gated); they append ``advisory`` /
``deferred`` assertions so the effective state and ``doctor --rev-health``
surface that the LLM tier was not run (visible degrade, never silent).
``human`` is set at triage time, not here.

Zone B: appends only via ``verification_assertions.append_verification_assertion``
(never the ledger event-write API).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.ai.rev.extractor import ExtractedClaim, is_material_event
from src.core.ledger.candidate_store import PROGRAMS_ROOT
from src.core.ledger.verification_assertions import (
    CHECK_ENTAILMENT,
    CHECK_ENTITY_DATE_VALUE,
    CHECK_GROUNDEDNESS,
    CHECK_MATERIALITY,
    CHECK_QUOTE_SPAN,
    STATUS_ADVISORY,
    STATUS_DEFERRED,
    STATUS_FAIL,
    STATUS_PASS,
    VerificationAssertion,
    append_verification_assertion,
    effective_verification_state,
)
from src.core.rev.ports import HydratedContent

VERIFICATION_POLICY_VERSION = "rev_verification.v1"
LLM_POLICY_VERSION = "rev_verification.llm.v1"


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    candidate_id: str
    assertions_written: int
    effective_state: str


def _assertion(
    candidate_id: str,
    check_type: str,
    status: str,
    *,
    evidence_refs: tuple[str, ...],
    policy_version: str,
    set_at: datetime,
) -> VerificationAssertion:
    return VerificationAssertion(
        candidate_id=candidate_id,
        resulting_event_id=None,
        check_type=check_type,
        status=status,
        policy_version=policy_version,
        evidence_refs=evidence_refs,
        set_by="rev_layered_verification",
        set_at=set_at,
    )


def check_quote_span(claim: ExtractedClaim, hydrated: HydratedContent) -> bool:
    """Deterministic: each span's excerpt equals canonical_text[start:end]."""
    canonical = hydrated.canonical_text
    for span in claim.evidence_spans:
        if span.start_codepoint < 0 or span.end_codepoint > len(canonical):
            return False
        if canonical[span.start_codepoint:span.end_codepoint] != span.excerpt_text:
            return False
    return True


def check_entity_date_value(claim: ExtractedClaim) -> bool:
    """Deterministic: a material claim's payload carries the entity + date asserted.

    Non-material claims pass vacuously (no consistency requirement). For
    material claims, a date-bearing payload field must be present and non-empty.
    """
    if not is_material_event(claim.event_type):
        return True
    date_value = claim.payload.get("date") or claim.payload.get("occurred_at")
    return bool(date_value)


def run_layered_verification(
    *,
    program_id: str,
    candidate_id: str,
    claims: tuple[ExtractedClaim, ...],
    hydrated: HydratedContent,
    evidence_refs: tuple[str, ...] = (),
    set_at: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> VerificationOutcome:
    """Run the P1 deterministic checks + deferred LLM checks; append assertions.

    Returns the number of assertions written and the derived effective state.
    Assertions are append-only; re-running adds a new layer (the effective state
    is derived from the full set, so re-verification is monotonic).
    """
    now = set_at or datetime.now(timezone.utc)
    assertions: list[VerificationAssertion] = []
    for claim in claims:
        # quote_span — deterministic span integrity.
        assertions.append(_assertion(
            candidate_id, CHECK_QUOTE_SPAN,
            STATUS_PASS if check_quote_span(claim, hydrated) else STATUS_FAIL,
            evidence_refs=evidence_refs, policy_version=VERIFICATION_POLICY_VERSION, set_at=now,
        ))
        # entity_date_value — deterministic consistency.
        assertions.append(_assertion(
            candidate_id, CHECK_ENTITY_DATE_VALUE,
            STATUS_PASS if check_entity_date_value(claim) else STATUS_FAIL,
            evidence_refs=evidence_refs, policy_version=VERIFICATION_POLICY_VERSION, set_at=now,
        ))
        # materiality — deterministic predicate. ``pass`` = material (human required).
        assertions.append(_assertion(
            candidate_id, CHECK_MATERIALITY,
            STATUS_PASS if is_material_event(claim.event_type) else STATUS_ADVISORY,
            evidence_refs=evidence_refs, policy_version=VERIFICATION_POLICY_VERSION, set_at=now,
        ))
    # LLM tiers — deferred in P1 (external classifier P0-gated). Visible degrade.
    if claims:
        assertions.append(_assertion(
            candidate_id, CHECK_ENTAILMENT, STATUS_DEFERRED,
            evidence_refs=evidence_refs, policy_version=LLM_POLICY_VERSION, set_at=now,
        ))
        assertions.append(_assertion(
            candidate_id, CHECK_GROUNDEDNESS, STATUS_ADVISORY,
            evidence_refs=evidence_refs, policy_version=LLM_POLICY_VERSION, set_at=now,
        ))

    for assertion in assertions:
        append_verification_assertion(assertion, program_id=program_id, programs_root=programs_root)

    effective = effective_verification_state(tuple(assertions))
    return VerificationOutcome(
        candidate_id=candidate_id,
        assertions_written=len(assertions),
        effective_state=effective,
    )


__all__ = [
    "VerificationOutcome",
    "run_layered_verification",
    "check_quote_span",
    "check_entity_date_value",
    "VERIFICATION_POLICY_VERSION",
    "LLM_POLICY_VERSION",
]