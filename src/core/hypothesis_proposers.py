from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Protocol
from uuid import uuid4

from src.core.hypothesis_models import Hypothesis, HypothesisKind, HypothesisStatus
from src.core.models_v2 import ClaimEntry
from src.core.reality_store import RealityStore
from src.core.source_models import SourceKind, SourceRef


class HypothesisProposer(Protocol):
    def propose(
        self,
        *,
        store: RealityStore,
        claims: tuple[ClaimEntry, ...],
        proposed_at: datetime | None = None,
    ) -> tuple[Hypothesis, ...]:
        ...


@dataclass(frozen=True, slots=True)
class ClaimProposer:
    actor: str = "vertex/claim_proposer"
    re_proposal_cooldown: timedelta = timedelta(days=14)

    def propose(
        self,
        *,
        store: RealityStore,
        claims: tuple[ClaimEntry, ...],
        proposed_at: datetime | None = None,
    ) -> tuple[Hypothesis, ...]:
        created: list[Hypothesis] = []
        resolved_proposed_at = proposed_at or datetime.now(timezone.utc)
        for claim in claims:
            if claim.due_date is None:
                continue
            if not _claim_is_eligible_for_proposal(
                store,
                claim_id=claim.id,
                as_of=resolved_proposed_at,
                cooldown=self.re_proposal_cooldown,
            ):
                continue
            hypothesis = Hypothesis(
                id=str(uuid4()),
                short_id=store.next_hypothesis_short_id(),
                program_id=claim.program_id,
                kind=HypothesisKind.DELIVERY_DATE,
                statement=claim.text,
                expected_value=claim.due_date.isoformat(),
                as_of_date=claim.claim_date,
                telemetry_assertion_id=None,
                source_refs=(SourceRef(kind=SourceKind.CLAIM, ref=claim.id),),
                workstream_id=claim.workstream_id,
                proposed_by=self.actor,
                proposed_at=resolved_proposed_at,
                status=HypothesisStatus.PROPOSED,
                linked_claim_id=claim.id,
                linked_ado_item_id=_extract_linked_ado_item_id(claim),
            )
            store.upsert_hypothesis(hypothesis)
            created.append(hypothesis)
        return tuple(created)


def _claim_is_eligible_for_proposal(
    store: RealityStore,
    *,
    claim_id: str,
    as_of: datetime,
    cooldown: timedelta,
) -> bool:
    latest_state = store.get_latest_claim_hypothesis_state(claim_id)
    if latest_state is None:
        return True
    status, changed_at = latest_state
    if status not in {HypothesisStatus.REJECTED, HypothesisStatus.INVALIDATED}:
        return False
    if changed_at is None:
        return False
    return as_of - changed_at >= cooldown


def _extract_linked_ado_item_id(claim: ClaimEntry) -> int | None:
    for entity_ref in claim.entity_refs:
        match = re.fullmatch(r"WI:(\d+)", entity_ref.strip(), flags=re.IGNORECASE)
        if match is not None:
            return int(match.group(1))
    return None


_REGISTRY: tuple[HypothesisProposer, ...] = (ClaimProposer(),)


def run_registered_hypothesis_proposers(
    *,
    store: RealityStore,
    claims: tuple[ClaimEntry, ...],
    proposed_at: datetime | None = None,
    registry: tuple[HypothesisProposer, ...] | None = None,
) -> tuple[Hypothesis, ...]:
    created: list[Hypothesis] = []
    for proposer in registry or _REGISTRY:
        created.extend(
            proposer.propose(
                store=store,
                claims=claims,
                proposed_at=proposed_at,
            )
        )
    return tuple(created)