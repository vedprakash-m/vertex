"""WI-3.2a: Signal promotion — observe and append signals as program facts.

Signal promotion is the ONLY write path from raw signals into ProgramFactStore.
This module uses ONLY append_fact — the confirmed-snapshot write method is
forbidden here and enforced by AST contract.
All writes go through `ProgramFactStore.append_fact()`.

Design:
- `promote_observation()` — append a signal.observation fact; idempotent via natural_key
- `is_provisional_signal()` — classify source as provisional (human_comms family)
- Reconfirmation: if same natural_key already exists, emit `fact.reconfirmation` event
- Breaker pre-check: consult `trust_ctx.suspended_sources` before promoting
- Provisional signals set `review_state=PROPOSED` (never ACCEPTED)

Zone A module — must not import from src.ai or src.m365 (INV-1).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.core.program_fact_store import (
    FactLifecycleState,
    FactPrecedence,
    FactReviewState,
    ProgramFactInput,
    ProgramFactStore,
    ProgramFactWriteResult,
    ProgramFactSnapshot,
    build_natural_key,
)
from src.core.truth_model import TruthContext

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Source families that are treated as provisional (human communications)
_PROVISIONAL_FAMILIES: frozenset[str] = frozenset(
    {"workiq", "teams", "transcript", "human_comms"}
)

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PromotionResult:
    """Result of a signal promotion attempt."""

    natural_key: str
    action: str          # "created" | "noop" | "reconfirmed" | "suspended"
    fact_write: ProgramFactWriteResult | None
    reconfirmation_write: ProgramFactWriteResult | None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_provisional_signal(source_family: str) -> bool:
    """True when the signal source is a provisional/human-comms family.

    Provisional signals are stored as PROPOSED, not ACCEPTED, and
    require human review before entering the confirmed reality model.

    Provisional families: workiq, teams, transcript, human_comms
    """
    return source_family.lower() in _PROVISIONAL_FAMILIES


def promote_observation(
    *,
    program_id: str,
    fact_type: str,
    entity_refs: tuple[str, ...],
    payload: dict[str, Any],
    source_signal_ids: tuple[str, ...] = (),
    source_family: str = "ado",
    scope: str = "program",
    truth_ctx: TruthContext | None = None,
    recorded_at: datetime | None = None,
    db_root: Path | None = None,
) -> PromotionResult:
    """Promote a signal observation to ProgramFactStore.

    Contract:
    1. Check circuit breaker — if source is suspended, return PromotionResult(action="suspended")
    2. Build natural_key from (fact_type, entity_refs, scope)
    3. Check for existing fact — if same natural_key exists, emit fact.reconfirmation
    4. Provisional sources (human_comms family) → review_state=PROPOSED
    5. Non-provisional → review_state=ACCEPTED (VERIFIED_SYSTEM_SIGNAL precedence)
    6. Uses append_fact only — the confirmed-snapshot write path is not invoked here.
    """
    # --- Breaker pre-check ---
    if truth_ctx is not None:
        normalized_source = source_family.lower()
        if normalized_source in truth_ctx.suspended_sources:
            return PromotionResult(
                natural_key="",
                action="suspended",
                fact_write=None,
                reconfirmation_write=None,
            )

    # --- Build natural key ---
    natural_key = build_natural_key(fact_type, entity_refs=entity_refs, scope=scope)

    # --- Determine review state ---
    provisional = is_provisional_signal(source_family)
    review_state = FactReviewState.PROPOSED if provisional else FactReviewState.ACCEPTED

    # --- Promote the observation ---
    store = ProgramFactStore(program_id, db_root=db_root)
    ts = recorded_at or datetime.now(timezone.utc)

    fact_input = ProgramFactInput(
        fact_type=fact_type,
        entity_refs=entity_refs,
        payload=payload,
        scope=scope,
        source_signal_ids=source_signal_ids,
        confidence=None,
        precedence=FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
        review_state=review_state,
        lifecycle_state=FactLifecycleState.ACTIVE,
        natural_key=natural_key,
        created_by=f"signal_promotion:{source_family}",
        privacy_classification="internal",
    )
    fact_result = store.append_fact(fact_input, recorded_at=ts)

    # --- Reconfirmation check ---
    # If the fact already existed (noop), emit a reconfirmation event
    reconf_result: ProgramFactWriteResult | None = None
    action = fact_result.action

    if fact_result.action == "noop":
        reconf_natural_key = build_natural_key(
            "fact.reconfirmation",
            entity_refs=(f"reconf:{natural_key}",),
            scope=scope,
        )
        reconf_payload = {
            "target_natural_key": natural_key,
            "day_bucket": ts.date().isoformat(),
            "reconfirmed_by": f"signal_promotion:{source_family}",
            "reconfirmed_at": ts.isoformat(),
            "source_signal_ids": list(source_signal_ids),
        }
        reconf_input = ProgramFactInput(
            fact_type="fact.reconfirmation",
            entity_refs=(f"reconf:{natural_key}",),
            payload=reconf_payload,
            scope=scope,
            source_signal_ids=source_signal_ids,
            precedence=FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
            review_state=FactReviewState.ACCEPTED,
            lifecycle_state=FactLifecycleState.ACTIVE,
            natural_key=reconf_natural_key,
            created_by=f"signal_promotion:{source_family}",
            privacy_classification="internal",
        )
        reconf_result = store.append_fact(reconf_input, recorded_at=ts)
        action = "reconfirmed"

    return PromotionResult(
        natural_key=natural_key,
        action=action,
        fact_write=fact_result,
        reconfirmation_write=reconf_result,
    )


def batch_promote_observations(
    observations: list[dict[str, Any]],
    *,
    program_id: str,
    truth_ctx: TruthContext | None = None,
    db_root: Path | None = None,
) -> list[PromotionResult]:
    """Promote a batch of observations.

    Each observation dict must have:
      - fact_type: str
      - entity_refs: list[str]
      - payload: dict
      - source_family: str (optional, default "ado")
      - scope: str (optional, default "program")
      - source_signal_ids: list[str] (optional)
      - recorded_at: str (optional ISO datetime)

    Returns one PromotionResult per observation, in input order.
    """
    results = []
    for obs in observations:
        recorded_raw = obs.get("recorded_at")
        recorded_at: datetime | None = None
        if recorded_raw:
            try:
                recorded_at = datetime.fromisoformat(str(recorded_raw))
                if recorded_at.tzinfo is None:
                    recorded_at = recorded_at.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                recorded_at = None

        results.append(
            promote_observation(
                program_id=program_id,
                fact_type=obs["fact_type"],
                entity_refs=tuple(obs["entity_refs"]),
                payload=obs["payload"],
                source_signal_ids=tuple(obs.get("source_signal_ids", [])),
                source_family=obs.get("source_family", "ado"),
                scope=obs.get("scope", "program"),
                truth_ctx=truth_ctx,
                recorded_at=recorded_at,
                db_root=db_root,
            )
        )
    return results
