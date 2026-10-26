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
from urllib.parse import urlsplit

from src.core.integration_types import (
    IntegrationError,
    PaginationOutcome,
    RelationKind,
    RelationTargetKind,
    WorkItemRelation,
)


# --------------------------------------------------------------------------------------
# Raw -> typed parsing
# --------------------------------------------------------------------------------------

#: RELO-W2.1 (specs/relo.md Sec 8.3.2): the current provider reference name
#: for this link type is ``System.LinkTypes.Dependency-Forward``/
#: ``Dependency-Reverse`` (the same ``-Forward``/``-Reverse`` suffix
#: convention every other ADO link-type family in this map already uses --
#: Hierarchy, and, per Microsoft's public work-item link-type reference,
#: Dependency too). ``DependencyPredecessor``/``DependencySuccessor`` (no
#: ``-Forward``/``-Reverse`` suffix at all) do not match that convention and
#: are not a real ADO ``System.LinkTypes.*`` reference name; kept below,
#: unchanged, only because this repo's own existing fixtures/tests already
#: encode them and removing a recognized type is a regression this fix does
#: not need to make. Before this fix, a real dependency edge using the
#: correct reference name was silently classified UNKNOWN -- not merely
#: mis-directed, but not recognized as a dependency at all.
#:
#: Direction mirrors the already-verified Hierarchy convention (module
#: docstring; also independently confirmed against
#: ``dependency_graph.py:889-901``'s own worked example): ``-Forward`` is
#: not flipped (source's own view -- "I am the predecessor/parent"),
#: ``-Reverse`` is expressed from the other side and flipped at parse
#: ("I am the successor/child").
_DEPENDENCY_FORWARD_NAME = "System.LinkTypes.Dependency-Forward"
_DEPENDENCY_REVERSE_NAME = "System.LinkTypes.Dependency-Reverse"

#: ADO ``rel`` attribute prefixes mapped to typed kinds. ``-Reverse`` suffix
#: means the edge is expressed from the child/successor side (flipped at parse).
_LINK_TYPE_MAP: dict[str, tuple[RelationKind, str]] = {
    "System.LinkTypes.Hierarchy-Forward": (RelationKind.HIERARCHY_PARENT, "forward"),
    "System.LinkTypes.Hierarchy-Reverse": (RelationKind.HIERARCHY_CHILD, "reverse"),
    "System.LinkTypes.Related": (RelationKind.RELATED, "forward"),
    _DEPENDENCY_FORWARD_NAME: (RelationKind.PREDECESSOR, "forward"),
    _DEPENDENCY_REVERSE_NAME: (RelationKind.SUCCESSOR, "reverse"),
    "System.LinkTypes.DependencyPredecessor": (RelationKind.PREDECESSOR, "forward"),
    "System.LinkTypes.DependencySuccessor": (RelationKind.SUCCESSOR, "forward"),
    "System.LinkTypes.ArtifactLink": (RelationKind.ARTIFACT_LINK, "forward"),
    "System.LinkTypes.ExternalLink": (RelationKind.EXTERNAL_LINK, "forward"),
}

#: RELO-W2.1: "enumerate and version the provider's relation-types
#: metadata" (Sec 8.3.2). A raw ``rel`` name outside this set is UNKNOWN
#: and must not be silently absent. Parsing preserves the raw name; traversal
#: retains the edge as evidence, reports quarantine, and does not follow it.
KNOWN_RELATION_TYPES_VERSION = "1"
KNOWN_RELATION_TYPES: frozenset[str] = frozenset(_LINK_TYPE_MAP)


def _classify_rel(rel_type_name: str) -> tuple[RelationKind, str]:
    """Map a raw ADO ``rel`` to (kind, direction). Unknown rels -> UNKNOWN/forward."""
    if rel_type_name in _LINK_TYPE_MAP:
        return _LINK_TYPE_MAP[rel_type_name]
    # Exact short names are aliases; a custom name sharing a suffix is unseen.
    return _LINK_TYPE_MAP.get(f"System.LinkTypes.{rel_type_name}", (RelationKind.UNKNOWN, "forward"))


def _parse_target(url: str | None) -> tuple[RelationTargetKind, str, str | None]:
    """Classify an ADO relation ``url`` into (target_kind, target_id, target_type).

    ADO relation URLs look like:
      - ``vstfs:///WorkItemTracking/WorkItem/123``  -> work item 123
      - ``https://.../_apis/wit/workItems/123``     -> work item 123
      - ``vstfs:///Git/Commit/...``                  -> artifact
      - ``https://...``                              -> external
    """
    if not url:
        return (RelationTargetKind.EXTERNAL, url or "", None)
    lowered = url.lower()
    try:
        parsed = urlsplit(url)
    except ValueError:
        return (RelationTargetKind.EXTERNAL, url, None)
    parts = parsed.path.rstrip("/").split("/")
    is_vstfs_work_item = (
        parsed.scheme.lower() == "vstfs"
        and not parsed.netloc
        and [part.lower() for part in parts[:-1]] == ["", "workitemtracking", "workitem"]
    )
    is_rest_work_item = (
        parsed.scheme.lower() in {"http", "https"}
        and bool(parsed.netloc)
        and [part.lower() for part in parts[-4:-1]] == ["_apis", "wit", "workitems"]
    )
    if (is_vstfs_work_item or is_rest_work_item) and parts[-1].isdigit():
        return (RelationTargetKind.WORK_ITEM, parts[-1], None)
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


def relation_quarantine_errors(relations: Iterable[WorkItemRelation]) -> tuple[IntegrationError, ...]:
    """Describe unseen relation types without authorizing their semantics."""
    return tuple(
        IntegrationError(
            source="ado",
            stage="relations",
            message=(
                f"Relation {edge.rel_type_name!r} from work item {edge.source_work_item_id} "
                f"to {edge.target_kind.value}:{edge.target_id} quarantined: "
                f"unrecognized by relation inventory v{KNOWN_RELATION_TYPES_VERSION}"
            ),
            retryable=False,
            operator_action="Review provider relation metadata and captured fixtures before enabling this relation type.",
            ref_id=str(edge.source_work_item_id),
            ref_kind="work_item",
        )
        for edge in relations
        if edge.relation_kind is RelationKind.UNKNOWN
    )


# --------------------------------------------------------------------------------------
# Budgeted traversal
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RelationTraversalResult:
    """Result of a budgeted relation-graph traversal (Section 8.4.3).

    ``edges`` carries the discovered typed edges with ``depth`` populated.
    ``visited_ids`` is the set of work-item ids reached. ``truncation`` is
    non-None when a budget (max depth, max nodes, max edges) was hit while the
    frontier still had unreached nodes. ``errors`` reports unknown edges retained
    in ``edges`` as quarantined evidence, never followed to another work item.
    """

    edges: tuple[WorkItemRelation, ...]
    visited_ids: frozenset[int]
    truncation: PaginationOutcome | None
    errors: tuple[IntegrationError, ...] = ()


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
            target_wid = (
                _edge_target_work_item_id(edge)
                if edge.relation_kind is not RelationKind.UNKNOWN
                else None
            )
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
        errors=relation_quarantine_errors(out_edges),
    )


__all__ = [
    "KNOWN_RELATION_TYPES",
    "KNOWN_RELATION_TYPES_VERSION",
    "RelationTraversalResult",
    "parse_relations_for_work_item",
    "parse_relations_payload",
    "relation_quarantine_errors",
    "traverse_relations",
]
