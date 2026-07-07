from __future__ import annotations

from datetime import datetime

from src.core.hypothesis_proposers import run_registered_hypothesis_proposers
from src.core.models_v2 import ClaimEntry
from src.core.reality_store import RealityStore


def run_hypothesis_proposer_stage(
    program_id: str,
    *,
    claims: tuple[ClaimEntry, ...],
    proposed_at: datetime,
    store: RealityStore,
) -> tuple[object, ...]:
    if store.program_id != program_id:
        raise ValueError(f"RealityStore program_id {store.program_id!r} does not match {program_id!r}.")
    store.initialize()
    return run_registered_hypothesis_proposers(
        store=store,
        claims=claims,
        proposed_at=proposed_at,
    )
