"""REV Zone-B package — structured extraction + layered verification.

specs/program-context-intelligence.md §5.8/§5.9. Zone B implements the
extraction and verification tiers behind the Zone-A port contracts. P1 ships
the deterministic tiers (regex extraction, quote_span, entity_date_value
consistency, materiality predicate); the LLM entailment/groundedness tiers are
stubbed for P0 operator-gated providers.

Zone B: imports only ``src.core.*`` + stdlib. Never imports ``src.commands``;
never imports the ledger event-write API — verification assertions append only
via ``verification_assertions.append_verification_assertion``.
"""

from __future__ import annotations

from src.ai.rev.extractor import (
    DeterministicRevExtractor,
    EvidenceSpan,
    ExtractedClaim,
    FakeRevExtractor,
    RevExtractor,
    is_material_event,
)

__all__ = [
    "DeterministicRevExtractor",
    "EvidenceSpan",
    "ExtractedClaim",
    "FakeRevExtractor",
    "RevExtractor",
    "is_material_event",
]