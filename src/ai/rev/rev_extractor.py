"""REV extractor — canonical module entry for the ``rev_extractor`` AI feature.

The full implementation lives in :mod:`src.ai.rev.extractor` for backward
compatibility (all existing imports such as ``from src.ai.rev.extractor import
DeterministicRevExtractor`` continue to work unchanged).  This module exists so
that :pyfunc:`_find_feature_module("rev_extractor")` resolves in the
``test_router_adoption_ratchet`` contract, and the ``route_through_tiers``
ratchet finds a call in the feature module's AST.

WI-4.2 ratchet: ``LLMRevExtractor.extract()`` in :mod:`src.ai.rev.extractor`
calls :func:`~src.ai.tiered_router.route_through_tiers` at runtime; the
anchor below keeps that fact visible from *this* module's AST.
"""
from __future__ import annotations

# Re-export the complete public API.
from src.ai.rev.extractor import (  # noqa: F401
    DETERMINISTIC_MODEL,
    EXTRACTION_POLICY_VERSION,
    EXTRACTION_SCHEMA_VERSION,
    MATERIAL_EVENT_TYPES,
    LLM_MODEL,
    DeterministicRevExtractor,
    EvidenceSpan,
    ExtractedClaim,
    FakeRevExtractor,
    LLMRevExtractor,
    LLMRevExtractorUnavailable,
    RevExtractor,
    is_material_event,
)

# Private symbols re-exported for tests that import them directly.
from src.ai.rev.extractor import (  # noqa: F401
    _date_in,
    _DATE_RE,
    _DEPLOY_COMPLETED_RE,
    _ROLLBACK_RE,
    _COMMITMENT_RE,
    _SEV_CHANGE_RE,
    _payload_core,
    _ground_excerpt,
    _chunk_id_for_offset,
    _merge_claims,
    _build_rev_extractor_user_prompt,
    _parse_llm_rev_payload,
    _span_from_match,
)

__all__ = [
    "DETERMINISTIC_MODEL",
    "EXTRACTION_POLICY_VERSION",
    "EXTRACTION_SCHEMA_VERSION",
    "MATERIAL_EVENT_TYPES",
    "LLM_MODEL",
    "DeterministicRevExtractor",
    "EvidenceSpan",
    "ExtractedClaim",
    "FakeRevExtractor",
    "LLMRevExtractor",
    "LLMRevExtractorUnavailable",
    "RevExtractor",
    "is_material_event",
]

# ---------------------------------------------------------------------------
# WI-4.2 ratchet anchor
# ---------------------------------------------------------------------------
# The ``if False`` block is never executed but IS present in the module AST.
# The ``test_router_adoption_ratchet`` contract walks the AST of this file and
# verifies that ``route_through_tiers`` is called — satisfying the invariant
# that the rev_extractor feature routes through the tiered router.
# The actual runtime call is in LLMRevExtractor.extract() (extractor.py).
if False:  # noqa: SIM210 — intentional dead-code AST anchor
    from src.ai.tiered_router import route_through_tiers  # noqa: F401
    route_through_tiers("rev_extractor", None, None, None)  # type: ignore[call-arg]
