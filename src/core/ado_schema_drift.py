"""ADO schema-drift guard (activation.md §6.14.13 / O-16 / Gemini robustness).

The AG-9 conflict check compares an EML-derived fact against ADO state. If an
upstream ADO field is renamed, removed, or its status enum changes, the conflict
check can **silently break** — either spuriously flagging everything (a vanished
``System.State`` → default "Active") or never flagging anything. There is
currently *no* shape validation on the ADO inbound payload; every read is a
defensive ``.get(...) or <default>`` that hides drift behind a fallback.

This module defines the v1 guard:

- **Required fields** (``ADO_REQUIRED_FIELDS``) are the identity/state fields
  that must *always* be present on a work-item REST row. A missing required
  field is a hard schema change → ``SchemaDriftError`` (fail closed). These are
  the fields the conflict engine's ``compute_workitem_state_digest`` and the
  entity-resolution join depend on; defaulting silently here is exactly the
  silent-break §6.14.13 names.
- **Observed contract** (``ADO_CONTRACT_FIELDS``) is the wider pinned field set.
  An *addition* or *removal* in the observed shape is a **drift** — it does not
  fail the cycle, but it is recorded so the maintainer is alerted (the
  conflict check may need updating). This is the "alert the maintainer" half.

The guard is opt-in via ``VERTEX_ADO_SCHEMA_DRIFT_GUARD=1`` (default on when set;
off otherwise so existing defensive behavior is unchanged until an operator pins
the contract). When the guard is off, the inbound path behaves exactly as today.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

_ENV_GUARD = "VERTEX_ADO_SCHEMA_DRIFT_GUARD"
_ENV_STRICT_CONTRACT = "VERTEX_ADO_SCHEMA_DRIFT_STRICT_CONTRACT"

# Identity + state fields the AG-9 conflict engine and entity-resolution join
# depend on. These are always present on a well-formed ADO work-item REST row;
# absence is a hard schema change (fail closed).
ADO_REQUIRED_FIELDS: tuple[str, ...] = (
    "System.Id",
    "System.WorkItemType",
    "System.Title",
    "System.State",
)

# The wider pinned contract — the field set ``ADO_BATCH_FIELDS`` requests. Drift
# (add/remove) against this set is recorded for maintainer alerting. Callers
# pass the active batch-field tuple so this stays in sync with the request.
ADO_CONTRACT_FIELDS: tuple[str, ...] = (
    "System.Id",
    "System.WorkItemType",
    "System.Title",
    "System.State",
    "System.AreaPath",
    "System.IterationPath",
    "System.AssignedTo",
    "System.Tags",
    "System.ChangedDate",
    "Microsoft.VSTS.Scheduling.TargetDate",
)


class SchemaDriftError(ValueError):
    """Raised when an inbound ADO row is missing a required field (§6.14.13).

    Maps to ``FailureCategory.SCHEMA_DRIFT`` in the failure taxonomy. Failing
    closed here is deliberate: defaulting a vanished ``System.State`` to
    "Active" would silently degrade the AG-9 conflict check.
    """


@dataclass(frozen=True, slots=True)
class SchemaDriftReport:
    """The non-fatal drift observations for one hydration batch."""

    added_fields: tuple[str, ...] = ()
    removed_fields: tuple[str, ...] = ()
    rows_inspected: int = 0

    @property
    def has_drift(self) -> bool:
        return bool(self.added_fields or self.removed_fields)


def guard_enabled() -> bool:
    """Whether the fail-closed required-field guard is active (opt-in)."""
    return os.environ.get(_ENV_GUARD, "").strip() in {"1", "true", "yes", "on"}


def strict_contract_enabled() -> bool:
    """Whether contract add/remove drift should also fail closed (stricter)."""
    return os.environ.get(_ENV_STRICT_CONTRACT, "").strip() in {"1", "true", "yes", "on"}


def _extract_field_keys(row: dict[str, Any]) -> set[str]:
    raw = row.get("fields")
    if isinstance(raw, dict):
        return {str(k) for k in raw.keys()}
    return set()


def assert_row_shape(
    row: dict[str, Any],
    *,
    required_fields: tuple[str, ...] = ADO_REQUIRED_FIELDS,
) -> None:
    """Fail closed if a required field is absent from an inbound ADO row.

    No-op when the guard is disabled (``VERTEX_ADO_SCHEMA_DRIFT_GUARD`` unset) so
    the existing defensive ``.get()`` behavior is preserved until an operator
    pins the contract. When enabled, a missing required field raises
    ``SchemaDriftError`` rather than silently defaulting — protecting the AG-9
    conflict check from a degenerate state digest.
    """
    if not guard_enabled():
        return
    keys = _extract_field_keys(row)
    if not keys:
        # No fields dict at all is a structural break regardless of the contract.
        work_item_id = row.get("id", "<unknown>")
        raise SchemaDriftError(
            f"ADO schema drift: work item {work_item_id} returned no 'fields' dict "
            f"(§6.14.13); refusing to default — the AG-9 conflict check cannot "
            f"silently degrade. Run `vertex doctor --channels` to inspect."
        )
    missing = [f for f in required_fields if f not in keys]
    if missing:
        work_item_id = row.get("id") or row.get("fields", {}).get("System.Id", "<unknown>")
        raise SchemaDriftError(
            f"ADO schema drift: work item {work_item_id} is missing required field(s) "
            f"{sorted(missing)} (§6.14.13); refusing to default — the AG-9 conflict "
            f"check depends on these. Run `vertex doctor --channels` to inspect."
        )


def inspect_contract_drift(
    rows: list[dict[str, Any]],
    *,
    contract_fields: tuple[str, ...] = ADO_CONTRACT_FIELDS,
) -> SchemaDriftReport:
    """Compare the observed field set across ``rows`` to the pinned contract.

    Returns added/removed field names (non-fatal; for maintainer alerting). In
    strict mode (``VERTEX_ADO_SCHEMA_DRIFT_STRICT_CONTRACT``) a removal of a
    contract field that is not also a required field is escalated to a warning
    log so it surfaces in ``doctor`` without failing the cycle.
    """
    if not rows:
        return SchemaDriftReport()
    observed: set[str] = set()
    for row in rows:
        observed |= _extract_field_keys(row)
    contract = set(contract_fields)
    added = tuple(sorted(observed - contract))
    removed = tuple(sorted(contract - observed))
    if added or removed:
        log.warning(
            "ADO schema drift detected (§6.14.13): added=%s removed=%s — "
            "the AG-9 conflict check may need updating; run `vertex doctor --channels`.",
            list(added),
            list(removed),
        )
        if strict_contract_enabled() and removed:
            # Non-required contract removals are escalations, not hard failures
            # (a field may be legitimately absent on some work item types).
            log.error(
                "ADO schema drift (strict): contract fields removed: %s. "
                "Investigate before relying on AG-9 conflict output.",
                list(removed),
            )
    return SchemaDriftReport(
        added_fields=added,
        removed_fields=removed,
        rows_inspected=len(rows),
    )


__all__ = [
    "ADO_REQUIRED_FIELDS",
    "ADO_CONTRACT_FIELDS",
    "SchemaDriftError",
    "SchemaDriftReport",
    "guard_enabled",
    "strict_contract_enabled",
    "assert_row_shape",
    "inspect_contract_drift",
]
