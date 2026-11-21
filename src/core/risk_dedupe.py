"""RELO-W4.3: deterministic risk dedupe keys and observe-only duplicate/supersede findings."""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Any, TypeVar, TypedDict, cast

from src.core.models_v2 import RiskEntry, RiskStatus

_T = TypeVar("_T")

RISK_LIFECYCLE_FIELDS = (
    "dedupe_key",
    "supersedes",
    "superseded_by",
    "escalation_state",
    "contingency_trigger",
    "residual_risk",
    "outcome",
    "effectiveness",
)


@dataclass(frozen=True, slots=True)
class RiskDedupeFinding:
    kind: str
    risk_ids: tuple[str, ...]
    dedupe_key: str | None = None


class RiskLifecycleFields(TypedDict, total=False):
    dedupe_key: str
    supersedes: str
    superseded_by: str
    escalation_state: str
    contingency_trigger: str
    residual_risk: str
    outcome: str
    effectiveness: str


def active_risk_entries(entries: tuple[RiskEntry, ...] | list[RiskEntry]) -> tuple[RiskEntry, ...]:
    """Return the compact current risk working set used by reporting consumers."""
    return tuple(
        entry
        for entry in entries
        if entry.status in {RiskStatus.OPEN, RiskStatus.ESCALATED}
    )


def merge_inactive_duplicate_provenance(
    entries: tuple[RiskEntry, ...],
) -> tuple[tuple[RiskEntry, ...], dict[str, str]]:
    """Merge provenance from inactive duplicates into one unambiguous active record."""
    groups: dict[str, list[RiskEntry]] = {}
    for entry in entries:
        groups.setdefault(entry.dedupe_key or derive_risk_dedupe_key(entry), []).append(entry)

    merged_into: dict[str, str] = {}
    replacement: dict[str, RiskEntry] = {}
    for group in groups.values():
        active = [entry for entry in group if entry.status in {RiskStatus.OPEN, RiskStatus.ESCALATED}]
        inactive = [entry for entry in group if entry.status in {
            RiskStatus.CLOSED, RiskStatus.MITIGATED, RiskStatus.ACCEPTED,
        }]
        if len(active) != 1 or not inactive:
            continue
        current = active[0]
        for previous in sorted(inactive, key=lambda entry: entry.id):
            current = replace(
                current,
                linked_workstream_ids=_merge_unique(current.linked_workstream_ids, previous.linked_workstream_ids),
                linked_work_item_ids=_merge_unique(current.linked_work_item_ids, previous.linked_work_item_ids),
                linked_milestone_ids=_merge_unique(current.linked_milestone_ids, previous.linked_milestone_ids),
                linked_claim_ids=_merge_unique(current.linked_claim_ids, previous.linked_claim_ids),
                linked_action_ids=_merge_unique(current.linked_action_ids, previous.linked_action_ids),
                entity_refs=_merge_unique(current.entity_refs, previous.entity_refs),
                source_signal_ids=_merge_unique(current.source_signal_ids, previous.source_signal_ids),
                supersedes=current.supersedes or previous.id,
                residual_risk=current.residual_risk or previous.residual_risk,
                outcome=current.outcome or previous.outcome,
                effectiveness=current.effectiveness or previous.effectiveness,
            )
            merged_into[previous.id] = current.id
        replacement[current.id] = current
    return (
        tuple(replacement.get(entry.id, entry) for entry in entries),
        merged_into,
    )


def _merge_unique(current: tuple[_T, ...], retained: tuple[_T, ...]) -> tuple[_T, ...]:
    return tuple(dict.fromkeys((*current, *retained)))


def risk_lifecycle_record(entry: RiskEntry) -> dict[str, str]:
    return {name: value for name in RISK_LIFECYCLE_FIELDS if (value := getattr(entry, name)) is not None}


def parse_risk_lifecycle_fields(raw: dict[str, Any]) -> RiskLifecycleFields:
    return cast(
        RiskLifecycleFields,
        {name: str(raw[name]) for name in RISK_LIFECYCLE_FIELDS if raw.get(name) not in (None, "")},
    )


def derive_risk_dedupe_key(entry: RiskEntry) -> str:
    title = " ".join(re.sub(r"[^a-z0-9]+", " ", entry.title.lower()).split())
    work_items = ",".join(str(item) for item in sorted(set(entry.linked_work_item_ids)))
    return f"{title}|{entry.owner_alias.strip().lower()}|{work_items}"


def detect_risk_duplicates(entries: tuple[RiskEntry, ...]) -> tuple[RiskDedupeFinding, ...]:
    findings: list[RiskDedupeFinding] = []
    by_key: dict[str, list[str]] = {}
    for entry in entries:
        by_key.setdefault(entry.dedupe_key or derive_risk_dedupe_key(entry), []).append(entry.id)
    for key, ids in by_key.items():
        if len(ids) > 1:
            findings.append(RiskDedupeFinding(kind="duplicate", risk_ids=tuple(sorted(ids)), dedupe_key=key))

    known = {entry.id for entry in entries}
    edges: dict[str, set[str]] = {}
    for entry in sorted(entries, key=lambda e: e.id):
        for ref, is_supersedes in ((entry.supersedes, True), (entry.superseded_by, False)):
            if ref is None:
                continue
            old, new = (ref, entry.id) if is_supersedes else (entry.id, ref)
            if ref not in known:
                findings.append(RiskDedupeFinding(kind="supersede_dangling", risk_ids=(entry.id, ref)))
            else:
                edges.setdefault(old, set()).add(new)

    for old, successors in sorted(edges.items()):
        if len(successors) > 1:
            findings.append(RiskDedupeFinding(
                kind="supersede_fork",
                risk_ids=(old, *sorted(successors)),
            ))
    predecessors: dict[str, set[str]] = {}
    for old, successors in edges.items():
        for new in successors:
            predecessors.setdefault(new, set()).add(old)
    for new, old_entries in sorted(predecessors.items()):
        cyclic_predecessors = edges.get(new, set()) & old_entries
        if len(old_entries) > 1 and not cyclic_predecessors:
            findings.append(RiskDedupeFinding(
                kind="supersede_merge",
                risk_ids=(new, *sorted(old_entries)),
            ))

    successor = {old: sorted(successors)[0] for old, successors in edges.items()}

    visited: set[str] = set()
    for head in sorted(set(successor) - set(successor.values())):
        chain = [head]
        while chain[-1] in successor and successor[chain[-1]] not in chain:
            chain.append(successor[chain[-1]])
        if chain[-1] in successor:
            cycle_start = successor[chain[-1]]
            if cycle_start not in visited:
                cycle = chain[chain.index(cycle_start):]
                pivot = cycle.index(min(cycle))
                findings.append(RiskDedupeFinding(
                    kind="supersede_cycle", risk_ids=tuple(cycle[pivot:] + cycle[:pivot]),
                ))
        visited.update(chain)
        findings.append(RiskDedupeFinding(kind="supersede_chain", risk_ids=tuple(chain)))
    for start in sorted(set(successor) - visited):
        if start in visited:
            continue
        cycle = [start]
        while successor[cycle[-1]] != start:
            cycle.append(successor[cycle[-1]])
        visited.update(cycle)
        findings.append(RiskDedupeFinding(kind="supersede_cycle", risk_ids=tuple(cycle)))

    return tuple(sorted(findings, key=lambda f: (f.kind, f.risk_ids)))
