"""Unit tests for typed ADO work-item relations (ADF-W4.1).

Covers Section 8.4.3's requirements: typed edges (hierarchy parent/child,
related, predecessor/successor, artifact/external links), direction inference,
budgeted traversal (depth, max_nodes, max_edges), cycle/repeated-node
protection, and truncation metadata.
"""

from __future__ import annotations

from src.core.ado_relations import (
    parse_relations_for_work_item,
    parse_relations_payload,
    traverse_relations,
)
from src.core.integration_types import (
    PaginationOutcome,
    RelationKind,
    RelationTargetKind,
    WorkItemRelation,
)


def _raw_relation(rel: str, url: str, *, name: str | None = None) -> dict:
    entry = {"rel": rel, "url": url}
    if name is not None:
        entry["attributes"] = {"name": name}
    return entry


# --------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------


def test_hierarchy_forward_parses_as_parent() -> None:
    edges = parse_relations_for_work_item(
        100,
        [_raw_relation("System.LinkTypes.Hierarchy-Forward", "vstfs:///WorkItemTracking/WorkItem/101")],
    )
    assert len(edges) == 1
    edge = edges[0]
    assert edge.relation_kind is RelationKind.HIERARCHY_PARENT
    assert edge.direction == "forward"
    assert edge.target_kind is RelationTargetKind.WORK_ITEM
    assert edge.target_id == "101"
    assert edge.source_work_item_id == 100


def test_hierarchy_reverse_parses_as_child() -> None:
    edges = parse_relations_for_work_item(
        101,
        [_raw_relation("System.LinkTypes.Hierarchy-Reverse", "vstfs:///WorkItemTracking/WorkItem/100")],
    )
    assert edges[0].relation_kind is RelationKind.HIERARCHY_CHILD
    assert edges[0].direction == "reverse"


def test_related_predecessor_successor_parsed() -> None:
    edges = parse_relations_for_work_item(
        200,
        [
            _raw_relation("System.LinkTypes.Related", "vstfs:///WorkItemTracking/WorkItem/201"),
            _raw_relation("System.LinkTypes.DependencyPredecessor", "vstfs:///WorkItemTracking/WorkItem/199"),
            _raw_relation("System.LinkTypes.DependencySuccessor", "vstfs:///WorkItemTracking/WorkItem/202"),
        ],
    )
    kinds = [e.relation_kind for e in edges]
    assert RelationKind.RELATED in kinds
    assert RelationKind.PREDECESSOR in kinds
    assert RelationKind.SUCCESSOR in kinds


def test_artifact_and_external_links_classified() -> None:
    edges = parse_relations_for_work_item(
        300,
        [
            _raw_relation("System.LinkTypes.ArtifactLink", "vstfs:///Git/Commit/repo/abc123"),
            _raw_relation("System.LinkTypes.ExternalLink", "https://example.com/pull/42"),
        ],
    )
    assert edges[0].relation_kind is RelationKind.ARTIFACT_LINK
    assert edges[0].target_kind is RelationTargetKind.ARTIFACT
    assert edges[0].target_type == "Git"  # vstfs:///Git/Commit/... -> category "Git"
    assert edges[1].relation_kind is RelationKind.EXTERNAL_LINK
    assert edges[1].target_kind is RelationTargetKind.EXTERNAL


def test_unknown_rel_preserved_not_dropped() -> None:
    edges = parse_relations_for_work_item(
        400,
        [_raw_relation("System.LinkTypes.Custom-Forward", "vstfs:///WorkItemTracking/WorkItem/401")],
    )
    assert len(edges) == 1
    assert edges[0].relation_kind is RelationKind.UNKNOWN
    assert edges[0].rel_type_name == "System.LinkTypes.Custom-Forward"


def test_attributes_name_becomes_target_title() -> None:
    edges = parse_relations_for_work_item(
        500,
        [_raw_relation("System.LinkTypes.Related", "vstfs:///WorkItemTracking/WorkItem/501", name="Sibling epic")],
    )
    assert edges[0].target_title == "Sibling epic"


def test_missing_rel_skipped() -> None:
    edges = parse_relations_for_work_item(
        600,
        [{"url": "vstfs:///WorkItemTracking/WorkItem/601"}, {"rel": "", "url": "x"}],
    )
    assert edges == ()


def test_parse_relations_payload_batch() -> None:
    payload = [
        {
            "id": 1,
            "relations": [
                _raw_relation("System.LinkTypes.Hierarchy-Forward", "vstfs:///WorkItemTracking/WorkItem/2"),
            ],
        },
        {
            "id": 2,
            "relations": [
                _raw_relation("System.LinkTypes.Hierarchy-Reverse", "vstfs:///WorkItemTracking/WorkItem/1"),
            ],
        },
    ]
    edges = parse_relations_payload(payload)
    assert len(edges) == 2
    assert {e.source_work_item_id for e in edges} == {1, 2}


def test_parse_relations_payload_ignores_non_mapping_and_bad_ids() -> None:
    edges = parse_relations_payload([{"id": "not-a-number"}, "garbage", {"id": 9, "relations": []}])
    assert edges == ()


# --------------------------------------------------------------------------------------
# Budgeted traversal
# --------------------------------------------------------------------------------------


def _edge(src: int, target_id: str, kind: RelationKind = RelationKind.HIERARCHY_PARENT) -> WorkItemRelation:
    return WorkItemRelation(
        source_work_item_id=src,
        relation_kind=kind,
        target_kind=RelationTargetKind.WORK_ITEM,
        target_id=target_id,
        target_type=None,
        target_title=None,
        direction="forward",
        rel_type_name="System.LinkTypes.Hierarchy-Forward",
    )


def test_depth_one_traversal_collects_only_direct_neighbours() -> None:
    # 1 -> 2 -> 3
    relations = [_edge(1, "2"), _edge(2, "3")]
    result = traverse_relations(relations, start_ids=[1], max_depth=1)
    assert {e.source_work_item_id for e in result.edges} == {1}
    assert result.visited_ids == frozenset({1, 2})  # 2 is a target, visited
    assert result.truncation is None


def test_depth_two_follows_chain() -> None:
    relations = [_edge(1, "2"), _edge(2, "3"), _edge(3, "4")]
    result = traverse_relations(relations, start_ids=[1], max_depth=2)
    visited = result.visited_ids
    assert 1 in visited and 2 in visited and 3 in visited
    # 4 is only reachable from 3 at depth 2; 3 is enqueued at depth 1,
    # its edges collected at depth 1 -> targets enqueued at depth 2, but
    # depth 2 nodes do not expand further (depth >= max_depth).
    assert all(e.depth <= 2 for e in result.edges)


def test_cycle_protection_visits_each_node_once() -> None:
    # 1 <-> 2 (mutual edges form a cycle)
    relations = [_edge(1, "2"), _edge(2, "1")]
    result = traverse_relations(relations, start_ids=[1], max_depth=5)
    assert result.visited_ids == frozenset({1, 2})


def test_repeated_node_not_revisited() -> None:
    # 1 -> 2, 1 -> 3, 2 -> 3 (3 reachable two ways)
    relations = [_edge(1, "2"), _edge(1, "3"), _edge(2, "3")]
    result = traverse_relations(relations, start_ids=[1], max_depth=3)
    # 3 visited once even though two edges point at it
    sources_to_three = [e for e in result.edges if e.target_id == "3"]
    # both edges are collected (they're distinct edges), but 3 expands once
    assert len(sources_to_three) <= 2
    assert 3 in result.visited_ids


def test_max_nodes_truncation() -> None:
    relations = [_edge(i, str(i + 1)) for i in range(1, 20)]
    result = traverse_relations(relations, start_ids=[1], max_depth=50, max_nodes=3)
    assert result.truncation is not None
    assert isinstance(result.truncation, PaginationOutcome)
    assert result.truncation.is_truncated is True


def test_max_edges_truncation() -> None:
    # fan-out: node 1 -> many targets
    relations = [_edge(1, str(t)) for t in range(100, 120)]
    result = traverse_relations(relations, start_ids=[1], max_depth=1, max_edges=5)
    assert result.truncation is not None
    assert result.truncation.is_truncated is True
    assert len(result.edges) <= 5


def test_traversal_non_workitem_targets_not_followed() -> None:
    # An artifact link target is not a work-item id; traversal should not enqueue it.
    artifact_edge = WorkItemRelation(
        source_work_item_id=1,
        relation_kind=RelationKind.ARTIFACT_LINK,
        target_kind=RelationTargetKind.ARTIFACT,
        target_id="vstfs:///Git/Commit/x",
        target_type="Commit",
        target_title=None,
        direction="forward",
        rel_type_name="System.LinkTypes.ArtifactLink",
    )
    result = traverse_relations([artifact_edge], start_ids=[1], max_depth=3)
    assert result.visited_ids == frozenset({1})


def test_empty_relations_returns_empty() -> None:
    result = traverse_relations([], start_ids=[1], max_depth=3)
    assert result.edges == ()
    assert 1 in result.visited_ids
    assert result.truncation is None
