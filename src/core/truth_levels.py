"""WI-0.7: TruthLevel enumeration for the Vertex truth model (§6.1, §6.2).

This module contains ONLY the enum — no logic, no imports from other
src.core modules. Importable standalone (Zone A; INV-1 compliant).
"""
from __future__ import annotations

from enum import Enum


class TruthLevel(str, Enum):
    """Validation state for a fact, derived at read time from TruthContext.

    Ordered from least to most trusted:

    RAW_OBSERVED
        The fact arrived from a source but has not been validated by any
        authority or corroborated by an independent source.

    SOURCE_VALIDATED
        The primary authority for the fact's family produced this
        observation AND that source is not suspended by the circuit breaker.

    CORROBORATED
        Two independent-provenance observations agree on the fact
        (independence checked via INV-13 echo-chamber guards).

    HUMAN_CONFIRMED
        The fact was confirmed by a human via the confirm loop, an explicit
        human action, or governed actuation.

    GOVERNANCE_LOCKED
        The fact is part of a locked/archived issue snapshot; changes
        require governance unlock.
    """

    RAW_OBSERVED = "raw_observed"
    SOURCE_VALIDATED = "source_validated"
    CORROBORATED = "corroborated"
    HUMAN_CONFIRMED = "human_confirmed"
    GOVERNANCE_LOCKED = "governance_locked"
