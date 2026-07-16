"""Typed ADO work-item relation parsing and budgeted traversal (ADF-W4.1).

Section 8.4.3 requires: typed edges (hierarchy parent/child, related,
predecessor/successor, artifact links, external links), budgeted graph
traversal (depth 1 for normal gather, depth 2-3 for targeted investigation),
cycle and repeated-node protection, and explicit truncation metadata.

This module turns the raw ADO ``relations`` JSON payload (the ``rel`` attribute
uses ``System.LinkTypes.*`` names) into :class:`WorkItemRelation` records and
runs the budgeted traversal. It is deterministic Zone-A code: no provider SDK,
no AI, no side effects.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from src.core.integration_types import (
    PaginationOutcome,
    RelationKind,
    RelationTargetKind,
    WorkItemRelation,
)


# --------------------------------------------------------------------------------------
# Raw -> typed parsing
# --------------------------------------------------------------------------------------

#: ADO ``rel`` attribute prefixes mapped to typed kinds. ``-Reverse`` suffix
#: means the edge is expressed from the child/successor side (flipped at parse).
_LINK_TYPE_MAP: dict[str, tuple[RelationKind, str]] = {
    "System.LinkTypes.Hierarchy-Forward": (RelationKind.HIERARCHY_PARENT, "forward"),
    "System.LinkTypes.Hierarchy-Reverse": (RelationKind.HIERARCHY_CHILD, "reverse"),
    "System.LinkTypes.Related": (RelationKind.RELATED, "forward"),
    "System.LinkTypes.DependencyPredecessor": (RelationKind.PREDECESSOR, "forward"),
    "System.LinkTypes.DependencySuccessor": (RelationKind.SUCCESSOR, "forward"),
    "System.LinkTypes.ArtifactLink": (RelationKind.ARTIFACT_LINK, "forward"),
    "System.LinkTypes.ExternalLink": (RelationKind.EXTERNAL_LINK, "forward"),
}


def _classify_rel(rel_type_name: str) -> tuple[RelationKind, str]:
    """Map a raw ADO ``rel`` to (kind, direction). Unknown rels -> UNKNOWN/forward."""
    if rel_type_name in _LINK_TYPE_MAP:
        return _LINK_TYPE_MAP[rel_type_name]
    # Handle the fully-qualified vs short form (ADO sometimes omits the prefix).
    short = rel_type_name.split(".", 1)[-1] if "." in rel_type_name else rel_type_name
    for suffix, mapped in (
        ("Hierarchy-Forward", (RelationKind.HIERARCHY_PARENT, "forward")),
        ("Hierarchy-Reverse", (RelationKind.HIERARCHY_CHILD, "reverse")),
        ("Related", (RelationKind.RELATED, "forward")),
        ("DependencyPredecessor", (RelationKind.PREDECESSOR, "forward")),
        ("DependencySuccessor", (RelationKind.SUCCESSOR, "forward")),
        ("ArtifactLink", (RelationKind.ARTIFACT_LINK, "forward")),
        ("ExternalLink", (RelationKind.EXTERNAL_LINK, "forward")),
    ):
        if short.endswith(suffix):
            return mapped
    return (RelationKind.UNKNOWN, "forward")


def _parse_target(url: str | None) -> tuple[RelationTargetKind, str, str | None]:
    """Classify an ADO relation ``url`` into (target_kind, target_id, target_type).

    ADO relation URLs look like:
      - ``vstfs:///WorkItemTracking/WorkItem/123``  -> work item 123
      - ``vstfs:///Git/Commit/...``                  -> artifact
      - ``https://...``                              -> external
    """
    if not url:
        return (RelationTargetKind.EXTERNAL, url or "", None)
    lowered = url.lower()
    if "workitemtracking/workitem" in lowered:
        # The id is the last path segment.
        wid = url.rstrip("/").rsplit("/", 1)[-1]
        return (RelationTargetKind.WORK_ITEM, wid, None)
    if lowered.startswith("vstfs:///"):
        # vstfs:///<Category>/<Type>/<id> -- capture the type segment.
        parts = url.split("/")
        target_type = parts[3] if len(parts) > 3 else None
        return (RelationTargetKind.ARTIFACT, url, target_type)
    return (RelationTargetKind.EXTERNAL, url, None)


def parse_relations_for_work_item(
    work_item_id: int,
    raw_relations: Iterable[Mapping[str, object]],
) -> tuple[WorkItemRelation, ...]:
    """Parse one work item's raw ADO ``relations`` array into typed edges.

    Each raw entry is ``{"rel": "...", "url": "...", "attributes": {...}}``.
    ``attributes.name`` may carry the link's human name; the target title is not
    in the relations payload (it requires a separate fetch), so ``target_title``
    stays ``None`` here for the parser to fill later if a hydration path chooses.
    """
    parsed: list[WorkItemRelation] = []
    for raw in raw_relations:
        if not isinstance(raw, Mapping):
            continue
        rel_type_name = str(raw.get("rel") or "")
        if not rel_type_name:
            continue
        kind, direction = _classify_rel(rel_type_name)
        url = raw.get("url")
        url_str = str(url) if url is not None else None
        target_kind, target_id, target_type = _parse_target(url_str)
        attributes = raw.get("attributes")
        target_title: str | None = None
        if isinstance(attributes, Mapping):
            name = attributes.get("name")
            if isinstance(name, str):
                target_title = name
        parsed.append(
            WorkItemRelation(
                source_work_item_id=work_item_id,
                relation_kind=kind,
                target_kind=target_kind,
                target_id=target_id,
                target_type=target_type,
                target_title=target_title,
                direction=direction,
                rel_type_name=rel_type_name,
            )
        )
    return tuple(parsed)


def parse_relations_payload(
    raw_work_items: Iterable[Mapping[str, object]],
) -> tuple[WorkItemRelation, ...]:
    """Parse the batch ``get_work_item_relations`` payload into typed edges.

    ``raw_work_items`` is the list of work-item JSON dicts (each with an ``id``
    and a ``relations`` array) returned by ``ADOClient.get_work_item_relations``.
    """
    all_relations: list[WorkItemRelation] = []
    for raw in raw_work_items:
        if not isinstance(raw, Mapping):
            continue
        wid = raw.get("id")
        if wid is None:
            continue
        if not isinstance(wid, (int, str, bytes, bytearray)):
            continue
        try:
            work_item_id = int(wid)
        except (TypeError, ValueError):
            continue
        relations = raw.get("relations")
        if isinstance(relations, Iterable):
            all_relations.extend(parse_relations_for_work_item(work_item_id, relations))
    return tuple(all_relations)


# --------------------------------------------------------------------------------------
# Budgeted traversal
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RelationTraversalResult:
    """Result of a budgeted relation-graph traversal (Section 8.4.3).

    ``edges`` carries the discovered typed edges with ``depth`` populated.
    ``visited_ids`` is the set of work-item ids reached. ``truncation`` is
    non-None when a budget (max depth, max nodes, max edges) was hit while the
    frontier still had unreached nodes.
    """

    edges: tuple[WorkItemRelation, ...]
    visited_ids: frozenset[int]
    truncation: PaginationOutcome | None


def _edge_target_work_item_id(edge: WorkItemRelation) -> int | None:
    if edge.target_kind is RelationTargetKind.WORK_ITEM:
        try:
            return int(edge.target_id)
        except (TypeError, ValueError):
            return None
    return None


def traverse_relations(
    relations: Iterable[WorkItemRelation],
    *,
    start_ids: Iterable[int],
    max_depth: int = 1,
    max_nodes: int = 500,
    max_edges: int = 2000,
) -> RelationTraversalResult:
    """BFS over typed relations from ``start_ids`` with cycle/repeat protection.

    Depth 1 (default, Section 8.4.3 "normal gather") collects only the direct
    neighbours of each start node. Depth 2-3 ("targeted dependency
    investigation") follows further. A node is visited at most once (cycle and
    repeated-node protection); hitting ``max_nodes`` or ``max_edges`` while the
    frontier is non-empty sets ``truncation`` rather than silently expanding.
    """
    # Index edges by source for forward traversal.
    temp: dict[int, list[WorkItemRelation]] = {}
    for edge in relations:
        temp.setdefault(edge.source_work_item_id, []).append(edge)
    adjacency: dict[int, tuple[WorkItemRelation, ...]] = {k: tuple(v) for k, v in temp.items()}

    start_set = {int(sid) for sid in start_ids}
    visited: set[int] = set()
    out_edges: list[WorkItemRelation] = []
    frontier: deque[tuple[int, int]] = deque((sid, 0) for sid in start_set)
    truncated = False

    while frontier:
        node, depth = frontier.popleft()
        if node in visited:
            continue
        visited.add(node)
        if len(visited) > max_nodes:
            truncated = True
            break
        if depth >= max_depth:
            continue
        for edge in adjacency.get(node, ()):
            if len(out_edges) >= max_edges:
                truncated = True
                break
            out_edges.append(WorkItemRelation(
                source_work_item_id=edge.source_work_item_id,
                relation_kind=edge.relation_kind,
                target_kind=edge.target_kind,
                target_id=edge.target_id,
                target_type=edge.target_type,
                target_title=edge.target_title,
                direction=edge.direction,
                rel_type_name=edge.rel_type_name,
                depth=depth + 1,
            ))
            target_wid = _edge_target_work_item_id(edge)
            if target_wid is not None and target_wid not in visited:
                frontier.append((target_wid, depth + 1))
        if len(out_edges) >= max_edges:
            truncated = True
            break

    truncation = (
        PaginationOutcome(total_fetched=len(out_edges), page_count=max_depth, is_truncated=True)
        if truncated
        else None
    )
    return RelationTraversalResult(
        edges=tuple(out_edges),
        visited_ids=frozenset(visited),
        truncation=truncation,
    )


__all__ = [
    "RelationTraversalResult",
    "parse_relations_for_work_item",
    "parse_relations_payload",
    "traverse_relations",
]
