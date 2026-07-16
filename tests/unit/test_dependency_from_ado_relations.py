"""ADF-W4.4 remainder: dependency_graph.py's AUTHORITATIVE_RELATION converter.

Direction convention verified against a real programs/acme/dependencies.yaml
entry: `from` = the item that finishes first (blocker/predecessor), `to` =
the item that is blocked (successor).
"""
from __future__ import annotations

from src.core.dependency_graph import (
    RelationItemContext,
    dependency_from_authoritative_relation,
    relations_to_dependencies,
)
from src.core.integration_types import RelationKind, RelationTargetKind, WorkItemRelation
from src.core.models_v2 import DependencyEvidenceTier, DependencyStatus, DependencyType


def _relation(
    *,
    source: int = 100,
    kind: RelationKind = RelationKind.PREDECESSOR,
    target_kind: RelationTargetKind = RelationTargetKind.WORK_ITEM,
    target_id: str = "200",
    rel_type_name: str = "System.LinkTypes.DependencyPredecessor",
) -> WorkItemRelation:
    return WorkItemRelation(
        source_work_item_id=source,
        relation_kind=kind,
        target_kind=target_kind,
        target_id=target_id,
        target_type="Task",
        target_title="Some item",
        direction="forward",
        rel_type_name=rel_type_name,
    )


def test_predecessor_relation_makes_target_the_blocker() -> None:
    """source=100 has a PREDECESSOR link to target=200: 200 must finish
    before 100, i.e. 200 blocks 100."""
    dep = dependency_from_authoritative_relation(_relation(source=100, kind=RelationKind.PREDECESSOR, target_id="200"), program_id="acme")
    assert dep is not None
    assert dep.from_item_id == 200  # blocker
    assert dep.to_item_id == 100  # blocked
    assert dep.dependency_type == DependencyType.BLOCKS
    assert dep.evidence_tier == DependencyEvidenceTier.AUTHORITATIVE_RELATION
    assert dep.status == DependencyStatus.ACTIVE


def test_successor_relation_makes_source_the_blocker() -> None:
    """source=400 has a SUCCESSOR link to target=100: 400 must finish
    before 100, i.e. 400 blocks 100."""
    dep = dependency_from_authoritative_relation(
        _relation(source=400, kind=RelationKind.SUCCESSOR, target_id="100", rel_type_name="System.LinkTypes.DependencySuccessor"),
        program_id="acme",
    )
    assert dep is not None
    assert dep.from_item_id == 400  # blocker
    assert dep.to_item_id == 100  # blocked


def test_hierarchy_relations_are_not_dependencies() -> None:
    dep = dependency_from_authoritative_relation(
        _relation(kind=RelationKind.HIERARCHY_PARENT, rel_type_name="System.LinkTypes.Hierarchy-Reverse"), program_id="acme"
    )
    assert dep is None


def test_related_and_artifact_and_external_and_unknown_are_not_dependencies() -> None:
    for kind, rel_name in (
        (RelationKind.RELATED, "System.LinkTypes.Related"),
        (RelationKind.ARTIFACT_LINK, "ArtifactLink"),
        (RelationKind.EXTERNAL_LINK, "Hyperlink"),
        (RelationKind.UNKNOWN, "Some.Unrecognized.Type"),
    ):
        assert dependency_from_authoritative_relation(_relation(kind=kind, rel_type_name=rel_name), program_id="acme") is None


def test_non_work_item_target_is_not_a_dependency() -> None:
    dep = dependency_from_authoritative_relation(
        _relation(target_kind=RelationTargetKind.ARTIFACT, target_id="vstfs:///Git/Commit/abc123"), program_id="acme"
    )
    assert dep is None


def test_non_numeric_target_id_is_not_a_dependency() -> None:
    dep = dependency_from_authoritative_relation(
        _relation(target_kind=RelationTargetKind.WORK_ITEM, target_id="not-a-number"), program_id="acme"
    )
    assert dep is None


def test_risk_if_broken_is_honestly_empty_not_guessed() -> None:
    dep = dependency_from_authoritative_relation(_relation(), program_id="acme")
    assert dep is not None
    assert dep.risk_if_broken == ""


def test_relations_to_dependencies_filters_and_batches() -> None:
    relations = [
        _relation(source=1, target_id="2"),
        _relation(source=1, kind=RelationKind.HIERARCHY_CHILD, target_id="3", rel_type_name="System.LinkTypes.Hierarchy-Forward"),
        _relation(source=5, kind=RelationKind.SUCCESSOR, target_id="6", rel_type_name="System.LinkTypes.DependencySuccessor"),
    ]
    deps = relations_to_dependencies(relations, program_id="acme")
    assert len(deps) == 2
    assert all(d.evidence_tier == DependencyEvidenceTier.AUTHORITATIVE_RELATION for d in deps)


def test_relations_to_dependencies_deduplicates_the_same_edge_reported_both_directions() -> None:
    """ADO commonly reports both sides of a dependency link: item 1's own
    PREDECESSOR entry naming item 2, AND item 2's own SUCCESSOR entry naming
    item 1. Both describe the identical edge (2 blocks 1) and must collapse
    to one Dependency, not two."""
    relations = [
        _relation(source=1, kind=RelationKind.PREDECESSOR, target_id="2", rel_type_name="System.LinkTypes.DependencyPredecessor"),
        _relation(source=2, kind=RelationKind.SUCCESSOR, target_id="1", rel_type_name="System.LinkTypes.DependencySuccessor"),
    ]
    deps = relations_to_dependencies(relations, program_id="acme")
    assert len(deps) == 1
    assert deps[0].from_item_id == 2
    assert deps[0].to_item_id == 1


def test_relations_to_dependencies_empty_input_returns_empty_tuple() -> None:
    assert relations_to_dependencies([], program_id="acme") == ()


def test_owner_and_workstream_resolve_from_the_blocker_not_the_blocked_item() -> None:
    """RELO-W2.7: owner_alias is the blocker's assignee (the accountable,
    escalation-actionable party), not the blocked item's -- and each side's
    workstream_id is resolved independently from item_lookup."""
    item_lookup = {
        200: RelationItemContext(owner_alias="blocker.alias", workstream_id="ws-blocker"),
        100: RelationItemContext(owner_alias="blocked.alias", workstream_id="ws-blocked"),
    }
    dep = dependency_from_authoritative_relation(
        _relation(source=100, kind=RelationKind.PREDECESSOR, target_id="200"),
        program_id="acme",
        item_lookup=item_lookup,
    )
    assert dep is not None
    assert dep.from_item_id == 200  # blocker
    assert dep.to_item_id == 100  # blocked
    assert dep.owner_alias == "blocker.alias"
    assert dep.from_workstream_id == "ws-blocker"
    assert dep.to_workstream_id == "ws-blocked"


def test_item_missing_from_lookup_resolves_to_honest_none() -> None:
    dep = dependency_from_authoritative_relation(
        _relation(source=100, kind=RelationKind.PREDECESSOR, target_id="200"),
        program_id="acme",
        item_lookup={200: RelationItemContext(owner_alias="blocker.alias", workstream_id="ws-blocker")},
    )
    assert dep is not None
    # source=100 (the blocked item) has no entry in item_lookup.
    assert dep.to_workstream_id is None


def test_omitted_item_lookup_preserves_pre_w2_7_all_none_behavior() -> None:
    dep = dependency_from_authoritative_relation(_relation(), program_id="acme")
    assert dep is not None
    assert dep.owner_alias is None
    assert dep.from_workstream_id is None
    assert dep.to_workstream_id is None


def test_relations_to_dependencies_threads_item_lookup_through_batch() -> None:
    item_lookup = {2: RelationItemContext(owner_alias="owner.two", workstream_id="ws-2")}
    deps = relations_to_dependencies(
        [_relation(source=1, target_id="2")], program_id="acme", item_lookup=item_lookup
    )
    assert len(deps) == 1
    assert deps[0].owner_alias == "owner.two"
    assert deps[0].from_workstream_id == "ws-2"
