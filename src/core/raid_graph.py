from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path

from src.core.journal import PROGRAMS_ROOT
from src.core.models_v2 import ActionItem, ActionStatus, Assumption, DecisionEntry, RaidChainLink, RiskEntry
from src.core.program_fact_store import (
    load_program_facts,
    project_action_items,
    project_assumptions,
    project_decision_entries,
    project_risk_entries,
)
_MITIGATING_ACTION_STATUSES = {ActionStatus.IN_PROGRESS, ActionStatus.DONE}
_TrailKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class RaidChainResult:
    risk_id: str
    links: tuple[RaidChainLink, ...]
    has_mitigating_action: bool
    warnings: tuple[str, ...] = ()


def build_raid_chain_index(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> dict[str, RaidChainResult]:
    risks = _load_current_risks(program_id, programs_root=programs_root)
    actions = _load_current_actions(program_id, programs_root=programs_root)
    decisions = _load_current_decisions(program_id, programs_root=programs_root)
    assumptions = _load_current_assumptions(program_id, programs_root=programs_root)
    return build_raid_chain_index_from_entries(
        risks=risks,
        actions=actions,
        decisions=decisions,
        assumptions=assumptions,
    )


def _load_current_actions(program_id: str, *, programs_root: Path) -> tuple[ActionItem, ...]:
    return project_action_items(
        load_program_facts(
            program_id,
            programs_root=programs_root,
            fact_types=("action.item",),
        )
    )


def _load_current_assumptions(program_id: str, *, programs_root: Path) -> tuple[Assumption, ...]:
    return project_assumptions(
        load_program_facts(
            program_id,
            programs_root=programs_root,
            fact_types=("assumption.entry",),
        )
    )


def _load_current_decisions(program_id: str, *, programs_root: Path) -> tuple[DecisionEntry, ...]:
    return project_decision_entries(
        load_program_facts(
            program_id,
            programs_root=programs_root,
            fact_types=("decision.entry",),
        )
    )


def _load_current_risks(program_id: str, *, programs_root: Path) -> tuple[RiskEntry, ...]:
    return project_risk_entries(
        load_program_facts(
            program_id,
            programs_root=programs_root,
            fact_types=("risk.entry",),
        )
    )


def build_raid_chain_index_from_entries(
    *,
    risks: tuple[RiskEntry, ...],
    actions: tuple[ActionItem, ...],
    decisions: tuple[DecisionEntry, ...],
    assumptions: tuple[Assumption, ...],
) -> dict[str, RaidChainResult]:
    action_by_id = {entry.id: entry for entry in actions}
    decision_by_id = {entry.id: entry for entry in decisions}
    assumptions_by_risk: dict[str, list[Assumption]] = {}
    actions_by_risk: dict[str, list[ActionItem]] = {}
    decisions_by_risk: dict[str, list[DecisionEntry]] = {}
    decisions_by_action: dict[str, list[DecisionEntry]] = {}

    for assumption in assumptions:
        if assumption.linked_risk_id is not None:
            assumptions_by_risk.setdefault(assumption.linked_risk_id, []).append(assumption)

    for action in actions:
        if action.linked_risk_id is not None:
            actions_by_risk.setdefault(action.linked_risk_id, []).append(action)

    for decision in decisions:
        if decision.linked_risk_id is not None:
            decisions_by_risk.setdefault(decision.linked_risk_id, []).append(decision)
        for action_id in decision.linked_action_ids:
            decisions_by_action.setdefault(action_id, []).append(decision)

    chain_index: dict[str, RaidChainResult] = {}
    for risk in risks:
        explicit_actions = tuple(
            action_by_id[action_id]
            for action_id in risk.linked_action_ids
            if action_id in action_by_id
        )
        risk_actions = _dedupe_actions(tuple(actions_by_risk.get(risk.id, ())) + explicit_actions)
        risk_decisions = tuple(sorted(decisions_by_risk.get(risk.id, ()), key=lambda entry: entry.id))
        risk_assumptions = tuple(sorted(assumptions_by_risk.get(risk.id, ()), key=lambda entry: entry.id))
        chain_index[risk.id] = _build_risk_chain(
            risk,
            risk_actions=risk_actions,
            risk_decisions=risk_decisions,
            risk_assumptions=risk_assumptions,
            decisions_by_action=decisions_by_action,
            action_by_id=action_by_id,
            decision_by_id=decision_by_id,
        )
    return chain_index


def _build_risk_chain(
    risk: RiskEntry,
    *,
    risk_actions: tuple[ActionItem, ...],
    risk_decisions: tuple[DecisionEntry, ...],
    risk_assumptions: tuple[Assumption, ...],
    decisions_by_action: dict[str, list[DecisionEntry]],
    action_by_id: dict[str, ActionItem],
    decision_by_id: dict[str, DecisionEntry],
) -> RaidChainResult:
    queue: deque[tuple[str, str, int, tuple[_TrailKey, ...]]] = deque(
        [("risk", risk.id, 0, (("risk", risk.id),))]
    )
    visited: set[_TrailKey] = set()
    links: list[RaidChainLink] = []
    warnings: list[str] = []
    seen_warnings: set[str] = set()
    has_mitigating_action = any(action.status in _MITIGATING_ACTION_STATUSES for action in risk_actions)

    while queue:
        node_type, node_id, hop, trail = queue.popleft()
        key = (node_type, node_id)
        if key in visited:
            continue
        visited.add(key)
        links.append(
            _build_chain_link(
                node_type=node_type,
                node_id=node_id,
                risk=risk,
                action_by_id=action_by_id,
                decision_by_id=decision_by_id,
                assumption_by_id={entry.id: entry for entry in risk_assumptions},
                hop=hop,
            )
        )

        for neighbor_type, neighbor_id in _iter_neighbors(
            node_type=node_type,
            node_id=node_id,
            risk_actions=risk_actions,
            risk_decisions=risk_decisions,
            risk_assumptions=risk_assumptions,
            decisions_by_action=decisions_by_action,
            action_by_id=action_by_id,
            decision_by_id=decision_by_id,
        ):
            neighbor_key = (neighbor_type, neighbor_id)
            if neighbor_key in trail:
                warning = _format_cycle_warning(trail, neighbor_key)
                if warning not in seen_warnings:
                    warnings.append(warning)
                    seen_warnings.add(warning)
                continue
            if neighbor_key in visited:
                continue
            queue.append((neighbor_type, neighbor_id, hop + 1, trail + (neighbor_key,)))

    return RaidChainResult(
        risk_id=risk.id,
        links=tuple(links),
        has_mitigating_action=has_mitigating_action,
        warnings=tuple(warnings),
    )


def _build_chain_link(
    *,
    node_type: str,
    node_id: str,
    risk: RiskEntry,
    action_by_id: dict[str, ActionItem],
    decision_by_id: dict[str, DecisionEntry],
    assumption_by_id: dict[str, Assumption],
    hop: int,
) -> RaidChainLink:
    if node_type == "risk":
        return RaidChainLink(node_id=risk.id, node_type="risk", title=risk.title, status=risk.status.value, hop=hop)
    if node_type == "action":
        action = action_by_id[node_id]
        return RaidChainLink(node_id=action.id, node_type="action", title=action.text, status=action.status.value, hop=hop)
    if node_type == "decision":
        decision = decision_by_id[node_id]
        return RaidChainLink(node_id=decision.id, node_type="decision", title=decision.title, status=decision.status.value, hop=hop)
    assumption = assumption_by_id[node_id]
    return RaidChainLink(node_id=assumption.id, node_type="assumption", title=assumption.text, status=assumption.status.value, hop=hop)


def _iter_neighbors(
    *,
    node_type: str,
    node_id: str,
    risk_actions: tuple[ActionItem, ...],
    risk_decisions: tuple[DecisionEntry, ...],
    risk_assumptions: tuple[Assumption, ...],
    decisions_by_action: dict[str, list[DecisionEntry]],
    action_by_id: dict[str, ActionItem],
    decision_by_id: dict[str, DecisionEntry],
) -> tuple[_TrailKey, ...]:
    if node_type == "risk":
        return (
            tuple(("assumption", entry.id) for entry in risk_assumptions)
            + tuple(("action", entry.id) for entry in risk_actions)
            + tuple(("decision", entry.id) for entry in risk_decisions)
        )
    if node_type == "action":
        linked_decisions = tuple(sorted(decisions_by_action.get(node_id, ()), key=lambda entry: entry.id))
        return tuple(("decision", entry.id) for entry in linked_decisions)
    if node_type == "decision":
        decision = decision_by_id[node_id]
        linked_actions = tuple(
            sorted(
                (action_by_id[action_id] for action_id in decision.linked_action_ids if action_id in action_by_id),
                key=lambda entry: entry.id,
            )
        )
        return tuple(("action", entry.id) for entry in linked_actions)
    return ()


def _dedupe_actions(actions: tuple[ActionItem, ...]) -> tuple[ActionItem, ...]:
    deduped: dict[str, ActionItem] = {}
    for action in actions:
        deduped[action.id] = action
    return tuple(sorted(deduped.values(), key=lambda entry: entry.id))


def _format_cycle_warning(trail: tuple[_TrailKey, ...], repeated_key: _TrailKey) -> str:
    path = " -> ".join(node_id for _node_type, node_id in trail + (repeated_key,))
    return f"Cycle detected in RAID chain: {path}. Chain truncated at cycle entry."