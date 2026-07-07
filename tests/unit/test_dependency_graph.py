from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from src.core.dependency_graph import build_dependency_dag, compute_blast_radius, detect_cross_program_cascades, load_dependencies, load_inbound_cross_program_dependencies
from src.core.exceptions import ConfigError
from src.core.models import Confidence
from src.core.models_v2 import Dependency, DependencyStatus, DependencyType, Signal
from src.core.trajectory_analyzer import DriftPattern


def test_build_dependency_dag_returns_adjacency_list() -> None:
    dependencies = (
        _dependency("dep-a-b", from_workstream_id="alpha", to_workstream_id="beta"),
        _dependency("dep-b-c", from_workstream_id="beta", to_workstream_id="gamma"),
    )

    adjacency = build_dependency_dag(dependencies)

    assert adjacency == {
        "alpha": ["beta"],
        "beta": ["gamma"],
        "gamma": [],
    }


def test_build_dependency_dag_raises_on_cycle() -> None:
    dependencies = (
        _dependency("dep-a-b", from_workstream_id="alpha", to_workstream_id="beta"),
        _dependency("dep-b-a", from_workstream_id="beta", to_workstream_id="alpha"),
    )

    with pytest.raises(ConfigError, match="alpha -> beta -> alpha"):
        build_dependency_dag(dependencies)


def test_compute_blast_radius_supports_single_and_multi_hop() -> None:
    dependencies = (
        _dependency("dep-a-b", from_workstream_id="alpha", to_workstream_id="beta"),
        _dependency("dep-b-c", from_workstream_id="beta", to_workstream_id="gamma"),
        _dependency("dep-c-d", from_workstream_id="gamma", to_workstream_id="delta"),
    )

    single_hop = compute_blast_radius("alpha", dependencies, max_hops=1)
    multi_hop = compute_blast_radius("alpha", dependencies, max_hops=2)

    assert [dependency.id for dependency in single_hop] == ["dep-a-b"]
    assert [dependency.id for dependency in multi_hop] == ["dep-a-b", "dep-b-c"]


def test_load_dependencies_parses_cross_program_refs(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    dependency_path = programs_root / "acme" / "dependencies.yaml"
    dependency_path.parent.mkdir(parents=True, exist_ok=True)
    dependency_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "dependencies": [
                    {
                        "id": "dep-cross-program",
                        "from_milestone_id": "m3-code-complete",
                        "to_workstream_id": "fabrikam:buildouts",
                        "resolution_path": "cross_org_compute_pf",
                        "dependency_type": "informs",
                        "risk_if_broken": "Fabrikam buildout sequencing stays provisional.",
                        "status": "active",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    dependencies = load_dependencies("acme", programs_root=programs_root)

    assert len(dependencies) == 1
    dependency = dependencies[0]
    assert dependency.from_program_id == "acme"
    assert dependency.from_milestone_id == "m3-code-complete"
    assert dependency.to_program_id == "fabrikam"
    assert dependency.to_workstream_id == "buildouts"
    assert dependency.resolution_path == "cross_org_compute_pf"


def test_load_dependencies_falls_back_to_legacy_key_dependencies(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_path = programs_root / "acme" / "program.yaml"
    program_path.parent.mkdir(parents=True, exist_ok=True)
    program_path.write_text(
        yaml.safe_dump(
            {
                "key_dependencies": [
                    {
                        "from_item": "BIOS compliance",
                        "to_item": "Fleet readiness",
                        "impact": "Fleet readiness stays blocked until BIOS compliance closes.",
                    }
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    dependencies = load_dependencies("acme", programs_root=programs_root)

    assert len(dependencies) == 1
    dependency = dependencies[0]
    assert dependency.id == "legacy-acme-1"
    assert dependency.from_program_id == "acme"
    assert dependency.from_workstream_id == "BIOS compliance"
    assert dependency.to_workstream_id == "Fleet readiness"
    assert dependency.dependency_type == DependencyType.BLOCKS
    assert dependency.risk_if_broken == "Fleet readiness stays blocked until BIOS compliance closes."


def test_load_dependencies_includes_shared_root_registry_entries_for_program(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    shared_path = programs_root / "dependencies.yaml"
    shared_path.parent.mkdir(parents=True, exist_ok=True)
    shared_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "dependencies": [
                    {
                        "id": "dep-shared-acme-fabrikam",
                        "from_workstream_id": "acme:networking",
                        "to_workstream_id": "fabrikam:buildouts",
                        "dependency_type": "blocks",
                        "risk_if_broken": "Fabrikam buildouts slip.",
                        "status": "active",
                    },
                    {
                        "id": "dep-shared-fabrikam-acme",
                        "from_workstream_id": "fabrikam:capacity",
                        "to_workstream_id": "acme:release",
                        "dependency_type": "informs",
                        "risk_if_broken": "Acme readiness stays provisional.",
                        "status": "active",
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    local_path = programs_root / "acme" / "dependencies.yaml"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "dependencies": [
                    {
                        "id": "dep-local",
                        "from_workstream_id": "release",
                        "to_workstream_id": "quality",
                        "dependency_type": "blocks",
                        "risk_if_broken": "Local release path slips.",
                        "status": "active",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    dependencies = load_dependencies("acme", programs_root=programs_root)

    assert [dependency.id for dependency in dependencies] == ["dep-local", "dep-shared-acme-fabrikam"]


def test_load_inbound_cross_program_dependencies_includes_shared_registry_entries(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    shared_path = programs_root / "dependencies.yaml"
    shared_path.parent.mkdir(parents=True, exist_ok=True)
    shared_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "dependencies": [
                    {
                        "id": "dep-shared-fabrikam-acme",
                        "from_workstream_id": "fabrikam:capacity",
                        "to_workstream_id": "acme:release",
                        "dependency_type": "informs",
                        "risk_if_broken": "Acme readiness stays provisional.",
                        "status": "active",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    armada_program = programs_root / "fabrikam" / "program.yaml"
    armada_program.parent.mkdir(parents=True, exist_ok=True)
    armada_program.write_text("schema_version: '3.0'\nid: fabrikam\nname: Fabrikam\n", encoding="utf-8")

    inbound = load_inbound_cross_program_dependencies("acme", programs_root=programs_root)

    assert len(inbound) == 1
    assert inbound[0].id == "dep-shared-fabrikam-acme"
    assert inbound[0].from_program_id == "fabrikam"
    assert inbound[0].to_program_id == "acme"


def test_load_shared_registry_requires_explicit_program_scoping(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    shared_path = programs_root / "dependencies.yaml"
    shared_path.parent.mkdir(parents=True, exist_ok=True)
    shared_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "dependencies": [
                    {
                        "id": "dep-bad-shared",
                        "from_workstream_id": "networking",
                        "to_workstream_id": "fabrikam:buildouts",
                        "dependency_type": "blocks",
                        "risk_if_broken": "Shared ref is ambiguous.",
                        "status": "active",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="requires explicit program scoping"):
        load_dependencies("acme", programs_root=programs_root)


def test_load_shared_registry_rejects_same_program_edges(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    shared_path = programs_root / "dependencies.yaml"
    shared_path.parent.mkdir(parents=True, exist_ok=True)
    shared_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "dependencies": [
                    {
                        "id": "dep-bad-same-program",
                        "from_workstream_id": "acme:networking",
                        "to_workstream_id": "acme:release",
                        "dependency_type": "blocks",
                        "risk_if_broken": "This belongs in the local registry.",
                        "status": "active",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="only supports cross-program edges"):
        load_dependencies("acme", programs_root=programs_root)


def test_load_dependencies_rejects_duplicate_ids_across_local_and_shared_registries(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    shared_path = programs_root / "dependencies.yaml"
    shared_path.parent.mkdir(parents=True, exist_ok=True)
    shared_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "dependencies": [
                    {
                        "id": "dep-duplicate",
                        "from_workstream_id": "acme:networking",
                        "to_workstream_id": "fabrikam:buildouts",
                        "dependency_type": "blocks",
                        "risk_if_broken": "Fabrikam buildouts slip.",
                        "status": "active",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    local_path = programs_root / "acme" / "dependencies.yaml"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "dependencies": [
                    {
                        "id": "dep-duplicate",
                        "from_workstream_id": "networking",
                        "to_workstream_id": "quality",
                        "dependency_type": "blocks",
                        "risk_if_broken": "Local release path slips.",
                        "status": "active",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="Duplicate dependency id 'dep-duplicate'"):
        load_dependencies("acme", programs_root=programs_root)


def test_detect_cross_program_cascades_returns_downstream_impact() -> None:
    dependencies = (
        _dependency(
            "dep-cross-program",
            from_program_id="acme",
            from_workstream_id="networking",
            to_program_id="fabrikam",
            to_workstream_id="buildouts",
            dependency_type=DependencyType.INFORMS,
            risk_if_broken="Fabrikam buildout scheduling stays provisional until networking parity closes.",
        ),
    )

    cascades = detect_cross_program_cascades(
        signals=(
            Signal(
                id="sig-1",
                timestamp=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
                source="manual",
                program_id="acme",
                workstream_id="acme",
                entity_refs=("networking",),
                text="Networking parity remains blocked this week.",
                raw_ref=None,
                confidence=Confidence.HIGH,
                metadata=None,
            ),
        ),
        drift_patterns=(
            DriftPattern(
                work_item_id=900001,
                pattern="eta_drift",
                severity="high",
                detail="Target date slipped twice in the last 60 days.",
                occurrences=2,
                window_days=60,
            ),
        ),
        dependencies=dependencies,
    )

    assert len(cascades) == 1
    cascade = cascades[0]
    assert cascade.source_item == "networking"
    assert cascade.target_item == "fabrikam:buildouts"
    assert cascade.target_workstream_ids == ("fabrikam:buildouts",)


def _dependency(
    dependency_id: str,
    *,
    from_program_id: str = "acme",
    from_workstream_id: str | None = None,
    from_item_id: int | None = None,
    from_milestone_id: str | None = None,
    to_program_id: str = "acme",
    to_workstream_id: str | None = None,
    to_item_id: int | None = None,
    to_milestone_id: str | None = None,
    dependency_type: DependencyType = DependencyType.BLOCKS,
    risk_if_broken: str = "Downstream execution slips.",
    resolution_path: str | None = None,
) -> Dependency:
    return Dependency(
        id=dependency_id,
        from_program_id=from_program_id,
        from_workstream_id=from_workstream_id,
        from_item_id=from_item_id,
        from_milestone_id=from_milestone_id,
        to_program_id=to_program_id,
        to_workstream_id=to_workstream_id,
        to_item_id=to_item_id,
        to_milestone_id=to_milestone_id,
        dependency_type=dependency_type,
        risk_if_broken=risk_if_broken,
        mitigation=None,
        status=DependencyStatus.ACTIVE,
        owner_alias=None,
        resolution_path=resolution_path,
    )