"""specs/backlog.md BL-E1 (DIR-08A/08B): graded PII retention/encryption/
reveal policy for the people registry.

Platform default lives in `vertex/policies/privacy_policy.yaml`'s
`people_registry` section, shallow-overridden by
`knowledge_root/policies/privacy_policy.yaml`'s
`privacy_policy_override.people_registry` key -- the exact override shape
`knowledge/people_registry_policy.example.yaml` already illustrates,
mirroring `identity_provider_validation.load_identity_source_authority_policy`'s
established platform-default + override pattern exactly.

Policy decision (operator-approved 2026-07-24, presented with full context
and alternatives; see specs/backlog.md BL-E1):

- ``retention_days`` (365): clocked from `PersonDirectory.departed_at`, not
  record creation -- a person's PII retention concern only starts once they
  have left. `entity_id` and historical fact-lineage/attribution are never
  subject to this clock; only the PII-bearing directory fields are.
- ``default_encryption`` ("sensitive_only"): the REQUIRED floor. Below this
  is a policy violation (DIR-08B, FAIL).
- ``recommended_encryption`` ("all"): the aspirational bar. Meeting the
  required floor but not this bar is DIR-08A (WARN), not a FAIL. Note:
  "all" is not currently achievable with today's code -- no encryption
  mechanism exists yet for `people_directory.yaml`/`teams.yaml`, only for
  the sensitive-profile file (`profile_encryption.py`) -- so this WARN is
  expected to fire until that capability exists, which is itself an honest
  signal, not a bug in the check.
- ``reveal_requires_principal_allowlist`` (True): always required, no WARN
  tier -- a missing/disabled `pii_reveal_principals` allowlist is a FAIL,
  not a matter of degree.
- Scope: workspace-global, not per-program -- matches every other policy
  override in this codebase (identity, freshness, AI-field-allowlist) and
  the BL-G1 decision to stay single/dual-program.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_POLICY_PATH = Path("vertex/policies/privacy_policy.yaml")
_REPO_ROOT = Path(".")
_OVERRIDE_RELATIVE_PATH = Path("policies/privacy_policy.yaml")

#: Ordered worst-to-best; encryption_rank() compares actual vs. policy tiers.
_ENCRYPTION_LEVELS = ("none", "sensitive_only", "all")


@dataclass(frozen=True, slots=True)
class PeopleRegistryPrivacyPolicy:
    schema_version: str
    retention_days: int
    default_encryption: str
    recommended_encryption: str
    reveal_requires_principal_allowlist: bool


@functools.lru_cache(maxsize=1)
def _load_policy_doc(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_people_registry_privacy_policy(
    *,
    knowledge_root: Path | None = None,
    repo_root: Path = _REPO_ROOT,
    override_path: Path | None = None,
) -> PeopleRegistryPrivacyPolicy:
    """Platform default (`vertex/policies/privacy_policy.yaml`'s
    `people_registry` section), shallow-overridden by
    `knowledge_root/policies/privacy_policy.yaml`'s
    `privacy_policy_override.people_registry` key if present. Cached per
    path; tests pass `override_path` directly."""
    base_path = override_path or (repo_root / _POLICY_PATH)
    raw = dict(_load_policy_doc(base_path))
    section = dict(raw.get("people_registry") or {})

    if knowledge_root is not None:
        override_file = knowledge_root / _OVERRIDE_RELATIVE_PATH
        if override_file.exists():
            override_doc = yaml.safe_load(override_file.read_text(encoding="utf-8")) or {}
            override_section = (override_doc.get("privacy_policy_override") or {}).get("people_registry") or {}
            section = {**section, **override_section}

    return PeopleRegistryPrivacyPolicy(
        schema_version=str(raw.get("policy_schema_version", "1")),
        retention_days=int(section.get("retention_days", 365)),
        default_encryption=str(section.get("default_encryption", "sensitive_only")),
        recommended_encryption=str(section.get("recommended_encryption", "all")),
        reveal_requires_principal_allowlist=bool(section.get("reveal_requires_principal_allowlist", True)),
    )


def encryption_rank(level: str) -> int:
    """Unknown values rank below "none" (worse than the weakest known tier),
    so a typo'd or unrecognized encryption level fails closed, not open."""
    try:
        return _ENCRYPTION_LEVELS.index(level)
    except ValueError:
        return -1
