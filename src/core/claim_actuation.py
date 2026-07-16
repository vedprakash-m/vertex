"""WI-7.1b: Claim-derived actuation proposals (§6.11.4).

Zone A module. Must not import from src.ai or src.m365.

Routes structured-ref-bearing ClaimEntry outputs into ActuationProposal facts
(source_type: claim_extraction). Commitment-shaped claims additionally propose
commitment.entry creations with direction inferred from the claim subject via
an EntityRegistry instance — injected, never loaded here (Q:-drive rule).

Rules:
- Free text never proposes: entity_refs must be non-empty.
- Per-program opt-in: caller passes enabled=True (backed by claim_actuation.yaml).
- Commitment-shaped = has owner_alias AND due_date.
- Direction inference (M-1, v3.2): owner_alias resolved via EntityRegistry.
    Internal entity types → outbound (our team commits).
    External entity types → inbound (external party commits).
    Unresolvable or unknown type → ambiguous; proposal payload asks.
- EntityRegistry is injected; never loaded inside this module.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from src.core.config_loader import PROGRAMS_ROOT

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Entity types that indicate the commitment belongs to our team (outbound)
_INTERNAL_ENTITY_TYPES = frozenset(("person", "team", "internal_team", "dri"))

# Entity types that indicate an external party (inbound)
_EXTERNAL_ENTITY_TYPES = frozenset((
    "external_team", "external_person", "vendor", "partner", "customer",
))

_CLAIM_ACTUATION_POLICY_FILENAME = "claim_actuation.yaml"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_claim_actuation_enabled(
    program_id: str,
    *,
    programs_root: Path | None = None,
) -> bool:
    """Return True if claim-derived proposals are opted-in for this program.

    Reads ``programs/<program_id>/policies/claim_actuation.yaml``.
    Defaults to False (must explicitly opt in).
    """
    root = programs_root or PROGRAMS_ROOT
    policy_path = root / program_id / "policies" / _CLAIM_ACTUATION_POLICY_FILENAME
    if not policy_path.exists():
        return False
    try:
        raw = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
        return bool(raw.get("enabled", False))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Direction inference
# ---------------------------------------------------------------------------

def infer_commitment_direction(
    owner_alias: str | None,
    registry: Any,
) -> str:
    """Infer commitment direction from owner_alias via EntityRegistry.

    Returns:
        "outbound" — owner resolves to an internal entity (our team commits).
        "inbound"  — owner resolves to an external entity (they commit to us).
        "ambiguous" — owner is unresolvable, ambiguous (ADF-W2.6: two
            close-scoring candidates), or entity_type is unknown.

    ``registry`` is typed as Any to avoid circular imports; it must implement
    ``resolve(raw: str) -> CanonicalEntity | None``. When it also implements
    ``resolve_with_binding`` (ADF-W2.6's Section 8.14.3 record), that is
    preferred -- a near-tie between two candidates now correctly returns
    "ambiguous" instead of silently picking whichever scored marginally
    higher and reporting that pick's direction with false confidence.
    """
    if not owner_alias or registry is None:
        return "ambiguous"

    if hasattr(registry, "resolve_with_binding"):
        entity = registry.resolve_with_binding(owner_alias).resolved_entity
    else:
        entity = registry.resolve(owner_alias)
    if entity is None:
        return "ambiguous"

    if entity.entity_type in _INTERNAL_ENTITY_TYPES:
        return "outbound"
    if entity.entity_type in _EXTERNAL_ENTITY_TYPES:
        return "inbound"
    return "ambiguous"


# ---------------------------------------------------------------------------
# Commitment-shape predicate
# ---------------------------------------------------------------------------

def is_commitment_shaped(claim: Any) -> bool:
    """Return True if the claim has both owner_alias and due_date (commitment-shaped)."""
    return bool(getattr(claim, "owner_alias", None)) and bool(getattr(claim, "due_date", None))


# ---------------------------------------------------------------------------
# Proposal builders
# ---------------------------------------------------------------------------

def _build_claim_proposal(
    *,
    operation: str,
    entity_ref: str,
    claim_id: str,
    claim_text: str,
    program_id: str,
    extra_payload: dict[str, Any],
    now: datetime,
    approval_ttl_hours: int,
) -> Any:
    """Build an ActuationProposal for a claim-derived operation."""
    from src.core.program_reality import ActuationProposal

    return ActuationProposal(
        proposal_id=str(uuid.uuid4()),
        rule_id="claim_extraction",
        adapter="claim",
        operation=operation,
        entity_ref=entity_ref,
        payload={
            "source_type": "claim_extraction",
            "claim_id": claim_id,
            "claim_text": claim_text,
            "program_id": program_id,
            "approval_ttl_hours": approval_ttl_hours,
            **extra_payload,
        },
        proposed_at=now,
        approved=False,
        gap_reason="",
    )


def _proposals_for_claim(
    claim: Any,
    registry: Any,
    *,
    now: datetime,
    approval_ttl_hours: int,
) -> list[Any]:
    """Derive proposals for a single ClaimEntry."""
    # ref-required: free text (no entity_refs) never proposes
    entity_refs: tuple[str, ...] = getattr(claim, "entity_refs", ())
    if not entity_refs:
        return []

    entity_ref = entity_refs[0]
    program_id: str = getattr(claim, "program_id", "")
    claim_id: str = getattr(claim, "id", "")
    claim_text: str = getattr(claim, "text", "")

    if is_commitment_shaped(claim):
        owner_alias: str = str(getattr(claim, "owner_alias", "") or "")
        due_date = getattr(claim, "due_date", None)
        direction = infer_commitment_direction(owner_alias, registry)

        extra: dict[str, Any] = {
            "operation_subtype": "commitment_entry_create",
            "owner_alias": owner_alias,
            "due_date": due_date.isoformat() if due_date is not None else None,
            "direction": direction,
        }
        if direction == "ambiguous":
            extra["direction_ambiguous"] = True

        return [_build_claim_proposal(
            operation="commitment_entry_create",
            entity_ref=entity_ref,
            claim_id=claim_id,
            claim_text=claim_text,
            program_id=program_id,
            extra_payload=extra,
            now=now,
            approval_ttl_hours=approval_ttl_hours,
        )]

    # Regular claim → action_item proposal
    return [_build_claim_proposal(
        operation="action_item",
        entity_ref=entity_ref,
        claim_id=claim_id,
        claim_text=claim_text,
        program_id=program_id,
        extra_payload={},
        now=now,
        approval_ttl_hours=approval_ttl_hours,
    )]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def propose_from_claims(
    claims: tuple[Any, ...],
    registry: Any,
    program_id: str,
    *,
    enabled: bool = False,
    approval_ttl_hours: int = 24,
    as_of: datetime | None = None,
) -> tuple[Any, ...]:
    """Derive actuation proposals from claim-extractor outputs.

    Returns an empty tuple when ``enabled=False`` (per-program opt-in gate).
    Free-text claims (empty entity_refs) never produce proposals regardless.

    Args:
        claims:             ClaimEntry tuple from ClaimExtractionResult.
        registry:           EntityRegistry instance — injected, never loaded here.
        program_id:         Program identifier (for payload annotation).
        enabled:            Per-program opt-in flag. Load via
                            load_claim_actuation_enabled() at the call site.
        approval_ttl_hours: Proposal TTL carried through to payload.
        as_of:              Override current time (tests/determinism).
    """
    if not enabled:
        return ()
    if not claims:
        return ()

    now = as_of or datetime.now(timezone.utc)
    proposals: list[Any] = []

    for claim in claims:
        proposals.extend(
            _proposals_for_claim(
                claim,
                registry,
                now=now,
                approval_ttl_hours=approval_ttl_hours,
            )
        )

    return tuple(proposals)
