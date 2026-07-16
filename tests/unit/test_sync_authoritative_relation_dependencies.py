"""ADF-W4.4 (Section 8.10.3): gather-time wiring of typed ADO relations into
AUTHORITATIVE_RELATION dependencies.

Verifies the fact-store-only sync (no YAML rewrite), idempotency across
gathers, the scoped closure (relation-derived facts close when the relation
disappears; authored deps are never touched), and that the
AUTHORITATIVE_RELATION evidence tier round-trips through the fact store.
"""
from __future__ import annotations

from pathlib import Path

from src.core.dependency_graph import (
    save_dependencies,
    sync_authoritative_relation_dependencies,
)
from src.core.integration_types import RelationKind, RelationTargetKind, WorkItemRelation
from src.core.models_v2 import (
    Dependency,
    DependencyEvidenceTier,
    DependencyStatus,
    DependencyType,
)
from src.core.program_fact_store import (
    ProgramFactStore,
    load_program_facts,
    project_dependencies,
)


def _relation(
    *,
    source: int = 100,
    kind: RelationKind = RelationKind.PREDECESSOR,
    target_id: str = "200",
) -> WorkItemRelation:
    return WorkItemRelation(
        source_work_item_id=source,
        relation_kind=kind,
        target_kind=RelationTargetKind.WORK_ITEM,
        target_id=target_id,
        target_type="Task",
        target_title="Some item",
        direction="forward",
        rel_type_name="System.LinkTypes.DependencyPredecessor",
    )


def _programs_root(tmp_path: Path) -> Path:
    # ProgramFactStore resolves db_root as programs_root.parent by convention;
    # put the program under a programs/ subdir so the fact store lands in tmp.
    programs_root = tmp_path / "programs"
    (programs_root / "xpf").mkdir(parents=True)
    return programs_root


def test_sync_appends_authoritative_relation_dependency_facts(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    deps = sync_authoritative_relation_dependencies(
        "xpf", [_relation(source=100, target_id="200")], programs_root=programs_root,
    )
    assert len(deps) == 1
    snapshot = load_program_facts("xpf", db_root=tmp_path, programs_root=programs_root)
    projected = project_dependencies(snapshot)
    assert len(projected) == 1
    dep = projected[0]
    assert dep.evidence_tier == DependencyEvidenceTier.AUTHORITATIVE_RELATION
    assert dep.evidence_refs == ("System.LinkTypes.DependencyPredecessor",)
    assert dep.from_item_id == 200  # blocker
    assert dep.to_item_id == 100  # blocked


def test_sync_is_idempotent_across_gathers(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    relations = [_relation(source=100, target_id="200")]
    sync_authoritative_relation_dependencies("xpf", relations, programs_root=programs_root)
    # Second gather with the same relations -> one fact, not two.
    sync_authoritative_relation_dependencies("xpf", relations, programs_root=programs_root)
    snapshot = load_program_facts("xpf", db_root=tmp_path, programs_root=programs_root)
    assert len(project_dependencies(snapshot)) == 1


def test_sync_dedupes_mirrored_predecessor_successor_edge(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    # ADO commonly emits both directions of one edge.
    relations = [
        _relation(source=100, kind=RelationKind.PREDECESSOR, target_id="200"),
        _relation(source=200, kind=RelationKind.SUCCESSOR, target_id="100"),
    ]
    deps = sync_authoritative_relation_dependencies("xpf", relations, programs_root=programs_root)
    assert len(deps) == 1  # one edge, not two


def test_disappeared_relation_is_closed_but_authored_dep_survives(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    # Seed an authored dependency via the canonical writer.
    authored = Dependency(
        id="human-authored-1",
        from_program_id="xpf",
        from_workstream_id=None,
        from_item_id=300,
        from_milestone_id=None,
        to_program_id="xpf",
        to_workstream_id=None,
        to_item_id=400,
        to_milestone_id=None,
        dependency_type=DependencyType.BLOCKS,
        risk_if_broken="Authored dep risk.",
        mitigation=None,
        status=DependencyStatus.ACTIVE,
        owner_alias=None,
    )
    save_dependencies("xpf", (authored,), programs_root=programs_root)

    # Gather 1: one relation-derived dep alongside the authored one.
    sync_authoritative_relation_dependencies(
        "xpf", [_relation(source=100, target_id="200")], programs_root=programs_root,
    )
    snapshot = load_program_facts("xpf", db_root=tmp_path, programs_root=programs_root)
    assert len(project_dependencies(snapshot)) == 2

    # Gather 2: relation disappears. Authored dep must survive; relation dep closes.
    sync_authoritative_relation_dependencies("xpf", [], programs_root=programs_root)
    snapshot = load_program_facts("xpf", db_root=tmp_path, programs_root=programs_root)
    projected = project_dependencies(snapshot)
    assert len(projected) == 1
    assert projected[0].id == "human-authored-1"


def test_scope_item_ids_none_preserves_full_close_behavior(tmp_path: Path) -> None:
    # ADF-W2.2: the pre-existing default -- no scope means "treat this fetch
    # as full," so a disappeared relation still closes exactly as before.
    programs_root = _programs_root(tmp_path)
    sync_authoritative_relation_dependencies(
        "xpf", [_relation(source=100, target_id="200")], programs_root=programs_root,
    )
    sync_authoritative_relation_dependencies("xpf", [], scope_item_ids=None, programs_root=programs_root)
    snapshot = load_program_facts("xpf", db_root=tmp_path, programs_root=programs_root)
    assert project_dependencies(snapshot) == ()


def test_out_of_scope_relation_is_left_untouched_when_disappeared(tmp_path: Path) -> None:
    # ADF-W2.2: neither endpoint (100/200) of this relation was queried this
    # cycle (scope only covers item 999) -- an incremental fetch has no fresh
    # information about it, so it must NOT be closed even though it's absent
    # from this cycle's (necessarily partial) relations argument.
    programs_root = _programs_root(tmp_path)
    sync_authoritative_relation_dependencies(
        "xpf", [_relation(source=100, target_id="200")], programs_root=programs_root,
    )
    sync_authoritative_relation_dependencies(
        "xpf", [], scope_item_ids=frozenset({999}), programs_root=programs_root,
    )
    snapshot = load_program_facts("xpf", db_root=tmp_path, programs_root=programs_root)
    projected = project_dependencies(snapshot)
    assert len(projected) == 1
    assert projected[0].from_item_id == 200
    assert projected[0].to_item_id == 100


def test_in_scope_relation_is_closed_when_disappeared(tmp_path: Path) -> None:
    # ADF-W2.2: item 100 (one endpoint) WAS queried this cycle and no longer
    # reports the relation -- real evidence of removal, so it must close even
    # though item 200 (the other endpoint) was out of scope.
    programs_root = _programs_root(tmp_path)
    sync_authoritative_relation_dependencies(
        "xpf", [_relation(source=100, target_id="200")], programs_root=programs_root,
    )
    sync_authoritative_relation_dependencies(
        "xpf", [], scope_item_ids=frozenset({100}), programs_root=programs_root,
    )
    snapshot = load_program_facts("xpf", db_root=tmp_path, programs_root=programs_root)
    assert project_dependencies(snapshot) == ()


def test_no_relations_is_clean_noop_for_first_gather(tmp_path: Path) -> None:
    programs_root = _programs_root(tmp_path)
    deps = sync_authoritative_relation_dependencies("xpf", [], programs_root=programs_root)
    assert deps == ()
    snapshot = load_program_facts("xpf", db_root=tmp_path, programs_root=programs_root)
    assert project_dependencies(snapshot) == ()


def test_evidence_tier_defaults_to_authored_for_legacy_payloads(tmp_path: Path) -> None:
    """A dependency fact persisted WITHOUT evidence_tier (legacy, pre-W4.4)
    must project to AUTHORED, not crash -- the tolerant-reader contract."""
    programs_root = _programs_root(tmp_path)
    store = ProgramFactStore("xpf", db_root=tmp_path)
    # Hand-construct a legacy payload missing evidence_tier/evidence_refs.
    legacy_payload = {
        "id": "legacy-1",
        "from_program_id": "xpf",
        "from_workstream_id": None,
        "from_item_id": 10,
        "from_milestone_id": None,
        "to_program_id": "xpf",
        "to_workstream_id": None,
        "to_item_id": 20,
        "to_milestone_id": None,
        "dependency_type": "blocks",
        "risk_if_broken": "legacy risk",
        "mitigation": None,
        "status": "active",
        "owner_alias": None,
        "resolution_path": None,
        "planned_resolution_date": None,
        "schedule_status": None,
        "linked_risk_ids": [],
    }
    from src.core.program_fact_store import (
        FactLifecycleState,
        FactPrecedence,
        ProgramFactInput,
        build_natural_key,
    )
    entity_refs = ("DEPENDENCY:legacy-1",)
    store.append_fact(
        ProgramFactInput(
            fact_type="dependency.link",
            scope="program",
            entity_refs=entity_refs,
            payload=legacy_payload,
            precedence=FactPrecedence.ACTIVE_PM_JUDGMENT,
            natural_key=build_natural_key("dependency.link", entity_refs=entity_refs, scope="program"),
            created_by="test",
        ),
    )
    snapshot = load_program_facts("xpf", db_root=tmp_path, programs_root=programs_root)
    projected = project_dependencies(snapshot)
    assert len(projected) == 1
    assert projected[0].evidence_tier == DependencyEvidenceTier.AUTHORED
    assert projected[0].evidence_refs == ()
