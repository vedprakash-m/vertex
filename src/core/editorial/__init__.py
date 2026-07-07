"""Stage γ-Read: Editorial Engine — deterministic quality checks (Zone A).

All symbols in this package are Zone A: no AI imports, no network calls,
no filesystem I/O during evaluation.  Every check operates on already-loaded
data passed via EvaluationContext / ScopeResolver.

Public surface:
    check_types     — 7 new PersonaCheckEvaluator implementations
    OverridesSerializer — serialises EditionOverrides to the unit-separator
                          format consumed by legacy regex extractors
"""
from __future__ import annotations

from src.core.editorial.check_types import (
    CountRangeCheck,
    CrossScopeConsistencyCheck,
    FormatMatchesCheck,
    OverridesSerializer,
    PublishedBaselineMatchCheck,
    ScorecardAlignmentCheck,
    SectionStructureCheck,
    TerminologyConsistencyCheck,
)

__all__ = [
    "CountRangeCheck",
    "CrossScopeConsistencyCheck",
    "FormatMatchesCheck",
    "OverridesSerializer",
    "PublishedBaselineMatchCheck",
    "ScorecardAlignmentCheck",
    "SectionStructureCheck",
    "TerminologyConsistencyCheck",
]
