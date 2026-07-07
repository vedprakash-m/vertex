"""WI-7.1: Actuation engine — governed proposal derivation (§6.11).

Zone A module. Must not import from src.ai or src.m365.

Shipping note: actuation.enabled defaults to false. The engine produces 0
proposals when enabled=False. Enabling is a per-program operator act gated
by CP-7 (dry-run review before any live flip).

Gates (§6.11.2):
  same_system  → inputs ≥ SOURCE_VALIDATED
  cross_system → inputs ≥ CORROBORATED

Condition predicates (A-12): trigger condition_predicates are predicate IDs
keyed into _CONDITION_PREDICATES — never expression strings, never evaluated.
Unknown predicate IDs are refused at load time.

Area-path gate (v3.2 / R-19): work_item_create proposals MUST carry an
area_path derived from the target entity's workstream config. A workstream
without a configured area_path blocks the proposal at derivation time with
gap_reason="missing_area_path" (rendered in dry-run) — never a runtime ADO 400.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from src.core.truth_levels import TruthLevel

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_POLICIES_ROOT = Path(__file__).resolve().parents[2] / "vertex" / "policies"
_GLOBAL_RULES_FILE = "actuation_rules.yaml"

_VALID_GATES = frozenset(("same_system", "cross_system"))
_VALID_OPERATIONS = frozenset(("state_transition", "comment", "work_item_create"))
_VALID_TRIGGER_KINDS = frozenset(("entity_condition", "structural_gap"))

# Minimum truth level per gate (§6.11.2)
_GATE_MIN_TRUTH: dict[str, TruthLevel] = {
    "same_system": TruthLevel.SOURCE_VALIDATED,
    "cross_system": TruthLevel.CORROBORATED,
}

# Ordered for comparison
_TRUTH_ORDER: list[TruthLevel] = [
    TruthLevel.RAW_OBSERVED,
    TruthLevel.SOURCE_VALIDATED,
    TruthLevel.CORROBORATED,
    TruthLevel.HUMAN_CONFIRMED,
    TruthLevel.GOVERNANCE_LOCKED,
]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ActuationRateCap:
    per_program_per_run: int = 10


@dataclass(frozen=True, slots=True)
class ActuationRule:
    id: str
    adapter: str
    operation: str
    gate: str                           # "same_system" | "cross_system"
    trigger_kind: str = "entity_condition"
    trigger_entity_type: str = ""
    trigger_condition_predicate: str = ""
    gap_id: str = ""
    rate_cap: ActuationRateCap = field(default_factory=lambda: ActuationRateCap())
    enabled: bool = False


@dataclass(frozen=True, slots=True)
class ActuationPolicy:
    schema_version: str
    enabled: bool
    approval_ttl_hours: int
    per_adapter_per_run_cap: int
    rules: tuple[ActuationRule, ...]

    @property
    def enabled_rules(self) -> tuple[ActuationRule, ...]:
        """Return rules that are individually enabled AND global policy is on."""
        if not self.enabled:
            return ()
        return tuple(r for r in self.rules if r.enabled)


# ---------------------------------------------------------------------------
# Truth gate
# ---------------------------------------------------------------------------

def truth_level_satisfies_gate(level: TruthLevel, gate: str) -> bool:
    """Return True if ``level`` meets the minimum requirement for ``gate``.

    >>> truth_level_satisfies_gate(TruthLevel.SOURCE_VALIDATED, "same_system")
    True
    >>> truth_level_satisfies_gate(TruthLevel.RAW_OBSERVED, "same_system")
    False
    >>> truth_level_satisfies_gate(TruthLevel.CORROBORATED, "cross_system")
    True
    >>> truth_level_satisfies_gate(TruthLevel.SOURCE_VALIDATED, "cross_system")
    False
    """
    minimum = _GATE_MIN_TRUTH.get(gate)
    if minimum is None:
        return False
    try:
        return _TRUTH_ORDER.index(level) >= _TRUTH_ORDER.index(minimum)
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# YAML loading / schema validation
# ---------------------------------------------------------------------------

def _parse_rule(raw: dict[str, Any]) -> ActuationRule:
    """Parse and validate a single rule dict from YAML into ActuationRule."""
    for key in ("id", "adapter", "operation", "gate"):
        if key not in raw:
            raise ValueError(f"Actuation rule missing required field: {key!r}")

    gate = raw["gate"]
    if gate not in _VALID_GATES:
        raise ValueError(f"Rule {raw['id']!r}: invalid gate {gate!r}; must be one of {sorted(_VALID_GATES)}")

    operation = raw["operation"]
    if operation not in _VALID_OPERATIONS:
        raise ValueError(f"Rule {raw['id']!r}: invalid operation {operation!r}; must be one of {sorted(_VALID_OPERATIONS)}")

    trigger_kind = raw.get("trigger_kind", "entity_condition")
    if trigger_kind not in _VALID_TRIGGER_KINDS:
        raise ValueError(f"Rule {raw['id']!r}: invalid trigger_kind {trigger_kind!r}")

    trigger = raw.get("trigger", {})
    condition_predicate = trigger.get("condition_predicate", "")

    # A-12: validate predicate id is registered (unknown ids refused at load)
    if condition_predicate and condition_predicate not in _CONDITION_PREDICATES:
        raise ValueError(
            f"Rule {raw['id']!r}: unknown condition_predicate {condition_predicate!r}. "
            f"Register it in actuation_engine._CONDITION_PREDICATES first."
        )

    rate_cap_raw = raw.get("rate_cap", {})
    return ActuationRule(
        id=raw["id"],
        adapter=raw["adapter"],
        operation=operation,
        gate=gate,
        trigger_kind=trigger_kind,
        trigger_entity_type=trigger.get("entity_type", ""),
        trigger_condition_predicate=condition_predicate,
        gap_id=raw.get("gap_id", ""),
        rate_cap=ActuationRateCap(
            per_program_per_run=int(rate_cap_raw.get("per_program_per_run", 10))
        ),
        enabled=bool(raw.get("enabled", False)),
    )


def load_actuation_policy(
    program_id: str,
    *,
    policies_root: Path | None = None,
    programs_root: Path | None = None,
) -> ActuationPolicy:
    """Load the global actuation policy with optional per-program overrides.

    Per-program overrides live at:
      programs/<program_id>/policies/actuation_rules.yaml

    A per-program file may:
    - Set actuation.enabled: true/false (per-program gate)
    - Override or add rules by id (merge by rule id; last write wins)
    - Must NOT set approval_ttl_hours below 1
    """
    from src.core.program_fact_store import PROGRAMS_ROOT as _PROG_ROOT

    resolved_policies_root = policies_root or _POLICIES_ROOT
    global_path = resolved_policies_root / _GLOBAL_RULES_FILE

    if not global_path.exists():
        return ActuationPolicy(
            schema_version="1",
            enabled=False,
            approval_ttl_hours=24,
            per_adapter_per_run_cap=10,
            rules=(),
        )

    with global_path.open() as fh:
        raw = yaml.safe_load(fh) or {}

    if "policy_schema_version" not in raw:
        raise ValueError(f"{global_path}: missing required 'policy_schema_version' field")

    actuation_cfg = raw.get("actuation", {})
    rate_caps = actuation_cfg.get("rate_caps", {})
    enabled = bool(actuation_cfg.get("enabled", False))
    approval_ttl_hours = int(actuation_cfg.get("approval_ttl_hours", 24))

    rules_by_id: dict[str, ActuationRule] = {}
    for r in raw.get("rules", []):
        rule = _parse_rule(r)
        rules_by_id[rule.id] = rule

    # Per-program merge
    resolved_programs_root = programs_root or _PROG_ROOT
    per_program_path = resolved_programs_root / program_id / "policies" / "actuation_rules.yaml"
    if per_program_path.exists():
        with per_program_path.open() as fh:
            per_raw = yaml.safe_load(fh) or {}
        per_actuation = per_raw.get("actuation", {})
        if "enabled" in per_actuation:
            enabled = bool(per_actuation["enabled"])
        for r in per_raw.get("rules", []):
            rule = _parse_rule(r)
            rules_by_id[rule.id] = rule  # Override or add

    return ActuationPolicy(
        schema_version=str(raw["policy_schema_version"]),
        enabled=enabled,
        approval_ttl_hours=max(1, approval_ttl_hours),
        per_adapter_per_run_cap=max(1, int(rate_caps.get("per_adapter_per_run", 10))),
        rules=tuple(rules_by_id.values()),
    )


# ---------------------------------------------------------------------------
# Condition predicates (A-12: ids only, never expression strings)
# ---------------------------------------------------------------------------

def _predicate_ado_open_but_evidence_resolved(assessment: Any, reality: Any) -> bool:
    """Fires when an action item is done in Vertex but not yet closed in ADO.

    Criterion: record has done/closed status AND truth level is RAW_OBSERVED,
    meaning the source system (ADO) hasn't confirmed the closure yet.
    """
    from src.core.models_v2 import ActionItem
    if not isinstance(assessment.record, ActionItem):
        return False
    status = getattr(assessment.record, "status", None)
    status_val = getattr(status, "value", str(status or "")).lower()
    if status_val not in ("done", "closed", "completed", "resolved"):
        return False
    return assessment.truth_level == TruthLevel.RAW_OBSERVED


def _predicate_linked_commitment_slipped(assessment: Any, reality: Any) -> bool:
    """Fires when a work item is linked to a slipped commitment.

    Looks for a COMMITMENT_SLIPPED attention item whose record matches
    the assessment being evaluated.
    """
    from src.core.models_v2 import ActionItem, Milestone
    if not isinstance(assessment.record, (ActionItem, Milestone)):
        return False
    record_id = str(getattr(assessment.record, "id", ""))
    if not record_id:
        return False
    for item in reality.attention():
        if item.kind != "commitment_slipped":
            continue
        if item.record is None:
            continue
        linked_id = str(getattr(item.record.record, "id", "") if hasattr(item.record, "record") else getattr(item.record, "id", ""))
        if linked_id == record_id:
            return True
    return False


# Predicate registry (A-12: code-backed, not evaluated)
_CONDITION_PREDICATES: dict[str, Any] = {
    "ado_open_but_evidence_resolved": _predicate_ado_open_but_evidence_resolved,
    "linked_commitment_slipped": _predicate_linked_commitment_slipped,
}


# ---------------------------------------------------------------------------
# Proposal derivation helpers
# ---------------------------------------------------------------------------

def _collect_terminal_failures(snapshot: Any) -> set[str]:
    """Return set of entity_refs that have terminal action.failed facts.

    Terminal failures suppress re-derivation until evidence changes (v3.2).
    """
    terminal: set[str] = set()
    for fact in snapshot.facts:
        if fact.fact_type == "action.failed":
            if fact.payload.get("terminal", False):
                for ref in fact.entity_refs:
                    terminal.add(ref)
    return terminal


def _derive_area_path(reality: Any) -> str | None:
    """Derive the ADO area path from workstream configuration.

    Returns None if no area path is configured — triggering missing_area_path
    gap_reason on work_item_create proposals (R-19, v3.2).
    """
    for ws_assessment in reality.workstreams():
        ws = ws_assessment.record
        area_paths = getattr(ws, "area_paths", ())
        if area_paths:
            return str(area_paths[0])
    # Fallback: check snapshot facts directly
    for fact in reality._snapshot.facts:
        if fact.fact_type == "workstream.entry":
            area_path = fact.payload.get("ado_area_path") or fact.payload.get("area_path")
            if area_path:
                return str(area_path)
    return None


def _build_proposal(
    *,
    rule: ActuationRule,
    entity_ref: str,
    payload: dict[str, Any],
    now: datetime,
    gap_reason: str = "",
    approval_ttl_hours: int = 24,
) -> Any:
    """Build an ActuationProposal. Imported lazily to avoid circular dep."""
    from src.core.program_reality import ActuationProposal
    return ActuationProposal(
        proposal_id=str(uuid.uuid4()),
        rule_id=rule.id,
        adapter=rule.adapter,
        operation=rule.operation,
        entity_ref=entity_ref,
        payload={**payload, "approval_ttl_hours": approval_ttl_hours},
        proposed_at=now,
        approved=False,
        gap_reason=gap_reason,
    )


def _derive_entity_proposals(
    rule: ActuationRule,
    reality: Any,
    now: datetime,
    terminal_failed: set[str],
    policy: ActuationPolicy,
) -> list[Any]:
    """Derive proposals for entity_condition type rules."""
    predicate_fn = _CONDITION_PREDICATES.get(rule.trigger_condition_predicate)
    if predicate_fn is None:
        return []

    # Collect candidate assessments by entity_type
    candidates: list[Any] = []
    if rule.trigger_entity_type == "work_item":
        candidates = list(reality.actions()) + list(reality.milestones())
    elif rule.trigger_entity_type == "risk":
        candidates = list(reality.risks())

    proposals = []
    for assessment in candidates:
        entity_ref = str(getattr(assessment.record, "id", ""))
        if not entity_ref:
            continue
        if entity_ref in terminal_failed:
            continue
        if not predicate_fn(assessment, reality):
            continue
        if not truth_level_satisfies_gate(assessment.truth_level, rule.gate):
            continue

        proposals.append(_build_proposal(
            rule=rule,
            entity_ref=entity_ref,
            payload={
                "entity_type": rule.trigger_entity_type,
                "record_id": entity_ref,
                "condition_predicate": rule.trigger_condition_predicate,
            },
            now=now,
            approval_ttl_hours=policy.approval_ttl_hours,
        ))

    return proposals


def _derive_gap_proposals(
    rule: ActuationRule,
    reality: Any,
    now: datetime,
    terminal_failed: set[str],
    policy: ActuationPolicy,
) -> list[Any]:
    """Derive proposals for structural_gap type rules."""
    proposals = []
    seen_descriptions: set[str] = set()

    for attn_item in reality.attention():
        desc = attn_item.description or ""
        # Match on gap_id prefix (attention items start with "critical_risk_no_mitigation: ...")
        if not desc.startswith(rule.gap_id):
            continue
        if desc in seen_descriptions:
            continue
        seen_descriptions.add(desc)

        record = attn_item.record
        truth_level = record.truth_level if record is not None else TruthLevel.RAW_OBSERVED
        if not truth_level_satisfies_gate(truth_level, rule.gate):
            continue

        entity_ref = str(record.fact_id or "") if record is not None else ""
        if entity_ref and entity_ref in terminal_failed:
            continue

        # work_item_create: derive area_path or block with gap_reason (R-19, v3.2)
        gap_reason = ""
        area_path = ""
        if rule.operation == "work_item_create":
            derived = _derive_area_path(reality)
            if derived is None:
                gap_reason = "missing_area_path"
            else:
                area_path = derived

        proposals.append(_build_proposal(
            rule=rule,
            entity_ref=entity_ref,
            payload={
                "gap_id": rule.gap_id,
                "area_path": area_path,
                "description": desc,
            },
            now=now,
            gap_reason=gap_reason,
            approval_ttl_hours=policy.approval_ttl_hours,
        ))

    return proposals


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def derive_proposals(
    reality: Any,
    policy: ActuationPolicy,
    *,
    as_of: datetime | None = None,
) -> tuple:
    """Derive actuation proposals from program reality and policy.

    Returns an empty tuple when policy.enabled is False (global gate).
    Also returns empty when no rules are individually enabled.

    ``reality`` must expose: .actions(), .milestones(), .risks(), .workstreams(),
    .attention(), ._snapshot (with .facts), typed as Any to avoid circular import.
    """
    if not policy.enabled:
        return ()

    enabled = policy.enabled_rules
    if not enabled:
        return ()

    now = as_of or datetime.now(timezone.utc)
    terminal_failed = _collect_terminal_failures(reality._snapshot)

    proposals: list[Any] = []
    adapter_run_counts: dict[str, int] = {}

    for rule in enabled:
        # Per-adapter-per-run cap
        if adapter_run_counts.get(rule.adapter, 0) >= policy.per_adapter_per_run_cap:
            continue

        if rule.trigger_kind == "structural_gap":
            rule_proposals = _derive_gap_proposals(rule, reality, now, terminal_failed, policy)
        else:  # entity_condition
            rule_proposals = _derive_entity_proposals(rule, reality, now, terminal_failed, policy)

        # Apply per-rule rate cap
        rule_proposals = rule_proposals[: rule.rate_cap.per_program_per_run]

        for p in rule_proposals:
            if adapter_run_counts.get(rule.adapter, 0) >= policy.per_adapter_per_run_cap:
                break
            proposals.append(p)
            adapter_run_counts[rule.adapter] = adapter_run_counts.get(rule.adapter, 0) + 1

    return tuple(proposals)


# ---------------------------------------------------------------------------
# Post-execution helpers (WI-7.2)
# ---------------------------------------------------------------------------

def build_reverse_proposal(
    original: Any,
    error_trace: str,
    *,
    now: datetime | None = None,
) -> Any:
    """Build a corrective/reverse proposal when execution verification fails.

    The reverse proposal carries the original operation context and a
    ``reverse_of`` back-reference so the review queue can display it as
    "change didn't stick — re-propose or withdraw?" (§6.11.1 v3.1).

    The reverse proposal is never auto-executed; it requires fresh human
    approval before it enters the execute phase.
    """
    from src.core.program_reality import ActuationProposal
    now = now or datetime.now(timezone.utc)
    return ActuationProposal(
        proposal_id=str(uuid.uuid4()),
        rule_id=original.rule_id,
        adapter=original.adapter,
        operation=original.operation,
        entity_ref=original.entity_ref,
        payload={**original.payload, "reverse_of": original.proposal_id, "error_trace": error_trace},
        proposed_at=now,
        approved=False,
        gap_reason="",
    )


def build_terminal_failure_fact(
    proposal: Any,
    error_trace: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a dict representing an ``action.failed`` terminal-state fact.

    This is a plain dict so actuation_engine stays decoupled from the fact
    store's concrete types.  The caller is responsible for persisting it via
    the appropriate store.  ``terminal: True`` suppresses re-derivation for
    the entity until its underlying evidence changes (§6.11.1 v3.2).
    """
    now = now or datetime.now(timezone.utc)
    return {
        "fact_type": "action.failed",
        "entity_refs": [proposal.entity_ref],
        "payload": {
            "rule_id": proposal.rule_id,
            "proposal_id": proposal.proposal_id,
            "adapter": proposal.adapter,
            "operation": proposal.operation,
            "error_trace": error_trace,
            "terminal": True,
        },
        "occurred_at": now.isoformat(),
    }


def is_requeue_not_drop(proposal: Any, policy: ActuationPolicy, *, now: datetime | None = None) -> bool:
    """Return True if an expired/invalid proposal should be re-queued (not executed).

    A proposal is stale when its TTL has elapsed.  Stale proposals are
    re-queued (put back into pending-approval state) rather than silently
    dropped or executed with degraded inputs (§6.11.1).
    """
    now = now or datetime.now(timezone.utc)
    ttl_hours = policy.approval_ttl_hours
    if hasattr(proposal, "proposed_at") and proposal.proposed_at is not None:
        age_hours = (now - proposal.proposed_at).total_seconds() / 3600
        return age_hours > ttl_hours
    return False
