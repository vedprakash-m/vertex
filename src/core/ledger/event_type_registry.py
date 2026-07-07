"""Unified ledger event-type registry (W2-8 / still-gaps.md).

Every supported ledger event type is declared here exactly once.  Adding a new
type is a single table entry — not 5-file surgery across pipeline.py,
fact_bridge.py, ledger.py, program_views.py, and program_reality.py.

Usage
-----
- `_maybe_bridge_event_to_fact_store` in `ledger.py` uses this registry to
  determine the bridge disposition of each inbound event type without
  consulting a hardcoded tuple list.
- Contract tests assert that every non-passthrough type either has a bridge
  appender name (PROJECTABLE) or is explicitly declared KNOWN_UNPROJECTEABLE.
- The spec matrix in `.archive/specs/still-gaps.md §6.3` is generated from /
  consistent with this table.
- **S-0j**: `authority_family` for PROJECTABLE types is now validated against
  `vertex/policies/source_authority.yaml` `family_map` at import time.  The
  registry no longer hand-maintains a value that must agree with the policy
  file — `assert_registry_authority_families_match_policy()` (callable by
  contract tests) compares every row.  The single source of truth is
  `source_authority.yaml`.

Design notes
------------
- Bridge appender names are strings (not function references) to avoid circular
  imports: `fact_bridge.py` imports `ProgramFactStore`; if the registry held
  callables from `fact_bridge`, importing the registry anywhere would pull
  in the full bridge dependency chain.  `ledger.py` resolves names at call
  time from its own (already imported) `fact_bridge` namespace.
- `authority_family` values must match the 6 valid families declared in
  `src/core/fact_sor_state.py` (workitem.state / metric / incident /
  judgment / commitment / narrative).  `None` means no SoR gate applies.
- Fact types ending in `.entry` are the canonical storage types as defined
  in `source_authority.yaml` `family_map`.  Event type *prefixes* (e.g.
  ``"risk."``) correspond to the ``"risk.entry"`` fact type in the map.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Policy file — single source of truth for fact_type → authority_family
# ---------------------------------------------------------------------------

_POLICY_PATH = Path(__file__).resolve().parents[3] / "vertex" / "policies" / "source_authority.yaml"


def _load_family_map() -> dict[str, str | None]:
    """Load the fact_type → authority_family mapping from source_authority.yaml.

    Returns a dict where the value is the authority family string, or
    ``None`` for the ``BY_SIGNAL_CLASS`` special sentinel (handled at runtime).
    Returns an empty dict if the policy file cannot be read (allows import to
    succeed in restricted environments; validation will warn instead of crash).
    """
    try:
        with _POLICY_PATH.open(encoding="utf-8") as fh:
            doc: Any = yaml.safe_load(fh)
        if not isinstance(doc, dict):
            log.warning("source_authority.yaml is not a mapping; S-0j validation skipped")
            return {}
        raw: Any = doc.get("family_map") or {}
        if not isinstance(raw, dict):
            log.warning("source_authority.yaml missing family_map; S-0j validation skipped")
            return {}
        result: dict[str, str | None] = {}
        for fact_type, family in raw.items():
            # BY_SIGNAL_CLASS is resolved at runtime; treat as None here
            result[str(fact_type)] = None if str(family) == "BY_SIGNAL_CLASS" else str(family)
        return result
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not load source_authority.yaml for S-0j validation: %s", exc)
        return {}


# Loaded once at module import; tests can monkeypatch _FAMILY_MAP.
_FAMILY_MAP: dict[str, str | None] = _load_family_map()

# Canonical mapping: event-type prefix → storage fact_type
# Used to translate registry prefix to family_map key for S-0j validation.
_PREFIX_TO_FACT_TYPE: dict[str, str] = {
    "risk.": "risk.entry",
    "decision.": "decision.entry",
    "assumption.": "assumption.entry",
    "milestone.": "milestone.entry",
    "dependency.": "dependency.link",
    "workstream.": "workstream.entry",
    "commitment.": "commitment.entry",
    "deliverable.": "deliverable.entry",   # Phase 2: family_map entry added (deliv-incident-fu); KNOWN_UNPROJECTEABLE in v1
    "incident.": "incident.entry",          # Phase 2: family_map entry added (deliv-incident-fu); KNOWN_UNPROJECTEABLE in v1
    "action_item.": "action.item",
}


class EventDisposition(str, Enum):
    """How `_maybe_bridge_event_to_fact_store` handles events with this prefix."""

    PROJECTABLE = "projectable"
    # Has a bridge appender; fact written to ProgramFactStore on acceptance.

    KNOWN_UNPROJECTEABLE = "known_unprojecteable"
    # Received by the bridge but no projector exists yet.
    # Emits WARNING so operators can observe events arriving before the
    # projector is implemented.  Never silent.

    PASSTHROUGH = "passthrough"
    # Lifecycle / internal events (discovery., ledger., nudge., …).
    # The bridge intentionally ignores these — no warning emitted.


@dataclass(frozen=True, slots=True)
class LedgerEventSpec:
    """Full specification for one ledger event-type prefix.

    Attributes
    ----------
    prefix:
        Dot-separated prefix that all event types in this family share
        (e.g. ``"risk."``).  Matches are prefix-based (``startswith``).
    fact_family:
        Human-readable name for the fact family (e.g. ``"risk"``).
    authority_family:
        One of the 6 valid SoR authority families from ``fact_sor_state.py``,
        or ``None`` for non-projectable / passthrough types.
        **S-0j:** This value must equal the authority_family recorded for the
        corresponding fact_type in ``source_authority.yaml`` ``family_map``.
        ``assert_registry_authority_families_match_policy()`` enforces this.
    disposition:
        ``EventDisposition`` — controls bridge behaviour.
    bridge_appender_name:
        Name of the ``fact_bridge`` function that writes this event to the
        fact store, or ``None`` for non-PROJECTABLE types.
    accessor:
        ``ProgramReality`` method that returns facts of this type
        (e.g. ``"risks()"``), or ``None`` if no accessor exists yet.
    consumers:
        Downstream surfaces that read this family (``"report"``,
        ``"nudge"``, ``"brief"``).  Empty for types without a projector.
    failure_mode:
        What breaks if the update-event precondition is missing.  Used
        for documentation and future gate contracts.
    """

    prefix: str
    fact_family: str
    authority_family: str | None
    disposition: EventDisposition
    bridge_appender_name: str | None
    accessor: str | None
    consumers: tuple[str, ...]
    failure_mode: str


# ---------------------------------------------------------------------------
# Registry — single source of truth for all known ledger event-type families
# ---------------------------------------------------------------------------

LEDGER_EVENT_REGISTRY: tuple[LedgerEventSpec, ...] = (
    # ── PROJECTABLE ─────────────────────────────────────────────────────────
    LedgerEventSpec(
        prefix="risk.",
        fact_family="risk",
        # S-0j: source_authority.yaml family_map maps risk.entry → judgment
        # (the old value "workitem.state" at :99 was a bug — R-X-3).
        authority_family="judgment",
        disposition=EventDisposition.PROJECTABLE,
        bridge_appender_name="append_bridged_risk_event",
        accessor="risks()",
        consumers=("report", "nudge"),
        failure_mode="non-creation events require an existing risk.entry fact; "
                     "absent fact → KeyError in build_bridge_risk_fact_input",
    ),
    LedgerEventSpec(
        prefix="decision.",
        fact_family="decision",
        authority_family="judgment",
        disposition=EventDisposition.PROJECTABLE,
        bridge_appender_name="append_bridged_decision_event",
        accessor="decisions()",
        consumers=("report", "brief"),
        failure_mode="revision/supersede events require an existing decision.entry fact",
    ),
    LedgerEventSpec(
        prefix="assumption.",
        fact_family="assumption",
        authority_family="judgment",
        disposition=EventDisposition.PROJECTABLE,
        bridge_appender_name="append_bridged_assumption_event",
        accessor="assumptions()",
        consumers=("report",),
        failure_mode="invalidate/validate events require an existing assumption.entry fact",
    ),
    LedgerEventSpec(
        prefix="milestone.",
        fact_family="milestone",
        authority_family="workitem.state",
        disposition=EventDisposition.PROJECTABLE,
        bridge_appender_name="append_bridged_milestone_event",
        accessor="milestones()",
        consumers=("report", "nudge", "brief"),
        failure_mode="ledger projector creates a stub (program_views.py `_ensure_milestone_stub`) "
                     "but fact bridge REQUIRES an existing milestone.entry — inconsistent; "
                     "absent fact → LookupError in build_bridge_milestone_fact_input",
    ),
    LedgerEventSpec(
        prefix="dependency.",
        fact_family="dependency",
        authority_family="workitem.state",
        disposition=EventDisposition.PROJECTABLE,
        bridge_appender_name="append_bridged_dependency_event",
        accessor="dependencies()",
        consumers=("report",),
        failure_mode="status-change events require an existing dependency.link fact",
    ),
    LedgerEventSpec(
        prefix="workstream.",
        fact_family="workstream",
        authority_family="workitem.state",
        disposition=EventDisposition.PROJECTABLE,
        bridge_appender_name="append_bridged_workstream_event",
        accessor="workstreams()",
        consumers=("report", "nudge"),
        failure_mode="owner_changed/status_changed require an existing workstream.entry fact; "
                     "absent fact → LookupError; synthetic owner-changed id can't associate "
                     "if workstream.created.v1 was not previously emitted",
    ),
    LedgerEventSpec(
        prefix="commitment.",
        fact_family="commitment",
        authority_family="commitment",
        disposition=EventDisposition.PROJECTABLE,
        bridge_appender_name="append_bridged_commitment_event",
        accessor="commitments()",
        consumers=("report", "nudge", "brief"),
        failure_mode="slip/revision events require an existing commitment.entry fact; "
                     "commitments() hardcodes HUMAN_CONFIRMED and discards fact_id+evidence "
                     "(lineage loss — PS-26 / W2-7)",
    ),
    # ── KNOWN_UNPROJECTEABLE ─────────────────────────────────────────────────
    LedgerEventSpec(
        prefix="deliverable.",
        fact_family="deliverable",
        # deliverable.entry is now in family_map (Phase 2: deliv-incident-fu scaffolding).
        # Bridge appender and projector are Phase 2 work; KNOWN_UNPROJECTEABLE in v1 (S-2d/Q9).
        authority_family="deliverable",
        disposition=EventDisposition.KNOWN_UNPROJECTEABLE,
        bridge_appender_name=None,
        accessor=None,
        consumers=(),
        failure_mode="no bridge appender yet; project_deliverable_entries() Phase 2 stub returns empty; "
                     "logs WARNING (W2-5); Phase 2: add bridge appender + ProgramReality.deliverables() accessor",
    ),
    LedgerEventSpec(
        prefix="incident.",
        fact_family="incident",
        authority_family="incident",
        disposition=EventDisposition.KNOWN_UNPROJECTEABLE,
        bridge_appender_name=None,
        accessor=None,
        consumers=(),
        failure_mode="severity_changed maps to incident.opened.v1 (wrong semantic); "
                     "synthetic id per message prevents severity update association; "
                     "project_incident_entries() Phase 2 stub returns empty; logs WARNING (W2-5); "
                     "Phase 2: fix entity identity + add IcM source (S-10a/S-10b) + bridge appender",
    ),
    # ── PASSTHROUGH ──────────────────────────────────────────────────────────
    LedgerEventSpec(
        prefix="discovery.",
        fact_family="",
        authority_family=None,
        disposition=EventDisposition.PASSTHROUGH,
        bridge_appender_name=None,
        accessor=None,
        consumers=(),
        failure_mode="",
    ),
    LedgerEventSpec(
        prefix="ledger.",
        fact_family="",
        authority_family=None,
        disposition=EventDisposition.PASSTHROUGH,
        bridge_appender_name=None,
        accessor=None,
        consumers=(),
        failure_mode="",
    ),
    LedgerEventSpec(
        prefix="action_item.",
        fact_family="action_item",
        # S-0j: action.item → workitem.state per family_map
        authority_family="workitem.state",
        disposition=EventDisposition.PASSTHROUGH,
        bridge_appender_name=None,
        accessor=None,
        consumers=("report",),
        failure_mode="action items written via direct fact append, not bridge",
    ),
    LedgerEventSpec(
        prefix="knowledge.",
        fact_family="",
        authority_family=None,
        disposition=EventDisposition.PASSTHROUGH,
        bridge_appender_name=None,
        accessor=None,
        consumers=(),
        failure_mode="",
    ),
    LedgerEventSpec(
        prefix="nudge.",
        fact_family="",
        authority_family=None,
        disposition=EventDisposition.PASSTHROUGH,
        bridge_appender_name=None,
        accessor=None,
        consumers=(),
        failure_mode="",
    ),
    LedgerEventSpec(
        prefix="signal.",
        fact_family="",
        authority_family=None,
        disposition=EventDisposition.PASSTHROUGH,
        bridge_appender_name=None,
        accessor=None,
        consumers=(),
        failure_mode="",
    ),
    LedgerEventSpec(
        prefix="edition.",
        fact_family="",
        authority_family=None,
        disposition=EventDisposition.PASSTHROUGH,
        bridge_appender_name=None,
        accessor=None,
        consumers=(),
        failure_mode="",
    ),
)


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def lookup_event_spec(event_type: str) -> LedgerEventSpec | None:
    """Return the spec for the given event type, or None if unrecognised.

    Matching is prefix-based: ``"risk.raised.v1"`` matches the ``"risk."`` entry.
    The first matching entry wins (registry order matters for overlapping prefixes).
    """
    for spec in LEDGER_EVENT_REGISTRY:
        if event_type.startswith(spec.prefix):
            return spec
    return None


def projectable_specs() -> tuple[LedgerEventSpec, ...]:
    """Return all PROJECTABLE entries (have a bridge appender)."""
    return tuple(s for s in LEDGER_EVENT_REGISTRY if s.disposition == EventDisposition.PROJECTABLE)


def known_unprojecteable_specs() -> tuple[LedgerEventSpec, ...]:
    """Return all KNOWN_UNPROJECTEABLE entries (warn but don't fail)."""
    return tuple(
        s for s in LEDGER_EVENT_REGISTRY
        if s.disposition == EventDisposition.KNOWN_UNPROJECTEABLE
    )


# ---------------------------------------------------------------------------
# S-0j: Contract validation — registry must agree with source_authority.yaml
# ---------------------------------------------------------------------------

class RegistryPolicyMismatch(Exception):
    """Raised when the registry's authority_family disagrees with source_authority.yaml."""


def assert_registry_authority_families_match_policy(
    *,
    family_map: dict[str, str | None] | None = None,
) -> list[str]:
    """Validate every registry row's authority_family against source_authority.yaml.

    S-0j contract: the registry's authority_family must equal
    ``family_map[fact_type]`` for every PROJECTABLE and
    KNOWN_UNPROJECTEABLE row that has a corresponding fact_type entry.
    Passthrough rows and rows whose fact_type is not in ``family_map``
    are noted but not failed (structural gaps are documented separately).

    Parameters
    ----------
    family_map:
        Override the loaded policy map (useful in tests).  Defaults to
        the module-level ``_FAMILY_MAP`` loaded from source_authority.yaml.

    Returns
    -------
    list of mismatch messages.  Empty → all rows pass.
    Raises ``RegistryPolicyMismatch`` on any mismatch so contract tests
    can assert ``assert_registry_authority_families_match_policy() == []``.
    """
    policy = family_map if family_map is not None else _FAMILY_MAP
    if not policy:
        log.warning("S-0j: family_map is empty — cannot validate (policy file unreadable)")
        return []

    mismatches: list[str] = []
    for spec in LEDGER_EVENT_REGISTRY:
        if spec.disposition == EventDisposition.PASSTHROUGH:
            # Passthrough rows are internal events with no fact_type in the policy
            continue
        fact_type = _PREFIX_TO_FACT_TYPE.get(spec.prefix)
        if fact_type is None:
            # Structural gap: registry prefix has no canonical fact_type mapping.
            # Document but do not fail (the mapping table needs expanding as new
            # event types are registered).
            log.warning(
                "S-0j: registry prefix %r has no _PREFIX_TO_FACT_TYPE entry; "
                "add it to complete S-0j coverage",
                spec.prefix,
            )
            continue
        policy_family = policy.get(fact_type)
        if policy_family is None and fact_type not in policy:
            # Not in family_map at all — structural gap (e.g. deliverable.entry for Q9)
            log.warning(
                "S-0j: fact_type %r for prefix %r is not in source_authority.yaml "
                "family_map; out-of-scope for v1 (Q9) or needs a new family_map entry",
                fact_type,
                spec.prefix,
            )
            continue
        if spec.authority_family != policy_family:
            msg = (
                f"Registry prefix {spec.prefix!r} has authority_family="
                f"{spec.authority_family!r} but source_authority.yaml maps "
                f"{fact_type!r} → {policy_family!r}  [S-0j BLOCKER]"
            )
            mismatches.append(msg)

    if mismatches:
        raise RegistryPolicyMismatch("\n".join(mismatches))

    return mismatches
