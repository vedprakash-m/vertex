"""WI-3.7: Privacy policy enforcement — fact-level classification and filtering.

Policy file: vertex/policies/privacy_policy.yaml
  default_classification: internal   (default-deny; unclassified → internal)
  fact_type_classifications: {type → public | internal | sensitive}

`filter_facts_for_render(snapshot, max_classification)` removes facts whose
classification exceeds the ceiling. Used by `ProgramReality.to_dict()`.

Zone A module — must not import from src.ai or src.m365 (INV-1).
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

_POLICY_PATH = Path(__file__).resolve().parents[2] / "vertex" / "policies" / "privacy_policy.yaml"

# Classification rank: lower = more open.
_CLASSIFICATION_RANK: dict[str, int] = {
    "public": 0,
    "internal": 1,
    "sensitive": 2,
}

_DEFAULT_CLASSIFICATION = "internal"


@dataclass(frozen=True, slots=True)
class PrivacyPolicy:
    """Loaded privacy policy configuration."""

    policy_schema_version: str
    default_classification: str
    fact_type_classifications: dict[str, str]  # fact_type → classification level


def load_privacy_policy(*, policy_path: Path | None = None) -> PrivacyPolicy:
    """Load privacy_policy.yaml; fall back to hard-coded defaults when absent."""
    from src.core.yaml_utils import load_yaml_mapping

    resolved = policy_path or _POLICY_PATH
    raw: dict[str, Any] = {}
    if resolved.exists():
        raw = load_yaml_mapping(resolved)

    return PrivacyPolicy(
        policy_schema_version=str(raw.get("policy_schema_version", "1")),
        default_classification=str(raw.get("default_classification", _DEFAULT_CLASSIFICATION)),
        fact_type_classifications=dict(raw.get("fact_type_classifications", {})),
    )


def get_fact_classification(fact_type: str, policy: PrivacyPolicy) -> str:
    """Return the classification level for a fact type.

    Default-deny: unregistered types return policy.default_classification
    (which is 'internal' unless overridden).
    """
    return policy.fact_type_classifications.get(fact_type, policy.default_classification)


def classification_rank(level: str) -> int:
    """Return numeric rank for a classification level (lower = more open)."""
    return _CLASSIFICATION_RANK.get(level.lower(), 1)  # unknown → internal


def is_fact_visible(
    fact_type: str,
    *,
    max_classification: str,
    policy: PrivacyPolicy,
) -> bool:
    """True iff the fact_type's classification is ≤ max_classification ceiling.

    A fact is visible when its sensitivity rank ≤ the ceiling rank.
    Examples:
      public (0) ≤ internal (1) → visible ✓
      sensitive (2) ≤ internal (1) → NOT visible ✗
      sensitive (2) ≤ sensitive (2) → visible ✓
    """
    fact_rank = classification_rank(get_fact_classification(fact_type, policy))
    ceiling_rank = classification_rank(max_classification)
    return fact_rank <= ceiling_rank


def filter_facts_for_render(
    snapshot: Any,  # ProgramFactSnapshot (avoid circular import)
    *,
    max_classification: str = "internal",
    policy: PrivacyPolicy | None = None,
) -> Any:
    """Return a snapshot with facts exceeding max_classification removed.

    Contract:
    - default_classification=internal → unclassified types are internal (not public)
    - Sensitive facts are filtered unless max_classification='sensitive'
    """
    resolved_policy = policy or load_privacy_policy()
    visible = tuple(
        fact for fact in snapshot.facts
        if is_fact_visible(fact.fact_type, max_classification=max_classification, policy=resolved_policy)
    )
    # Return same type (duck-typing: ProgramFactSnapshot)
    return type(snapshot)(
        program_id=snapshot.program_id,
        as_of=snapshot.as_of,
        facts=visible,
    )
